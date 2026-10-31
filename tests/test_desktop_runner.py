import threading
import time

from telegram.error import BadRequest, InvalidToken, NetworkError, TimedOut

from bot.runner import BotRunner, Status, is_network_error


class FakeUpdater:
    def __init__(self, events):
        self.events = events
        self.running = False

    async def start_polling(self, **kwargs):
        self.events.append("start_polling")
        self.running = True

    async def stop(self):
        self.events.append("updater.stop")
        self.running = False


class FakeApplication:
    """Thay telegram.ext.Application: ghi lại thứ tự gọi; initialize có thể lỗi vài lần đầu."""

    def __init__(self, events, fail_with=None):
        self.events = events
        self.fail_with = fail_with
        self.updater = FakeUpdater(events)
        self.running = False
        self.bot_data = {}

    async def initialize(self):
        self.events.append("initialize")
        if self.fail_with:
            raise self.fail_with

    async def start(self):
        self.events.append("start")
        self.running = True

    async def stop(self):
        self.events.append("stop")
        self.running = False

    async def shutdown(self):
        self.events.append("shutdown")


def make_runner(events, failures=()):
    failures = list(failures)
    statuses = []
    changed = threading.Condition()

    def on_status(status, detail):
        with changed:
            statuses.append((status, detail))
            changed.notify_all()

    def build(config):
        events.append("build")
        return FakeApplication(events, failures.pop(0) if failures else None)

    async def post_init(app):
        events.append("post_init")

    runner = BotRunner(on_status, build=build, post_init=post_init, first_retry_delay=0.01, max_retry_delay=0.02)

    def wait_for(status, timeout=5):
        deadline = time.monotonic() + timeout
        with changed:
            while not any(s is status for s, _ in statuses):
                left = deadline - time.monotonic()
                assert left > 0, f"chưa tới trạng thái {status}: {statuses}"
                changed.wait(left)

    return runner, statuses, wait_for


def test_start_and_stop_in_order():
    events = []
    runner, statuses, wait_for = make_runner(events)
    runner.start(config=None)
    wait_for(Status.RUNNING)
    assert runner.running
    runner.stop()
    assert not runner.running
    assert events == [
        "build", "initialize", "post_init", "start_polling", "start",
        "updater.stop", "stop", "shutdown",
    ]
    assert [s for s, _ in statuses] == [Status.STARTING, Status.RUNNING, Status.STOPPED]
    # Bật lại được sau khi tắt
    runner.start(config=None)
    wait_for(Status.RUNNING)
    runner.stop()


def test_waits_for_network_then_retries():
    events = []
    runner, statuses, wait_for = make_runner(events, failures=[NetworkError("offline"), TimedOut()])
    runner.start(config=None)
    wait_for(Status.RUNNING)
    runner.stop()
    assert events.count("build") == 3
    assert events.count("shutdown") == 3  # lần lỗi cũng được dọn
    assert [s for s, _ in statuses][:4] == [
        Status.STARTING, Status.WAITING_NETWORK, Status.STARTING, Status.WAITING_NETWORK,
    ]


def test_non_network_error_stops_with_error():
    events = []
    runner, statuses, wait_for = make_runner(events, failures=[InvalidToken("bad")])
    runner.start(config=None)
    wait_for(Status.ERROR)
    runner.stop()
    assert events.count("build") == 1
    status, detail = statuses[-1]
    assert status is Status.ERROR and "Telegram bot token không đúng" in detail


def test_stop_while_waiting_for_network():
    events = []
    runner, statuses, wait_for = make_runner(events, failures=[NetworkError("offline")] * 100)
    runner.max_retry_delay = runner.first_retry_delay = 30
    runner.start(config=None)
    wait_for(Status.WAITING_NETWORK)
    started = time.monotonic()
    runner.stop()
    assert time.monotonic() - started < 5
    assert statuses[-1][0] is Status.STOPPED and not runner.running


def test_is_network_error():
    assert is_network_error(NetworkError("x"))
    assert is_network_error(TimedOut())
    assert not is_network_error(BadRequest("x"))  # BadRequest là lớp con của NetworkError
    assert not is_network_error(InvalidToken("x"))
    assert not is_network_error(ValueError("x"))
    try:
        try:
            raise ConnectionRefusedError()
        except OSError as e:
            raise RuntimeError("bọc lại") from e
    except RuntimeError as wrapped:
        assert is_network_error(wrapped)

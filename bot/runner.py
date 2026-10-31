"""Chạy bot trong một thread riêng có asyncio loop riêng (dùng cho app desktop).

Tách khỏi loop giao diện: lời gọi chặn của bot (Google Sheet, SQLite) không làm treo cửa sổ, và bật /
tắt bot không cần thoát app. Khi khởi động máy, mạng thường chưa sẵn sàng: lỗi mạng lúc kết nối thì
thử lại với thời gian chờ tăng dần (tối đa MAX_RETRY_DELAY giây), lỗi khác (sai token...) thì dừng.
"""
import asyncio
import logging
import threading
import time
from collections.abc import Callable
from enum import Enum

from telegram import Update
from telegram.error import BadRequest, NetworkError

from .checks import describe_error
from .config import Config

log = logging.getLogger("bot.runner")


class Status(Enum):
    STARTING = "Đang khởi động"
    RUNNING = "Đang chạy"
    WAITING_NETWORK = "Đang chờ mạng"
    ERROR = "Lỗi"
    STOPPED = "Đã dừng"


FIRST_RETRY_DELAY = 5.0
MAX_RETRY_DELAY = 300.0
# Polling báo lỗi mạng trong khoảng này thì coi là đang mất mạng
NETWORK_ERROR_WINDOW = 60.0
STATUS_POLL_SECONDS = 5.0


def is_network_error(e: BaseException) -> bool:
    """Lỗi do mất mạng / chưa có mạng (đáng chờ rồi thử lại), xét cả chuỗi nguyên nhân."""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, NetworkError) and not isinstance(e, BadRequest):
            return True
        if isinstance(e, (ConnectionError, TimeoutError)) or type(e).__name__ in (
            "ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "TransportError",  # requests, google-auth
            "gaierror",
        ):
            return True
        e = e.__cause__ or e.__context__
    return False


def _default_build(config: Config):
    from .main import build_application

    return build_application(config)


async def _default_post_init(app) -> None:
    from .main import set_bot_commands

    await set_bot_commands(app)


class BotRunner:
    """Bật / tắt bot. `on_status(status, detail)` được gọi từ thread của bot."""

    def __init__(
        self,
        on_status: Callable[[Status, str], None] = lambda status, detail: None,
        build: Callable[[Config], object] = _default_build,
        post_init=_default_post_init,
        first_retry_delay: float = FIRST_RETRY_DELAY,
        max_retry_delay: float = MAX_RETRY_DELAY,
    ):
        self.on_status = on_status
        self.build = build
        self.post_init = post_init
        self.first_retry_delay = first_retry_delay
        self.max_retry_delay = max_retry_delay
        self.status = Status.STOPPED
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._last_poll_error = 0.0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: Config) -> None:
        if self.running:
            raise RuntimeError("Bot đang chạy")
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, args=(config,), name="bot", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self, timeout: float = 30) -> None:
        """Dừng bot và chờ thread kết thúc (tối đa `timeout` giây)."""
        thread, loop, stop = self._thread, self._loop, self._stop
        if thread is None:
            return
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:  # loop vừa đóng
                pass
        thread.join(timeout)
        if thread.is_alive():
            log.warning("Bot chưa dừng hẳn sau %s giây", timeout)
        else:
            self._thread = None

    def _set_status(self, status: Status, detail: str = "") -> None:
        if status is self.status and not detail:
            return
        self.status = status
        try:
            self.on_status(status, detail)
        except Exception:
            log.exception("Lỗi trong callback trạng thái")

    def _run(self, config: Config) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main(config))
        except Exception as e:
            log.exception("Bot dừng vì lỗi")
            self._set_status(Status.ERROR, describe_error(e))
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None
            self._ready.set()

    async def _wait_stop(self, seconds: float) -> bool:
        """Chờ tối đa `seconds` giây; True nếu đã được yêu cầu dừng."""
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        return self._stop.is_set()

    def _on_poll_error(self, e: Exception) -> None:
        if is_network_error(e):
            self._last_poll_error = time.monotonic()
        else:
            log.error("Lỗi khi nhận tin từ Telegram: %s", e)

    async def _connect(self, config: Config):
        """Tạo Application và kết nối Telegram; lỗi mạng thì chờ rồi thử lại. None nếu bị dừng."""
        delay = self.first_retry_delay
        while True:
            self._set_status(Status.STARTING)
            app = None
            try:
                # build mở Google Sheet (lời gọi chặn, cần mạng)
                app = await asyncio.to_thread(self.build, config)
                await app.initialize()
                await self.post_init(app)
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES, bootstrap_retries=0, error_callback=self._on_poll_error
                )
                await app.start()
                return app
            except Exception as e:
                if app is not None:
                    await self._shutdown(app)
                if not is_network_error(e):
                    raise
                log.warning("Chưa kết nối được (%s), thử lại sau %.0f giây", e, delay)
                self._set_status(Status.WAITING_NETWORK)
                if await self._wait_stop(delay):
                    return None
                delay = min(delay * 2, self.max_retry_delay)

    @staticmethod
    async def _shutdown(app) -> None:
        """Dừng theo thứ tự ngược lúc khởi động; bỏ qua bước chưa chạy."""
        for step in (
            lambda: app.updater.stop() if app.updater and app.updater.running else None,
            lambda: app.stop() if app.running else None,
            app.shutdown,
            # Đóng DB để thư mục dữ liệu xoá được (Windows không cho xoá tệp đang mở)
            lambda: app.bot_data["state"].storage.close() if "state" in app.bot_data else None,
        ):
            try:
                coro = step()
                if coro is not None:
                    await coro
            except Exception:
                log.warning("Lỗi khi dừng bot", exc_info=True)

    async def _main(self, config: Config) -> None:
        self._stop = asyncio.Event()
        self._ready.set()
        try:
            app = await self._connect(config)
        except Exception as e:
            log.exception("Không khởi động được bot")
            self._set_status(Status.ERROR, describe_error(e))
            return
        if app is None:
            self._set_status(Status.STOPPED)
            return
        log.info("Bot đang chạy")
        self._set_status(Status.RUNNING)
        try:
            while not await self._wait_stop(STATUS_POLL_SECONDS):
                offline = time.monotonic() - self._last_poll_error < NETWORK_ERROR_WINDOW
                self._set_status(Status.WAITING_NETWORK if offline else Status.RUNNING)
        finally:
            await self._shutdown(app)
            log.info("Bot đã dừng")
            self._set_status(Status.STOPPED)

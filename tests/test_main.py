import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.main import do_tasks, on_startup, sent_while_offline, split_message
from bot.sheets import HEADER
from tests.test_sheets import FakeWorksheet
from bot.storage import DaySummary, Storage

STARTED = datetime(2026, 10, 29, 4, 30, 0, tzinfo=timezone.utc)


def test_message_sent_before_start_is_offline():
    assert sent_while_offline(STARTED - timedelta(minutes=5), STARTED)
    assert sent_while_offline(STARTED - timedelta(seconds=1), STARTED)


def test_startup_finalizes_past_days_and_processes_today_pending():
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    s = Storage(":memory:")
    for chat, day, mid in [(-1, "2026-10-28", 1), (-1, "2026-10-29", 2), (-2, "2026-10-29", 3)]:
        s.add_message(chat, mid, 1, "A", "x", None, datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=tz))
    # Nhóm -2: tin hôm nay đã xử lý hết -> không gọi lại
    s.save_summary(DaySummary(-2, "2026-10-29", "ok", 3, False))
    s.record_task(-2, 1, "2026-10-29", created=True)  # nhóm -2 có task trên sheet
    calls = []

    async def compact(chat_id, day, finalize, retry=True, keep_recent=True):
        calls.append((chat_id, day, finalize, keep_recent))

    async def review(chat_id):
        calls.append(("review", chat_id))

    st = SimpleNamespace(storage=s, busy=set(), today=lambda: "2026-10-29", compact=compact, review=review, userbot=None)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"state": st}))
    asyncio.run(on_startup(context))
    assert calls == [
        (-1, "2026-10-28", True, True),    # chốt bù hôm qua
        (-1, "2026-10-29", False, False),  # xử lý hết tin hôm nay, không chốt ngày
        ("review", -2),                    # rà soát sheet các nhóm có task
    ]


def test_message_sent_after_start_is_answered():
    # Telegram làm tròn ngày giờ tin nhắn xuống giây: tin gửi cùng giây khởi động vẫn được trả lời
    assert not sent_while_offline(STARTED, STARTED)
    assert not sent_while_offline(STARTED + timedelta(seconds=3), STARTED)


def test_split_message_respects_limit():
    lines = ["a" * 30] * 10
    chunks = split_message(lines, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines


class Replies:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append(text)
        self.markups = getattr(self, "markups", []) + [reply_markup]
        return SimpleNamespace(message_id=100 + len(self.sent))


def run_task_command(rows, args, pending_today=False):
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    s, ws = Storage(":memory:"), FakeWorksheet()
    ws.rows = [list(HEADER)] + rows
    if pending_today:
        s.add_message(-1, 1, 1, "A", "Hà làm slide nhé", None, datetime(2026, 10, 29, 9, tzinfo=tz))
    from bot.sheets import TaskSheet

    msg = Replies()
    msg.date = datetime(2026, 10, 29, 3, tzinfo=timezone.utc)
    st = SimpleNamespace(
        storage=s, started_at=datetime(2026, 10, 29, 2, tzinfo=timezone.utc), today=lambda: "2026-10-29",
        task_sheet=lambda chat_id: TaskSheet(ws, s, chat_id),
    )
    update = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=-1))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"state": st}), bot=SimpleNamespace(username="ant_bot"))
    asyncio.run(do_tasks(update, context, " ".join(args)))
    return "\n".join(msg.sent)


TASK_ROWS = [
    ["2", "Lỗi đăng nhập", "Minh", "Kiểm tra lỗi đăng nhập", "50%", "2026-10-30"],
    ["1", "Báo cáo Q3", "Lan", "Làm báo cáo doanh thu Q3", "Mới giao", "2026-11-02"],
    ["3", "Họp nhanh cả team", "Tất cả", "Cả team họp lúc 3h", "", "2026-10-29"],
    ["4", "Làm báo cáo Q3", "", "Trùng với task #1", "", ""],
    ["5", "Slide demo", "Lan, Trang", "Làm slide demo", "", ""],
]


def test_task_command_lists_all_real_tasks_sorted():
    out = run_task_command(TASK_ROWS, [])
    assert out.startswith("Tất cả task của nhóm (3):")
    assert out.index("#1. Báo cáo Q3") < out.index("#2. Lỗi đăng nhập") < out.index("#5. Slide demo")
    assert "Họp nhanh" not in out and "Trùng với" not in out  # bỏ dòng "Tất cả" và dòng trùng


def test_task_command_filters_by_assignee_case_insensitive():
    out = run_task_command(TASK_ROWS, ["lan"])
    assert out.startswith("Task của lan (2):")
    assert "#1. Báo cáo Q3" in out and "#5. Slide demo" in out and "Lỗi đăng nhập" not in out


def test_task_command_hints_unprocessed_messages():
    assert "Gõ @ant_bot <summarize> để cập nhật" in run_task_command(TASK_ROWS, [], pending_today=True)
    assert "<summarize>" not in run_task_command(TASK_ROWS, [])




# ---- tin tag bot trong nhóm ----

from bot.main import (
    command_argument, mention_help, on_group_message, remember_chat, route_mention, strip_mention, OFFLINE_REPLY,
)


def test_command_argument_keeps_text_after_command():
    assert command_argument("/g @team hạn khi nào?") == "@team hạn khi nào?"
    assert command_argument("/g@ant_bot  dòng 1\ndòng 2") == "dòng 1\ndòng 2"
    assert command_argument("/g") == ""


class FakeMsg(Replies):
    ids = iter(range(1, 10**6))

    def __init__(self, text, date, mentions=(), user_id=1, reply_to=None, chat_id=-1):
        super().__init__()
        self.text, self.caption, self.date, self.chat_id = text, None, date, chat_id
        self.message_id, self.from_user = next(FakeMsg.ids), SimpleNamespace(id=user_id, full_name="An")
        self.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to else None
        self._mentions = {i: m for i, m in enumerate(mentions)}

    def parse_entities(self, types):
        return self._mentions


class RecordingApp:
    def __init__(self, st):
        self.bot_data = {"state": st}
        self.tasks = []

    def create_task(self, coro):
        self.tasks.append(coro)


ONLINE = datetime(2026, 10, 29, 3, tzinfo=timezone.utc)
OFFLINE = datetime(2026, 10, 29, 1, tzinfo=timezone.utc)


def make_state(userbot=None):
    return SimpleNamespace(
        storage=Storage(":memory:"), started_at=datetime(2026, 10, 29, 2, tzinfo=timezone.utc),
        config=SimpleNamespace(timezone=ZoneInfo("Asia/Ho_Chi_Minh"), compact_threshold_chars=60000,
                               userbot_history_days=7),
        busy=set(), today=lambda: "2026-10-29", userbot=userbot, pending_edits={}, pending_picks={}, session=None,
    )


def deliver(st, handler, msg, edited=False):
    """Gửi một tin tới handler, chạy các tác vụ nền; trả về [(loại, tham số)] các yêu cầu đã chuyển đi."""
    calls = []

    async def fake_answer(update, context, question):
        calls.append(("ask", question))

    async def fake_summary(update, context, day):
        calls.append(("summary", day))

    async def fake_tasks(update, context, who):
        calls.append(("tasks", who))

    import bot.main as m
    saved = m.answer_question, m.do_summary, m.do_tasks
    m.answer_question, m.do_summary, m.do_tasks = fake_answer, fake_summary, fake_tasks
    try:
        app = RecordingApp(st)
        update = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=-1, title="A"),
                                 edited_message=msg if edited else None)
        context = SimpleNamespace(application=app, bot=SimpleNamespace(username="ant_bot"))
        asyncio.run(handler(update, context))
        for coro in app.tasks:
            asyncio.run(coro)
    finally:
        m.answer_question, m.do_summary, m.do_tasks = saved
    return calls


def stored(st):
    return st.storage.conn.execute("select count(*) from messages").fetchone()[0]


def test_route_mention():
    assert route_mention("<summarize>") == ("summary", "")
    assert route_mention("<summarize> 2026-11-01") == ("summary", "2026-11-01")
    assert route_mention("<SUMMARIZE>") == ("summary", "")
    assert route_mention("<task>") == ("tasks", "")
    assert route_mention("<task> Lan") == ("tasks", "Lan")
    assert route_mention("<Task>  Lan Anh") == ("tasks", "Lan Anh")
    # Từ khoá không có ngoặc nhọn là câu hỏi
    assert route_mention("task Q3 hạn khi nào?") == ("ask", "task Q3 hạn khi nào?")
    assert route_mention("dời hạn task Q3 sang 10/11") == ("ask", "dời hạn task Q3 sang 10/11")
    assert route_mention("<abc>") == ("help", "")
    assert route_mention("<summarize> hôm qua") == ("help", "")  # ngày sai cú pháp
    assert route_mention("") == ("help", "")


def test_strip_mention_keeps_question():
    assert strip_mention("@ant_bot  task Q3 hạn khi nào?", "ant_bot") == "task Q3 hạn khi nào?"
    assert strip_mention("task Q3 @Ant_Bot hạn khi nào?", "ant_bot") == "task Q3 hạn khi nào?"
    assert strip_mention("@ant_bot dòng 1\ndòng 2", "ant_bot") == "dòng 1\ndòng 2"
    assert strip_mention("@ant_bot_khac hỏi @ant_bot", "ant_bot") == "@ant_bot_khac hỏi"
    assert strip_mention("  @ant_bot  ", "ant_bot") == ""


def test_tag_bot_routes_by_keyword_and_is_not_stored():
    st = make_state()
    cases = [
        ("@ant_bot dời hạn task Q3 sang 10/11", ("ask", "dời hạn task Q3 sang 10/11")),
        ("@ant_bot <summarize>", ("summary", "2026-10-29")),  # mặc định hôm nay
        ("@ant_bot <summarize> 2026-10-20", ("summary", "2026-10-20")),
        ("@ant_bot <task> Lan", ("tasks", "Lan")),
        ("@ant_bot task Q3 hạn khi nào?", ("ask", "task Q3 hạn khi nào?")),
    ]
    for text, expected in cases:
        msg = FakeMsg(text, ONLINE, mentions=["@ant_bot"])
        assert deliver(st, on_group_message, msg) == [expected]
        assert msg.sent == []
    # Không lưu: bước ghi task không thể làm theo yêu cầu sửa chưa được xác nhận
    assert stored(st) == 0


def test_tag_bot_alone_or_unknown_keyword_replies_help():
    st = make_state()
    for text in ("@ant_bot", "@ant_bot <abc> gì đó"):
        msg = FakeMsg(text, ONLINE, mentions=["@ant_bot"])
        assert deliver(st, on_group_message, msg) == []
        assert msg.sent == [mention_help("ant_bot")]
    assert "@ant_bot <summarize>" in mention_help("ant_bot") and "@ant_bot <task>" in mention_help("ant_bot")
    assert stored(st) == 0
    assert not hasattr(st, "pending_asks")  # không còn trạng thái chờ câu hỏi


def test_tag_bot_offline_or_edited_is_not_answered():
    st = make_state()
    offline = FakeMsg("@ant_bot hạn khi nào?", OFFLINE, mentions=["@ant_bot"])
    assert deliver(st, on_group_message, offline) == [] and offline.sent == [OFFLINE_REPLY]
    edited = FakeMsg("@ant_bot hạn khi nào?", ONLINE, mentions=["@ant_bot"])
    assert deliver(st, on_group_message, edited, edited=True) == [] and edited.sent == []
    assert stored(st) == 0


def test_group_message_stored_by_bot_only_without_userbot():
    # Chế độ dự phòng: không có userbot thì bot tự lưu tin nhóm như cũ
    st = make_state()
    deliver(st, on_group_message, FakeMsg("Lan làm báo cáo nhé", ONLINE))
    assert stored(st) == 1
    # Có userbot: userbot là nguồn tin, bot không lưu
    st = make_state(userbot=SimpleNamespace(me=SimpleNamespace(id=7), tracked=set()))
    deliver(st, on_group_message, FakeMsg("Lan làm báo cáo nhé", ONLINE))
    assert stored(st) == 0


def test_remember_chat_records_group_title_and_tracks_it():
    """Cập nhật đầu tiên trong nhóm (DB còn trống) cũng lưu tên nhóm, để tab mới không bị đặt theo chat id."""
    st = make_state()
    context = SimpleNamespace(application=RecordingApp(st))

    def send(chat):
        asyncio.run(remember_chat(SimpleNamespace(effective_chat=chat, my_chat_member=None), context))

    send(SimpleNamespace(id=-1, type="supergroup", title="Team A"))
    assert st.storage.chat_title(-1) == "Team A"
    assert st.storage.tracked_chats() == [-1]
    send(SimpleNamespace(id=-1, type="supergroup", title="Team A mới"))  # nhóm đổi tên
    assert st.storage.chat_title(-1) == "Team A mới"
    send(SimpleNamespace(id=5, type="private", title=None))  # chat riêng: không lưu
    send(None)  # cập nhật không gắn với chat nào
    assert st.storage.chat_title(5) is None
    assert st.storage.conn.execute("select count(*) from chats").fetchone()[0] == 1

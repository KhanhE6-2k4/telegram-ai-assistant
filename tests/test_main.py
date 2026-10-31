import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.main import cmd_tasks, on_startup, sent_while_offline, split_message
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

    st = SimpleNamespace(storage=s, busy=set(), today=lambda: "2026-10-29", compact=compact, review=review)
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
    context = SimpleNamespace(args=args, application=SimpleNamespace(bot_data={"state": st}))
    asyncio.run(cmd_tasks(update, context))
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
    assert "Gõ /tomtat để cập nhật" in run_task_command(TASK_ROWS, [], pending_today=True)
    assert "/tomtat" not in run_task_command(TASK_ROWS, [])


# ---- /ask và tin tag bot ----

from telegram import ForceReply

from bot.main import cmd_ask, command_argument, on_group_message, strip_mention, ASK_PROMPT, OFFLINE_REPLY


def test_command_argument_keeps_text_after_command():
    assert command_argument("/ask task Q3 hạn khi nào?") == "task Q3 hạn khi nào?"
    assert command_argument("/ask@ant_bot  dòng 1\ndòng 2") == "dòng 1\ndòng 2"
    assert command_argument("/ask") == ""


class FakeMsg(Replies):
    ids = iter(range(1, 10**6))

    def __init__(self, text, date, mentions=(), user_id=1, reply_to=None):
        super().__init__()
        self.text, self.caption, self.date = text, None, date
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


def make_state():
    return SimpleNamespace(
        storage=Storage(":memory:"), started_at=datetime(2026, 10, 29, 2, tzinfo=timezone.utc),
        config=SimpleNamespace(timezone=ZoneInfo("Asia/Ho_Chi_Minh"), compact_threshold_chars=60000),
        pending_asks={}, busy=set(), today=lambda: "2026-10-29",
    )


def deliver(st, handler, msg, edited=False):
    """Gửi một tin tới handler, chạy các tác vụ nền; trả về các câu hỏi đã chuyển cho answer_question."""
    asked = []

    async def fake_answer(update, context, question):
        asked.append(question)

    import bot.main as m
    original, m.answer_question = m.answer_question, fake_answer
    try:
        app = RecordingApp(st)
        update = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=-1, title="A"),
                                 edited_message=msg if edited else None)
        context = SimpleNamespace(application=app, bot=SimpleNamespace(username="ant_bot"))
        asyncio.run(handler(update, context))
        for coro in app.tasks:
            asyncio.run(coro)
    finally:
        m.answer_question = original
    return asked


def stored(st):
    return st.storage.conn.execute("select count(*) from messages").fetchone()[0]


def test_ask_passes_question():
    st, msg = make_state(), FakeMsg("/ask task báo cáo Q3 hạn khi nào?", ONLINE)
    assert deliver(st, cmd_ask, msg) == ["task báo cáo Q3 hạn khi nào?"]
    assert msg.sent == []


def test_ask_sent_while_offline_is_not_answered():
    st, msg = make_state(), FakeMsg("/ask hạn khi nào?", OFFLINE)
    assert deliver(st, cmd_ask, msg) == []
    assert msg.sent == [OFFLINE_REPLY]


def test_empty_ask_prompts_with_force_reply_then_reply_is_the_question():
    """Bấm /ask từ menu (gửi ngay, không có câu hỏi) -> bot hỏi lại, mở ô trả lời; câu trả lời là câu hỏi."""
    st, cmd = make_state(), FakeMsg("/ask@ant_bot", ONLINE, user_id=1)
    assert deliver(st, cmd_ask, cmd) == []
    assert cmd.sent == [ASK_PROMPT]
    markup = cmd.markups[0]
    assert isinstance(markup, ForceReply) and markup.selective and markup.input_field_placeholder
    prompt_id = 101  # message_id của tin hỏi lại (xem Replies.reply_text)
    assert st.pending_asks == {(-1, prompt_id): 1}

    # Người khác reply tin hỏi lại: coi là tin thường của nhóm
    other = FakeMsg("chuyện khác", ONLINE, user_id=2, reply_to=prompt_id)
    assert deliver(st, on_group_message, other) == [] and stored(st) == 1

    # Đúng người gõ /ask trả lời: thành câu hỏi, không lưu vào hội thoại
    answer = FakeMsg("báo cáo Q3 hạn khi nào?", ONLINE, user_id=1, reply_to=prompt_id)
    assert deliver(st, on_group_message, answer) == ["báo cáo Q3 hạn khi nào?"]
    assert stored(st) == 1 and st.pending_asks == {}

    # Chỉ có hiệu lực một lần: trả lời tiếp lần nữa là tin thường
    again = FakeMsg("thêm câu nữa", ONLINE, user_id=1, reply_to=prompt_id)
    assert deliver(st, on_group_message, again) == [] and stored(st) == 2


def test_remember_chat_records_group_title_from_any_update():
    """Lệnh đầu tiên trong nhóm (DB còn trống) cũng lưu tên nhóm, để tab mới không bị đặt theo chat id."""
    from bot.main import remember_chat

    st = make_state()
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"state": st}))

    def send(chat):
        asyncio.run(remember_chat(SimpleNamespace(effective_chat=chat), context))

    send(SimpleNamespace(id=-1, type="supergroup", title="Team A"))
    assert st.storage.chat_title(-1) == "Team A"
    send(SimpleNamespace(id=-1, type="supergroup", title="Team A mới"))  # nhóm đổi tên
    assert st.storage.chat_title(-1) == "Team A mới"
    send(SimpleNamespace(id=5, type="private", title=None))  # chat riêng: không lưu
    send(None)  # cập nhật không gắn với chat nào
    assert st.storage.chat_title(5) is None
    assert st.storage.conn.execute("select count(*) from chats").fetchone()[0] == 1


def test_strip_mention_keeps_question():
    assert strip_mention("@ant_bot  task Q3 hạn khi nào?", "ant_bot") == "task Q3 hạn khi nào?"
    assert strip_mention("task Q3 @Ant_Bot hạn khi nào?", "ant_bot") == "task Q3 hạn khi nào?"
    assert strip_mention("@ant_bot dòng 1\ndòng 2", "ant_bot") == "dòng 1\ndòng 2"
    assert strip_mention("@ant_bot_khac hỏi @ant_bot", "ant_bot") == "@ant_bot_khac hỏi"
    assert strip_mention("  @ant_bot  ", "ant_bot") == ""


def test_tag_bot_is_answered_like_ask_and_is_not_stored():
    st = make_state()
    msg = FakeMsg("@ant_bot dời hạn task Q3 sang 10/11", ONLINE, mentions=["@ant_bot"])
    assert deliver(st, on_group_message, msg) == ["dời hạn task Q3 sang 10/11"]
    assert msg.sent == []
    # Không lưu: bước ghi task không thể làm theo yêu cầu sửa chưa được xác nhận
    assert stored(st) == 0


def test_tag_bot_alone_prompts_for_question():
    st, msg = make_state(), FakeMsg("@ant_bot", ONLINE, mentions=["@ant_bot"], user_id=1)
    assert deliver(st, on_group_message, msg) == []
    assert msg.sent == [ASK_PROMPT] and isinstance(msg.markups[0], ForceReply)
    assert st.pending_asks == {(-1, 101): 1} and stored(st) == 0
    # Người đó trả lời tin hỏi lại: thành câu hỏi
    answer = FakeMsg("báo cáo Q3 hạn khi nào?", ONLINE, user_id=1, reply_to=101)
    assert deliver(st, on_group_message, answer) == ["báo cáo Q3 hạn khi nào?"]


def test_tag_bot_offline_or_edited_is_not_answered():
    st = make_state()
    offline = FakeMsg("@ant_bot hạn khi nào?", OFFLINE, mentions=["@ant_bot"])
    assert deliver(st, on_group_message, offline) == [] and offline.sent == [OFFLINE_REPLY]
    edited = FakeMsg("@ant_bot hạn khi nào?", ONLINE, mentions=["@ant_bot"])
    assert deliver(st, on_group_message, edited, edited=True) == [] and edited.sent == []
    assert stored(st) == 0

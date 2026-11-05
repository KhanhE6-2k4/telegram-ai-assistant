"""Userbot (nguồn tin), lệnh riêng của chủ bot, menu lệnh. Không dùng mạng: FakeUserbot thay Telethon."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram import BotCommandScopeAllGroupChats, BotCommandScopeChat, BotCommandScopeDefault

import bot.main as m
from bot.storage import DaySummary, Storage, format_chat
from bot.userbot import is_conversation, message_fields
from tests.test_main import FakeMsg, RecordingApp, make_state

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
OWNER = 7


# ---- lọc tin hội thoại ----

def test_is_conversation():
    assert is_conversation("Lan làm báo cáo nhé", 1, 9, "ant_bot")
    assert is_conversation("ok anh", 1, 9, "ant_bot")  # kể cả khi trả lời (reply) tin của bot
    assert is_conversation("hỏi @ant_bot_khac", 1, 9, "ant_bot")  # tag bot khác
    assert not is_conversation("Đây là câu trả lời", 9, 9, "ant_bot")  # tin do chính bot gửi
    assert not is_conversation("/tomtat", 1, 9, "ant_bot")  # lệnh, kể cả lệnh cũ hay lệnh của bot khác
    assert not is_conversation("  /start@other_bot", 1, 9, "ant_bot")
    assert not is_conversation("@Ant_Bot task Q3 hạn khi nào?", 1, 9, "ant_bot")  # tag bot = yêu cầu
    assert not is_conversation("task Q3 @ant_bot", 1, 9, "ant_bot")
    assert not is_conversation("", 1, 9, "ant_bot")
    assert not is_conversation(None, 1, 9, "ant_bot")


def test_message_fields_converts_telethon_message():
    msg = SimpleNamespace(id=5, message="xin chào", sender_id=3, reply_to_msg_id=2,
                          date=datetime(2026, 10, 29, 17, 30, tzinfo=timezone.utc))
    f = message_fields(msg, -100123, "Lan", TZ)
    assert f == dict(chat_id=-100123, message_id=5, user_id=3, user_name="Lan", text="xin chào", reply_to=2,
                     sent_at=datetime(2026, 10, 30, 0, 30, tzinfo=TZ))
    assert f["sent_at"].date().isoformat() == "2026-10-30"  # ngày theo TIMEZONE


def test_userbot_event_forwards_only_tracked_conversation():
    pytest.importorskip("telethon")
    from telethon.tl.types import PeerChannel

    from bot.userbot import Userbot

    got = []
    ub = Userbot(1, "hash", "session", TZ, on_message=lambda fields, edited: got.append((fields["text"], edited)))
    ub.tracked, ub.bot_id, ub.bot_username = {-1000000000123}, 9, "ant_bot"

    def event(text, channel=123, sender=1):
        async def get_sender():
            return None

        return SimpleNamespace(message=SimpleNamespace(
            peer_id=PeerChannel(channel), id=1, message=text, sender_id=sender, reply_to_msg_id=None,
            date=datetime(2026, 10, 29, 3, tzinfo=timezone.utc), get_sender=get_sender,
        ))

    asyncio.run(ub._on_new(event("Lan làm báo cáo")))
    asyncio.run(ub._on_edited(event("Lan làm báo cáo Q3")))
    asyncio.run(ub._on_new(event("tin nhóm khác", channel=456)))  # chat không theo dõi
    asyncio.run(ub._on_new(event("câu trả lời", sender=9)))  # tin của bot
    asyncio.run(ub._on_new(event("@ant_bot hỏi")))  # tag bot
    assert got == [("Lan làm báo cáo", False), ("Lan làm báo cáo Q3", True)]


# ---- lưu tin qua ingest ----

def fields(mid, text, sent_at, chat_id=-1):
    return dict(chat_id=chat_id, message_id=mid, user_id=1, user_name="An", text=text, reply_to=None, sent_at=sent_at)


def ingest_state(threshold=60000):
    st = make_state()
    st.config.compact_threshold_chars = threshold
    st.compacts = []

    async def compact(chat_id, day, finalize, retry=True, keep_recent=True):
        st.compacts.append((chat_id, day, finalize))

    st.compact = compact
    return st


def texts(st):
    return [r[0] for r in st.storage.conn.execute("select text from messages order by id")]


def test_ingest_stores_edits_and_reopens_finalized_day():
    st, app = ingest_state(), RecordingApp(None)
    t = datetime(2026, 10, 28, 9, tzinfo=TZ)
    m.ingest(app, st, fields(1, "Lan làm báo cáo", t), edited=False)
    m.ingest(app, st, fields(1, "Lan làm báo cáo", t), edited=False)  # trùng: bỏ
    assert texts(st) == ["Lan làm báo cáo"]
    st.storage.save_summary(DaySummary(-1, "2026-10-28", "tóm tắt", 1, True))
    m.ingest(app, st, fields(1, "Lan làm báo cáo Q3", t), edited=True)
    assert texts(st) == ["Lan làm báo cáo Q3"]
    s = st.storage.get_summary(-1, "2026-10-28")
    assert not s.finalized  # ngày đã chốt được mở lại
    assert st.storage.pending_messages(-1, "2026-10-28", s.compacted_until)[0].edit_pending == 1
    assert app.tasks == []  # ngày cũ: không nén giữa ngày


def test_edit_with_unchanged_text_is_ignored():
    st, app = ingest_state(), RecordingApp(None)
    t = datetime(2026, 10, 28, 9, tzinfo=TZ)
    m.ingest(app, st, fields(1, "Lan làm báo cáo", t), edited=False)
    st.storage.save_summary(DaySummary(-1, "2026-10-28", "tóm tắt", 1, True))
    m.ingest(app, st, fields(1, "Lan làm báo cáo", t), edited=True)  # ví dụ chỉ đổi reaction
    assert st.storage.get_summary(-1, "2026-10-28").finalized


def test_ingest_compacts_today_over_threshold():
    st, app = ingest_state(threshold=10), RecordingApp(None)
    today = datetime(2026, 10, 29, 9, tzinfo=TZ)
    m.ingest(app, st, fields(1, "ngắn", today), edited=False)
    assert app.tasks == []
    m.ingest(app, st, fields(2, "tin dài hơn ngưỡng", today), edited=False)
    assert len(app.tasks) == 1
    asyncio.run(app.tasks[0])
    assert st.compacts == [(-1, "2026-10-29", False)]
    # Bù tin (compact=False): không nén, người gọi tự xử lý
    m.ingest(app, st, fields(3, "thêm một tin dài nữa", today), edited=False, compact=False)
    assert len(app.tasks) == 1


# ---- bù tin ----

class FakeUserbot:
    """users / groups: [(chat_id, tên, username)], gần đây trước (như iter_dialogs)."""

    def __init__(self, history=None, users=(), groups=()):
        self.me = SimpleNamespace(id=OWNER)
        self.owner_id, self.owner_name = OWNER, "Chủ bot"
        self.started_at = datetime.now(TZ)
        self.tracked: set[int] = set()
        self.history = history or {}
        self.users = list(users)
        self.groups = list(groups)
        self.fetches = []

    async def fetch_since(self, chat_id, since):
        self.fetches.append((chat_id, since))
        return [f for f in self.history.get(chat_id, []) if f["sent_at"] >= since]

    @staticmethod
    def _find(chats, ref):
        if ref.startswith("@"):
            return [c for c in chats if c[2] and c[2].casefold() == ref[1:].casefold()]
        return [c for c in chats if ref.casefold() in c[1].casefold()]

    async def find_users(self, ref):
        return self._find(self.users, ref)

    async def find_groups(self, ref):
        return self._find(self.groups, ref)

    async def recent_users(self):
        return self.users[:10]

    async def recent_groups(self):
        return self.groups[:10]


def test_supplement_first_time_takes_history_days_then_from_last_message():
    now = datetime.now(TZ)
    history = {-1: [fields(1, "cũ quá", now - timedelta(days=10)), fields(2, "tuần này", now - timedelta(days=3)),
                    fields(3, "vừa xong", now - timedelta(hours=1))]}
    ub = FakeUserbot(history)
    st, app = make_state(ub), RecordingApp(None)
    assert asyncio.run(m.supplement_chat(app, st, -1)) == 2
    assert texts(st) == ["tuần này", "vừa xong"]  # chỉ 7 ngày gần nhất
    assert abs(ub.fetches[0][1] - (now - timedelta(days=7))) < timedelta(minutes=1)

    history[-1].append(fields(4, "tin mới", now))
    asyncio.run(m.supplement_chat(app, st, -1))
    assert ub.fetches[1][1] == now - timedelta(hours=1)  # từ tin cuối đã lưu (tính cả nó)
    assert texts(st) == ["tuần này", "vừa xong", "tin mới"]  # tin trùng bị bỏ
    assert app.tasks == []


def test_supplement_tracked_covers_only_tracked_chats_from_before_start():
    now = datetime.now(TZ)
    ub = FakeUserbot({-1: [fields(1, "a", now - timedelta(days=1))], -2: [fields(1, "b", now, chat_id=-2)]})
    st, app = make_state(ub), RecordingApp(None)
    m.track_chat(st, -1, "Team A")
    assert ub.tracked == {-1}
    # Tin userbot nhận trực tiếp sau khi khởi động không làm bỏ sót tin gửi lúc bot tắt
    ub.started_at = now - timedelta(minutes=5)
    m.ingest(app, st, fields(9, "tin nhận trực tiếp", now - timedelta(minutes=1)), edited=False)
    asyncio.run(m.supplement_tracked(app, st))
    assert [c for c, _ in ub.fetches] == [-1]
    assert ub.fetches[0][1] < now - timedelta(days=6)
    assert sorted(texts(st)) == ["a", "tin nhận trực tiếp"]


def test_no_userbot_no_supplement():
    st = make_state()
    assert asyncio.run(m.supplement_chat(RecordingApp(None), st, -1)) == 0


# ---- theo dõi chat ----

def test_bot_removed_from_group_untracks_it():
    ub = FakeUserbot()
    st = make_state(ub)
    app = RecordingApp(st)
    context = SimpleNamespace(application=app)
    chat = SimpleNamespace(id=-1, type="supergroup", title="Team A")

    def member_update(status):
        return SimpleNamespace(effective_chat=chat, my_chat_member=SimpleNamespace(
            chat=chat, new_chat_member=SimpleNamespace(status=status)))

    added = member_update("member")
    asyncio.run(m.remember_chat(added, context))
    asyncio.run(m.on_my_chat_member(added, context))
    assert st.storage.tracked_chats() == [-1] and ub.tracked == {-1}
    assert len(app.tasks) == 1  # chat mới theo dõi: bù lịch sử
    asyncio.run(app.tasks.pop())
    removed = member_update("kicked")
    asyncio.run(m.remember_chat(removed, context))
    asyncio.run(m.on_my_chat_member(removed, context))
    assert st.storage.tracked_chats() == [] and ub.tracked == set()


def test_migration_adds_tracked_for_known_chats(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT, updated_at TEXT NOT NULL);"
        "INSERT INTO chats VALUES (-1, 'Team A', 'x');"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
        "message_id INTEGER NOT NULL, user_id INTEGER, user_name TEXT, text TEXT NOT NULL, reply_to INTEGER, "
        "sent_at TEXT NOT NULL, day TEXT NOT NULL, UNIQUE (chat_id, message_id));"
        "INSERT INTO messages (chat_id, message_id, text, sent_at, day) VALUES (-2, 1, 'x', '2026-10-28', '2026-10-28');"
    )
    conn.commit()
    conn.close()
    s = Storage(path)
    assert sorted(s.tracked_chats()) == [-2, -1]
    assert s.chat_title(-1) == "Team A"
    s.close()
    s = Storage(path)  # mở lại: không chạy migration lần nữa
    s.untrack_chat(-2)
    s.close()
    assert Storage(path).tracked_chats() == [-1]


# ---- lệnh riêng của chủ bot ----

class FakeQA:
    """Ghi lại các lần hỏi; câu trả lời thứ n là "Trả lời n" (lần đầu: "Trả lời")."""

    def __init__(self, proposals=()):
        self.calls, self.proposals, self.histories = [], list(proposals), []

    async def answer(self, chat_id, title, today, asker, request, can_edit=False, history=()):
        self.calls.append((chat_id, title, asker, request, can_edit))
        self.histories.append(list(history))
        n = len(self.calls)
        return ("Trả lời" if n == 1 else f"Trả lời {n}"), self.proposals


def private_state(ub=None, proposals=()):
    st = make_state(ub)
    st.today = lambda: datetime.now(TZ).date().isoformat()
    st.started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    st.qa = FakeQA(proposals)
    st.compacts = []

    async def compact(chat_id, day, finalize, retry=True, keep_recent=True):
        st.compacts.append((chat_id, day, finalize, retry, keep_recent))

    st.compact = compact
    return st


def run_command(st, handler, text, user_id=OWNER, date=None):
    msg = FakeMsg(text, date or datetime.now(timezone.utc), user_id=user_id, chat_id=user_id)
    actions = []

    async def send_chat_action(chat_id, action):
        actions.append((chat_id, action))

    app = RecordingApp(st)
    update = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=user_id),
                             effective_chat=SimpleNamespace(id=user_id, type="private", title=None))
    context = SimpleNamespace(application=app, bot=SimpleNamespace(username="ant_bot", send_chat_action=send_chat_action))
    asyncio.run(handler(update, context))
    return msg, context


LAN = (55, "Lan", "lan")
FOOTER = "\n\n" + m.SESSION_FOOTER


def test_private_command_rejects_non_owner():
    st = private_state(FakeUserbot(users=[LAN]))
    msg, _ = run_command(st, m.cmd_p, "/p @lan tóm tắt", user_id=99)
    assert msg.sent == [m.NOT_OWNER_REPLY]
    msg, _ = run_command(st, m.cmd_g, "/g @team tóm tắt", user_id=99)
    assert msg.sent == [m.NOT_OWNER_REPLY]
    msg, _ = run_command(st, m.cmd_e, "/e", user_id=99)
    assert msg.sent == [m.NOT_OWNER_REPLY]
    assert st.qa.calls == [] and st.storage.tracked_chats() == [] and st.session is None


def test_private_command_without_userbot():
    st = private_state(None)
    msg, _ = run_command(st, m.cmd_p, "/p @lan tóm tắt")
    assert msg.sent == [m.NO_USERBOT_REPLY]


def test_private_command_sent_while_offline():
    st = private_state(FakeUserbot())
    msg, _ = run_command(st, m.cmd_g, "/g @team x", date=datetime(2025, 1, 1, tzinfo=timezone.utc))
    assert msg.sent == [m.OFFLINE_COMMAND_REPLY]


def test_empty_command_without_chats_replies_help():
    st = private_state(FakeUserbot())
    assert run_command(st, m.cmd_p, "/p")[0].sent == [m.PERSONAL_HELP]
    assert run_command(st, m.cmd_g, "/g")[0].sent == [m.GROUP_HELP]


def test_parse_target_argument():
    assert m.parse_target_argument("@team_a task nào trễ?") == ("@team_a", "task nào trễ?")
    assert m.parse_target_argument('"Team Marketing" tóm tắt\ndòng 2') == ("Team Marketing", "tóm tắt\ndòng 2")
    assert m.parse_target_argument('"Nguyễn Văn A"') == ("Nguyễn Văn A", "")
    assert m.parse_target_argument("marketing tóm tắt") == ("marketing", "tóm tắt")
    assert m.parse_target_argument("Lan") == ("Lan", "")
    assert m.parse_target_argument("") == ("", "")


def press(st, context, msg, data, user_id=OWNER):
    """Bấm nút chọn chat; trả về (các câu query.answer, các lần sửa tin nút)."""
    answers, edits = [], []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    async def edit_message_text(text):
        edits.append(text)

    query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id), message=msg,
                            answer=answer, edit_message_text=edit_message_text)
    asyncio.run(m.on_pick_chat(SimpleNamespace(callback_query=query), context))
    return answers, edits


def say(st, context, text, user_id=OWNER, date=None):
    """Chủ bot (hoặc `user_id`) nhắn một tin thường trong chat riêng với bot."""
    msg = FakeMsg(text, date or datetime.now(timezone.utc), user_id=user_id, chat_id=user_id)
    update = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=user_id, type="private"))
    asyncio.run(m.on_private_message(update, context))
    return msg


def test_p_with_question_starts_session_and_follow_up_has_context():
    now = datetime.now(TZ)
    yesterday = now - timedelta(days=1)
    ub = FakeUserbot(history={55: [fields(1, "Lan làm slide nhé", yesterday, chat_id=55),
                                   fields(2, "ok anh", now, chat_id=55)]},
                     users=[LAN])
    st = private_state(ub)
    msg, context = run_command(st, m.cmd_p, "/p @lan tóm tắt việc cần làm")
    assert st.storage.tracked_chats() == [55] and 55 in ub.tracked
    assert st.storage.chat_title(55) == "Lan"  # tab trên sheet mang tên người kia
    assert st.storage.chat_label(55) == '"Lan" (@lan, 55)'
    assert texts(st) == ["Lan làm slide nhé", "ok anh"]
    # Mọi ngày còn tin chưa xử lý: ngày trước được chốt, hôm nay thì không; không chờ thử lại
    assert st.compacts == [
        (55, yesterday.date().isoformat(), True, False, False),
        (55, now.date().isoformat(), False, False, False),
    ]
    assert st.qa.calls == [(55, "Chat riêng với Lan", "Chủ bot", "tóm tắt việc cần làm", True)]
    assert msg.sent == ["Trả lời" + FOOTER]

    # Câu hỏi tiếp: không cần cú pháp, có ngữ cảnh lượt trước, không bù / xử lý lại
    follow = say(st, context, "việc nào gấp nhất?")
    assert follow.sent == ["Trả lời 2" + FOOTER]
    assert st.qa.calls[1] == (55, "Chat riêng với Lan", "Chủ bot", "việc nào gấp nhất?", True)
    assert st.qa.histories[1] == [("tóm tắt việc cần làm", "Trả lời")]
    assert len(st.compacts) == 2 and len(ub.fetches) == 1
    say(st, context, "còn gì nữa?")
    assert st.qa.histories[2] == [("tóm tắt việc cần làm", "Trả lời"), ("việc nào gấp nhất?", "Trả lời 2")]


def test_empty_p_picks_user_then_messages_go_to_session():
    ub = FakeUserbot(users=[LAN, (66, "Nguyễn Văn A", None)])
    st = private_state(ub)
    msg, context = run_command(st, m.cmd_p, "/p")
    assert msg.sent == ['Chọn người (không thấy thì gõ /p "tên" để tìm):']
    buttons = [row[0] for row in msg.markups[0].inline_keyboard]
    assert [b.text for b in buttons] == ["Lan (@lan)", "Nguyễn Văn A"]

    # Người khác bấm: từ chối, lựa chọn vẫn còn
    assert press(st, context, msg, buttons[1].callback_data, 99) == ([m.NOT_OWNER_REPLY], [])
    assert press(st, context, msg, buttons[1].callback_data) == ([None], ["Đã chọn: Nguyễn Văn A"])
    assert msg.sent[-1] == "Đã bắt đầu chủ đề: Chat riêng với Nguyễn Văn A. Bạn hỏi gì cứ nhắn." + FOOTER
    assert st.qa.calls == [] and st.storage.tracked_chats() == [66]

    # Người khác nhắn riêng bot: bỏ qua
    assert say(st, context, "tóm tắt", user_id=99).sent == []
    assert say(st, context, "tóm tắt việc cần làm").sent == ["Trả lời" + FOOTER]
    assert st.qa.calls == [(66, "Chat riêng với Nguyễn Văn A", "Chủ bot", "tóm tắt việc cần làm", True)]
    # Bấm lại nút cũ: đã hết hạn
    assert press(st, context, msg, buttons[0].callback_data) == (["Yêu cầu này đã hết hạn."], [])


def test_message_without_session_or_offline():
    st = private_state(FakeUserbot(users=[LAN]))
    _, context = run_command(st, m.cmd_e, "/e")
    assert say(st, context, "tóm tắt").sent == [m.NO_SESSION_REPLY]
    run_command(st, m.cmd_p, "/p Lan")
    late = say(st, context, "tóm tắt", date=datetime(2025, 1, 1, tzinfo=timezone.utc))  # gửi lúc bot tắt
    assert late.sent == [m.OFFLINE_REPLY] and st.qa.calls == []


def test_e_ends_session():
    st = private_state(FakeUserbot(users=[LAN]))
    msg, context = run_command(st, m.cmd_e, "/e")
    assert msg.sent == ["Không có chủ đề nào đang mở."]
    run_command(st, m.cmd_p, "/p @lan tóm tắt")
    msg, _ = run_command(st, m.cmd_e, "/e")
    assert msg.sent == ["Đã kết thúc chủ đề: Chat riêng với Lan."]
    assert st.session is None
    assert say(st, context, "còn gì nữa?").sent == [m.NO_SESSION_REPLY]
    assert len(st.qa.calls) == 1


def test_new_session_replaces_old_context():
    st = private_state(FakeUserbot(users=[LAN], groups=[(-100, "Team A", None)]))
    _, context = run_command(st, m.cmd_p, "/p @lan tóm tắt")
    run_command(st, m.cmd_g, "/g team task nào trễ?")
    assert st.session.option == (-100, "Team A", None)
    assert st.qa.histories[1] == []  # chủ đề mới: không mang ngữ cảnh cũ
    say(st, context, "của ai?")
    assert st.qa.calls[2][:2] == (-100, "Team A")
    assert st.qa.histories[2] == [("task nào trễ?", "Trả lời 2")]


def test_session_expires_after_an_hour_without_messages():
    st = private_state(FakeUserbot(users=[LAN]))
    _, context = run_command(st, m.cmd_p, "/p @lan tóm tắt")
    st.session.last_active = datetime.now(timezone.utc) - timedelta(minutes=59)
    assert say(st, context, "tiếp").sent == ["Trả lời 2" + FOOTER]  # còn hạn, và tính lại từ tin này
    st.session.last_active = datetime.now(timezone.utc) - timedelta(minutes=61)
    msg = say(st, context, "tiếp nữa")
    assert msg.sent == ["Chủ đề Chat riêng với Lan đã hết hạn (quá 1 giờ không trao đổi). "
                        "Dùng /p hoặc /g để bắt đầu lại."]
    assert st.session is None and len(st.qa.calls) == 2


def test_session_sends_only_recent_turns():
    st = private_state(FakeUserbot(users=[LAN]))
    _, context = run_command(st, m.cmd_p, "/p Lan")
    for i in range(m.MAX_SESSION_TURNS + 3):
        say(st, context, f"câu {i}")
    assert len(st.session.history) == m.MAX_SESSION_TURNS
    assert len(st.qa.histories[-1]) == m.MAX_SESSION_TURNS
    assert st.qa.histories[-1][-1][0] == f"câu {m.MAX_SESSION_TURNS + 1}"


def test_answer_after_session_ended_is_not_recorded():
    st = private_state(FakeUserbot(users=[LAN]))
    _, context = run_command(st, m.cmd_p, "/p Lan")
    session = st.session
    answer = st.qa.answer

    async def answer_then_end(*args, **kwargs):
        st.session = None  # /e trong lúc đang trả lời
        return await answer(*args, **kwargs)

    st.qa.answer = answer_then_end
    msg = say(st, context, "tóm tắt")
    assert msg.sent == ["Trả lời" + FOOTER] and session.history == []


def test_long_answer_footer_in_last_message():
    st = private_state(FakeUserbot(users=[LAN]))
    _, context = run_command(st, m.cmd_p, "/p Lan")

    async def long_answer(*args, **kwargs):
        return "\n".join(["x" * 100] * 60), []

    st.qa.answer = long_answer
    msg = say(st, context, "tóm tắt")
    assert len(msg.sent) == 2 and msg.sent[-1].endswith(FOOTER) and not msg.sent[0].endswith(m.SESSION_FOOTER)


def test_p_by_display_name_without_question_starts_session():
    st = private_state(FakeUserbot(users=[LAN, (66, "Nguyễn Văn A", None)]))
    msg, _ = run_command(st, m.cmd_p, '/p "nguyễn văn a"')
    assert msg.sent == ["Đã bắt đầu chủ đề: Chat riêng với Nguyễn Văn A. Bạn hỏi gì cứ nhắn." + FOOTER]
    assert st.session.option == (66, "Nguyễn Văn A", None)


def test_p_many_name_matches_shows_picker_with_question():
    st = private_state(FakeUserbot(users=[(1, "Lan Anh", None), (2, "Lan", "lan_ha")]))
    msg, context = run_command(st, m.cmd_p, "/p lan tóm tắt")
    assert msg.sent == ["Có 2 người khớp với lan. Chọn người:"]
    buttons = [row[0] for row in msg.markups[0].inline_keyboard]
    press(st, context, msg, buttons[1].callback_data)
    # Đã có câu hỏi trong lệnh: trả lời luôn
    assert st.qa.calls == [(2, "Chat riêng với Lan", "Chủ bot", "tóm tắt", True)]


def test_p_unknown_user():
    st = private_state(FakeUserbot(users=[LAN]))
    msg, _ = run_command(st, m.cmd_p, "/p @ai_do tóm tắt")
    assert msg.sent[0].startswith("Không tìm thấy ai có tên / username @ai_do")
    assert st.session is None


def test_edit_proposal_in_session_targets_task_chat():
    proposal = {"stt": 1, "changes": {"deadline": "2026-11-10"},
                "current": {"name": "Báo cáo Q3", "deadline": "2026-11-02"}}
    st = private_state(FakeUserbot(groups=[(-100, "Team A", None)]), proposals=[proposal])
    msg, _ = run_command(st, m.cmd_g, "/g team dời hạn báo cáo Q3 sang 10/11")
    assert msg.sent[0] == "Trả lời" + FOOTER and msg.sent[1].startswith("Xác nhận sửa task #1")
    (edit,) = st.pending_edits.values()
    assert edit["chat_id"] == -100 and edit["user_id"] == OWNER  # nút ở DM, sửa sheet của nhóm


def test_g_many_matches_shows_picker():
    ub = FakeUserbot(groups=[(-100, "Team Marketing", None), (-200, "Marketing Hà Nội", "mkt_hn"), (-300, "Sales", None)])
    st = private_state(ub)
    msg, context = run_command(st, m.cmd_g, "/g marketing task nào trễ?")
    assert msg.sent == ["Có 2 nhóm khớp với marketing. Chọn nhóm:"]
    buttons = [row[0] for row in msg.markups[0].inline_keyboard]
    assert [b.text for b in buttons] == ["Team Marketing", "Marketing Hà Nội (@mkt_hn)"]
    assert st.qa.calls == []
    assert press(st, context, msg, buttons[1].callback_data) == ([None], ["Đã chọn: Marketing Hà Nội (@mkt_hn)"])
    assert st.qa.calls == [(-200, "Marketing Hà Nội", "Chủ bot", "task nào trễ?", True)]
    assert st.storage.tracked_chats() == [-200]


def test_empty_g_lists_tracked_groups_first():
    ub = FakeUserbot(groups=[(-300, "Sales", None), (-100, "Team A", None)])
    st = private_state(ub)
    st.storage.track_chat(-100, "Team A", "team_a")
    st.storage.track_chat(77, "Lan")  # chat riêng: không phải nhóm
    msg, _ = run_command(st, m.cmd_g, "/g")
    assert [row[0].text for row in msg.markups[0].inline_keyboard] == ["Team A (@team_a)", "Sales"]


def test_g_no_match():
    st = private_state(FakeUserbot(groups=[(-100, "Sales", None)]))
    msg, _ = run_command(st, m.cmd_g, "/g marketing x")
    assert msg.sent == ["Không tìm thấy nhóm nào khớp với marketing."]


# ---- tìm chat qua Telethon (danh sách chat giả) ----

def test_userbot_find_users_by_display_name():
    pytest.importorskip("telethon")
    from telethon.tl.types import User

    from bot.userbot import Userbot

    def dialog(user):
        return SimpleNamespace(is_user=True, is_group=False, entity=user, id=user.id)

    dialogs = [
        dialog(User(id=1, first_name="Lan", last_name="Anh")),
        dialog(User(id=2, first_name="BotFather", bot=True)),
        dialog(User(id=3, first_name="lan", username="lan_ha")),
        dialog(User(id=7, first_name="Chủ", is_self=True)),
        SimpleNamespace(is_user=False, is_group=True, entity=None, id=-5),
    ]

    class Client:
        async def iter_dialogs(self):
            for d in dialogs:
                yield d

    ub = Userbot(1, "hash", "session", TZ)
    ub.client = Client()
    # Khớp đúng hoàn toàn đứng trước; bỏ bot, chính chủ bot và nhóm
    assert asyncio.run(ub.find_users("LAN")) == [(3, "lan", "lan_ha"), (1, "Lan Anh", None)]
    assert asyncio.run(ub.recent_users()) == [(1, "Lan Anh", None), (3, "lan", "lan_ha")]
    assert asyncio.run(ub.find_users("Hà")) == []


# ---- nhãn chat trong log ----

def test_format_chat():
    assert format_chat(-1001234, "Team Dev", "teamdev") == '"Team Dev" (@teamdev, -1001234)'
    assert format_chat(-1001234, "Team Dev", None) == '"Team Dev" (-1001234)'
    assert format_chat(55, None, "lan") == "@lan (55)"
    assert format_chat(55, None, None) == "55"


def test_chat_username_kept_when_not_given():
    s = Storage(":memory:")
    s.track_chat(-1, "Team A", "team_a")
    s.upsert_chat(-1, "Team A mới")  # cập nhật từ bot: không có username
    assert s.chat_label(-1) == '"Team A mới" (@team_a, -1)'
    s.track_chat(-1, None)
    assert s.chat_label(-1) == '"Team A mới" (@team_a, -1)'
    assert s.chat_label(-9) == "-9"


def test_migration_adds_username_column(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT, updated_at TEXT NOT NULL, "
        "tracked INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO chats VALUES (-1, 'Team A', 'x', 1);"
    )
    conn.commit()
    conn.close()
    s = Storage(path)
    assert s.chat_label(-1) == '"Team A" (-1)'
    s.upsert_chat(-1, "Team A", "team_a")
    assert s.chat_label(-1) == '"Team A" (@team_a, -1)'


# ---- menu lệnh ----

class FakeBot:
    def __init__(self):
        self.calls = []

    async def delete_my_commands(self, scope=None):
        self.calls.append(("delete", scope))

    async def set_my_commands(self, commands, scope=None):
        self.calls.append(("set", [c.command for c in commands], scope))


def test_bot_commands_only_in_owner_private_chat():
    bot = FakeBot()
    app = SimpleNamespace(bot=bot, bot_data={"state": make_state(FakeUserbot())})
    asyncio.run(m.set_bot_commands(app))
    deleted = [c[1] for c in bot.calls if c[0] == "delete"]
    assert any(isinstance(s, BotCommandScopeDefault) for s in deleted)
    assert any(isinstance(s, BotCommandScopeAllGroupChats) for s in deleted)
    (set_call,) = [c for c in bot.calls if c[0] == "set"]
    assert set_call[1] == ["p", "g", "e"]
    assert isinstance(set_call[2], BotCommandScopeChat) and set_call[2].chat_id == OWNER


def test_bot_commands_without_userbot_sets_no_menu():
    bot = FakeBot()
    asyncio.run(m.set_bot_commands(SimpleNamespace(bot=bot, bot_data={"state": make_state()})))
    assert [c[0] for c in bot.calls] == ["delete", "delete"]

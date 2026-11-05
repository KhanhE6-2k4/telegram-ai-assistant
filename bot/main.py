"""Điểm khởi chạy bot: python -m bot.main [--provider TÊN] [--model MODEL] [--base-url URL]

Vai trò:
- Userbot (bot/userbot.py, tài khoản của chủ bot): nguồn tin nhắn duy nhất, theo dõi và bù tin của
  các chat đang theo dõi (nhóm có bot, chat chủ bot đã mở chủ đề bằng /p, /g).
- Bot BotFather (file này): chỉ nhận yêu cầu và trả lời. Trong nhóm chỉ có tag @bot; lệnh "/" (/p, /g,
  /e: chủ đề hỏi đáp về một chat) chỉ có trong chat riêng của chủ bot. Không cấu hình userbot thì bot tự lưu tin nhóm (chế độ dự phòng).
"""
import argparse
import asyncio
import logging
import re
import secrets
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from telegram import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton,
    InlineKeyboardMarkup, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler,
    TypeHandler, filters,
)

from .config import Config, llm_settings, load_config, load_env_file
from .providers import available_providers, create_provider
from .qa import QA
from .review import review_sheet
from .sheets import FIELDS, SheetClient, TaskSheet, apply_edit, is_marked
from .storage import Storage
from .summarizer import pending_chars, process_day

log = logging.getLogger("bot")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Thư viện HTTP ghi log mỗi request: chỉ giữ cảnh báo
QUIET_LOGGERS = ("httpx", "httpx2")


class BotState:
    def __init__(self, config: Config):
        self.config = config
        self.storage = Storage(config.db_path)
        self.llm = create_provider(
            config.llm_provider, config.llm_model, api_key=config.llm_api_key, base_url=config.llm_base_url
        )
        # Mỗi chat một khoá: không xử lý song song, không ghi sheet chồng lên nhau
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # (chat_id, day) đang được xử lý (kể cả lúc chờ thử lại), để không khởi động thêm lượt trùng
        self.busy: set[tuple[int, str]] = set()
        # Tab của mỗi chat đặt theo tên chat (nhóm: tên nhóm, chat riêng: tên người kia), định danh bằng chat id
        self.sheets = SheetClient.open(
            config.google_credentials_file, config.spreadsheet_id, title_of=self.storage.chat_title,
            label_of=self.storage.chat_label,
        )
        # Đề xuất sửa task đang chờ xác nhận: id ngẫu nhiên -> {chat_id, user_id, stt, changes, current}
        self.pending_edits: dict[str, dict] = {}
        # Lệnh riêng chờ chủ bot bấm chọn người / nhóm:
        # id ngẫu nhiên -> {kind: "user" | "group", options: [(chat_id, tên, username)], request: str | None}
        self.pending_picks: dict[str, dict] = {}
        # Chủ đề đang mở trong chat riêng của chủ bot với bot (/p, /g), chỉ trong RAM. None = chưa có
        self.session = None
        self.qa = QA(self.storage, self.llm, self.sheets)
        # Userbot (tạo ở build_application, đăng nhập ở post_init). None = chế độ dự phòng
        self.userbot = None
        # Thời điểm bot bắt đầu chạy (làm tròn xuống giây, như ngày giờ tin nhắn Telegram)
        self.started_at = datetime.now(timezone.utc).replace(microsecond=0)

    def today(self) -> str:
        return datetime.now(self.config.timezone).date().isoformat()

    def task_sheet(self, chat_id: int) -> TaskSheet:
        return TaskSheet(self.sheets.worksheet_for(chat_id), self.storage, chat_id)

    async def compact(
        self, chat_id: int, day: str, finalize: bool, retry: bool = True, keep_recent: bool = True
    ) -> bool:
        """Ghi task lên sheet rồi tóm tắt. Thử lại tới COMPACT_MAX_ATTEMPTS lần cho đến khi
        cả LLM lẫn Google Sheet đều thành công. Trả về True nếu thành công.
        keep_recent=False: xử lý cả các tin gần nhất (không giữ lại ở dạng gốc)."""
        attempts = self.config.compact_max_attempts if retry else 1
        self.busy.add((chat_id, day))
        try:
            for attempt in range(1, attempts + 1):
                async with self.locks[chat_id]:
                    try:
                        sheet = await asyncio.to_thread(self.task_sheet, chat_id)
                        await process_day(self.storage, sheet, self.llm, chat_id, day, finalize, keep_recent)
                        return True
                    except Exception:
                        log.exception(
                            "Xử lý chat %s ngày %s thất bại (lần %d/%d)",
                            self.storage.chat_label(chat_id), day, attempt, attempts,
                        )
                        # Có thể tab đã bị xoá / đổi trên sheet: lần thử sau tra lại tab từ đầu
                        self.sheets.forget(chat_id)
                if attempt < attempts:
                    # Chờ ngoài khoá để các thao tác khác (bấm Xác nhận...) không bị kẹt
                    await asyncio.sleep(self.config.compact_retry_delay_seconds * 2 ** (attempt - 1))
            log.error(
                "Bỏ qua chat %s ngày %s sau %d lần thử, sẽ xử lý lại ở lần chốt sau",
                self.storage.chat_label(chat_id), day, attempts,
            )
            return False
        finally:
            self.busy.discard((chat_id, day))

    async def review(self, chat_id: int) -> None:
        """Rà soát sheet của chat (thiếu người thực hiện, deadline chưa chuẩn, dòng không phải task,
        task trùng, dòng thiếu STT). Lỗi thì ghi log; lần khởi động sau rà lại."""
        async with self.locks[chat_id]:
            try:
                sheet = await asyncio.to_thread(self.task_sheet, chat_id)
                await review_sheet(self.storage, sheet, self.llm, chat_id, self.today())
            except Exception:
                log.exception("Rà soát sheet của chat %s thất bại", self.storage.chat_label(chat_id))
                self.sheets.forget(chat_id)


def state_of(context: ContextTypes.DEFAULT_TYPE) -> BotState:
    return context.application.bot_data["state"]


def active_userbot(st):
    """Userbot đã đăng nhập xong, hoặc None (chưa cấu hình / lỗi khi khởi động: chế độ dự phòng)."""
    ub = getattr(st, "userbot", None)
    return ub if ub is not None and ub.me is not None else None


GROUP_CHAT_TYPES = ("group", "supergroup")


# ---- lưu tin, theo dõi chat, bù tin ----

def ingest(app, st, fields: dict, edited: bool, compact: bool = True) -> None:
    """Lưu một tin hội thoại (mới hoặc bị sửa), dùng chung cho userbot và chế độ dự phòng.

    UNIQUE (chat_id, message_id) chặn tin trùng; tin mới / tin sửa của ngày đã chốt mở lại ngày đó.
    compact=True: hôm nay vượt COMPACT_THRESHOLD_CHARS thì nén giữa ngày (chạy nền qua app.create_task).
    """
    if edited:
        st.storage.edit_message(**fields)
    else:
        st.storage.add_message(**fields)
    chat_id, day = fields["chat_id"], fields["sent_at"].date().isoformat()
    # Ngày cũ (tin sửa lại) để lần chốt 00:01 xử lý; chỉ nén giữa ngày cho hôm nay
    if (
        compact
        and day == st.today()
        and (chat_id, day) not in st.busy
        and pending_chars(st.storage, chat_id, day) > st.config.compact_threshold_chars
    ):
        app.create_task(st.compact(chat_id, day, finalize=False))


def track_chat(st, chat_id: int, title: str | None, username: str | None = None) -> bool:
    """Đánh dấu userbot theo dõi chat (DB + tập trong bộ nhớ). True nếu trước đó chưa theo dõi."""
    new = st.storage.track_chat(chat_id, title, username)
    ub = getattr(st, "userbot", None)
    if ub is not None:
        ub.tracked.add(chat_id)
    if new:
        log.info("Bắt đầu theo dõi chat %s", st.storage.chat_label(chat_id))
    return new


def untrack_chat(st, chat_id: int) -> None:
    st.storage.untrack_chat(chat_id)
    ub = getattr(st, "userbot", None)
    if ub is not None:
        ub.tracked.discard(chat_id)


async def supplement_chat(app, st, chat_id: int, before: datetime | None = None) -> int:
    """Bù tin của chat qua userbot: từ tin cuối đã lưu (gửi trước `before`, mặc định bây giờ), chat chưa
    có tin thì từ USERBOT_HISTORY_DAYS ngày trước. Không nén ở đây: người gọi tự xử lý. Trả về số tin lấy được."""
    ub = active_userbot(st)
    if ub is None:
        return 0
    tz = st.config.timezone
    last = st.storage.last_sent_at(chat_id, before or datetime.now(tz))
    since = last or datetime.now(tz) - timedelta(days=st.config.userbot_history_days)
    fetched = await ub.fetch_since(chat_id, since)
    for fields in fetched or []:
        ingest(app, st, fields, edited=False, compact=False)
    if fetched:
        log.info("Đã bù %d tin của chat %s", len(fetched), st.storage.chat_label(chat_id))
    return len(fetched or [])


async def supplement_tracked(app, st) -> None:
    """Bù tin cho mọi chat đang theo dõi. Lấy từ tin cuối gửi trước lúc userbot khởi động: tin userbot
    nhận trực tiếp sau đó không làm bỏ sót các tin gửi lúc bot tắt."""
    ub = active_userbot(st)
    if ub is None:
        return
    for chat_id in st.storage.tracked_chats():
        try:
            await supplement_chat(app, st, chat_id, before=ub.started_at)
        except Exception:
            log.exception("Bù tin chat %s thất bại", st.storage.chat_label(chat_id))


def start_supplement(app, st, chat_id: int) -> None:
    """Chat vừa được theo dõi: bù tin nền (lịch sử USERBOT_HISTORY_DAYS ngày)."""
    if active_userbot(st) is not None:
        app.create_task(supplement_chat(app, st, chat_id, before=datetime.now(st.config.timezone)))


async def remember_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lưu tên nhóm từ mọi cập nhật (tin thường, tin tag bot, nút bấm, bot được thêm vào nhóm) và đánh
    dấu theo dõi, để tab tạo lần đầu trên sheet đã mang tên nhóm và userbot thu tin của nhóm."""
    chat = update.effective_chat
    member = getattr(update, "my_chat_member", None)
    if member and member.new_chat_member.status in ("left", "kicked"):
        return  # bot bị xoá khỏi nhóm: on_my_chat_member thôi theo dõi
    if chat and chat.type in GROUP_CHAT_TYPES and chat.title:
        st = state_of(context)
        if track_chat(st, chat.id, chat.title, getattr(chat, "username", None)):
            start_supplement(context.application, st, chat.id)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bot bị xoá khỏi nhóm: thôi theo dõi (bot được thêm vào thì remember_chat đã đánh dấu theo dõi)."""
    cm = update.my_chat_member
    if cm.chat.type in GROUP_CHAT_TYPES and cm.new_chat_member.status in ("left", "kicked"):
        untrack_chat(state_of(context), cm.chat.id)


# ---- tin trong nhóm: chỉ có tag @bot ----

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    text = msg.text or msg.caption
    if not text:
        return
    st = state_of(context)
    # Tin tag @bot là yêu cầu gửi bot, không phải hội thoại của nhóm: không lưu (để bước ghi task không
    # làm theo một yêu cầu sửa chưa được xác nhận). Tin tag được sửa lại thì bỏ qua.
    if mentions_bot(msg, context.bot):
        if update.edited_message:
            return
        if sent_while_offline(msg.date, st.started_at):
            context.application.create_task(msg.reply_text(OFFLINE_REPLY))
            return
        question = strip_mention(text, context.bot.username)
        context.application.create_task(handle_mention(update, context, question))
        return
    if active_userbot(st) is not None:
        return  # userbot là nguồn tin: bot không lưu nữa
    # Chế độ dự phòng (chưa cấu hình userbot): bot tự lưu tin nhóm
    user = msg.from_user
    fields = dict(
        chat_id=chat.id,
        message_id=msg.message_id,
        user_id=user.id if user else None,
        user_name=(user.full_name if user else None),
        text=text,
        reply_to=msg.reply_to_message.message_id if msg.reply_to_message else None,
        sent_at=msg.date.astimezone(st.config.timezone),
    )
    ingest(context.application, st, fields, edited=bool(update.edited_message))


OFFLINE_REPLY = (
    "Lúc bạn gửi câu hỏi này tôi đang tạm ngừng hoạt động nên không trả lời được. "
    "Tôi đã hoạt động trở lại, bạn vui lòng hỏi lại nhé."
)
OFFLINE_COMMAND_REPLY = (
    "Lúc bạn gửi lệnh này tôi đang tạm ngừng hoạt động nên chưa thực hiện. "
    "Tôi đã hoạt động trở lại, bạn vui lòng gõ lại lệnh nhé."
)


def sent_while_offline(sent_at: datetime, started_at: datetime) -> bool:
    """Tin được gửi trước khi bot khởi động, tức lúc bot đang tắt."""
    return sent_at < started_at


def mention_help(username: str) -> str:
    """Hướng dẫn cú pháp tag bot (tag trống hoặc từ khoá lạ)."""
    bot = f"@{username}"
    return (
        "Cách dùng:\n"
        f"- {bot} <summarize> [YYYY-MM-DD]: cập nhật tin mới lên sheet, xem task giao / cập nhật trong ngày "
        "(mặc định hôm nay)\n"
        f"- {bot} <task> [tên]: xem mọi task của nhóm, hoặc chỉ task của một người\n"
        f"- {bot} <câu hỏi hoặc yêu cầu>: hỏi về task / nội dung trao đổi, hoặc nhờ sửa task\n"
        "Ví dụ:\n"
        f"{bot} <summarize> 2026-11-01\n"
        f"{bot} <task> Lan\n"
        f"{bot} task báo cáo Q3 hạn khi nào?\n"
        f"{bot} dời hạn task báo cáo Q3 sang 10/11"
    )


def route_mention(question: str) -> tuple[str, str]:
    """Phân loại yêu cầu trong tin tag bot theo từ khoá trong ngoặc nhọn ở đầu (không phân biệt hoa thường):
    "<summarize> [ngày]" -> ("summary", ngày), "<task> [tên]" -> ("tasks", tên), không có từ khoá
    -> ("ask", câu hỏi), trống / từ khoá lạ / ngày sai -> ("help", "")."""
    question = question.strip()
    if not question:
        return "help", ""
    m = re.match(r"<([^<>\s]*)>(.*)", question, flags=re.DOTALL)
    if not m:
        return "ask", question
    keyword, arg = m[1].casefold(), m[2].strip()
    if keyword == "summarize":
        if arg:
            try:
                arg = date.fromisoformat(arg).isoformat()
            except ValueError:
                return "help", ""
        return "summary", arg
    if keyword == "task":
        return "tasks", arg
    return "help", ""


async def handle_mention(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    kind, arg = route_mention(question)
    if kind == "summary":
        await do_summary(update, context, arg or state_of(context).today())
    elif kind == "tasks":
        await do_tasks(update, context, arg)
    elif kind == "ask":
        await answer_question(update, context, question)
    else:
        await update.effective_message.reply_text(mention_help(context.bot.username))


def mentions_bot(msg, bot) -> bool:
    """Tin nhắn có tag @bot."""
    mention = f"@{bot.username}".lower()
    entities = msg.parse_entities(["mention"]) if msg.text else msg.parse_caption_entities(["mention"])
    return any(v.lower() == mention for v in entities.values())


def strip_mention(text: str, username: str) -> str:
    """Câu hỏi trong tin tag bot: bỏ mọi chỗ "@bot" (không phân biệt hoa thường), giữ nguyên xuống dòng.
    ("@ant_bot  task Q3 hạn khi nào?" -> "task Q3 hạn khi nào?"; "@ant_bot_khac" không bị bỏ.)"""
    text = re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE)
    return "\n".join(re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.split("\n")).strip()


def command_argument(text: str) -> str:
    """Phần sau tên lệnh ("/g@bot  @nhom yêu cầu\\ndòng 2" -> "@nhom yêu cầu\\ndòng 2"), giữ nguyên xuống dòng."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def answer_question(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    st = state_of(context)
    msg = update.effective_message
    chat = update.effective_chat
    await context.bot.send_chat_action(chat.id, "typing")
    proposals = []
    try:
        can_edit = await can_edit_tasks(context, chat.id, msg.from_user.id)
        reply, proposals = await st.qa.answer(
            chat.id, chat.title or "", st.today(), msg.from_user.full_name, question, can_edit
        )
    except Exception:
        log.exception("Trả lời thất bại")
        reply = "Xin lỗi, mình đang gặp lỗi khi tra cứu. Bạn thử lại sau nhé."
    await send_answer(st, msg, chat.id, msg.from_user.id, reply, proposals)


async def send_answer(
    st, msg, chat_id: int, user_id: int, reply: str, proposals: list[dict], footer: str | None = None
) -> None:
    """Gửi câu trả lời (reply vào `msg`) và nút xác nhận cho từng đề xuất sửa task của chat `chat_id`.
    Chỉ `user_id` bấm xác nhận được. Nút có thể nằm ở chat khác chat của task (chat riêng của chủ bot).
    `footer`: dòng cuối (cách một dòng trống) của câu trả lời; câu trả lời dài bị tách thì nằm ở tin cuối."""
    text = reply or "Mình chưa có câu trả lời."
    if footer:
        text = f"{text}\n\n{footer}"
    for chunk in split_message(text.split("\n")):
        await msg.reply_text(chunk)
    for p in proposals:
        # Mã ngẫu nhiên: nút của lần chạy trước không thể trùng với đề xuất mới
        edit_id = secrets.token_hex(8)
        st.pending_edits[edit_id] = {"chat_id": chat_id, "user_id": user_id, **p}
        lines = [f"Xác nhận sửa task #{p['stt']} ({p['current']['name']}):"]
        for field, value in p["changes"].items():
            lines.append(f"- {FIELD_LABELS[field]}: {p['current'][field] or '(trống)'} → {value}")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Xác nhận", callback_data=f"edit:{edit_id}:ok"),
            InlineKeyboardButton("Huỷ", callback_data=f"edit:{edit_id}:cancel"),
        ]])
        await msg.reply_text("\n".join(lines), reply_markup=keyboard)


FIELD_LABELS = dict(zip(FIELDS, ["Tên task", "Người thực hiện", "Nội dung", "Tiến độ", "Deadline"]))


async def can_edit_tasks(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Admin của nhóm Telegram, hoặc user id nằm trong EDITOR_USER_IDS."""
    if user_id in state_of(context).config.editor_user_ids:
        return True
    member = await context.bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")


async def on_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = state_of(context)
    query = update.callback_query
    _, edit_id, action = query.data.split(":")
    edit = st.pending_edits.get(edit_id)
    if edit is None:
        await query.answer("Yêu cầu này đã hết hạn.")
        return
    if query.from_user.id != edit["user_id"]:
        await query.answer("Chỉ người yêu cầu mới xác nhận được.", show_alert=True)
        return
    del st.pending_edits[edit_id]
    if action != "ok":
        await query.answer()
        await query.edit_message_text(query.message.text + "\n\nĐã huỷ.")
        return
    chat_id, today = edit["chat_id"], st.today()
    try:
        async with st.locks[chat_id]:
            await asyncio.to_thread(
                lambda: apply_edit(
                    st.storage, st.sheets.worksheet_for(chat_id), chat_id, edit["stt"], edit["changes"], today
                )
            )
    except Exception as e:
        log.exception("Sửa task thất bại")
        st.sheets.forget(chat_id)
        await query.answer()
        await query.edit_message_text(query.message.text + f"\n\nLỗi khi ghi sheet: {e}")
        return
    await query.answer("Đã cập nhật")
    await query.edit_message_text(
        query.message.text.replace("Xác nhận sửa", "Đã sửa", 1) + f"\n\nNgười xác nhận: {query.from_user.full_name}"
    )


def format_task(t: dict) -> str:
    return (
        f"#{t['stt']}. {t['name']}: {t['description']} | Người thực hiện: {t['assignee'] or '-'} "
        f"| Tiến độ: {t['progress'] or '-'} | Deadline: {t['deadline'] or '-'}"
    )


# Telegram giới hạn 4096 ký tự mỗi tin nhắn
MAX_MESSAGE_CHARS = 4000


def split_message(lines: list[str], limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Ghép các dòng thành các tin nhắn không vượt giới hạn độ dài của Telegram."""
    chunks, current = [], ""
    for line in lines:
        line = line[:limit]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def do_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, who: str) -> None:
    """@bot <task> [tên người]: liệt kê mọi task của nhóm, đọc thẳng từ sheet (không gọi LLM).
    Bỏ qua dòng không phải task (Người thực hiện = "Tất cả") và dòng trùng."""
    st = state_of(context)
    msg = update.effective_message
    chat_id = update.effective_chat.id
    who = who.strip()
    try:
        rows = (await asyncio.to_thread(st.task_sheet, chat_id)).rows()
    except Exception:
        log.exception("Đọc sheet thất bại")
        await msg.reply_text("Xin lỗi, tôi chưa đọc được Google Sheet. Bạn thử lại sau nhé.")
        return
    tasks = [t for t in rows if not is_marked(t)]
    if who:
        tasks = [t for t in tasks if who.lower() in t["assignee"].lower()]

    title = f"Task của {who}" if who else "Tất cả task của nhóm"
    lines = [f"{title} ({len(tasks)}):"] + [format_task(t) for t in sorted(tasks, key=lambda t: t["stt"])]
    if not tasks:
        lines.append("(không có)")
    if pending_chars(st.storage, chat_id, st.today()) > 0:
        lines += [
            "",
            "Còn tin nhắn hôm nay chưa xử lý, task trong đó chưa có ở đây. "
            f"Gõ @{context.bot.username} <summarize> để cập nhật.",
        ]
    for chunk in split_message(lines):
        await msg.reply_text(chunk)


async def do_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, day: str) -> None:
    """@bot <summarize> [YYYY-MM-DD]: xem task của một ngày - được giao hoặc có cập nhật (mặc định hôm nay)."""
    st = state_of(context)
    chat_id = update.effective_chat.id
    # Với hôm nay, xử lý hết các tin nhắn mới (kể cả tin vừa gửi) để task lên sheet và tóm tắt đầy đủ
    # ngay. Không chờ thử lại để người gọi không phải đợi; lỗi thì lần chốt sau xử lý.
    if day == st.today():
        await st.compact(chat_id, day, finalize=False, retry=False, keep_recent=False)
    try:
        tasks = [
            t for t in (await asyncio.to_thread(st.task_sheet, chat_id)).rows()
            if day in (t["created_day"], t["updated_day"])
        ]
    except Exception:
        log.exception("Đọc sheet thất bại")
        tasks = []
    lines = [f"Task giao hoặc cập nhật ngày {day}:"]
    lines += [format_task(t) for t in tasks]
    if not tasks:
        lines.append("(không có)")
    for chunk in split_message(lines):
        await update.effective_message.reply_text(chunk)


# ---- chủ đề trong chat riêng của chủ bot với bot: /p, /g, /e ----

# /p hoặc /g mở một chủ đề về một chat; sau đó mọi tin thường của chủ bot trong chat riêng với bot là câu
# hỏi tiếp của chủ đề đó (bot nhớ các lượt trước). Mỗi lúc chỉ một chủ đề, chỉ lưu trong RAM: mất khi
# gõ /e, khi bot khởi động lại, hoặc sau SESSION_TTL không trao đổi.
SESSION_TTL = timedelta(hours=1)
# Số lượt hỏi đáp gần nhất gửi kèm cho LLM làm ngữ cảnh
MAX_SESSION_TURNS = 10
SESSION_FOOTER = "Muốn kết thúc chủ đề này thì nhập /e"

PERSONAL_HELP = (
    "Cách dùng /p (chủ đề về chat riêng với một người):\n"
    "- Bấm /p rồi chọn người trong danh sách; sau đó cứ nhắn câu hỏi, bot trả lời theo chủ đề đó.\n"
    "- Hoặc gõ: /p <người> [câu hỏi đầu tiên]. <người> là @username (tên người dùng Telegram, không phải "
    "tên hiển thị), user id, số điện thoại (+84...) hoặc tên hiển thị; tên có dấu cách thì đặt trong ngoặc kép.\n"
    "Ví dụ: /p @lan tóm tắt việc cần làm\n"
    '/p "Nguyễn Văn A"\n'
    "Kết thúc chủ đề: /e"
)
GROUP_HELP = (
    "Cách dùng /g (chủ đề về một nhóm):\n"
    "- Bấm /g rồi chọn nhóm trong danh sách; sau đó cứ nhắn câu hỏi, bot trả lời theo chủ đề đó.\n"
    "- Hoặc gõ: /g <nhóm> [câu hỏi đầu tiên]. <nhóm> là @username của nhóm hoặc một phần tên nhóm; "
    "tên có dấu cách thì đặt trong ngoặc kép.\n"
    "Ví dụ: /g @team_mkt task nào sắp tới hạn?\n"
    '/g "Team Marketing"\n'
    "Kết thúc chủ đề: /e"
)
NO_USERBOT_REPLY = (
    "Chưa cấu hình userbot (TELEGRAM_API_ID, TELEGRAM_API_HASH, USERBOT_SESSION) nên chưa dùng được lệnh này."
)
NOT_OWNER_REPLY = "Lệnh này chỉ dành cho chủ bot."
NO_SESSION_REPLY = "Chưa có chủ đề nào. Dùng /p (chat riêng với một người) hoặc /g (nhóm) để bắt đầu."
# Số chat tối đa hiện thành nút chọn
MAX_PICK_BUTTONS = 10
MAX_PENDING_PICKS = 100


@dataclass
class Session:
    """Chủ đề đang mở: chat được hỏi và các lượt (câu hỏi, câu trả lời) đã có."""

    kind: str  # "user" | "group"
    option: tuple  # (chat_id, tên, username)
    last_active: datetime
    history: list[tuple[str, str]] = field(default_factory=list)
    # Câu hỏi gửi liên tiếp được trả lời lần lượt, lượt sau thấy lượt trước
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def title(self) -> str:
        return target_title(self.kind, self.option[1])

    def expired(self, now: datetime) -> bool:
        return now - self.last_active > SESSION_TTL


def parse_target_argument(arg: str) -> tuple[str, str]:
    """'"Team A" yêu cầu' -> ("Team A", "yêu cầu"); '@team yêu cầu' / 'team yêu cầu' -> từ đầu, phần còn lại;
    chỉ có đối tượng thì yêu cầu trống."""
    m = re.match(r'"([^"]+)"\s*(.*)', arg, flags=re.DOTALL)
    if m:
        return m[1].strip(), m[2].strip()
    parts = arg.split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def target_title(kind: str, name: str) -> str:
    """Tên chat đưa vào câu trả lời / prompt: chat riêng thì "Chat riêng với <tên>", nhóm thì tên nhóm."""
    return f"Chat riêng với {name}" if kind == "user" else name


def option_label(option: tuple) -> str:
    """Chữ trên nút chọn: tên, kèm @username nếu có (phân biệt người / nhóm trùng tên)."""
    _, name, username = option
    return f"{name} (@{username})" if username else name


def remember_limited(store: dict, key, value, limit: int = MAX_PENDING_PICKS) -> None:
    if len(store) >= limit:
        store.pop(next(iter(store)))  # bỏ mục cũ nhất
    store[key] = value


async def check_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra chung của lệnh riêng; trả về userbot nếu được chạy, ngược lại đã trả lời và trả về None."""
    st = state_of(context)
    msg = update.effective_message
    if sent_while_offline(msg.date, st.started_at):
        await msg.reply_text(OFFLINE_COMMAND_REPLY)
        return None
    ub = active_userbot(st)
    if ub is None:
        await msg.reply_text(NO_USERBOT_REPLY)
        return None
    if update.effective_user is None or update.effective_user.id != ub.owner_id:
        await msg.reply_text(NOT_OWNER_REPLY)
        return None
    return ub


async def cmd_p(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/p [người] [câu hỏi]: mở chủ đề về chat riêng giữa chủ bot và một người."""
    ub = await check_private_command(update, context)
    if ub is None:
        return
    msg = update.effective_message
    ref, request = parse_target_argument(command_argument(msg.text or ""))
    try:
        options = await ub.find_users(ref) if ref else await ub.recent_users()
    except Exception:
        log.exception("Tìm người dùng thất bại")
        options = []
    if not options:
        if ref:
            await msg.reply_text(
                f"Không tìm thấy ai có tên / username {ref} trong các chat riêng của bạn. "
                "Bấm /p (không kèm gì) để chọn từ danh sách chat gần đây."
            )
        else:
            await msg.reply_text(PERSONAL_HELP)
        return
    await offer_targets(context, msg, "user", ref, options, request)


async def cmd_g(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/g [nhóm] [câu hỏi]: mở chủ đề về một nhóm chủ bot tham gia."""
    ub = await check_private_command(update, context)
    if ub is None:
        return
    st = state_of(context)
    msg = update.effective_message
    ref, request = parse_target_argument(command_argument(msg.text or ""))
    try:
        if ref:
            options = await ub.find_groups(ref)
        else:
            # Nhóm đang theo dõi trước, rồi các nhóm gần đây khác
            options = list(st.storage.tracked_groups())
            seen = {o[0] for o in options}
            options += [o for o in await ub.recent_groups() if o[0] not in seen]
    except Exception:
        log.exception("Tìm nhóm thất bại")
        options = []
    if not options:
        await msg.reply_text(f"Không tìm thấy nhóm nào khớp với {ref}." if ref else GROUP_HELP)
        return
    await offer_targets(context, msg, "group", ref, options, request)


async def cmd_e(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/e: kết thúc chủ đề đang mở, xoá ngữ cảnh."""
    if await check_private_command(update, context) is None:
        return
    st = state_of(context)
    session, st.session = st.session, None
    if session is None:
        await update.effective_message.reply_text("Không có chủ đề nào đang mở.")
        return
    log.info("Kết thúc chủ đề về chat %s", st.storage.chat_label(session.option[0]))
    await update.effective_message.reply_text(f"Đã kết thúc chủ đề: {session.title}.")


async def offer_targets(context, msg, kind: str, ref: str, options: list[tuple], request: str) -> None:
    """Một kết quả tìm theo tên -> mở chủ đề luôn; còn lại (danh sách gần đây, nhiều kết quả) -> nút chọn."""
    if ref and len(options) == 1:
        await start_session(context, msg, kind, options[0], request)
        return
    st = state_of(context)
    pick_id = secrets.token_hex(8)
    shown = options[:MAX_PICK_BUTTONS]
    remember_limited(st.pending_picks, pick_id, {"kind": kind, "options": shown, "request": request or None})
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(option_label(o), callback_data=f"pick:{pick_id}:{i}")] for i, o in enumerate(shown)]
    )
    noun = "người" if kind == "user" else "nhóm"
    if ref:
        more = f" (hiện {MAX_PICK_BUTTONS} đầu, gõ cụ thể hơn nếu không thấy)" if len(options) > len(shown) else ""
        text = f"Có {len(options)} {noun} khớp với {ref}{more}. Chọn {noun}:"
    else:
        command = "/p" if kind == "user" else "/g"
        text = f'Chọn {noun} (không thấy thì gõ {command} "tên" để tìm):'
    await msg.reply_text(text, reply_markup=keyboard)


async def on_pick_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chủ bot bấm chọn người / nhóm: mở chủ đề về chat đó."""
    st = state_of(context)
    query = update.callback_query
    ub = active_userbot(st)
    if ub is None or query.from_user.id != ub.owner_id:
        await query.answer(NOT_OWNER_REPLY, show_alert=True)
        return
    _, pick_id, index = query.data.split(":")
    pick = st.pending_picks.pop(pick_id, None)
    if pick is None or not index.isdigit() or int(index) >= len(pick["options"]):
        await query.answer("Yêu cầu này đã hết hạn.")
        return
    option = pick["options"][int(index)]
    await query.answer()
    await query.edit_message_text(f"Đã chọn: {option_label(option)}")
    await start_session(context, query.message, pick["kind"], option, pick["request"])


async def start_session(context, msg, kind: str, option: tuple, request: str | None) -> None:
    """Mở chủ đề mới (thay chủ đề cũ, ngữ cảnh cũ bị bỏ), chuẩn bị dữ liệu của chat, rồi trả lời câu hỏi
    đầu tiên nếu có."""
    st = state_of(context)
    session = Session(kind, option, last_active=datetime.now(timezone.utc))
    st.session = session
    async with session.lock:
        await context.bot.send_chat_action(msg.chat_id, "typing")
        try:
            await prepare_chat(context, option)
        except Exception:
            log.exception("Chuẩn bị dữ liệu chat %s thất bại", st.storage.chat_label(option[0]))
    # Sau prepare_chat: tên / username của chat đã được lưu nên nhãn trong log đầy đủ
    log.info("Bắt đầu chủ đề về chat %s", st.storage.chat_label(option[0]))
    if request:
        await ask_in_session(context, msg, session, request)
    else:
        await msg.reply_text(f"Đã bắt đầu chủ đề: {session.title}. Bạn hỏi gì cứ nhắn.\n\n{SESSION_FOOTER}")


async def prepare_chat(context, option: tuple) -> None:
    """Theo dõi chat, bù tin, xử lý mọi ngày còn tin chưa xử lý (ghi task lên sheet, tóm tắt), để câu hỏi
    trong chủ đề có dữ liệu đầy đủ ngay từ đầu."""
    st = state_of(context)
    chat_id, name, username = option
    before = datetime.now(st.config.timezone)
    track_chat(st, chat_id, name, username)
    await supplement_chat(context.application, st, chat_id, before=before)
    today = st.today()
    for day in st.storage.pending_days(chat_id):
        if (chat_id, day) not in st.busy:
            await st.compact(chat_id, day, finalize=day < today, retry=False, keep_recent=False)


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tin thường của chủ bot trong chat riêng với bot: câu hỏi tiếp của chủ đề đang mở.
    Tin của người khác thì bỏ qua."""
    st = state_of(context)
    msg = update.effective_message
    ub = active_userbot(st)
    if ub is None or msg.from_user is None or msg.from_user.id != ub.owner_id:
        return
    if sent_while_offline(msg.date, st.started_at):
        await msg.reply_text(OFFLINE_REPLY)
        return
    session = st.session
    if session is None:
        await msg.reply_text(NO_SESSION_REPLY)
        return
    if session.expired(datetime.now(timezone.utc)):
        st.session = None
        log.info("Chủ đề về chat %s hết hạn", st.storage.chat_label(session.option[0]))
        await msg.reply_text(
            f"Chủ đề {session.title} đã hết hạn (quá 1 giờ không trao đổi). Dùng /p hoặc /g để bắt đầu lại."
        )
        return
    request = (msg.text or "").strip()
    if request:
        await ask_in_session(context, msg, session, request)


async def ask_in_session(context, msg, session: Session, request: str) -> None:
    """Trả lời một câu hỏi trong chủ đề, kèm các lượt trước làm ngữ cảnh (chủ bot có quyền sửa task)."""
    st = state_of(context)
    ub = active_userbot(st)
    chat_id = session.option[0]
    async with session.lock:
        session.last_active = datetime.now(timezone.utc)
        await context.bot.send_chat_action(msg.chat_id, "typing")
        proposals = []
        try:
            reply, proposals = await st.qa.answer(
                chat_id, session.title, st.today(), ub.owner_name, request, can_edit=True,
                history=session.history[-MAX_SESSION_TURNS:],
            )
        except Exception:
            log.exception("Trả lời trong chủ đề về chat %s thất bại", st.storage.chat_label(chat_id))
            reply = "Xin lỗi, mình đang gặp lỗi khi xử lý yêu cầu. Bạn thử lại sau nhé."
        else:
            # Chủ đề đã bị /e hoặc bị thay trong lúc trả lời: không ghi vào chủ đề đã bỏ
            if st.session is session and reply:
                session.history.append((request, reply))
                del session.history[:-MAX_SESSION_TURNS]
                session.last_active = datetime.now(timezone.utc)
        await send_answer(st, msg, chat_id, ub.owner_id, reply, proposals, footer=SESSION_FOOTER)


# ---- lịch chạy ----

async def finalize_past_days(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chốt mọi ngày trước hôm nay chưa được chốt: ghi task lên sheet + tóm tắt.

    Chạy lúc DAILY_SUMMARY_TIME (mặc định 00:01) và một lần khi khởi động. Ngày hôm qua luôn được
    chốt, kể cả khi trong ngày đã có <summarize> hay đã nén vì vượt ngưỡng; ngày có tin bị sửa sau khi
    chốt cũng được mở lại và chốt lại ở đây.
    """
    st = state_of(context)
    for chat_id, day in st.storage.unfinalized_days_before(st.today()):
        if (chat_id, day) not in st.busy:
            await st.compact(chat_id, day, finalize=True)


# Chờ Telegram gửi bù các cập nhật lúc bot tắt rồi mới xử lý, để không bỏ sót chúng
STARTUP_DELAY_SECONDS = 10


async def on_startup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chạy một lần sau khi khởi động:
    0. Userbot bù tin gửi lúc bot tắt cho mọi chat đang theo dõi.
    1. Chốt bù các ngày trước hôm nay còn dở.
    2. Xử lý hết tin nhắn chưa xử lý của hôm nay (như <summarize>, nhưng không chốt ngày).
    3. Rà soát sheet của từng chat (xem bot/review.py). Sheet không đổi từ lần rà trước thì bỏ qua.
    """
    st = state_of(context)
    await supplement_tracked(context.application, st)
    await finalize_past_days(context)
    today = st.today()
    for chat_id in st.storage.chats_with_messages_on(today):
        if (chat_id, today) not in st.busy and pending_chars(st.storage, chat_id, today) > 0:
            await st.compact(chat_id, today, finalize=False, keep_recent=False)
    for chat_id in st.storage.chats_with_tasks():
        await st.review(chat_id)


# Menu lệnh chỉ hiện trong chat riêng của chủ bot; trong nhóm không gợi ý lệnh nào
PRIVATE_COMMANDS = [
    ("p", "Chủ đề về chat riêng với một người (bấm để chọn người)"),
    ("g", "Chủ đề về một nhóm (bấm để chọn nhóm)"),
    ("e", "Kết thúc chủ đề đang mở"),
]


async def set_bot_commands(app: Application) -> None:
    """Menu lệnh khi gõ "/": xoá menu mặc định và menu nhóm; hai lệnh riêng chỉ ở chat của chủ bot."""
    try:
        await app.bot.delete_my_commands(scope=BotCommandScopeDefault())
        await app.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
        ub = active_userbot(app.bot_data.get("state"))
        if ub is not None:
            await app.bot.set_my_commands(
                [BotCommand(name, text) for name, text in PRIVATE_COMMANDS], scope=BotCommandScopeChat(ub.owner_id)
            )
    except Exception:
        log.warning("Không đăng ký được menu lệnh", exc_info=True)


async def start_userbot(app: Application) -> None:
    """Đăng nhập userbot (nếu có cấu hình) và nạp các chat đang theo dõi. Lỗi thì dùng chế độ dự phòng."""
    st = app.bot_data["state"]
    ub = st.userbot
    if ub is None:
        return
    try:
        await ub.start()
        ub.set_bot(app.bot.id, app.bot.username)
        ub.tracked = set(st.storage.tracked_chats())
    except Exception:
        log.exception("Userbot không khởi động được: bot tự lưu tin nhóm (chế độ dự phòng)")
        try:
            await ub.stop()
        except Exception:
            pass
        st.userbot = None


async def stop_userbot(app: Application) -> None:
    st = app.bot_data.get("state")
    if st is not None and st.userbot is not None:
        try:
            await st.userbot.stop()
        except Exception:
            log.warning("Lỗi khi dừng userbot", exc_info=True)


async def post_init(app: Application) -> None:
    """Sau khi kết nối Telegram (CLI và app desktop): userbot rồi menu lệnh (cần biết chủ bot)."""
    await start_userbot(app)
    await set_bot_commands(app)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m bot.main",
        description="Bot Telegram ghi task và tóm tắt hội thoại. Tham số ghi đè biến môi trường tương ứng.",
    )
    parser.add_argument("--provider", choices=available_providers(), help="Nhà cung cấp LLM (LLM_PROVIDER)")
    parser.add_argument("--model", help="Tên model (LLM_MODEL); bỏ trống thì dùng mặc định của provider")
    parser.add_argument("--base-url", help="Endpoint API tuỳ chỉnh (LLM_BASE_URL), ví dụ dịch vụ tương thích OpenAI")
    parser.add_argument(
        "--check", action="store_true",
        help="Chỉ thử gọi LLM (trả lời + gọi công cụ) rồi thoát, không cần Telegram hay Google Sheet",
    )
    return parser.parse_args(argv)


def build_application(config: Config) -> Application:
    """Tạo Application đầy đủ handler, BotState, userbot (chưa đăng nhập) và lịch chạy (chưa kết nối Telegram).

    Dùng chung cho bản dòng lệnh (run_polling) và app desktop (bot/runner.py). Mở Google Sheet nên
    cần mạng. post_init / post_shutdown chỉ được run_polling gọi; runner tự gọi post_init và stop_userbot.
    """
    app = (
        Application.builder().token(config.telegram_token).post_init(post_init).post_shutdown(stop_userbot).build()
    )
    st = BotState(config)
    app.bot_data["state"] = st
    if config.userbot_configured:
        from .userbot import Userbot

        st.userbot = Userbot(
            config.telegram_api_id, config.telegram_api_hash, config.userbot_session, config.timezone,
            on_message=lambda fields, edited: ingest(app, st, fields, edited),
            on_chat=st.storage.upsert_chat, label_of=st.storage.chat_label,
        )
    else:
        log.info("Chưa cấu hình userbot: bot tự lưu tin nhóm (chế độ dự phòng)")

    groups = filters.ChatType.GROUPS
    # Lệnh chỉ có hiệu lực lần gửi đầu (UpdateType.MESSAGE) và chỉ trong chat riêng; gửi trong nhóm thì
    # không khớp handler nào, bot im lặng
    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    # Nhóm -1 chạy trước các handler bên dưới cho cùng cập nhật, không chặn chúng
    app.add_handler(TypeHandler(Update, remember_chat), group=-1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("p", cmd_p, filters=private))
    app.add_handler(CommandHandler("g", cmd_g, filters=private))
    app.add_handler(CommandHandler("e", cmd_e, filters=private))
    app.add_handler(CallbackQueryHandler(on_edit_button, pattern=r"^edit:"))
    app.add_handler(CallbackQueryHandler(on_pick_chat, pattern=r"^pick:"))
    # Tin thường của chủ bot trong chat riêng với bot: câu hỏi của chủ đề đang mở
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_private_message))
    app.add_handler(MessageHandler(groups & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_group_message))

    # APScheduler mặc định bỏ qua job nếu chạy trễ quá 1 giây (misfire_grace_time=1). Job đăng ký ở
    # đây trước khi bot kết nối xong Telegram nên luôn trễ vài giây; None = trễ bao lâu vẫn chạy.
    always_run = {"misfire_grace_time": None}
    hh, mm = map(int, config.daily_summary_time.split(":"))
    app.job_queue.run_daily(finalize_past_days, time(hh, mm, tzinfo=config.timezone), job_kwargs=always_run)
    # Khi khởi động: bù tin, chốt bù các ngày còn dở (ví dụ bot bị tắt lúc 00:01) và xử lý tin hôm nay
    app.job_queue.run_once(on_startup, STARTUP_DELAY_SECONDS, job_kwargs=always_run)
    return app


def setup_console_logging() -> None:
    # Console Windows mặc định không in được tiếng Việt (cp1252)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    setup_console_logging()
    args = parse_args(argv)
    if args.check:
        from .checks import check_llm

        load_env_file()
        try:
            asyncio.run(check_llm(
                *llm_settings(args.provider, args.model, args.base_url), report=lambda line: print(line, flush=True)
            ))
        except Exception as e:
            sys.exit(f"Lỗi: {type(e).__name__}: {e}")
        return

    config = load_config(args.provider, args.model, args.base_url)
    log.info("LLM: %s, model: %s", config.llm_provider, config.llm_model or "(mặc định)")
    app = build_application(config)
    log.info("Bot đang chạy")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

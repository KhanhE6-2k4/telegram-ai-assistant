"""Điểm khởi chạy bot: python -m bot.main [--provider TÊN] [--model MODEL] [--base-url URL]"""
import argparse
import asyncio
import logging
import re
import secrets
import sys
from collections import defaultdict
from datetime import datetime, time, timezone

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters,
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
        # Mỗi nhóm một khoá: không xử lý song song, không ghi sheet chồng lên nhau
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # (chat_id, day) đang được xử lý (kể cả lúc chờ thử lại), để không khởi động thêm lượt trùng
        self.busy: set[tuple[int, str]] = set()
        # Tab của mỗi nhóm đặt theo tên nhóm (lấy từ tin nhắn gần nhất), định danh bằng chat id
        self.sheets = SheetClient.open(
            config.google_credentials_file, config.spreadsheet_id, title_of=self.storage.chat_title
        )
        # Đề xuất sửa task đang chờ xác nhận: id ngẫu nhiên -> {chat_id, user_id, stt, changes, current}
        self.pending_edits: dict[str, dict] = {}
        # /ask gửi trống (hoặc chỉ tag @bot): bot hỏi lại "Bạn muốn hỏi gì?" và chờ người đó trả lời
        # (reply) tin hỏi lại. (chat_id, id tin hỏi lại) -> user id của người hỏi
        self.pending_asks: dict[tuple[int, int], int] = {}
        self.qa = QA(self.storage, self.llm, self.sheets)
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
                            "Xử lý chat %s ngày %s thất bại (lần %d/%d)", chat_id, day, attempt, attempts
                        )
                        # Có thể tab đã bị xoá / đổi trên sheet: lần thử sau tra lại tab từ đầu
                        self.sheets.forget(chat_id)
                if attempt < attempts:
                    # Chờ ngoài khoá để các thao tác khác (bấm Xác nhận...) không bị kẹt
                    await asyncio.sleep(self.config.compact_retry_delay_seconds * 2 ** (attempt - 1))
            log.error("Bỏ qua chat %s ngày %s sau %d lần thử, sẽ xử lý lại ở lần chốt sau", chat_id, day, attempts)
            return False
        finally:
            self.busy.discard((chat_id, day))

    async def review(self, chat_id: int) -> None:
        """Rà soát sheet của nhóm (thiếu người thực hiện, deadline chưa chuẩn, dòng không phải task,
        task trùng, dòng thiếu STT). Lỗi thì ghi log; lần khởi động sau rà lại."""
        async with self.locks[chat_id]:
            try:
                sheet = await asyncio.to_thread(self.task_sheet, chat_id)
                await review_sheet(self.storage, sheet, self.llm, chat_id, self.today())
            except Exception:
                log.exception("Rà soát sheet của chat %s thất bại", chat_id)
                self.sheets.forget(chat_id)


def state_of(context: ContextTypes.DEFAULT_TYPE) -> BotState:
    return context.application.bot_data["state"]


GROUP_CHAT_TYPES = ("group", "supergroup")


async def remember_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lưu tên nhóm từ mọi cập nhật (tin thường, lệnh, tin tag bot, nút bấm, bot được thêm vào nhóm),
    để tab tạo lần đầu trên sheet đã mang tên nhóm, kể cả khi thứ đầu tiên chạm tới sheet là một lệnh."""
    chat = update.effective_chat
    if chat and chat.type in GROUP_CHAT_TYPES and chat.title:
        state_of(context).storage.upsert_chat(chat.id, chat.title)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    text = msg.text or msg.caption
    if not text:
        return
    st = state_of(context)
    # Trả lời lời hỏi lại của /ask trống: đây là câu hỏi cho bot, không lưu vào hội thoại
    asker = take_pending_ask(st, update)
    if asker:
        if sent_while_offline(msg.date, st.started_at):
            context.application.create_task(msg.reply_text(OFFLINE_REPLY))
        else:
            context.application.create_task(answer_question(update, context, text))
        return
    # Tin tag @bot là câu hỏi / yêu cầu gửi cho bot, xử lý như /ask, không phải hội thoại của nhóm:
    # không lưu (để bước ghi task không làm theo một yêu cầu sửa chưa được xác nhận). Tin tag được sửa
    # lại thì bỏ qua, như lệnh được sửa lại.
    if mentions_bot(msg, context.bot):
        if update.edited_message:
            return
        if sent_while_offline(msg.date, st.started_at):
            context.application.create_task(msg.reply_text(OFFLINE_REPLY))
            return
        question = strip_mention(text, context.bot.username)
        if question:
            context.application.create_task(answer_question(update, context, question))
        else:
            context.application.create_task(prompt_question(update, context))
        return
    user = msg.from_user
    sent_at = msg.date.astimezone(st.config.timezone)
    fields = dict(
        chat_id=chat.id,
        message_id=msg.message_id,
        user_id=user.id if user else None,
        user_name=(user.full_name if user else None),
        text=text,
        reply_to=msg.reply_to_message.message_id if msg.reply_to_message else None,
        sent_at=sent_at,
    )

    if update.edited_message:
        st.storage.edit_message(**fields)
    else:
        st.storage.add_message(**fields)

    day = sent_at.date().isoformat()
    # Ngày cũ (tin sửa lại) để lần chốt 00:01 xử lý; chỉ nén giữa ngày cho hôm nay
    if (
        day == st.today()
        and (chat.id, day) not in st.busy
        and pending_chars(st.storage, chat.id, day) > st.config.compact_threshold_chars
    ):
        context.application.create_task(st.compact(chat.id, day, finalize=False))


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


ASK_PROMPT = (
    "Bạn muốn hỏi gì? Nhập câu hỏi hoặc yêu cầu vào ô trả lời rồi gửi.\n"
    "Ví dụ: task báo cáo Q3 hạn khi nào? / dời hạn task báo cáo Q3 sang 10/11"
)
ASK_PLACEHOLDER = "Nhập câu hỏi cho bot..."  # chữ mờ trong ô nhập (tối đa 64 ký tự)
MAX_PENDING_ASKS = 500


def take_pending_ask(st: "BotState", update: Update) -> int | None:
    """Tin này có phải là trả lời (reply) lời hỏi lại của /ask, từ đúng người đã gõ /ask không?
    Nếu đúng: bỏ khỏi danh sách chờ và trả về user id, ngược lại trả về None."""
    msg = update.effective_message
    if update.edited_message or not msg.reply_to_message or not msg.from_user:
        return None
    key = (update.effective_chat.id, msg.reply_to_message.message_id)
    if st.pending_asks.get(key) != msg.from_user.id:
        return None
    return st.pending_asks.pop(key)


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
    """Phần sau tên lệnh ("/ask@bot  câu hỏi\\ndòng 2" -> "câu hỏi\\ndòng 2"), giữ nguyên xuống dòng."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask <câu hỏi>: hỏi về task / nội dung trao đổi, hoặc (người có quyền) nhờ sửa task."""
    st = state_of(context)
    msg = update.effective_message
    if sent_while_offline(msg.date, st.started_at):
        # Telegram giữ lại tin gửi lúc bot tắt và gửi bù khi bot chạy lại: không trả lời muộn
        await msg.reply_text(OFFLINE_REPLY)
        return
    question = command_argument(msg.text or "")
    if not question:
        # Bấm /ask từ menu gợi ý thì Telegram gửi ngay lệnh trống
        await prompt_question(update, context)
        return
    await answer_question(update, context, question)


async def prompt_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask trống hoặc chỉ tag @bot: hỏi lại và mở ô trả lời cho đúng người đó (selective), câu họ trả
    lời sẽ được coi là câu hỏi."""
    st = state_of(context)
    msg = update.effective_message
    prompt = await msg.reply_text(
        ASK_PROMPT, reply_markup=ForceReply(selective=True, input_field_placeholder=ASK_PLACEHOLDER)
    )
    if len(st.pending_asks) >= MAX_PENDING_ASKS:
        st.pending_asks.pop(next(iter(st.pending_asks)))  # bỏ lời hỏi lại cũ nhất
    st.pending_asks[(update.effective_chat.id, prompt.message_id)] = msg.from_user.id


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
    await msg.reply_text(reply or "Mình chưa có câu trả lời.")

    for p in proposals:
        # Mã ngẫu nhiên: nút của lần chạy trước không thể trùng với đề xuất mới
        edit_id = secrets.token_hex(8)
        st.pending_edits[edit_id] = {"chat_id": chat.id, "user_id": msg.from_user.id, **p}
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


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/task [tên người]: liệt kê mọi task của nhóm, đọc thẳng từ sheet (không gọi LLM).
    Bỏ qua dòng không phải task (Người thực hiện = "Tất cả") và dòng trùng."""
    st = state_of(context)
    msg = update.effective_message
    if sent_while_offline(msg.date, st.started_at):
        await msg.reply_text(OFFLINE_COMMAND_REPLY)
        return
    chat_id = update.effective_chat.id
    who = " ".join(context.args).strip()
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
        lines += ["", "Còn tin nhắn hôm nay chưa xử lý, task trong đó chưa có ở đây. Gõ /tomtat để cập nhật."]
    for chunk in split_message(lines):
        await msg.reply_text(chunk)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tomtat [YYYY-MM-DD]: xem tóm tắt và task của một ngày - được giao hoăc có cập nhật (mặc định hôm nay)."""
    st = state_of(context)
    # Lệnh gửi lúc bot tắt (Telegram gửi bù khi bot chạy lại): không thực hiện muộn
    if sent_while_offline(update.effective_message.date, st.started_at):
        await update.effective_message.reply_text(OFFLINE_COMMAND_REPLY)
        return
    chat_id = update.effective_chat.id
    day = context.args[0] if context.args else st.today()

    # Với hôm nay, xử lý hết các tin nhắn mới (kể cả tin vừa gửi) để task lên sheet và tóm tắt đầy đủ
    # ngay. Không chờ thử lại để người gõ lệnh không phải đợi; lỗi thì lần chốt sau xử lý.
    if day == st.today():
        await st.compact(chat_id, day, finalize=False, retry=False, keep_recent=False)
    # s = st.storage.get_summary(chat_id, day)
    try:
        tasks = [
            t for t in (await asyncio.to_thread(st.task_sheet, chat_id)).rows()
            if day in (t["created_day"], t["updated_day"])
        ]
    except Exception:
        log.exception("Đọc sheet thất bại")
        tasks = []
    # if not s.summary and not tasks:
    #     await update.effective_message.reply_text(f"Chưa có tóm tắt cho ngày {day}.")
    #     return

    # lines = [f"Tóm tắt ngày {day}:", s.summary or "(trống)", "", "Task giao hoặc cập nhật trong ngày:"]
    lines = ["Task giao hoặc cập nhật trong ngày:"]
    lines += [format_task(t) for t in tasks]
    if not tasks:
        lines.append("(không có)")
    for chunk in split_message(lines):
        await update.effective_message.reply_text(chunk)


async def finalize_past_days(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chốt mọi ngày trước hôm nay chưa được chốt: ghi task lên sheet + tóm tắt.

    Chạy lúc DAILY_SUMMARY_TIME (mặc định 00:01) và một lần khi khởi động. Ngày hôm qua luôn được
    chốt, kể cả khi trong ngày đã có /tomtat hay đã nén vì vượt ngưỡng; ngày có tin bị sửa sau khi
    chốt cũng được mở lại và chốt lại ở đây.
    """
    st = state_of(context)
    for chat_id, day in st.storage.unfinalized_days_before(st.today()):
        if (chat_id, day) not in st.busy:
            await st.compact(chat_id, day, finalize=True)


# Chờ Telegram gửi bù các tin nhắn lúc bot tắt rồi mới xử lý, để không bỏ sót chúng
STARTUP_DELAY_SECONDS = 10


async def on_startup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chạy một lần sau khi khởi động:
    1. Chốt bù các ngày trước hôm nay còn dở.
    2. Xử lý hết tin nhắn chưa xử lý của hôm nay (như /tomtat, nhưng không chốt ngày): task từ các
       tin gửi trước hoặc trong lúc bot tắt lên sheet ngay, không phải chờ /tomtat hay 00:01.
    3. Rà soát sheet của từng nhóm (xem bot/review.py). Sheet không đổi từ lần rà trước thì bỏ qua.
    """
    await finalize_past_days(context)
    st = state_of(context)
    today = st.today()
    for chat_id in st.storage.chats_with_messages_on(today):
        if (chat_id, today) not in st.busy and pending_chars(st.storage, chat_id, today) > 0:
            await st.compact(chat_id, today, finalize=False, keep_recent=False)
    for chat_id in st.storage.chats_with_tasks():
        await st.review(chat_id)


BOT_COMMANDS = [
    ("ask", "Hỏi về task / nội dung trao đổi, hoặc nhờ sửa task. /ask <câu hỏi> hoặc tag @bot <câu hỏi>"),
    ("task","Xem mọi task của nhóm. /task <tên> để lọc theo người thực hiện"),
    ("tomtat", "Cập nhật tin nhắn mới lên sheet, xem task của ngày. /tomtat YYYY-MM-DD"),
]


async def set_bot_commands(app: Application) -> None:
    """Đăng ký menu lệnh hiện khi gõ "/" trong Telegram."""
    try:
        await app.bot.set_my_commands([BotCommand(name, text) for name, text in BOT_COMMANDS])
    except Exception:
        log.warning("Không đăng ký được menu lệnh", exc_info=True)


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
    """Tạo Application đầy đủ handler, BotState và lịch chạy (chưa kết nối Telegram).

    Dùng chung cho bản dòng lệnh (run_polling) và app desktop (bot/runner.py). Mở Google Sheet nên
    cần mạng. post_init (đăng ký menu lệnh) chỉ được run_polling gọi; runner tự gọi set_bot_commands.
    """
    app = Application.builder().token(config.telegram_token).post_init(set_bot_commands).build()
    app.bot_data["state"] = BotState(config)

    groups = filters.ChatType.GROUPS
    # Nhóm -1 chạy trước các handler bên dưới cho cùng cập nhật, không chặn chúng
    app.add_handler(TypeHandler(Update, remember_chat), group=-1)
    app.add_handler(CommandHandler("tomtat", cmd_summary, filters=groups & filters.UpdateType.MESSAGE))
    app.add_handler(CommandHandler("task", cmd_tasks, filters=groups & filters.UpdateType.MESSAGE))
    # Lệnh chỉ có hiệu lực lần gửi đầu (UpdateType.MESSAGE): sửa lại tin lệnh thì bot không chạy lại
    app.add_handler(CommandHandler("ask", cmd_ask, filters=groups & filters.UpdateType.MESSAGE))
    app.add_handler(CallbackQueryHandler(on_edit_button, pattern=r"^edit:"))
    app.add_handler(MessageHandler(groups & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_group_message))

    # APScheduler mặc định bỏ qua job nếu chạy trễ quá 1 giây (misfire_grace_time=1). Job đăng ký ở
    # đây trước khi bot kết nối xong Telegram nên luôn trễ vài giây; None = trễ bao lâu vẫn chạy.
    always_run = {"misfire_grace_time": None}
    hh, mm = map(int, config.daily_summary_time.split(":"))
    app.job_queue.run_daily(finalize_past_days, time(hh, mm, tzinfo=config.timezone), job_kwargs=always_run)
    # Khi khởi động: chốt bù các ngày còn dở (ví dụ bot bị tắt lúc 00:01) và xử lý tin hôm nay
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

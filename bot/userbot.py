"""Userbot: tài khoản Telegram của chủ bot (Telethon), nguồn tin nhắn duy nhất của bot.

Bot API chỉ thấy tin của nhóm có bot, tính từ lúc bot vào, và không đọc được chat riêng hay lịch sử
cũ. Userbot đăng nhập bằng tài khoản của chủ bot nên đọc được mọi chat chủ bot tham gia. Nó chạy chung
tiến trình và vòng lặp asyncio với bot:
- theo dõi liên tục tin mới / tin sửa của các chat trong `tracked`, chuyển cho `on_message`;
- `fetch_since` lấy những tin chưa có trong DB để đồng bộ tin nhắn (lúc khởi động, khi mở chủ đề /p /g,
  khi bắt đầu theo dõi một chat);
- `find_users` / `find_groups` / `recent_users` / `recent_groups` tìm chat cho lệnh /p,
  /g. Mỗi chat là bộ (chat_id, tên, username | None).

Bảo mật: session string cho toàn quyền vào tài khoản. Không bao giờ ghi nó ra log.
Telethon được import lười (trong các hàm), như SDK của các provider LLM, để test không cần nó.
"""
import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Telegram yêu cầu chờ (FloodWait) lâu hơn mức này thì bỏ qua chat đó, để lần đồng bộ tin nhắn sau
MAX_FLOOD_WAIT_SECONDS = 300
# Số chat tối đa trả về khi tìm / liệt kê (bằng số nút chọn tối đa của bot)
MAX_CHOICES = 10

# (chat_id, tên hiển thị, username hoặc None)
ChatInfo = tuple[int, str, str | None]


def is_conversation(text: str | None, sender_id: int | None, bot_id: int | None, bot_username: str | None) -> bool:
    """Tin có phải hội thoại cần lưu không. Không lưu: tin trống, tin do chính bot gửi, lệnh
    (bắt đầu bằng "/", kể cả lệnh cho bot khác) và tin tag @bot (là yêu cầu gửi bot, không phải hội thoại)."""
    if not text or not text.strip():
        return False
    if bot_id is not None and sender_id == bot_id:
        return False
    if text.lstrip().startswith("/"):
        return False
    if bot_username and re.search(rf"@{re.escape(bot_username)}\b", text, flags=re.IGNORECASE):
        return False
    return True


def message_fields(msg, chat_id: int, sender_name: str | None, tz: ZoneInfo) -> dict:
    """Tin Telethon -> các trường của Storage.add_message (giống tin bot nhận qua Bot API)."""
    return dict(
        chat_id=chat_id,
        message_id=msg.id,
        user_id=msg.sender_id,
        user_name=sender_name,
        text=msg.message or "",
        reply_to=getattr(msg, "reply_to_msg_id", None),
        sent_at=msg.date.astimezone(tz),
    )


class Userbot:
    """`on_message(fields, edited)` được gọi (trong vòng lặp asyncio) cho mỗi tin hội thoại mới / bị sửa
    của chat đang theo dõi."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str,
        timezone: ZoneInfo,
        on_message: Callable[[dict, bool], None] = lambda fields, edited: None,
        on_chat: Callable[[int, str, str | None], None] = lambda chat_id, title, username: None,
        label_of: Callable[[int], str] = str,
    ):
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self.timezone = timezone
        self.on_message = on_message
        # Gọi khi đọc được thông tin chat lúc đồng bộ tin nhắn (cập nhật tên / username trong DB)
        self.on_chat = on_chat
        # Nhãn chat cho log (tên + username + chat id), xem Storage.chat_label
        self.label_of = label_of
        self.client = None
        self.me = None
        self.started_at: datetime | None = None
        self.tracked: set[int] = set()
        self.bot_id: int | None = None
        self.bot_username: str | None = None
        self._dialogs_loaded = False
        self._warned: set[int] = set()

    @property
    def owner_id(self) -> int | None:
        return self.me.id if self.me else None

    @property
    def owner_name(self) -> str:
        if not self.me:
            return ""
        from telethon import utils

        return utils.get_display_name(self.me)

    def set_bot(self, bot_id: int, bot_username: str | None) -> None:
        self.bot_id, self.bot_username = bot_id, bot_username

    async def start(self) -> None:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        self.client = TelegramClient(StringSession(self._session), self._api_id, self._api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError("Session userbot chưa đăng nhập hoặc đã hết hạn: chạy python -m bot.userbot_login")
        self.me = await self.client.get_me()
        self.started_at = datetime.now(self.timezone)
        self.client.add_event_handler(self._on_new, events.NewMessage())
        self.client.add_event_handler(self._on_edited, events.MessageEdited())
        log.info("Userbot đã đăng nhập (user id %s)", self.me.id)

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    # ---- chuyển đổi tin ----

    async def _fields(self, msg, chat_id: int) -> dict | None:
        """Trường của tin hội thoại, hoặc None nếu không phải hội thoại (xem is_conversation)."""
        # Tin dịch vụ (MessageService: ai vào nhóm, đổi tên...) không có thuộc tính message
        if not is_conversation(getattr(msg, "message", None), msg.sender_id, self.bot_id, self.bot_username):
            return None
        from telethon import utils

        sender = await msg.get_sender()
        name = utils.get_display_name(sender) if sender else None
        return message_fields(msg, chat_id, name or None, self.timezone)

    async def _on_new(self, event) -> None:
        await self._on_event(event, False)

    async def _on_edited(self, event) -> None:
        await self._on_event(event, True)

    async def _on_event(self, event, edited: bool) -> None:
        from telethon import utils

        try:
            chat_id = utils.get_peer_id(event.message.peer_id)
            if chat_id not in self.tracked:
                return
            fields = await self._fields(event.message, chat_id)
            if fields:
                self.on_message(fields, edited)
        except Exception:
            log.exception("Userbot xử lý tin thất bại")

    # ---- tra cứu ----

    async def _load_dialogs(self) -> None:
        """StringSession không lưu danh bạ thực thể: tải danh sách chat một lần để tra được theo id."""
        if not self._dialogs_loaded:
            await self.client.get_dialogs()
            self._dialogs_loaded = True

    async def _entity(self, ref):
        try:
            return await self.client.get_entity(ref)
        except ValueError:
            if self._dialogs_loaded or not isinstance(ref, int):
                raise
            await self._load_dialogs()
            return await self.client.get_entity(ref)

    async def fetch_since(self, chat_id: int, since: datetime) -> list[dict] | None:
        """Tin hội thoại của chat từ `since` (tính cả tin gửi đúng lúc đó) tới nay, cũ trước mới sau.
        None nếu chủ bot không vào được chat này (không phải thành viên...)."""
        from telethon import errors

        try:
            entity = await self._entity(chat_id)
        except (ValueError, errors.RPCError) as e:
            if chat_id not in self._warned:
                self._warned.add(chat_id)
                log.warning(
                    "Userbot không đọc được chat %s (chủ bot có trong chat không?): %s", self.label_of(chat_id), e
                )
            return None
        _, title, username = chat_info(entity)
        self.on_chat(chat_id, title, username)
        out: list[dict] = []
        last_id = 0
        while True:
            try:
                # offset_date không tính tin gửi đúng thời điểm đó: lùi 1 giây, tin trùng bị UNIQUE chặn
                kwargs = {"min_id": last_id} if last_id else {"offset_date": since - timedelta(seconds=1)}
                async for msg in self.client.iter_messages(entity, reverse=True, **kwargs):
                    last_id = msg.id
                    fields = await self._fields(msg, chat_id)
                    if fields:
                        out.append(fields)
                return out
            except errors.FloodWaitError as e:
                if e.seconds > MAX_FLOOD_WAIT_SECONDS:
                    log.warning(
                        "Telegram yêu cầu chờ %s giây khi lấy tin nhắn của chat %s: bỏ qua, để lần đồng bộ sau",
                        e.seconds, self.label_of(chat_id),
                    )
                    return out
                log.info(
                    "Thời gian - Telegram yêu cầu chờ %s giây khi lấy tin nhắn của chat %s: đang chờ",
                    e.seconds, self.label_of(chat_id),
                )
                await asyncio.sleep(e.seconds)

    async def _direct(self, ref: str):
        """Thực thể tra thẳng theo @username, id hoặc số điện thoại; None nếu không thấy."""
        from telethon import errors

        key: str | int = int(ref) if re.fullmatch(r"-?\d+", ref) else ref
        try:
            return await self._entity(key)
        except (ValueError, TypeError, errors.RPCError):
            return None

    async def _dialogs(self, want: Callable[[object], bool], name: str | None = None) -> list[ChatInfo]:
        """Các chat (gần đây trước) thoả `want`; `name` có thì chỉ lấy chat có tên chứa nó (không phân biệt
        hoa thường), tên khớp đúng hoàn toàn đứng trước."""
        wanted = name.casefold() if name else None
        found: list[ChatInfo] = []
        async for dialog in self.client.iter_dialogs():
            if not want(dialog):
                continue
            info = chat_info(dialog.entity, dialog.id)
            if wanted is None or wanted in info[1].casefold():
                found.append(info)
            if wanted is None and len(found) >= MAX_CHOICES:
                break
        self._dialogs_loaded = True
        if wanted is not None:
            found.sort(key=lambda c: c[1].casefold() != wanted)  # sort ổn định: giữ thứ tự gần đây
        return found

    def _is_person(self, dialog) -> bool:
        """Chat riêng với người thật: không phải bot, không phải "Saved Messages" của chính chủ bot."""
        entity = dialog.entity
        return dialog.is_user and not getattr(entity, "bot", False) and not getattr(entity, "is_self", False)

    async def find_users(self, ref: str) -> list[ChatInfo]:
        """Người có chat riêng với chủ bot: @username, user id, số điện thoại (+84...) tra thẳng; còn lại
        tìm theo tên hiển thị (chứa `ref`, không phân biệt hoa thường) trong các chat riêng."""
        from telethon import types

        ref = ref.strip()
        if ref.startswith("@") or re.fullmatch(r"\+?\d+", ref):
            entity = await self._direct(ref)
            if isinstance(entity, types.User) and not entity.bot:
                return [chat_info(entity)]
            return []
        return await self._dialogs(self._is_person, ref)

    async def find_groups(self, ref: str) -> list[ChatInfo]:
        """@username -> nhóm đó; ngược lại các nhóm có tên chứa `ref` (không phân biệt hoa thường)."""
        from telethon import types

        ref = ref.strip()
        if ref.startswith("@"):
            entity = await self._direct(ref)
            if isinstance(entity, types.Chat) or (isinstance(entity, types.Channel) and entity.megagroup):
                return [chat_info(entity)]
            return []
        return await self._dialogs(lambda d: d.is_group, ref)

    async def recent_users(self) -> list[ChatInfo]:
        """Các chat riêng gần đây nhất (tối đa MAX_CHOICES)."""
        return await self._dialogs(self._is_person)

    async def recent_groups(self) -> list[ChatInfo]:
        """Các nhóm gần đây nhất (tối đa MAX_CHOICES)."""
        return await self._dialogs(lambda d: d.is_group)


def chat_info(entity, chat_id: int | None = None) -> ChatInfo:
    """Thực thể Telethon (người / nhóm) -> (chat_id dạng Bot API, tên hiển thị, username | None)."""
    from telethon import utils

    if chat_id is None:
        chat_id = utils.get_peer_id(entity)
    return chat_id, utils.get_display_name(entity) or str(chat_id), getattr(entity, "username", None) or None

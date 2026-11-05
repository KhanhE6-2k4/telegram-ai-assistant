"""Đăng nhập tài khoản Telegram của chủ bot (userbot) và kiểm tra session, dùng cho app desktop.

Không phụ thuộc Toga. Telethon được import lười (như SDK của các provider LLM), để test dùng client giả.
- `check_session`: session đã lưu còn đăng nhập không.
- `LoginFlow`: đăng nhập từng bước (số điện thoại → mã OTP → mật khẩu 2 bước nếu có) với một client sống
  qua các bước, chạy trong thread + asyncio loop riêng: giao diện chờ từng bước bằng `await flow.call(...)`
  mà không bị treo, và client không dính vào loop của giao diện.

Bảo mật: session string cho toàn quyền vào tài khoản. Không bao giờ ghi nó ra log.
"""
import asyncio
import threading
from collections.abc import Callable


def telethon_client(api_id: int, api_hash: str, session: str = ""):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(session), api_id, api_hash)


def account_label(me) -> str:
    """Tài khoản cho người dùng / log: "Tên (user id N)". Không chứa gì bí mật."""
    name = " ".join(p for p in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if p)
    if not name and getattr(me, "username", None):
        name = f"@{me.username}"
    return f"{name or 'Tài khoản'} (user id {me.id})"


async def check_session(
    api_id: int, api_hash: str, session: str, client_factory: Callable = telethon_client
) -> str | None:
    """Nhãn tài khoản nếu session còn đăng nhập; None nếu chưa đăng nhập / đã hết hạn (bị chấm dứt).
    Lỗi mạng hay lỗi khác thì ném ra."""
    if not session:
        return None
    client = client_factory(api_id, api_hash, session)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return None
        return account_label(await client.get_me())
    finally:
        await client.disconnect()


class LoginFlow:
    """Đăng nhập từng bước. Các bước là coroutine chạy trong loop riêng; từ loop giao diện gọi
    `await flow.call(flow.send_code, phone)`. Xong thì `close()` (kể cả khi huỷ)."""

    def __init__(self, api_id: int, api_hash: str, client_factory: Callable = telethon_client):
        self.api_id, self.api_hash = api_id, api_hash
        self._factory = client_factory
        self.client = None
        self.phone: str | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="telegram-login", daemon=True)
        self._thread.start()

    async def call(self, step: Callable, *args):
        """Chạy một bước trong loop riêng, chờ kết quả mà không chặn loop đang gọi."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(step(*args), self._loop))

    async def send_code(self, phone: str) -> None:
        """Telegram gửi mã OTP về app Telegram (hoặc SMS) của số điện thoại này."""
        if self.client is None:
            self.client = self._factory(self.api_id, self.api_hash)
            await self.client.connect()
        await self.client.send_code_request(phone)
        self.phone = phone

    async def sign_in(self, code: str) -> str:
        """"done" nếu đăng nhập xong, "password" nếu tài khoản bật xác minh 2 bước."""
        try:
            await self.client.sign_in(self.phone, code)
        except Exception as e:
            if type(e).__name__ == "SessionPasswordNeededError":
                return "password"
            raise
        return "done"

    async def sign_in_password(self, password: str) -> str:
        await self.client.sign_in(password=password)
        return "done"

    async def result(self) -> tuple[str, str]:
        """(session string, nhãn tài khoản) sau khi đăng nhập xong."""
        me = await self.client.get_me()
        return self.client.session.save(), account_label(me)

    def close(self) -> None:
        """Ngắt kết nối và dừng loop riêng. Gọi được nhiều lần."""
        if self._loop.is_closed():
            return
        client, self.client = self.client, None
        if client is not None:
            try:
                asyncio.run_coroutine_threadsafe(client.disconnect(), self._loop).result(timeout=10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        if not self._thread.is_alive():
            self._loop.close()


LOGIN_ERRORS = {
    "PhoneNumberInvalidError": "Số điện thoại không hợp lệ. Nhập kèm mã quốc gia, ví dụ +84912345678.",
    "PhoneNumberBannedError": "Số điện thoại này đã bị Telegram khoá.",
    "PhoneNumberUnoccupiedError": "Số điện thoại này chưa có tài khoản Telegram.",
    "PhoneCodeInvalidError": "Mã xác nhận không đúng. Kiểm tra lại mã Telegram vừa gửi.",
    "PhoneCodeEmptyError": "Chưa nhập mã xác nhận.",
    "PhoneCodeExpiredError": "Mã xác nhận đã hết hạn. Hãy gửi lại mã.",
    "PasswordHashInvalidError": "Mật khẩu xác minh 2 bước không đúng.",
    "ApiIdInvalidError": "Telegram API ID / API Hash không đúng. Kiểm tra lại ở my.telegram.org.",
    "ApiIdPublishedFloodError": "API ID này bị Telegram hạn chế. Hãy tạo app khác ở my.telegram.org.",
}


def describe_login_error(e: BaseException) -> str:
    """Thông báo lỗi đăng nhập dễ hiểu (tiếng Việt), hiện trên giao diện. Không kèm tên lớp lỗi trong code."""
    name = type(e).__name__
    if name in LOGIN_ERRORS:
        return LOGIN_ERRORS[name]
    if name == "FloodWaitError":
        return f"Telegram yêu cầu chờ {getattr(e, 'seconds', '?')} giây rồi mới thử lại"
    if isinstance(e, (ConnectionError, TimeoutError, OSError)):
        return "Không kết nối được Telegram, kiểm tra mạng rồi thử lại"
    from .checks import describe_error

    return describe_error(e)

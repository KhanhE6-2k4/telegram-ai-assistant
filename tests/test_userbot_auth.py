"""Đăng nhập Telegram cho userbot (bot/userbot_auth.py): client giả thay Telethon, không dùng mạng."""
import asyncio
from types import SimpleNamespace

import pytest

from bot.userbot_auth import LoginFlow, account_label, check_session, describe_login_error


class FakeClient:
    """Client giả: ghi lại các lời gọi; `password` có giá trị thì sign_in bằng mã đòi mật khẩu 2 bước."""

    def __init__(self, authorized=True, password=None, code="12345"):
        self.authorized, self.password, self.code = authorized, password, code
        self.calls = []
        self.connected = False
        self.loop = None
        self.session = SimpleNamespace(save=lambda: "SESSION-STRING")

    async def connect(self):
        self.loop = asyncio.get_running_loop()
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(id=42, first_name="Lan", last_name="Nguyễn", username="lan")

    async def send_code_request(self, phone):
        self.calls.append(("send_code", phone))

    async def sign_in(self, phone=None, code=None, password=None):
        self.calls.append(("sign_in", phone, code, password))
        if password is not None:
            if password != self.password:
                raise type("PasswordHashInvalidError", (Exception,), {})()
            return
        if code != self.code:
            raise type("PhoneCodeInvalidError", (Exception,), {})()
        if self.password:
            raise type("SessionPasswordNeededError", (Exception,), {})()


def run(coro):
    return asyncio.run(coro)


def test_account_label():
    assert account_label(SimpleNamespace(id=1, first_name="Lan", last_name=None, username="x")) == "Lan (user id 1)"
    assert account_label(SimpleNamespace(id=2, first_name=None, last_name=None, username="lan")) == "@lan (user id 2)"


def test_check_session_authorized_or_expired():
    client = FakeClient()
    label = run(check_session(1, "hash", "S", client_factory=lambda *a: client))
    assert label == "Lan Nguyễn (user id 42)" and not client.connected
    assert run(check_session(1, "hash", "S", client_factory=lambda *a: FakeClient(authorized=False))) is None
    assert run(check_session(1, "hash", "", client_factory=lambda *a: pytest.fail("không cần kết nối"))) is None


def login(client):
    created = []

    def factory(api_id, api_hash):
        created.append((api_id, api_hash))
        return client

    return LoginFlow(7, "hash", client_factory=factory), created


def test_login_without_password():
    client = FakeClient()
    flow, created = login(client)
    try:
        async def steps():
            await flow.call(flow.send_code, "+84912345678")
            assert await flow.call(flow.sign_in, "12345") == "done"
            return await flow.call(flow.result)

        assert run(steps()) == ("SESSION-STRING", "Lan Nguyễn (user id 42)")
        assert created == [(7, "hash")]
        assert client.calls == [("send_code", "+84912345678"), ("sign_in", "+84912345678", "12345", None)]
        # Client chạy trong loop riêng của flow, không phải loop của người gọi (giao diện)
        assert client.loop is flow._loop
    finally:
        flow.close()
    assert not client.connected and not flow._thread.is_alive()
    flow.close()  # gọi lại không lỗi


def test_login_with_two_step_password_and_wrong_inputs():
    client = FakeClient(password="bí mật ")
    flow, _ = login(client)
    try:
        async def steps():
            await flow.call(flow.send_code, "+84912345678")
            with pytest.raises(Exception) as wrong_code:
                await flow.call(flow.sign_in, "00000")
            assert type(wrong_code.value).__name__ == "PhoneCodeInvalidError"
            assert await flow.call(flow.sign_in, "12345") == "password"
            with pytest.raises(Exception) as wrong_password:
                await flow.call(flow.sign_in_password, "sai")
            assert type(wrong_password.value).__name__ == "PasswordHashInvalidError"
            assert await flow.call(flow.sign_in_password, "bí mật ") == "done"
            return await flow.call(flow.result)

        assert run(steps())[0] == "SESSION-STRING"
    finally:
        flow.close()


def test_close_without_any_step():
    flow = LoginFlow(1, "h", client_factory=lambda *a: pytest.fail("chưa bước nào thì không tạo client"))
    flow.close()
    assert not flow._thread.is_alive()


def test_describe_login_error():
    def err(name, **attrs):
        e = type(name, (Exception,), {})()
        for k, v in attrs.items():
            setattr(e, k, v)
        return e

    assert describe_login_error(err("PhoneCodeInvalidError")).startswith("Mã xác nhận không đúng")
    assert describe_login_error(err("PasswordHashInvalidError")).startswith("Mật khẩu xác minh 2 bước không đúng")
    assert describe_login_error(err("ApiIdInvalidError")).startswith("Telegram API ID / API Hash không đúng")
    assert describe_login_error(err("PhoneNumberInvalidError")).startswith("Số điện thoại không hợp lệ")
    assert "chờ 30 giây" in describe_login_error(err("FloodWaitError", seconds=30))
    assert describe_login_error(ConnectionError("x")).startswith("Không kết nối được Telegram")
    assert describe_login_error(ValueError("lạ")) == "lạ"
    # Giao diện không hiện tên lớp lỗi trong code
    for name in ("PhoneNumberInvalidError", "PhoneCodeInvalidError", "FloodWaitError"):
        assert name not in describe_login_error(err(name, seconds=5))
    assert "ConnectionError" not in describe_login_error(ConnectionError("x"))


def test_describe_login_error_with_real_telethon_errors():
    errors = pytest.importorskip("telethon.errors")
    assert describe_login_error(errors.PhoneCodeExpiredError(request=None)).startswith("Mã xác nhận đã hết hạn")
    assert "chờ 15 giây" in describe_login_error(errors.FloodWaitError(request=None, capture=15))

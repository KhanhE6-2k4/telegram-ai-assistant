"""Cửa sổ con "Đăng nhập Telegram" cho userbot: số điện thoại → mã OTP → mật khẩu 2 bước (nếu có).

Các bước được dựng sẵn khi chưa gắn vào cửa sổ; chuyển bước = một lần gán `window.content` (chỉ bố trí lại
một lần). Việc gọi Telegram chạy trong `LoginFlow` (thread + loop riêng, bot/userbot_auth.py), cửa sổ chỉ
chờ kết quả nên không bị treo. Không giữ session hay mật khẩu sau khi đóng.
"""
import asyncio
import logging

import toga
from toga.style import Pack
from toga.style.pack import BOLD, COLUMN

from bot.userbot_auth import LoginFlow, describe_login_error

from . import theme as t
from .layout import ButtonBar, FormRow, WrappedLabel, button, remeasure, set_input

log = logging.getLogger(__name__)

WINDOW_SIZE = (480, 360)
# Viền + thanh cuộn của cửa sổ Windows: phần nội dung hẹp hơn WINDOW_SIZE một chút
WINDOW_CHROME = 16


class _Step:
    """Một bước: tiêu đề (tuỳ chọn), hướng dẫn, một ô nhập, dòng báo lỗi, nút Tiếp tục / Huỷ."""

    def __init__(
        self, theme: t.Theme, title: str | None, hint: str, label: str, field: toga.Widget, on_submit, on_cancel,
        strip: bool = True,
    ):
        self.field = field
        self.strip = strip  # mật khẩu giữ nguyên dấu cách
        self.hint = WrappedLabel(hint, style=Pack(font_size=t.BODY, color=theme.text))
        self.error = WrappedLabel(style=Pack(font_size=t.BODY, color=theme.danger))
        row = FormRow(theme, label, field)
        self.submit = button(theme, "Tiếp tục", on_submit)
        self.cancel = button(theme, "Huỷ", on_cancel, kind="secondary")
        field.on_confirm = on_submit  # Enter = Tiếp tục
        width = WINDOW_SIZE[0] - WINDOW_CHROME - 2 * t.XL
        for label_ in (self.hint, self.error):
            label_.set_width(width)
        children = [self.hint.widget, row.box, self.error.widget, ButtonBar(self.submit, self.cancel).box]
        if title:
            heading = toga.Label(title, style=Pack(font_size=t.HEADING, font_weight=BOLD, color=theme.heading))
            children.insert(0, heading)
        # Hộp gốc của cửa sổ không áp margin: lề nằm ở hộp con bên trong (như layout.Card), hộp ngoài giữ nền
        inner = toga.Box(children=children, style=Pack(direction=COLUMN, gap=t.M, margin=t.XL))
        self.box = toga.Box(children=[inner], style=Pack(direction=COLUMN, background_color=theme.background))

    def value(self) -> str:
        value = self.field.value or ""
        return value.strip() if self.strip else value

    def set_busy(self, busy: bool) -> None:
        # enabled không làm bố trí lại cửa sổ
        self.submit.enabled = self.field.enabled = not busy


class LoginWindow:
    """`await LoginWindow(theme, flow).run()` -> (session, nhãn tài khoản), hoặc None nếu người dùng huỷ /
    đóng cửa sổ. Luôn đóng `flow` khi xong."""

    def __init__(self, theme: t.Theme, flow: LoginFlow):
        self.flow = flow
        self.result: asyncio.Future | None = None
        # Bước đầu không có tiêu đề riêng: thanh tiêu đề cửa sổ đã là "Đăng nhập Telegram"
        self.phone = _Step(
            theme, None,
            "Bot dùng tài khoản Telegram của bạn để đọc tin nhắn. Nhập số điện thoại của tài khoản, "
            "kèm mã quốc gia. Telegram sẽ gửi mã xác nhận về app Telegram của bạn.",
            "Số điện thoại", toga.TextInput(placeholder="+84912345678"), self._on_phone, self._on_cancel,
        )
        self.code = _Step(
            theme, "Nhập mã xác nhận", "", "Mã xác nhận",
            toga.TextInput(placeholder="Mã gồm 5 chữ số"), self._on_code, self._on_cancel,
        )
        self.password = _Step(
            theme, "Xác minh 2 bước",
            "Tài khoản này bật xác minh 2 bước. Nhập mật khẩu (cloud password) của tài khoản Telegram.",
            "Mật khẩu", toga.PasswordInput(), self._on_password, self._on_cancel, strip=False,
        )
        self.current = self.phone
        self.closed = False
        self.window = toga.Window(
            title="Đăng nhập Telegram", size=WINDOW_SIZE, resizable=False, on_close=self._on_close
        )
        self.window.content = self.phone.box

    async def run(self) -> tuple[str, str] | None:
        self.result = asyncio.get_running_loop().create_future()
        self.window.show()
        remeasure(self.phone.box)  # đo lại chữ theo DPI thật sau khi gắn vào cửa sổ
        self.phone.field.focus()
        try:
            return await self.result
        finally:
            # close() chờ thread của flow dừng (tối đa vài giây): không chạy trên thread giao diện
            await asyncio.get_running_loop().run_in_executor(None, self.flow.close)
            if not self.closed:
                self.closed = True
                self.window.close()

    # Chuyển bước

    def _show(self, step: _Step, error: str = "") -> None:
        step.error.text = error
        step.set_busy(False)
        if step is not self.current:
            self.current = step
            self.window.content = step.box
            remeasure(step.box)
        step.field.focus()

    def _finish(self, value) -> None:
        if self.result is not None and not self.result.done():
            self.result.set_result(value)

    async def _step(self, step: _Step, call, *args):
        """Chạy một bước của flow với giá trị đang nhập: (kết quả, None) hoặc (None, lỗi).
        Chưa nhập gì thì báo ngay ở bước đó và trả về None."""
        value = step.value()
        if not value.strip():
            step.error.text = "Chưa nhập."
            return None
        step.error.text = ""
        step.set_busy(True)
        try:
            return await self.flow.call(call, value, *args), None
        except Exception as e:
            # Giao diện chỉ hiện thông báo tiếng Việt; tên lỗi ghi vào log để tra cứu (không ghi giá trị đã nhập)
            log.warning("Đăng nhập Telegram, bước %s lỗi: %s", call.__name__, type(e).__name__)
            return None, e
        finally:
            step.set_busy(False)

    # Sự kiện

    async def _on_phone(self, widget, **kwargs) -> None:
        out = await self._step(self.phone, self.flow.send_code)
        if out is None:
            return
        _, error = out
        if error:
            self._show(self.phone, describe_login_error(error))
            return
        self.code.hint.text = (
            f"Telegram đã gửi mã xác nhận tới {self.flow.phone} (trong app Telegram, hoặc SMS). Nhập mã đó."
        )
        set_input(self.code.field, value="")
        self._show(self.code)

    async def _on_code(self, widget, **kwargs) -> None:
        out = await self._step(self.code, self.flow.sign_in)
        if out is None:
            return
        state, error = out
        if error:
            if type(error).__name__ == "PhoneCodeExpiredError":
                self._show(self.phone, describe_login_error(error))
            else:
                self._show(self.code, describe_login_error(error))
            return
        if state == "password":
            self._show(self.password)
            return
        await self._done(self.code)

    async def _on_password(self, widget, **kwargs) -> None:
        out = await self._step(self.password, self.flow.sign_in_password)
        if out is None:
            return
        _, error = out
        if error:
            self._show(self.password, describe_login_error(error))
            return
        await self._done(self.password)

    async def _done(self, step: _Step) -> None:
        step.set_busy(True)
        try:
            self._finish(await self.flow.call(self.flow.result))
        except Exception as e:
            self._show(step, describe_login_error(e))

    def _on_cancel(self, widget, **kwargs) -> None:
        self._finish(None)

    def _on_close(self, window, **kwargs) -> bool:
        self.closed = True
        self._finish(None)
        return True

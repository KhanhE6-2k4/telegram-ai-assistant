"""Tab Cấu hình: provider, model, API key, Telegram (bot token, API ID / Hash của userbot), Google Sheet,
tệp khoá, Kiểm tra kết nối.

Không có nút Lưu: bấm Kiểm tra kết nối, cả 3 mục đều ✓ thì app tự lưu và (khởi động lại) bot.
"""
import toga
from toga.style import Pack
from toga.style.pack import COLUMN

from bot.checks import CheckResult

from ..settings import PROVIDERS, Settings
from . import theme as t
from .layout import ButtonBar, Card, FormRow, Page, WrappedLabel, button, set_input, set_text

PICK_CREDENTIALS = "Chọn tệp khoá Google (credentials.json)…"
CHANGE_CREDENTIALS = "Đổi tệp khoá"
LABEL_TO_PROVIDER = {info.label: name for name, info in PROVIDERS.items()}


class ConfigView:
    def __init__(self, theme: t.Theme, on_pick_credentials, on_check):
        self.theme = theme
        self.page = Page(theme, "Cấu hình")
        self._provider = next(iter(PROVIDERS))
        self._keys: dict[str, str] = {}
        self._loading = False

        # LLM
        llm = Card(theme, "LLM")
        self.provider = toga.Selection(
            items=[info.label for info in PROVIDERS.values()], on_change=self._on_provider_change
        )
        self.model = toga.TextInput()
        self.api_key = toga.PasswordInput()
        self.provider_row = FormRow(theme, "Provider", self.provider)
        self.model_row = FormRow(theme, "Model", self.model)
        self.api_key_row = FormRow(theme, "API key", self.api_key)
        rows = [self.provider_row, self.model_row, self.api_key_row]
        llm.add(*(r.box for r in rows))

        # Telegram
        telegram = Card(theme, "Telegram")
        self.token = toga.PasswordInput(placeholder="Lấy từ @BotFather")
        token_row = FormRow(theme, "Bot token", self.token)
        # Userbot (tài khoản Telegram của chủ bot): tuỳ chọn. Session lấy qua cửa sổ đăng nhập khi kiểm tra
        # kết nối / bật bot, không nhập tay
        self.api_id = toga.TextInput(placeholder="Ví dụ 1234567")
        self.api_hash = toga.PasswordInput()
        api_id_row = FormRow(theme, "Telegram API ID", self.api_id)
        api_hash_row = FormRow(theme, "Telegram API Hash", self.api_hash)
        self.api_hint = WrappedLabel(
            "Tài khoản Telegram của bạn (userbot) dùng để đọc tin nhắn của nhóm và chat riêng. Lấy API ID / "
            "API Hash ở my.telegram.org → API development tools. Để trống nếu không dùng.",
            # Khi kiểm tra kết "
            # "nối hoặc bật bot, nếu chưa đăng nhập (hoặc phiên đã hết hạn) app sẽ mở cửa sổ đăng nhập Telegram.",
            style=Pack(font_size=t.SMALL, color=theme.muted),
        )
        telegram.add(token_row.box, api_id_row.box, api_hash_row.box, self.api_hint.widget)

        # Google Sheet
        sheet = Card(theme, "Google Sheet")
        self.spreadsheet = toga.TextInput(placeholder="Dán link Google Sheet hoặc Spreadsheet ID")
        sheet_row = FormRow(theme, "Spreadsheet", self.spreadsheet)
        self.pick = button(theme, PICK_CREDENTIALS, on_pick_credentials, kind="secondary")
        pick_bar = ButtonBar(self.pick)
        self.credentials = WrappedLabel(style=Pack(font_size=t.BODY, color=theme.text))
        self.share_hint = WrappedLabel(style=Pack(font_size=t.SMALL, color=theme.muted))
        sheet.add(sheet_row.box, pick_bar.box, self.credentials.widget, self.share_hint.widget)

        # Kiểm tra kết nối
        check = Card(theme, "Kiểm tra kết nối")
        self.check = button(theme, "Kiểm tra kết nối", on_check)
        check_bar = ButtonBar(self.check)
        self.check_hint = WrappedLabel(
            "Kiểm tra LLM, Telegram (bot, và tài khoản nếu đã nhập API ID / Hash) và Google Sheet. "
            "Cả 3 đều ✓ thì có nghĩa là đã cấu hình thành công.",
            style=Pack(font_size=t.SMALL, color=theme.muted),
        )
        self.results_box = toga.Box(style=Pack(direction=COLUMN, gap=t.XS))
        self.results: list[WrappedLabel] = []
        check.add(self.check_hint.widget, check_bar.box, self.results_box)

        self.page.add(llm, telegram, sheet, check)
        self.page.rows += [pick_bar, check_bar]
        self.page.labels += [self.api_hint, self.credentials, self.share_hint, self.check_hint]

    # Nạp / đọc giá trị

    def load(
        self,
        settings: Settings,
        token: str,
        keys: dict[str, str],
        credentials_email: str | None,
        telegram_api: tuple[str, str] = ("", ""),
    ) -> None:
        self._loading = True
        try:
            self._keys = dict(keys)
            self._provider = settings.provider
            self.provider.value = PROVIDERS[settings.provider].label
            self._fill_provider(settings.provider, settings.model)
            set_input(self.token, value=token)
            set_input(self.api_id, value=telegram_api[0])
            set_input(self.api_hash, value=telegram_api[1])
            set_input(self.spreadsheet, value=settings.spreadsheet_id)
        finally:
            self._loading = False
        self.set_credentials_email(credentials_email)

    def values(self) -> tuple[Settings, str, dict[str, str]]:
        """(Settings, Telegram token, API key theo từng provider) đang nhập trên form."""
        keys = dict(self._keys)
        keys[self._provider] = (self.api_key.value or "").strip()
        settings = Settings(
            provider=self._provider, model=(self.model.value or "").strip(), spreadsheet_id=(self.spreadsheet.value or "").strip()
        )
        return settings, (self.token.value or "").strip(), keys

    def telegram_api(self) -> tuple[str, str]:
        """(Telegram API ID, API Hash) của userbot đang nhập trên form."""
        return (self.api_id.value or "").strip(), (self.api_hash.value or "").strip()

    def set_credentials_email(self, email: str | None) -> None:
        if email:
            set_text(self.pick, CHANGE_CREDENTIALS)
            self.credentials.text = f"Service email: {email}"
            self.share_hint.text = "Hãy Share Google Sheet cho email này với quyền Editor."
        else:
            set_text(self.pick, PICK_CREDENTIALS)
            self.credentials.text = "Chưa có tệp khoá Google."
            self.share_hint.text = (
                "Tạo service account trên Google Cloud, tải khoá dạng JSON rồi chọn tệp đó ở đây."
            )

    # Kết quả kiểm tra

    def set_checking(self, checking: bool) -> None:
        self.check.enabled = not checking
        self.pick.enabled = not checking
        if checking:
            self.show_messages([("Đang kiểm tra…", self.theme.muted)])

    def show_results(self, results: list[CheckResult], note: str | None = None) -> None:
        lines = [
            (f"{'✓' if r.ok else '✗'} {r.name}: {r.detail}", self.theme.success if r.ok else self.theme.danger)
            for r in results
        ]
        if note:
            lines.append((note, self.theme.success))
        self.show_messages(lines)

    def show_messages(self, lines: list[tuple[str, str]]) -> None:
        # Mỗi lần add / remove là cả cửa sổ bố trí lại: gỡ hết một lần, gắn tất cả nhãn mới một lần
        self.results_box.clear()
        for label in self.results:
            self.page.labels.remove(label)
        self.results = [WrappedLabel(text, style=Pack(font_size=t.BODY, color=color)) for text, color in lines]
        for label in self.results:
            label.set_width(self.check_hint._width)
        self.results_box.add(*(label.widget for label in self.results))
        self.page.labels += self.results

    # Sự kiện

    def _on_provider_change(self, widget, **kwargs) -> None:
        if self._loading:
            return
        new = LABEL_TO_PROVIDER.get(widget.value, self._provider)
        if new == self._provider:
            return
        # Giữ key đang nhập của provider cũ: đổi qua lại không mất key
        self._keys[self._provider] = (self.api_key.value or "").strip()
        self._provider = new
        self._loading = True
        try:
            self._fill_provider(new, "")
        finally:
            self._loading = False

    def _fill_provider(self, provider: str, model: str) -> None:
        info = PROVIDERS[provider]
        self.api_key_row.set_label(info.key_label)
        set_input(self.api_key, value=self._keys.get(provider, ""))
        # Chưa có model (hoặc vừa đổi provider): điền sẵn model gợi ý đầu tiên, người dùng sửa được
        suggested = info.models[0] if info.models else ""
        set_input(
            self.model,
            value=model or suggested,
            placeholder=f"Tên model, ví dụ {suggested}" if suggested else "Tên model",
        )

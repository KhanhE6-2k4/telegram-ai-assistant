"""Cấu hình của app desktop (không phụ thuộc Toga, để test trực tiếp).

- Thiết lập thường (provider, model, spreadsheet_id): `<thư mục cấu hình>/settings.json`.
- Khoá bí mật (Telegram token, API key của từng provider): Windows Credential Manager qua `keyring`.
  Mỗi provider một key riêng, nên đổi qua lại provider không mất key.
- Tệp khoá Google được chép vào `<thư mục cấu hình>/credentials.json`.
- Các thiết lập khác (múi giờ, giờ chốt...) dùng giá trị mặc định như bản dòng lệnh.
"""
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bot.config import Config, config_from

# Tên dịch vụ trong Credential Manager
KEYRING_SERVICE = "Telegram Bot"
TOKEN_SECRET = "telegram_bot_token"


@dataclass(frozen=True)
class ProviderInfo:
    label: str
    key_label: str
    # Model gợi ý: model đầu tiên được điền sẵn vào ô Model khi chưa có / khi đổi provider
    models: list[str] = field(default_factory=list)


# Provider có trong app. Không có openai-chat: app không có ô Base URL.
PROVIDERS = {
    "anthropic": ProviderInfo(
        "Anthropic (Claude)", "Anthropic API key", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
    ),
    "openai": ProviderInfo("OpenAI", "OpenAI API key", ["gpt-6-luna"]),
}


def api_key_secret(provider: str) -> str:
    return f"{provider}_api_key"


def parse_spreadsheet_id(text: str) -> str:
    """Nhận ID hoặc nguyên link sheet (https://docs.google.com/spreadsheets/d/<ID>/edit...)."""
    text = text.strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    return m.group(1) if m else text


def read_service_account(path: Path) -> str:
    """Kiểm tra tệp là khoá service account của Google; trả về email của service account."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Không đọc được tệp JSON: {e}") from e
    if not isinstance(data, dict) or data.get("type") != "service_account" or not data.get("client_email"):
        raise ValueError(
            "Tệp này không phải khoá service account của Google "
            '(cần có "type": "service_account" và "client_email").'
        )
    return data["client_email"]


@dataclass
class Settings:
    provider: str = "anthropic"
    model: str = ""
    spreadsheet_id: str = ""


class SettingsStore:
    """Đọc / ghi cấu hình. `secrets` có API như module `keyring` (get/set/delete_password)."""

    def __init__(self, config_dir: Path, data_dir: Path, secrets=None):
        self.config_dir = Path(config_dir)
        self.data_dir = Path(data_dir)
        if secrets is None:
            import keyring as secrets
        self.secrets = secrets

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def credentials_file(self) -> Path:
        return self.config_dir / "credentials.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    # Thiết lập thường

    def load(self) -> Settings:
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Settings()
        known = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__ and isinstance(v, str)}
        settings = Settings(**known)
        if settings.provider not in PROVIDERS:
            settings.provider = Settings.provider
        return settings

    def save(self, settings: Settings) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.settings_file)

    # Khoá bí mật

    def get_secret(self, name: str) -> str:
        try:
            return self.secrets.get_password(KEYRING_SERVICE, name) or ""
        except Exception:
            return ""

    def set_secret(self, name: str, value: str) -> None:
        """Lưu khoá; giá trị trống thì xoá."""
        value = value.strip()
        if value:
            self.secrets.set_password(KEYRING_SERVICE, name, value)
        else:
            self.delete_secret(name)

    def delete_secret(self, name: str) -> None:
        try:
            self.secrets.delete_password(KEYRING_SERVICE, name)
        except Exception:  # chưa có khoá này
            pass

    def telegram_token(self) -> str:
        return self.get_secret(TOKEN_SECRET)

    def api_key(self, provider: str) -> str:
        return self.get_secret(api_key_secret(provider))

    def delete_all_secrets(self) -> None:
        self.delete_secret(TOKEN_SECRET)
        for provider in PROVIDERS:
            self.delete_secret(api_key_secret(provider))

    # Tệp khoá Google

    def import_credentials(self, source: Path) -> str:
        """Kiểm tra rồi chép tệp khoá vào thư mục cấu hình (file gốc bị xoá vẫn chạy được).
        Trả về email của service account."""
        email = read_service_account(source)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if Path(source).resolve() != self.credentials_file.resolve():
            shutil.copyfile(source, self.credentials_file)
        return email

    def credentials_email(self) -> str | None:
        try:
            return read_service_account(self.credentials_file)
        except ValueError:
            return None

    # Tạo Config cho bot

    def to_config(
        self, settings: Settings, telegram_token: str | None = None, api_key: str | None = None
    ) -> Config:
        """Config cho bot. Token / key không truyền vào thì lấy từ Credential Manager."""
        return config_from(
            {
                "TELEGRAM_BOT_TOKEN": telegram_token if telegram_token is not None else self.telegram_token(),
                "SPREADSHEET_ID": parse_spreadsheet_id(settings.spreadsheet_id),
                "GOOGLE_CREDENTIALS_FILE": str(self.credentials_file),
                "DB_PATH": str(self.db_path),
            },
            provider=settings.provider,
            model=settings.model or None,
            api_key=api_key if api_key is not None else self.api_key(settings.provider),
        )

    def missing(self, settings: Settings) -> list[str]:
        """Những mục còn thiếu để bật được bot (rỗng = đủ)."""
        missing = []
        if not self.api_key(settings.provider):
            missing.append(PROVIDERS[settings.provider].key_label)
        if not self.telegram_token():
            missing.append("Telegram bot token")
        if not parse_spreadsheet_id(settings.spreadsheet_id):
            missing.append("Spreadsheet ID")
        if self.credentials_email() is None:
            missing.append("Tệp khoá Google")
        return missing

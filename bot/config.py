"""Cấu hình đọc từ biến môi trường (hoặc file .env)."""
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    telegram_token: str
    # Nhà cung cấp LLM (anthropic, openai, ...). API key đọc từ biến riêng của provider,
    # ví dụ ANTHROPIC_API_KEY, OPENAI_API_KEY. Model để trống thì dùng mặc định của provider.
    llm_provider: str
    llm_model: str | None
    llm_base_url: str | None
    db_path: str
    timezone: ZoneInfo
    # Giờ chốt các ngày trước (ghi task + tóm tắt), dạng "HH:MM" theo múi giờ ở trên
    daily_summary_time: str
    # Tin nhắn chưa xử lý của hôm nay vượt quá số ký tự này thì xử lý tin nhắn ngay giữa ngày
    # (tên biến môi trường COMPACT_* giữ nguyên để không làm hỏng file .env đang dùng)
    compact_threshold_chars: int
    # Số lần thử tối đa cho mỗi lần xử lý (LLM + ghi sheet), và thời gian chờ lần đầu (nhân đôi mỗi lần)
    compact_max_attempts: int
    compact_retry_delay_seconds: float
    # Google Sheet: nơi lưu task chính
    google_credentials_file: str
    spreadsheet_id: str
    # User id Telegram được quyền sửa task, ngoài admin của nhóm
    editor_user_ids: frozenset[int]
    # API key của provider; để trống thì provider đọc biến môi trường riêng của nó.
    # App desktop truyền key lấy từ Windows Credential Manager vào đây.
    llm_api_key: str | None = field(default=None, repr=False)
    # Userbot (tài khoản Telegram của chủ bot, nguồn tin nhắn). Thiếu một trong ba thì chạy chế độ dự
    # phòng: bot tự lưu tin nhóm như trước. Session cho toàn quyền vào tài khoản: không ghi log.
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = field(default=None, repr=False)
    userbot_session: str | None = field(default=None, repr=False)
    # Lần đầu bổ sung tin của một chat thì lấy bấy nhiêu ngày gần nhất
    userbot_history_days: int = 7

    @property
    def userbot_configured(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash and self.userbot_session)


def load_env_file() -> None:
    """Nạp .env vào biến môi trường. Chỉ bản dòng lệnh gọi; app desktop không đọc .env."""
    from dotenv import load_dotenv

    load_dotenv()


def llm_settings(
    provider: str | None = None, model: str | None = None, base_url: str | None = None
) -> tuple[str, str | None, str | None]:
    """(provider, model, base_url): tham số truyền vào (từ dòng lệnh) ghi đè biến môi trường.

    Biến môi trường để trống coi như không có. LLM_MODEL chỉ áp dụng cho provider trong
    LLM_PROVIDER: nếu dòng lệnh chọn provider khác mà không kèm --model thì dùng model mặc định
    của provider đó, tránh gửi nhầm tên model của hãng này sang hãng khác.
    """
    env_provider = os.environ.get("LLM_PROVIDER") or "anthropic"
    provider = provider or env_provider
    if not model and provider == env_provider:
        # CLAUDE_MODEL: tên biến cũ, vẫn nhận khi dùng anthropic
        model = os.environ.get("LLM_MODEL") or (
            os.environ.get("CLAUDE_MODEL") if provider == "anthropic" else None
        )
    return provider, model or None, base_url or os.environ.get("LLM_BASE_URL") or None


# Giá trị mặc định khi biến môi trường để trống (app desktop dùng nguyên các giá trị này)
DEFAULTS = {
    "DB_PATH": "data/bot.db",
    "TIMEZONE": "Asia/Ho_Chi_Minh",
    "DAILY_SUMMARY_TIME": "00:01",
    "COMPACT_THRESHOLD_CHARS": "60000",
    "COMPACT_MAX_ATTEMPTS": "3",
    "COMPACT_RETRY_DELAY_SECONDS": "60",
    "GOOGLE_CREDENTIALS_FILE": "credentials.json",
    "EDITOR_USER_IDS": "",
    "USERBOT_HISTORY_DAYS": "7",
}


def config_from(
    values: Mapping[str, str],
    provider: str,
    model: str | None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Config:
    """Tạo Config từ các giá trị dạng tên biến môi trường; thiếu hoặc để trống thì dùng DEFAULTS."""

    def get(name: str) -> str:
        return values.get(name) or DEFAULTS.get(name, "")

    if not get("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN")
    if not get("SPREADSHEET_ID"):
        raise RuntimeError("Thiếu SPREADSHEET_ID: task được lưu chính trên Google Sheet")
    return Config(
        telegram_token=get("TELEGRAM_BOT_TOKEN"),
        llm_provider=provider,
        llm_model=model or None,
        llm_base_url=base_url or None,
        db_path=get("DB_PATH"),
        timezone=ZoneInfo(get("TIMEZONE")),
        daily_summary_time=get("DAILY_SUMMARY_TIME"),
        compact_threshold_chars=int(get("COMPACT_THRESHOLD_CHARS")),
        compact_max_attempts=max(1, int(get("COMPACT_MAX_ATTEMPTS"))),
        compact_retry_delay_seconds=float(get("COMPACT_RETRY_DELAY_SECONDS")),
        google_credentials_file=get("GOOGLE_CREDENTIALS_FILE"),
        spreadsheet_id=get("SPREADSHEET_ID"),
        editor_user_ids=frozenset(int(x) for x in get("EDITOR_USER_IDS").replace(" ", "").split(",") if x),
        llm_api_key=api_key or None,
        telegram_api_id=int(get("TELEGRAM_API_ID")) if get("TELEGRAM_API_ID") else None,
        telegram_api_hash=get("TELEGRAM_API_HASH") or None,
        userbot_session=get("USERBOT_SESSION") or None,
        userbot_history_days=max(0, int(get("USERBOT_HISTORY_DAYS"))),
    )


def load_config(
    provider: str | None = None, model: str | None = None, base_url: str | None = None
) -> Config:
    """Đọc cấu hình từ biến môi trường (và file .env trong thư mục hiện hành). Các tham số (từ dòng
    lệnh) nếu có sẽ ghi đè biến môi trường."""
    load_env_file()
    return config_from(os.environ, *llm_settings(provider, model, base_url))

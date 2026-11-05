import json
from zoneinfo import ZoneInfo

import pytest

from telegram_bot.settings import (
    KEYRING_SERVICE, Settings, SettingsStore, parse_spreadsheet_id, read_service_account,
)
from tests.fakes_desktop import FakeKeyring

SA = {"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com", "private_key": "x"}


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "Config", tmp_path / "Data", secrets=FakeKeyring())


def write_sa(path, data=SA):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_parse_spreadsheet_id_from_link_or_id():
    link = "https://docs.google.com/spreadsheets/d/1AbC-d_9xYz/edit?gid=0#gid=0"
    assert parse_spreadsheet_id(link) == "1AbC-d_9xYz"
    assert parse_spreadsheet_id("  1AbC-d_9xYz ") == "1AbC-d_9xYz"
    assert parse_spreadsheet_id("") == ""


def test_settings_roundtrip_and_defaults(store):
    assert store.load() == Settings()
    store.save(Settings("openai", "gpt-6-luna", "sheet-id"))
    assert store.load() == Settings("openai", "gpt-6-luna", "sheet-id")
    # Provider không còn hỗ trợ trong app -> về mặc định
    store.settings_file.write_text(json.dumps({"provider": "openai-chat", "extra": 1}), encoding="utf-8")
    assert store.load().provider == Settings.provider


def test_api_keys_are_kept_per_provider(store):
    store.set_secret("anthropic_api_key", "sk-ant")
    store.set_secret("openai_api_key", "sk-oai")
    assert store.api_key("anthropic") == "sk-ant"
    assert store.api_key("openai") == "sk-oai"
    assert (KEYRING_SERVICE, "anthropic_api_key") in store.secrets.store
    # Giá trị trống = xoá; xoá khoá chưa có không lỗi
    store.set_secret("openai_api_key", " ")
    store.delete_secret("openai_api_key")
    assert store.api_key("openai") == ""
    store.delete_all_secrets()
    assert store.secrets.store == {}


def test_to_config_uses_app_paths_and_defaults(store, tmp_path):
    store.set_secret("telegram_bot_token", "123:abc")
    store.set_secret("anthropic_api_key", "sk-ant")
    settings = Settings("anthropic", "", "https://docs.google.com/spreadsheets/d/SHEET/edit")
    config = store.to_config(settings)
    assert config.telegram_token == "123:abc"
    assert config.llm_api_key == "sk-ant"
    assert config.llm_provider == "anthropic" and config.llm_model is None
    assert config.spreadsheet_id == "SHEET"
    assert config.db_path == str(tmp_path / "Data" / "bot.db")
    assert config.google_credentials_file == str(tmp_path / "Config" / "credentials.json")
    assert config.timezone == ZoneInfo("Asia/Ho_Chi_Minh")
    assert config.daily_summary_time == "00:01"
    assert config.editor_user_ids == frozenset()
    # Giá trị đang nhập trên form (chưa lưu) được ưu tiên
    config = store.to_config(Settings("openai", "gpt-6-luna", "S"), telegram_token="9:x", api_key="sk-new")
    assert (config.telegram_token, config.llm_api_key, config.llm_model) == ("9:x", "sk-new", "gpt-6-luna")


def test_import_credentials_copies_and_validates(store, tmp_path):
    source = write_sa(tmp_path / "key.json")
    assert store.credentials_email() is None
    assert store.import_credentials(source) == SA["client_email"]
    source.unlink()  # file gốc bị xoá: bản chép vẫn dùng được
    assert store.credentials_email() == SA["client_email"]

    bad = write_sa(tmp_path / "bad.json", {"type": "authorized_user"})
    with pytest.raises(ValueError):
        store.import_credentials(bad)
    (tmp_path / "junk.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        read_service_account(tmp_path / "junk.json")


def test_missing_lists_what_blocks_starting(store, tmp_path):
    settings = Settings("anthropic", "", "")
    assert store.missing(settings) == ["Anthropic API key", "Telegram bot token", "Spreadsheet ID", "Tệp khoá Google"]
    store.set_secret("anthropic_api_key", "k")
    store.set_secret("telegram_bot_token", "t")
    store.import_credentials(write_sa(tmp_path / "key.json"))
    assert store.missing(Settings("anthropic", "", "SHEET")) == []
    assert store.missing(Settings("openai", "m", "SHEET")) == ["OpenAI API key"]


def test_userbot_secrets_and_config(store):
    from telegram_bot.settings import valid_api_id

    store.set_secret("telegram_bot_token", "123:abc")
    settings = Settings("anthropic", "", "SHEET")
    # Chưa nhập API ID / Hash: không có userbot (chế độ dự phòng), không bị coi là thiếu cấu hình
    assert store.telegram_api() == ("", "") and not store.to_config(settings).userbot_configured
    assert "Telegram API ID" not in " ".join(store.missing(settings))

    store.set_secret("telegram_api_id", "1234567")
    store.set_secret("telegram_api_hash", "abcdef")
    store.set_secret("userbot_session", "SESSION")
    config = store.to_config(settings)
    assert config.userbot_configured
    assert (config.telegram_api_id, config.telegram_api_hash, config.userbot_session) == (1234567, "abcdef", "SESSION")
    assert "SESSION" not in repr(config)  # session không lộ khi in Config
    # Giá trị truyền vào (đang nhập trên form) được ưu tiên; session rỗng = chưa đăng nhập
    config = store.to_config(settings, telegram_api=("7", "h"), userbot_session="")
    assert (config.telegram_api_id, config.telegram_api_hash, config.userbot_session) == (7, "h", None)
    # API ID sai dạng: bỏ qua userbot
    assert not store.to_config(settings, telegram_api=("abc", "h"), userbot_session="S").userbot_configured

    assert valid_api_id(" 123 ") and not valid_api_id("12a") and not valid_api_id("")
    store.delete_all_secrets()
    assert store.secrets.store == {}

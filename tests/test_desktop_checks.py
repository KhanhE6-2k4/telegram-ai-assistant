import asyncio
from zoneinfo import ZoneInfo

import pytest

from bot import checks
from bot.config import config_from
from bot.providers import LLMProvider, Response, ToolCall


class ScriptedLLM(LLMProvider):
    """Trả lời thường, gọi công cụ khi có tool (lượt sau của vòng gọi công cụ thì trả lời chữ)."""

    def __init__(self, call_tools=True):
        super().__init__(model="m")
        self.call_tools = call_tools

    async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
        if tools and self.call_tools and messages[-1].role == "user":
            args = {"city": "Hà Nội"} if tools[0].name == "get_weather" else {"text": "xin chào"}
            return Response("", (ToolCall("c1", tools[0].name, args),))
        return Response("2")


def test_check_llm_reports_each_step():
    lines = []
    assert asyncio.run(checks.check_llm("x", None, report=lines.append, llm=ScriptedLLM())) == " / m"
    assert len(lines) == 4 and lines[2].startswith("2. Bắt buộc gọi công cụ: [('echo'")


def test_check_llm_fails_when_model_ignores_forced_tool():
    with pytest.raises(RuntimeError):
        asyncio.run(checks.check_llm("x", None, llm=ScriptedLLM(call_tools=False)))


def test_run_checks_reports_each_item_independently(monkeypatch):
    async def telegram_ok(token):
        return "@test_bot"

    def sheet_fails(credentials, spreadsheet_id):
        raise type("SpreadsheetNotFound", (Exception,), {})()

    monkeypatch.setattr(checks, "check_telegram", telegram_ok)
    monkeypatch.setattr(checks, "check_sheet", sheet_fails)
    config = config_from({"TELEGRAM_BOT_TOKEN": "t", "SPREADSHEET_ID": "s"}, "anthropic", None)
    results = asyncio.run(checks.run_checks(config, llm=ScriptedLLM()))
    assert [(r.name, r.ok) for r in results] == [("LLM", True), ("Telegram", True), ("Google Sheet", False)]
    assert results[1].detail == "@test_bot"
    assert "chưa Share sheet" in results[2].detail
    assert "SpreadsheetNotFound" not in results[2].detail  # không hiện tên lớp lỗi trong code
    assert checks.describe_error(ValueError("lạ")) == "lạ"
    assert checks.describe_error(ValueError()) == "Lỗi không rõ nguyên nhân, xem chi tiết trong log"


def test_config_from_defaults_and_required():
    config = config_from({"TELEGRAM_BOT_TOKEN": "t", "SPREADSHEET_ID": "s", "TIMEZONE": ""}, "openai", "m", api_key="k")
    assert config.timezone == ZoneInfo("Asia/Ho_Chi_Minh")
    assert (config.compact_max_attempts, config.llm_api_key, config.db_path) == (3, "k", "data/bot.db")
    with pytest.raises(RuntimeError):
        config_from({"TELEGRAM_BOT_TOKEN": "t"}, "openai", "m")


def test_run_checks_includes_userbot_in_telegram_item(monkeypatch):
    async def telegram_ok(token):
        return "@test_bot"

    seen = []

    async def userbot(api_id, api_hash, session):
        seen.append((api_id, api_hash, session))
        if not session:
            raise RuntimeError(checks.NOT_LOGGED_IN)
        return "Lan (user id 42)"

    monkeypatch.setattr(checks, "check_telegram", telegram_ok)
    monkeypatch.setattr(checks, "check_userbot", userbot)
    monkeypatch.setattr(checks, "check_sheet", lambda credentials, spreadsheet_id: "Sheet")
    values = {"TELEGRAM_BOT_TOKEN": "t", "SPREADSHEET_ID": "s"}

    # Không có API ID: như cũ, không kiểm tra userbot
    results = asyncio.run(checks.run_checks(config_from(values, "anthropic", None), llm=ScriptedLLM()))
    assert results[1].detail == "@test_bot" and seen == []

    with_api = {**values, "TELEGRAM_API_ID": "7", "TELEGRAM_API_HASH": "h", "USERBOT_SESSION": "S"}
    results = asyncio.run(checks.run_checks(config_from(with_api, "anthropic", None), llm=ScriptedLLM()))
    assert (results[1].ok, results[1].detail) == (True, "@test_bot · tài khoản: Lan (user id 42)")
    assert seen == [(7, "h", "S")]

    no_session = {**with_api, "USERBOT_SESSION": ""}
    results = asyncio.run(checks.run_checks(config_from(no_session, "anthropic", None), llm=ScriptedLLM()))
    assert (results[1].ok, results[1].detail) == (False, checks.NOT_LOGGED_IN)
    assert [r.name for r in results] == ["LLM", "Telegram", "Google Sheet"]  # không thêm mục riêng

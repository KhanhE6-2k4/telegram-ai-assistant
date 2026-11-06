"""Kiểm tra kết nối LLM, Telegram (bot, và tài khoản userbot nếu có API ID), Google Sheet.

Dùng chung cho `python -m bot.main --check` và nút "Kiểm tra kết nối" của app desktop. Các hàm trả
kết quả (hoặc ném lỗi) thay vì in ra màn hình.
"""
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .providers import ChatMessage, LLMProvider, Tool, create_provider, run_tool_loop
from .sheets import SheetClient


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


async def check_llm(
    provider: str,
    model: str | None,
    base_url: str | None = None,
    api_key: str | None = None,
    report: Callable[[str], None] = lambda line: None,
    llm: LLMProvider | None = None,
) -> str:
    """Gọi thử provider theo đúng các kiểu bot dùng: trả lời thường, bắt buộc gọi công cụ (như bước
    tóm tắt), và vòng gọi công cụ nhiều lượt (như bước ghi task / hỏi đáp). Từng bước được báo qua
    `report`. Trả về "provider / model"; lỗi thì ném ngoại lệ."""
    llm = llm or create_provider(provider, model, api_key=api_key, base_url=base_url)
    report(f"Provider: {llm.name}, model: {llm.model}")
    reply = await llm.complete(
        system="Trả lời ngắn bằng tiếng Việt.", messages=[ChatMessage.user("Chào bạn, 1 + 1 bằng mấy?")]
    )
    report(f"1. Trả lời thường: {reply.text}")

    echo = Tool("echo", "Lặp lại một câu.", {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
    })
    reply = await llm.complete(
        system="Luôn dùng công cụ được cung cấp.",
        messages=[ChatMessage.user("Hãy lặp lại câu: xin chào")],
        tools=[echo],
        tool_choice="echo",
    )
    report(f"2. Bắt buộc gọi công cụ: {[(c.name, c.arguments) for c in reply.tool_calls]}")
    if not reply.tool_calls:
        raise RuntimeError("Model không gọi công cụ khi bị bắt buộc")

    weather = Tool("get_weather", "Tra thời tiết hiện tại của một thành phố.", {
        "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
    })
    called = []

    async def execute(call):
        called.append((call.name, call.arguments))
        return json.dumps({"city": call.arguments.get("city"), "temp_c": 31, "sky": "nắng"}, ensure_ascii=False)

    answer = await run_tool_loop(
        llm,
        system="Dùng công cụ để tra cứu rồi trả lời ngắn bằng tiếng Việt.",
        prompt="Thời tiết ở Hà Nội thế nào?",
        tools=[weather],
        execute=execute,
        max_rounds=4,
        purpose="kiểm tra kết nối",
    )
    report(f"3. Vòng gọi công cụ: {called} -> {answer}")
    return f"{llm.name} / {llm.model}"


async def check_telegram(token: str) -> str:
    """Gọi getMe. Trả về @username của bot."""
    from telegram import Bot

    async with Bot(token) as bot:  # initialize() đã gọi getMe
        return f"@{bot.username}"


NOT_LOGGED_IN = "Chưa đăng nhập Telegram hoặc phiên Telegram đã hết hạn"


async def check_userbot(api_id: int, api_hash: str, session: str | None) -> str:
    """Session userbot còn đăng nhập không. Trả về nhãn tài khoản; chưa đăng nhập / hết hạn thì ném lỗi."""
    from .userbot_auth import check_session

    label = await check_session(api_id, api_hash, session or "")
    if label is None:
        raise RuntimeError(NOT_LOGGED_IN)
    return label


async def check_telegram_all(config: Config) -> str:
    """Bot token, và tài khoản userbot nếu đã nhập Telegram API ID (gộp thành một mục "Telegram")."""
    bot = await check_telegram(config.telegram_token)
    if not config.telegram_api_id:
        return bot
    account = await check_userbot(config.telegram_api_id, config.telegram_api_hash, config.userbot_session)
    return f"{bot} · tài khoản: {account}"


def check_sheet(credentials_file: str, spreadsheet_id: str) -> str:
    """Mở Google Sheet bằng service account (lời gọi chặn). Trả về tên sheet."""
    return SheetClient.open(credentials_file, spreadsheet_id, title_of=lambda chat_id: None).sheet.title


def describe_error(e: BaseException) -> str:
    """Thông báo lỗi dễ hiểu cho người dùng (hiện trên giao diện). Không kèm tên lớp lỗi trong code: tên
    lỗi và traceback nằm trong log."""
    name = type(e).__name__
    text = str(e) or "Lỗi không rõ nguyên nhân, xem chi tiết trong log"
    hints = {
        "InvalidToken": "Telegram bot token không đúng",
        "ApiIdInvalidError": "Telegram API ID / API Hash không đúng",
        "AuthenticationError": "API key không đúng hoặc đã bị thu hồi",
        "PermissionDeniedError": "API key không có quyền dùng model này",
        "NotFoundError": "Không có model này (kiểm tra lại tên model)",
        "SpreadsheetNotFound": "Không tìm thấy sheet: sai Spreadsheet ID, hoặc chưa Share sheet cho email "
        "service account",
        "FileNotFoundError": "Không tìm thấy tệp khoá Google (credentials.json)",
    }
    if name == "RuntimeError" and text == NOT_LOGGED_IN:
        return text
    if name in hints:
        return hints[name]
    if name == "APIError" and "403" in text:
        return "Service account chưa có quyền với sheet: hãy Share sheet với quyền Editor"
    return text


async def run_checks(config: Config, llm: LLMProvider | None = None) -> list[CheckResult]:
    """Chạy cả 3 phép kiểm tra (song song); lỗi của mục này không làm dừng mục khác."""

    async def one(name: str, coro) -> CheckResult:
        try:
            return CheckResult(name, True, await coro)
        except Exception as e:
            return CheckResult(name, False, describe_error(e))

    return list(await asyncio.gather(
        one("LLM", check_llm(
            config.llm_provider, config.llm_model, config.llm_base_url, config.llm_api_key, llm=llm
        )),
        one("Telegram", check_telegram_all(config)),
        one("Google Sheet", asyncio.to_thread(
            check_sheet, config.google_credentials_file, config.spreadsheet_id
        )),
    ))

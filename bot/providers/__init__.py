"""Các nhà cung cấp LLM, độc lập với phần còn lại của bot.

    from bot.providers import create_provider
    llm = create_provider("openai", model="...")
    response = await llm.complete(system=..., messages=[ChatMessage.user("...")], tools=[...])
"""
from .base import ChatMessage, LLMProvider, Response, Tool, ToolCall, ToolResult, run_tool_loop
from .factory import available_providers, create_provider, register_provider

# Import để các provider tự đăng ký với factory (SDK chỉ được import khi tạo provider)
from . import anthropic_provider, openai_provider  # noqa: E402,F401

__all__ = [
    "ChatMessage",
    "LLMProvider",
    "Response",
    "Tool",
    "ToolCall",
    "ToolResult",
    "available_providers",
    "create_provider",
    "register_provider",
    "run_tool_loop",
]

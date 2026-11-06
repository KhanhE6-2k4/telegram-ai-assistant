"""Giao diện chung cho các nhà cung cấp LLM (Strategy pattern).

Phần còn lại của bot (summarizer, qa) chỉ làm việc với các kiểu dữ liệu trung lập ở đây:
Tool, ToolCall, ToolResult, ChatMessage, Response và LLMProvider.complete(). Mỗi provider
(Anthropic, OpenAI, ...) tự chuyển đổi sang/khỏi định dạng API riêng của mình.
"""
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from ..timing import log_elapsed, timed

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    """Công cụ cho model gọi. `parameters` là JSON Schema của đối số."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str


@dataclass(frozen=True)
class ChatMessage:
    """Một lượt hội thoại.

    - role="user": `text` là nội dung người dùng.
    - role="assistant": `text` và/hoặc `tool_calls` model đã trả về.
    - role="tool": `tool_results` là kết quả các công cụ vừa chạy.
    """

    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    #: Dữ liệu riêng của provider đã tạo ra lượt này (ví dụ các item suy luận của model reasoning),
    #: để chính provider đó gửi lại nguyên vẹn ở lượt sau. Provider khác bỏ qua trường này.
    provider_data: Any = field(default=None, compare=False)

    @classmethod
    def user(cls, text: str) -> "ChatMessage":
        return cls("user", text=text)

    @classmethod
    def tools(cls, results: Sequence[ToolResult]) -> "ChatMessage":
        return cls("tool", tool_results=tuple(results))


@dataclass(frozen=True)
class Response:
    text: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    provider_data: Any = field(default=None, compare=False)

    def as_message(self) -> ChatMessage:
        return ChatMessage(
            "assistant", text=self.text, tool_calls=self.tool_calls, provider_data=self.provider_data
        )


class LLMProvider(ABC):
    """Strategy: một cách gọi LLM. Lớp con đăng ký với factory bằng @register_provider."""

    #: Tên dùng trong LLM_PROVIDER / --provider (được factory gán khi đăng ký)
    name: ClassVar[str] = ""
    #: Biến môi trường chứa API key mặc định
    api_key_env: ClassVar[str] = ""
    #: Model mặc định; để trống nghĩa là bắt buộc truyền model
    default_model: ClassVar[str] = ""

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[Tool] = (),
        tool_choice: str | None = None,
        max_tokens: int = 4000,
    ) -> Response:
        """Gọi model một lượt. `tool_choice` = tên công cụ thì bắt buộc model gọi công cụ đó."""


async def run_tool_loop(
    llm: LLMProvider,
    *,
    system: str,
    prompt: str,
    tools: Sequence[Tool],
    execute: Callable[[ToolCall], Awaitable[str]],
    max_rounds: int,
    max_tokens: int = 4000,
    history: Sequence[ChatMessage] = (),
    purpose: str = "LLM",
) -> str | None:
    """Vòng gọi công cụ: hỏi model, chạy các công cụ nó yêu cầu, gửi kết quả lại, đến khi model
    trả lời không kèm công cụ. Trả về câu trả lời cuối, hoặc None nếu vượt quá max_rounds.
    `history`: các lượt hội thoại trước (ngữ cảnh), gửi trước `prompt`.
    `purpose`: tên việc trong log thời gian (vd. "hỏi đáp", "ghi task"). Mỗi lượt gọi LLM và mỗi công cụ
    được ghi thời gian, kèm độ dài dữ liệu gửi đi (prompt càng dài, model càng chậm)."""
    messages = [*history, ChatMessage.user(prompt)]
    for round_no in range(1, max_rounds + 1):
        start = time.perf_counter()
        response = await llm.complete(system=system, messages=messages, tools=tools, max_tokens=max_tokens)
        log_elapsed(
            log, start, "gọi LLM (%s) lượt %d, gửi %d ký tự, kết quả: %s",
            purpose, round_no, prompt_chars(system, messages),
            f"{len(response.tool_calls)} lệnh gọi công cụ" if response.tool_calls else "câu trả lời",
        )
        if not response.tool_calls:
            return response.text
        messages.append(response.as_message())
        results = []
        for call in response.tool_calls:
            with timed(log, "công cụ %s (%s)", call.name, purpose):
                results.append(ToolResult(call.id, await execute(call)))
        messages.append(ChatMessage.tools(results))
    return None


def prompt_chars(system: str, messages: Sequence[ChatMessage]) -> int:
    """Độ dài (ký tự) phần chữ gửi cho model: system + các lượt + kết quả công cụ. Chỉ để ghi log."""
    return len(system) + sum(len(m.text) + sum(len(r.content) for r in m.tool_results) for m in messages)

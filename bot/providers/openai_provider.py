"""Provider OpenAI.

- "openai": Responses API (/v1/responses). Dùng được function tools với model reasoning, giữ lại
  phần suy luận của model giữa các vòng gọi công cụ.
- "openai-chat": Chat Completions API (/v1/chat/completions). Dành cho các dịch vụ tương thích
  OpenAI chưa có Responses API (Ollama, vLLM, OpenRouter, ...), truyền LLM_BASE_URL / --base-url.
"""
import json
from collections.abc import Sequence

from .base import ChatMessage, LLMProvider, Response, Tool, ToolCall
from .factory import register_provider

# Model reasoning tính cả token suy luận vào max_output_tokens; cộng thêm phần này để suy luận
# không ăn hết chỗ của câu trả lời / lệnh gọi công cụ.
REASONING_HEADROOM_TOKENS = 16000


class _OpenAIBase(LLMProvider):
    api_key_env = "OPENAI_API_KEY"
    default_model = ""  # bắt buộc chỉ định model

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        super().__init__(model, api_key, base_url)
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)


@register_provider("openai")
class OpenAIProvider(_OpenAIBase):
    """Responses API, chạy không lưu trạng thái phía server (store=False): mỗi lượt gửi lại toàn bộ
    hội thoại. Phần suy luận được trả về dạng mã hoá và gửi lại qua ChatMessage.provider_data."""

    @staticmethod
    def _convert(message: ChatMessage) -> list:
        if message.role == "user":
            return [{"role": "user", "content": message.text}]
        if message.role == "assistant":
            if message.provider_data is not None:
                # Các output item gốc (reasoning, message, function_call) của chính lượt đó
                return list(message.provider_data)
            items = [{"role": "assistant", "content": message.text}] if message.text else []
            items += [
                {
                    "type": "function_call",
                    "call_id": c.id,
                    "name": c.name,
                    "arguments": json.dumps(c.arguments, ensure_ascii=False),
                }
                for c in message.tool_calls
            ]
            return items
        return [
            {"type": "function_call_output", "call_id": r.call_id, "output": r.content}
            for r in message.tool_results
        ]

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[Tool] = (),
        tool_choice: str | None = None,
        max_tokens: int = 4000,
    ) -> Response:
        kwargs = {}
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "strict": False,
                }
                for t in tools
            ]
        if tool_choice:
            kwargs["tool_choice"] = {"type": "function", "name": tool_choice}
        response = await self.client.responses.create(
            model=self.model,
            instructions=system,
            input=[item for message in messages for item in self._convert(message)],
            max_output_tokens=max_tokens + REASONING_HEADROOM_TOKENS,
            store=False,
            include=["reasoning.encrypted_content"],
            **kwargs,
        )
        calls = tuple(
            ToolCall(item.call_id, item.name, json.loads(item.arguments or "{}"))
            for item in response.output
            if item.type == "function_call"
        )
        text = (response.output_text or "").strip()
        if response.status == "incomplete" and not calls and not text:
            reason = getattr(response.incomplete_details, "reason", None)
            raise RuntimeError(f"Model trả về chưa xong (incomplete: {reason})")
        return Response(text=text, tool_calls=calls, provider_data=list(response.output))


@register_provider("openai-chat")
class OpenAIChatProvider(_OpenAIBase):
    @staticmethod
    def _convert(message: ChatMessage) -> list[dict]:
        if message.role == "user":
            return [{"role": "user", "content": message.text}]
        if message.role == "assistant":
            out = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                out["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                    }
                    for c in message.tool_calls
                ]
            return [out]
        # Mỗi kết quả công cụ là một message role="tool" riêng
        return [{"role": "tool", "tool_call_id": r.call_id, "content": r.content} for r in message.tool_results]

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[Tool] = (),
        tool_choice: str | None = None,
        max_tokens: int = 4000,
    ) -> Response:
        kwargs = {}
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        if tool_choice:
            kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_choice}}
        response = await self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}]
            + [m for message in messages for m in self._convert(message)],
            **kwargs,
        )
        msg = response.choices[0].message
        return Response(
            text=(msg.content or "").strip(),
            tool_calls=tuple(
                ToolCall(c.id, c.function.name, json.loads(c.function.arguments or "{}"))
                for c in (msg.tool_calls or [])
            ),
        )

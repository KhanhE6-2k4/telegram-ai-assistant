"""Provider Anthropic (Claude), dùng Messages API với tool use."""
from collections.abc import Sequence

from .base import ChatMessage, LLMProvider, Response, Tool, ToolCall
from .factory import register_provider


@register_provider("anthropic")
class AnthropicProvider(LLMProvider):
    api_key_env = "ANTHROPIC_API_KEY"
    default_model = "claude-sonnet-5-5"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        super().__init__(model, api_key, base_url)
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    @staticmethod
    def _convert(message: ChatMessage) -> dict:
        if message.role == "user":
            return {"role": "user", "content": message.text}
        if message.role == "assistant":
            blocks = [{"type": "text", "text": message.text}] if message.text else []
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in message.tool_calls
            ]
            return {"role": "assistant", "content": blocks}
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content}
                for r in message.tool_results
            ],
        }

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
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        if tool_choice:
            kwargs["tool_choice"] = {"type": "tool", "name": tool_choice}
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[self._convert(m) for m in messages],
            **kwargs,
        )
        return Response(
            text="".join(b.text for b in response.content if b.type == "text").strip(),
            tool_calls=tuple(
                ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"
            ),
        )

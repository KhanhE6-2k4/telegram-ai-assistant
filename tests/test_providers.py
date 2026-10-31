import asyncio
import json
from types import SimpleNamespace

import pytest

from bot.config import llm_settings
from bot.main import parse_args
from bot.providers import (
    ChatMessage, LLMProvider, Response, Tool, ToolCall, ToolResult,
    available_providers, create_provider, register_provider, run_tool_loop,
)
from bot.providers.factory import _REGISTRY

ECHO = Tool("echo", "Lặp lại.", {"type": "object", "properties": {"text": {"type": "string"}}})
# Hội thoại có đủ các loại lượt: user, assistant gọi công cụ, kết quả công cụ
HISTORY = [
    ChatMessage.user("hỏi"),
    ChatMessage("assistant", text="để mình xem", tool_calls=(ToolCall("c1", "echo", {"text": "à"}),)),
    ChatMessage.tools([ToolResult("c1", "à")]),
]


class Recorder:
    """Thay client SDK: ghi lại tham số và trả về phản hồi cho trước."""

    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


# ---- factory ----

def test_factory_lists_and_creates_providers(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert {"anthropic", "openai", "openai-chat"} <= set(available_providers())
    llm = create_provider("anthropic")
    assert llm.name == "anthropic" and llm.model == "claude-sonnet-5-5"
    assert create_provider("anthropic", model="claude-haiku-5-5").model == "claude-haiku-5-5"


def test_factory_errors():
    with pytest.raises(ValueError, match="Không có provider"):
        create_provider("khong-co")
    with pytest.raises(ValueError, match="cần chỉ định model"):
        create_provider("openai", api_key="k")


def test_register_custom_provider():
    @register_provider("test-echo")
    class EchoProvider(LLMProvider):
        default_model = "echo-1"

        async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
            return Response(messages[-1].text)

    try:
        llm = create_provider("test-echo")
        assert asyncio.run(llm.complete(system="", messages=[ChatMessage.user("xin chào")])).text == "xin chào"
        with pytest.raises(ValueError, match="đã được đăng ký"):
            register_provider("test-echo")(EchoProvider)
    finally:
        _REGISTRY.pop("test-echo")


def test_llm_settings_and_cli_override_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-old")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert llm_settings() == ("anthropic", "claude-old", None)
    args = parse_args(["--provider", "openai", "--model", "gpt-x", "--base-url", "http://localhost:8000/v1"])
    assert llm_settings(args.provider, args.model, args.base_url) == ("openai", "gpt-x", "http://localhost:8000/v1")
    with pytest.raises(SystemExit):
        parse_args(["--provider", "khong-co"])


def test_llm_settings_empty_env_and_cross_provider_model(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    # Để trống trong .env coi như không có
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("LLM_MODEL", "")
    assert llm_settings() == ("anthropic", None, None)
    assert llm_settings("openai", "gpt-x") == ("openai", "gpt-x", None)
    # LLM_MODEL của anthropic không bị gửi nhầm sang openai khi đổi provider qua dòng lệnh
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    assert llm_settings("openai") == ("openai", None, None)
    assert llm_settings("anthropic") == ("anthropic", "claude-sonnet-5-5", None)


# ---- vòng gọi công cụ ----

def test_run_tool_loop_feeds_results_back():
    class TwoStep(LLMProvider):
        def __init__(self):
            super().__init__("m")
            self.seen = []

        async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
            self.seen.append(list(messages))
            if len(self.seen) == 1:
                return Response("", (ToolCall("c1", "echo", {"text": "hi"}),))
            return Response(f"xong: {messages[-1].tool_results[0].content}")

    llm = TwoStep()

    async def execute(call):
        return call.arguments["text"].upper()

    out = asyncio.run(run_tool_loop(llm, system="s", prompt="p", tools=[ECHO], execute=execute, max_rounds=3))
    assert out == "xong: HI"
    assert [m.role for m in llm.seen[1]] == ["user", "assistant", "tool"]


def test_run_tool_loop_gives_up_after_max_rounds():
    class Forever(LLMProvider):
        async def complete(self, **kw):
            return Response("", (ToolCall("c", "echo", {}),))

    async def execute(call):
        return "x"

    assert asyncio.run(run_tool_loop(Forever("m"), system="s", prompt="p", tools=[ECHO],
                                     execute=execute, max_rounds=2)) is None


# ---- chuyển đổi định dạng từng provider ----

def test_anthropic_request_and_response():
    llm = create_provider("anthropic", api_key="k")
    llm.client = SimpleNamespace(messages=Recorder(SimpleNamespace(content=[
        SimpleNamespace(type="text", text=" ok "),
        SimpleNamespace(type="tool_use", id="c2", name="echo", input={"text": "b"}),
    ])))
    out = asyncio.run(llm.complete(system="sys", messages=HISTORY, tools=[ECHO], tool_choice="echo", max_tokens=99))

    kw = llm.client.messages.kwargs
    assert kw["system"] == "sys" and kw["max_tokens"] == 99 and kw["model"] == "claude-sonnet-5-5"
    assert kw["tools"] == [{"name": "echo", "description": "Lặp lại.", "input_schema": ECHO.parameters}]
    assert kw["tool_choice"] == {"type": "tool", "name": "echo"}
    assert kw["messages"] == [
        {"role": "user", "content": "hỏi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "để mình xem"},
            {"type": "tool_use", "id": "c1", "name": "echo", "input": {"text": "à"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "à"}]},
    ]
    assert out == Response("ok", (ToolCall("c2", "echo", {"text": "b"}),))


def responses_reply(output, text="", status="completed"):
    return SimpleNamespace(output=output, output_text=text, status=status, incomplete_details=None)


def test_openai_responses_request_and_response():
    llm = create_provider("openai", model="gpt-x", api_key="k")
    reasoning = SimpleNamespace(type="reasoning", id="rs_1", encrypted_content="...")
    call = SimpleNamespace(type="function_call", call_id="c2", name="echo", arguments='{"text": "b"}')
    llm.client = SimpleNamespace(responses=Recorder(responses_reply([reasoning, call])))
    out = asyncio.run(llm.complete(system="sys", messages=HISTORY, tools=[ECHO], tool_choice="echo", max_tokens=99))

    kw = llm.client.responses.kwargs
    assert kw["model"] == "gpt-x" and kw["instructions"] == "sys"
    assert kw["max_output_tokens"] > 99  # có thêm chỗ cho token suy luận
    assert kw["store"] is False and kw["include"] == ["reasoning.encrypted_content"]
    assert kw["tools"] == [{"type": "function", "name": "echo", "description": "Lặp lại.",
                            "parameters": ECHO.parameters, "strict": False}]
    assert kw["tool_choice"] == {"type": "function", "name": "echo"}
    assert kw["input"] == [
        {"role": "user", "content": "hỏi"},
        {"role": "assistant", "content": "để mình xem"},
        {"type": "function_call", "call_id": "c1", "name": "echo", "arguments": json.dumps({"text": "à"}, ensure_ascii=False)},
        {"type": "function_call_output", "call_id": "c1", "output": "à"},
    ]
    assert out == Response("", (ToolCall("c2", "echo", {"text": "b"}),))
    assert out.provider_data == [reasoning, call]


def test_openai_responses_replays_reasoning_items_in_tool_loop():
    """Model reasoning: lượt sau phải gửi lại nguyên các item (kể cả reasoning) của lượt trước."""
    llm = create_provider("openai", model="gpt-x", api_key="k")
    reasoning = SimpleNamespace(type="reasoning", id="rs_1", encrypted_content="...")
    call = SimpleNamespace(type="function_call", call_id="c1", name="echo", arguments='{"text": "a"}')
    replies = iter([responses_reply([reasoning, call]), responses_reply([], text="xong")])
    inputs = []

    async def create(**kw):
        inputs.append(kw["input"])
        return next(replies)

    llm.client = SimpleNamespace(responses=SimpleNamespace(create=create))

    async def execute(c):
        return "A"

    out = asyncio.run(run_tool_loop(llm, system="s", prompt="p", tools=[ECHO], execute=execute, max_rounds=3))
    assert out == "xong"
    assert inputs[1] == [
        {"role": "user", "content": "p"},
        reasoning,
        call,
        {"type": "function_call_output", "call_id": "c1", "output": "A"},
    ]


def test_openai_responses_incomplete_without_output_raises():
    llm = create_provider("openai", model="gpt-x", api_key="k")
    llm.client = SimpleNamespace(responses=Recorder(SimpleNamespace(
        output=[], output_text="", status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"))))
    with pytest.raises(RuntimeError, match="max_output_tokens"):
        asyncio.run(llm.complete(system="s", messages=[ChatMessage.user("hi")]))


def test_openai_chat_request_and_response():
    llm = create_provider("openai-chat", model="gpt-x", api_key="k")
    message = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
        id="c2", function=SimpleNamespace(name="echo", arguments='{"text": "b"}'))])
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=Recorder(
        SimpleNamespace(choices=[SimpleNamespace(message=message)]))))
    out = asyncio.run(llm.complete(system="sys", messages=HISTORY, tools=[ECHO], tool_choice="echo", max_tokens=99))

    kw = llm.client.chat.completions.kwargs
    assert kw["model"] == "gpt-x" and kw["max_completion_tokens"] == 99
    assert kw["tools"] == [{"type": "function", "function": {
        "name": "echo", "description": "Lặp lại.", "parameters": ECHO.parameters}}]
    assert kw["tool_choice"] == {"type": "function", "function": {"name": "echo"}}
    assert kw["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hỏi"},
        {"role": "assistant", "content": "để mình xem", "tool_calls": [{
            "id": "c1", "type": "function", "function": {"name": "echo", "arguments": json.dumps({"text": "à"}, ensure_ascii=False)},
        }]},
        {"role": "tool", "tool_call_id": "c1", "content": "à"},
    ]
    assert out == Response("", (ToolCall("c2", "echo", {"text": "b"}),))


def test_no_tools_means_no_tool_params():
    llm = create_provider("openai-chat", model="gpt-x", api_key="k")
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=Recorder(SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="chào", tool_calls=None))]))))
    out = asyncio.run(llm.complete(system="s", messages=[ChatMessage.user("hi")]))
    assert "tools" not in llm.client.chat.completions.kwargs
    assert out == Response("chào")

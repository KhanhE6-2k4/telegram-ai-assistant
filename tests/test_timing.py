"""Log đo thời gian (bot/timing.py) và các dòng đo trong vòng gọi công cụ."""
import asyncio
import logging

import pytest

from bot.providers import LLMProvider, Response, Tool, ToolCall, run_tool_loop
from bot.timing import PREFIX, timed

log = logging.getLogger("test.timing")


def timing_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(PREFIX)]


def test_timed_logs_duration_and_errors(caplog):
    caplog.set_level(logging.INFO)
    with timed(log, "đọc Google Sheet của chat %s", "Team A"):
        pass
    with pytest.raises(ValueError), timed(log, "ghi Google Sheet"):
        raise ValueError
    lines = timing_lines(caplog)
    assert lines[0].startswith("Thời gian - đọc Google Sheet của chat Team A: ") and lines[0].endswith(" s")
    assert lines[1].endswith(" s (lỗi)")


def test_run_tool_loop_logs_each_llm_round_and_tool(caplog):
    caplog.set_level(logging.INFO)

    class TwoRounds(LLMProvider):
        async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
            if len(messages) == 1:
                return Response("", (ToolCall("c1", "list_tasks", {}), ToolCall("c2", "get_day", {"day": "x"})))
            return Response("xong")

    async def execute(call):
        return "{}"

    tool = Tool("list_tasks", "", {"type": "object", "properties": {}})
    out = asyncio.run(run_tool_loop(TwoRounds("m"), system="s", prompt="câu hỏi", tools=[tool], execute=execute,
                                    max_rounds=3, purpose="hỏi đáp"))
    assert out == "xong"
    lines = timing_lines(caplog)
    assert lines[0].startswith("Thời gian - gọi LLM (hỏi đáp) lượt 1, gửi 8 ký tự, kết quả: 2 lệnh gọi công cụ")
    assert lines[1].startswith("Thời gian - công cụ list_tasks (hỏi đáp): ")
    assert lines[2].startswith("Thời gian - công cụ get_day (hỏi đáp): ")
    assert lines[3].startswith("Thời gian - gọi LLM (hỏi đáp) lượt 2, gửi 12 ký tự, kết quả: câu trả lời")
    # Log không chứa nội dung câu hỏi
    assert not any("câu hỏi" in line for line in lines)

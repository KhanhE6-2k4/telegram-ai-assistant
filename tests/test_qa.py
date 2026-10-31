import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.providers import LLMProvider, Response, ToolCall
from bot.qa import QA
from bot.sheets import TaskSheet, apply_edit
from bot.storage import Storage
from tests.test_sheets import DEADLINE, FakeWorksheet, task


class ScriptedLLM(LLMProvider):
    """Lượt 1 trả về `first` (danh sách ToolCall); lượt sau trả lời bằng văn bản do `reply(messages)` tạo."""

    def __init__(self, first, reply=lambda messages: "ok"):
        super().__init__("fake")
        self.first = first
        self.reply = reply
        self.calls = []

    async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
        self.calls.append({"system": system, "tools": list(tools)})
        if len(self.calls) == 1:
            return Response("", tuple(self.first))
        return Response(self.reply(messages))


class FakeSheets:
    def __init__(self, ws):
        self.ws = ws

    def worksheet_for(self, chat_id):
        return self.ws


def test_answer_reads_tasks_from_sheet_with_day():
    s, ws = Storage(":memory:"), FakeWorksheet()
    TaskSheet(ws, s, -1).add(task("Báo cáo Q3", deadline="2026-10-30"), "2026-10-27")
    ws.rows[1][DEADLINE] = "2026-11-05"  # sửa tay trên sheet: bot phải đọc giá trị này
    llm = ScriptedLLM(
        [ToolCall("t1", "list_tasks", {})],
        reply=lambda m: f"Deadline: {json.loads(m[-1].tool_results[0].content)[0]['deadline']}",
    )
    qa = QA(s, llm, FakeSheets(ws))
    out, proposals = asyncio.run(qa.answer(-1, "Nhóm A", "2026-10-28", "An", "task báo cáo hạn khi nào?"))
    assert out == "Deadline: 2026-11-05"
    assert proposals == []
    assert qa._list_tasks(-1, "2026-10-28", None, None)[0]["day"] == "2026-10-27"


def test_list_tasks_filters_by_day_but_keeps_recently_updated():
    s, ws = Storage(":memory:"), FakeWorksheet()
    sheet = TaskSheet(ws, s, -1)
    sheet.add(task("Old"), "2026-08-01")
    sheet.add(task("Old but updated"), "2026-08-01")
    sheet.update(2, {"progress": "Đang làm"}, "2026-10-27")
    sheet.add(task("A"), "2026-10-27")
    qa = QA(s, None, FakeSheets(ws))
    assert [t["name"] for t in qa._list_tasks(-1, "2026-10-28", None, None)] == ["Old but updated", "A"]


def test_get_day_includes_uncompacted_messages():
    s = Storage(":memory:")
    s.add_message(-1, 5, 1, "An", "nộp báo cáo trước thứ 6 nhé", None,
                  datetime(2026, 10, 28, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")))
    day = QA(s, None, None)._get_day(-1, "2026-10-28")
    assert "nộp báo cáo" in day["recent_messages"][0]


def edit_llm():
    return ScriptedLLM([ToolCall("t1", "propose_task_edit", {"stt": 1, "changes": {"deadline": "2026-11-10"}})],
                       reply=lambda m: "Chờ xác nhận")


def test_editor_gets_proposal_and_apply_edit_writes_sheet():
    s, ws = Storage(":memory:"), FakeWorksheet()
    TaskSheet(ws, s, -1).add(task("Báo cáo", deadline="2026-10-30"), "2026-10-27")
    qa = QA(s, edit_llm(), FakeSheets(ws))
    _, proposals = asyncio.run(qa.answer(-1, "A", "2026-10-28", "An", "dời hạn báo cáo sang 10/11", can_edit=True))
    assert proposals == [{"stt": 1, "changes": {"deadline": "2026-11-10"},
                          "current": {"name": "Báo cáo", "assignee": "", "description": "d", "progress": "Mới giao", "deadline": "2026-10-30"}}]
    assert ws.rows[1][DEADLINE] == "2026-10-30"  # chưa ghi khi chưa xác nhận

    apply_edit(s, ws, -1, 1, {"deadline": "2026-11-10"}, "2026-10-28")
    assert ws.rows[1][DEADLINE] == "2026-11-10"
    assert s.task_days(-1)[1] == ("2026-10-27", "2026-10-28")


def test_non_editor_has_no_edit_tool():
    s, ws = Storage(":memory:"), FakeWorksheet()
    llm = edit_llm()
    qa = QA(s, llm, FakeSheets(ws))
    _, proposals = asyncio.run(qa.answer(-1, "A", "2026-10-28", "An", "dời hạn", can_edit=False))
    assert "propose_task_edit" not in [t.name for t in llm.calls[0]["tools"]]
    assert "KHÔNG có quyền" in llm.calls[0]["system"]
    assert proposals == []

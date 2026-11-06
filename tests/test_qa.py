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


class OneShotLLM(LLMProvider):
    """Trả lời ngay ở lượt đầu, không gọi công cụ; ghi lại prompt nhận được."""

    def __init__(self):
        super().__init__("fake")
        self.prompts = []

    async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
        self.prompts.append(messages[-1].text)
        return Response("Báo cáo Q3 hạn 30/10")


def test_answer_preloads_tasks_and_today_so_one_llm_round_is_enough():
    s, ws = Storage(":memory:"), FakeWorksheet()
    TaskSheet(ws, s, -1).add(task("Báo cáo Q3", deadline="2026-10-30"), "2026-10-27")
    s.add_message(-1, 5, 1, "An", "Lan nộp slide trước thứ 6 nhé", None,
                  datetime(2026, 10, 28, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")))
    ws.reads = 0
    llm = OneShotLLM()
    out, _ = asyncio.run(QA(s, llm, FakeSheets(ws)).answer(-1, "Nhóm A", "2026-10-28", "An", "task nào sắp tới hạn?"))
    assert out == "Báo cáo Q3 hạn 30/10"
    assert len(llm.prompts) == 1  # một lượt gọi LLM
    prompt = llm.prompts[0]
    assert prompt.startswith("[Thứ 4, 28/10/2026] An hỏi: task nào sắp tới hạn?")  # ngày hỏi ở đầu câu
    assert "Báo cáo Q3" in prompt and "Lan nộp slide trước thứ 6" in prompt
    assert ws.reads == 1


def test_tools_reuse_preloaded_sheet_within_one_answer():
    s, ws = Storage(":memory:"), FakeWorksheet()
    TaskSheet(ws, s, -1).add(task("Báo cáo", deadline="2026-10-30"), "2026-10-27")
    ws.reads = 0
    llm = ScriptedLLM([ToolCall("t1", "list_tasks", {"from_day": "2026-01-01"}),
                       ToolCall("t2", "propose_task_edit", {"stt": 1, "changes": {"deadline": "2026-11-10"}})])
    _, proposals = asyncio.run(QA(s, llm, FakeSheets(ws)).answer(-1, "A", "2026-10-28", "An", "dời hạn", can_edit=True))
    assert proposals[0]["current"]["deadline"] == "2026-10-30"
    assert ws.reads == 1  # nạp sẵn, list_tasks và propose_task_edit dùng chung một lần đọc


def test_answer_still_works_when_preload_fails():
    class BrokenSheets:
        def worksheet_for(self, chat_id):
            raise RuntimeError("mất mạng")

    llm = OneShotLLM()
    out, _ = asyncio.run(QA(Storage(":memory:"), llm, BrokenSheets()).answer(-1, "A", "2026-10-28", "An", "hỏi"))
    assert out == "Báo cáo Q3 hạn 30/10"
    assert "Không đọc được Google Sheet: mất mạng" in llm.prompts[0]


def test_non_editor_has_no_edit_tool():
    s, ws = Storage(":memory:"), FakeWorksheet()
    llm = edit_llm()
    qa = QA(s, llm, FakeSheets(ws))
    _, proposals = asyncio.run(qa.answer(-1, "A", "2026-10-28", "An", "dời hạn", can_edit=False))
    assert "propose_task_edit" not in [t.name for t in llm.calls[0]["tools"]]
    assert "KHÔNG có quyền" in llm.calls[0]["system"]
    assert proposals == []


def test_bad_date_argument_returns_error_so_model_can_fix_it():
    s = Storage(":memory:")
    s.add_message(-1, 5, 1, "An", "họp thứ 3 nhé", None, datetime(2026, 10, 27, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")))
    seen = []

    class FixesDate(LLMProvider):
        async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
            seen.append(messages[-1])
            if len(seen) == 1:
                return Response("", (ToolCall("t1", "get_day", {"day": "27/10"}),))
            if len(seen) == 2:
                return Response("", (ToolCall("t2", "get_day", {"day": "2026-10-27"}),))
            return Response(json.loads(messages[-1].tool_results[0].content)["recent_messages"][0])

    out, _ = asyncio.run(QA(s, FixesDate("m"), FakeSheets(FakeWorksheet())).answer(
        -1, "A", "2026-11-06", "An", "thứ 3 tuần trước nói gì?"))
    error = json.loads(seen[1].tool_results[0].content)["error"]
    assert "YYYY-MM-DD" in error and "27/10" in error  # model được báo lỗi rõ ràng
    assert "họp thứ 3 nhé" in out  # gọi lại với ngày đúng thì có dữ liệu


def test_system_prompt_has_past_rules_and_past_weeks():
    llm = ScriptedLLM([])
    asyncio.run(QA(Storage(":memory:"), llm, FakeSheets(FakeWorksheet())).answer(-1, "A", "2026-11-06", "An", "hỏi"))
    system = llm.calls[0]["system"]
    assert "Hôm nay là Thứ 6, 06/11/2026 (2026-11-06)" in system
    assert '"thứ N tuần trước"' in system and "- Tuần trước: Thứ 2 2026-10-26" in system


def october_storage():
    """Chat -1 chỉ có tin từ 29/10 (giống chat POM): 29/10 đã xử lý, 30/10 còn 1 tin chưa xử lý."""
    from bot.storage import DaySummary

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    s = Storage(":memory:")
    s.add_message(-1, 1, 1, "An", "Lan làm slide nhé", None, datetime(2026, 10, 29, 9, tzinfo=tz))
    s.add_message(-1, 2, 2, "Lan", "ok anh", None, datetime(2026, 10, 29, 10, tzinfo=tz))
    s.save_summary(DaySummary(-1, "2026-10-29", "An giao Lan làm slide.", 2, True))
    s.add_message(-1, 3, 1, "An", "mai họp 9h", None, datetime(2026, 10, 30, 9, tzinfo=tz))
    s.add_message(-1, 4, 1, "An", "tin tháng 11", None, datetime(2026, 11, 2, 9, tzinfo=tz))
    return s


def test_storage_days_overview_and_data_range():
    s = october_storage()
    assert s.message_day_range(-1) == ("2026-10-29", "2026-11-02")
    assert s.message_day_range(-9) is None
    assert s.days_overview(-1, "2026-10-01", "2026-10-31") == [
        {"day": "2026-10-29", "summary": "An giao Lan làm slide.", "messages": 2, "unprocessed": 0},
        {"day": "2026-10-30", "summary": "", "messages": 1, "unprocessed": 1},
    ]


def test_get_period_answers_a_month_question_with_one_tool_call():
    s = october_storage()
    seen = []

    class MonthQuestion(LLMProvider):
        async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
            seen.append(messages[-1])
            if len(seen) == 1:
                return Response("", (ToolCall("t1", "get_period", {"from_day": "2026-10-01", "to_day": "2026-10-31"}),))
            period = json.loads(messages[-1].tool_results[0].content)
            return Response(f"{len(period['days'])} ngày, từ {period['coverage']['data_from']}")

    out, _ = asyncio.run(QA(s, MonthQuestion("m"), FakeSheets(FakeWorksheet())).answer(
        -1, "A", "2026-11-06", "An", "Tóm tắt cuộc trò chuyện trong tháng 10"))
    assert out == "2 ngày, từ 2026-10-29"  # chỉ ngày có tin, kèm phạm vi dữ liệu
    assert len(seen) == 2  # 2 lượt LLM, 1 lời gọi công cụ cho cả tháng
    # Phần nạp sẵn đã có phạm vi dữ liệu, để model không tra ngày bot không có dữ liệu
    assert '"data_from": "2026-10-29"' in seen[0].text


def test_coverage_uses_first_sync_start_not_first_message():
    """Theo dõi từ 05/11, đồng bộ lần đầu lấy từ 29/10 nhưng 29–31/10 không ai nhắn: bot vẫn có đủ dữ liệu
    từ 29/10 (ngày không có tin là ngày không ai nhắn), không phải từ ngày có tin đầu tiên."""
    s = Storage(":memory:")
    s.add_message(-1, 1, 1, "An", "tin đầu tiên", None, datetime(2026, 11, 1, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")))
    assert s.data_coverage(-1) == (None, "2026-11-01")  # chat cũ chưa có mốc: lấy ngày có tin đầu tiên
    s.track_chat(-1, "Team", today="2026-11-05")
    s.track_chat(-1, "Team", today="2026-11-20")  # theo dõi lại: giữ ngày bắt đầu theo dõi lần đầu
    s.set_data_from(-1, "2026-10-29")
    s.set_data_from(-1, "2026-11-10")  # đã có mốc: giữ nguyên
    assert s.data_coverage(-1) == ("2026-11-05", "2026-10-29")
    coverage = QA(s, None, None)._coverage(-1)
    assert coverage["tracked_since"] == "2026-11-05" and coverage["data_from"] == "2026-10-29"
    assert "ngày không có tin là ngày không ai nhắn" in coverage["note"]
    assert QA(Storage(":memory:"), None, None)._coverage(-9)["data_from"] is None


def test_get_period_rejects_reversed_or_too_long_ranges():
    qa = QA(october_storage(), None, None)
    for args, expected in [
        ({"from_day": "2026-10-31", "to_day": "2026-10-01"}, "phải trước hoặc bằng"),
        ({"from_day": "2026-01-01", "to_day": "2026-12-31"}, "tối đa 91 ngày"),
        ({"from_day": "1/10", "to_day": "2026-10-31"}, "YYYY-MM-DD"),
    ]:
        try:
            asyncio.run(qa._run_tool("get_period", args, -1, "2026-11-06", None))
        except ValueError as e:
            assert expected in str(e)
        else:
            raise AssertionError(f"không báo lỗi: {args}")

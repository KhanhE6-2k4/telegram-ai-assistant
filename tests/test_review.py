import asyncio

from bot.providers import LLMProvider, Response, ToolCall
from bot.review import find_issues, review_sheet
from bot.sheets import HEADER, TaskSheet
from bot.storage import DaySummary, Storage
from tests.test_sheets import FakeWorksheet

TODAY = "2026-10-30"


class ScriptedLLM(LLMProvider):
    """Lượt đầu gọi các công cụ trong `calls`; lượt sau trả lời "Xong". Ghi lại kết quả công cụ."""

    def __init__(self, calls=()):
        super().__init__("fake")
        self.script = list(calls)
        self.prompts = []
        self.results = []

    async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
        if len(messages) == 1:
            self.prompts.append(messages[0].text)
            return Response("", tuple(ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(self.script)))
        self.results += [r.content for r in messages[-1].tool_results]
        return Response("Xong")


def sheet_with(rows):
    s, ws = Storage(":memory:"), FakeWorksheet()
    ws.rows = [list(HEADER)] + [list(r) for r in rows]
    for r in rows:
        if r[0].isdigit():
            s.record_task(-1, int(r[0]), "2026-10-29", created=True)
    return s, ws


def run(s, ws, llm):
    return asyncio.run(review_sheet(s, TaskSheet(ws, s, -1), llm, -1, TODAY))


ROWS = [
    ["1", "Báo cáo Q3", "Lan", "Lan làm báo cáo Q3", "Mới giao", "2026-11-02"],
    ["2", "Lỗi đăng nhập", "", "Minh kiểm tra lỗi đăng nhập", "50%", "mai"],
    ["3", "Họp nhanh cả team", "", "Cả team họp lúc 3h", "Mới giao", "2026-10-29"],
    ["4", "Báo cáo doanh thu Q3", "Lan", "Lan làm báo cáo doanh thu Q3", "", ""],
]


def test_find_issues():
    s, ws = sheet_with(ROWS)
    issues = find_issues(TaskSheet(ws, s, -1).rows())
    assert issues == {"thiếu người thực hiện": [2, 3], "deadline chưa ở dạng YYYY-MM-DD": [2]}


def test_review_fills_marks_and_dedups():
    s, ws = sheet_with(ROWS)
    s.save_summary(DaySummary(-1, "2026-10-29", "Anh A giao Minh kiểm tra lỗi đăng nhập", 0, False))
    llm = ScriptedLLM([
        ("update_task", {"stt": 2, "changes": {"assignee": "Minh", "deadline": "2026-10-30"}}),
        ("mark_row", {"stt": 3, "reason": "not_task"}),
        ("mark_row", {"stt": 4, "reason": "duplicate", "duplicate_of": 1}),
    ])
    assert run(s, ws, llm) is True
    assert "Minh kiểm tra lỗi đăng nhập" in llm.prompts[0]  # có tóm tắt ngày giao làm ngữ cảnh
    assert ws.rows[2] == ["2", "Lỗi đăng nhập", "Minh", "Minh kiểm tra lỗi đăng nhập", "50%", "2026-10-30"]
    # Dòng không phải task: người thực hiện "Tất cả", tiến độ trống, giữ nguyên tên, nội dung, deadline
    assert ws.rows[3] == ["3", "Họp nhanh cả team", "Tất cả", "Cả team họp lúc 3h", "", "2026-10-29"]
    assert ws.rows[4] == ["4", "Báo cáo doanh thu Q3", "", "Trùng với task #1", "", ""]


def test_unchanged_sheet_is_not_reviewed_again():
    s, ws = sheet_with(ROWS)
    run(s, ws, ScriptedLLM([("mark_row", {"stt": 3, "reason": "not_task"})]))
    again = ScriptedLLM()
    assert run(s, ws, again) is False
    assert again.prompts == []  # không gọi LLM
    ws.rows[1][4] = "Đang làm"  # người dùng sửa sheet -> lần sau rà lại
    assert run(s, ws, ScriptedLLM()) is True


def test_guards_against_wrong_marks():
    s, ws = sheet_with(ROWS)
    llm = ScriptedLLM([
        ("mark_row", {"stt": 1, "reason": "not_task"}),                     # có người thực hiện -> từ chối
        ("mark_row", {"stt": 4, "reason": "duplicate", "duplicate_of": 4}),  # trùng chính nó -> từ chối
        ("mark_row", {"stt": 9, "reason": "not_task"}),                     # không có STT 9
    ])
    run(s, ws, llm)
    assert all('"error"' in r for r in llm.results)
    assert [r[3] for r in ws.rows[1:]] == [r[3] for r in ROWS]  # không dòng nào bị đổi


def test_rows_without_stt_get_numbers_and_empty_rows_are_skipped():
    s, ws = sheet_with([ROWS[0], ["", "Việc nhập tay", "Hà", "", "", ""], ["", "", "", "", "", ""]])
    run(s, ws, ScriptedLLM())
    assert [r[0] for r in ws.rows[1:]] == ["1", 2, ""]
    assert s.task_days(-1)[2] == (TODAY, TODAY)


def test_marked_rows_are_not_listed_as_missing_assignee_in_task_prompt():
    from bot.summarizer import _task_prompt

    s, ws = sheet_with([ROWS[0], ["3", "Họp nhanh cả team", "Tất cả", "Cả team họp lúc 3h", "", ""], ROWS[1]])
    tasks = TaskSheet(ws, s, -1).rows()
    prompt = _task_prompt(tasks, DaySummary(-1, TODAY, "", 0, False), [])
    assert "Task chưa có người thực hiện: STT 2" in prompt
    # Dòng "Tất cả" không bị coi là thiếu người thực hiện khi rà soát
    assert find_issues(tasks)["thiếu người thực hiện"] == [2]


def test_add_task_with_general_assignee_is_rejected():
    from bot.summarizer import run_task_tool

    s, ws = sheet_with([])
    out = asyncio.run(run_task_tool(TaskSheet(ws, s, -1), "add_task",
                                    {"name": "Họp", "assignee": "tất cả", "description": "", "progress": "", "deadline": ""},
                                    TODAY))
    assert "error" in out and len(ws.rows) == 1

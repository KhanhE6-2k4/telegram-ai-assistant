import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.providers import LLMProvider, Response, ToolCall
from bot.sheets import TaskSheet
from bot.storage import Storage
from bot.summarizer import KEEP_RECENT_RAW, pending_chars, process_day
from tests.test_sheets import DEADLINE, FakeWorksheet, task

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DAY = "2026-10-28"


class FakeClaude(LLMProvider):
    """Giả lập LLM.

    Pha A (ghi task): `script(prompt)` trả về danh sách (tên công cụ, input) gọi ở vòng đầu, rồi kết thúc.
    Pha B (tóm tắt, có tool_choice): trả "tóm tắt lần N"; lỗi `fail_summary` lần đầu.
    """

    def __init__(self, script=lambda prompt: [], fail_summary=0, on_summary=None):
        super().__init__("fake")
        self.script = script
        self.fail_summary = fail_summary
        self.on_summary = on_summary
        self.calls = []
        self.summaries = 0

    async def complete(self, *, system, messages, tools=(), tool_choice=None, max_tokens=4000):
        # chụp lại messages, vì vòng công cụ còn nối thêm vào list gốc
        self.calls.append({"system": system, "messages": list(messages), "tools": list(tools), "tool_choice": tool_choice})
        if tool_choice:
            if self.fail_summary:
                self.fail_summary -= 1
                raise RuntimeError("API lỗi")
            if self.on_summary:
                self.on_summary()
            self.summaries += 1
            return Response("", (ToolCall("s", "record_summary", {"summary": f"tóm tắt lần {self.summaries}"}),))
        todo = self.script(messages[0].text) if len(messages) == 1 else []
        return Response("" if todo else "Xong", tuple(ToolCall(f"t{i}", n, a) for i, (n, a) in enumerate(todo)))

    def task_prompts(self):
        return [c["messages"][0].text for c in self.calls if not c["tool_choice"] and len(c["messages"]) == 1]


def add(storage, n, day=DAY, start=1):
    for i in range(start, start + n):
        storage.add_message(-100, i, 1, "An", f"tin nhắn {i}", None,
                            datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=TZ))


def run(s, ws, claude, day=DAY, finalize=False, keep_recent=True):
    return asyncio.run(process_day(s, TaskSheet(ws, s, -100), claude, -100, day, finalize, keep_recent))


def test_tasks_written_to_sheet_before_summary():
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, 3)
    rows_at_summary = []
    claude = FakeClaude(
        script=lambda p: [("add_task", task("Báo cáo", deadline="2026-10-30", assignee="Lan"))],
        on_summary=lambda: rows_at_summary.append(len(ws.rows)),
    )
    result = run(s, ws, claude, finalize=True)
    assert rows_at_summary == [2]  # header + task đã có trên sheet khi bắt đầu tóm tắt
    assert ws.rows[1] == [1, "Báo cáo", "Lan", "d", "Mới giao", "2026-10-30"]
    assert result.summary == "tóm tắt lần 1" and result.finalized
    assert s.pending_messages(-100, DAY, result.compacted_until) == []


def test_update_task_from_chat_on_later_day_hits_same_row():
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, 2, day="2026-10-27")
    run(s, ws, FakeClaude(script=lambda p: [("add_task", task("Báo cáo", assignee="Lan"))]), day="2026-10-27", finalize=True)
    add(s, 2, start=10)
    claude = FakeClaude(script=lambda p: [("update_task", {"stt": 1, "changes": {"deadline": "2026-11-10"}})])
    run(s, ws, claude, finalize=True)
    assert '"stt": 1' in claude.task_prompts()[0]  # Claude thấy task cũ trên sheet
    assert len(ws.rows) == 2 and ws.rows[1][DEADLINE] == "2026-11-10"


def test_failure_keeps_messages_pending_and_retry_does_not_duplicate():
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, 3)
    # Claude thông minh: task đã có trên sheet thì không thêm nữa
    script = lambda p: [] if '"stt": 1' in p else [("add_task", task("Báo cáo", assignee="Lan"))]
    claude = FakeClaude(script=script, fail_summary=1)
    with pytest.raises(RuntimeError):
        run(s, ws, claude, finalize=True)
    assert s.get_summary(-100, DAY).compacted_until == 0
    assert len(s.pending_messages(-100, DAY, 0)) == 3

    result = run(s, ws, claude, finalize=True)  # lần thử lại
    assert result.finalized
    assert len(ws.rows) == 2


def test_midday_compaction_keeps_recent_raw():
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, KEEP_RECENT_RAW + 10)
    claude = FakeClaude()
    result = run(s, ws, claude)
    assert not result.finalized
    prompt = claude.task_prompts()[0]
    assert "tin nhắn 10" in prompt and "tin nhắn 11" not in prompt
    assert len(s.pending_messages(-100, DAY, result.compacted_until)) == KEEP_RECENT_RAW


def test_task_without_assignee_is_not_recorded():
    """Lịch họp / thông báo chung (không có người thực hiện cụ thể) không được ghi lên sheet."""
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, 2)
    claude = FakeClaude(script=lambda p: [
        ("add_task", task("Họp nhanh cả team", assignee="")),
        ("add_task", task("Báo cáo", assignee="Lan")),
    ])
    run(s, ws, claude, finalize=True)
    assert [r[1] for r in ws.rows[1:]] == ["Báo cáo"]
    # Model được báo lý do để biết
    results = [c["messages"][-1].tool_results for c in claude.calls if len(c["messages"]) == 3][0]
    assert "người thực hiện" in results[0].content


def test_reused_stt_gets_new_created_day():
    """Người dùng xoá dòng cuối trên sheet rồi bot thêm task mới cùng STT: ngày giao phải là ngày mới."""
    from bot.sheets import TaskSheet as TS

    s, ws = Storage(":memory:"), FakeWorksheet()
    TS(ws, s, -1).add(task("Cũ", assignee="A"), "2026-10-20")
    del ws.rows[1]  # xoá dòng trên sheet
    TS(ws, s, -1).add(task("Mới", assignee="B"), "2026-10-29")
    assert s.task_days(-1)[1] == ("2026-10-29", "2026-10-29")


def test_summary_command_processes_all_messages_without_closing_day():
    """/tomtat: ít tin nhắn (dưới KEEP_RECENT_RAW) vẫn được xử lý hết, task lên sheet ngay."""
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, 3)
    claude = FakeClaude(script=lambda p: [("add_task", task("Báo cáo", assignee="Lan"))])
    result = run(s, ws, claude, keep_recent=False)
    assert "tin nhắn 3" in claude.task_prompts()[0]
    assert ws.rows[1][1] == "Báo cáo"
    assert not result.finalized  # ngày vẫn mở, 00:01 hôm sau mới chốt
    assert s.pending_messages(-100, DAY, result.compacted_until) == []

    add(s, 1, start=4)  # tin mới sau /tomtat vẫn được xử lý ở lần sau
    assert len(s.pending_messages(-100, DAY, result.compacted_until)) == 1


def test_edited_message_is_reprocessed_and_reopens_finalized_day():
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, 3)
    run(s, ws, FakeClaude(), finalize=True)
    assert s.unfinalized_days_before("2026-10-29") == []

    s.edit_message(-100, 2, 1, "An", "hạn đổi sang thứ 6", None, datetime(2026, 10, 28, 9, tzinfo=TZ))
    assert s.unfinalized_days_before("2026-10-29") == [(-100, DAY)]
    assert pending_chars(s, -100, DAY) == len("hạn đổi sang thứ 6")

    claude = FakeClaude()
    result = run(s, ws, claude, finalize=True)
    assert "(đã sửa): hạn đổi sang thứ 6" in claude.task_prompts()[0]
    assert result.finalized
    assert s.pending_messages(-100, DAY, result.compacted_until) == []


def test_finalize_with_nothing_pending_only_marks_day():
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, 3)
    run(s, ws, FakeClaude())  # giữa ngày: 3 tin đều nằm trong phần giữ nguyên -> không làm gì
    assert s.get_summary(-100, DAY).compacted_until == 0
    run(s, ws, FakeClaude(), finalize=True)
    claude = FakeClaude()
    assert run(s, ws, claude, finalize=True) is None
    assert claude.calls == []


def test_late_message_reopens_finalized_day():
    """Tin của ngày đã chốt về muộn (Telegram gửi bù lúc bot chạy lại) mở lại ngày đó để chốt lại."""
    s, ws = Storage(":memory:"), FakeWorksheet()
    add(s, 2, day="2026-10-28")
    run(s, ws, FakeClaude(), day="2026-10-28", finalize=True)
    assert s.unfinalized_days_before("2026-10-29") == []
    add(s, 1, day="2026-10-28", start=50)  # tin 23:xx hôm qua về muộn
    assert s.unfinalized_days_before("2026-10-29") == [(-100, "2026-10-28")]
    add(s, 1, day="2026-10-28", start=50)  # tin trùng (đã có) không mở lại lần nữa
    run(s, ws, FakeClaude(), day="2026-10-28", finalize=True)
    assert s.unfinalized_days_before("2026-10-29") == []


def test_unfinalized_days_before():
    s = Storage(":memory:")
    add(s, 2, day="2026-10-26")
    add(s, 2, day="2026-10-27", start=10)
    add(s, 2, day="2026-10-28", start=20)
    assert s.unfinalized_days_before("2026-10-28") == [(-100, "2026-10-26"), (-100, "2026-10-27")]


def test_migrates_old_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, user_id INTEGER, user_name TEXT, text TEXT NOT NULL,
            reply_to INTEGER, sent_at TEXT NOT NULL, day TEXT NOT NULL, UNIQUE (chat_id, message_id));
        CREATE TABLE sheet_tasks (chat_id INTEGER NOT NULL, day TEXT NOT NULL, task_name TEXT NOT NULL,
            stt INTEGER NOT NULL, last_values TEXT NOT NULL, PRIMARY KEY (chat_id, day, task_name));
        INSERT INTO sheet_tasks VALUES (-100, '2026-10-27', 'a', 1, '[]'), (-100, '2026-10-28', 'a', 1, '[]');
    """)
    conn.commit()
    conn.close()
    s = Storage(str(path))
    add(s, 1)
    assert s.pending_messages(-100, DAY, 0)[0].edit_pending == 0
    assert s.task_days(-100) == {1: ("2026-10-27", "2026-10-28")}

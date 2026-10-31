import re

import pytest

from bot.sheets import HEADER, TaskSheet, apply_edit
from bot.storage import Storage

# Vị trí cột (đánh số từ 0) trong một dòng của sheet
ASSIGNEE = HEADER.index("Người thực hiện")
PROGRESS = HEADER.index("Tiến độ")
DEADLINE = HEADER.index("Deadline dự kiến")


class FakeWorksheet:
    def __init__(self):
        self.rows = []
        self.reads = 0

    def get_all_values(self):
        self.reads += 1
        return [list(map(str, r)) for r in self.rows]

    def row_values(self, row):
        return list(map(str, self.rows[row - 1])) if len(self.rows) >= row else []

    def append_row(self, values):
        self.rows.append(list(values))

    def update(self, range_name, values):
        # Chỉ hỗ trợ một ô, ví dụ "E2"
        col, row = re.fullmatch(r"([A-Z])(\d+)", range_name).groups()
        r, c = int(row) - 1, ord(col) - ord("A")
        self.rows[r] += [""] * (c + 1 - len(self.rows[r]))
        self.rows[r][c] = values[0][0]

    def insert_cols(self, values, col):
        # values: danh sách cột, mỗi cột là danh sách ô từ dòng 1
        for r, row in enumerate(self.rows):
            for k, column in enumerate(values):
                row.insert(col - 1 + k, column[r] if r < len(column) else "")


def task(name, desc="d", progress="Mới giao", deadline="", assignee=""):
    return {"name": name, "assignee": assignee, "description": desc, "progress": progress, "deadline": deadline}


def test_creates_header_and_appends_with_stt():
    s, ws = Storage(":memory:"), FakeWorksheet()
    sheet = TaskSheet(ws, s, -1)
    assert sheet.add(task("A", assignee="Lan"), "2026-10-28") == 1
    assert sheet.add(task("B"), "2026-10-28") == 2
    assert ws.rows[0] == HEADER
    assert ws.rows[1] == [1, "A", "Lan", "d", "Mới giao", ""]
    assert [r[:2] for r in ws.rows[1:]] == [[1, "A"], [2, "B"]]
    assert ws.reads == 1  # chỉ đọc sheet một lần


def test_update_from_later_day_writes_same_row():
    s, ws = Storage(":memory:"), FakeWorksheet()
    TaskSheet(ws, s, -1).add(task("Báo cáo", deadline="2026-10-30", assignee="Lan"), "2026-10-27")
    # Hôm sau: đổi hạn và người làm của task cũ -> sửa đúng dòng STT 1, không thêm dòng
    sheet = TaskSheet(ws, s, -1)
    sheet.update(1, {"deadline": "2026-11-02", "assignee": "Minh"}, "2026-10-28")
    assert len(ws.rows) == 2
    assert ws.rows[1] == [1, "Báo cáo", "Minh", "d", "Mới giao", "2026-11-02"]
    row = sheet.rows()[0]
    assert row["assignee"] == "Minh"
    assert (row["created_day"], row["updated_day"]) == ("2026-10-27", "2026-10-28")


def test_update_only_touches_changed_cells():
    s, ws = Storage(":memory:"), FakeWorksheet()
    sheet = TaskSheet(ws, s, -1)
    sheet.add(task("A", progress="Mới giao"), "2026-10-28")
    ws.rows[1][PROGRESS] = "Xong 50%"  # người dùng sửa tay sau khi bot đã đọc sheet
    sheet.update(1, {"deadline": "2026-11-01"}, "2026-10-28")
    assert ws.rows[1][PROGRESS] == "Xong 50%"
    assert ws.rows[1][DEADLINE] == "2026-11-01"


def test_new_stt_follows_rows_added_by_hand():
    s, ws = Storage(":memory:"), FakeWorksheet()
    ws.rows = [HEADER, ["7", "Tay", "", "", "", ""]]
    assert TaskSheet(ws, s, -1).add(task("A"), "2026-10-28") == 8


def test_apply_edit_unknown_stt_raises():
    s, ws = Storage(":memory:"), FakeWorksheet()
    TaskSheet(ws, s, -1)
    with pytest.raises(ValueError):
        apply_edit(s, ws, -1, 5, {"deadline": "x"}, "2026-10-28")

import itertools

from bot.sheets import CHAT_ID_KEY, HEADER, OLD_HEADER, TAB_TITLE_KEY, SheetClient
from tests.test_sheets import FakeWorksheet


class FakeSpreadsheet:
    """Mô phỏng phần Sheets API mà SheetClient dùng: metadata của tab, batchUpdate, thêm tab."""

    def __init__(self):
        self.tabs = {}  # sheetId -> {"title", "metadata": [..], "ws"}
        self.ids = itertools.count(100)
        self.metadata_ids = itertools.count(1)
        self.reads = 0

    def add_tab(self, title, ws=None):
        sheet_id = next(self.ids)
        ws = ws or FakeWorksheet()
        ws.id = sheet_id
        self.tabs[sheet_id] = {"title": title, "metadata": [], "ws": ws}
        return ws

    def title_of(self, ws):
        return self.tabs[ws.id]["title"]

    # ---- API ----

    def fetch_sheet_metadata(self, params=None):
        self.reads += 1
        return {"sheets": [
            {"properties": {"sheetId": i, "title": t["title"]}, "developerMetadata": [dict(m) for m in t["metadata"]]}
            for i, t in self.tabs.items()
        ]}

    def get_worksheet_by_id(self, sheet_id):
        return self.tabs[sheet_id]["ws"]

    def add_worksheet(self, title, rows, cols):
        assert title.lower() not in {t["title"].lower() for t in self.tabs.values()}, "trùng tên tab"
        return self.add_tab(title)

    def batch_update(self, body):
        for req in body["requests"]:
            if "updateSheetProperties" in req:
                p = req["updateSheetProperties"]["properties"]
                self.tabs[p["sheetId"]]["title"] = p["title"]
            elif "createDeveloperMetadata" in req:
                m = req["createDeveloperMetadata"]["developerMetadata"]
                self.tabs[m["location"]["sheetId"]]["metadata"].append({
                    "metadataId": next(self.metadata_ids), "metadataKey": m["metadataKey"],
                    "metadataValue": m["metadataValue"],
                })
            elif "updateDeveloperMetadata" in req:
                r = req["updateDeveloperMetadata"]
                mid = r["dataFilters"][0]["developerMetadataLookup"]["metadataId"]
                for t in self.tabs.values():
                    for m in t["metadata"]:
                        if m["metadataId"] == mid:
                            m["metadataValue"] = r["developerMetadata"]["metadataValue"]
            else:
                raise AssertionError(f"request lạ: {req}")


def client(sp, titles, **kw):
    return SheetClient(sp, title_of=titles.get, cache_seconds=0, **kw)


def test_new_group_gets_tab_named_after_group():
    sp, titles = FakeSpreadsheet(), {-1: "Team Marketing"}
    ws = client(sp, titles).worksheet_for(-1)
    assert sp.title_of(ws) == "Team Marketing"
    assert ws.rows == [HEADER]
    keys = {m["metadataKey"]: m["metadataValue"] for m in sp.tabs[ws.id]["metadata"]}
    assert keys == {CHAT_ID_KEY: "-1", TAB_TITLE_KEY: "Team Marketing"}


def test_tab_found_by_chat_id_even_if_moved_or_db_lost():
    sp, titles = FakeSpreadsheet(), {-1: "Team A"}
    ws = client(sp, titles).worksheet_for(-1)
    sp.add_tab("Tab khác")
    # SheetClient mới (như khi khởi động lại / mất cache): vẫn ra đúng tab, không tạo tab mới
    assert client(sp, titles).worksheet_for(-1) is ws
    assert len(sp.tabs) == 2


def test_group_rename_renames_tab():
    sp, titles = FakeSpreadsheet(), {-1: "Team A"}
    c = client(sp, titles)
    ws = c.worksheet_for(-1)
    titles[-1] = "Team A - Q4"
    assert c.worksheet_for(-1) is ws
    assert sp.title_of(ws) == "Team A - Q4"
    titles[-1] = "Team A - 2027"  # đổi tên lần nữa: metadata tên tab được cập nhật đúng
    c.worksheet_for(-1)
    assert sp.title_of(ws) == "Team A - 2027"


def test_manual_tab_rename_is_kept():
    sp, titles = FakeSpreadsheet(), {-1: "Team A"}
    c = client(sp, titles)
    ws = c.worksheet_for(-1)
    sp.tabs[ws.id]["title"] = "Việc của team A"  # người dùng tự đổi tên tab trên sheet
    titles[-1] = "Team A mới"
    assert c.worksheet_for(-1) is ws
    assert sp.title_of(ws) == "Việc của team A"


def test_same_group_name_gets_chat_id_suffix():
    sp = FakeSpreadsheet()
    sp.add_tab("Sheet1")
    titles = {-1: "Team", -2: "team"}  # tên tab không phân biệt hoa thường
    c = client(sp, titles)
    a, b = c.worksheet_for(-1), c.worksheet_for(-2)
    assert sp.title_of(a) == "Team"
    assert sp.title_of(b) == "team (-2)"


def test_legacy_tab_named_by_chat_id_is_adopted_renamed_and_gets_assignee_column():
    sp = FakeSpreadsheet()
    old = FakeWorksheet()
    old.rows = [list(OLD_HEADER), ["1", "Báo cáo", "Lan viết báo cáo", "Mới giao", "2026-10-30"]]
    sp.add_tab("-1", old)
    ws = client(sp, {-1: "Team A"}).worksheet_for(-1)
    assert ws is old
    assert sp.title_of(ws) == "Team A"
    assert len(sp.tabs) == 1
    # Chèn cột C "Người thực hiện", dữ liệu cũ dịch sang phải, không mất ô nào
    assert ws.rows == [HEADER, ["1", "Báo cáo", "", "Lan viết báo cáo", "Mới giao", "2026-10-30"]]


def test_assignee_column_inserted_only_once():
    sp = FakeSpreadsheet()
    old = FakeWorksheet()
    old.rows = [list(OLD_HEADER)]
    sp.add_tab("-1", old)
    client(sp, {-1: "A"}).worksheet_for(-1)
    client(sp, {-1: "A"}).worksheet_for(-1)  # khởi động lại: tiêu đề đã mới, không chèn nữa
    assert old.rows == [HEADER]


def test_unknown_group_name_falls_back_to_chat_id():
    sp = FakeSpreadsheet()
    ws = client(sp, {}).worksheet_for(-5)
    assert sp.title_of(ws) == "-5"


def test_long_name_is_truncated_and_whitespace_normalized():
    sp = FakeSpreadsheet()
    ws = client(sp, {-1: "  Nhóm   " + "x" * 200}).worksheet_for(-1)
    assert sp.title_of(ws).startswith("Nhóm x") and len(sp.title_of(ws)) == 100


def test_cache_avoids_repeated_reads_and_forget_clears_it():
    sp = FakeSpreadsheet()
    c = SheetClient(sp, title_of={-1: "A"}.get, cache_seconds=300)
    c.worksheet_for(-1)
    reads = sp.reads
    c.worksheet_for(-1)
    assert sp.reads == reads
    c.forget(-1)
    c.worksheet_for(-1)
    assert sp.reads > reads

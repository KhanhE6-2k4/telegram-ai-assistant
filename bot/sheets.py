"""Google Sheet là nơi lưu task chính: mỗi nhóm chat một tab, tên tab là tên nhóm.

Tab được định danh bằng developer metadata `telegram_chat_id` gắn vào tab (lưu ngay trong file
sheet), không dựa vào tên tab: đổi tên, đổi vị trí tab hay mất database của bot vẫn tìm đúng.

Cột: STT | Tên task | Người thực hiện | Nội dung task | Tiến độ | Deadline dự kiến

Mỗi task được định danh bằng STT. Model đọc các dòng hiện có (kể cả những ô người dùng sửa tay),
rồi gọi công cụ để thêm dòng mới hoặc sửa đúng dòng theo STT. Khi sửa, chỉ ghi những ô được đổi,
nên các ô khác (kể cả ô sửa tay) giữ nguyên. Ngày giao / ngày cập nhật của task lưu trong SQLite.
"""
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .storage import Storage
from .timing import timed

log = logging.getLogger(__name__)

HEADER = ["STT", "Tên task", "Người thực hiện", "Nội dung task", "Tiến độ", "Deadline dự kiến"]
FIELDS = ["name", "assignee", "description", "progress", "deadline"]  # ứng với cột B..F
WIDTH = len(HEADER)
# Tiêu đề của phiên bản trước (chưa có cột Người thực hiện): tab cũ được chèn thêm cột C
OLD_HEADER = ["STT", "Tên task", "Nội dung task", "Tiến độ", "Deadline dự kiến"]
ASSIGNEE_COL = HEADER.index("Người thực hiện") + 1  # cột C (đánh số từ 1)


class Worksheet(Protocol):
    def get_all_values(self) -> list[list[str]]: ...
    def append_row(self, values: list) -> None: ...
    def update(self, range_name: str, values: list[list]) -> None: ...


# Dòng không phải task thật (lịch họp, thông báo chung): giữ lại trên sheet, Người thực hiện = "Tất cả",
# Nội dung giữ nguyên, Tiến độ để trống.
GENERAL_MARK = "Tất cả"
# Dòng trùng task khác: Nội dung = "Trùng với task #N", Người thực hiện và Tiến độ để trống.
DUPLICATE_PREFIX = "Trùng với task #"


def duplicate_mark(stt: int) -> str:
    return f"{DUPLICATE_PREFIX}{stt}"


def is_general(assignee: str) -> bool:
    return str(assignee or "").strip().lower() == GENERAL_MARK.lower()


def is_marked(task: dict) -> bool:
    """Dòng đã được đánh dấu không phải task (Người thực hiện = Tất cả) hoặc trùng task khác."""
    return is_general(task.get("assignee")) or str(task.get("description") or "").strip().startswith(DUPLICATE_PREFIX)


def _pad(row: list[str]) -> list[str]:
    return (list(row) + [""] * WIDTH)[:WIDTH]


def _column(field: str) -> str:
    return chr(ord("B") + FIELDS.index(field))


class TaskSheet:
    """Một tab task của một nhóm. Đọc sheet một lần lúc tạo rồi giữ bản sao trong bộ nhớ,
    để một lượt xử lý nhiều task không tốn nhiều lượt đọc Google API."""

    def __init__(self, ws: Worksheet, storage: Storage, chat_id: int):
        self.ws = ws
        self.storage = storage
        self.chat_id = chat_id
        self.label = storage.chat_label(chat_id)
        with timed(log, "đọc Google Sheet của chat %s (cả tab)", self.label):
            self._rows = [list(map(str, r)) for r in ws.get_all_values()]
        if not self._rows:
            ws.append_row(HEADER)
            self._rows = [list(HEADER)]

    def _index_of(self, stt: int) -> int | None:
        for i, row in enumerate(self._rows[1:], start=1):
            if row and row[0].strip() == str(stt):
                return i
        return None

    def rows(self) -> list[dict]:
        """Mọi task trên sheet: stt, name, assignee, description, progress, deadline, created_day, updated_day."""
        days = self.storage.task_days(self.chat_id)
        tasks = []
        for row in self._rows[1:]:
            row = _pad(row)
            if not row[0].strip().isdigit():
                continue
            stt = int(row[0])
            created, updated = days.get(stt, ("", ""))
            tasks.append({"stt": stt, **dict(zip(FIELDS, row[1:])), "created_day": created, "updated_day": updated})
        return tasks

    def get(self, stt: int) -> dict | None:
        i = self._index_of(stt)
        if i is None:
            return None
        return dict(zip(FIELDS, _pad(self._rows[i])[1:]))

    def add(self, task: dict, day: str) -> int:
        """Thêm task mới ở cuối sheet với STT kế tiếp. Trả về STT."""
        stt = max((t["stt"] for t in self.rows()), default=0) + 1
        values = [str(task.get(f) or "") for f in FIELDS]
        with timed(log, "ghi Google Sheet: thêm task #%d, chat %s", stt, self.label):
            self.ws.append_row([stt, *values])
        self._rows.append([str(stt), *values])
        self.storage.record_task(self.chat_id, stt, day, created=True)
        return stt

    def update(self, stt: int, changes: dict, day: str) -> dict:
        """Ghi các trường trong `changes` vào dòng có STT tương ứng. Trả về giá trị mới của dòng."""
        i = self._index_of(stt)
        if i is None:
            raise ValueError(f"Không có task STT {stt}")
        self._rows[i] = _pad(self._rows[i])
        fields = [f for f in changes if f in FIELDS]
        # Mỗi ô là một lần gọi Google API: sửa nhiều trường thì lâu tương ứng
        with timed(log, "ghi Google Sheet: sửa task #%d (%d ô), chat %s", stt, len(fields), self.label):
            for field in fields:
                value = changes[field]
                self.ws.update(range_name=f"{_column(field)}{i + 1}", values=[[str(value)]])
                self._rows[i][1 + FIELDS.index(field)] = str(value)
        self.storage.record_task(self.chat_id, stt, day)
        return self.get(stt)

    def assign_missing_stt(self, day: str) -> list[int]:
        """Dòng có nội dung nhưng chưa có STT (người dùng nhập tay): gán STT kế tiếp. Trả về các STT mới."""
        next_stt = max((t["stt"] for t in self.rows()), default=0) + 1
        assigned = []
        for i, row in enumerate(self._rows[1:], start=1):
            row = _pad(row)
            if row[0].strip().isdigit() or not any(cell.strip() for cell in row[1:]):
                continue
            self.ws.update(range_name=f"A{i + 1}", values=[[next_stt]])
            self._rows[i] = [str(next_stt), *row[1:]]
            self.storage.record_task(self.chat_id, next_stt, day, created=True)
            assigned.append(next_stt)
            next_stt += 1
        return assigned

    def mark_general(self, stt: int, day: str) -> dict:
        """Dòng không phải task thật: Người thực hiện = "Tất cả", Tiến độ để trống, Nội dung giữ nguyên."""
        return self.update(stt, {"assignee": GENERAL_MARK, "progress": ""}, day)

    def mark_duplicate(self, stt: int, keep_stt: int, day: str) -> dict:
        """Dòng trùng task khác: Nội dung = "Trùng với task #N", Người thực hiện và Tiến độ để trống."""
        return self.update(stt, {"assignee": "", "progress": "", "description": duplicate_mark(keep_stt)}, day)


def apply_edit(storage: Storage, ws: Worksheet, chat_id: int, stt: int, changes: dict, day: str) -> dict:
    """Ghi thay đổi đã được người dùng xác nhận. Trả về giá trị mới của dòng."""
    return TaskSheet(ws, storage, chat_id).update(stt, changes, day)


CHAT_ID_KEY = "telegram_chat_id"
# Tên tab bot đặt lần gần nhất. Tên tab hiện tại khác giá trị này nghĩa là người dùng đã tự đổi tên
# trên sheet: khi đó bot giữ nguyên, không đổi lại theo tên nhóm.
TAB_TITLE_KEY = "telegram_tab_title"
MAX_TITLE_LEN = 100  # giới hạn độ dài tên tab của Google Sheets


class SheetClient:
    """Tìm hoặc tạo tab cho từng nhóm chat.

    - Tab nhận diện bằng developer metadata telegram_chat_id = <chat id>.
    - Tên tab = tên nhóm (trùng tên với tab khác thì thêm " (<chat id>)"). Nhóm đổi tên thì tab đổi
      theo, trừ khi người dùng đã tự đổi tên tab trên sheet.
    - Tab cũ đặt tên theo chat id (phiên bản trước) được nhận lại: gắn metadata và đổi sang tên nhóm.
    """

    def __init__(
        self,
        spreadsheet: Any,
        title_of: Callable[[int], str | None] = lambda chat_id: None,
        cache_seconds: float = 300,
        label_of: Callable[[int], str] = str,
    ):
        self.sheet = spreadsheet
        self.title_of = title_of
        # Nhãn chat cho log (tên + username + chat id), xem Storage.chat_label
        self.label_of = label_of
        self.cache_seconds = cache_seconds
        self._cache: dict[int, tuple[float, Any]] = {}
        # Các tab (sheetId) đã kiểm tra cấu trúc cột trong lần chạy này
        self._checked_columns: set[int] = set()
        # worksheet_for được gọi từ nhiều luồng (asyncio.to_thread): tránh tạo trùng tab / chèn trùng cột
        self._lock = threading.Lock()

    @classmethod
    def open(
        cls,
        credentials_file: str,
        spreadsheet_id: str,
        title_of: Callable[[int], str | None],
        label_of: Callable[[int], str] = str,
    ) -> "SheetClient":
        import gspread

        return cls(
            gspread.service_account(filename=credentials_file).open_by_key(spreadsheet_id), title_of,
            label_of=label_of,
        )

    def forget(self, chat_id: int) -> None:
        """Bỏ tab đã nhớ của nhóm (ví dụ sau khi ghi sheet lỗi), lần sau tra lại từ đầu."""
        with self._lock:
            self._cache.pop(chat_id, None)

    def worksheet_for(self, chat_id: int):
        with self._lock:
            hit = self._cache.get(chat_id)
            if hit and time.monotonic() - hit[0] < self.cache_seconds:
                return hit[1]
            # Chỉ khi chưa có trong bộ nhớ (hoặc quá cache_seconds): đọc danh sách tab của cả file sheet
            with timed(log, "tìm tab Google Sheet của chat %s", self.label_of(chat_id)):
                ws = self._resolve(chat_id)
                self._ensure_columns(ws)
            self._cache[chat_id] = (time.monotonic(), ws)
            return ws

    # ---- nội bộ ----

    def _ensure_columns(self, ws) -> None:
        """Tab tạo bởi phiên bản cũ (chưa có cột Người thực hiện): chèn cột C, dữ liệu dịch sang phải."""
        if ws.id in self._checked_columns:
            return
        if ws.row_values(1)[: len(OLD_HEADER)] == OLD_HEADER:
            ws.insert_cols([["Người thực hiện"]], col=ASSIGNEE_COL)
            log.info("Thêm cột Người thực hiện vào tab %r", getattr(ws, "title", ws.id))
        self._checked_columns.add(ws.id)

    def _tabs(self) -> list[dict]:
        meta = self.sheet.fetch_sheet_metadata(
            params={"fields": "sheets(properties(sheetId,title),developerMetadata)"}
        )
        return meta.get("sheets", [])

    @staticmethod
    def _metadata(tab: dict) -> dict[str, dict]:
        return {m["metadataKey"]: m for m in tab.get("developerMetadata", [])}

    def _wanted_title(self, chat_id: int, tabs: list[dict], own_sheet_id: int | None) -> str:
        """Tên nhóm, không trùng với tab khác (tên tab trên Google Sheets không phân biệt hoa thường)."""
        name = " ".join((self.title_of(chat_id) or "").split()) or str(chat_id)
        taken = {
            t["properties"]["title"].lower() for t in tabs if t["properties"]["sheetId"] != own_sheet_id
        }
        title = name[:MAX_TITLE_LEN]
        if title.lower() in taken:
            suffix = f" ({chat_id})"
            title = name[: MAX_TITLE_LEN - len(suffix)] + suffix
        return title

    def _resolve(self, chat_id: int):
        tabs = self._tabs()
        for tab in tabs:
            md = self._metadata(tab)
            if md.get(CHAT_ID_KEY, {}).get("metadataValue") == str(chat_id):
                self._sync_title(chat_id, tab, md, tabs)
                return self.sheet.get_worksheet_by_id(tab["properties"]["sheetId"])

        legacy = next((t for t in tabs if t["properties"]["title"] == str(chat_id)), None)
        if legacy is not None:
            sheet_id = legacy["properties"]["sheetId"]
            title = self._wanted_title(chat_id, tabs, sheet_id)
            requests = [self._tag(sheet_id, CHAT_ID_KEY, str(chat_id)), self._tag(sheet_id, TAB_TITLE_KEY, title)]
            if title != legacy["properties"]["title"]:
                requests.insert(0, self._rename(sheet_id, title))
            self.sheet.batch_update({"requests": requests})
            log.info("Nhận lại tab cũ của chat %s, đổi tên thành %r", self.label_of(chat_id), title)
            return self.sheet.get_worksheet_by_id(sheet_id)

        title = self._wanted_title(chat_id, tabs, None)
        ws = self.sheet.add_worksheet(title=title, rows=1000, cols=WIDTH)
        ws.append_row(HEADER)
        self._checked_columns.add(ws.id)
        self.sheet.batch_update({"requests": [
            self._tag(ws.id, CHAT_ID_KEY, str(chat_id)),
            self._tag(ws.id, TAB_TITLE_KEY, title),
        ]})
        log.info("Tạo tab %r cho chat %s", title, self.label_of(chat_id))
        return ws

    def _sync_title(self, chat_id: int, tab: dict, md: dict[str, dict], tabs: list[dict]) -> None:
        """Nhóm đổi tên thì đổi tên tab theo, trừ khi người dùng đã tự đổi tên tab."""
        sheet_id, current = tab["properties"]["sheetId"], tab["properties"]["title"]
        last_set = md.get(TAB_TITLE_KEY)
        if last_set is not None and last_set.get("metadataValue") != current:
            return  # người dùng đã tự đặt tên tab
        wanted = self._wanted_title(chat_id, tabs, sheet_id)
        if wanted == current:
            return
        update_title = (
            {"updateDeveloperMetadata": {
                "dataFilters": [{"developerMetadataLookup": {"metadataId": last_set["metadataId"]}}],
                "developerMetadata": {"metadataValue": wanted},
                "fields": "metadataValue",
            }}
            if last_set is not None
            else self._tag(sheet_id, TAB_TITLE_KEY, wanted)
        )
        self.sheet.batch_update({"requests": [self._rename(sheet_id, wanted), update_title]})
        log.info("Đổi tên tab của chat %s: %r -> %r", self.label_of(chat_id), current, wanted)

    @staticmethod
    def _rename(sheet_id: int, title: str) -> dict:
        return {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "title": title}, "fields": "title"}}

    @staticmethod
    def _tag(sheet_id: int, key: str, value: str) -> dict:
        return {"createDeveloperMetadata": {"developerMetadata": {
            "metadataKey": key,
            "metadataValue": value,
            "location": {"sheetId": sheet_id},
            "visibility": "DOCUMENT",
        }}}

"""Lịch và quy ước ngày cho prompt, để model quy đổi deadline tương đối ("đầu tuần sau", "cuối tháng")
bằng cách tra bảng do code tính sẵn, không tự nhẩm thứ (LLM hay nhẩm sai thứ của một ngày).

Tuần tính từ thứ Hai đến Chủ nhật.
"""
import calendar
from datetime import date, timedelta

WEEKDAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]
WEEK_NAMES = ["Tuần này", "Tuần sau", "Tuần sau nữa"]

# Quy ước cho các cụm thường gặp. "Ngày mốc" là ngày đặt câu (ngày gửi tin / ngày giao task).
DEADLINE_RULES = """Quy đổi deadline tương đối theo bảng lịch của ngày mốc (tuần tính từ thứ Hai đến Chủ nhật),
không tự tính thứ:
- "mai" = ngày mốc + 1; "ngày kia" = ngày mốc + 2.
- "thứ N" / "thứ N này" = thứ N của tuần này; "thứ N tuần sau" = thứ N của tuần sau.
- "đầu tuần sau" = thứ 2 tuần sau.
- "giữa tuần" = thứ 5 tuần này; "giữa tuần sau" = thứ 5 tuần sau.
- "cuối tuần" = Chủ nhật tuần này; "cuối tuần sau" = Chủ nhật tuần sau.
- "cuối tháng" = ngày cuối cùng của tháng này; "cuối tháng sau" = ngày cuối cùng của tháng sau.
- Cụm không chỉ được ngày nào (ví dụ "sớm", "khi nào xong") thì ghi nguyên văn."""


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def calendar_text(day: str) -> str:
    """Bảng lịch quanh ngày mốc `day` (YYYY-MM-DD): thứ của ngày mốc, ngày từng thứ của tuần này và
    2 tuần tới, ngày cuối tháng này và tháng sau."""
    d = date.fromisoformat(day)
    monday = d - timedelta(days=d.weekday())
    lines = [f"Ngày mốc: {WEEKDAYS[d.weekday()]}, {d.isoformat()}"]
    for i, name in enumerate(WEEK_NAMES):
        start = monday + timedelta(weeks=i)
        days = ", ".join(f"{WEEKDAYS[k]} {(start + timedelta(days=k)).isoformat()}" for k in range(7))
        lines.append(f"- {name}: {days}")
    next_month = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    lines.append(
        f"- Cuối tháng này: {_month_end(d.year, d.month).isoformat()}; "
        f"cuối tháng sau: {_month_end(next_month.year, next_month.month).isoformat()}"
    )
    return "\n".join(lines)

"""Lịch và quy ước ngày cho prompt, để model quy đổi ngày tương đối ("đầu tuần sau", "thứ 3 tuần trước",
"tháng trước") bằng cách tra bảng do code tính sẵn, không tự nhẩm thứ (LLM hay nhẩm sai thứ của một ngày).

- `calendar_text`: bảng lịch quanh ngày mốc (2 tuần trước, tuần này, 2 tuần sau, tháng trước / tháng này).
- `DEADLINE_RULES`: quy ước cho deadline (tương lai), dùng khi ghi task, rà soát sheet và hỏi đáp.
- `PAST_RULES`: quy ước cho cụm chỉ thời gian đã qua trong câu hỏi, dùng khi hỏi đáp.
- `day_label`: nhãn "Thứ 6, 06/11/2026" ghi ở đầu mỗi câu hỏi gửi model.
- `check_day`: kiểm tra tham số ngày model truyền vào công cụ (đúng dạng YYYY-MM-DD, là ngày có thật).

Bảng và quy tắc chỉ giúp model dễ tra đúng; code không kiểm tra được model có chọn đúng dòng trong bảng hay
không, chỉ chặn được ngày sai dạng / không có thật. Tuần tính từ thứ Hai đến Chủ nhật.
"""
import calendar
from datetime import date, timedelta

WEEKDAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]
# (độ lệch so với tuần này, tên dòng trong bảng lịch)
WEEKS = [(-2, "Tuần trước nữa"), (-1, "Tuần trước"), (0, "Tuần này"), (1, "Tuần sau"), (2, "Tuần sau nữa")]

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

PAST_RULES = """Quy đổi cụm chỉ thời gian đã qua trong câu hỏi theo bảng lịch của ngày mốc (ngày ghi ở đầu câu hỏi),
không tự tính thứ:
- "hôm qua" = ngày mốc - 1; "hôm kia" = ngày mốc - 2.
- "thứ N tuần trước" = thứ N của dòng "Tuần trước"; "thứ N tuần trước nữa" = thứ N của dòng "Tuần trước nữa".
- "tuần trước" = cả tuần trước (thứ 2 → Chủ nhật của dòng "Tuần trước"); "tuần này" = thứ 2 tuần này → ngày mốc.
- "tháng trước" = cả tháng trước (dòng "Tháng trước"); "tháng này" = ngày 1 của tháng này → ngày mốc.
- "đầu tháng trước" = ngày 1 → 10 của tháng trước; "cuối tháng trước" = ngày 21 → ngày cuối của tháng trước.
- "tháng N" không nói năm = tháng N gần nhất không sau ngày mốc (từ ngày 1 đến ngày cuối tháng đó).
- Khi gọi công cụ, ngày luôn ghi dạng YYYY-MM-DD."""


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def day_label(day: str) -> str:
    """'2026-11-06' -> 'Thứ 6, 06/11/2026' (ghi ở đầu câu hỏi gửi model)."""
    d = date.fromisoformat(day)
    return f"{WEEKDAYS[d.weekday()]}, {d:%d/%m/%Y}"


def check_day(value, name: str = "day") -> str:
    """Tham số ngày model truyền vào công cụ: phải là chuỗi YYYY-MM-DD của một ngày có thật.
    Sai thì ném ValueError với thông báo để model sửa (vd. "27/10", "tuần trước", "2026-02-30")."""
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"Tham số {name} phải là ngày dạng YYYY-MM-DD, nhận được {value!r}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"Tham số {name} không phải ngày có thật (dạng YYYY-MM-DD): {value!r}") from None


def calendar_text(day: str) -> str:
    """Bảng lịch quanh ngày mốc `day` (YYYY-MM-DD): thứ của ngày mốc; ngày từng thứ của 2 tuần trước,
    tuần này và 2 tuần sau; dải ngày tháng trước / tháng này; ngày cuối tháng này và tháng sau."""
    d = date.fromisoformat(day)
    monday = d - timedelta(days=d.weekday())
    lines = [f"Ngày mốc: {WEEKDAYS[d.weekday()]}, {d.isoformat()}"]
    for offset, name in WEEKS:
        start = monday + timedelta(weeks=offset)
        days = ", ".join(f"{WEEKDAYS[k]} {(start + timedelta(days=k)).isoformat()}" for k in range(7))
        lines.append(f"- {name}: {days}")
    this_month = d.replace(day=1)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    next_month = (this_month + timedelta(days=32)).replace(day=1)
    lines.append(
        f"- Tháng trước: {last_month.isoformat()} → {_month_end(last_month.year, last_month.month).isoformat()}; "
        f"tháng này: {this_month.isoformat()} → {_month_end(d.year, d.month).isoformat()}"
    )
    lines.append(
        f"- Cuối tháng này: {_month_end(d.year, d.month).isoformat()}; "
        f"cuối tháng sau: {_month_end(next_month.year, next_month.month).isoformat()}"
    )
    return "\n".join(lines)

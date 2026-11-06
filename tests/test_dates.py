from datetime import date

from bot.dates import WEEKDAYS, calendar_text


def week_line(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"- {name}:"))


def test_calendar_lists_weeks_from_monday_to_sunday():
    text = calendar_text("2026-10-30")  # thứ 6
    assert text.splitlines()[0] == "Ngày mốc: Thứ 6, 2026-10-30"
    assert week_line(text, "Tuần này") == (
        "- Tuần này: Thứ 2 2026-10-26, Thứ 3 2026-10-27, Thứ 4 2026-10-28, Thứ 5 2026-10-29, "
        "Thứ 6 2026-10-30, Thứ 7 2026-10-31, Chủ nhật 2026-11-01"
    )
    # Quy ước: đầu tuần sau = thứ 2, giữa tuần sau = thứ 5, cuối tuần sau = Chủ nhật của tuần sau
    next_week = week_line(text, "Tuần sau")
    assert "Thứ 2 2026-11-02" in next_week
    assert "Thứ 5 2026-11-05" in next_week
    assert next_week.endswith("Chủ nhật 2026-11-08")
    assert "Thứ 2 2026-11-09" in week_line(text, "Tuần sau nữa")
    assert "Cuối tháng này: 2026-10-31; cuối tháng sau: 2026-11-30" in text


def test_sunday_belongs_to_the_week_that_started_on_monday():
    text = calendar_text("2026-11-01")  # Chủ nhật
    assert text.splitlines()[0] == "Ngày mốc: Chủ nhật, 2026-11-01"
    assert week_line(text, "Tuần này").startswith("- Tuần này: Thứ 2 2026-10-26")
    assert week_line(text, "Tuần sau").startswith("- Tuần sau: Thứ 2 2026-11-02")


def test_month_ends_across_year_and_leap_february():
    assert "Cuối tháng này: 2026-12-31; cuối tháng sau: 2027-01-31" in calendar_text("2026-12-15")
    assert "Cuối tháng này: 2028-01-31; cuối tháng sau: 2028-02-29" in calendar_text("2028-01-31")
    # Mỗi ngày trong bảng mang đúng thứ của nó
    for line in calendar_text("2026-12-30").splitlines()[1:4]:
        for item in line.split(": ", 1)[1].split(", "):
            name, iso = item.rsplit(" ", 1)
            assert WEEKDAYS[date.fromisoformat(iso).weekday()] == name


def test_calendar_has_past_weeks_and_month_ranges():
    from bot.dates import calendar_text

    text = calendar_text("2026-11-06")  # thứ 6
    # "thứ 3 tuần trước" = 2026-10-27: tra được trong bảng, không phải tự tính
    assert "Thứ 3 2026-10-27" in week_line(text, "Tuần trước")
    assert week_line(text, "Tuần trước").startswith("- Tuần trước: Thứ 2 2026-10-26")
    assert week_line(text, "Tuần trước nữa").startswith("- Tuần trước nữa: Thứ 2 2026-10-19")
    assert "- Tháng trước: 2026-10-01 → 2026-10-31; tháng này: 2026-11-01 → 2026-11-30" in text
    # Tháng trước qua năm
    assert "Tháng trước: 2025-12-01 → 2025-12-31" in calendar_text("2026-01-05")


def test_day_label_and_check_day():
    import pytest

    from bot.dates import check_day, day_label

    assert day_label("2026-11-06") == "Thứ 6, 06/11/2026"
    assert day_label("2026-11-08") == "Chủ nhật, 08/11/2026"
    assert check_day("2026-10-27") == "2026-10-27"
    for bad in ("27/10", "tuần trước", "2026-02-30", "2026-1-5", None, 20261027):
        with pytest.raises(ValueError):
            check_day(bad)

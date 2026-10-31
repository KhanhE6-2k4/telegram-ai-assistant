"""Thang khoảng cách, màu và cỡ chữ của giao diện, theo phong cách Discord.

Màu tự theo chế độ sáng / tối của Windows (đọc lúc mở app). Toga dùng widget gốc của Windows: không
bo góc được, nhưng nền, chữ, màu nút và màu ô nhập đặt được.
"""
from dataclasses import dataclass

from bot.runner import Status

# Khoảng cách (px), bội số của 4
XS, S, M, L, XL = 4, 8, 12, 16, 24

# Cỡ chữ (pt)
PAGE_TITLE, TITLE, HEADING, BODY, SMALL = 18, 15, 12, 10, 8

# Thanh điều hướng bên trái
SIDEBAR_WIDTH = 220
# Nội dung căn giữa, rộng tối đa; vùng nội dung hẹp hơn NARROW thì các nút xếp dọc
MAX_CONTENT_WIDTH = 720
NARROW = 480


@dataclass(frozen=True)
class Theme:
    sidebar: str      # nền thanh bên trái
    background: str   # nền vùng nội dung
    surface: str      # nền thẻ
    input: str        # nền ô nhập
    selected: str     # mục đang chọn ở thanh bên trái
    heading: str      # tiêu đề
    text: str
    muted: str        # nhãn, chữ phụ
    accent: str       # nút chính (blurple)
    on_accent: str    # chữ trên nút màu
    success: str
    warning: str
    danger: str
    idle: str

    def status_color(self, status: Status) -> str:
        return {
            Status.RUNNING: self.success,
            Status.STARTING: self.warning,
            Status.WAITING_NETWORK: self.warning,
            Status.ERROR: self.danger,
            Status.STOPPED: self.idle,
        }[status]


DARK = Theme(
    sidebar="#2b2d31",
    # background="#313338",
    background="#3A4352",
    # surface="#2b2d31",
    surface="#1E293B",
    input="#1e1f22",
    # selected="#404249",
    selected = "#315B4A",
    heading="#f2f3f5",
    text="#dbdee1",
    muted="#b5bac1",
    accent="#5865f2",
    on_accent="#ffffff",
    success="#23a55a",
    warning="#f0b232",
    danger="#f23f43",
    idle="#80848e",
)
LIGHT = Theme(
    sidebar="#f2f3f5",
    background="#ffffff",
    # surface="#f2f3f5",
    surface="#F1F5F9",
    input="#e3e5e8",
    # selected="#d7d9dc",
    selected = "#D9F2E3",
    heading="#060607",
    text="#313338",
    muted="#4e5058",
    accent="#5865f2",
    on_accent="#ffffff",
    success="#1f8b4c",
    warning="#c27c0e",
    danger="#da373c",
    idle="#80848e",
)


def theme_for(dark_mode: bool | None) -> Theme:
    return DARK if dark_mode else LIGHT

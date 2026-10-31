"""Các khối bố cục dùng chung (phong cách Discord): trang cuộn có tiêu đề, thẻ, nhãn + ô nhập, nút.

Trang có `set_width(px)` để đổi bố cục khi cửa sổ đổi cỡ: vùng nội dung hẹp (dưới theme.NARROW) thì
các nút xếp dọc; nhãn dài tự xuống dòng theo bề rộng.
"""
import textwrap

import toga
from toga.style import Pack
from toga.style.pack import BOLD, COLUMN, ROW

from . import theme as t

# Bề rộng trung bình một ký tự (px) ở cỡ chữ BODY, để tự xuống dòng cho nhãn dài
CHAR_WIDTH = 7


# Với Toga, mỗi lần gán chữ là bố trí lại cả cửa sổ và WinForms vẽ lại (nháy), kể cả khi chữ không
# đổi; nên chỉ gán khi giá trị khác.
def set_text(widget: toga.Widget, value: str) -> None:
    if widget.text != value:
        widget.text = value


def set_input(widget: toga.TextInput, value: str | None = None, placeholder: str | None = None) -> None:
    """Đổi chữ / gợi ý của ô nhập mà không bố trí lại cửa sổ.

    Setter của Toga bố trí lại cả cửa sổ sau mỗi lần gán, nhưng cỡ ô nhập (WinForms: chỉ chiều cao theo
    font) không phụ thuộc nội dung nên việc đó thừa. Toga không có API công khai bỏ bước này, nên gán qua
    `_impl` (vẫn phát sự kiện đổi giá trị như setter).
    """
    if value is not None and widget.value != value:
        widget._impl.set_value(value)
    if placeholder is not None and widget.placeholder != placeholder:
        widget._impl.set_placeholder(placeholder)


def set_style(widget: toga.Widget, **props) -> None:
    """Chỉ cập nhật những thuộc tính style khác giá trị hiện tại."""
    style = widget.style
    changed = {}
    for name, value in props.items():
        # So với giá trị đã chuẩn hoá (vd. "#fff" → rgb), như cách style lưu
        if style[name] != getattr(type(style), name).validate(value):
            changed[name] = value
    if changed:
        style.update(**changed)


class WrappedLabel:
    """Label tự xuống dòng theo bề rộng (Label của Toga không tự xuống dòng)."""

    def __init__(self, text: str = "", style: Pack | None = None):
        self._text = text
        self._width = t.MAX_CONTENT_WIDTH
        self.widget = toga.Label("", style=style or Pack())
        self._render()

    @property
    def text(self) -> str:
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        self._text = value
        self._render()

    def set_width(self, width: int) -> None:
        self._width = width
        self._render()

    def _render(self) -> None:
        chars = max(20, self._width // CHAR_WIDTH)
        set_text(self.widget, "\n".join(
            textwrap.fill(line, chars) if line else "" for line in self._text.split("\n")
        ))


def button(theme: t.Theme, text: str, on_press, kind: str = "primary") -> toga.Button:
    """Nút kiểu Discord: primary (blurple), danger (đỏ), secondary (xám)."""
    background, color = {
        "primary": (theme.accent, theme.on_accent),
        "danger": (theme.danger, theme.on_accent),
        "secondary": (theme.selected, theme.heading),
    }[kind]
    return toga.Button(
        text, on_press=on_press, style=Pack(background_color=background, color=color, font_weight=BOLD)
    )


class FormRow:
    """Nhãn chữ in hoa nhỏ nằm trên ô nhập, như form của Discord."""

    def __init__(self, theme: t.Theme, label: str, widget: toga.Widget):
        self.label = toga.Label("", style=Pack(color=theme.muted, font_size=t.SMALL, font_weight=BOLD))
        self.set_label(label)
        self.widget = widget
        widget.style.update(background_color=theme.input, color=theme.text)
        self.box = toga.Box(children=[self.label, widget], style=Pack(direction=COLUMN, gap=t.XS))

    def set_label(self, text: str) -> None:
        set_text(self.label, text.upper())


class ButtonBar:
    def __init__(self, *buttons: toga.Button):
        self.box = toga.Box(children=list(buttons), style=Pack(direction=ROW, gap=t.S))

    def set_narrow(self, narrow: bool) -> None:
        set_style(self.box, direction=COLUMN if narrow else ROW)


class Card:
    """Khối nền riêng có lề trong và tiêu đề."""

    def __init__(self, theme: t.Theme, title: str | None = None):
        # Pack không có padding: lề trong = margin của hộp con
        self.inner = toga.Box(style=Pack(direction=COLUMN, margin=t.L, gap=t.M))
        self.box = toga.Box(children=[self.inner], style=Pack(direction=COLUMN, background_color=theme.surface))
        if title:
            self.inner.add(toga.Label(title, style=Pack(font_size=t.HEADING, font_weight=BOLD, color=theme.heading)))

    def add(self, *widgets: toga.Widget) -> None:
        self.inner.add(*widgets)


class Page:
    """Nội dung một mục ở thanh bên trái: tiêu đề lớn, rộng tối đa theme.MAX_CONTENT_WIDTH.

    Trang không tự cuộn: cửa sổ có một ScrollContainer dùng chung, chuyển trang = gán `page.box` làm
    nội dung của nó (xem app.select_page).
    """

    # Lề hai bên + thanh cuộn
    SIDE = 2 * t.XL + 24

    def __init__(self, theme: t.Theme, title: str):
        self.content = toga.Box(style=Pack(direction=COLUMN, gap=t.L, width=t.MAX_CONTENT_WIDTH))
        self.content.add(toga.Label(
            title, style=Pack(font_size=t.PAGE_TITLE, font_weight=BOLD, color=theme.heading, margin_bottom=t.XS)
        ))
        self.box = toga.Box(
            children=[self.content],
            style=Pack(direction=COLUMN, align_items="center", margin=t.XL, background_color=theme.background),
        )
        self.rows: list[ButtonBar] = []  # đổi bố cục khi hẹp
        self.labels: list[WrappedLabel] = []
        self.available: int | None = None  # bề rộng đã áp dụng lần cuối (set_width)

    def add(self, *items) -> None:
        for item in items:
            self.content.add(item.box if isinstance(item, Card) else item)

    def refresh(self) -> None:
        """Tính lại cỡ chữ và bố cục sau khi trang được gắn lại vào cửa sổ.

        Lúc trang bị gỡ khỏi cửa sổ, WinForms đo chữ và đặt kích thước theo tỉ lệ DPI 1 (không có cửa
        sổ); gắn lại thì chữ được phóng theo DPI thật nhưng không được đo lại, nên chữ bị cắt khi màn
        hình đặt tỉ lệ khác 100%.

        `Widget.refresh()` của Toga = đo lại riêng widget đó (`_impl.refresh()`) + bố trí lại từ gốc
        (ở đây là nội dung ScrollContainer); gọi trên từng widget thì trang N widget bị bố trí lại N lần
        (giật). Toga không có API công khai chỉ để đo lại, nên ở đây dùng `_impl.refresh()` cho từng
        widget rồi bố trí lại một lần.
        """

        def remeasure(widget: toga.Widget) -> None:
            for child in widget.children:
                remeasure(child)
            widget._impl.refresh()

        remeasure(self.content)
        self.content.refresh()

    def set_width(self, available: int) -> None:
        """`available`: bề rộng vùng nội dung (cửa sổ trừ thanh bên trái)."""
        self.available = available
        width = max(280, min(t.MAX_CONTENT_WIDTH, available - self.SIDE))
        set_style(self.content, width=width)
        narrow = width < t.NARROW
        for row in self.rows:
            row.set_narrow(narrow)
        for label in self.labels:
            label.set_width(width - 2 * t.L)  # trừ lề trong của thẻ

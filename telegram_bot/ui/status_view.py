"""Trang Trạng thái: chấm màu + trạng thái bot, nội dung lỗi, provider/model, nút Bật / Tắt."""
import toga
from toga.style import Pack
from toga.style.pack import BOLD, ROW

from bot.runner import Status

from . import theme as t
from .layout import ButtonBar, Card, Page, WrappedLabel, button, set_style, set_text


class StatusView:
    def __init__(self, theme: t.Theme, on_toggle):
        self.theme = theme
        self.page = Page(theme, "Trạng thái")

        card = Card(theme)
        self.dot = toga.Label("●", style=Pack(font_size=t.TITLE, color=theme.idle))
        self.status = toga.Label(
            Status.STOPPED.value, style=Pack(font_size=t.TITLE, font_weight=BOLD, color=theme.heading)
        )
        card.add(toga.Box(children=[self.dot, self.status], style=Pack(direction=ROW, gap=t.S)))
        self.detail = WrappedLabel(style=Pack(font_size=t.BODY, color=theme.danger))
        self.llm = toga.Label("", style=Pack(font_size=t.BODY, color=theme.muted))
        self.toggle = button(theme, "Bật bot", on_toggle)
        buttons = ButtonBar(self.toggle)
        card.add(self.detail.widget, self.llm, buttons.box)

        self.page.add(card)
        self.page.rows.append(buttons)
        self.page.labels.append(self.detail)

    def update(self, status: Status, detail: str, llm: str, running: bool) -> None:
        # Chỉ gán những gì đổi: mỗi lần gán là cả cửa sổ bố trí lại (xem layout.set_text)
        set_style(self.dot, color=self.theme.status_color(status))
        set_text(self.status, status.value)
        self.detail.text = detail if status is Status.ERROR else ""
        set_text(self.llm, f"LLM: {llm}" if llm else "")
        # Đang chạy: nút đỏ "Tắt bot"; đang tắt: nút blurple "Bật bot"
        set_text(self.toggle, "Tắt bot" if running else "Bật bot")
        set_style(self.toggle, background_color=self.theme.danger if running else self.theme.accent)

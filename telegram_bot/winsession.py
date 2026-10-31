"""Riêng Windows (pythonnet): bắt sự kiện tắt máy / đăng xuất.

Toga huỷ mọi yêu cầu đóng cửa sổ để gọi on_close; app này luôn trả False (ẩn xuống khay), nên nếu
không bắt sự kiện này thì app có thể chặn Windows tắt máy. Khi phiên sắp kết thúc, app thoát luôn.
"""
import logging

log = logging.getLogger(__name__)


def on_session_ending(callback) -> None:
    try:
        from Microsoft.Win32 import SessionEndingEventHandler, SystemEvents

        SystemEvents.SessionEnding += SessionEndingEventHandler(lambda sender, event: callback())
    except Exception:
        log.warning("Không đăng ký được sự kiện tắt máy", exc_info=True)

"""Dọn dữ liệu khi gỡ app (chỉ Windows).

App không có nút gỡ riêng: gỡ từ Settings → Apps. Bộ cài gọi `packaging/windows/pre_uninstall.bat`, tệp
này chạy `"Telegram Bot.exe" --uninstall-cleanup` (hàm `cleanup_for_uninstaller`): báo bản đang chạy
thoát, xoá khoá Run, rồi cố gắng hỏi có xoá dữ liệu không; không hỏi được thì giữ dữ liệu.
"""
import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def data_dirs(paths) -> list[Path]:
    """Các thư mục app ghi dữ liệu (theo `app.paths`)."""
    return [Path(paths.config), Path(paths.data), Path(paths.logs), Path(paths.cache)]


def app_root(paths) -> Path:
    """Thư mục chung chứa Config / Data / Logs (`%LOCALAPPDATA%\\<author>\\Telegram Bot`)."""
    return Path(paths.config).parent


def release_stdout_log() -> None:
    """Nhả tệp log của stub Briefcase để xoá được thư mục Logs.

    App chạy không có console thì stub (Telegram Bot.exe) chuyển cả stdout lẫn stderr (`_wfreopen_s`,
    mỗi luồng một handle) vào `Logs\\<app>.<giờ>.log` và giữ tệp mở tới khi app thoát. Trỏ cả fd 1 và
    fd 2 sang NUL thì tệp được đóng (chỉ trỏ fd 1 thì fd 2 vẫn giữ tệp); output sau đó bị bỏ.
    """
    try:
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.flush()
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)
        os.dup2(null, 2)
        os.close(null)
    except (OSError, ValueError):
        log.warning("Không nhả được tệp log stdout", exc_info=True)


def delete_data(paths, store) -> list[str]:
    """Xoá các thư mục dữ liệu và khoá trong Credential Manager. Trả về các lỗi (rỗng = xoá hết)."""
    errors = []
    for d in data_dirs(paths):
        if d.exists():
            shutil.rmtree(d, onexc=lambda func, path, exc: errors.append(f"{path}: {exc}"))
    store.delete_all_secrets()
    return errors


def remove_empty_root(paths) -> None:
    """Xoá thư mục chung của app (và thư mục tác giả) nếu đã trống."""
    root = app_root(paths)
    for d in (root, root.parent):
        try:
            d.rmdir()  # chỉ xoá được khi trống
        except OSError:
            break


def ask_yes_no(title: str, message: str) -> bool | None:
    """Hộp thoại Có / Không của Windows, dùng khi không có giao diện Toga (chế độ --uninstall-cleanup).
    None nếu không hiện được (ví dụ bộ cài chạy ngoài phiên đăng nhập của người dùng)."""
    try:
        import ctypes

        MB_YESNO, MB_ICONQUESTION, MB_TOPMOST, MB_SETFOREGROUND, IDYES = 0x4, 0x20, 0x40000, 0x10000, 6
        result = ctypes.windll.user32.MessageBoxW(
            None, message, title, MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND
        )
    except Exception:
        log.exception("Không hiện được hộp thoại")
        return None
    return None if result == 0 else result == IDYES


DELETE_DATA_QUESTION = (
    "Xoá cả dữ liệu của Telegram Bot (cấu hình, khoá, lịch sử tin nhắn)?\n\n"
    "Chọn Không để giữ lại: cài lại app sẽ tự dùng tiếp dữ liệu cũ."
)


def cleanup_for_uninstaller(
    paths, store, notify_running, disable_autostart, ask=ask_yes_no, release_log=lambda: None
) -> None:
    """Chế độ --uninstall-cleanup (bộ cài gọi trước khi xoá tệp của app).

    `release_log` nhả tệp log của chính tiến trình này trong Logs (xem release_stdout_log)."""
    notify_running()
    disable_autostart()
    if ask("Gỡ Telegram Bot", DELETE_DATA_QUESTION):  # không hỏi được (None): giữ dữ liệu
        release_log()
        for error in delete_data(paths, store):
            log.warning("Không xoá được %s", error)
        remove_empty_root(paths)

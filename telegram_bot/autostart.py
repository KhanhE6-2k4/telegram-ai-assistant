"""Tự khởi động cùng Windows: khoá HKCU\\...\\CurrentVersion\\Run\\TelegramBot (chỉ Windows).

Giá trị là `"<exe>" --background`: Windows gọi lúc đăng nhập, app chỉ hiện ở khay và tự bật bot.
`winreg` được truyền vào được để test trên máy không phải Windows / không đụng registry thật.
"""
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "TelegramBot"
BACKGROUND_FLAG = "--background"


def _winreg(winreg):
    if winreg is None:
        import winreg
    return winreg


def command(executable: str | None = None) -> str:
    return f'"{executable or sys.executable}" {BACKGROUND_FLAG}'


def current_command(winreg=None) -> str | None:
    winreg = _winreg(winreg)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except OSError:
        return None


def is_enabled(executable: str | None = None, winreg=None) -> bool:
    """Khoá Run đã có và trỏ đúng tệp chạy hiện tại."""
    return current_command(winreg) == command(executable)


def enable(executable: str | None = None, winreg=None) -> None:
    winreg = _winreg(winreg)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command(executable))


def disable(winreg=None) -> None:
    winreg = _winreg(winreg)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except OSError:  # chưa có khoá
        pass

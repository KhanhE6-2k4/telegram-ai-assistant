"""Phần riêng Windows của app desktop: khoá Run, dọn dữ liệu khi gỡ (winreg giả)."""
from types import SimpleNamespace

from telegram_bot import autostart, uninstall
from telegram_bot.settings import SettingsStore
from tests.fakes_desktop import FakeKeyring, FakeWinreg

EXE = r"C:\Users\a\AppData\Local\Programs\Telegram Bot\Telegram Bot.exe"


def test_autostart_enable_check_disable():
    reg = FakeWinreg()
    assert not autostart.is_enabled(EXE, winreg=reg)
    autostart.disable(winreg=reg)  # chưa có khoá: không lỗi
    autostart.enable(EXE, winreg=reg)
    assert autostart.current_command(winreg=reg) == f'"{EXE}" --background'
    assert autostart.is_enabled(EXE, winreg=reg)
    # Cài lại vào chỗ khác: khoá cũ không còn đúng
    assert not autostart.is_enabled(r"D:\khac\Telegram Bot.exe", winreg=reg)
    autostart.disable(winreg=reg)
    assert autostart.current_command(winreg=reg) is None


def make_paths(tmp_path):
    root = tmp_path / "khanhpl2op04" / "Telegram Bot"
    paths = SimpleNamespace(config=root / "Config", data=root / "Data", logs=root / "Logs", cache=root / "Cache")
    for d in (paths.config, paths.data, paths.logs):
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x")
    store = SettingsStore(paths.config, paths.data, secrets=FakeKeyring())
    store.set_secret("telegram_bot_token", "t")
    return paths, store, root


def run_cleanup(paths, store, answer):
    calls = []
    uninstall.cleanup_for_uninstaller(
        paths, store,
        notify_running=lambda: calls.append("quit"),
        disable_autostart=lambda: calls.append("autostart"),
        ask=lambda title, message: calls.append("ask") or answer,
        release_log=lambda: calls.append("release_log"),
    )
    return calls


def test_hook_keeps_data_when_question_cannot_be_shown(tmp_path):
    paths, store, root = make_paths(tmp_path)
    assert run_cleanup(paths, store, answer=None) == ["quit", "autostart", "ask"]
    assert (paths.data / "f.txt").exists() and store.telegram_token() == "t"


def test_hook_deletes_data_when_user_says_yes(tmp_path):
    paths, store, root = make_paths(tmp_path)
    assert run_cleanup(paths, store, answer=True) == ["quit", "autostart", "ask", "release_log"]
    assert not root.exists() and not root.parent.exists()
    assert store.telegram_token() == ""


def test_hook_removes_own_log_when_deleting(tmp_path):
    paths, store, root = make_paths(tmp_path)
    # Tiến trình --uninstall-cleanup tạo tệp log mới của chính nó (stub Briefcase): vẫn phải xoá
    (paths.logs / "telegram_bot.20261030_213121_089.log").write_text("")
    assert run_cleanup(paths, store, answer=True) == ["quit", "autostart", "ask", "release_log"]
    assert not root.exists()


def test_hook_keeps_data_when_user_says_no(tmp_path):
    paths, store, root = make_paths(tmp_path)
    assert run_cleanup(paths, store, answer=False) == ["quit", "autostart", "ask"]
    assert (paths.data / "f.txt").exists() and store.telegram_token() == "t"

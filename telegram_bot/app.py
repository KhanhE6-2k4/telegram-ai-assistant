"""App desktop "Telegram Bot": cấu hình qua giao diện, chạy bot nền với biểu tượng ở khay hệ thống.

- Đóng cửa sổ thì app ẩn xuống khay, bot vẫn chạy; "Thoát" ở menu khay mới tắt hẳn.
- Bot tự khởi động cùng Windows (khoá Run, `--background`: chỉ hiện ở khay, tự bật bot).
- Chỉ chạy một bản: mở app lần nữa thì cửa sổ của bản đang chạy hiện lên.
- Userbot (tuỳ chọn): đã nhập Telegram API ID / Hash thì khi kiểm tra kết nối / bật bot, session đã lưu được
  kiểm tra; chưa có hoặc hết hạn thì mở cửa sổ đăng nhập Telegram. Session chỉ lưu trong Credential Manager,
  không ghi log.
"""
import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

import toga
from toga.constants import WindowState
from toga.style import Pack
from toga.style.pack import BOLD, COLUMN, ROW

from bot.checks import run_checks
from bot.userbot_auth import LoginFlow, check_session, describe_login_error
from bot.main import LOG_FORMAT, QUIET_LOGGERS
from bot.runner import BotRunner, Status

from . import autostart, uninstall
from .settings import (
    API_HASH_SECRET, API_ID_SECRET, PROVIDERS, SESSION_SECRET, TOKEN_SECRET, SettingsStore, api_key_secret,
    valid_api_id,
)
from .single_instance import QUIT, SHOW, InstanceServer, send_command
from .ui import theme as t
from .ui.config_view import ConfigView
from .ui.layout import WrappedLabel, set_style
from .ui.login_window import LoginWindow
from .ui.status_view import StatusView

log = logging.getLogger("telegram_bot")

UNINSTALL_FLAG = "--uninstall-cleanup"
WINDOW_SIZE = (960, 700)
# Thứ tự các trang ở thanh bên trái
STATUS_PAGE, CONFIG_PAGE = 0, 1
USERBOT_NOTE = (
    "Chưa đăng nhập Telegram hoặc phiên Telegram đã hết hạn: bot chạy ở chế độ dự phòng (không có userbot). "
    "Tắt rồi bật lại bot để đăng nhập."
)


def is_windows() -> bool:
    return toga.platform.current_platform == "windows"


def setup_file_logging(logs_dir) -> RotatingFileHandler:
    """Ghi log vào <thư mục log>/bot.log (xoay vòng 1 MB × 5 tệp), chỉ để tra lỗi."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs_dir / "bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


class TelegramBotApp(toga.App):
    def startup(self):
        self.main_window = toga.App.BACKGROUND
        setup_file_logging(self.paths.logs)
        self.store = SettingsStore(self.paths.config, self.paths.data)
        # Màu theo chế độ sáng / tối của Windows lúc mở app
        self.theme = t.theme_for(self.dark_mode)
        self.bot_status, self.bot_detail = Status.STOPPED, ""
        self.shown_status = None  # bộ giá trị trang Trạng thái đang hiển thị (xem refresh_status)
        self.runner = BotRunner(on_status=self.on_bot_status)
        self.exiting = False
        # Đang chờ đăng nhập Telegram trước khi bật bot (chặn bật hai lần)
        self.starting = False
        # Ghi chú ở trang Trạng thái (vd. userbot chưa đăng nhập)
        self.userbot_note = ""
        # Mỗi lúc chỉ một cửa sổ đăng nhập Telegram
        self.login_lock = asyncio.Lock()

        # Bản chạy sau gửi lệnh tới bản này (xem single_instance.py)
        self.instance = InstanceServer(
            self.paths.data / "instance.port",
            {
                SHOW: lambda: self.loop.call_soon_threadsafe(self.show_window),
                QUIT: lambda: self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self.quit())),
            },
        )
        self.instance.start()
        if is_windows():
            from . import winsession

            winsession.on_session_ending(self.exit)

        self.build_tray()
        self.build_window()

        if "--background" not in sys.argv[1:]:
            self.show_window()
        if not self.store.missing(self.store.load()):
            self.start_bot()
        elif self.window.visible:
            self.select_page(CONFIG_PAGE)

    # Khay hệ thống

    def build_tray(self):
        self.tray = toga.MenuStatusIcon(id="tray", text=self.formal_name)
        self.status_icons.add(self.tray)
        # Bỏ About / Exit mặc định của Toga, dùng menu tiếng Việt
        for cmd_id in (toga.Command.ABOUT, toga.Command.EXIT):
            if cmd_id in self.status_icons.commands:
                del self.status_icons.commands[cmd_id]
        self.cmd_show = toga.Command(lambda cmd, **kw: self.show_window(), "Mở cửa sổ", group=self.tray, order=1)
        self.cmd_start = toga.Command(lambda cmd, **kw: self.start_bot(), "Bật bot", group=self.tray, order=2)
        self.cmd_stop = toga.Command(self.on_stop_command, "Tắt bot", group=self.tray, order=3, enabled=False)
        self.cmd_quit = toga.Command(self.on_quit_command, "Thoát", group=self.tray, section=1)
        self.status_icons.commands.add(self.cmd_show, self.cmd_start, self.cmd_stop, self.cmd_quit)

    async def on_stop_command(self, command, **kwargs):
        await self.stop_bot()

    async def on_quit_command(self, command, **kwargs):
        await self.quit()

    # Cửa sổ

    def build_window(self):
        theme = self.theme
        self.status_view = StatusView(theme, on_toggle=self.on_toggle)
        self.config_view = ConfigView(
            theme, on_pick_credentials=self.on_pick_credentials, on_check=self.on_check
        )
        self.pages = [self.status_view.page, self.config_view.page]
        # Bề rộng vùng nội dung; đặt sẵn theo cỡ cửa sổ ban đầu khi trang chưa gắn vào cửa sổ (rẻ),
        # để lúc mở cửa sổ không phải bố trí lại
        self.content_width = WINDOW_SIZE[0] - t.SIDEBAR_WIDTH
        self.current_page = None

        # Thanh điều hướng bên trái như phần Cài đặt của Discord (thay cho tab gốc của Windows,
        # vốn không đổi màu được). Vùng nội dung chỉ chứa trang đang chọn (xem select_page).
        subtitle = WrappedLabel(
            # "Thống kê task từ nhóm Telegram lên Google Sheet và tóm tắt hội thoại",
            "Thống kê task từ nhóm Telegram lên Google Sheet",
            style=Pack(font_size=t.SMALL, color=theme.muted, margin=(0, t.S, t.L, t.S)),
        )
        subtitle.set_width(t.SIDEBAR_WIDTH - 4 * t.S)
        nav = toga.Box(
            children=[
                toga.Label(
                    self.formal_name,
                    style=Pack(font_size=t.TITLE, font_weight=BOLD, color=theme.heading, margin=(0, t.S, t.XS, t.S)),
                ),
                subtitle.widget,
            ],
            style=Pack(direction=COLUMN, gap=t.XS, margin=(t.XL, t.S)),
        )
        self.nav_buttons = []
        for i, title in enumerate(["Trạng thái", "Cấu hình"]):
            nav_button = toga.Button(
                title, on_press=lambda widget, i=i, **kwargs: self.select_page(i), style=Pack(font_size=t.BODY)
            )
            self.nav_buttons.append(nav_button)
            nav.add(nav_button)
        sidebar = toga.Box(
            children=[nav],
            style=Pack(direction=COLUMN, width=t.SIDEBAR_WIDTH, background_color=theme.sidebar),
        )
        # Một ScrollContainer dùng chung cho mọi trang (xem select_page)
        self.scroller = toga.ScrollContainer(
            horizontal=False, style=Pack(flex=1, background_color=theme.background)
        )
        content = toga.Box(
            children=[sidebar, self.scroller], style=Pack(direction=ROW, background_color=theme.background)
        )
        self.select_page(STATUS_PAGE)
        self.window = toga.Window(
            title=self.formal_name,
            size=WINDOW_SIZE,
            on_close=self.on_window_close,
            on_resize=self.on_window_resize,
        )
        self.window.content = content
        self.load_config_form()
        self.refresh_status()

    def load_config_form(self):
        settings = self.store.load()
        self.update_llm_label(settings)
        keys = {p: self.store.api_key(p) for p in PROVIDERS}
        self.config_view.load(
            settings, self.store.telegram_token(), keys, self.store.credentials_email(), self.store.telegram_api()
        )

    def update_llm_label(self, settings):
        """Chữ "provider / model" ở trang Trạng thái; cập nhật khi đọc / lưu cấu hình."""
        self.llm_label = f"{PROVIDERS[settings.provider].label} / {settings.model or '(mặc định)'}"

    def select_page(self, index: int):
        # Đổi nội dung của ScrollContainer dùng chung: thay trang trong một bước và chỉ bố trí lại bên
        # trong khung cuộn (thanh bên không bị đụng tới). Gỡ / gắn trang vào một Box thì cả cửa sổ bố
        # trí lại hai lần, giữa hai lần vùng nội dung trống (nháy). Không dùng style display="none":
        # Toga chỉ dùng nó cho bản web, trên WinForms các trang vẫn được vẽ chồng lên nhau.
        if index == self.current_page:
            return
        self.current_page = index
        page = self.pages[index]
        # Cửa sổ đổi cỡ lúc trang đang bị gỡ (xem on_window_resize): áp bề rộng trước khi gắn lại
        if page.available != self.content_width:
            page.set_width(self.content_width)
        self.scroller.content = page.box
        page.refresh()
        # Trang mới luôn hiện từ đầu (vị trí cuộn dùng chung); chỉ cuộn, không bố trí lại
        self.scroller.vertical_position = 0
        # Đánh dấu nút đang chọn chỉ bằng màu: đổi màu chỉ vẽ lại nút, đổi font (in đậm) thì cả cửa sổ
        # bố trí lại
        theme = self.theme
        for i, nav_button in enumerate(self.nav_buttons):
            selected = i == index
            set_style(
                nav_button,
                background_color=theme.selected if selected else theme.sidebar,
                color=theme.heading if selected else theme.muted,
            )

    def show_window(self):
        self.window.show()
        if self.window.state == WindowState.MINIMIZED:
            self.window.state = WindowState.NORMAL

    def on_window_close(self, window, **kwargs):
        # Ẩn xuống khay thay vì đóng: bot vẫn chạy
        window.hide()
        return False

    def on_window_resize(self, window, **kwargs):
        # Chỉ cập nhật trang đang hiện; trang khác nhận bề rộng mới khi được chọn (select_page)
        available = window.size[0] - t.SIDEBAR_WIDTH
        if available == self.content_width:
            return
        self.content_width = available
        self.pages[self.current_page].set_width(available)

    # Bot

    def on_bot_status(self, status: Status, detail: str):
        """Gọi từ thread của bot: chuyển về thread giao diện."""
        self.loop.call_soon_threadsafe(self.apply_status, status, detail)

    def apply_status(self, status: Status, detail: str):
        self.bot_status, self.bot_detail = status, detail
        self.refresh_status()
        if status is Status.RUNNING:
            self.ensure_autostart()

    def refresh_status(self):
        running = self.runner.running and self.bot_status is not Status.STOPPED
        shown = (self.bot_status, self.bot_detail, self.llm_label, running, self.userbot_note)
        # Không đổi gì thì bỏ qua: mỗi lần gán là cả cửa sổ bố trí lại và vẽ lại
        if shown == self.shown_status:
            return
        self.shown_status = shown
        self.status_view.update(*shown)
        if self.cmd_start.enabled == running:
            self.cmd_start.enabled = not running
        if self.cmd_stop.enabled != running:
            self.cmd_stop.enabled = running

    def ensure_autostart(self):
        """Bot đã kết nối được Telegram: ghi khoá Run để tự khởi động cùng Windows (cố định, chỉ gỡ app
        mới xoá). Chỉ khi app đã đóng gói; chạy `briefcase dev` thì không ghi."""
        if not (is_windows() and self.is_bundled):
            return
        try:
            if not autostart.is_enabled():
                autostart.enable()
                log.info("Đã bật tự khởi động cùng Windows")
        except OSError:
            log.exception("Không ghi được khoá tự khởi động")

    def start_bot(self) -> bool:
        """Bật bot (menu khay, nút Bật, lúc mở app). Có userbot thì trước đó kiểm tra / đăng nhập Telegram,
        nên phần bật thật chạy nền (_start_bot). Trả về False nếu thiếu cấu hình."""
        if self.runner.running or self.starting:
            return True
        settings = self.store.load()
        missing = self.store.missing(settings)
        if missing:
            self.show_window()
            self.select_page(CONFIG_PAGE)
            self.loop.create_task(self.window.dialog(toga.InfoDialog(
                "Chưa đủ cấu hình", "Cần nhập: " + ", ".join(missing) + ".\nSau đó bấm Kiểm tra kết nối."
            )))
            return False
        self.starting = True
        self.loop.create_task(self._start_bot(settings))
        return True

    async def _start_bot(self, settings):
        try:
            api_id, api_hash = self.store.telegram_api()
            use_userbot = valid_api_id(api_id) and bool(api_hash)
            session = ""
            if use_userbot:
                session = await self.ensure_userbot_session(api_id, api_hash) or ""
            # Huỷ đăng nhập: bot vẫn chạy, ở chế độ dự phòng
            self.userbot_note = USERBOT_NOTE if use_userbot and not session else ""
            if not (self.exiting or self.runner.running):
                self.runner.start(self.store.to_config(settings, userbot_session=session))
        except Exception:
            log.exception("Không bật được bot")
        finally:
            self.starting = False
            self.refresh_status()

    async def ensure_userbot_session(self, api_id: str, api_hash: str) -> str | None:
        """Session userbot dùng được: session đã lưu nếu còn đăng nhập, ngược lại mở cửa sổ đăng nhập
        Telegram. None nếu người dùng huỷ. Không kiểm tra được vì mất mạng thì vẫn dùng session đã lưu (bot
        tự chuyển chế độ dự phòng nếu session hỏng)."""
        async with self.login_lock:
            session = self.store.userbot_session()
            if session:
                try:
                    # Thread riêng, loop riêng: không làm treo cửa sổ
                    label = await self.loop.run_in_executor(
                        None, lambda: asyncio.run(check_session(int(api_id), api_hash, session))
                    )
                except (OSError, ConnectionError, TimeoutError) as e:
                    log.warning(
                        "Không kiểm tra được phiên Telegram (%s: %s): dùng phiên đã lưu",
                        type(e).__name__, describe_login_error(e),
                    )
                    return session
                except Exception as e:
                    log.warning("Phiên Telegram không dùng được: %s: %s", type(e).__name__, describe_login_error(e))
                    label = None
                if label:
                    return session
                log.info("Chưa đăng nhập Telegram hoặc phiên Telegram đã hết hạn: mở cửa sổ đăng nhập")
            return await self.login_telegram(int(api_id), api_hash)

    async def login_telegram(self, api_id: int, api_hash: str) -> str | None:
        """Cửa sổ đăng nhập Telegram. Xong thì lưu session vào Credential Manager (không ghi log session)."""
        self.show_window()
        result = await LoginWindow(self.theme, LoginFlow(api_id, api_hash)).run()
        if result is None:
            log.info("Đã huỷ đăng nhập Telegram")
            return None
        session, account = result
        self.store.set_secret(SESSION_SECRET, session)
        log.info("Đã đăng nhập Telegram: %s", account)
        return session

    async def stop_bot(self):
        await self.loop.run_in_executor(None, self.runner.stop)
        self.bot_status, self.bot_detail = Status.STOPPED, ""
        self.userbot_note = ""
        self.refresh_status()

    async def on_toggle(self, widget, **kwargs):
        if self.runner.running:
            await self.stop_bot()
        else:
            self.start_bot()

    async def quit(self):
        if self.exiting:
            return
        self.exiting = True
        await self.loop.run_in_executor(None, self.runner.stop)
        self.instance.close()
        self.exit()

    # Cấu hình

    async def on_pick_credentials(self, widget, **kwargs):
        path = await self.window.dialog(toga.OpenFileDialog("Chọn tệp khoá Google", file_types=["json"]))
        if not path:
            return
        try:
            email = self.store.import_credentials(path)
        except (ValueError, OSError) as e:
            await self.window.dialog(toga.ErrorDialog("Tệp khoá không hợp lệ", str(e)))
            return
        self.config_view.set_credentials_email(email)

    async def on_check(self, widget, **kwargs):
        view = self.config_view
        settings, token, keys = view.values()
        api_id, api_hash = view.telegram_api()
        use_userbot = bool(api_id or api_hash)
        missing = [
            name for name, value in [
                (PROVIDERS[settings.provider].key_label, keys.get(settings.provider)),
                ("Telegram bot token", token),
                ("Spreadsheet ID", settings.spreadsheet_id),
                ("Tệp khoá Google", self.store.credentials_email()),
                ("Tên model", settings.model),
            ] if not value
        ]
        # API ID / Hash: để trống cả hai = không dùng userbot; đã nhập thì phải đủ và đúng dạng
        if use_userbot and not valid_api_id(api_id):
            missing.append("Telegram API ID (chỉ gồm chữ số)")
        if use_userbot and not api_hash:
            missing.append("Telegram API Hash")
        if missing:
            view.show_messages([("Cần nhập: " + ", ".join(missing), self.theme.danger)])
            return
        view.set_checking(True)
        try:
            session = ""
            if use_userbot:
                # Có thể mở cửa sổ đăng nhập Telegram (session chưa có / hết hạn)
                session = await self.ensure_userbot_session(api_id, api_hash) or ""
            config = self.store.to_config(
                settings, telegram_token=token, api_key=keys[settings.provider],
                telegram_api=(api_id, api_hash), userbot_session=session,
            )
            # Thread riêng, loop riêng: không làm treo cửa sổ
            results = await self.loop.run_in_executor(None, lambda: asyncio.run(run_checks(config)))
        except Exception as e:
            log.exception("Kiểm tra kết nối thất bại")
            view.show_messages([(f"Lỗi: {e}", self.theme.danger)])
            return
        finally:
            view.set_checking(False)
        view.show_results(results)
        if not all(r.ok for r in results):
            return

        # Cả 3 đều ✓: lưu cấu hình rồi (khởi động lại) bot
        self.store.save(settings)
        self.update_llm_label(settings)
        self.store.set_secret(TOKEN_SECRET, token)
        for provider, key in keys.items():
            self.store.set_secret(api_key_secret(provider), key)
        self.store.set_secret(API_ID_SECRET, api_id)
        self.store.set_secret(API_HASH_SECRET, api_hash)
        if not use_userbot:
            self.store.delete_secret(SESSION_SECRET)  # bỏ userbot: không giữ session
        await self.stop_bot()
        self.start_bot()
        view.show_results(results, note="Cấu hình thành công.")
        # note="Đã lưu cấu hình và khởi động lại bot."


def uninstall_cleanup(app: TelegramBotApp) -> None:
    """Chế độ --uninstall-cleanup: bộ cài gọi trước khi xoá tệp (packaging/windows/pre_uninstall.bat)."""
    port_file = app.paths.data / "instance.port"

    def notify_running():
        # Báo bản đang chạy thoát rồi chờ nó tắt hẳn (nó xoá instance.port khi thoát)
        if send_command(port_file, QUIT):
            deadline = time.monotonic() + 45
            while port_file.exists() and time.monotonic() < deadline:
                time.sleep(0.5)

    uninstall.cleanup_for_uninstaller(
        app.paths,
        SettingsStore(app.paths.config, app.paths.data),
        notify_running=notify_running,
        disable_autostart=autostart.disable if is_windows() else lambda: None,
        release_log=uninstall.release_stdout_log,
    )


def main():
    app = TelegramBotApp()
    if UNINSTALL_FLAG in sys.argv[1:]:
        uninstall_cleanup(app)
        sys.exit(0)
    # Đã có bản đang chạy: hiện cửa sổ của bản đó rồi thoát
    if send_command(app.paths.data / "instance.port", SHOW):
        sys.exit(0)
    return app

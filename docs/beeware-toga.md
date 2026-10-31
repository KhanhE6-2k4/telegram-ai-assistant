# BeeWare Toga & Briefcase Rules

You are an expert Python developer using BeeWare (Toga + Briefcase) to build native, cross-platform GUI apps. Target **Toga >= 0.5** and the current Briefcase release. If the project pins an older Toga in `pyproject.toml`, follow that version's API instead.

## 1. Briefcase workflow

Do not run the app with `python app.py` or install app dependencies with `pip install`. Use Briefcase:

| Goal | Command |
|---|---|
| Create a new project | `briefcase new` |
| Run in dev mode (fast loop) | `briefcase dev` |
| Run tests | `briefcase dev --test` |
| Generate native scaffold (once per platform) | `briefcase create` |
| Push code changes into the scaffold | `briefcase update` (add `-r` after changing `requires`) |
| Compile the app | `briefcase build` |
| Run the built app | `briefcase run` (add `-u` to update code first) |
| Build an installer (.msi, .dmg, .apk, ...) | `briefcase package` |

Mobile targets: append the platform, e.g. `briefcase run android`, `briefcase run iOS`.

### Dependencies
- Add runtime dependencies to `requires = [...]` under `[tool.briefcase.app.<app_name>]` in `pyproject.toml`, not to a `requirements.txt`.
- Platform-specific dependencies go in the platform sections (e.g. `[tool.briefcase.app.<app_name>.android]`).
- After editing `requires`, run `briefcase dev -r` or `briefcase update -r`.

### Installers and shortcuts
- Desktop apps must create a **desktop shortcut** when installed, in addition to the Start menu / launcher
  entry. Use Briefcase's built-in option rather than custom install scripts, so the installer also
  removes the shortcut on uninstall:
  - Windows (MSI): set `create_desktop_shortcut = true` in `[tool.briefcase.app.<app_name>.windows]`.
    The installer then shows a "Create a desktop shortcut" checkbox, checked by default (`false` = shown
    but unchecked; omitted = no shortcut and no checkbox). Silent installs (`msiexec /qn`) create it too.
    Requires Briefcase >= 0.4.7 (Windows app template with `create_desktop_shortcut`); on older versions,
    upgrade Briefcase instead of hand-editing the generated `.wxs`.
  - Other platforms: follow the platform convention (macOS apps are launched from Applications / Dock;
    Linux packages install a `.desktop` launcher), and do not write shortcut files from app code.
- Installer settings (`create_desktop_shortcut`, `install_option`, `post_install_script`,
  `pre_uninstall_script`, ...) are baked into the generated installer project. After changing them,
  regenerate it with `briefcase create <platform>` (answer yes to overwrite); `briefcase update` does
  not regenerate installer templates.
- Never edit files under `build/` by hand: they are overwritten by `briefcase create`.

## 2. App structure

Subclass `toga.App` and build the UI in `startup()`. `main()` returns the app instance; `formal_name` and `app_id` come from `pyproject.toml`.

```python
import asyncio

import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW


class MyAwesomeApp(toga.App):
    def startup(self):
        main_box = toga.Box(style=Pack(direction=COLUMN, margin=10))

        self.label = toga.Label("Hello BeeWare", style=Pack(margin=5))
        button = toga.Button(
            "Load", on_press=self.handle_button_press, style=Pack(margin=5)
        )
        main_box.add(self.label, button)

        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = main_box
        self.main_window.show()

    async def handle_button_press(self, widget):
        self.label.text = "Loading..."
        await asyncio.sleep(2)  # yields to the event loop; UI stays responsive
        self.label.text = "Done!"


def main():
    return MyAwesomeApp()
```

## 3. Layout and UI

- Layout uses `toga.style.Pack`, a CSS-like box model.
- Set `MainWindow.content` to a `toga.Box` (or `ScrollContainer` / `SplitContainer` / `OptionContainer`) that holds the widgets, rather than a single bare widget.
- `toga.Box` defaults to `ROW`. Always set `direction=COLUMN` or `direction=ROW` explicitly.
- Toga 0.5 style names follow CSS: use `margin` (not `padding`), `align_items` (not `alignment`), `text_align`. The old names still work but are deprecated; do not generate them.
- Use `flex=1` for widgets that should expand. Do not hard-code x/y positions.
- Keep references to widgets you update later on `self` (e.g. `self.label`).

## 4. Event handlers and threading

- Handlers have the signature `def handler(self, widget, **kwargs)`.
- Anything that waits (network, file I/O, sleep) must be `async def` and use `await`. Never call `time.sleep()` or blocking `requests` calls in a handler.
- For CPU-heavy or blocking library calls, use `await asyncio.get_running_loop().run_in_executor(None, func)`.
- Only touch widgets from the main thread. To update the UI from background work, schedule it with `self.loop.call_soon_threadsafe(...)`.
- Use the window dialog API (`await self.main_window.dialog(toga.InfoDialog(...))`) rather than printing to the console for user feedback.

## 5. Platform-agnostic code

- Do not import `ctypes`, `win32api`, `rubicon.objc`, `gi` (GTK), or Android Java bridges in app logic.
- If platform-specific code is unavoidable, isolate it in its own module and guard it with `toga.platform.current_platform` (values such as `"windows"`, `"macOS"`, `"linux"`, `"android"`, `"iOS"`, `"web"`), importing the native module only inside that branch.
- Use `self.paths` (`self.paths.data`, `self.paths.config`, `self.paths.cache`, `self.paths.app`) for file locations; never hard-code OS paths.

## 6. Tests

- Put tests in `tests/` and run them with `briefcase dev --test` (or `briefcase run --test` for the built app).
- Keep business logic in plain Python modules without Toga imports so it can be unit-tested directly.

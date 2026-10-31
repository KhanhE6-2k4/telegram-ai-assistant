# Toga UI Rules: avoiding flicker and redundant relayouts

Rules for writing Toga (BeeWare) user interfaces that stay smooth, especially on Windows (WinForms
backend). They complement general BeeWare rules (project structure, Briefcase workflow, threading).

Verified against **Toga 0.5.7 / travertino** source. Toga's internals change between releases: when the
project pins a different version, re-check the table in section 1 against `toga/widgets/*.py`,
`toga/style/layout.py` (`_apply`) and the backend's `widgets/*.py` before relying on it.

## 1. What each operation costs

Toga does **not** batch changes. Many setters call `Widget.refresh()`, which re-measures that widget and
then **recomputes the layout of the whole window** from the root and repositions every native widget.
WinForms repaints without double buffering, so each relayout can flicker; several in one event handler
look like stutter. Resizing the window by dragging is usually smooth; it is per-event bursts of
relayouts that are visible.

| Operation | Whole-window relayout? |
|---|---|
| `Label.text = …`, `Button.text = …` | Yes, **even if the text is unchanged** |
| `TextInput.value = …`, `.placeholder = …` (also `PasswordInput`, `MultilineTextInput`) | Yes, even if unchanged |
| `box.add(...)`, `insert`, `remove`, `clear()` | Yes, **once per call** (`add(a, b, c)` = one relayout) |
| `widget.refresh()` | Yes |
| Layout-affecting style change: `width`, `height`, `flex`, `direction`, `margin`, `gap`, `align_items`, `font_*` | Yes, if the value changes |
| `color`, `background_color`, `text_align`, `visibility` | No, repaints that widget only |
| Setting a style property to its **current** value | No (travertino skips it) |
| `style.update(a=…, b=…)` | At most one relayout for the whole group (`batch_apply`) |
| `widget.enabled = …`, `Command.enabled = …`, `Selection.value = …` | No |
| Anything on a widget **not yet attached** to a window | No (nothing to lay out) |

Other facts that matter:
- `display="none"` is only used to generate CSS for the web backend. The Pack layout algorithm and the
  WinForms backend ignore it: the widget is still drawn, so "hidden" pages overlap visible ones.
- `visibility="hidden"` really hides the native widget, but it **keeps its space** in the layout.
- The content of a `ScrollContainer` is its own layout root: `widget.refresh()` inside it, and
  assigning `scroller.content`, lay out only the scroller's content, not the whole window.
- On WinForms, a widget measured while detached from a window is measured at DPI scale 1. After it is
  attached on a 125%/150% display its text is larger than its measured size and gets clipped until it
  is re-measured.

## 2. Helpers every project should have

Put these in one shared UI module and use them everywhere after the window has content. Using `_impl`
is confined to this module, with a comment explaining why the public API is not enough.

```python
import toga


def set_text(widget: toga.Widget, value: str) -> None:
    """Label / Button: assign only when different (the setter always relayouts)."""
    if widget.text != value:
        widget.text = value


def set_input(widget: toga.TextInput, value: str | None = None, placeholder: str | None = None) -> None:
    """TextInput: change value / placeholder without a relayout.

    The size of a text input does not depend on its content, so the relayout the public setter
    triggers is wasted. `_impl.set_value` still fires the native change event (on_change, validators).
    """
    if value is not None and widget.value != value:
        widget._impl.set_value(value)
    if placeholder is not None and widget.placeholder != placeholder:
        widget._impl.set_placeholder(placeholder)


def set_style(widget: toga.Widget, **props) -> None:
    """Update only the properties that differ, in one batched update.

    Compare against the validated value (e.g. "#FFF" is stored as an rgb object).
    """
    style = widget.style
    changed = {
        name: value for name, value in props.items()
        if style[name] != getattr(type(style), name).validate(value)
    }
    if changed:
        style.update(**changed)


def remeasure(root: toga.Widget) -> None:
    """Re-measure every widget under `root` (e.g. after re-attaching it), then relayout once.

    `Widget.refresh()` = re-measure that widget + relayout the whole window, so calling it per
    widget costs N relayouts. `_impl.refresh()` re-measures only.
    """
    def walk(widget: toga.Widget) -> None:
        for child in widget.children:
            walk(child)
        widget._impl.refresh()

    walk(root)
    root.refresh()
```

Before relying on `set_text` for a backend, check that its `get_text` returns exactly what was set
(WinForms `Label` does; `Button` maps `""` ↔ zero-width space, which Toga normalizes).

## 3. Rules

1. **Never assign `.text`, `.value`, `.placeholder` or individual style properties directly once the
   window has content.** Go through the helpers in section 2. Wrapper components (e.g. a word-wrapping
   label) must use the helpers internally, so callers can assign freely.
2. **Minimize relayouts per user event; target one.** In a handler:
   - Adding several widgets: one `box.add(*widgets)`, never `add` in a loop.
   - Replacing a list of widgets: one `clear()`, then one `add(*new_widgets)`.
   - Several style changes on one widget: one `set_style(...)` call.
3. **Build detached, attach last.** Create widgets, set their text and sizes while they are not in the
   window; attach the finished subtree in a single `add`. Give new pages their correct initial width
   before the window is shown so that showing it does not trigger another relayout.
4. **Skip no-op updates at the top.** A function that pushes state into several widgets keeps the tuple
   of values it last displayed and returns early when nothing changed. Do not read files, settings or
   keyrings inside UI update functions; compute derived display strings when the underlying data
   changes.
5. **Update only what is visible.** On window resize: ignore events where the relevant dimension did not
   change, apply the new size only to the visible page, and mark the others stale; apply it to a stale
   page when it is shown, before attaching it.
6. **Never call `widget.refresh()` in a loop.** Use `remeasure(root)`.
7. **Switching pages (sidebar / custom tabs):** use **one shared `ScrollContainer`** as the page area and
   assign the page's root box to `scroller.content`. That setter swaps pages in a single step and only
   lays out the scroller's content (its content is a separate layout root), so the rest of the window
   is untouched. Then `remeasure(page_box)` to fix DPI-scaled text, and reset
   `scroller.vertical_position = 0` (scrolling does not relayout). Pages must not contain their own
   `ScrollContainer`.
   - Do not swap pages with `box.remove(old)` / `box.add(new)` (or `clear()` + `add`): that is two
     whole-window relayouts, and between them the page area is painted empty (flicker).
   - Do not hide pages with `display="none"` (they overlap) or `visibility="hidden"` (they keep their
     space).
   - Selecting the page that is already shown does nothing.
8. **Signal state with color, not geometry.** To mark a selected item or a status, change `color` /
   `background_color` (repaint only) rather than `font_weight`, `font_size`, `margin` or `width`
   (relayout).
9. **Native widgets that cannot be styled** (e.g. WinForms tabs ignore colors): replace them with a
   custom sidebar of `Button`s plus page swapping (rule 7), instead of fighting the native widget.

## 4. Reusable structure

- Keep reusable building blocks in one module (page with title and scroll, card, form row with label,
  button bar, word-wrapping label, styled button factory). Add a block there once it is used in two
  places; each block applies section 3 internally so views do not have to remember it.
- Responsive blocks expose a setter (`set_width(px)`, `set_narrow(bool)`) and register with their page;
  the page's `set_width` calls them. Widgets created at runtime (e.g. result lines) are registered and
  unregistered together with adding / removing them from the widget tree.
- One class per page / view. It owns a `page` (or `container`) attribute and exposes update methods
  that take **data**, not widgets (`update(status, detail, running)`). The app class calls those
  methods and never sets widget attributes of a view directly.
- Colors, font sizes and spacing come from a theme module (named constants and a light / dark palette);
  views do not hard-code numbers or color codes.
- Keep business logic out of view modules so it can be unit-tested without Toga.

## 5. Checklist before finishing UI work

- Search the UI code for `\.text =`, `\.value =`, `\.placeholder =`, `\.style\.\w+ =`,
  `\.style\.update(` and `.add(` inside loops. Every hit that can run after the window has content goes
  through a helper (or is inside a helper).
- For each user action (switch page, press a button, change a selection, receive a background status
  update), count the relayouts using the table in section 1. Target 1; more than 2 needs a reason.
- Run the app (e.g. `briefcase dev`) and test at 100% and at 125% / 150% display scaling: no flicker
  when switching pages or updating status, no clipped text.

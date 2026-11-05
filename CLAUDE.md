# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Telegram bot: a userbot (owner's account, Telethon) collects messages of tracked chats into SQLite; an LLM
extracts tasks into Google Sheets and summarizes each day; the BotFather bot answers `@bot` requests in groups
and owner-only topics (`/p` / `/g`, end with `/e`) in its DM, and lets authorized users edit tasks via a confirm button.
Code comments, prompts, user-facing text and README are in Vietnamese; keep them that way. README.md is
the detailed spec (process flow, storage layout); keep it in sync with behavior changes.

## Commands

Python 3.12 (same as `Dockerfile` and the packaged app). Two virtual environments by convention, both
gitignored; activate the matching one before running commands:
- `.venv`: bot and tests (`pip install -r requirements.txt pytest`).
- `.venv-app`: only `briefcase`. Briefcase builds its own isolated environments from `requires` in
  `pyproject.toml` (`.briefcase/` for `briefcase dev`, `build/` for the app), so never pip-install app
  dependencies into `.venv-app`; after changing `requires`, run with `-r`.

```bash
# .venv
python -m pytest -q                                   # all tests (offline, no API keys needed)
python -m pytest -q tests/test_review.py::test_find_issues   # single test
python -m bot.main --check --provider openai --model <m>     # smoke-test an LLM provider only (needs API key)
python -m bot.main [--provider anthropic|openai|openai-chat] [--model M] [--base-url URL]   # run the bot
python -m bot.userbot_login                          # log the userbot in once, prints USERBOT_SESSION
# .venv-app (desktop app is Windows only)
briefcase dev                                         # run the desktop app from source
briefcase dev --test                                  # run tests inside the app's environment
briefcase create windows && briefcase build windows && briefcase package windows   # build the .msi
```

Running the bot needs `.env` (`TELEGRAM_BOT_TOKEN`, `SPREADSHEET_ID` required, plus the provider's key) and
`credentials.json` (Google service account). Userbot needs `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`USERBOT_SESSION` (all optional: missing → fallback mode, the bot stores group messages itself). CLI flags override `LLM_PROVIDER/LLM_MODEL/LLM_BASE_URL`;
`LLM_MODEL` from env only applies when the provider equals `LLM_PROVIDER` (see `config.llm_settings`).
No linter/formatter is configured. Only one process may poll a given bot token (else Telegram `Conflict`).

## Architecture

- `bot/main.py`: `build_application(config)` (handlers, `BotState`, `Userbot` if configured, jobs; CLI
  `main()` adds logging and `run_polling`), `post_init` (= `start_userbot` then `set_bot_commands`, shared
  with the runner), handlers, `BotState` (per-chat `asyncio.Lock`, `busy` set, in-memory
  `pending_edits`/`pending_picks`, one `Session`), `ingest` (store one message + mid-day compaction, used by userbot and
  fallback), `supplement_chat` / `supplement_tracked` (backfill via userbot), retry loop `BotState.compact`,
  scheduled jobs (`finalize_past_days` daily at `DAILY_SUMMARY_TIME`, `on_startup` ~10 s after start:
  supplement → finalize → today → review). Jobs use `misfire_grace_time=None` because APScheduler's
  default 1 s grace silently skips them.
- `bot/userbot.py`: `Userbot` (Telethon, imported lazily; `StringSession`, runs in the bot's asyncio loop):
  `NewMessage`/`MessageEdited` handlers forward only chats in `tracked`, `fetch_since` (backfill, FloodWait
  ≤ 5 min waited, longer → skip chat), `find_users` (@username / id / phone direct, else display name in private dialogs), `find_groups`,
  `recent_users` / `recent_groups`; chats are `(chat_id, name, username)` tuples. Pure helpers `is_conversation`,
  `message_fields`. `bot/userbot_login.py` prints the session once; never log it.
- `bot/summarizer.py` `process_day`: one processing pass = **phase A** (LLM tool loop `add_task`/`update_task`
  writes the sheet) then **phase B** (forced `record_summary`). `compacted_until` advances only after both
  succeed; a retry re-reads the sheet so partial writes don't duplicate tasks.
- `bot/review.py`: startup sheet audit (assign missing STT in code; LLM fills assignee, normalizes relative
  deadlines by task's created day, marks non-tasks / duplicates). Skipped when the sheet fingerprint is
  unchanged (`sheet_reviews` table).
- `bot/qa.py`: question answering (`@bot` in groups, private commands) via tools `list_tasks` (sheet, default last 30 days) and `get_day`
  (day summary + unprocessed messages); `propose_task_edit` only for editors, applied after button press.
- `bot/sheets.py`: `TaskSheet` (one tab, reads once, writes single cells) and `SheetClient` (finds tab by
  developer metadata `telegram_chat_id`, never by title; tab title follows group name unless user renamed it;
  auto-migrates legacy chat-id-named tabs and 5-column tabs by inserting column C).
- `bot/storage.py`: SQLite schema + light migrations in `Storage._migrate`. `format_chat` / `Storage.chat_label`
  give the log label `"Title" (@username, chat_id)`; logs about a chat use it (`SheetClient` / `Userbot` get
  `label_of`), never a bare chat id. `chats.username` is filled from bot updates and userbot entities.
- `bot/dates.py`: `calendar_text(day)` (weekday-labelled weeks Mon–Sun + month ends, computed in code) and
  `DEADLINE_RULES` (Vietnamese phrase conventions), injected into the task, review and Q&A prompts so
  the model looks dates up instead of computing weekdays. Review fingerprint includes the rules.
- `bot/checks.py`: `check_llm` / `check_telegram` / `check_sheet` / `run_checks`, shared by `--check`
  and the desktop app's "Kiểm tra kết nối"; return results instead of printing. With a Telegram API ID the
  "Telegram" item also runs `check_userbot` (no separate item).
- `bot/userbot_auth.py`: `check_session` and `LoginFlow` (phone → code → 2FA password, one Telethon client
  in its own thread + loop; the UI awaits `flow.call(step, ...)`), `describe_login_error`. Used by the
  desktop app: `ensure_userbot_session` (stored session, else login window; cancel → fallback mode with a
  status note). The session string is stored only in keyring and never logged (the CLI
  `bot/userbot_login.py` still prints it once).
- `bot/runner.py` `BotRunner`: runs `build_application(config)` in its own thread + asyncio loop
  (initialize → post_init → start_polling → start; stop in reverse incl. `stop_userbot`, closes the DB). Network errors while
  connecting retry with backoff up to 5 min (`Đang chờ mạng`); other errors end in `Status.ERROR`.
- `telegram_bot/`: Briefcase/Toga desktop app (Windows only, `pyproject.toml`). `app.py` (tray icon,
  `main_window = BACKGROUND`, window hides on close, `--background`, `--uninstall-cleanup`),
  `settings.py` (`settings.json` in `app.paths.config`, secrets in `keyring` per provider plus
  `telegram_api_id` / `telegram_api_hash` / `userbot_session`, copied `credentials.json`, `to_config`), `autostart.py` (HKCU Run key, only when `app.is_bundled`),
  `single_instance.py` (localhost socket, `show`/`quit`), `ui/login_window.py` (Telegram login sub-window,
  steps built detached and swapped via `window.content`), `uninstall.py` (data cleanup for the MSI
  pre-uninstall hook only; no in-app uninstall), `winsession.py`
  (pythonnet), `ui/`. Only `app.py`, `winsession.py` and `ui/` import Toga / native code; the rest is
  plain Python with injectable `keyring` / `winreg` for tests. Never change `bundle`, `author`,
  `formal_name`: they define the data folder. MSI strings must fit code page 1252 (else WiX `WIX0311`),
  so the `windows` section of `pyproject.toml` overrides `description` (unaccented) and `license-files`
  (`LICENSE.rtf`, Vietnamese written as RTF `\uN?` escapes); keep `LICENSE.rtf` in sync with `LICENSE`.
- `bot/providers/`: provider-agnostic LLM layer. Everything else uses only `LLMProvider.complete()`,
  neutral types (`Tool`, `ToolCall`, `ChatMessage`, `Response`) and `run_tool_loop`. Providers register via
  `@register_provider` and are built by `create_provider`. `openai` uses the Responses API (needed for tools
  with reasoning models; reasoning items round-trip through `ChatMessage.provider_data`); `openai-chat` uses
  Chat Completions for compatible endpoints. SDKs are imported lazily inside provider constructors.

## Invariants that span files

- Google Sheet is the source of truth for tasks; a task's identity is its STT (column A). Columns are
  `HEADER`/`FIELDS` in `sheets.py`; only cells in `changes` are written so manual sheet edits survive.
- A row is a task only with a concrete assignee: `add_task` without one (or with the `Tất cả` label) is
  rejected in code. Non-task rows get assignee `Tất cả`; duplicates get description `Trùng với task #N`.
  `is_marked()` excludes both from assignee backfill and `<task>`. The model never deletes rows.
- A message's `day` is its original send time converted to `TIMEZONE`. Pending = `id > compacted_until` or
  `edit_pending = 1`. Edited or late-arriving messages of a finalized day reopen it (`finalized = 0`) so the
  next finalize run re-processes it.
- The userbot is the only message source (except fallback mode, when no userbot is configured or it failed
  to start: then `on_group_message` stores group messages itself). It only collects chats with
  `chats.tracked = 1`: groups the bot sees (`remember_chat`; bot removed → `0`) and chats used in
  `/p` / `/g`. Newly tracked chats are backfilled (`USERBOT_HISTORY_DAYS` first time,
  later from the last stored message sent before the userbot started / before the request).
- In groups there are no `/` commands, only `@bot` mentions routed by `route_mention`: `<summarize> [day]`,
  `<task> [name]` (keyword in angle brackets, case-insensitive), anything else is a Q&A question, empty or
  unknown `<keyword>` → `mention_help`. `/` commands exist only in the owner's DM (`p`, `g`,
  `e`, menu scoped to `BotCommandScopeChat(owner)`), checked offline → userbot configured →
  `effective_user.id == userbot.me.id`. Menu taps send an empty command, so an empty / name-only command
  shows pick buttons (`pick:<id>:<i>`, `on_pick_chat`). A chosen chat starts a topic (`start_session`:
  `prepare_chat` = track + backfill + compact pending days, once): `BotState.session` (RAM only, a single one,
  replaced by the next `/p`/`/g`, dropped by `/e`, restart, or `SESSION_TTL` = 1 h since the last message).
  Every plain owner message in the DM (`on_private_message`) is a question of that topic: `QA.answer(history=)`
  gets the last `MAX_SESSION_TURNS` (question, answer) pairs as text-only `ChatMessage`s, the answer ends with
  `SESSION_FOOTER`, and a turn is recorded only if the session is still current. Private chats are processed
  like groups (own tab).
- Commands, `@bot` mentions and the bot's own messages are never stored as conversation messages
  (`is_conversation` in the userbot, `~filters.COMMAND` + mention check in fallback), so an edit request can
  only take effect through the confirm button. Requests sent while the bot was offline
  (`msg.date < started_at`) get an "offline" reply instead of a late answer; edited mention messages are ignored.
- Blocking gspread / sheet calls run via `asyncio.to_thread`; per-chat lock serializes sheet writes.

## Tests

Tests never hit the network (nor keyring / registry: `tests/fakes_desktop.py`): `tests/test_sheets.FakeWorksheet`, `tests/test_sheet_client.FakeSpreadsheet`
and scripted `LLMProvider` subclasses stand in for gspread and the LLM; `tests/test_userbot.FakeUserbot` for
Telethon (the one Telethon-touching test uses `pytest.importorskip`). Use the `HEADER`-derived column
constants (`DEADLINE`, `PROGRESS`, ...) from `tests/test_sheets.py` instead of hard-coded indexes.

## Rules
Follow these rules for all BeeWare (Toga/Briefcase) code in this project: @docs/beeware-toga.md
Follow these rules for all desktop UI code (`telegram_bot/ui/`, UI parts of `telegram_bot/app.py`) to
avoid flicker / redundant relayouts: @docs/toga-ui-rules.md
In this project the helpers from that file are `set_text` / `set_input` / `set_style` in
`telegram_bot/ui/layout.py` (re-measure: `Page.refresh`); shared blocks are `Page`, `Card`, `FormRow`,
`ButtonBar`, `WrappedLabel`, `button` there; spacing / colors come from `ui/theme.py`. Views
(`StatusView`, `ConfigView`) take data; `app.py` swaps pages in `select_page` by assigning `page.box` to
the shared `self.scroller` (pages have no own ScrollContainer), `refresh_status` skips unchanged state,
`on_window_resize` updates only the visible page.

"""Lưu tin nhắn và bản tóm tắt theo ngày trong SQLite.

Bảng chats: tên chat gần nhất (đặt tên tab) và cờ tracked (userbot theo dõi / bổ sung tin của chat này).

Mỗi chat (nhóm hoặc chat riêng, chat_id) có:
- bảng messages: toàn bộ tin nhắn gốc, gắn nhãn ngày (day = YYYY-MM-DD theo múi giờ cấu hình).
  edit_pending = 1 nghĩa là tin đã được nén rồi nhưng sau đó bị sửa, cần đưa lại vào lần xử lý tới.
- bảng day_summaries: một dòng cho mỗi (chat_id, day), gồm bản tóm tắt và mốc compacted_until
  (id tin nhắn cuối cùng đã được nén vào tóm tắt)
- bảng task_meta: ngày giao / ngày cập nhật gần nhất của từng task (theo STT trên Google Sheet).
  Bản thân nội dung task nằm trên Google Sheet.
"""
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    updated_at  TEXT NOT NULL,
    tracked     INTEGER NOT NULL DEFAULT 0,
    username    TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    user_id       INTEGER,
    user_name     TEXT,
    text          TEXT NOT NULL,
    reply_to      INTEGER,
    sent_at       TEXT NOT NULL,
    day           TEXT NOT NULL,
    edit_pending  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_day ON messages (chat_id, day, id);

CREATE TABLE IF NOT EXISTS day_summaries (
    chat_id          INTEGER NOT NULL,
    day              TEXT NOT NULL,
    summary          TEXT NOT NULL DEFAULT '',
    tasks_json       TEXT NOT NULL DEFAULT '[]',  -- không còn dùng, task nằm trên Google Sheet
    compacted_until  INTEGER NOT NULL DEFAULT 0,
    finalized        INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (chat_id, day)
);

CREATE TABLE IF NOT EXISTS task_meta (
    chat_id      INTEGER NOT NULL,
    stt          INTEGER NOT NULL,
    created_day  TEXT NOT NULL,
    updated_day  TEXT NOT NULL,
    PRIMARY KEY (chat_id, stt)
);

-- Lần rà soát sheet gần nhất của mỗi nhóm: sheet không đổi kể từ đó thì không cần rà lại
CREATE TABLE IF NOT EXISTS sheet_reviews (
    chat_id      INTEGER PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    reviewed_at  TEXT NOT NULL
);
"""


def format_chat(chat_id: int, title: str | None, username: str | None) -> str:
    """Nhãn chat cho log: '"Team Dev" (@teamdev, -1001234)', thiếu username thì '"Team Dev" (-1001234)',
    chưa biết tên thì chỉ chat id."""
    if not title:
        return f"@{username} ({chat_id})" if username else str(chat_id)
    return f'"{title}" (@{username}, {chat_id})' if username else f'"{title}" ({chat_id})'


@dataclass
class Message:
    id: int
    chat_id: int
    message_id: int
    user_id: int | None
    user_name: str | None
    text: str
    reply_to: int | None
    sent_at: str
    day: str
    edit_pending: int = 0

    def format_line(self) -> str:
        time = self.sent_at[11:16]
        reply = f" (trả lời #{self.reply_to})" if self.reply_to else ""
        edited = " (đã sửa)" if self.edit_pending else ""
        return f"[#{self.message_id} {time}] {self.user_name or self.user_id}{reply}{edited}: {self.text}"


@dataclass
class DaySummary:
    chat_id: int
    day: str
    summary: str
    compacted_until: int
    finalized: bool


class Storage:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        """Nâng cấp DB tạo bởi phiên bản cũ."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if "edit_pending" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN edit_pending INTEGER NOT NULL DEFAULT 0")
        # Bảng sheet_tasks cũ (ghép task theo tên) -> task_meta (định danh theo STT)
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sheet_tasks'").fetchone():
            self.conn.execute(
                "INSERT OR IGNORE INTO task_meta (chat_id, stt, created_day, updated_day) "
                "SELECT chat_id, stt, MIN(day), MAX(day) FROM sheet_tasks GROUP BY chat_id, stt"
            )
            self.conn.execute("DROP TABLE sheet_tasks")
        # Cờ tracked (userbot): mọi chat bot đã biết từ trước đều được theo dõi
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(chats)")}
        if "tracked" not in cols:
            self.conn.execute("ALTER TABLE chats ADD COLUMN tracked INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE chats SET tracked = 1")
            self.conn.execute(
                "INSERT OR IGNORE INTO chats (chat_id, title, updated_at, tracked) "
                "SELECT DISTINCT chat_id, NULL, ?, 1 FROM messages",
                (datetime.now(timezone.utc).isoformat(),),
            )
        # Username của chat (nhóm công khai / người kia trong chat riêng), để log dễ đọc
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(chats)")}
        if "username" not in cols:
            self.conn.execute("ALTER TABLE chats ADD COLUMN username TEXT")
        self.conn.commit()

    def upsert_chat(self, chat_id: int, title: str | None, username: str | None = None) -> None:
        """Lưu tên chat; username bỏ trống thì giữ username cũ."""
        self.conn.execute(
            "INSERT INTO chats (chat_id, title, updated_at, username) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at, "
            "username = COALESCE(excluded.username, chats.username)",
            (chat_id, title, datetime.now(timezone.utc).isoformat(), username),
        )
        self.conn.commit()

    def track_chat(self, chat_id: int, title: str | None, username: str | None = None) -> bool:
        """Lưu tên chat (bỏ trống thì giữ tên / username cũ) và đánh dấu theo dõi. True nếu trước đó chưa theo dõi."""
        row = self.conn.execute("SELECT tracked FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        self.conn.execute(
            "INSERT INTO chats (chat_id, title, updated_at, tracked, username) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET title = COALESCE(excluded.title, chats.title), "
            "updated_at = excluded.updated_at, tracked = 1, username = COALESCE(excluded.username, chats.username)",
            (chat_id, title, datetime.now(timezone.utc).isoformat(), username),
        )
        self.conn.commit()
        return row is None or not row["tracked"]

    def untrack_chat(self, chat_id: int) -> None:
        self.conn.execute("UPDATE chats SET tracked = 0 WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    def tracked_chats(self) -> list[int]:
        return [r["chat_id"] for r in self.conn.execute("SELECT chat_id FROM chats WHERE tracked = 1")]

    def last_sent_at(self, chat_id: int, before: datetime | None = None) -> datetime | None:
        """Thời điểm gửi của tin mới nhất đã lưu (chỉ xét tin gửi trước `before` nếu có)."""
        if before is None:
            row = self.conn.execute("SELECT MAX(sent_at) AS t FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(sent_at) AS t FROM messages WHERE chat_id = ? AND sent_at < ?",
                (chat_id, before.isoformat()),
            ).fetchone()
        return datetime.fromisoformat(row["t"]) if row["t"] else None

    def pending_days(self, chat_id: int) -> list[str]:
        """Các ngày của chat còn tin chưa xử lý (chưa nén hoặc bị sửa sau khi nén), cũ trước."""
        rows = self.conn.execute(
            "SELECT DISTINCT m.day FROM messages m "
            "LEFT JOIN day_summaries s ON s.chat_id = m.chat_id AND s.day = m.day "
            "WHERE m.chat_id = ? AND (m.id > COALESCE(s.compacted_until, 0) OR m.edit_pending = 1) ORDER BY m.day",
            (chat_id,),
        ).fetchall()
        return [r["day"] for r in rows]

    def chat_title(self, chat_id: int) -> str | None:
        """Tên nhóm gần nhất bot thấy (cập nhật theo mỗi tin nhắn)."""
        row = self.conn.execute("SELECT title FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row["title"] if row else None

    def chat_label(self, chat_id: int) -> str:
        """Nhãn của chat cho log: tên, username (nếu có) và chat id (xem format_chat)."""
        row = self.conn.execute("SELECT title, username FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return format_chat(chat_id, row["title"] if row else None, row["username"] if row else None)

    def tracked_groups(self) -> list[tuple[int, str, str | None]]:
        """Các nhóm đang theo dõi (chat id âm), tên A-Z: (chat_id, tên, username)."""
        rows = self.conn.execute(
            "SELECT chat_id, title, username FROM chats WHERE tracked = 1 AND chat_id < 0 AND title IS NOT NULL "
            "ORDER BY title COLLATE NOCASE"
        ).fetchall()
        return [(r["chat_id"], r["title"], r["username"]) for r in rows]

    # ---- tin nhắn ----

    def add_message(
        self,
        chat_id: int,
        message_id: int,
        user_id: int | None,
        user_name: str | None,
        text: str,
        reply_to: int | None,
        sent_at: datetime,
    ) -> None:
        """sent_at phải là datetime đã đổi sang múi giờ cấu hình, để day đúng theo giờ địa phương.

        Tin của một ngày đã chốt (ví dụ Telegram gửi bù tin lúc bot tắt, sau khi ngày đó đã được chốt)
        sẽ mở lại ngày đó, để lần chốt kế tiếp xử lý tin này.
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(chat_id, message_id, user_id, user_name, text, reply_to, sent_at, day) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                message_id,
                user_id,
                user_name,
                text,
                reply_to,
                sent_at.isoformat(),
                sent_at.date().isoformat(),
            ),
        )
        if cur.rowcount:
            self.conn.execute(
                "UPDATE day_summaries SET finalized = 0 WHERE chat_id = ? AND day = ? AND finalized = 1",
                (chat_id, sent_at.date().isoformat()),
            )
        self.conn.commit()

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        user_id: int | None,
        user_name: str | None,
        text: str,
        reply_to: int | None,
        sent_at: datetime,
    ) -> None:
        """Cập nhật nội dung tin nhắn bị sửa.

        Nếu tin đã được nén vào tóm tắt, đánh dấu edit_pending để lần xử lý tới đưa lại vào.
        Nếu ngày của tin đã được chốt, mở lại ngày đó để lần chốt kế tiếp cập nhật.
        """
        row = self.conn.execute(
            "SELECT id, day, text FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ).fetchone()
        if row is not None and row["text"] == text:
            return  # chỉ đổi thứ khác nội dung (ví dụ reaction): không cần xử lý lại
        if row is None:
            self.add_message(chat_id, message_id, user_id, user_name, text, reply_to, sent_at)
            day = sent_at.date().isoformat()
        else:
            day = row["day"]
            s = self.get_summary(chat_id, day)
            self.conn.execute(
                "UPDATE messages SET text = ?, edit_pending = MAX(edit_pending, ?) WHERE id = ?",
                (text, int(row["id"] <= s.compacted_until), row["id"]),
            )
        self.conn.execute(
            "UPDATE day_summaries SET finalized = 0 WHERE chat_id = ? AND day = ? AND finalized = 1",
            (chat_id, day),
        )
        self.conn.commit()

    def pending_messages(self, chat_id: int, day: str, after_id: int) -> list[Message]:
        """Tin nhắn chưa nén (id > after_id) cộng với các tin đã nén nhưng bị sửa sau đó."""
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND day = ? AND (id > ? OR edit_pending = 1) ORDER BY id",
            (chat_id, day, after_id),
        ).fetchall()
        return [Message(**dict(r)) for r in rows]

    def clear_edit_pending(self, ids: list[int]) -> None:
        self.conn.executemany("UPDATE messages SET edit_pending = 0 WHERE id = ?", [(i,) for i in ids])
        self.conn.commit()

    def chats_with_messages_on(self, day: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT chat_id FROM messages WHERE day = ?", (day,)
        ).fetchall()
        return [r["chat_id"] for r in rows]

    def unfinalized_days_before(self, day: str) -> list[tuple[int, str]]:
        """Các (chat_id, day) trước ngày `day` có tin nhắn nhưng chưa được chốt tóm tắt."""
        rows = self.conn.execute(
            "SELECT DISTINCT m.chat_id, m.day FROM messages m "
            "LEFT JOIN day_summaries s ON s.chat_id = m.chat_id AND s.day = m.day "
            "WHERE m.day < ? AND COALESCE(s.finalized, 0) = 0 ORDER BY m.day",
            (day,),
        ).fetchall()
        return [(r["chat_id"], r["day"]) for r in rows]

    # ---- tóm tắt ----

    def get_summary(self, chat_id: int, day: str) -> DaySummary:
        row = self.conn.execute(
            "SELECT * FROM day_summaries WHERE chat_id = ? AND day = ?", (chat_id, day)
        ).fetchone()
        if row is None:
            return DaySummary(chat_id, day, "", 0, False)
        return DaySummary(
            chat_id=row["chat_id"],
            day=row["day"],
            summary=row["summary"],
            compacted_until=row["compacted_until"],
            finalized=bool(row["finalized"]),
        )

    def save_summary(self, s: DaySummary) -> None:
        self.conn.execute(
            "INSERT INTO day_summaries "
            "(chat_id, day, summary, compacted_until, finalized, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (chat_id, day) DO UPDATE SET summary = excluded.summary, "
            "compacted_until = excluded.compacted_until, "
            "finalized = excluded.finalized, updated_at = excluded.updated_at",
            (
                s.chat_id,
                s.day,
                s.summary,
                s.compacted_until,
                int(s.finalized),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    # ---- task (nội dung ở Google Sheet, ở đây chỉ lưu ngày) ----

    def record_task(self, chat_id: int, stt: int, day: str, created: bool = False) -> None:
        """Ghi nhận task STT được tạo (created=True) hoặc cập nhật vào ngày `day`.

        Tạo mới thì ghi đè ngày cũ: STT có thể được dùng lại nếu người dùng xoá dòng cuối trên sheet.
        """
        if created:
            self.conn.execute(
                "INSERT OR REPLACE INTO task_meta (chat_id, stt, created_day, updated_day) VALUES (?, ?, ?, ?)",
                (chat_id, stt, day, day),
            )
        else:
            self.conn.execute(
                "INSERT INTO task_meta (chat_id, stt, created_day, updated_day) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (chat_id, stt) DO UPDATE SET updated_day = MAX(updated_day, excluded.updated_day)",
                (chat_id, stt, day, day),
            )
        self.conn.commit()

    def chats_with_tasks(self) -> list[int]:
        return [r["chat_id"] for r in self.conn.execute("SELECT DISTINCT chat_id FROM task_meta")]

    def review_fingerprint(self, chat_id: int) -> str | None:
        row = self.conn.execute("SELECT fingerprint FROM sheet_reviews WHERE chat_id = ?", (chat_id,)).fetchone()
        return row["fingerprint"] if row else None

    def save_review(self, chat_id: int, fingerprint: str) -> None:
        self.conn.execute(
            "INSERT INTO sheet_reviews (chat_id, fingerprint, reviewed_at) VALUES (?, ?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET fingerprint = excluded.fingerprint, "
            "reviewed_at = excluded.reviewed_at",
            (chat_id, fingerprint, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def task_days(self, chat_id: int) -> dict[int, tuple[str, str]]:
        """STT -> (ngày giao, ngày cập nhật gần nhất)."""
        return {
            r["stt"]: (r["created_day"], r["updated_day"])
            for r in self.conn.execute(
                "SELECT stt, created_day, updated_day FROM task_meta WHERE chat_id = ?", (chat_id,)
            )
        }

"""Logos Messaging Layer — agent-to-agent async messaging."""

import json
import logging
import os
import sqlite3
import time
import uuid

logger = logging.getLogger("pulse.logos.messages")

DEFAULT_DB_PATH = os.path.expanduser("~/.pulse/logos/backlog.db")

CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  thread_id TEXT,
  priority INTEGER DEFAULT 3,
  read INTEGER DEFAULT 0,
  created_at REAL NOT NULL,
  read_at REAL
)
"""

SEED_MESSAGES = [
    dict(
        from_agent="vera",
        to_agent="mira",
        subject="Weather bot dashboard endpoint",
        body="Weather bot needs a dashboard endpoint — can you add `/status/summary` that returns current graduation stats as JSON? Needed for the SDCA integration.",
        priority=4,
    ),
    dict(
        from_agent="sage",
        to_agent="all",
        subject="Roadmap update — Gnosis critical path",
        body="Roadmap update: Gnosis Stripe live is the critical path this week. Everything else yields to it.",
        priority=5,
    ),
    dict(
        from_agent="mira",
        to_agent="lyra",
        subject="Anima Sprint 4 tweet variants needed",
        body="Anima Sprint 4 landing page is live. Need 3 tweet variants for the Echo demo — focused on the memory/pattern recognition feature.",
        priority=3,
    ),
]


class MessageStore:
    """SQLite-backed message store, shares DB with Logos tasks."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(CREATE_MESSAGES_TABLE)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def send_message(
        self,
        from_agent: str,
        to_agent: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        priority: int = 3,
    ) -> str:
        """Send a message. Returns message id."""
        msg_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO messages (id, from_agent, to_agent, subject, body, thread_id, priority, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, from_agent, to_agent, subject, body, thread_id, priority, time.time()),
        )
        self._conn.commit()
        logger.debug(f"Message {msg_id}: {from_agent} → {to_agent}: {subject}")
        return msg_id

    def get_inbox(self, agent: str, include_read: bool = False) -> list[dict]:
        """Get messages for an agent. Unread only by default."""
        query = "SELECT * FROM messages WHERE (to_agent = ? OR to_agent = 'all')"
        params: list = [agent]
        if not include_read:
            query += " AND read = 0"
        query += " ORDER BY priority DESC, created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def mark_read(self, message_id: str) -> bool:
        """Mark a message as read."""
        cursor = self._conn.execute(
            "UPDATE messages SET read = 1, read_at = ? WHERE id = ?",
            (time.time(), message_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_thread(self, thread_id: str) -> list[dict]:
        """Get full conversation thread sorted by created_at."""
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_messages(self, limit: int = 50) -> list[dict]:
        """Soma oversight — all messages across all agents."""
        rows = self._conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM messages").fetchone()
        return row["cnt"] == 0


def seed_messages(store: MessageStore | None = None) -> int:
    """Seed example messages if none exist."""
    s = store or MessageStore()
    if not s.is_empty():
        return 0
    for msg in SEED_MESSAGES:
        s.send_message(**msg)
    logger.info(f"Seeded {len(SEED_MESSAGES)} example messages")
    return len(SEED_MESSAGES)


# Module-level convenience functions
_default_store: MessageStore | None = None


def _get_store() -> MessageStore:
    global _default_store
    if _default_store is None:
        _default_store = MessageStore()
    return _default_store


def send_message(from_agent, to_agent, subject, body, thread_id=None, priority=3):
    return _get_store().send_message(from_agent, to_agent, subject, body, thread_id, priority)


def get_inbox(agent, include_read=False):
    return _get_store().get_inbox(agent, include_read)


def mark_read(message_id):
    return _get_store().mark_read(message_id)


def get_thread(thread_id):
    return _get_store().get_thread(thread_id)


def get_all_messages(limit=50):
    return _get_store().get_all_messages(limit)

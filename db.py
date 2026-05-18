"""SQLite persistence for entry state, media cache, and conversation history.

Replaces the in-memory pending_entries dict. Survives bot restarts so threads
can be resumed without re-downloading media or re-extracting from Gemini.
"""

import json
import logging
from datetime import datetime, timezone

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       INTEGER UNIQUE NOT NULL,
    message_url     TEXT NOT NULL DEFAULT '',
    original_text   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending_clarification',
    entries_json    TEXT NOT NULL DEFAULT '[]',
    confidence      REAL NOT NULL DEFAULT 0.0,
    questions_json  TEXT NOT NULL DEFAULT '[]',
    raw_summary     TEXT NOT NULL DEFAULT '',
    sheet_rows_json TEXT NOT NULL DEFAULT '[]',
    tab_name        TEXT NOT NULL DEFAULT 'Entries',
    user_id         INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL,
    media_type      TEXT NOT NULL,
    cached_text     TEXT NOT NULL,
    mime_type       TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_cache_message
    ON media_cache (message_id);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_messages_thread
    ON conversation_messages (thread_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create tables if they don't exist. Call once at startup."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(_SCHEMA)
        # Migration: add tab_name column if upgrading from older schema
        try:
            await conn.execute(
                "ALTER TABLE conversations ADD COLUMN tab_name TEXT NOT NULL DEFAULT 'Entries'"
            )
            await conn.commit()
            logger.info("op=init_db | Migrated: added tab_name column")
        except Exception:
            pass  # Column already exists
        # Migration: add user_id column (nullable — legacy rows have no value
        # and the @-mention will simply be skipped for them).
        try:
            await conn.execute(
                "ALTER TABLE conversations ADD COLUMN user_id INTEGER"
            )
            await conn.commit()
            logger.info("op=init_db | Migrated: added user_id column")
        except Exception:
            pass  # Column already exists
        await conn.commit()
    logger.info("op=init_db | Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

async def create_conversation(
    thread_id: int,
    message_url: str,
    original_text: str,
    entries: list[dict],
    confidence: float,
    questions: list[str],
    raw_summary: str,
    tab_name: str = "Entries",
    user_id: int | None = None,
) -> int:
    """Insert a new conversation row. Returns the conversation id.

    `user_id` is the Discord user ID of the person who posted the original
    message that started the thread. Stored so we can @-mention them when
    the bot needs their input.
    """
    now = _now()
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """
            INSERT INTO conversations
                (thread_id, message_url, original_text, status,
                 entries_json, confidence, questions_json, raw_summary,
                 tab_name, user_id, created_at, updated_at)
            VALUES (?, ?, ?, 'pending_clarification', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                message_url,
                original_text,
                json.dumps(entries),
                confidence,
                json.dumps(questions),
                raw_summary,
                tab_name,
                user_id,
                now,
                now,
            ),
        )
        await conn.commit()
        row_id = cursor.lastrowid
    logger.info(
        "thread=%d op=create_conversation | id=%d, tab=%s, user_id=%s",
        thread_id, row_id, tab_name, user_id,
    )
    return row_id


async def get_conversation(thread_id: int) -> dict | None:
    """Fetch a conversation by thread_id. Returns None if not found."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM conversations WHERE thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_conversation(row)


def _row_to_conversation(row: aiosqlite.Row) -> dict:
    """Convert a database row to a conversation dict with parsed JSON fields."""
    d = dict(row)
    d["entries"] = json.loads(d.pop("entries_json"))
    d["questions"] = json.loads(d.pop("questions_json"))
    d["sheet_row_numbers"] = json.loads(d.pop("sheet_rows_json"))
    return d


async def update_conversation_entries(
    thread_id: int,
    entries: list[dict],
    confidence: float,
    questions: list[str],
    raw_summary: str,
) -> None:
    """Replace the extraction result for a conversation."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            UPDATE conversations
            SET entries_json = ?, confidence = ?, questions_json = ?,
                raw_summary = ?, updated_at = ?
            WHERE thread_id = ?
            """,
            (
                json.dumps(entries),
                confidence,
                json.dumps(questions),
                raw_summary,
                _now(),
                thread_id,
            ),
        )
        await conn.commit()
    logger.debug(
        "thread=%d op=update_entries | %d entries, confidence=%.2f",
        thread_id, len(entries), confidence,
    )


async def update_conversation_entry_field(
    thread_id: int,
    entry_index: int,
    field_name: str,
    new_value: str | float | None,
) -> list[dict]:
    """Update a single field on one entry. Returns the updated entries list."""
    conv = await get_conversation(thread_id)
    if conv is None:
        raise ValueError(f"No conversation found for thread {thread_id}")

    entries = conv["entries"]
    if entry_index < 0 or entry_index >= len(entries):
        raise IndexError(
            f"entry_index {entry_index} out of range (have {len(entries)} entries)"
        )

    old_value = entries[entry_index].get(field_name)
    entries[entry_index][field_name] = new_value

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE conversations SET entries_json = ?, updated_at = ? WHERE thread_id = ?",
            (json.dumps(entries), _now(), thread_id),
        )
        await conn.commit()

    logger.info(
        "thread=%d op=field_update | entry[%d].%s: %r -> %r",
        thread_id, entry_index, field_name, old_value, new_value,
    )
    return entries


async def update_conversation_status(
    thread_id: int,
    status: str,
    sheet_row_numbers: list[int] | None = None,
) -> None:
    """Update the status (and optionally sheet row numbers) of a conversation."""
    fields = ["status = ?", "updated_at = ?"]
    params: list = [status, _now()]

    if sheet_row_numbers is not None:
        fields.append("sheet_rows_json = ?")
        params.append(json.dumps(sheet_row_numbers))

    params.append(thread_id)

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE conversations SET {', '.join(fields)} WHERE thread_id = ?",
            params,
        )
        await conn.commit()

    logger.info("thread=%d op=update_status | status=%s", thread_id, status)


async def update_conversation_questions(
    thread_id: int,
    questions: list[str],
) -> None:
    """Update the pending clarifying questions for a conversation."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE conversations SET questions_json = ?, updated_at = ? WHERE thread_id = ?",
            (json.dumps(questions), _now(), thread_id),
        )
        await conn.commit()


async def get_stuck_conversations(max_age_days: int = 14) -> list[dict]:
    """Find conversations that were created but never updated, meaning the
    bot died mid-pipeline (OOM kill, deploy, crash) after writing the row
    but before posting the response to Discord.

    Used by startup recovery to repost the saved bot response to each thread.
    Filters out conversations older than max_age_days to avoid surprising the
    user with stale messages.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """
            SELECT * FROM conversations
            WHERE status = 'pending_clarification'
              AND created_at = updated_at
              AND julianday('now') - julianday(created_at) < ?
            ORDER BY created_at ASC
            """,
            (max_age_days,),
        )
        rows = await cursor.fetchall()
    return [_row_to_conversation(row) for row in rows]


async def set_user_id(thread_id: int, user_id: int) -> None:
    """Backfill the user_id on a conversation that was created before the
    column existed (or for any other reason). One-shot: future-proofs the
    @-mention path without requiring a separate migration script.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE conversations SET user_id = ?, updated_at = ? WHERE thread_id = ?",
            (user_id, _now(), thread_id),
        )
        await conn.commit()
    logger.info(
        "thread=%d op=set_user_id | backfilled user_id=%s",
        thread_id, user_id,
    )


async def touch_conversation(thread_id: int) -> None:
    """Bump updated_at on a conversation without changing anything else.

    Used by startup recovery to mark a stuck conversation as 'recovered'
    (breaks the created_at == updated_at invariant we use to detect stuck rows).
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE thread_id = ?",
            (_now(), thread_id),
        )
        await conn.commit()


async def delete_conversation(thread_id: int) -> dict | None:
    """Delete a conversation row and its chat history, returning the deleted
    row (for audit logging) or None if it didn't exist.

    Used when the user asks to retry — we wipe the stale state so the fresh
    extraction can insert cleanly via `db.create_conversation`. The media_cache
    rows for the parent message_id are intentionally kept (they're keyed by
    message_id, not thread_id, and may be re-used).
    """
    existing = await get_conversation(thread_id)
    if existing is None:
        return None
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM conversation_messages WHERE thread_id = ?",
            (thread_id,),
        )
        await conn.execute(
            "DELETE FROM conversations WHERE thread_id = ?",
            (thread_id,),
        )
        await conn.commit()
    logger.info(
        "thread=%d op=delete_conversation | status was %s, %d sheet rows",
        thread_id, existing["status"], len(existing.get("sheet_row_numbers") or []),
    )
    return existing


async def get_last_bot_message(thread_id: int) -> str | None:
    """Return the most recent bot message content for a thread, or None."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """
            SELECT content FROM conversation_messages
            WHERE thread_id = ? AND role = 'bot'
            ORDER BY id DESC LIMIT 1
            """,
            (thread_id,),
        )
        row = await cursor.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Media cache
# ---------------------------------------------------------------------------

async def cache_media(
    message_id: int,
    media_type: str,
    cached_text: str,
    mime_type: str = "",
) -> None:
    """Store a transcription or image description for a media attachment."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO media_cache (message_id, media_type, cached_text, mime_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, media_type, cached_text, mime_type, _now()),
        )
        await conn.commit()
    logger.info(
        "message=%d op=cache_media | type=%s, %d chars cached",
        message_id, media_type, len(cached_text),
    )


async def get_cached_media(message_id: int) -> list[dict]:
    """Get all cached media for a message. Returns list of {media_type, cached_text, mime_type}."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT media_type, cached_text, mime_type FROM media_cache WHERE message_id = ?",
            (message_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Conversation messages (thread history for Gemini context)
# ---------------------------------------------------------------------------

async def add_message(thread_id: int, role: str, content: str) -> None:
    """Append a message to the conversation history for a thread."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO conversation_messages (thread_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (thread_id, role, content, _now()),
        )
        await conn.commit()


async def get_messages(thread_id: int) -> list[dict]:
    """Get all messages for a thread, ordered chronologically."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT role, content, created_at FROM conversation_messages "
            "WHERE thread_id = ? ORDER BY id ASC",
            (thread_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]

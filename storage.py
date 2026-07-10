import os
from datetime import datetime
from typing import List, Optional, Set

import aiosqlite

from config import DB_PATH

_connection: Optional[aiosqlite.Connection] = None


async def get_connection(db_path: str = DB_PATH) -> aiosqlite.Connection:
    global _connection
    if _connection is None:
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _connection = await aiosqlite.connect(db_path)
        _connection.row_factory = aiosqlite.Row
        await _connection.execute("PRAGMA journal_mode=WAL")
        await _connection.execute("PRAGMA foreign_keys=ON")
    return _connection


async def close_connection():
    global _connection
    if _connection:
        await _connection.close()
        _connection = None


async def _create_schema(db: aiosqlite.Connection):
    statements = (
        """
        CREATE TABLE IF NOT EXISTS active_collection (
            chat_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS collection_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            username TEXT,
            display_name TEXT,
            paid INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES active_collection(chat_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_collection_members_chat_user
            ON collection_members(chat_id, user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_collection_members_chat_username
            ON collection_members(chat_id, username)
        """,
        """
        CREATE TABLE IF NOT EXISTS chat_members (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, chat_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_chat_members_username
            ON chat_members(username)
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        await db.execute(statement)


async def _migrate_legacy_collection_schema(db: aiosqlite.Connection):
    """Migrate the old single global collection into a collection per chat."""
    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        await db.execute("BEGIN")
        await db.execute("ALTER TABLE active_collection RENAME TO active_collection_legacy")
        await db.execute("ALTER TABLE collection_members RENAME TO collection_members_legacy")
        await _create_schema(db)
        await db.execute(
            """
            INSERT INTO active_collection (chat_id, message_id, created_at)
            SELECT chat_id, message_id, created_at
            FROM active_collection_legacy
            """
        )
        await db.execute(
            """
            INSERT INTO collection_members (chat_id, user_id, username, display_name, paid)
            SELECT ac.chat_id, cm.user_id, cm.username, cm.display_name, cm.paid
            FROM collection_members_legacy AS cm
            JOIN active_collection_legacy AS ac ON ac.id = cm.collection_id
            """
        )
        await db.execute("DROP TABLE collection_members_legacy")
        await db.execute("DROP TABLE active_collection_legacy")
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.execute("PRAGMA foreign_keys=ON")


async def init_db(db_path: str = DB_PATH):
    db = await get_connection(db_path)
    cursor = await db.execute("PRAGMA table_info(active_collection)")
    columns = {row[1] for row in await cursor.fetchall()}

    if "id" in columns and "chat_id" in columns:
        await _migrate_legacy_collection_schema(db)
    else:
        await _create_schema(db)
        await db.commit()


# --- bot_settings ---

async def get_setting(db: aiosqlite.Connection, key: str) -> Optional[str]:
    cursor = await db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_setting(db: aiosqlite.Connection, key: str, value: str):
    await db.execute(
        """
        INSERT INTO bot_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, datetime.now().isoformat()),
    )
    await db.commit()


# --- chat_members ---

async def upsert_chat_member(
    db: aiosqlite.Connection,
    user_id: int,
    chat_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
):
    now = datetime.now().isoformat()
    await db.execute(
        """
        INSERT INTO chat_members (user_id, chat_id, username, first_name, last_name, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, chat_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            updated_at = excluded.updated_at
        """,
        (user_id, chat_id, username, first_name, last_name, now),
    )
    await db.commit()


async def find_members_by_name(db: aiosqlite.Connection, chat_id: int, name_part: str) -> List[dict]:
    if len(name_part) < 3:
        return []
    like = f"%{name_part.lower()}%"
    cursor = await db.execute(
        """
        SELECT user_id, username, first_name, last_name
        FROM chat_members
        WHERE chat_id = ?
          AND (
              LOWER(COALESCE(username, '')) LIKE ?
              OR LOWER(COALESCE(first_name, '')) LIKE ?
              OR LOWER(COALESCE(last_name, '')) LIKE ?
          )
        """,
        (chat_id, like, like, like),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_member_by_username(db: aiosqlite.Connection, chat_id: int, username: str) -> Optional[dict]:
    cursor = await db.execute(
        """
        SELECT user_id, username, first_name, last_name
        FROM chat_members
        WHERE chat_id = ? AND LOWER(COALESCE(username, '')) = ?
        """,
        (chat_id, username.lower()),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def find_user_id_by_username(db: aiosqlite.Connection, username: str) -> Optional[int]:
    """Resolve a configured username from history, including records from older chats."""
    cursor = await db.execute(
        """
        SELECT user_id
        FROM chat_members
        WHERE LOWER(COALESCE(username, '')) = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (username.lower(),),
    )
    row = await cursor.fetchone()
    return row["user_id"] if row else None


# --- active collections, scoped by chat ---

async def create_collection(db: aiosqlite.Connection, message_id: int, chat_id: int):
    await db.execute("DELETE FROM collection_members WHERE chat_id = ?", (chat_id,))
    await db.execute(
        """
        INSERT INTO active_collection (chat_id, message_id, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            message_id = excluded.message_id,
            created_at = excluded.created_at
        """,
        (chat_id, message_id, datetime.now().isoformat()),
    )
    await db.commit()


async def get_active_collection(db: aiosqlite.Connection, chat_id: int) -> Optional[dict]:
    cursor = await db.execute("SELECT * FROM active_collection WHERE chat_id = ?", (chat_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_active_collections(db: aiosqlite.Connection) -> List[dict]:
    cursor = await db.execute("SELECT * FROM active_collection ORDER BY created_at")
    return [dict(row) for row in await cursor.fetchall()]


async def add_collection_member(
    db: aiosqlite.Connection,
    chat_id: int,
    user_id: Optional[int],
    username: Optional[str],
    display_name: Optional[str],
):
    await db.execute(
        """
        INSERT INTO collection_members (chat_id, user_id, username, display_name, paid)
        VALUES (?, ?, ?, ?, 0)
        """,
        (chat_id, user_id, username, display_name),
    )
    await db.commit()


async def get_collection_member_by_user_id(
    db: aiosqlite.Connection, chat_id: int, user_id: int
) -> Optional[dict]:
    cursor = await db.execute(
        "SELECT * FROM collection_members WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_collection_member_by_username(
    db: aiosqlite.Connection, chat_id: int, username: str
) -> Optional[dict]:
    cursor = await db.execute(
        """
        SELECT * FROM collection_members
        WHERE chat_id = ? AND LOWER(COALESCE(username, '')) = ?
        """,
        (chat_id, username.lower()),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_paid(db: aiosqlite.Connection, member_id: int, paid: bool):
    await db.execute("UPDATE collection_members SET paid = ? WHERE id = ?", (1 if paid else 0, member_id))
    await db.commit()


async def set_member_user_id(db: aiosqlite.Connection, member_id: int, user_id: int):
    await db.execute("UPDATE collection_members SET user_id = ? WHERE id = ?", (user_id, member_id))
    await db.commit()


async def restore_paid_members(
    db: aiosqlite.Connection,
    chat_id: int,
    user_ids: Set[int],
    usernames: Set[str],
):
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        await db.execute(
            f"UPDATE collection_members SET paid = 1 WHERE chat_id = ? AND user_id IN ({placeholders})",
            (chat_id, *user_ids),
        )
    if usernames:
        placeholders = ",".join("?" for _ in usernames)
        await db.execute(
            f"""
            UPDATE collection_members SET paid = 1
            WHERE chat_id = ? AND LOWER(COALESCE(username, '')) IN ({placeholders})
            """,
            (chat_id, *sorted(name.lower() for name in usernames)),
        )
    await db.commit()


async def get_unpaid_members(db: aiosqlite.Connection, chat_id: int) -> List[dict]:
    cursor = await db.execute(
        "SELECT * FROM collection_members WHERE chat_id = ? AND paid = 0",
        (chat_id,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_all_collection_members(db: aiosqlite.Connection, chat_id: int) -> List[dict]:
    cursor = await db.execute("SELECT * FROM collection_members WHERE chat_id = ?", (chat_id,))
    return [dict(row) for row in await cursor.fetchall()]


async def clear_collection(db: aiosqlite.Connection, chat_id: int):
    await db.execute("DELETE FROM active_collection WHERE chat_id = ?", (chat_id,))
    await db.commit()

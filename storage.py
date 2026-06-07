import os
import aiosqlite
from datetime import datetime
from typing import Optional, List

from config import DB_PATH

_connection: Optional[aiosqlite.Connection] = None


async def get_connection(db_path: str = DB_PATH) -> aiosqlite.Connection:
    global _connection
    if _connection is None:
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
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


async def init_db(db_path: str = DB_PATH):
    db = await get_connection(db_path)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS active_collection (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            message_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS collection_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id INTEGER NOT NULL DEFAULT 1,
            user_id INTEGER,
            username TEXT,
            display_name TEXT,
            paid INTEGER NOT NULL DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS chat_members (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
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
        "SELECT user_id, username, first_name, last_name FROM chat_members WHERE chat_id = ? AND username = ?",
        (chat_id, username),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


# --- active_collection ---

async def create_collection(db: aiosqlite.Connection, message_id: int, chat_id: int):
    await db.execute("DELETE FROM collection_members WHERE collection_id = 1")
    await db.execute("DELETE FROM active_collection WHERE id = 1")
    await db.execute(
        "INSERT INTO active_collection (id, message_id, chat_id, created_at) VALUES (1, ?, ?, ?)",
        (message_id, chat_id, datetime.now().isoformat()),
    )
    await db.commit()


async def get_active_collection(db: aiosqlite.Connection) -> Optional[dict]:
    cursor = await db.execute("SELECT * FROM active_collection WHERE id = 1")
    row = await cursor.fetchone()
    return dict(row) if row else None


async def add_collection_member(
    db: aiosqlite.Connection,
    user_id: Optional[int],
    username: Optional[str],
    display_name: Optional[str],
):
    await db.execute(
        "INSERT INTO collection_members (collection_id, user_id, username, display_name, paid) VALUES (1, ?, ?, ?, 0)",
        (user_id, username, display_name),
    )
    await db.commit()


async def get_collection_member_by_user_id(db: aiosqlite.Connection, user_id: int) -> Optional[dict]:
    cursor = await db.execute(
        "SELECT * FROM collection_members WHERE collection_id = 1 AND user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_collection_member_by_username(db: aiosqlite.Connection, username: str) -> Optional[dict]:
    cursor = await db.execute(
        "SELECT * FROM collection_members WHERE collection_id = 1 AND username = ?",
        (username,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_paid(db: aiosqlite.Connection, member_id: int, paid: bool):
    await db.execute(
        "UPDATE collection_members SET paid = ? WHERE id = ?",
        (1 if paid else 0, member_id),
    )
    await db.commit()


async def set_member_user_id(db: aiosqlite.Connection, member_id: int, user_id: int):
    await db.execute(
        "UPDATE collection_members SET user_id = ? WHERE id = ?",
        (user_id, member_id),
    )
    await db.commit()


async def get_unpaid_members(db: aiosqlite.Connection) -> List[dict]:
    cursor = await db.execute("SELECT * FROM collection_members WHERE collection_id = 1 AND paid = 0")
    return [dict(row) for row in await cursor.fetchall()]


async def get_all_collection_members(db: aiosqlite.Connection) -> List[dict]:
    cursor = await db.execute("SELECT * FROM collection_members WHERE collection_id = 1")
    return [dict(row) for row in await cursor.fetchall()]


async def clear_collection(db: aiosqlite.Connection):
    await db.execute("DELETE FROM collection_members WHERE collection_id = 1")
    await db.execute("DELETE FROM active_collection WHERE id = 1")
    await db.commit()

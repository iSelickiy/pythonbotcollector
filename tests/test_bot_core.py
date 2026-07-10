import inspect
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

os.environ.setdefault("BOT_TOKEN", "123456:test-token")

from telegram import Chat, Message, MessageEntity, User

import collector
from bot import on_error
from storage import (
    add_collection_member,
    clear_collection,
    close_connection,
    create_collection,
    get_active_collection,
    get_all_collection_members,
    get_connection,
    get_setting,
    init_db,
    mark_paid,
    upsert_chat_member,
)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _message_with_mention(
    *, message_id: int, chat_id: int, sender: User, text: str, mention: str
) -> Message:
    start = text.index(mention)
    entity = MessageEntity(
        type=MessageEntity.MENTION,
        offset=_utf16_length(text[:start]),
        length=_utf16_length(mention),
    )
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(chat_id, Chat.SUPERGROUP),
        from_user=sender,
        text=text,
        entities=(entity,),
    )


class BotCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "state.db")
        await close_connection()
        await init_db(self.db_path)
        self.db = await get_connection()
        self.original_organizer_id = collector.ORGANIZER_ID
        self.original_organizer_username = collector.ORGANIZER_USERNAME
        self.original_cached_id = collector._cached_organizer_id

    async def asyncTearDown(self):
        collector.ORGANIZER_ID = self.original_organizer_id
        collector.ORGANIZER_USERNAME = self.original_organizer_username
        collector._cached_organizer_id = self.original_cached_id
        await close_connection()
        self.temp_dir.cleanup()

    async def test_collections_are_independent_per_chat(self):
        await create_collection(self.db, message_id=10, chat_id=-1001)
        await create_collection(self.db, message_id=20, chat_id=-1002)
        await add_collection_member(self.db, -1001, 1, "one", "One")
        await add_collection_member(self.db, -1002, 2, "two", "Two")

        await clear_collection(self.db, -1001)

        self.assertIsNone(await get_active_collection(self.db, -1001))
        self.assertEqual((await get_active_collection(self.db, -1002))["message_id"], 20)
        members = await get_all_collection_members(self.db, -1002)
        self.assertEqual([member["user_id"] for member in members], [2])

    async def test_organizer_id_is_learned_from_username_history(self):
        collector.ORGANIZER_ID = 0
        collector.ORGANIZER_USERNAME = "old_username"
        collector._cached_organizer_id = None
        await upsert_chat_member(self.db, 137796019, -1001, "old_username", "Organizer", None)

        current_user = User(
            id=137796019,
            first_name="Organizer",
            is_bot=False,
            username="new_username",
        )

        self.assertTrue(await collector.is_organizer(self.db, current_user))
        self.assertEqual(await get_setting(self.db, "organizer_id"), "137796019")

    async def test_configured_organizer_id_replaces_stale_saved_value(self):
        collector.ORGANIZER_ID = 137796019
        collector._cached_organizer_id = 198523080
        await collector.set_setting(self.db, "organizer_id", "198523080")

        organizer_id = await collector.get_organizer_id(self.db)

        self.assertEqual(organizer_id, 137796019)
        self.assertEqual(await get_setting(self.db, "organizer_id"), "137796019")

    async def test_unicode_before_mention_is_parsed_correctly(self):
        chat_id = -1001
        await create_collection(self.db, message_id=10, chat_id=chat_id)
        await upsert_chat_member(self.db, 42, chat_id, "PlayerCase", "Player", None)
        sender = User(id=7, first_name="Organizer", is_bot=False, username="organizer")
        text = "⚽️ Сбор: @PlayerCase https://tbank.ru/example"
        message = _message_with_mention(
            message_id=10,
            chat_id=chat_id,
            sender=sender,
            text=text,
            mention="@PlayerCase",
        )

        count = await collector.extract_and_store_users(self.db, message, chat_id)

        self.assertEqual(count, 1)
        members = await get_all_collection_members(self.db, chat_id)
        self.assertEqual(members[0]["user_id"], 42)
        self.assertEqual(members[0]["username"], "playercase")

    async def test_editing_same_collection_preserves_paid_state(self):
        chat_id = -1001
        await upsert_chat_member(self.db, 42, chat_id, "player", "Player", None)
        sender = User(id=7, first_name="Organizer", is_bot=False, username="organizer")
        original = _message_with_mention(
            message_id=10,
            chat_id=chat_id,
            sender=sender,
            text="Сбор @player https://tbank.ru/example",
            mention="@player",
        )
        edited = _message_with_mention(
            message_id=10,
            chat_id=chat_id,
            sender=sender,
            text="⚽ Сбор @player https://tbank.ru/example",
            mention="@player",
        )

        await collector.handle_collection_message(original, chat_id)
        member = (await get_all_collection_members(self.db, chat_id))[0]
        await mark_paid(self.db, member["id"], True)
        await collector.handle_collection_message(edited, chat_id)

        members = await get_all_collection_members(self.db, chat_id)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["paid"], 1)

    def test_error_handler_is_async(self):
        self.assertTrue(inspect.iscoroutinefunction(on_error))


class LegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_single_collection_is_migrated(self):
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "legacy.db")
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE active_collection (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE collection_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL DEFAULT 1,
                user_id INTEGER,
                username TEXT,
                display_name TEXT,
                paid INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE chat_members (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, chat_id)
            );
            INSERT INTO active_collection VALUES (1, 99, -1001, '2026-01-01T00:00:00');
            INSERT INTO collection_members
                (collection_id, user_id, username, display_name, paid)
            VALUES (1, 42, 'player', 'Player', 1);
            """
        )
        connection.commit()
        connection.close()

        await close_connection()
        try:
            await init_db(db_path)
            db = await get_connection()
            collection = await get_active_collection(db, -1001)
            members = await get_all_collection_members(db, -1001)
            self.assertEqual(collection["message_id"], 99)
            self.assertEqual(members[0]["user_id"], 42)
            self.assertEqual(members[0]["paid"], 1)
        finally:
            await close_connection()
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()

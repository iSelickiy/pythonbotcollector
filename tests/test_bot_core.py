import inspect
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("BOT_TOKEN", "123456:test-token")

from telegram import Chat, Message, MessageEntity, Update, User

import collector
from bot import on_error, on_message
from storage import (
    add_collection_member,
    clear_collection,
    close_connection,
    create_collection,
    find_members_by_name,
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


ORGANIZER = User(id=7, first_name="Organizer", is_bot=False, username="organizer")


def _message_with_mentions(
    *, text: str, message_id: int = 10, chat_id: int = -1001, sender: User = ORGANIZER, extra_entities=()
) -> Message:
    """A message with every @username tagged as a mention, the way Telegram does it."""
    mentions = tuple(
        MessageEntity(
            type=MessageEntity.MENTION,
            offset=_utf16_length(text[: match.start()]),
            length=_utf16_length(match.group()),
        )
        for match in re.finditer(r"@\w+", text)
    )
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(chat_id, Chat.SUPERGROUP),
        from_user=sender,
        text=text,
        entities=mentions + tuple(extra_entities),
    )


# The organizer's weekly message, with made-up names, phone and link code.
COLLECTION_TEMPLATE = (
    "@player_one @player_two (+1 передай, пожалуйста) @player_three @Player_Four \n\n"
    "С вас по 410 рублей по ссылке или по номеру +79990000000 на Тинек/Сбер:\n"
    "https://t.tb.ru/pm_short/AbCdEf12345\n\n"
    "Перевели — поставьте плюс. Нет плюса — нет перевода!"
)
TEMPLATE_USERNAMES = {"player_one", "player_two", "player_three", "player_four"}


class PaymentDetailsTests(unittest.TestCase):
    def test_recognizes_the_weekly_message_with_a_t_bank_short_link(self):
        self.assertTrue(collector.has_payment_details(_message_with_mentions(text=COLLECTION_TEMPLATE)))

    def test_recognizes_every_t_bank_domain(self):
        for link in (
            "https://t.tb.ru/pm_short/AbCdEf12345",
            "HTTPS://T.TB.RU/pm_short/AbCdEf12345",
            "https://www.tbank.ru/cf/AbCdEf12345",
            "tbank.ru/cf/AbCdEf12345",
            "https://www.tinkoff.ru/rm/ivanov.ivan1/AbCdE12345",
        ):
            with self.subTest(link=link):
                message = _message_with_mentions(text=f"@player_one скидываемся: {link}")
                self.assertTrue(collector.has_payment_details(message))

    def test_recognizes_a_link_hidden_behind_text(self):
        text = "@player_one скиньте по ссылке"
        link = MessageEntity(
            type=MessageEntity.TEXT_LINK,
            offset=_utf16_length(text[: text.index("ссылке")]),
            length=_utf16_length("ссылке"),
            url="https://t.tb.ru/pm_short/AbCdEf12345",
        )

        self.assertTrue(collector.has_payment_details(_message_with_mentions(text=text, extra_entities=(link,))))
        self.assertFalse(collector.has_payment_details(_message_with_mentions(text=text)))

    def test_recognizes_a_phone_number_with_an_amount_without_a_t_bank_link(self):
        for text in (
            COLLECTION_TEMPLATE.replace("https://t.tb.ru/", "https://pay.example.com/"),
            "@player_one с вас по 410 рублей по номеру +7 999 000-00-00",
            "@player_one скиньте 410₽ на 8 (999) 000-00-00",
        ):
            with self.subTest(text=text):
                self.assertTrue(collector.has_payment_details(_message_with_mentions(text=text)))

    def test_ignores_messages_that_do_not_say_where_to_pay(self):
        for text in (
            "@player_one глянь https://stb.ru/cf/AbCdEf12345",
            "@player_one глянь https://tbank.ru.example.com/cf/AbCdEf12345",
            "@player_one бутсы за 4100 рублей https://market.example.com/boots",
            "@player_one поле в пятницу, по 410 рублей с человека",
            "@player_one мой номер +79990000000, звони",
        ):
            with self.subTest(text=text):
                self.assertFalse(collector.has_payment_details(_message_with_mentions(text=text)))


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

        participants = await collector.extract_participants(self.db, message, chat_id)

        self.assertEqual(participants, [(42, "playercase", "Player")])

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

    async def test_status_prefers_username_tag_over_display_name(self):
        chat_id = -1001
        await create_collection(self.db, message_id=10, chat_id=chat_id)
        await add_collection_member(self.db, chat_id, 42, "playercase", "Vasily")

        text = await collector.get_status_text(self.db, chat_id)

        self.assertIn("@playercase", text)
        self.assertNotIn("Vasily", text)

    async def test_handle_collection_message_reports_whether_it_started_a_new_collection(self):
        chat_id = -1001
        sender = User(id=7, first_name="Organizer", is_bot=False, username="organizer")
        message = _message_with_mention(
            message_id=10,
            chat_id=chat_id,
            sender=sender,
            text="Сбор @player https://tbank.ru/example",
            mention="@player",
        )

        self.assertEqual(await collector.handle_collection_message(message, chat_id), collector.COLLECTION_CREATED)
        self.assertEqual(await collector.handle_collection_message(message, chat_id), collector.COLLECTION_UPDATED)

    async def test_weekly_message_starts_a_collection_for_everyone_it_mentions(self):
        message = _message_with_mentions(text=COLLECTION_TEMPLATE)

        self.assertEqual(await collector.handle_collection_message(message, -1001), collector.COLLECTION_CREATED)

        members = await get_all_collection_members(self.db, -1001)
        self.assertEqual({member["username"] for member in members}, TEMPLATE_USERNAMES)

    async def test_payment_details_naming_nobody_leave_the_active_collection_alone(self):
        await collector.handle_collection_message(_message_with_mentions(text=COLLECTION_TEMPLATE), -1001)
        member = (await get_all_collection_members(self.db, -1001))[0]
        await mark_paid(self.db, member["id"], True)
        answer = _message_with_mentions(message_id=11, text="Сюда: https://t.tb.ru/pm_short/AbCdEf12345")

        self.assertIsNone(await collector.handle_collection_message(answer, -1001))

        self.assertEqual((await get_active_collection(self.db, -1001))["message_id"], 10)
        members = await get_all_collection_members(self.db, -1001)
        self.assertEqual(len(members), len(TEMPLATE_USERNAMES))
        self.assertEqual(sum(member["paid"] for member in members), 1)

    def test_collection_started_message_is_one_of_the_configured_phrases(self):
        self.assertIn(collector.get_collection_started_message(), collector.COLLECTION_STARTED_PHRASES)

    async def test_build_reminder_text_includes_stage_phrase_and_message_link(self):
        chat_id = -1002102186488
        await create_collection(self.db, message_id=9430, chat_id=chat_id)
        await add_collection_member(self.db, chat_id, 42, "unpaidguy", "Unpaid Guy")

        text = await collector.build_reminder_text(self.db, chat_id, 9430, collector.STAGE_GENTLE)

        self.assertIn("@unpaidguy", text)
        self.assertIn("https://t.me/c/2102186488/9430", text)
        self.assertTrue(any(phrase in text for phrase in collector.REMINDER_PHRASES[collector.STAGE_GENTLE]))

    async def test_build_reminder_text_final_stage_mentions_organizer(self):
        collector.ORGANIZER_USERNAME = "seankalejs"
        chat_id = -1002102186488
        await create_collection(self.db, message_id=9430, chat_id=chat_id)
        await add_collection_member(self.db, chat_id, 42, "unpaidguy", "Unpaid Guy")

        text = await collector.build_reminder_text(self.db, chat_id, 9430, collector.STAGE_FINAL)

        self.assertIn("@seankalejs", text)
        self.assertIn("@unpaidguy", text)
        self.assertNotIn("{organizer}", text)

    def test_build_message_link_strips_supergroup_prefix(self):
        self.assertEqual(
            collector.build_message_link(-1002102186488, 9430),
            "https://t.me/c/2102186488/9430",
        )

    def test_build_message_link_returns_none_for_a_private_chat(self):
        self.assertIsNone(collector.build_message_link(137796019, 10))

    def test_reminder_message_is_one_of_the_configured_phrases_for_stage(self):
        self.assertIn(
            collector.get_reminder_message(collector.STAGE_GENTLE),
            collector.REMINDER_PHRASES[collector.STAGE_GENTLE],
        )

    async def test_find_members_by_name_requires_exact_match_not_substring(self):
        chat_id = -1001
        await upsert_chat_member(self.db, 42, chat_id, "vvp969", "Филипп", "Москалёв")

        exact = await find_members_by_name(self.db, chat_id, "филипп")
        self.assertEqual([m["user_id"] for m in exact], [42])

        # "или" is a common word that happens to appear inside "Филипп" —
        # it must not match, or an uninvolved person gets pulled into a collection.
        substring_collision = await find_members_by_name(self.db, chat_id, "или")
        self.assertEqual(substring_collision, [])

    async def test_extract_participants_ignores_common_word_inside_a_name(self):
        chat_id = -1001
        await upsert_chat_member(self.db, 42, chat_id, "vvp969", "Филипп", "Москалёв")
        sender = User(id=7, first_name="Organizer", is_bot=False, username="organizer")
        message = Message(
            message_id=10,
            date=datetime.now(timezone.utc),
            chat=Chat(chat_id, Chat.SUPERGROUP),
            from_user=sender,
            text="Сбор в четверг или в пятницу, как получится https://tbank.ru/example",
        )

        self.assertEqual(await collector.extract_participants(self.db, message, chat_id), [])

    async def test_send_reminder_sends_nag_text_when_someone_unpaid(self):
        chat_id = -1002102186488
        await create_collection(self.db, message_id=9430, chat_id=chat_id)
        await add_collection_member(self.db, chat_id, 42, "unpaidguy", "Unpaid Guy")

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await collector.send_reminder(context, stage=collector.STAGE_GENTLE)

        sent_text = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn("@unpaidguy", sent_text)
        self.assertIsNotNone(await get_active_collection(self.db, chat_id))

    def test_get_all_paid_message_before_930_msk_wishes_a_good_game(self):
        before_cutoff = datetime(2026, 7, 24, 9, 29, tzinfo=collector._MSK)
        self.assertIn(collector.get_all_paid_message(before_cutoff), collector.ALL_PAID_PHRASES_WITH_WISH)

    def test_get_all_paid_message_at_or_after_930_msk_is_plain_thanks(self):
        at_cutoff = datetime(2026, 7, 24, 9, 30, tzinfo=collector._MSK)
        self.assertIn(collector.get_all_paid_message(at_cutoff), collector.ALL_PAID_PHRASES_NO_WISH)

    async def test_send_reminder_announces_completion_as_a_fallback_when_all_paid(self):
        chat_id = -1002102186488
        await create_collection(self.db, message_id=9430, chat_id=chat_id)
        await add_collection_member(self.db, chat_id, 42, "paidguy", "Paid Guy")
        member = (await get_all_collection_members(self.db, chat_id))[0]
        await mark_paid(self.db, member["id"], True)

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await collector.send_reminder(context, stage=collector.STAGE_FIRM)

        sent_text = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn(sent_text, collector.ALL_PAID_PHRASES_WITH_WISH + collector.ALL_PAID_PHRASES_NO_WISH)
        self.assertIsNone(await get_active_collection(self.db, chat_id))

    async def test_handle_reaction_update_announces_completion_when_last_member_pays(self):
        chat_id = -1002102186488
        await create_collection(self.db, message_id=9430, chat_id=chat_id)
        await add_collection_member(self.db, chat_id, 42, "lastguy", "Last Guy")
        user = User(id=42, first_name="Last", is_bot=False, username="lastguy")

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await collector.handle_reaction_update(
            context=context,
            chat_id=chat_id,
            message_id=9430,
            user=user,
            new_reaction=["👍"],
            old_reaction=[],
        )

        context.bot.send_message.assert_called_once()
        sent_text = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn(sent_text, collector.ALL_PAID_PHRASES_WITH_WISH + collector.ALL_PAID_PHRASES_NO_WISH)
        self.assertIsNone(await get_active_collection(self.db, chat_id))

    async def test_handle_reaction_update_does_not_announce_while_someone_still_unpaid(self):
        chat_id = -1002102186488
        await create_collection(self.db, message_id=9430, chat_id=chat_id)
        await add_collection_member(self.db, chat_id, 42, "payer", "Payer")
        await add_collection_member(self.db, chat_id, 43, "stillowes", "Still Owes")
        user = User(id=42, first_name="Payer", is_bot=False, username="payer")

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await collector.handle_reaction_update(
            context=context,
            chat_id=chat_id,
            message_id=9430,
            user=user,
            new_reaction=["👍"],
            old_reaction=[],
        )

        context.bot.send_message.assert_not_called()
        self.assertIsNotNone(await get_active_collection(self.db, chat_id))

    async def test_send_reminder_sends_nothing_once_collection_already_cleared(self):
        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await collector.send_reminder(context, stage=collector.STAGE_FIRM)

        context.bot.send_message.assert_not_called()

    async def _send_as_organizer(self, message: Message, *, edited: bool = False) -> MagicMock:
        """Run an organizer's message through on_message; returns the bot, to check replies."""
        collector.ORGANIZER_ID = ORGANIZER.id
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.defaults = None
        message.set_bot(bot)
        update = Update(update_id=1, edited_message=message) if edited else Update(update_id=1, message=message)
        await on_message(update, MagicMock())
        return bot

    async def test_organizer_posting_the_weekly_message_starts_a_collection(self):
        bot = await self._send_as_organizer(_message_with_mentions(text=COLLECTION_TEMPLATE))

        bot.send_message.assert_called_once()
        self.assertIn(bot.send_message.call_args.kwargs["text"], collector.COLLECTION_STARTED_PHRASES)
        self.assertEqual((await get_active_collection(self.db, -1001))["message_id"], 10)

    async def test_organizer_answering_with_just_the_link_starts_nothing(self):
        await self._send_as_organizer(_message_with_mentions(text=COLLECTION_TEMPLATE))

        bot = await self._send_as_organizer(
            _message_with_mentions(message_id=11, text="Сюда: https://t.tb.ru/pm_short/AbCdEf12345")
        )

        bot.send_message.assert_not_called()
        self.assertEqual((await get_active_collection(self.db, -1001))["message_id"], 10)

    async def test_editing_the_payment_details_out_of_the_collection_message_clears_it(self):
        await self._send_as_organizer(_message_with_mentions(text=COLLECTION_TEMPLATE))

        await self._send_as_organizer(
            _message_with_mentions(text="@player_one @player_two игра отменяется"), edited=True
        )

        self.assertIsNone(await get_active_collection(self.db, -1001))

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

import html
import logging
import re
import unicodedata
from typing import Optional, Set

from telegram import Message, User
from telegram.ext import ContextTypes

from config import ORGANIZER_ID, ORGANIZER_USERNAME
from storage import (
    add_collection_member,
    clear_collection,
    create_collection,
    find_members_by_name,
    find_user_id_by_username,
    get_active_collection,
    get_active_collections,
    get_all_collection_members,
    get_collection_member_by_user_id,
    get_collection_member_by_username,
    get_connection,
    get_member_by_username,
    get_setting,
    get_unpaid_members,
    mark_paid,
    restore_paid_members,
    set_member_user_id,
    set_setting,
    upsert_chat_member,
)

logger = logging.getLogger(__name__)

_ORGANIZER_ID_SETTING = "organizer_id"
_cached_organizer_id: Optional[int] = ORGANIZER_ID or None


async def get_organizer_id(db) -> Optional[int]:
    """Return a stable organizer ID, learning and persisting it when possible."""
    global _cached_organizer_id

    if ORGANIZER_ID:
        return ORGANIZER_ID
    if _cached_organizer_id:
        return _cached_organizer_id

    saved = await get_setting(db, _ORGANIZER_ID_SETTING)
    if saved:
        try:
            _cached_organizer_id = int(saved)
            return _cached_organizer_id
        except (TypeError, ValueError):
            logger.error("Stored organizer_id is invalid: %r", saved)

    # This also upgrades existing installations where the organizer changed
    # username after the bot had already seen the old configured username.
    if ORGANIZER_USERNAME:
        historical_id = await find_user_id_by_username(db, ORGANIZER_USERNAME)
        if historical_id:
            _cached_organizer_id = historical_id
            await set_setting(db, _ORGANIZER_ID_SETTING, str(historical_id))
            logger.info(
                "Organizer ID learned from chat history: user_id=%d (configured username @%s)",
                historical_id,
                ORGANIZER_USERNAME,
            )
            return historical_id

    return None


async def is_organizer(db, user: Optional[User]) -> bool:
    """Authenticate the organizer by stable ID and bootstrap that ID by username once."""
    global _cached_organizer_id

    if user is None:
        return False

    organizer_id = await get_organizer_id(db)
    if organizer_id:
        return user.id == organizer_id

    if ORGANIZER_USERNAME and user.username and user.username.lower() == ORGANIZER_USERNAME:
        _cached_organizer_id = user.id
        await set_setting(db, _ORGANIZER_ID_SETTING, str(user.id))
        logger.info("Organizer ID learned from message: user_id=%d username=@%s", user.id, user.username)
        return True

    return False


def has_collection_link(message: Message) -> bool:
    text = message.text or message.caption or ""
    return "tbank.ru" in text.lower()


def clean_name_token(token: str) -> str:
    result = []
    for ch in token.lstrip("@"):
        category = unicodedata.category(ch)
        if category.startswith(("L", "M", "Pc")):
            result.append(ch)
    return "".join(result).strip()


def _utf16_offset_to_python_index(text: str, offset: int) -> int:
    """Convert Telegram's UTF-16 code-unit offset to a Python string index."""
    encoded_prefix = text.encode("utf-16-le")[: offset * 2]
    return len(encoded_prefix.decode("utf-16-le"))


def _text_without_entity_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return text

    result = []
    previous_end = 0
    for start, end in sorted(ranges):
        if start > previous_end:
            result.append(text[previous_end:start])
        previous_end = max(previous_end, end)
    result.append(text[previous_end:])
    return "".join(result)


async def extract_and_store_users(db, message: Message, chat_id: int) -> int:
    """Parse mentions and names with correct Telegram UTF-16 entity handling."""
    tracked_user_ids: Set[int] = set()
    tracked_usernames: Set[str] = set()
    plain_text_parts = []

    sources = (
        (message.text or "", list(message.entities or []), message.parse_entity),
        (message.caption or "", list(message.caption_entities or []), message.parse_caption_entity),
    )

    for text, entities, parse_entity in sources:
        if not text:
            continue

        entity_ranges = []
        for entity in entities:
            if entity.type not in ("mention", "text_mention"):
                continue

            start = _utf16_offset_to_python_index(text, entity.offset)
            end = _utf16_offset_to_python_index(text, entity.offset + entity.length)
            entity_ranges.append((start, end))

            if entity.type == "text_mention" and entity.user:
                user = entity.user
                tracked_user_ids.add(user.id)
                await upsert_chat_member(
                    db, user.id, chat_id, user.username, user.first_name, user.last_name
                )
            elif entity.type == "mention":
                username = parse_entity(entity).lstrip("@").lower()
                if username:
                    tracked_usernames.add(username)

        plain_text_parts.append(_text_without_entity_ranges(text, entity_ranges))

    tokens = re.split(r"[\s,;]+", " ".join(plain_text_parts))
    plain_names_found: Set[int] = set()
    for token in tokens:
        cleaned = clean_name_token(token)
        if len(cleaned) >= 3:
            members = await find_members_by_name(db, chat_id, cleaned)
            plain_names_found.update(member["user_id"] for member in members)

    all_user_ids = tracked_user_ids | plain_names_found

    for username in tracked_usernames:
        member = await get_member_by_username(db, chat_id, username)
        if member:
            all_user_ids.add(member["user_id"])

    stored_count = 0
    for user_id in all_user_ids:
        cursor = await db.execute(
            """
            SELECT username, first_name, last_name
            FROM chat_members
            WHERE user_id = ? AND chat_id = ?
            """,
            (user_id, chat_id),
        )
        row = await cursor.fetchone()
        if row:
            username = row["username"].lower() if row["username"] else None
            display_name = row["first_name"] or row["last_name"] or row["username"] or str(user_id)
        else:
            username = None
            display_name = str(user_id)

        await add_collection_member(db, chat_id, user_id, username, display_name)
        stored_count += 1

    resolved_usernames = set()
    for user_id in all_user_ids:
        cursor = await db.execute(
            "SELECT username FROM chat_members WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        row = await cursor.fetchone()
        if row and row["username"]:
            resolved_usernames.add(row["username"].lower())

    for username in tracked_usernames - resolved_usernames:
        await add_collection_member(db, chat_id, None, username, f"@{username}")
        stored_count += 1

    return stored_count


async def handle_collection_message(message: Message, chat_id: int):
    """Create or refresh one chat's collection, preserving paid state on edits."""
    db = await get_connection()
    existing = await get_active_collection(db, chat_id)
    same_message = bool(existing and existing["message_id"] == message.message_id)

    paid_user_ids: Set[int] = set()
    paid_usernames: Set[str] = set()
    if same_message:
        old_members = await get_all_collection_members(db, chat_id)
        paid_user_ids = {m["user_id"] for m in old_members if m["paid"] and m["user_id"]}
        paid_usernames = {m["username"] for m in old_members if m["paid"] and m["username"]}

    await create_collection(db, message.message_id, chat_id)
    count = await extract_and_store_users(db, message, chat_id)

    if same_message and (paid_user_ids or paid_usernames):
        await restore_paid_members(db, chat_id, paid_user_ids, paid_usernames)

    logger.info(
        "Collection %s: msg_id=%d chat_id=%d members=%d",
        "updated" if same_message else "created",
        message.message_id,
        chat_id,
        count,
    )


async def handle_reaction_update(
    chat_id: int,
    message_id: int,
    user: Optional[User],
    new_reaction: list,
    old_reaction: list,
):
    """Track reaction changes on the active collection message in this chat."""
    if user is None:
        return

    db = await get_connection()
    collection = await get_active_collection(db, chat_id)
    if collection is None or collection["message_id"] != message_id:
        return

    member = await get_collection_member_by_user_id(db, chat_id, user.id)
    if member is None and user.username:
        member = await get_collection_member_by_username(db, chat_id, user.username)
        if member and member["user_id"] is None:
            await set_member_user_id(db, member["id"], user.id)
            member = await get_collection_member_by_user_id(db, chat_id, user.id)

    if member is None:
        return

    paid = len(new_reaction) > 0
    await mark_paid(db, member["id"], paid)

    display_name = member.get("display_name") or member.get("username") or str(user.id)
    logger.info(
        "Reaction update: %s (%d) -> paid=%s on msg %d in chat %d",
        display_name,
        user.id,
        paid,
        message_id,
        chat_id,
    )


async def build_reminder_text(db, chat_id: int) -> Optional[str]:
    unpaid = await get_unpaid_members(db, chat_id)
    if not unpaid:
        return None

    lines = []
    for member in unpaid:
        if member["username"]:
            lines.append(f"@{member['username']}")
        elif member["user_id"]:
            display_name = html.escape(member["display_name"] or str(member["user_id"]), quote=False)
            lines.append(f'<a href="tg://user?id={member["user_id"]}">{display_name}</a>')
        else:
            lines.append(html.escape(member.get("display_name") or "??", quote=False))

    return f"Напоминаю про оплату!\n{', '.join(lines)}"


async def send_reminder(context: ContextTypes.DEFAULT_TYPE, reset_after: bool = False):
    """Send reminders for every chat that currently has an active collection."""
    db = await get_connection()
    collections = await get_active_collections(db)

    for collection in collections:
        chat_id = collection["chat_id"]
        text = await build_reminder_text(db, chat_id)
        if text is None:
            await clear_collection(db, chat_id)
            logger.info("Collection cleared in chat %d (all paid)", chat_id)
            continue

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_to_message_id=collection["message_id"],
            )
            logger.info("Reminder sent to chat %d", chat_id)
        except Exception:
            logger.exception("Failed to send reminder to chat %d", chat_id)

        if reset_after:
            await clear_collection(db, chat_id)
            logger.info("Collection cleared in chat %d after Friday 15:00 reminder", chat_id)


async def get_status_text(db, chat_id: int) -> str:
    collection = await get_active_collection(db, chat_id)
    if collection is None:
        return "Нет активного сбора в этом чате."

    members = await get_all_collection_members(db, chat_id)
    paid = [member for member in members if member["paid"]]
    unpaid = [member for member in members if not member["paid"]]

    lines = [f"Активный сбор (сообщение #{collection['message_id']}):"]
    lines.append(f"\nОплатили ({len(paid)}):")
    for member in paid:
        name = member.get("display_name") or member.get("username") or str(member.get("user_id", "?"))
        lines.append(f"  ✅ {name}")

    lines.append(f"\nНе оплатили ({len(unpaid)}):")
    for member in unpaid:
        name = member.get("display_name") or member.get("username") or str(member.get("user_id", "?"))
        lines.append(f"  ❌ {name}")

    return "\n".join(lines)

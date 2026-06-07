import re
import unicodedata
import logging
from typing import Set, Optional
from telegram import Message, User
from telegram.ext import ContextTypes

from config import ORGANIZER_ID
from storage import (
    get_connection,
    upsert_chat_member,
    find_members_by_name,
    get_member_by_username,
    get_active_collection,
    create_collection,
    add_collection_member,
    get_collection_member_by_user_id,
    get_collection_member_by_username,
    set_member_user_id,
    mark_paid,
    get_unpaid_members,
    get_all_collection_members,
    clear_collection,
)

logger = logging.getLogger(__name__)


def clean_name_token(token: str) -> str:
    result = []
    for ch in token.lstrip("@"):
        cat = unicodedata.category(ch)
        if cat.startswith(("L", "M", "Pc")):
            result.append(ch)
    return "".join(result).strip()


def is_collection_message(message: Message) -> bool:
    if message.from_user is None:
        return False
    if message.from_user.id != ORGANIZER_ID:
        return False
    text = message.text or message.caption or ""
    if not text:
        return False
    return "tbank.ru" in text.lower()


async def extract_and_store_users(
    db,
    message: Message,
    chat_id: int,
) -> int:
    """
    Parse mentions from entities + plain text, resolve user_ids, store in DB.
    Returns count of members added.
    """
    text = message.text or message.caption or ""
    entities = list(message.entities or []) + list(message.caption_entities or [])

    tracked_user_ids: Set[int] = set()
    tracked_usernames: Set[str] = set()

    # Collect entity-covered ranges for text-based extraction later
    entity_ranges = []
    for ent in entities:
        if ent.type in ("mention", "text_mention"):
            entity_ranges.append((ent.offset, ent.offset + ent.length))

    # 1. TEXT_MENTION entities: have user_id directly
    for ent in entities:
        if ent.type == "text_mention" and ent.user:
            user = ent.user
            tracked_user_ids.add(user.id)
            await upsert_chat_member(db, user.id, chat_id, user.username, user.first_name, user.last_name)

    # 2. MENTION entities: @username, resolve via chat_members
    for ent in entities:
        if ent.type == "mention":
            username = text[ent.offset : ent.offset + ent.length].lstrip("@")
            tracked_usernames.add(username.lower())

    # 3. Plain text names (outside entity ranges)
    remaining_text = ""
    prev_end = 0
    entity_ranges.sort()
    for start, end in entity_ranges:
        remaining_text += text[prev_end:start]
        prev_end = end
    remaining_text += text[prev_end:]

    tokens = re.split(r"[\s,;]+", remaining_text)
    plain_names_found = set()
    for token in tokens:
        cleaned = clean_name_token(token)
        if len(cleaned) >= 3:
            members = await find_members_by_name(db, chat_id, cleaned)
            for m in members:
                plain_names_found.add(m["user_id"])

    # Merge all found user_ids
    all_user_ids = tracked_user_ids | plain_names_found

    # Resolve usernames from MENTION entities
    for username_lower in tracked_usernames:
        member = await get_member_by_username(db, chat_id, username_lower)
        if member:
            all_user_ids.add(member["user_id"])

    # Store all resolved members
    stored_count = 0
    for user_id in all_user_ids:
        username_val = None
        # Look up info from chat_members
        cursor = await db.execute(
            "SELECT username, first_name, last_name FROM chat_members WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        row = await cursor.fetchone()
        if row:
            username_val = row["username"]
            display = row["first_name"] or row["last_name"] or row["username"] or str(user_id)
        else:
            display = str(user_id)

        if username_val:
            username_val = username_val.lower()

        await add_collection_member(db, user_id, username_val, display)
        stored_count += 1

    # Store unresolved @mentions (no user_id yet, will be linked later on reaction)
    for username_lower in tracked_usernames:
        already_stored = False
        for uid in all_user_ids:
            cursor = await db.execute(
                "SELECT username FROM chat_members WHERE user_id = ? AND chat_id = ?",
                (uid, chat_id),
            )
            row = await cursor.fetchone()
            if row and row["username"] and row["username"].lower() == username_lower:
                already_stored = True
                break
        if not already_stored:
            await add_collection_member(db, None, username_lower, f"@{username_lower}")
            stored_count += 1

    return stored_count


async def handle_collection_message(message: Message, chat_id: int):
    """Process a detected collection message: create collection, parse and store members."""
    db = await get_connection()
    await create_collection(db, message.message_id, chat_id)
    count = await extract_and_store_users(db, message, chat_id)
    logger.info(
        "Collection created: msg_id=%d, chat_id=%d, members=%d",
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
    """Track reaction changes on the active collection message."""
    if user is None:
        return

    db = await get_connection()
    coll = await get_active_collection(db)
    if coll is None:
        return
    if coll["message_id"] != message_id or coll["chat_id"] != chat_id:
        return

    # Find member record
    member = await get_collection_member_by_user_id(db, user.id)

    if member is None and user.username:
        # Try to resolve by username (for previously unresolved @mentions)
        member = await get_collection_member_by_username(db, user.username.lower())
        if member and member["user_id"] is None:
            await set_member_user_id(db, member["id"], user.id)
            member = await get_collection_member_by_user_id(db, user.id)

    if member is None:
        return  # Reacting user is not in collection list

    paid = len(new_reaction) > 0
    await mark_paid(db, member["id"], paid)

    display = member.get("display_name") or member.get("username") or str(user.id)
    logger.info(
        "Reaction update: %s (%d) -> paid=%s on msg %d",
        display,
        user.id,
        paid,
        message_id,
    )


async def build_reminder_text(db) -> Optional[str]:
    """Build reminder message for unpaid members, or None if everyone paid."""
    unpaid = await get_unpaid_members(db)
    if not unpaid:
        return None

    lines = []
    for m in unpaid:
        if m["username"]:
            lines.append(f"@{m['username']}")
        elif m["user_id"]:
            lines.append(f'<a href="tg://user?id={m["user_id"]}">{m["display_name"]}</a>')
        else:
            lines.append(m.get("display_name", "??"))

    mentions = ", ".join(lines)
    return f"Напоминаю про оплату!\n{mentions}"


async def send_reminder(context: ContextTypes.DEFAULT_TYPE, reset_after: bool = False):
    """Send reminder to chat about unpaid members, optionally clear collection after."""
    db = await get_connection()
    coll = await get_active_collection(db)
    if coll is None:
        return

    text = await build_reminder_text(db)
    if text is None:
        # Everyone paid - optionally clear anyway
        if reset_after:
            await clear_collection(db)
            logger.info("Collection cleared (all paid, Friday 15:00)")
        return

    try:
        await context.bot.send_message(
            chat_id=coll["chat_id"],
            text=text,
            parse_mode="HTML",
            reply_to_message_id=coll["message_id"],
        )
        logger.info("Reminder sent to chat %d", coll["chat_id"])
    except Exception as e:
        logger.error("Failed to send reminder: %s", e)

    if reset_after:
        await clear_collection(db)
        logger.info("Collection cleared after Friday 15:00 reminder")


async def get_status_text(db) -> str:
    """Get human-readable status of the current collection."""
    coll = await get_active_collection(db)
    if coll is None:
        return "Нет активного сбора."

    members = await get_all_collection_members(db)
    paid = [m for m in members if m["paid"]]
    unpaid = [m for m in members if not m["paid"]]

    lines = [f"Активный сбор (сообщение #{coll['message_id']}):"]
    lines.append(f"\nОплатили ({len(paid)}):")
    for m in paid:
        name = m.get("display_name") or m.get("username") or str(m.get("user_id", "?"))
        lines.append(f"  ✅ {name}")

    lines.append(f"\nНе оплатили ({len(unpaid)}):")
    for m in unpaid:
        name = m.get("display_name") or m.get("username") or str(m.get("user_id", "?"))
        lines.append(f"  ❌ {name}")

    return "\n".join(lines)

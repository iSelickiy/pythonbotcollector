import html
import logging
import random
import re
import unicodedata
from datetime import datetime
from typing import Optional, Set
from zoneinfo import ZoneInfo

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

_MSK = ZoneInfo("Europe/Moscow")

_ORGANIZER_ID_SETTING = "organizer_id"
_cached_organizer_id: Optional[int] = ORGANIZER_ID or None


async def get_organizer_id(db) -> Optional[int]:
    """Return a stable organizer ID, learning and persisting it when possible."""
    global _cached_organizer_id

    if ORGANIZER_ID:
        configured_id = str(ORGANIZER_ID)
        if await get_setting(db, _ORGANIZER_ID_SETTING) != configured_id:
            await set_setting(db, _ORGANIZER_ID_SETTING, configured_id)
            logger.info("Organizer ID synchronized from configuration: user_id=%d", ORGANIZER_ID)
        _cached_organizer_id = ORGANIZER_ID
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


async def get_organizer_mention(db) -> str:
    """A tag/link for the organizer, to call them out by name in a message."""
    if ORGANIZER_USERNAME:
        return f"@{ORGANIZER_USERNAME}"
    organizer_id = await get_organizer_id(db)
    if organizer_id:
        return f'<a href="tg://user?id={organizer_id}">организатору</a>'
    return "организатору"


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


COLLECTION_STARTED_PHRASES = (
    "⚽💰 Сбор открыт! Реагируйте на сообщение — я слежу за каждым, я всё-таки коллектор.",
    "Новый сбор стартовал. Поле забронировано, осталось забронировать вашу совесть на оплату.",
    "Внимание, деньги любят счёт, а я люблю реакции на это сообщение. Погнали!",
    "Сбор в деле! Кто не поставит реакцию до игры — весь матч слышит от меня «а денежку?».",
    "Официально: касса открыта. Я не банк, но памятью на должников не уступлю.",
    "Мяч круглый, поле большое, а сбор — уже активен. Не тяните с оплатой до свистка.",
    "Новый сбор запущен! Я коллектор не только по имени — буду вежливо, но упорно стучаться за оплатой.",
    "Всё, сбор пошёл! Реакция на это сообщение = ты в игре. Без неё — только на трибуне должников.",
)


def get_collection_started_message() -> str:
    return random.choice(COLLECTION_STARTED_PHRASES)


async def handle_collection_message(message: Message, chat_id: int) -> bool:
    """Create or refresh one chat's collection, preserving paid state on edits.

    Returns True if this created a brand-new collection (as opposed to
    refreshing the currently active one), so callers can announce the start.
    """
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

    return not same_message


async def handle_reaction_update(
    context: ContextTypes.DEFAULT_TYPE,
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

    if paid and not await get_unpaid_members(db, chat_id):
        await announce_collection_complete(context, chat_id, message_id)


STAGE_GENTLE = "gentle"
STAGE_FIRM = "firm"
STAGE_FINAL = "final"

REMINDER_PHRASES = {
    STAGE_GENTLE: (
        "Так, по-хорошему напоминаю: сбор ещё открыт, а вот совесть некоторых — нет.",
        "Коллектор снова на связи. Пока вежливо: закиньте, будьте людьми.",
        "Первое напоминание, ещё доброе. Второе будет с меньшим терпением.",
        "Сбор не закрылся сам, и я тоже не закроюсь, пока не увижу оплату.",
        "Тук-тук, это я, ваш любимый коллектор. Пока ещё вежливый.",
        "Небольшое дружеское: деньги сами себя не переведут.",
    ),
    STAGE_FIRM: (
        "Доброе утро! У кого-то оно будет добрым только после перевода.",
        "Второй раз напоминаю, вежливость на исходе, как и моё терпение.",
        "Утро, кофе, немного дисциплины — переведите уже, а?",
        "Я коллектор, не будильник, но раз уж разбудил — заодно и оплатите.",
        "Список неплательщиков не худеет сам по себе, помогите мне с этим.",
        "Ещё один шанс сделать всё по-хорошему. Использовать рекомендую.",
    ),
    STAGE_FINAL: (
        "Всё, я сдаюсь — не мой уровень воздействия. {organizer}, забирай, дальше пусть кожаный разбирается лично.",
        "Терпение коллектора закончилось безвозвратно. Передаю должников {organizer} — с живым человеком аргумент «забыл» уже не прокатит.",
        "Я слишком вежливый бот для этого разговора. {organizer}, включай кожаное обаяние, я пас.",
        "Официально прекращаю попытки. {organizer}, эти люди твои — у ботов на них иммунитет выработался.",
        "Финальное предупреждение закончилось ничем, как обычно. {organizer}, дальше сам, я передаю дело кожаному.",
        "Всё, сдаю пост. {organizer}, лично разберись — на роботов эти должники давно забили.",
    ),
}


def get_reminder_message(stage: str) -> str:
    return random.choice(REMINDER_PHRASES[stage])


def build_message_link(chat_id: int, message_id: int) -> Optional[str]:
    """Build a t.me deep link to a message, for supergroups/channels (chat_id starts with -100)."""
    chat_part = str(chat_id)
    if not chat_part.startswith("-100"):
        return None
    return f"https://t.me/c/{chat_part[4:]}/{message_id}"


async def build_reminder_text(db, chat_id: int, message_id: int, stage: str) -> Optional[str]:
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

    intro = get_reminder_message(stage)
    if stage == STAGE_FINAL:
        intro = intro.format(organizer=await get_organizer_mention(db))

    text = f"{intro}\n{', '.join(lines)}"

    link = build_message_link(chat_id, message_id)
    if link:
        text += f'\n\n<a href="{link}">📌 Сообщение со сбором</a>'

    return text


ALL_PAID_PHRASES_WITH_WISH = (
    "Все скинулись, красавчики! 💪 Хорошей игры!",
    "Касса закрыта, все оплатили — можно выдыхать. Хорошей игры!",
    "Ни одного должника! Коллектор доволен и даже улыбается. Хорошей игры!",
    "Сбор закрыт, все при деньгах, молодцы. Хорошей игры, до встречи на поле!",
    "Чудо случилось — все оплатили без единого лишнего напоминания. Хорошей игры!",
    "Все переводы дошли, коллектор уходит отдыхать. Хорошей игры!",
)

ALL_PAID_PHRASES_NO_WISH = (
    "Сбор завершён, все скинулись. Спасибо всем!",
    "Готово — все оплатили. Всем спасибо, красавчики.",
    "Сбор закрыт, деньги все на месте. Спасибо, что не пришлось никого пинать... почти.",
    "Все при оплате. Сбор завершён, спасибо каждому.",
    "Финиш! Все скинулись, сбор закрыт. Всем спасибо.",
    "Деньги собраны полностью. Сбор завершён, спасибо всем участникам.",
)

_ALL_PAID_WISH_CUTOFF = (9, 30)


def get_all_paid_message(now: Optional[datetime] = None) -> str:
    """Pick an all-paid message: cheerful with a game wish before 9:30 MSK, plain thanks after."""
    if now is None:
        now = datetime.now(_MSK)
    if (now.hour, now.minute) < _ALL_PAID_WISH_CUTOFF:
        return random.choice(ALL_PAID_PHRASES_WITH_WISH)
    return random.choice(ALL_PAID_PHRASES_NO_WISH)


async def announce_collection_complete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Announce that a collection just became fully paid, then clear it."""
    db = await get_connection()
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=get_all_paid_message(),
            reply_to_message_id=message_id,
        )
        logger.info("All-paid message sent to chat %d", chat_id)
    except Exception:
        logger.exception("Failed to send all-paid message to chat %d", chat_id)
    await clear_collection(db, chat_id)
    logger.info("Collection cleared in chat %d (all paid)", chat_id)


async def send_reminder(context: ContextTypes.DEFAULT_TYPE, stage: str, reset_after: bool = False):
    """Send reminders for every chat that currently has an active collection."""
    db = await get_connection()
    collections = await get_active_collections(db)

    for collection in collections:
        chat_id = collection["chat_id"]
        text = await build_reminder_text(db, chat_id, collection["message_id"], stage)
        if text is None:
            # Normally already handled reactively the moment the last person
            # paid (see handle_reaction_update) — this is just a safety net.
            await announce_collection_complete(context, chat_id, collection["message_id"])
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


def _member_label(member: dict) -> str:
    """Prefer a real @username tag over the stored display name."""
    if member.get("username"):
        return f"@{member['username']}"
    return member.get("display_name") or str(member.get("user_id", "?"))


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
        lines.append(f"  ✅ {_member_label(member)}")

    lines.append(f"\nНе оплатили ({len(unpaid)}):")
    for member in unpaid:
        lines.append(f"  ❌ {_member_label(member)}")

    return "\n".join(lines)

import logging
import asyncio
import signal
from datetime import time
from zoneinfo import ZoneInfo

from telegram.ext import (
    Application,
    MessageHandler,
    MessageReactionHandler,
    CommandHandler,
    TypeHandler,
    filters,
    ContextTypes,
    Defaults,
)
from telegram import Update

from config import BOT_TOKEN, DB_PATH, WEBHOOK_URL, WEBHOOK_SECRET
from storage import (
    clear_collection,
    close_connection,
    get_active_collection,
    get_connection,
    init_db,
    upsert_chat_member,
)
from collector import (
    has_collection_link,
    handle_collection_message,
    handle_reaction_update,
    get_organizer_id,
    get_collection_started_message,
    is_organizer,
    send_reminder,
    get_status_text,
    STAGE_GENTLE,
    STAGE_FIRM,
    STAGE_FINAL,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")


# ── message handler: track all senders, detect collection messages ──

async def on_message(update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.edited_message
    if message is None:
        return

    chat_id = message.chat_id
    user = message.from_user
    if user is None:
        return

    db = await get_connection()
    await upsert_chat_member(db, user.id, chat_id, user.username, user.first_name, user.last_name)
    is_edit = update.edited_message is not None
    logger.info(
        "%s chat=%d type=%s from=@%s text=%s",
        "EDIT" if is_edit else "MSG",
        chat_id,
        message.chat.type,
        user.username,
        (message.text or message.caption or "")[:80],
    )

    organizer = await is_organizer(db, user)
    if organizer and has_collection_link(message):
        logger.info("Collection message detected from organizer in chat %d (edit=%s)", chat_id, is_edit)
        is_new_collection = await handle_collection_message(message, chat_id)
        if is_new_collection:
            await message.reply_text(get_collection_started_message())
    elif organizer and is_edit:
        collection = await get_active_collection(db, chat_id)
        if collection and collection["message_id"] == message.message_id:
            await clear_collection(db, chat_id)
            logger.info("Collection cleared in chat %d because the edited message lost its T-Bank link", chat_id)


# ── reaction handler ──

async def on_reaction(update, context: ContextTypes.DEFAULT_TYPE):
    reaction = update.message_reaction
    if reaction is None:
        return

    await handle_reaction_update(
        chat_id=reaction.chat.id,
        message_id=reaction.message_id,
        user=reaction.user,
        new_reaction=reaction.new_reaction,
        old_reaction=reaction.old_reaction,
    )


# ── commands ──


async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот-коллектор запущен. Слушаю чат.")


async def cmd_status(update, context: ContextTypes.DEFAULT_TYPE):
    db = await get_connection()
    if not await is_organizer(db, update.effective_user):
        await update.message.reply_text("Только организатор может смотреть статус.")
        return
    text = await get_status_text(db, update.effective_chat.id)
    await update.message.reply_text(text)


async def cmd_reset(update, context: ContextTypes.DEFAULT_TYPE):
    db = await get_connection()
    if not await is_organizer(db, update.effective_user):
        await update.message.reply_text("Только организатор может сбросить сбор.")
        return
    await clear_collection(db, update.effective_chat.id)
    await update.message.reply_text("Сбор сброшен.")


# ── scheduled reminders ──

async def remind_thursday(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Thursday 22:00 MSK reminder triggered")
    await send_reminder(context, stage=STAGE_GENTLE, reset_after=False)


async def remind_friday_morning(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Friday 09:00 MSK reminder triggered")
    await send_reminder(context, stage=STAGE_FIRM, reset_after=False)


async def remind_friday_afternoon(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Friday 15:00 MSK reminder triggered")
    await send_reminder(context, stage=STAGE_FINAL, reset_after=True)


def setup_jobs(application: Application):
    jq = application.job_queue

    jq.run_daily(
        remind_thursday,
        time=time(hour=22, minute=0, tzinfo=_MSK),
        days=(4,),
        name="remind_thursday",
    )
    jq.run_daily(
        remind_friday_morning,
        time=time(hour=9, minute=0, tzinfo=_MSK),
        days=(5,),
        name="remind_friday_morning",
    )
    jq.run_daily(
        remind_friday_afternoon,
        time=time(hour=15, minute=0, tzinfo=_MSK),
        days=(5,),
        name="remind_friday_afternoon",
    )


async def raw_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.edited_message
    if message:
        logger.info(
            "[RAW] %s chat_id=%d from=@%s type=%s text=%s",
            "edited_message" if update.edited_message else "message",
            message.chat_id,
            message.from_user.username if message.from_user else None,
            message.chat.type,
            (message.text or message.caption or "")[:80],
        )
    elif update.message_reaction:
        logger.info(
            "[RAW] reaction chat_id=%d msg_id=%d user=%s",
            update.message_reaction.chat.id,
            update.message_reaction.message_id,
            update.message_reaction.user.username if update.message_reaction.user else None,
        )
    else:
        logger.info("[RAW] update type=%s", type(update).__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    update_id = getattr(update, "update_id", None)
    if isinstance(error, BaseException):
        logger.error(
            "Unhandled error while processing update_id=%s",
            update_id,
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Unhandled non-exception error while processing update_id=%s: %r", update_id, error)


# ── main ──

async def main():
    await init_db(DB_PATH)
    logger.info("Database initialized at %s", DB_PATH)
    db = await get_connection()
    organizer_id = await get_organizer_id(db)
    if organizer_id:
        logger.info("Organizer identity ready: user_id=%d", organizer_id)

    app = Application.builder().token(BOT_TOKEN).defaults(Defaults(tzinfo=_MSK)).build()

    app.add_handler(TypeHandler(Update, raw_update), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message))
    app.add_handler(MessageReactionHandler(on_reaction))
    app.add_error_handler(on_error)

    setup_jobs(app)

    logger.info("Bot starting...")
    await app.initialize()
    await app.start()

    try:
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=8080,
            url_path="webhook",
            webhook_url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "edited_message", "message_reaction"],
        )
    except Exception as e:
        logger.critical("Webhook setup failed: %s", e)
        raise

    webhook_info = await app.bot.get_webhook_info()
    logger.info(
        "Webhook info: url=%s pending=%d last_error=%s",
        webhook_info.url,
        webhook_info.pending_update_count,
        webhook_info.last_error_message,
    )
    logger.info("Webhook server listening on :8080/webhook")

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task = loop.create_task(main())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, main_task.cancel)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.run_until_complete(close_connection())
        loop.close()

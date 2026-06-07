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
    filters,
    ContextTypes,
)

from config import BOT_TOKEN, DB_PATH
from storage import init_db, get_connection, upsert_chat_member, close_connection
from collector import (
    is_collection_message,
    handle_collection_message,
    handle_reaction_update,
    send_reminder,
    get_status_text,
    clear_collection,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")


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

    if is_collection_message(message):
        logger.info("Collection message detected from organizer in chat %d", chat_id)
        await handle_collection_message(message, chat_id)


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
    text = await get_status_text(db)
    await update.message.reply_text(text)


async def cmd_reset(update, context: ContextTypes.DEFAULT_TYPE):
    db = await get_connection()
    await clear_collection(db)
    await update.message.reply_text("Сбор сброшен.")


# ── scheduled reminders ──

async def remind_thursday(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Thursday 22:00 MSK reminder triggered")
    await send_reminder(context, reset_after=False)


async def remind_friday_morning(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Friday 09:00 MSK reminder triggered")
    await send_reminder(context, reset_after=False)


async def remind_friday_afternoon(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Friday 15:00 MSK reminder triggered")
    await send_reminder(context, reset_after=True)


def setup_jobs(application: Application):
    jq = application.job_queue

    jq.run_daily(
        remind_thursday,
        time=time(hour=22, minute=0, tzinfo=MSK),
        days=(3,),  # Thursday (Monday=0)
        name="remind_thursday",
    )
    jq.run_daily(
        remind_friday_morning,
        time=time(hour=9, minute=0, tzinfo=MSK),
        days=(4,),  # Friday
        name="remind_friday_morning",
    )
    jq.run_daily(
        remind_friday_afternoon,
        time=time(hour=15, minute=0, tzinfo=MSK),
        days=(4,),  # Friday
        name="remind_friday_afternoon",
    )


# ── main ──

async def main():
    await init_db(DB_PATH)
    logger.info("Database initialized at %s", DB_PATH)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message))
    app.add_handler(MessageReactionHandler(on_reaction))

    setup_jobs(app)

    logger.info("Bot starting...")
    async with app:
        await app.updater.start_polling(allowed_updates=["message", "message_reaction"])
        await asyncio.Event().wait()


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

import os
import sys
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN is not set. Copy .env.example to .env and fill in the values.")
    sys.exit(1)

_organizer_id = os.getenv("ORGANIZER_ID", "0").strip()
try:
    ORGANIZER_ID = int(_organizer_id) if _organizer_id else 0
except (ValueError, TypeError):
    print("ERROR: ORGANIZER_ID must be a numeric Telegram user ID.")
    sys.exit(1)

ORGANIZER_USERNAME = os.getenv("ORGANIZER_USERNAME", "").strip().lstrip("@").lower()

DB_PATH = os.getenv("DB_PATH", "data/state.db")

import logging
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "credentials.json")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))


def _parse_spreadsheet_id(raw: str | None) -> str | None:
    """Extract the spreadsheet ID from a full URL or return the raw value."""
    if not raw:
        return raw
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", raw)
    if match:
        return match.group(1)
    return raw.strip()


SPREADSHEET_ID = _parse_spreadsheet_id(os.getenv("SPREADSHEET_ID"))

# ---------------------------------------------------------------------------
# SQLite database
# ---------------------------------------------------------------------------
def _default_db_path() -> str:
    """Use /data/bot.db if the Fly.io volume is mounted, otherwise ./bot.db."""
    if os.path.isdir("/data"):
        return "/data/bot.db"
    return os.path.join(os.path.dirname(__file__) or ".", "bot.db")


DB_PATH = os.getenv("DB_PATH", _default_db_path())


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    """Configure structured logging for the entire application.

    Format: timestamp [LEVEL] module | message
    Call once at startup in main.py before any other imports log.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # Remove any existing handlers to avoid duplicates
    root.handlers.clear()
    root.addHandler(handler)

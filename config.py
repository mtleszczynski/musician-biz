import os
import re

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "credentials.json")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))


def _parse_spreadsheet_id(raw: str | None) -> str | None:
    """Extract the spreadsheet ID from a full URL or return the raw value."""
    if not raw:
        return raw
    # Match the ID from a full Google Sheets URL
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", raw)
    if match:
        return match.group(1)
    return raw.strip()


SPREADSHEET_ID = _parse_spreadsheet_id(os.getenv("SPREADSHEET_ID"))

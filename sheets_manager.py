"""Google Sheets operations for the Entries and Conversation Log tabs.

gspread is synchronous — all public functions are async wrappers using
asyncio.to_thread() so they don't block the Discord event loop.
"""

import asyncio
import json
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_CREDENTIALS_JSON, SPREADSHEET_ID

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Date",
    "Type",
    "Category",
    "Client/Event",
    "Vendor",
    "Mode of Payment",
    "Amount ($)",
    "Description",
    "Notes",
    "Discord Link",
    "Timestamp",
]

LOG_HEADERS = [
    "Timestamp",
    "User Input",
    "Bot Response",
    "Outcome",
    "Discord Link",
]

ENTRIES_TAB = "Entries"
LOG_TAB = "Conversation Log"

# Map from FinancialEntry field names to column indices (0-based)
_FIELD_TO_COL = {
    "date": 0,
    "type": 1,
    "category": 2,
    "client_or_event": 3,
    "vendor": 4,
    "mode_of_payment": 5,
    "amount": 6,
    "description": 7,
    "notes": 8,
}


# ---------------------------------------------------------------------------
# Auth & worksheet helpers
# ---------------------------------------------------------------------------

def _get_credentials() -> Credentials:
    """Load Google service account credentials from file path or JSON string."""
    creds_value = GOOGLE_CREDENTIALS_JSON
    if not creds_value:
        raise ValueError(
            "GOOGLE_CREDENTIALS_JSON is not set. "
            "Set it to a file path or a JSON string of your service account credentials."
        )
    if creds_value.strip().startswith("{"):
        info = json.loads(creds_value)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    return Credentials.from_service_account_file(creds_value, scopes=SCOPES)


def _get_spreadsheet() -> gspread.Spreadsheet:
    """Authorize and return the configured spreadsheet."""
    creds = _get_credentials()
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def _get_or_create_worksheet(
    spreadsheet: gspread.Spreadsheet,
    title: str,
    headers: list[str],
) -> gspread.Worksheet:
    """Get a worksheet by title, or create it with headers if it doesn't exist."""
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        worksheet.append_row(headers, value_input_option="USER_ENTERED")
        logger.info("op=create_worksheet | Created '%s' with headers", title)
        return worksheet

    first_row = worksheet.row_values(1)
    if not first_row or first_row[0] != headers[0]:
        worksheet.insert_row(headers, index=1)
        logger.info("op=create_worksheet | Added header row to '%s'", title)

    return worksheet


def _get_entries_worksheet() -> gspread.Worksheet:
    """Return the Entries worksheet, creating it if needed."""
    spreadsheet = _get_spreadsheet()
    try:
        sheet1 = spreadsheet.worksheet("Sheet1")
        sheet1.update_title(ENTRIES_TAB)
        logger.info("op=init | Renamed 'Sheet1' to '%s'", ENTRIES_TAB)
    except gspread.WorksheetNotFound:
        pass
    return _get_or_create_worksheet(spreadsheet, ENTRIES_TAB, HEADERS)


def _get_log_worksheet() -> gspread.Worksheet:
    """Return the Conversation Log worksheet, creating it if needed."""
    spreadsheet = _get_spreadsheet()
    return _get_or_create_worksheet(spreadsheet, LOG_TAB, LOG_HEADERS)


# ---------------------------------------------------------------------------
# Internal sync operations
# ---------------------------------------------------------------------------

def _build_row(entry: dict, discord_link: str) -> list[str]:
    """Build a spreadsheet row from an entry dict."""
    amount = entry.get("amount", 0)
    if isinstance(amount, str):
        amount = float(amount.replace("$", "").replace(",", ""))
    return [
        entry.get("date", ""),
        str(entry.get("type", "")).capitalize(),
        entry.get("category", ""),
        entry.get("client_or_event") or "",
        entry.get("vendor") or "",
        entry.get("mode_of_payment") or "",
        f"{amount:.2f}",
        entry.get("description", ""),
        entry.get("notes", ""),
        discord_link,
        datetime.now().isoformat(),
    ]


def _append_row_sync(row: list[str]) -> int:
    """Append a single row to the Entries sheet. Returns the row number."""
    worksheet = _get_entries_worksheet()
    result = worksheet.append_row(row, value_input_option="USER_ENTERED")
    updated_range = result.get("updates", {}).get("updatedRange", "")
    try:
        row_num = int(updated_range.split("!")[1].split(":")[0][1:])
    except (IndexError, ValueError):
        row_num = len(worksheet.get_all_values())
    logger.info("op=append_row | Appended row %d", row_num)
    return row_num


def _update_row_in_place_sync(row_number: int, entry: dict, discord_link: str) -> None:
    """Overwrite an existing row with updated entry data."""
    worksheet = _get_entries_worksheet()
    row = _build_row(entry, discord_link)
    cell_range = f"A{row_number}:K{row_number}"
    worksheet.update(cell_range, [row], value_input_option="USER_ENTERED")
    logger.info("op=update_in_place | Updated row %d", row_number)


def _safe_replace_sync(
    old_row_numbers: list[int],
    new_entries: list[dict],
    discord_link: str,
) -> list[int]:
    """Write new rows first, then delete old rows. Returns new row numbers.

    This prevents data loss: if the write succeeds but delete fails, you have
    duplicates (recoverable) instead of missing data (not recoverable).
    """
    worksheet = _get_entries_worksheet()

    # Step 1: Append new rows
    new_row_numbers = []
    for entry in new_entries:
        row = _build_row(entry, discord_link)
        result = worksheet.append_row(row, value_input_option="USER_ENTERED")
        updated_range = result.get("updates", {}).get("updatedRange", "")
        try:
            row_num = int(updated_range.split("!")[1].split(":")[0][1:])
        except (IndexError, ValueError):
            row_num = len(worksheet.get_all_values())
        new_row_numbers.append(row_num)
        logger.info("op=safe_replace | Appended new row %d", row_num)

    # Step 2: Delete old rows (in reverse to preserve row numbers)
    for row_num in sorted(old_row_numbers, reverse=True):
        try:
            worksheet.delete_rows(row_num)
            logger.info("op=safe_replace | Deleted old row %d", row_num)
            # Adjust new row numbers that shifted down
            new_row_numbers = [
                n - 1 if n > row_num else n for n in new_row_numbers
            ]
        except Exception:
            logger.exception("op=safe_replace | Failed to delete old row %d", row_num)

    return new_row_numbers


def _delete_last_row_sync() -> dict | None:
    """Delete the last data row from the Entries sheet."""
    worksheet = _get_entries_worksheet()
    all_values = worksheet.get_all_values()
    if len(all_values) <= 1:
        return None
    last_row = all_values[-1]
    worksheet.delete_rows(len(all_values))
    logger.info("op=delete_last | Deleted row %d", len(all_values))
    return {HEADERS[i]: last_row[i] for i in range(min(len(HEADERS), len(last_row)))}


def _delete_rows_sync(row_numbers: list[int]) -> int:
    """Delete specific rows. Returns count deleted."""
    if not row_numbers:
        return 0
    worksheet = _get_entries_worksheet()
    count = 0
    for row_num in sorted(row_numbers, reverse=True):
        try:
            worksheet.delete_rows(row_num)
            count += 1
            logger.info("op=delete_rows | Deleted row %d", row_num)
        except Exception:
            logger.exception("op=delete_rows | Failed to delete row %d", row_num)
    return count


def _find_rows_by_discord_link_sync(discord_link: str) -> list[int]:
    """Find all row numbers matching a Discord link."""
    worksheet = _get_entries_worksheet()
    all_values = worksheet.get_all_values()
    discord_link_col = HEADERS.index("Discord Link")
    row_numbers = []
    for i, row in enumerate(all_values[1:], start=2):
        if len(row) > discord_link_col and row[discord_link_col] == discord_link:
            row_numbers.append(i)
    return row_numbers


def _get_monthly_summary_sync(month: int, year: int) -> dict:
    """Get income/expense totals by category for a given month."""
    worksheet = _get_entries_worksheet()
    all_values = worksheet.get_all_values()

    income: dict[str, float] = {}
    expenses: dict[str, float] = {}

    for row in all_values[1:]:
        if len(row) < 7:
            continue
        try:
            row_date = datetime.strptime(row[0], "%Y-%m-%d")
        except ValueError:
            continue
        if row_date.month != month or row_date.year != year:
            continue

        entry_type = row[1].lower()
        category = row[2]
        try:
            amount = float(row[6].replace("$", "").replace(",", ""))
        except ValueError:
            continue

        if entry_type == "income":
            income[category] = income.get(category, 0.0) + amount
        else:
            expenses[category] = expenses.get(category, 0.0) + amount

    return {
        "income": income,
        "expenses": expenses,
        "total_income": sum(income.values()),
        "total_expenses": sum(expenses.values()),
    }


def _log_conversation_sync(
    user_input: str,
    bot_response: str,
    outcome: str,
    discord_link: str,
) -> None:
    """Append a row to the Conversation Log tab."""
    worksheet = _get_log_worksheet()
    row = [
        datetime.now().isoformat(),
        user_input[:500],
        bot_response[:500],
        outcome,
        discord_link,
    ]
    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info("op=log_conversation | outcome=%s", outcome)


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

async def append_entry(entry: dict, discord_link: str) -> int:
    """Append a financial entry to the sheet. Returns the row number."""
    row = _build_row(entry, discord_link)
    return await asyncio.to_thread(_append_row_sync, row)


async def append_entries(entries: list[dict], discord_link: str) -> list[int]:
    """Append multiple entries. Returns list of row numbers."""
    row_numbers = []
    for entry in entries:
        row = _build_row(entry, discord_link)
        row_num = await asyncio.to_thread(_append_row_sync, row)
        row_numbers.append(row_num)
    return row_numbers


async def update_entry_in_place(
    row_number: int, entry: dict, discord_link: str
) -> None:
    """Update an existing row in-place with new entry data."""
    await asyncio.to_thread(_update_row_in_place_sync, row_number, entry, discord_link)


async def safe_replace_entries(
    old_row_numbers: list[int],
    new_entries: list[dict],
    discord_link: str,
) -> list[int]:
    """Write new entries first, then delete old ones. Returns new row numbers."""
    return await asyncio.to_thread(
        _safe_replace_sync, old_row_numbers, new_entries, discord_link
    )


async def find_rows_by_discord_link(discord_link: str) -> list[int]:
    """Find all row numbers matching a Discord link."""
    return await asyncio.to_thread(_find_rows_by_discord_link_sync, discord_link)


async def delete_rows(row_numbers: list[int]) -> int:
    """Delete specific rows. Returns count deleted."""
    return await asyncio.to_thread(_delete_rows_sync, row_numbers)


async def delete_last_entry() -> dict | None:
    """Delete the last entry. Returns the deleted data or None."""
    return await asyncio.to_thread(_delete_last_row_sync)


async def get_monthly_summary(month: int, year: int) -> dict:
    """Get monthly income/expense summary by category."""
    return await asyncio.to_thread(_get_monthly_summary_sync, month, year)


async def log_conversation(
    user_input: str,
    bot_response: str,
    outcome: str,
    discord_link: str,
) -> None:
    """Log a conversation to the Conversation Log tab."""
    await asyncio.to_thread(
        _log_conversation_sync, user_input, bot_response, outcome, discord_link
    )

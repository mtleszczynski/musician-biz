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
        logger.info("Created worksheet '%s' with headers", title)
        return worksheet

    first_row = worksheet.row_values(1)
    if not first_row or first_row[0] != headers[0]:
        worksheet.insert_row(headers, index=1)
        logger.info("Added header row to worksheet '%s'", title)

    return worksheet


def _get_entries_worksheet() -> gspread.Worksheet:
    """Return the Entries worksheet, creating it if needed."""
    spreadsheet = _get_spreadsheet()

    # Rename the default "Sheet1" to "Entries" if that's what exists
    try:
        sheet1 = spreadsheet.worksheet("Sheet1")
        sheet1.update_title(ENTRIES_TAB)
        logger.info("Renamed 'Sheet1' to '%s'", ENTRIES_TAB)
    except gspread.WorksheetNotFound:
        pass

    return _get_or_create_worksheet(spreadsheet, ENTRIES_TAB, HEADERS)


def _get_log_worksheet() -> gspread.Worksheet:
    """Return the Conversation Log worksheet, creating it if needed."""
    spreadsheet = _get_spreadsheet()
    return _get_or_create_worksheet(spreadsheet, LOG_TAB, LOG_HEADERS)


# ---------------------------------------------------------------------------
# Entries tab operations
# ---------------------------------------------------------------------------

def _append_row_sync(row: list[str]) -> int:
    """Append a single row to the Entries sheet. Returns the row number."""
    worksheet = _get_entries_worksheet()
    result = worksheet.append_row(row, value_input_option="USER_ENTERED")
    updated_range = result.get("updates", {}).get("updatedRange", "")
    try:
        row_num = int(updated_range.split("!")[1].split(":")[0][1:])
    except (IndexError, ValueError):
        row_num = len(worksheet.get_all_values())
    logger.info("Appended row %d to Entries sheet", row_num)
    return row_num


def _delete_last_row_sync() -> dict | None:
    """Delete the last data row from the Entries sheet."""
    worksheet = _get_entries_worksheet()
    all_values = worksheet.get_all_values()
    if len(all_values) <= 1:
        return None

    last_row = all_values[-1]
    worksheet.delete_rows(len(all_values))
    logger.info("Deleted last row (row %d) from Entries sheet", len(all_values))
    return {HEADERS[i]: last_row[i] for i in range(min(len(HEADERS), len(last_row)))}


def _get_monthly_summary_sync(month: int, year: int) -> dict:
    """Get income/expense totals by category for a given month."""
    worksheet = _get_entries_worksheet()
    all_values = worksheet.get_all_values()

    income: dict[str, float] = {}
    expenses: dict[str, float] = {}

    # Amount is column index 6 ("Amount ($)")
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


# ---------------------------------------------------------------------------
# Conversation Log tab operations
# ---------------------------------------------------------------------------

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
        user_input[:500],  # Truncate long inputs
        bot_response[:500],
        outcome,
        discord_link,
    ]
    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info("Logged conversation (outcome=%s)", outcome)


# ---------------------------------------------------------------------------
# Async wrappers (gspread is synchronous)
# ---------------------------------------------------------------------------

async def append_entry(
    date: str,
    entry_type: str,
    category: str,
    amount: float,
    discord_link: str,
    client_or_event: str | None = None,
    vendor: str | None = None,
    mode_of_payment: str | None = None,
    description: str = "",
    notes: str = "",
) -> int:
    """Append a financial entry to the Google Sheet. Returns the row number."""
    row = [
        date,
        entry_type.capitalize(),
        category,
        client_or_event or "",
        vendor or "",
        mode_of_payment or "",
        f"{amount:.2f}",
        description,
        notes,
        discord_link,
        datetime.now().isoformat(),
    ]
    return await asyncio.to_thread(_append_row_sync, row)


async def delete_last_entry() -> dict | None:
    """Delete the last entry from the Google Sheet. Returns the deleted data or None."""
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

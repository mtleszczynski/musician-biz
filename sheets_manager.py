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
    "Amount",
    "Description",
    "Student",
    "Payment Method",
    "Discord Link",
    "Raw Summary",
    "Timestamp",
]


def _get_credentials() -> Credentials:
    """Load Google service account credentials from file path or JSON string."""
    creds_value = GOOGLE_CREDENTIALS_JSON
    if not creds_value:
        raise ValueError(
            "GOOGLE_CREDENTIALS_JSON is not set. "
            "Set it to a file path or a JSON string of your service account credentials."
        )

    # Try parsing as JSON string first (for Railway / cloud deployments)
    if creds_value.strip().startswith("{"):
        info = json.loads(creds_value)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    # Fall back to treating it as a file path
    return Credentials.from_service_account_file(creds_value, scopes=SCOPES)


def _get_worksheet() -> gspread.Worksheet:
    """Authorize and return the first worksheet of the configured spreadsheet."""
    creds = _get_credentials()
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.sheet1
    _ensure_headers(worksheet)
    return worksheet


def _ensure_headers(worksheet: gspread.Worksheet) -> None:
    """Add header row if the sheet is empty."""
    first_row = worksheet.row_values(1)
    if not first_row or first_row[0] != HEADERS[0]:
        worksheet.insert_row(HEADERS, index=1)
        logger.info("Created header row in spreadsheet")


def _append_row_sync(row: list[str]) -> int:
    """Append a single row to the sheet. Returns the row number."""
    worksheet = _get_worksheet()
    result = worksheet.append_row(row, value_input_option="USER_ENTERED")
    updated_range = result.get("updates", {}).get("updatedRange", "")
    # Extract row number from range like 'Sheet1!A5:J5'
    try:
        row_num = int(updated_range.split("!")[1].split(":")[0][1:])
    except (IndexError, ValueError):
        row_num = len(worksheet.get_all_values())
    logger.info("Appended row %d to spreadsheet", row_num)
    return row_num


def _delete_last_row_sync() -> dict | None:
    """Delete the last data row from the sheet. Returns the deleted row data or None."""
    worksheet = _get_worksheet()
    all_values = worksheet.get_all_values()
    if len(all_values) <= 1:
        return None  # Only header row or empty

    last_row = all_values[-1]
    worksheet.delete_rows(len(all_values))
    logger.info("Deleted last row (row %d) from spreadsheet", len(all_values))
    return {HEADERS[i]: last_row[i] for i in range(min(len(HEADERS), len(last_row)))}


def _get_monthly_summary_sync(month: int, year: int) -> dict:
    """Get income/expense totals by category for a given month.

    Returns:
        {
            "income": {"Student Payment": 500.0, ...},
            "expenses": {"Sheet Music": 45.99, ...},
            "total_income": 500.0,
            "total_expenses": 45.99,
        }
    """
    worksheet = _get_worksheet()
    all_values = worksheet.get_all_values()

    income: dict[str, float] = {}
    expenses: dict[str, float] = {}

    for row in all_values[1:]:  # Skip header
        if len(row) < 5:
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
            amount = float(row[3].replace("$", "").replace(",", ""))
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


# --- Async wrappers (gspread is synchronous) ---


async def append_entry(
    date: str,
    entry_type: str,
    category: str,
    amount: float,
    description: str,
    discord_link: str,
    raw_summary: str,
    student: str | None = None,
    payment_method: str | None = None,
) -> int:
    """Append a financial entry to the Google Sheet. Returns the row number."""
    row = [
        date,
        entry_type.capitalize(),
        category,
        f"{amount:.2f}",
        description,
        student or "",
        payment_method or "",
        discord_link,
        raw_summary,
        datetime.now().isoformat(),
    ]
    return await asyncio.to_thread(_append_row_sync, row)


async def delete_last_entry() -> dict | None:
    """Delete the last entry from the Google Sheet. Returns the deleted data or None."""
    return await asyncio.to_thread(_delete_last_row_sync)


async def get_monthly_summary(month: int, year: int) -> dict:
    """Get monthly income/expense summary by category."""
    return await asyncio.to_thread(_get_monthly_summary_sync, month, year)

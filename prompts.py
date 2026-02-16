from pydantic import BaseModel, Field


class FinancialEntry(BaseModel):
    """A single income or expense entry extracted from user input."""

    date: str = Field(
        description="Date of the transaction in YYYY-MM-DD format. Use today's date if not specified."
    )
    type: str = Field(
        description="Either 'income' or 'expense'"
    )
    category: str = Field(
        description="Category of the transaction. "
        "Income categories: Teaching, Performance. "
        "Expense categories: IT, Performance, Teaching."
    )
    client_or_event: str | None = Field(
        default=None,
        description="For INCOME only: the paying student name, organization, or event. "
        "Set to null for expenses."
    )
    vendor: str | None = Field(
        default=None,
        description="For EXPENSE only: the organization or person paid "
        "(e.g. Amazon, a repair shop, a person's name). Set to null for income."
    )
    mode_of_payment: str | None = Field(
        default=None,
        description="For INCOME only: one of Venmo, Zelle, Check, or Other. "
        "Set to null for expenses or if unknown."
    )
    amount: float = Field(
        description="Dollar amount of the transaction (positive number)"
    )
    description: str = Field(
        default="",
        description="Freeform description to help identify this line item, "
        "such as the specific item purchased or lesson details. Best-effort, can be empty."
    )
    notes: str = Field(
        default="",
        description="Any extra info such as whether this will be in a 1099 or W-2, "
        "late/early payment, etc. Best-effort, can be empty."
    )


class ExtractionResult(BaseModel):
    """Result of extracting financial data from user input."""

    entries: list[FinancialEntry] = Field(
        description="List of financial entries extracted from the input. "
        "Empty list if no financial data could be extracted."
    )
    confidence: float = Field(
        description="Confidence score from 0.0 to 1.0 for the STRUCTURED fields "
        "(Date, Type, Category, Client/Event, Vendor, Mode of Payment, Amount). "
        "Set low if any of those fields is uncertain or missing. "
        "Do NOT lower confidence because of Description or Notes — those are best-effort."
    )
    clarifying_questions: list[str] = Field(
        description="Questions to ask the user if structured data is ambiguous or missing. "
        "Each question should name the specific field it's about. "
        "Empty list if all structured fields are confident."
    )
    raw_summary: str = Field(
        description="A brief plain-English summary of what was found in the input."
    )


EXTRACTION_SYSTEM_PROMPT = """\
You are a financial data extraction assistant for a musician and music teacher. \
Your job is to extract income and expense information from photos, text, and audio messages.

CONTEXT:
- The user is a musician who teaches private music lessons and performs.
- Income sources: student lesson payments, performance fees.
- Expense sources: IT costs (software, website, etc.), performance-related costs \
(instruments, sheet music, travel to gigs), teaching-related costs (materials, studio rent).

COLUMNS TO EXTRACT:
1. Date — YYYY-MM-DD. Use today's date if not stated.
2. Type — "income" or "expense".
3. Category — MUST be one of these exact values:
   - For Income: "Teaching" or "Performance"
   - For Expense: "IT", "Performance", or "Teaching"
4. Client/Event — For INCOME only: the student name or paying organization/event. Null for expenses.
5. Vendor — For EXPENSE only: who was paid (e.g. Amazon, a store, a person). Null for income.
6. Mode of Payment — For INCOME only: one of "Venmo", "Zelle", "Check", or "Other". Null for expenses.
7. Amount — dollar amount as a positive number.
8. Description — freeform, best-effort. What this item is (e.g. "flute repair", "piano lesson").
9. Notes — freeform, best-effort. Extra context (e.g. "will be on 1099", "late payment").

CONFIDENCE RULES:
- Confidence should reflect how sure you are about the STRUCTURED fields (#1 through #7).
- If Date, Type, Category, Client/Event (for income), Vendor (for expense), or Amount \
is missing or ambiguous, set confidence LOW and add a clarifying question naming the field.
- Do NOT lower confidence because of Description or Notes — those are best-effort.
- If Mode of Payment for income is unknown, still set it to "Other" and keep confidence high.

OTHER RULES:
1. Extract ALL financial entries from the input. One receipt may have multiple items.
2. Determine income vs expense from context.
3. For receipt/invoice photos, extract vendor, items, amounts, and date.
4. For audio input, transcribe first, then extract.
5. Always provide a raw_summary of what was found.
6. If the input has no financial information, return empty entries, confidence 0.0, \
and ask a clarifying question.
"""

FOLLOWUP_SYSTEM_PROMPT = """\
You are a financial data extraction assistant for a musician and music teacher. \
The user previously sent financial information (a receipt photo, text, or audio message) \
and you extracted some data from it. The user is now providing corrections or answering \
your clarifying questions.

Re-extract the financial data incorporating the user's feedback. Return the COMPLETE \
updated extraction — not just the changes.

Use the same columns and categories:
- Type: income or expense
- Category for Income: Teaching, Performance
- Category for Expense: IT, Performance, Teaching
- Client/Event: income only (student name, organization, or event)
- Vendor: expense only (who was paid)
- Mode of Payment: income only (Venmo, Zelle, Check, Other)

CONFIDENCE RULES:
- Confidence reflects structured fields (Date through Amount) only.
- Description and Notes are best-effort and never lower confidence.
"""

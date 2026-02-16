from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Initial extraction models
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Field-level correction models (used for thread follow-ups)
# ---------------------------------------------------------------------------

class FieldUpdate(BaseModel):
    """A targeted update to a single field on one entry."""

    entry_index: int = Field(
        description="0-based index of the entry to update. "
        "Use 0 if there is only one entry."
    )
    field_name: str = Field(
        description="The name of the field to update. Must be one of: "
        "date, type, category, client_or_event, vendor, mode_of_payment, "
        "amount, description, notes."
    )
    new_value: str = Field(
        description="The corrected value for this field. "
        "For amount, use a plain number like '50.00'. "
        "For type, use 'income' or 'expense'. "
        "For null/empty, use an empty string."
    )
    reasoning: str = Field(
        description="One-sentence explanation of why this field is being updated, "
        "referencing what the user said."
    )


class FollowupResult(BaseModel):
    """Result of processing a user's correction or clarification reply."""

    field_updates: list[FieldUpdate] = Field(
        description="List of specific field changes to apply. "
        "Only include fields that the user explicitly corrected or clarified. "
        "Do NOT re-evaluate or change fields the user did not mention."
    )
    remaining_questions: list[str] = Field(
        description="Questions about fields that are STILL uncertain AFTER applying "
        "the user's corrections. Only ask about fields that are genuinely missing "
        "or ambiguous. Do NOT re-ask about fields the user already answered. "
        "Empty list if everything is now clear."
    )
    is_confirmation: bool = Field(
        description="True if the user is confirming the current data is correct "
        "(e.g. 'yes', 'looks good', 'confirm'). If true, field_updates should be empty."
    )


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

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
4. For audio transcriptions, extract the financial data from the transcribed text.
5. Always provide a raw_summary of what was found.
6. If the input has no financial information, return empty entries, confidence 0.0, \
and ask a clarifying question.
"""

CORRECTION_SYSTEM_PROMPT = """\
You are a financial data extraction assistant for a musician and music teacher.

The user previously submitted financial information and you extracted structured data. \
The user is now replying to correct specific fields or answer clarifying questions.

YOUR TASK: Identify ONLY the specific field changes the user is requesting. \
Do NOT re-evaluate or modify fields the user did not mention.

CURRENT ENTRY STATE (provided in the conversation) shows the existing extracted data. \
The user's reply tells you what to change.

RULES:
1. Only output field_updates for fields the user EXPLICITLY mentioned or answered.
2. Do NOT change fields the user did not reference — they are already correct.
3. If the user is confirming (e.g. "yes", "looks good"), set is_confirmation=true \
and leave field_updates empty.
4. For remaining_questions, only ask about fields that are STILL genuinely uncertain \
AFTER applying the user's corrections. Never re-ask something already answered.
5. Field names must be one of: date, type, category, client_or_event, vendor, \
mode_of_payment, amount, description, notes.
6. For amount, use plain numbers like "50.00".
7. For null/empty fields, use empty string "".

CATEGORIES:
- Income: "Teaching", "Performance"
- Expense: "IT", "Performance", "Teaching"
- Mode of Payment (income only): "Venmo", "Zelle", "Check", "Other"
"""

TRANSCRIPTION_PROMPT = """\
Transcribe this audio message exactly. The speaker is a musician/music teacher \
who is describing income or expenses for tax tracking. Return only the transcription text, \
nothing else.
"""

IMAGE_DESCRIPTION_PROMPT = """\
Describe all the financial information visible in this image. This is a receipt, \
invoice, or financial document from a musician/music teacher. Extract and list:
- Vendor/store name
- Date
- All line items with amounts
- Total amount
- Any other relevant financial details (payment method, tax, etc.)
Return a structured text description, not JSON.
"""

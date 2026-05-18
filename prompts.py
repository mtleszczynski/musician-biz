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
        "Expense categories: IT, Performance, Teaching. "
        "ALWAYS pick one yourself based on best available context. NEVER ask "
        "the user to disambiguate Teaching vs Performance — this is a "
        "nice-to-have distinction for accounting and the user prefers a quick "
        "save with a guess over a clarifying question. When genuinely "
        "ambiguous, default to 'Teaching' (her dominant income source)."
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
        "Do NOT re-evaluate or change fields the user did not mention. "
        "MUST be empty if is_retry_request is true."
    )
    remaining_questions: list[str] = Field(
        description="Questions about fields that are STILL uncertain AFTER applying "
        "the user's corrections. Only ask about fields that are genuinely missing "
        "or ambiguous. Do NOT re-ask about fields the user already answered. "
        "Empty list if everything is now clear. "
        "MUST be empty if is_retry_request is true."
    )
    is_confirmation: bool = Field(
        description="True if the user is confirming the current data is correct "
        "(e.g. 'yes', 'looks good', 'confirm'). If true, field_updates should be empty."
    )
    is_retry_request: bool = Field(
        default=False,
        description="True ONLY when the user is asking to RE-EXTRACT the entire "
        "original message from scratch — i.e. they think the bot misread the input "
        "fundamentally and want a fresh attempt against the original photo/text/audio. "
        "Examples that ARE retry requests: 'retry this', 'reprocess', 'try again', "
        "'do it again', 'pick this up', 'start over', 'this is all wrong, redo it', "
        "'you missed everything, try again'. "
        "Examples that are NOT retry requests (these are corrections): "
        "'try $400 as the amount', 'no it was Venmo not Zelle', 'change the date', "
        "'actually the vendor is X' \u2014 anything that names specific fields to fix. "
        "When in doubt, prefer correction (false) over retry (true), because retry "
        "discards the existing extraction. Set field_updates and remaining_questions "
        "to EMPTY when is_retry_request is true."
    )
    is_delete_request: bool = Field(
        default=False,
        description="True ONLY when the user is asking to REMOVE the entry/entries "
        "from the spreadsheet entirely (not edit, not correct, not retry). The bot "
        "will delete the saved rows from the sheet and mark the conversation as "
        "deleted. "
        "Examples that ARE delete requests: 'delete this', 'delete this entry', "
        "'remove this from the sheet', 'remove this entry', 'erase this', "
        "'get rid of this', 'scratch this entry', 'nevermind, remove it', "
        "'remove from spreadsheet'. "
        "Examples that are NOT delete requests: "
        "'change the amount to 0' (correction), 'this is wrong, retry' (retry), "
        "'remove the description but keep the entry' (correction \u2014 ambiguous, "
        "probably a description field update), 'delete the wrong amount, it was $50' "
        "(correction). "
        "is_delete_request and is_retry_request are mutually exclusive \u2014 if both "
        "could apply, choose the more conservative one (retry over delete, "
        "correction over either). When in doubt, prefer false. Set field_updates "
        "and remaining_questions to EMPTY when is_delete_request is true."
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
- If Date, Type, Client/Event (for income), Vendor (for expense), or Amount \
is missing or ambiguous, set confidence LOW and add a clarifying question naming the field.
- Do NOT lower confidence because of Description or Notes — those are best-effort.
- Do NOT lower confidence because of Category ambiguity (see CATEGORY RULES).
- If Mode of Payment for income is unknown, still set it to "Other" and keep confidence high.

CATEGORY RULES (important — the user has explicitly asked for this behavior):
- ALWAYS pick a category yourself. NEVER add a clarifying question about \
Teaching vs Performance.
- For income: if context clearly indicates a performance (e.g. a concert hall, \
venue, recital, payroll from a music organization that hires her to perform), \
pick "Performance". Otherwise pick "Teaching" (her dominant income source).
- For expense: if context clearly indicates IT (software, hosting, website, \
internet, equipment for computing), pick "IT". If it clearly relates to a \
performance (concert tickets, performance clothing, travel to a gig), pick \
"Performance". Otherwise default to "Teaching" (studio rent, sheet music, \
teaching materials, instrument repair, etc.).
- A wrong category is easy for the user to correct in the thread; a blocking \
clarification question is friction the user wants to avoid.

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
The user is now replying. Their reply could be one of FOUR things:

  (a) A CORRECTION — they want to fix specific fields ("change amount to $400").
  (b) A CONFIRMATION — they're saying it's all correct ("yes", "looks good").
  (c) A RETRY REQUEST — they think you misread the entire message and want you \
to re-extract from scratch ("retry this", "try again", "reprocess").
  (d) A DELETE REQUEST — they want the entry/entries REMOVED entirely \
("delete this", "remove this from the sheet", "scratch this").

Choose ONE of these four. Default to (a) correction when in doubt — retry and \
delete are destructive (they discard your current work or remove data from the \
spreadsheet), so only choose them when the user clearly wants that.

YOUR TASK depends on which kind of reply it is:

(a) For CORRECTIONS:
    - Identify ONLY the specific field changes the user is requesting.
    - Do NOT re-evaluate or modify fields the user did not mention.
    - Output field_updates for those fields.
    - Set is_confirmation=false, is_retry_request=false, is_delete_request=false.

(b) For CONFIRMATIONS:
    - Set is_confirmation=true.
    - Leave field_updates and remaining_questions empty.
    - Set is_retry_request=false, is_delete_request=false.

(c) For RETRY REQUESTS:
    - Set is_retry_request=true.
    - Leave field_updates, remaining_questions, and is_confirmation empty/false.
    - Set is_delete_request=false.
    - The orchestrator will discard the current entries and re-extract from the \
original message.

(d) For DELETE REQUESTS:
    - Set is_delete_request=true.
    - Leave field_updates, remaining_questions, and is_confirmation empty/false.
    - Set is_retry_request=false.
    - The orchestrator will remove the saved rows from the spreadsheet and mark \
the conversation as deleted.

CURRENT ENTRY STATE (provided in the conversation) shows the existing extracted data. \
Use it to decide what the user is referring to.

DETAILED RULES:
1. Only output field_updates for fields the user EXPLICITLY mentioned or answered.
2. Do NOT change fields the user did not reference — they are already correct.
3. For remaining_questions, only ask about fields that are STILL genuinely uncertain \
AFTER applying the user's corrections. Never re-ask something already answered.
4. NEVER add a remaining_question about Teaching vs Performance category — the \
user has explicitly asked us to make a call without asking. If you want to flip \
category, just emit a field_update with your best guess; the user can correct \
it back if needed.
5. Field names must be one of: date, type, category, client_or_event, vendor, \
mode_of_payment, amount, description, notes.
6. For amount, use plain numbers like "50.00".
7. For null/empty fields, use empty string "".

DISTINGUISHING CORRECTION FROM RETRY:
- "Try $400 instead" → CORRECTION (amount field). is_retry_request=false.
- "Try again" → RETRY. is_retry_request=true.
- "Retry but change amount to $400" → CORRECTION (the user is naming a specific \
field, not asking to re-extract). is_retry_request=false.
- "This is all wrong, do it again" → RETRY. is_retry_request=true.
- "The date is wrong, redo it" → CORRECTION (date field). is_retry_request=false.

DISTINGUISHING CORRECTION FROM DELETE:
- "Delete this" → DELETE. is_delete_request=true.
- "Remove this entry" → DELETE. is_delete_request=true.
- "Scratch this, get rid of it" → DELETE. is_delete_request=true.
- "Change the amount to 0" → CORRECTION (amount field). is_delete_request=false.
- "Remove the description" → CORRECTION (description field set to empty). \
is_delete_request=false.
- "Delete the wrong amount, it was $50" → CORRECTION (amount field to $50). \
is_delete_request=false.
- "Erase this" / "Erase the entry" → DELETE.
- "Erase the notes field" → CORRECTION (notes field set to empty).

DISTINGUISHING DELETE FROM RETRY:
- "Delete this" → DELETE (just remove, don't re-extract).
- "This is all wrong" → RETRY (re-extract from original).
- "Wrong amount, retry" → RETRY (re-extract from original).
- "Wrong, delete it" → DELETE.

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

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
        "Income categories: Student Payment, Performance Fee, Workshop, Royalties, Other Income. "
        "Expense categories: Sheet Music, Instruments, Travel, Studio Rent, "
        "Software/Subscriptions, Professional Development, Marketing, Other Expense."
    )
    amount: float = Field(
        description="Dollar amount of the transaction (positive number)"
    )
    description: str = Field(
        description="Brief description of the transaction"
    )
    student: str | None = Field(
        default=None,
        description="Student name if this is a student payment, otherwise null"
    )
    payment_method: str | None = Field(
        default=None,
        description="Payment method if identifiable (Cash, Check, Venmo, Zelle, Credit Card, etc.), otherwise null"
    )


class ExtractionResult(BaseModel):
    """Result of extracting financial data from user input."""

    entries: list[FinancialEntry] = Field(
        description="List of financial entries extracted from the input. "
        "Empty list if no financial data could be extracted."
    )
    confidence: float = Field(
        description="Confidence score from 0.0 to 1.0 on how accurate the extraction is. "
        "Set low if the image is blurry, the audio is unclear, or key info is missing."
    )
    clarifying_questions: list[str] = Field(
        description="Questions to ask the user if data is ambiguous or missing. "
        "Empty list if everything is clear."
    )
    raw_summary: str = Field(
        description="A brief plain-English summary of what was found in the input."
    )


EXTRACTION_SYSTEM_PROMPT = """\
You are a financial data extraction assistant for a musician and music teacher. \
Your job is to extract income and expense information from photos, text, and audio messages.

CONTEXT:
- The user is a musician who teaches private music lessons and performs.
- Income sources: student lesson payments, performance fees, workshop fees, royalties.
- Expense sources: sheet music, instruments/accessories, travel to gigs/lessons, \
studio rent, software subscriptions, professional development, marketing.

RULES:
1. Extract ALL financial entries from the input. One receipt may contain multiple items.
2. If a date is not explicitly stated, use today's date.
3. Determine whether each entry is income or expense based on context.
4. Choose the most appropriate category from the predefined list.
5. If you see a receipt or invoice photo, extract the vendor, items, amounts, and date.
6. If you hear or read about a student payment, extract the student name, amount, and lesson details.
7. If something is unclear or ambiguous, set confidence LOW and add specific clarifying questions.
8. For audio input, first transcribe what was said, then extract the financial data.
9. Always provide a raw_summary describing what you found in the input.
10. If the input doesn't seem to contain financial information, set entries to empty, \
confidence to 0.0, and ask a clarifying question.

CATEGORIES:
Income: Student Payment, Performance Fee, Workshop, Royalties, Other Income
Expenses: Sheet Music, Instruments, Travel, Studio Rent, Software/Subscriptions, \
Professional Development, Marketing, Other Expense
"""

FOLLOWUP_SYSTEM_PROMPT = """\
You are a financial data extraction assistant for a musician and music teacher. \
The user previously sent financial information (a receipt photo, text, or audio message) \
and you extracted some data from it. The user is now providing corrections or answering \
your clarifying questions.

Re-extract the financial data incorporating the user's feedback. Return the COMPLETE \
updated extraction — not just the changes.

Use the same categories:
Income: Student Payment, Performance Fee, Workshop, Royalties, Other Income
Expenses: Sheet Music, Instruments, Travel, Studio Rent, Software/Subscriptions, \
Professional Development, Marketing, Other Expense
"""

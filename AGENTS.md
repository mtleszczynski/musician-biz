# AGENTS.md — AI Agent Context for Musician Expense Tracker

> This file provides context for AI coding agents (Cursor, Copilot, etc.)
> working on this project. **Update this file when making architectural changes.**

## What This Project Does

A Discord bot that helps a musician/music teacher track income and expenses for taxes.
She sends photos of receipts, text descriptions, or voice messages to a Discord channel.
The bot uses Google Gemini to extract financial data, auto-saves high-confidence entries
to a Google Sheet, and asks clarifying questions in a Discord thread when unsure.

## Architecture

```
Discord Channel ──message──▶ Discord Bot (main.py)
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
              download     download     get text
              images       audio        content
                    │           │           │
                    └───────────┼───────────┘
                                ▼
                     Gemini 3 Flash (gemini_processor.py)
                     structured JSON extraction
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
              High confidence         Low confidence
              Auto-save to sheet      Ask in thread
                    │                       │
                    ▼                       ▼
              Google Sheets           User clarifies
              "Entries" tab           then → auto-save
                    │
                    ▼
              "Conversation Log" tab
```

## Key Decisions and Rationale

| Decision | Choice | Why |
|----------|--------|-----|
| Chat platform | Discord | Free, excellent thread support for tracking conversations per entry |
| LLM | Gemini 3 Flash | Multimodal (vision+audio+text), thinking/reasoning, cheap, free tier |
| Data store | Google Sheets | User wanted spreadsheet for taxes, easy to share with accountant |
| Language | Python 3.11+ | Best Discord bot ecosystem (discord.py), good Google API support |
| Hosting | Railway | Discord bots need persistent WebSocket — Vercel is serverless, can't do this |
| Sheets auth | Service account | No OAuth flow needed, just share sheet with service account email |
| Structured output | Pydantic + response_schema | Forces Gemini to return valid JSON matching our schema |
| Auto-confirm | Yes, at high confidence | Reduces friction — user doesn't have to type "yes" every time |

## File Responsibilities

| File | Purpose |
|------|---------|
| `main.py` | Discord bot entry point. Event handlers, commands, auto-confirm flow, emoji reactions |
| `gemini_processor.py` | Gemini API integration. Sends multimodal content, returns structured ExtractionResult |
| `sheets_manager.py` | Google Sheets: Entries tab (financial data) + Conversation Log tab (interaction history) |
| `prompts.py` | System prompts for Gemini + Pydantic models (FinancialEntry, ExtractionResult) |
| `config.py` | Environment variable loading (auto-extracts spreadsheet ID from full URLs) |
| `Procfile` / `railway.toml` | Railway deployment configuration |

## Spreadsheet Schema ("Entries" tab)

| Column | Description |
|--------|-------------|
| Date | YYYY-MM-DD |
| Type | Income or Expense |
| Category | Income: Teaching, Performance / Expense: IT, Performance, Teaching |
| Client/Event | Income only: student name or paying organization |
| Vendor | Expense only: who was paid |
| Mode of Payment | Income only: Venmo, Zelle, Check, or Other |
| Amount ($) | Dollar amount |
| Description | Freeform — what this item is |
| Notes | Freeform — extra context (1099/W-2, late payment, etc.) |
| Discord Link | Link to the Discord thread for this entry |
| Timestamp | When the row was added |

## Conversation Log ("Conversation Log" tab)

| Column | Description |
|--------|-------------|
| Timestamp | When the conversation occurred |
| User Input | The original user message (truncated to 500 chars) |
| Bot Response | What the bot showed/extracted (truncated to 500 chars) |
| Outcome | auto-confirmed, user-confirmed, corrected, or error |
| Discord Link | Link to the thread |

## Data Flow for a New Entry

1. User sends message in the configured Discord channel
2. `on_message` in `main.py` calls `handle_new_entry()`
3. Bot reacts with ⏳ (hourglass), creates a thread, downloads attachments
4. `gemini_processor.extract_financial_data()` sends content to Gemini 3 Flash
5. Gemini returns `ExtractionResult` (entries, confidence, clarifying questions)
6. **If confident** (>= threshold, no clarifying questions):
   - Auto-saves to Entries tab immediately
   - Posts "Saved!" summary in thread
   - Reacts with ✅ on original message
   - Logs to Conversation Log as "auto-confirmed"
7. **If uncertain** (low confidence or has questions):
   - Posts summary + clarifying questions in thread
   - Reacts with 💬 (speech bubble) on original message
   - User replies → `handle_thread_reply()` re-extracts
   - Once confident → saves, reacts ✅, logs to Conversation Log
8. **On error**: reacts with ❌ (only for actual crashes, never for "needs input")

## Emoji Reactions Guide

| Emoji | Meaning |
|-------|---------|
| ⏳ | Bot is processing |
| ✅ | Entry saved to spreadsheet |
| 💬 | Waiting for user input/clarification |
| ❌ | Actual error (API failure, crash) |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Bot token from Discord Developer Portal |
| `CHANNEL_ID` | Yes | ID of the Discord channel to monitor |
| `GEMINI_API_KEY` | Yes | API key from Google AI Studio |
| `SPREADSHEET_ID` | Yes | Google Sheet ID (or full URL — auto-extracted) |
| `GOOGLE_CREDENTIALS_JSON` | Yes | Service account creds (JSON string or file path) |
| `GEMINI_MODEL` | No | Model name (default: `gemini-3-flash-preview`) |
| `CONFIDENCE_THRESHOLD` | No | Min confidence for auto-confirm (default: `0.8`) |

## Common Tasks

### Adding a new category
Edit the category lists in `prompts.py` (both in `FinancialEntry.category` field description
and in `EXTRACTION_SYSTEM_PROMPT`).

### Changing the LLM model
Set the `GEMINI_MODEL` env var. No code changes needed.

### Adding a new command
Add a `@bot.command()` function in `main.py`, following the pattern of existing commands.

### Adding a new column to the spreadsheet
1. Add the column name to `HEADERS` in `sheets_manager.py`
2. Add the field to `append_entry()` and the row list in `sheets_manager.py`
3. If it comes from Gemini, add it to the `FinancialEntry` Pydantic model in `prompts.py`
4. Update `format_entry()` in `main.py` to display the new field

## Conventions

- **Async everywhere**: The Discord bot is async. Use `await` for all I/O.
  gspread is sync, so it's wrapped with `asyncio.to_thread()`.
- **Logging**: Use the `logging` module, not `print()`.
- **Type hints**: All function signatures should have type hints.
- **Error handling**: Catch exceptions in event handlers, report to user via Discord, log the traceback.
- **Confidence**: Only structured fields (Date through Amount) affect confidence.
  Description and Notes are best-effort and never trigger clarification.

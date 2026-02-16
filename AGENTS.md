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
Discord Channel ──message──▶ main.py (thin dispatcher)
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
              download     download     get text
              images       audio        content
                    │           │           │
                    └───────────┼───────────┘
                                ▼
                     entry_manager.py (orchestration)
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
              gemini_processor  db.py     sheets_manager
              (transcribe,    (SQLite)   (Google Sheets)
               extract,
               correct)
                    │
                    ▼
              prompts.py (system prompts + Pydantic models)
```

### Data Flow — New Entry
1. User sends message → main.py downloads attachments, creates thread
2. entry_manager.create_entry() orchestrates everything:
   - Transcribes audio via Gemini (cached in SQLite, never re-sent as bytes)
   - Describes images via Gemini (cached in SQLite for followup corrections)
   - Extracts financial data via Gemini (images sent as bytes for accuracy)
   - Stores conversation state in SQLite
   - If confident: saves to Google Sheet, returns "saved" result
   - If uncertain: returns "needs clarification" result with questions
3. main.py posts the response in the thread and sets emoji

### Data Flow — Thread Reply (Correction)
1. User replies in thread → main.py downloads any new attachments
2. entry_manager.process_reply() orchestrates:
   - Loads conversation state from SQLite (instant, no re-downloading)
   - If confirmation word: saves existing entries to sheet
   - If new media: full extraction of new content only
   - If text correction: **field-level update** via Gemini (NOT full re-extraction)
     - Gemini identifies ONLY the fields to change
     - Unchanged fields stay locked (no regression)
   - Updates sheet in-place (no delete-then-append)
3. main.py posts response and updates emoji

## Key Decisions and Rationale

| Decision | Choice | Why |
|----------|--------|-----|
| Chat platform | Discord | Free, excellent thread support for tracking conversations per entry |
| LLM | Gemini 3 Flash | Multimodal (vision+audio+text), thinking/reasoning, cheap, free tier |
| Data store | Google Sheets | User wanted spreadsheet for taxes, easy to share with accountant |
| Local state | SQLite (aiosqlite) | Persistent across restarts, no external DB service needed |
| Language | Python 3.11+ | Best Discord bot ecosystem (discord.py), good Google API support |
| Hosting | Fly.io | Discord bots need persistent WebSocket; Fly.io supports volumes for SQLite |
| Sheets auth | Service account | No OAuth flow needed, just share sheet with service account email |
| Structured output | Pydantic + response_schema | Forces Gemini to return valid JSON matching our schema |
| Auto-confirm | Yes, at high confidence | Reduces friction — user doesn't have to type "yes" every time |
| Corrections | Field-level updates | Prevents regression of already-correct fields |
| Sheet updates | In-place update | Prevents data loss from delete-before-write pattern |
| Audio | Transcribe once, cache | Avoids re-sending slow audio bytes on every followup |

## File Responsibilities

| File | Purpose |
|------|---------|
| `main.py` | Discord event dispatcher. Downloads attachments, manages emoji, creates threads. Thin — all logic delegated to entry_manager. |
| `entry_manager.py` | Entry lifecycle orchestration. Creates entries, processes corrections, confirms, saves. Coordinates db + gemini + sheets. |
| `gemini_processor.py` | Gemini API integration. Transcribes audio, describes images, extracts financial data, processes field-level corrections. |
| `sheets_manager.py` | Google Sheets CRUD. Append, update-in-place, safe-replace. Each channel maps to its own tab (e.g. "Entries", "Test Entries"). Sync gspread wrapped in asyncio.to_thread(). |
| `db.py` | SQLite persistence. Conversations (entry state), media_cache (transcriptions/descriptions), conversation_messages (thread history). |
| `prompts.py` | System prompts + Pydantic models. FinancialEntry, ExtractionResult (initial), FieldUpdate, FollowupResult (corrections). Tightly coupled with prompts. |
| `config.py` | Environment variable loading + logging setup. |
| `Dockerfile` / `fly.toml` | Fly.io deployment configuration with persistent volume for SQLite. |

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

## Multi-Channel Routing

Each Discord channel maps to its own sheet tab via `CHANNEL_TAB_MAP` in `config.py`:
- `PROD_CHANNEL_ID` -> "Entries" tab (production)
- `TEST_CHANNEL_ID` -> "Test Entries" tab (testing)

The tab name is stored in the SQLite `conversations` table so that thread replies
automatically route to the correct tab without needing the channel ID again.

## SQLite Schema (db.py)

| Table | Purpose |
|-------|---------|
| conversations | Entry state: thread_id, entries JSON, confidence, status, sheet row numbers, tab_name |
| media_cache | Cached transcriptions (audio) and descriptions (images) by message_id |
| conversation_messages | Full thread history (role + content) for Gemini context in corrections |

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
| `PROD_CHANNEL_ID` | Yes | Production channel ID -> "Entries" tab |
| `TEST_CHANNEL_ID` | No | Testing channel ID -> "Test Entries" tab |
| `GEMINI_API_KEY` | Yes | API key from Google AI Studio |
| `SPREADSHEET_ID` | Yes | Google Sheet ID (or full URL — auto-extracted) |
| `GOOGLE_CREDENTIALS_JSON` | Yes | Service account creds (JSON string or file path) |
| `GEMINI_MODEL` | No | Model name (default: `gemini-3-flash-preview`) |
| `CONFIDENCE_THRESHOLD` | No | Min confidence for auto-confirm (default: `0.8`) |
| `DB_PATH` | No | SQLite database path (default: `./bot.db` locally, set to `/data/bot.db` on Fly.io) |

## Deployment (Fly.io)

The bot runs on Fly.io with a persistent volume for the SQLite database.

- `fly.toml` — App config with `[mounts]` for the `/data` volume
- `Dockerfile` — Python 3.11-slim, creates `/data` directory
- Volume must be created once: `fly volumes create bot_data --region lax --size 1`
- Set `DB_PATH=/data/bot.db` in Fly.io secrets
- Deploy: `fly deploy`

## Common Tasks

### Adding a new category
Edit the category lists in `prompts.py` (both in `FinancialEntry.category` field description
and in `EXTRACTION_SYSTEM_PROMPT` and `CORRECTION_SYSTEM_PROMPT`).

### Changing the LLM model
Set the `GEMINI_MODEL` env var. No code changes needed.

### Adding a new command
Add a `@bot.command()` function in `main.py`, following the pattern of existing commands.

### Adding a new column to the spreadsheet
1. Add the column name to `HEADERS` in `sheets_manager.py`
2. Update `_build_row()` in `sheets_manager.py`
3. If it comes from Gemini, add it to the `FinancialEntry` Pydantic model in `prompts.py`
4. Update `_format_entry()` in `entry_manager.py` to display the new field

## Conventions

- **Async everywhere**: The Discord bot is async. Use `await` for all I/O.
  gspread is sync, so it's wrapped with `asyncio.to_thread()`.
  SQLite uses aiosqlite (native async).
- **Logging**: Use the `logging` module with structured context: `thread=X op=Y | message`.
  Timing: log elapsed time for Gemini calls and sheet operations.
- **Type hints**: All function signatures should have type hints.
- **Error handling**: Catch exceptions in event handlers, report to user via Discord, log the traceback.
- **Confidence**: Only structured fields (Date through Amount) affect confidence.
  Description and Notes are best-effort and never trigger clarification.
- **Corrections**: Use field-level updates (FollowupResult), NOT full re-extraction.
  Never delete sheet rows before new rows are written.

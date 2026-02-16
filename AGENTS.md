# AGENTS.md — AI Agent Context for Musician Expense Tracker

> This file provides context for AI coding agents (Cursor, Copilot, etc.)
> working on this project. **Update this file when making architectural changes.**

## What This Project Does

A Discord bot that helps a musician/music teacher track income and expenses for taxes.
She sends photos of receipts, text descriptions, or voice messages to a Discord channel.
The bot uses Google Gemini to extract financial data, asks clarifying questions in a
Discord thread, and writes confirmed entries to a Google Sheet.

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
                                ▼
                     Discord Thread (clarify / confirm)
                                │
                                ▼
                     Google Sheets (sheets_manager.py)
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

## File Responsibilities

| File | Purpose |
|------|---------|
| `main.py` | Discord bot entry point. Event handlers, commands, message routing, thread management |
| `gemini_processor.py` | Gemini API integration. Sends multimodal content, returns structured ExtractionResult |
| `sheets_manager.py` | Google Sheets CRUD. Append rows, delete last row, monthly summary |
| `prompts.py` | System prompts for Gemini + Pydantic models (FinancialEntry, ExtractionResult) |
| `config.py` | Environment variable loading |
| `Procfile` / `railway.toml` | Railway deployment configuration |

## Data Flow for a New Entry

1. User sends message in the configured Discord channel
2. `on_message` in `main.py` calls `handle_new_entry()`
3. Bot reacts with ⏳, creates a thread, downloads attachments
4. `gemini_processor.extract_financial_data()` sends content to Gemini 3 Flash
5. Gemini returns `ExtractionResult` (entries, confidence, clarifying questions)
6. Bot posts formatted summary in the thread
7. If confidence >= threshold: asks for confirmation ("Reply yes to confirm")
8. If confidence < threshold: asks clarifying questions
9. User replies in thread → `handle_thread_reply()` processes it
10. On confirmation → `sheets_manager.append_entry()` writes to Google Sheet
11. Bot reacts with ✅ on the original message

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Bot token from Discord Developer Portal |
| `CHANNEL_ID` | Yes | ID of the Discord channel to monitor |
| `GEMINI_API_KEY` | Yes | API key from Google AI Studio |
| `SPREADSHEET_ID` | Yes | Google Sheet ID (from the URL) |
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
2. Add the field to the `append_entry()` function's row list
3. If it comes from Gemini, add it to the `FinancialEntry` Pydantic model in `prompts.py`

## Conventions

- **Async everywhere**: The Discord bot is async. Use `await` for all I/O.
  gspread is sync, so it's wrapped with `asyncio.to_thread()`.
- **Logging**: Use the `logging` module, not `print()`.
- **Type hints**: All function signatures should have type hints.
- **Error handling**: Catch exceptions in event handlers, report to user via Discord, log the traceback.

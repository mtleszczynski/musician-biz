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
 - If conversation is missing (orphan from OOM): returns status=`retry_orphan`
 - If status is `pending_retry_confirm`: routes to "confirm destructive retry" gate
 - If confirmation word: saves existing entries to sheet
 - If new media: full extraction of new content only
 - If text correction: **field-level update** via Gemini (NOT full re-extraction)
 - Gemini ALSO classifies retry intent (`is_retry_request`); if true, returns
 status=`retry_active` (or `retry_needs_confirm` if entry is already saved)
 - Gemini identifies ONLY the fields to change
 - Unchanged fields stay locked (no regression)
 - Updates sheet in-place (no delete-then-append)
3. main.py posts response and updates emoji
4. If status is one of the retry_* family, main.py routes to `_handle_retry()`
 which fetches the parent message and re-runs `entry_manager.create_entry()`
 against it in the same thread (after deleting saved sheet rows on confirmed
 destructive retries).

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
| Image preprocessing | Resize to ≤1600px before Gemini | Cuts a 4K phone photo from ~3MB to ~300KB — keeps the 256MB Fly VM out of OOM territory |
| Message concurrency | `asyncio.Semaphore(1)` | One in-flight extraction at a time. Burst posts get queued; users see ⏳ but no OOM kill |
| Gemini call hang protection | `asyncio.wait_for(..., 90s)` per attempt | Prevents silent infinite hangs from leaving the hourglass forever |
| Startup recovery | Repost stuck bot responses | If bot died between `db.add_message` and `thread.send`, on restart it reposts the saved reply with a "I crashed earlier" prefix |

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

### Category disambiguation policy
By explicit user request, Gemini must **never** ask the user to disambiguate
Teaching vs Performance — it picks one. Defaults: **Teaching** for income
(her dominant source) and **Teaching** for expense (covers studio rent,
materials, repairs, etc.). "Performance" or "IT" only when context clearly
indicates them. Category ambiguity must NOT lower extraction confidence
either, so it doesn't block auto-save. See `CATEGORY RULES` in
`EXTRACTION_SYSTEM_PROMPT` and rule #4 in `CORRECTION_SYSTEM_PROMPT`. The
trade-off is intentional: a wrong category is trivial to correct in-thread;
a blocking clarification is friction the user explicitly asked us to avoid.

### Changing the LLM model
Set the `GEMINI_MODEL` env var. No code changes needed.

### Adding a new command
Add a `@bot.command()` function in `main.py`, following the pattern of existing commands.

### Adding a new column to the spreadsheet
1. Add the column name to `HEADERS` in `sheets_manager.py`
2. Update `_build_row()` in `sheets_manager.py`
3. If it comes from Gemini, add it to the `FinancialEntry` Pydantic model in `prompts.py`
4. Update `_format_entry()` in `entry_manager.py` to display the new field

## Duplicate detection contract (`entry_manager._find_duplicates`)

An extracted entry is treated as a duplicate of a recent sheet row only when
**all three** conditions hold:

1. Amount equal within $0.01.
2. Date **exactly** equal (same calendar day).
3. A **name match** — same client/event OR same vendor (substring
 either way, case-insensitive).

Design rationale (we've iterated twice):

- We previously also matched on `type+category` alone. That dropped real
 income because different students paying the same rate on adjacent days
 were collapsed (the 2026-02-22 Guangling Li $240 incident). **Removed.**
- We previously allowed `±1 day` on the date to catch receipt OCR errors.
 That collapsed weekly-recurring payments from the same client (e.g.
 Guangling Li sends $240 every Sunday — entries 7 days apart, but if one
 extraction lands on Saturday and another on Sunday, the ±1d window
 silently merged them). **Removed** — bank texts and check dates are
 reliable; the OCR-error scenario this guarded was theoretical.

The bias is intentional:

- **False negative tolerated** (a real duplicate row slips through) — user
 deletes it manually, low cost.
- **False positive avoided** (a real entry silently dropped) — could mean
 missing tax records the user never notices, high cost.

Dedupe matches are logged at INFO level with `op=dedupe_match` plus the
matched fields, so future false positives are diagnosable from the logs
without re-running the bot.

Tests: `.investigation/test_dedupe.py` covers both historical incidents
and is a good place to add new edge cases if/when they show up.

## Conventions

- **Async everywhere**: The Discord bot is async. Use `await` for all I/O.
 gspread is sync, so it's wrapped with `asyncio.to_thread()`.
 SQLite uses aiosqlite (native async).
 Pillow (image resize) is CPU-bound, also wrapped with `asyncio.to_thread()`.
- **Logging**: Use the `logging` module with structured context: `thread=X op=Y | message`.
 Timing: log elapsed time for Gemini calls and sheet operations.
- **Type hints**: All function signatures should have type hints.
- **Error handling**: Catch exceptions in event handlers, report to user via Discord, log the traceback.
- **Confidence**: Only structured fields (Date through Amount) affect confidence.
 Description and Notes are best-effort and never trigger clarification.
- **Corrections**: Use field-level updates (FollowupResult), NOT full re-extraction.
 Never delete sheet rows before new rows are written.

## Memory & Reliability Constraints (512MB Fly VM)

The bot runs on a `shared-cpu-1x:512mb` Fly VM (bumped from 256mb on
2026-05-18 after two OOMs on legitimate 4MB phone photos; the code-level
mitigations below stay in place regardless). Several patterns exist to keep peak memory
safely below the OOM ceiling and to recover gracefully when things go wrong:

- **Image resize** (`gemini_processor.resize_image`): every image is downscaled
 to ≤1600px on its longest side and re-encoded as JPEG before being sent to
 Gemini. A 4K phone photo drops from ~3MB to ~300KB. Run in a thread pool
 because Pillow is CPU-bound. Failures fall back silently to original bytes.
 Uses Pillow's `draft()` to ask the JPEG decoder for a lower-resolution decode
 directly (libjpeg native scaling), cutting peak memory by ~50% versus full-res
 decode + downscale. The intermediate BytesIO is closed explicitly in a finally
 block.
- **Upfront image preprocessing** (`gemini_processor.preprocess_images`):
 `entry_manager.create_entry` and `process_reply` call this AT THE TOP to
 resize every image once and immediately drop the original (multi-MB) bytes
 via `images.clear()` + `gc.collect()`. The describe/extract calls downstream
 then operate on ~300KB images, avoiding holding the large originals AND
 paying decode cost twice. This fix shipped after a 4MB phone photo OOM-killed
 the bot (incident 2026-05-18 05:04).
- **Serialised processing** (`PROCESSING_LOCK` in main.py): a global
 `asyncio.Semaphore(1)` wraps both `handle_new_entry` and `handle_thread_reply`.
 Concurrent message bursts queue up rather than running in parallel. The
 hourglass emoji is set BEFORE acquiring the lock so the user has feedback.
- **Eager byte cleanup** (`entry_manager.create_entry` /
 `entry_manager.process_reply`): image bytes are explicitly cleared and
 `gc.collect()` is called immediately after extraction so the working set
 shrinks back to baseline before the next message.
- **Gemini timeout** (`gemini_processor.GEMINI_CALL_TIMEOUT`): every
 `generate_content` call is wrapped in `asyncio.wait_for(..., 90s)`.
 Timeouts are retried (with the same backoff as transient API errors).
- **Startup recovery** (`main.recover_stuck_conversations`): if the bot died
 between `db.add_message` and `thread.send` (the OOM-kill window), the
 conversation row has `created_at == updated_at` and the response text is
 sitting in `conversation_messages`. On `on_ready` we scan for these,
 repost the saved bot response prefixed with ":pushpin: _I crashed before I
 could reply earlier..._", update the original message's emoji from ⏳ to 💬,
 and call `db.touch_conversation` so we don't double-post on the next restart.
 Capped at `RECOVERY_MAX_AGE_DAYS = 30` to avoid surprising the user with
 ancient messages.

If the bot starts OOMing again despite the 512MB VM + these mitigations,
the next step is bumping `fly.toml` from `'512mb'` to `'1024mb'`. Don't
revert to 256MB even if it looks stable — a single 4MB image is enough to
trigger an OOM there (see 2026-05-18 incidents in the log).

### Memory monitoring

- **Fly memory graph**: https://fly.io/apps/musician-expenses-bot/monitoring
  (look for steady idle around 100–120MB; spikes during processing should
  cap around 130–150MB)
- **Recent OOM events** (any `exit_code=137`):
  ```bash
  ~/.fly/bin/flyctl machine status 8e7d3df779e1d8 --app musician-expenses-bot | tail -20
  ```
- **Current live memory inside the VM**:
  ```bash
  ~/.fly/bin/flyctl machine exec 8e7d3df779e1d8 \
    "sh -c 'head -3 /proc/meminfo; for p in /proc/[0-9]*; do \
     name=\$(head -1 \$p/status 2>/dev/null | cut -f2); \
     rss=\$(grep VmRSS \$p/status 2>/dev/null | awk \"{print \\\$2}\"); \
     [ -n \"\$rss\" ] && [ \"\$rss\" -gt 5000 ] && echo \"\$rss kB  \$name\"; \
     done | sort -rn | head -5'" \
    --app musician-expenses-bot
  ```

**Bump triggers (512mb → 1024mb)** — bump memory if any of these become true:
- Another `exit_code=137` event appears in `flyctl machine status`
- Idle Python RSS creeps above ~200MB (was ~106MB on baseline)
- We add PDF support (PDFs can be 5–20MB and Pillow can't shrink them)
- We bump the processing semaphore above 1
- Workload shifts to multi-image messages (each 4MB image still spikes ~50MB)

## @-mention contract (`main._with_mention_if_needed` / `_format_mention`)

When the bot needs the user to take action (answer a clarifying question,
confirm a destructive retry, see an error), it prefixes the message with a
Discord `<@user_id>` mention of the **original poster** so they get a push
notification on mobile/desktop.

The mapping is intentionally driven by `ProcessingResult.status` (so the
decision lives close to the semantics, not scattered across send sites):

- `status == "pending_clarification"` → mention
- `status == "error"` → mention
- All other statuses (`saved`, `skipped`, `retry_*` intros) → no mention

In addition, **two special prompts always mention** regardless of status,
because they're high-stakes and the cost of being missed is high:

- `_request_retry_confirmation` (touches the spreadsheet)
- `recover_stuck_conversations` (the user may have posted this hours/days
 ago and is waiting on a reply)

The user_id is captured from `message.author.id` when `handle_new_entry`
runs and stored on the `conversations.user_id` column. For **legacy conversation
rows** (created before this column existed) or any row that has `user_id IS
NULL`, the `_resolve_user_id()` helper falls back to fetching the parent
message via Discord API and using `parent.author.id`, then backfills the DB
via `db.set_user_id()` so the cost is paid at most once per thread. This way
even old threads get correctly @-mentioned on the first prompt after restart.

In the retry path, when create_entry runs on the parent message, we use
`parent.author.id` so a fresh conversation row is associated with the right
person, even if a different user typed "retry".

## Sheet ↔ SQLite consistency

SQLite holds the bot's view of conversations; the Google Sheet is the
user-facing source of truth. Two mechanisms keep them in sync:

1. **Eager sheet sync on corrections to saved entries** — when a thread
 reply triggers `field_updates` and the conversation is already saved
 (`sheet_row_numbers` populated), `entry_manager.process_reply` mirrors
 the change to the sheet immediately via `update_entry_in_place()` — even
 if Gemini also asked a follow-up clarifying question. This prevents drift
 where SQLite has the latest correction but the sheet still shows the old
 value. Motivating incident: 2026-05-06 "amount: 350 → 0" correction
 followed by a clarifying question — the sheet kept $350 and SQLite kept
 $0 until a later delete confirmation exposed the inconsistency.

2. **Live preview for destructive prompts** — `_request_delete_confirmation`
 calls `sheets_manager.get_rows_data()` to display the *actual current
 sheet contents*, not the SQLite state. This way any source of drift
 (manual sheet edits, race conditions, future bugs) can't mislead the user
 about what's about to be deleted. Falls back to SQLite preview gracefully
 if the live fetch fails.

## Natural-language delete

The user can ask the bot to remove an entry from the spreadsheet using free-form
language ("delete this", "remove this entry", "scratch this", "get rid of it",
etc.). Detection strategy parallels the retry feature:

- **Gemini classifies** the reply via `FollowupResult.is_delete_request`. The
 prompt teaches it to distinguish:
   - "delete this" → DELETE
   - "change amount to 0" → CORRECTION (field update, not delete)
   - "remove the description" → CORRECTION (description field → empty)
   - "delete the wrong amount, it was $50" → CORRECTION
- **Unsaved entries** (status=`pending_clarification`, no sheet rows): no
 confirmation needed; conv is marked `deleted` and a 🗑️ "discarded" message
 is posted.
- **Saved entries** (status=`saved` with sheet rows): a confirmation gate
 (status=`pending_delete_confirm`) protects against accidental loss. The
 prompt shows a preview of the entries to be deleted and waits for explicit
 `yes`. Any other reply cancels and reverts to `saved`.
- **No undo** by design — if the user deletes by mistake, they can use
 `retry` to re-extract from the parent message (Gemini may return slightly
 different output) or manually re-add to the sheet.

Delete orchestration lives in `main._handle_delete()` (similar to
`_handle_retry`). It calls `sheets_manager.delete_rows()` to remove the rows,
updates the conversation row to `status='deleted'`, posts a 🗑️ confirmation,
and flips the parent message's emoji from ✅ to 🗑️.

After deletion, **replies in the same thread are politely refused** with
"This entry was deleted. To create a new one, send a fresh message in the
main channel." Uses a dedicated `deleted_ack` status so main.py knows to
acknowledge the user's message (✅ on their reply) but leave the parent's
🗑️ alone.

The mention contract applies: the confirmation prompt always @-mentions
(high-stakes), the post-delete message and refusal don't (no further action
needed from the user).

## Natural-language retry

Inside a thread, the user can ask the bot to **re-extract from the original
parent message** using free-form language ("retry this", "reprocess", "try
again from scratch", "this is mangled, do it over", etc.). Detection
strategy:

- **Orphan threads** (no SQLite conversation row, typically because an OOM
 killed the bot before `db.create_conversation`): ANY reply triggers retry —
 it's the only useful thing we can do. Handled in `entry_manager.process_reply`
 by returning `status='retry_orphan'`.
- **Active threads** (conversation exists): Gemini classifies the reply via a
 new `FollowupResult.is_retry_request` bool. Field-level corrections like
 "change amount to $400" stay as corrections; phrases like "try again" or
 "this is wrong, redo it" become retries.
- **Saved entries** (sheet rows exist): a confirmation gate (`pending_retry_confirm`
 status) protects against accidental destructive retries. The bot tells the
 user how many sheet rows would be deleted and waits for explicit "yes". Any
 other reply cancels and reverts to saved.

Retry orchestration lives in `main._handle_retry()` because it needs Discord
context (fetching the parent message via `bot.fetch_channel` →
`channel.fetch_message(thread.id)`). It deletes any existing conversation
row + chat history (`db.delete_conversation`) and calls
`entry_manager.create_entry()` against the parent message inside the
existing thread, then posts the result and updates the parent emoji.

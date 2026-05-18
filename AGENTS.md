# AGENTS.md — AI Agent Context for Musician Expense Tracker

> Context for AI coding agents (Cursor, Copilot, etc.) working on this project.
> **Update this file when making architectural changes.** Lighter conventions
> also live in `.cursor/rules/project.mdc` — keep both files coherent.

## What this is

A Discord bot that helps a musician/music teacher track income and expenses
for taxes. She sends photos of receipts, text descriptions, or voice messages
to a Discord channel. The bot uses Google Gemini to extract financial data,
auto-saves high-confidence entries to a Google Sheet, and asks clarifying
questions in a Discord thread when uncertain.

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

### Data flow — new entry (main channel post)

1. `main.handle_new_entry` adds ⏳ to the user's message, acquires the
   `PROCESSING_LOCK` semaphore (one in-flight at a time), downloads attachments,
   creates a thread, captures `message.author.id`.
2. `entry_manager.create_entry` orchestrates:
   - Transcribes audio (cached in SQLite — never re-sent as bytes).
   - **Preprocesses images upfront** via `gemini_processor.preprocess_images`
     (resize to ≤1600px JPEG), frees originals via `images.clear()` + `gc.collect()`.
   - Describes resized images via Gemini (cached in SQLite).
   - Extracts financial data via Gemini (also resized images).
   - Stores conversation in SQLite with the original poster's `user_id`.
   - If confident: dedupe-check, then save to Google Sheet → `status=saved`.
   - If uncertain: → `status=pending_clarification` with questions.
3. `main.handle_new_entry` posts the response (`@`-mentioning the poster
   if the status needs action), sets emoji on user's message and parent.

### Data flow — thread reply (correction / retry / delete / confirm)

1. `main.handle_thread_reply` adds ⏳, acquires the semaphore, pre-loads the
   conversation's `user_id` (with backfill via `_resolve_user_id` for legacy rows).
2. `entry_manager.process_reply`:
   1. If conv is **None** (orphan thread, usually OOM-killed before save):
      return `retry_orphan`.
   2. If conv status is **`deleted`**: refuse politely → `deleted_ack`.
   3. If text is a skip/discard word and not in a pending-confirm state: mark
      `skipped` and stop.
   4. If text is **affirmative** (`yes`, `yep`, `yes!`, etc.) and not in a
      pending-confirm state: save via `_confirm_and_save`.
   5. If conv status is **`pending_retry_confirm`**: affirmative → `retry_confirmed`;
      anything else → revert to `saved` and post cancellation.
   6. If conv status is **`pending_delete_confirm`**: same pattern → `delete_confirmed`
      or revert.
   7. Otherwise → Gemini correction call. Gemini classifies into one of four:
      - `is_retry_request` → `retry_active` (or `retry_needs_confirm` if saved)
      - `is_delete_request` → `delete_needs_confirm` (saved) or marks `deleted` immediately (unsaved)
      - `is_confirmation` → save
      - Otherwise: apply `field_updates`, ask any `remaining_questions`.
3. **For already-saved entries with field updates**, the sheet is updated
   in-place IMMEDIATELY (even if there are remaining clarifying questions).
   Prevents the SQLite-vs-sheet drift that bit us on 2026-05-06.
4. `main` routes the result:
   - `retry_*` → `_handle_retry` (fetches parent, deletes conv, re-runs `create_entry`)
   - `delete_*` → `_handle_delete` (deletes sheet rows, marks conv `deleted`)
   - `deleted_ack` → just acknowledge, don't touch parent emoji
   - Everything else → post response (mention if action needed), update emojis

## State and contracts

### Pydantic models (`prompts.py`)

| Model | Purpose |
|-------|---------|
| `FinancialEntry` | Single income/expense row. Fields: date, type, category, client_or_event, vendor, mode_of_payment, amount, description, notes. |
| `ExtractionResult` | First-pass output: `entries`, `confidence`, `clarifying_questions`, `raw_summary`. |
| `FieldUpdate` | A targeted change to one field on one entry, with `reasoning`. |
| `FollowupResult` | Thread-reply output: `field_updates`, `remaining_questions`, `is_confirmation`, `is_retry_request`, `is_delete_request`. The boolean flags are **mutually exclusive** — at most one is true. When in doubt, all false (correction). |

### `ProcessingResult.status` values (entry_manager → main.py)

| Status | Meaning | Mention on output? |
|---|---|---|
| `saved` | Entries written to sheet | no |
| `skipped` | User discarded the entry, nothing written | no |
| `pending_clarification` | Bot asked a question, waiting for user | **yes** |
| `error` | Caught exception with a user-facing message | **yes** |
| `retry_orphan` | No conv row exists; main.py should retry from parent | (handled by `_handle_retry`) |
| `retry_active` | User explicitly asked retry on unsaved/safe conv | (handled by `_handle_retry`) |
| `retry_needs_confirm` | User asked retry on saved entry — needs `yes` first | (handled by `_request_retry_confirmation`) |
| `retry_confirmed` | User just confirmed a destructive retry | (handled by `_handle_retry`) |
| `delete_needs_confirm` | User asked delete on saved entry — needs `yes` first | (handled by `_request_delete_confirmation`) |
| `delete_confirmed` | User just confirmed a destructive delete | (handled by `_handle_delete`) |
| `deleted_ack` | Reply in already-deleted thread, polite refusal | no (don't touch parent emoji) |

### `conversations.status` values (db.py)

| Status | Meaning |
|---|---|
| `pending_clarification` | Initial state; bot is asking questions OR was OOM-killed mid-pipeline |
| `saved` | Entries are on the sheet |
| `skipped` | User discarded; nothing on the sheet |
| `deleted` | User confirmed delete; sheet rows removed |
| `pending_duplicate_review` | All entries flagged as duplicates; awaiting "yes" or "skip" |
| `pending_retry_confirm` | User asked retry on saved entry; awaiting "yes" |
| `pending_delete_confirm` | User asked delete on saved entry; awaiting "yes" |

### Emoji reactions

| Emoji | Meaning | Set by |
|---|---|---|
| ⏳ | Bot is processing | `handle_new_entry`, `handle_thread_reply` |
| ✅ | Entry saved to sheet (or reply acknowledged) | post-processing in main.py |
| 💬 | Waiting for user input | post-processing when status is pending |
| ❌ | Caught error | exception handlers |
| ❓ | Message had no extractable content | `_handle_new_entry_locked` |
| 🗑️ | Entries were deleted by the user | `_handle_delete` |

`set_reaction` (main.py) blind-removes all known bot emojis before adding
the new one — it does NOT rely on `message.reactions` cache (which can be
stale on slow operations and was leaving 🪟+✅ alongside each other on
retries).

## Critical contracts

These are invariants that future changes MUST preserve.

### Sheet ↔ SQLite consistency

The Google Sheet is the user-facing source of truth; SQLite is the bot's
view. Two mechanisms keep them in sync:

1. **Eager sheet sync on corrections to saved entries** (`entry_manager.process_reply`):
   when a thread reply triggers `field_updates` on an already-saved conversation,
   `update_entry_in_place` runs immediately — even if Gemini also asked a
   follow-up question. Previously the sheet only updated when all questions
   were resolved, which caused drift (see 2026-05-06 incident).
2. **Live preview for destructive prompts** (`_request_delete_confirmation`):
   the delete confirmation reads `sheets_manager.get_rows_data()` — the
   actual current sheet contents — for its preview. So any other source of
   drift (manual sheet edits, race conditions) can't mislead the user about
   what's about to be deleted. Falls back to SQLite preview if the live
   fetch fails.

### Duplicate detection (`entry_manager._find_duplicates`)

An extracted entry is a duplicate of a recent sheet row **only when all
three** hold:

1. Amount equal within $0.01.
2. Date **exactly** equal (same calendar day).
3. A **name match** — same client/event OR same vendor (substring either way,
   case-insensitive).

Iteration history:

- Originally also matched on `type+category` alone → dropped real income
  because different students paying the same rate on adjacent days were
  collapsed. **Removed.**
- Originally allowed `±1 day` on the date → collapsed weekly recurring
  payments from the same client. **Removed** — bank texts and check dates
  are reliable; the OCR-error scenario this guarded was theoretical.

The bias is intentional:

- **False negative** (a real duplicate slips through) → user deletes the
  extra row. Low cost.
- **False positive** (a real entry silently dropped) → missing tax record
  the user may never notice. High cost.

Every dedupe match is logged at INFO with `op=dedupe_match` plus matched
fields. Tests: `.investigation/test_dedupe.py`.

### Memory safety (512MB Fly VM)

The bot runs on `shared-cpu-1x:512mb` (bumped from 256mb on 2026-05-18
after two OOMs on legitimate 4MB phone photos). Code mitigations stay in
place regardless:

- **`Pillow draft()` mode** in `_resize_image_sync` — asks libjpeg for a
  lower-resolution decode directly, cutting peak memory by ~50% versus
  full-res decode + downscale. Intermediate BytesIO closed in `finally`.
- **Upfront image preprocessing** (`preprocess_images`): `entry_manager`
  resizes ALL images at the top of `create_entry` / `process_reply`, then
  `images.clear()` + `gc.collect()` to free the multi-MB originals BEFORE
  describe/extract runs.
- **Serialised processing** (`PROCESSING_LOCK` = `asyncio.Semaphore(1)`):
  one in-flight extraction at a time. Hourglass is set BEFORE acquiring
  the lock so queued users still see feedback.
- **Gemini timeout** (`GEMINI_CALL_TIMEOUT = 90s`): every `generate_content`
  call is wrapped in `asyncio.wait_for(..., 90)`. Timeouts are retried.
- **Startup recovery** (`main.recover_stuck_conversations`): if the bot
  died between `db.add_message` and `thread.send`, the conv row has
  `created_at == updated_at` and the response text is in `conversation_messages`.
  On `on_ready`, scan for these, repost the saved response with a "I crashed
  earlier" prefix, flip parent emoji to 💬, `db.touch_conversation` so we
  don't double-post. Capped at `RECOVERY_MAX_AGE_DAYS = 30`.
- **`!recover` admin command** ignores the age cap for one-off recoveries.

**Bump triggers (512mb → 1024mb)** — bump memory if any become true:

- Another `exit_code=137` event in `flyctl machine status`
- Idle Python RSS creeps above ~200MB (baseline was ~106MB)
- We add PDF support (PDFs can be 5–20MB; Pillow can't shrink them)
- We bump `PROCESSING_LOCK` above semaphore(1)
- Workload shifts to multi-image messages (each 4MB image still spikes ~50MB)

**Never revert to 256MB** — a single 4MB image is enough to OOM there.

### @-mentions on input-needed prompts

Whenever the bot sends a prompt that needs user action, it prefixes the
message with a Discord `<@user_id>` mention of the **original poster**
(captured at thread creation, stored on `conversations.user_id`). The
mapping is driven by `ProcessingResult.status`:

- `status == "pending_clarification"` → mention
- `status == "error"` → mention
- All other statuses → no mention

In addition, **three special prompts always mention** regardless of status,
because they're high-stakes:

- `_request_retry_confirmation` (touches the spreadsheet)
- `_request_delete_confirmation` (touches the spreadsheet)
- `recover_stuck_conversations` (may be hours/days old)

For **legacy conversation rows** with `user_id IS NULL` (created before the
column existed), `_resolve_user_id()` falls back to fetching the parent
message via Discord API and using `parent.author.id`, then backfills the
DB via `db.set_user_id()` so the cost is paid at most once per thread.

In the retry path, `_handle_retry` uses `parent.author.id` so a fresh
conversation row is associated with the right person even if a different
user typed "retry".

## User-facing features

### Auto-recovery on startup

See [Memory safety](#memory-safety-512mb-fly-vm) above. Run admin manually
via `!recover [max_age_days]` from any monitored channel (owner-only).

### Natural-language retry

User says "retry this" / "try again" / "reprocess" / etc. in a thread. Gemini
classifies via `FollowupResult.is_retry_request`.

- **Orphan threads** (no SQLite conv): ANY reply triggers retry — only useful action.
- **Active threads** (conv exists, not saved): retry immediately.
- **Saved entries** (sheet rows exist): `pending_retry_confirm` gate; user
  must explicitly type `yes`. Any other reply cancels and reverts to `saved`.

Orchestrator: `main._handle_retry`. Fetches parent, deletes existing conv,
calls `create_entry` in same thread.

### Natural-language delete

User says "delete this" / "remove this entry" / "scratch this" / etc.
Gemini classifies via `FollowupResult.is_delete_request`. Distinguished
from corrections by explicit prompt examples ("change amount to 0" is a
correction, not delete; "remove the description" is a correction).

- **Unsaved entries**: no confirmation; mark `deleted`, post 🗑️ message.
- **Saved entries**: `pending_delete_confirm` gate with a **live sheet preview**;
  user types `yes`. Any other reply cancels.
- **After delete**: parent emoji → 🗑️, conv `status=deleted`, further replies
  in the thread get a polite refusal (`deleted_ack`).
- **No undo by design**: user can use `retry` to re-extract from the parent
  if they deleted by mistake (Gemini may give slightly different output).

Orchestrator: `main._handle_delete`. Uses `sheets_manager.delete_rows()`.

### Category auto-pick (no clarification questions)

By explicit user request, Gemini **never** asks the user to disambiguate
Teaching vs Performance — it picks one. Defaults: **Teaching** for income
(her dominant source) and **Teaching** for expense (covers studio rent,
materials, repairs). "Performance" or "IT" only when context clearly
indicates them. Category ambiguity must NOT lower extraction confidence
either, so it doesn't block auto-save. See `CATEGORY RULES` in
`EXTRACTION_SYSTEM_PROMPT` and rule #4 in `CORRECTION_SYSTEM_PROMPT`.

A wrong category is trivial to correct in-thread; a blocking clarification
is friction the user explicitly asked us to avoid.

### `_is_affirmative` parsing

User confirmations accept variations: `yes`, `yes!`, `Yes.`, `yes please`,
`yep`, `yeah`, `confirm`, `ok`, `y`. The helper strips trailing punctuation
and accepts the first word of short multi-word replies. **It does NOT**
match "yes but the amount is wrong" (too long, has substantive content) —
that falls through to Gemini for proper correction handling.

## Reference

### File responsibilities

| File | Purpose |
|------|---------|
| `main.py` | Discord event dispatcher. Downloads attachments, manages emoji, creates threads, orchestrates retry/delete flows. Thin — extraction logic lives in entry_manager. |
| `entry_manager.py` | Entry lifecycle. Creates entries, processes corrections, runs dedupe, coordinates DB + Gemini + sheets. The central coordinator. |
| `gemini_processor.py` | Gemini API. Transcribe audio, describe images, extract financial data, process field-level corrections. Image resize + preprocessing. Retry + timeout wrapping. |
| `sheets_manager.py` | Google Sheets CRUD. Append, update-in-place, safe-replace, delete-rows, get-rows-data. Sync `gspread` wrapped in `asyncio.to_thread`. |
| `db.py` | SQLite via `aiosqlite`. Conversations (entry state + tab_name + user_id), media_cache (transcriptions/descriptions), conversation_messages (thread history). |
| `prompts.py` | System prompts + Pydantic models. Tightly coupled — change prompts and schemas together. |
| `config.py` | Env var loading, logging setup, channel→tab map. |
| `Dockerfile` / `fly.toml` | Fly.io deployment configuration with persistent volume for SQLite. |

### Spreadsheet schema ("Entries" tab)

| Column | Description |
|--------|-------------|
| Date | YYYY-MM-DD |
| Type | Income or Expense |
| Category | Income: Teaching, Performance · Expense: IT, Performance, Teaching |
| Client/Event | Income only: student or paying organization |
| Vendor | Expense only: who was paid |
| Mode of Payment | Income only: Venmo, Zelle, Check, or Other |
| Amount ($) | Dollar amount |
| Description | Freeform — what this item is |
| Notes | Freeform — extra context (1099/W-2, late payment, etc.) |
| Discord Link | Link to the originating Discord message |
| Timestamp | When the row was added |

### SQLite schema

| Table | Purpose |
|-------|---------|
| `conversations` | Entry state. Columns: thread_id, message_url, original_text, status, entries_json, confidence, questions_json, raw_summary, sheet_rows_json, tab_name, **user_id**, created_at, updated_at. |
| `media_cache` | Cached transcriptions (audio) and descriptions (images) by message_id. |
| `conversation_messages` | Full thread history (role + content) for Gemini context in corrections. |

### Multi-channel routing

Each Discord channel maps to its own sheet tab via `CHANNEL_TAB_MAP` in
`config.py`:

- `PROD_CHANNEL_ID` → "Entries" tab
- `TEST_CHANNEL_ID` → "Test Entries" tab

The tab name is stored on the SQLite conversation so replies route
automatically without re-checking the channel.

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Bot token from Discord Developer Portal |
| `PROD_CHANNEL_ID` | Yes | Production channel ID → "Entries" tab |
| `TEST_CHANNEL_ID` | No | Testing channel ID → "Test Entries" tab |
| `GEMINI_API_KEY` | Yes | API key from Google AI Studio |
| `SPREADSHEET_ID` | Yes | Google Sheet ID (full URL also accepted — auto-extracted) |
| `GOOGLE_CREDENTIALS_JSON` | Yes | Service account creds (JSON string or file path) |
| `GEMINI_MODEL` | No | Default `gemini-3-flash-preview` |
| `CONFIDENCE_THRESHOLD` | No | Default `0.8` |
| `DB_PATH` | No | Default `./bot.db` locally; set `/data/bot.db` on Fly.io |

### Deployment (Fly.io)

```
fly volumes create bot_data --region lax --size 1   # one-time
fly secrets set DB_PATH=/data/bot.db                # one-time
fly deploy                                          # each time
```

Watch deploy + recovery in real time:

```bash
~/.fly/bin/flyctl logs --app musician-expenses-bot
```

## Conventions

- **Async everywhere**: bot is async. `await` for all I/O. `gspread` is sync,
  wrap in `asyncio.to_thread()`. `Pillow` (CPU-bound), same. SQLite uses
  native-async `aiosqlite`.
- **Logging**: `logging` with structured context — `thread=X op=Y | message`.
  Log elapsed time for Gemini calls and sheet ops. INFO for milestones
  (`op=dedupe_match`, `op=resize`, `op=retry | Done`, etc.), WARNING for
  recoverable oddities, ERROR (with traceback) for caught exceptions.
- **Type hints**: all function signatures.
- **Error handling**: catch exceptions in event handlers, report to user via
  Discord, log the traceback. NEVER let an exception crash the bot loop.
- **Confidence**: only structured fields (Date through Amount) affect
  confidence. Description, Notes, and Category never lower it.
- **Corrections**: field-level updates only (FollowupResult). Never re-extract.
- **Saved entries**: sheet update mirrors SQLite immediately on every
  correction (don't defer until questions are resolved).
- **Destructive actions** (retry-on-saved, delete-on-saved): require an
  explicit `_is_affirmative` confirmation via a pending-confirm gate.
- **`amount` field updates**: drop them silently if unparseable. NEVER
  store `""` — it makes the entry un-saveable (see 2026-05-07 incident).
- **Imports**: stdlib → third-party → local.

## Common tasks

### Adding a new category
1. Update `FinancialEntry.category` description in `prompts.py`.
2. Update `EXTRACTION_SYSTEM_PROMPT` (`CATEGORY RULES`) in `prompts.py`.
3. Update `CORRECTION_SYSTEM_PROMPT` in `prompts.py`.
4. No code changes elsewhere.

### Adding a new column to the spreadsheet
1. Add column name to `HEADERS` in `sheets_manager.py`.
2. Update `_build_row()` in `sheets_manager.py`.
3. Add field to `FinancialEntry` Pydantic model in `prompts.py`.
4. Update `_format_entry()` in `entry_manager.py` to render it.
5. Update `_get_rows_data_sync` and `_get_recent_entries_sync` if needed.

### Adding a new bot command
Add a `@bot.command()` function in `main.py` following the existing pattern.
For admin-only commands use `@commands.is_owner()` (see `!recover`).

### Changing the LLM model
Set `GEMINI_MODEL` env var. No code changes.

### Memory monitoring

- **Fly dashboard**: https://fly.io/apps/musician-expenses-bot/monitoring
  (idle baseline ~106 MB, per-message peak ~150 MB on 512mb VM)
- **Recent OOM events**:
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

### Running scripts against the live DB

The `.investigation/` folder (gitignored) holds ad-hoc query scripts and
unit tests used during incident investigation. Pattern for running a
Python script against the live bot's environment:

```bash
~/.fly/bin/flyctl ssh sftp shell --app musician-expenses-bot <<EOF
put .investigation/my_script.py /data/my_script.py
EOF
~/.fly/bin/flyctl machine exec 8e7d3df779e1d8 --app musician-expenses-bot \
  --timeout 60 'python /data/my_script.py'
```

To pull a snapshot of `bot.db`:

```bash
~/.fly/bin/flyctl ssh sftp get /data/bot.db ./bot.db.snapshot --app musician-expenses-bot
```

## Recent incidents & learnings

Real-world bugs we hit during the 2026-05-17 polish session, with the
specific fixes. Useful for understanding *why* the code is shaped the way
it is.

| Date | Symptom | Root cause | Fix |
|------|---------|------------|-----|
| 2026-05-03 | Multiple stuck threads showing only ⏳, no bot reply | Bot OOM-killed on 4MB phone photos; SIGKILL bypassed the exception handler | Image resize + semaphore + upfront preprocessing + startup recovery (reposts saved-but-undelivered responses) |
| 2026-05-06 | Bot's "Updated 1 entry: amount 350 → 0" later showed $0 in a delete preview, but sheet still had $350 | Sheet only updated when no `remaining_questions`; SQLite drifted from sheet | Eager sheet sync on corrections to saved entries (regardless of remaining questions) |
| 2026-05-07 | "Remove it entirely" silently corrupted the entry; future operations on it crashed | Gemini returned `""` for amount; the `except ValueError: pass` left it as `""`; `_build_row` crashed on `float("")` | Drop unparseable amount field_updates with a WARNING log instead of silently storing |
| 2026-02-22 | $240 Guangling Li payment silently skipped as "duplicate" of $240 Reina and Angel | `_find_duplicates` matched on `type+category` alone (any income+Teaching at $240 on adjacent days collapsed) | Remove the `type_category_match` fallback; require client OR vendor name match |
| 2026-03-15 / 03-21 | Weekly recurring $240 Guangling Li payments silently skipped | Date matching was "within 1 day"; consecutive-week entries 6-8 days apart collided when one date was extracted slightly off | Tighten date match to **exact**; bank texts and check dates are reliable |
| 2026-05-18 | Long retries left ⏳ stuck alongside ✅ on the user's reply | `set_reaction` iterated `message.reactions` (stale cache during long ops) | Blind-remove all known bot emojis instead of trusting the cache |
| 2026-05-18 | Hourglass on parent never updated to 💬 when bot asked for retry confirmation | `_request_retry_confirmation` didn't flip parent emoji | Call `_update_original_reaction` from the confirm helper |
| 2026-05-18 | Replying "yes!" cancelled a destructive retry instead of confirming | `text_to_check in CONFIRM_WORDS` literal match didn't accept trailing punctuation | New `_is_affirmative()` helper that strips punctuation and accepts short multi-word forms |
| 2026-05-18 | OOM persisted even after Pillow `draft()` and preprocessing fixes | Code-level mitigations got peak down from ~200MB to ~150MB, still right at 256MB ceiling | Bump VM to 512mb; ~$2/mo |
| 2026-05-18 | "yes" in `pending_delete_confirm` crashed with `float("")` instead of deleting | Step-5 confirm fast-path excluded `pending_retry_confirm` but not `pending_delete_confirm` — routed "yes" to save path instead of delete gate | Exclude all pending-confirm statuses from both step-4 (skip) and step-5 (confirm) fast-paths |
| 2026-05-18 | Confirmation prompts didn't @-mention on legacy threads (created pre-`user_id` column) | `conv.user_id` was NULL → `_format_mention(None)` returned `""` | `_resolve_user_id()` with fetch-and-backfill fallback |

The dedupe tests in `.investigation/test_dedupe.py` and `_is_affirmative`
tests in `.investigation/test_affirmative.py` lock in the fixes for the
top items. Add a test there if you fix a future false-positive case.

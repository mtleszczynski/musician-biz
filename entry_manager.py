"""Entry lifecycle management — the central orchestration layer.

Handles: create entry, process corrections, confirm, save to sheet.
Coordinates between db.py (state), gemini_processor.py (AI), and
sheets_manager.py (Google Sheets). main.py delegates all logic here.
"""

import gc
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import db
import gemini_processor
import sheets_manager
from config import CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)

# Words that count as confirmation from the user
CONFIRM_WORDS = frozenset({
    "yes", "y", "confirm", "correct", "ok", "looks good",
    "lgtm", "approve", "yep", "yeah",
})

# Strip trailing punctuation/emoji-ish chars before checking for confirmation
# words, so "yes!" / "Yes." / "yes," all count.
_TRAILING_PUNCT_RE = re.compile(r"[!?.,;:\s\u2019\u2018\u201c\u201d]+$")
# Max length for the "first word counts" heuristic. Short messages like
# "yes please" should confirm, but "yes but the amount is wrong" must NOT —
# that's a correction with caveats, which Gemini will handle properly.
_AFFIRMATIVE_FIRST_WORD_MAX_LEN = 20


def _is_affirmative(text: str | None) -> bool:
    """Return True if `text` is unambiguously a confirmation reply.

    Accepts variations users actually type ("yes!", "Yes.", "yes please",
    "yeah") without being so loose that "yes but the amount is wrong" gets
    misclassified as confirmation. When in doubt, return False — the
    downstream Gemini correction call handles edge cases correctly.
    """
    if not text:
        return False
    cleaned = _TRAILING_PUNCT_RE.sub("", text.strip().lower()).strip()
    if not cleaned:
        return False
    if cleaned in CONFIRM_WORDS:
        return True
    if len(cleaned) <= _AFFIRMATIVE_FIRST_WORD_MAX_LEN:
        first_word = cleaned.split(maxsplit=1)[0]
        if first_word in CONFIRM_WORDS:
            return True
    return False


@dataclass
class ProcessingResult:
    """Returned to main.py so it knows what to post in Discord."""

    response_text: str = ""
    status: str = "pending_clarification"  # saved | pending_clarification | error
    entries_saved: int = 0
    fields_changed: list[str] = field(default_factory=list)
    audio_transcription: str | None = None


# ---------------------------------------------------------------------------
# Formatting helpers (for Discord messages)
# ---------------------------------------------------------------------------

def _format_entry(entry: dict, index: int | None = None) -> str:
    """Format a single entry dict for display in Discord."""
    prefix = f"**Entry {index}:**\n" if index is not None else ""
    lines = [
        f"• **Date:** {entry.get('date', '?')}",
        f"• **Type:** {str(entry.get('type', '?')).capitalize()}",
        f"• **Category:** {entry.get('category', '?')}",
    ]
    if entry.get("client_or_event"):
        lines.append(f"• **Client/Event:** {entry['client_or_event']}")
    if entry.get("vendor"):
        lines.append(f"• **Vendor:** {entry['vendor']}")
    if entry.get("mode_of_payment"):
        lines.append(f"• **Payment:** {entry['mode_of_payment']}")

    amount = entry.get("amount", 0)
    if isinstance(amount, str):
        try:
            amount = float(amount.replace("$", "").replace(",", ""))
        except ValueError:
            amount = 0
    lines.append(f"• **Amount:** ${amount:,.2f}")

    if entry.get("description"):
        lines.append(f"• **Description:** {entry['description']}")
    if entry.get("notes"):
        lines.append(f"• **Notes:** {entry['notes']}")
    return prefix + "\n".join(lines)


def _format_result_message(
    entries: list[dict],
    questions: list[str],
    raw_summary: str,
    saved: bool = False,
) -> str:
    """Build the full Discord message for an extraction result."""
    if not entries:
        msg = f"I wasn't able to extract any financial data.\n\n> {raw_summary}"
        if questions:
            msg += "\n\n**I have some questions:**\n"
            for q in questions:
                msg += f"• {q}\n"
        return msg

    count = len(entries)
    if saved:
        header = f"**Saved {count} {'entry' if count == 1 else 'entries'} to the spreadsheet!**\n"
    else:
        header = f"**Here's what I found ({count} {'entry' if count == 1 else 'entries'}):**\n"

    entry_blocks = []
    for i, entry in enumerate(entries, start=1):
        label = i if count > 1 else None
        entry_blocks.append(_format_entry(entry, label))

    msg = header + "\n\n".join(entry_blocks)

    if questions:
        msg += "\n\n**I have some questions:**\n"
        for q in questions:
            msg += f"• {q}\n"
        msg += "\nPlease answer the questions above, or tell me what to fix."
    elif saved:
        msg += "\n\n_Reply here if anything needs to be corrected._"
    else:
        msg += "\n\nReply **yes** to confirm, or tell me what needs to be corrected."

    return msg


def _format_correction_message(
    entries: list[dict],
    fields_changed: list[str],
    remaining_questions: list[str],
    saved: bool = False,
) -> str:
    """Build a Discord message after applying corrections."""
    count = len(entries)

    if saved:
        header = "**Updated and saved!**\n"
        if fields_changed:
            header += "Changes: " + ", ".join(fields_changed) + "\n"
    else:
        header = f"**Updated {count} {'entry' if count == 1 else 'entries'}:**\n"
        if fields_changed:
            header += "Changes: " + ", ".join(fields_changed) + "\n"

    entry_blocks = []
    for i, entry in enumerate(entries, start=1):
        label = i if count > 1 else None
        entry_blocks.append(_format_entry(entry, label))

    msg = header + "\n" + "\n\n".join(entry_blocks)

    if remaining_questions:
        msg += "\n\n**Still need to clarify:**\n"
        for q in remaining_questions:
            msg += f"• {q}\n"
        msg += "\nPlease answer the above, or tell me what to fix."
    elif saved:
        msg += "\n\n_Reply here if anything else needs to be corrected._"
    else:
        msg += "\n\nReply **yes** to confirm, or tell me what needs to be corrected."

    return msg


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

SKIP_WORDS = frozenset({
    "skip", "no", "discard", "don't save", "duplicate", "already added",
    "don't add", "nope",
})


def _find_duplicates(
    new_entries: list[dict],
    recent_entries: list[dict],
) -> list[tuple[int, dict]]:
    """Compare new entries against recent sheet entries for potential duplicates.

    Returns list of (new_entry_index, matching_existing_entry) pairs.

    Match criteria (ALL must hold):
      1. Amount equal within $0.01.
      2. Date EXACTLY equal (same calendar day, no fuzziness).
      3. A NAME match: same client/event OR same vendor (substring either way,
         case-insensitive).

    Design history:
      - We previously also matched on type+category alone. That dropped real
        income because different students paying the same rate on adjacent
        days were collapsed. Removed.
      - We previously allowed ±1 day on the date to catch receipt OCR errors.
        That collapsed weekly-recurring payments from the same client (e.g.
        Guangling Li sends $240 every Sunday — entries 7 days apart, but if
        one extraction lands on Saturday and another on Sunday, the 1-day
        window silently merged them). Removed.

    The bias is intentional:
      - False negative (a real duplicate slips through) → user sees an extra
        row, deletes it manually. Low cost.
      - False positive (a real entry silently dropped) → user may never
        notice the missing tax record. High cost.
    """
    matches: list[tuple[int, dict]] = []

    for i, new_entry in enumerate(new_entries):
        new_amount = new_entry.get("amount", 0)
        if isinstance(new_amount, str):
            try:
                new_amount = float(str(new_amount).replace("$", "").replace(",", ""))
            except ValueError:
                new_amount = 0.0

        new_date_str = new_entry.get("date", "")
        try:
            datetime.strptime(new_date_str, "%Y-%m-%d")
        except ValueError:
            continue  # No usable date — can't safely dedupe

        new_client = (new_entry.get("client_or_event") or "").strip().lower()
        new_vendor = (new_entry.get("vendor") or "").strip().lower()

        for existing in recent_entries:
            # Amount must match within $0.01
            if abs(new_amount - existing["amount"]) > 0.01:
                continue

            # Date must be EXACTLY equal (same calendar day)
            if existing.get("date") != new_date_str:
                continue

            existing_client = (existing.get("client_or_event") or "").strip().lower()
            existing_vendor = (existing.get("vendor") or "").strip().lower()

            client_match = bool(
                new_client and existing_client
                and (new_client in existing_client or existing_client in new_client)
            )
            vendor_match = bool(
                new_vendor and existing_vendor
                and (new_vendor in existing_vendor or existing_vendor in new_vendor)
            )

            if client_match or vendor_match:
                reason = "client" if client_match else "vendor"
                logger.info(
                    "op=dedupe_match | entry[%d] (%s | %s | $%.2f) matched existing "
                    "row (%s | %s | $%.2f) via %s name (exact date)",
                    i, new_date_str,
                    new_client or new_vendor or "(no name)",
                    new_amount,
                    existing.get("date"),
                    existing_client or existing_vendor or "(no name)",
                    existing["amount"],
                    reason,
                )
                matches.append((i, existing))
                break  # One match per new entry is enough

    return matches


def _format_duplicate_warning(
    entries: list[dict],
    duplicates: list[tuple[int, dict]],
) -> str:
    """Build a Discord warning message about potential duplicate entries."""
    lines: list[str] = []

    # Show the extracted entries first
    count = len(entries)
    lines.append(f"**Here's what I found ({count} {'entry' if count == 1 else 'entries'}):**\n")
    for i, entry in enumerate(entries, start=1):
        label = i if count > 1 else None
        lines.append(_format_entry(entry, label))

    # Show duplicate warnings
    lines.append("\n\n:warning: **Possible duplicate detected!**\n")
    lines.append("This looks similar to existing entries:\n")

    for new_idx, existing in duplicates:
        entry_label = f"Entry {new_idx + 1}" if count > 1 else "This entry"
        existing_amount = existing['amount']
        existing_desc = existing.get('description', '')
        existing_client = existing.get('client_or_event', '')
        existing_vendor = existing.get('vendor', '')
        who = existing_client or existing_vendor or ""

        detail = f"{existing['date']} | {existing.get('category', '?')}"
        if who:
            detail += f" | {who}"
        detail += f" | ${existing_amount:,.2f}"
        if existing_desc:
            detail += f" | {existing_desc}"

        lines.append(f"• **{entry_label}** matches: {detail}")
        if existing.get("discord_link"):
            lines.append(f"  Thread: {existing['discord_link']}")

    lines.append("\nReply **yes** to save anyway, or **skip** to discard.")

    return "\n".join(lines)


def _format_duplicate_hold_message(
    entries: list[dict],
    duplicates: list[tuple[int, dict]],
) -> str:
    """Warning when all entries are duplicates at save time (entries already shown)."""
    lines = [":warning: **Hold on — these entries appear to already be in the spreadsheet:**\n"]

    for new_idx, existing in duplicates:
        entry = entries[new_idx]
        amount = entry.get("amount", 0)
        if isinstance(amount, str):
            try:
                amount = float(str(amount).replace("$", "").replace(",", ""))
            except ValueError:
                amount = 0
        who = entry.get("client_or_event") or entry.get("vendor") or ""
        detail = f"{entry.get('date', '?')}"
        if who:
            detail += f" | {who}"
        detail += f" | ${amount:,.2f}"
        lines.append(f"• {detail}")
        if existing.get("discord_link"):
            lines.append(f"  Existing thread: {existing['discord_link']}")

    lines.append("\nReply **yes** to save anyway, or **skip** to discard.")
    return "\n".join(lines)


def _format_skipped_duplicates(
    all_entries: list[dict],
    skipped: list[tuple[int, dict]],
) -> str:
    """Format info about entries that were auto-skipped as duplicates."""
    count = len(skipped)
    lines = [f"\n:white_check_mark: **Skipped {count} likely duplicate{'s' if count > 1 else ''}:**"]
    for new_idx, existing in skipped:
        entry = all_entries[new_idx]
        amount = entry.get("amount", 0)
        if isinstance(amount, str):
            try:
                amount = float(str(amount).replace("$", "").replace(",", ""))
            except ValueError:
                amount = 0
        who = entry.get("client_or_event") or entry.get("vendor") or ""
        detail = f"{entry.get('date', '?')}"
        if who:
            detail += f" | {who}"
        detail += f" | ${amount:,.2f}"
        lines.append(f"• {detail} _(already in spreadsheet)_")
    return "\n".join(lines)


async def _filter_duplicates(
    entries: list[dict],
    exclude_discord_link: str | None = None,
    tab_name: str = "Entries",
) -> tuple[list[dict], list[tuple[int, dict]]]:
    """Check entries for duplicates against recent sheet data.

    Returns (entries_to_save, duplicate_pairs) where duplicate_pairs is
    a list of (original_entry_index, matching_existing_entry).
    Excludes the conversation's own sheet rows to avoid self-matching.
    """
    try:
        recent = await sheets_manager.get_recent_entries(days=90, tab_name=tab_name)
        if exclude_discord_link:
            recent = [r for r in recent if r.get("discord_link") != exclude_discord_link]
        duplicates = _find_duplicates(entries, recent)
    except Exception:
        logger.exception("op=filter_duplicates | Duplicate check failed, skipping")
        return entries, []

    if not duplicates:
        return entries, []

    dup_indices = {idx for idx, _ in duplicates}
    clean = [e for i, e in enumerate(entries) if i not in dup_indices]
    return clean, duplicates


# ---------------------------------------------------------------------------
# Core lifecycle operations
# ---------------------------------------------------------------------------

async def create_entry(
    thread_id: int,
    message_url: str,
    message_id: int,
    text: str | None,
    images: list[tuple[bytes, str]] | None,
    audio: tuple[bytes, str] | None,
    tab_name: str = "Entries",
    user_id: int | None = None,
) -> ProcessingResult:
    """Process a new message: transcribe media, extract data, decide save vs ask.

    Called by main.py when a new message arrives in the expenses channel.
    `user_id` is the Discord ID of the original poster, captured at thread
    creation and stored on the conversation so we can @-mention them later
    when the bot needs their input.
    """
    t0 = time.monotonic()

    # 1. Transcribe audio if present (and cache). Drop audio bytes immediately
    # after — they're not needed again, only the transcription is.
    audio_transcription: str | None = None
    if audio:
        audio_bytes, mime_type = audio
        audio_transcription = await gemini_processor.transcribe_audio(
            audio_bytes, mime_type
        )
        await db.cache_media(message_id, "audio", audio_transcription, mime_type)
    audio = None
    gc.collect()

    # 1b. Resize all images upfront, then drop the original raw bytes. This
    # is the most important memory mitigation on the 256MB VM: a 4MB phone
    # photo decodes to ~37MB of raw RGB in Pillow; keeping the originals
    # around AND doing this work inside describe/extract caused an OOM
    # (incident 2026-05-18 05:04). After this step `images` holds small
    # (~300KB) JPEG bytes that can flow through the rest of the pipeline
    # without further resize work.
    images = await gemini_processor.preprocess_images(images)
    gc.collect()

    # 2. Describe images (and cache)
    if images:
        for img_bytes, mime_type in images:
            description = await gemini_processor.describe_image(img_bytes, mime_type)
            await db.cache_media(message_id, "image", description, mime_type)

    # 3. Extract financial data
    result = await gemini_processor.extract_financial_data(
        text=text,
        images=images,
        audio_transcription=audio_transcription,
    )

    # Free image bytes once extraction is done
    if images:
        images.clear()
    images = None
    gc.collect()

    # 4. Convert entries to dicts for storage
    entries_dicts = [e.model_dump() for e in result.entries]

    # 5. Persist conversation to SQLite
    conv_id = await db.create_conversation(
        thread_id=thread_id,
        message_url=message_url,
        original_text=text or "",
        entries=entries_dicts,
        confidence=result.confidence,
        questions=result.clarifying_questions,
        raw_summary=result.raw_summary,
        tab_name=tab_name,
        user_id=user_id,
    )

    # 6. Record the initial bot response in conversation history
    original_content = text or ""
    if audio_transcription:
        original_content += f"\n[Audio transcription]: {audio_transcription}"
    await db.add_message(thread_id, "user", original_content)

    # 7. Check confidence and decide auto-save vs ask
    is_confident = (
        result.confidence >= CONFIDENCE_THRESHOLD
        and not result.clarifying_questions
        and len(result.entries) > 0
    )

    if is_confident:
        # Check for duplicates before auto-saving
        entries_to_save, skipped = await _filter_duplicates(entries_dicts, tab_name=tab_name)

        if skipped and not entries_to_save:
            # ALL entries are duplicates — hold everything, let user decide
            response_text = _format_duplicate_warning(entries_dicts, skipped)
            await db.add_message(thread_id, "bot", response_text)
            await db.update_conversation_status(thread_id, "pending_duplicate_review")

            elapsed = time.monotonic() - t0
            logger.info(
                "thread=%d op=create_entry | %.1fs, all %d entries are duplicates, holding",
                thread_id, elapsed, len(skipped),
            )

            return ProcessingResult(
                response_text=response_text,
                status="pending_clarification",
                audio_transcription=audio_transcription,
            )

        if skipped:
            # SOME are duplicates — save non-dupes, skip dupes, report both
            row_numbers = await sheets_manager.append_entries(
                entries_to_save, message_url, tab_name=tab_name
            )
            await db.update_conversation_entries(
                thread_id=thread_id,
                entries=entries_to_save,
                confidence=result.confidence,
                questions=[],
                raw_summary=result.raw_summary,
            )
            await db.update_conversation_status(thread_id, "saved", row_numbers)

            response_text = _format_result_message(
                entries_to_save, [], result.raw_summary, saved=True
            )
            response_text += "\n" + _format_skipped_duplicates(entries_dicts, skipped)
            await db.add_message(thread_id, "bot", response_text)

            elapsed = time.monotonic() - t0
            logger.info(
                "thread=%d op=create_entry | %.1fs, saved %d entries, skipped %d duplicates",
                thread_id, elapsed, len(entries_to_save), len(skipped),
            )

            return ProcessingResult(
                response_text=response_text,
                status="saved",
                entries_saved=len(row_numbers),
                audio_transcription=audio_transcription,
            )

        # No duplicates — auto-save all to sheet
        row_numbers = await sheets_manager.append_entries(
            entries_dicts, message_url, tab_name=tab_name
        )
        await db.update_conversation_status(thread_id, "saved", row_numbers)

        response_text = _format_result_message(
            entries_dicts, [], result.raw_summary, saved=True
        )
        await db.add_message(thread_id, "bot", response_text)

        elapsed = time.monotonic() - t0
        logger.info(
            "thread=%d op=create_entry | %.1fs, auto-saved %d entries (rows %s)",
            thread_id, elapsed, len(row_numbers), row_numbers,
        )

        return ProcessingResult(
            response_text=response_text,
            status="saved",
            entries_saved=len(row_numbers),
            audio_transcription=audio_transcription,
        )

    else:
        # Need clarification
        response_text = _format_result_message(
            entries_dicts, result.clarifying_questions, result.raw_summary, saved=False
        )
        await db.add_message(thread_id, "bot", response_text)

        elapsed = time.monotonic() - t0
        logger.info(
            "thread=%d op=create_entry | %.1fs, needs clarification "
            "(confidence=%.2f, questions=%d)",
            thread_id, elapsed, result.confidence, len(result.clarifying_questions),
        )

        return ProcessingResult(
            response_text=response_text,
            status="pending_clarification",
            audio_transcription=audio_transcription,
        )


async def process_reply(
    thread_id: int,
    user_text: str,
    message_id: int | None = None,
    images: list[tuple[bytes, str]] | None = None,
    audio: tuple[bytes, str] | None = None,
) -> ProcessingResult:
    """Process a user's reply in a thread (correction, confirmation, or new media).

    Called by main.py when a user sends a message in an existing thread.
    """
    t0 = time.monotonic()

    # 1. Load conversation state from SQLite
    conv = await db.get_conversation(thread_id)
    if conv is None:
        # Orphan thread — typically because the bot was OOM-killed before it
        # could write the conversation row. The user is in this thread for a
        # reason (the parent message), so the only useful thing we can do is
        # re-process the parent. Return a signal to main.py which has the
        # Discord context needed to fetch the parent and re-extract.
        logger.info(
            "thread=%d op=process_reply | Orphan thread, signalling retry",
            thread_id,
        )
        return ProcessingResult(
            response_text="",
            status="retry_orphan",
        )

    # 1b. Refuse replies in already-deleted threads. The user explicitly
    # deleted these entries; subsequent corrections would either re-create
    # state (confusing) or do nothing. Direct them to start fresh.
    # Use a dedicated status so main.py knows to leave the parent's 🗑️
    # emoji alone (status="saved" would flip it back to ✅).
    if conv["status"] == "deleted":
        logger.info(
            "thread=%d op=process_reply | Reply in deleted thread, refusing",
            thread_id,
        )
        response_text = (
            "This entry was deleted. To create a new one, send a fresh "
            "message in the main channel."
        )
        return ProcessingResult(
            response_text=response_text,
            status="deleted_ack",
        )

    entries = conv["entries"]
    message_url = conv["message_url"]

    # 2. Transcribe/describe any media attachments into text
    #    In a thread reply, media is a correction (voice note, updated receipt),
    #    NOT a new financial entry. Transcribe to text and treat as a text correction.
    correction_text = user_text
    reply_transcription: str | None = None
    if audio:
        audio_bytes, mime_type = audio
        reply_transcription = await gemini_processor.transcribe_audio(audio_bytes, mime_type)
        if message_id:
            await db.cache_media(message_id, "audio", reply_transcription, mime_type)
        correction_text = f"{user_text}\n[Audio]: {reply_transcription}".strip()
        logger.info(
            "thread=%d op=process_reply | Transcribed voice reply: %s",
            thread_id, reply_transcription[:100],
        )

    if images:
        # Resize upfront and drop originals before describe_image runs — same
        # memory-safety pattern used in create_entry.
        images = await gemini_processor.preprocess_images(images)
        gc.collect()
        for img_bytes, mime_type in images:
            description = await gemini_processor.describe_image(img_bytes, mime_type)
            if message_id:
                await db.cache_media(message_id, "image", description, mime_type)
            correction_text = f"{correction_text}\n[Image]: {description}".strip()

    # Free media bytes — they've been transcribed/described and cached in SQLite.
    if images:
        images.clear()
    images = None
    audio = None
    gc.collect()

    # 3. Record user message (with transcription context)
    await db.add_message(thread_id, "user", correction_text)

    # 4. Check for skip/discard (duplicate rejection). Exclude any state
    # where a specific confirmation gate (retry/delete) is pending — otherwise
    # "no" / "nope" would hit the skip path instead of cancelling the gate.
    text_to_check = correction_text.strip().lower()
    if (
        text_to_check in SKIP_WORDS
        and conv["status"] not in (
            "saved", "pending_retry_confirm", "pending_delete_confirm",
        )
    ):
        await db.update_conversation_status(thread_id, "skipped")
        response_text = "Got it — this entry has been discarded and won't be saved."
        await db.add_message(thread_id, "bot", response_text)

        elapsed = time.monotonic() - t0
        logger.info(
            "thread=%d op=process_reply | %.1fs, user skipped/discarded entry",
            thread_id, elapsed,
        )

        return ProcessingResult(
            response_text=response_text,
            status="skipped",
            audio_transcription=reply_transcription,
        )

    # 5. Check for simple confirmation. Exclude any state where a specific
    # confirmation gate is pending — "yes" there means "confirm THAT action",
    # not "confirm the save". Each gate handles its own affirmative check.
    if (
        _is_affirmative(text_to_check)
        and len(entries) > 0
        and conv["status"] not in (
            "saved", "pending_retry_confirm", "pending_delete_confirm",
        )
    ):
        # If user is confirming after a duplicate warning, force save without re-checking
        force = conv["status"] == "pending_duplicate_review"
        result = await _confirm_and_save(thread_id, conv, t0, force_save=force)
        result.audio_transcription = reply_transcription
        return result

    # 5b. Handle the "confirm destructive retry" gate.
    # When the user previously asked to retry an already-saved entry, we set
    # status to 'pending_retry_confirm' and asked them to confirm. Now their
    # reply tells us whether to proceed.
    if conv["status"] == "pending_retry_confirm":
        if _is_affirmative(text_to_check):
            logger.info(
                "thread=%d op=process_reply | Retry confirmed by user (text=%r)",
                thread_id, correction_text[:50],
            )
            return ProcessingResult(
                response_text="",
                status="retry_confirmed",
                audio_transcription=reply_transcription,
            )
        # Anything other than confirm cancels the retry and reverts state.
        # Log the rejected text at INFO so debugging future "I said yes!" issues
        # is straightforward.
        logger.info(
            "thread=%d op=process_reply | Retry NOT confirmed; reverting to saved "
            "(text=%r)", thread_id, correction_text[:50],
        )
        await db.update_conversation_status(thread_id, "saved")
        response_text = (
            "OK, cancelled the retry. The saved entry stays as-is. "
            "Reply if you want to make targeted corrections instead, or say "
            "**retry** again if you actually did mean to reprocess."
        )
        await db.add_message(thread_id, "bot", response_text)
        return ProcessingResult(
            response_text=response_text,
            status="saved",
            audio_transcription=reply_transcription,
        )

    # 5c. Handle the "confirm destructive delete" gate. Same pattern as 5b.
    if conv["status"] == "pending_delete_confirm":
        if _is_affirmative(text_to_check):
            logger.info(
                "thread=%d op=process_reply | Delete confirmed by user (text=%r)",
                thread_id, correction_text[:50],
            )
            return ProcessingResult(
                response_text="",
                status="delete_confirmed",
                audio_transcription=reply_transcription,
            )
        logger.info(
            "thread=%d op=process_reply | Delete NOT confirmed; reverting to saved "
            "(text=%r)", thread_id, correction_text[:50],
        )
        await db.update_conversation_status(thread_id, "saved")
        response_text = (
            "OK, cancelled the delete. The saved entry stays as-is. "
            "Reply if you want to make targeted corrections instead, or say "
            "**delete** again if you actually did mean to remove it."
        )
        await db.add_message(thread_id, "bot", response_text)
        return ProcessingResult(
            response_text=response_text,
            status="saved",
            audio_transcription=reply_transcription,
        )

    # 6. Field-level correction via Gemini
    conversation_history = await db.get_messages(thread_id)
    followup = await gemini_processor.process_correction(
        current_entries=entries,
        conversation_history=conversation_history,
        user_reply=correction_text,
    )

    # 6b. Did Gemini classify this as a retry request? If so, surface it to
    # main.py which has the Discord context to refetch the parent message.
    if followup.is_retry_request:
        logger.info(
            "thread=%d op=process_reply | Gemini classified reply as retry intent",
            thread_id,
        )
        if conv["status"] == "saved" and conv.get("sheet_row_numbers"):
            # Saved entries need explicit confirmation before we destroy them.
            return ProcessingResult(
                response_text="",
                status="retry_needs_confirm",
                audio_transcription=reply_transcription,
            )
        return ProcessingResult(
            response_text="",
            status="retry_active",
            audio_transcription=reply_transcription,
        )

    # 6c. Did Gemini classify this as a delete request?
    if followup.is_delete_request:
        logger.info(
            "thread=%d op=process_reply | Gemini classified reply as delete intent",
            thread_id,
        )
        if conv["status"] == "saved" and conv.get("sheet_row_numbers"):
            # Saved entries need explicit confirmation before we remove them
            # from the spreadsheet.
            return ProcessingResult(
                response_text="",
                status="delete_needs_confirm",
                audio_transcription=reply_transcription,
            )
        # Unsaved entry — nothing on the sheet to remove. Just discard the
        # conversation state, no confirmation needed.
        await db.update_conversation_status(thread_id, "deleted")
        response_text = (
            ":wastebasket: Got it — this entry has been discarded and won't "
            "be saved."
        )
        await db.add_message(thread_id, "bot", response_text)
        return ProcessingResult(
            response_text=response_text,
            status="deleted_ack",
            audio_transcription=reply_transcription,
        )

    # 7. Handle pure confirmation from Gemini
    if followup.is_confirmation and not followup.field_updates:
        if conv["status"] != "saved" and len(entries) > 0:
            force = conv["status"] == "pending_duplicate_review"
            result = await _confirm_and_save(thread_id, conv, t0, force_save=force)
            result.audio_transcription = reply_transcription
            return result
        else:
            response_text = "It looks like this entry is already saved. Reply if you need to make changes."
            await db.add_message(thread_id, "bot", response_text)
            return ProcessingResult(
                response_text=response_text, status="saved",
                audio_transcription=reply_transcription,
            )

    # 8. Apply field updates
    fields_changed = []
    for update in followup.field_updates:
        idx = update.entry_index
        field_name = update.field_name
        new_value: str | float | None = update.new_value

        # Type coercion for amount. If Gemini returns something we can't
        # parse to a positive float (e.g. empty string, "TBD"), DROP the
        # update entirely — silently storing an unparseable value would
        # corrupt the row and break later saves (see 2026-05-07
        # "Remove it entirely" incident where amount became "" and the
        # entry became un-saveable).
        if field_name == "amount":
            try:
                new_value = float(str(new_value).replace("$", "").replace(",", ""))
            except (ValueError, TypeError):
                logger.warning(
                    "thread=%d op=process_reply | Gemini returned unparseable "
                    "amount %r — skipping this field_update to avoid corrupting "
                    "the entry", thread_id, update.new_value,
                )
                continue  # skip; leave the existing amount untouched

        # Handle null/empty
        if field_name in ("client_or_event", "vendor", "mode_of_payment"):
            if new_value == "" or new_value is None:
                new_value = None

        if 0 <= idx < len(entries):
            old_val = entries[idx].get(field_name)
            entries[idx][field_name] = new_value
            fields_changed.append(f"{field_name}: {old_val} -> {new_value}")

    # Persist updated entries
    await db.update_conversation_entries(
        thread_id=thread_id,
        entries=entries,
        confidence=1.0 if not followup.remaining_questions else conv["confidence"],
        questions=followup.remaining_questions,
        raw_summary=conv["raw_summary"],
    )

    # 9. If no remaining questions, save (or update in place)
    if not followup.remaining_questions:
        result = await _save_entries(
            thread_id, conv, entries, fields_changed, message_url, t0
        )
        result.audio_transcription = reply_transcription
        return result

    # 9b. Sheet-sync drift fix: for ALREADY-SAVED entries with new field
    # changes, mirror the change to the sheet immediately even though we
    # still have remaining clarifying questions. Otherwise SQLite and the
    # sheet drift out of sync — see the 5/6 "amount: 350 -> 0" incident
    # where the user was shown $0 in a later delete confirmation but the
    # sheet still held the real $350 row.
    if conv.get("sheet_row_numbers") and fields_changed:
        tab_name = conv.get("tab_name", "Entries")
        for i, row_num in enumerate(conv["sheet_row_numbers"]):
            if i < len(entries):
                try:
                    await sheets_manager.update_entry_in_place(
                        row_num, entries[i], message_url, tab_name=tab_name,
                    )
                except Exception:
                    logger.exception(
                        "thread=%d op=sync_to_sheet | Failed to mirror "
                        "in-flight correction to row %d", thread_id, row_num,
                    )

    # 10. Still have questions — ask them
    response_text = _format_correction_message(
        entries, fields_changed, followup.remaining_questions, saved=False
    )
    await db.add_message(thread_id, "bot", response_text)

    elapsed = time.monotonic() - t0
    logger.info(
        "thread=%d op=process_reply | %.1fs, %d fields changed, "
        "%d remaining questions",
        thread_id, elapsed, len(fields_changed), len(followup.remaining_questions),
    )

    return ProcessingResult(
        response_text=response_text,
        status="pending_clarification",
        fields_changed=fields_changed,
        audio_transcription=reply_transcription,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _confirm_and_save(
    thread_id: int,
    conv: dict,
    t0: float,
    force_save: bool = False,
) -> ProcessingResult:
    """Save entries to sheet when user confirms."""
    entries = conv["entries"]
    message_url = conv["message_url"]
    tab_name = conv.get("tab_name", "Entries")
    skipped: list[tuple[int, dict]] = []

    if conv["status"] == "saved" and conv["sheet_row_numbers"]:
        # Already saved — update in place (no duplicate check needed)
        for i, row_num in enumerate(conv["sheet_row_numbers"]):
            if i < len(entries):
                await sheets_manager.update_entry_in_place(
                    row_num, entries[i], message_url, tab_name=tab_name
                )
        row_numbers = conv["sheet_row_numbers"]
        entries_to_save = entries
    else:
        # First save — check for duplicates unless user explicitly forced
        entries_to_save = entries
        if not force_save:
            entries_to_save, skipped = await _filter_duplicates(
                entries, exclude_discord_link=message_url, tab_name=tab_name
            )

            if skipped and not entries_to_save:
                # ALL entries are duplicates — hold and warn
                response_text = _format_duplicate_hold_message(entries, skipped)
                await db.add_message(thread_id, "bot", response_text)
                await db.update_conversation_status(thread_id, "pending_duplicate_review")
                elapsed = time.monotonic() - t0
                logger.info(
                    "thread=%d op=confirm_save | %.1fs, all %d entries are duplicates, holding",
                    thread_id, elapsed, len(entries),
                )
                return ProcessingResult(
                    response_text=response_text,
                    status="pending_clarification",
                )

        # If some entries were filtered, update conversation state
        if skipped:
            await db.update_conversation_entries(
                thread_id=thread_id,
                entries=entries_to_save,
                confidence=conv["confidence"],
                questions=[],
                raw_summary=conv["raw_summary"],
            )

        row_numbers = await sheets_manager.append_entries(
            entries_to_save, message_url, tab_name=tab_name
        )

    await db.update_conversation_status(thread_id, "saved", row_numbers)

    response_text = _format_result_message(
        entries_to_save, [], conv["raw_summary"], saved=True
    )
    if skipped:
        response_text += "\n" + _format_skipped_duplicates(entries, skipped)
    await db.add_message(thread_id, "bot", response_text)

    elapsed = time.monotonic() - t0
    logger.info(
        "thread=%d op=confirm_save | %.1fs, saved %d entries (rows %s), skipped %d duplicates",
        thread_id, elapsed, len(entries_to_save), row_numbers, len(skipped),
    )

    return ProcessingResult(
        response_text=response_text,
        status="saved",
        entries_saved=len(entries_to_save),
    )


async def _save_entries(
    thread_id: int,
    conv: dict,
    entries: list[dict],
    fields_changed: list[str],
    message_url: str,
    t0: float,
) -> ProcessingResult:
    """Save or update entries in the spreadsheet after corrections."""
    old_rows = conv["sheet_row_numbers"]
    tab_name = conv.get("tab_name", "Entries")
    skipped: list[tuple[int, dict]] = []
    original_entries = list(entries)

    if old_rows and len(old_rows) == len(entries):
        # Same number of entries — update in place (no duplicate check for corrections)
        for i, row_num in enumerate(old_rows):
            await sheets_manager.update_entry_in_place(
                row_num, entries[i], message_url, tab_name=tab_name
            )
        row_numbers = old_rows
        logger.info(
            "thread=%d op=save_entries | Updated %d rows in place",
            thread_id, len(row_numbers),
        )
    elif old_rows:
        # Entry count changed — safe replace (no duplicate check for corrections)
        row_numbers = await sheets_manager.safe_replace_entries(
            old_rows, entries, message_url, tab_name=tab_name
        )
        logger.info(
            "thread=%d op=save_entries | Safe-replaced %d old rows with %d new",
            thread_id, len(old_rows), len(row_numbers),
        )
    else:
        # Never saved before — check for duplicates first
        entries_to_save, skipped = await _filter_duplicates(
            entries, exclude_discord_link=message_url, tab_name=tab_name
        )

        if skipped and not entries_to_save:
            # ALL entries are duplicates — hold and warn
            response_text = _format_duplicate_hold_message(entries, skipped)
            await db.add_message(thread_id, "bot", response_text)
            await db.update_conversation_status(thread_id, "pending_duplicate_review")
            elapsed = time.monotonic() - t0
            logger.info(
                "thread=%d op=save_entries | %.1fs, all %d entries are duplicates, holding",
                thread_id, elapsed, len(entries),
            )
            return ProcessingResult(
                response_text=response_text,
                status="pending_clarification",
                fields_changed=fields_changed,
            )

        if skipped:
            entries = entries_to_save
            await db.update_conversation_entries(
                thread_id=thread_id,
                entries=entries,
                confidence=1.0,
                questions=[],
                raw_summary=conv["raw_summary"],
            )

        row_numbers = await sheets_manager.append_entries(
            entries, message_url, tab_name=tab_name
        )
        logger.info(
            "thread=%d op=save_entries | Appended %d new rows, skipped %d duplicates",
            thread_id, len(row_numbers), len(skipped),
        )

    await db.update_conversation_status(thread_id, "saved", row_numbers)

    response_text = _format_correction_message(
        entries, fields_changed, [], saved=True
    )
    if skipped:
        response_text += "\n" + _format_skipped_duplicates(original_entries, skipped)
    await db.add_message(thread_id, "bot", response_text)

    elapsed = time.monotonic() - t0
    logger.info(
        "thread=%d op=save_entries | %.1fs, saved %d entries, skipped %d duplicates, changes: %s",
        thread_id, elapsed, len(entries), len(skipped), fields_changed or "none",
    )

    return ProcessingResult(
        response_text=response_text,
        status="saved",
        entries_saved=len(entries),
        fields_changed=fields_changed,
    )



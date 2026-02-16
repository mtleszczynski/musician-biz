"""Entry lifecycle management — the central orchestration layer.

Handles: create entry, process corrections, confirm, save to sheet.
Coordinates between db.py (state), gemini_processor.py (AI), and
sheets_manager.py (Google Sheets). main.py delegates all logic here.
"""

import logging
import time
from dataclasses import dataclass, field

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


@dataclass
class ProcessingResult:
    """Returned to main.py so it knows what to post in Discord."""

    response_text: str = ""
    status: str = "pending_clarification"  # saved | pending_clarification | error
    entries_saved: int = 0
    fields_changed: list[str] = field(default_factory=list)


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
# Core lifecycle operations
# ---------------------------------------------------------------------------

async def create_entry(
    thread_id: int,
    message_url: str,
    message_id: int,
    text: str | None,
    images: list[tuple[bytes, str]] | None,
    audio: tuple[bytes, str] | None,
) -> ProcessingResult:
    """Process a new message: transcribe media, extract data, decide save vs ask.

    Called by main.py when a new message arrives in the expenses channel.
    """
    t0 = time.monotonic()

    # 1. Transcribe audio if present (and cache)
    audio_transcription: str | None = None
    if audio:
        audio_bytes, mime_type = audio
        audio_transcription = await gemini_processor.transcribe_audio(
            audio_bytes, mime_type
        )
        await db.cache_media(message_id, "audio", audio_transcription, mime_type)

    # 2. Describe images (and cache) — but still send raw bytes for extraction
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
        # Auto-save to sheet
        row_numbers = await sheets_manager.append_entries(entries_dicts, message_url)
        await db.update_conversation_status(thread_id, "saved", row_numbers)

        response_text = _format_result_message(
            entries_dicts, [], result.raw_summary, saved=True
        )
        await db.add_message(thread_id, "bot", response_text)

        # Log to conversation log
        await sheets_manager.log_conversation(
            user_input=text or "(attachment)",
            bot_response=response_text[:500],
            outcome="auto-confirmed",
            discord_link=message_url,
        )

        elapsed = time.monotonic() - t0
        logger.info(
            "thread=%d op=create_entry | %.1fs, auto-saved %d entries (rows %s)",
            thread_id, elapsed, len(row_numbers), row_numbers,
        )

        return ProcessingResult(
            response_text=response_text,
            status="saved",
            entries_saved=len(row_numbers),
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
        logger.warning("thread=%d op=process_reply | No conversation found", thread_id)
        return ProcessingResult(
            response_text=(
                "Sorry, I couldn't find the context for this thread. "
                "Please create a new entry in the main channel."
            ),
            status="error",
        )

    entries = conv["entries"]
    message_url = conv["message_url"]

    # 2. Record user message
    await db.add_message(thread_id, "user", user_text)

    # 3. Check for simple confirmation
    if (
        user_text.strip().lower() in CONFIRM_WORDS
        and len(entries) > 0
        and conv["status"] != "saved"
    ):
        return await _confirm_and_save(thread_id, conv, t0)

    # 4. Handle new media attachments
    if images or audio:
        return await _handle_new_media(
            thread_id, conv, user_text, message_id, images, audio, t0
        )

    # 5. Field-level correction via Gemini
    conversation_history = await db.get_messages(thread_id)
    followup = await gemini_processor.process_correction(
        current_entries=entries,
        conversation_history=conversation_history,
        user_reply=user_text,
    )

    # 6. Handle pure confirmation from Gemini
    if followup.is_confirmation and not followup.field_updates:
        if conv["status"] != "saved" and len(entries) > 0:
            return await _confirm_and_save(thread_id, conv, t0)
        else:
            response_text = "It looks like this entry is already saved. Reply if you need to make changes."
            await db.add_message(thread_id, "bot", response_text)
            return ProcessingResult(response_text=response_text, status="saved")

    # 7. Apply field updates
    fields_changed = []
    for update in followup.field_updates:
        idx = update.entry_index
        field_name = update.field_name
        new_value: str | float | None = update.new_value

        # Type coercion for amount
        if field_name == "amount":
            try:
                new_value = float(str(new_value).replace("$", "").replace(",", ""))
            except ValueError:
                pass

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

    # 8. If no remaining questions, save (or update in place)
    if not followup.remaining_questions:
        return await _save_entries(
            thread_id, conv, entries, fields_changed, message_url, t0
        )

    # 9. Still have questions — ask them
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
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _confirm_and_save(
    thread_id: int,
    conv: dict,
    t0: float,
) -> ProcessingResult:
    """Save entries to sheet when user confirms."""
    entries = conv["entries"]
    message_url = conv["message_url"]

    if conv["status"] == "saved" and conv["sheet_row_numbers"]:
        # Already saved — update in place
        for i, row_num in enumerate(conv["sheet_row_numbers"]):
            if i < len(entries):
                await sheets_manager.update_entry_in_place(
                    row_num, entries[i], message_url
                )
        row_numbers = conv["sheet_row_numbers"]
    else:
        # First save
        row_numbers = await sheets_manager.append_entries(entries, message_url)

    await db.update_conversation_status(thread_id, "saved", row_numbers)

    response_text = _format_result_message(
        entries, [], conv["raw_summary"], saved=True
    )
    await db.add_message(thread_id, "bot", response_text)

    await sheets_manager.log_conversation(
        user_input=conv["original_text"] or "(attachment)",
        bot_response=response_text[:500],
        outcome="user-confirmed",
        discord_link=message_url,
    )

    elapsed = time.monotonic() - t0
    logger.info(
        "thread=%d op=confirm_save | %.1fs, saved %d entries (rows %s)",
        thread_id, elapsed, len(entries), row_numbers,
    )

    return ProcessingResult(
        response_text=response_text,
        status="saved",
        entries_saved=len(entries),
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

    if old_rows and len(old_rows) == len(entries):
        # Same number of entries — update in place (no deletion needed)
        for i, row_num in enumerate(old_rows):
            await sheets_manager.update_entry_in_place(
                row_num, entries[i], message_url
            )
        row_numbers = old_rows
        logger.info(
            "thread=%d op=save_entries | Updated %d rows in place",
            thread_id, len(row_numbers),
        )
    elif old_rows:
        # Entry count changed — safe replace (write first, then delete)
        row_numbers = await sheets_manager.safe_replace_entries(
            old_rows, entries, message_url
        )
        logger.info(
            "thread=%d op=save_entries | Safe-replaced %d old rows with %d new",
            thread_id, len(old_rows), len(row_numbers),
        )
    else:
        # Never saved before — just append
        row_numbers = await sheets_manager.append_entries(entries, message_url)
        logger.info(
            "thread=%d op=save_entries | Appended %d new rows",
            thread_id, len(row_numbers),
        )

    await db.update_conversation_status(thread_id, "saved", row_numbers)

    response_text = _format_correction_message(
        entries, fields_changed, [], saved=True
    )
    await db.add_message(thread_id, "bot", response_text)

    outcome = "corrected" if fields_changed else "auto-confirmed"
    await sheets_manager.log_conversation(
        user_input=conv["original_text"] or "(attachment)",
        bot_response=response_text[:500],
        outcome=outcome,
        discord_link=message_url,
    )

    elapsed = time.monotonic() - t0
    logger.info(
        "thread=%d op=save_entries | %.1fs, saved %d entries, changes: %s",
        thread_id, elapsed, len(entries), fields_changed or "none",
    )

    return ProcessingResult(
        response_text=response_text,
        status="saved",
        entries_saved=len(entries),
        fields_changed=fields_changed,
    )


async def _handle_new_media(
    thread_id: int,
    conv: dict,
    user_text: str,
    message_id: int | None,
    images: list[tuple[bytes, str]] | None,
    audio: tuple[bytes, str] | None,
    t0: float,
) -> ProcessingResult:
    """Handle a reply that includes new media attachments.

    Falls back to full extraction for genuinely new content, then
    either saves or asks questions.
    """
    message_url = conv["message_url"]

    # Transcribe new audio
    audio_transcription: str | None = None
    if audio:
        audio_bytes, mime_type = audio
        audio_transcription = await gemini_processor.transcribe_audio(
            audio_bytes, mime_type
        )
        if message_id:
            await db.cache_media(message_id, "audio", audio_transcription, mime_type)

    # Describe new images
    if images and message_id:
        for img_bytes, mime_type in images:
            description = await gemini_processor.describe_image(img_bytes, mime_type)
            await db.cache_media(message_id, "image", description, mime_type)

    # Full extraction of the new media
    result = await gemini_processor.extract_financial_data(
        text=user_text or None,
        images=images,
        audio_transcription=audio_transcription,
    )

    entries_dicts = [e.model_dump() for e in result.entries]

    # Decide: replace existing entries or merge
    # For simplicity, new media replaces the existing extraction
    await db.update_conversation_entries(
        thread_id=thread_id,
        entries=entries_dicts,
        confidence=result.confidence,
        questions=result.clarifying_questions,
        raw_summary=result.raw_summary,
    )

    is_confident = (
        result.confidence >= CONFIDENCE_THRESHOLD
        and not result.clarifying_questions
        and len(result.entries) > 0
    )

    if is_confident:
        # Save (handling existing rows safely)
        old_rows = conv["sheet_row_numbers"]
        if old_rows:
            row_numbers = await sheets_manager.safe_replace_entries(
                old_rows, entries_dicts, message_url
            )
        else:
            row_numbers = await sheets_manager.append_entries(
                entries_dicts, message_url
            )
        await db.update_conversation_status(thread_id, "saved", row_numbers)

        response_text = _format_result_message(
            entries_dicts, [], result.raw_summary, saved=True
        )
        await db.add_message(thread_id, "bot", response_text)

        elapsed = time.monotonic() - t0
        logger.info(
            "thread=%d op=new_media | %.1fs, saved %d entries from new media",
            thread_id, elapsed, len(row_numbers),
        )

        return ProcessingResult(
            response_text=response_text,
            status="saved",
            entries_saved=len(row_numbers),
        )
    else:
        response_text = _format_result_message(
            entries_dicts, result.clarifying_questions, result.raw_summary, saved=False
        )
        await db.add_message(thread_id, "bot", response_text)

        elapsed = time.monotonic() - t0
        logger.info(
            "thread=%d op=new_media | %.1fs, needs clarification from new media",
            thread_id, elapsed,
        )

        return ProcessingResult(
            response_text=response_text,
            status="pending_clarification",
        )

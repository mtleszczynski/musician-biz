"""Discord bot entry point — thin event dispatcher.

All business logic lives in entry_manager.py. This file handles:
- Discord event routing (on_message, commands)
- Downloading attachments from Discord messages
- Emoji reactions on messages
- Thread creation
- Startup / shutdown
"""

import asyncio
import logging
import re
import sys
from datetime import datetime

import discord
from discord.ext import commands

import db
import entry_manager
import sheets_manager
from config import CHANNEL_TAB_MAP, DISCORD_TOKEN, get_tab_for_channel, setup_logging

# ---------------------------------------------------------------------------
# Logging — must be configured before anything else logs
# ---------------------------------------------------------------------------
setup_logging()
logger = logging.getLogger("musician-bot")

# ---------------------------------------------------------------------------
# Emoji constants
# ---------------------------------------------------------------------------
EMOJI_PROCESSING = "\u23f3"       # hourglass — working on it
EMOJI_DONE = "\u2705"             # green checkmark — saved to sheet
EMOJI_NEEDS_INPUT = "\U0001f4ac"  # speech bubble — waiting for user
EMOJI_ERROR = "\u274c"            # red X — actual error/crash
EMOJI_QUESTION = "\u2753"         # question mark — message had no content
EMOJI_DELETED = "\U0001f5d1\ufe0f"  # 🗑️ wastebasket — entries removed by user

# All reactions the bot ever sets on a message — used by set_reaction() to
# guarantee old reactions are cleared even if message.reactions cache is stale.
_BOT_MANAGED_REACTIONS = (
    EMOJI_PROCESSING,
    EMOJI_DONE,
    EMOJI_NEEDS_INPUT,
    EMOJI_ERROR,
    EMOJI_QUESTION,
    EMOJI_DELETED,
)


# ---------------------------------------------------------------------------
# @-mention helpers
# ---------------------------------------------------------------------------
# When the bot needs the user to take action (answer a clarifying question,
# confirm a destructive retry, see an error), it prefixes the message with a
# Discord @-mention of the conversation's original poster so they get a push
# notification on mobile/desktop. Routine acknowledgements ("Saved!") are NOT
# mentioned, to avoid notification fatigue.

# Statuses returned by entry_manager that mean "the user has to do something"
# — i.e. these are the only response_text payloads that should be mentioned.
_NEEDS_MENTION_STATUSES = frozenset({"pending_clarification", "error"})


def _format_mention(user_id: int | None) -> str:
    """Return a Discord mention prefix like '<@123> ' (with trailing space),
    or empty string if user_id is unknown (legacy conversations don't have one).
    """
    if not user_id:
        return ""
    return f"<@{user_id}> "


def _with_mention_if_needed(text: str, user_id: int | None, status: str) -> str:
    """Prepend an @-mention to `text` iff the status calls for user action."""
    if status not in _NEEDS_MENTION_STATUSES:
        return text
    return _format_mention(user_id) + text


async def _resolve_user_id(conv: dict | None, thread: discord.Thread) -> int | None:
    """Return the @-mention target for a thread, with on-demand backfill.

    Tries (in order):
      1. `conv.user_id` if the column is populated
      2. The parent message's author.id, fetched via Discord API
      3. None (best-effort; nothing to mention)

    When (2) succeeds, also writes the resolved id back to the conversations
    table so future prompts in the same thread don't pay the fetch cost.
    Used everywhere mentions are sent so legacy threads (created before the
    user_id column existed) still get pinged correctly.
    """
    if conv and conv.get("user_id"):
        return conv["user_id"]
    try:
        parent = await _fetch_parent_message(thread)
    except Exception:
        logger.debug(
            "thread=%d op=resolve_user_id | parent fetch failed", thread.id,
        )
        return None
    if parent is None or parent.author is None:
        return None
    user_id = parent.author.id
    # Backfill so we don't refetch on the next prompt in this thread.
    try:
        await db.set_user_id(thread.id, user_id)
    except Exception:
        logger.debug(
            "thread=%d op=resolve_user_id | backfill failed", thread.id,
        )
    return user_id

# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------
# The Fly VM runs on 256MB. Each in-flight message extraction holds raw image
# bytes + Gemini SDK buffers (~25-35MB peak). Concurrent processing has caused
# OOM kills (exit_code=137) that leave the hourglass emoji stuck forever
# because the SIGKILL bypasses the try/except handler. Serializing prevents
# this without needing more memory; for a single-user bot this is invisible.
PROCESSING_LOCK = asyncio.Semaphore(1)

# Cap recovery to reasonably recent stuck conversations — reposting a
# months-old bot reply out of the blue would be confusing. 30 days is a
# good balance: catches recent OOM-stuck threads, skips ancient ones.
RECOVERY_MAX_AGE_DAYS = 30

# ---------------------------------------------------------------------------
# Attachment handling
# ---------------------------------------------------------------------------

IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
AUDIO_MIME_TYPES = {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm"}


async def download_attachments(
    message: discord.Message,
) -> tuple[list[tuple[bytes, str]], tuple[bytes, str] | None]:
    """Download image and audio attachments from a Discord message."""
    images: list[tuple[bytes, str]] = []
    audio: tuple[bytes, str] | None = None

    for attachment in message.attachments:
        content_type = (attachment.content_type or "").split(";")[0].strip()

        if content_type in IMAGE_MIME_TYPES:
            data = await attachment.read()
            images.append((data, content_type))
            logger.info(
                "op=download | %s (%s, %d bytes)",
                attachment.filename, content_type, len(data),
            )
        elif content_type in AUDIO_MIME_TYPES:
            data = await attachment.read()
            audio = (data, content_type)
            logger.info(
                "op=download | %s (%s, %d bytes)",
                attachment.filename, content_type, len(data),
            )
        else:
            logger.debug(
                "op=download | Skipping unsupported: %s (%s)",
                attachment.filename, content_type,
            )

    # Discord voice messages use a special flag
    if message.flags.value & (1 << 13):  # IS_VOICE_MESSAGE
        for attachment in message.attachments:
            if not audio:
                data = await attachment.read()
                audio = (data, "audio/ogg")
                logger.info("op=download | Voice message: %s (%d bytes)",
                            attachment.filename, len(data))

    return images, audio


async def set_reaction(message: discord.Message, emoji: str) -> None:
    """Set the bot's single reaction on a message, replacing any prior bot reactions.

    Explicitly attempts to remove every known bot-managed reaction rather than
    iterating `message.reactions` — that cached collection is updated by
    Gateway events (not by our own add_reaction calls) and can lag, causing
    stale reactions to linger (e.g. the hourglass staying alongside the
    checkmark after a long-running retry). Discord returns 404 for reactions
    that aren't there, which we silently ignore.
    """
    for old in _BOT_MANAGED_REACTIONS:
        if old == emoji:
            continue
        try:
            await message.remove_reaction(old, bot.user)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        except Exception:
            logger.debug("op=set_reaction | Could not remove %s", old)
    try:
        await message.add_reaction(emoji)
    except Exception:
        logger.exception("op=set_reaction | Failed to set %s", emoji)


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    # Initialise SQLite tables on startup
    await db.init_db()
    logger.info(
        "op=startup | Bot ready as %s (ID: %s)", bot.user.name, bot.user.id
    )
    if CHANNEL_TAB_MAP:
        for ch_id, tab in CHANNEL_TAB_MAP.items():
            logger.info("op=startup | Listening in channel %s -> tab '%s'", ch_id, tab)
    else:
        logger.warning("op=startup | No channels configured — bot won't process messages")

    # Recover any threads we left stuck on the previous run (OOM/crash/deploy
    # killed us after writing the response to SQLite but before sending it
    # to Discord). Run as a background task so we don't block other events.
    asyncio.create_task(recover_stuck_conversations())


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

# Parses /channels/<guild>/<channel>/<message> out of a Discord jump URL.
_JUMP_URL_RE = re.compile(
    r"/channels/(?:\d+|@me)/(?P<channel>\d+)/(?P<message>\d+)"
)


async def recover_stuck_conversations(max_age_days: int = RECOVERY_MAX_AGE_DAYS) -> dict:
    """Repost the saved bot response for any conversation that was stuck
    in 'pending_clarification' with no Discord delivery (bot died between
    db.add_message and thread.send).

    Each conversation is touched in SQLite after a successful repost so
    we don't double-post on the next restart. Returns a {recovered, skipped,
    failed} count dict so callers (e.g. an admin command) can report results.
    """
    counts = {"recovered": 0, "skipped": 0, "failed": 0}
    try:
        stuck = await db.get_stuck_conversations(max_age_days=max_age_days)
    except Exception:
        logger.exception("op=recovery | Failed to query stuck conversations")
        return counts

    if not stuck:
        logger.info("op=recovery | No stuck conversations to recover")
        return counts

    logger.info("op=recovery | Found %d stuck conversation(s) to recover", len(stuck))

    for conv in stuck:
        thread_id = conv["thread_id"]
        try:
            bot_msg = await db.get_last_bot_message(thread_id)
            if not bot_msg:
                logger.warning(
                    "thread=%d op=recovery | No bot message stored, skipping",
                    thread_id,
                )
                counts["skipped"] += 1
                continue

            thread = await _safe_fetch_channel(thread_id)
            if thread is None:
                logger.warning(
                    "thread=%d op=recovery | Thread not accessible, skipping",
                    thread_id,
                )
                counts["skipped"] += 1
                continue

            # Always mention on recovery — the user posted this potentially
            # days ago and needs to know it finally finished processing.
            mention_user = await _resolve_user_id(conv, thread)
            mention = _format_mention(mention_user)
            await thread.send(
                mention
                + ":pushpin: _I crashed before I could reply earlier. "
                "Here's what I had figured out:_"
            )
            await thread.send(bot_msg)

            # Update the original channel message's emoji from hourglass
            # to speech bubble so the user sees there's something pending.
            await _update_original_reaction(conv.get("message_url"))

            await db.touch_conversation(thread_id)
            counts["recovered"] += 1
            logger.info(
                "thread=%d op=recovery | Reposted bot response (%d chars)",
                thread_id, len(bot_msg),
            )

            # Pace ourselves to avoid Discord rate limits.
            await asyncio.sleep(0.5)
        except Exception:
            counts["failed"] += 1
            logger.exception(
                "thread=%d op=recovery | Failed to recover", thread_id,
            )

    logger.info(
        "op=recovery | Done: recovered=%d, skipped=%d, failed=%d",
        counts["recovered"], counts["skipped"], counts["failed"],
    )
    return counts


async def _safe_fetch_channel(channel_id: int):
    """Fetch a channel/thread, handling not-found and archived threads."""
    try:
        ch = bot.get_channel(channel_id)
        if ch is not None:
            return ch
        return await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden):
        return None
    except Exception:
        logger.exception("op=fetch_channel | Unexpected error for %d", channel_id)
        return None


async def _update_original_reaction(message_url: str | None) -> None:
    """Best-effort flip the original message's hourglass to speech-bubble."""
    if not message_url:
        return
    match = _JUMP_URL_RE.search(message_url)
    if not match:
        return
    channel_id = int(match.group("channel"))
    message_id = int(match.group("message"))
    try:
        channel = await _safe_fetch_channel(channel_id)
        if channel is None:
            return
        msg = await channel.fetch_message(message_id)
        await set_reaction(msg, EMOJI_NEEDS_INPUT)
    except (discord.NotFound, discord.Forbidden):
        pass
    except Exception:
        logger.debug("op=recovery | Could not update reaction for %s", message_url)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Process commands first
    await bot.process_commands(message)
    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    # New message in a monitored channel (not a thread)
    channel_id_str = str(message.channel.id)
    if channel_id_str in CHANNEL_TAB_MAP and not isinstance(message.channel, discord.Thread):
        await handle_new_entry(message, CHANNEL_TAB_MAP[channel_id_str])

    # Reply in a thread whose parent is a monitored channel
    elif (
        isinstance(message.channel, discord.Thread)
        and message.channel.parent_id
        and str(message.channel.parent_id) in CHANNEL_TAB_MAP
    ):
        await handle_thread_reply(message)


# ---------------------------------------------------------------------------
# New entry handler
# ---------------------------------------------------------------------------

async def handle_new_entry(message: discord.Message, tab_name: str = "Entries"):
    """Process a new message in a monitored channel."""
    # Set the hourglass immediately so the user has feedback even while
    # queued behind another in-flight message.
    try:
        await message.add_reaction(EMOJI_PROCESSING)
    except Exception:
        pass

    async with PROCESSING_LOCK:
        await _handle_new_entry_locked(message, tab_name)


async def _handle_new_entry_locked(message: discord.Message, tab_name: str):
    thread = None
    try:
        images, audio = await download_attachments(message)
        text = message.content.strip() if message.content else None

        if not text and not images and not audio:
            await set_reaction(message, EMOJI_QUESTION)
            return

        # Create a thread
        thread = await message.create_thread(
            name=f"Entry {datetime.now().strftime('%b %d %H:%M')}",
            auto_archive_duration=1440,
        )

        # Capture the original poster's Discord ID so we can @-mention them
        # whenever the bot needs their attention later in the thread.
        author_id = message.author.id if message.author else None

        # Delegate all logic to entry_manager
        result = await entry_manager.create_entry(
            thread_id=thread.id,
            message_url=message.jump_url,
            message_id=message.id,
            text=text,
            images=images,
            audio=audio,
            tab_name=tab_name,
            user_id=author_id,
        )

        # Post audio transcription if present (so user can see what was heard)
        if result.audio_transcription:
            await thread.send(
                f"**Heard:** {result.audio_transcription}"
            )

        # Post the response in the thread, prefixed with @-mention if user action is needed
        await thread.send(
            _with_mention_if_needed(result.response_text, author_id, result.status)
        )

        # Set appropriate emoji
        if result.status == "saved":
            await set_reaction(message, EMOJI_DONE)
        else:
            await set_reaction(message, EMOJI_NEEDS_INPUT)

    except Exception:
        logger.exception("thread=new op=handle_new_entry | Error")
        await set_reaction(message, EMOJI_ERROR)
        author_id = message.author.id if message.author else None
        error_msg = (
            _format_mention(author_id)
            + "Sorry, something went wrong processing this message. "
            "Please try again or check the bot logs."
        )
        try:
            if thread is not None:
                await thread.send(error_msg)
            else:
                thread = await message.create_thread(
                    name="Error", auto_archive_duration=60
                )
                await thread.send(error_msg)
        except Exception:
            logger.exception("op=handle_new_entry | Failed to send error message")


# ---------------------------------------------------------------------------
# Thread reply handler
# ---------------------------------------------------------------------------

async def handle_thread_reply(message: discord.Message):
    """Process a follow-up message in a thread."""
    try:
        await message.add_reaction(EMOJI_PROCESSING)
    except Exception:
        pass

    async with PROCESSING_LOCK:
        await _handle_thread_reply_locked(message)


async def _handle_thread_reply_locked(message: discord.Message):
    thread_id = message.channel.id

    try:
        # Immediately show processing on the original channel message too
        try:
            original_msg = message.channel.starter_message
            if original_msg:
                await set_reaction(original_msg, EMOJI_PROCESSING)
        except Exception:
            pass

        images, audio = await download_attachments(message)
        user_text = message.content.strip() if message.content else ""
        had_attachments = bool(images or audio)

        # Pre-load the user_id from the conversation so we can @-mention
        # the original poster on any prompts that need their input. For
        # legacy convs with NULL user_id, fall back to fetching the parent
        # message's author (and backfill the DB while we're at it).
        pre_conv = await db.get_conversation(thread_id)
        author_user_id = await _resolve_user_id(pre_conv, message.channel)

        # Delegate to entry_manager
        result = await entry_manager.process_reply(
            thread_id=thread_id,
            user_text=user_text,
            message_id=message.id,
            images=images if images else None,
            audio=audio,
        )

        # Post audio transcription if present (so user can see what was heard)
        if result.audio_transcription:
            await message.channel.send(
                f"**Heard:** {result.audio_transcription}"
            )

        # Retry paths — entry_manager has determined the user wants to
        # reprocess the parent message from scratch.
        if result.status in (
            "retry_orphan", "retry_active", "retry_needs_confirm", "retry_confirmed",
        ):
            await _handle_retry(message, result.status, had_attachments)
            await set_reaction(message, EMOJI_DONE)
            return

        # Delete paths — entry_manager has determined the user wants to
        # remove the entries from the spreadsheet.
        if result.status in ("delete_needs_confirm", "delete_confirmed"):
            await _handle_delete(message, result.status)
            await set_reaction(message, EMOJI_DONE)
            return

        # "deleted_ack" — bot has acknowledged a reply in an already-deleted
        # thread. Post the message and mark the reply ✅, but DON'T touch the
        # parent emoji (it should remain 🗑️).
        if result.status == "deleted_ack":
            await message.channel.send(result.response_text)
            await set_reaction(message, EMOJI_DONE)
            return

        # Post response, prefixed with @-mention if user action is needed
        await message.channel.send(
            _with_mention_if_needed(result.response_text, author_user_id, result.status)
        )

        # Set emoji on the reply
        if result.status in ("saved", "skipped"):
            await set_reaction(message, EMOJI_DONE)
        elif result.status == "error":
            await set_reaction(message, EMOJI_ERROR)
        else:
            await set_reaction(message, EMOJI_NEEDS_INPUT)

        # Also update the original (parent) message's emoji. starter_message
        # may be None for older threads — fall back to explicit fetch.
        try:
            original_msg = message.channel.starter_message
            if original_msg is None:
                original_msg = await _fetch_parent_message(message.channel)
            if original_msg is not None:
                if result.status in ("saved", "skipped"):
                    await set_reaction(original_msg, EMOJI_DONE)
                elif result.status == "error":
                    await set_reaction(original_msg, EMOJI_ERROR)
                else:
                    await set_reaction(original_msg, EMOJI_NEEDS_INPUT)
        except Exception:
            logger.debug(
                "thread=%d op=handle_thread_reply | Could not update original emoji",
                thread_id,
            )

    except Exception:
        logger.exception("thread=%d op=handle_thread_reply | Error", thread_id)
        await set_reaction(message, EMOJI_ERROR)
        # Best-effort mention so the user sees the error
        try:
            err_conv = await db.get_conversation(thread_id)
            err_user_id = await _resolve_user_id(err_conv, message.channel)
        except Exception:
            err_user_id = None
        await message.channel.send(
            _format_mention(err_user_id)
            + "Sorry, something went wrong. Please try again or rephrase your correction."
        )


# ---------------------------------------------------------------------------
# Retry: re-extract from the thread's original (parent) message
# ---------------------------------------------------------------------------

async def _handle_retry(
    message: discord.Message,
    status: str,
    had_attachments: bool = False,
) -> None:
    """Orchestrate a retry triggered from a thread reply.

    status is one of:
      retry_orphan        — no SQLite conv row (OOM-orphan); always proceed
      retry_active        — conv exists, not saved; always proceed
      retry_needs_confirm — conv exists, status=saved; ask user to confirm
      retry_confirmed     — user already said yes after a needs_confirm prompt
    """
    thread = message.channel
    thread_id = thread.id

    # Confirmation gate for already-saved entries: just ask, don't act.
    if status == "retry_needs_confirm":
        await _request_retry_confirmation(thread, thread_id)
        return

    # If the user confirmed a destructive retry, delete the saved sheet rows
    # before we wipe the conversation row.
    if status == "retry_confirmed":
        conv = await db.get_conversation(thread_id)
        if conv and conv.get("sheet_row_numbers"):
            tab = conv.get("tab_name", "Entries")
            rows = conv["sheet_row_numbers"]
            try:
                deleted = await sheets_manager.delete_rows(rows, tab_name=tab)
                logger.info(
                    "thread=%d op=retry | Deleted %d saved sheet row(s) before retry",
                    thread_id, deleted,
                )
                await thread.send(
                    f":wastebasket: _Deleted {len(rows)} saved sheet row(s); "
                    f"re-extracting now..._"
                )
            except Exception:
                logger.exception(
                    "thread=%d op=retry | Failed to delete sheet rows", thread_id,
                )
                await thread.send(
                    ":x: Couldn't delete the existing sheet rows — retry aborted. "
                    "The original entry remains saved."
                )
                # Revert state to saved so the user isn't stuck in pending_retry_confirm
                await db.update_conversation_status(thread_id, "saved")
                return

    # Fetch the original message that started this thread.
    parent = await _fetch_parent_message(thread)
    if parent is None:
        # Best-effort: mention the user who initiated the retry so they know
        # we couldn't help. We don't have a conv to look up an author from.
        retry_initiator_id = message.author.id if message.author else None
        await thread.send(
            _format_mention(retry_initiator_id)
            + ":x: I can't find the original message — was it deleted? "
            "Please send a fresh entry in the main channel."
        )
        return

    # Nudge if the user posted a new attachment in an orphan thread —
    # we're retrying the *parent*, not processing the new file.
    if status == "retry_orphan" and had_attachments:
        await thread.send(
            ":information_source: _I noticed your new attachment, but I'm "
            "retrying the **original** message in this thread. If you want me "
            "to process the new attachment as its own entry, send it in the "
            "main channel instead._"
        )

    # Pick the right intro for the user.
    if status == "retry_orphan":
        intro = (
            ":arrows_counterclockwise: _I lost context for this thread "
            "(probably crashed earlier). Re-processing the original message..._"
        )
    elif status == "retry_confirmed":
        intro = ":arrows_counterclockwise: _Re-extracting from scratch..._"
    else:  # retry_active
        intro = ":arrows_counterclockwise: _Reprocessing the original message from scratch..._"
    await thread.send(intro)

    # Determine which sheet tab this thread belongs to. Prefer the stored
    # tab from the conv row (if any); fall back to the parent channel.
    deleted_conv = await db.delete_conversation(thread_id)
    tab_name = (deleted_conv or {}).get("tab_name") if deleted_conv else None
    if not tab_name:
        tab_name = get_tab_for_channel(parent.channel.id) or "Entries"

    # Set the original message back to ⏳ while we work.
    try:
        await set_reaction(parent, EMOJI_PROCESSING)
    except Exception:
        pass

    # Pull content from the parent and re-extract.
    parent_images, parent_audio = await download_attachments(parent)
    parent_text = parent.content.strip() if parent.content else None

    if not parent_text and not parent_images and not parent_audio:
        await thread.send(
            ":x: The original message has no text, image, or audio I can process."
        )
        try:
            await set_reaction(parent, EMOJI_QUESTION)
        except Exception:
            pass
        return

    # Pass the parent author's ID so the (re-created) conversation is
    # associated with them, and any subsequent prompts mention them.
    parent_author_id = parent.author.id if parent.author else None

    result = await entry_manager.create_entry(
        thread_id=thread_id,
        message_url=parent.jump_url,
        message_id=parent.id,
        text=parent_text,
        images=parent_images,
        audio=parent_audio,
        tab_name=tab_name,
        user_id=parent_author_id,
    )

    if result.audio_transcription:
        await thread.send(f"**Heard:** {result.audio_transcription}")
    await thread.send(
        _with_mention_if_needed(result.response_text, parent_author_id, result.status)
    )

    if result.status == "saved":
        await set_reaction(parent, EMOJI_DONE)
    else:
        await set_reaction(parent, EMOJI_NEEDS_INPUT)

    logger.info(
        "thread=%d op=retry | Done (status=%s, new_result_status=%s)",
        thread_id, status, result.status,
    )


async def _request_retry_confirmation(
    thread: discord.Thread, thread_id: int,
) -> None:
    """Tell the user a destructive retry would wipe saved sheet rows, and
    set the conversation status so the next reply can confirm or cancel.

    This is the highest-stakes prompt the bot sends (it touches the
    spreadsheet), so it always @-mentions the original author.
    """
    conv = await db.get_conversation(thread_id)
    if not conv:
        await thread.send(
            "Something's off — I can't find this conversation in my records. "
            "Try sending the original message again in the main channel."
        )
        return

    rows = conv.get("sheet_row_numbers") or []
    rows_phrase = "row" if len(rows) == 1 else "rows"
    user_id = await _resolve_user_id(conv, thread)
    msg = (
        f"{_format_mention(user_id)}"
        f":warning: This entry is already saved to the spreadsheet "
        f"({len(rows)} {rows_phrase}: {rows}).\n\n"
        f"Retrying will **delete the saved {rows_phrase}** and re-extract from "
        f"the original message from scratch.\n\n"
        f"Reply `yes` to confirm (variants like `yes!`, `Yes.`, or `yep` "
        f"also work). Any other reply keeps the saved entry as-is."
    )
    await db.update_conversation_status(thread_id, "pending_retry_confirm")
    await thread.send(msg)
    await db.add_message(thread_id, "bot", msg)

    # Also flip the parent message's emoji to speech-bubble so the user
    # notices from the channel list, not just the @-mention notification.
    await _update_original_reaction(conv.get("message_url"))


# ---------------------------------------------------------------------------
# Delete: remove the thread's entries from the spreadsheet
# ---------------------------------------------------------------------------

async def _handle_delete(message: discord.Message, status: str) -> None:
    """Orchestrate a delete triggered from a thread reply.

    status is one of:
      delete_needs_confirm — conv exists, status=saved, sheet rows present;
                             ask user to confirm
      delete_confirmed     — user already said yes after a needs_confirm prompt
    """
    thread = message.channel
    thread_id = thread.id

    # Confirmation gate: just ask, don't act.
    if status == "delete_needs_confirm":
        await _request_delete_confirmation(thread, thread_id)
        return

    # User confirmed — actually delete.
    conv = await db.get_conversation(thread_id)
    if not conv:
        await thread.send(
            ":x: Something went wrong — I lost the conversation state. "
            "Nothing was deleted."
        )
        return

    rows = conv.get("sheet_row_numbers") or []
    tab = conv.get("tab_name", "Entries")
    user_id = await _resolve_user_id(conv, thread)

    if not rows:
        # Edge case: nothing to delete on the sheet. Just mark as deleted.
        await db.update_conversation_status(thread_id, "deleted")
        await thread.send(
            _format_mention(user_id)
            + f"{EMOJI_DELETED} Done — there were no sheet rows to remove, "
            "and the entry has been marked as deleted."
        )
        await _set_parent_reaction(thread, EMOJI_DELETED)
        return

    try:
        deleted_count = await sheets_manager.delete_rows(rows, tab_name=tab)
        logger.info(
            "thread=%d op=delete | Deleted %d sheet row(s) %s from tab %s",
            thread_id, deleted_count, rows, tab,
        )
    except Exception:
        logger.exception(
            "thread=%d op=delete | Failed to delete sheet rows", thread_id,
        )
        await thread.send(
            _format_mention(user_id)
            + ":x: Couldn't delete the sheet rows. The entry is still saved."
        )
        # Revert state so the user isn't stuck in pending_delete_confirm
        await db.update_conversation_status(thread_id, "saved")
        return

    # Mark the conversation as deleted. We deliberately keep entries_json and
    # sheet_rows_json intact as an audit trail (in case we ever want to add
    # undo later or for diagnostic purposes), but reset sheet_rows_json since
    # those rows no longer exist.
    await db.update_conversation_status(thread_id, "deleted", [])

    rows_phrase = "row" if len(rows) == 1 else "rows"
    response_text = (
        f"{EMOJI_DELETED} Deleted {len(rows)} {rows_phrase} from the spreadsheet. "
        f"This entry is gone."
    )
    await thread.send(response_text)
    await db.add_message(thread_id, "bot", response_text)

    # Flip the parent message's emoji to 🗑️ so the channel list shows the
    # entry was removed.
    await _set_parent_reaction(thread, EMOJI_DELETED)


async def _request_delete_confirmation(
    thread: discord.Thread, thread_id: int,
) -> None:
    """Tell the user what will be deleted and set the conversation to
    pending_delete_confirm so the next reply can confirm or cancel.

    Always @-mentions because this is a high-stakes prompt (touches data).
    The preview reads the LIVE sheet rows (not SQLite) so the user sees
    exactly what's about to be removed — guards against any drift between
    SQLite and the sheet (e.g. manual edits, or the 5/6 "amount: 350 -> 0"
    incident that motivated this fix).
    """
    conv = await db.get_conversation(thread_id)
    if not conv:
        await thread.send(
            "Something's off — I can't find this conversation in my records. "
            "Try sending the original message again in the main channel."
        )
        return

    rows = conv.get("sheet_row_numbers") or []
    tab = conv.get("tab_name", "Entries")
    rows_phrase = "row" if len(rows) == 1 else "rows"

    # Fetch live sheet data for the preview rather than trusting SQLite.
    try:
        sheet_rows = await sheets_manager.get_rows_data(rows, tab_name=tab) if rows else []
    except Exception:
        logger.exception(
            "thread=%d op=delete_confirm | Failed to fetch live sheet data; "
            "falling back to SQLite preview", thread_id,
        )
        sheet_rows = []

    # If we couldn't fetch sheet data, fall back to SQLite entries.
    preview_source = sheet_rows or (conv.get("entries") or [])
    preview_lines: list[str] = []
    for entry in preview_source:
        if entry.get("missing"):
            preview_lines.append(
                f"• (row {entry.get('row_number')} no longer in sheet)"
            )
            continue
        date = entry.get("date", "?")
        typ = (entry.get("type") or "?").capitalize()
        cat = entry.get("category") or "?"
        who = entry.get("client_or_event") or entry.get("vendor") or ""
        amount = entry.get("amount", 0)
        if isinstance(amount, str):
            try:
                amount = float(amount.replace("$", "").replace(",", ""))
            except ValueError:
                amount = 0
        bits = [date, typ, cat]
        if who:
            bits.append(who)
        bits.append(f"${amount:,.2f}")
        preview_lines.append("• " + " | ".join(bits))

    preview_block = "\n".join(preview_lines) if preview_lines else "(no entries)"

    user_id = await _resolve_user_id(conv, thread)
    msg = (
        f"{_format_mention(user_id)}"
        f":warning: Delete this entry permanently? This will remove "
        f"**{len(rows)} {rows_phrase}** from the spreadsheet:\n\n"
        f"{preview_block}\n\n"
        f"Reply `yes` to confirm (variants like `yes!`, `Yes.`, or `yep` "
        f"also work). Any other reply keeps the saved entry as-is."
    )
    await db.update_conversation_status(thread_id, "pending_delete_confirm")
    await thread.send(msg)
    await db.add_message(thread_id, "bot", msg)

    # Flip the parent message's emoji to speech-bubble so the user notices
    # from the channel list, not just the @-mention notification.
    await _update_original_reaction(conv.get("message_url"))


async def _set_parent_reaction(thread: discord.Thread, emoji: str) -> None:
    """Set a reaction on the thread's parent message. Best-effort."""
    try:
        parent = thread.starter_message
        if parent is None:
            parent = await _fetch_parent_message(thread)
        if parent is not None:
            await set_reaction(parent, emoji)
    except Exception:
        logger.debug(
            "thread=%d op=set_parent_reaction | Could not set %s",
            thread.id, emoji,
        )


async def _fetch_parent_message(thread: discord.Thread) -> discord.Message | None:
    """Fetch the message that started a thread. For threads created from a
    message (`message.create_thread`), the thread.id equals the source
    message.id. Returns None if the message was deleted or inaccessible.
    """
    # Fast path: discord.py caches starter_message if it's still in cache.
    cached = thread.starter_message
    if cached is not None:
        return cached
    parent_channel = bot.get_channel(thread.parent_id)
    if parent_channel is None:
        try:
            parent_channel = await bot.fetch_channel(thread.parent_id)
        except (discord.NotFound, discord.Forbidden):
            return None
    try:
        return await parent_channel.fetch_message(thread.id)
    except (discord.NotFound, discord.Forbidden):
        return None
    except Exception:
        logger.exception(
            "thread=%d op=fetch_parent | Unexpected error", thread.id,
        )
        return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Show available commands and usage instructions."""
    channel_mentions = " or ".join(f"<#{ch}>" for ch in CHANNEL_TAB_MAP)
    help_text = (
        "**Musician Expense Tracker — Help**\n\n"
        "**How to use:**\n"
        f"Send a message in {channel_mentions} with any combination of:\n"
        "• A photo of a receipt or invoice\n"
        "• A text description of income or an expense\n"
        "• A voice message describing the transaction\n\n"
        "If the bot is confident, it saves directly to the spreadsheet.\n"
        "If something is unclear, it asks you to clarify in a thread.\n\n"
        "**Commands:**\n"
        "`!summary` — Show this month's income & expense summary\n"
        "`!summary MM YYYY` — Show summary for a specific month (e.g. `!summary 01 2026`)\n"
        "`!undo` — Remove the last entry from the spreadsheet\n"
        "`!categories` — List available categories\n"
        "`!help` — Show this message\n"
    )
    await ctx.send(help_text)


@bot.command(name="summary")
async def summary_command(ctx: commands.Context, month: int = 0, year: int = 0):
    """Show monthly income/expense summary."""
    if month == 0:
        now = datetime.now()
        month = now.month
        year = now.year

    try:
        # Determine which tab to query based on the channel
        tab = get_tab_for_channel(ctx.channel.id) or "Entries"
        await ctx.message.add_reaction(EMOJI_PROCESSING)
        data = await sheets_manager.get_monthly_summary(month, year, tab_name=tab)
        await ctx.message.remove_reaction(EMOJI_PROCESSING, bot.user)

        month_name = datetime(year, month, 1).strftime("%B %Y")
        lines = [f"**Summary for {month_name}:**\n"]

        if data["income"]:
            lines.append("**Income:**")
            for cat, amt in sorted(data["income"].items()):
                lines.append(f"• {cat}: ${amt:,.2f}")
            lines.append(f"**Total Income: ${data['total_income']:,.2f}**\n")
        else:
            lines.append("**Income:** None recorded\n")

        if data["expenses"]:
            lines.append("**Expenses:**")
            for cat, amt in sorted(data["expenses"].items()):
                lines.append(f"• {cat}: ${amt:,.2f}")
            lines.append(f"**Total Expenses: ${data['total_expenses']:,.2f}**\n")
        else:
            lines.append("**Expenses:** None recorded\n")

        net = data["total_income"] - data["total_expenses"]
        lines.append(f"**Net: ${net:,.2f}**")

        await ctx.send("\n".join(lines))

    except Exception:
        logger.exception("op=summary_command | Error")
        await ctx.send("Sorry, something went wrong fetching the summary.")


@bot.command(name="recover")
@commands.is_owner()
async def recover_command(ctx: commands.Context, max_age_days: int = 365):
    """One-off recovery for stuck conversations older than the auto-recovery
    cap. Bot owner only. Defaults to 365 days (essentially "all"), which is
    safe because each recovered conversation is touched in SQLite so it's
    only recovered once across all invocations.

    Usage: !recover         (recovers everything stuck)
           !recover 90      (recovers stuck convos from the last 90 days)
    """
    try:
        await ctx.message.add_reaction(EMOJI_PROCESSING)
        counts = await recover_stuck_conversations(max_age_days=max_age_days)
        await ctx.message.remove_reaction(EMOJI_PROCESSING, bot.user)

        if counts["recovered"] == 0 and counts["skipped"] == 0:
            await ctx.send("No stuck conversations to recover.")
        else:
            await ctx.send(
                f"Recovery done: **{counts['recovered']}** reposted, "
                f"**{counts['skipped']}** skipped, "
                f"**{counts['failed']}** failed (max_age={max_age_days}d)."
            )
    except Exception:
        logger.exception("op=recover_command | Error")
        await ctx.send("Sorry, something went wrong during recovery.")


@bot.command(name="undo")
async def undo_command(ctx: commands.Context):
    """Remove the last entry from the spreadsheet."""
    try:
        tab = get_tab_for_channel(ctx.channel.id) or "Entries"
        await ctx.message.add_reaction(EMOJI_PROCESSING)
        deleted = await sheets_manager.delete_last_entry(tab_name=tab)
        await ctx.message.remove_reaction(EMOJI_PROCESSING, bot.user)

        if deleted:
            await ctx.send(
                f"Removed the last entry:\n"
                f"• {deleted.get('Date', '?')} | {deleted.get('Type', '?')} | "
                f"{deleted.get('Category', '?')} | ${deleted.get('Amount ($)', '?')} | "
                f"{deleted.get('Description', '?')}"
            )
        else:
            await ctx.send("The spreadsheet is empty — nothing to undo.")

    except Exception:
        logger.exception("op=undo_command | Error")
        await ctx.send("Sorry, something went wrong removing the last entry.")


@bot.command(name="categories")
async def categories_command(ctx: commands.Context):
    """List available categories."""
    await ctx.send(
        "**Available Categories:**\n\n"
        "**Income:**\n"
        "• Teaching\n"
        "• Performance\n\n"
        "**Expenses:**\n"
        "• IT\n"
        "• Performance\n"
        "• Teaching\n\n"
        "*The bot will auto-categorize entries. If it's unsure, it will ask you.*"
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def main():
    if not DISCORD_TOKEN:
        logger.error("op=startup | DISCORD_TOKEN is not set")
        sys.exit(1)
    if not CHANNEL_TAB_MAP:
        logger.warning("op=startup | No channels configured — bot won't process messages")

    logger.info("op=startup | Starting bot...")
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime

import discord
from discord.ext import commands
from google.genai import types

import gemini_processor
import sheets_manager
from config import CHANNEL_ID, CONFIDENCE_THRESHOLD, DISCORD_TOKEN
from prompts import ExtractionResult

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("musician-bot")

# ---------------------------------------------------------------------------
# Emoji constants
# ---------------------------------------------------------------------------
EMOJI_PROCESSING = "\u23f3"       # hourglass — working on it
EMOJI_DONE = "\u2705"             # green checkmark — saved to sheet
EMOJI_NEEDS_INPUT = "\U0001f4ac"  # speech bubble — waiting for user
EMOJI_ERROR = "\u274c"            # red X — actual error/crash

# ---------------------------------------------------------------------------
# Pending entry tracking
# ---------------------------------------------------------------------------

@dataclass
class PendingEntry:
    """Tracks an in-progress extraction for a Discord thread."""

    result: ExtractionResult
    original_parts: list[types.Part] = field(default_factory=list)
    original_message_url: str = ""
    original_text: str = ""
    status: str = "pending"  # "pending_clarification" | "saved"
    saved_row_numbers: list[int] = field(default_factory=list)


# thread_id -> PendingEntry
pending_entries: dict[int, PendingEntry] = {}

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
AUDIO_MIME_TYPES = {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm"}

CONFIRM_WORDS = {"yes", "y", "confirm", "correct", "ok", "looks good", "lgtm", "approve", "yep", "yeah"}


def format_entry(entry, index: int | None = None) -> str:
    """Format a single FinancialEntry for display in Discord."""
    prefix = f"**Entry {index}:**\n" if index is not None else ""
    lines = [
        f"• **Date:** {entry.date}",
        f"• **Type:** {entry.type.capitalize()}",
        f"• **Category:** {entry.category}",
    ]
    if entry.client_or_event:
        lines.append(f"• **Client/Event:** {entry.client_or_event}")
    if entry.vendor:
        lines.append(f"• **Vendor:** {entry.vendor}")
    if entry.mode_of_payment:
        lines.append(f"• **Payment:** {entry.mode_of_payment}")
    lines.append(f"• **Amount:** ${entry.amount:,.2f}")
    if entry.description:
        lines.append(f"• **Description:** {entry.description}")
    if entry.notes:
        lines.append(f"• **Notes:** {entry.notes}")
    return prefix + "\n".join(lines)


def format_extraction_message(result: ExtractionResult, saved: bool = False) -> str:
    """Build the full Discord message for an extraction result.

    Args:
        result: The extraction result from Gemini.
        saved: If True, this was auto-confirmed and already saved to the sheet.
    """
    if not result.entries:
        msg = f"I wasn't able to extract any financial data.\n\n> {result.raw_summary}"
        if result.clarifying_questions:
            msg += "\n\n**I have some questions:**\n"
            for q in result.clarifying_questions:
                msg += f"• {q}\n"
        return msg

    count = len(result.entries)
    if saved:
        header = f"**Saved {count} {'entry' if count == 1 else 'entries'} to the spreadsheet!**\n"
    else:
        header = f"**Here's what I found ({count} {'entry' if count == 1 else 'entries'}):**\n"

    entry_blocks = []
    for i, entry in enumerate(result.entries, start=1):
        label = i if count > 1 else None
        entry_blocks.append(format_entry(entry, label))

    msg = header + "\n\n".join(entry_blocks)

    if result.clarifying_questions:
        msg += "\n\n**I have some questions:**\n"
        for q in result.clarifying_questions:
            msg += f"• {q}\n"
        msg += "\nPlease answer the questions above, or tell me what to fix."
    elif saved:
        msg += "\n\n_Reply here if anything needs to be corrected._"
    else:
        msg += "\n\nReply **yes** to confirm, or tell me what needs to be corrected."

    return msg


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
            logger.info("Downloaded image: %s (%s)", attachment.filename, content_type)
        elif content_type in AUDIO_MIME_TYPES:
            data = await attachment.read()
            audio = (data, content_type)
            logger.info("Downloaded audio: %s (%s)", attachment.filename, content_type)
        else:
            logger.info("Skipping unsupported attachment: %s (%s)", attachment.filename, content_type)

    # Discord voice messages use a special flag
    if message.flags.value & (1 << 13):  # IS_VOICE_MESSAGE flag
        for attachment in message.attachments:
            if not audio:
                data = await attachment.read()
                audio = (data, "audio/ogg")
                logger.info("Downloaded voice message: %s", attachment.filename)

    return images, audio


def build_original_parts(
    text: str | None,
    images: list[tuple[bytes, str]],
    audio: tuple[bytes, str] | None,
) -> list[types.Part]:
    """Build Gemini Part objects from the original message content."""
    parts: list[types.Part] = []
    if text:
        parts.append(types.Part.from_text(text=text))
    for img_bytes, mime_type in images:
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime_type))
    if audio:
        parts.append(types.Part.from_bytes(data=audio[0], mime_type=audio[1]))
    return parts


async def write_entries_to_sheet(pending: PendingEntry) -> list[int]:
    """Write all entries from a PendingEntry to Google Sheets. Returns row numbers written."""
    row_numbers: list[int] = []
    for entry in pending.result.entries:
        row_num = await sheets_manager.append_entry(
            date=entry.date,
            entry_type=entry.type,
            category=entry.category,
            amount=entry.amount,
            discord_link=pending.original_message_url,
            client_or_event=entry.client_or_event,
            vendor=entry.vendor,
            mode_of_payment=entry.mode_of_payment,
            description=entry.description,
            notes=entry.notes,
        )
        row_numbers.append(row_num)
    return row_numbers


async def set_reaction(message: discord.Message, emoji: str) -> None:
    """Clear all bot reactions and set a single new one."""
    for reaction in message.reactions:
        if reaction.me:
            try:
                await message.remove_reaction(reaction.emoji, bot.user)
            except Exception:
                pass
    try:
        await message.add_reaction(emoji)
    except Exception:
        logger.exception("Failed to set reaction %s", emoji)


# ---------------------------------------------------------------------------
# Thread reconstruction (for threads lost after bot restart)
# ---------------------------------------------------------------------------

async def reconstruct_pending_entry(thread: discord.Thread) -> PendingEntry | None:
    """Rebuild a PendingEntry from an untracked thread's history.

    This handles the case where the bot restarted and lost in-memory state,
    but the user still wants to correct an entry in an existing thread.
    """
    try:
        # Get the starter message (the original expense message)
        starter = thread.starter_message
        if not starter:
            # Starter message might not be cached; fetch from the parent channel
            starter = await thread.parent.fetch_message(thread.id)

        # Re-download original attachments
        images, audio = await download_attachments(starter)
        text = starter.content.strip() if starter.content else None

        if not text and not images and not audio:
            return None

        # Re-extract from original content
        result = await gemini_processor.extract_financial_data(
            text=text, images=images, audio=audio
        )
        original_parts = build_original_parts(text, images, audio)

        # Look up saved rows by discord link
        saved_rows = await sheets_manager.find_rows_by_discord_link(starter.jump_url)

        logger.info(
            "Reconstructed pending entry for thread %d (found %d saved rows)",
            thread.id,
            len(saved_rows),
        )

        return PendingEntry(
            result=result,
            original_parts=original_parts,
            original_message_url=starter.jump_url,
            original_text=text or "",
            status="saved" if saved_rows else "pending_clarification",
            saved_row_numbers=saved_rows,
        )

    except Exception:
        logger.exception("Failed to reconstruct pending entry for thread %d", thread.id)
        return None


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    logger.info("Bot is ready! Logged in as %s (ID: %s)", bot.user.name, bot.user.id)
    if CHANNEL_ID:
        logger.info("Listening for messages in channel %s", CHANNEL_ID)
    else:
        logger.warning("CHANNEL_ID not set — bot will not process any messages")


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    # New message in the designated channel (not a thread)
    if (
        CHANNEL_ID
        and str(message.channel.id) == str(CHANNEL_ID)
        and not isinstance(message.channel, discord.Thread)
    ):
        await handle_new_entry(message)

    # Follow-up in a tracked thread
    elif isinstance(message.channel, discord.Thread) and message.channel.id in pending_entries:
        await handle_thread_reply(message)

    # Follow-up in an untracked thread (bot restarted, lost in-memory state)
    elif (
        isinstance(message.channel, discord.Thread)
        and message.channel.parent_id
        and str(message.channel.parent_id) == str(CHANNEL_ID)
        and message.channel.id not in pending_entries
    ):
        await handle_untracked_thread_reply(message)


async def handle_new_entry(message: discord.Message):
    """Process a new message in the expenses channel."""
    try:
        await message.add_reaction(EMOJI_PROCESSING)

        images, audio = await download_attachments(message)
        text = message.content.strip() if message.content else None

        if not text and not images and not audio:
            await set_reaction(message, "\u2753")  # question mark
            return

        # Create a thread
        thread = await message.create_thread(
            name=f"Entry {datetime.now().strftime('%b %d %H:%M')}",
            auto_archive_duration=1440,
        )

        # Extract via Gemini
        result = await gemini_processor.extract_financial_data(
            text=text, images=images, audio=audio
        )

        original_parts = build_original_parts(text, images, audio)
        is_confident = (
            result.confidence >= CONFIDENCE_THRESHOLD
            and not result.clarifying_questions
            and len(result.entries) > 0
        )

        if is_confident:
            # --- AUTO-CONFIRM: write to sheet immediately ---
            pending = PendingEntry(
                result=result,
                original_parts=original_parts,
                original_message_url=message.jump_url,
                original_text=text or "",
                status="saved",
            )
            row_numbers = await write_entries_to_sheet(pending)
            pending.saved_row_numbers = row_numbers

            # Keep thread tracked so user can send corrections
            pending_entries[thread.id] = pending

            response_text = format_extraction_message(result, saved=True)
            await thread.send(response_text)
            await set_reaction(message, EMOJI_DONE)

            # Log to conversation log
            await sheets_manager.log_conversation(
                user_input=text or "(attachment)",
                bot_response=response_text[:500],
                outcome="auto-confirmed",
                discord_link=message.jump_url,
            )
            logger.info("Auto-confirmed %d entries (rows %s)", len(row_numbers), row_numbers)

        else:
            # --- NEEDS CLARIFICATION: ask the user ---
            pending_entries[thread.id] = PendingEntry(
                result=result,
                original_parts=original_parts,
                original_message_url=message.jump_url,
                original_text=text or "",
                status="pending_clarification",
            )
            response_text = format_extraction_message(result, saved=False)
            await thread.send(response_text)
            await set_reaction(message, EMOJI_NEEDS_INPUT)

    except Exception:
        logger.exception("Error processing new entry")
        await set_reaction(message, EMOJI_ERROR)
        try:
            thread = await message.create_thread(name="Error", auto_archive_duration=60)
            await thread.send(
                "Sorry, something went wrong processing this message. "
                "Please try again or check the bot logs."
            )
        except Exception:
            logger.exception("Failed to create error thread")


async def handle_thread_reply(message: discord.Message):
    """Process a follow-up message in a tracked thread."""
    thread_id = message.channel.id
    pending = pending_entries[thread_id]

    try:
        user_text = message.content.strip().lower()

        # If entry isn't saved yet, check if user is confirming
        if (
            pending.status != "saved"
            and user_text in CONFIRM_WORDS
            and len(pending.result.entries) > 0
        ):
            await message.add_reaction(EMOJI_PROCESSING)

            row_numbers = await write_entries_to_sheet(pending)
            pending.saved_row_numbers = row_numbers
            pending.status = "saved"

            await set_reaction(message, EMOJI_DONE)
            response_text = format_extraction_message(pending.result, saved=True)
            await message.channel.send(response_text)

            # Update reaction on original message
            try:
                original_msg = message.channel.starter_message
                if original_msg:
                    await set_reaction(original_msg, EMOJI_DONE)
            except Exception:
                logger.exception("Could not update original message reaction")

            # Log conversation
            await sheets_manager.log_conversation(
                user_input=pending.original_text or "(attachment)",
                bot_response=response_text[:500],
                outcome="user-confirmed",
                discord_link=pending.original_message_url,
            )
            return

        # User is providing corrections or answering questions
        await message.add_reaction(EMOJI_PROCESSING)

        new_images, new_audio = await download_attachments(message)
        full_text = message.content.strip() if message.content else ""

        if new_images or new_audio:
            all_parts = list(pending.original_parts)
            new_parts = build_original_parts(full_text, new_images, new_audio)
            all_parts.extend(new_parts)
            pending.original_parts = all_parts

            result = await gemini_processor.extract_financial_data(
                text=full_text, images=new_images, audio=new_audio,
            )
        else:
            result = await gemini_processor.process_followup(
                original_parts=pending.original_parts,
                previous_result=pending.result,
                user_reply=message.content.strip(),
            )

        # If previously saved, delete old rows before writing corrected ones
        if pending.status == "saved" and pending.saved_row_numbers:
            deleted = await sheets_manager.delete_rows(pending.saved_row_numbers)
            logger.info("Deleted %d old rows for correction", deleted)
            pending.saved_row_numbers = []

        pending.result = result

        is_confident = (
            result.confidence >= CONFIDENCE_THRESHOLD
            and not result.clarifying_questions
            and len(result.entries) > 0
        )

        if is_confident:
            # Data is good — save (or re-save after correction)
            row_numbers = await write_entries_to_sheet(pending)
            pending.saved_row_numbers = row_numbers
            pending.status = "saved"

            await set_reaction(message, EMOJI_DONE)
            response_text = format_extraction_message(result, saved=True)
            await message.channel.send(response_text)

            try:
                original_msg = message.channel.starter_message
                if original_msg:
                    await set_reaction(original_msg, EMOJI_DONE)
            except Exception:
                logger.exception("Could not update original message reaction")

            await sheets_manager.log_conversation(
                user_input=pending.original_text or "(attachment)",
                bot_response=response_text[:500],
                outcome="corrected",
                discord_link=pending.original_message_url,
            )
        else:
            # Still not confident — ask again
            pending.status = "pending_clarification"
            await set_reaction(message, EMOJI_NEEDS_INPUT)
            response_text = format_extraction_message(result, saved=False)
            await message.channel.send(response_text)

            # Update original message to show it needs input
            try:
                original_msg = message.channel.starter_message
                if original_msg:
                    await set_reaction(original_msg, EMOJI_NEEDS_INPUT)
            except Exception:
                logger.exception("Could not update original message reaction")

    except Exception:
        logger.exception("Error processing thread reply")
        await set_reaction(message, EMOJI_ERROR)
        await message.channel.send(
            "Sorry, something went wrong. Please try again or rephrase your correction."
        )

        await sheets_manager.log_conversation(
            user_input=pending.original_text or "(attachment)",
            bot_response="Error during processing",
            outcome="error",
            discord_link=pending.original_message_url,
        )


async def handle_untracked_thread_reply(message: discord.Message):
    """Handle a reply in a thread the bot created but lost track of (e.g. after restart)."""
    thread = message.channel

    try:
        await message.add_reaction(EMOJI_PROCESSING)

        # Reconstruct the pending entry from thread history
        pending = await reconstruct_pending_entry(thread)
        if not pending:
            await set_reaction(message, EMOJI_ERROR)
            await thread.send(
                "Sorry, I couldn't find the original message for this thread. "
                "Please create a new entry in the main channel."
            )
            return

        # Store it so future replies in this thread are handled normally
        pending_entries[thread.id] = pending

        # Now process the user's correction through the normal followup flow
        result = await gemini_processor.process_followup(
            original_parts=pending.original_parts,
            previous_result=pending.result,
            user_reply=message.content.strip(),
        )

        # If previously saved, delete old rows before writing corrected ones
        if pending.saved_row_numbers:
            deleted = await sheets_manager.delete_rows(pending.saved_row_numbers)
            logger.info("Deleted %d old rows for correction (reconstructed)", deleted)
            pending.saved_row_numbers = []

        pending.result = result

        is_confident = (
            result.confidence >= CONFIDENCE_THRESHOLD
            and not result.clarifying_questions
            and len(result.entries) > 0
        )

        if is_confident:
            row_numbers = await write_entries_to_sheet(pending)
            pending.saved_row_numbers = row_numbers
            pending.status = "saved"

            await set_reaction(message, EMOJI_DONE)
            response_text = format_extraction_message(result, saved=True)
            await thread.send(response_text)

            try:
                original_msg = thread.starter_message
                if original_msg:
                    await set_reaction(original_msg, EMOJI_DONE)
            except Exception:
                logger.exception("Could not update original message reaction")

            await sheets_manager.log_conversation(
                user_input=pending.original_text or "(attachment)",
                bot_response=response_text[:500],
                outcome="corrected-after-restart",
                discord_link=pending.original_message_url,
            )
        else:
            pending.status = "pending_clarification"
            await set_reaction(message, EMOJI_NEEDS_INPUT)
            response_text = format_extraction_message(result, saved=False)
            await thread.send(response_text)

    except Exception:
        logger.exception("Error handling untracked thread reply")
        await set_reaction(message, EMOJI_ERROR)
        await thread.send(
            "Sorry, something went wrong. Please try again or rephrase your correction."
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Show available commands and usage instructions."""
    help_text = (
        "**Musician Expense Tracker — Help**\n\n"
        "**How to use:**\n"
        f"Send a message in <#{CHANNEL_ID}> with any combination of:\n"
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
        await ctx.message.add_reaction(EMOJI_PROCESSING)
        data = await sheets_manager.get_monthly_summary(month, year)
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
        logger.exception("Error getting monthly summary")
        await ctx.send("Sorry, something went wrong fetching the summary.")


@bot.command(name="undo")
async def undo_command(ctx: commands.Context):
    """Remove the last entry from the spreadsheet."""
    try:
        await ctx.message.add_reaction(EMOJI_PROCESSING)
        deleted = await sheets_manager.delete_last_entry()
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
        logger.exception("Error undoing last entry")
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
        logger.error("DISCORD_TOKEN is not set. Please check your .env file.")
        sys.exit(1)
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID is not set — the bot won't process any messages.")

    logger.info("Starting bot...")
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()

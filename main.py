import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime

import discord
from aiohttp import web
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
    status: str = "pending"  # "pending_clarification"


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
    elif not saved:
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


async def write_entries_to_sheet(pending: PendingEntry) -> int:
    """Write all entries from a PendingEntry to Google Sheets. Returns count written."""
    count = 0
    for entry in pending.result.entries:
        await sheets_manager.append_entry(
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
        count += 1
    return count


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
# Health check server (keeps Railway happy)
# ---------------------------------------------------------------------------

async def _health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_health_server() -> None:
    """Start a minimal HTTP server so Railway's network check passes."""
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", _health_check)
    app.router.add_get("/health", _health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health check server listening on port %d", port)


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
async def setup_hook():
    """Called when the bot is starting up, before connecting to Discord."""
    await start_health_server()


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
            )
            count = await write_entries_to_sheet(pending)

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
            logger.info("Auto-confirmed %d entries", count)

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

        # Check if user is confirming
        if user_text in CONFIRM_WORDS and len(pending.result.entries) > 0:
            await message.add_reaction(EMOJI_PROCESSING)

            count = await write_entries_to_sheet(pending)

            await message.remove_reaction(EMOJI_PROCESSING, bot.user)
            response_text = format_extraction_message(pending.result, saved=True)
            await message.channel.send(response_text)

            # Update reaction on original message
            try:
                original_channel = bot.get_channel(int(CHANNEL_ID))
                if original_channel:
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

            del pending_entries[thread_id]
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

        pending.result = result

        is_confident = (
            result.confidence >= CONFIDENCE_THRESHOLD
            and not result.clarifying_questions
            and len(result.entries) > 0
        )

        await message.remove_reaction(EMOJI_PROCESSING, bot.user)

        if is_confident:
            # After clarification the data is now good — auto-save
            count = await write_entries_to_sheet(pending)

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

            del pending_entries[thread_id]
        else:
            # Still not confident — ask again
            response_text = format_extraction_message(result, saved=False)
            await message.channel.send(response_text)

    except Exception:
        logger.exception("Error processing thread reply")
        try:
            await message.remove_reaction(EMOJI_PROCESSING, bot.user)
        except Exception:
            pass
        await message.channel.send(
            "Sorry, something went wrong. Please try again or rephrase your correction."
        )

        await sheets_manager.log_conversation(
            user_input=pending.original_text or "(attachment)",
            bot_response="Error during processing",
            outcome="error",
            discord_link=pending.original_message_url,
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

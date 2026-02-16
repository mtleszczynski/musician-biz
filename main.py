"""Discord bot entry point — thin event dispatcher.

All business logic lives in entry_manager.py. This file handles:
- Discord event routing (on_message, commands)
- Downloading attachments from Discord messages
- Emoji reactions on messages
- Thread creation
- Startup / shutdown
"""

import logging
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
    thread = None
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

        # Delegate all logic to entry_manager
        result = await entry_manager.create_entry(
            thread_id=thread.id,
            message_url=message.jump_url,
            message_id=message.id,
            text=text,
            images=images,
            audio=audio,
            tab_name=tab_name,
        )

        # Post audio transcription if present (so user can see what was heard)
        if result.audio_transcription:
            await thread.send(
                f"**Heard:** {result.audio_transcription}"
            )

        # Post the response in the thread
        await thread.send(result.response_text)

        # Set appropriate emoji
        if result.status == "saved":
            await set_reaction(message, EMOJI_DONE)
        else:
            await set_reaction(message, EMOJI_NEEDS_INPUT)

    except Exception:
        logger.exception("thread=new op=handle_new_entry | Error")
        await set_reaction(message, EMOJI_ERROR)
        error_msg = (
            "Sorry, something went wrong processing this message. "
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
    thread_id = message.channel.id

    try:
        await message.add_reaction(EMOJI_PROCESSING)

        # Immediately show processing on the original channel message too
        try:
            original_msg = message.channel.starter_message
            if original_msg:
                await set_reaction(original_msg, EMOJI_PROCESSING)
        except Exception:
            pass

        images, audio = await download_attachments(message)
        user_text = message.content.strip() if message.content else ""

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

        # Post response
        await message.channel.send(result.response_text)

        # Set emoji on the reply
        if result.status in ("saved", "skipped"):
            await set_reaction(message, EMOJI_DONE)
        elif result.status == "error":
            await set_reaction(message, EMOJI_ERROR)
        else:
            await set_reaction(message, EMOJI_NEEDS_INPUT)

        # Also update the original message's emoji
        try:
            original_msg = message.channel.starter_message
            if original_msg:
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
        await message.channel.send(
            "Sorry, something went wrong. Please try again or rephrase your correction."
        )


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

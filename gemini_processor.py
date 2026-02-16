"""Gemini API integration for financial data extraction, media transcription,
and field-level corrections.
"""

import json
import logging
import time
from datetime import date

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL
from prompts import (
    CORRECTION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    IMAGE_DESCRIPTION_PROMPT,
    TRANSCRIPTION_PROMPT,
    ExtractionResult,
    FollowupResult,
)

logger = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)


def _build_date_context() -> str:
    """Return a short string telling the model today's date."""
    return f"Today's date is {date.today().isoformat()}."


# ---------------------------------------------------------------------------
# Audio transcription
# ---------------------------------------------------------------------------

async def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    """Transcribe an audio message using Gemini. Returns the transcription text.

    This is called once when audio is first received, and the result is cached
    in SQLite so we never re-send raw audio bytes.
    """
    t0 = time.monotonic()

    parts = [
        types.Part.from_text(text=TRANSCRIPTION_PROMPT),
        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
    ]

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=parts,
        config=types.GenerateContentConfig(temperature=0.1),
    )

    transcription = response.text.strip()
    elapsed = time.monotonic() - t0

    logger.info(
        "op=transcribe_audio | %.1fs, %d chars transcribed, mime=%s",
        elapsed, len(transcription), mime_type,
    )
    return transcription


# ---------------------------------------------------------------------------
# Image description
# ---------------------------------------------------------------------------

async def describe_image(image_bytes: bytes, mime_type: str) -> str:
    """Describe the financial information in an image using Gemini.

    Called once per image when first received; result cached in SQLite
    for use in subsequent correction calls.
    """
    t0 = time.monotonic()

    parts = [
        types.Part.from_text(text=IMAGE_DESCRIPTION_PROMPT),
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
    ]

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=parts,
        config=types.GenerateContentConfig(temperature=0.1),
    )

    description = response.text.strip()
    elapsed = time.monotonic() - t0

    logger.info(
        "op=describe_image | %.1fs, %d chars described, mime=%s",
        elapsed, len(description), mime_type,
    )
    return description


# ---------------------------------------------------------------------------
# Initial extraction
# ---------------------------------------------------------------------------

async def extract_financial_data(
    text: str | None = None,
    images: list[tuple[bytes, str]] | None = None,
    audio_transcription: str | None = None,
) -> ExtractionResult:
    """Extract financial data from text, images, and/or an audio transcription.

    For the initial extraction, raw image bytes are sent for best accuracy.
    Audio should already be transcribed (via transcribe_audio) before calling this.

    Args:
        text: Optional text message from the user.
        images: Optional list of (image_bytes, mime_type) tuples.
        audio_transcription: Optional pre-transcribed audio text.

    Returns:
        ExtractionResult with extracted entries, confidence, and questions.
    """
    t0 = time.monotonic()
    parts: list[types.Part] = []

    parts.append(types.Part.from_text(text=_build_date_context()))

    if text:
        parts.append(types.Part.from_text(text=text))

    if audio_transcription:
        parts.append(types.Part.from_text(
            text=f"[Audio transcription]: {audio_transcription}"
        ))

    if images:
        for img_bytes, mime_type in images:
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime_type))

    if len(parts) == 1:
        return ExtractionResult(
            entries=[],
            confidence=0.0,
            clarifying_questions=[
                "I didn't receive any text, image, or audio. Could you try again?"
            ],
            raw_summary="No input received.",
        )

    logger.info("op=extract | Sending %d part(s) to Gemini", len(parts))

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=EXTRACTION_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ExtractionResult,
            temperature=0.2,
        ),
    )

    result = ExtractionResult.model_validate_json(response.text)
    elapsed = time.monotonic() - t0

    logger.info(
        "op=extract | %.1fs, %d entries, confidence=%.2f",
        elapsed, len(result.entries), result.confidence,
    )
    return result


# ---------------------------------------------------------------------------
# Field-level correction (replaces the old full re-extraction followup)
# ---------------------------------------------------------------------------

async def process_correction(
    current_entries: list[dict],
    conversation_history: list[dict],
    user_reply: str,
) -> FollowupResult:
    """Process a user's correction or clarification reply.

    Instead of re-extracting everything, identifies only the specific fields
    the user wants to change. Fields not mentioned stay untouched.

    Args:
        current_entries: List of current FinancialEntry dicts (the entry state).
        conversation_history: List of {role, content} dicts from the thread.
        user_reply: The user's latest message.

    Returns:
        FollowupResult with targeted field updates and any remaining questions.
    """
    t0 = time.monotonic()

    # Build a multi-turn conversation for context
    contents: list[types.Content] = []

    # Show the model the conversation so far
    for msg in conversation_history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg["content"])],
            )
        )

    # Build a clear summary of current state for the model
    entry_summary = json.dumps(current_entries, indent=2)
    state_text = (
        f"CURRENT ENTRY STATE:\n{entry_summary}\n\n"
        f"The user's latest reply is below. Identify ONLY the fields they want to change."
    )
    contents.append(
        types.Content(
            role="model",
            parts=[types.Part.from_text(text=state_text)],
        )
    )

    # The user's correction
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_reply)],
        )
    )

    logger.info("op=correction | Sending %d turns to Gemini", len(contents))

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=CORRECTION_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=FollowupResult,
            temperature=0.2,
        ),
    )

    result = FollowupResult.model_validate_json(response.text)
    elapsed = time.monotonic() - t0

    logger.info(
        "op=correction | %.1fs, %d field updates, %d remaining questions, confirm=%s",
        elapsed, len(result.field_updates), len(result.remaining_questions),
        result.is_confirmation,
    )
    return result

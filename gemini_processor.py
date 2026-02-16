import logging
from datetime import date

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL
from prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    FOLLOWUP_SYSTEM_PROMPT,
    ExtractionResult,
)

logger = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)


def _build_date_context() -> str:
    """Return a short string telling the model today's date."""
    return f"Today's date is {date.today().isoformat()}."


async def extract_financial_data(
    text: str | None = None,
    images: list[tuple[bytes, str]] | None = None,
    audio: tuple[bytes, str] | None = None,
) -> ExtractionResult:
    """Extract financial data from any combination of text, images, and audio.

    Args:
        text: Optional text message from the user.
        images: Optional list of (image_bytes, mime_type) tuples.
        audio: Optional (audio_bytes, mime_type) tuple.

    Returns:
        ExtractionResult with extracted entries, confidence, and any clarifying questions.
    """
    parts: list[types.Part] = []

    parts.append(types.Part.from_text(text=_build_date_context()))

    if text:
        parts.append(types.Part.from_text(text=text))

    if images:
        for img_bytes, mime_type in images:
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime_type))

    if audio:
        audio_bytes, mime_type = audio
        parts.append(types.Part.from_bytes(data=audio_bytes, mime_type=mime_type))

    if len(parts) == 1:
        # Only the date context — no actual user content
        return ExtractionResult(
            entries=[],
            confidence=0.0,
            clarifying_questions=["I didn't receive any text, image, or audio. Could you try again?"],
            raw_summary="No input received.",
        )

    logger.info("Sending %d part(s) to Gemini for extraction", len(parts))

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
    logger.info(
        "Extraction complete: %d entries, confidence=%.2f",
        len(result.entries),
        result.confidence,
    )
    return result


async def process_followup(
    original_parts: list[types.Part],
    previous_result: ExtractionResult,
    user_reply: str,
) -> ExtractionResult:
    """Re-extract financial data after user provides corrections or answers.

    Args:
        original_parts: The Part objects from the original message (images, audio, text).
        previous_result: The previous extraction result shown to the user.
        user_reply: The user's follow-up message with corrections or answers.

    Returns:
        Updated ExtractionResult.
    """
    contents = [
        types.Content(
            role="user",
            parts=original_parts,
        ),
        types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f"Here is what I previously extracted:\n{previous_result.model_dump_json(indent=2)}"
                )
            ],
        ),
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_reply)],
        ),
    ]

    logger.info("Sending follow-up to Gemini for re-extraction")

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=FOLLOWUP_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ExtractionResult,
            temperature=0.2,
        ),
    )

    result = ExtractionResult.model_validate_json(response.text)
    logger.info(
        "Follow-up extraction: %d entries, confidence=%.2f",
        len(result.entries),
        result.confidence,
    )
    return result

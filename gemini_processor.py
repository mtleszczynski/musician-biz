"""Gemini API integration for financial data extraction, media transcription,
and field-level corrections.
"""

import asyncio
import io
import json
import logging
import time
from datetime import date

from google import genai
from google.genai import types
from google.genai.errors import APIError, ServerError
from PIL import Image

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

# Retry config for transient Gemini errors (503 overload, 429 rate limit)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds, doubled on each retry

# Hard cap on a single Gemini call (protects against silent hangs).
# A high-res image extraction usually completes in 5-15s; correction calls
# in 2-8s. 90s gives generous headroom while still failing fast on hangs.
GEMINI_CALL_TIMEOUT = 90.0

# Image resize target: max longest side in pixels. Gemini vision models
# perform well on receipts/checks at this resolution while cutting payload
# size dramatically (a 4K phone photo at 3MB drops to ~300-500KB).
IMAGE_MAX_DIMENSION = 1600
IMAGE_RESIZE_QUALITY = 85

client = genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def _resize_image_sync(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Downscale an image to IMAGE_MAX_DIMENSION on its longest side.

    Returns (new_bytes, new_mime_type). If the image is already small enough
    AND in a Gemini-friendly format, returns it unchanged. PNG/GIF/WebP get
    re-encoded as JPEG (with white background for transparency) to shrink
    further. On any failure, returns the original bytes unchanged.

    Memory profile: for a typical 4032x3024 phone JPEG (~4MB), decoding to
    full-resolution RGB needs ~37MB. We use Pillow's `draft()` to ask the
    JPEG decoder for a lower-resolution decode directly (libjpeg supports
    1/2, 1/4, 1/8 native scaling), which cuts decode memory by ~16x. The
    BytesIO buffer is closed explicitly so the kernel can reclaim it before
    we allocate the output JPEG buffer.
    """
    buf = None
    try:
        buf = io.BytesIO(image_bytes)
        img = Image.open(buf)

        # Ask the JPEG decoder for a smaller decode. No-op for non-JPEG formats.
        # Safe to call before any pixel access; only affects subsequent loads.
        try:
            img.draft("RGB", (IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION))
        except (AttributeError, Exception):
            pass  # draft() not supported by this codec; fine, we still resize below

        original_w, original_h = img.size
        longest = max(original_w, original_h)

        needs_resize = longest > IMAGE_MAX_DIMENSION
        needs_reencode = mime_type not in {"image/jpeg", "image/jpg"}

        if not needs_resize and not needs_reencode:
            return image_bytes, mime_type

        if needs_resize:
            img.thumbnail(
                (IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )

        # JPEG doesn't support transparency; flatten onto white if needed.
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=IMAGE_RESIZE_QUALITY, optimize=True)
        new_bytes = out.getvalue()
        out.close()
        logger.info(
            "op=resize | %dx%d %s (%d KB) -> %dx%d JPEG (%d KB)",
            original_w, original_h, mime_type, len(image_bytes) // 1024,
            img.size[0], img.size[1], len(new_bytes) // 1024,
        )
        return new_bytes, "image/jpeg"
    except Exception:
        logger.exception("op=resize | Failed, sending original bytes")
        return image_bytes, mime_type
    finally:
        if buf is not None:
            try:
                buf.close()
            except Exception:
                pass


async def resize_image(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Async wrapper for _resize_image_sync (Pillow is CPU-bound)."""
    return await asyncio.to_thread(_resize_image_sync, image_bytes, mime_type)


async def preprocess_images(
    images: list[tuple[bytes, str]] | None,
) -> list[tuple[bytes, str]] | None:
    """Resize all images upfront and return a new list with the smaller bytes.

    The caller should immediately replace its `images` reference with the
    return value (and clear/release the original list) so the large raw bytes
    can be garbage-collected before the rest of the pipeline runs. This
    significantly reduces peak memory pressure on the 256MB Fly VM: subsequent
    describe_image / extract_financial_data calls then operate on ~300KB
    images instead of multi-MB originals.
    """
    if not images:
        return images
    out: list[tuple[bytes, str]] = []
    for img_bytes, mime_type in images:
        new_bytes, new_mime = await resize_image(img_bytes, mime_type)
        out.append((new_bytes, new_mime))
    return out


def _build_date_context() -> str:
    """Return a short string telling the model today's date."""
    return f"Today's date is {date.today().isoformat()}."


async def _generate_with_retry(**kwargs) -> types.GenerateContentResponse:
    """Call Gemini's generate_content with retry for transient errors (503, 429).

    Each individual attempt is bounded by GEMINI_CALL_TIMEOUT to protect
    against silent hangs (which can otherwise leave the hourglass forever).
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await asyncio.wait_for(
                client.aio.models.generate_content(**kwargs),
                timeout=GEMINI_CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            if attempt == MAX_RETRIES - 1:
                logger.error(
                    "op=gemini_timeout | Gave up after %d attempts (timeout=%.0fs)",
                    MAX_RETRIES, GEMINI_CALL_TIMEOUT,
                )
                raise
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "op=gemini_timeout | Attempt %d/%d timed out after %.0fs, retrying in %.1fs",
                attempt + 1, MAX_RETRIES, GEMINI_CALL_TIMEOUT, delay,
            )
            await asyncio.sleep(delay)
        except (ServerError, APIError) as exc:
            status = getattr(exc, "status", None) or getattr(exc, "code", 0)
            retryable = isinstance(exc, ServerError) or status in (429, 503)
            if not retryable or attempt == MAX_RETRIES - 1:
                raise
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "op=gemini_retry | Attempt %d/%d failed (%s), retrying in %.1fs",
                attempt + 1, MAX_RETRIES, exc, delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Unreachable")


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

    response = await _generate_with_retry(
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
    for use in subsequent correction calls. Image is downscaled first to
    keep memory pressure low on small VMs.
    """
    t0 = time.monotonic()

    image_bytes, mime_type = await resize_image(image_bytes, mime_type)

    parts = [
        types.Part.from_text(text=IMAGE_DESCRIPTION_PROMPT),
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
    ]

    response = await _generate_with_retry(
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
        # Resize each image before sending. Resizes run sequentially to keep
        # peak memory bounded (concurrent resizes would defeat the purpose).
        for img_bytes, mime_type in images:
            resized_bytes, resized_mime = await resize_image(img_bytes, mime_type)
            parts.append(types.Part.from_bytes(
                data=resized_bytes, mime_type=resized_mime,
            ))

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

    response = await _generate_with_retry(
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

    response = await _generate_with_retry(
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

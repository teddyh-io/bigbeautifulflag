"""OpenAI vision wrapper for describing Truth Social media attachments.

When a truth has no caption but does have an image or video, we send the
image (or, for videos, the still ``preview_url`` thumbnail — we never
download the mp4 itself) to gpt-4o-mini and scroll the returned
description on the LED matrix in place of the missing body.

Configuration (env vars):

==================  ============================================
 OPENAI_API_KEY      Required to enable. Absent -> describe_media
                     returns None and the matrix shows the empty
                     "(no post)" fallback as before.
 OPENAI_MODEL        Defaults to ``gpt-4o-mini``.
==================  ============================================
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Literal

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"
MAX_OUTPUT_CHARS = 280

_client_lock = threading.Lock()
_client = None
_client_init_failed = False


def _get_client():
    """Lazily build a single OpenAI client.

    Returns ``None`` (and caches the failure) when ``OPENAI_API_KEY`` is
    missing or the ``openai`` SDK isn't installed, so the caller can fall
    through to the no-description path without crashing the service.
    """
    global _client, _client_init_failed
    with _client_lock:
        if _client is not None or _client_init_failed:
            return _client
        if not os.getenv("OPENAI_API_KEY"):
            log.info("vision: OPENAI_API_KEY not set, image description disabled")
            _client_init_failed = True
            return None
        try:
            from openai import OpenAI
            _client = OpenAI()
        except Exception as exc:
            log.warning("vision: OpenAI client init failed: %s", exc)
            _client_init_failed = True
            return None
        return _client


_PROMPT_IMAGE = """\
Describe this image from a Truth Social post for an LED ticker. ALWAYS
begin your output with the exact words "Image of".

Format guidance:
- Tweet/post screenshot ->
    Image of a tweet from @<author>: "<full text, verbatim>"
- Photo (with or without overlaid caption) ->
    Image of <subject> <what they're doing>[, with caption "<overlay>"]
    e.g. Image of Donald Trump saluting, with caption "The best is yet to come"
- News headline / article screenshot ->
    Image of a headline: "<headline text, verbatim>"
- Meme or other graphic ->
    Image of <brief description>[, with caption "<text>"]

Output plain ASCII only (no emojis, no smart quotes, no em dashes), no
preamble or trailing commentary, max 280 characters.
"""

_PROMPT_VIDEO = """\
Describe this video preview thumbnail from a Truth Social post for an LED
ticker. ALWAYS begin your output with the exact words "Video of".

Format guidance:
- Tweet/post screenshot ->
    Video of a tweet from @<author>: "<full text, verbatim>"
- Photo (with or without overlaid caption) ->
    Video of <subject> <what they're doing>[, with caption "<overlay>"]
    e.g. Video of Donald Trump saluting, with caption "The best is yet to come"
- News headline / article screenshot ->
    Video of a headline: "<headline text, verbatim>"
- Meme or other graphic ->
    Video of <brief description>[, with caption "<text>"]

If the thumbnail is a black screen, blank, or otherwise too uninformative
to describe, output exactly "[Video]" (with the brackets) and nothing else.

Output plain ASCII only (no emojis, no smart quotes, no em dashes), no
preamble or trailing commentary, max 280 characters.
"""


def describe_media(
    url: str,
    *,
    kind: Literal["image", "video"] = "image",
) -> str | None:
    """Return a one-line description of the media at ``url``, or ``None``.

    For ``kind="video"``, ``url`` should be the attachment's ``preview_url``
    (a still thumbnail); the prompt also lets the model fall back to the
    literal string ``"[Video]"`` when the thumbnail is uninformative.
    """
    client = _get_client()
    if client is None:
        return None
    if not url:
        return None

    prompt = _PROMPT_VIDEO if kind == "video" else _PROMPT_IMAGE
    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }],
            max_tokens=200,
            temperature=0.2,
        )
    except Exception as exc:
        log.warning("vision: describe_media(kind=%s) failed: %s", kind, exc)
        return None

    try:
        text = (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError) as exc:
        log.warning("vision: malformed response from %s: %s", model, exc)
        return None

    if not text:
        log.info("vision: empty response from %s for %s", model, kind)
        return None

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS]

    log.info("vision: described %s (%d chars): %s", kind, len(text), text)
    return text

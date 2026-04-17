"""RGB matrix renderer for Truth Social posts.

Renders the body of the latest post to a 64x32 HUB75 RGB matrix driven by a
Raspberry Pi 5 through the Adafruit Blinka Piomatter library, using the
`tom-thumb.bdf` 4x6 pixel font.  Short bodies are displayed statically.
Longer bodies are rendered to a tall offscreen image and scrolled vertically
with pauses at the top and bottom.
"""

from __future__ import annotations

import html
import logging
import re
import textwrap
import threading
import time
import unicodedata
from pathlib import Path

import numpy as np
from PIL import BdfFontFile, Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

FONT_PATH = Path(__file__).with_name("fonts") / "tom-thumb.bdf"

MATRIX_W = 64
MATRIX_H = 32
CHAR_W = 4        # tom-thumb cell width
LINE_H = 6        # tom-thumb cell height
TEXT_COLS = MATRIX_W // CHAR_W   # 16
BODY_COLOR = (255, 255, 255)
BG_COLOR = (0, 0, 0)

# Scroll tuning
SCROLL_STEP_MS = 80
PAUSE_MS = 1500
FRAME_SLEEP = 0.02  # 50 Hz render loop


def _load_font() -> ImageFont.ImageFont:
    """Load the tom-thumb BDF font as a PIL bitmap font.

    PIL cannot read .bdf directly at render time — BdfFontFile compiles it
    to a sidecar .pil/.pbm pair on first use, then ImageFont.load reads those.
    """
    if not FONT_PATH.exists():
        raise FileNotFoundError(
            f"Missing font: {FONT_PATH}. See README for how to fetch tom-thumb.bdf."
        )
    pil_path = FONT_PATH.with_suffix(".pil")
    if not pil_path.exists():
        with FONT_PATH.open("rb") as f:
            bdf = BdfFontFile.BdfFontFile(f)
        bdf.save(str(FONT_PATH.with_suffix("")))
    return ImageFont.load(str(pil_path))


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

# PIL's BdfFontFile loader only keeps the first 256 codepoints, so the
# tom-thumb font can only render Latin-1. Map common smart-punctuation
# and symbols to ASCII, then drop anything else (emojis, non-Latin scripts).
_SMART_PUNCT = str.maketrans({
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote / apostrophe
    "\u201A": ",",   # single low-9 quote
    "\u201B": "'",
    "\u201C": '"',   # left double quote
    "\u201D": '"',   # right double quote
    "\u201E": '"',   # double low-9 quote
    "\u2032": "'",   # prime
    "\u2033": '"',   # double prime
    "\u2013": "-",   # en dash
    "\u2014": "-",   # em dash
    "\u2015": "-",   # horizontal bar
    "\u2026": "...", # ellipsis
    "\u00A0": " ",   # non-breaking space
    "\u2022": "*",   # bullet
    "\u00B7": "*",   # middle dot
    "\u00AB": '"',
    "\u00BB": '"',
})


def _sanitize(text: str) -> str:
    """Reduce text to what the tom-thumb PIL bitmap font can actually render.

    Smart punctuation is mapped to ASCII, accented letters are stripped to
    their base form, and anything still outside printable ASCII (emojis,
    CJK, etc.) is dropped.
    """
    text = text.translate(_SMART_PUNCT)
    text = unicodedata.normalize("NFKD", text)
    return "".join(
        ch for ch in text
        if ch == "\n" or ch == " " or (ch.isprintable() and ord(ch) < 128)
    )


def clean_body(raw: str) -> str:
    """Strip HTML tags and collapse whitespace from a Truth Social post body."""
    if not raw:
        return ""
    # Preserve paragraph breaks as newlines.
    text = raw.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n")
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _sanitize(text)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def wrap_body(text: str, cols: int = TEXT_COLS) -> list[str]:
    """Word-wrap text to `cols` characters per line, preserving paragraph breaks."""
    wrapped: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(paragraph, width=cols) or [""])
    return wrapped


def render_body_image(text: str, font: ImageFont.ImageFont) -> Image.Image:
    """Render cleaned post body into a 64 x H image (H >= MATRIX_H)."""
    lines = wrap_body(text) if text else ["(no post)"]
    height = max(MATRIX_H, len(lines) * LINE_H)
    img = Image.new("RGB", (MATRIX_W, height), BG_COLOR)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((0, i * LINE_H), line, font=font, fill=BODY_COLOR)
    return img


class MatrixScroller:
    """Owns the Piomatter handle and continuously renders the latest body."""

    def __init__(self) -> None:
        self._font = _load_font()
        self._lock = threading.Lock()
        self._image: Image.Image = render_body_image("", self._font)
        self._dirty = True
        self._stop = threading.Event()
        self._matrix, self._framebuffer = self._init_matrix()

    @staticmethod
    def _init_matrix():
        """Initialise Piomatter for a single 64x32 HUB75 panel.

        Imported lazily so that the rest of the code (and unit tests) can be
        exercised on non-Pi hardware without the library installed.
        """
        import adafruit_blinka_raspberry_pi5_piomatter as piomatter

        geometry = piomatter.Geometry(
            width=MATRIX_W,
            height=MATRIX_H,
            n_addr_lines=4,
            rotation=piomatter.Orientation.Normal,
        )
        framebuffer = np.zeros((MATRIX_H, MATRIX_W, 3), dtype=np.uint8)
        matrix = piomatter.PioMatter(
            colorspace=piomatter.Colorspace.RGB888Packed,
            pinout=piomatter.Pinout.AdafruitMatrixBonnet,
            framebuffer=framebuffer,
            geometry=geometry,
        )
        return matrix, framebuffer

    def set_body(self, raw_body: str) -> None:
        """Update the text shown on the matrix (HTML-cleaned + re-wrapped)."""
        cleaned = clean_body(raw_body)
        img = render_body_image(cleaned, self._font)
        with self._lock:
            self._image = img
            self._dirty = True
        log.info(
            "matrix: new body (%d chars → %dpx tall)",
            len(cleaned),
            img.height,
        )

    def clear(self) -> None:
        self._framebuffer[:] = 0
        self._matrix.show()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Main render loop. Intended to be the thread target.

        State machine: PAUSE_TOP -> SCROLLING -> PAUSE_BOTTOM -> PAUSE_TOP ...
        When the current image is shorter than the panel we just stay in
        PAUSE_TOP forever and only repaint when the text changes.
        """
        PAUSE_TOP, SCROLLING, PAUSE_BOTTOM = 0, 1, 2
        state = PAUSE_TOP
        y_offset = 0
        last_step = 0.0
        pause_until = time.monotonic() + PAUSE_MS / 1000.0

        while not self._stop.is_set():
            now = time.monotonic()

            with self._lock:
                img = self._image
                if self._dirty:
                    state = PAUSE_TOP
                    y_offset = 0
                    pause_until = now + PAUSE_MS / 1000.0
                    self._dirty = False

            max_offset = max(0, img.height - MATRIX_H)

            if max_offset == 0:
                window = img
            else:
                window = img.crop((0, y_offset, MATRIX_W, y_offset + MATRIX_H))

            self._framebuffer[:] = np.asarray(window, dtype=np.uint8)
            self._matrix.show()

            if max_offset > 0:
                if state == PAUSE_TOP and now >= pause_until:
                    state = SCROLLING
                    last_step = now
                elif state == SCROLLING and now - last_step >= SCROLL_STEP_MS / 1000.0:
                    last_step = now
                    y_offset += 1
                    if y_offset >= max_offset:
                        y_offset = max_offset
                        state = PAUSE_BOTTOM
                        pause_until = now + PAUSE_MS / 1000.0
                elif state == PAUSE_BOTTOM and now >= pause_until:
                    y_offset = 0
                    state = PAUSE_TOP
                    pause_until = now + PAUSE_MS / 1000.0

            time.sleep(FRAME_SLEEP)

        self.clear()

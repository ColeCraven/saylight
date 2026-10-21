"""What OCR finds, and what a reading from the screen remembers about where its words are.

Everything here lives in memory only and is never saved or logged. A ScreenText keeps word boxes, a tiny
thumbnail hash and one lightness flag: never pixels and never text.

Qt types appear only in annotations, so this module, and the document model built on it, import without Qt,
as the file readers do. Nothing here imports winrt; only winrt_support.py does, on first use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QRect, QRectF

# Why OcrWorker couldn't read an image: no OCR language pack, an image larger than Windows accepts, or anything
# else (the log has the exception's class name, never text).
OCR_FAILURES = ("no_language", "too_large", "error")
# The card notices a failed reading from the screen or a scan shows (the engine's "ocr_notice" overlay state).
SCREEN_NOTICES = ("screen_nothing", "screen_too_large", "screen_language", "screen_error")
SCAN_NOTICES = ("scan_nothing", "scan_language", "scan_error")
OCR_NOTICE_KINDS = SCREEN_NOTICES + SCAN_NOTICES


@dataclass(frozen=True)
class OcrWord:
    """One recognised word. `rect` is in global logical px for the screen, or page px for a scanned PDF."""
    text: str
    rect: QRectF


@dataclass(frozen=True)
class OcrLine:
    words: tuple[OcrWord, ...]


@dataclass(frozen=True)
class OcrResult:
    """Windows' lines in its reading order, the text angle in degrees, and the BCP-47 tag of the OCR language."""
    lines: tuple[OcrLine, ...]
    angle: float
    language: str


@dataclass(frozen=True)
class OcrText:
    """Paragraph text built from an OcrResult (paragraphs separated by blank lines), plus its words in text
    order. Like a str or a LoadedDocument, it is a RawInput a reading is built from."""
    text: str
    words: tuple[OcrWord, ...]


@dataclass(frozen=True)
class ScreenText:
    """Where a reading from the screen came from, for the on-screen highlight.

    `boxes` maps each sentence to its words as (lo, hi, rect): character offsets in the sentence, as in
    Engine.position, and the word's box in global logical px. `thumb` is the region's tiny hash at capture time,
    compared once a second to notice the page moving. `light` says the capture's mean luminance was 0.5 or more,
    worked out before the image was dropped."""
    screen: str                                      # QScreen.name() of the screen the area was on
    region: QRect                                    # the captured area, global logical px
    boxes: dict[int, list[tuple[int, int, QRectF]]]
    thumb: bytes
    light: bool = True

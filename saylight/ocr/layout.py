"""From OCR lines to paragraphs, and from a built reading back to word boxes.

Paragraphs come from geometry alone: a break where the next line jumps back up (a new column), where the line
step is more than 1.45 times the usual pitch, or at an indent. Windows already returns the lines one column at a
time, in reading order.
"""

from __future__ import annotations

import bisect
import difflib
import re
import statistics
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF

from .model import OcrLine, OcrResult, OcrText

if TYPE_CHECKING:
    from ..text_model import Document

GAP_PITCH = 1.45      # a line step more than this x the usual pitch starts a paragraph
INDENT_HEIGHT = 1.5   # a line starting this many line heights right of the one before starts a paragraph

_TOKEN_RE = re.compile(r"\S+")
_NON_WORD_RE = re.compile(r"[^\w]+")


def _box(line: OcrLine) -> tuple[float, float, float, float]:
    left = min(w.rect.left() for w in line.words)
    top = min(w.rect.top() for w in line.words)
    right = max(w.rect.right() for w in line.words)
    bottom = max(w.rect.bottom() for w in line.words)
    return left, top, right, bottom


def paragraphs(result: OcrResult) -> list[list[OcrLine]]:
    """The result's lines grouped into paragraphs, in Windows' reading order."""
    lines = [line for line in result.lines if line.words]
    if not lines:
        return []
    boxes = [_box(line) for line in lines]
    height = statistics.median(b[3] - b[1] for b in boxes) or 1.0
    steps = [b[1] - a[1] for a, b in zip(boxes, boxes[1:]) if b[1] > a[1]]
    pitch = statistics.median(steps) if steps else height * 1.3
    out: list[list[OcrLine]] = [[lines[0]]]
    for prev, box, line in zip(boxes, boxes[1:], lines[1:]):
        new_column = box[1] < prev[1] - height
        gap = box[1] - prev[1] > GAP_PITCH * pitch
        indent = not new_column and box[0] - prev[0] > INDENT_HEIGHT * height
        if new_column or gap or indent:
            out.append([])
        out[-1].append(line)
    return out


def to_text(result: OcrResult) -> OcrText:
    """The result's text in paragraphs, with its words in text order."""
    paras = paragraphs(result)
    text = "\n\n".join("\n".join(" ".join(w.text for w in line.words) for line in para) for para in paras)
    words = tuple(w for para in paras for line in para for w in line.words)
    return OcrText(text, words)


def _key(token: str) -> str:
    return _NON_WORD_RE.sub("", token.casefold())


def _trim(text: str, a: int, b: int) -> tuple[int, int]:
    """A token without the punctuation around it ("(Smith," -> "Smith"), unless that is all it is."""
    lo, hi = a, b
    while lo < hi and not text[lo].isalnum():
        lo += 1
    while hi > lo and not text[hi - 1].isalnum():
        hi -= 1
    return (lo, hi) if lo < hi else (a, b)


def map_boxes(doc: Document, text: OcrText) -> dict[int, list[tuple[int, int, QRectF]]]:
    """Each sentence's words as (lo, hi, rect): offsets in the sentence and the word's box.

    The reading's shown words are aligned with the OCR words by their letters and digits, so the cleanup the
    text goes through (a citation dimmed, a hyphen joined) moves no box. A misread word keeps its box when the
    words around it agree, and a word split over two lines ("exam-" "ple") gets both boxes."""
    words = text.words
    sentences = doc.sentences
    if not words or not sentences:
        return {}
    tokens = [m.span() for m in _TOKEN_RE.finditer(doc.text)]
    a = [_key(doc.text[s:e]) for s, e in tokens]
    b = [_key(w.text) for w in words]
    match: list[list[int]] = [[] for _ in tokens]
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                match[i1 + k].append(j1 + k)
        elif op == "replace" and i2 - i1 == 1 and a[i1] and a[i1] == "".join(b[j1:j2]):
            match[i1].extend(range(j1, j2))
    starts = [s.start for s in sentences]
    out: dict[int, list[tuple[int, int, QRectF]]] = {}
    for (s, e), found in zip(tokens, match):
        if not found:
            continue
        k = bisect.bisect_right(starts, s) - 1
        if k < 0 or s >= sentences[k].end:
            continue
        sent = sentences[k]
        lo, hi = _trim(doc.text, s, min(e, sent.end))
        for j in found:
            out.setdefault(k, []).append((lo - sent.start, hi - sent.start, QRectF(words[j].rect)))
    return out

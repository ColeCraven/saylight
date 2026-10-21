"""Grabbing an area of one screen, in memory only.

Nothing captured is ever saved or logged: the image goes to OCR and is dropped, and thumb() keeps only a coarse
grid of grey levels (at most 24 x 24 cells over at most 640 x 400 px, far too coarse to read) for noticing that
the page has moved.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QScreen

THUMB_CELLS = 24          # the change check's grid is at most this many cells a side
CHECK_MAX = (640, 400)    # logical px; a larger area is checked in its middle
CHANGED_LEVEL = 12        # a cell differs when its mean grey moved by more than this (0-255)
CHANGED_SHARE = 0.05      # and the page has moved when more than this share of the compared cells differ
LIGHT_SAMPLE = 64         # is_light() averages a smooth downscale at most this many px a side


def grab(screen: QScreen, rect: QRect) -> QImage:
    """The area `rect` (global logical px) of `screen`, grabbed relative to that screen.

    QScreen.grabWindow(0, ...) takes coordinates relative to the screen it is called on; global ones grab the
    wrong area on every screen but the primary. The image is in device px with its device pixel ratio set."""
    g = screen.geometry()
    return screen.grabWindow(0, rect.x() - g.x(), rect.y() - g.y(), rect.width(), rect.height()).toImage()


def check_rect(rect: QRect) -> QRect:
    """The part of `rect` the change check compares: its middle, at most CHECK_MAX. Offsets and sizes are
    multiples of 4 logical px, which are whole device px at every scale Windows offers (100% to 500% in 25%
    steps), so two grabs of it line up exactly."""
    w = min(rect.width(), CHECK_MAX[0]) // 4 * 4
    h = min(rect.height(), CHECK_MAX[1]) // 4 * 4
    dx = (rect.width() - w) // 2 // 4 * 4
    dy = (rect.height() - h) // 2 // 4 * 4
    return QRect(rect.x() + dx, rect.y() + dy, max(4, w), max(4, h))


def grid_size(width: float, height: float) -> tuple[int, int]:
    """Columns and rows for an area: square-ish cells, at most THUMB_CELLS a side."""
    if width <= 0 or height <= 0:
        return 1, 1
    if width >= height:
        return THUMB_CELLS, max(1, min(THUMB_CELLS, round(THUMB_CELLS * height / width)))
    return max(1, min(THUMB_CELLS, round(THUMB_CELLS * width / height))), THUMB_CELLS


def _lumas(image: QImage, cols: int, rows: int) -> list[float]:
    """Mean luma (Rec. 709 on the stored values, 0-255) of each cell of a cols x rows smooth downscale."""
    small = image.scaled(cols, rows, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    small = small.convertToFormat(QImage.Format.Format_RGB32)
    bits = small.constBits()
    data = bits.tobytes() if hasattr(bits, "tobytes") else bytes(bits)
    stride = small.bytesPerLine()
    out = []
    for y in range(rows):
        row = y * stride
        for x in range(cols):
            b, g, r = data[row + 4 * x], data[row + 4 * x + 1], data[row + 4 * x + 2]  # little-endian 0xAARRGGBB
            out.append(0.2126 * r + 0.7152 * g + 0.0722 * b)
    return out


def thumb_of(image: QImage) -> bytes:
    """The coarse grey grid of an image: two bytes (columns, rows), then one byte per cell, row by row."""
    if image.isNull() or image.width() < 1 or image.height() < 1:
        return b""
    cols, rows = grid_size(image.width(), image.height())
    return bytes((cols, rows)) + bytes(min(255, int(v + 0.5)) for v in _lumas(image, cols, rows))


def thumb(screen: QScreen, rect: QRect) -> bytes:
    """A tiny in-memory signature of the area (its check_rect), to compare with a later one using changed()."""
    return thumb_of(grab(screen, check_rect(rect)))


def cells_under(area: QRect, thumb_bytes: bytes, rects: Iterable[QRect]) -> frozenset[int]:
    """The cells of a thumb of `area` (its check_rect) that any of `rects` (global logical px) touches: what
    Saylight's own windows cover, left out of the comparison."""
    if len(thumb_bytes) < 2:
        return frozenset()
    cols, rows = thumb_bytes[0], thumb_bytes[1]
    checked = check_rect(area)
    cw, ch = checked.width() / cols, checked.height() / rows
    out = set()
    for r in rects:
        hit = r.intersected(checked)
        if hit.isEmpty():
            continue
        c0 = int((hit.left() - checked.left()) // cw)
        c1 = int((hit.left() + hit.width() - 1 - checked.left()) // cw)
        r0 = int((hit.top() - checked.top()) // ch)
        r1 = int((hit.top() + hit.height() - 1 - checked.top()) // ch)
        for row in range(max(0, r0), min(rows - 1, r1) + 1):
            for col in range(max(0, c0), min(cols - 1, c1) + 1):
                out.add(row * cols + col)
    return frozenset(out)


def changed(before: bytes, after: bytes, *, skip: frozenset[int] = frozenset()) -> bool:
    """Whether the page under the area has moved: more than CHANGED_SHARE of the cells (other than `skip`)
    changed by more than CHANGED_LEVEL. A blinking caret or a redrawn button stays well under that; a scroll of
    one line of text doesn't."""
    if len(before) < 2 or len(before) != len(after) or before[:2] != after[:2]:
        return True
    cells = [i for i in range(len(before) - 2) if i not in skip]
    if not cells:
        return False
    differ = sum(1 for i in cells if abs(before[2 + i] - after[2 + i]) > CHANGED_LEVEL)
    return differ > CHANGED_SHARE * len(cells)


def is_light(image: QImage) -> bool:
    """True if the image's mean luma is 0.5 or more (ScreenText.light): dark ink on a light page, as far as the
    on-screen marks are concerned."""
    if image.isNull() or image.width() < 1 or image.height() < 1:
        return True
    cols = min(LIGHT_SAMPLE, image.width())
    rows = min(LIGHT_SAMPLE, image.height())
    values = _lumas(image, cols, rows)
    return sum(values) / len(values) >= 127.5

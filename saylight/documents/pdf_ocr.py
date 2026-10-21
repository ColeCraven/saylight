"""Reading scanned PDF pages with OCR, one page at a time, entirely in memory.

Pages are rendered with Windows.Data.Pdf at about 220 dpi effective and read on the loader's thread. Nothing
rendered or recognised is written to disk or logged.

Windows.Data.Pdf multiplies the width it is asked for by the primary display's scale (1000 px comes back as
1250 at 125%), so each document measures that scale once with a tiny render and asks for less.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

from ..ocr.model import OcrText
from .pdf import Cancelled

log = logging.getLogger("saylight.documents")

TARGET_DPI = 220           # the render resolution OCR reads pages at
LOW_DPI = 150              # scans coarser than this misread enough words for the reading to say so
SECONDS_PER_PAGE = 0.8     # the first guess at how long one page takes, before any page is timed
PROBE_PX = 200             # the width of the render that measures Windows' scale
MAX_SIDE = 10_000          # OCR's largest image side


def estimate_s(scanned: int) -> int:
    """About how many seconds finding the text in `scanned` pages takes."""
    return math.ceil(max(0, scanned) * SECONDS_PER_PAGE)


class ScanPdf:
    """A PDF opened with Windows.Data.Pdf from bytes in memory, for rendering its pages one at a time.

    The password, if any, opens the document here and is not kept. Worker threads only: rendering blocks."""

    def __init__(self, data: bytes, password: str | None = None) -> None:
        from .. import winrt_support
        streams = winrt_support.load("storage.streams")
        pdf = winrt_support.load("data.pdf")
        self._imaging = winrt_support.load("graphics.imaging")
        self._pdf = pdf
        self._streams = streams
        stream = streams.InMemoryRandomAccessStream()
        writer = streams.DataWriter(stream)
        writer.write_bytes(data)
        writer.store_async().get()
        writer.flush_async().get()
        writer.detach_stream()
        stream.seek(0)
        if password:
            self.doc = pdf.PdfDocument.load_from_stream_with_password_async(stream, password).get()
        else:
            self.doc = pdf.PdfDocument.load_from_stream_async(stream).get()
        self._stream = stream
        self.pages: int = int(self.doc.page_count)
        self.dpi: dict[int, int] = {}   # page -> the resolution it was rendered at
        self._scale: float | None = None

    def _render(self, page, width: int):
        imaging = self._imaging
        options = self._pdf.PdfPageRenderOptions()
        options.destination_width = max(1, int(width))
        options.bitmap_encoder_id = imaging.BitmapEncoder.bmp_encoder_id  # much faster than PNG
        out = self._streams.InMemoryRandomAccessStream()
        try:
            page.render_with_options_to_stream_async(out, options).get()
            out.seek(0)
            bitmap = imaging.BitmapDecoder.create_async(out).get().get_software_bitmap_async().get()
        finally:
            out.close()
        if bitmap.bitmap_pixel_format not in (imaging.BitmapPixelFormat.BGRA8, imaging.BitmapPixelFormat.GRAY8):
            bitmap = imaging.SoftwareBitmap.convert(bitmap, imaging.BitmapPixelFormat.BGRA8)
        return bitmap

    def display_scale(self, page) -> float:
        """How much bigger than asked Windows renders (the primary display's scale), measured once."""
        if self._scale is None:
            probe = self._render(page, PROBE_PX)
            got = probe.pixel_width
            probe.close()
            self._scale = got / PROBE_PX if got > 0 else 1.0
        return self._scale

    def render(self, number: int, dpi: int = TARGET_DPI):
        """Page `number` (1-based) as a SoftwareBitmap at about `dpi`, never more than OCR's largest side."""
        page = self.doc.get_page(number - 1)
        try:
            size = page.size  # DIP: 1/96 inch
            longest_in = max(size.width, size.height, 1.0) / 96
            dpi = min(float(dpi), (MAX_SIDE - 8) / longest_in)
            width = size.width / 96 * dpi / self.display_scale(page)
            bitmap = self._render(page, round(width))
        finally:
            page.close()
        side = max(bitmap.pixel_width, bitmap.pixel_height)
        self.dpi[number] = round(side / longest_in)
        return bitmap


def ocr_pages(data: bytes | ScanPdf, pages: list[int], *, progress: Callable[[int, int], None],
              cancelled: Callable[[], bool], target_dpi: int = TARGET_DPI,
              password: str | None = None) -> list[OcrText]:
    """The text of each of `pages` (1-based), in order. Raises Cancelled when `cancelled()` turns true, OcrError
    when OCR can't run. `data` is the file's bytes, or a ScanPdf already open (then `password` isn't needed)."""
    from ..ocr.engine import recognize_bitmap
    from ..ocr.layout import to_text
    doc = data if isinstance(data, ScanPdf) else ScanPdf(data, password)
    password = None
    out: list[OcrText] = []
    for i, number in enumerate(pages):
        if cancelled():
            raise Cancelled()
        bitmap = doc.render(number, target_dpi)
        try:
            if cancelled():
                raise Cancelled()
            out.append(to_text(recognize_bitmap(bitmap)))
        finally:
            bitmap.close()
        progress(i + 1, len(pages))
    return out


# --- How finely a page was scanned -------------------------------------------------------------

def _mult(m: list[float], n: list[float]) -> list[float]:
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return [a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F]


def page_dpi(page) -> int:
    """The resolution the largest picture on a pypdf page is drawn at, in dots per inch; 0 without one."""
    try:
        from pypdf.generic import ContentStream
        xobjects = page["/Resources"].get_object().get("/XObject")
        if xobjects is None:
            return 0
        sizes = {}
        for name, ref in xobjects.get_object().items():
            x = ref.get_object()
            if x.get("/Subtype") == "/Image":
                sizes[name] = (int(x["/Width"]), int(x["/Height"]))
        contents = page.get_contents()
        if not sizes or contents is None:
            return 0
        ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        stack: list[list[float]] = []
        best_area, dpi = 0.0, 0.0
        for operands, op in ContentStream(contents, page.pdf).operations:
            if op == b"q":
                stack.append(ctm)
            elif op == b"Q":
                ctm = stack.pop() if stack else [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            elif op == b"cm" and len(operands) == 6:
                ctm = _mult([float(v) for v in operands], ctm)
            elif op == b"Do" and operands and operands[0] in sizes:
                w_in = math.hypot(ctm[0], ctm[1]) / 72
                h_in = math.hypot(ctm[2], ctm[3]) / 72
                area = w_in * h_in
                if area > best_area and w_in > 0 and h_in > 0:
                    px_w, px_h = sizes[operands[0]]
                    best_area, dpi = area, min(px_w / w_in, px_h / h_in)
        return round(dpi)
    except Exception:  # an odd page: no estimate rather than a failed opening
        return 0

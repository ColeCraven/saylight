"""Windows OCR on one worker thread.

One MTA thread owns one OcrEngine (TryCreateFromUserProfileLanguages): engines shared between threads fail.
Images are upscaled smoothly (2x, capped at 10,000 px a side) and handed to WinRT as BGRA8 bitmaps in memory;
nothing is written to disk, and an image is dropped as soon as its result is out. Logs give sizes, counts and
timings, never text.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal, Slot
from PySide6.QtGui import QImage

from .. import winrt_support
from .model import OCR_FAILURES, OcrLine, OcrResult, OcrWord

log = logging.getLogger("saylight.ocr")

MAX_SIDE = 10_000           # OcrEngine.MaxImageDimension: Windows refuses anything larger
LANGUAGE_WAIT_S = 2.0       # languages() waits this long for the worker's first query


class OcrError(Exception):
    """OCR couldn't read an image; `kind` is one of OCR_FAILURES."""

    def __init__(self, kind: str) -> None:
        if kind not in OCR_FAILURES:
            raise ValueError(f"unknown OCR failure: {kind}")
        super().__init__(kind)
        self.kind = kind


def upscale_for(width: int, height: int, upscale: float) -> float:
    """The scale to recognise a width x height image at: `upscale`, or less to stay within MAX_SIDE, but never
    below native size (or below `upscale` when that is under 1). OcrError("too_large") when it can't fit."""
    longest = max(width, height)
    if longest <= 0:
        return upscale
    cap = MAX_SIDE / longest
    if upscale <= cap:
        return upscale
    if cap >= min(1.0, upscale):
        return cap
    raise OcrError("too_large")


# --- Engines: one per thread -------------------------------------------------------------------

_generation = 0   # bumped by refresh_languages(): every thread makes its engine again on its next image
_local = threading.local()


def _new_engine():
    ocr = winrt_support.load("media.ocr")
    return ocr.OcrEngine.try_create_from_user_profile_languages()


def user_languages() -> list[str]:
    """The OCR languages Windows has for this person, the one OCR will use first; empty when none of their
    languages has an OCR pack. Asks Windows now (a few ms); safe on any thread."""
    engine = _new_engine()
    if engine is None:
        return []
    first = engine.recognizer_language.language_tag
    ocr = winrt_support.load("media.ocr")
    return [first] + [lang.language_tag for lang in ocr.OcrEngine.available_recognizer_languages
                      if lang.language_tag != first]


def _thread_engine():
    """This thread's engine, made on first use (and again after a language refresh, or while there is none)."""
    if threading.current_thread() is threading.main_thread():
        raise RuntimeError("OCR blocks: run it on a worker thread")
    engine = getattr(_local, "engine", None)
    if engine is None or getattr(_local, "generation", -1) != _generation:
        engine = _local.engine = _new_engine()
        _local.generation = _generation
    if engine is None:
        raise OcrError("no_language")
    return engine


def _to_bitmap(image: QImage, scale: float):
    imaging = winrt_support.load("graphics.imaging")
    img = image.convertToFormat(QImage.Format.Format_RGB32)  # bytes B, G, R, 255; rows are always packed
    if abs(scale - 1.0) > 1e-6:
        img = img.scaled(max(1, round(img.width() * scale)), max(1, round(img.height() * scale)),
                         Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return imaging.SoftwareBitmap.create_copy_with_alpha_from_buffer(
        img.constBits(), imaging.BitmapPixelFormat.BGRA8, img.width(), img.height(),
        imaging.BitmapAlphaMode.IGNORE)


def _result(res, language: str, origin: QPointF, factor: float) -> OcrResult:
    """WinRT's result as model types: a box at image px p ends up at origin + p / factor."""
    ox, oy = origin.x(), origin.y()
    lines = []
    for line in res.lines:
        words = []
        for w in line.words:
            r = w.bounding_rect
            words.append(OcrWord(w.text, QRectF(ox + r.x / factor, oy + r.y / factor, r.width / factor,
                                                r.height / factor)))
        if words:
            lines.append(OcrLine(tuple(words)))
    angle = res.text_angle
    return OcrResult(tuple(lines), float(angle) if angle is not None else 0.0, language)


def recognize_bitmap(bitmap, *, origin: QPointF = QPointF(0, 0), factor: float = 1.0) -> OcrResult:
    """OCR of a WinRT SoftwareBitmap (BGRA8 or Gray8) on the calling worker thread, with that thread's engine;
    boxes at origin + bitmap px / factor."""
    if max(bitmap.pixel_width, bitmap.pixel_height) > MAX_SIDE:
        raise OcrError("too_large")
    engine = _thread_engine()
    res = engine.recognize_async(bitmap).get()
    return _result(res, engine.recognizer_language.language_tag, origin, factor)


def _recognize(image: QImage, origin: QPointF, dpr: float, upscale: float) -> OcrResult:
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        return OcrResult((), 0.0, "")
    scale = upscale_for(image.width(), image.height(), upscale)
    engine = _thread_engine()  # before converting: no language means no work
    bitmap = _to_bitmap(image, scale)
    try:
        res = engine.recognize_async(bitmap).get()
    finally:
        bitmap.close()
    return _result(res, engine.recognizer_language.language_tag, origin, scale * (dpr or 1.0))


def recognize_blocking(image: QImage, *, upscale: float = 2.0) -> OcrResult:
    """OCR on the calling thread, with that thread's own engine; worker threads only (pdf_ocr uses it). Boxes
    are in `image` px. Raises OcrError."""
    return _recognize(image, QPointF(0, 0), 1.0, upscale)


# --- The worker --------------------------------------------------------------------------------

class OcrWorker(QObject):
    """Recognises images queued from the GUI thread; results come back queued, tagged with the caller's token."""

    done = Signal(int, object)     # token, OcrResult
    failed = Signal(int, str)      # token, one of OCR_FAILURES

    _relay_done = Signal(int, object)
    _relay_failed = Signal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._langs: list[str] = []
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        queued = Qt.ConnectionType.QueuedConnection
        self._relay_done.connect(self._on_done, queued)
        self._relay_failed.connect(self._on_failed, queued)

    def start(self) -> None:
        """Start the thread and create the engine; the OCR languages are read once here."""
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(target=self._run, name="saylight.ocr", daemon=True)
            self._thread.start()

    def recognize(self, token: int, image: QImage, *, origin: QPointF, dpr: float, upscale: float = 2.0) -> None:
        """Queue `image` (device px). Word boxes come back in global logical px: origin + px / dpr."""
        self.start()
        self._jobs.put((token, image, QPointF(origin), float(dpr), float(upscale)))

    def languages(self) -> list[str]:
        """The OCR language tags found at start(); empty when Windows has no OCR language pack."""
        self.start()
        self._ready.wait(LANGUAGE_WAIT_S)
        with self._lock:
            return list(self._langs)

    def refresh_languages(self) -> list[str]:
        """Ask Windows again (after the person adds a language) and return the new list."""
        global _generation
        try:
            langs = user_languages()
        except Exception as e:  # winrt missing or refused: the same as no language
            log.warning("OCR languages unavailable (%s)", type(e).__name__)
            langs = []
        with self._lock:
            self._langs = langs
            _generation += 1
        self._ready.set()
        log.info("OCR languages: %d", len(langs))
        return list(langs)

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._closed = True
            thread = self._thread
        self._jobs.put(None)
        if thread is not None:
            thread.join(timeout)

    @Slot(int, object)
    def _on_done(self, token: int, result: object) -> None:
        if not self._closed:
            self.done.emit(token, result)

    @Slot(int, str)
    def _on_failed(self, token: int, kind: str) -> None:
        if not self._closed:
            self.failed.emit(token, kind)

    def _run(self) -> None:
        try:
            langs = user_languages()
        except Exception as e:
            log.warning("OCR languages unavailable (%s)", type(e).__name__)
            langs = []
        with self._lock:
            if not self._ready.is_set():  # a refresh on the GUI thread got there first: keep its answer
                self._langs = langs
        self._ready.set()
        log.info("OCR ready: %d languages", len(langs))
        while True:
            item = self._jobs.get()
            if item is None:
                return
            token, image, origin, dpr, upscale = item
            item = None
            t0 = time.perf_counter()
            try:
                result = _recognize(image, origin, dpr, upscale)
            except OcrError as e:
                log.info("OCR failed: %s", e.kind)
                self._relay_failed.emit(token, e.kind)
            except Exception as e:  # never let one image stop the worker
                log.warning("OCR failed: %s", type(e).__name__)
                self._relay_failed.emit(token, "error")
            else:
                log.info("OCR: %dx%d px, %d lines, %.0f ms", image.width(), image.height(), len(result.lines),
                         (time.perf_counter() - t0) * 1000)
                self._relay_done.emit(token, result)
            finally:
                image = None

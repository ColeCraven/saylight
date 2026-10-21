"""Reading from the screen, start to finish: language check, selection, capture, OCR and the reading.

The capture stays in memory and is dropped after OCR. Logs give counts and timings, never text.

The App pauses the reading and covers the panel, then calls begin(). ScreenReader opens the overlay and, when an
input thread is given, gates the shortcuts and registers Esc for as long as it is open (when another app holds Esc,
that app gets every Esc press, so the pill says right-click cancels instead). Releasing the mouse grabs the area
with the overlay still up (it is left out of captures), closes the overlay, keeps the ring up as a frame and queues
the OCR. The result is built into a Document whose source carries the word boxes for the on-screen marks, and
handed over with textReady.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QPointF, QRect, QTimer, Signal

from . import screen_capture, win32_overlay
from .documents.model import DocSource
from .ocr import layout
from .ocr.model import OcrResult, ScreenText
from .ui.region_overlay import OverlayManager
from .ui.screen_highlight import ScreenHighlight

log = logging.getLogger("saylight.screen_reader")

FAILURES = ("no_language", "nothing_found", "cancelled", "error", "too_large", "nothing_speakable")
# The engine's card notice for each failure ("cancelled" shows none). Nothing speakable shows the no-text notice
# with its own first line.
NOTICE_FOR = {"no_language": "screen_language", "nothing_found": "screen_nothing",
              "nothing_speakable": "screen_nothing", "too_large": "screen_too_large", "error": "screen_error"}
UNHIDE_MS = 40           # where capture exclusion is unavailable: hide the overlay this long before the grab
OCR_TIMEOUT_MS = 30_000  # a recognition that never answers is reported as an error
UPSCALE = 2.0


class ScreenReader(QObject):
    stateChanged = Signal(str)          # "idle" | "selecting" | "recognizing"
    textReady = Signal(object, object)  # Document, OcrText
    # "no_language" | "nothing_found" | "cancelled" | "error" | "too_large" | "nothing_speakable"
    failed = Signal(str)

    def __init__(self, theme, settings, ocr, *, input_thread=None, build: Callable[[str], object] | None = None,
                 overlay: OverlayManager | None = None, highlight: ScreenHighlight | None = None,
                 parent: QObject | None = None) -> None:
        """`ocr` is the OcrWorker. `build` makes a Document from the recognised text (the App's build_document,
        which applies skip citations and the pronunciation list); plain Document.from_raw by default."""
        super().__init__(parent)
        self.theme = theme
        self.settings = settings
        self.ocr = ocr
        self.input = input_thread
        self.overlay = overlay or OverlayManager(theme, self)
        self.highlight = highlight or ScreenHighlight(theme, settings, self)
        self._build = build or _default_build
        self._state = "idle"
        self._return_to = 0
        self._token = 0
        self._job: dict | None = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self.overlay.selected.connect(self._on_selected)
        self.overlay.cancelled.connect(self._on_overlay_cancelled)
        ocr.done.connect(self._on_done)
        ocr.failed.connect(self._on_failed)
        if input_thread is not None:
            input_thread.overlayCancelPressed.connect(self._on_cancel_pressed)
            input_thread.registered.connect(self._on_registered)

    # --- public

    @property
    def state(self) -> str:
        return self._state

    def selecting(self) -> bool:
        return self._state == "selecting"

    def busy(self) -> bool:
        """Selecting or recognising."""
        return self._state != "idle"

    def language_ready(self, *, refresh: bool = False) -> bool:
        """Whether Windows has an OCR language. `refresh` asks Windows again (Try again after adding one)."""
        try:
            langs = self.ocr.refresh_languages() if refresh else self.ocr.languages()
        except Exception:
            log.exception("Screen: couldn't read the OCR languages")
            return True  # unknown: let the recognition report what is wrong
        return bool(langs)

    def begin(self, *, return_to: int = 0) -> None:
        """Open the selection overlay. `return_to` is the window to give focus back to afterwards.

        With no OCR language, `failed("no_language")` follows at once and nothing opens."""
        if self._state != "idle":
            log.info("Screen: already %s", self._state)
            return
        if not self.language_ready():
            log.info("Screen: no OCR language")
            self.failed.emit("no_language")
            return
        self._return_to = int(return_to or 0)
        if self.input is not None:
            self.input.set_overlay_open(True)
            self.input.set_escape_hotkey(True)
        try:
            self.overlay.open(pill_detail="esc")
        except Exception:
            log.exception("Screen: the overlay couldn't open")
            self.overlay.close()
            self._end_selection()  # or every shortcut would stay a cancel and Esc stay taken
            self.failed.emit("error")
            return
        if self.input is not None:  # watched from the input thread in case the app stops answering
            self.input.set_overlay_open(True, hwnds=[int(w.winId()) for w in self.overlay.windows()])
        self._set_state("selecting")

    def cancel(self, reason: str = "app") -> None:
        """Close the overlay, or drop a recognition under way (its frame goes at once). `failed("cancelled")`
        follows; nothing else changes."""
        if self._state == "selecting":
            self.overlay.close()
            self._end_selection()
        elif self._state == "recognizing":
            self._token += 1
            self._job = None
            self._timeout.stop()
            self.highlight.hide_frame(animate=False)
        else:
            return
        log.info("Screen: cancelled (%s)", reason)
        self._set_state("idle")
        self.failed.emit("cancelled")

    def escape_unavailable(self) -> None:
        """Esc belongs to another app, which gets every press whatever is in front: say right-click cancels.
        Nothing is activated, so the foreground stays where it was."""
        if self._state == "selecting":
            self.overlay.set_pill_detail("right_click")

    # --- selection

    def _on_cancel_pressed(self, reason: str) -> None:
        if self._state == "selecting":
            self.cancel(reason)

    def _on_registered(self, hk_id: int, ok: bool) -> None:
        from .input_thread import HK_ESCAPE
        if hk_id == HK_ESCAPE and not ok:
            log.info("Screen: Esc unavailable")
            self.escape_unavailable()

    def _on_overlay_cancelled(self, reason: str) -> None:
        if self._state != "selecting":
            return
        self._end_selection()
        log.info("Screen: cancelled (%s)", reason)
        self._set_state("idle")
        self.failed.emit("cancelled")

    def _end_selection(self) -> None:
        """The overlay is gone: release the shortcuts and Esc, and give the foreground back if it was taken."""
        if self.input is not None:
            self.input.set_overlay_open(False)
            self.input.set_escape_hotkey(False)
        hwnd, self.overlay.activated_hwnd = self.overlay.activated_hwnd, 0
        if hwnd:
            win32_overlay.give_back_foreground(hwnd, self._return_to)

    def _on_selected(self, screen, rect: QRect) -> None:
        if self._state != "selecting":
            return
        if self.overlay.capture_excluded():
            self._capture(screen, QRect(rect))
            return
        # Older Windows can't leave the overlay out of a capture: hide it first and give the screen a frame or two
        self.overlay.close()
        self._end_selection()
        self._set_state("recognizing")
        token = self._token

        def capture() -> None:
            if self._token == token and self._state == "recognizing":
                self._capture(screen, QRect(rect))
        QTimer.singleShot(UNHIDE_MS, self, capture)

    def _capture(self, screen, rect: QRect) -> None:
        started = time.perf_counter()
        try:
            image = screen_capture.grab(screen, rect)
            thumb = screen_capture.thumb(screen, rect)
            light = screen_capture.is_light(image)
        except Exception:
            log.exception("Screen: capture failed")
            image, thumb, light = None, b"", True
        if self.overlay.is_open():
            self.overlay.close()
            self._end_selection()
        self.highlight.show_frame(screen, rect)
        if self._state != "recognizing":
            self._set_state("recognizing")
        if image is None or image.isNull():
            self._fail("error")
            return
        self._token += 1
        self._job = {"token": self._token, "screen": screen, "rect": QRect(rect), "thumb": thumb, "light": light,
                     "size": (image.width(), image.height()), "dpr": image.devicePixelRatio(),
                     "grab_ms": (time.perf_counter() - started) * 1000, "started": time.perf_counter()}
        self._timeout.start(OCR_TIMEOUT_MS)
        self.ocr.recognize(self._token, image, origin=QPointF(rect.topLeft()), dpr=image.devicePixelRatio(),
                           upscale=UPSCALE)
        # the image is the worker's now; nothing here keeps a reference to it

    # --- recognition

    def _on_done(self, token: int, result: OcrResult) -> None:
        job = self._job
        if job is None or token != job["token"] or self._state != "recognizing":
            return
        self._timeout.stop()
        self._job = None
        ocr_ms = (time.perf_counter() - job["started"]) * 1000
        lines = len(result.lines)
        if not any(line.words for line in result.lines):
            log.info("Screen: no text found")
            self._fail("nothing_found")
            return
        try:
            text = layout.to_text(result)
            doc = self._build(text.text)
        except Exception:
            log.exception("Screen: couldn't build the reading")
            self._fail("error")
            return
        if not doc.sentences or doc.spoken_chars_from(0) <= 0:
            log.info("Screen: nothing to read in %d lines", lines)
            self._fail("nothing_speakable")
            return
        try:
            boxes = layout.map_boxes(doc, text)
        except Exception:
            log.exception("Screen: couldn't place the words")
            boxes = {}
        rect = job["rect"]
        doc.source = DocSource("screen", screen=ScreenText(job["screen"].name(), rect, boxes, job["thumb"],
                                                           job["light"]))
        w, h = job["size"]
        log.info("Screen: captured %dx%d px at %g, OCR %d ms, %d lines, %s characters", w, h, job["dpr"],
                 round(ocr_ms), lines, f"{len(text.text):,}")
        self._set_state("idle")
        self.textReady.emit(doc, text)

    def _on_failed(self, token: int, kind: str) -> None:
        job = self._job
        if job is None or token != job["token"] or self._state != "recognizing":
            return
        self._timeout.stop()
        self._job = None
        if kind == "no_language":
            log.info("Screen: no OCR language")
        elif kind == "too_large":
            log.info("Screen: area too large")
        else:
            log.info("Screen: OCR error")
        self._fail(kind if kind in ("no_language", "too_large") else "error")

    def _on_timeout(self) -> None:
        if self._state == "recognizing":
            log.warning("Screen: OCR didn't answer")
            self._token += 1
            self._job = None
            self._fail("error")

    def _fail(self, kind: str) -> None:
        self.highlight.hide_frame(animate=True)
        self._set_state("idle")
        self.failed.emit(kind)

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.stateChanged.emit(state)


def _default_build(text: str):
    from .text_model import Document
    return Document.from_raw(text)


def notice_for(kind: str) -> str | None:
    """The engine's ocr_notice kind for a failure, or None for "cancelled"."""
    return NOTICE_FOR.get(kind)

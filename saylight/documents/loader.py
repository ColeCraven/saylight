"""Open a file and build its reading on one worker thread, off the GUI thread.

`read_file` picks the reader by extension. `DocumentLoader` runs it, then `Document.build`, on its own thread
and reports back through queued signals, tagged with the caller's token; a cancelled or superseded token
reports nothing. It also builds large selections, which take more than a frame to prepare. The password, if
any, lives only in the job until the file is opened. Logs give kinds and counts, never paths, titles or text.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..text_model import BuildOptions, Document
from .model import DocSource, LoadedDocument, LoadError, RawInput
from .pdf import Cancelled

log = logging.getLogger("saylight.documents")

SUPPORTED = (".pdf", ".docx", ".txt", ".md", ".markdown")
PROGRESS_INTERVAL_S = 0.25   # at most four progress reports a second


def read_file(path: str, *, password: str | None = None, progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> LoadedDocument:
    """The file's text. Raises LoadError (its kind says why) or Cancelled."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED:
        raise LoadError("unsupported", ext if ext in (".doc", ".rtf", ".odt", ".pages") else "")
    if not os.path.isfile(path):
        raise LoadError("io", "missing")
    if ext == ".pdf":
        from .pdf import extract_pdf
        return extract_pdf(path, password=password, progress=progress, cancelled=cancelled or (lambda: False))
    if ext == ".docx":
        from .docx import read_docx
        return read_docx(path)
    from .text import read_text_file
    return read_text_file(path)


class DocumentLoader(QObject):
    """One worker thread for opening files and building big readings; results arrive on the GUI thread."""

    progress = Signal(int, int, int)       # token, done, total (PDF pages)
    built = Signal(int, object, object)    # token, Document, RawInput (kept for rebuilds)
    failed = Signal(int, str)              # token, LoadError kind

    _relay_progress = Signal(int, int, int)
    _relay_built = Signal(int, object, object)
    _relay_failed = Signal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._live: set[int] = set()       # tokens still wanted
        self._thread: threading.Thread | None = None
        self._closed = False
        queued = Qt.ConnectionType.QueuedConnection
        self._relay_progress.connect(self._on_progress, queued)
        self._relay_built.connect(self._on_built, queued)
        self._relay_failed.connect(self._on_failed, queued)

    # --- GUI thread -----------------------------------------------------------------------------------------

    def open(self, token: int, path: str, options: BuildOptions, *, password: str | None = None) -> None:
        """Read the file and build its Document; `password` is used for this call only."""
        self._submit(token, ("open", path, options, password))

    def build(self, token: int, raw: RawInput, options: BuildOptions) -> None:
        """Build a Document from text or an already-read file (a rebuild after a toggle)."""
        self._submit(token, ("build", raw, options, None))

    def cancel(self, token: int) -> None:
        """Nothing more is reported for this token; a PDF being read stops at its next page."""
        with self._lock:
            self._live.discard(token)

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._closed = True
            self._live.clear()
        self._jobs.put(None)
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._live)

    def _submit(self, token: int, job: tuple) -> None:
        with self._lock:
            if self._closed:
                return
            self._live.add(token)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="saylight.documents", daemon=True)
                self._thread.start()
        self._jobs.put((token, job))

    def _wanted(self, token: int) -> bool:
        with self._lock:
            return token in self._live

    @Slot(int, int, int)
    def _on_progress(self, token: int, done: int, total: int) -> None:
        if self._wanted(token):
            self.progress.emit(token, done, total)

    @Slot(int, object, object)
    def _on_built(self, token: int, doc: object, raw: object) -> None:
        with self._lock:
            wanted = token in self._live
            self._live.discard(token)
        if wanted:
            self.built.emit(token, doc, raw)

    @Slot(int, str)
    def _on_failed(self, token: int, kind: str) -> None:
        with self._lock:
            wanted = token in self._live
            self._live.discard(token)
        if wanted:
            self.failed.emit(token, kind)

    # --- worker thread --------------------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                return
            token, (what, subject, options, password) = item
            item = None
            if not self._wanted(token):
                continue
            try:
                self._work(token, what, subject, options, password)
            except Cancelled:
                log.info("Opening cancelled")
            except LoadError as e:
                log.info("Opening failed: %s", e.kind)
                self._relay_failed.emit(token, e.kind)
            except MemoryError:
                log.warning("Opening failed: out of memory")
                self._relay_failed.emit(token, "too_large")
            except Exception as e:  # never let one file stop the worker
                log.warning("Opening failed: %s", type(e).__name__)
                self._relay_failed.emit(token, "corrupt")
            finally:
                password = None

    def _work(self, token: int, what: str, subject, options: BuildOptions, password: str | None) -> None:
        t0 = time.perf_counter()
        source = None
        if what == "open":
            last = [0.0]

            def progress(done: int, total: int) -> None:
                now = time.monotonic()
                if done == total or now - last[0] >= PROGRESS_INTERVAL_S:
                    last[0] = now
                    self._relay_progress.emit(token, done, total)

            raw = read_file(subject, password=password, progress=progress, cancelled=lambda: not self._wanted(token))
            source = DocSource("file", subject, raw.title)
        else:
            raw = subject
            if isinstance(raw, LoadedDocument):
                source = None  # the caller keeps the Document's source across rebuilds
        if not self._wanted(token):
            raise Cancelled()
        doc = Document.build(raw, options)
        doc.source = source
        if what == "open" and not doc.sentences:
            raise LoadError("empty")
        log.info("Built: %d sentences, %d characters, %d regions, %.0f ms", len(doc.sentences), len(doc.text),
                 len(doc.regions), (time.perf_counter() - t0) * 1000)
        self._relay_built.emit(token, doc, raw)

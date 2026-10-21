"""Open a file and build its reading on one worker thread, off the GUI thread.

`read_file` picks the reader by extension. `DocumentLoader` runs it, then `Document.build`, on its own thread
and reports back through queued signals, tagged with the caller's token; a cancelled or superseded token
reports nothing. It also builds large selections, which take more than a frame to prepare. The password, if
any, lives only in the job until the file is opened. Logs give kinds and counts, never paths, titles or text.

A PDF with scanned pages has them read with Windows OCR on the same thread, one page at a time, after checking
that Windows has an OCR language (before any page is rendered). More than SCAN_CHOICE_PAGES scanned pages wait
for the person to choose which to read (scanChoice, then continue_scan); until then the opened document is held
in memory, and dropped on cancel. Pages, pictures and the text found are never written anywhere.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..text_model import BuildOptions, Document
from .model import DocSource, LoadedDocument, LoadError, RawInput
from .pdf import MAX_CHARS, Cancelled

log = logging.getLogger("saylight.documents")

_BLANKS_RE = re.compile(r"\n{3,}")

SUPPORTED = (".pdf", ".docx", ".txt", ".md", ".markdown")
PROGRESS_INTERVAL_S = 0.25   # at most four progress reports a second
SCAN_CHOICE_PAGES = 50       # more scanned pages than this: the person chooses which pages to read


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


@dataclass
class _Scan:
    """A scanned PDF waiting for OCR: what continue_scan needs to go on. Memory only."""
    token: int
    path: str
    raw: LoadedDocument
    options: BuildOptions
    pdf: object      # pdf_ocr.ScanPdf, already open: the password isn't kept
    reader: object   # the pypdf reader, for each page's scan resolution


class DocumentLoader(QObject):
    """One worker thread for opening files and building big readings; results arrive on the GUI thread."""

    progress = Signal(int, int, int)       # token, done, total (PDF pages)
    built = Signal(int, object, object)    # token, Document, RawInput (kept for rebuilds)
    failed = Signal(int, str)              # token, LoadError kind
    scanChoice = Signal(int, int, int)     # token, scanned pages, total pages: too many to find the text in unasked
    scanProgress = Signal(int, int, int)   # token, done, total: scanned pages read with OCR (0 of n as it starts)

    _relay_progress = Signal(int, int, int)
    _relay_built = Signal(int, object, object)
    _relay_failed = Signal(int, str)
    _relay_choice = Signal(int, int, int)
    _relay_scan_progress = Signal(int, int, int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._live: set[int] = set()       # tokens still wanted
        self._thread: threading.Thread | None = None
        self._closed = False
        self._choice: _Scan | None = None  # the scanned PDF waiting for continue_scan
        queued = Qt.ConnectionType.QueuedConnection
        self._relay_progress.connect(self._on_progress, queued)
        self._relay_built.connect(self._on_built, queued)
        self._relay_failed.connect(self._on_failed, queued)
        self._relay_choice.connect(self._on_choice, queued)
        self._relay_scan_progress.connect(self._on_scan_progress, queued)

    # --- GUI thread -----------------------------------------------------------------------------------------

    def open(self, token: int, path: str, options: BuildOptions, *, password: str | None = None) -> None:
        """Read the file and build its Document; `password` is used for this call only."""
        with self._lock:
            if self._choice is not None and self._choice.token != token:
                self._choice = None  # one scanned PDF waits at a time
        self._submit(token, ("open", path, options, password))

    def build(self, token: int, raw: RawInput, options: BuildOptions) -> None:
        """Build a Document from text or an already-read file (a rebuild after a toggle)."""
        self._submit(token, ("build", raw, options, None))

    def continue_scan(self, token: int, first: int, last: int) -> None:
        """After scanChoice: read pages first..last (1-based document pages, inclusive), finding the text in the
        scanned ones. The pages outside the range are left out of the reading. Swapped or out-of-range numbers
        are put right."""
        with self._lock:
            waiting = self._choice is not None and self._choice.token == token and token in self._live
        if waiting:
            self._submit(token, ("scan", (int(first), int(last)), None, None))

    def scanned_in(self, token: int, first: int, last: int) -> int:
        """How many scanned pages of the PDF waiting for its choice lie in first..last; 0 for any other token."""
        a, b = sorted((first, last))
        with self._lock:
            choice = self._choice
            if choice is None or choice.token != token:
                return 0
            return sum(1 for p in choice.raw.scanned_pages if a <= p <= b)

    def cancel(self, token: int) -> None:
        """Nothing more is reported for this token; a PDF being read stops at its next page, and one waiting for
        its choice of pages is dropped."""
        with self._lock:
            self._live.discard(token)
            if self._choice is not None and self._choice.token == token:
                self._choice = None

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._closed = True
            self._live.clear()
            self._choice = None
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

    @Slot(int, int, int)
    def _on_scan_progress(self, token: int, done: int, total: int) -> None:
        if self._wanted(token):
            self.scanProgress.emit(token, done, total)

    @Slot(int, int, int)
    def _on_choice(self, token: int, scanned: int, total: int) -> None:
        with self._lock:
            waiting = token in self._live and self._choice is not None and self._choice.token == token
        if waiting:
            self.scanChoice.emit(token, scanned, total)

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

            try:
                raw = read_file(subject, password=password, progress=progress,
                                cancelled=lambda: not self._wanted(token))
            except LoadError as e:
                if e.kind != "scanned":
                    raise
                raw = None  # no page has text to read: every page is a scan
            if raw is None or raw.scanned_pages:
                raw = self._scan_start(token, subject, password, raw, options)
                if raw is None:
                    return  # waiting for the person to choose pages
            source = DocSource("file", subject, raw.title)
        elif what == "scan":
            with self._lock:
                job = self._choice if self._choice is not None and self._choice.token == token else None
                if job is not None:
                    self._choice = None
            if job is None:
                return
            first, last = subject
            raw = self._scan_run(job, first, last)
            options = job.options
            source = DocSource("file", job.path, raw.title)
        else:
            raw = subject
            if isinstance(raw, LoadedDocument):
                source = None  # the caller keeps the Document's source across rebuilds
        if not self._wanted(token):
            raise Cancelled()
        doc = Document.build(raw, options)
        doc.source = source
        if what in ("open", "scan") and not doc.sentences:
            raise LoadError("scan_nothing" if getattr(raw, "scan_dpi", 0) else "empty")
        log.info("Built: %d sentences, %d characters, %d regions, %.0f ms", len(doc.sentences), len(doc.text),
                 len(doc.regions), (time.perf_counter() - t0) * 1000)
        self._relay_built.emit(token, doc, raw)

    # --- scanned pages (worker thread) ------------------------------------------------------------------------

    def _scan_start(self, token: int, path: str, password: str | None, raw: LoadedDocument | None,
                    options: BuildOptions) -> LoadedDocument | None:
        """A PDF with scanned pages (`raw` None: every page is one): read them, or wait for the person's choice
        (None). Checks for an OCR language before any page is rendered."""
        from .. import winrt_support
        if not winrt_support.available():
            return _without_ocr(raw, "OCR unavailable", "scanned")
        from ..ocr.engine import user_languages
        try:
            languages = user_languages()
        except Exception as e:
            log.warning("Scan: OCR languages unavailable (%s)", type(e).__name__)
            languages = []
        if not languages:
            return _without_ocr(raw, "no OCR language", "scan_language")
        from . import pdf_ocr
        from .pdf import _open
        reader = _open(path, password)
        if raw is None:
            raw = _all_scanned(path, len(reader.pages))
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            raise LoadError("io", type(e).__name__) from None
        try:
            scan = pdf_ocr.ScanPdf(data, password)
        except Exception as e:
            log.warning("Scan: the PDF couldn't be opened for OCR (%s)", type(e).__name__)
            raise LoadError("scan_error", type(e).__name__) from None
        finally:
            data = password = None
        job = _Scan(token, path, raw, options, scan, reader)
        scanned = len(raw.scanned_pages)
        log.info("Scan: %d of %d pages need OCR", scanned, raw.pages)
        if scanned > SCAN_CHOICE_PAGES:
            with self._lock:
                if token not in self._live:
                    raise Cancelled()
                self._choice = job
            log.info("Scan: choice shown (%d scanned of %d)", scanned, raw.pages)
            self._relay_choice.emit(token, scanned, raw.pages)
            return None
        return self._scan_run(job, 1, raw.pages)

    def _scan_run(self, job: _Scan, first: int, last: int) -> LoadedDocument:
        """Read the scanned pages in first..last with OCR and put their text in the document."""
        from ..ocr.engine import OcrError
        from . import pdf_ocr
        raw, token = job.raw, job.token
        n = max(1, raw.pages)
        first, last = sorted((max(1, min(n, first)), max(1, min(n, last))))
        if (first, last) != (1, n):
            log.info("Scan: pages %d-%d chosen", first, last)
        pages = [p for p in raw.scanned_pages if first <= p <= last]
        done = [0]

        def progress(d: int, total: int) -> None:
            done[0] = d
            self._relay_scan_progress.emit(token, d, total)

        self._relay_scan_progress.emit(token, 0, len(pages))
        try:
            texts = pdf_ocr.ocr_pages(job.pdf, pages, progress=progress, cancelled=lambda: not self._wanted(token))
        except Cancelled:
            log.info("Scan: cancelled at page %d of %d", min(done[0] + 1, len(pages)), len(pages))
            raise
        except OcrError as e:
            log.info("Scan: OCR failed (%s)", e.kind)
            raise LoadError("scan_language" if e.kind == "no_language" else "scan_error", e.kind) from None
        except (LoadError, MemoryError):
            raise
        except Exception as e:
            log.warning("Scan: OCR error (%s)", type(e).__name__)
            raise LoadError("scan_error", type(e).__name__) from None
        dpi = 0
        for p in pages:
            rendered = job.pdf.dpi.get(p, pdf_ocr.TARGET_DPI)
            try:
                native = pdf_ocr.page_dpi(job.reader.pages[p - 1])
            except Exception:
                native = 0
            here = min(native, rendered) if native else rendered
            dpi = here if not dpi else min(dpi, here)
        if 0 < dpi < pdf_ocr.LOW_DPI:
            log.info("Scan: low resolution (about %d dpi)", dpi)
        return _merge(raw, first, last, dict(zip(pages, (t.text for t in texts))), dpi)


def _without_ocr(raw: LoadedDocument | None, why: str, kind: str) -> LoadedDocument:
    """No OCR: a PDF that is all scans fails with `kind`; one with text pages is read without its scans."""
    if raw is None:
        log.info("Scan: %s", why)
        raise LoadError(kind)
    log.info("Scan: %s, %d pages skipped", why, len(raw.scanned_pages))
    return raw


def _all_scanned(path: str, pages: int) -> LoadedDocument:
    """A PDF with no text at all, every page waiting for OCR: empty pages in the PDF reader's layout."""
    title = os.path.splitext(os.path.basename(path))[0]
    return LoadedDocument("pdf", title, "\n\n" * max(0, pages - 1), None, [], pages, list(range(1, pages + 1)),
                          "", [], [2 * k for k in range(pages)])


def _merge(raw: LoadedDocument, first: int, last: int, found: dict[int, str], dpi: int) -> LoadedDocument:
    """The document with the OCR text of `found`'s pages, and the pages outside first..last left out (as empty
    pages, so page numbers stay right)."""
    from ..academic_text import references_start
    starts = list(raw.page_starts) or [0]
    ends = [s - 2 for s in starts[1:]] + [len(raw.text)]
    parts: list[str] = []
    empty = 0
    for k, (a, b) in enumerate(zip(starts, ends)):
        number = k + 1
        if not first <= number <= last:
            parts.append("")
        elif number in found:
            text = _BLANKS_RE.sub("\n\n", found[number]).strip()
            empty += not text
            parts.append(text)
        else:
            parts.append(raw.text[a:max(a, b)])
    if empty:
        log.info("Scan: %d empty pages skipped", empty)
    text = "\n\n".join(parts)
    page_starts, pos = [], 0
    for part in parts:
        page_starts.append(pos)
        pos += len(part) + 2
    asides = [a for a in raw.asides if a.page is None or first <= a.page <= last]
    if not text.strip() and not asides:
        raise LoadError("scan_nothing" if found else "empty")
    if len(text) + sum(len(a.text) for a in asides) > MAX_CHARS:
        raise LoadError("too_large")
    left = [p for p in raw.scanned_pages if first <= p <= last and p not in found]
    return replace(raw, text=text, references_at=references_start(text), asides=asides, scanned_pages=left,
                   page_starts=page_starts, scan_dpi=dpi if found else 0)

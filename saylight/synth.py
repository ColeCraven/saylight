"""Speech synthesis: edge-tts with WordBoundary timing on a persistent asyncio loop thread.

SynthWorker is created on the GUI thread. Jobs run one at a time, in FIFO order, on a daemon
"synth" thread. Results are relayed back to the GUI thread before the public signals fire, so a
cancel issued on the GUI thread always wins over a result that has not been delivered yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import itertools
import logging
import os
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
import edge_tts
from edge_tts.exceptions import EdgeTTSException, NoAudioReceived
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtNetwork import QNetworkInformation

log = logging.getLogger("saylight.synth")

CONNECT_TIMEOUT_S = 8
RECEIVE_TIMEOUT_S = 30
IDLE_TIMEOUT_S = 20.0      # no stream item for this long -> TimeoutError (a silently stalled connection)
DIR_PREFIX = "saylight_"
OLD_DIR_PREFIXES = ("read_aloud_",)  # cache folders left by the app's earlier name; purged like our own
STALE_DIR_AGE_S = 3600
DELETE_RETRIES = 8         # a chunk file still open in MCI is retried this many times ...
DELETE_RETRY_S = 0.25      # ... this far apart
QUOTE_RUN = 12             # an error message sharing this many characters with the text is withheld

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_NET_ERRNOS = {errno.ENETDOWN, errno.ENETUNREACH, errno.ENETRESET, errno.ECONNABORTED, errno.ECONNRESET,
               errno.ETIMEDOUT, errno.ECONNREFUSED, errno.EHOSTDOWN, errno.EHOSTUNREACH}
# ERROR_NETNAME_DELETED, ERROR_SEM_TIMEOUT, ERROR_CONNECTION_REFUSED, ERROR_NETWORK_UNREACHABLE,
# ERROR_HOST_UNREACHABLE, ERROR_CONNECTION_ABORTED, then the WSAE* codes.
_NET_WINERRORS = {64, 121, 1225, 1231, 1232, 1236, 10050, 10051, 10052, 10053, 10054, 10060, 10061, 10064, 10065}
_OFFLINE_TYPES = (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, socket.gaierror, TimeoutError,
                  ConnectionError, ssl.SSLError)
_SERVICE_TYPES = (aiohttp.ClientResponseError, EdgeTTSException)  # includes WSServerHandshakeError


class IncompleteAudio(ConnectionError):
    """The stream ended before the service confirmed the end of the chunk (a dropped connection).

    aiohttp reports a lost websocket as a normal close, so edge-tts would otherwise return the
    partial audio as if it were the whole chunk.
    """


@dataclass(frozen=True)
class Word:
    """One WordBoundary event, in ms from the start of the chunk's MP3 (leading silence included)."""
    offset_ms: float
    duration_ms: float
    text: str


@dataclass(eq=False)
class _Job:
    gen: int
    chunk_id: int
    text: str
    voice: str
    rate: str
    cancelled: bool = False


@dataclass(eq=False)
class _Result:
    job: _Job
    path: str | None = None
    words: list[Word] | None = None
    kind: str = ""
    detail: str = ""


def rate_string(speed: float) -> str:
    """The edge-tts rate for a speed multiplier: 0.95 -> "-5%", 1.25 -> "+25%"."""
    return f"{round((speed - 1) * 100):+d}%"


def classify_error(exc: BaseException, *, disconnected: bool = False) -> str:
    """Classify an error as "offline", "service" or "other".

    A service-type error while the OS reports no network is treated as offline. Local errors
    ("other": bad arguments, disk) keep their kind.
    """
    if isinstance(exc, _SERVICE_TYPES):
        kind = "service"
    elif isinstance(exc, _OFFLINE_TYPES) or (
            isinstance(exc, OSError) and (exc.errno in _NET_ERRNOS or getattr(exc, "winerror", None) in _NET_WINERRORS)):
        kind = "offline"
    else:
        kind = "other"
    return "offline" if disconnected and kind == "service" else kind


def purge_stale_dirs(root: str, *, keep: str | None = None, max_age_s: float = STALE_DIR_AGE_S) -> int:
    """Delete cache folders in `root` (ours and any from the earlier name) with nothing newer than `max_age_s`.
    Returns the count."""
    cutoff = time.time() - max_age_s
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith((DIR_PREFIX, *OLD_DIR_PREFIXES)) or entry.path == keep:
            continue
        try:
            if not entry.is_dir(follow_symlinks=False) or _newest_mtime(entry.path) > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed += not os.path.exists(entry.path)
    if removed:
        log.info("Removed %d stale audio cache folder(s)", removed)
    return removed


def _newest_mtime(path: str) -> float:
    newest = os.stat(path).st_mtime
    with os.scandir(path) as it:
        for child in it:
            with contextlib.suppress(OSError):
                newest = max(newest, child.stat(follow_symlinks=False).st_mtime)
    return newest


def _try_remove(path: str) -> bool:
    """Remove a file. False only if it is locked (still open elsewhere) and worth retrying."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except PermissionError:
        return False
    except OSError as e:
        log.warning("Couldn't delete a cached chunk: %s", type(e).__name__)
    return True


def _describe(exc: BaseException, text: str) -> str:
    """Class name and message for chunkFailed, never the text being read."""
    name = type(exc).__name__
    if isinstance(exc, UnicodeError):
        return name
    msg = " ".join(str(exc).split())[:300]
    if msg and _quotes(msg, text):
        msg = "(message withheld: it quoted the text)"
    return f"{name}: {msg}" if msg else name


def _quotes(msg: str, text: str) -> bool:
    """True if `msg` contains any QUOTE_RUN-character stretch of `text` (whitespace collapsed)."""
    flat = " ".join(text.split())
    if len(flat) <= QUOTE_RUN:
        return bool(flat) and flat in msg
    return any(msg[i:i + QUOTE_RUN] in flat for i in range(len(msg) - QUOTE_RUN + 1))


def _confirmed_bytes(comm: object) -> int | None:
    """Audio bytes edge-tts saw confirmed by the service's turn.end, or None if it does not say."""
    state = getattr(comm, "state", None)
    value = state.get("cumulative_audio_bytes") if isinstance(state, dict) else None
    return value if isinstance(value, int) else None


class SynthWorker(QObject):
    """edge-tts synthesis, one request in flight, results as MP3 files plus word timings."""

    chunkReady = Signal(int, int, str, object)    # gen, chunk_id, path, list[Word]
    chunkFailed = Signal(int, int, str, str)      # gen, chunk_id, kind, detail
    _delivered = Signal(object)                   # synth thread -> GUI thread relay (_Result)
    _watchRequested = Signal()                    # start() off the GUI thread -> _watch_network on it

    def __init__(self, parent: QObject | None = None, *, temp_root: str | None = None):
        super().__init__(parent)
        self._temp_root = temp_root or tempfile.gettempdir()
        self._lock = threading.Lock()
        self._pending: deque[_Job] = deque()
        self._live: list[_Job] = []               # submitted and not yet delivered or dropped
        self._current: _Job | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._dir: str | None = None
        self._closed = False
        self._disconnected = False
        self._seq = itertools.count(1)
        self._delivered.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._watchRequested.connect(self._watch_network, Qt.ConnectionType.QueuedConnection)

    @property
    def temp_dir(self) -> str | None:
        """This run's cache folder (None before start())."""
        return self._dir

    # --- Public API (thread-safe unless noted) ---------------------------------------------

    def start(self) -> None:
        """Create the cache folder, purge stale ones and start the loop thread."""
        with self._lock:
            if self._thread is not None or self._closed:
                return
            os.makedirs(self._temp_root, exist_ok=True)
            self._dir = tempfile.mkdtemp(prefix=DIR_PREFIX, dir=self._temp_root)
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, name="synth", daemon=True)
        purge_stale_dirs(self._temp_root, keep=self._dir)
        self._thread.start()
        if QThread.currentThread() is self.thread():
            self._watch_network()
        else:
            self._watchRequested.emit()         # QNetworkInformation belongs on the GUI thread
        self._post(self._pump)

    def synthesize(self, gen: int, chunk_id: int, text: str, voice: str, rate: str | float) -> None:
        """Queue a chunk. `rate` is an edge-tts rate ("-5%") or a speed multiplier (0.95)."""
        job = _Job(gen, chunk_id, text, voice, rate if isinstance(rate, str) else rate_string(rate))
        with self._lock:
            if self._closed:
                return
            self._pending.append(job)
            self._live.append(job)
            running = self._thread is not None
        log.debug("Queued gen %d chunk %d (%d chars, rate %s)", gen, chunk_id, len(text), job.rate)
        if running:
            self._post(self._pump)

    def cancel_before(self, gen: int) -> None:
        """Cancel queued, in-flight and undelivered jobs with job.gen < gen. Emits nothing for them."""
        self._cancel(lambda job: job.gen < gen)

    def cancel_all(self) -> None:
        """Cancel every queued, in-flight and undelivered job."""
        self._cancel(lambda job: True)

    def delete(self, path: str) -> None:
        """Delete a delivered chunk file. A file still open in MCI is retried for about 2 s."""
        if not self._owns(path):
            log.warning("Refused to delete a file outside the audio cache folder")
            return
        if not _try_remove(path):
            self._post(self._retry_delete, path, DELETE_RETRIES)

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel everything, stop and join the loop thread, and delete the cache folder."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for job in self._live:
                job.cancelled = True
            self._pending.clear()
            self._live.clear()
            loop, thread = self._loop, self._thread
        if loop is not None and thread is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Synth thread did not stop within %.1f s", timeout)
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            if os.path.exists(self._dir):
                log.warning("Couldn't fully delete the audio cache folder")

    # --- GUI thread ----------------------------------------------------------------------

    @Slot(object)
    def _deliver(self, res: _Result) -> None:
        job = res.job
        with self._lock:
            with contextlib.suppress(ValueError):
                self._live.remove(job)
            drop = job.cancelled or self._closed
        if drop:
            if res.path:
                self.delete(res.path)
            return
        if res.path is not None:
            self.chunkReady.emit(job.gen, job.chunk_id, res.path, res.words)
        else:
            self.chunkFailed.emit(job.gen, job.chunk_id, res.kind, res.detail)

    @Slot()
    def _watch_network(self) -> None:
        try:
            if not QNetworkInformation.loadBackendByFeatures(QNetworkInformation.Feature.Reachability):
                log.info("No network reachability backend; errors are classified by type only")
                return
            info = QNetworkInformation.instance()
            self._on_reachability(info.reachability())
            info.reachabilityChanged.connect(self._on_reachability)
        except Exception:  # noqa: BLE001 - optional refinement, never fatal
            log.warning("Network reachability unavailable", exc_info=True)

    @Slot(QNetworkInformation.Reachability)
    def _on_reachability(self, reachability: QNetworkInformation.Reachability) -> None:
        self._disconnected = reachability == QNetworkInformation.Reachability.Disconnected

    # --- Any thread ----------------------------------------------------------------------

    def _post(self, fn: Callable, *args) -> None:
        """Run fn(*args) on the loop thread, if it is running."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # loop already closed by shutdown()
            pass

    def _cancel(self, match: Callable[[_Job], bool]) -> None:
        with self._lock:
            hit = 0
            for job in self._live:
                if not job.cancelled and match(job):
                    job.cancelled = True
                    hit += 1
            if not hit:
                return
            queued = [job for job in self._pending if job.cancelled]
            if queued:
                self._pending = deque(job for job in self._pending if not job.cancelled)
                self._live = [job for job in self._live if job not in queued]
            task = self._task if self._current is not None and self._current.cancelled else None
        log.debug("Cancelled %d job(s)%s", hit, ", one in flight" if task else "")
        if task is not None:
            self._post(task.cancel)

    def _owns(self, path: str) -> bool:
        if not self._dir:
            return False
        root = os.path.normcase(os.path.abspath(self._dir))
        try:
            return os.path.commonpath([os.path.normcase(os.path.abspath(path)), root]) == root
        except ValueError:  # different drives
            return False

    # --- Loop thread ---------------------------------------------------------------------

    def _run_loop(self) -> None:
        loop = self._loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.wait(tasks, timeout=1.0))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor(timeout=0.5))
        except Exception:  # noqa: BLE001 - keep the thread's end orderly
            log.exception("Synth loop failed")
        finally:
            loop.close()

    def _pump(self) -> None:
        with self._lock:
            if self._task is not None or self._closed or not self._pending:
                return
            job = self._current = self._pending.popleft()
            task = self._task = self._loop.create_task(self._synth(job))
        task.add_done_callback(self._task_done)

    def _task_done(self, _task: asyncio.Task) -> None:
        with self._lock:
            self._task = self._current = None
        self._pump()

    def _retry_delete(self, path: str, left: int) -> None:
        if _try_remove(path):
            return
        if left > 1:
            self._loop.call_later(DELETE_RETRY_S, self._retry_delete, path, left - 1)
        else:
            log.warning("Couldn't delete a cached chunk: still in use")

    async def _synth(self, job: _Job) -> None:
        part = os.path.join(self._dir, f"g{job.gen}_c{job.chunk_id}_{next(self._seq)}.part")
        words: list[Word] = []
        size = 0
        t0 = time.monotonic()
        try:
            text = _LONE_SURROGATE.sub(" ", job.text)  # same length; lone surrogates can't be UTF-8 encoded
            if not text.strip():
                raise ValueError("empty text")
            comm = edge_tts.Communicate(text, job.voice, rate=job.rate, boundary="WordBoundary",
                                        connect_timeout=CONNECT_TIMEOUT_S, receive_timeout=RECEIVE_TIMEOUT_S)
            loop = asyncio.get_running_loop()
            os.makedirs(self._dir, exist_ok=True)  # a temp cleaner may have removed it in a long session
            with open(part, "wb") as f:
                async with asyncio.timeout(IDLE_TIMEOUT_S) as idle:
                    async with contextlib.aclosing(comm.stream()) as stream:
                        async for item in stream:
                            idle.reschedule(loop.time() + IDLE_TIMEOUT_S)
                            if item["type"] == "audio":
                                f.write(item["data"])
                                size += len(item["data"])
                            elif item["type"] == "WordBoundary":
                                words.append(Word(item["offset"] / 10_000, item["duration"] / 10_000, item["text"]))
            if not size:
                raise NoAudioReceived("No audio was received.")
            confirmed = _confirmed_bytes(comm)
            if confirmed is not None and confirmed != size:
                raise IncompleteAudio(f"the connection closed early ({confirmed} of {size} audio bytes confirmed)")
            path = part[:-len(".part")] + ".mp3"
            os.replace(part, path)
        except asyncio.CancelledError:
            _try_remove(part)
            with self._lock, contextlib.suppress(ValueError):
                self._live.remove(job)
            log.debug("Gen %d chunk %d cancelled mid-request", job.gen, job.chunk_id)
            raise
        except Exception as e:  # noqa: BLE001 - every failure is reported as chunkFailed
            _try_remove(part)
            kind = classify_error(e, disconnected=self._disconnected)
            detail = _describe(e, job.text)
            log.warning("Gen %d chunk %d failed (%s) after %.2f s: %s",
                        job.gen, job.chunk_id, kind, time.monotonic() - t0, detail)
            self._delivered.emit(_Result(job, kind=kind, detail=detail))
            return
        log.debug("Gen %d chunk %d ready: %d words, %d bytes, %.2f s",
                  job.gen, job.chunk_id, len(words), size, time.monotonic() - t0)
        self._delivered.emit(_Result(job, path=path, words=words))

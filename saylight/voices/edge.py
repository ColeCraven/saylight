"""Microsoft's online voices through edge-tts, with WordBoundary timing, on a persistent asyncio loop thread.

EdgeBackend is created on the GUI thread. Jobs run one at a time, in FIFO order, on a daemon "synth"
thread. Results are relayed back to the GUI thread before the public signals fire, so a cancel issued on
the GUI thread always wins over a result that has not been delivered yet.

It is also Saylight 1.0's SynthWorker (synth.py re-exports it): with no AudioCache given to start(), it
makes and owns one, and it still accepts edge-tts rate strings ("-5%") and bare voice names.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import re
import socket
import ssl
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

from ..audio_cache import AudioCache, _try_remove
from .base import EDGE, VoiceInfo, Word, parse_voice_id
from .catalog import CURATED

log = logging.getLogger("saylight.voices.edge")

CONNECT_TIMEOUT_S = 8
RECEIVE_TIMEOUT_S = 30
IDLE_TIMEOUT_S = 20.0      # no stream item for this long -> TimeoutError (a silently stalled connection)
LIST_TIMEOUT_S = 10.0      # the full voice list, fetched only when the person asks for it
QUOTE_RUN = 12             # an error message sharing this many characters with the text is withheld
FIRST_CHUNK_CHARS, CHUNK_CHARS = 250, 1500

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


def fetch_voice_list(timeout: float = LIST_TIMEOUT_S) -> list[dict]:
    """Microsoft's full online voice list (blocking; run it on a worker thread). Sends no text."""
    async def fetch() -> list[dict]:
        async with asyncio.timeout(timeout):
            return await edge_tts.list_voices()
    return asyncio.run(fetch())


def _describe(exc: BaseException, text: str) -> str:
    """Class name and message for chunkFailed, never the text being read or a file path."""
    name = type(exc).__name__
    if isinstance(exc, UnicodeError):
        return name
    msg = " ".join(_message(exc).split())[:300]
    if msg and _quotes(msg, text):
        msg = "(message withheld: it quoted the text)"
    return f"{name}: {msg}" if msg else name


def _message(exc: BaseException) -> str:
    """str(exc), except that an OSError about a file loses the path (it holds the user's profile folder)."""
    if isinstance(exc, OSError) and (exc.filename is not None or exc.filename2 is not None):
        winerror = getattr(exc, "winerror", None)
        code = f"[WinError {winerror}]" if winerror else f"[Errno {exc.errno}]" if exc.errno is not None else ""
        return " ".join(part for part in (code, exc.strerror or "") if part)
    return str(exc)


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


class EdgeBackend(QObject):
    """edge-tts synthesis, one request in flight, results as MP3 files plus word timings."""

    kind = EDGE
    audio_ext = ".mp3"

    chunkReady = Signal(int, int, str, object)    # gen, chunk_id, path, list[Word]
    chunkFailed = Signal(int, int, str, str)      # gen, chunk_id, kind, detail
    _delivered = Signal(object)                   # synth thread -> GUI thread relay (_Result)
    _watchRequested = Signal()                    # start() off the GUI thread -> _watch_network on it

    def __init__(self, parent: QObject | None = None, *, temp_root: str | None = None):
        super().__init__(parent)
        self._temp_root = temp_root
        self._lock = threading.Lock()
        self._pending: deque[_Job] = deque()
        self._live: list[_Job] = []               # submitted and not yet delivered or dropped
        self._current: _Job | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._cache: AudioCache | None = None
        self._owns_cache = False
        self._closed = False
        self._disconnected = False
        self._delivered.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._watchRequested.connect(self._watch_network, Qt.ConnectionType.QueuedConnection)

    @property
    def temp_dir(self) -> str | None:
        """The cache folder (None before start())."""
        return self._cache.dir if self._cache is not None else None

    # --- Public API (thread-safe unless noted) ---------------------------------------------

    def start(self, cache: AudioCache | None = None) -> None:
        """Start the loop thread, writing into `cache`. With none, make and own one (Saylight 1.0's way)."""
        with self._lock:
            if self._thread is not None or self._closed:
                return
            owns = cache is None
            if owns:
                cache = AudioCache(self._temp_root)
            if cache.dir is None:
                cache.start()                   # also purges folders left by earlier runs
            self._cache, self._owns_cache = cache, owns
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, name="synth", daemon=True)
        self._thread.start()
        if QThread.currentThread() is self.thread():
            self._watch_network()
        else:
            self._watchRequested.emit()         # QNetworkInformation belongs on the GUI thread
        self._post(self._pump)

    def ready(self) -> bool:
        """Online voices need no download."""
        return not self._closed

    def voices(self) -> list[VoiceInfo]:
        """The built-in English list; the full list needs the network (see VoiceCatalog)."""
        return list(CURATED)

    def chunk_limits(self) -> tuple[int, int]:
        return FIRST_CHUNK_CHARS, CHUNK_CHARS

    def warm(self) -> None:
        """Nothing to prepare."""

    def synthesize(self, gen: int, chunk_id: int, text: str, voice: str, speed: float | str) -> None:
        """Queue a chunk. `voice` is "edge:<name>" or a bare name; `speed` is a multiplier (0.95) or an
        edge-tts rate ("-5%")."""
        kind, name = parse_voice_id(voice)
        rate = speed if isinstance(speed, str) else rate_string(speed)
        job = _Job(gen, chunk_id, text, name, rate)
        with self._lock:
            if self._closed:
                return
            self._live.append(job)
            if kind != EDGE:                    # not ours: fail it, asynchronously like any result
                result = _Result(job, kind="unavailable", detail=f"not an online voice ({kind})")
            else:
                result = None
                self._pending.append(job)
            running = self._thread is not None
        if result is not None:
            self._delivered.emit(result)
            return
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
        """Delete a delivered chunk file through the cache (Saylight 1.0's SynthWorker.delete)."""
        if self._cache is None:
            log.warning("Refused to delete a file outside the audio cache folder")
            return
        self._cache.delete(path)

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel everything, stop and join the loop thread, and delete the cache folder if it is ours."""
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
        if self._owns_cache and self._cache is not None:
            self._cache.shutdown()

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

    async def _synth(self, job: _Job) -> None:
        part = ""
        words: list[Word] = []
        size = 0
        t0 = time.monotonic()
        try:
            # Inside the try: if the cache can't give a path, the job must still fail rather than vanish.
            part = self._cache.new_path(f"g{job.gen}_c{job.chunk_id}", ".part")
            text = _LONE_SURROGATE.sub(" ", job.text)  # same length; lone surrogates can't be UTF-8 encoded
            if not text.strip():
                raise ValueError("empty text")
            comm = edge_tts.Communicate(text, job.voice, rate=job.rate, boundary="WordBoundary",
                                        connect_timeout=CONNECT_TIMEOUT_S, receive_timeout=RECEIVE_TIMEOUT_S)
            loop = asyncio.get_running_loop()
            with self._cache.create(part) as f:
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
            path = part[:-len(".part")] + self.audio_ext
            os.replace(part, path)
        except asyncio.CancelledError:
            if part:
                _try_remove(part)
            with self._lock, contextlib.suppress(ValueError):
                self._live.remove(job)
            log.debug("Gen %d chunk %d cancelled mid-request", job.gen, job.chunk_id)
            raise
        except Exception as e:  # noqa: BLE001 - every failure is reported as chunkFailed
            if part:
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

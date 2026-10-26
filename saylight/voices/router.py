"""SynthRouter: the engine's synth. It has Saylight 1.0's SynthWorker interface and sends each chunk to the
backend its voice belongs to, through one shared AudioCache.

It can also fall back to the offline Windows voice when the online one can't be reached. The fallback
stays for the rest of the reading (until new_reading()), and sends no test traffic of its own. With no offline
backend registered, failures pass straight through to the engine. With "Always use the private voice" on,
the text never goes to the online voice: if no private voice is ready, the chunk fails as "unavailable".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..audio_cache import AudioCache
from .base import EDGE, WINDOWS, VoiceBackend, VoiceInfo, make_voice_id, parse_voice_id

log = logging.getLogger("saylight.voices.router")

DEFAULT_LIMITS = (250, 1500)          # the online voice's chunk sizes, for a voice with no backend
OFFLINE_ORDER = (WINDOWS,)            # offline voices, preferred first
PRIVATE = "private"                   # names the missing voice when "Always use the private voice" can't be met
_RATE = re.compile(r"[+-]?\d+(?:\.\d+)?%")


@dataclass(eq=False)
class _Route:
    kind: str                         # the backend now responsible for the job
    text: str                         # kept until the job is delivered, so a fallback can re-send it
    speed: float


def as_speed(rate: str | float) -> float:
    """A speed multiplier from an edge-tts rate string or a number: "-5%" -> 0.95, 1.25 -> 1.25."""
    if isinstance(rate, bool):
        raise ValueError("invalid rate")
    if isinstance(rate, (int, float)):
        return float(rate)
    if isinstance(rate, str) and _RATE.fullmatch(rate.strip()):
        return round(1 + float(rate.strip()[:-1]) / 100, 6)
    raise ValueError("invalid rate")


class SynthRouter(QObject):
    """Engine-facing; the same duck type as 1.0's SynthWorker. GUI thread, except start() (any thread)."""

    chunkReady = Signal(int, int, str, object)      # gen, chunk_id, path, list[Word]
    chunkFailed = Signal(int, int, str, str)        # gen, chunk_id, kind, detail
    backendChanged = Signal(str, str)               # kind, reason: "chosen" | "offline_fallback" | "service_fallback"
    _failLater = Signal(int, int, str, str)         # a failure found inside synthesize(), delivered like a result

    def __init__(self, backends: Mapping[str, VoiceBackend], cache: AudioCache, *, settings: Any = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._backends: dict[str, VoiceBackend] = dict(backends)
        self._cache = cache
        self._settings = settings
        self._jobs: dict[tuple[int, int], _Route] = {}
        self._current: str | None = None             # the kind that spoke last
        self._sticky: str | None = None              # the offline kind this reading fell back to
        self._reachable = True
        self._closed = False
        for kind, backend in self._backends.items():
            backend.chunkReady.connect(lambda g, c, p, w, k=kind: self._on_ready(k, g, c, p, w))
            backend.chunkFailed.connect(lambda g, c, f, d, k=kind: self._on_failed(k, g, c, f, d))
        self._failLater.connect(self._deliver_failure, Qt.ConnectionType.QueuedConnection)

    @property
    def temp_dir(self) -> str | None:
        """This run's cache folder (None before start())."""
        return self._cache.dir

    @property
    def cache(self) -> AudioCache:
        return self._cache

    def backend(self, kind: str) -> VoiceBackend | None:
        return self._backends.get(kind)

    # --- SynthWorker interface -----------------------------------------------------------

    def start(self) -> None:
        """Create the cache folder (purging earlier runs' folders) and start every backend."""
        if self._closed:
            return
        if self._cache.dir is None:
            self._cache.start()
        for backend in self._backends.values():
            backend.start(self._cache)

    def synthesize(self, gen: int, chunk_id: int, text: str, voice: str, rate: str | float) -> None:
        """Queue a chunk. `voice` is a voice ID or a bare 1.0 name; `rate` is a multiplier (0.95) or an
        edge-tts rate ("-5%")."""
        if self._closed:
            return
        try:
            speed = as_speed(rate)
        except ValueError:
            self._fail_soon(gen, chunk_id, EDGE, "other", "ValueError: invalid rate")
            return
        chosen, name = parse_voice_id(voice)
        kind = self._kind_for(chosen, commit=True)
        backend = self._backends.get(kind) if kind is not None else None
        if backend is None or not backend.ready():
            label = kind or PRIVATE
            self._fail_soon(gen, chunk_id, label, "unavailable", f"no {label} voice is available")
            return
        self._jobs[(gen, chunk_id)] = _Route(kind, text, speed)
        self._note(kind, "chosen")
        voice_id = make_voice_id(kind, name) if kind == chosen else self._offline_voice(kind)
        backend.synthesize(gen, chunk_id, text, voice_id, speed)

    def cancel_before(self, gen: int) -> None:
        """Cancel queued, in-flight and undelivered jobs with job.gen < gen. Emits nothing for them."""
        self._jobs = {key: route for key, route in self._jobs.items() if key[0] >= gen}
        for backend in self._backends.values():
            backend.cancel_before(gen)

    def cancel_all(self) -> None:
        """Cancel every queued, in-flight and undelivered job."""
        self._jobs.clear()
        for backend in self._backends.values():
            backend.cancel_all()

    def delete(self, path: str) -> None:
        """Delete a delivered chunk file. A file still open in MCI is retried for about 2 s."""
        self._cache.delete(path)

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop every backend, then delete the cache folder."""
        if self._closed:
            return
        self._closed = True
        self._jobs.clear()
        for backend in self._backends.values():
            backend.shutdown(timeout)
        self._cache.shutdown()

    # --- Voices and routing --------------------------------------------------------------

    def limits_for(self, voice: str) -> tuple[int, int]:
        """(first, later) chunk sizes in characters for the backend that would speak `voice` now."""
        kind = self._kind_for(parse_voice_id(voice)[0], commit=False)
        backend = self._backends.get(kind) if kind is not None else None
        return backend.chunk_limits() if backend is not None else DEFAULT_LIMITS

    def voices(self) -> list[VoiceInfo]:
        """Every backend's voices that need no network to list."""
        return [voice for backend in self._backends.values() for voice in backend.voices()]

    def set_reachable(self, reachable: bool) -> None:
        """The network state from Windows. When it drops, an offline voice is prepared."""
        self._reachable = bool(reachable)
        if not self._reachable and self._auto():
            kind = self._offline_kind()
            if kind is not None:
                self._backends[kind].warm()

    def new_reading(self) -> None:
        """A new reading starts on the chosen voice again."""
        self._sticky = None

    # --- Internals -----------------------------------------------------------------------

    def _kind_for(self, chosen: str, *, commit: bool) -> str | None:
        """The backend kind to use for a voice of kind `chosen`. A local voice is always used as chosen.

        None when the person asked for the private voice and none is ready: the text must not go online.
        """
        if chosen != EDGE:
            return chosen
        if self._sticky is not None:
            return self._sticky
        private = self._private_only()
        if private or (not self._reachable and self._auto()):
            kind = self._offline_kind()
            if kind is not None:
                if commit:
                    self._stick(kind, "chosen" if private else "offline_fallback")
                return kind
            if private:
                return None
        return EDGE

    def _offline_kind(self) -> str | None:
        for kind in OFFLINE_ORDER:
            backend = self._backends.get(kind)
            if backend is not None and backend.ready():
                return kind
        return None

    def _offline_voice(self, kind: str) -> str:
        """The voice an offline backend reads with when it stands in for the online voice."""
        chosen = self._setting("offline.voice", "")
        if chosen and parse_voice_id(chosen)[0] == kind:
            return chosen
        voices = self._backends[kind].voices()
        return voices[0].id if voices else make_voice_id(kind, "")

    def _auto(self) -> bool:
        return bool(self._setting("offline.fallback", True))

    def _private_only(self) -> bool:
        return bool(self._setting("offline.private_only", False))

    def _setting(self, key: str, default: Any) -> Any:
        if self._settings is None:
            return default
        try:
            return self._settings.get(key)
        except KeyError:     # the key arrives with the release that uses it
            return default

    def _stick(self, kind: str, reason: str) -> None:
        self._sticky = kind
        self._note(kind, reason)

    def _note(self, kind: str, reason: str) -> None:
        if kind != self._current:
            self._current = kind
            log.info("Speaking with the %s backend (%s)", kind, reason)
            self.backendChanged.emit(kind, reason)

    def _fail_soon(self, gen: int, chunk_id: int, kind: str, fail_kind: str, detail: str) -> None:
        """Report a failure asynchronously, like any backend result, so a cancel can still win over it."""
        self._jobs[(gen, chunk_id)] = _Route(kind, "", 1.0)
        log.warning("Gen %d chunk %d failed (%s): %s", gen, chunk_id, fail_kind, detail)
        self._failLater.emit(gen, chunk_id, fail_kind, detail)

    @Slot(int, int, str, str)
    def _deliver_failure(self, gen: int, chunk_id: int, fail_kind: str, detail: str) -> None:
        if self._closed or self._jobs.pop((gen, chunk_id), None) is None:
            return
        self.chunkFailed.emit(gen, chunk_id, fail_kind, detail)

    def _on_ready(self, kind: str, gen: int, chunk_id: int, path: str, words: object) -> None:
        key = (gen, chunk_id)
        route = self._jobs.get(key)
        if self._closed or (route is not None and route.kind != kind):
            self._cache.delete(path)      # a late result from a backend this job moved away from
            return
        self._jobs.pop(key, None)
        self.chunkReady.emit(gen, chunk_id, path, words)

    def _on_failed(self, kind: str, gen: int, chunk_id: int, fail_kind: str, detail: str) -> None:
        key = (gen, chunk_id)
        route = self._jobs.get(key)
        if self._closed or (route is not None and route.kind != kind):
            return
        if route is not None and kind == EDGE and fail_kind in ("offline", "service") and self._auto():
            offline = self._offline_kind()
            if offline is not None:
                self._stick(offline, "offline_fallback" if fail_kind == "offline" else "service_fallback")
                route.kind = offline
                self._backends[offline].synthesize(gen, chunk_id, route.text, self._offline_voice(offline),
                                                   route.speed)
                return
        self._jobs.pop(key, None)
        self.chunkFailed.emit(gen, chunk_id, fail_kind, detail)

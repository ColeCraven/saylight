"""Voice preview: reads a fixed sample sentence in a chosen voice, never the person's text.

It has its own backend per voice kind, its own PlayerThread (MCI gives each load its own device, so it can
play beside the reader's) and an AudioCache. Each sample file is deleted once it has played.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from ..audio_cache import AudioCache
from .base import VoiceBackend, parse_voice_id

log = logging.getLogger("saylight.voices.preview")

SAMPLE_TEXT = "This is how I sound. I can read your papers aloud and highlight each word."
STATES = ("idle", "loading", "playing", "failed")


class VoicePreview(QObject):
    """Plays one sample at a time; a new play() replaces the last. GUI thread only."""

    stateChanged = Signal(str)                          # "idle" | "loading" | "playing" | "failed"

    def __init__(self, backend_factory: Callable[[str], VoiceBackend | None], volume: Callable[[], int],
                 parent: QObject | None = None, *, cache: AudioCache | None = None, player=None):
        super().__init__(parent)
        self._factory = backend_factory
        self._volume = volume
        self._cache = cache
        self._owns_cache = cache is None
        self._player = player
        self._owns_player = player is None
        self._backends: dict[str, VoiceBackend] = {}
        self._files: dict[int, str] = {}                # preview number -> its sample file, until played
        self._wired = False                             # player signals connected
        self._gen = 0
        self._state = "idle"
        self._closed = False

    @property
    def state(self) -> str:
        return self._state

    def play(self, voice: str, speed: float, text: str = SAMPLE_TEXT) -> None:
        """Synthesise `text` in `voice` at `speed`, then play it at the current volume."""
        if self._closed:
            return
        self._stop_current()
        self._gen += 1
        kind = parse_voice_id(voice)[0]
        backend = self._backend(kind)
        if backend is None or not backend.ready():
            log.info("Preview unavailable: no %s backend", kind)
            self._set_state("failed")
            return
        self._set_state("loading")
        backend.synthesize(self._gen, 0, text, voice, speed)

    def stop(self) -> None:
        """Stop the sample, or drop it if it is still being made."""
        self._stop_current()
        self._gen += 1
        self._set_state("idle")

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop, then shut down the backends, the player and the cache made for previews."""
        if self._closed:
            return
        self._stop_current()
        self._closed = True
        for backend in self._backends.values():
            backend.shutdown(timeout)
        if self._player is not None and self._owns_player:
            self._player.shutdown(timeout)
        for gen in list(self._files):
            self._drop_file(gen)
        if self._cache is not None and self._owns_cache:
            self._cache.shutdown()

    # --- Internals -----------------------------------------------------------------------

    def _backend(self, kind: str) -> VoiceBackend | None:
        backend = self._backends.get(kind)
        if backend is not None:
            return backend
        try:
            backend = self._factory(kind)
        except Exception:  # noqa: BLE001 - a backend that can't be made just can't preview
            log.warning("Couldn't create the %s backend for previews", kind, exc_info=True)
            backend = None
        if backend is None:
            return None
        if self._cache is None:
            self._cache = AudioCache()
        if self._cache.dir is None:
            self._cache.start()
        backend.start(self._cache)
        backend.chunkReady.connect(self._on_ready)
        backend.chunkFailed.connect(self._on_failed)
        self._backends[kind] = backend
        return backend

    def _ensure_player(self):
        if self._player is None:
            from ..player import PlayerThread
            self._player = PlayerThread(self, volume=self._current_volume())
        if not self._wired:
            self._wired = True
            self._player.started.connect(self._on_started)
            self._player.ended.connect(self._on_ended)
            self._player.failed.connect(self._on_play_failed)
            self._player.start()
        return self._player

    def _current_volume(self) -> int:
        try:
            return int(self._volume())
        except Exception:  # noqa: BLE001 - a broken volume source must never make the preview loud
            return 0

    def _stop_current(self) -> None:
        for backend in self._backends.values():
            backend.cancel_all()
        if self._gen in self._files and self._player is not None:
            self._player.stop_close()                   # ended(gen, "stopped") then deletes the file

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.stateChanged.emit(state)

    @Slot(int, int, str, object)
    def _on_ready(self, gen: int, _chunk_id: int, path: str, _words: object) -> None:
        if self._closed or gen != self._gen:
            self._cache.delete(path)
            return
        player = self._ensure_player()
        self._files[gen] = path
        player.set_volume(self._current_volume())
        player.load(gen, path, play=True)

    @Slot(int, int, str, str)
    def _on_failed(self, gen: int, _chunk_id: int, kind: str, _detail: str) -> None:
        if gen == self._gen and not self._closed:
            log.info("Preview failed (%s)", kind)
            self._set_state("failed")

    @Slot(int, int)
    def _on_started(self, gen: int, _length_ms: int) -> None:
        if gen == self._gen and not self._closed:
            self._set_state("playing")

    @Slot(int, str)
    def _on_ended(self, gen: int, _why: str) -> None:
        self._drop_file(gen)
        if gen == self._gen and not self._closed:
            self._set_state("idle")

    @Slot(int, str)
    def _on_play_failed(self, gen: int, _error: str) -> None:
        self._drop_file(gen)
        if gen == self._gen and not self._closed:
            log.info("Preview playback failed")
            self._set_state("failed")

    def _drop_file(self, gen: int) -> None:
        path = self._files.pop(gen, None)
        if path is not None and self._cache is not None:
            self._cache.delete(path)

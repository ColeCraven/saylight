"""Windows' built-in voices through WinRT's SpeechSynthesizer: private and offline, with exact word timing.

WindowsVoiceBackend is created on the GUI thread. Jobs run one at a time, in FIFO order, on a daemon
"winvoice" thread that owns the synthesizer, because blocking WinRT calls can't run on the GUI thread.
Results are relayed back to the GUI thread before the public signals fire, so a cancel issued there always
wins over a result that has not been delivered yet. A chunk already being synthesized finishes (it takes
well under a second) and is then dropped.

Each chunk is a 16 kHz mono WAV, written through the AudioCache. Nothing leaves this PC. winrt is loaded
through winrt_support, on the worker thread, when the backend starts.

Word timing comes from the SpeechWord track (IncludeWordBoundaryMetadata). Its cues have no duration, so a
word lasts until the next one starts. Their positions are UTF-16 offsets with an inclusive end, and a number
is cued once per spoken part, every time at the same position.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import threading
import time
import wave
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..audio_cache import AudioCache, _try_remove
from .base import WINDOWS, VoiceInfo, Word, make_voice_id, parse_voice_id
from .catalog import LOCALES, WINDOWS_DETAIL

log = logging.getLogger("saylight.voices.windows")

FIRST_CHUNK_CHARS, CHUNK_CHARS = 250, 1500
MIN_RATE, MAX_RATE = 0.5, 6.0          # SpeakingRate's range
WORD_TRACK = "SpeechWord"
VENDOR = "Microsoft "                  # "Microsoft Zira" is listed as "Zira"
_GENDERS = {0: "Male", 1: "Female"}    # VoiceGender
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

# Cue: (start ms, the cue's text, first and last UTF-16 position in the input, both inclusive)
Cue = tuple[float, str, int, int]


class NoSuchVoice(LookupError):
    """The chosen Windows voice isn't installed on this PC."""


@dataclass(eq=False)
class _Job:
    gen: int
    chunk_id: int
    text: str
    voice: str                        # the name part of the voice ID; "" for Windows' default voice
    speed: float
    cancelled: bool = False


@dataclass(eq=False)
class _Result:
    job: _Job
    path: str | None = None
    words: list[Word] | None = None
    kind: str = ""
    detail: str = ""


def voice_key(token_id: str) -> str:
    """The name part of a Windows voice's ID: the last part of its registry token, e.g. "MSTTS_V110_enUS_ZiraM"."""
    return str(token_id).rstrip("\\").rsplit("\\", 1)[-1].strip()


def describe_voice(token_id: str, display_name: str, language: str, gender: int) -> VoiceInfo:
    """A VoiceInfo for one of Windows' voices."""
    name = str(display_name).strip()
    if name.startswith(VENDOR) and len(name) > len(VENDOR):
        name = name[len(VENDOR):]
    return VoiceInfo(id=make_voice_id(WINDOWS, voice_key(token_id)), backend=WINDOWS, name=name,
                     locale=str(language), gender=_GENDERS.get(int(gender), ""), private=True, word_timing=True,
                     detail=WINDOWS_DETAIL)


def sort_voices(voices: Iterable[VoiceInfo]) -> list[VoiceInfo]:
    """English accents in the online list's order, then other languages, each by name."""
    rank = {locale.lower(): i for i, locale in enumerate(LOCALES)}
    return sorted(voices, key=lambda v: (rank.get(v.locale.lower(), len(rank)), v.locale.lower(), v.name.lower()))


def cue_words(text: str, cues: Iterable[Cue], length_ms: float) -> list[Word]:
    """Words from SpeechWord cues, in ms from the start of the audio.

    Repeated cues at one position (a number, once per spoken part) count once. A word lasts until the next
    one starts, and the last one until the audio ends. Its text is the input it points at.
    """
    kept: list[Cue] = []
    for cue in cues:
        if kept and cue[2:] == kept[-1][2:]:
            continue
        kept.append(cue)
    index = _utf16_index(text)
    units = len(index) - 1
    words: list[Word] = []
    for i, (start, said, first, last) in enumerate(kept):
        end = kept[i + 1][0] if i + 1 < len(kept) else length_ms
        if 0 <= first <= last < units:
            said = text[index[first]:index[last] + 1] or said
        words.append(Word(start, max(0.0, end - start), said))
    return words


def wav_length_ms(data: bytes) -> float:
    """The length of a PCM WAV held in memory. Raises wave.Error or EOFError if it isn't one."""
    with wave.open(io.BytesIO(data), "rb") as w:
        rate = w.getframerate()
        if rate <= 0:
            raise wave.Error("no frame rate")
        return w.getnframes() * 1000 / rate


def speaking_rate(speed: float) -> float:
    """SpeakingRate for a speed multiplier. It scales the audio's length exactly by 1/rate."""
    return min(MAX_RATE, max(MIN_RATE, float(speed)))


def _utf16_index(text: str) -> list[int]:
    """For each UTF-16 code unit of `text`, the index of its character; then len(text)."""
    index: list[int] = []
    for i, ch in enumerate(text):
        index.append(i)
        if ord(ch) > 0xFFFF:
            index.append(i)
    index.append(len(text))
    return index


def _describe(exc: BaseException) -> str:
    """The error class (and a Windows error code), never a message: it could quote the text or a path."""
    code = getattr(exc, "winerror", None)
    return f"{type(exc).__name__} [WinError {code}]" if isinstance(code, int) else type(exc).__name__


class _Speech:
    """The WinRT side, used only on the worker thread: the voice list, and one synthesizer made on first use."""

    def __init__(self, load: Callable[[str], ModuleType], volume: float):
        self._ss = load("media.speechsynthesis")
        self._core = load("media.core")
        self._streams = load("storage.streams")
        self._volume = volume
        self._synth: Any = None
        self._installed = {voice_key(v.id).lower(): v for v in self._ss.SpeechSynthesizer.all_voices}

    def voices(self) -> list[VoiceInfo]:
        return sort_voices(describe_voice(v.id, v.display_name, v.language, v.gender)
                           for v in self._installed.values())

    def warm(self) -> None:
        if self._synth is None:
            self._synth = self._ss.SpeechSynthesizer()

    def synthesize(self, text: str, voice: str, speed: float) -> tuple[bytes, list[Cue]]:
        """The chunk's WAV bytes and its word cues. Blocking."""
        if voice:
            chosen = self._installed.get(voice.lower())
            if chosen is None:
                raise NoSuchVoice("that Windows voice isn't installed")
        else:
            chosen = self._ss.SpeechSynthesizer.default_voice
        self.warm()
        synth = self._synth
        synth.voice = chosen
        options = synth.options
        options.include_word_boundary_metadata = True
        options.speaking_rate = speaking_rate(speed)
        options.audio_volume = self._volume
        stream = synth.synthesize_text_to_stream_async(text).get()
        size = int(stream.size)
        reader = self._streams.DataReader(stream.get_input_stream_at(0))
        reader.load_async(size).get()
        data = bytearray(size)
        reader.read_bytes(data)
        cues: list[Cue] = []
        for track in stream.timed_metadata_tracks:
            if track.id != WORD_TRACK:
                continue
            for cue in track.cues:
                c = cue.as_(self._core.SpeechCue)
                cues.append((c.start_time.total_seconds() * 1000, c.text or "",
                             int(c.start_position_in_input), int(c.end_position_in_input)))
        return bytes(data), cues

    def close(self) -> None:
        synth, self._synth = self._synth, None
        if synth is not None:
            with contextlib.suppress(Exception):
                synth.close()


class WindowsVoiceBackend(QObject):
    """Windows' voices, one chunk at a time on a worker thread, as WAV files plus word timings."""

    kind = WINDOWS
    audio_ext = ".wav"

    chunkReady = Signal(int, int, str, object)    # gen, chunk_id, path, list[Word]
    chunkFailed = Signal(int, int, str, str)      # gen, chunk_id, kind, detail
    _delivered = Signal(object)                   # worker thread -> GUI thread relay (_Result)

    def __init__(self, parent: QObject | None = None, *, load: Callable[[str], ModuleType] | None = None,
                 volume: float = 1.0):
        """`load` imports a winrt namespace (winrt_support.load). `volume` is the synthesizer's own (0 to 1),
        separate from the player's: tests make silent audio with 0."""
        super().__init__(parent)
        self._load = load
        self._volume = min(1.0, max(0.0, float(volume)))
        self._cond = threading.Condition()
        self._pending: deque[_Job] = deque()
        self._live: list[_Job] = []               # submitted and not yet delivered or dropped
        self._thread: threading.Thread | None = None
        self._cache: AudioCache | None = None
        self._voices: list[VoiceInfo] = []
        self._state = "new"                       # "new" | "loading" | "ready" | "missing"
        self._warm = False
        self._closed = False
        self._delivered.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    # --- Public API (thread-safe unless noted) ---------------------------------------------

    def start(self, cache: AudioCache) -> None:
        """Start the worker thread, which loads winrt and lists the voices, then writes chunks into `cache`."""
        with self._cond:
            if self._thread is not None or self._closed:
                return
            if cache.dir is None:
                cache.start()
            self._cache = cache
            self._state = "loading"
            self._thread = threading.Thread(target=self._run, name="winvoice", daemon=True)
        self._thread.start()

    def ready(self) -> bool:
        """Windows' voices need no download. False once it's known that this PC can't use them."""
        with self._cond:
            return not self._closed and self._state != "missing"

    def voices(self) -> list[VoiceInfo]:
        """The voices installed in Windows (empty until the worker has listed them)."""
        with self._cond:
            return list(self._voices)

    def chunk_limits(self) -> tuple[int, int]:
        return FIRST_CHUNK_CHARS, CHUNK_CHARS

    def warm(self) -> None:
        """Make the synthesizer now rather than with the first chunk."""
        with self._cond:
            self._warm = True
            self._cond.notify_all()

    def synthesize(self, gen: int, chunk_id: int, text: str, voice: str, speed: float) -> None:
        """Queue a chunk. `voice` is "windows:<name>" ("windows:" alone is Windows' default voice); `speed` is
        a multiplier."""
        kind, name = parse_voice_id(voice)
        job = _Job(gen, chunk_id, text, name, float(speed))
        with self._cond:
            if self._closed:
                return
            self._live.append(job)
            if kind != WINDOWS:                 # not ours: fail it, asynchronously like any result
                result = _Result(job, kind="unavailable", detail=f"not a Windows voice ({kind})")
            else:
                result = None
                self._pending.append(job)
                self._cond.notify_all()
        if result is not None:
            self._delivered.emit(result)
            return
        log.debug("Queued gen %d chunk %d (%d chars, speed %.2f)", gen, chunk_id, len(text), job.speed)

    def cancel_before(self, gen: int) -> None:
        """Cancel queued, in-flight and undelivered jobs with job.gen < gen. Emits nothing for them."""
        self._cancel(lambda job: job.gen < gen)

    def cancel_all(self) -> None:
        """Cancel every queued, in-flight and undelivered job."""
        self._cancel(lambda job: True)

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel everything and stop the worker thread. The cache belongs to the caller."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            for job in self._live:
                job.cancelled = True
            self._pending.clear()
            self._live.clear()
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Windows voice thread did not stop within %.1f s", timeout)

    # --- GUI thread ----------------------------------------------------------------------

    @Slot(object)
    def _deliver(self, res: _Result) -> None:
        job = res.job
        with self._cond:
            with contextlib.suppress(ValueError):
                self._live.remove(job)
            drop = job.cancelled or self._closed
        if drop:
            if res.path and self._cache is not None:
                self._cache.delete(res.path)
            return
        if res.path is not None:
            self.chunkReady.emit(job.gen, job.chunk_id, res.path, res.words)
        else:
            self.chunkFailed.emit(job.gen, job.chunk_id, res.kind, res.detail)

    # --- Any thread ----------------------------------------------------------------------

    def _cancel(self, match: Callable[[_Job], bool]) -> None:
        with self._cond:
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
        log.debug("Cancelled %d job(s)", hit)

    def _emit(self, res: _Result) -> None:
        try:
            self._delivered.emit(res)
        except RuntimeError:     # the backend was deleted while the chunk was made
            pass

    # --- Worker thread -------------------------------------------------------------------

    def _run(self) -> None:
        speech = self._open()
        try:
            while True:
                with self._cond:
                    while not self._closed and not self._pending and not (self._warm and speech is not None):
                        self._cond.wait()
                    if self._closed:
                        return
                    warm, self._warm = self._warm, False
                    job = self._pending.popleft() if self._pending else None
                if speech is not None and warm:
                    with contextlib.suppress(Exception):
                        speech.warm()
                if job is not None:
                    self._emit(self._synth(speech, job))
        except Exception:  # noqa: BLE001 - keep the thread's end orderly
            log.exception("Windows voice thread failed")
        finally:
            if speech is not None:
                speech.close()

    def _open(self) -> _Speech | None:
        t0 = time.monotonic()
        try:
            load = self._load
            if load is None:
                from ..winrt_support import load
            speech = _Speech(load, self._volume)
            voices = speech.voices()
        except Exception as e:  # noqa: BLE001 - no winrt or no speech here: the backend is just unavailable
            log.warning("Windows voices unavailable (%s)", _describe(e))
            with self._cond:
                self._state = "missing"
            return None
        with self._cond:
            self._voices, self._state = voices, "ready"
        log.info("Found %d Windows voice(s) in %.2f s", len(voices), time.monotonic() - t0)
        return speech

    def _synth(self, speech: _Speech | None, job: _Job) -> _Result:
        if job.cancelled:
            return _Result(job)
        if speech is None:
            return _Result(job, kind="unavailable", detail="Windows voices aren't available")
        path = ""
        t0 = time.monotonic()
        try:
            text = _LONE_SURROGATE.sub(" ", job.text)   # same length; WinRT strings can't hold lone surrogates
            if not text.strip():
                raise ValueError("empty text")
            data, cues = speech.synthesize(text, job.voice, job.speed)
            if job.cancelled:
                return _Result(job)
            words = cue_words(text, cues, wav_length_ms(data))
            path = self._cache.new_path(f"g{job.gen}_c{job.chunk_id}", self.audio_ext)
            with self._cache.create(path) as f:
                f.write(data)
        except NoSuchVoice:
            log.warning("Gen %d chunk %d failed: the chosen Windows voice isn't installed", job.gen, job.chunk_id)
            return _Result(job, kind="unavailable", detail="that Windows voice isn't installed")
        except Exception as e:  # noqa: BLE001 - every failure is reported as chunkFailed
            if path:
                _try_remove(path)
            detail = _describe(e)
            log.warning("Gen %d chunk %d failed (other) after %.2f s: %s",
                        job.gen, job.chunk_id, time.monotonic() - t0, detail)
            return _Result(job, kind="other", detail=detail)
        log.debug("Gen %d chunk %d ready: %d words, %d bytes, %.2f s",
                  job.gen, job.chunk_id, len(words), len(data), time.monotonic() - t0)
        return _Result(job, path=path, words=words)

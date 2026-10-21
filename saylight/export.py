"""Save as audio: the one command that writes audio outside the run's temporary folder, and only to the path the
person picked in the Save dialog.

AudioExporter has its own voice backend and its own temporary folder, so it never disturbs a reading. It sends
the chunks in order, appends each one to "<target>.part" as it arrives and deletes the chunk file at once, then
renames the .part over the target when the last chunk is in. Cancelling or a failure deletes the .part, so no
file is left; an existing file at the target survives until the rename.

Online voices give MP3: the chunks are 48 kbit/s constant-bitrate streams with no tags, so joined end to end
they make one stream (Windows reports exactly the sum of the chunks' lengths). Other voices give WAV, joined
frame by frame under one header. One file always uses one voice; a chunk that fails stops the save, and Try
again runs it again from the start with the same path.

Logs hold counts, sizes and times only: never text, titles or paths.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import math
import os
import re
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .academic_text import chunk_sentences, chunk_text
from .audio_cache import AudioCache, _try_remove
from .voices.base import EDGE, VoiceBackend, parse_voice_id

log = logging.getLogger("saylight.export")

FAIL_KINDS = ("offline", "service", "io", "other")
FAIL_DETAILS = {"offline": "No internet connection",
                "service": "The voice service stopped answering",
                "io": "Couldn't write the file",
                "other": "Something went wrong. See saylight.log"}
WORDS_PER_MIN = 142        # spoken words a minute at 1x
MB_PER_MIN = {EDGE: 0.36, "windows": 1.92, "piper": 2.65}
IN_FLIGHT = 2              # chunks asked for ahead, so the voice is never idle and few files wait
ETA_AFTER = 3              # chunks written before a time left is offered
PART_SUFFIX = ".part"
NAME_LIMIT = 120           # characters of a title kept in a file name

_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$")


# --- What a save will be -------------------------------------------------------------------------

def export_format(voice: str) -> tuple[str, str]:
    """(".mp3", "MP3 audio") for an online voice, else (".wav", "WAV audio")."""
    return (".mp3", "MP3 audio") if parse_voice_id(voice)[0] == EDGE else (".wav", "WAV audio")


def _say(sentence) -> str:
    return getattr(sentence, "say", "") or sentence.text


def spoken_words(doc, first: int = 0, last: int | None = None) -> int:
    """How many words the voice will say from sentence `first` to `last` (inclusive; None is the end)."""
    stop = len(doc.sentences) if last is None else last + 1
    return sum(len(_say(s).split()) for s in doc.sentences[first:stop])


def estimate(words: int, speed: float, voice: str) -> tuple[float, float]:
    """(minutes, megabytes) for `words` spoken words at `speed` in `voice`."""
    minutes = words / (WORDS_PER_MIN * max(0.1, float(speed)))
    return minutes, minutes * MB_PER_MIN.get(parse_voice_id(voice)[0], MB_PER_MIN["windows"])


def _minutes_label(minutes: float) -> str:
    if minutes < 1:
        return "less than 1 min"
    m = round(minutes)
    if m < 60:
        return f"about {m} min"
    h, m = divmod(m, 60)
    return f"about {h} h {m} min" if m else f"about {h} h"


def _size_label(mb: float) -> str:
    if mb < 1:
        return "less than 1 MB"
    if mb >= 1000:
        return f"about {mb / 1000:.1f} GB"
    return f"about {round(mb)} MB"


def file_label(words: int, speed: float, voice: str) -> str:
    """The Save sheet's File row: "MP3 · about 38 min · about 14 MB"."""
    minutes, mb = estimate(words, speed, voice)
    kind = export_format(voice)[1].split()[0]
    return f"{kind} · {_minutes_label(minutes)} · {_size_label(mb)}"


def default_name(title: str, ext: str, folder: str, today: _dt.date | None = None) -> str:
    """The Save dialog's suggested file name. A file's title gives "{title}.mp3" (Windows asks before replacing
    one). Selected or typed text gets "Saylight audio 2026-10-20.mp3", made unique with " (2)", " (3)"; it is
    never made from the text itself."""
    name = _BAD_NAME_CHARS.sub(" ", title or "")
    name = " ".join(name.split())[:NAME_LIMIT].rstrip(" .")
    if name and not _RESERVED.match(name):
        return name + ext
    base = f"Saylight audio {(today or _dt.date.today()).isoformat()}"
    candidate, n = base + ext, 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate, n = f"{base} ({n}){ext}", n + 1
    return candidate


def plan(doc, first: int, last: int | None, limit: int) -> list[str]:
    """The chunk texts for sentences `first` to `last`: the voice's own text for each, grouped as for reading."""
    stop = len(doc.sentences) if last is None else last + 1
    texts = [t for t in (" ".join(_say(s).split()) for s in doc.sentences[first:stop]) if t]
    return [chunk_text(texts, group) for group in chunk_sentences(texts, first_limit=limit, limit=limit)]


def _strip_id3(data: bytes) -> bytes:
    """An MP3 chunk without a leading ID3v2 tag (a tag in the middle of a stream confuses some players)."""
    if len(data) >= 10 and data[:3] == b"ID3":
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        return data[10 + size + (10 if data[5] & 0x10 else 0):]
    return data


# --- The exporter --------------------------------------------------------------------------------

@dataclass
class _Request:
    doc: object
    target: str
    voice: str
    speed: float
    first: int
    last: int | None


class _Failure(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(kind)
        self.kind, self.detail = kind, detail


class AudioExporter(QObject):
    """One save at a time. GUI thread only; every signal fires on the GUI thread, never inside start()."""

    progress = Signal(int, int)        # chunks done, total
    finished = Signal(str)             # the final path
    failed = Signal(str, str)          # kind (FAIL_KINDS), detail: an error class, never text

    def __init__(self, backend_factory: Callable[[str], VoiceBackend | None], parent: QObject | None = None, *,
                 cache: AudioCache | None = None):
        super().__init__(parent)
        self._factory = backend_factory
        self._cache = cache
        self._owns_cache = cache is None
        self._backends: dict[str, VoiceBackend] = {}
        self._request: _Request | None = None    # kept after a failure for Try again; text stays in memory only
        self._gen = 0
        self._busy = False
        self._closed = False
        self._chunks: list[str] = []
        self._next_ask = 0                       # next chunk to ask the voice for
        self._next_write = 0                     # next chunk to append to the file
        self._ready: dict[int, str] = {}         # chunk files that arrived ahead of their turn
        self._file = None
        self._wav: wave.Wave_write | None = None
        self._wav_params: tuple | None = None
        self._written = 0                        # characters of text in the file so far
        self._total_chars = 0
        self._started_at = 0.0

    # --- Public API ----------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def can_retry(self) -> bool:
        return not self._busy and self._request is not None

    @property
    def target(self) -> str:
        """Where the current or last save goes ("" when there is none)."""
        return self._request.target if self._request else ""

    def fraction(self) -> float:
        """How much of the current save is done, 0-1, by characters of text."""
        return self._written / self._total_chars if self._total_chars else 0.0

    def remaining_s(self) -> float | None:
        """About how many seconds are left, once a few chunks have been written; else None."""
        if not self._busy or self._next_write < ETA_AFTER or not self._written:
            return None
        elapsed = time.monotonic() - self._started_at
        return elapsed * (self._total_chars - self._written) / self._written

    def start(self, doc, target: str, *, voice: str, speed: float, first: int = 0, last: int | None = None) -> None:
        """Save sentences `first` to `last` (inclusive; None is the end) of `doc` to `target`, a path the person
        picked. RuntimeError if a save is already running."""
        if self._busy:
            raise RuntimeError("a save is already running")
        if self._closed:
            return
        self._request = _Request(doc, os.path.abspath(target), voice, float(speed), max(0, first), last)
        self._run()

    def retry(self) -> bool:
        """Run the last failed save again, from the start and to the same path. False if there is none."""
        if not self.can_retry or self._closed:
            return False
        self._run()
        return True

    def cancel(self) -> None:
        """Stop the save and delete the unfinished file. With no save running, forget the failed one."""
        if not self._busy:
            self._request = None
            return
        log.info("Export cancelled")
        self._stop()
        self._request = None

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel any save (its .part is deleted), then stop the voice and delete the temporary folder."""
        if self._closed:
            return
        self._closed = True
        if self._busy:
            log.info("Export cancelled")
            self._stop()
        self._request = None
        for backend in self._backends.values():
            backend.shutdown(timeout)
        self._backends.clear()
        if self._cache is not None and self._owns_cache:
            self._cache.shutdown()

    # --- Running -------------------------------------------------------------------------------

    def _run(self) -> None:
        req = self._request
        self._gen += 1
        self._busy = True
        self._started_at = time.monotonic()
        self._next_ask = self._next_write = self._written = 0
        self._ready.clear()
        self._wav_params = None
        try:
            backend = self._backend(parse_voice_id(req.voice)[0])
            self._chunks = plan(req.doc, req.first, req.last, backend.chunk_limits()[1])
            if not self._chunks:
                raise _Failure("other", "nothing to say")
            self._total_chars = sum(len(c) for c in self._chunks)
            self._file = open(req.target + PART_SUFFIX, "wb")  # noqa: SIM115 - closed in _close_part
        except _Failure as f:
            self._fail_later(f.kind, f.detail)
            return
        except OSError as e:
            self._fail_later("io", type(e).__name__)
            return
        log.info("Export started: %s, %d sentences, %d chunks", export_format(req.voice)[0][1:],
                 (len(req.doc.sentences) if req.last is None else req.last + 1) - req.first, len(self._chunks))
        self._ask()

    def _backend(self, kind: str) -> VoiceBackend:
        backend = self._backends.get(kind)
        if backend is None:
            try:
                backend = self._factory(kind)
            except Exception:  # noqa: BLE001 - a backend that can't be made just can't save
                log.warning("Couldn't create the %s backend for saving", kind, exc_info=True)
                backend = None
            if backend is None:
                raise _Failure("other", "no voice")
            if self._cache is None:
                self._cache = AudioCache()
            if self._cache.dir is None:
                self._cache.start()
            backend.start(self._cache)
            backend.chunkReady.connect(self._on_ready)
            backend.chunkFailed.connect(self._on_failed)
            self._backends[kind] = backend
        if not backend.ready():
            raise _Failure("other", "voice not ready")
        return backend

    def _ask(self) -> None:
        req = self._request
        backend = self._backends[parse_voice_id(req.voice)[0]]
        while self._next_ask < len(self._chunks) and self._next_ask - self._next_write < IN_FLIGHT:
            backend.synthesize(self._gen, self._next_ask, self._chunks[self._next_ask], req.voice, req.speed)
            self._next_ask += 1

    @Slot(int, int, str, object)
    def _on_ready(self, gen: int, chunk_id: int, path: str, _words: object) -> None:
        if gen != self._gen or not self._busy:
            self._delete_chunk(path)
            return
        self._ready[chunk_id] = path
        try:
            while self._next_write in self._ready:
                chunk = self._ready.pop(self._next_write)
                try:
                    self._append(chunk)
                finally:
                    self._delete_chunk(chunk)
                self._written += len(self._chunks[self._next_write])
                self._next_write += 1
                self.progress.emit(self._next_write, len(self._chunks))
                if gen != self._gen or not self._busy:
                    return  # cancelled from a progress slot
            if self._next_write == len(self._chunks):
                self._finish()
            else:
                self._ask()
        except _Failure as f:
            self._fail(f.kind, f.detail)
        except OSError as e:
            self._fail("io", type(e).__name__)

    @Slot(int, int, str, str)
    def _on_failed(self, gen: int, _chunk_id: int, kind: str, _detail: str) -> None:
        if gen != self._gen or not self._busy:
            return
        self._fail(kind if kind in ("offline", "service") else "other", kind)

    def _append(self, chunk: str) -> None:
        if chunk.lower().endswith(".wav"):
            self._append_wav(chunk)
            return
        with open(chunk, "rb") as f:
            self._file.write(_strip_id3(f.read()))

    def _append_wav(self, chunk: str) -> None:
        try:
            with wave.open(chunk, "rb") as w:
                params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                frames = w.readframes(w.getnframes())
        except (wave.Error, EOFError) as e:
            raise _Failure("other", type(e).__name__) from e
        if self._wav is None:
            self._wav = wave.open(self._file, "wb")
            self._wav.setnchannels(params[0])
            self._wav.setsampwidth(params[1])
            self._wav.setframerate(params[2])
            self._wav_params = params
        elif params != self._wav_params:
            raise _Failure("other", "mixed formats")
        self._wav.writeframes(frames)

    def _finish(self) -> None:
        req = self._request
        part = req.target + PART_SUFFIX
        self._close_part()
        try:
            size = os.path.getsize(part)
            os.replace(part, req.target)
        except OSError as e:
            self._fail("io", type(e).__name__)
            return
        self._busy = False
        self._request = None
        self._ready.clear()
        log.info("Export finished: %.1f MB, %d s", size / 1e6, round(time.monotonic() - self._started_at))
        self.finished.emit(req.target)

    def _fail(self, kind: str, detail: str) -> None:
        log.info("Export failed: %s", kind)
        self._stop()
        self.failed.emit(kind, detail)

    def _fail_later(self, kind: str, detail: str) -> None:
        """A failure found inside start(): delivered from the event loop, like any other result."""
        self._stop()
        gen = self._gen

        def deliver() -> None:
            if gen == self._gen and not self._closed:
                log.info("Export failed: %s", kind)
                self.failed.emit(kind, detail)
        QTimer.singleShot(0, self, deliver)

    def _stop(self) -> None:
        """End the running save and delete everything it made: queued chunks, chunk files and the .part."""
        self._busy = False
        for backend in self._backends.values():
            backend.cancel_all()
        for path in self._ready.values():
            self._delete_chunk(path)
        self._ready.clear()
        self._close_part()
        if self._request is not None:
            self._delete_part(self._request.target + PART_SUFFIX)

    def _delete_part(self, part: str, left: int = 8) -> None:
        """Delete an unfinished file; one a virus scanner holds for a moment is tried again shortly."""
        if not os.path.exists(part) or _try_remove(part):
            return
        if left <= 0:
            log.warning("Couldn't delete the unfinished audio file")
        elif self._closed:  # quitting: no event loop to come back on
            time.sleep(0.05)
            self._delete_part(part, left - 1)
        else:
            QTimer.singleShot(250, self, lambda: self._delete_part(part, left - 1))

    def _close_part(self) -> None:
        wav, f = self._wav, self._file
        self._wav = self._file = None
        if wav is not None:
            with contextlib.suppress(Exception):
                wav.close()  # writes the final header; it leaves the file itself open
        if f is not None:
            with contextlib.suppress(OSError):
                f.close()

    def _delete_chunk(self, path: str) -> None:
        if self._cache is not None:
            self._cache.delete(path)


def remaining_label(seconds: float | None) -> str:
    """", about 3 min left" for the More tooltip, or "" before there is an estimate."""
    if seconds is None:
        return ""
    minutes = max(1, math.ceil(seconds / 60))
    return f", about {minutes} min left"

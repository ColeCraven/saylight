"""The voice-backend interface: voice IDs, voice descriptions and the protocol every backend implements.

A voice ID is "backend:name", for example "edge:en-US-AvaNeural". A bare name, as stored by Saylight 1.0,
is read as an online (edge) voice. This module imports nothing heavy, so the settings and the UI can use it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from PySide6.QtCore import SignalInstance

    from ..audio_cache import AudioCache

EDGE = "edge"
WINDOWS = "windows"
BACKENDS = (EDGE, WINDOWS)
DEFAULT_VOICE_ID = "edge:en-US-AvaNeural"

# chunkFailed kinds. "unavailable": the voice's backend is missing or not ready (not installed, no such voice).
FAIL_KINDS = ("offline", "service", "other", "unavailable")

_KIND = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Word:
    """One word-boundary event, in ms from the start of the chunk's audio (leading silence included)."""
    offset_ms: float
    duration_ms: float
    text: str


@dataclass(frozen=True)
class VoiceInfo:
    """One selectable voice, as the voice page lists it."""
    id: str                    # "edge:en-US-AvaNeural" | "windows:<id>"
    backend: str               # "edge" | "windows"
    name: str                  # display name, e.g. "Ava"
    locale: str                # "en-US"
    gender: str                # "Female" | "Male" | "" when unknown
    private: bool              # True: the text never leaves this PC
    word_timing: bool = True   # False: the voice gives no word boundaries, so only the sentence is highlighted
    detail: str = ""           # "Natural · online", "Windows voice · offline"


def parse_voice_id(value: str) -> tuple[str, str]:
    """Split a voice ID into (backend, name). A bare 1.0 name is an edge voice.

    "en-US-AvaNeural" -> ("edge", "en-US-AvaNeural")
    "windows:MSTTS_V110_enUS_ZiraM" -> ("windows", "MSTTS_V110_enUS_ZiraM")
    """
    value = str(value).strip()
    kind, sep, name = value.partition(":")
    if sep and _KIND.fullmatch(kind):
        return kind.lower(), name.strip()
    return EDGE, value


def make_voice_id(backend: str, name: str) -> str:
    """The voice ID for a backend and a voice name: ("edge", "en-US-AvaNeural") -> "edge:en-US-AvaNeural"."""
    if not _KIND.fullmatch(backend or ""):
        raise ValueError("invalid backend name")
    return f"{backend.lower()}:{str(name).strip()}"


@runtime_checkable
class VoiceBackend(Protocol):
    """A source of speech. A QObject on the GUI thread; any worker threads are its own.

    Results are delivered on the GUI thread, never from inside synthesize(), and a cancel issued on the GUI
    thread wins over any result that has not been delivered yet. Every audio file is written through the
    AudioCache given to start(), and the receiver deletes it through the same cache.
    """

    kind: str                          # "edge" | "windows"
    audio_ext: str                     # ".mp3" | ".wav"
    chunkReady: SignalInstance         # (gen: int, chunk_id: int, path: str, words: list[Word])
    chunkFailed: SignalInstance        # (gen: int, chunk_id: int, kind: str, detail: str); detail never quotes the text

    def start(self, cache: AudioCache) -> None:
        """Begin accepting work, writing audio into `cache` (started by the caller if needed)."""

    def ready(self) -> bool:
        """True if the backend can speak now, with no download."""

    def voices(self) -> list[VoiceInfo]:
        """The voices this backend offers without using the network."""

    def chunk_limits(self) -> tuple[int, int]:
        """(first, later) chunk sizes in characters."""

    def synthesize(self, gen: int, chunk_id: int, text: str, voice: str, speed: float) -> None:
        """Queue one chunk. `voice` is a voice ID; `speed` is a multiplier (1.0 is normal)."""

    def cancel_before(self, gen: int) -> None:
        """Drop queued, in-flight and undelivered jobs with job.gen < gen, emitting nothing for them."""

    def cancel_all(self) -> None:
        """Drop every queued, in-flight and undelivered job."""

    def warm(self) -> None:
        """Optional preparation, such as starting a helper process or loading a model."""

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel everything and stop any threads or processes."""

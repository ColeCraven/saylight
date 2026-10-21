"""Voices behind one interface.

base     the contract: voice IDs, VoiceInfo, Word and the VoiceBackend protocol (imports nothing heavy)
edge     Microsoft's online voices through edge-tts
windows  Windows' built-in voices through WinRT: private and offline
router   SynthRouter, the engine's synth: sends each chunk to its voice's backend
catalog  the voice list: built-in English voices, and every language on request
preview  plays a sample sentence in a chosen voice
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import (BACKENDS, DEFAULT_VOICE_ID, FAIL_KINDS, VoiceBackend, VoiceInfo, Word, make_voice_id,
                   parse_voice_id)

if TYPE_CHECKING:
    from PySide6.QtCore import QObject

    from .router import SynthRouter

__all__ = ["BACKENDS", "DEFAULT_VOICE_ID", "FAIL_KINDS", "VoiceBackend", "VoiceInfo", "Word", "make_backend",
           "make_router", "make_voice_id", "parse_voice_id"]


def make_backend(kind: str, parent: QObject | None = None) -> VoiceBackend | None:
    """A new, unstarted backend of `kind`, or None if this build has none."""
    if kind == "edge":
        from .edge import EdgeBackend
        return EdgeBackend(parent)
    if kind == "windows":
        from .windows import WindowsVoiceBackend
        return WindowsVoiceBackend(parent)
    return None


def make_router(*, temp_root: str | None = None, settings: Any = None, parent: QObject | None = None) -> SynthRouter:
    """The engine's synth: a SynthRouter over every backend this build has, sharing one AudioCache."""
    from ..audio_cache import AudioCache
    from .router import SynthRouter
    backends = {kind: backend for kind in BACKENDS if (backend := make_backend(kind)) is not None}
    return SynthRouter(backends, AudioCache(temp_root), settings=_ChosenVoicesOnly(settings), parent=parent)


class _ChosenVoicesOnly:
    """The settings as the router reads them. A Windows voice speaks only when the person chooses one: until
    the settings have a "voice.offline_fallback" switch, the online voice never falls back to it by itself."""

    def __init__(self, settings: Any):
        self._settings = settings

    def get(self, key: str) -> Any:
        try:
            if self._settings is None:
                raise KeyError(key)
            return self._settings.get(key)
        except KeyError:
            if key == "voice.offline_fallback":
                return False
            raise

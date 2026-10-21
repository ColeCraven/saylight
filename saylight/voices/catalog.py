"""The voice list: a built-in English list that needs no network, plus the full online list on request.

Microsoft's full list is fetched only when the person clicks "Show all languages", on a worker thread,
and kept in memory for the session. If it can't be fetched, the built-in list is shown.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .base import EDGE, VoiceInfo, make_voice_id

log = logging.getLogger("saylight.voices.catalog")

ONLINE_DETAIL = "Natural · online"
LOCALES = ("en-US", "en-GB", "en-AU", "en-CA", "en-IE", "en-IN", "en-NZ", "en-ZA")
LOCALE_NAMES = {
    "en-US": "English (United States)", "en-GB": "English (United Kingdom)", "en-AU": "English (Australia)",
    "en-CA": "English (Canada)", "en-IE": "English (Ireland)", "en-IN": "English (India)",
    "en-NZ": "English (New Zealand)", "en-ZA": "English (South Africa)",
}

# Microsoft's English voices for LOCALES as of October 2026, each checked to send word boundaries.
# Ava (the default) comes first.
_ENGLISH = (
    ("en-US-AvaNeural", "Female"), ("en-US-AnaNeural", "Female"), ("en-US-AndrewNeural", "Male"),
    ("en-US-AndrewMultilingualNeural", "Male"), ("en-US-AriaNeural", "Female"),
    ("en-US-AvaMultilingualNeural", "Female"), ("en-US-BrianNeural", "Male"),
    ("en-US-BrianMultilingualNeural", "Male"), ("en-US-ChristopherNeural", "Male"),
    ("en-US-EmmaNeural", "Female"), ("en-US-EmmaMultilingualNeural", "Female"), ("en-US-EricNeural", "Male"),
    ("en-US-GuyNeural", "Male"), ("en-US-JennyNeural", "Female"), ("en-US-MichelleNeural", "Female"),
    ("en-US-RogerNeural", "Male"), ("en-US-SteffanNeural", "Male"),
    ("en-GB-LibbyNeural", "Female"), ("en-GB-MaisieNeural", "Female"), ("en-GB-RyanNeural", "Male"),
    ("en-GB-SoniaNeural", "Female"), ("en-GB-ThomasNeural", "Male"),
    ("en-AU-NatashaNeural", "Female"), ("en-AU-WilliamMultilingualNeural", "Male"),
    ("en-CA-ClaraNeural", "Female"), ("en-CA-LiamNeural", "Male"),
    ("en-IE-ConnorNeural", "Male"), ("en-IE-EmilyNeural", "Female"),
    ("en-IN-NeerjaNeural", "Female"), ("en-IN-NeerjaExpressiveNeural", "Female"), ("en-IN-PrabhatNeural", "Male"),
    ("en-NZ-MitchellNeural", "Male"), ("en-NZ-MollyNeural", "Female"),
    ("en-ZA-LeahNeural", "Female"), ("en-ZA-LukeNeural", "Male"),
)

_SHORT_NAME = re.compile(r"[A-Za-z]{2,3}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+")
_CAMEL = re.compile(r"[A-Z][a-z]*|[a-z]+|\d+")


def display_name(short_name: str) -> str:
    """The name to show for an online voice: "en-US-AvaMultilingualNeural" -> "Ava Multilingual".

    A regional variant keeps its region: "zh-CN-liaoning-XiaobeiNeural" -> "Xiaobei (liaoning)".
    """
    parts = short_name.split("-")
    core = parts[-1] if len(parts) > 2 else short_name
    if core.endswith("Neural"):
        core = core[:-len("Neural")]
    name = " ".join(_CAMEL.findall(core)) or core
    extra = parts[2:-1]
    return f"{name} ({' '.join(extra)})" if extra else name


def online_voice(short_name: str, locale: str, gender: str) -> VoiceInfo:
    """A VoiceInfo for one of Microsoft's online voices."""
    return VoiceInfo(id=make_voice_id(EDGE, short_name), backend=EDGE, name=display_name(short_name),
                     locale=locale, gender=gender, private=False, word_timing=True, detail=ONLINE_DETAIL)


CURATED: tuple[VoiceInfo, ...] = tuple(online_voice(n, n[:5], g) for n, g in _ENGLISH)


def parse_online_list(entries: Iterable[object]) -> tuple[list[VoiceInfo], dict[str, str]]:
    """VoiceInfos and locale names from edge-tts' voice list. Malformed and deprecated entries are skipped.

    A voice that is also in CURATED keeps its built-in entry.
    """
    curated = {v.id: v for v in CURATED}
    voices: dict[str, VoiceInfo] = {}
    names: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        short, locale = entry.get("ShortName"), entry.get("Locale")
        if not isinstance(short, str) or not isinstance(locale, str) or not _SHORT_NAME.fullmatch(short):
            continue
        if str(entry.get("Status", "")).lower() == "deprecated":
            continue
        gender = entry.get("Gender") if entry.get("Gender") in ("Female", "Male") else ""
        info = curated.get(make_voice_id(EDGE, short)) or online_voice(short, locale, gender)
        voices.setdefault(info.id, info)
        if isinstance(entry.get("LocaleName"), str):
            names.setdefault(locale, entry["LocaleName"])
    ordered = sorted(voices.values(), key=lambda v: (v.locale.lower(), v.name.lower()))
    return ordered, names


class VoiceCatalog(QObject):
    """The voices the voice page offers. GUI thread only.

    status: "builtin" (no full list yet), "loading", "online" (full list loaded) or "failed" (the
    built-in list is shown). `changed` fires whenever the list or the status changes.
    """

    changed = Signal()
    _fetched = Signal(object, str)       # worker thread -> GUI thread: (entries or None, error class name)

    def __init__(self, parent: QObject | None = None, *,
                 fetch: Callable[[], list[dict]] | None = None,
                 local: Callable[[], Iterable[VoiceInfo]] | None = None):
        super().__init__(parent)
        self._fetch = fetch
        self._local = local or (lambda: ())
        self._online: list[VoiceInfo] | None = None
        self._names: dict[str, str] = dict(LOCALE_NAMES)
        self._status = "builtin"
        self._fetched.connect(self._on_fetched, Qt.ConnectionType.QueuedConnection)

    @property
    def status(self) -> str:
        return self._status

    def voices(self, *, all_languages: bool = False) -> list[VoiceInfo]:
        """Local voices first (from other backends), then the online voices: the built-in English list,
        or every language once refresh_online() has loaded them."""
        online = self._online if all_languages and self._online is not None else list(CURATED)
        return [*self._local(), *online]

    def find(self, voice_id: str) -> VoiceInfo | None:
        """The listed voice with this ID, if any."""
        for voice in self.voices(all_languages=True):
            if voice.id == voice_id:
                return voice
        return None

    def locale_name(self, locale: str) -> str:
        """ "en-GB" -> "English (United Kingdom)"; other languages are named once the full list is loaded."""
        return self._names.get(locale, locale)

    def refresh_online(self) -> None:
        """Fetch the full list on a worker thread. Only when the person asks for it ("Show all languages")."""
        if self._status in ("loading", "online"):
            return
        self._set_status("loading")
        threading.Thread(target=self._run_fetch, name="voice-list", daemon=True).start()

    # --- Internals ----------------------------------------------------------------------

    def _run_fetch(self) -> None:
        fetch = self._fetch
        if fetch is None:
            from .edge import fetch_voice_list
            fetch = fetch_voice_list
        try:
            entries, error = fetch(), ""
        except Exception as e:  # noqa: BLE001 - reported as "failed"; the built-in list stays
            entries, error = None, type(e).__name__
        try:
            self._fetched.emit(entries, error)
        except RuntimeError:     # the catalog was deleted while the request ran
            pass

    @Slot(object, str)
    def _on_fetched(self, entries: object, error: str) -> None:
        voices, names = parse_online_list(entries) if isinstance(entries, list) else ([], {})
        if not voices:
            log.warning("Couldn't load the online voice list (%s); showing the built-in list", error or "empty")
            self._set_status("failed")
            return
        self._online = voices
        for locale, name in names.items():
            self._names.setdefault(locale, name)
        log.info("Loaded %d online voices in %d languages", len(voices), len({v.locale for v in voices}))
        self._set_status("online")

    def _set_status(self, status: str) -> None:
        self._status = status
        self.changed.emit()

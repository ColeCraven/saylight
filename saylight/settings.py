"""Settings: load, validate and save saylight_settings.json.

Every known key lives in SCHEMA as dotted path -> (default, validator), so adding a key is one line.
A validator returns the coerced value or raises ValueError. Unknown keys in the file are kept.
Saving is debounced (500 ms) and atomic (.tmp + os.replace, retried). GUI thread only.
"""

import contextlib
import copy
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from .voices.base import BACKENDS, DEFAULT_VOICE_ID, make_voice_id, parse_voice_id

log = logging.getLogger("saylight.settings")

SETTINGS_VERSION = 1
DEFAULT_VOICE = DEFAULT_VOICE_ID
SPEEDS = (0.5, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.5, 1.75, 2.0)
TEXT_SIZES = ("small", "medium", "large", "xlarge")
# Reading backgrounds: "auto" is Cream in light mode and Soft dark in dark mode.
READING_TINTS = ("auto", "cream", "peach", "blue", "dark")  # keep in step with theme.READING_PRESETS (tested)
READING_FONTS = ("verdana", "segoe", "calibri", "sitka")
SPACINGS = ("standard", "wide", "extra")
HIGHLIGHTS = ("word", "sentence", "off")
SNAP_X = ("left", "center", "right")
SNAP_Y = ("top", "bottom")
RESUME_OFFERS = 3  # "Remember this page next time" is offered at most this many times, ever

SAVE_DELAY_MS = 500
RETRY_MAX_MS = 30_000  # back-off cap when the file stays locked (antivirus, OneDrive)
_FILE_ATTEMPTS = 4  # one try plus 3 retries, 50 ms apart
_FILE_GAP_S = 0.05


# --- Validators --------------------------------------------------------------

def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ValueError("expected true or false")


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("expected a number")
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (ValueError, OverflowError):  # no value in the message: logs never echo file content
        raise ValueError("expected a number") from None
    if not math.isfinite(number):
        raise ValueError("expected a finite number")
    return number


def _speed(value: Any) -> float:
    """One of SPEEDS; anything else snaps to the nearest."""
    number = _number(value)
    return min(SPEEDS, key=lambda s: abs(s - number))


def _int_range(lo: int, hi: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        return max(lo, min(hi, math.floor(_number(value) + 0.5)))
    return check


def _choice(*options: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if isinstance(value, str):
            for option in options:
                if option.lower() == value.strip().lower():
                    return option
        raise ValueError(f"expected one of: {', '.join(options)}")
    return check


def _text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("expected a non-empty string")


def _str(value: Any) -> str:
    """Any string, "" included."""
    if isinstance(value, str):
        return value.strip()
    raise ValueError("expected a string")


def _voice(value: Any) -> str:
    """A voice ID, "backend:name". A bare name from Saylight 1.0 or 1.1 is an online voice: "edge:<name>"."""
    backend, name = parse_voice_id(_text(value))
    if backend not in BACKENDS or not name:
        raise ValueError("expected a voice ID")
    return make_voice_id(backend, name)


def _timestamp(value: Any) -> float:
    """Seconds since 1970; 0.0 for never."""
    return max(0.0, _number(value))


_MODIFIERS = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "windows": "Win"}
_MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")
_FUNCTION_KEY = re.compile(r"[Ff]([1-9]|1[0-9]|2[0-4])")
# Named keys that win32.parse_hotkey also accepts; only Pause and ScrollLock (like F-keys) work without modifiers.
_NAMED_KEYS = {name.lower(): name for name in
               ("Space", "PageUp", "PageDown", "End", "Home", "Insert", "Delete", "Pause", "ScrollLock")}
_BARE_KEYS = ("Pause", "ScrollLock")


def _hotkey(value: Any) -> str:
    """"" (disabled) or "Ctrl|Alt|Shift|Win + one character, F1-F24 or a named key", in canonical order.

    Characters and editing keys need Ctrl, Alt or Win, so a shortcut can never swallow plain typing."""
    if not isinstance(value, str):
        raise ValueError("expected a string")
    spec = value.strip()
    if not spec:
        return ""
    if spec.endswith("++"):
        head, key = spec[:-2], "+"
    else:
        head, _, key = spec.rpartition("+")
        key = key.strip()
    mods = set()
    for part in head.split("+") if head else ():
        name = _MODIFIERS.get(part.strip().lower())
        if name is None:
            raise ValueError("unknown modifier")
        mods.add(name)
    if match := _FUNCTION_KEY.fullmatch(key):
        key, bare_ok = f"F{match.group(1)}", True
    elif key.lower() in _NAMED_KEYS:
        key = _NAMED_KEYS[key.lower()]
        bare_ok = key in _BARE_KEYS
    elif len(key) == 1 and not key.isspace() and ord(key) <= 0xFFFF:
        bare_ok = False
    else:
        raise ValueError("expected one character, F1-F24 or a named key after the modifiers")
    if not bare_ok and not mods & {"Ctrl", "Alt", "Win"}:
        raise ValueError("this key needs Ctrl, Alt or Win")
    return "+".join([m for m in _MODIFIER_ORDER if m in mods] + [key])


def _position(value: Any) -> dict | None:
    """null, or the saved panel position with fx/fy clamped to 0-1."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("expected null or an object")
    fx, fy = (round(min(1.0, max(0.0, _number(value.get(k)))), 4) for k in ("fx", "fy"))
    screen, serial = (value.get(k) if isinstance(value.get(k), str) else "" for k in ("screen", "serial"))
    return {"screen": screen, "serial": serial, "fx": fx, "fy": fy,
            "snap_x": value.get("snap_x") if value.get("snap_x") in SNAP_X else None,
            "snap_y": value.get("snap_y") if value.get("snap_y") in SNAP_Y else None}


SCHEMA: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "version": (SETTINGS_VERSION, _int_range(1, 1_000_000)),
    "speed": (0.95, _speed),
    "volume": (100, _int_range(0, 100)),
    "voice": (DEFAULT_VOICE, _voice),
    "panel.visible": (True, _bool),
    "panel.position": (None, _position),
    "panel.expanded": (False, _bool),
    "hud_when_hidden": (True, _bool),
    "media_keys": (True, _bool),
    "hotkeys.read": ("Ctrl+`", _hotkey),
    "hotkeys.toggle_panel": ("", _hotkey),
    "hotkeys.pause_resume": ("", _hotkey),
    "hotkeys.compose": ("", _hotkey),
    "first_hide_tip_shown": (False, _bool),
    "skip_citations": (True, _bool),
    "skip_urls": (True, _bool),
    "read_references": (False, _bool),
    "read_asides": (False, _bool),
    "pronunciations.enabled": (True, _bool),
    "resume.enabled": (False, _bool),
    "resume.offer_count": (0, _int_range(0, RESUME_OFFERS)),
    "tips.dimmed_note_shown": (False, _bool),
    "text_size": ("medium", _choice(*TEXT_SIZES)),
    "reading_tint": ("auto", _choice(*READING_TINTS)),
    "reading_font": ("verdana", _choice(*READING_FONTS)),
    "spacing": ("wide", _choice(*SPACINGS)),
    "highlight": ("word", _choice(*HIGHLIGHTS)),
    "updates.check": (True, _bool),
    "updates.last_check": (0.0, _timestamp),
    "updates.etag": ("", _str),
    "updates.skipped": ("", _str),
}


# --- Tree and file helpers ---------------------------------------------------

def _lookup(tree: dict, key: str) -> tuple[bool, Any]:
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _store(tree: dict, key: str, value: Any) -> None:
    *parents, leaf = key.split(".")
    node = tree
    for part in parents:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[leaf] = value


def _retry(action: Callable[[], Any]) -> Any:
    """Run a file operation, retrying OSError (a lock by antivirus or OneDrive) 50 ms apart."""
    for attempt in range(_FILE_ATTEMPTS):
        try:
            return action()
        except FileNotFoundError:
            raise
        except OSError:
            if attempt == _FILE_ATTEMPTS - 1:
                raise
            time.sleep(_FILE_GAP_S)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _dump(data: dict) -> bytes:
    """The file content: UTF-8 JSON, indent 2. A lone surrogate (legal as a \\uD800 escape in the file) can't be
    encoded as UTF-8, so such data is written with ASCII escapes instead, which round-trip exactly."""
    try:
        return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        return (json.dumps(data, indent=2) + "\n").encode("ascii")


def _atomic_write(path: str, data: bytes) -> None:
    """Write to <path>.tmp, fsync, then os.replace it over <path> (retried). The .tmp never outlives a failure."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _retry(lambda: os.replace(tmp, path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


class Settings(QObject):
    """The settings file as validated values. Create and use on the GUI thread."""

    changed = Signal(str)  # dotted key

    def __init__(self, path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = os.path.abspath(path)
        self._data: dict = {}
        self._dirty = False
        self._can_save = True
        self._retry_ms = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._save)
        self._load()
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.flush)  # backstop; the app also flushes during shutdown

    # --- Public API

    def get(self, key: str) -> Any:
        """A copy of the value at a dotted key, or a dict of the known keys under a group such as "hotkeys"."""
        if key in SCHEMA:
            return copy.deepcopy(_lookup(self._data, key)[1])
        prefix = key + "."
        group: dict = {}
        for name in SCHEMA:
            if name.startswith(prefix):
                _store(group, name[len(prefix):], copy.deepcopy(_lookup(self._data, name)[1]))
        if not group:
            raise KeyError(key)
        return group

    def set(self, key: str, value: Any) -> None:
        """Validate, coerce and store a value. Raises KeyError for unknown keys, ValueError for bad values."""
        if key not in SCHEMA:
            raise KeyError(key)
        value = SCHEMA[key][1](value)
        if _lookup(self._data, key)[1] == value:
            return
        _store(self._data, key, copy.deepcopy(value))
        self._dirty = True
        self._timer.start(SAVE_DELAY_MS)
        self.changed.emit(key)

    def flush(self) -> bool:
        """Write pending changes now. True if the file is up to date."""
        self._timer.stop()
        return self._save()

    # --- Loading

    def _load(self) -> None:
        tree: dict = {}
        raw = self._read()
        if raw is not None:
            try:
                parsed = json.loads(raw)  # bytes: UTF-8 (with or without BOM), or UTF-16/32 from some editors
                if not isinstance(parsed, dict):
                    raise ValueError("the top level is not an object")
                tree = parsed
            # JSONDecodeError and UnicodeDecodeError are ValueErrors; absurdly deep nesting raises RecursionError.
            except (ValueError, RecursionError) as e:
                self._quarantine(e)
        self._data = tree
        for key, (default, check) in SCHEMA.items():
            found, value = _lookup(tree, key)
            if found:
                try:
                    value = check(value)
                except Exception as e:  # a bad file must never stop the app starting
                    log.warning("Setting %s is invalid (%s); using the default", key, e)
                    value = copy.deepcopy(default)
            else:
                value = copy.deepcopy(default)
            _store(self._data, key, value)

    def _read(self) -> bytes | None:
        try:
            return _retry(lambda: _read_bytes(self._path))
        except FileNotFoundError:
            log.info("No settings file yet; using defaults")
        except OSError as e:
            # Never overwrite a file we could not read: it may hold the user's settings.
            log.error("Couldn't read the settings file (%s); using defaults and not saving this session",
                      type(e).__name__)
            self._can_save = False
        return None

    def _quarantine(self, error: Exception) -> None:
        where = f" at line {error.lineno} column {error.colno}" if isinstance(error, json.JSONDecodeError) else ""
        bad = self._path + ".bad"
        try:
            _retry(lambda: os.replace(self._path, bad))
            log.warning("Settings file was unreadable (%s%s); moved it to %s and using defaults",
                        type(error).__name__, where, os.path.basename(bad))
        except FileNotFoundError:
            pass  # it vanished in the meantime; nothing to protect
        except OSError as e:
            log.error("Settings file was unreadable and couldn't be moved aside (%s); not saving this session",
                      type(e).__name__)
            self._can_save = False

    # --- Saving

    def _save(self) -> bool:
        if not self._dirty:
            return True
        if not self._can_save:
            return False
        data = _dump(self._data)
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            _atomic_write(self._path, data)
        except OSError as e:
            first = self._retry_ms == 0
            self._retry_ms = min(RETRY_MAX_MS, max(1000, self._retry_ms * 2))
            (log.warning if first else log.debug)(
                "Couldn't save settings (%s); retrying in %d s", type(e).__name__, self._retry_ms // 1000)
            self._timer.start(self._retry_ms)
            return False
        if self._retry_ms:
            log.info("Settings saved after an earlier failure")
        self._dirty = False
        self._retry_ms = 0
        return True

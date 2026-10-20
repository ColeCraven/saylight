"""The copy job: copy the user's selection with Ctrl+C, then put their clipboard back.

copy_selection() runs on the input thread, which owns a ClipboardOwner window and pumps its messages.
If an app answers Ctrl+C only after the 0.35 s timeout, the input thread's loop calls watch_late_copy()
so the user's clipboard is still put back. The decision logic is in small pure functions and in _copy(),
which takes the Win32 side as an object so tests can drive it with a fake. Nothing here logs clipboard
text, only lengths and reasons.
"""

import contextlib
import ctypes
import json
import logging
import re
import threading
import time
from collections.abc import Iterator, Sequence
from ctypes import wintypes
from dataclasses import dataclass

from . import win32

log = logging.getLogger("saylight.clipboard")

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

REASONS = ("ok", "empty", "line_copy", "console", "shell", "busy")

KEY_RELEASE_WAIT_S = 1.5     # wait at most this long for the user's hotkey keys to be let go
KEY_POLL_S = 0.02
SEQUENCE_POLL_S = 0.01
SETTLE_S = 0.05              # after the clipboard changes, let the app finish filling it
FOREGROUND_RESTORE_S = 0.05  # after SetForegroundWindow(last_app), before re-checking
OPEN_TIMEOUT_S, OPEN_RETRY_S = 0.5, 0.02  # another app may hold the clipboard open briefly
MAX_MARKER_BYTES = 1 << 20   # ignore huge custom-data blobs when looking for line-copy markers
LATE_COPY_WATCH_S = 0.65     # after a timeout, undo a copy that lands this much later (1.0 s in all)
LATE_COPY_POLL_S = 0.02      # how often the input thread checks for a late copy
LATE_COPY_GRACE_S = 0.1      # a change first seen later than this after the watch ended isn't ours to undo

VSCODE_FORMAT = "vscode-editor-data"
CHROMIUM_CUSTOM_FORMAT = "Chromium Web Custom MIME Data Format"  # how Chromium/Electron store web custom types
LINE_COPY_TAGS = ("VisualStudioEditorOperationsLineCutCopyClipboardTag", "MSDEVLineSelect")  # VS, Scintilla
HISTORY_OPT_OUT = ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")

MODIFIER_VKS = (win32.VK_CONTROL, win32.VK_SHIFT, win32.VK_MENU, win32.VK_LWIN, win32.VK_RWIN)
_EMPTY_SELECTION_FLAG = re.compile(r'"isFromEmptySelection"\s*:\s*true')


@dataclass(frozen=True)
class Saved:
    """The clipboard before the copy: its text (None if it held none) and whether it held nothing at all."""
    text: str | None
    empty: bool


@dataclass(frozen=True)
class Copied:
    """What the app put on the clipboard: its text (None if none) and whether it was a whole-line copy."""
    text: str | None
    line_copy: bool


@dataclass(frozen=True)
class _LateCopy:
    """A copy job that timed out: a slow app may still answer its Ctrl+C, and then the clipboard is restored."""
    before: int       # the clipboard sequence number when Ctrl+C was sent
    saved: Saved
    deadline: float   # system.monotonic() time when the watch ends


# --- Pure decision logic ----------------------------------------------------------

def copy_keys(terminal: bool) -> list[int]:
    """Ctrl+Shift+C in terminals (copies without sending SIGINT), Ctrl+C everywhere else."""
    return [win32.VK_CONTROL, win32.VK_SHIFT, win32.VK_C] if terminal else [win32.VK_CONTROL, win32.VK_C]


def keys_to_release(key_vks: Sequence[int]) -> list[int]:
    """The hotkey's own keys plus every modifier, without duplicates."""
    return list(dict.fromkeys([*key_vks, *MODIFIER_VKS]))


def _empty_selection_flag(text: str) -> bool:
    """True if the first JSON object in text has "isFromEmptySelection": true."""
    start = text.find("{")
    if start < 0:
        return False
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return bool(_EMPTY_SELECTION_FLAG.search(text, start))
    return isinstance(obj, dict) and obj.get("isFromEmptySelection") is True


def vscode_json_is_line_copy(raw: bytes) -> bool:
    """Parse the registered "vscode-editor-data" format (UTF-8 JSON, maybe NUL-terminated)."""
    return _empty_selection_flag(raw.split(b"\0", 1)[0].decode("utf-8", "replace"))


def chromium_custom_is_line_copy(raw: bytes) -> bool:
    """Parse Chromium's custom-data pickle (UTF-16 type/value pairs) for a vscode-editor-data entry."""
    text = raw[: len(raw) & ~1].decode("utf-16-le", "replace")
    i = text.find(VSCODE_FORMAT)
    return i >= 0 and _empty_selection_flag(text[i + len(VSCODE_FORMAT):])


def decide(copied: Copied) -> tuple[str, str]:
    """(text, reason) for what the copy produced."""
    if copied.line_copy:
        return "", "line_copy"
    if copied.text is None or not copied.text.strip():
        return "", "empty"
    return copied.text, "ok"


# --- Clipboard I/O (call with the clipboard closed; these open and close it) ----------

_tls = threading.local()


def _owner() -> int:
    return getattr(_tls, "owner", 0)


class ClipboardOwner:
    """A hidden STATIC window that owns what this thread writes to the clipboard.

    Create, use and destroy it on one thread, and keep that thread answering sent messages while it
    lives (the input thread's loop and win32.wait_pumping_sent do): Windows sends WM_DESTROYCLIPBOARD
    to it synchronously when another app empties the clipboard.
    """

    def __init__(self) -> None:
        self.hwnd = 0

    def create(self) -> int:
        self.hwnd = win32.user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None) or 0
        if not self.hwnd:
            log.error("Couldn't create the clipboard owner window (error %d)", win32.last_error())
        _tls.owner = self.hwnd
        return self.hwnd

    def destroy(self) -> None:
        if self.hwnd:
            win32.user32.DestroyWindow(self.hwnd)
            if _owner() == self.hwnd:
                _tls.owner = 0
            self.hwnd = 0

    def __enter__(self) -> "ClipboardOwner":
        self.create()
        return self

    def __exit__(self, *exc) -> None:
        self.destroy()


@contextlib.contextmanager
def _opened() -> Iterator[bool]:
    """Open the clipboard, retrying for OPEN_TIMEOUT_S; yields False if another app kept it open."""
    deadline = time.monotonic() + OPEN_TIMEOUT_S
    while not win32.user32.OpenClipboard(_owner()):
        if time.monotonic() >= deadline:
            yield False
            return
        win32.wait_pumping_sent(OPEN_RETRY_S)
    try:
        yield True
    finally:
        win32.user32.CloseClipboard()


def _format_id(name: str) -> int:
    return win32.user32.RegisterClipboardFormatW(name)


def _available(fmt: int) -> bool:
    return bool(fmt) and bool(win32.user32.IsClipboardFormatAvailable(fmt))


def _read_bytes(fmt: int, limit: int | None = None) -> bytes | None:
    """The raw bytes of one format; the clipboard must be open."""
    handle = win32.user32.GetClipboardData(fmt) if _available(fmt) else None
    if not handle:
        return None
    size = win32.kernel32.GlobalSize(handle)
    if limit is not None and size > limit:
        return None
    ptr = win32.kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        return ctypes.string_at(ptr, size)
    finally:
        win32.kernel32.GlobalUnlock(handle)


def _read_text() -> str | None:
    """CF_UNICODETEXT up to its first NUL, never reading past the allocation; the clipboard must be open.

    surrogatepass keeps even malformed UTF-16 intact, so a restore puts back exactly what was there.
    """
    raw = _read_bytes(CF_UNICODETEXT)
    if raw is None:
        return None
    return raw[: len(raw) & ~1].decode("utf-16-le", "surrogatepass").split("\0", 1)[0]


def _write(fmt: int, data: ctypes._CData, size: int) -> bool:
    """SetClipboardData with a moveable copy of the first size bytes of a ctypes object.

    The clipboard must be open and emptied by us.
    """
    handle = win32.kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False
    ptr = win32.kernel32.GlobalLock(handle)
    if not ptr:
        win32.kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, ctypes.addressof(data), size)
    win32.kernel32.GlobalUnlock(handle)
    if not win32.user32.SetClipboardData(fmt, handle):
        win32.kernel32.GlobalFree(handle)  # still ours: the system takes ownership only on success
        return False
    return True


def get_text() -> str | None:
    """The clipboard's text, or None if it holds no text or couldn't be opened."""
    if not _available(CF_UNICODETEXT):
        return None
    with _opened() as ok:
        return _read_text() if ok else None


def set_text(text: str, *, history: bool = True) -> bool:
    """Replace the clipboard with text. history=False keeps it out of Win+V history and cloud sync."""
    data = ctypes.create_unicode_buffer(text)
    with _opened() as ok:
        if not ok or not win32.user32.EmptyClipboard():
            return False
        if not _write(CF_UNICODETEXT, data, ctypes.sizeof(data)):
            return False
        if not history:
            no = wintypes.DWORD(0)
            for name in HISTORY_OPT_OUT:
                _write(_format_id(name), no, ctypes.sizeof(no))
        return True


def empty_clipboard() -> bool:
    with _opened() as ok:
        return ok and bool(win32.user32.EmptyClipboard())


def _save() -> Saved | None:
    """The clipboard before copying, or None if it can't be saved (so we must not copy over it): it
    couldn't be opened, or it offers text that couldn't be read (e.g. its owner failed to render it)."""
    if not _available(CF_UNICODETEXT):
        return Saved(None, win32.user32.CountClipboardFormats() == 0)
    with _opened() as ok:
        if not ok:
            return None
        text = _read_text()
        if text is not None:
            return Saved(text, False)
        if _available(CF_UNICODETEXT):
            return None
        return Saved(None, win32.user32.CountClipboardFormats() == 0)  # its text went away meanwhile


def _is_line_copy() -> bool:
    """Whole-line copy markers from VS Code, Visual Studio and Scintilla editors; the clipboard must be open."""
    if any(_available(_format_id(name)) for name in LINE_COPY_TAGS):
        return True
    raw = _read_bytes(_format_id(VSCODE_FORMAT), MAX_MARKER_BYTES)
    if raw is not None and vscode_json_is_line_copy(raw):
        return True
    raw = _read_bytes(_format_id(CHROMIUM_CUSTOM_FORMAT), MAX_MARKER_BYTES)
    return raw is not None and chromium_custom_is_line_copy(raw)


def _read_copied() -> Copied | None:
    with _opened() as ok:
        return Copied(_read_text(), _is_line_copy()) if ok else None


def _restore(saved: Saved) -> bool:
    """Put back the previous text (or emptiness). Non-text content can't be restored, so it's left alone."""
    if saved.text is not None:
        return set_text(saved.text, history=False)  # already in history from when it was first copied
    if saved.empty:
        return empty_clipboard()
    return True


# --- The copy job ------------------------------------------------------------

class _System:
    """The Win32 side of the copy job. Tests replace it, or parts of it."""

    def foreground(self) -> int:
        return win32.get_foreground_window()

    def is_app(self, hwnd: int) -> bool:
        return win32.is_app_window(hwnd)

    def is_terminal(self, hwnd: int) -> bool:
        return win32.is_terminal_window(hwnd)

    def set_foreground(self, hwnd: int) -> bool:
        return win32.set_foreground_window(hwnd)

    def key_down(self, vk: int) -> bool:
        return win32.key_down(vk)

    def send_keys(self, vks: list[int]) -> bool:
        return win32.send_keys(vks)

    def sequence(self) -> int:
        return win32.user32.GetClipboardSequenceNumber()

    def save(self) -> Saved | None:
        return _save()

    def read(self) -> Copied | None:
        return _read_copied()

    def restore(self, saved: Saved) -> bool:
        return _restore(saved)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        win32.wait_pumping_sent(seconds)


def _wait_released(system: _System, key_vks: Sequence[int]) -> None:
    vks = keys_to_release(key_vks)
    deadline = system.monotonic() + KEY_RELEASE_WAIT_S
    while any(system.key_down(vk) for vk in vks):
        if system.monotonic() >= deadline:
            log.info("Keys still held after %.1f s; copying anyway", KEY_RELEASE_WAIT_S)
            return
        system.sleep(KEY_POLL_S)


def _wait_changed(system: _System, before: int, timeout_s: float) -> bool:
    deadline = system.monotonic() + timeout_s
    while system.sequence() == before:
        if system.monotonic() >= deadline:
            return False
        system.sleep(SEQUENCE_POLL_S)
    return True


def _check_late_copy(system: _System) -> float | None:
    """Undo a copy that landed after its job had timed out; the seconds left to watch, or None if none."""
    late: _LateCopy | None = getattr(_tls, "late", None)
    if late is None:
        return None
    now = system.monotonic()
    if now > late.deadline + LATE_COPY_GRACE_S:
        _tls.late = None  # nobody checked in time, so a change now could be the user's own copy
        return None
    if system.sequence() != late.before:
        _tls.late = None
        system.sleep(SETTLE_S)
        if system.restore(late.saved):
            log.info("A copy landed after the timeout; the previous clipboard was put back")
        else:
            log.warning("A copy landed after the timeout; couldn't restore the previous clipboard")
        return None
    if now >= late.deadline:
        _tls.late = None
        return None
    return late.deadline - now


def watch_late_copy() -> float | None:
    """For the input thread's loop, before every wait: undo a copy that landed after its job timed out.

    Returns the seconds left to keep watching (wait at most LATE_COPY_POLL_S, then call again), or None
    when nothing is being watched. Never raises.
    """
    try:
        return _check_late_copy(_System())
    except Exception:
        _tls.late = None
        log.exception("Late-copy check failed")
        return None


def _copy(system: _System, *, is_click: bool, last_app: int, key_vks: Sequence[int],
          timeout_s: float) -> tuple[str, str]:
    _check_late_copy(system)  # undo the previous job's late copy before this job saves the clipboard
    _tls.late = None          # from here on, this job's own save and restore cover the clipboard
    fg = system.foreground()
    if is_click and not system.is_app(fg):  # the taskbar, desktop or Saylight itself is in front
        if not system.is_app(last_app):
            return "", "shell"
        system.set_foreground(last_app)
        system.sleep(FOREGROUND_RESTORE_S)
        fg = system.foreground()
        if not system.is_app(fg):
            return "", "shell"
    terminal = system.is_terminal(fg)
    if terminal and is_click:
        return "", "console"

    _wait_released(system, key_vks)
    saved = system.save()
    if saved is None:
        return "", "busy"  # the clipboard couldn't be saved, so don't risk overwriting it
    before = system.sequence()
    if not system.send_keys(copy_keys(terminal)):
        return "", "empty"
    if not _wait_changed(system, before, timeout_s):
        _tls.late = _LateCopy(before, saved, system.monotonic() + LATE_COPY_WATCH_S)
        return "", "empty"  # nothing was selected, the app ignored the keys, or it is slow (see watch_late_copy)
    try:
        system.sleep(SETTLE_S)
        copied = system.read()
    finally:
        if not system.restore(saved):
            log.warning("Couldn't restore the previous clipboard")
    return ("", "busy") if copied is None else decide(copied)


def copy_selection(*, is_click: bool, last_app: int, key_vks: list[int], timeout_s: float = 0.35) -> tuple[str, str]:
    """Copy the foreground app's selection and return (text, reason); see REASONS.

    Call it on the input thread, which owns a ClipboardOwner. is_click: the panel's Play button (refuses
    terminals; restores last_app when the shell or Saylight is in front). key_vks: the hotkey's keys,
    which are waited on (with every modifier) before sending Ctrl+C.
    """
    start = time.monotonic()
    text, reason = _copy(_System(), is_click=is_click, last_app=last_app, key_vks=key_vks, timeout_s=timeout_s)
    log.info("Copy (%s): %s, %d chars, %d ms", "click" if is_click else "hotkey", reason, len(text),
             round((time.monotonic() - start) * 1000))
    return text, reason

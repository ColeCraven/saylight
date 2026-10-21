"""Win32 helpers for the windows drawn over other apps: the selection overlay, the screen marks and the ruler."""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from . import win32

log = logging.getLogger("saylight.win32_overlay")

WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x00, 0x11
EXCLUDE_MIN_BUILD = 19041  # Windows 10 2004; before it the flag silently acts like WDA_MONITOR (a black box)
# SHQueryUserNotificationState values that hide the ruler: not present (lock screen, screen saver, switching
# users), busy (a full-screen app), a Direct3D full-screen app, and presentation mode.
QUNS_HIDE = frozenset({1, 2, 3, 4})

_user32 = win32.user32
_user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
_user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def windows_build() -> int:
    try:
        return int(sys.getwindowsversion().build)
    except (AttributeError, ValueError):
        return 0


def exclude_from_capture(hwnd: int) -> bool:
    """Leave the window out of screen captures (SetWindowDisplayAffinity). False where Windows can't, so the
    caller hides the window for a capture instead (and the marks skip their change check)."""
    if not hwnd or windows_build() < EXCLUDE_MIN_BUILD:
        return False
    if not _user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        log.info("Capture exclusion refused (error %d)", win32.last_error())
        return False
    value = wintypes.DWORD(0)
    return bool(_user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(value))) and value.value == WDA_EXCLUDEFROMCAPTURE


def cursor_pos() -> tuple[int, int]:
    """The pointer in physical screen px. Raises OSError when Windows can't say (the secure desktop)."""
    return win32.get_cursor_pos()


def activate(hwnd: int) -> bool:
    """Bring the window to the front (the overlay's Esc fallback). True if it is now the foreground window."""
    if not hwnd:
        return False
    win32.set_foreground_window(hwnd)
    return win32.get_foreground_window() == hwnd


def is_foreground(hwnd: int) -> bool:
    return bool(hwnd) and win32.get_foreground_window() == hwnd


def give_back_foreground(own: int, target: int) -> bool:
    """After the overlay took the foreground, hand it back to `target` if `own` (or nothing) still has it."""
    front = win32.get_foreground_window()
    if front not in (0, own) and not win32.is_own_window(front):
        return False
    if not target or not win32.is_window(target):
        return False
    return win32.set_foreground_window(target)


def short_of_full_screen(rect, screen):
    """`rect` (a QRect), ending one px above the bottom of `screen` if it would cover all of it. Windows takes a
    topmost window the size of its monitor for a full-screen app, and Saylight hides its panel and ruler for
    those."""
    out = rect.adjusted(0, 0, 0, 0)
    if out.contains(screen):
        out.setBottom(screen.bottom() - 1)
    return out


def screen_hidden_by_system() -> bool:
    """True on the lock screen or screen saver, and while a full-screen app or presentation mode is on."""
    return win32.query_user_notification_state() in QUNS_HIDE

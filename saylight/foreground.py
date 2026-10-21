"""ForegroundTracker: remembers the last app the user worked in, so focus can go back to it.

Clicking the tray icon or opening its menu puts the taskbar (or Saylight) in front; restore_foreground()
then hands the foreground back to that app, and the panel's Play path copies from it. appActivated tells
the Type or paste box that the user has gone to another app.
GUI thread only: the WinEvent hook is out-of-context, so Windows calls it from this thread's message loop.
"""

import logging

from PySide6.QtCore import QObject, Signal

from . import win32

log = logging.getLogger("saylight.foreground")


class ForegroundTracker(QObject):
    """Tracks EVENT_SYSTEM_FOREGROUND for every process except ours, ignoring shell windows."""

    appActivated = Signal(int)  # hwnd: another app's window came to the front (not the shell, not the seed)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hook = 0
        self._proc = None  # the ctypes callback; must outlive the hook
        self._last = 0

    def start(self) -> bool:
        """Install the hook (once) and seed last_app() with the current foreground window."""
        if self._hook:
            return True
        self._proc = win32.WINEVENTPROC(self._on_event)
        self._hook = win32.set_win_event_hook(win32.EVENT_SYSTEM_FOREGROUND, win32.EVENT_SYSTEM_FOREGROUND, self._proc,
                                              win32.WINEVENT_OUTOFCONTEXT | win32.WINEVENT_SKIPOWNPROCESS)
        if not self._hook:
            log.warning("SetWinEventHook failed (error %d); focus can't be restored after tray clicks",
                        win32.last_error())
            self._proc = None
            return False
        self._consider(win32.get_foreground_window())
        return True

    def stop(self) -> None:
        if self._hook:
            if not win32.unhook_win_event(self._hook):
                log.warning("UnhookWinEvent failed (error %d)", win32.last_error())
            self._hook = 0
        self._proc = None

    def last_app(self) -> int:
        """The last foreground window that is neither the shell nor ours, if it still exists; else 0."""
        if self._last and not win32.is_window(self._last):
            self._last = 0
        return self._last

    def restore_foreground(self) -> bool:
        """If the shell, Saylight or nothing is in front, bring last_app() back. True if that worked."""
        if win32.is_app_window(win32.get_foreground_window()):
            return False
        target = self.last_app()
        if not target:
            return False
        ok = win32.set_foreground_window(target)
        if not ok:
            log.info("SetForegroundWindow refused")
        return ok

    def _on_event(self, hook, event, hwnd, id_object, id_child, thread_id, time_ms) -> None:
        try:  # an exception must never escape a ctypes callback
            if event == win32.EVENT_SYSTEM_FOREGROUND:
                hwnd = hwnd or 0
                if self._consider(hwnd):
                    self.appActivated.emit(hwnd)
        except Exception:
            log.exception("Foreground event failed")

    def _consider(self, hwnd: int) -> bool:
        """Remember hwnd if it is another app's window; True if it was."""
        if win32.is_app_window(hwnd):
            self._last = hwnd
            return True
        return False

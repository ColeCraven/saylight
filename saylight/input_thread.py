"""InputThread: the dedicated Win32 thread that owns the hotkeys, the quit event and the copy job.

The QObject is created on the GUI thread; its Python thread emits the signals, so every connection is
delivered queued on the GUI thread. The GUI talks to the thread only by posting
WM_APP messages, so every public method is thread-safe and returns at once.
"""

import ctypes
import logging
import math
import threading
import time
from collections.abc import Iterable
from ctypes import wintypes

from PySide6.QtCore import QObject, QTimer, Signal

from . import win32
from .clipboard import LATE_COPY_POLL_S, ClipboardOwner, copy_selection, watch_late_copy

log = logging.getLogger("saylight.input_thread")

HK_READ, HK_PANEL, HK_PAUSE, HK_COMPOSE = 1, 2, 3, 4
HK_READ_SCREEN, HK_RULER = 5, 6
HK_ESCAPE = 20  # registered only while the screen-selection overlay is open
HK_MEDIA_PLAY_PAUSE, HK_MEDIA_STOP, HK_MEDIA_NEXT, HK_MEDIA_PREV = 10, 11, 12, 13
MEDIA_KEYS = {HK_MEDIA_PLAY_PAUSE: win32.VK_MEDIA_PLAY_PAUSE, HK_MEDIA_STOP: win32.VK_MEDIA_STOP,
              HK_MEDIA_NEXT: win32.VK_MEDIA_NEXT_TRACK, HK_MEDIA_PREV: win32.VK_MEDIA_PREV_TRACK}

WM_APP_STOP = win32.WM_APP + 1
WM_APP_COPY = win32.WM_APP + 2        # wParam = token << 1 | is_click, lParam = last_app
WM_APP_MEDIA = win32.WM_APP + 3       # wParam = 1 to register the media keys, 0 to unregister them
WM_APP_REREGISTER = win32.WM_APP + 4  # the new hotkeys dict is handed over under a lock
WM_APP_ESCAPE = win32.WM_APP + 5      # wParam = 1 to register Esc, 0 to unregister it
VK_ESCAPE = 0x1B
# While the selection overlay is up, the input thread checks that the app still answers: from a moment after a
# cancel press, and past the overlay's own 30 s idle timeout.
HANG_CHECK_MS = 1000
CANCEL_GRACE_S = 2.0
IDLE_WATCH_S = 35.0

AUTO_COPY_TOKEN = 0


class InputThread(QObject):
    """Hotkeys, the --quit event and the copy job, on one Win32 thread with its own message loop.

    hotkeys maps an hk_id (HK_READ … HK_RULER) to a shortcut such as "Ctrl+`" (see win32.parse_hotkey).
    "" disables that id; it is unregistered and not reported. Ids 10–13 are reserved for the media keys
    (set_media_keys), and 20 for Esc while the screen-selection overlay is open (set_escape_hotkey).
    """

    ready = Signal()
    registered = Signal(int, bool)          # hk_id, ok: after start() and each set_hotkeys/set_media_keys
    hotkeyPressed = Signal(int)             # hk_id; for HK_READ this comes before its automatic copy
    copied = Signal(int, str, str)          # token (0 = automatic copy after HK_READ), text, reason
    composeReadPressed = Signal()           # HK_READ while the Type or paste box is in front: no copy at all
    quitRequested = Signal()                # the quit event was set (e.g. by --quit)
    overlayCancelPressed = Signal(str)      # "esc" | "hotkey": a press while the selection overlay is open

    def __init__(self, quit_event_handle: int, hotkeys: dict[int, str], parent=None):
        super().__init__(parent)
        self._quit_event = quit_event_handle or 0
        self._hotkeys = dict(hotkeys)
        self._pending: dict[int, str] | None = None
        self._compose_open = False  # written on the GUI thread, read on the input thread
        self._overlay_open = False  # likewise
        self._overlay_hwnds: tuple[int, ...] = ()  # likewise: the overlay's windows
        self._watch_at = 0.0  # input thread only: when to start checking the overlay still answers (0: off)
        self._lock = threading.Lock()
        self._active: dict[int, tuple[int, int]] = {}  # hk_id -> (modifiers, vk); input thread only
        self._thread_id = 0
        self._queue_ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="saylight.input", daemon=True)

    # --- GUI-thread API -------------------------------------------------------

    def start(self) -> None:
        """Start the thread; returns once its message queue exists. `ready` follows the registrations."""
        if self._thread.ident is not None:
            log.warning("InputThread.start() called twice")
            return
        self._thread.start()
        if not self._queue_ready.wait(2.0):
            log.error("Input thread did not start within 2 s")

    def request_copy(self, token: int, last_app: int) -> None:
        """Copy the selection for the panel's Play button; `copied(token, …)` always follows.

        token must fit the signal's 32-bit int (1 … 2**31 − 1; 0 is the automatic copy's token).
        """
        if not 0 < token < 2 ** 31:
            raise ValueError(f"copy token out of range: {token}")
        if not self._post(WM_APP_COPY, (token << 1) | 1, last_app):
            QTimer.singleShot(0, self, lambda: self.copied.emit(token, "", "busy"))

    def set_media_keys(self, on: bool) -> None:
        self._post(WM_APP_MEDIA, 1 if on else 0, 0)

    def set_compose_open(self, on: bool) -> None:
        """While the Type or paste box is open, a Read press with one of Saylight's windows in front emits
        composeReadPressed instead of hotkeyPressed, and nothing is copied. A plain attribute write."""
        self._compose_open = bool(on)

    def set_escape_hotkey(self, on: bool) -> None:
        """Register Esc (HK_ESCAPE) while the selection overlay is open, like set_media_keys. `registered`
        reports the result; Esc may already belong to another app."""
        self._post(WM_APP_ESCAPE, 1 if on else 0, 0)

    def set_overlay_open(self, on: bool, hwnds: Iterable[int] = ()) -> None:
        """While the selection overlay is open, every registered hotkey but the media keys emits only
        overlayCancelPressed ("esc" for Esc, "hotkey" for the rest), and nothing is copied. A plain attribute
        write, so it holds from the next press on.

        `hwnds` are the overlay's windows. They cover every screen and only the GUI thread can close them, so if
        it stops answering while they're up (after a cancel press, or past the idle timeout), the input thread
        ends the process rather than leave the mouse locked out."""
        self._overlay_hwnds = tuple(int(h) for h in hwnds) if on else ()
        self._overlay_open = bool(on)

    def set_hotkeys(self, hotkeys: dict[int, str]) -> None:
        """Re-register with new specs. Unchanged ones stay registered; failed ones are retried."""
        with self._lock:
            if self._thread.ident is None:
                self._hotkeys = dict(hotkeys)  # not started yet: these are what start() registers
                return
            self._pending = dict(hotkeys)
        self._post(WM_APP_REREGISTER, 0, 0)

    def stop(self, timeout: float = 3.0) -> None:
        """Unregister everything on the input thread and join it."""
        if not self._thread.is_alive():
            return
        self._post(WM_APP_STOP, 0, 0)
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.error("Input thread did not stop within %.1f s", timeout)

    def _post(self, message: int, wparam: int, lparam: int) -> bool:
        if not self._queue_ready.is_set() or not self._thread.is_alive():
            log.warning("Input thread is not running; message 0x%04X dropped", message)
            return False
        if not win32.post_thread_message(self._thread_id, message, wparam, lparam):
            log.warning("Couldn't post message 0x%04X to the input thread (error %d)", message, win32.last_error())
            return False
        return True

    # --- Input thread ----------------------------------------------------------

    def _run(self) -> None:
        self._thread_id = win32.get_current_thread_id()
        msg = wintypes.MSG()
        win32.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, win32.PM_NOREMOVE)  # create the queue first
        self._queue_ready.set()
        owner = ClipboardOwner()
        owner.create()
        try:
            self._apply_hotkeys(self._hotkeys)
            self.ready.emit()
            self._loop(msg)
        except Exception:
            log.exception("Input thread failed")
        finally:
            for hk_id in self._active:
                win32.unregister_hotkey(hk_id)
            self._active.clear()
            watch_late_copy()  # a copy that has just landed late is still undone while we own the clipboard
            owner.destroy()
            log.info("Input thread stopped")

    def _loop(self, msg: wintypes.MSG) -> None:
        handles = (wintypes.HANDLE * 1)(self._quit_event or None)
        watch_quit = bool(self._quit_event)
        while True:
            late_s = watch_late_copy()  # after a copy timed out, poll briefly for the app's late answer
            timeout = win32.INFINITE if late_s is None else max(1, math.ceil(min(late_s, LATE_COPY_POLL_S) * 1000))
            if self._watch_at:
                timeout = min(timeout, HANG_CHECK_MS)
            # INPUTAVAILABLE: watch_late_copy() and the WAIT_FAILED branch pump sent messages, and per the docs
            # that PeekMessage can make an already-queued hotkey "not new", which a plain wait would sleep on.
            r = win32.user32.MsgWaitForMultipleObjectsEx(1 if watch_quit else 0, handles, timeout,
                                                         win32.QS_ALLINPUT, win32.MWMO_INPUTAVAILABLE)
            self._check_overlay()
            if r == win32.WAIT_TIMEOUT:
                continue
            if watch_quit and r == win32.WAIT_OBJECT_0:
                watch_quit = False  # a manual-reset event stays signalled; report it once
                log.info("Quit requested")
                self.quitRequested.emit()
                continue
            if r == win32.WAIT_FAILED:
                log.error("MsgWaitForMultipleObjects failed (error %d); no longer watching the quit event",
                          win32.last_error())
                watch_quit = False
                win32.wait_pumping_sent(0.05)
                continue
            while win32.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, win32.PM_REMOVE):
                if msg.message == WM_APP_STOP:
                    return
                self._handle(msg)

    def _handle(self, msg: wintypes.MSG) -> None:
        try:
            if msg.message == win32.WM_HOTKEY:
                self._on_hotkey(int(msg.wParam))
            elif msg.message == WM_APP_COPY:
                self._copy(int(msg.wParam) >> 1, bool(msg.wParam & 1), int(msg.lParam), [])
            elif msg.message == WM_APP_MEDIA:
                self._set_media(bool(msg.wParam))
            elif msg.message == WM_APP_ESCAPE:
                self._set_escape(bool(msg.wParam))
            elif msg.message == WM_APP_REREGISTER:
                with self._lock:
                    hotkeys, self._pending = self._pending, None
                if hotkeys is not None:
                    self._apply_hotkeys(hotkeys)
            else:
                win32.user32.TranslateMessage(ctypes.byref(msg))
                win32.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("Input thread: message 0x%04X failed", msg.message)

    def _on_hotkey(self, hk_id: int) -> None:
        if hk_id not in self._active:
            return  # queued before that hotkey was unregistered
        if self._overlay_open and hk_id not in MEDIA_KEYS:
            # Selecting an area: any of Saylight's shortcuts cancels it, and Ctrl+` must never send Ctrl+C.
            self.overlayCancelPressed.emit("esc" if hk_id == HK_ESCAPE else "hotkey")
            soon = time.monotonic() + CANCEL_GRACE_S
            if not self._watch_at or soon < self._watch_at:
                self._watch_at = soon
            return
        if hk_id == HK_ESCAPE:
            return  # pressed just as the overlay closed
        if hk_id == HK_READ and self._compose_open and win32.is_own_window(win32.get_foreground_window()):
            log.info("Read shortcut routed to the Type or paste box")
            self.composeReadPressed.emit()  # no Ctrl+C is sent, so the clipboard is left alone
            return
        self.hotkeyPressed.emit(hk_id)
        if hk_id == HK_READ:
            self._copy(AUTO_COPY_TOKEN, False, 0, [self._active[HK_READ][1]])

    def _copy(self, token: int, is_click: bool, last_app: int, key_vks: list[int]) -> None:
        try:
            text, reason = copy_selection(is_click=is_click, last_app=last_app, key_vks=key_vks)
        except Exception:
            log.exception("Copy job failed")
            text, reason = "", "busy"
        self.copied.emit(token, text, reason)
        self._drop_read_presses_made_while_copying()

    def _drop_read_presses_made_while_copying(self) -> None:
        """Read presses that queued up during a copy happened in the Copying state, where they're ignored."""
        msg = wintypes.MSG()
        others, dropped = [], 0
        while win32.user32.PeekMessageW(ctypes.byref(msg), None, win32.WM_HOTKEY, win32.WM_HOTKEY, win32.PM_REMOVE):
            if msg.wParam == HK_READ:
                dropped += 1
            else:
                others.append(int(msg.wParam))
        if dropped:
            log.info("Ignored %d Read presses made while copying", dropped)
        for hk_id in others:
            self._on_hotkey(hk_id)

    def _register(self, hk_id: int, modifiers: int, vk: int) -> None:
        ok = win32.register_hotkey(hk_id, modifiers | win32.MOD_NOREPEAT, vk)
        if ok:
            self._active[hk_id] = (modifiers, vk)
        else:
            log.warning("Couldn't register hotkey %d (error %d)", hk_id, win32.last_error())
        self.registered.emit(hk_id, ok)

    def _unregister(self, hk_id: int) -> None:
        win32.unregister_hotkey(hk_id)
        del self._active[hk_id]

    def _apply_hotkeys(self, hotkeys: dict[int, str]) -> None:
        wanted = {hk_id: (spec, win32.parse_hotkey(spec)) for hk_id, spec in hotkeys.items() if spec}
        for hk_id in [h for h in self._active if h not in MEDIA_KEYS and h != HK_ESCAPE]:
            if hk_id not in wanted or wanted[hk_id][1] != self._active[hk_id]:
                self._unregister(hk_id)
        for hk_id, (spec, parsed) in wanted.items():
            if parsed is None:
                log.warning("Hotkey %d: %r is not a valid shortcut", hk_id, spec)
                self.registered.emit(hk_id, False)
            elif hk_id in self._active:
                self.registered.emit(hk_id, True)
            else:
                self._register(hk_id, *parsed)

    def _check_overlay(self) -> None:
        """The last resort while the selection overlay is up: if the GUI thread has stopped answering, end the
        process, since nothing else can take the overlay off the screens."""
        if not self._watch_at or time.monotonic() < self._watch_at:
            return
        hwnds = self._overlay_hwnds if self._overlay_open else ()
        if any(win32.is_hung_window(h) for h in hwnds):
            log.error("Screen: overlay not answering, quitting")
            win32.end_process(1)

    def _set_escape(self, on: bool) -> None:
        self._watch_at = time.monotonic() + IDLE_WATCH_S if on else 0.0  # the overlay opened, or closed
        if on and HK_ESCAPE not in self._active:
            self._register(HK_ESCAPE, 0, VK_ESCAPE)
        elif not on and HK_ESCAPE in self._active:
            self._unregister(HK_ESCAPE)

    def _set_media(self, on: bool) -> None:
        for hk_id, vk in MEDIA_KEYS.items():
            if on and hk_id not in self._active:
                self._register(hk_id, 0, vk)
            elif not on and hk_id in self._active:
                self._unregister(hk_id)

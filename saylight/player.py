"""MCI playback on a dedicated thread: the only thread in the app that sends MCI commands.

MCI aliases belong to the thread that opened them, so every command goes
through a queue to the "player" thread. Each load opens a fresh device with a unique alias.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("saylight.player")

POLL_S = 0.050               # status polling, and the 20 Hz tick
FAST_POLL_S = 0.010          # while a cut is less than FAST_POLL_WINDOW_MS away
FAST_POLL_WINDOW_MS = 300
END_SLACK_MS = 50            # ended: not playing and pos >= length - END_SLACK_MS ...
SAFETY_MS = 3000             # ... or the playing-time clock passes length + SAFETY_MS
VOLUME_INTERVAL_S = 1 / 30   # volume changes are applied at most 30 times a second
MAX_CLOCK_STEP_S = 0.5       # a longer gap between polls (sleep, starvation) is not counted as playing
MCI_MAX_PATH = 250           # MCI open fails with error 268 from about 256 characters (measured)

winmm = ctypes.WinDLL("winmm")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _sig(func, restype, *argtypes):
    func.restype = restype
    func.argtypes = argtypes


_sig(winmm.mciSendStringW, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE)
_sig(winmm.mciGetErrorStringW, wintypes.BOOL, wintypes.DWORD, wintypes.LPWSTR, wintypes.UINT)
_sig(kernel32.GetShortPathNameW, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)

_aliases = itertools.count(1)


class MciError(RuntimeError):
    """An MCI command failed (a playback error)."""

    def __init__(self, code: int, command: str):
        buf = ctypes.create_unicode_buffer(256)
        winmm.mciGetErrorStringW(code, buf, len(buf))
        verb = " ".join(command.split()[:2]) if not command.startswith("open") else "open"
        super().__init__(f"MCI error {code} on {verb}: {buf.value or 'unknown error'}")
        self.code = code


def _mci(command: str) -> str:
    buf = ctypes.create_unicode_buffer(256)
    err = winmm.mciSendStringW(command, buf, len(buf), None)
    if err:
        raise MciError(err, command)
    return buf.value


def _mci_quiet(command: str) -> None:
    winmm.mciSendStringW(command, None, 0, None)


def _mci_path(path: str) -> str:
    """The path to give MCI: the 8.3 short form when the full path is too long for it."""
    if len(path) < MCI_MAX_PATH:
        return path
    buf = ctypes.create_unicode_buffer(1024)
    n = kernel32.GetShortPathNameW(path, buf, len(buf))
    return buf.value if 0 < n < len(buf) else path


def mci_volume(v: int) -> int:
    """MCI volume (0-1000) for a 0-100 setting. The one place to change the curve."""
    return round(10 * max(0, min(100, v)))


@dataclass(eq=False)
class _Device:
    chunk_id: int
    alias: str
    cut: int | None = None
    length: int = 0
    pos: int = 0
    state: str = "idle"            # idle (stopped at pos) | playing | paused
    has_played: bool = False       # since the load or the last seek: decides resume vs play
    started_sent: bool = False
    anchor: int = 0                # position where the playing-time clock was last reset
    clock_ms: float = 0.0          # playing time since the anchor, paused time excluded
    last: float = 0.0


class PlayerThread(QObject):
    """Plays one chunk MP3 at a time, reports position, and detects end and cut points."""

    started = Signal(int, int)        # chunk_id, length_ms: first actual play after a load
    tick = Signal(int, int, bool)     # chunk_id, pos_ms, playing: 20 Hz while loaded
    ended = Signal(int, str)          # chunk_id, "end" | "cut" | "stopped"
    failed = Signal(int, str)         # chunk_id, MCI error

    def __init__(self, parent: QObject | None = None, *, volume: int = 100):
        super().__init__(parent)
        self._q: queue.Queue[tuple] = queue.Queue()
        self._volume = self._clamp(volume)
        self._thread = threading.Thread(target=self._run, name="player", daemon=True)
        self._dev: _Device | None = None
        self._vol_dirty = False
        self._vol_at = 0.0
        self._last_tick = 0.0

    # --- Public API (thread-safe) -----------------------------------------------------

    def start(self) -> None:
        if not self._thread.is_alive() and self._thread.ident is None:
            self._thread.start()

    def load(self, chunk_id: int, path: str, *, start_ms: int = 0, play: bool = True,
             cut_ms: int | None = None) -> None:
        """Open `path` (closing any loaded chunk with ended(prev, "stopped")), seek, and maybe play."""
        self._q.put(("load", chunk_id, path, int(start_ms), bool(play), None if cut_ms is None else int(cut_ms)))

    def play(self) -> None:
        """Start or continue playback (MCI resume after a pause, play after a load or seek)."""
        self._q.put(("play",))

    def pause(self) -> None:
        self._q.put(("pause",))

    def resume(self) -> None:
        """Same as play(): resume only if the device has played since the load or seek."""
        self._q.put(("play",))

    def seek(self, ms: int, play: bool) -> None:
        """Move to `ms` in the loaded chunk. MCI seek leaves the device stopped; play=True restarts it."""
        self._q.put(("seek", int(ms), bool(play)))

    def set_cut(self, ms: int | None) -> None:
        """Stop the loaded chunk with ended("cut") once it plays past `ms`. None clears it."""
        self._q.put(("cut", None if ms is None else int(ms)))

    def set_volume(self, v: int) -> None:
        """0-100. Throttled to 30 Hz and re-applied after every open."""
        self._volume = self._clamp(v)
        self._q.put(("volume",))

    def stop_close(self) -> None:
        """Stop and close the loaded chunk, emitting ended(chunk, "stopped"). No-op if nothing is loaded."""
        self._q.put(("stop_close",))

    def shutdown(self, timeout: float = 2.0) -> None:
        """Close the device without signals and join the thread."""
        self._q.put(("quit",))
        if self._thread.is_alive():
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("Player thread did not stop within %.1f s", timeout)

    def _run_on_thread(self, fn: Callable, timeout: float = 2.0):
        """Test hook: run fn() on the player thread (where the MCI aliases live) and return its result."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put(("call", fn, fut))
        return fut.result(timeout)

    @staticmethod
    def _clamp(v: int) -> int:
        return max(0, min(100, int(round(v))))

    # --- Player thread ------------------------------------------------------------------

    def _run(self) -> None:
        next_poll = last_poll = 0.0
        try:
            while True:
                timeout = None if self._dev is None else max(0.0, next_poll - time.monotonic())
                try:
                    cmd = self._q.get(timeout=timeout)
                except queue.Empty:
                    cmd = None
                if cmd is not None and cmd[0] == "quit":
                    break
                try:
                    if cmd is not None:
                        before = self._dev
                        self._handle(cmd)
                        if self._dev is not before:
                            next_poll = 0.0      # report a new chunk at once
                        elif self._dev is not None:
                            # a cut set (or play resumed) close ahead needs the 10 ms rate now,
                            # not after the 50 ms poll that is already scheduled
                            next_poll = min(next_poll, last_poll + self._interval())
                    now = time.monotonic()
                    if self._dev is not None and self._vol_dirty and now - self._vol_at >= VOLUME_INTERVAL_S:
                        self._apply_volume(self._dev)
                    if self._dev is not None and now >= next_poll:
                        self._poll(now)
                        last_poll, next_poll = now, now + self._interval()
                except MciError as e:
                    self._fail(e, cmd)
                except Exception as e:  # noqa: BLE001 - never let the MCI thread die silently
                    log.exception("Player error")
                    self._fail(e, cmd)
        finally:
            if self._dev is not None:
                _mci_quiet(f"stop {self._dev.alias}")
                _mci_quiet(f"close {self._dev.alias}")
                self._dev = None

    def _handle(self, cmd: tuple) -> None:
        name, dev = cmd[0], self._dev
        if name == "load":
            self._load(*cmd[1:])
        elif name == "call":
            fn, fut = cmd[1], cmd[2]
            try:
                fut.set_result(fn())
            except Exception as e:  # noqa: BLE001 - handed to the caller
                fut.set_exception(e)
        elif name == "volume":
            self._vol_dirty = True
        elif name == "stop_close":
            self._close("stopped")
        elif dev is None:
            log.debug("Ignored %s: nothing loaded", name)
        elif name == "play":
            self._play(dev)
        elif name == "pause":
            self._pause(dev)
        elif name == "seek":
            self._seek(dev, cmd[1], cmd[2])
        elif name == "cut":
            dev.cut = cmd[1]

    def _load(self, chunk_id: int, path: str, start_ms: int, play: bool, cut_ms: int | None) -> None:
        self._close("stopped")
        alias = f"ra_{next(_aliases)}"
        try:
            _mci(f'open "{_mci_path(path)}" type mpegvideo alias {alias}')
        except MciError as e:
            log.warning("Chunk %d: %s", chunk_id, e)
            self.failed.emit(chunk_id, str(e))
            return
        dev = self._dev = _Device(chunk_id, alias, cut=cut_ms)
        _mci(f"set {alias} time format milliseconds")
        dev.length = int(_mci(f"status {alias} length") or 0)
        self._apply_volume(dev)                      # always before any play
        if start_ms > 0:
            dev.pos = dev.anchor = min(start_ms, dev.length)
            _mci(f"seek {alias} to {dev.pos}")
        log.debug("Loaded chunk %d as %s (%d ms)", chunk_id, alias, dev.length)
        if play:
            self._play(dev)

    def _apply_volume(self, dev: _Device) -> None:
        _mci(f"setaudio {dev.alias} volume to {mci_volume(self._volume)}")
        self._vol_dirty = False
        self._vol_at = time.monotonic()

    def _play(self, dev: _Device) -> None:
        if dev.state == "playing":
            return
        # resume is only relied on after a pause; a device that hasn't played since its load or
        # seek gets play instead.
        _mci(f"resume {dev.alias}" if dev.state == "paused" and dev.has_played else f"play {dev.alias}")
        dev.state, dev.has_played, dev.last = "playing", True, time.monotonic()
        if not dev.started_sent:
            dev.started_sent = True
            self.started.emit(dev.chunk_id, dev.length)

    def _pause(self, dev: _Device) -> None:
        if dev.state != "playing":
            return
        _mci(f"pause {dev.alias}")
        self._advance_clock(dev, time.monotonic())
        dev.state = "paused"

    def _seek(self, dev: _Device, ms: int, play: bool) -> None:
        ms = max(0, min(ms, dev.length))
        _mci(f"seek {dev.alias} to {ms}")
        dev.pos = dev.anchor = ms
        dev.clock_ms, dev.state, dev.has_played = 0.0, "idle", False
        if play:
            self._play(dev)

    def _poll(self, now: float) -> None:
        dev = self._dev
        dev.pos = int(_mci(f"status {dev.alias} position") or 0)
        playing = dev.state == "playing"
        if playing:
            self._advance_clock(dev, now)
            if dev.cut is not None and dev.pos >= dev.cut:
                self._close("cut")
                return
            if dev.anchor + dev.clock_ms > dev.length + SAFETY_MS or (
                    dev.pos >= dev.length - END_SLACK_MS and _mci(f"status {dev.alias} mode") != "playing"):
                self._close("end")
                return
        if now - self._last_tick >= POLL_S - 0.005:
            self._last_tick = now
            self.tick.emit(dev.chunk_id, dev.pos, playing)

    def _interval(self) -> float:
        dev = self._dev
        if (dev is not None and dev.state == "playing" and dev.cut is not None
                and dev.cut - dev.pos < FAST_POLL_WINDOW_MS):
            return FAST_POLL_S
        return POLL_S

    @staticmethod
    def _advance_clock(dev: _Device, now: float) -> None:
        dev.clock_ms += min(now - dev.last, MAX_CLOCK_STEP_S) * 1000
        dev.last = now

    def _close(self, why: str) -> None:
        dev, self._dev = self._dev, None
        if dev is None:
            return
        if why != "end":
            _mci_quiet(f"stop {dev.alias}")
        _mci_quiet(f"close {dev.alias}")
        log.debug("Chunk %d closed (%s) at %d of %d ms", dev.chunk_id, why, dev.pos, dev.length)
        self.ended.emit(dev.chunk_id, why)

    def _fail(self, err: Exception, cmd: tuple | None) -> None:
        """Close the loaded chunk and emit failed for it, or for a load that failed before it opened."""
        dev, self._dev = self._dev, None
        if dev is not None:
            _mci_quiet(f"stop {dev.alias}")
            _mci_quiet(f"close {dev.alias}")
            chunk_id = dev.chunk_id
        elif cmd is not None and cmd[0] == "load":
            chunk_id = cmd[1]
        else:
            return
        log.warning("Chunk %d: %s", chunk_id, err)
        self.failed.emit(chunk_id, str(err))

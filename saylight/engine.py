"""Reading engine for Saylight: the GUI-thread state machine.

Covers the reading states, the controls and copy path, seeking, speed changes, and timing, progress
and privacy. There is one shortcut: Ctrl+` pauses at once, then reads the new selection, resumes, or
toggles the panel. Text typed or pasted into Saylight is read through read_text(); clear() forgets the
text and hands back a token that restore() can bring it back from.

The synth, player, input thread and settings are duck-typed. The engine connects to their signals
itself; if the app also connects input_thread.hotkeyPressed / copied to the on_hotkey / on_copied
slots, those duplicate deliveries are recognised and ignored. The synth and player are started by
start(), or on the first event-loop pass if the app never calls it. The synth gets the speed as a
multiplier (0.95); a synth with limits_for(voice) sets the chunk sizes, and one with new_reading() hears
when each reading starts. Timers come from an injectable factory so tests can drive them with a fake
clock. Never logs text, only counts and ids.
"""

import logging
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .text_model import ChunkPlan, ChunkTiming, Document, map_words, plan_chunks
from .voices.base import DEFAULT_VOICE_ID

log = logging.getLogger("saylight.engine")

# Hotkey ids, as input_thread reports them
HK_READ, HK_PANEL, HK_PAUSE = 1, 2, 3
HK_MEDIA_PLAY_PAUSE, HK_MEDIA_STOP, HK_MEDIA_NEXT, HK_MEDIA_PREV = 10, 11, 12, 13

# States
READY, READY_OFFLINE, SHORTCUT_UNAVAILABLE = "ready", "ready_offline", "shortcut_unavailable"
COPYING, PREPARING, READING, WAITING, PAUSED = "copying", "preparing", "reading", "waiting", "paused"
FINISHED, STOPPED, NOTHING_SELECTED = "finished", "stopped", "nothing_selected"
ERROR_OFFLINE, ERROR_SERVICE, ERROR_PLAYBACK = "error_offline", "error_service", "error_playback"

IDLE = frozenset({READY, READY_OFFLINE, SHORTCUT_UNAVAILABLE})
ERRORS = frozenset({ERROR_OFFLINE, ERROR_SERVICE, ERROR_PLAYBACK})
RESTING = IDLE | ERRORS | {FINISHED, STOPPED}          # Nothing selected is an overlay on one of these
SESSION = frozenset({PREPARING, READING, WAITING, PAUSED})  # a reading is running
ACTIVE = SESSION | {COPYING}

LABELS = {
    READY: "Ready", READY_OFFLINE: "Offline", SHORTCUT_UNAVAILABLE: "Shortcut unavailable",
    COPYING: "Copying selection…", PREPARING: "Preparing audio…", READING: "Reading",
    WAITING: "Preparing audio…", PAUSED: "Paused", FINISHED: "Finished", STOPPED: "Stopped",
    NOTHING_SELECTED: "Nothing selected", ERROR_OFFLINE: "Can't reach the voice service",
    ERROR_SERVICE: "Voice service error", ERROR_PLAYBACK: "Couldn't play audio",
}
_PLAY_ACTIONS = {READY: "read", READY_OFFLINE: "read", SHORTCUT_UNAVAILABLE: "read", FINISHED: "read",
                 STOPPED: "read", COPYING: "none", PREPARING: "pause", READING: "pause", WAITING: "pause",
                 PAUSED: "resume"}  # errors: "retry"
_PREV_ACTIONS = {READING: "prev", WAITING: "prev", PAUSED: "prev", FINISHED: "read_last_again",
                 STOPPED: "read_from"}

SPEEDS = (0.5, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.5, 1.75, 2.0)
DEFAULT_VOICE = DEFAULT_VOICE_ID

COPY_SHOW_MS = 150            # Copying is shown only after this long, so quick copies don't flicker
COPY_WATCHDOG_MS = 5000       # give up on a copy result that never comes
WAITING_SHOW_MS = 300         # Waiting is shown only after this long when it interrupts Reading
SEEK_DELAY_MS = 150           # audio acts this long after the last seek press
SEEK_LEAD_MS = 80             # seek to max(prev_end, start - 80 ms)
SEEK_ACK_MS = 300             # a tick this close to a sent seek's target shows the player has applied it
SEEK_ACK_TIMEOUT_MS = 1000    # after this long, ticks are trusted again even without that confirmation
RESTART_AFTER_MS = 1500       # Previous restarts the sentence when further in than this
SPEED_COMMIT_MS = 400         # a new speed is committed this long after the last change
NOTE_MS = 3000                # "from next reading" note
NOTHING_SELECTED_MS = 3000    # the Nothing-selected overlay
PREFETCH_RETRY_MS = 1500      # silent retry of a failed prefetch
PURGE_MS = 10 * 60 * 1000     # privacy purge after this long at rest
MEDIA_PAUSE_MS = 10 * 60 * 1000  # media keys stay registered this long into a pause
VOLUME_INTERVAL_MS = 33       # volume changes throttled to 30 Hz
FIRST_LIMIT, LIMIT = 250, 1500  # chunk sizes in characters for a synth without limits_for()
PREFETCH = 2                  # at most this many unplayed chunks requested or ready
MAX_CACHED_CHUNKS = 24        # ready chunks kept for Previous / jumps (bounded temp disk use)
DEFAULT_MS_PER_CHAR = 65.0    # length estimate at 1.0x before anything has been measured
MPC_ALPHA = 0.3               # EMA weight of a new ms-per-character measurement
MAX_TEXT_CHARS = 100_000      # typed or pasted text beyond this is not read


def snap_speed(v: float) -> float:
    """The nearest allowed speed (ties go to the slower one)."""
    return min(SPEEDS, key=lambda s: (abs(s - v), s))


def speed_label(v: float) -> str:
    """'0.95×', '1.0×', '0.5×': at least one decimal."""
    text = f"{v:.2f}".rstrip("0")
    return (text + "0" if text.endswith(".") else text) + "×"


def rate_string(v: float) -> str:
    """edge-tts rate for a speed multiplier: 0.95 -> '-5%'."""
    return f"{round((v - 1) * 100):+d}%"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class _QtTimer:
    """A one-shot QTimer behind the small interface the engine uses."""

    def __init__(self, parent: QObject, callback: Callable[[], None]):
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(callback)

    def start(self, ms: float) -> None:
        self._timer.start(max(0, int(ms)))

    def stop(self) -> None:
        self._timer.stop()

    def is_active(self) -> bool:
        return self._timer.isActive()


@dataclass(eq=False)
class _Chunk:
    """One synthesis unit and its audio file."""
    plan: ChunkPlan
    speed: float
    gen: int
    status: str = "requested"   # requested | ready | failed
    path: str = ""
    words: list = field(default_factory=list)
    timing: ChunkTiming | None = None
    length_ms: int | None = None
    started: bool = False       # has started playing since its latest load
    retried: bool = False
    fail_kind: str = ""

    @property
    def id(self) -> int:
        return self.plan.chunk_id

    def contains(self, sentence: int) -> bool:
        return self.plan.first <= sentence <= self.plan.last


@dataclass(frozen=True)
class ClearedReading:
    """What clear() took away, so restore() can bring it back. Memory only; the app drops it after 10 s."""
    doc: Document
    sentence: int
    finished: bool


@dataclass
class _Press:
    """A Ctrl+` press whose automatic copy result has not arrived yet."""
    optimistic: bool = False    # this press paused the reading
    copying: bool = False       # this press started the Copying state
    ignored: bool = False       # drop its result (pressed while copying, or the copy was cancelled)


class Engine(QObject):
    """The reading state machine. Lives on the GUI thread; every slot is GUI-thread only."""

    stateChanged = Signal(str, dict)
    sessionChanged = Signal(object)
    position = Signal(int, int, int, float, float, bool)
    timeLeft = Signal(int)
    speedChanged = Signal(float, str)
    volumeChanged = Signal(int)
    notice = Signal(str)
    panelToggleRequested = Signal()
    mediaKeysWanted = Signal(bool)

    def __init__(self, *, synth, player, input_thread, settings, clock=time.monotonic, parent=None,
                 timer_factory: Callable[[Callable[[], None]], object] | None = None,
                 beep: Callable[[str], None] | None = None):
        super().__init__(parent)
        self._synth, self._player, self._input, self._settings = synth, player, input_thread, settings
        self._clock = clock
        self._beep = beep
        make = timer_factory or (lambda callback: _QtTimer(self, callback))
        self._copy_show_timer = make(self._on_copy_show)
        self._copy_watchdog = make(self._on_copy_timeout)
        self._waiting_timer = make(self._on_waiting_show)
        self._seek_timer = make(self._do_seek)
        self._commit_timer = make(self._commit_speed)
        self._note_timer = make(self._clear_pending)
        self._overlay_timer = make(self._on_overlay_expired)
        self._purge_timer = make(self._on_purge_due)
        self._media_timer = make(self._update_media_keys)
        self._volume_timer = make(self._send_volume)
        self._retry_timer = make(self._on_retry_due)
        self._start_timer = make(self._auto_start)
        self._timers = (self._copy_show_timer, self._copy_watchdog, self._waiting_timer, self._seek_timer,
                        self._commit_timer, self._note_timer, self._overlay_timer, self._purge_timer,
                        self._media_timer, self._volume_timer, self._retry_timer, self._start_timer)

        # session
        self._doc: Document | None = None
        self._chunks: dict[int, _Chunk] = {}
        self._gen = 0
        self._next_id = 1
        self._cur = 0                    # current (or target) sentence
        self._floor = 0                  # the sentence playback was last started from
        self._loaded: int | None = None  # chunk id loaded in the player
        self._open: Counter[int] = Counter()   # loads not yet confirmed closed by player.ended
        self._doomed: dict[int, str] = {}      # files to delete once the player has closed them
        self._pos_ms = 0.0
        self._played_idx = 0.0
        self._played_any = False
        self._after_cut: int | None = None     # next sentence after the playing chunk is cut
        self._seek_target: int | None = None
        self._seek_ack: tuple[int, float] | None = None  # (ms, sent at): a seek of the loaded chunk no tick showed yet
        self._retry_due: dict[int, float] = {}

        # state
        self._state = READY
        self._rest = READY               # the resting state under the Nothing-selected overlay
        self._reason = ""
        self._mid_session = False
        self._back_online = False
        self._online = True
        self._shortcut_ok = True
        self._shown: tuple[str, dict] | None = None
        self._hold_waiting = False
        self._copy_shown = False
        self._copy_token = 0
        self._copy_source = ""
        self._copy_prev = READY
        self._copy_press: _Press | None = None
        self._presses: deque[_Press] = deque(maxlen=8)  # presses whose copy result is still to come

        # speed, volume, time left
        self._committed = snap_speed(float(self._get("speed", 0.95)))
        self._speed = self._rate = self._committed
        self._speed_pending = False
        self._note = ""
        self._volume = int(_clamp(round(float(self._get("volume", 100))), 0, 100))
        self._volume_sent_at = float("-inf")
        self._media_on = False
        self._mpc: float | None = None
        self._mpc_speed = 1.0
        self._time_left_at = float("-inf")
        self._time_left_value: int | None = None
        self._started = False
        self._shut = False

        synth.chunkReady.connect(self._on_chunk_ready)
        synth.chunkFailed.connect(self._on_chunk_failed)
        player.started.connect(self._on_started)
        player.tick.connect(self._on_tick)
        player.ended.connect(self._on_ended)
        player.failed.connect(self._on_player_failed)
        input_thread.hotkeyPressed.connect(self._on_input_hotkey)
        input_thread.copied.connect(self._on_input_copied)
        input_thread.registered.connect(self._on_registered)
        settings.changed.connect(self._on_setting_changed)
        self._start_timer.start(0)  # a safety net in case the app never calls start()

    # --- Properties ----------------------------------------------------------------------------

    @property
    def state(self) -> str:
        """The internal state (may be ahead of the displayed one; see display_state)."""
        return self._state

    @property
    def display_state(self) -> str:
        """The state last sent with stateChanged."""
        return self._shown[0] if self._shown else self._state

    @property
    def document(self) -> Document | None:
        return self._doc

    @property
    def current_sentence(self) -> int:
        return self._cur if self._doc is not None else -1

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def speed_pending(self) -> bool:
        return self._speed_pending

    @property
    def pending_note(self) -> str:
        return self._note

    @property
    def volume(self) -> int:
        return self._volume

    # --- Lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        """Start the synth and player workers and announce the initial state.

        Call it once the views are connected. If it is never called, the engine calls it itself on the
        first event-loop pass. Later calls do nothing.
        """
        if self._started or self._shut:
            return
        self._started = True
        self._start_timer.stop()
        self._synth.start()
        self._player.start()
        self._send_volume()
        self.speedChanged.emit(self._speed, "")
        self.volumeChanged.emit(self._volume)
        self.timeLeft.emit(-1)
        self._publish()

    def _auto_start(self) -> None:
        if not self._started and not self._shut:
            log.info("Engine.start() was not called; starting the synth and player now")
            self.start()

    @Slot()
    def shutdown(self) -> None:
        """Stop everything, forget the text, and shut the synth and player down (input is the app's)."""
        if self._shut:
            return
        for timer in self._timers:
            timer.stop()
        self._purge()
        self._set_media(False)
        self._shut = True
        self._player.shutdown(2.0)
        self._synth.shutdown(2.0)
        log.info("Engine shut down")

    # --- Input: hotkeys and the copy path ------------------------------------------------------

    @Slot(int)
    def on_hotkey(self, hk_id: int) -> None:
        """A hotkey press. The engine already receives input_thread.hotkeyPressed itself,
        so a delivery from that same signal through this slot is a duplicate connection and is ignored."""
        if self.sender() is self._input:
            return
        self._handle_hotkey(hk_id)

    @Slot(int, str, str)
    def on_copied(self, token: int, text: str, reason: str) -> None:
        """A copy result; duplicate deliveries from input_thread.copied are ignored."""
        if self.sender() is self._input:
            return
        self._handle_copied(token, text, reason)

    @Slot(int)
    def _on_input_hotkey(self, hk_id: int) -> None:
        self._handle_hotkey(hk_id)

    @Slot(int, str, str)
    def _on_input_copied(self, token: int, text: str, reason: str) -> None:
        self._handle_copied(token, text, reason)

    def _handle_hotkey(self, hk_id: int) -> None:
        if self._shut:
            return
        if hk_id == HK_READ:
            self._on_read_hotkey()
        elif hk_id == HK_PANEL:
            self.panelToggleRequested.emit()
        elif hk_id in (HK_PAUSE, HK_MEDIA_PLAY_PAUSE):
            self.toggle_pause()
        elif hk_id == HK_MEDIA_STOP:
            self.stop()
        elif hk_id == HK_MEDIA_NEXT:
            self.next()
        elif hk_id == HK_MEDIA_PREV:
            self.prev()

    def _on_read_hotkey(self) -> None:
        state = self._state
        if state == COPYING:
            self._presses.append(_Press(ignored=True))
            log.info("Read shortcut ignored while copying")
            return
        press = _Press(optimistic=state in (PREPARING, READING, WAITING))
        self._presses.append(press)
        if press.optimistic:
            self._pause()          # pausing feels instant; the copy result decides what happens next
        elif state != PAUSED:      # a resting state (or the Nothing-selected overlay)
            self._clear_overlay()
            press.copying = True
            self._begin_copy("hotkey", press)

    @Slot(int)
    def read_selection_click(self, last_app: int) -> None:
        """The panel's Play button in a resting state: copy through the input thread (click source)."""
        if self._shut:
            return
        if self._underlying() not in RESTING:
            log.info("Read selection ignored in state %s", self._state)
            return
        self._clear_overlay()
        self._copy_token += 1
        self._begin_copy("click")
        self._input.request_copy(self._copy_token, int(last_app or 0))

    def _handle_copied(self, token: int, text: str, reason: str) -> None:
        if self._shut:
            return
        press = None
        if token == 0:
            press = self._presses.popleft() if self._presses else _Press()
            if press.ignored or (self._state == COPYING and not press.copying):
                # pressed during Copying, cancelled by Stop or the watchdog, or overtaken by a Play click
                log.info("Dropped the copy result of an ignored or cancelled press (%s)", reason)
                return
        elif token != self._copy_token or self._state != COPYING or self._copy_source != "click":
            log.info("Dropped a stale copy result (%s)", reason)
            return
        source = "hotkey" if press is not None else "click"
        log.info("Copy result: source=%s reason=%s, %d characters", source, reason, len(text))
        if self._state == COPYING and (press is None or press.copying):
            self._end_copy()

        unspeakable = False
        if reason == "ok":
            if text.strip() and self._new_session(text):
                return
            unspeakable, reason = bool(text.strip()), "empty"
        gesture = reason in ("empty", "line_copy") and not unspeakable  # an empty press
        if press is None:                        # click path: always tell the user
            self._show_nothing_selected(reason)
        elif press.optimistic:                   # stay paused (or wherever the user went since)
            self._publish()
        elif press.copying:                      # pressed at rest: toggle the panel, no beep, no notice
            if gesture:
                self._publish()
                self.panelToggleRequested.emit()
            else:
                self._show_nothing_selected(reason)
        elif self._state == PAUSED and gesture:  # pressed while paused: resume
            self._resume()

    def _begin_copy(self, source: str, press: _Press | None = None) -> None:
        self._copy_source, self._copy_press = source, press
        self._copy_prev = self._state
        self._copy_shown = False
        self._copy_show_timer.start(COPY_SHOW_MS)
        self._copy_watchdog.start(COPY_WATCHDOG_MS)
        self._set_state(COPYING)

    def _end_copy(self) -> None:
        """Leave Copying for the previous resting state, without publishing (the caller does)."""
        self._copy_show_timer.stop()
        self._copy_watchdog.stop()
        self._copy_shown = False
        self._copy_press = None
        self._state = self._copy_prev

    def _cancel_copy(self, *, publish: bool = True) -> None:
        """Stop, the watchdog, typed text or Clear in Copying: the pending result goes stale."""
        if self._copy_source == "click":
            self._copy_token += 1
        elif self._copy_press is not None:
            self._copy_press.ignored = True
        self._end_copy()
        if publish:
            self._publish()

    def _on_copy_show(self) -> None:
        if self._state == COPYING:
            self._copy_shown = True
            self._publish()

    def _on_copy_timeout(self) -> None:
        if self._state == COPYING:
            log.warning("No copy result after %d ms; cancelled", COPY_WATCHDOG_MS)
            self._cancel_copy()

    def _show_nothing_selected(self, reason: str) -> None:
        self._rest = self._underlying()
        self._reason = reason
        self._overlay_timer.start(NOTHING_SELECTED_MS)
        self._set_state(NOTHING_SELECTED)
        self._do_beep("ok")
        self.notice.emit("nothing_selected")

    def _clear_overlay(self) -> None:
        if self._state == NOTHING_SELECTED:
            self._overlay_timer.stop()
            self._state = self._rest

    def _on_overlay_expired(self) -> None:
        if self._state == NOTHING_SELECTED:
            self._set_state(self._rest)

    def _underlying(self) -> str:
        return self._rest if self._state == NOTHING_SELECTED else self._state

    # --- Input: typed text, Clear and Undo -----------------------------------------------------

    @Slot(str, result=bool)
    def read_text(self, text: str) -> bool:
        """Read text handed over directly (Type or paste), the same way as a successful copy.

        Returns False, changing nothing, when there is nothing to read; a reading in progress goes on.
        """
        if self._shut or not text or not text.strip():
            return False
        text = text[:MAX_TEXT_CHARS]
        doc = self._make_doc(text)
        if not doc.sentences:
            log.info("Nothing to read in %d typed characters", len(text))
            return False
        for press in self._presses:  # a Ctrl+` copy still running must not replace this reading
            press.ignored = True
        if self._state == COPYING:
            self._cancel_copy(publish=False)
        self._clear_overlay()
        log.info("Reading typed text: %d characters", len(text))
        return self._new_session(text, doc)

    def clear(self) -> ClearedReading | None:
        """Clear: stop, forget the text and its audio, and go back to Ready (or Offline / Shortcut unavailable).

        Returns a token for restore(), or None when there was no text. With nothing to clear it does nothing.
        """
        if self._shut:
            return None
        for press in self._presses:  # a Ctrl+` copy still running must not start a reading after Clear
            press.ignored = True
        if self._doc is None and self._state in IDLE:
            return None
        if self._state == COPYING:
            self._cancel_copy(publish=False)
        self._clear_overlay()
        doc = self._doc
        token = ClearedReading(doc, self._cur, self._state == FINISHED) if doc is not None else None
        self._settle_speed()  # commit a waiting change now; its note no longer applies
        self._purge()
        self._mid_session = self._back_online = False
        self._reason = ""
        self._set_state(self._base_idle())
        if token is not None:
            log.info("Cleared (%d sentences)", len(token.doc.sentences))
        return token

    def restore(self, token: ClearedReading | None) -> bool:
        """Undo a Clear: the text comes back Stopped at the same sentence, or Finished, with no audio.

        Refused (False) once another text is loaded, while copying, or for no token.
        """
        if self._shut or token is None or self._doc is not None or self._underlying() not in IDLE:
            return False
        self._clear_overlay()
        self._doc, self._time_left_value = token.doc, None
        self.sessionChanged.emit(self._doc)
        self._cur = self._floor = _clamp(token.sentence, 0, len(self._doc.sentences) - 1)
        self._played_any = False
        self._set_state(FINISHED if token.finished else STOPPED)  # also starts the privacy purge timer
        self._emit_position()
        self._emit_time_left(force=True)
        log.info("Clear undone")
        return True

    # --- Input: transport ----------------------------------------------------------------------

    @Slot()
    def toggle_pause(self) -> None:
        """Play/Pause: pause or resume a reading; Try again in an error; else nothing."""
        if self._shut:
            return
        state = self._underlying()
        if state in (PREPARING, READING, WAITING):
            self._pause()
        elif state == PAUSED:
            self._resume()
        elif state in ERRORS:
            self.retry()

    @Slot(result=bool)
    def pause(self) -> bool:
        """Pause a reading that is preparing, playing or waiting; never resumes. True if it paused."""
        if self._shut or self._underlying() not in (PREPARING, READING, WAITING):
            return False
        self._pause()
        return True

    @Slot(result=bool)
    def resume(self) -> bool:
        """Resume a paused reading; never pauses. True if it resumed."""
        if self._shut or self._underlying() != PAUSED:
            return False
        self._resume()
        return True

    def _pause(self) -> None:
        if self._loaded is not None:
            self._player.pause()
        self._set_state(PAUSED)
        self.notice.emit("paused")

    def _resume(self) -> None:
        self._flush_seek()
        if self._loaded is not None:
            self._player.resume()          # the player uses `play` if this load never played
            self._set_state(READING)
        else:
            self._set_state(WAITING if self._played_any else PREPARING)
        self.notice.emit("resumed")
        self._prefetch()

    @Slot()
    def stop(self) -> None:
        """Stop: Copying returns to the previous resting state; a reading goes to Stopped."""
        if self._shut:
            return
        state = self._underlying()
        if state == COPYING:
            self._cancel_copy()
        elif state in SESSION:
            for press in self._presses:  # a Ctrl+` copy still running must not start a reading after Stop
                press.ignored = True
            if self._seek_target is not None:
                self._seek_timer.stop()
                self._seek_target = None
            self._bump_gen()
            self._close_player()
            self._after_cut = None
            self._set_state(STOPPED)
            self.notice.emit("stopped")
        elif state in ERRORS:
            self._clear_overlay()
            self._set_state(STOPPED)

    @Slot()
    def prev(self) -> None:
        if self._shut or self._doc is None:
            return
        state = self._underlying()
        if state in (READING, WAITING, PAUSED):
            if self._seek_target is not None:
                target = self._seek_target - 1
            elif self._cur == 0 or self._elapsed_in(self._cur) > RESTART_AFTER_MS:
                target = self._cur
            else:
                target = self._cur - 1
            self._seek_to(target)
        elif state == FINISHED:
            self._clear_overlay()
            self._restart(len(self._doc.sentences) - 1)
        elif state == STOPPED:
            self._clear_overlay()
            self._restart(self._cur)

    @Slot()
    def next(self) -> None:
        if self._shut or self._doc is None or self._state not in (READING, WAITING, PAUSED):
            return
        base = self._seek_target if self._seek_target is not None else self._cur
        if base < len(self._doc.sentences) - 1:
            self._seek_to(base + 1)

    @Slot(int)
    def jump_to(self, sentence: int) -> None:
        """Progress-bar click: seek while reading; read from there when Finished or Stopped."""
        if self._shut or self._doc is None:
            return
        state = self._underlying()
        if state in (READING, WAITING, PAUSED):
            self._seek_to(sentence)
        elif state in (FINISHED, STOPPED):
            self._clear_overlay()
            self._restart(_clamp(sentence, 0, len(self._doc.sentences) - 1))

    @Slot()
    def retry(self) -> None:
        """Try again: from the first sentence not fully played, at the current speed."""
        if self._shut or self._doc is None or self._underlying() not in ERRORS:
            return
        self._clear_overlay()
        self._restart(self._cur)

    @Slot()
    def replay_from_start(self) -> None:
        if self._shut or self._doc is None or self._underlying() not in (ERRORS | {FINISHED, STOPPED}):
            return
        self._clear_overlay()
        self._restart(0)

    def _restart(self, target: int) -> None:
        """Read on from `target` with the text in memory (Finished, Stopped or an error)."""
        self._settle_speed()
        self._drop_other_rates()
        self._mid_session = self._back_online = False
        self._cur = target
        self._set_state(WAITING if self._played_any else PREPARING)
        self._start_at(target, play=True)

    # --- Seeking -------------------------------------------------------------------------------

    def _seek_to(self, target: int) -> None:
        """Move the counter now; act on the audio SEEK_DELAY_MS after the last press."""
        target = _clamp(target, 0, len(self._doc.sentences) - 1)
        self._seek_target = self._cur = target
        self._seek_timer.start(SEEK_DELAY_MS)
        self._publish()
        self._emit_position()

    def _flush_seek(self) -> None:
        if self._seek_timer.is_active():
            self._seek_timer.stop()
            self._do_seek()

    def _do_seek(self) -> None:
        target, self._seek_target = self._seek_target, None
        if target is None or self._doc is None or self._state not in (READING, WAITING, PAUSED):
            return
        state = self._state
        how = self._start_at(target, play=state != PAUSED)
        if state != PAUSED:
            self._set_state(READING if how == "seek" else WAITING)

    def _elapsed_in(self, sentence: int) -> float:
        chunk = self._chunks.get(self._loaded)
        if chunk is None or chunk.timing is None or sentence not in chunk.timing.sentence_ms:
            return 0.0
        return self._pos_ms - chunk.timing.sentence_ms[sentence][0]

    def _start_at(self, target: int, *, play: bool) -> str:
        """Make `target` the reading position. Returns 'seek', 'load' or 'plan' (synthesis needed)."""
        self._cur = self._floor = target
        self._seek_target = None
        self._after_cut = None
        chunk = self._ready_chunk(target)
        if chunk is None:
            self._bump_gen()
            self._close_player()
            how = "plan"
        else:
            offset = self._offset_in(chunk, target)
            if chunk.id == self._loaded and chunk.started:
                self._player.seek(offset, play)
                self._pos_ms = float(offset)
                self._seek_ack = (offset, self._clock())
                how = "seek"
            else:
                self._load(chunk, offset, play)
                how = "load"
        self._prefetch()
        self._publish()
        self._emit_position()
        return how

    # --- Speed ---------------------------------------------------------------------------------

    @Slot(float)
    def set_speed(self, v: float) -> None:
        """Show the new speed at once; commit it SPEED_COMMIT_MS after the last change."""
        if self._shut:
            return
        v = snap_speed(float(v))
        if v != self._speed:
            self._speed = v
            self._speed_pending = True
            self._note = self._predicted_note(v)
            self._note_timer.stop()
            self.speedChanged.emit(v, self._note)
            self._publish()
        if self._speed_pending:
            self._commit_timer.start(SPEED_COMMIT_MS)

    def _predicted_note(self, v: float) -> str:
        if self._doc is None or v == self._rate:
            return ""
        if self._state == PAUSED:
            return f"{speed_label(v)} when resumed"
        if self._state == READING and self._cur >= len(self._doc.sentences) - 1:
            return f"{speed_label(v)} from next reading"
        if self._state in (READING, WAITING):
            return f"{speed_label(v)} from next sentence"
        return ""

    def _settle_speed(self) -> None:
        """Before reading starts afresh: commit a pending change now and drop any note (no re-plan)."""
        if self._commit_timer.is_active():
            self._commit_timer.stop()
            self._committed = self._speed
            self._settings.set("speed", self._speed)
            log.info("Speed %s committed for the new reading", rate_string(self._speed))
        if self._speed_pending:
            self._speed_pending, self._note = False, ""
            self._note_timer.stop()
            self.speedChanged.emit(self._speed, "")
        self._rate = self._committed

    def _commit_speed(self) -> None:
        v = self._speed
        self._flush_seek()
        self._committed = v
        self._settings.set("speed", v)
        state = self._underlying()
        log.info("Speed %s committed in state %s", rate_string(v), state)
        if state not in SESSION or self._doc is None:
            self._rate = v
            self._drop_other_rates()
            self._clear_pending()
            return
        if v == self._rate:
            self._clear_pending()
            return
        if state == READING and self._loaded is not None:
            if not self._commit_reading(v):
                return
        elif state == PAUSED:
            self._replan(v, play=False, note=f"{speed_label(v)} when resumed")
        else:  # Preparing (nothing played yet) or Waiting
            self._replan(v, play=True, note="" if state == PREPARING else f"{speed_label(v)} from next sentence")
        self.speedChanged.emit(self._speed, self._note)
        self._publish()
        self._emit_time_left(force=True)

    def _replan(self, v: float, *, play: bool, note: str) -> None:
        """Preparing, Waiting, Paused: drop every old-rate chunk and plan from the current sentence."""
        self._rate = v
        self._bump_gen()
        self._close_player()
        self._drop_other_rates()
        self._note = note
        self._start_at(self._cur, play=play)

    def _commit_reading(self, v: float) -> bool:
        """Commit a new speed while Reading. Returns False when it already announced its own result."""
        chunk = self._chunks[self._loaded]
        if chunk.speed == v:  # back to the rate of the audio that is playing: let it play on
            self._rate = v
            self._player.set_cut(None)
            self._after_cut = None
            self._bump_gen()
            self._drop_other_rates()
            self._prefetch()
            self._clear_pending()
            return False
        s, pos, timing = self._cur, self._pos_ms, chunk.timing
        exact = timing is not None and not timing.proportional and timing.has_words(s)
        target = s if exact and pos < timing.sentence_ms[s][0] else s + 1
        cut = None
        if exact and chunk.contains(target):
            if target == s:
                cut = pos  # still before this sentence starts: stop now and say it at the new rate
            else:
                end, start = timing.sentence_ms[s][1], timing.sentence_ms[target][0]
                cut = end + _clamp(start - end - 60, 20, 150)
        else:
            target = chunk.plan.last + 1  # no reliable timing: this chunk plays to its end
        if target >= len(self._doc.sentences):
            self._note = f"{speed_label(v)} from next reading"
            self._note_timer.start(NOTE_MS)
            return True
        self._rate = v
        self._bump_gen()
        self._drop_other_rates()  # keeps only the playing chunk
        if cut is not None:
            self._player.set_cut(int(cut))
        self._after_cut = target
        self._note = f"{speed_label(v)} from next sentence"
        self._prefetch()
        return True

    def _clear_pending(self) -> None:
        self._speed_pending = False
        self._note = ""
        self._note_timer.stop()
        self.speedChanged.emit(self._speed, "")
        self._publish()

    # --- Volume --------------------------------------------------------------------------------

    @Slot(int)
    def set_volume(self, v: int) -> None:
        if self._shut:
            return
        v = int(_clamp(round(v), 0, 100))
        if v == self._volume:
            return
        self._volume = v
        self._settings.set("volume", v)
        self.volumeChanged.emit(v)
        wait_ms = VOLUME_INTERVAL_MS - (self._clock() - self._volume_sent_at) * 1000
        if wait_ms <= 0:
            self._send_volume()
        elif not self._volume_timer.is_active():
            self._volume_timer.start(wait_ms)

    def _send_volume(self) -> None:
        self._volume_sent_at = self._clock()
        self._player.set_volume(self._volume)

    # --- Network and shortcut status -----------------------------------------------------------

    @Slot(bool)
    def set_online(self, online: bool) -> None:
        """QNetworkInformation reachability: Offline when idle, "back online" in the offline error."""
        online = bool(online)
        if self._shut or online == self._online:
            return
        self._online = online
        if self._underlying() == ERROR_OFFLINE:
            self._back_online = online
        self._refresh_idle()

    @Slot(int, bool)
    def _on_registered(self, hk_id: int, ok: bool) -> None:
        if self._shut or hk_id != HK_READ or bool(ok) == self._shortcut_ok:
            return
        self._shortcut_ok = bool(ok)
        if ok:
            log.info("Read shortcut registered")
        else:
            log.warning("Read shortcut is unavailable")
            self.notice.emit("shortcut_unavailable")
        self._refresh_idle()

    @Slot(str)
    def _on_setting_changed(self, key: str) -> None:
        """Follow changes made elsewhere (our own writes come back equal and are ignored)."""
        if self._shut:
            return
        if key == "media_keys":
            self._update_media_keys()
        elif key == "speed":
            v = snap_speed(float(self._get("speed", self._committed)))
            if v not in (self._committed, self._speed):
                self.set_speed(v)
        elif key == "volume":
            self.set_volume(int(self._get("volume", self._volume)))

    def _base_idle(self) -> str:
        if not self._shortcut_ok:
            return SHORTCUT_UNAVAILABLE
        return READY if self._online else READY_OFFLINE

    def _refresh_idle(self) -> None:
        idle = self._base_idle()
        if self._state in IDLE:
            self._set_state(idle)
            return
        if self._state == NOTHING_SELECTED and self._rest in IDLE:
            self._rest = idle
        elif self._state == COPYING and self._copy_prev in IDLE:
            self._copy_prev = idle
        self._publish()

    # --- Sessions and the chunk pipeline -------------------------------------------------------

    def _make_doc(self, text: str) -> Document:
        return Document.from_raw(text, skip_citations=bool(self._get("skip_citations", True)),
                                 skip_urls=bool(self._get("skip_urls", True)))

    def _new_session(self, text: str, doc: Document | None = None) -> bool:
        """Start reading text (or its already-built document) from the first sentence; False if unspeakable."""
        doc = doc if doc is not None else self._make_doc(text)
        if not doc.sentences:
            log.info("Nothing to read after normalizing %d characters", len(text))
            return False
        self._purge(announce=False)
        self._settle_speed()
        self._doc = doc
        self._mid_session = self._back_online = False
        self._time_left_value = None
        log.info("New reading: %d sentences, %d characters", len(doc.sentences), len(doc.text))
        new_reading = getattr(self._synth, "new_reading", None)
        if new_reading is not None:  # each reading starts on the chosen voice again
            new_reading()
        self.sessionChanged.emit(doc)
        self._cur = 0
        self._set_state(PREPARING)
        self._start_at(0, play=True)
        self._emit_time_left(force=True)
        return True

    def _purge(self, *, announce: bool = True) -> None:
        """Forget the text and delete the cached audio (privacy)."""
        self._seek_timer.stop()
        self._seek_target = None
        self._bump_gen()
        self._close_player()
        for chunk in list(self._chunks.values()):
            self._delete_chunk(chunk)
        self._after_cut = None
        self._purge_timer.stop()
        had_text = self._doc is not None
        self._doc = None
        self._cur = self._floor = 0
        self._pos_ms = self._played_idx = 0.0
        self._played_any = False
        if had_text and announce:
            self.sessionChanged.emit(None)
            self._emit_position()
            self._emit_time_left(force=True)

    def _on_purge_due(self) -> None:
        log.info("Purged the text and audio cache after %d minutes at rest", PURGE_MS // 60000)
        self._purge()
        for path in self._doomed.values():  # nothing has been playing for minutes
            self._synth.delete(path)
        self._doomed.clear()
        self._open.clear()
        idle = self._base_idle()
        if self._state == NOTHING_SELECTED:
            self._rest = idle
        elif self._state == COPYING:
            self._copy_prev = idle
        elif self._state in RESTING:
            self._set_state(idle)
            return
        self._publish()

    def _bump_gen(self) -> None:
        """New generation: cancel all synthesis; results of older generations are discarded."""
        self._gen += 1
        self._synth.cancel_before(self._gen)
        self._retry_due.clear()
        self._retry_timer.stop()
        for chunk in [c for c in self._chunks.values() if c.status != "ready"]:
            del self._chunks[chunk.id]

    def _find(self, sentence: int) -> _Chunk | None:
        """A current-rate chunk containing `sentence` that is ready, or requested/failed in this gen."""
        best = None
        for c in self._chunks.values():
            if c.speed != self._rate or not c.contains(sentence):
                continue
            if c.status == "ready":
                return c
            if c.gen == self._gen and best is None:
                best = c
        return best

    def _ready_chunk(self, sentence: int) -> _Chunk | None:
        chunk = self._find(sentence)
        return chunk if chunk is not None and chunk.status == "ready" else None

    def _prefetch(self) -> None:
        """Keep at most PREFETCH unplayed chunks requested or ready, ahead of the position."""
        if self._doc is None or self._state not in SESSION:
            return
        n = len(self._doc.sentences)
        loaded = self._chunks.get(self._loaded)
        if loaded is not None:
            nxt = self._after_cut if self._after_cut is not None else loaded.plan.last + 1
            unplayed = 0 if loaded.started else 1
            short = self._after_cut is not None  # a cut is coming soon: start the new rate quickly
        else:
            nxt, unplayed, short = self._cur, 0, True
        while nxt < n and unplayed < PREFETCH:
            chunk = self._find(nxt) or self._request(nxt, short)
            unplayed += 1
            short = False
            nxt = chunk.plan.last + 1

    def _request(self, start: int, short: bool) -> _Chunk:
        """Ask for the chunk starting at sentence `start`: a short one when audio is wanted soon."""
        limits_for = getattr(self._synth, "limits_for", lambda _voice: (FIRST_LIMIT, LIMIT))
        first, later = limits_for(self._voice())
        plan = plan_chunks(self._doc, start, first_limit=first if short else later, limit=later,
                           first_chunk_id=self._next_id, max_chunks=1)[0]
        self._next_id += 1
        chunk = _Chunk(plan, self._rate, self._gen)
        self._chunks[plan.chunk_id] = chunk
        self._synthesize(chunk)
        return chunk

    def _synthesize(self, chunk: _Chunk) -> None:
        log.info("Synthesizing chunk %d (gen %d): sentences %d-%d, %d characters, rate %s",
                 chunk.id, chunk.gen, chunk.plan.first + 1, chunk.plan.last + 1, len(chunk.plan.text),
                 rate_string(chunk.speed))
        self._synth.synthesize(chunk.gen, chunk.id, chunk.plan.text, self._voice(), chunk.speed)

    def _delete_chunk(self, chunk: _Chunk) -> None:
        self._chunks.pop(chunk.id, None)
        if chunk.path:
            if self._open[chunk.id] > 0:
                self._doomed[chunk.id] = chunk.path
            else:
                self._synth.delete(chunk.path)

    def _drop_other_rates(self) -> None:
        """The cache holds the current rate only; the playing chunk goes when it has played."""
        for chunk in [c for c in self._chunks.values() if c.speed != self._rate and c.id != self._loaded]:
            self._delete_chunk(chunk)

    def _trim_cache(self) -> None:
        ready = [c for c in self._chunks.values() if c.status == "ready" and c.id != self._loaded]
        excess = len(ready) - MAX_CACHED_CHUNKS
        if excess > 0:
            ready.sort(key=lambda c: -min(abs(c.plan.first - self._cur), abs(c.plan.last - self._cur)))
            for chunk in ready[:excess]:
                self._delete_chunk(chunk)

    def _load(self, chunk: _Chunk, offset: float, play: bool) -> None:
        self._player.load(chunk.id, chunk.path, start_ms=int(offset), play=play, cut_ms=None)
        self._loaded = chunk.id
        self._open[chunk.id] += 1
        chunk.started = False
        self._pos_ms = float(offset)
        self._seek_ack = None

    def _close_player(self) -> None:
        self._seek_ack = None
        if self._loaded is not None:
            self._player.stop_close()
            self._loaded = None

    def _offset_in(self, chunk: _Chunk, sentence: int) -> int:
        """Where to start a sentence in its chunk: max(previous end, start - 80 ms)."""
        j = chunk.plan.sentence_ids.index(sentence)
        if j == 0 or chunk.timing is None or sentence not in chunk.timing.sentence_ms:
            return 0
        prev_end = chunk.timing.sentence_ms.get(chunk.plan.sentence_ids[j - 1], (0.0, 0.0))[1]
        return int(max(prev_end, chunk.timing.sentence_ms[sentence][0] - SEEK_LEAD_MS))

    def _estimate_length(self, chunk: _Chunk) -> float:
        mpc = self._mpc * self._mpc_speed if self._mpc is not None else DEFAULT_MS_PER_CHAR
        return len(chunk.plan.text) * mpc / chunk.speed

    @Slot(int, int, str, object)
    def _on_chunk_ready(self, gen: int, chunk_id: int, path: str, words) -> None:
        if self._shut:
            return
        chunk = self._chunks.get(chunk_id)
        if chunk is None or gen != self._gen or chunk.gen != gen or chunk.status != "requested":
            log.debug("Discarded stale chunk %d (gen %d)", chunk_id, gen)
            self._synth.delete(path)
            return
        chunk.status, chunk.path, chunk.words = "ready", path, list(words or [])
        chunk.timing = map_words(chunk.plan, chunk.words, None if chunk.words else self._estimate_length(chunk))
        log.info("Chunk %d ready: %d words", chunk_id, len(chunk.words))
        if (self._loaded is None and self._seek_target is None and self._state in SESSION
                and chunk.contains(self._cur) and chunk.speed == self._rate):
            self._load(chunk, self._offset_in(chunk, self._cur), self._state != PAUSED)
        self._prefetch()
        self._trim_cache()
        self._emit_position()

    @Slot(int, int, str, str)
    def _on_chunk_failed(self, gen: int, chunk_id: int, kind: str, detail: str) -> None:
        if self._shut:
            return
        chunk = self._chunks.get(chunk_id)
        if chunk is None or gen != self._gen or chunk.gen != gen or chunk.status != "requested":
            log.debug("Ignored a stale failure of chunk %d (gen %d)", chunk_id, gen)
            return
        log.warning("Synthesis failed for chunk %d (%s): %s", chunk_id, kind, str(detail)[:300])
        awaited = self._loaded is None and self._state in SESSION and chunk.contains(self._cur)
        if not awaited and not chunk.retried:  # earlier audio is still playing: retry once, silently
            chunk.retried = True
            self._retry_due[chunk_id] = self._clock() + PREFETCH_RETRY_MS / 1000
            self._arm_retry_timer()
            return
        chunk.status, chunk.fail_kind = "failed", kind
        if awaited:
            self._fail(self._error_state(kind))

    def _arm_retry_timer(self) -> None:
        if self._retry_due:
            self._retry_timer.start(max(0.0, min(self._retry_due.values()) - self._clock()) * 1000)
        else:
            self._retry_timer.stop()

    def _on_retry_due(self) -> None:
        now = self._clock()
        for chunk_id, due in list(self._retry_due.items()):
            if due <= now + 1e-6:
                del self._retry_due[chunk_id]
                chunk = self._chunks.get(chunk_id)
                if chunk is not None and chunk.status == "requested" and chunk.gen == self._gen:
                    log.info("Retrying chunk %d", chunk_id)
                    self._synthesize(chunk)
        self._arm_retry_timer()

    def _error_state(self, kind: str) -> str:
        return ERROR_OFFLINE if kind == "offline" or not self._online else ERROR_SERVICE

    def _fail(self, state: str) -> None:
        """Enter an error: everything stops; the position is the first sentence not fully played."""
        self._seek_timer.stop()
        self._seek_target = None
        self._mid_session = self._played_any
        self._back_online = False
        self._bump_gen()
        self._close_player()
        self._after_cut = None
        log.warning("Reading stopped (%s) at sentence %d of %d", state, self._cur + 1, len(self._doc.sentences))
        self._set_state(state)
        self._do_beep("error")
        self.notice.emit("error")

    # --- Player events -------------------------------------------------------------------------

    def _from_current_load(self, chunk_id: int) -> bool:
        """True for player signals about the load the engine is following.

        The same chunk can be loaded again before the player has closed its previous load; until that
        older load's `ended` arrives, signals carrying this chunk id may come from either, so ignore them.
        """
        return chunk_id == self._loaded and self._open[chunk_id] <= 1

    @Slot(int, int)
    def _on_started(self, chunk_id: int, length_ms: int) -> None:
        if self._shut or not self._from_current_load(chunk_id):
            return
        chunk = self._chunks[chunk_id]
        chunk.started = True
        if length_ms > 0:
            chunk.length_ms = int(length_ms)
            chunk.timing = map_words(chunk.plan, chunk.words, length_ms)
            self._measure(chunk)
        if not self._played_any:
            self._played_any = True
            self.notice.emit("reading_started")
        if self._state in (PREPARING, WAITING):
            self._set_state(READING)
        if (self._speed_pending and chunk.speed == self._speed and not self._commit_timer.is_active()
                and not self._note_timer.is_active()):
            self._clear_pending()
        self._prefetch()
        self._emit_time_left(force=True)

    @Slot(int, int, bool)
    def _on_tick(self, chunk_id: int, pos_ms: int, playing: bool) -> None:
        if self._shut or not self._from_current_load(chunk_id):
            return
        if self._seek_ack is not None:
            offset, sent = self._seek_ack
            if abs(pos_ms - offset) > SEEK_ACK_MS and (self._clock() - sent) * 1000 < SEEK_ACK_TIMEOUT_MS:
                return  # measured before the player applied the seek: it would pull the counter back
            self._seek_ack = None
        chunk = self._chunks[chunk_id]
        self._pos_ms = float(pos_ms)
        if self._seek_target is None and chunk.timing is not None and self._state in (READING, PAUSED):
            s = max(chunk.timing.sentence_at(self._pos_ms, chunk.plan.first), self._floor)
            s = min(s, chunk.plan.last)
            if s != self._cur:
                self._cur = s
                self._publish()
        self._emit_position()
        self._emit_time_left()

    @Slot(int, str)
    def _on_ended(self, chunk_id: int, why: str) -> None:
        if self._shut:
            return
        current = self._from_current_load(chunk_id)
        if self._open[chunk_id] > 0:
            self._open[chunk_id] -= 1
            if self._open[chunk_id] == 0:
                del self._open[chunk_id]
                if chunk_id in self._doomed:
                    self._synth.delete(self._doomed.pop(chunk_id))
        if why == "stopped" or not current:
            return
        chunk = self._chunks[chunk_id]
        # A seek no tick has confirmed yet, then "end": the device finished before the seek reached it
        # (the player ignores a seek once nothing is loaded). Go where the user asked, not past the chunk.
        lost_seek = why == "end" and self._seek_ack is not None
        self._loaded = None
        self._seek_ack = None
        if lost_seek:
            log.info("Chunk %d ended before its seek was applied; reloading at the seek target", chunk_id)
            nxt = self._floor
        else:
            nxt = self._after_cut if self._after_cut is not None else chunk.plan.last + 1
        self._after_cut = None
        if chunk.speed != self._rate:
            self._delete_chunk(chunk)
        if self._seek_timer.is_active():  # the user already chose where to go next
            self._flush_seek()
            return
        if nxt >= len(self._doc.sentences):
            self._finish()
            return
        self._cur = self._floor = nxt
        following = self._find(nxt)
        if following is not None and following.status == "ready":
            self._load(following, self._offset_in(following, nxt), self._state != PAUSED)
        elif following is not None and following.status == "failed":
            self._fail(self._error_state(following.fail_kind))
            return
        elif self._state == READING:
            self._set_state(WAITING)
        self._prefetch()
        self._trim_cache()
        self._publish()
        self._emit_position()

    @Slot(int, str)
    def _on_player_failed(self, chunk_id: int, error: str) -> None:
        if self._shut:
            return
        self._open.pop(chunk_id, None)  # the player closed the device without an ended signal
        if chunk_id in self._doomed:
            self._synth.delete(self._doomed.pop(chunk_id))
        if chunk_id != self._loaded:
            log.info("Ignored a playback failure of chunk %d", chunk_id)
            return
        log.warning("Playback failed for chunk %d: %s", chunk_id, str(error)[:300])
        self._fail(ERROR_PLAYBACK)

    def _finish(self) -> None:
        self._cur = len(self._doc.sentences) - 1
        log.info("Finished reading")
        self._set_state(FINISHED)
        self.notice.emit("finished")
        self._emit_position()
        self._emit_time_left(force=True)

    # --- State publication ---------------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        old, self._state = self._state, state
        if state in SESSION:
            self._purge_timer.stop()
        elif state in RESTING:
            self._rate = self._committed
            self._drop_other_rates()
            if self._speed_pending and not self._commit_timer.is_active():
                # a note such as "1.25× from next sentence" no longer applies at rest
                self._speed_pending, self._note = False, ""
                self._note_timer.stop()
                self.speedChanged.emit(self._speed, "")
            if self._doc is not None and state not in IDLE and not self._purge_timer.is_active():
                self._purge_timer.start(PURGE_MS)
        if state != PAUSED:
            self._media_timer.stop()
        elif old != PAUSED:
            self._media_timer.start(MEDIA_PAUSE_MS)
        if state != WAITING:
            self._hold_waiting = False
            self._waiting_timer.stop()
        elif old != WAITING and self.display_state == READING:
            self._hold_waiting = True  # a short gap mid-reading should not flash "Preparing audio…"
            self._waiting_timer.start(WAITING_SHOW_MS)
        self._update_media_keys()
        self._publish()

    def _on_waiting_show(self) -> None:
        self._hold_waiting = False
        self._publish()

    def _publish(self) -> None:
        """Emit stateChanged when the displayed state or its info changed."""
        if self._shut:
            return
        shown = self._state
        if shown == COPYING and not self._copy_shown:
            shown = self._copy_prev
        elif shown == WAITING and self._hold_waiting:
            shown = READING
        info = self._info(shown)
        if self._shown != (shown, info):
            self._shown = (shown, info)
            self.stateChanged.emit(shown, dict(info))

    def _info(self, shown: str) -> dict:
        base = self._rest if shown == NOTHING_SELECTED else shown
        doc = self._doc
        n = len(doc.sentences) if doc is not None else 0
        has_text = doc is not None
        overlay = shown == NOTHING_SELECTED
        return {
            "label": LABELS[shown],
            "sentence": self._cur if has_text and base not in IDLE else -1,
            "total": n,
            "reason": self._reason if overlay else "",
            "underlying": base if overlay else "",
            "pending_note": self._note,
            "speed": self._speed,
            "speed_pending": self._speed_pending,
            "has_text": has_text,
            "mid_session": self._mid_session if base in ERRORS else False,
            "back_online": self._back_online if base == ERROR_OFFLINE else False,
            "online": self._online,
            "shortcut_ok": self._shortcut_ok,
            "play_action": "read" if overlay else _PLAY_ACTIONS.get(base, "retry"),
            "prev_action": _PREV_ACTIONS.get(base, "none") if has_text else "none",
            "can_next": base in (READING, WAITING, PAUSED) and has_text and self._cur < n - 1,
            "can_stop": base in ACTIVE or base in ERRORS,
            "can_seek": base in (READING, WAITING, PAUSED, FINISHED, STOPPED) and has_text,
            "can_type": shown != COPYING,
            "can_clear": has_text and shown != COPYING,
        }

    def _emit_position(self) -> None:
        """position(sentence, word_lo, word_hi, played, buffered, in_word)."""
        if self._shut:
            return
        doc = self._doc
        if doc is None or not doc.text:
            self._played_idx = 0.0
            self.position.emit(-1, -1, -1, 0.0, 0.0, False)
            return
        total = len(doc.text)
        s = self._cur
        sent = doc.sentences[s]
        lo = hi = -1
        in_word = False
        idx = float(sent.start)
        chunk = self._chunks.get(self._loaded) if self._seek_target is None else None
        if self._state == FINISHED:
            idx = float(total)
        elif (chunk is not None and chunk.timing is not None and chunk.contains(s)
              and self._state in (READING, PAUSED)):
            pos = self._pos_ms
            word, in_word = chunk.timing.word_at(pos, s)
            if word is not None:
                lo, hi = word.lo, word.hi
                span = word.end_ms - word.start_ms
                frac = 1.0 if span <= 0 else _clamp((pos - word.start_ms) / span, 0.0, 1.0)
                idx += lo + (hi - lo) * frac
            elif s in chunk.timing.sentence_ms:
                a, b = chunk.timing.sentence_ms[s]
                frac = 0.0 if b <= a else _clamp((pos - a) / (b - a), 0.0, 1.0)
                idx += len(sent.text) * frac
                in_word = chunk.timing.proportional and a <= pos < b
        self._played_idx = idx
        self.position.emit(s, lo, hi, idx / total, self._buffered(total), in_word)

    def _buffered(self, total: int) -> float:
        """End of the contiguous ready audio from the position, as a fraction of the text."""
        doc = self._doc
        if self._state == FINISHED:
            return 1.0
        chunk = self._chunks.get(self._loaded)
        if chunk is not None and chunk.contains(self._cur) and self._after_cut is not None:
            last = self._after_cut - 1
        elif chunk is not None and chunk.contains(self._cur):
            last = chunk.plan.last
        else:
            chunk = self._ready_chunk(self._cur)
            if chunk is None:
                return doc.sentences[self._cur].start / total
            last = chunk.plan.last
        while last + 1 < len(doc.sentences):
            following = self._ready_chunk(last + 1)
            if following is None:
                break
            last = following.plan.last
        return doc.sentences[last].end / total

    def _measure(self, chunk: _Chunk) -> None:
        """Exponential moving average of audio ms per character, per speed (for time left)."""
        m = chunk.length_ms / max(1, len(chunk.plan.text))
        if self._mpc is None or self._mpc_speed != chunk.speed:
            self._mpc, self._mpc_speed = m, chunk.speed
        else:
            self._mpc += MPC_ALPHA * (m - self._mpc)

    def _time_left(self) -> int:
        if self._doc is None or self._mpc is None:
            return -1
        if self._state == FINISHED:
            return 0
        mpc = self._mpc * self._mpc_speed / self._rate  # old ms/char x old speed / new speed
        return int(round(max(0.0, len(self._doc.text) - self._played_idx) * mpc / 1000))

    def _emit_time_left(self, force: bool = False) -> None:
        """timeLeft at most once a second (or when forced), and only when the value changes."""
        now = self._clock()
        if self._shut or (not force and now - self._time_left_at < 1.0):
            return
        self._time_left_at = now
        value = self._time_left()
        if value != self._time_left_value:
            self._time_left_value = value
            self.timeLeft.emit(value)

    def _update_media_keys(self) -> None:
        want = bool(self._get("media_keys", True)) and (
            self._state in (PREPARING, READING, WAITING)
            or (self._state == PAUSED and self._media_timer.is_active()))
        self._set_media(want)

    def _set_media(self, on: bool) -> None:
        if on != self._media_on and not self._shut:
            self._media_on = on
            self._input.set_media_keys(on)
            self.mediaKeysWanted.emit(on)

    # --- Helpers -------------------------------------------------------------------------------

    def _get(self, key: str, default):
        """A settings value, or `default` when the key is missing (settings are duck-typed)."""
        try:
            value = self._settings.get(key)
        except KeyError:
            return default
        return default if value is None else value

    def _voice(self) -> str:
        return str(self._get("voice", DEFAULT_VOICE))

    def _do_beep(self, kind: str) -> None:
        if self._beep is not None:
            self._beep(kind)

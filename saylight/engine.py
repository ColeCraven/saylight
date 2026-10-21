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

Files come in through begin_opening() (the Opening state, which keeps the current document until the new
one is built), then read_document() or opening_failed() (an open notice that stays until the next action).
Reading from the screen opens the same way (kind "screen", "Finding the text…"); a scanned PDF adds the OCR
phase (opening_progress(..., phase="ocr")) and, when it is long, a wait for the person to choose pages
(opening_choice). A failed screen or scan opening goes back to the state under it, with an OCR notice over it
(ocr_notice).
The optional builder, resume store and pronunciation list are duck-typed too:
* builder(raw) builds a Document with this reading's options; if it also has start(token, raw), text over
  BUILD_ASYNC_CHARS is built on its worker instead and comes back through read_document(..., token=token);
* resume has lookup(), remember() and forget() (resume.ResumeStore); it is used only while the
  resume.enabled setting is on, and only for files;
* pronunciations has current() (or is the list itself) and is used by the default builder.
"""

import bisect
import hashlib
import logging
import math
import os
import re
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .resume import ResumePoint
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
OPENING, OPEN_FAILED, OCR_NOTICE = "opening", "open_failed", "ocr_notice"

IDLE = frozenset({READY, READY_OFFLINE, SHORTCUT_UNAVAILABLE})
ERRORS = frozenset({ERROR_OFFLINE, ERROR_SERVICE, ERROR_PLAYBACK})
RESTING = IDLE | ERRORS | {FINISHED, STOPPED}          # Nothing selected is an overlay on one of these
SESSION = frozenset({PREPARING, READING, WAITING, PAUSED})  # a reading is running
ACTIVE = SESSION | {COPYING, OPENING}
_PLAYING = frozenset({PREPARING, READING, WAITING})
OVERLAYS = frozenset({NOTHING_SELECTED, OPEN_FAILED, OCR_NOTICE})  # shown over the state underneath (_underlying)

LABELS = {
    READY: "Ready", READY_OFFLINE: "Offline", SHORTCUT_UNAVAILABLE: "Shortcut unavailable",
    COPYING: "Copying selection…", PREPARING: "Preparing audio…", READING: "Reading",
    WAITING: "Preparing audio…", PAUSED: "Paused", FINISHED: "Finished", STOPPED: "Stopped",
    NOTHING_SELECTED: "Nothing selected", ERROR_OFFLINE: "Can't reach the voice service",
    ERROR_SERVICE: "Voice service error", ERROR_PLAYBACK: "Couldn't play audio",
    OPENING: "Opening…", OPEN_FAILED: "Can't open this file", OCR_NOTICE: "Couldn't read the screen",
}
FINDING_LABEL = "Finding the text…"  # reading from the screen, and the OCR phase of a scanned PDF
SCAN_CHOICE_LABEL = "Scanned PDF"     # a long scan waiting for the person to choose pages
# The OCR notice's label for each kind (ocr.model.OCR_NOTICE_KINDS). The caution ones go by themselves after
# OCR_NOTICE_MS; the critical ones stay until the next action.
OCR_LABELS = {"screen_nothing": "No text found", "screen_too_large": "Area too large",
              "screen_language": "Can't read the screen", "screen_error": "Couldn't read the screen",
              "scan_nothing": "No text found", "scan_language": "Can't read this scan",
              "scan_error": "Couldn't read this scan"}
OCR_CAUTION = frozenset({"screen_nothing", "screen_too_large", "scan_nothing"})
# The open notice's label for each kind (LoadError kinds, plus "missing" for a file that has gone).
OPEN_LABELS = {"password": "Password needed", "protected": "Can't open this PDF", "scanned": "No text to read",
               "empty": "No text to read", "unsupported": "Can't read this file", "too_large": "File too large",
               "corrupt": "Can't open this file", "io": "Can't open this file", "missing": "File not found"}
OPEN_CRITICAL = frozenset({"corrupt", "io", "missing"})  # these beep as errors; the rest as cautions
_PLAY_ACTIONS = {READY: "read", READY_OFFLINE: "read", SHORTCUT_UNAVAILABLE: "read", FINISHED: "read",
                 STOPPED: "read", COPYING: "none", OPENING: "none", PREPARING: "pause", READING: "pause",
                 WAITING: "pause", PAUSED: "resume"}  # errors: "retry"
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
NOTHING_SCREEN_MS = 6000      # ...when it also offers Read from screen (time to reach the chip)
SCREEN_CHIP_REASONS = frozenset({"empty", "line_copy", "console"})  # the Nothing-selected cards with that chip
OCR_NOTICE_MS = 8000          # a caution OCR notice
HOLD_LEAVE_MS = 2000          # a held notice stays at least this long after the pointer leaves the card
OCR_INFO_S = 2.0              # the OCR page count and time left change at most this often
OCR_FIRST_GUESS_S = 0.8       # seconds per scanned page before any page has been timed
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
OPEN_SHOW_MS = 150            # Opening is shown only after this long, so quick opens don't flicker
BUILD_ASYNC_CHARS = 20_000    # text longer than this is built on the builder's worker, when it has one
RESUME_SAVE_MS = 30_000       # a file's place is saved this often while reading (resume.enabled only)
# Settings that change what is read aloud: the engine rebuilds the document when one changes.
REBUILD_KEYS = frozenset({"skip_citations", "skip_urls", "read_references", "read_asides", "pronunciations.enabled"})


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


def spoken_span(doc) -> tuple[int, int]:
    """The stretch of the shown text that progress measures: the first sentence's start to the last's end.

    Notes or a reference list that are shown but not read aloud follow the last sentence, so they never count
    towards progress; citations skipped inside the span still do (they are short)."""
    if doc is None or not doc.sentences:
        return 0, 0
    return doc.sentences[0].start, doc.sentences[-1].end


def span_fraction(doc, offset: float) -> float:
    """A shown offset as a fraction of the spoken span (0 to 1), for the progress bar and its marks."""
    lo, hi = spoken_span(doc)
    return _clamp((offset - lo) / (hi - lo), 0.0, 1.0) if hi > lo else 0.0


def sentence_at_offset(doc, offset: int) -> int:
    """The sentence at (or last before) a shown offset; the document's own method when it has one."""
    find = getattr(doc, "sentence_at_offset", None)
    if find is not None:
        return _clamp(int(find(offset)), 0, len(doc.sentences) - 1)
    starts = [s.start for s in doc.sentences]
    return _clamp(bisect.bisect_right(starts, offset) - 1, 0, len(doc.sentences) - 1)


def _say(sentence) -> str:
    """What the voice gets for a sentence (its shown text unless the text model says otherwise)."""
    return getattr(sentence, "say", None) or sentence.text


def _page_of(doc, sentence: int) -> int:
    """The page a sentence is on, or 0 when the document has no pages."""
    page_of = getattr(doc, "page_of", None)
    try:
        page = page_of(sentence) if page_of is not None else None
    except (IndexError, ValueError):
        page = None
    return int(page or 0)


def _fingerprint(doc) -> str:
    fingerprint = getattr(doc, "fingerprint", None)
    return fingerprint() if fingerprint is not None else hashlib.sha256(doc.text.encode("utf-8")).hexdigest()


def _raw_len(raw) -> int:
    return len(raw) if isinstance(raw, str) else len(getattr(raw, "text", "") or "")


_NON_SPACE = re.compile(r"\S")


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
    raw: object = None          # the input the document was built from (for rebuilds)
    file: tuple | None = None   # (path, size, mtime_ns, fingerprint) of a file, for resume


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
    openingCancelled = Signal(int)   # an opening (or a worker build) that will not be used: cancel its job
    resumeApplied = Signal(int)      # a file continued from its saved place, at this sentence

    def __init__(self, *, synth, player, input_thread, settings, clock=time.monotonic, parent=None,
                 timer_factory: Callable[[Callable[[], None]], object] | None = None,
                 beep: Callable[[str], None] | None = None, builder=None, resume=None, pronunciations=None):
        super().__init__(parent)
        self._synth, self._player, self._input, self._settings = synth, player, input_thread, settings
        self._clock = clock
        self._beep = beep
        self._builder, self._store, self._pronunciations = builder, resume, pronunciations
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
        self._open_show_timer = make(self._on_open_show)
        self._resume_timer = make(self._on_resume_due)
        self._rebuild_timer = make(self.rebuild)
        self._timers = (self._copy_show_timer, self._copy_watchdog, self._waiting_timer, self._seek_timer,
                        self._commit_timer, self._note_timer, self._overlay_timer, self._purge_timer,
                        self._media_timer, self._volume_timer, self._retry_timer, self._start_timer,
                        self._open_show_timer, self._resume_timer, self._rebuild_timer)

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
        self._raw = None                       # what the document was built from (kept for rebuilds)
        self._file: tuple | None = None        # (path, size, mtime_ns, fingerprint) when reading a file
        self._resumed: tuple[int, bool] | None = None  # (sentence, exact) when this file continued
        self._suffix: tuple[object, list[int]] | None = None  # (doc, spoken characters from each sentence)

        # opening files, worker builds and open notices
        self._open_token = 0                   # the last token handed out (tokens only grow)
        self._open_live: int | None = None     # the token the Opening state waits for
        self._open_kind = ""                   # "file", or "text" for a long selection built on the worker
        self._open_title = ""                  # UI only, never logged
        self._open_prev = READY                # the state under Opening
        self._open_shown = False
        self._open_progress = (0, 0)
        self._open_phase = "read"              # "read", "ocr" (finding the text in scanned pages) or "choice"
        self._ocr_started = 0.0                # when the OCR phase started (the clock's time)
        self._ocr_info: tuple[int, int, int] = (0, 0, -1)  # (page, total, seconds left or -1): what the header says
        self._ocr_info_at = float("-inf")
        self._choice: tuple[int, int, int] = (0, 0, 0)     # (scanned, pages, estimated seconds) while choosing
        self._rebuild_live: int | None = None  # the token of a rebuild running on the worker
        self._read_section: str | None = None  # a section to read once the rebuild is in (from Finished)
        self._fail_kind = ""
        self._fail_title = ""
        self._ocr_kind = ""                    # the OCR notice shown (OCR_LABELS)
        self._ocr_detail = ""                  # "nothing_speakable": text was found but none of it can be read
        self._held = False                     # the pointer is over the card: Nothing selected and cautions wait
        self._overlay_due = 0.0                # when the overlay timer would end it (the clock's time)

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

    @property
    def raw(self):
        """What the current document was built from: the text, or the LoadedDocument of a file."""
        return self._raw

    @property
    def resumed(self) -> tuple[int, bool] | None:
        """(sentence, exact) when the current file continued from its saved place, else None. Not exact means
        the file changed since, so the place was found by its offset ("Continuing near page 14")."""
        return self._resumed

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
        if self._state == OPENING:  # a new read replaces the file being opened
            self._cancel_opening()
        self._end_notice()
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
        self._clear_overlay()
        self._rest = self._state
        self._reason = reason
        self._start_overlay_timer(NOTHING_SCREEN_MS if reason in SCREEN_CHIP_REASONS else NOTHING_SELECTED_MS)
        self._set_state(NOTHING_SELECTED)
        self._do_beep("ok")
        self.notice.emit("nothing_selected")

    def _clear_overlay(self) -> None:
        """Drop Nothing selected, an open notice or an OCR notice, without publishing (the caller does)."""
        if self._state == NOTHING_SELECTED:
            self._overlay_timer.stop()
            self._state = self._rest
        elif self._state == OPEN_FAILED:
            self._state = self._rest
            self._fail_kind = self._fail_title = ""
        elif self._state == OCR_NOTICE:
            self._overlay_timer.stop()
            self._state = self._rest
            self._ocr_kind = self._ocr_detail = ""

    def _start_overlay_timer(self, ms: int) -> None:
        self._overlay_due = self._clock() + ms / 1000
        self._overlay_timer.start(ms)

    def _timed_overlay(self) -> bool:
        """Nothing selected or a caution OCR notice: shown for a while, then gone by itself."""
        return self._state == NOTHING_SELECTED or (self._state == OCR_NOTICE and self._ocr_kind in OCR_CAUTION)

    def _on_overlay_expired(self) -> None:
        if self._held:  # the pointer is over the card: it goes once the pointer leaves
            return
        if self._timed_overlay():
            self._clear_overlay()
            self._set_state(self._state)
            self._emit_position()

    @Slot(bool)
    def hold_overlay(self, on: bool) -> None:
        """Hold Nothing selected and the caution notices up while the pointer is over the card; once it
        leaves, they stay at least HOLD_LEAVE_MS more."""
        on = bool(on)
        if on == self._held or self._shut:
            return
        self._held = on
        if not on and self._timed_overlay():
            self._start_overlay_timer(max(HOLD_LEAVE_MS, round((self._overlay_due - self._clock()) * 1000)))

    def _underlying(self) -> str:
        return self._rest if self._state in OVERLAYS else self._state

    def _end_notice(self) -> None:
        """Any action ends an open notice or an OCR notice; the state underneath it carries on."""
        if self._state in (OPEN_FAILED, OCR_NOTICE):
            self._clear_overlay()
            self._publish()
            self._emit_position()

    @Slot()
    def dismiss_notice(self) -> None:
        """End an open notice (the panel calls this for its controls that don't reach the engine)."""
        if not self._shut:
            self._end_notice()

    # --- Input: typed text, Clear and Undo -----------------------------------------------------

    @Slot(str, result=bool)
    def read_text(self, text: str) -> bool:
        """Read text handed over directly (Type or paste), the same way as a successful copy.

        Returns False, changing nothing, when there is nothing to read; a reading in progress goes on.
        """
        if self._shut or not text or not text.strip():
            return False
        text = text[:MAX_TEXT_CHARS]
        doc = None
        if not self._builds_async(text):
            doc = self._make_doc(text)
            if not doc.sentences:
                log.info("Nothing to read in %d typed characters", len(text))
                return False
        for press in self._presses:  # a Ctrl+` copy still running must not replace this reading
            press.ignored = True
        if self._state == COPYING:
            self._cancel_copy(publish=False)
        if self._state == OPENING:
            self._cancel_opening(publish=False)
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
        token = None
        if doc is not None:
            under = self._open_prev if self._state == OPENING else self._state
            token = ClearedReading(doc, self._cur, under == FINISHED, self._raw, self._file)
            self._remember()  # with resume on, Clear saves the place first
        self._settle_speed()  # commit a waiting change now; its note no longer applies
        self._purge()
        self._mid_session = self._back_online = False
        self._reason = ""
        if self._state == OPENING:  # the file being opened carries on; it now opens over Ready
            self._open_prev = self._base_idle()
            self._publish()
        else:
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
        self._raw, self._file = token.raw, token.file
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
        self._end_notice()
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
        self._end_notice()
        self._pause()
        return True

    @Slot(result=bool)
    def resume(self) -> bool:
        """Resume a paused reading; never pauses. True if it resumed."""
        if self._shut or self._underlying() != PAUSED:
            return False
        self._end_notice()
        self._resume()
        return True

    def _pause(self) -> None:
        if self._loaded is not None:
            self._player.pause()
        self._set_state(PAUSED)
        self.notice.emit("paused")
        self._remember()

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
        """Stop: Copying and Opening return to the previous state; a reading goes to Stopped."""
        if self._shut:
            return
        self._end_notice()
        state = self._underlying()
        if state == COPYING:
            self._cancel_copy()
        elif state == OPENING:
            self._cancel_opening()
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
            self._remember()
        elif state in ERRORS:
            self._clear_overlay()
            self._set_state(STOPPED)

    @Slot()
    def prev(self) -> None:
        if self._shut or self._doc is None:
            return
        self._end_notice()
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
        if self._shut or self._doc is None:
            return
        self._end_notice()
        if self._state not in (READING, WAITING, PAUSED):
            return
        base = self._seek_target if self._seek_target is not None else self._cur
        if base < len(self._doc.sentences) - 1:
            self._seek_to(base + 1)

    @Slot(int)
    def jump_to(self, sentence: int) -> None:
        """Progress-bar click: seek while reading; read from there when Finished or Stopped."""
        if self._shut or self._doc is None:
            return
        self._end_notice()
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
    def start_over(self) -> None:
        """Start from the beginning (the chip on a file that continued from its saved place)."""
        if self._shut or self._doc is None:
            return
        self._end_notice()
        self._resumed = None
        state = self._underlying()
        if state in (READING, WAITING, PAUSED):
            self._seek_to(0)
        elif state == PREPARING or state in ERRORS or state in (FINISHED, STOPPED):
            self._clear_overlay()
            self._restart(0)

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

    # --- Files: opening, open notices, rebuilds ------------------------------------------------

    def begin_opening(self, title: str, kind: str = "file") -> int:
        """Start Opening (shown after OPEN_SHOW_MS) and return its token for the loader's answers.

        The current document stays until read_document() brings the new one; a reading in progress pauses, as
        it does for Ctrl+`. Opening again replaces the open in progress (openingCancelled has its token). `title`
        is UI only and never logged."""
        if self._shut:
            return 0
        for press in self._presses:  # a Ctrl+` copy still running must not replace this
            press.ignored = True
        if self._state == COPYING:
            self._cancel_copy(publish=False)
        self._clear_overlay()
        if self._state in (PREPARING, READING, WAITING):
            self._pause()
        if self._state == OPENING:
            replaced = self._open_live
            if replaced is not None:
                self.openingCancelled.emit(replaced)
        else:
            self._open_prev = self._state
            self._open_shown = False
            self._open_show_timer.start(OPEN_SHOW_MS)
        self._open_token += 1
        self._open_live = self._open_token
        self._open_kind, self._open_title = kind, title
        self._open_progress = (0, 0)
        self._reset_scan()
        log.info("Opening %s", {"file": "a file", "screen": "an area of the screen"}.get(kind, "a long text"))
        self._set_state(OPENING)
        self._emit_position()
        return self._open_token

    def opening_progress(self, token: int, done: int, total: int, *, phase: str = "read") -> None:
        """The loader's progress (pages done of total; total 0 when unknown). `phase` "ocr" is finding the text
        in scanned pages: the label becomes "Finding the text…", and the header's page and time left change at
        most every OCR_INFO_S (the progress bar follows every page)."""
        if self._shut or token != self._open_live or self._state != OPENING:
            return
        progress = (max(0, int(done)), max(0, int(total)))
        if phase != "ocr":
            if progress != self._open_progress:
                self._open_progress = progress
                self._publish()
                self._emit_position()
            return
        now = self._clock()
        if self._open_phase != "ocr" or progress[1] != self._open_progress[1]:
            self._open_phase = "ocr"
            self._ocr_started = now
            self._ocr_info_at = float("-inf")
        self._open_progress = progress
        done, total = progress
        if now - self._ocr_info_at >= OCR_INFO_S or done >= total or done == 0:
            self._ocr_info_at = now
            per_page = (now - self._ocr_started) / done if done else OCR_FIRST_GUESS_S
            left = (total - done) * per_page
            self._ocr_info = (min(total, done + 1), total, math.ceil(left) if done >= 2 and left >= 10 else -1)
        self._publish()
        self._emit_position()

    def opening_choice(self, token: int, scanned: int, pages: int, est_s: int) -> None:
        """A scanned PDF too long to read unasked: wait, shown at once, for the person to choose pages (the App
        hands their choice to the loader). Stop cancels the opening."""
        if self._shut or token != self._open_live or self._state != OPENING:
            return
        self._open_phase = "choice"
        self._choice = (max(0, int(scanned)), max(0, int(pages)), max(0, int(est_s)))
        self._open_show_timer.stop()
        self._open_shown = True
        self._open_progress = (0, 0)
        self._publish()
        self._emit_position()
        self.notice.emit("scan_choice")

    def ocr_notice(self, kind: str, detail: str = "") -> None:
        """Show an OCR notice (ocr.model.OCR_NOTICE_KINDS) over the current state: a screen or scan opening goes
        back to the state under it first, and a reading still playing waits under the notice, paused. The
        caution ones go after OCR_NOTICE_MS (held while the pointer is over the card); the critical ones stay
        until the next action. `detail` "nothing_speakable" changes the no-text card's first line."""
        if self._shut:
            return
        kind = kind if kind in OCR_LABELS else "screen_error"
        if self._state == OPENING:
            token, file = self._open_live, self._open_kind == "file"
            self._leave_opening()
            if file and token is not None:
                self.openingCancelled.emit(token)
        if self._state == COPYING:
            self._cancel_copy(publish=False)
        self._clear_overlay()
        if self._state in _PLAYING:  # a reading under the notice waits, paused (as it does under an open notice)
            self._pause()
        self._rest = self._state
        self._ocr_kind, self._ocr_detail = kind, detail
        caution = kind in OCR_CAUTION
        if caution:
            self._start_overlay_timer(OCR_NOTICE_MS)
        log.info("OCR notice: %s", kind)
        self._set_state(OCR_NOTICE)
        self._emit_position()
        self._do_beep("ok" if caution else "error")
        self.notice.emit("ocr_notice")

    def opening_failed(self, token: int, kind: str) -> None:
        """The file couldn't be read: back to the state under Opening, with the open notice over it. For the
        OCR kinds the notice is an OCR notice; "cancelled" (a screen reading that stopped) shows none."""
        if self._shut or token is None:
            return
        if token == self._rebuild_live:  # a rebuild that failed keeps the document as it was
            self._rebuild_live = None
            log.info("Rebuild failed: %s", kind)
            return
        if token != self._open_live:
            return
        title = self._open_title
        self._leave_opening()
        if kind == "cancelled":
            self._set_state(self._state)
            self._emit_position()
        elif kind in OCR_LABELS:
            self.ocr_notice(kind)
        else:
            self.open_notice(kind, title)

    def open_notice(self, kind: str, title: str = "") -> None:
        """Show the open notice for `kind` (OPEN_LABELS) over the current state, until the next action.

        For files that never reached the loader too: Open with on an unsupported or missing file."""
        if self._shut:
            return
        kind = kind if kind in OPEN_LABELS else "corrupt"
        if self._state == OPENING:
            self._cancel_opening(publish=False)
        if self._state == COPYING:
            self._cancel_copy(publish=False)
        self._clear_overlay()
        if self._state in _PLAYING:  # a reading under the notice waits, paused (as it does while opening)
            self._pause()
        self._rest = self._state
        self._fail_kind, self._fail_title = kind, title
        log.info("Open failed: %s", kind)
        self._set_state(OPEN_FAILED)
        self._emit_position()
        self._do_beep("error" if kind in OPEN_CRITICAL else "ok")
        self.notice.emit("open_failed")

    def _cancel_opening(self, *, publish: bool = True) -> None:
        """Stop, or a new read, while opening: the loader's job is dropped and nothing changes."""
        token = self._open_live
        self._leave_opening()
        if publish:
            self._set_state(self._state)
            self._emit_position()
        log.info("Opening cancelled")
        if token is not None:
            self.openingCancelled.emit(token)

    def _leave_opening(self) -> None:
        """Back to the state under Opening, without publishing (the caller does)."""
        self._open_show_timer.stop()
        self._open_shown = False
        self._open_live = None
        self._open_progress = (0, 0)
        self._open_kind = self._open_title = ""
        self._reset_scan()
        if self._state == OPENING:
            self._state = self._open_prev

    def _reset_scan(self) -> None:
        self._open_phase = "read"
        self._ocr_info, self._ocr_info_at = (0, 0, -1), float("-inf")
        self._choice = (0, 0, 0)

    def _on_open_show(self) -> None:
        if self._state == OPENING:
            self._open_shown = True
            self._publish()
            self._emit_position()
            if self._open_kind == "file":
                self.notice.emit("opening")

    def read_document(self, doc: Document, raw=None, *, token: int | None = None) -> bool:
        """Read a document built elsewhere (an opened file, or text built on the worker) from its start, or from
        its saved place. With the token of an earlier begin_opening() or rebuild, a stale answer is dropped.

        False when it was dropped, or when there is nothing to read (the "empty" notice then shows)."""
        if self._shut:
            return False
        if token is not None and token == self._rebuild_live:
            self._rebuild_live = None
            return self._apply_rebuild(doc, raw)
        if token is not None and token != self._open_live:
            log.info("Dropped a document that is no longer wanted")
            return False
        if not doc.sentences:
            if self._state == OPENING:
                self.opening_failed(self._open_live, "empty")
            else:
                self.open_notice("empty")
            return False
        if self._state == OPENING:
            self._leave_opening()
        for press in self._presses:  # a Ctrl+` copy still running must not replace this reading
            press.ignored = True
        if self._state == COPYING:
            self._cancel_copy(publish=False)
        self._clear_overlay()
        return self._new_session(doc.text, doc, raw=raw if raw is not None else doc.text)

    @Slot()
    def rebuild(self, read_section: str | None = None) -> None:
        """Build the document again from the same input, keeping the place on screen: after a change to what is
        read aloud (the engine does this itself for the settings), a section chip or a pronunciation change.

        With `read_section` ("notes" or "refs", from Document.sections), a reading that has finished goes on to
        read that section once the new document is ready; one still going reaches it by itself."""
        self._rebuild_timer.stop()
        if self._shut or self._doc is None or self._raw is None:
            return
        self._read_section = read_section
        raw = self._raw
        if self._builds_async(raw):
            self._open_token += 1
            if self._rebuild_live is not None:
                self.openingCancelled.emit(self._rebuild_live)
            self._rebuild_live = self._open_token
            self._builder.start(self._rebuild_live, raw)
            return
        try:
            doc = self._build(raw)
        except Exception as e:  # keep reading the document as it was
            log.warning("Rebuild failed (%s)", type(e).__name__)
            return
        self._apply_rebuild(doc, raw)

    def _apply_rebuild(self, doc: Document, raw) -> bool:
        old = self._doc
        if old is None or raw is not self._raw or not doc.sentences:
            return False
        if getattr(doc, "source", None) is None and getattr(old, "source", None) is not None:
            doc.source = old.source
        state = self._underlying()
        place = self._seek_target if self._seek_target is not None else self._cur
        target = sentence_at_offset(doc, old.sentences[_clamp(place, 0, len(old.sentences) - 1)].start)
        same = 0  # leading sentences that are unchanged: their audio stays valid
        for a, b in zip(old.sentences, doc.sentences):
            if (a.start, a.end, _say(a)) != (b.start, b.end, _say(b)):
                break
            same += 1
        loaded = self._chunks.get(self._loaded)
        keep_playing = (state in (READING, WAITING, PAUSED) and loaded is not None and loaded.plan.last < same
                        and self._seek_target is None)
        self._bump_gen()  # chunks still being made were planned on the old document
        if not keep_playing:
            self._seek_timer.stop()
            self._seek_target = None
            self._close_player()
            self._after_cut = None
        for chunk in [c for c in self._chunks.values() if c.plan.last >= same and c.id != self._loaded]:
            self._delete_chunk(chunk)
        self._doc, self._suffix, self._time_left_value = doc, None, None
        log.info("Rebuilt: %d sentences, %d unchanged", len(doc.sentences), same)
        self.sessionChanged.emit(doc)
        if keep_playing:
            self._prefetch()
            self._publish()
            self._emit_position()
        elif state in SESSION:
            if state != PAUSED:
                self._set_state(WAITING if self._played_any else PREPARING)
            self._start_at(target, play=state != PAUSED)
        else:
            self._cur = self._floor = target
            self._publish()
            self._emit_position()
        self._emit_time_left(force=True)
        section, self._read_section = self._read_section, None
        offset = (getattr(doc, "sections", None) or {}).get(section) if section else None
        if offset is not None and state == FINISHED:
            first = next((s.index for s in doc.sentences if s.start >= offset), None)
            if first is not None:
                self._clear_overlay()
                self._restart(first)
        return True

    def _builds_async(self, raw) -> bool:
        return getattr(self._builder, "start", None) is not None and _raw_len(raw) > BUILD_ASYNC_CHARS

    def _build(self, raw) -> Document:
        """A Document for `raw` with this reading's options (the builder's when there is one)."""
        if self._builder is not None:
            return self._builder(raw)
        skip_citations, skip_urls = bool(self._get("skip_citations", True)), bool(self._get("skip_urls", True))
        extra = {}
        pronunciations = self._pronunciation_list()
        if pronunciations is not None:
            extra["pronunciations"] = pronunciations
        if isinstance(raw, str):
            return Document.from_raw(raw, skip_citations=skip_citations, skip_urls=skip_urls, **extra)
        from . import text_model
        options = text_model.BuildOptions(skip_citations=skip_citations, skip_urls=skip_urls,
                                          read_references=bool(self._get("read_references", False)),
                                          read_asides=bool(self._get("read_asides", False)), **extra)
        return Document.build(raw, options)

    def _pronunciation_list(self):
        if self._pronunciations is None or not self._get("pronunciations.enabled", True):
            return None
        current = getattr(self._pronunciations, "current", None)
        return current() if callable(current) else self._pronunciations

    # --- Resume (files only, and only while resume.enabled is on) ------------------------------

    def _resume_on(self) -> bool:
        return self._store is not None and bool(self._get("resume.enabled", False))

    def _file_of(self, doc) -> tuple | None:
        """(path, size, mtime_ns, fingerprint) for a document read from a file, else None."""
        source = getattr(doc, "source", None)
        path = getattr(source, "path", "") if getattr(source, "kind", "") == "file" else ""
        if not path or self._store is None:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        return path, st.st_size, st.st_mtime_ns, _fingerprint(doc)

    def _resume_start(self, doc) -> int:
        """Where a file starts: its saved place when there is one (resumeApplied follows), else 0."""
        self._resumed = None
        if self._file is None or not self._resume_on():
            return 0
        try:
            found = self._store.lookup(*self._file)
        except Exception as e:
            log.warning("Couldn't look up the saved place (%s)", type(e).__name__)
            return 0
        if not found:
            return 0
        point, exact = found
        n = len(doc.sentences)
        # An exact match keeps the offset exact, but not always the sentence number: sentences are split on the
        # spoken text, which Skip citations and Skip links change without changing the shown text.
        start = (point.sentence if exact and 0 <= point.sentence < n and doc.sentences[point.sentence].start
                 == point.offset else sentence_at_offset(doc, point.offset))
        if start <= 0:
            return 0
        self._resumed = (start, bool(exact))
        log.info("Continuing at sentence %d of %d (%s)", start + 1, n, "exact" if exact else "near")
        return start

    def _remember(self) -> None:
        """Save the current file's place (path and numbers only, never text)."""
        if (self._doc is None or self._file is None or not self._resume_on()
                or self._underlying() == FINISHED or (self._state == OPENING and self._open_prev == FINISHED)):
            return
        path, size, mtime_ns, fingerprint = self._file
        s = _clamp(self._cur, 0, len(self._doc.sentences) - 1)
        point = ResumePoint(path, size, mtime_ns, fingerprint, self._doc.sentences[s].start, s, time.time(),
                            _page_of(self._doc, s))
        try:
            self._store.remember(point)
        except Exception as e:
            log.warning("Couldn't save the place (%s)", type(e).__name__)

    def _forget(self) -> None:
        if self._file is None or not self._resume_on():
            return
        try:
            self._store.forget(self._file[0])
        except Exception as e:
            log.warning("Couldn't forget the place (%s)", type(e).__name__)

    def _on_resume_due(self) -> None:
        if self._base() in _PLAYING:
            self._remember()
            self._arm_resume_timer()

    def _arm_resume_timer(self) -> None:
        if self._base() in _PLAYING and self._file is not None and self._resume_on():
            self._resume_timer.start(RESUME_SAVE_MS)
        else:
            self._resume_timer.stop()

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
        state = self._base()
        if state == PAUSED:
            return f"{speed_label(v)} when resumed"
        if state == READING and self._cur >= len(self._doc.sentences) - 1:
            return f"{speed_label(v)} from next reading"
        if state in (READING, WAITING):
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
        state = self._base()
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
        elif key in REBUILD_KEYS:
            if self._doc is not None:
                self._rebuild_timer.start(0)  # one rebuild for several changes in a row
        elif key == "resume.enabled":
            if self._resume_on():  # turned on: remember this place now ("Remember this page next time")
                self._remember()
            self._arm_resume_timer()

    def _base_idle(self) -> str:
        if not self._shortcut_ok:
            return SHORTCUT_UNAVAILABLE
        return READY if self._online else READY_OFFLINE

    def _refresh_idle(self) -> None:
        idle = self._base_idle()
        if self._state in IDLE:
            self._set_state(idle)
            return
        if self._state in OVERLAYS and self._rest in IDLE:
            self._rest = idle
        elif self._state == COPYING and self._copy_prev in IDLE:
            self._copy_prev = idle
        elif self._state == OPENING and self._open_prev in IDLE:
            self._open_prev = idle
        self._publish()

    # --- Sessions and the chunk pipeline -------------------------------------------------------

    def _make_doc(self, text: str) -> Document:
        return self._build(text)

    def _new_session(self, text: str, doc: Document | None = None, *, raw=None) -> bool:
        """Start reading text (or its already-built document) from the first sentence, or from a file's saved
        place; False if unspeakable. Long text is built on the builder's worker first (as Opening)."""
        if doc is None and self._builds_async(text):
            token = self.begin_opening("", "text")
            self._builder.start(token, text)
            return True
        doc = doc if doc is not None else self._make_doc(text)
        if not doc.sentences:
            log.info("Nothing to read after normalizing %d characters", len(text))
            return False
        self._purge(announce=False)
        self._settle_speed()
        self._doc, self._raw = doc, raw if raw is not None else text
        self._file = self._file_of(doc)
        self._mid_session = self._back_online = False
        self._time_left_value = None
        log.info("New reading: %d sentences, %d characters", len(doc.sentences), len(doc.text))
        new_reading = getattr(self._synth, "new_reading", None)
        if new_reading is not None:  # each reading starts on the chosen voice again
            new_reading()
        self.sessionChanged.emit(doc)
        start = self._resume_start(doc)
        self._cur = start
        self._set_state(PREPARING)
        self._start_at(start, play=True)
        self._emit_time_left(force=True)
        if self._resumed is not None:
            self.resumeApplied.emit(start)
        return True

    def _purge(self, *, announce: bool = True) -> None:
        """Forget the text and delete the cached audio (privacy). A file's place is saved first (resume)."""
        self._remember()
        self._seek_timer.stop()
        self._seek_target = None
        self._bump_gen()
        self._close_player()
        for chunk in list(self._chunks.values()):
            self._delete_chunk(chunk)
        self._after_cut = None
        self._purge_timer.stop()
        had_text = self._doc is not None
        self._doc = self._raw = self._file = self._resumed = self._suffix = None
        self._rebuild_timer.stop()
        if self._rebuild_live is not None:
            self.openingCancelled.emit(self._rebuild_live)
            self._rebuild_live = None
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
        if self._state in OVERLAYS:
            self._rest = idle
        elif self._state == COPYING:
            self._copy_prev = idle
        elif self._state == OPENING and self._open_prev in RESTING:
            self._open_prev = idle
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
        self._forget()  # a finished file is not offered again
        self._set_state(FINISHED)
        self.notice.emit("finished")
        self._emit_position()
        self._emit_time_left(force=True)

    # --- State publication ---------------------------------------------------------------------

    def _base(self) -> str:
        """The state that decides the timers and media keys: the one under an overlay or under Opening."""
        if self._state in OVERLAYS:
            return self._rest
        return self._open_prev if self._state == OPENING else self._state

    def _set_state(self, state: str) -> None:
        old, old_base = self._state, self._base()
        self._state = state
        base = self._base()
        if base in SESSION:
            self._purge_timer.stop()
        elif base in RESTING:
            self._rate = self._committed
            self._drop_other_rates()
            if self._speed_pending and not self._commit_timer.is_active():
                # a note such as "1.25× from next sentence" no longer applies at rest
                self._speed_pending, self._note = False, ""
                self._note_timer.stop()
                self.speedChanged.emit(self._speed, "")
            if self._doc is not None and base not in IDLE and not self._purge_timer.is_active():
                self._purge_timer.start(PURGE_MS)
        if base != PAUSED:
            self._media_timer.stop()
        elif old_base != PAUSED:
            self._media_timer.start(MEDIA_PAUSE_MS)
        if base not in _PLAYING:
            self._resume_timer.stop()
        elif not self._resume_timer.is_active():
            self._arm_resume_timer()
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
        elif shown == OPENING and not self._open_shown:
            shown = self._open_prev
        elif shown == WAITING and self._hold_waiting:
            shown = READING
        info = self._info(shown)
        if self._shown != (shown, info):
            self._shown = (shown, info)
            self.stateChanged.emit(shown, dict(info))

    def _info(self, shown: str) -> dict:
        base = self._rest if shown in OVERLAYS else shown
        doc = self._doc
        n = len(doc.sentences) if doc is not None else 0
        has_text = doc is not None
        overlay = shown == NOTHING_SELECTED
        source = getattr(doc, "source", None)
        label = LABELS[shown]
        opening = shown == OPENING
        ocr = opening and self._open_phase == "ocr"
        choice = opening and self._open_phase == "choice"
        if shown == OPEN_FAILED:
            label = OPEN_LABELS.get(self._fail_kind, label)
        elif shown == OCR_NOTICE:
            label = OCR_LABELS.get(self._ocr_kind, label)
        elif choice:
            label = SCAN_CHOICE_LABEL
        elif opening and (self._open_kind == "screen" or ocr):
            label = FINDING_LABEL
        elif opening and self._open_kind != "file":
            label = LABELS[PREPARING]  # a long text being built: it reads as the start of preparing
        if shown == OPENING:
            title = self._open_title
        elif shown == OPEN_FAILED:
            title = self._fail_title
        else:
            title = getattr(source, "title", "") if getattr(source, "kind", "") == "file" else ""
        return {
            "label": label,
            "sentence": self._cur if has_text and base not in IDLE else -1,
            "total": n,
            "reason": self._reason if overlay else "",
            "underlying": base if shown in OVERLAYS else "",
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
            "can_type": shown not in (COPYING, OPENING),
            "can_clear": has_text and shown not in (COPYING, OPENING),
            # files, open notices and pages
            "source": getattr(source, "kind", "") if has_text else "",
            "title": title,                                   # UI only, never logged
            "kind": self._fail_kind if shown == OPEN_FAILED else "",
            "open_kind": self._open_kind if shown == OPENING else "",
            "open_done": self._open_progress[0] if shown == OPENING else 0,
            "open_total": self._open_progress[1] if shown == OPENING else 0,
            "page": _page_of(doc, self._cur) if has_text and n else 0,
            "pages": int(getattr(self._raw, "pages", 0) or 0) if has_text else 0,
            "end_of_main_text": base == FINISHED and has_text and self._unspoken_tail(),
            # reading from the screen and scanned PDFs
            "open_phase": self._open_phase if opening else "",
            "ocr_page": self._ocr_info[0] if ocr else 0,
            "ocr_total": self._ocr_info[1] if ocr else 0,
            "ocr_left": self._ocr_info[2] if ocr else -1,
            "scan_scanned": self._choice[0] if choice else 0,
            "scan_pages": self._choice[1] if choice else 0,
            "scan_est": self._choice[2] if choice else 0,
            "notice": self._ocr_kind if shown == OCR_NOTICE else "",
            "notice_detail": self._ocr_detail if shown == OCR_NOTICE else "",
            "severity": ("caution" if self._ocr_kind in OCR_CAUTION else "critical") if shown == OCR_NOTICE else "",
        }

    def _unspoken_tail(self) -> bool:
        """Text is shown after the last sentence read aloud (notes or a reference list that are skipped)."""
        doc = self._doc
        return bool(doc.sentences) and _NON_SPACE.search(doc.text, doc.sentences[-1].end) is not None

    def _emit_position(self) -> None:
        """position(sentence, word_lo, word_hi, played, buffered, in_word). Played and buffered are fractions of
        the spoken span (spoken_span()); while a file opens they are 0 and the share of pages read so far."""
        if self._shut:
            return
        if self._state == OPENING and self._open_shown:
            done, total = self._open_progress
            self.position.emit(-1, -1, -1, 0.0, _clamp(done / total, 0.0, 1.0) if total > 0 else 0.0, False)
            return
        doc = self._doc
        if doc is None or not doc.text or not doc.sentences:
            self._played_idx = 0.0
            self.position.emit(-1, -1, -1, 0.0, 0.0, False)
            return
        base = self._base()
        s = self._cur
        sent = doc.sentences[s]
        lo = hi = -1
        in_word = False
        idx = float(sent.start)
        chunk = self._chunks.get(self._loaded) if self._seek_target is None else None
        if base == FINISHED:
            idx = float(spoken_span(doc)[1])
        elif (chunk is not None and chunk.timing is not None and chunk.contains(s)
              and base in (READING, PAUSED)):
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
        self.position.emit(s, lo, hi, span_fraction(doc, idx), span_fraction(doc, self._buffered()), in_word)

    def _buffered(self) -> float:
        """End of the contiguous ready audio from the position, as a shown offset."""
        doc = self._doc
        if self._base() == FINISHED:
            return float(spoken_span(doc)[1])
        chunk = self._chunks.get(self._loaded)
        if chunk is not None and chunk.contains(self._cur) and self._after_cut is not None:
            last = self._after_cut - 1
        elif chunk is not None and chunk.contains(self._cur):
            last = chunk.plan.last
        else:
            chunk = self._ready_chunk(self._cur)
            if chunk is None:
                return float(doc.sentences[self._cur].start)
            last = chunk.plan.last
        while last + 1 < len(doc.sentences):
            following = self._ready_chunk(last + 1)
            if following is None:
                break
            last = following.plan.last
        return float(doc.sentences[last].end)

    def _measure(self, chunk: _Chunk) -> None:
        """Exponential moving average of audio ms per character, per speed (for time left)."""
        m = chunk.length_ms / max(1, len(chunk.plan.text))
        if self._mpc is None or self._mpc_speed != chunk.speed:
            self._mpc, self._mpc_speed = m, chunk.speed
        else:
            self._mpc += MPC_ALPHA * (m - self._mpc)

    def _time_left(self) -> int:
        doc = self._doc
        if doc is None or self._mpc is None or not doc.sentences:
            return -1
        if self._base() == FINISHED:
            return 0
        mpc = self._mpc * self._mpc_speed / self._rate  # old ms/char x old speed / new speed
        s = _clamp(self._cur, 0, len(doc.sentences) - 1)
        sent = doc.sentences[s]
        done = _clamp((self._played_idx - sent.start) / max(1, len(sent.text)), 0.0, 1.0)
        left = self._spoken_from(s + 1) + len(_say(sent)) * (1.0 - done)
        return int(round(left * mpc / 1000))

    def _spoken_from(self, sentence: int) -> int:
        """Spoken characters from `sentence` to the end, with the spaces chunks put between sentences (the
        unit the ms-per-character measurement uses). Skipped citations, notes and references don't count."""
        doc = self._doc
        own = getattr(doc, "spoken_chars_from", None)
        if own is not None:
            return own(sentence)
        if self._suffix is None or self._suffix[0] is not doc:
            n = len(doc.sentences)
            sums = [0] * (n + 1)
            for i in range(n - 1, -1, -1):
                sums[i] = sums[i + 1] + len(_say(doc.sentences[i])) + (1 if i < n - 1 else 0)
            self._suffix = (doc, sums)
        sums = self._suffix[1]
        return sums[_clamp(sentence, 0, len(sums) - 1)]

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
        base = self._base()
        want = bool(self._get("media_keys", True)) and (
            base in (PREPARING, READING, WAITING) or (base == PAUSED and self._media_timer.is_active()))
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

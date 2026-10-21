"""Tests for saylight.engine: the reading state machine.

Everything is faked: synth, player, input thread and settings are QObjects with the real signals,
and a fake clock drives the engine's injected one-shot timers. No audio, no network, no windows.
"""

import hashlib
import logging
import os
import re
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from unittest import mock

from PySide6.QtCore import QObject, Signal

from saylight import engine as E
from saylight.resume import ResumePoint, ResumeStore
from saylight.text_model import BuildOptions, Document

_quiet = logging.NullHandler()


def setUpModule():
    # keep warnings out of the test output (they would otherwise go to logging's last-resort handler)
    logging.getLogger("saylight").addHandler(_quiet)


def tearDownModule():
    logging.getLogger("saylight").removeHandler(_quiet)


# --- Test text ---------------------------------------------------------------------------------
# Each sentence is 59 characters and 10 words, so with the fake word timing below:
#   sentence j of a chunk starts at 3000*j + 100 ms and ends at 3000*j + 3050 ms (50 ms gaps),
#   the first chunk (<= 250 chars) holds 4 sentences and later chunks (<= 1500 chars) hold 25.

def sentence(i: int) -> str:
    return f"Sentence {i:03d} alpha beta gamma delta epsilon zeta eta theta."


def make_text(n: int) -> str:
    return " ".join(sentence(i) for i in range(n))


@dataclass(frozen=True)
class Word:
    offset_ms: float
    duration_ms: float
    text: str


def make_words(text: str) -> list[Word]:
    return [Word(100 + 300 * k, 250, tok) for k, tok in enumerate(re.findall(r"[A-Za-z0-9]+", text))]


def s_start(j: int) -> int:
    """Start (ms) of the j-th sentence within its chunk."""
    return 3000 * j + 100


def s_end(j: int) -> int:
    return 3000 * j + 3050


def chunk_length(text: str) -> int:
    return 3000 * text.count("Sentence ") + 100


# --- Fakes -------------------------------------------------------------------------------------

class FakeTimer:
    def __init__(self, clock: "FakeClock", callback):
        self.clock, self.callback = clock, callback
        self.active, self.due, self.seq = False, 0.0, 0

    def start(self, ms):
        self.active = True
        self.due = self.clock.now + ms / 1000
        self.seq = self.clock.next_seq()

    def stop(self):
        self.active = False

    def is_active(self):
        return self.active


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self._timers: list[FakeTimer] = []
        self._seq = 0

    def __call__(self) -> float:
        return self.now

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def timer(self, callback) -> FakeTimer:
        t = FakeTimer(self, callback)
        self._timers.append(t)
        return t

    def advance(self, ms: float):
        end = self.now + ms / 1000
        while True:
            due = [t for t in self._timers if t.active and t.due <= end + 1e-9]
            if not due:
                break
            t = min(due, key=lambda t: (t.due, t.seq))
            self.now = max(self.now, t.due)
            t.active = False
            t.callback()
        self.now = end


class FakeSynth(QObject):
    chunkReady = Signal(int, int, str, object)
    chunkFailed = Signal(int, int, str, str)

    def __init__(self):
        super().__init__()
        self.requests: list[dict] = []
        self.cancelled_before: list[int] = []
        self.deleted: list[str] = []
        self.started = self.shut = False

    def start(self):
        self.started = True

    def synthesize(self, gen, chunk_id, text, voice, rate):
        self.requests.append(dict(gen=gen, chunk_id=chunk_id, text=text, voice=voice, rate=rate))

    def cancel_before(self, gen):
        self.cancelled_before.append(gen)

    def cancel_all(self):
        self.cancelled_before.append(-1)

    def delete(self, path):
        self.deleted.append(path)

    def shutdown(self, timeout=2.0):
        self.shut = True


class FakePlayer(QObject):
    started = Signal(int, int)
    tick = Signal(int, int, bool)
    ended = Signal(int, str)
    failed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.cmds: list[tuple] = []
        self.loaded = None
        self.pending: list[tuple] = []
        self.running = self.shut = False

    def start(self):
        self.running = True

    def load(self, chunk_id, path, *, start_ms=0, play=True, cut_ms=None):
        if self.loaded is not None:
            self.pending.append((self.loaded, "stopped"))
        self.loaded = chunk_id
        self.cmds.append(("load", chunk_id, path, start_ms, play, cut_ms))

    def play(self):
        self.cmds.append(("play",))

    def pause(self):
        self.cmds.append(("pause",))

    def resume(self):
        self.cmds.append(("resume",))

    def seek(self, ms, play):
        self.cmds.append(("seek", ms, play))

    def set_cut(self, ms):
        self.cmds.append(("set_cut", ms))

    def set_volume(self, v):
        self.cmds.append(("set_volume", v))

    def stop_close(self):
        if self.loaded is not None:
            self.pending.append((self.loaded, "stopped"))
        self.loaded = None
        self.cmds.append(("stop_close",))

    def shutdown(self, timeout=2.0):
        self.shut = True

    def flush(self):
        pending, self.pending = self.pending, []
        for cid, why in pending:
            self.ended.emit(cid, why)


class FakeInput(QObject):
    ready = Signal()
    registered = Signal(int, bool)
    hotkeyPressed = Signal(int)
    copied = Signal(int, str, str)
    quitRequested = Signal()

    def __init__(self):
        super().__init__()
        self.copy_requests: list[tuple[int, int]] = []
        self.media: list[bool] = []

    def request_copy(self, token, last_app):
        self.copy_requests.append((token, last_app))

    def set_media_keys(self, on):
        self.media.append(on)


class FakeSettings(QObject):
    changed = Signal(str)

    def __init__(self, **values):
        super().__init__()
        self.values = {"speed": 0.95, "volume": 100, "voice": "edge:en-US-AvaNeural", "media_keys": True,
                       "skip_citations": True, "skip_urls": True}
        self.values.update(values)
        self.sets: list[tuple] = []

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        self.sets.append((key, value))
        if self.values.get(key) != value:
            self.values[key] = value
            self.changed.emit(key)


class H:
    """Test harness: an engine wired to fakes, with recorders for every engine signal."""

    def __init__(self, synth=None, engine_kwargs=None, **settings):
        self.clock = FakeClock()
        self.synth, self.player, self.input = synth or FakeSynth(), FakePlayer(), FakeInput()
        self.settings = FakeSettings(**settings)
        self.beeps: list[str] = []
        self.engine = E.Engine(synth=self.synth, player=self.player, input_thread=self.input,
                               settings=self.settings, clock=self.clock, timer_factory=self.clock.timer,
                               beep=self.beeps.append, **(engine_kwargs or {}))
        e = self.engine
        self.states, self.sessions, self.positions, self.time_left = [], [], [], []
        self.speeds, self.volumes, self.notices, self.media = [], [], [], []
        self.toggles = 0
        e.stateChanged.connect(lambda s, i: self.states.append((s, i)))
        e.sessionChanged.connect(self.sessions.append)
        e.position.connect(lambda *a: self.positions.append(a))
        e.timeLeft.connect(self.time_left.append)
        e.speedChanged.connect(lambda v, note: self.speeds.append((v, note)))
        e.volumeChanged.connect(self.volumes.append)
        e.notice.connect(self.notices.append)
        e.mediaKeysWanted.connect(self.media.append)
        e.panelToggleRequested.connect(self._toggle)
        e.start()

    def _toggle(self):
        self.toggles += 1

    # state
    @property
    def state(self) -> str:
        return self.engine.state

    @property
    def shown(self) -> str:
        return self.states[-1][0]

    @property
    def info(self) -> dict:
        return self.states[-1][1]

    def shown_states(self) -> list[str]:
        return [s for s, _ in self.states]

    # input
    def press(self, hk=E.HK_READ):
        self.input.hotkeyPressed.emit(hk)

    def copied(self, text, reason="ok", token=0):
        self.input.copied.emit(token, text, reason)

    def begin(self, n=12) -> Document:
        """Start a session through Ctrl+` with n sentences; returns the document."""
        self.press()
        self.copied(make_text(n))
        assert self.state == E.PREPARING, self.state
        return self.sessions[-1]

    # synth
    def req(self, i=-1) -> dict:
        return self.synth.requests[i]

    def reqs(self, chunk_id) -> list[dict]:
        return [r for r in self.synth.requests if r["chunk_id"] == chunk_id]

    def path(self, chunk_id) -> str:
        r = self.reqs(chunk_id)[-1]
        return f"C:/tmp/g{r['gen']}_c{chunk_id}.mp3"

    def ready(self, chunk_id, words=None):
        r = self.reqs(chunk_id)[-1]
        self.synth.chunkReady.emit(r["gen"], chunk_id, self.path(chunk_id),
                                   make_words(r["text"]) if words is None else words)

    def fail(self, chunk_id, kind="service"):
        r = self.reqs(chunk_id)[-1]
        self.synth.chunkFailed.emit(r["gen"], chunk_id, kind, "WebSocketError: boom")

    # player
    def started(self, chunk_id, length=None):
        r = self.reqs(chunk_id)[-1]
        self.player.started.emit(chunk_id, chunk_length(r["text"]) if length is None else length)

    def tick(self, chunk_id, pos, playing=True):
        self.player.tick.emit(chunk_id, pos, playing)

    def end(self, chunk_id, why="end"):
        if self.player.loaded == chunk_id:
            self.player.loaded = None
        self.player.ended.emit(chunk_id, why)

    def pump(self):
        self.player.flush()

    def cmds(self, name) -> list[tuple]:
        return [c for c in self.player.cmds if c[0] == name]

    def first_sentence(self, chunk_id) -> int:
        m = re.match(r"Sentence (\d{3})", self.reqs(chunk_id)[-1]["text"])
        return int(m.group(1))

    def play(self, n=12, ready_all=False) -> tuple[int, int]:
        """Start a session and get its first chunk playing; returns (A, B) chunk ids."""
        self.begin(n)
        a = self.req(0)["chunk_id"]
        b = self.req(1)["chunk_id"] if len(self.synth.requests) > 1 else None
        self.ready(a)
        self.started(a)
        if ready_all and b is not None:
            self.ready(b)
        assert self.state == E.READING, self.state
        return a, b

    def advance(self, ms):
        self.clock.advance(ms)


# --- Tests -------------------------------------------------------------------------------------

class StartupTests(unittest.TestCase):
    def test_initial_state_and_start(self):
        h = H()
        self.assertEqual(h.state, E.READY)
        self.assertEqual(h.shown, "ready")
        self.assertTrue(h.synth.started)
        self.assertTrue(h.player.running)
        self.assertIn(("set_volume", 100), h.player.cmds)
        self.assertEqual(h.speeds[-1], (0.95, ""))
        self.assertEqual(h.volumes[-1], 100)

    def test_shortcut_failure_and_recovery(self):
        h = H()
        h.input.registered.emit(E.HK_READ, False)
        self.assertEqual(h.shown, E.SHORTCUT_UNAVAILABLE)
        self.assertIn("shortcut_unavailable", h.notices)
        self.assertFalse(h.info["shortcut_ok"])
        self.assertEqual(h.beeps, [])
        h.input.registered.emit(E.HK_READ, True)
        self.assertEqual(h.shown, E.READY)

    def test_other_hotkey_registration_failures_are_ignored(self):
        h = H()
        h.input.registered.emit(E.HK_MEDIA_NEXT, False)
        self.assertEqual(h.shown, E.READY)

    def test_offline_ready(self):
        h = H()
        h.engine.set_online(False)
        self.assertEqual(h.shown, E.READY_OFFLINE)
        h.engine.set_online(True)
        self.assertEqual(h.shown, E.READY)

    def test_shortcut_unavailable_wins_over_offline(self):
        h = H()
        h.engine.set_online(False)
        h.input.registered.emit(E.HK_READ, False)
        self.assertEqual(h.shown, E.SHORTCUT_UNAVAILABLE)

    def test_offline_does_not_replace_finished(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        h.engine.set_online(False)
        self.assertEqual(h.shown, E.FINISHED)
        self.assertFalse(h.info["online"])


class HotkeyTests(unittest.TestCase):
    def test_press_in_ready_shows_copying_only_after_150_ms(self):
        h = H()
        h.press()
        self.assertEqual(h.state, E.COPYING)
        self.assertEqual(h.shown, E.READY)
        h.advance(149)
        self.assertEqual(h.shown, E.READY)
        h.advance(2)
        self.assertEqual(h.shown, E.COPYING)
        self.assertEqual(h.info["play_action"], "none")
        self.assertTrue(h.info["can_stop"])

    def test_fast_copy_never_shows_copying(self):
        h = H()
        h.press()
        h.advance(100)
        h.copied(make_text(3))
        self.assertEqual(h.shown, E.PREPARING)
        self.assertNotIn(E.COPYING, h.shown_states())

    def test_empty_in_ready_toggles_panel_without_beep(self):
        h = H()
        h.press()
        h.copied("", "empty")
        self.assertEqual(h.state, E.READY)
        self.assertEqual(h.toggles, 1)
        self.assertEqual(h.beeps, [])
        self.assertNotIn("nothing_selected", h.notices)
        self.assertNotIn(E.NOTHING_SELECTED, h.shown_states())

    def test_line_copy_counts_as_empty(self):
        h = H()
        h.press()
        h.copied("", "line_copy")
        self.assertEqual(h.toggles, 1)
        self.assertEqual(h.state, E.READY)

    def test_empty_in_finished_toggles_panel_and_keeps_finished(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        h.press()
        h.copied("", "empty")
        self.assertEqual(h.state, E.FINISHED)
        self.assertEqual(h.toggles, 1)
        self.assertEqual(h.beeps, [])

    def test_text_in_error_starts_new_reading(self):
        h = H()
        h.begin()
        h.fail(h.req(0)["chunk_id"], "service")
        self.assertEqual(h.state, E.ERROR_SERVICE)
        h.press()
        h.copied(make_text(2))
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual(len(h.sessions[-1].sentences), 2)

    def test_press_while_reading_pauses_immediately(self):
        h = H()
        h.play()
        h.press()
        self.assertEqual(h.state, E.PAUSED)
        self.assertEqual(h.player.cmds[-1], ("pause",))
        self.assertEqual(h.input.copy_requests, [])  # the input thread does the copy itself

    def test_optimistic_pause_then_text_replaces_session(self):
        h = H()
        a, b = h.play(ready_all=True)
        old_gen = h.req(0)["gen"]
        h.press()
        h.copied("A brand new text. With two sentences.")
        h.pump()
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual([s.text for s in h.sessions[-1].sentences],
                         ["A brand new text.", "With two sentences."])
        self.assertIn(("stop_close",), h.player.cmds)
        self.assertGreater(h.synth.cancelled_before[-1], old_gen)
        self.assertIn(h.path(a), h.synth.deleted)
        self.assertIn(h.path(b), h.synth.deleted)
        self.assertEqual(h.req()["text"], "A brand new text. With two sentences.")

    def test_optimistic_pause_then_empty_stays_paused(self):
        h = H()
        h.play()
        h.press()
        h.copied("", "empty")
        self.assertEqual(h.state, E.PAUSED)
        self.assertEqual(h.toggles, 0)
        self.assertEqual(h.cmds("resume"), [])
        self.assertEqual(h.beeps, [])

    def test_paused_then_press_with_empty_resumes(self):
        h = H()
        h.play()
        h.engine.toggle_pause()
        h.press()
        self.assertEqual(h.state, E.PAUSED)
        h.copied("", "empty")
        self.assertEqual(h.state, E.READING)
        self.assertEqual(h.player.cmds[-1], ("resume",))
        self.assertEqual(h.toggles, 0)

    def test_paused_then_press_with_text_starts_new_reading(self):
        h = H()
        h.play()
        h.engine.toggle_pause()
        h.press()
        h.copied(make_text(3))
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual(len(h.sessions[-1].sentences), 3)

    def test_press_while_preparing_pauses_and_synthesis_continues(self):
        h = H()
        h.begin()
        cancels = list(h.synth.cancelled_before)
        a = h.req(0)["chunk_id"]
        h.press()
        self.assertEqual(h.state, E.PAUSED)
        self.assertEqual(h.synth.cancelled_before, cancels)
        h.ready(a)
        self.assertEqual(h.cmds("load")[-1][1], a)
        self.assertFalse(h.cmds("load")[-1][4])  # loaded unplayed
        h.copied("", "empty")
        self.assertEqual(h.state, E.PAUSED)

    def test_press_while_waiting_pauses(self):
        h = H()
        a, b = h.play()
        h.end(a)
        self.assertEqual(h.state, E.WAITING)
        h.press()
        self.assertEqual(h.state, E.PAUSED)
        h.copied("", "empty")
        self.assertEqual(h.state, E.PAUSED)

    def test_press_during_click_copy_is_ignored(self):
        h = H()
        h.engine.read_selection_click(42)
        token = h.input.copy_requests[-1][0]
        h.press()
        self.assertEqual(h.state, E.COPYING)
        h.copied("Ignored hotkey text.", token=0)
        self.assertEqual(h.state, E.COPYING)
        self.assertEqual(h.sessions, [])
        h.copied("Click text.", token=token)
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual(h.sessions[-1].sentences[0].text, "Click text.")

    def test_stop_during_hotkey_copy_cancels_it(self):
        h = H()
        h.press()
        h.engine.stop()
        self.assertEqual(h.state, E.READY)
        h.copied(make_text(2))
        self.assertEqual(h.state, E.READY)
        self.assertEqual(h.sessions, [])

    def test_user_resume_before_empty_result_is_kept(self):
        h = H()
        h.play()
        h.press()
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.READING)
        h.copied("", "empty")
        self.assertEqual(h.state, E.READING)
        self.assertEqual(len(h.cmds("pause")), 1)

    def test_stop_after_optimistic_pause_cancels_the_pending_copy(self):
        h = H()
        h.play()
        requests = len(h.synth.requests)
        h.press()                       # pauses at once; the copy is still running
        h.engine.stop()
        h.copied(make_text(2))          # the copy finishes after the user pressed Stop
        self.assertEqual(h.state, E.STOPPED)
        self.assertEqual(len(h.sessions), 1)
        self.assertEqual(len(h.synth.requests), requests)

    def test_media_stop_after_press_while_paused_cancels_the_pending_copy(self):
        h = H()
        h.play()
        h.engine.toggle_pause()
        h.press()
        h.press(E.HK_MEDIA_STOP)
        h.copied(make_text(2))
        self.assertEqual(h.state, E.STOPPED)
        self.assertEqual(len(h.sessions), 1)

    def test_stop_cancels_only_presses_already_made(self):
        h = H()
        h.play()
        h.press()
        h.engine.stop()
        h.copied("", "empty")           # the cancelled press
        h.press()                       # a new press from Stopped reads the new selection
        h.copied(make_text(3))
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual(len(h.sessions[-1].sentences), 3)

    def test_hotkey_result_arriving_during_a_click_copy_is_dropped(self):
        h = H()
        h.begin()
        h.engine.toggle_pause()          # Paused before any audio
        h.press()                        # its copy is still running ...
        h.fail(h.req(0)["chunk_id"])     # ... when the awaited chunk fails: an error, a resting state
        self.assertEqual(h.state, E.ERROR_SERVICE)
        h.engine.read_selection_click(1)
        token = h.input.copy_requests[-1][0]
        h.copied("Hotkey text.", token=0)
        self.assertEqual(h.state, E.COPYING)
        h.copied("Click text.", token=token)
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual(h.sessions[-1].sentences[0].text, "Click text.")

    def test_press_while_paused_then_stop_then_empty_does_not_toggle_panel(self):
        h = H()
        h.play()
        h.engine.toggle_pause()
        h.press()
        h.engine.stop()
        h.copied("", "empty")
        self.assertEqual(h.state, E.STOPPED)
        self.assertEqual(h.toggles, 0)
        self.assertEqual(h.cmds("resume"), [])

    def test_whitespace_only_text_counts_as_empty(self):
        h = H()
        h.press()
        h.copied("  \n\t ", "ok")
        self.assertEqual(h.state, E.READY)
        self.assertEqual(h.toggles, 1)
        self.assertEqual(h.beeps, [])

    def test_busy_clipboard_on_hotkey_in_resting_shows_notice(self):
        h = H()
        h.press()
        h.copied("", "busy")
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        self.assertEqual(h.info["reason"], "busy")
        self.assertEqual(h.beeps, ["ok"])
        self.assertEqual(h.toggles, 0)

    def test_busy_clipboard_on_hotkey_while_paused_stays_paused(self):
        h = H()
        h.play()
        h.engine.toggle_pause()
        h.press()
        h.copied("", "busy")
        self.assertEqual(h.state, E.PAUSED)
        self.assertEqual(h.cmds("resume"), [])

    def test_unspeakable_text_is_treated_as_nothing_selected(self):
        h = H()
        with mock.patch.object(E.Document, "from_raw", return_value=Document("", [])):
            h.press()
            h.copied("https://example.org")
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        self.assertEqual(h.info["reason"], "empty")
        self.assertEqual(h.sessions, [])

    def test_press_during_nothing_selected_overlay_reads(self):
        h = H()
        h.engine.read_selection_click(1)
        h.copied("", "empty", token=h.input.copy_requests[-1][0])
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        h.press()
        h.copied(make_text(2))
        self.assertEqual(h.state, E.PREPARING)

    def test_panel_hotkey(self):
        h = H()
        h.press(E.HK_PANEL)
        self.assertEqual(h.toggles, 1)
        self.assertEqual(h.state, E.READY)

    def test_pause_hotkey(self):
        h = H()
        h.press(E.HK_PAUSE)
        self.assertEqual(h.state, E.READY)
        self.assertEqual(h.beeps, [])
        h.play()
        h.press(E.HK_PAUSE)
        self.assertEqual(h.state, E.PAUSED)
        h.press(E.HK_PAUSE)
        self.assertEqual(h.state, E.READING)

    def test_media_keys(self):
        h = H()
        a, _ = h.play(ready_all=True)
        h.press(E.HK_MEDIA_PLAY_PAUSE)
        self.assertEqual(h.state, E.PAUSED)
        h.press(E.HK_MEDIA_PLAY_PAUSE)
        self.assertEqual(h.state, E.READING)
        h.press(E.HK_MEDIA_NEXT)
        self.assertEqual(h.info["sentence"], 1)
        h.press(E.HK_MEDIA_PREV)
        self.assertEqual(h.info["sentence"], 0)
        h.press(E.HK_MEDIA_STOP)
        self.assertEqual(h.state, E.STOPPED)


class ClickTests(unittest.TestCase):
    def test_click_requests_copy(self):
        h = H()
        h.engine.read_selection_click(1234)
        self.assertEqual(len(h.input.copy_requests), 1)
        token, last_app = h.input.copy_requests[0]
        self.assertGreater(token, 0)
        self.assertEqual(last_app, 1234)
        self.assertEqual(h.state, E.COPYING)

    def test_click_text_starts_reading(self):
        h = H()
        h.engine.read_selection_click(1)
        h.copied(make_text(5), token=h.input.copy_requests[-1][0])
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual(len(h.sessions[-1].sentences), 5)

    def test_click_empty_shows_nothing_selected_then_returns(self):
        h = H()
        h.engine.read_selection_click(1)
        h.copied("", "empty", token=h.input.copy_requests[-1][0])
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        self.assertEqual(h.info["reason"], "empty")
        self.assertEqual(h.info["underlying"], E.READY)
        self.assertEqual(h.info["play_action"], "read")
        self.assertEqual(h.beeps, ["ok"])
        self.assertIn("nothing_selected", h.notices)
        self.assertEqual(h.toggles, 0)
        h.advance(E.NOTHING_SCREEN_MS - 1)  # it offers Read from screen, so it stays 6 s
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        h.advance(2)
        self.assertEqual(h.shown, E.READY)

    def test_click_reason_variants(self):
        for reason in ("line_copy", "shell", "console", "busy"):
            h = H()
            h.engine.read_selection_click(1)
            h.copied("", reason, token=h.input.copy_requests[-1][0])
            self.assertEqual(h.shown, E.NOTHING_SELECTED, reason)
            self.assertEqual(h.info["reason"], reason)
            self.assertEqual(h.beeps, ["ok"])

    def test_stale_click_token_ignored(self):
        h = H()
        h.engine.read_selection_click(1)
        token = h.input.copy_requests[-1][0]
        h.engine.stop()
        self.assertEqual(h.state, E.READY)
        h.copied(make_text(2), token=token)
        self.assertEqual(h.state, E.READY)
        self.assertEqual(h.sessions, [])

    def test_second_click_while_copying_ignored(self):
        h = H()
        h.engine.read_selection_click(1)
        h.engine.read_selection_click(1)
        self.assertEqual(len(h.input.copy_requests), 1)

    def test_click_ignored_while_active(self):
        h = H()
        h.play()
        h.engine.read_selection_click(1)
        self.assertEqual(h.input.copy_requests, [])
        self.assertEqual(h.state, E.READING)

    def test_hotkey_copy_watchdog_returns_to_resting_state(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        h.press()
        h.advance(5000)
        self.assertEqual(h.state, E.FINISHED)
        h.copied(make_text(2))          # far too late: the press was given up
        self.assertEqual(h.state, E.FINISHED)
        self.assertEqual(len(h.sessions), 1)
        h.press()                       # the next press works normally
        h.copied(make_text(2))
        self.assertEqual(h.state, E.PREPARING)

    def test_copy_watchdog_returns_to_resting_state(self):
        h = H()
        h.engine.read_selection_click(1)
        token = h.input.copy_requests[-1][0]
        h.advance(5000)
        self.assertEqual(h.state, E.READY)
        h.copied(make_text(2), token=token)
        self.assertEqual(h.sessions, [])

    def test_overlay_over_stopped_keeps_underlying_controls(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(2) + 500)
        h.engine.stop()
        h.engine.read_selection_click(1)
        h.copied("", "empty", token=h.input.copy_requests[-1][0])
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        self.assertEqual(h.info["underlying"], E.STOPPED)
        self.assertEqual(h.info["prev_action"], "read_from")
        h.engine.prev()
        self.assertEqual(h.state, E.WAITING)
        self.assertEqual(h.info["sentence"], 2)
        h.advance(3000)
        self.assertEqual(h.state, E.WAITING)


class SessionTests(unittest.TestCase):
    def test_new_session_plans_short_first_chunk_and_prefetches_two(self):
        h = H()
        doc = h.begin(12)
        self.assertEqual(len(doc.sentences), 12)
        self.assertEqual(len(h.synth.requests), 2)
        first, second = h.synth.requests
        self.assertLessEqual(len(first["text"]), 250)
        self.assertEqual(first["text"], " ".join(sentence(i) for i in range(4)))
        self.assertEqual(second["text"], " ".join(sentence(i) for i in range(4, 12)))
        self.assertEqual(first["rate"], 0.95)  # a number; the synth makes its own rate format
        self.assertIsInstance(first["rate"], float)
        self.assertEqual(first["voice"], "edge:en-US-AvaNeural")
        self.assertNotEqual(first["chunk_id"], second["chunk_id"])
        self.assertEqual(h.shown, E.PREPARING)
        self.assertEqual(h.info["total"], 12)
        self.assertEqual(h.info["play_action"], "pause")
        self.assertEqual(h.info["prev_action"], "none")
        self.assertFalse(h.info["can_next"])
        self.assertFalse(h.info["can_seek"])

    def test_first_chunk_ready_loads_and_plays(self):
        h = H()
        h.begin()
        a = h.req(0)["chunk_id"]
        h.ready(a)
        self.assertEqual(h.cmds("load")[-1], ("load", a, h.path(a), 0, True, None))
        self.assertEqual(h.state, E.PREPARING)
        h.started(a)
        self.assertEqual(h.state, E.READING)
        self.assertIn("reading_started", h.notices)
        self.assertEqual(h.media[-1], True)
        self.assertEqual(h.input.media[-1], True)

    def test_prefetch_keeps_at_most_two_unplayed(self):
        h = H()
        h.begin(100)
        self.assertEqual(len(h.synth.requests), 2)
        a, b = h.req(0)["chunk_id"], h.req(1)["chunk_id"]
        h.ready(a)
        self.assertEqual(len(h.synth.requests), 2)
        h.started(a)
        self.assertEqual(len(h.synth.requests), 3)
        c = h.req(2)["chunk_id"]
        self.assertEqual(h.first_sentence(c), 29)
        h.ready(b)
        h.ready(c)
        self.assertEqual(len(h.synth.requests), 3)
        h.end(a)
        self.assertEqual(h.cmds("load")[-1][1], b)
        self.assertEqual(len(h.synth.requests), 3)
        h.started(b)
        self.assertEqual(len(h.synth.requests), 4)
        self.assertEqual(h.first_sentence(h.req()["chunk_id"]), 54)
        self.assertGreater(len(h.req()["text"]), 250)

    def test_chunk_end_loads_next_ready_chunk(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.end(a)
        self.assertEqual(h.cmds("load")[-1], ("load", b, h.path(b), 0, True, None))
        self.assertEqual(h.state, E.READING)
        h.started(b)
        self.assertEqual(h.state, E.READING)
        self.assertEqual(h.info["sentence"], 4)

    def test_chunk_end_waits_when_next_not_ready(self):
        h = H()
        a, b = h.play()
        h.end(a)
        self.assertEqual(h.state, E.WAITING)
        self.assertEqual(h.shown, E.READING)  # Waiting shows only after 300 ms
        self.assertEqual(h.info["sentence"], 4)
        h.advance(300)
        self.assertEqual(h.shown, E.WAITING)
        h.ready(b)
        self.assertEqual(h.cmds("load")[-1][1], b)
        h.started(b)
        self.assertEqual(h.shown, E.READING)

    def test_short_wait_never_shows_waiting(self):
        h = H()
        a, b = h.play()
        h.end(a)
        h.advance(100)
        h.ready(b)
        h.started(b)
        h.advance(500)
        self.assertNotIn(E.WAITING, h.shown_states())

    def test_last_chunk_end_finishes(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.end(a)
        h.started(b)
        h.end(b)
        self.assertEqual(h.state, E.FINISHED)
        self.assertIn("finished", h.notices)
        self.assertEqual(h.media[-1], False)
        self.assertEqual(h.positions[-1][0], 11)
        self.assertAlmostEqual(h.positions[-1][3], 1.0)
        self.assertEqual(h.info["play_action"], "read")
        self.assertEqual(h.info["prev_action"], "read_last_again")
        self.assertFalse(h.info["can_next"])
        self.assertFalse(h.info["can_stop"])

    def test_stale_generation_result_is_deleted(self):
        h = H()
        h.begin()
        old = h.req(0)
        h.press()  # optimistic pause
        h.copied(make_text(3))
        h.synth.chunkReady.emit(old["gen"], old["chunk_id"], "C:/tmp/stale.mp3", [])
        self.assertIn("C:/tmp/stale.mp3", h.synth.deleted)
        self.assertNotIn(old["chunk_id"], [c[1] for c in h.cmds("load")])

    def test_tick_updates_sentence_and_word(self):
        h = H()
        a, _ = h.play()
        pos = s_start(1) + 600 + 50  # third word of sentence 1 ("alpha"), inside it
        h.tick(a, pos)
        sent, lo, hi, played, buffered, in_word = h.positions[-1]
        self.assertEqual(sent, 1)
        self.assertEqual(sentence(1)[lo:hi], "alpha")
        self.assertTrue(in_word)
        self.assertEqual(h.info["sentence"], 1)
        h.tick(a, s_end(1) + 20)  # in the gap after sentence 1
        sent, lo, hi, played, buffered, in_word = h.positions[-1]
        self.assertEqual((sent, sentence(1)[lo:hi], in_word), (1, "theta", False))

    def test_played_and_buffered_fractions(self):
        h = H()
        a, b = h.play()
        doc = h.sessions[-1]
        total = len(doc.text)
        h.tick(a, s_start(1) + 600 + 125)  # half-way through "alpha" of sentence 1
        _, lo, hi, played, buffered, _ = h.positions[-1]
        expected = (doc.sentences[1].start + lo + (hi - lo) * 0.5) / total
        self.assertAlmostEqual(played, expected)
        self.assertAlmostEqual(buffered, doc.sentences[3].end / total)
        h.ready(b)
        h.tick(a, s_start(1) + 700)
        self.assertAlmostEqual(h.positions[-1][4], 1.0)

    def test_time_left_unknown_until_measured_then_throttled(self):
        h = H()
        h.begin()
        self.assertEqual(h.time_left[-1], -1)
        a = h.req(0)["chunk_id"]
        h.ready(a)
        length = chunk_length(h.req(0)["text"])
        h.started(a, length)
        h.tick(a, 100)
        total = len(h.sessions[-1].text)
        expected = round(total * length / len(h.req(0)["text"]) / 1000)
        self.assertEqual(h.time_left[-1], expected)
        count = len(h.time_left)
        for k in range(1, 20):  # 1 s of ticks at 20 Hz
            h.advance(50)
            h.tick(a, 100 + 50 * k)
        self.assertLessEqual(len(h.time_left) - count, 1)

    def test_session_changed_and_state_info(self):
        h = H()
        a, _ = h.play()
        self.assertEqual(h.info["play_action"], "pause")
        self.assertEqual(h.info["prev_action"], "prev")
        self.assertTrue(h.info["can_next"])
        self.assertTrue(h.info["can_seek"])
        self.assertTrue(h.info["has_text"])
        self.assertEqual(h.info["label"], "Reading")


class PauseTests(unittest.TestCase):
    def test_pause_and_resume_while_reading(self):
        h = H()
        a, _ = h.play()
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.PAUSED)
        self.assertEqual(h.player.cmds[-1], ("pause",))
        self.assertIn("paused", h.notices)
        self.assertEqual(h.info["play_action"], "resume")
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.READING)
        self.assertEqual(h.player.cmds[-1], ("resume",))
        self.assertIn("resumed", h.notices)

    def test_pause_while_preparing_loads_unplayed_then_resume_plays(self):
        h = H()
        h.begin()
        a = h.req(0)["chunk_id"]
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.PAUSED)
        h.ready(a)
        self.assertEqual(h.cmds("load")[-1], ("load", a, h.path(a), 0, False, None))
        h.engine.toggle_pause()
        self.assertEqual(h.player.cmds[-1], ("resume",))
        h.started(a)
        self.assertEqual(h.state, E.READING)
        self.assertIn("reading_started", h.notices)

    def test_resume_before_audio_is_ready_waits(self):
        h = H()
        h.begin()
        h.engine.toggle_pause()
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.PREPARING)
        a = h.req(0)["chunk_id"]
        h.ready(a)
        self.assertTrue(h.cmds("load")[-1][4])

    def test_pause_while_waiting_then_chunk_loads_unplayed(self):
        h = H()
        a, b = h.play()
        h.end(a)
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.PAUSED)
        h.ready(b)
        self.assertEqual(h.cmds("load")[-1][1:5], (b, h.path(b), 0, False))
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.READING)

    def test_media_keys_released_ten_minutes_into_pause(self):
        h = H()
        h.play()
        h.engine.toggle_pause()
        self.assertTrue(h.media[-1])
        h.advance(10 * 60 * 1000 - 1)
        self.assertTrue(h.media[-1])
        h.advance(2)
        self.assertFalse(h.media[-1])
        self.assertFalse(h.input.media[-1])
        h.engine.toggle_pause()
        self.assertTrue(h.media[-1])

    def test_media_keys_setting_off(self):
        h = H(media_keys=False)
        h.play()
        self.assertNotIn(True, h.media)
        self.assertEqual(h.input.media, [])

    def test_toggle_pause_does_nothing_when_resting(self):
        h = H()
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.READY)
        self.assertEqual(h.player.cmds, [("set_volume", 100)])


class StopTests(unittest.TestCase):
    def test_stop_reading_keeps_text_and_cache(self):
        h = H()
        a, b = h.play()
        h.tick(a, s_start(2) + 100)
        gen = h.req(0)["gen"]
        h.engine.stop()
        h.pump()
        self.assertEqual(h.state, E.STOPPED)
        self.assertIn(("stop_close",), h.player.cmds)
        self.assertGreater(h.synth.cancelled_before[-1], gen)
        self.assertNotIn(h.path(a), h.synth.deleted)
        self.assertIsNotNone(h.sessions[-1])
        self.assertEqual(h.info["sentence"], 2)
        self.assertEqual(h.info["prev_action"], "read_from")
        self.assertFalse(h.info["can_stop"])
        self.assertTrue(h.info["can_seek"])
        self.assertIn("stopped", h.notices)
        self.assertFalse(h.media[-1])

    def test_stop_in_error_goes_to_stopped(self):
        h = H()
        h.begin()
        h.fail(h.req(0)["chunk_id"], "offline")
        h.engine.stop()
        self.assertEqual(h.state, E.STOPPED)

    def test_stop_does_nothing_when_ready(self):
        h = H()
        h.engine.stop()
        self.assertEqual(h.state, E.READY)

    def test_prev_in_stopped_restarts_stopped_sentence_from_cache(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(2) + 100)
        h.engine.stop()
        h.pump()
        n_requests = len(h.synth.requests)
        h.engine.prev()
        self.assertEqual(h.state, E.WAITING)
        self.assertEqual(h.shown, E.WAITING)
        self.assertEqual(h.cmds("load")[-1], ("load", a, h.path(a), max(s_end(1), s_start(2) - 80), True, None))
        h.started(a)
        self.assertEqual(h.state, E.READING)
        self.assertEqual(h.info["sentence"], 2)
        # the cached first chunk is reused; only the dropped prefetch (from sentence 4) is requested again
        self.assertTrue(all(h.first_sentence(r["chunk_id"]) >= 4 for r in h.synth.requests[n_requests:]))

    def test_prev_in_finished_reads_last_sentence(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        h.engine.prev()
        self.assertEqual(h.state, E.WAITING)
        self.assertEqual(h.cmds("load")[-1][1], a)
        self.assertEqual(h.cmds("load")[-1][3], max(s_end(2), s_start(3) - 80))
        self.assertEqual(h.info["sentence"], 3)

    def test_replay_from_start(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        h.engine.replay_from_start()
        self.assertEqual(h.cmds("load")[-1][1:5], (a, h.path(a), 0, True))
        self.assertEqual(h.info["sentence"], 0)

    def test_jump_to_in_finished_reads_from_there(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        h.engine.jump_to(1)
        self.assertEqual(h.state, E.WAITING)
        self.assertEqual(h.cmds("load")[-1][3], max(s_end(0), s_start(1) - 80))

    def test_next_does_nothing_when_stopped(self):
        h = H()
        h.play()
        h.engine.stop()
        h.engine.next()
        self.assertEqual(h.state, E.STOPPED)


class SeekTests(unittest.TestCase):
    def test_next_updates_counter_now_and_audio_after_150_ms(self):
        h = H()
        a, _ = h.play()
        h.engine.next()
        self.assertEqual(h.info["sentence"], 1)
        self.assertEqual(h.positions[-1][0], 1)
        self.assertEqual(h.cmds("seek"), [])
        h.advance(149)
        self.assertEqual(h.cmds("seek"), [])
        h.advance(1)
        self.assertEqual(h.cmds("seek"), [("seek", max(s_end(0), s_start(1) - 80), True)])
        self.assertEqual(h.state, E.READING)

    def test_seek_presses_accumulate(self):
        h = H()
        a, _ = h.play()
        for _ in range(3):
            h.engine.next()
            h.advance(100)
        self.assertEqual(h.info["sentence"], 3)
        h.advance(50)
        self.assertEqual(h.cmds("seek"), [("seek", max(s_end(2), s_start(3) - 80), True)])

    def test_seek_into_other_ready_chunk_loads_it(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.engine.jump_to(6)
        h.advance(150)
        self.assertEqual(h.cmds("load")[-1], ("load", b, h.path(b), max(s_end(1), s_start(2) - 80), True, None))
        self.assertEqual(h.state, E.WAITING)
        h.started(b)
        self.assertEqual(h.state, E.READING)
        self.assertNotIn(E.WAITING, h.shown_states())

    def test_seek_to_uncached_sentence_replans(self):
        h = H()
        a, b = h.play(100)
        gen = h.req(0)["gen"]
        h.engine.jump_to(50)
        h.advance(150)
        h.pump()
        self.assertEqual(h.state, E.WAITING)
        self.assertGreater(h.synth.cancelled_before[-1], gen)
        self.assertIn(("stop_close",), h.player.cmds)
        new = [r for r in h.synth.requests if r["gen"] > gen]
        self.assertEqual(len(new), 2)
        self.assertTrue(new[0]["text"].startswith(sentence(50)))
        self.assertLessEqual(len(new[0]["text"]), 250)
        h.ready(new[0]["chunk_id"])
        self.assertEqual(h.cmds("load")[-1][1:5], (new[0]["chunk_id"], h.path(new[0]["chunk_id"]), 0, True))

    def test_prev_restarts_sentence_after_1_5_s(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 1600)
        h.engine.prev()
        self.assertEqual(h.info["sentence"], 1)
        h.advance(150)
        self.assertEqual(h.cmds("seek")[-1], ("seek", max(s_end(0), s_start(1) - 80), True))

    def test_prev_goes_back_within_1_5_s(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 1400)
        h.engine.prev()
        self.assertEqual(h.info["sentence"], 0)
        h.advance(150)
        self.assertEqual(h.cmds("seek")[-1], ("seek", 0, True))

    def test_prev_on_first_sentence_restarts(self):
        h = H()
        a, _ = h.play()
        h.tick(a, 500)
        h.engine.prev()
        self.assertEqual(h.info["sentence"], 0)
        h.advance(150)
        self.assertEqual(h.cmds("seek")[-1], ("seek", 0, True))

    def test_repeated_prev_steps_back_each_press(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(3) + 2000)
        h.engine.prev()  # restart 3
        h.engine.prev()  # 2
        h.engine.prev()  # 1
        self.assertEqual(h.info["sentence"], 1)

    def test_next_disabled_on_last_sentence(self):
        h = H()
        a, _ = h.play(4)
        h.tick(a, s_start(3) + 100)
        self.assertFalse(h.info["can_next"])
        h.engine.next()
        h.advance(200)
        self.assertEqual(h.cmds("seek"), [])
        self.assertEqual(h.info["sentence"], 3)

    def test_seek_while_paused_stays_paused(self):
        h = H()
        a, _ = h.play()
        h.engine.toggle_pause()
        h.engine.next()
        h.advance(150)
        self.assertEqual(h.cmds("seek")[-1], ("seek", max(s_end(0), s_start(1) - 80), False))
        self.assertEqual(h.state, E.PAUSED)
        self.assertEqual(h.info["prev_action"], "prev")

    def test_uncached_seek_while_paused_loads_unplayed(self):
        h = H()
        a, _ = h.play(100)
        h.engine.toggle_pause()
        h.engine.jump_to(60)
        h.advance(150)
        self.assertEqual(h.state, E.PAUSED)
        new = h.req(-2)
        self.assertTrue(new["text"].startswith(sentence(60)))
        h.ready(new["chunk_id"])
        self.assertFalse(h.cmds("load")[-1][4])
        h.engine.toggle_pause()
        self.assertEqual(h.player.cmds[-1], ("resume",))

    def test_ticks_during_pending_seek_do_not_move_counter(self):
        h = H()
        a, _ = h.play()
        h.engine.next()
        h.engine.next()
        h.tick(a, 300)
        self.assertEqual(h.info["sentence"], 2)
        self.assertEqual(h.positions[-1][0], 2)

    def test_counter_holds_during_seek_preroll(self):
        h = H()
        a, _ = h.play()
        h.engine.next()
        h.advance(150)
        h.tick(a, max(s_end(0), s_start(1) - 80) + 10)  # still before sentence 1 starts
        self.assertEqual(h.info["sentence"], 1)

    def test_chunk_end_during_pending_seek_goes_to_the_seek_target(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.tick(a, s_start(3) + 2000)
        h.engine.prev()  # restart sentence 3 (more than 1.5 s in)
        h.end(a)         # the chunk ends before the seek acts
        self.assertEqual(h.info["sentence"], 3)
        self.assertEqual(h.cmds("load")[-1][1:5], (a, h.path(a), max(s_end(2), s_start(3) - 80), True))
        self.assertNotIn(b, [c[1] for c in h.cmds("load")])
        h.advance(200)
        self.assertEqual(len(h.cmds("load")), 2)

    def test_jump_to_ignored_when_ready_or_preparing(self):
        h = H()
        h.engine.jump_to(3)
        self.assertEqual(h.state, E.READY)
        h.begin()
        h.engine.jump_to(3)
        h.advance(200)
        self.assertEqual(h.info["sentence"], 0)
        self.assertEqual(h.cmds("seek"), [])

    def test_jump_to_clamps(self):
        h = H()
        h.play()
        h.engine.jump_to(99)
        self.assertEqual(h.info["sentence"], 11)


class SpeedTests(unittest.TestCase):
    def speeds_set(self, h):
        return [v for k, v in h.settings.sets if k == "speed"]

    def test_shown_at_once_committed_after_400_ms(self):
        h = H()
        h.engine.set_speed(1.25)
        self.assertEqual(h.speeds[-1][0], 1.25)
        self.assertEqual(h.engine.speed, 1.25)
        self.assertEqual(self.speeds_set(h), [])
        h.advance(399)
        self.assertEqual(self.speeds_set(h), [])
        h.advance(1)
        self.assertEqual(self.speeds_set(h), [1.25])
        self.assertFalse(h.engine.speed_pending)

    def test_rapid_input_collapses_into_one_commit(self):
        h = H()
        a, _ = h.play()
        before = len(h.synth.requests)
        for v in (1.0, 1.05, 1.1, 1.15):
            h.engine.set_speed(v)
            h.advance(100)
        h.advance(400)
        self.assertEqual(self.speeds_set(h), [1.15])
        new = h.synth.requests[before:]
        self.assertTrue(new)
        self.assertTrue(all(r["rate"] == 1.15 for r in new))
        self.assertEqual(len(h.cmds("set_cut")), 1)

    def test_snaps_to_speed_list(self):
        h = H()
        h.engine.set_speed(1.3)
        self.assertEqual(h.engine.speed, 1.25)
        h.engine.set_speed(9)
        self.assertEqual(h.engine.speed, 2.0)

    def test_speed_label_and_rate_string(self):
        self.assertEqual(E.speed_label(0.95), "0.95×")
        self.assertEqual(E.speed_label(1.0), "1.0×")
        self.assertEqual(E.speed_label(0.5), "0.5×")
        self.assertEqual(E.rate_string(0.95), "-5%")
        self.assertEqual(E.rate_string(1.0), "+0%")
        self.assertEqual(E.rate_string(1.15), "+15%")
        self.assertEqual(E.rate_string(2.0), "+100%")

    def test_commit_equal_to_session_rate_does_nothing(self):
        h = H()
        a, _ = h.play()
        before = len(h.synth.requests)
        h.engine.set_speed(1.25)
        h.engine.set_speed(0.95)
        h.advance(400)
        self.assertEqual(len(h.synth.requests), before)
        self.assertFalse(h.engine.speed_pending)
        self.assertEqual(h.speeds[-1], (0.95, ""))
        self.assertEqual(h.cmds("set_cut"), [])

    def test_ready_commit_applies_to_next_reading(self):
        h = H()
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertEqual(h.speeds[-1], (1.25, ""))
        h.begin()
        self.assertEqual(h.req(0)["rate"], 1.25)

    def test_new_reading_commits_a_pending_change_first(self):
        h = H()
        h.engine.set_speed(1.5)
        h.begin()
        self.assertEqual(h.req(0)["rate"], 1.5)
        self.assertEqual(self.speeds_set(h), [1.5])

    def test_preparing_commit_replans_from_start(self):
        h = H()
        h.begin()
        gen = h.req(0)["gen"]
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertEqual(h.state, E.PREPARING)
        self.assertGreater(h.synth.cancelled_before[-1], gen)
        new = [r for r in h.synth.requests if r["gen"] > gen]
        self.assertEqual(new[0]["rate"], 1.25)
        self.assertTrue(new[0]["text"].startswith(sentence(0)))
        self.assertLessEqual(len(new[0]["text"]), 250)
        self.assertTrue(h.engine.speed_pending)
        h.ready(new[0]["chunk_id"])
        h.started(new[0]["chunk_id"])
        self.assertFalse(h.engine.speed_pending)

    def test_reading_mid_chunk_sets_cut_and_replans_from_next_sentence(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.tick(a, s_start(1) + 900)
        gen = h.req(0)["gen"]
        h.engine.set_speed(1.25)
        self.assertEqual(h.speeds[-1], (1.25, "1.25× from next sentence"))
        h.advance(400)
        self.assertEqual(h.cmds("set_cut"), [("set_cut", s_end(1) + 20)])
        self.assertIn(h.path(b), h.synth.deleted)
        self.assertNotIn(h.path(a), h.synth.deleted)
        new = [r for r in h.synth.requests if r["gen"] > gen]
        self.assertEqual(len(new), 2)
        self.assertTrue(new[0]["text"].startswith(sentence(2)))
        self.assertLessEqual(len(new[0]["text"]), 250)
        self.assertEqual(new[0]["rate"], 1.25)
        self.assertEqual(h.engine.pending_note, "1.25× from next sentence")
        self.assertEqual(h.info["pending_note"], "1.25× from next sentence")
        self.assertEqual(self.speeds_set(h), [1.25])

    def test_cut_gap_is_clamped(self):
        for gap, extra in ((50, 20), (100, 40), (500, 150)):
            h = H()
            h.begin(4)
            a = h.req(0)["chunk_id"]
            words = []
            t = 100.0
            for k, tok in enumerate(re.findall(r"[A-Za-z0-9]+", h.req(0)["text"])):
                if k and k % 10 == 0:
                    t += gap - 50  # the fake words leave 50 ms between words already
                words.append(Word(t, 250, tok))
                t += 300
            h.ready(a, words)
            h.started(a, 20000)
            h.tick(a, 1000)
            h.engine.set_speed(1.25)
            h.advance(400)
            end0 = words[9].offset_ms + 250
            self.assertEqual(h.cmds("set_cut"), [("set_cut", int(end0 + extra))], gap)

    def test_reading_last_sentence_of_chunk_lets_chunk_finish(self):
        h = H()
        a, b = h.play()
        h.tick(a, s_start(3) + 500)
        gen = h.req(0)["gen"]
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertEqual(h.cmds("set_cut"), [])
        new = [r for r in h.synth.requests if r["gen"] > gen]
        self.assertTrue(new[0]["text"].startswith(sentence(4)))
        self.assertEqual(new[0]["rate"], 1.25)

    def test_reading_last_sentence_applies_to_next_reading(self):
        h = H()
        a, _ = h.play(4)
        h.tick(a, s_start(3) + 500)
        before = len(h.synth.requests)
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertEqual(len(h.synth.requests), before)
        self.assertEqual(h.cmds("set_cut"), [])
        self.assertEqual(h.speeds[-1], (1.25, "1.25× from next reading"))
        self.assertEqual(self.speeds_set(h), [1.25])
        h.advance(3000)
        self.assertEqual(h.speeds[-1], (1.25, ""))
        self.assertFalse(h.engine.speed_pending)
        h.end(a)
        self.assertEqual(h.state, E.FINISHED)
        h.begin()
        self.assertEqual(h.req()["rate"], 1.25)

    def test_before_sentence_start_cuts_now(self):
        h = H()
        a, _ = h.play()
        h.engine.next()
        h.advance(150)
        pos = max(s_end(0), s_start(1) - 80) + 10
        h.tick(a, pos)
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertEqual(h.cmds("set_cut"), [("set_cut", pos)])
        self.assertTrue(h.req(-2)["text"].startswith(sentence(1)))

    def test_cut_end_plays_new_chunk_when_ready(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 900)
        h.engine.set_speed(1.25)
        h.advance(400)
        t = h.req(-2)["chunk_id"]
        h.ready(t)
        self.assertNotEqual(h.cmds("load")[-1][1], t)  # still playing the old-rate chunk
        h.end(a, "cut")
        self.assertEqual(h.cmds("load")[-1], ("load", t, h.path(t), 0, True, None))
        self.assertTrue(h.engine.speed_pending)
        h.started(t)
        self.assertFalse(h.engine.speed_pending)
        self.assertEqual(h.speeds[-1], (1.25, ""))
        self.assertEqual(h.info["sentence"], 2)

    def test_cut_end_waits_when_new_chunk_not_ready(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 900)
        h.engine.set_speed(1.25)
        h.advance(400)
        t = h.req(-2)["chunk_id"]
        h.end(a, "cut")
        self.assertEqual(h.state, E.WAITING)
        self.assertEqual(h.info["sentence"], 2)
        h.pump()
        self.assertIn(h.path(a), h.synth.deleted)  # old-rate chunk is dropped once it has played
        h.ready(t)
        self.assertEqual(h.cmds("load")[-1][1], t)

    def test_paused_commit_replans_from_current_sentence(self):
        h = H()
        a, b = h.play()
        h.tick(a, s_start(1) + 900)
        h.engine.toggle_pause()
        gen = h.req(0)["gen"]
        h.engine.set_speed(1.5)
        self.assertEqual(h.speeds[-1], (1.5, "1.5× when resumed"))
        h.advance(400)
        h.pump()
        self.assertIn(("stop_close",), h.player.cmds)
        self.assertIn(h.path(a), h.synth.deleted)
        self.assertEqual(h.state, E.PAUSED)
        new = [r for r in h.synth.requests if r["gen"] > gen]
        self.assertTrue(new[0]["text"].startswith(sentence(1)))
        self.assertEqual(new[0]["rate"], 1.5)
        self.assertEqual(h.info["pending_note"], "1.5× when resumed")
        self.assertEqual(h.info["sentence"], 1)
        h.ready(new[0]["chunk_id"])
        self.assertEqual(h.cmds("load")[-1][1:5], (new[0]["chunk_id"], h.path(new[0]["chunk_id"]), 0, False))
        h.engine.toggle_pause()
        self.assertEqual(h.player.cmds[-1], ("resume",))
        h.started(new[0]["chunk_id"])
        self.assertEqual(h.state, E.READING)
        self.assertFalse(h.engine.speed_pending)

    def test_waiting_commit_replans_from_target(self):
        h = H()
        a, b = h.play()
        h.end(a)
        self.assertEqual(h.state, E.WAITING)
        gen = h.reqs(b)[-1]["gen"]
        h.engine.set_speed(0.75)
        h.advance(400)
        self.assertEqual(h.state, E.WAITING)
        new = [r for r in h.synth.requests if r["gen"] > gen]
        self.assertTrue(new[0]["text"].startswith(sentence(4)))
        self.assertEqual(new[0]["rate"], 0.75)
        self.assertIn(h.path(a), h.synth.deleted)
        self.assertEqual(h.info["pending_note"], "0.75× from next sentence")

    def test_proportional_chunk_is_never_cut(self):
        h = H()
        h.begin()
        a = h.req(0)["chunk_id"]
        h.ready(a, words=[])
        h.started(a)
        h.tick(a, s_start(1) + 900)
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertEqual(h.cmds("set_cut"), [])
        self.assertTrue(h.req(-2)["text"].startswith(sentence(4)))

    def test_back_to_playing_chunk_rate_removes_cut(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 900)
        h.engine.set_speed(1.25)
        h.advance(400)
        h.engine.set_speed(0.95)
        h.advance(400)
        self.assertEqual(h.cmds("set_cut")[-1], ("set_cut", None))
        self.assertTrue(h.req()["text"].startswith(sentence(4)))
        self.assertEqual(h.req()["rate"], 0.95)
        self.assertFalse(h.engine.speed_pending)
        self.assertEqual(h.speeds[-1], (0.95, ""))
        h.end(a)
        self.assertEqual(h.info["sentence"], 4)

    def test_commit_flushes_pending_seek_first(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.engine.next()
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertEqual(h.cmds("seek"), [("seek", max(s_end(0), s_start(1) - 80), True)])

    def test_time_left_rescaled_after_change(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 900)
        h.advance(1000)
        h.tick(a, s_start(1) + 900)
        before = h.time_left[-1]
        h.engine.set_speed(1.9)  # snaps to 2.0
        h.advance(1400)
        h.tick(a, s_start(1) + 900)
        self.assertAlmostEqual(h.time_left[-1], before * 0.95 / 2.0, delta=1)

    def test_pending_note_is_cleared_when_stopped(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 900)
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertEqual(h.info["pending_note"], "1.25× from next sentence")
        h.engine.stop()
        self.assertEqual(h.state, E.STOPPED)
        self.assertEqual(h.info["pending_note"], "")
        self.assertFalse(h.info["speed_pending"])
        self.assertFalse(h.engine.speed_pending)
        self.assertEqual(h.speeds[-1], (1.25, ""))
        h.engine.prev()                  # reads on at the committed speed
        self.assertEqual(h.req()["rate"], 1.25)

    def test_pending_note_is_cleared_on_error(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 900)
        h.engine.set_speed(1.25)
        h.advance(400)
        h.player.failed.emit(a, "MCI error 263")
        self.assertEqual(h.state, E.ERROR_PLAYBACK)
        self.assertEqual(h.info["pending_note"], "")
        self.assertFalse(h.info["speed_pending"])

    def test_pending_note_paused_then_stopped_is_cleared(self):
        h = H()
        a, _ = h.play()
        h.engine.toggle_pause()
        h.engine.set_speed(1.5)
        h.advance(400)
        self.assertEqual(h.info["pending_note"], "1.5× when resumed")
        h.engine.stop()
        self.assertEqual(h.info["pending_note"], "")

    def test_change_still_uncommitted_at_stop_is_committed_after_400_ms(self):
        h = H()
        h.play()
        h.engine.set_speed(1.25)
        h.engine.stop()
        self.assertTrue(h.engine.speed_pending)  # the stepper still shows the change as pending
        h.advance(400)
        self.assertEqual(self.speeds_set(h), [1.25])
        self.assertFalse(h.engine.speed_pending)
        self.assertEqual(h.speeds[-1], (1.25, ""))

    def test_resting_commit_drops_old_rate_cache(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        h.engine.set_speed(1.25)
        h.advance(400)
        self.assertIn(h.path(a), h.synth.deleted)
        h.engine.prev()
        self.assertEqual(h.req()["rate"], 1.25)
        self.assertTrue(h.req()["text"].startswith(sentence(3)))


class ErrorTests(unittest.TestCase):
    def test_awaited_chunk_offline_error(self):
        h = H()
        h.begin()
        h.fail(h.req(0)["chunk_id"], "offline")
        self.assertEqual(h.state, E.ERROR_OFFLINE)
        self.assertEqual(h.beeps, ["error"])
        self.assertIn("error", h.notices)
        self.assertFalse(h.info["mid_session"])
        self.assertEqual(h.info["play_action"], "retry")
        self.assertTrue(h.info["can_stop"])
        self.assertFalse(h.info["can_seek"])
        self.assertEqual(h.info["label"], "Can't reach the voice service")
        self.assertFalse(h.media[-1])

    def test_service_error(self):
        h = H()
        h.begin()
        h.fail(h.req(0)["chunk_id"], "service")
        self.assertEqual(h.state, E.ERROR_SERVICE)

    def test_other_error_while_offline_is_offline(self):
        h = H()
        h.begin()
        h.engine.set_online(False)
        h.fail(h.req(0)["chunk_id"], "other")
        self.assertEqual(h.state, E.ERROR_OFFLINE)

    def test_prefetch_failure_retries_silently_once(self):
        h = H()
        a, b = h.play()
        h.fail(b, "service")
        self.assertEqual(h.state, E.READING)
        self.assertEqual(h.beeps, [])
        self.assertEqual(len(h.reqs(b)), 1)
        h.advance(1499)
        self.assertEqual(len(h.reqs(b)), 1)
        h.advance(1)
        self.assertEqual(len(h.reqs(b)), 2)
        h.ready(b)
        h.end(a)
        self.assertEqual(h.cmds("load")[-1][1], b)

    def test_prefetch_failing_twice_errors_when_reached(self):
        h = H()
        a, b = h.play()
        h.fail(b, "service")
        h.advance(1500)
        h.fail(b, "service")
        self.assertEqual(h.state, E.READING)
        h.end(a)
        self.assertEqual(h.state, E.ERROR_SERVICE)
        self.assertTrue(h.info["mid_session"])
        self.assertEqual(h.info["sentence"], 4)
        self.assertEqual(h.beeps, ["error"])

    def test_prefetch_failing_twice_while_waiting_errors_at_once(self):
        h = H()
        a, b = h.play()
        h.fail(b, "offline")
        h.end(a)
        self.assertEqual(h.state, E.WAITING)
        h.advance(1500)
        h.fail(b, "offline")
        self.assertEqual(h.state, E.ERROR_OFFLINE)
        self.assertTrue(h.info["mid_session"])

    def test_playback_error(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(2) + 100)
        h.player.failed.emit(a, "MCI error 263")
        self.assertEqual(h.state, E.ERROR_PLAYBACK)
        self.assertEqual(h.beeps, ["error"])
        self.assertIn(("stop_close",), h.player.cmds)
        self.assertEqual(h.info["sentence"], 2)

    def test_failed_chunk_file_is_deleted_without_waiting_for_ended(self):
        h = H()
        a, _ = h.play()
        h.player.loaded = None  # the real player closes the device itself and emits only failed
        h.player.failed.emit(a, "MCI error 263")
        h.press()
        h.copied(make_text(2))
        self.assertIn(h.path(a), h.synth.deleted)

    def test_stale_failures_are_ignored(self):
        h = H()
        a, _ = h.play()
        h.player.failed.emit(a + 100, "MCI error 263")
        h.synth.chunkFailed.emit(0, a, "service", "old")
        self.assertEqual(h.state, E.READING)

    def test_retry_resumes_from_first_unplayed_sentence(self):
        h = H()
        a, b = h.play()
        h.fail(b, "service")
        h.advance(1500)
        h.fail(b, "service")
        h.end(a)
        gen = h.reqs(b)[-1]["gen"]
        h.engine.retry()
        self.assertEqual(h.state, E.WAITING)
        new = [r for r in h.synth.requests if r["gen"] > gen]
        self.assertTrue(new[0]["text"].startswith(sentence(4)))
        self.assertLessEqual(len(new[0]["text"]), 250)

    def test_retry_before_any_audio_is_preparing_and_uses_current_speed(self):
        h = H()
        h.begin()
        h.fail(h.req(0)["chunk_id"], "offline")
        h.engine.set_speed(1.25)
        h.advance(400)
        h.engine.toggle_pause()  # Play button / pause hotkey in an error state = Try again
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual(h.req(-2)["rate"], 1.25)
        self.assertTrue(h.req(-2)["text"].startswith(sentence(0)))

    def test_back_online_variant(self):
        h = H()
        h.begin()
        h.engine.set_online(False)
        h.fail(h.req(0)["chunk_id"], "offline")
        self.assertFalse(h.info["back_online"])
        h.engine.set_online(True)
        self.assertEqual(h.state, E.ERROR_OFFLINE)
        self.assertTrue(h.info["back_online"])

    def test_no_automatic_retry_from_error(self):
        h = H()
        h.begin()
        h.fail(h.req(0)["chunk_id"], "service")
        count = len(h.synth.requests)
        h.advance(60_000)
        self.assertEqual(len(h.synth.requests), count)
        self.assertEqual(h.state, E.ERROR_SERVICE)

    def test_late_chunk_after_error_is_discarded(self):
        h = H()
        h.begin()
        a, b = h.req(0)["chunk_id"], h.req(1)["chunk_id"]
        h.fail(a, "service")
        h.ready(b)
        self.assertIn(h.path(b), h.synth.deleted)
        self.assertEqual(h.cmds("load"), [])


class PrivacyTests(unittest.TestCase):
    def test_purge_ten_minutes_after_finished(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.end(a)
        h.started(b)
        h.end(b)
        h.advance(10 * 60 * 1000 - 1)
        self.assertIsNotNone(h.sessions[-1])
        h.advance(2)
        self.assertIsNone(h.sessions[-1])
        self.assertIn(h.path(a), h.synth.deleted)
        self.assertIn(h.path(b), h.synth.deleted)
        self.assertEqual(h.state, E.READY)
        self.assertFalse(h.info["has_text"])
        self.assertIsNone(h.engine.document)

    def test_purge_after_stop_and_after_error(self):
        h = H()
        h.play()
        h.engine.stop()
        h.advance(10 * 60 * 1000)
        self.assertIsNone(h.sessions[-1])
        self.assertEqual(h.state, E.READY)
        h.begin()
        h.fail(h.req(-2)["chunk_id"], "service")  # the new session's first (awaited) chunk
        self.assertEqual(h.state, E.ERROR_SERVICE)
        h.advance(10 * 60 * 1000)
        self.assertIsNone(h.sessions[-1])
        self.assertEqual(h.state, E.READY)

    def test_files_still_open_in_the_player_are_deleted_only_once_closed(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 900)
        h.engine.toggle_pause()
        h.engine.set_speed(1.5)
        h.advance(400)                  # paused commit: the player is told to close chunk a
        self.assertIn(("stop_close",), h.player.cmds)
        self.assertNotIn(h.path(a), h.synth.deleted)  # MCI may still hold it open
        h.pump()                        # ended(a, "stopped")
        self.assertIn(h.path(a), h.synth.deleted)

    def test_cache_is_capped_keeping_the_chunks_nearest_the_position(self):
        with mock.patch.object(E, "MAX_CACHED_CHUNKS", 1):
            h = H()
            h.begin(100)
            a, b = h.req(0)["chunk_id"], h.req(1)["chunk_id"]
            h.ready(a)
            h.started(a)
            c = h.req(2)["chunk_id"]
            h.ready(b)
            h.ready(c)                  # two ready chunks besides the playing one: one too many
            self.assertIn(h.path(c), h.synth.deleted)
            self.assertNotIn(h.path(b), h.synth.deleted)
            self.assertNotIn(h.path(a), h.synth.deleted)

    def test_new_session_purges_previous_cache(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.engine.stop()
        h.pump()
        h.press()
        h.copied(make_text(2))
        self.assertIn(h.path(a), h.synth.deleted)
        self.assertIn(h.path(b), h.synth.deleted)

    def test_restart_cancels_purge(self):
        h = H()
        a, _ = h.play(4)
        h.engine.stop()
        h.pump()  # the player confirms the close at once
        h.advance(5 * 60 * 1000)
        h.engine.prev()
        h.started(a)
        h.advance(6 * 60 * 1000)
        self.assertIsNotNone(h.sessions[-1])
        self.assertEqual(h.state, E.READING)

    def test_paused_session_is_not_purged(self):
        h = H()
        h.play()
        h.engine.toggle_pause()
        h.advance(30 * 60 * 1000)
        self.assertIsNotNone(h.sessions[-1])

    def test_logs_never_contain_text(self):
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("saylight")
        handler, old_level = Grab(level=logging.DEBUG), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            h = H()
            a, b = h.play(ready_all=True)
            h.tick(a, 4000)
            h.engine.set_speed(1.25)
            h.advance(400)
            h.fail(h.req()["chunk_id"], "service")
            h.engine.stop()
            h.engine.read_selection_click(1)
            h.copied("Secret words here.", token=h.input.copy_requests[-1][0])
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertTrue(records)
        for msg in records:
            for needle in ("alpha", "Sentence 0", "Secret", "theta"):
                self.assertNotIn(needle, msg)


class VolumeTests(unittest.TestCase):
    def test_set_volume_clamps_persists_and_emits(self):
        h = H()
        h.engine.set_volume(150)
        self.assertEqual(h.volumes[-1], 100)
        h.engine.set_volume(-5)
        self.assertEqual(h.volumes[-1], 0)
        self.assertEqual(h.settings.values["volume"], 0)
        self.assertEqual(h.engine.volume, 0)

    def test_volume_is_throttled_to_30_hz(self):
        h = H()
        h.advance(1000)
        h.engine.set_volume(50)
        self.assertEqual(h.cmds("set_volume")[-1], ("set_volume", 50))
        h.advance(10)
        h.engine.set_volume(60)
        h.engine.set_volume(70)
        self.assertEqual(h.cmds("set_volume")[-1], ("set_volume", 50))
        h.advance(25)
        self.assertEqual(h.cmds("set_volume")[-1], ("set_volume", 70))
        self.assertEqual(h.volumes[-2:], [60, 70])


class SettingsFollowTests(unittest.TestCase):
    def test_media_keys_setting_turned_off_mid_reading(self):
        h = H()
        h.play()
        self.assertTrue(h.media[-1])
        h.settings.set("media_keys", False)
        self.assertFalse(h.media[-1])
        self.assertFalse(h.input.media[-1])

    def test_speed_changed_elsewhere_goes_through_the_commit(self):
        h = H()
        h.settings.set("speed", 1.5)
        self.assertEqual(h.engine.speed, 1.5)
        h.advance(400)
        h.begin()
        self.assertEqual(h.req()["rate"], 1.5)

    def test_volume_changed_elsewhere(self):
        h = H()
        h.settings.set("volume", 40)
        self.assertEqual(h.volumes[-1], 40)
        h.advance(40)  # the start-up volume went out at this same instant: 30 Hz throttle
        self.assertEqual(h.cmds("set_volume")[-1], ("set_volume", 40))

    def test_new_reading_with_pending_speed_never_shows_a_textless_session(self):
        h = H()
        h.play()
        h.engine.set_speed(1.5)
        h.press()
        h.copied(make_text(3))
        self.assertEqual(h.state, E.PREPARING)
        self.assertEqual(h.req()["rate"], 1.5)
        for state, info in h.states:
            if state in E.SESSION:
                self.assertTrue(info["has_text"], state)

    def test_purge_resets_position_and_time_left(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        h.advance(10 * 60 * 1000)
        self.assertEqual(h.positions[-1], (-1, -1, -1, 0.0, 0.0, False))
        self.assertEqual(h.time_left[-1], -1)


class SeekRaceTests(unittest.TestCase):
    """Player signals are queued: some were sent before the player saw the engine's latest command."""

    def test_tick_from_before_the_seek_does_not_pull_the_counter_back(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(2) + 500)
        h.engine.prev()                  # under 1.5 s in: back to sentence 1
        h.advance(150)
        offset = max(s_end(0), s_start(1) - 80)
        self.assertEqual(h.cmds("seek")[-1], ("seek", offset, True))
        n_states, n_positions = len(h.states), len(h.positions)
        h.tick(a, s_start(2) + 550)      # measured by the player before it applied the seek
        self.assertEqual(h.info["sentence"], 1)
        self.assertEqual(len(h.states), n_states)
        self.assertEqual(len(h.positions), n_positions)
        h.tick(a, offset + 40)           # the seek has been applied
        self.assertEqual(h.positions[-1][0], 1)
        h.tick(a, s_start(1) + 700)
        self.assertEqual(h.info["sentence"], 1)
        self.assertEqual(sentence(1)[h.positions[-1][1]:h.positions[-1][2]], "alpha")

    def test_seek_lost_to_the_end_of_the_last_chunk_does_not_finish(self):
        h = H()
        a, _ = h.play(4)
        h.tick(a, s_start(3) + 500)
        h.engine.prev()                  # back to sentence 2
        h.advance(150)
        h.end(a)                         # the device ended before the seek reached the player
        self.assertNotIn("finished", h.notices)
        self.assertEqual(h.cmds("load")[-1], ("load", a, h.path(a), max(s_end(1), s_start(2) - 80), True, None))
        self.assertEqual(h.info["sentence"], 2)
        h.started(a)
        self.assertEqual(h.state, E.READING)

    def test_seek_lost_to_a_chunk_end_does_not_skip_to_the_next_chunk(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.tick(a, s_start(3) + 2000)
        h.engine.prev()                  # restart sentence 3
        h.advance(150)
        h.end(a)
        self.assertEqual(h.cmds("load")[-1][1:4], (a, h.path(a), max(s_end(2), s_start(3) - 80)))
        self.assertNotIn(b, [c[1] for c in h.cmds("load")])
        self.assertEqual(h.info["sentence"], 3)

    def test_end_after_a_confirmed_seek_moves_on(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.tick(a, s_start(3) + 500)
        h.engine.prev()
        h.advance(150)
        h.tick(a, max(s_end(1), s_start(2) - 80) + 30)   # confirms the seek
        h.tick(a, s_end(3))
        h.end(a)
        self.assertEqual(h.cmds("load")[-1][1], b)
        self.assertEqual(h.info["sentence"], 4)

    def test_ticks_are_trusted_again_when_a_seek_is_never_confirmed(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(2) + 500)
        h.engine.prev()
        h.advance(150)
        h.advance(999)
        h.tick(a, s_start(2) + 500)
        self.assertEqual(h.info["sentence"], 1)  # still ignored
        h.advance(2)
        h.tick(a, s_start(2) + 500)
        self.assertEqual(h.info["sentence"], 2)  # after 1 s the player's report wins

    def test_signals_from_an_older_load_of_the_same_chunk_are_ignored(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.engine.jump_to(6)
        h.advance(150)                   # loads chunk b at sentence 6; not started yet
        h.engine.next()
        h.advance(150)                   # b loaded again at sentence 7 before its first load started
        loads = [c for c in h.cmds("load") if c[1] == b]
        self.assertEqual(len(loads), 2)
        h.started(b)                     # from the first load, which the player is about to close
        h.tick(b, s_start(2) + 100)
        self.assertEqual(h.state, E.WAITING)
        self.assertEqual(h.info["sentence"], 7)
        h.pump()                         # the player closes the older load
        h.started(b)
        self.assertEqual(h.state, E.READING)
        h.tick(b, s_start(3) + 100)
        self.assertEqual(h.info["sentence"], 7)


# --- Typed text, Clear and Undo ----------------------------------------------------------------

def typed_text(n: int) -> str:
    return " ".join(f"Typed {i:03d} one two three four five six." for i in range(n))


def _waiting(h: H) -> None:
    a, _ = h.play()
    h.end(a)


def _stopped(h: H) -> None:
    a, _ = h.play()
    h.tick(a, s_start(2) + 100)
    h.engine.stop()


def _finished(h: H) -> None:
    a, _ = h.play(4)
    h.end(a)


def _empty_click(h: H) -> None:
    h.engine.read_selection_click(1)
    h.copied("", "empty", token=h.input.copy_requests[-1][0])


def _overlay_over_stopped(h: H) -> None:
    _stopped(h)
    _empty_click(h)


# name -> (setup, internal state it leaves)
SCENARIOS = {
    "ready": (lambda h: None, E.READY),
    "offline": (lambda h: h.engine.set_online(False), E.READY_OFFLINE),
    "shortcut_unavailable": (lambda h: h.input.registered.emit(E.HK_READ, False), E.SHORTCUT_UNAVAILABLE),
    "copying_hotkey": (lambda h: h.press(), E.COPYING),
    "copying_click": (lambda h: h.engine.read_selection_click(1), E.COPYING),
    "preparing": (lambda h: h.begin(), E.PREPARING),
    "reading": (lambda h: h.play(), E.READING),
    "waiting": (_waiting, E.WAITING),
    "paused": (lambda h: (h.play(), h.engine.toggle_pause()), E.PAUSED),
    "finished": (_finished, E.FINISHED),
    "stopped": (_stopped, E.STOPPED),
    "error_offline": (lambda h: (h.begin(), h.fail(h.req(0)["chunk_id"], "offline")), E.ERROR_OFFLINE),
    "error_service": (lambda h: (h.begin(), h.fail(h.req(0)["chunk_id"], "service")), E.ERROR_SERVICE),
    "error_playback": (lambda h: h.player.failed.emit(h.play()[0], "MCI error 263"), E.ERROR_PLAYBACK),
    "overlay_over_ready": (_empty_click, E.NOTHING_SELECTED),
    "overlay_over_stopped": (_overlay_over_stopped, E.NOTHING_SELECTED),
}


def reach(test: unittest.TestCase, name: str, h: H | None = None) -> H:
    """A harness brought to one of SCENARIOS."""
    h = h or H()
    setup, state = SCENARIOS[name]
    setup(h)
    test.assertEqual(h.state, state, name)
    return h


def counts(h: H) -> tuple:
    """How much every recorder (and fake worker) has seen so far."""
    return (len(h.states), len(h.sessions), len(h.positions), len(h.time_left), len(h.speeds), len(h.notices),
            len(h.media), len(h.beeps), h.toggles, len(h.synth.requests), len(h.synth.cancelled_before),
            len(h.synth.deleted), len(h.player.cmds), len(h.settings.sets), len(h.input.copy_requests))


class TypedTextTests(unittest.TestCase):
    def test_reads_from_every_state(self):
        for name in SCENARIOS:
            with self.subTest(name):
                h = reach(self, name)
                notices, sessions, requests = len(h.notices), len(h.sessions), len(h.synth.requests)
                self.assertTrue(h.engine.read_text(typed_text(6)))
                doc = h.engine.document
                self.assertEqual(len(doc.sentences), 6)
                self.assertEqual(h.sessions[sessions:], [doc])  # never a textless moment in between
                self.assertEqual((h.state, h.shown), (E.PREPARING, E.PREPARING))
                self.assertEqual(h.info["sentence"], 0)
                self.assertTrue(h.info["has_text"])
                self.assertNotIn("stopped", h.notices[notices:])
                self.assertTrue(h.synth.requests[requests]["text"].startswith("Typed 000"))
                self.assertTrue(all(r["gen"] == h.engine._gen for r in h.synth.requests[requests:]))

    def test_a_copy_in_flight_goes_stale(self):
        h = reach(self, "copying_hotkey")
        h.engine.read_text(typed_text(6))
        h.copied(make_text(3))
        self.assertEqual(len(h.engine.document.sentences), 6)
        h = reach(self, "copying_click")
        h.engine.read_text(typed_text(6))
        h.copied(make_text(3), token=h.input.copy_requests[-1][0])
        self.assertEqual(len(h.engine.document.sentences), 6)
        self.assertEqual(h.state, E.PREPARING)
        h.advance(E.COPY_WATCHDOG_MS + 100)  # the cancelled copy's timers are gone too
        self.assertEqual(h.state, E.PREPARING)
        self.assertNotIn(E.COPYING, h.shown_states())

    def test_from_a_visible_copy_it_goes_straight_to_preparing(self):
        h = reach(self, "finished")
        h.press()
        h.advance(E.COPY_SHOW_MS + 1)
        self.assertEqual(h.shown, E.COPYING)
        states = len(h.states)
        h.engine.read_text(typed_text(6))
        self.assertEqual(h.shown_states()[states:], [E.PREPARING])  # the old Finished never flashes back

    def test_pending_presses_are_ignored(self):
        h = H()
        h.play()
        h.press()                                  # optimistic pause; its copy result is still to come
        self.assertTrue(h.engine.read_text(typed_text(6)))
        h.copied(make_text(3))                     # would otherwise replace the typed text
        self.assertEqual(len(h.engine.document.sentences), 6)
        self.assertEqual(h.state, E.PREPARING)

        h = H()
        h.play()
        h.engine.toggle_pause()
        h.press()                                  # pressed while paused: an empty result would resume
        h.engine.read_text(typed_text(6))
        h.engine.toggle_pause()                    # the user pauses the typed reading
        h.copied("", "empty")
        self.assertEqual(h.state, E.PAUSED)
        self.assertEqual(h.toggles, 0)

    def test_old_audio_is_dropped(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.engine.read_text(typed_text(6))
        h.pump()
        self.assertIn(("stop_close",), h.player.cmds)
        self.assertIn(h.path(a), h.synth.deleted)
        self.assertIn(h.path(b), h.synth.deleted)

    def test_blank_or_unspeakable_text_changes_nothing(self):
        for name in ("ready", "reading", "paused", "finished"):
            with self.subTest(name):
                h = reach(self, name)
                doc, before = h.engine.document, counts(h)
                for text in ("", "   \n\t ", "---", "* * *", "​"):
                    self.assertFalse(h.engine.read_text(text), repr(text))
                self.assertEqual(counts(h), before)
                self.assertIs(h.engine.document, doc)
                self.assertEqual(h.state, SCENARIOS[name][1])

    def test_reading_goes_on_after_unspeakable_text(self):
        h = H()
        a, _ = h.play()
        self.assertFalse(h.engine.read_text("---"))
        h.tick(a, s_start(1) + 100)
        self.assertEqual((h.state, h.info["sentence"]), (E.READING, 1))

    def test_text_over_the_limit_is_cut(self):
        self.assertEqual(E.MAX_TEXT_CHARS, 100_000)
        h = H()
        text = make_text(2000)
        self.assertGreater(len(text), E.MAX_TEXT_CHARS)
        real, seen = E.Document.from_raw, []

        def spy(raw, **kw):
            seen.append(len(raw))
            return real(raw, **kw)

        with mock.patch.object(E.Document, "from_raw", side_effect=spy):
            self.assertTrue(h.engine.read_text(text))
            self.assertTrue(h.engine.read_text(text[:E.MAX_TEXT_CHARS]))
        self.assertEqual(seen, [E.MAX_TEXT_CHARS, E.MAX_TEXT_CHARS])
        self.assertLessEqual(len(h.engine.document.text), E.MAX_TEXT_CHARS)

    def test_a_pending_speed_is_used_for_the_typed_reading(self):
        h = H()
        h.engine.set_speed(1.25)
        h.engine.read_text(typed_text(3))
        self.assertEqual(h.req()["rate"], 1.25)
        self.assertEqual(h.settings.values["speed"], 1.25)

    def test_the_compose_shortcut_is_not_the_engines(self):
        from saylight.input_thread import HK_COMPOSE
        h = reach(self, "reading")
        before = counts(h)
        h.press(HK_COMPOSE)
        self.assertEqual(counts(h), before)

    def test_after_shutdown(self):
        h = H()
        h.engine.shutdown()
        self.assertFalse(h.engine.read_text(typed_text(3)))
        self.assertIsNone(h.engine.clear())

    def test_logs_counts_never_text(self):
        with self.assertLogs("saylight.engine", "INFO") as logs:
            h = H()
            h.engine.read_text("Private words typed here.")
            h.engine.read_text("---")
            token = h.engine.clear()
            h.engine.restore(token)
        output = "\n".join(logs.output)
        self.assertIn("Reading typed text: 25 characters", output)
        self.assertIn("Cleared (1 sentences)", output)
        self.assertIn("Clear undone", output)
        for needle in ("Private", "words typed", "here."):
            self.assertNotIn(needle, output)


class ClearTests(unittest.TestCase):
    def test_from_every_state(self):
        idle = {"offline": E.READY_OFFLINE, "shortcut_unavailable": E.SHORTCUT_UNAVAILABLE}
        for name in SCENARIOS:
            with self.subTest(name):
                h = reach(self, name)
                had_text, notices, beeps = h.engine.document is not None, len(h.notices), len(h.beeps)
                token = h.engine.clear()
                expected = idle.get(name, E.READY)
                self.assertEqual((h.state, h.shown), (expected, expected))
                self.assertIsNone(h.engine.document)
                self.assertEqual(token is not None, had_text)
                self.assertFalse(h.info["has_text"])
                self.assertEqual((h.info["can_type"], h.info["can_clear"]), (True, False))
                self.assertEqual(h.info["sentence"], -1)
                self.assertFalse(h.engine._purge_timer.is_active())
                self.assertFalse(h.engine._media_on)
                self.assertEqual(h.notices[notices:], [])
                self.assertEqual(len(h.beeps), beeps)
                if had_text:
                    self.assertIsNone(h.sessions[-1])
                    self.assertEqual(h.positions[-1][0], -1)
                    self.assertEqual(h.time_left[-1], -1)
                before = counts(h)
                h.advance(E.PURGE_MS + 60_000)  # nothing is left running
                self.assertEqual(counts(h), before)

    def test_signals(self):
        h = H()
        h.play()
        h.engine.set_speed(1.25)  # a change still waiting to be committed, with its note
        self.assertTrue(h.engine.pending_note)
        events = []
        e = h.engine
        e.sessionChanged.connect(lambda doc: events.append(("session", doc)))
        e.position.connect(lambda s, *rest: events.append(("position", s)))
        e.timeLeft.connect(lambda v: events.append(("time_left", v)))
        e.speedChanged.connect(lambda v, note: events.append(("speed", v, note)))
        e.stateChanged.connect(lambda s, info: events.append(("state", s)))
        e.mediaKeysWanted.connect(lambda on: events.append(("media", on)))
        e.notice.connect(lambda kind: events.append(("notice", kind)))
        e.clear()
        self.assertEqual(sorted(events, key=str), sorted([("session", None), ("position", -1), ("time_left", -1),
                                                          ("speed", 1.25, ""), ("state", E.READY),
                                                          ("media", False)], key=str))
        self.assertLess(events.index(("session", None)), events.index(("state", E.READY)))
        self.assertEqual(h.input.media[-1], False)

    def test_deletes_the_audio_and_stops_playback_and_synthesis(self):
        h = H()
        a, b = h.play(ready_all=True)
        gen = h.engine._gen
        h.engine.clear()
        h.pump()
        self.assertIn(("stop_close",), h.player.cmds)
        self.assertGreater(h.synth.cancelled_before[-1], gen)
        self.assertIn(h.path(a), h.synth.deleted)
        self.assertIn(h.path(b), h.synth.deleted)
        h.ready(h.req()["chunk_id"])  # a late result of the old generation is discarded
        self.assertIsNone(h.player.loaded)

    def test_cancels_a_pending_seek(self):
        h = H()
        h.play(ready_all=True)
        h.engine.next()
        self.assertTrue(h.engine._seek_timer.is_active())
        h.engine.clear()
        self.assertFalse(h.engine._seek_timer.is_active())
        cmds = len(h.player.cmds)
        h.advance(1000)
        self.assertEqual(h.player.cmds[cmds:], [])

    def test_commits_a_pending_speed_and_drops_its_note(self):
        h = H()
        h.play()
        h.engine.set_speed(1.25)
        h.engine.clear()
        self.assertEqual(h.settings.values["speed"], 1.25)
        self.assertEqual(h.speeds[-1], (1.25, ""))
        self.assertEqual((h.engine.speed_pending, h.engine.pending_note), (False, ""))
        self.assertEqual((h.info["speed"], h.info["speed_pending"], h.info["pending_note"]), (1.25, False, ""))
        sets = len(h.settings.sets)
        h.advance(1000)
        self.assertEqual(len(h.settings.sets), sets)  # not committed a second time

    def test_drops_a_committed_from_next_reading_note(self):
        h = H()
        a, _ = h.play(4)
        h.tick(a, s_start(3) + 100)
        h.engine.set_speed(1.25)
        h.advance(E.SPEED_COMMIT_MS)
        self.assertEqual(h.engine.pending_note, "1.25× from next reading")
        h.engine.clear()
        self.assertEqual(h.engine.pending_note, "")
        self.assertEqual(h.speeds[-1], (1.25, ""))

    def test_ends_in_the_current_idle_state(self):
        h = H()
        h.play()
        h.engine.set_online(False)
        h.engine.clear()
        self.assertEqual(h.shown, E.READY_OFFLINE)
        h = H()
        h.play()
        h.input.registered.emit(E.HK_READ, False)
        h.engine.clear()
        self.assertEqual(h.shown, E.SHORTCUT_UNAVAILABLE)

    def test_with_no_text_it_is_a_no_op(self):
        for name in ("ready", "offline", "shortcut_unavailable"):
            with self.subTest(name):
                h = reach(self, name)
                before = counts(h)
                self.assertIsNone(h.engine.clear())
                self.assertEqual(counts(h), before)
        h = H()
        h.engine.set_speed(1.25)  # in Ready: committed by its own timer, not by Clear
        before = counts(h)
        self.assertIsNone(h.engine.clear())
        self.assertEqual(counts(h), before)
        h.advance(E.SPEED_COMMIT_MS)
        self.assertEqual(h.settings.values["speed"], 1.25)

    def test_clears_the_overlay_over_ready(self):
        h = reach(self, "overlay_over_ready")
        states = len(h.states)
        self.assertIsNone(h.engine.clear())
        self.assertEqual(h.shown_states()[states:], [E.READY])
        h.advance(E.NOTHING_SELECTED_MS)
        self.assertEqual(h.shown_states()[states:], [E.READY])

    def test_cancels_a_copy(self):
        h = reach(self, "copying_hotkey")
        self.assertIsNone(h.engine.clear())
        h.advance(E.COPY_SHOW_MS + 50)
        h.copied(make_text(3))
        self.assertEqual((h.state, h.toggles), (E.READY, 0))
        self.assertNotIn(E.COPYING, h.shown_states())
        self.assertIsNone(h.engine.document)

        h = reach(self, "copying_click")
        h.engine.clear()
        h.copied(make_text(3), token=h.input.copy_requests[-1][0])
        self.assertEqual(h.state, E.READY)
        self.assertIsNone(h.engine.document)
        h.advance(E.COPY_WATCHDOG_MS + 100)
        self.assertEqual(h.state, E.READY)

        h = reach(self, "finished")
        h.press()
        h.advance(E.COPY_SHOW_MS + 1)
        self.assertEqual(h.shown, E.COPYING)
        states = len(h.states)
        token = h.engine.clear()
        self.assertTrue(token.finished)  # the text copied over was Finished
        self.assertEqual(h.shown_states()[states:], [E.READY])  # the old Finished never flashes back
        h.copied(make_text(3))
        self.assertEqual(h.state, E.READY)
        self.assertIsNone(h.engine.document)

    def test_token(self):
        for name, sentence, finished in (("finished", 3, True), ("stopped", 2, False), ("reading", 1, False),
                                         ("paused", 1, False), ("error_service", 0, False),
                                         ("overlay_over_stopped", 2, False)):
            with self.subTest(name):
                h = H()
                if name in ("reading", "paused"):
                    a, _ = h.play()
                    h.tick(a, s_start(1) + 100)
                    if name == "paused":
                        h.engine.toggle_pause()
                else:
                    reach(self, name, h)
                doc = h.engine.document
                token = h.engine.clear()
                self.assertIsInstance(token, E.ClearedReading)
                self.assertEqual((token.doc, token.sentence, token.finished), (doc, sentence, finished))
        h = reach(self, "overlay_over_ready")
        self.assertIsNone(h.engine.clear())

    def test_token_is_frozen(self):
        h = reach(self, "stopped")
        token = h.engine.clear()
        with self.assertRaises(AttributeError):
            token.sentence = 0


class RestoreTests(unittest.TestCase):
    def test_restores_stopped_at_the_sentence(self):
        h = reach(self, "stopped")
        doc = h.engine.document
        token = h.engine.clear()
        events = []
        h.engine.sessionChanged.connect(lambda d: events.append("session"))
        h.engine.stateChanged.connect(lambda s, i: events.append(s))
        h.engine.position.connect(lambda *p: events.append("position"))
        h.engine.timeLeft.connect(lambda v: events.append("time_left"))
        self.assertTrue(h.engine.restore(token))
        self.assertIs(h.engine.document, doc)
        self.assertIs(h.sessions[-1], doc)
        self.assertEqual((h.state, h.shown), (E.STOPPED, E.STOPPED))
        self.assertEqual(h.info["sentence"], 2)
        self.assertEqual(h.info["prev_action"], "read_from")
        self.assertTrue(h.info["can_clear"])
        self.assertEqual(h.positions[-1][0], 2)
        self.assertGreaterEqual(h.time_left[-1], 0)
        self.assertEqual(events, ["session", E.STOPPED, "position", "time_left"])
        self.assertTrue(h.engine._purge_timer.is_active())
        self.assertFalse(h.engine._media_on)

    def test_restores_finished(self):
        h = reach(self, "finished")
        token = h.engine.clear()
        self.assertTrue(h.engine.restore(token))
        self.assertEqual((h.state, h.info["sentence"]), (E.FINISHED, 3))
        self.assertEqual(h.positions[-1][3], 1.0)
        self.assertEqual(h.info["prev_action"], "read_last_again")
        self.assertEqual(h.time_left[-1], 0)

    def test_a_reading_comes_back_stopped(self):
        h = H()
        a, _ = h.play()
        h.tick(a, s_start(1) + 100)
        token = h.engine.clear()
        self.assertTrue(h.engine.restore(token))
        self.assertEqual((h.state, h.info["sentence"]), (E.STOPPED, 1))

    def test_the_restored_text_is_purged_after_ten_minutes(self):
        h = reach(self, "stopped")
        h.engine.restore(h.engine.clear())
        h.advance(E.PURGE_MS - 1)
        self.assertIsNotNone(h.engine.document)
        h.advance(2)
        self.assertIsNone(h.engine.document)
        self.assertEqual(h.state, E.READY)

    def test_refused(self):
        h = reach(self, "stopped")
        token = h.engine.clear()
        self.assertFalse(h.engine.restore(None))
        h.begin()                                   # another text is loaded
        before = counts(h)
        self.assertFalse(h.engine.restore(token))
        self.assertEqual(counts(h), before)
        self.assertEqual(len(h.engine.document.sentences), 12)
        self.assertIsNot(h.engine.document, token.doc)

        h = reach(self, "stopped")
        token = h.engine.clear()
        h.press()                                   # copying
        before = counts(h)
        self.assertFalse(h.engine.restore(token))
        self.assertEqual(counts(h), before)

        h = reach(self, "stopped")
        token = h.engine.clear()
        self.assertTrue(h.engine.restore(token))
        self.assertFalse(h.engine.restore(token))   # already back
        h.engine.shutdown()
        self.assertFalse(h.engine.restore(token))

    def test_over_the_nothing_selected_overlay(self):
        h = reach(self, "stopped")
        token = h.engine.clear()
        _empty_click(h)
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        self.assertTrue(h.engine.restore(token))
        self.assertEqual(h.shown, E.STOPPED)
        h.advance(E.NOTHING_SELECTED_MS)
        self.assertEqual(h.shown, E.STOPPED)

    def test_reading_again_synthesises_from_the_stored_sentence(self):
        h = reach(self, "stopped")
        requests = len(h.synth.requests)
        h.engine.restore(h.engine.clear())
        self.assertEqual(len(h.synth.requests), requests)  # no audio comes back with the text
        h.engine.prev()                                     # "Read from here"
        self.assertEqual(h.state, E.PREPARING)
        first = h.req(requests)["chunk_id"]
        self.assertEqual(h.first_sentence(first), 2)
        h.ready(first)
        h.started(first)
        self.assertEqual((h.state, h.info["sentence"]), (E.READING, 2))
        self.assertTrue(h.engine._media_on)


class PauseResumeApiTests(unittest.TestCase):
    def test_pause_per_state(self):
        for name in SCENARIOS:
            with self.subTest(name):
                h = reach(self, name)
                expected = name in ("preparing", "reading", "waiting")
                self.assertIs(h.engine.pause(), expected)
                self.assertEqual(h.state, E.PAUSED if expected else SCENARIOS[name][1])

    def test_resume_per_state(self):
        for name in SCENARIOS:
            with self.subTest(name):
                h = reach(self, name)
                expected = name == "paused"
                self.assertIs(h.engine.resume(), expected)
                self.assertEqual(h.state, E.READING if expected else SCENARIOS[name][1])

    def test_pause_never_resumes_and_resume_never_pauses(self):
        h = H()
        h.play()
        self.assertFalse(h.engine.resume())
        self.assertEqual(h.state, E.READING)
        self.assertTrue(h.engine.pause())
        self.assertEqual(h.cmds("pause"), [("pause",)])
        self.assertFalse(h.engine.pause())
        self.assertEqual(h.state, E.PAUSED)
        self.assertTrue(h.engine.resume())
        self.assertEqual(h.cmds("resume"), [("resume",)])
        self.assertEqual(h.state, E.READING)
        self.assertEqual(h.notices[-2:], ["paused", "resumed"])

    def test_resume_before_any_audio_is_preparing(self):
        h = H()
        h.begin()
        self.assertTrue(h.engine.pause())
        self.assertTrue(h.engine.resume())
        self.assertEqual(h.state, E.PREPARING)

    def test_after_shutdown(self):
        h = reach(self, "reading")
        h.engine.shutdown()
        self.assertFalse(h.engine.pause())
        self.assertFalse(h.engine.resume())


class InfoTests(unittest.TestCase):
    def test_can_type_and_can_clear_per_shown_state(self):
        expected = {"ready": (True, False), "offline": (True, False), "shortcut_unavailable": (True, False),
                    "overlay_over_ready": (True, False)}
        for name in SCENARIOS:
            with self.subTest(name):
                h = reach(self, name)
                h.advance(E.COPY_SHOW_MS + 1)  # long enough for Copying to show
                info = h.info
                if h.shown == E.COPYING:
                    self.assertEqual((info["can_type"], info["can_clear"]), (False, False))
                else:
                    self.assertEqual((info["can_type"], info["can_clear"]), expected.get(name, (True, True)))

    def test_copying_from_finished(self):
        h = reach(self, "finished")
        states = len(h.states)
        h.press()
        h.advance(E.COPY_SHOW_MS - 1)
        self.assertEqual(len(h.states), states)  # nothing new to show in the first 150 ms
        self.assertEqual((h.shown, h.info["can_type"], h.info["can_clear"]), (E.FINISHED, True, True))
        h.advance(2)
        self.assertEqual(len(h.states), states + 1)
        self.assertEqual((h.shown, h.info["can_type"], h.info["can_clear"]), (E.COPYING, False, False))

    def test_hidden_copying_in_ready_emits_nothing(self):
        h = H()
        states = len(h.states)
        h.press()
        h.advance(E.COPY_SHOW_MS - 1)
        h.copied(make_text(3))
        self.assertEqual(h.shown_states()[states:], [E.PREPARING])


class WiringTests(unittest.TestCase):
    def test_app_also_connecting_the_input_slots_is_not_double_handled(self):
        h = H()
        h.input.hotkeyPressed.connect(h.engine.on_hotkey)
        h.input.copied.connect(h.engine.on_copied)
        h.press(E.HK_PANEL)
        self.assertEqual(h.toggles, 1)
        h.press()
        h.copied("", "empty")
        self.assertEqual(h.toggles, 2)
        self.assertEqual(h.state, E.READY)
        h.play()
        self.assertEqual(len(h.synth.requests), 2)
        h.press(E.HK_PAUSE)
        self.assertEqual(h.state, E.PAUSED)

    def test_direct_slot_calls_are_handled(self):
        h = H()
        h.engine.on_hotkey(E.HK_PANEL)
        self.assertEqual(h.toggles, 1)
        h.engine.on_hotkey(E.HK_READ)
        h.engine.on_copied(0, make_text(2), "ok")
        self.assertEqual(h.state, E.PREPARING)

    def test_workers_start_even_without_start(self):
        clock = FakeClock()
        synth, player, inp, settings = FakeSynth(), FakePlayer(), FakeInput(), FakeSettings(volume=40)
        engine = E.Engine(synth=synth, player=player, input_thread=inp, settings=settings, clock=clock,
                          timer_factory=clock.timer)
        states = []
        engine.stateChanged.connect(lambda s, i: states.append(s))
        self.assertFalse(synth.started or player.running)
        clock.advance(0)                 # the first event-loop pass
        self.assertTrue(synth.started and player.running)
        self.assertEqual(player.cmds, [("set_volume", 40)])
        self.assertEqual(states, [E.READY])

    def test_start_is_idempotent(self):
        h = H()
        h.engine.start()
        h.advance(10)
        self.assertEqual(h.cmds("set_volume"), [("set_volume", 100)])

    def test_shutdown_before_start_does_not_start_workers(self):
        clock = FakeClock()
        synth, player = FakeSynth(), FakePlayer()
        engine = E.Engine(synth=synth, player=player, input_thread=FakeInput(), settings=FakeSettings(),
                          clock=clock, timer_factory=clock.timer)
        engine.shutdown()
        clock.advance(10)
        self.assertFalse(synth.started or player.running)
        self.assertTrue(synth.shut and player.shut)


class RealTimerTests(unittest.TestCase):
    """The default QTimer-based timers, driven by a real Qt event loop (no windows are shown)."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_default_timers_fire(self):
        from PySide6.QtCore import QEventLoop, QTimer
        synth, player, inp, settings = FakeSynth(), FakePlayer(), FakeInput(), FakeSettings()
        engine = E.Engine(synth=synth, player=player, input_thread=inp, settings=settings)
        engine.start()
        engine.set_speed(1.25)
        inp.hotkeyPressed.emit(E.HK_READ)
        loop = QEventLoop()
        QTimer.singleShot(600, loop.quit)
        loop.exec()
        self.assertEqual(settings.values["speed"], 1.25)       # 400 ms commit timer fired
        self.assertEqual(engine.display_state, E.COPYING)      # 150 ms copy-display timer fired
        engine.shutdown()
        self.assertTrue(player.shut and synth.shut)


class ShutdownTests(unittest.TestCase):
    def test_shutdown_stops_workers_and_purges(self):
        h = H()
        a, b = h.play(ready_all=True)
        h.engine.shutdown()
        self.assertTrue(h.player.shut)
        self.assertTrue(h.synth.shut)
        self.assertIsNone(h.sessions[-1])
        self.assertIn(h.path(b), h.synth.deleted)
        self.assertFalse(h.media[-1])

    def test_signals_after_shutdown_are_ignored(self):
        h = H()
        a, _ = h.play()
        h.engine.shutdown()
        count = len(h.states)
        h.tick(a, 500)
        h.press()
        h.copied(make_text(2))
        self.assertEqual(len(h.states), count)


class RouterSynth(FakeSynth):
    """A synth with the router's extras: chunk sizes per voice, and a call at each new reading."""

    def __init__(self, limits=(130, 300)):
        super().__init__()
        self.limits = limits
        self.asked: list[str] = []
        self.readings = 0

    def limits_for(self, voice):
        self.asked.append(voice)
        return self.limits

    def new_reading(self):
        self.readings += 1


class SynthExtrasTests(unittest.TestCase):
    def test_chunk_sizes_come_from_the_synth_for_the_chosen_voice(self):
        h = H(synth=RouterSynth(), voice="edge:en-GB-SoniaNeural")
        h.begin(12)
        first, second = h.synth.requests
        self.assertEqual(first["text"], " ".join(sentence(i) for i in range(2)))      # up to 130 characters
        self.assertEqual(second["text"], " ".join(sentence(i) for i in range(2, 7)))  # up to 300
        self.assertEqual(set(h.synth.asked), {"edge:en-GB-SoniaNeural"})
        self.assertEqual(first["voice"], "edge:en-GB-SoniaNeural")

    def test_a_synth_without_limits_gets_the_default_sizes(self):
        h = H()
        h.begin(12)
        self.assertFalse(hasattr(h.synth, "limits_for"))
        self.assertEqual([len(r["text"]) <= limit for r, limit in zip(h.synth.requests, (E.FIRST_LIMIT, E.LIMIT))],
                         [True, True])
        self.assertEqual(h.synth.requests[1]["text"], " ".join(sentence(i) for i in range(4, 12)))

    def test_each_new_reading_is_announced_once(self):
        h = H(synth=RouterSynth())
        a, _ = h.play()
        self.assertEqual(h.synth.readings, 1)
        self.assertTrue(h.engine.pause())
        self.assertTrue(h.engine.resume())
        h.engine.next()
        self.assertEqual(h.synth.readings, 1)
        h.press()
        h.copied(make_text(3))       # a new selection is a new reading
        self.assertEqual(h.synth.readings, 2)
        self.assertTrue(h.engine.read_text(make_text(2)))
        self.assertEqual(h.synth.readings, 3)


# --- Files: opening, open notices, rebuilds, spoken-span progress and resume --------------------

class FakeSource:
    """Stands in for documents.DocSource."""

    def __init__(self, kind="file", path="", title=""):
        self.kind, self.path, self.title = kind, path, title


class FakeLoaded:
    """Stands in for documents.LoadedDocument: the raw input of a file."""

    def __init__(self, text, pages=0):
        self.text, self.pages = text, pages


NOTES = "\n\nNotes\n\nA note that is shown but not read aloud. Another one."


def file_doc(n=12, *, path="", title="Smith 2024", tail="", pages_of=None) -> Document:
    """A document read from a file: n test sentences, then `tail` (shown, never spoken)."""
    body = Document.from_raw(make_text(n))
    doc = Document(body.text + tail, body.sentences)
    doc.source = FakeSource("file", path, title)
    if pages_of is not None:
        doc.page_of = pages_of
    return doc


class FakeBuilder:
    """builder(raw) -> Document, recording calls; with `async_=True` it also has start(token, raw)."""

    def __init__(self, make, async_=False):
        self.make, self.calls, self.started = make, [], []
        if async_:
            self.start = lambda token, raw: self.started.append((token, raw))

    def __call__(self, raw):
        self.calls.append(raw)
        return self.make(raw)


class FakeStore:
    """Stands in for resume.ResumeStore."""

    def __init__(self, found=None):
        self.found, self.lookups, self.remembered, self.forgotten = found, [], [], []

    def lookup(self, path, size, mtime_ns, fingerprint):
        self.lookups.append((path, size, mtime_ns, fingerprint))
        return self.found

    def remember(self, point):
        self.remembered.append(point)

    def forget(self, path=None):
        self.forgotten.append(path)


def opened(h, doc, raw=None, title="Smith 2024") -> int:
    """Open `doc` as a file through begin_opening and read_document; returns the token."""
    token = h.engine.begin_opening(title, "file")
    assert h.engine.read_document(doc, raw if raw is not None else FakeLoaded(doc.text), token=token)
    return token


class OpeningTests(unittest.TestCase):
    def test_opening_shows_after_150_ms_and_reads_the_file(self):
        h = H()
        cancelled = []
        h.engine.openingCancelled.connect(cancelled.append)
        token = h.engine.begin_opening("Smith 2024", "file")
        self.assertEqual(h.state, E.OPENING)
        self.assertEqual(h.shown, E.READY)
        h.advance(E.OPEN_SHOW_MS - 1)
        self.assertEqual(h.shown, E.READY)
        h.advance(2)
        self.assertEqual(h.shown, E.OPENING)
        info = h.info
        self.assertEqual((info["label"], info["title"], info["open_kind"]), ("Opening…", "Smith 2024", "file"))
        self.assertEqual(info["play_action"], "none")
        self.assertTrue(info["can_stop"])
        self.assertFalse(info["can_type"] or info["can_clear"] or info["can_seek"] or info["can_next"])
        self.assertIn("opening", h.notices)
        h.engine.opening_progress(token, 40, 160)
        self.assertEqual((h.info["open_done"], h.info["open_total"]), (40, 160))
        self.assertEqual(h.positions[-1], (-1, -1, -1, 0.0, 0.25, False))
        h.engine.opening_progress(token + 1, 80, 160)  # not this opening's
        self.assertEqual(h.info["open_done"], 40)
        doc, raw = file_doc(), FakeLoaded("raw text", pages=18)
        self.assertTrue(h.engine.read_document(doc, raw, token=token))
        self.assertEqual(h.state, E.PREPARING)
        self.assertIs(h.sessions[-1], doc)
        self.assertIs(h.engine.raw, raw)
        self.assertEqual((h.info["source"], h.info["title"], h.info["pages"]), ("file", "Smith 2024", 18))
        self.assertEqual(cancelled, [])
        self.assertEqual(h.beeps, [])

    def test_the_current_document_stays_until_the_new_one_is_read(self):
        h = H()
        a, _ = h.play()
        old = h.engine.document
        token = h.engine.begin_opening("Smith 2024", "file")
        self.assertEqual(h.state, E.OPENING)
        self.assertIn(("pause",), h.player.cmds)  # a reading pauses, as it does for Ctrl+`
        self.assertIs(h.engine.document, old)
        self.assertEqual(len(h.sessions), 1)
        doc = file_doc(4)
        h.engine.read_document(doc, FakeLoaded(doc.text), token=token)
        self.assertIs(h.engine.document, doc)

    def test_stop_cancels_opening_and_returns_to_the_paused_reading(self):
        h = H()
        a, _ = h.play()
        cancelled = []
        h.engine.openingCancelled.connect(cancelled.append)
        token = h.engine.begin_opening("Smith 2024", "file")
        h.advance(E.OPEN_SHOW_MS + 1)
        beeps = len(h.beeps)
        h.engine.stop()
        self.assertEqual((h.state, h.shown), (E.PAUSED, E.PAUSED))
        self.assertEqual(cancelled, [token])
        self.assertEqual(len(h.beeps), beeps)  # no beep and no notice for a cancel
        self.assertEqual(h.positions[-1][0], 0)  # the old document's place comes back
        self.assertFalse(h.engine.read_document(file_doc(), token=token))  # the loader's late answer
        self.assertTrue(h.engine.resume())
        self.assertEqual(h.state, E.READING)

    def test_opening_again_replaces_the_open_in_progress(self):
        h = H()
        cancelled = []
        h.engine.openingCancelled.connect(cancelled.append)
        first = h.engine.begin_opening("One", "file")
        second = h.engine.begin_opening("Two", "file")
        self.assertEqual(cancelled, [first])
        self.assertFalse(h.engine.read_document(file_doc(), token=first))
        h.engine.opening_failed(first, "io")  # stale: ignored
        self.assertEqual(h.state, E.OPENING)
        self.assertTrue(h.engine.read_document(file_doc(), token=second))

    def test_failure_shows_a_notice_that_stays_until_the_next_action(self):
        for kind, beep in (("password", "ok"), ("protected", "ok"), ("scanned", "ok"), ("unsupported", "ok"),
                           ("too_large", "ok"), ("corrupt", "error"), ("io", "error")):
            with self.subTest(kind):
                h = H()
                token = h.engine.begin_opening("Smith 2024", "file")
                h.engine.opening_failed(token, kind)
                self.assertEqual(h.shown, E.OPEN_FAILED)
                self.assertEqual((h.info["kind"], h.info["title"], h.info["underlying"]),
                                 (kind, "Smith 2024", E.READY))
                self.assertEqual(h.info["label"], E.OPEN_LABELS[kind])
                self.assertEqual((h.beeps, h.notices[-1]), ([beep], "open_failed"))
                h.advance(60_000)
                self.assertEqual(h.shown, E.OPEN_FAILED)
                h.engine.stop()
                self.assertEqual((h.shown, h.info["kind"]), (E.READY, ""))

    def test_under_a_notice_play_acts_on_the_paused_reading(self):
        h = H()
        a, _ = h.play()
        token = h.engine.begin_opening("Smith 2024", "file")
        h.engine.opening_failed(token, "password")
        self.assertEqual((h.shown, h.info["underlying"], h.info["play_action"]), (E.OPEN_FAILED, E.PAUSED, "resume"))
        self.assertEqual(h.media[-1], True)  # media keys still work under the notice
        h.engine.toggle_pause()
        self.assertEqual((h.state, h.shown), (E.READING, E.READING))

    def test_a_new_read_or_dismiss_ends_the_notice(self):
        h = H()
        h.engine.open_notice("missing", "Smith 2024")
        self.assertEqual((h.shown, h.info["label"], h.beeps), (E.OPEN_FAILED, "File not found", ["error"]))
        h.engine.dismiss_notice()
        self.assertEqual(h.shown, E.READY)
        h.engine.open_notice("unsupported")
        self.assertTrue(h.engine.read_text(make_text(2)))
        self.assertEqual(h.state, E.PREPARING)
        h.engine.open_notice("unsupported")  # over a reading: it pauses underneath
        self.assertEqual((h.shown, h.info["underlying"]), (E.OPEN_FAILED, E.PAUSED))
        h.press()
        self.assertEqual(h.state, E.PAUSED)  # Ctrl+` ended the notice; the copy result decides

    def test_an_empty_document_gives_the_empty_notice(self):
        h = H()
        token = h.engine.begin_opening("Blank", "file")
        self.assertFalse(h.engine.read_document(Document("", []), FakeLoaded(""), token=token))
        self.assertEqual((h.shown, h.info["kind"], h.info["title"]), (E.OPEN_FAILED, "empty", "Blank"))

    def test_ctrl_grave_and_typed_text_replace_the_opening(self):
        h = H()
        cancelled = []
        h.engine.openingCancelled.connect(cancelled.append)
        token = h.engine.begin_opening("Smith 2024", "file")
        h.press()
        self.assertEqual(cancelled, [token])
        self.assertEqual(h.state, E.COPYING)
        h.copied(make_text(3))
        self.assertEqual(h.state, E.PREPARING)
        token = h.engine.begin_opening("Smith 2024", "file")
        self.assertTrue(h.engine.read_text(make_text(2)))
        self.assertEqual(cancelled[-1], token)
        self.assertEqual(len(h.sessions[-1].sentences), 2)

    def test_clear_while_opening_keeps_the_open_going(self):
        h = H()
        h.play()
        token = h.engine.begin_opening("Smith 2024", "file")
        self.assertIsNotNone(h.engine.clear())
        self.assertIsNone(h.engine.document)
        self.assertEqual(h.state, E.OPENING)
        self.assertTrue(h.engine.read_document(file_doc(), token=token))

    def test_logs_never_hold_titles(self):
        with self.assertLogs("saylight.engine", "INFO") as logs:
            h = H()
            token = h.engine.begin_opening("Secret Title", "file")
            h.engine.opening_failed(token, "password")
            h.engine.open_notice("missing", "Secret Title")
        output = "\n".join(logs.output)
        self.assertIn("Open failed: password", output)
        self.assertNotIn("Secret", output)


class SpokenSpanTests(unittest.TestCase):
    def test_progress_measures_the_spoken_span_only(self):
        """Notes shown after the main text don't count, so Finished is 100% and the bar never stops at 44%."""
        h = H()
        doc = file_doc(4, tail=NOTES)
        h.engine.read_document(doc, FakeLoaded(doc.text))
        lo, hi = E.spoken_span(doc)
        self.assertEqual((lo, hi), (0, doc.sentences[-1].end))
        self.assertLess(hi, len(doc.text))
        a = h.req(0)["chunk_id"]
        h.ready(a)
        h.started(a)
        h.tick(a, s_start(1) + 600 + 125)  # half-way through "alpha" of sentence 1
        _, wlo, whi, played, buffered, _ = h.positions[-1]
        self.assertAlmostEqual(played, (doc.sentences[1].start + wlo + (whi - wlo) * 0.5) / hi)
        self.assertAlmostEqual(buffered, 1.0)  # the one chunk holds every spoken sentence
        h.end(a)
        self.assertEqual(h.state, E.FINISHED)
        self.assertAlmostEqual(h.positions[-1][3], 1.0)
        self.assertTrue(h.info["end_of_main_text"])

    def test_no_end_of_main_text_without_an_unspoken_tail(self):
        h = H()
        a, _ = h.play(4)
        h.end(a)
        self.assertEqual(h.state, E.FINISHED)
        self.assertFalse(h.info["end_of_main_text"])

    def test_span_fraction(self):
        doc = file_doc(4, tail=NOTES)
        hi = doc.sentences[-1].end
        self.assertEqual(E.span_fraction(doc, hi / 2), 0.5)
        self.assertEqual(E.span_fraction(doc, len(doc.text)), 1.0)
        self.assertEqual(E.span_fraction(None, 3), 0.0)
        self.assertEqual(E.spoken_span(Document("", [])), (0, 0))

    def test_time_left_counts_spoken_characters_only(self):
        values = []
        for tail in ("", NOTES * 20):
            h = H()
            doc = file_doc(12, tail=tail)
            h.engine.read_document(doc, FakeLoaded(doc.text))
            a = h.req(0)["chunk_id"]
            h.ready(a)
            h.started(a)
            h.tick(a, 100)
            values.append(h.time_left[-1])
        self.assertGreater(values[0], 0)
        self.assertEqual(values[0], values[1])


class RebuildTests(unittest.TestCase):
    def test_settings_rebuild_at_rest_keeps_the_place(self):
        builder = FakeBuilder(lambda raw: Document.from_raw(raw))
        h = H(engine_kwargs={"builder": builder})
        a, _ = h.play(12)
        h.tick(a, s_start(2) + 100)
        h.engine.stop()
        self.assertEqual(h.engine.current_sentence, 2)
        builder.calls.clear()
        h.settings.set("skip_citations", False)
        h.settings.set("skip_urls", False)  # two changes, one rebuild
        h.advance(1)
        self.assertEqual(len(builder.calls), 1)
        self.assertIsNot(h.sessions[-1], h.sessions[0])
        self.assertEqual((h.state, h.engine.current_sentence), (E.STOPPED, 2))

    def test_rebuild_while_reading_keeps_unchanged_audio_playing(self):
        """Adding a section after the main text leaves every sentence read so far as it was: no restart."""
        base = make_text(6)
        builder = FakeBuilder(lambda raw: Document.from_raw(raw))
        h = H(engine_kwargs={"builder": builder})
        h.engine.read_text(base)
        a = h.req(0)["chunk_id"]
        h.ready(a)
        h.started(a)
        h.tick(a, s_start(1) + 100)
        builder.make = lambda raw: Document.from_raw(raw + " " + make_text(9)[len(base) + 1:])
        closes, loads = len(h.cmds("stop_close")), len(h.cmds("load"))
        h.engine.rebuild()
        self.assertEqual((len(h.cmds("stop_close")), len(h.cmds("load"))), (closes, loads))
        self.assertEqual((h.state, h.engine.current_sentence), (E.READING, 1))
        self.assertEqual(len(h.engine.document.sentences), 9)

    def test_rebuild_that_changes_the_current_sentence_restarts_it(self):
        builder = FakeBuilder(lambda raw: Document.from_raw(raw))
        h = H(engine_kwargs={"builder": builder})
        h.engine.read_text(make_text(6))
        a = h.req(0)["chunk_id"]
        h.ready(a)
        h.started(a)
        h.tick(a, s_start(2) + 100)
        builder.make = lambda raw: Document.from_raw(raw.replace("Sentence 00", "Line 00"))
        h.engine.rebuild()
        self.assertEqual(h.engine.current_sentence, 2)
        self.assertIn("Line 002", h.req()["text"])  # re-planned from the same sentence
        self.assertEqual(h.state, E.WAITING)

    def test_long_text_is_built_on_the_worker(self):
        builder = FakeBuilder(lambda raw: Document.from_raw(raw), async_=True)
        h = H(engine_kwargs={"builder": builder})
        text = make_text(400)  # over 20k characters
        self.assertGreater(len(text), E.BUILD_ASYNC_CHARS)
        self.assertTrue(h.engine.read_text(text))
        self.assertEqual(builder.calls, [])
        (token, raw), = builder.started
        self.assertEqual((h.state, raw), (E.OPENING, text))
        h.advance(E.OPEN_SHOW_MS + 1)
        self.assertEqual((h.info["label"], h.info["open_kind"]), ("Preparing audio…", "text"))
        self.assertTrue(h.engine.read_document(Document.from_raw(text), text, token=token))
        self.assertEqual(h.state, E.PREPARING)
        self.assertIs(h.engine.raw, text)
        # a rebuild of it goes to the worker too, and comes back keeping the place
        h.engine.rebuild()
        rebuild_token, _ = builder.started[-1]
        self.assertNotEqual(rebuild_token, token)
        self.assertEqual(h.state, E.PREPARING)
        self.assertFalse(h.engine.read_document(Document.from_raw(text), "other raw", token=rebuild_token))
        h.engine.rebuild()
        newer, _ = builder.started[-1]
        h.engine.opening_failed(newer, "corrupt")  # a failed rebuild keeps the document, with no notice
        self.assertEqual((h.state, h.beeps), (E.PREPARING, []))
        h.engine.rebuild()
        newest, _ = builder.started[-1]
        rebuilt = Document.from_raw(text)
        self.assertTrue(h.engine.read_document(rebuilt, text, token=newest))
        self.assertIs(h.engine.document, rebuilt)
        self.assertEqual(h.state, E.PREPARING)

    def test_from_finished_a_section_chip_reads_that_section(self):
        body = make_text(3)

        def build(raw, notes=False):
            doc = Document.from_raw(raw if not notes else raw + " Notes heading here. A note to read.")
            offset = len(Document.from_raw(raw).text) + 1
            doc.sections = {"notes": offset}
            return doc

        builder = FakeBuilder(build)
        h = H(engine_kwargs={"builder": builder})
        h.engine.read_text(body)
        a = h.req(0)["chunk_id"]
        h.ready(a)
        h.started(a)
        h.end(a)
        self.assertEqual(h.state, E.FINISHED)
        builder.make = lambda raw: build(raw, notes=True)
        h.engine.rebuild(read_section="notes")
        self.assertEqual((h.state, h.engine.current_sentence), (E.WAITING, 3))
        self.assertTrue(h.req()["text"].startswith("Notes heading here."))

    def test_default_build_without_a_builder(self):
        h = H(skip_citations=False)
        h.engine.read_text("Reading improved (Smith, 2020). It lasted.")
        self.assertIn("(Smith, 2020)", h.sessions[-1].text)

    def test_purge_forgets_the_raw_input(self):
        h = H()
        h.engine.read_text(make_text(3))
        self.assertIsNotNone(h.engine.raw)
        token = h.engine.clear()
        self.assertIsNone(h.engine.raw)
        h.engine.restore(token)
        self.assertEqual(h.engine.raw, make_text(3))


class ResumeTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"%PDF test")
        os.close(fd)

    def tearDown(self):
        os.remove(self.path)

    def point(self, sentence, offset=None, fingerprint="", exact=True):
        """A saved place; `offset` defaults to where `sentence` starts in file_doc(12)."""
        if offset is None:
            starts = [s.start for s in file_doc(12).sentences]
            offset = starts[sentence] if 0 <= sentence < len(starts) else 0
        st = os.stat(self.path)
        return (ResumePoint(self.path, st.st_size, st.st_mtime_ns, fingerprint, offset, sentence, 1.0, 3), exact)

    def test_an_exact_match_continues_from_the_saved_sentence(self):
        store = FakeStore(self.point(5))
        h = H(engine_kwargs={"resume": store}, **{"resume.enabled": True})
        applied = []
        h.engine.resumeApplied.connect(applied.append)
        doc = file_doc(12, path=self.path)
        opened(h, doc)
        (path, size, mtime_ns, fingerprint), = store.lookups
        self.assertEqual((path, size), (self.path, os.stat(self.path).st_size))
        self.assertEqual(fingerprint, hashlib.sha256(doc.text.encode("utf-8")).hexdigest())
        self.assertEqual((applied, h.engine.current_sentence, h.engine.resumed), ([5], 5, (5, True)))
        self.assertTrue(h.req(0)["text"].startswith(sentence(5)))

    def test_an_exact_match_keeps_the_place_after_skip_citations_changes(self):
        # The fingerprint covers the shown text, which Skip citations doesn't change, but sentences are split on
        # the spoken text, so the saved sentence number can point somewhere else; the offset can't.
        text = ("Earlier work agrees (Smith et al. 2019). (Jones, 2020) argues otherwise. A third sentence follows "
                "here. And a fourth one.")
        before = Document.build(text, BuildOptions(skip_citations=True))
        after = Document.build(text, BuildOptions(skip_citations=False))
        after.source = FakeSource("file", self.path, "paper")
        self.assertEqual(before.fingerprint(), after.fingerprint())
        self.assertNotEqual(len(before.sentences), len(after.sentences))
        folder = tempfile.mkdtemp(prefix="slrs")
        self.addCleanup(shutil.rmtree, folder, True)
        store = ResumeStore(os.path.join(folder, "resume.json"))
        k = len(before.sentences) - 1
        st = os.stat(self.path)
        store.remember(ResumePoint(self.path, st.st_size, st.st_mtime_ns, before.fingerprint(),
                                   before.sentences[k].start, k, 1.0, 0))
        h = H(engine_kwargs={"resume": store}, **{"resume.enabled": True})
        opened(h, after)
        self.assertEqual(after.sentences[h.engine.current_sentence].text, "And a fourth one.")

    def test_a_changed_file_continues_near_the_saved_offset(self):
        doc = file_doc(12, path=self.path)
        store = FakeStore(self.point(99, offset=doc.sentences[7].start + 3, exact=False))
        h = H(engine_kwargs={"resume": store}, **{"resume.enabled": True})
        opened(h, doc)
        self.assertEqual(h.engine.resumed, (7, False))

    def test_saves_paths_and_numbers_on_pause_stop_and_every_30_s(self):
        store = FakeStore()
        h = H(engine_kwargs={"resume": store}, **{"resume.enabled": True})
        doc = file_doc(12, path=self.path, pages_of=lambda s: 1 + s // 4)
        opened(h, doc)
        a = h.req(0)["chunk_id"]
        h.ready(a)
        h.started(a)
        h.tick(a, s_start(2) + 100)
        self.assertEqual(store.remembered, [])
        h.advance(E.RESUME_SAVE_MS)
        self.assertEqual(len(store.remembered), 1)
        h.engine.pause()
        point = store.remembered[-1]
        self.assertEqual((point.path, point.sentence, point.offset, point.page),
                         (self.path, 2, doc.sentences[2].start, 1))
        for value in (point.size, point.mtime_ns, point.offset, point.sentence, point.page):
            self.assertIsInstance(value, int)
        self.assertNotIn(doc.sentences[2].text, repr(point))
        h.engine.stop()
        self.assertEqual(len(store.remembered), 3)
        h.advance(E.RESUME_SAVE_MS * 3)  # nothing more while stopped
        self.assertEqual(len(store.remembered), 3)

    def test_finishing_forgets_the_file(self):
        store = FakeStore()
        h = H(engine_kwargs={"resume": store}, **{"resume.enabled": True})
        opened(h, file_doc(4, path=self.path))
        a = h.req(0)["chunk_id"]
        h.ready(a)
        h.started(a)
        h.end(a)
        self.assertEqual((h.state, store.forgotten), (E.FINISHED, [self.path]))
        remembered = len(store.remembered)
        h.engine.clear()  # a finished file is not saved again
        self.assertEqual(len(store.remembered), remembered)

    def test_nothing_is_stored_with_resume_off_or_for_selections(self):
        store = FakeStore(self.point(5))
        h = H(engine_kwargs={"resume": store})
        opened(h, file_doc(12, path=self.path))
        self.assertEqual((store.lookups, h.engine.current_sentence), ([], 0))
        h.engine.pause()
        h.engine.stop()
        h2 = H(engine_kwargs={"resume": store}, **{"resume.enabled": True})
        h2.engine.read_text(make_text(6))
        h2.engine.pause()
        self.assertEqual((store.lookups, store.remembered, store.forgotten), ([], [], []))

    def test_turning_resume_on_remembers_this_place_now(self):
        store = FakeStore()
        h = H(engine_kwargs={"resume": store})
        opened(h, file_doc(12, path=self.path))
        h.engine.stop()
        self.assertEqual(store.remembered, [])
        h.settings.set("resume.enabled", True)
        self.assertEqual(len(store.remembered), 1)

    def test_start_over_reads_from_the_first_sentence(self):
        store = FakeStore(self.point(5))
        h = H(engine_kwargs={"resume": store}, **{"resume.enabled": True})
        opened(h, file_doc(12, path=self.path))
        a = h.req(0)["chunk_id"]
        h.ready(a)
        h.started(a)
        requests = len(h.synth.requests)
        h.engine.start_over()
        self.assertEqual((h.engine.current_sentence, h.engine.resumed), (0, None))
        h.advance(E.SEEK_DELAY_MS + 1)
        self.assertTrue(h.req(requests)["text"].startswith(sentence(0)))


class ScreenAndScanTests(unittest.TestCase):
    """Reading from the screen and scanned PDFs: the screen opening, the OCR phase, the page choice and the OCR
    notices over the state underneath."""

    def test_a_screen_opening_keeps_the_paused_reading_and_shows_finding_the_text_after_150_ms(self):
        h = H()
        h.play()
        old = h.engine.document
        self.assertTrue(h.engine.pause())
        token = h.engine.begin_opening("", "screen")
        self.assertEqual((h.state, h.shown), (E.OPENING, E.PAUSED))
        h.advance(E.OPEN_SHOW_MS + 1)
        info = h.info
        self.assertEqual((h.shown, info["label"], info["open_kind"]), (E.OPENING, E.FINDING_LABEL, "screen"))
        self.assertEqual(info["play_action"], "none")
        self.assertTrue(info["can_stop"])
        self.assertFalse(info["can_type"] or info["can_clear"] or info["can_next"])
        self.assertNotIn("opening", h.notices)  # the HUD follows the state, not a file's notice
        self.assertIs(h.engine.document, old)
        h.engine.stop()  # cancels: the exact state before it comes back
        self.assertEqual((h.state, h.shown), (E.PAUSED, E.PAUSED))
        self.assertFalse(h.engine.read_document(file_doc(), token=token))

    def test_a_cancelled_screen_opening_goes_back_without_a_notice(self):
        h = H()
        token = h.engine.begin_opening("", "screen")
        h.advance(E.OPEN_SHOW_MS + 1)
        h.engine.opening_failed(token, "cancelled")
        self.assertEqual((h.state, h.shown), (E.READY, E.READY))
        self.assertEqual(h.beeps, [])

    def test_a_failed_screen_opening_restores_the_paused_reading_under_a_notice(self):
        h = H()
        h.play()
        h.engine.pause()
        token = h.engine.begin_opening("", "screen")
        h.engine.opening_failed(token, "screen_error")
        info = h.info
        self.assertEqual((h.state, h.shown), (E.OCR_NOTICE, E.OCR_NOTICE))
        self.assertEqual((info["label"], info["notice"], info["severity"], info["underlying"]),
                         ("Couldn't read the screen", "screen_error", "critical", E.PAUSED))
        self.assertEqual(info["play_action"], "resume")  # the controls follow Paused
        self.assertEqual(h.beeps, ["error"])
        h.advance(60_000)
        self.assertEqual(h.shown, E.OCR_NOTICE)  # critical: until the next action
        h.engine.toggle_pause()
        self.assertEqual(h.state, E.READING)

    def test_a_caution_notice_lasts_8_s_and_waits_while_the_pointer_is_over_the_card(self):
        h = H()
        h.engine.ocr_notice("screen_nothing", "nothing_speakable")
        self.assertEqual((h.shown, h.info["label"], h.info["severity"]), (E.OCR_NOTICE, "No text found", "caution"))
        self.assertEqual(h.info["notice_detail"], "nothing_speakable")
        self.assertEqual(h.beeps, ["ok"])
        h.engine.hold_overlay(True)
        h.advance(E.OCR_NOTICE_MS + 5000)
        self.assertEqual(h.shown, E.OCR_NOTICE)
        h.engine.hold_overlay(False)
        h.advance(E.HOLD_LEAVE_MS - 1)
        self.assertEqual(h.shown, E.OCR_NOTICE)
        h.advance(2)
        self.assertEqual(h.shown, E.READY)

    def test_nothing_selected_waits_while_held(self):
        h = H()
        h.engine.read_selection_click(1)
        h.copied("", "shell", token=h.input.copy_requests[-1][0])
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        h.engine.hold_overlay(True)
        h.advance(E.NOTHING_SELECTED_MS + 1000)
        self.assertEqual(h.shown, E.NOTHING_SELECTED)
        h.engine.hold_overlay(False)
        h.advance(E.HOLD_LEAVE_MS + 1)
        self.assertEqual(h.shown, E.READY)

    def test_a_language_notice_over_a_playing_reading_pauses_it(self):
        h = H()
        h.play()
        h.engine.ocr_notice("screen_language")
        self.assertEqual((h.shown, h.info["underlying"]), (E.OCR_NOTICE, E.PAUSED))
        h.engine.dismiss_notice()
        self.assertEqual(h.shown, E.PAUSED)

    def test_the_ocr_phase_counts_pages_and_says_the_time_left(self):
        h = H()
        token = h.engine.begin_opening("Scan", "file")
        h.advance(E.OPEN_SHOW_MS + 1)
        h.engine.opening_progress(token, 0, 12, phase="ocr")
        info = h.info
        self.assertEqual((info["label"], info["open_phase"], info["ocr_page"], info["ocr_total"], info["ocr_left"]),
                         (E.FINDING_LABEL, "ocr", 1, 12, -1))
        h.advance(3000)
        h.engine.opening_progress(token, 1, 12, phase="ocr")
        self.assertEqual(h.positions[-1][4], 1 / 12)  # the bar follows every page
        h.advance(3000)
        h.engine.opening_progress(token, 2, 12, phase="ocr")  # 3 s a page: 30 s left
        self.assertEqual((h.info["ocr_page"], h.info["ocr_left"]), (3, 30))
        h.advance(500)
        h.engine.opening_progress(token, 3, 12, phase="ocr")  # under 2 s since: the header waits
        self.assertEqual(h.info["ocr_page"], 3)
        h.advance(2000)
        h.engine.opening_progress(token, 4, 12, phase="ocr")
        self.assertEqual(h.info["ocr_page"], 5)
        h.engine.stop()
        self.assertEqual(h.state, E.READY)
        self.assertEqual(h.info.get("open_phase", ""), "")

    def test_a_long_scan_waits_for_a_choice_shown_at_once(self):
        h = H()
        cancelled = []
        h.engine.openingCancelled.connect(cancelled.append)
        token = h.engine.begin_opening("Scan", "file")
        h.engine.opening_choice(token, 160, 160, 128)
        info = h.info
        self.assertEqual((h.shown, info["label"], info["open_phase"]), (E.OPENING, E.SCAN_CHOICE_LABEL, "choice"))
        self.assertEqual((info["scan_scanned"], info["scan_pages"], info["scan_est"]), (160, 160, 128))
        self.assertIn("scan_choice", h.notices)
        h.engine.opening_progress(token, 0, 50, phase="ocr")  # the person chose pages
        self.assertEqual((h.info["label"], h.info["open_phase"]), (E.FINDING_LABEL, "ocr"))
        h.engine.stop()
        self.assertEqual(cancelled, [token])

    def test_scan_failures_are_ocr_notices_over_the_state_before(self):
        h = H()
        h.play()
        token = h.engine.begin_opening("Scan", "file")
        h.engine.opening_failed(token, "scan_language")
        self.assertEqual((h.shown, h.info["label"], h.info["underlying"]), (E.OCR_NOTICE, "Can't read this scan",
                                                                           E.PAUSED))
        token = h.engine.begin_opening("Scan", "file")
        h.engine.opening_failed(token, "scan_nothing")
        self.assertEqual((h.info["notice"], h.info["severity"]), ("scan_nothing", "caution"))

    def test_nothing_selected_with_the_screen_chip_lasts_6_s_and_the_others_3_s(self):
        for reason, ms in (("empty", E.NOTHING_SCREEN_MS), ("console", E.NOTHING_SCREEN_MS),
                           ("shell", E.NOTHING_SELECTED_MS), ("busy", E.NOTHING_SELECTED_MS)):
            with self.subTest(reason=reason):
                h = H()
                h.engine.read_selection_click(1)
                h.copied("", reason, token=h.input.copy_requests[-1][0])
                h.advance(ms - 1)
                self.assertEqual(h.shown, E.NOTHING_SELECTED)
                h.advance(2)
                self.assertEqual(h.shown, E.READY)


if __name__ == "__main__":
    unittest.main()

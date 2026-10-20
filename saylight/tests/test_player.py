"""Tests for player.py.

Silent by construction: every PlayerThread here is created with volume=0 and also gets
set_volume(0) before start(), and the open path applies the volume before any play. One MP3 is
synthesized in setUpClass (one edge-tts request) and reused.
"""

import asyncio
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import edge_tts
from PySide6.QtCore import QEventLoop

from .. import player
from ..player import MciError, PlayerThread, mci_volume

VOICE = "en-US-AvaNeural"
TEXT = "One, two, three, four, five, six, seven, eight, nine, ten."


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


async def _save(path: str) -> None:
    await edge_tts.Communicate(TEXT, VOICE, rate="-5%").save(path)
    await asyncio.sleep(0.25)          # let aiohttp's SSL transport finish closing before the loop does


def spin(seconds: float) -> None:
    app, end = _app(), time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
        time.sleep(0.002)


def spin_until(pred, timeout: float) -> bool:
    app, end = _app(), time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
        if pred():
            return True
        time.sleep(0.002)
    return pred()


class PlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        cls.dir = tempfile.mkdtemp(prefix="player_test_")
        cls.mp3 = os.path.join(cls.dir, "sample.mp3")
        try:
            asyncio.run(_save(cls.mp3))
        except Exception as e:  # noqa: BLE001 - no network: nothing to play
            shutil.rmtree(cls.dir, ignore_errors=True)
            raise unittest.SkipTest(f"edge-tts unavailable: {type(e).__name__}")
        alias = "player_test_len"                       # opened only to read the length, never played
        player._mci(f'open "{cls.mp3}" type mpegvideo alias {alias}')
        try:
            player._mci(f"set {alias} time format milliseconds")
            cls.length = int(player._mci(f"status {alias} length"))
        finally:
            player._mci_quiet(f"close {alias}")
        assert cls.length > 2500, cls.length

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.p = PlayerThread(volume=0)
        self.p.set_volume(0)                            # before any play
        self.events: list[tuple] = []
        self.ticks: list[tuple] = []
        self.p.started.connect(lambda c, n: self.events.append(("started", c, n)))
        self.p.ended.connect(lambda c, why: self.events.append(("ended", c, why)))
        self.p.failed.connect(lambda c, err: self.events.append(("failed", c, err)))
        self.p.tick.connect(lambda c, pos, playing: self.ticks.append((time.monotonic(), c, pos, playing)))
        self.p.start()
        self.addCleanup(self.p.shutdown)

    # --- helpers ---

    def mci(self, command: str) -> str:
        return self.p._run_on_thread(lambda: player._mci(command))

    def alias(self) -> str:
        return self.p._run_on_thread(lambda: self.p._dev.alias)

    def mode(self) -> str:
        return self.mci(f"status {self.alias()} mode")

    def last_tick(self, chunk_id: int) -> tuple:
        return [t for t in self.ticks if t[1] == chunk_id][-1]

    def wait_event(self, event: tuple, timeout: float = 3.0) -> float:
        """Wait for an exact event; return the time it was seen."""
        self.assertTrue(spin_until(lambda: event in self.events, timeout), f"{event} not in {self.events}")
        return time.monotonic()

    def fresh_ticks(self, chunk_id: int, n: int = 2, timeout: float = 2.0) -> list[tuple]:
        start = len(self.ticks)
        spin_until(lambda: len([t for t in self.ticks[start:] if t[1] == chunk_id]) >= n, timeout)
        return [t for t in self.ticks[start:] if t[1] == chunk_id]

    def record_mci(self) -> list[tuple[str, str]]:
        """Record every MCI command string with the name of the thread that sent it."""
        calls: list[tuple[str, str]] = []
        for name in ("_mci", "_mci_quiet"):
            real = getattr(player, name)

            def recorder(command: str, real=real):
                calls.append((threading.current_thread().name, command))
                return real(command)

            patcher = mock.patch.object(player, name, recorder)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    # --- tests ---

    def test_mci_volume_curve(self):
        self.assertEqual([mci_volume(v) for v in (-5, 0, 1, 50, 100, 150)], [0, 0, 10, 500, 1000, 1000])

    def test_volume_is_applied_on_open_and_while_loaded(self):
        self.p.load(1, self.mp3, play=False)
        self.fresh_ticks(1)
        self.assertEqual(self.mci(f"status {self.alias()} volume"), "0")
        self.p.set_volume(30)                           # never played while non-zero
        spin(0.15)
        self.assertAlmostEqual(int(self.mci(f"status {self.alias()} volume")), 300, delta=10)
        self.p.set_volume(0)
        spin(0.15)
        self.assertEqual(self.mci(f"status {self.alias()} volume"), "0")
        self.assertEqual(self.mode(), "stopped")
        self.assertEqual(self.events, [])

    def test_load_without_play_ticks_but_never_starts(self):
        self.p.load(2, self.mp3, start_ms=500, play=False)
        ticks = self.fresh_ticks(2, n=4)
        self.assertGreaterEqual(len(ticks), 4)
        self.assertTrue(all(t[2] == 500 and t[3] is False for t in ticks))
        self.assertEqual(self.events, [])

    def test_play_starts_and_ticks_at_20_hz(self):
        self.p.load(3, self.mp3)
        self.wait_event(("started", 3, self.length))
        spin(1.0)
        playing = [t for t in self.ticks if t[1] == 3 and t[3]]
        window = [t for t in playing if t[0] >= playing[-1][0] - 1.0]
        self.assertTrue(14 <= len(window) <= 24, len(window))
        positions = [t[2] for t in playing]
        self.assertEqual(positions, sorted(positions))
        self.assertGreater(positions[-1] - positions[0], 600)
        self.assertEqual([e for e in self.events if e[0] == "started"], [("started", 3, self.length)])

    def test_pause_and_resume(self):
        self.p.load(4, self.mp3)
        self.wait_event(("started", 4, self.length))
        spin(0.4)
        self.p.pause()
        spin(0.15)
        self.assertEqual(self.mode(), "paused")
        held = self.last_tick(4)[2]
        ticks = self.fresh_ticks(4, n=6)               # ticks keep coming while paused
        self.assertTrue(all(abs(t[2] - held) <= 30 and t[3] is False for t in ticks), ticks)
        self.p.resume()                                # played before: MCI resume
        spin(0.4)
        self.assertEqual(self.mode(), "playing")
        pos, playing = self.last_tick(4)[2:]
        self.assertTrue(playing)
        self.assertGreater(pos, held + 200)
        self.assertEqual(len([e for e in self.events if e[0] == "started"]), 1)

    def test_resume_after_seek_uses_play(self):
        self.p.load(5, self.mp3)
        self.wait_event(("started", 5, self.length))
        spin(0.3)
        self.p.pause()
        self.p.seek(1500, False)                       # MCI seek leaves the device stopped
        spin(0.15)
        self.assertEqual(self.mode(), "stopped")
        self.assertEqual(self.last_tick(5)[2:], (1500, False))
        self.p.resume()                                # never played since the seek: MCI play
        spin(0.4)
        self.assertEqual(self.mode(), "playing")
        pos, playing = self.last_tick(5)[2:]
        self.assertTrue(playing)
        self.assertTrue(1600 <= pos <= 2300, pos)

    def test_resume_on_a_never_played_load_plays(self):
        self.p.load(6, self.mp3, start_ms=800, play=False)
        self.fresh_ticks(6)
        self.p.resume()
        self.wait_event(("started", 6, self.length))
        spin(0.4)
        pos, playing = self.last_tick(6)[2:]
        self.assertTrue(playing)
        self.assertTrue(900 <= pos <= 1600, pos)

    def test_seek_with_play(self):
        self.p.load(7, self.mp3)
        self.wait_event(("started", 7, self.length))
        self.p.seek(2000, True)
        spin(0.3)
        self.assertEqual(self.mode(), "playing")
        pos, playing = self.last_tick(7)[2:]
        self.assertTrue(playing)
        self.assertTrue(2000 <= pos <= 2600, pos)
        self.assertEqual(len([e for e in self.events if e[0] == "started"]), 1)

    def test_cut_at_load(self):
        self.p.load(8, self.mp3, cut_ms=700)
        t0 = self.wait_event(("started", 8, self.length))
        t1 = self.wait_event(("ended", 8, "cut"))
        self.assertTrue(0.55 <= t1 - t0 <= 1.0, t1 - t0)
        self.assertLess(max(t[2] for t in self.ticks if t[1] == 8), 700)
        n = len(self.ticks)
        spin(0.2)
        self.assertEqual(len(self.ticks), n)           # no ticks once cut
        self.assertNotIn(("ended", 8, "stopped"), self.events)

    def test_set_cut_while_playing_and_clear(self):
        self.p.load(9, self.mp3, cut_ms=400)
        self.p.set_cut(None)
        self.wait_event(("started", 9, self.length))
        spin(0.7)
        self.assertEqual([e for e in self.events if e[0] == "ended"], [])
        pos = self.last_tick(9)[2]
        self.p.set_cut(pos + 300)
        t0 = time.monotonic()
        t1 = self.wait_event(("ended", 9, "cut"))
        self.assertLess(t1 - t0, 0.7)

    def test_natural_end(self):
        self.p.load(10, self.mp3, start_ms=self.length - 600)
        t0 = self.wait_event(("started", 10, self.length))
        t1 = self.wait_event(("ended", 10, "end"))
        self.assertTrue(0.4 <= t1 - t0 <= 1.5, t1 - t0)
        n = len(self.ticks)
        spin(0.2)
        self.assertEqual(len(self.ticks), n)           # no ticks once ended
        self.p.stop_close()                            # nothing loaded: no "stopped"
        self.p.load(11, self.mp3, play=False)
        self.fresh_ticks(11)
        self.assertEqual([e for e in self.events if e[0] == "ended"], [("ended", 10, "end")])

    def test_paused_chunk_is_not_ended(self):
        self.p.load(12, self.mp3, start_ms=self.length - 700)
        self.wait_event(("started", 12, self.length))
        spin(0.15)
        self.p.pause()
        spin(1.2)                                      # longer than what was left to play
        self.assertEqual([e for e in self.events if e[0] == "ended"], [])
        self.p.resume()
        self.wait_event(("ended", 12, "end"), timeout=2.0)

    def test_stop_close_and_replacing_load(self):
        self.p.load(13, self.mp3)
        self.wait_event(("started", 13, self.length))
        self.p.stop_close()
        self.wait_event(("ended", 13, "stopped"))
        self.p.stop_close()
        n = len(self.ticks)
        spin(0.3)
        self.assertEqual(len(self.ticks), n)
        self.assertEqual(self.events.count(("ended", 13, "stopped")), 1)

        self.p.load(14, self.mp3)
        self.wait_event(("started", 14, self.length))
        self.p.load(15, self.mp3)
        self.wait_event(("started", 15, self.length))
        self.assertLess(self.events.index(("ended", 14, "stopped")), self.events.index(("started", 15, self.length)))

    def test_unique_aliases_and_previous_closed(self):
        self.p.load(16, self.mp3, play=False)
        self.fresh_ticks(16)
        first = self.alias()
        self.p.load(17, self.mp3, play=False)
        self.fresh_ticks(17)
        second = self.alias()
        self.assertRegex(first, r"^ra_\d+$")
        self.assertRegex(second, r"^ra_\d+$")
        self.assertNotEqual(first, second)
        with self.assertRaises(MciError):
            self.mci(f"status {first} mode")
        self.assertEqual(self.events, [("ended", 16, "stopped")])

    def test_missing_file_fails_and_player_recovers(self):
        with self.assertLogs("saylight.player", "WARNING"):
            self.p.load(18, os.path.join(self.dir, "missing.mp3"))
            self.assertTrue(spin_until(lambda: self.events, 3))
        kind, chunk_id, error = self.events[0]
        self.assertEqual((kind, chunk_id), ("failed", 18))
        self.assertTrue(error.startswith("MCI error"), error)
        self.p.load(19, self.mp3, play=False)
        self.assertTrue(self.fresh_ticks(19))
        self.assertEqual(len(self.events), 1)

    def test_long_path_uses_the_short_form(self):
        folder = self.dir
        while len(folder) < 270:
            folder = os.path.join(folder, "long_folder_name_" + "x" * 20)
        os.makedirs(folder)
        path = os.path.join(folder, "sample.mp3")
        shutil.copy(self.mp3, path)
        self.assertGreater(len(path), 256)                # MCI alone fails here with error 268
        self.assertLess(len(player._mci_path(path)), player.MCI_MAX_PATH)
        self.assertEqual(player._mci_path(self.mp3), self.mp3)
        self.p.load(22, path, play=False)
        self.assertTrue(self.fresh_ticks(22))
        self.assertEqual(self.events, [])

    def test_commands_without_a_chunk_are_ignored(self):
        for command in (self.p.play, self.p.pause, self.p.resume):
            command()
        self.p.seek(100, True)
        self.p.set_cut(5)
        self.p.stop_close()
        spin(0.2)
        self.assertEqual((self.events, self.ticks), ([], []))
        self.p.load(20, self.mp3, play=False)
        self.assertTrue(self.fresh_ticks(20))

    def test_mci_only_on_player_thread_volume_first_and_resume_only_after_play(self):
        calls = self.record_mci()
        self.p.load(23, self.mp3, start_ms=300, play=False)
        self.fresh_ticks(23)
        self.p.play()
        self.wait_event(("started", 23, self.length))
        spin(0.2)
        self.p.pause()
        spin(0.1)
        self.p.resume()                                # played since the load: MCI resume
        spin(0.2)
        self.p.pause()
        self.p.seek(1200, False)
        spin(0.1)
        self.p.resume()                                # stopped and unplayed since the seek: MCI play
        spin(0.2)
        self.p.stop_close()
        self.wait_event(("ended", 23, "stopped"))
        self.assertEqual({thread for thread, _ in calls}, {"player"})
        verbs = [command.split()[0] for _, command in calls if not command.startswith("status")]
        self.assertEqual(verbs, ["open", "set", "setaudio", "seek", "play", "pause", "resume",
                                 "pause", "seek", "play", "stop", "close"])
        alias = calls[0][1].split()[-1]                # open "<path>" type mpegvideo alias ra_N
        self.assertEqual([c for _, c in calls if c.startswith("setaudio")], [f"setaudio {alias} volume to 0"])

    def test_safety_clock_counts_only_playing_time(self):
        with mock.patch.object(player, "SAFETY_MS", 450 - self.length):   # ends after ~450 ms of playing
            self.p.load(24, self.mp3)
            self.wait_event(("started", 24, self.length))
            spin(0.2)
            self.p.pause()
            spin(1.0)                                  # paused time must not count
            self.assertEqual([e for e in self.events if e[0] == "ended"], [])
            self.p.resume()
            t0 = time.monotonic()
            t1 = self.wait_event(("ended", 24, "end"))
        self.assertTrue(0.1 <= t1 - t0 <= 0.6, t1 - t0)
        self.assertLess(self.last_tick(24)[2], self.length - 1500)   # the clock ended it, not the file

    def test_mci_error_while_playing_emits_failed_and_recovers(self):
        self.p.load(25, self.mp3)
        self.wait_event(("started", 25, self.length))
        alias = self.alias()
        with self.assertLogs("saylight.player", "WARNING"):
            self.p._run_on_thread(lambda: player._mci_quiet(f"close {alias}"))   # the device vanishes
            self.assertTrue(spin_until(lambda: any(e[0] == "failed" for e in self.events), 2))
        failures = [e for e in self.events if e[0] == "failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][1], 25)
        self.assertTrue(failures[0][2].startswith("MCI error"), failures[0][2])
        self.assertEqual([e for e in self.events if e[0] == "ended"], [])
        n = len(self.ticks)
        spin(0.2)
        self.assertEqual(len(self.ticks), n)           # nothing loaded any more
        self.p.load(26, self.mp3, play=False)
        self.assertTrue(self.fresh_ticks(26))

    def test_unexpected_error_before_open_still_reports_failed(self):
        with mock.patch.object(player, "_mci_path", side_effect=OSError("path lookup failed")), \
                self.assertLogs("saylight.player", "WARNING"):
            self.p.load(27, self.mp3)
            self.assertTrue(spin_until(lambda: self.events, 2))
        self.assertEqual(self.events, [("failed", 27, "path lookup failed")])
        self.p.load(28, self.mp3, play=False)
        self.assertTrue(self.fresh_ticks(28))

    def test_cut_set_just_ahead_is_polled_at_once(self):
        self.p.load(29, self.mp3)
        self.wait_event(("started", 29, self.length))
        spin(0.5)
        with self.assertLogs("saylight.player", "DEBUG") as logs:
            pos = int(self.mci(f"status {self.alias()} position"))
            self.p.set_cut(pos + 5)
            self.wait_event(("ended", 29, "cut"))
        closed = [r.getMessage() for r in logs.records if "closed (cut)" in r.getMessage()]
        at = int(re.search(r" at (\d+) of ", closed[0]).group(1))
        self.assertLess(at - (pos + 5), 20)            # not left waiting for the scheduled 50 ms poll

    def test_signals_arrive_on_the_gui_thread(self):
        threads = set()
        for signal in (self.p.started, self.p.tick, self.p.ended, self.p.failed):
            signal.connect(lambda *a: threads.add(threading.current_thread()))
        self.p.load(30, self.mp3, cut_ms=300)
        self.wait_event(("ended", 30, "cut"))
        with self.assertLogs("saylight.player", "WARNING"):
            self.p.load(31, os.path.join(self.dir, "missing.mp3"))
            self.assertTrue(spin_until(lambda: any(e[0] == "failed" for e in self.events), 2))
        self.assertEqual({e[0] for e in self.events}, {"started", "ended", "failed"})
        self.assertEqual(threads, {threading.main_thread()})

    def test_shutdown_closes_silently(self):
        self.p.load(21, self.mp3)
        self.wait_event(("started", 21, self.length))
        self.p.shutdown()
        self.assertFalse(self.p._thread.is_alive())
        spin(0.2)
        self.assertEqual([e for e in self.events if e[0] == "ended"], [])


if __name__ == "__main__":
    unittest.main()

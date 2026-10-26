"""Tests for voices/router.py: SynthRouter, the engine's synth.

Fake backends stand in for the voices; the real AudioCache works in a temporary root. The 1.0 synth tests
also run through the router (test_synth.py), and an unchanged Engine is driven through it here. No network,
no audio.
"""

import os
import shutil
import tempfile
import unittest
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication, QObject, Signal

from .. import engine as E
from ..audio_cache import AudioCache
from ..voices import base, make_backend, make_router
from ..voices.base import VoiceInfo, Word
from ..voices.edge import EdgeBackend
from ..voices.router import DEFAULT_LIMITS, SynthRouter, as_speed
from ..voices.windows import WindowsVoiceBackend
from . import test_engine as TE


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def drain() -> None:
    """Deliver queued signals."""
    for _ in range(3):
        QCoreApplication.processEvents()


@dataclass
class Request:
    gen: int
    chunk_id: int
    text: str
    voice: str
    speed: float


class FakeBackend(QObject):
    """A VoiceBackend that records requests; finish() and fail() deliver results like a real one."""

    chunkReady = Signal(int, int, str, object)
    chunkFailed = Signal(int, int, str, str)

    def __init__(self, kind: str = "edge", *, ready: bool = True, limits=(250, 1500), voices=()):
        super().__init__()
        self.kind = kind
        self.audio_ext = ".mp3" if kind == "edge" else ".wav"
        self.is_ready, self.limits, self.voice_list = ready, limits, list(voices)
        self.requests: list[Request] = []
        self.cancelled: list[int | None] = []
        self.cache: AudioCache | None = None
        self.warmed = 0
        self.shut = False

    def start(self, cache):
        self.cache = cache

    def ready(self):
        return self.is_ready

    def voices(self):
        return list(self.voice_list)

    def chunk_limits(self):
        return self.limits

    def synthesize(self, gen, chunk_id, text, voice, speed):
        self.requests.append(Request(gen, chunk_id, text, voice, speed))

    def cancel_before(self, gen):
        self.cancelled.append(gen)

    def cancel_all(self):
        self.cancelled.append(None)

    def warm(self):
        self.warmed += 1

    def shutdown(self, timeout=2.0):
        self.shut = True

    def finish(self, i: int = -1, words=None) -> str:
        r = self.requests[i]
        path = self.cache.new_path(f"g{r.gen}_c{r.chunk_id}", self.audio_ext)
        with self.cache.create(path) as f:
            f.write(b"\0" * 16)
        self.chunkReady.emit(r.gen, r.chunk_id, path, [Word(100, 200, "x")] if words is None else words)
        return path

    def fail(self, i: int = -1, kind: str = "offline") -> None:
        r = self.requests[i]
        self.chunkFailed.emit(r.gen, r.chunk_id, kind, "ClientConnectorDNSError: no host")


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key):
        return self.values[key]       # unknown keys raise KeyError, like Settings


class RouterTestBase(unittest.TestCase):
    def setUp(self):
        _app()
        self.root = tempfile.mkdtemp(prefix="router_test_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cache = AudioCache(self.root)
        self.edge = FakeBackend("edge")

    def router(self, *extra: FakeBackend, settings=None) -> SynthRouter:
        backends = {"edge": self.edge, **{b.kind: b for b in extra}}
        r = SynthRouter(backends, self.cache, settings=settings)
        self.addCleanup(r.shutdown)
        self.ready, self.failed, self.changes = [], [], []
        r.chunkReady.connect(lambda *a: self.ready.append(a))
        r.chunkFailed.connect(lambda *a: self.failed.append(a))
        r.backendChanged.connect(lambda *a: self.changes.append(a))
        r.start()
        return r


class SpeedTests(unittest.TestCase):
    def test_as_speed(self):
        cases = {"-5%": 0.95, "+25%": 1.25, "+0%": 1.0, "-50%": 0.5, "+100%": 2.0, " +5% ": 1.05, 0.95: 0.95, 2: 2.0}
        for rate, speed in cases.items():
            with self.subTest(rate=rate):
                self.assertEqual(as_speed(rate), speed)
        for bad in ("fast", "5", "%", "+5%%", True, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                as_speed(bad)

    def test_rate_strings_round_trip_through_the_edge_backend(self):
        from ..voices.edge import rate_string
        for pct in range(-50, 101):
            rate = f"{pct:+d}%"
            self.assertEqual(rate_string(as_speed(rate)), rate)


class RoutingTests(RouterTestBase):
    def test_start_shares_one_cache(self):
        local = FakeBackend("windows")
        r = self.router(local)
        self.assertIsNotNone(r.temp_dir)
        self.assertEqual(r.temp_dir, self.cache.dir)
        self.assertIs(self.edge.cache, self.cache)
        self.assertIs(local.cache, self.cache)
        self.assertIs(r.backend("windows"), local)
        self.assertIsNone(r.backend("future"))

    def test_voices_route_by_id(self):
        local = FakeBackend("windows")
        r = self.router(local)
        r.synthesize(1, 0, "one", "en-US-AvaNeural", "-5%")
        r.synthesize(1, 1, "two", "edge:en-GB-SoniaNeural", 1.25)
        r.synthesize(1, 2, "three", "windows:David", 0.9)
        self.assertEqual(self.edge.requests, [Request(1, 0, "one", "edge:en-US-AvaNeural", 0.95),
                                              Request(1, 1, "two", "edge:en-GB-SoniaNeural", 1.25)])
        self.assertEqual(local.requests, [Request(1, 2, "three", "windows:David", 0.9)])

    def test_results_pass_through(self):
        r = self.router()
        r.synthesize(3, 7, "text", "en-US-AvaNeural", "-5%")
        path = self.edge.finish()
        self.assertEqual(self.ready, [(3, 7, path, [Word(100, 200, "x")])])
        r.synthesize(3, 8, "text", "en-US-AvaNeural", "-5%")
        self.edge.fail(kind="service")
        self.assertEqual(self.failed, [(3, 8, "service", "ClientConnectorDNSError: no host")])
        self.assertEqual(r._jobs, {})

    def test_a_result_for_a_job_the_router_never_saw_still_passes(self):
        self.router()
        self.edge.requests.append(Request(1, 5, "", "", 1.0))   # as Saylight 1.0 would, nothing is dropped
        path = self.edge.finish()
        self.assertEqual([a[:3] for a in self.ready], [(1, 5, path)])

    def test_missing_or_unready_backend_fails_unavailable_asynchronously(self):
        r = self.router(FakeBackend("windows", ready=False))
        with self.assertLogs("saylight.voices.router", "WARNING"):
            r.synthesize(1, 0, "secret words", "future:Voice", 1.0)
            r.synthesize(1, 1, "secret words", "windows:David", 1.0)
        self.assertEqual(self.failed, [])                 # never from inside synthesize()
        drain()
        self.assertEqual(self.failed, [(1, 0, "unavailable", "no future voice is available"),
                                       (1, 1, "unavailable", "no windows voice is available")])
        self.assertEqual(self.edge.requests, [])

    def test_cancel_wins_over_an_undelivered_router_failure(self):
        r = self.router()
        with self.assertLogs("saylight.voices.router", "WARNING"):
            r.synthesize(1, 0, "x", "future:x", 1.0)
            r.synthesize(2, 1, "x", "future:x", 1.0)
        r.cancel_before(2)
        drain()
        self.assertEqual([f[:3] for f in self.failed], [(2, 1, "unavailable")])
        with self.assertLogs("saylight.voices.router", "WARNING"):
            r.synthesize(3, 2, "x", "future:x", 1.0)
        r.cancel_all()
        drain()
        self.assertEqual(len(self.failed), 1)

    def test_an_invalid_rate_fails_as_other(self):
        r = self.router()
        with self.assertLogs("saylight.voices.router", "WARNING"):
            r.synthesize(1, 0, "x", "en-US-AvaNeural", "fast")
        drain()
        self.assertEqual(self.failed, [(1, 0, "other", "ValueError: invalid rate")])

    def test_cancel_and_delete_reach_every_backend_and_the_cache(self):
        local = FakeBackend("windows")
        r = self.router(local)
        r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
        r.synthesize(2, 1, "b", "en-US-AvaNeural", 1.0)
        r.cancel_before(2)
        self.assertEqual((self.edge.cancelled, local.cancelled), ([2], [2]))
        self.assertEqual(list(r._jobs), [(2, 1)])
        r.cancel_all()
        self.assertEqual((self.edge.cancelled, local.cancelled), ([2, None], [2, None]))
        self.assertEqual(r._jobs, {})
        r.synthesize(3, 2, "c", "en-US-AvaNeural", 1.0)
        path = self.edge.finish()
        self.assertTrue(os.path.isfile(path))
        r.delete(path)
        self.assertFalse(os.path.exists(path))
        outside = os.path.join(self.root, "keep.mp3")
        open(outside, "wb").close()
        with self.assertLogs("saylight.audio_cache", "WARNING"):
            r.delete(outside)
        self.assertTrue(os.path.exists(outside))

    def test_shutdown_stops_backends_and_removes_the_folder(self):
        local = FakeBackend("windows")
        r = self.router(local)
        folder = r.temp_dir
        r.shutdown()
        self.assertTrue(self.edge.shut and local.shut)
        self.assertFalse(os.path.exists(folder))
        r.synthesize(1, 0, "x", "future:x", 1.0)      # ignored, no failure either
        drain()
        self.assertEqual((self.edge.requests, self.failed), ([], []))
        r.shutdown()

    def test_limits_and_voices(self):
        ava = VoiceInfo("edge:en-US-AvaNeural", "edge", "Ava", "en-US", "Female", False)
        david = VoiceInfo("windows:David", "windows", "David", "en-US", "Male", True, detail="Windows voice · offline")
        self.edge.voice_list = [ava]
        r = self.router(FakeBackend("windows", limits=(150, 600), voices=[david]))
        self.assertEqual(r.limits_for("en-US-AvaNeural"), (250, 1500))
        self.assertEqual(r.limits_for("windows:David"), (150, 600))
        self.assertEqual(r.limits_for("future:x"), DEFAULT_LIMITS)
        self.assertEqual(r.voices(), [ava, david])

    def test_backend_changes_are_announced_once_per_change(self):
        r = self.router(FakeBackend("windows"))
        with self.assertLogs("saylight.voices.router", "INFO"):
            r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
            r.synthesize(1, 1, "b", "en-US-AvaNeural", 1.0)
            r.synthesize(1, 2, "c", "windows:David", 1.0)
            r.synthesize(1, 3, "d", "en-US-AvaNeural", 1.0)
        self.assertEqual(self.changes, [("edge", "chosen"), ("windows", "chosen"), ("edge", "chosen")])


class FallbackTests(RouterTestBase):
    """With an offline backend present, online failures move to it for the rest of the reading."""

    def test_no_offline_backend_means_failures_pass_through(self):
        r = self.router()
        r.set_reachable(False)
        r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
        self.assertEqual(len(self.edge.requests), 1)
        self.edge.fail(kind="offline")
        self.assertEqual([f[:3] for f in self.failed], [(1, 0, "offline")])

    def test_offline_failure_resends_the_job_and_sticks_for_the_reading(self):
        zira = VoiceInfo("windows:Zira", "windows", "Zira", "en-US", "Female", True)
        windows = FakeBackend("windows", limits=(150, 600), voices=[zira])
        r = self.router(windows)
        with self.assertLogs("saylight.voices.router", "INFO"):
            r.synthesize(4, 0, "first chunk", "en-US-AvaNeural", "-5%")
            self.edge.fail(kind="offline")
        self.assertEqual(self.failed, [])
        self.assertEqual(windows.requests, [Request(4, 0, "first chunk", "windows:Zira", 0.95)])
        self.assertEqual(self.changes, [("edge", "chosen"), ("windows", "offline_fallback")])
        path = windows.finish()
        self.assertEqual([a[:3] for a in self.ready], [(4, 0, path)])
        r.synthesize(4, 1, "second chunk", "en-US-AvaNeural", "-5%")       # sticky: straight to Windows
        self.assertEqual(len(self.edge.requests), 1)
        self.assertEqual(windows.requests[-1], Request(4, 1, "second chunk", "windows:Zira", 0.95))
        self.assertEqual(r.limits_for("en-US-AvaNeural"), windows.limits)
        with self.assertLogs("saylight.voices.router", "INFO"):
            r.new_reading()
            r.synthesize(5, 2, "next reading", "en-US-AvaNeural", "-5%")
        self.assertEqual(self.edge.requests[-1].chunk_id, 2)
        self.assertEqual(self.changes[-1], ("edge", "chosen"))

    def test_service_errors_fall_back_and_other_errors_do_not(self):
        windows = FakeBackend("windows")
        r = self.router(windows)
        r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
        r.synthesize(1, 1, "b", "en-US-AvaNeural", 1.0)
        self.edge.fail(0, kind="other")
        self.assertEqual([f[:3] for f in self.failed], [(1, 0, "other")])
        with self.assertLogs("saylight.voices.router", "INFO"):
            self.edge.fail(1, kind="service")
        self.assertEqual(windows.requests[0].chunk_id, 1)
        self.assertEqual(windows.requests[0].voice, "windows:")
        self.assertEqual(self.changes[-1], ("windows", "service_fallback"))

    def test_a_late_online_result_after_the_move_is_dropped_and_deleted(self):
        windows = FakeBackend("windows")
        r = self.router(windows)
        r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
        with self.assertLogs("saylight.voices.router", "INFO"):
            self.edge.fail(kind="offline")
        late = self.edge.finish()
        self.assertEqual(self.ready, [])
        self.assertFalse(os.path.exists(late))
        self.edge.fail(kind="offline")                                     # a late failure too
        self.assertEqual(self.failed, [])
        windows.finish()
        self.assertEqual(len(self.ready), 1)

    def test_fallback_can_be_switched_off(self):
        windows = FakeBackend("windows")
        r = self.router(windows, settings=FakeSettings(**{"offline.fallback": False}))
        r.set_reachable(False)
        r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
        self.edge.fail(kind="offline")
        self.assertEqual([f[:3] for f in self.failed], [(1, 0, "offline")])
        self.assertEqual((windows.requests, windows.warmed), ([], 0))

    def test_unknown_settings_keys_use_the_defaults(self):
        windows = FakeBackend("windows")
        r = self.router(windows, settings=FakeSettings())
        r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
        with self.assertLogs("saylight.voices.router", "INFO"):
            self.edge.fail(kind="offline")
        self.assertEqual(len(windows.requests), 1)

    def test_no_network_goes_offline_first_and_warms_it(self):
        windows = FakeBackend("windows", limits=(150, 600))
        r = self.router(windows)
        r.set_reachable(False)
        self.assertEqual(windows.warmed, 1)
        self.assertEqual(r.limits_for("en-US-AvaNeural"), windows.limits)   # no side effect
        self.assertEqual(self.changes, [])
        with self.assertLogs("saylight.voices.router", "INFO"):
            r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
        self.assertEqual((len(self.edge.requests), len(windows.requests)), (0, 1))
        self.assertEqual(self.changes, [("windows", "offline_fallback")])
        r.set_reachable(True)
        r.synthesize(1, 1, "b", "en-US-AvaNeural", 1.0)                     # still this reading
        self.assertEqual(len(windows.requests), 2)

    def test_private_only_and_the_chosen_offline_voice(self):
        windows = FakeBackend("windows")
        settings = FakeSettings(**{"offline.private_only": True, "offline.voice": "windows:Mark"})
        r = self.router(windows, settings=settings)
        with self.assertLogs("saylight.voices.router", "INFO"):
            r.synthesize(1, 0, "a", "en-US-AvaNeural", 1.0)
        self.assertEqual(windows.requests[0].voice, "windows:Mark")
        self.assertEqual(self.edge.requests, [])
        self.assertEqual(self.changes, [("windows", "chosen")])        # the person's choice, not "no internet"

    def test_private_only_never_sends_the_text_online(self):
        settings = FakeSettings(**{"offline.private_only": True})
        for extra in ((), (FakeBackend("windows", ready=False),)):
            with self.subTest(offline=[b.kind for b in extra]):
                self.edge = FakeBackend("edge")
                self.cache = AudioCache(self.root)
                r = self.router(*extra, settings=settings)
                with self.assertLogs("saylight.voices.router", "WARNING") as logs:
                    r.synthesize(1, 0, "private words", "en-US-AvaNeural", 1.0)
                drain()
                self.assertEqual(self.edge.requests, [])
                self.assertEqual(self.failed, [(1, 0, "unavailable", "no private voice is available")])
                self.assertEqual(r.limits_for("en-US-AvaNeural"), DEFAULT_LIMITS)
                self.assertFalse(any("private words" in rec.getMessage() for rec in logs.records))
                r.set_reachable(False)
                with self.assertLogs("saylight.voices.router", "WARNING"):
                    r.synthesize(1, 1, "more words", "en-US-AvaNeural", 1.0)
                drain()
                self.assertEqual(self.edge.requests, [])


class EngineThroughRouterTests(unittest.TestCase):
    """The engine with the router as its synth: speeds as numbers, chunk sizes from the voice's backend, and
    each new reading back on the chosen voice."""

    def engine(self, router: SynthRouter):
        clock, player, inp = TE.FakeClock(), TE.FakePlayer(), TE.FakeInput()
        eng = E.Engine(synth=router, player=player, input_thread=inp, settings=TE.FakeSettings(), clock=clock,
                       timer_factory=clock.timer, beep=lambda *_: None)
        self.addCleanup(eng.shutdown)
        eng.start()
        return eng, player, inp

    def test_a_reading_runs_through_the_router(self):
        _app()
        root = tempfile.mkdtemp(prefix="router_engine_")
        self.addCleanup(shutil.rmtree, root, True)
        backend = FakeBackend("edge", limits=(130, 300))
        router = SynthRouter({"edge": backend}, AudioCache(root))
        eng, player, inp = self.engine(router)
        folder = router.temp_dir
        self.assertTrue(os.path.isdir(folder))
        self.assertIs(backend.cache, router.cache)

        inp.hotkeyPressed.emit(E.HK_READ)
        inp.copied.emit(0, TE.make_text(12), "ok")
        self.assertEqual(eng.state, E.PREPARING)
        first = backend.requests[0]
        self.assertEqual((first.voice, first.speed), ("edge:en-US-AvaNeural", 0.95))
        self.assertEqual(first.text, " ".join(TE.sentence(i) for i in range(2)))         # up to 130 characters
        self.assertEqual(backend.requests[1].text, " ".join(TE.sentence(i) for i in range(2, 7)))  # up to 300
        path = backend.finish(0, words=TE.make_words(first.text))
        load = [c for c in player.cmds if c[0] == "load"][-1]
        self.assertEqual(load[1:3], (first.chunk_id, path))
        player.started.emit(first.chunk_id, TE.chunk_length(first.text))
        self.assertEqual(eng.state, E.READING)

        eng.stop()                                       # a new generation: the router passes the cancel on
        self.assertGreater(backend.cancelled[-1], first.gen)
        eng.shutdown()
        self.assertTrue(backend.shut)
        self.assertFalse(os.path.exists(folder))

    def test_a_new_reading_goes_back_to_the_online_voice(self):
        _app()
        root = tempfile.mkdtemp(prefix="router_engine_")
        self.addCleanup(shutil.rmtree, root, True)
        edge, windows = FakeBackend("edge"), FakeBackend("windows", limits=(130, 300))
        router = SynthRouter({"edge": edge, "windows": windows}, AudioCache(root))
        eng, _player, inp = self.engine(router)
        inp.hotkeyPressed.emit(E.HK_READ)
        inp.copied.emit(0, TE.make_text(12), "ok")
        with self.assertLogs("saylight.voices.router", "INFO"):
            edge.fail(0, kind="offline")
        self.assertEqual(len(windows.requests), 1)                  # the rest of this reading stays offline
        self.assertEqual(router.limits_for("edge:en-US-AvaNeural"), (130, 300))
        self.assertTrue(eng.read_text(TE.make_text(3)))
        self.assertEqual(edge.requests[-1].text, TE.make_text(3))   # a new reading tries the online voice again
        self.assertEqual(len(windows.requests), 1)


class FactoryTests(unittest.TestCase):
    def test_make_backend_and_make_router(self):
        _app()
        edge = make_backend("edge")
        self.assertIsInstance(edge, EdgeBackend)
        self.assertIsInstance(edge, base.VoiceBackend)
        edge.shutdown()
        windows = make_backend("windows")
        self.assertIsInstance(windows, WindowsVoiceBackend)
        self.assertIsInstance(windows, base.VoiceBackend)
        windows.shutdown()
        root = tempfile.mkdtemp(prefix="router_factory_")
        self.addCleanup(shutil.rmtree, root, True)
        router = make_router(temp_root=root)
        self.assertIsInstance(router.backend("edge"), EdgeBackend)
        self.assertIsInstance(router.backend("windows"), WindowsVoiceBackend)
        self.assertIsNone(router.temp_dir)                # nothing is created before start()
        router.start()
        self.assertEqual(os.path.dirname(router.temp_dir), root)
        router.shutdown()
        self.assertEqual(os.listdir(root), [])


if __name__ == "__main__":
    unittest.main()

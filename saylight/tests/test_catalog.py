"""Tests for the voice page's helpers: voices/catalog.py (the voice list) and voices/preview.py (samples).

No network: the online list is faked. The word-timing check of every built-in voice sends one short fixed
sentence per voice to Microsoft, so it runs only with SAYLIGHT_VOICE_CHECK=1 (CI). Previews play silent
WAV files at MCI volume 0, read back from the device, so nothing is audible.
"""

import io
import os
import shutil
import tempfile
import threading
import time
import unittest
import wave
from dataclasses import dataclass
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Signal

from .. import player as player_mod
from ..audio_cache import AudioCache
from ..player import PlayerThread
from ..voices import preview
from ..voices.base import DEFAULT_VOICE_ID, VoiceInfo, Word, parse_voice_id
from ..voices.catalog import CURATED, LOCALES, WINDOWS_DETAIL, VoiceCatalog, display_name, parse_online_list
from ..voices.edge import EdgeBackend
from ..voices.preview import SAMPLE_TEXT, VoicePreview
from . import test_engine as TE


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def spin_until(pred, timeout: float) -> bool:
    app, end = _app(), time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        if pred():
            return True
        time.sleep(0.002)
    QCoreApplication.processEvents()
    return pred()


def entry(short: str, gender: str = "Female", status: str = "GA", locale_name: str | None = None) -> dict:
    locale = "-".join(short.split("-")[:2])
    return {"ShortName": short, "Locale": locale, "Gender": gender, "Status": status,
            "LocaleName": locale_name or f"Language ({locale})", "Name": "x", "FriendlyName": "x"}


# --- Catalog -------------------------------------------------------------------------------------

class CuratedListTests(unittest.TestCase):
    def test_built_in_english_list(self):
        self.assertEqual(len(CURATED), 35)
        self.assertEqual(CURATED[0].id, DEFAULT_VOICE_ID)                 # Ava stays the default
        self.assertEqual(CURATED[0].name, "Ava")
        self.assertEqual(len({v.id for v in CURATED}), len(CURATED))
        self.assertEqual({v.locale for v in CURATED}, set(LOCALES))
        for v in CURATED:
            with self.subTest(voice=v.id):
                self.assertEqual(parse_voice_id(v.id), ("edge", v.id[len("edge:"):]))
                self.assertEqual(v.backend, "edge")
                self.assertTrue(v.id[len("edge:"):].startswith(v.locale + "-"))
                self.assertIn(v.gender, ("Female", "Male"))
                self.assertFalse(v.private)
                self.assertTrue(v.word_timing)
                self.assertEqual(v.detail, "Natural · online")
                self.assertTrue(v.name and v.name[0].isupper())

    def test_display_names(self):
        cases = {"en-US-AvaNeural": "Ava", "en-US-AvaMultilingualNeural": "Ava Multilingual",
                 "en-IN-NeerjaExpressiveNeural": "Neerja Expressive", "fil-PH-AngeloNeural": "Angelo",
                 "zh-CN-liaoning-XiaobeiNeural": "Xiaobei (liaoning)", "en-GB-RyanNeural": "Ryan"}
        for short, name in cases.items():
            with self.subTest(short=short):
                self.assertEqual(display_name(short), name)

    def test_the_edge_backend_lists_the_built_in_voices(self):
        _app()
        backend = EdgeBackend()
        self.assertEqual(backend.voices(), list(CURATED))
        backend.shutdown()


class OnlineListTests(unittest.TestCase):
    def test_parse_online_list(self):
        entries = [entry("fr-FR-DeniseNeural", locale_name="French (France)"),
                   entry("en-US-AvaNeural", locale_name="English (United States)"),
                   entry("de-DE-KatjaNeural", status="Deprecated"),
                   entry("bad name"), {"ShortName": 5}, "junk", entry("ja-JP-NanamiNeural", gender="Unknown"),
                   entry("fr-FR-DeniseNeural")]
        voices, names = parse_online_list(entries)
        self.assertEqual([v.id for v in voices], ["edge:en-US-AvaNeural", "edge:fr-FR-DeniseNeural",
                                                  "edge:ja-JP-NanamiNeural"])
        self.assertIs(voices[0], CURATED[0])                                 # built-in entries are kept
        self.assertEqual(voices[1], VoiceInfo("edge:fr-FR-DeniseNeural", "edge", "Denise", "fr-FR", "Female",
                                              False, True, "Natural · online"))
        self.assertEqual(voices[2].gender, "")
        self.assertEqual(names["fr-FR"], "French (France)")


class FetchStub:
    """Stands in for edge.fetch_voice_list; blocks until released so tests can look at "loading"."""

    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls, self.threads = 0, []
        self.go = threading.Event()

    def __call__(self):
        self.calls += 1
        self.threads.append(threading.current_thread())
        self.go.wait(5)
        if self.error is not None:
            raise self.error
        return self.result


class CatalogTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_no_network_until_asked(self):
        fetch = FetchStub([entry("fr-FR-DeniseNeural")])
        cat = VoiceCatalog(fetch=fetch)
        self.assertEqual(cat.status, "builtin")
        self.assertEqual(cat.voices(), list(CURATED))
        self.assertEqual(cat.voices(all_languages=True), list(CURATED))
        self.assertEqual(cat.locale_name("en-GB"), "English (United Kingdom)")
        self.assertEqual(cat.locale_name("fr-FR"), "fr-FR")
        self.assertEqual(fetch.calls, 0)

    def test_refresh_online_loads_every_language_off_the_gui_thread(self):
        fetch = FetchStub([entry("fr-FR-DeniseNeural", locale_name="French (France)"), entry("en-US-AvaNeural")])
        cat = VoiceCatalog(fetch=fetch)
        changes = []
        cat.changed.connect(lambda: changes.append(cat.status))
        cat.refresh_online()
        cat.refresh_online()                                                  # one request at a time
        self.assertEqual(cat.status, "loading")
        fetch.go.set()
        with self.assertLogs("saylight.voices.catalog", "INFO") as logs:
            self.assertTrue(spin_until(lambda: cat.status == "online", 5))
        self.assertEqual(changes, ["loading", "online"])
        self.assertEqual(fetch.calls, 1)
        self.assertIsNot(fetch.threads[0], threading.main_thread())
        self.assertEqual([v.id for v in cat.voices(all_languages=True)], ["edge:en-US-AvaNeural",
                                                                          "edge:fr-FR-DeniseNeural"])
        self.assertEqual(cat.voices(), list(CURATED))                         # the English page stays as it was
        self.assertEqual(cat.locale_name("fr-FR"), "French (France)")
        self.assertEqual(cat.find("edge:fr-FR-DeniseNeural").name, "Denise")
        self.assertIsNone(cat.find("edge:xx-XX-NoneNeural"))
        self.assertEqual(logs.records[0].getMessage(), "Loaded 2 online voices in 2 languages")
        cat.refresh_online()                                                  # loaded: nothing more is fetched
        self.assertEqual(fetch.calls, 1)

    def test_a_failed_fetch_shows_the_built_in_list_and_can_retry(self):
        fetch = FetchStub(error=OSError("no network"))
        cat = VoiceCatalog(fetch=fetch)
        fetch.go.set()
        with self.assertLogs("saylight.voices.catalog", "WARNING") as logs:
            cat.refresh_online()
            self.assertTrue(spin_until(lambda: cat.status == "failed", 5))
        self.assertIn("OSError", logs.records[0].getMessage())
        self.assertEqual(cat.voices(all_languages=True), list(CURATED))
        fetch.error, fetch.result = None, [entry("fr-FR-DeniseNeural")]
        cat.refresh_online()
        with self.assertLogs("saylight.voices.catalog", "INFO"):
            self.assertTrue(spin_until(lambda: cat.status == "online", 5))
        self.assertEqual(fetch.calls, 2)

    def test_an_empty_or_malformed_list_counts_as_failed(self):
        for result in ([], None, {"not": "a list"}, [entry("bad name")]):
            with self.subTest(result=result):
                fetch = FetchStub(result)
                fetch.go.set()
                cat = VoiceCatalog(fetch=fetch)
                with self.assertLogs("saylight.voices.catalog", "WARNING"):
                    cat.refresh_online()
                    self.assertTrue(spin_until(lambda: cat.status == "failed", 5))

    def test_local_voices_come_after_the_online_ones(self):
        david = VoiceInfo("windows:David", "windows", "David", "en-US", "Male", True, detail=WINDOWS_DETAIL)
        cat = VoiceCatalog(local=lambda: [david])
        self.assertEqual(cat.voices()[:-1], list(CURATED))
        self.assertEqual(cat.voices()[-1], david)
        self.assertEqual(cat.find("windows:David"), david)

    def test_default_fetch_uses_edge_tts_list_voices(self):
        calls = []

        async def fake_list_voices():
            calls.append(threading.current_thread())
            return [entry("fr-FR-DeniseNeural")]

        cat = VoiceCatalog()
        with mock.patch("edge_tts.list_voices", fake_list_voices), \
                self.assertLogs("saylight.voices.catalog", "INFO"):
            cat.refresh_online()
            self.assertTrue(spin_until(lambda: cat.status == "online", 5))
        self.assertEqual(len(calls), 1)
        self.assertIsNot(calls[0], threading.main_thread())


@unittest.skipUnless(os.environ.get("SAYLIGHT_VOICE_CHECK") == "1", "set SAYLIGHT_VOICE_CHECK=1 to check every voice")
class WordTimingCheck(unittest.TestCase):
    """Each built-in voice must send word boundaries; one without them must be marked word_timing=False."""

    def test_every_built_in_voice_sends_word_boundaries(self):
        _app()
        root = tempfile.mkdtemp(prefix="voice_check_")
        self.addCleanup(shutil.rmtree, root, True)
        backend = EdgeBackend(temp_root=root)
        self.addCleanup(backend.shutdown)
        ready, failed = {}, {}
        backend.chunkReady.connect(lambda g, c, p, w: ready.__setitem__(c, w))
        backend.chunkFailed.connect(lambda g, c, k, d: failed.__setitem__(c, (k, d)))
        backend.start()
        for i, voice in enumerate(CURATED):
            backend.synthesize(1, i, "Hello there.", voice.id, 1.0)
        self.assertTrue(spin_until(lambda: len(ready) + len(failed) == len(CURATED), 120))
        for i, voice in enumerate(CURATED):
            with self.subTest(voice=voice.id):
                self.assertNotIn(i, failed)
                self.assertEqual(bool(ready[i]), voice.word_timing)


# --- Preview -------------------------------------------------------------------------------------

@dataclass
class Job:
    gen: int
    chunk_id: int
    text: str
    voice: str
    speed: float


def silent_wav(ms: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * (16 * ms))
    return buf.getvalue()


class WavBackend(QObject):
    """A local-style backend that writes silent WAV files."""

    chunkReady = Signal(int, int, str, object)
    chunkFailed = Signal(int, int, str, str)
    kind, audio_ext = "edge", ".wav"

    def __init__(self, *, ready: bool = True):
        super().__init__()
        self.jobs: list[Job] = []
        self.cancels = 0
        self.cache = None
        self.is_ready = ready
        self.shut = False

    def start(self, cache):
        self.cache = cache

    def ready(self):
        return self.is_ready

    def voices(self):
        return []

    def chunk_limits(self):
        return 250, 1500

    def synthesize(self, gen, chunk_id, text, voice, speed):
        self.jobs.append(Job(gen, chunk_id, text, voice, speed))

    def cancel_before(self, gen):
        self.cancels += 1

    def cancel_all(self):
        self.cancels += 1

    def warm(self):
        pass

    def shutdown(self, timeout=2.0):
        self.shut = True

    def finish(self, i: int = -1, ms: int = 300) -> str:
        job = self.jobs[i]
        path = self.cache.new_path(f"p{job.gen}", ".wav")
        with self.cache.create(path) as f:
            f.write(silent_wav(ms))
        self.chunkReady.emit(job.gen, job.chunk_id, path, [Word(0, 100, "This")])
        return path

    def fail(self, i: int = -1, kind: str = "offline"):
        job = self.jobs[i]
        self.chunkFailed.emit(job.gen, job.chunk_id, kind, "ClientConnectorDNSError")


class PreviewTestBase(unittest.TestCase):
    def setUp(self):
        _app()
        self.root = tempfile.mkdtemp(prefix="preview_test_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cache = AudioCache(self.root)
        self.addCleanup(self.cache.shutdown)
        self.backend = WavBackend()

    def preview(self, *, player=None, volume=lambda: 0, factory=None, cache="shared") -> VoicePreview:
        made = factory or (lambda kind: self.backend if kind == "edge" else None)
        p = VoicePreview(made, volume, cache=self.cache if cache == "shared" else cache, player=player)
        self.addCleanup(p.shutdown)
        self.states = []
        p.stateChanged.connect(self.states.append)
        return p

    def chunk_files(self) -> list[str]:
        return sorted(n for n in os.listdir(self.cache.dir) if n.endswith(".wav"))


class PreviewWithFakePlayerTests(PreviewTestBase):
    def setUp(self):
        super().setUp()
        self.player = TE.FakePlayer()

    def test_reads_the_sample_text_and_deletes_the_file_after_playing(self):
        p = self.preview(player=self.player, volume=lambda: 35)
        p.play("edge:en-GB-SoniaNeural", 1.1)
        self.assertEqual(self.states, ["loading"])
        self.assertEqual(self.backend.jobs, [Job(1, 0, SAMPLE_TEXT, "edge:en-GB-SoniaNeural", 1.1)])
        self.assertIs(self.backend.cache, self.cache)
        path = self.backend.finish()
        self.assertEqual(self.player.cmds[-2:], [("set_volume", 35), ("load", 1, path, 0, True, None)])
        self.assertTrue(self.player.running)
        self.player.started.emit(1, 300)
        self.assertEqual(p.state, "playing")
        self.player.ended.emit(1, "end")
        self.assertEqual(self.states, ["loading", "playing", "idle"])
        self.assertFalse(os.path.exists(path))

    def test_stop_while_loading_drops_the_late_sample(self):
        p = self.preview(player=self.player)
        p.play(DEFAULT_VOICE_ID, 1.0)
        p.stop()
        self.assertEqual(self.states, ["loading", "idle"])
        self.assertGreaterEqual(self.backend.cancels, 1)
        path = self.backend.finish()
        self.assertFalse(os.path.exists(path))
        self.assertEqual([c for c in self.player.cmds if c[0] == "load"], [])

    def test_stop_while_playing_closes_the_device_and_deletes_the_file(self):
        p = self.preview(player=self.player)
        p.play(DEFAULT_VOICE_ID, 1.0)
        path = self.backend.finish()
        self.player.started.emit(1, 300)
        p.stop()
        self.assertIn(("stop_close",), self.player.cmds)
        self.player.flush()                                                  # ended(1, "stopped")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.states, ["loading", "playing", "idle"])

    def test_a_new_play_replaces_the_last(self):
        p = self.preview(player=self.player)
        p.play(DEFAULT_VOICE_ID, 1.0)
        p.play("edge:en-IE-EmilyNeural", 1.0)
        first = self.backend.finish(0)
        self.assertFalse(os.path.exists(first))
        second = self.backend.finish(1)
        loads = [c for c in self.player.cmds if c[0] == "load"]
        self.assertEqual(loads, [("load", 2, second, 0, True, None)])
        self.backend.fail(0)                                                 # an old failure is ignored
        self.assertEqual(p.state, "loading")

    def test_failures(self):
        p = self.preview(player=self.player)
        with self.assertLogs("saylight.voices.preview", "INFO"):
            p.play(DEFAULT_VOICE_ID, 1.0)
            self.backend.fail()
        self.assertEqual(p.state, "failed")
        with self.assertLogs("saylight.voices.preview", "INFO"):
            p.play(DEFAULT_VOICE_ID, 1.0)
            path = self.backend.finish()
            self.player.failed.emit(p._gen, "MCI error 263")
        self.assertEqual(p.state, "failed")
        self.assertFalse(os.path.exists(path))
        with self.assertLogs("saylight.voices.preview", "INFO"):
            p.play("nosuch:Voice", 1.0)                                       # no such backend in this build
        self.assertEqual(p.state, "failed")
        self.backend.is_ready = False
        with self.assertLogs("saylight.voices.preview", "INFO"):
            p.play(DEFAULT_VOICE_ID, 1.0)
        self.assertEqual(p.state, "failed")

    def test_a_backend_that_cannot_be_made_fails_quietly(self):
        def broken(kind):
            raise RuntimeError("no")

        p = self.preview(player=self.player, factory=broken)
        with self.assertLogs("saylight.voices.preview", "WARNING"):
            p.play(DEFAULT_VOICE_ID, 1.0)
        self.assertEqual(p.state, "failed")

    def test_a_broken_volume_source_means_silence(self):
        def volume():
            raise ValueError("no setting")

        p = self.preview(player=self.player, volume=volume)
        p.play(DEFAULT_VOICE_ID, 1.0)
        self.backend.finish()
        self.assertIn(("set_volume", 0), self.player.cmds)

    def test_own_cache_and_shutdown(self):
        made = []

        def own_cache():
            made.append(AudioCache(self.root, prefix="saylight_preview_test_"))
            return made[-1]

        with mock.patch.object(preview, "AudioCache", own_cache):
            p = self.preview(player=self.player, cache=None)
            p.play(DEFAULT_VOICE_ID, 1.0)
        folder = made[0].dir
        self.assertTrue(os.path.isdir(folder))
        self.assertIs(self.backend.cache, made[0])
        self.backend.finish()
        p.shutdown()
        self.assertTrue(self.backend.shut)
        self.assertFalse(self.player.shut)                                   # an injected player is the caller's
        self.assertFalse(os.path.exists(folder))
        p.play(DEFAULT_VOICE_ID, 1.0)                                        # ignored after shutdown
        self.assertEqual(len(self.backend.jobs), 1)


class PreviewPlaybackTests(PreviewTestBase):
    """Real MCI, silent files, volume 0."""

    def read_volume(self, thread: PlayerThread) -> str:
        return thread._run_on_thread(lambda: player_mod._mci(f"status {thread._dev.alias} volume"))

    def test_sample_plays_silently_and_is_deleted(self):
        p = self.preview()
        p.play(DEFAULT_VOICE_ID, 1.0)
        path = self.backend.finish(ms=300)
        self.assertTrue(spin_until(lambda: "playing" in self.states, 5), self.states)
        self.assertTrue(spin_until(lambda: self.states[-1] == "idle", 5), self.states)
        self.assertTrue(spin_until(lambda: not os.path.exists(path), 3))
        self.assertEqual(self.chunk_files(), [])

    def test_two_devices_play_side_by_side(self):
        reader = PlayerThread(volume=0)
        self.addCleanup(reader.shutdown)
        started = []
        reader.started.connect(lambda cid, ms: started.append(cid))
        reader.start()
        self.cache.start()
        reading = self.cache.new_path("reading", ".wav")
        with self.cache.create(reading) as f:
            f.write(silent_wav(6000))
        reader.load(7, reading, play=True)
        self.assertTrue(spin_until(lambda: started, 5))

        p = self.preview()
        p.play(DEFAULT_VOICE_ID, 1.0)
        sample = self.backend.finish(ms=3000)
        self.assertTrue(spin_until(lambda: p.state == "playing", 5), self.states)
        self.assertEqual(self.read_volume(reader), "0")
        self.assertEqual(self.read_volume(p._player), "0")
        self.assertNotEqual(reader._run_on_thread(lambda: reader._dev.alias),
                            p._player._run_on_thread(lambda: p._player._dev.alias))
        p.stop()
        self.assertTrue(spin_until(lambda: not os.path.exists(sample), 5))
        self.assertEqual(reader._run_on_thread(lambda: reader._dev.state), "playing")   # the reading goes on
        reader.stop_close()


if __name__ == "__main__":
    unittest.main()

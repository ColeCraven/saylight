"""Tests for voices/windows.py: Windows' built-in voices through WinRT's SpeechSynthesizer.

Most tests fake the winrt modules, so they run anywhere. The tests at the end use the real speech API when
this PC has the winrt packages: every word of a few paragraphs must be placed, speed must scale the audio,
and a chunk must play in MCI. That audio is synthesized at volume 0 and played at MCI volume 0, read back
from the device, so nothing is audible.
"""

import importlib
import importlib.util
import io
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import wave
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Signal

from .. import voices
from ..audio_cache import AudioCache
from ..player import PlayerThread
from ..settings import SCHEMA
from ..text_model import Document, map_words, plan_chunks
from ..voices import base, make_router
from ..voices.base import VoiceInfo, Word
from ..voices.catalog import WINDOWS_DETAIL
from ..voices.windows import (CHUNK_CHARS, FIRST_CHUNK_CHARS, WindowsVoiceBackend, cue_words, describe_voice,
                              speaking_rate, voice_key, wav_length_ms)

TOKENS = "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices\\Tokens\\"
ZIRA = "windows:MSTTS_V110_enUS_ZiraM"


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


def spin(seconds: float) -> None:
    spin_until(lambda: False, seconds)


def silent_wav(ms: int, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * (rate * ms // 1000))
    return buf.getvalue()


def utf16_cues(text: str, step_ms: float = 300.0) -> list[tuple[float, str, int, int]]:
    """A cue per word, as WinRT gives them: UTF-16 positions, inclusive end; a number once per digit group."""
    cues = []
    t = 100.0
    for m in re.finditer(r"\S+", text):
        first = len(m.string[:m.start()].encode("utf-16-le")) // 2
        last = first + len(m.group().encode("utf-16-le")) // 2 - 1
        for _ in range(3 if any(ch.isdigit() for ch in m.group()) else 1):
            cues.append((t, m.group().strip(".,"), first, last))
            t += step_ms
    return cues


# --- A fake of the winrt modules the backend loads --------------------------------------------


class FakeWorld:
    """What the fake winrt modules do: the installed voices, a gate that holds synthesis, and errors."""

    def __init__(self, voices=None):
        self.voices = voices if voices is not None else [
            SimpleNamespace(id=TOKENS + "MSTTS_V110_enUS_DavidM", display_name="Microsoft David",
                            language="en-US", gender=0),
            SimpleNamespace(id=TOKENS + "MSTTS_V110_enCA_LindaM", display_name="Microsoft Linda",
                            language="en-CA", gender=1),
            SimpleNamespace(id=TOKENS + "MSTTS_V110_enUS_ZiraM", display_name="Microsoft Zira",
                            language="en-US", gender=1),
            SimpleNamespace(id=TOKENS + "MSTTS_V110_enUS_MarkM", display_name="Microsoft Mark",
                            language="en-US", gender=0),
            SimpleNamespace(id=TOKENS + "MSTTS_V110_frFR_HortenseM", display_name="Microsoft Hortense",
                            language="fr-FR", gender=1),
        ]
        self.default = self.voices[1] if len(self.voices) > 1 else None
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self.error: BaseException | None = None
        self.calls: list[dict] = []
        self.synths: list = []
        self.closed = 0
        self.ms = 2000
        self.loaded: list[str] = []

    def load(self, namespace: str):
        self.loaded.append(namespace)
        world = self

        class Op:
            def __init__(self, fn):
                self.fn = fn

            def get(self):
                return self.fn()

        class Cue:
            def __init__(self, start, said, first, last):
                self.start_time = SimpleNamespace(total_seconds=lambda: start / 1000)
                self.text, self.start_position_in_input, self.end_position_in_input = said, first, last

            def as_(self, _cls):
                return self

        class Stream:
            def __init__(self, data, cues):
                self.data, self.size = data, len(data)
                self.timed_metadata_tracks = [SimpleNamespace(id="SpeechSentence", cues=[]),
                                              SimpleNamespace(id="SpeechWord", cues=[Cue(*c) for c in cues])]

            def get_input_stream_at(self, _pos):
                return self

        class DataReader:
            def __init__(self, stream):
                self.stream = stream

            def load_async(self, n):
                return Op(lambda: n)

            def read_bytes(self, buf):
                buf[:] = self.stream.data

        class SpeechSynthesizer:
            all_voices = world.voices
            default_voice = world.default

            def __init__(self):
                self.voice = None
                self.options = SimpleNamespace(include_word_boundary_metadata=False, speaking_rate=1.0,
                                               audio_volume=1.0)
                world.synths.append(self)

            def synthesize_text_to_stream_async(self, text):
                def run():
                    world.entered.set()
                    world.gate.wait(5)
                    world.calls.append(dict(text=text, voice=self.voice, thread=threading.current_thread().name,
                                            **vars(self.options)))
                    if world.error is not None:
                        raise world.error
                    return Stream(silent_wav(world.ms), utf16_cues(text))
                return Op(run)

            def close(self):
                world.closed += 1

        modules = {
            "media.speechsynthesis": SimpleNamespace(SpeechSynthesizer=SpeechSynthesizer),
            "media.core": SimpleNamespace(SpeechCue=Cue),
            "storage.streams": SimpleNamespace(DataReader=DataReader),
        }
        if namespace not in modules:
            raise ImportError(namespace)
        return modules[namespace]


class BackendCase(unittest.TestCase):
    def setUp(self):
        _app()
        self.root = tempfile.mkdtemp(prefix="winvoice_test_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cache = AudioCache(self.root)
        self.addCleanup(self.cache.shutdown)
        self.world = FakeWorld()
        self.addCleanup(self.world.gate.set)
        self.ready: list[tuple] = []
        self.failed: list[tuple] = []

    def backend(self, **kw) -> WindowsVoiceBackend:
        b = WindowsVoiceBackend(load=kw.pop("load", self.world.load), **kw)
        b.chunkReady.connect(lambda g, c, p, w: self.ready.append((g, c, p, w)))
        b.chunkFailed.connect(lambda g, c, k, d: self.failed.append((g, c, k, d)))
        self.addCleanup(b.shutdown)
        return b

    def started(self, **kw) -> WindowsVoiceBackend:
        b = self.backend(**kw)
        b.start(self.cache)
        self.assertTrue(spin_until(lambda: b.voices() or not b.ready(), 5))
        return b

    def files(self) -> list[str]:
        return sorted(name for name in os.listdir(self.cache.dir) if name.endswith(".wav"))


class VoiceListTests(BackendCase):
    def test_lists_the_installed_voices_by_accent_then_name(self):
        b = self.backend()
        self.assertEqual(b.voices(), [])                   # nothing is loaded before start()
        self.assertEqual(self.world.loaded, [])
        b.start(self.cache)
        self.assertTrue(spin_until(lambda: len(b.voices()) == 5, 5))
        listed = b.voices()
        self.assertEqual([v.name for v in listed], ["David", "Mark", "Zira", "Linda", "Hortense"])
        self.assertEqual(listed[2], VoiceInfo(ZIRA, "windows", "Zira", "en-US", "Female", True, True,
                                              WINDOWS_DETAIL))
        self.assertEqual([v.gender for v in listed], ["Male", "Male", "Female", "Female", "Female"])
        self.assertTrue(all(v.private and v.word_timing and v.detail == "Windows voice · offline" for v in listed))
        self.assertTrue(b.ready())
        self.assertEqual(self.world.synths, [])             # the synthesizer waits for the first chunk

    def test_the_ids_are_valid_settings(self):
        _default, check = SCHEMA["voice"]
        b = self.started()
        for v in b.voices():
            self.assertEqual(check(v.id), v.id)

    def test_describe_voice_keeps_other_vendors_names(self):
        v = describe_voice(TOKENS + "IVONA_Amy", "IVONA 2 Amy", "en-GB", 7)
        self.assertEqual((v.id, v.name, v.gender), ("windows:IVONA_Amy", "IVONA 2 Amy", ""))
        self.assertEqual(voice_key(TOKENS + "MSTTS_V110_enUS_MarkM"), "MSTTS_V110_enUS_MarkM")
        self.assertEqual(voice_key("MSTTS_V110_enUS_MarkM"), "MSTTS_V110_enUS_MarkM")

    def test_without_winrt_the_backend_is_unavailable(self):
        def no_winrt(_namespace):
            raise ImportError("no winrt here")

        b = self.backend(load=no_winrt)
        self.assertTrue(b.ready())                          # unknown until the worker has tried
        with self.assertLogs("saylight.voices.windows", "WARNING") as logs:
            b.start(self.cache)
            b.synthesize(1, 0, "Hello there.", ZIRA, 1.0)
            self.assertTrue(spin_until(lambda: self.failed, 5))
        self.assertFalse(b.ready())
        self.assertEqual(b.voices(), [])
        self.assertEqual(self.failed, [(1, 0, "unavailable", "Windows voices aren't available")])
        self.assertIn("ImportError", logs.output[0])
        self.assertNotIn("no winrt here", "".join(logs.output))

    def test_the_backend_follows_the_interface(self):
        b = self.backend()
        self.assertIsInstance(b, base.VoiceBackend)
        self.assertEqual((b.kind, b.audio_ext), ("windows", ".wav"))
        self.assertEqual(b.chunk_limits(), (FIRST_CHUNK_CHARS, CHUNK_CHARS))


class SynthesisTests(BackendCase):
    def test_a_chunk_becomes_a_wav_with_word_timings(self):
        b = self.started()
        text = "Accuracy rose in 2019 to 94.5%."
        b.synthesize(3, 7, text, ZIRA, 1.25)
        self.assertTrue(spin_until(lambda: self.ready, 5))
        gen, chunk, path, words = self.ready[0]
        self.assertEqual((gen, chunk), (3, 7))
        self.assertTrue(self.cache.owns(path) and path.endswith(".wav"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), silent_wav(2000))
        self.assertEqual([w.text for w in words], ["Accuracy", "rose", "in", "2019", "to", "94.5%."])
        self.assertEqual([w.offset_ms for w in words], [100, 400, 700, 1000, 1900, 2200])
        self.assertEqual([w.duration_ms for w in words], [300, 300, 300, 900, 300, 0])  # the last: to the end
        call = self.world.calls[0]
        self.assertEqual(call["voice"].display_name, "Microsoft Zira")
        self.assertTrue(call["include_word_boundary_metadata"])
        self.assertEqual((call["speaking_rate"], call["audio_volume"]), (1.25, 1.0))
        self.assertEqual(call["thread"], "winvoice")
        self.assertEqual(self.failed, [])

    def test_voice_and_speed_are_set_for_every_chunk(self):
        b = self.started(volume=0)
        b.synthesize(1, 0, "One.", "windows:MSTTS_V110_enUS_DavidM", 0.5)
        b.synthesize(1, 1, "Two.", "windows:", 2.0)          # no name: Windows' default voice
        b.synthesize(1, 2, "Three.", "windows:mstts_v110_enus_markm", 9.0)
        self.assertTrue(spin_until(lambda: len(self.ready) == 3, 5))
        self.assertEqual([c["voice"].display_name for c in self.world.calls],
                         ["Microsoft David", "Microsoft Linda", "Microsoft Mark"])
        self.assertEqual([c["speaking_rate"] for c in self.world.calls], [0.5, 2.0, 6.0])
        self.assertEqual({c["audio_volume"] for c in self.world.calls}, {0.0})
        self.assertEqual(len(self.world.synths), 1)          # one synthesizer, reused
        self.assertEqual([c for _g, c, _p, _w in self.ready], [0, 1, 2])

    def test_speaking_rate_stays_in_range(self):
        self.assertEqual([speaking_rate(s) for s in (0.1, 0.5, 0.95, 2.0, 7.0)], [0.5, 0.5, 0.95, 2.0, 6.0])

    def test_a_voice_that_isnt_installed_is_unavailable(self):
        b = self.started()
        with self.assertLogs("saylight.voices.windows", "WARNING"):
            b.synthesize(1, 0, "Hello.", "windows:MSTTS_V110_enGB_HazelM", 1.0)
            self.assertTrue(spin_until(lambda: self.failed, 5))
        self.assertEqual(self.failed, [(1, 0, "unavailable", "that Windows voice isn't installed")])
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.files(), [])

    def test_an_online_voice_isnt_ours(self):
        b = self.started()
        b.synthesize(1, 0, "Hello.", "edge:en-US-AvaNeural", 1.0)
        self.assertEqual(self.failed, [])                    # asynchronous, like any result
        self.assertTrue(spin_until(lambda: self.failed, 5))
        self.assertEqual(self.failed[0][2], "unavailable")

    def test_errors_name_the_class_never_the_text(self):
        b = self.started()
        self.world.error = OSError(22, "could not speak: Secret passage here")
        with self.assertLogs("saylight.voices.windows", "WARNING") as logs:
            b.synthesize(1, 0, "Secret passage here.", ZIRA, 1.0)
            b.synthesize(1, 1, "  \n ", ZIRA, 1.0)
            self.assertTrue(spin_until(lambda: len(self.failed) == 2, 5))
        self.assertEqual([f[2:] for f in self.failed], [("other", "OSError"), ("other", "ValueError")])
        self.assertNotIn("Secret", "".join(logs.output))
        self.assertEqual(self.files(), [])

    def test_lone_surrogates_become_spaces(self):
        b = self.started()
        b.synthesize(1, 0, "Odd \ud800 text.", ZIRA, 1.0)
        self.assertTrue(spin_until(lambda: self.ready, 5))
        self.assertEqual(self.world.calls[0]["text"], "Odd   text.")

    def test_jobs_queued_before_start_run_once_it_starts(self):
        b = self.backend()
        b.synthesize(1, 0, "Early.", ZIRA, 1.0)
        spin(0.05)
        self.assertEqual(self.ready, [])
        b.start(self.cache)
        self.assertTrue(spin_until(lambda: self.ready, 5))

    def test_warm_makes_the_synthesizer_before_the_first_chunk(self):
        b = self.started()
        b.warm()
        self.assertTrue(spin_until(lambda: self.world.synths, 5))
        self.assertEqual(self.world.calls, [])


class CancelTests(BackendCase):
    def hold(self) -> None:
        """Hold the next chunk inside synthesis."""
        self.world.gate.clear()
        self.world.entered.clear()

    def test_cancel_before_drops_older_readings_in_flight_queued_and_undelivered(self):
        b = self.started()
        self.hold()
        b.synthesize(1, 0, "In flight.", ZIRA, 1.0)
        self.assertTrue(self.world.entered.wait(5))
        b.synthesize(1, 1, "Queued.", ZIRA, 1.0)
        b.synthesize(2, 0, "New reading.", ZIRA, 1.0)
        b.cancel_before(2)
        self.world.gate.set()
        self.assertTrue(spin_until(lambda: self.ready, 5))
        spin(0.1)
        self.assertEqual([(g, c) for g, c, _p, _w in self.ready], [(2, 0)])
        self.assertEqual([c["text"] for c in self.world.calls], ["In flight.", "New reading."])
        self.assertEqual(self.files(), [os.path.basename(self.ready[0][2])])   # the dropped chunk was deleted
        self.assertEqual(self.failed, [])

    def test_a_result_waiting_for_the_gui_thread_is_dropped(self):
        b = self.started()
        b.synthesize(1, 0, "Done already.", ZIRA, 1.0)
        deadline = time.monotonic() + 5                      # made, but not delivered: no events are processed
        while not self.files():
            if time.monotonic() > deadline:
                self.fail("the chunk was never written")
            time.sleep(0.005)
        b.cancel_all()
        spin(0.1)
        self.assertEqual(self.ready, [])
        self.assertTrue(spin_until(lambda: not self.files(), 3))

    def test_cancel_all_and_shutdown(self):
        b = self.started()
        self.hold()
        b.synthesize(1, 0, "One.", ZIRA, 1.0)
        self.assertTrue(self.world.entered.wait(5))
        b.synthesize(1, 1, "Two.", ZIRA, 1.0)
        b.cancel_all()
        self.world.gate.set()
        spin(0.2)
        self.assertEqual((self.ready, self.failed), ([], []))
        b.shutdown()
        self.assertFalse(b.ready())
        self.assertEqual(self.world.closed, 1)
        b.synthesize(2, 0, "After.", ZIRA, 1.0)
        spin(0.05)
        self.assertEqual((self.ready, self.failed), ([], []))
        self.assertEqual([c["text"] for c in self.world.calls], ["One."])


class WordTimingTests(unittest.TestCase):
    def test_repeated_cues_for_a_number_count_once(self):
        cues = [(100, "In 2019", 0, 6), (250, "In 2019", 0, 6), (460, "In 2019", 0, 6), (2000, "rose", 8, 11)]
        words = cue_words("In 2019 rose", cues, 3000)
        self.assertEqual(words, [Word(100, 1900, "In 2019"), Word(2000, 1000, "rose")])

    def test_positions_are_utf16_with_an_inclusive_end(self):
        text = "𝑥 and 𝑦 rose."
        cues = [(100, "and", 3, 5), (400, "rose", 10, 13)]   # 𝑥 and 𝑦 are two UTF-16 units each, and get no cue
        self.assertEqual([w.text for w in cue_words(text, cues, 900)], ["and", "rose"])
        self.assertEqual(cue_words("𝑥", [(0, "x", 0, 0)], 10)[0].text, "𝑥")   # half a pair still names it

    def test_positions_outside_the_text_keep_the_cue_text(self):
        words = cue_words("Short.", [(100, "Short", 0, 4), (300, "ghost", 40, 44), (500, "bad", 3, 1)], 600)
        self.assertEqual([w.text for w in words], ["Short", "ghost", "bad"])

    def test_durations_never_go_negative(self):
        words = cue_words("a b", [(500, "a", 0, 0), (400, "b", 2, 2)], 300)
        self.assertEqual([w.duration_ms for w in words], [0.0, 0.0])
        self.assertEqual(cue_words("", [], 100), [])

    def test_wav_length(self):
        self.assertEqual(wav_length_ms(silent_wav(1500)), 1500)
        self.assertEqual(wav_length_ms(silent_wav(1000, rate=22050)), 1000)
        with self.assertRaises((wave.Error, EOFError)):
            wav_length_ms(b"RIFF....not a wave")


# --- Registration -----------------------------------------------------------------------------


class FakeBackend(QObject):
    chunkReady = Signal(int, int, str, object)
    chunkFailed = Signal(int, int, str, str)

    def __init__(self, kind: str):
        super().__init__()
        self.kind, self.audio_ext = kind, ".mp3" if kind == "edge" else ".wav"
        self.requests: list[tuple] = []

    def start(self, cache): pass
    def ready(self): return True
    def voices(self): return []
    def chunk_limits(self): return (250, 1500)
    def synthesize(self, gen, chunk_id, text, voice, speed): self.requests.append((gen, chunk_id, voice))
    def cancel_before(self, gen): pass
    def cancel_all(self): pass
    def warm(self): pass
    def shutdown(self, timeout=2.0): pass


class Settings:
    def __init__(self, **values):
        self.values = values

    def get(self, key):
        return self.values[key]


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.root = tempfile.mkdtemp(prefix="winvoice_router_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def router(self, settings):
        made = {}

        def make(kind, parent=None):
            made[kind] = FakeBackend(kind)
            return made[kind]

        with mock.patch.object(voices, "make_backend", make):
            router = make_router(temp_root=self.root, settings=settings)
        self.addCleanup(router.shutdown)
        failed = []
        router.chunkFailed.connect(lambda *a: failed.append(a))
        return router, made["edge"], made["windows"], failed

    def test_make_backend_makes_the_windows_backend(self):
        b = voices.make_backend("windows")
        self.addCleanup(b.shutdown)
        self.assertIsInstance(b, WindowsVoiceBackend)
        self.assertEqual(b.voices(), [])                      # nothing loads before start()

    def test_a_windows_voice_speaks_when_chosen(self):
        router, edge, windows, _failed = self.router(Settings(voice="windows:MSTTS_V110_enUS_ZiraM"))
        router.synthesize(1, 0, "Hi.", ZIRA, 1.0)
        self.assertEqual((edge.requests, windows.requests), ([], [(1, 0, ZIRA)]))

    def test_the_online_voice_never_falls_back_to_it_by_itself(self):
        for settings in (Settings(voice="edge:en-US-AvaNeural"), None):
            with self.subTest(settings=settings):
                router, edge, windows, failed = self.router(settings)
                router.set_reachable(False)
                router.synthesize(1, 0, "Hi.", "edge:en-US-AvaNeural", 1.0)
                edge.chunkFailed.emit(1, 0, "offline", "ClientConnectorError")
                QCoreApplication.processEvents()
                self.assertEqual(len(edge.requests), 1)
                self.assertEqual(windows.requests, [])
                self.assertEqual(failed, [(1, 0, "offline", "ClientConnectorError")])

    def test_a_fallback_switch_in_the_settings_is_obeyed(self):
        router, edge, windows, failed = self.router(Settings(**{"voice.offline_fallback": True}))
        router.synthesize(1, 0, "Hi.", "edge:en-US-AvaNeural", 1.0)
        with self.assertLogs("saylight.voices.router", "INFO"):
            edge.chunkFailed.emit(1, 0, "offline", "ClientConnectorError")
        self.assertEqual(len(windows.requests), 1)
        self.assertEqual(failed, [])


# --- Windows' real speech API -----------------------------------------------------------------

PARAGRAPHS = (
    "Reading speed was measured in 48 adults with dyslexia (aged 19–34) and 52 controls. Each read 120 short "
    "passages aloud while an eye tracker recorded fixations at 1000 Hz.",
    "Fixation durations were longer in the dyslexia group, e.g., 287 ms versus 231 ms, and regressions were "
    "about 1.4 times as frequent (Smith & Jones, 2021). The effect held after controlling for IQ.",
    "Text-to-speech support reduced re-reading by 23%, although the benefit was smaller for passages over 400 "
    "words. Data and code are at https://example.org/reading for anyone who wants to check them.",
    "Naïve readers often skip function words; skilled ones don't. Our model predicts this pattern.",
)


def _winrt(namespace: str):
    """winrt.windows.<namespace>, imported directly. PySide6 is already loaded in this process."""
    return importlib.import_module("winrt.windows." + namespace)


def _have_winrt() -> bool:
    try:
        return all(importlib.util.find_spec("winrt.windows." + ns) is not None
                   for ns in ("media.speechsynthesis", "media.core", "storage.streams"))
    except ModuleNotFoundError:
        return False


@unittest.skipUnless(os.name == "nt" and _have_winrt(), "needs Windows and the winrt packages")
class RealVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        cls.root = tempfile.mkdtemp(prefix="winvoice_real_")
        cls.cache = AudioCache(cls.root)
        cls.cache.start()
        cls.b = WindowsVoiceBackend(load=_winrt, volume=0.0)
        cls.results: dict[tuple[int, int], tuple] = {}
        cls.b.chunkReady.connect(lambda g, c, p, w: cls.results.__setitem__((g, c), ("ready", p, w)))
        cls.b.chunkFailed.connect(lambda g, c, k, d: cls.results.__setitem__((g, c), ("failed", k, d)))
        cls.b.start(cls.cache)
        if not spin_until(lambda: cls.b.voices() or not cls.b.ready(), 10) or not cls.b.voices():
            cls.tearDownClass()
            raise unittest.SkipTest("no Windows voices on this PC")
        english = [v for v in cls.b.voices() if v.locale == "en-US"]
        cls.voice = (english or cls.b.voices())[0].id
        cls.gen = 0

    @classmethod
    def tearDownClass(cls):
        cls.b.shutdown()
        cls.cache.shutdown()
        shutil.rmtree(cls.root, ignore_errors=True)

    def speak(self, text: str, speed: float = 1.0) -> tuple[str, list[Word]]:
        type(self).gen += 1
        key = (self.gen, 0)
        self.b.synthesize(key[0], 0, text, self.voice, speed)
        self.assertTrue(spin_until(lambda: key in self.results, 10))
        result = self.results.pop(key)
        self.assertEqual(result[0], "ready", result)
        self.addCleanup(self.cache.delete, result[1])
        return result[1], result[2]

    def test_lists_this_pcs_voices(self):
        listed = self.b.voices()
        self.assertEqual(len({v.id for v in listed}), len(listed))
        for v in listed:
            self.assertTrue(v.id.startswith("windows:") and v.name and v.locale, v)
            self.assertTrue(v.private and v.word_timing)
            self.assertEqual(v.detail, WINDOWS_DETAIL)

    def test_every_word_is_placed_in_every_chunk(self):
        doc = Document.from_raw("\n\n".join(PARAGRAPHS))
        first, later = self.b.chunk_limits()
        plans = plan_chunks(doc, 0, first_limit=first, limit=later, first_chunk_id=1)
        self.assertGreater(len(plans), 1)
        placed = cued = 0
        for plan in plans:
            path, words = self.speak(plan.text)
            with open(path, "rb") as f:
                length = wav_length_ms(f.read())
            timing = map_words(plan, words, length)
            self.assertFalse(timing.proportional, plan.chunk_id)
            self.assertEqual(len(timing.words), len(words), plan.chunk_id)
            self.assertTrue(all(0 <= w.offset_ms < length and w.duration_ms >= 0 for w in words))
            self.assertEqual([w.offset_ms for w in words], sorted(w.offset_ms for w in words))
            placed += len(timing.words)
            cued += len(words)
        self.assertEqual(placed, cued)
        self.assertGreater(cued, 80)

    def test_speed_scales_the_audio(self):
        text = "The quick brown fox jumps over the lazy dog, then naps in the warm afternoon sun."
        lengths = []
        for speed in (1.0, 2.0):
            path, _words = self.speak(text, speed)
            with open(path, "rb") as f:
                lengths.append(wav_length_ms(f.read()))
        self.assertAlmostEqual(lengths[0] / lengths[1], 2.0, delta=0.15)

    def test_the_wav_is_16_bit_pcm_that_save_as_audio_can_join(self):
        path, _words = self.speak("Saved audio joins chunks with the wave module.")
        with wave.open(path, "rb") as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth()), (1, 2))
            self.assertGreaterEqual(w.getframerate(), 16000)
            self.assertEqual(w.readframes(w.getnframes()).strip(b"\0"), b"")   # volume 0: silent

    def test_a_chunk_plays_silently_in_mci(self):
        path, _words = self.speak("Silent playback check.")
        with open(path, "rb") as f:
            length = wav_length_ms(f.read())
        p = PlayerThread(volume=0)
        p.set_volume(0)                                       # before any play
        events, ticks = [], []
        p.started.connect(lambda c, n: events.append(("started", c, n)))
        p.failed.connect(lambda c, err: events.append(("failed", c, err)))
        p.tick.connect(lambda c, pos, playing: ticks.append((pos, playing)))
        p.start()
        self.addCleanup(p.shutdown)
        p.load(1, path, play=False)
        self.assertTrue(spin_until(lambda: ticks, 3))
        from .. import player
        volume = p._run_on_thread(lambda: player._mci(f"status {p._dev.alias} volume"))
        self.assertEqual(volume, "0")
        p.play()
        self.assertTrue(spin_until(lambda: events, 3))
        self.assertEqual(events[0][:2], ("started", 1))
        self.assertAlmostEqual(events[0][2], length, delta=60)
        start = len(ticks)
        self.assertTrue(spin_until(lambda: any(pos > 100 and playing for pos, playing in ticks[start:]), 3))
        p.stop_close()
        spin(0.1)


if __name__ == "__main__":
    unittest.main()

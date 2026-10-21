"""Tests for export.py: the format and size estimate, the suggested file name, and AudioExporter with a fake voice:
chunks joined in order (MP3 bytes, WAV frames), audio written only to the picked path (through "<path>.part"),
Cancel and failures leaving no file, Try again, and logs without text or paths. Nothing is played or sent."""

import datetime
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
import wave
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from saylight.audio_cache import AudioCache
from saylight.export import (FAIL_DETAILS, FAIL_KINDS, PART_SUFFIX, AudioExporter, default_name, estimate,
                             export_format, file_label, plan, remaining_label, spoken_words)


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])


@dataclass
class Sentence:
    index: int
    text: str
    spoken: str = ""

    @property
    def say(self) -> str:
        return self.spoken or self.text


@dataclass
class Doc:
    sentences: list


def doc(n: int = 6) -> Doc:
    return Doc([Sentence(i, f"Sentence {i} is about reading (Smith, 2019).", f"Sentence {i} is about reading.")
                for i in range(n)])


class FakeBackend(QObject):
    """A voice that writes a small file per chunk into the cache, delivered later from the event loop."""

    chunkReady = Signal(int, int, str, object)
    chunkFailed = Signal(int, int, str, str)

    def __init__(self, kind: str = "edge", *, limit: int = 40, fail_at: int | None = None,
                 fail_kind: str = "offline"):
        super().__init__()
        self.kind = kind
        self.audio_ext = ".mp3" if kind == "edge" else ".wav"
        self.limit = limit
        self.fail_at = fail_at
        self.fail_kind = fail_kind
        self.cache = None
        self.sent: list[str] = []
        self.cancelled = 0
        self._live: set[tuple[int, int]] = set()

    def start(self, cache) -> None:
        self.cache = cache

    def ready(self) -> bool:
        return True

    def chunk_limits(self):
        return 10, self.limit

    def synthesize(self, gen, chunk_id, text, voice, speed) -> None:
        self.sent.append(text)
        self._live.add((gen, chunk_id))
        QTimer.singleShot(1, lambda: self._deliver(gen, chunk_id, text))

    def _deliver(self, gen, chunk_id, text) -> None:
        if (gen, chunk_id) not in self._live:
            return
        self._live.discard((gen, chunk_id))
        if chunk_id == self.fail_at:
            self.fail_at = None  # fails once; Try again works
            self.chunkFailed.emit(gen, chunk_id, self.fail_kind, "ConnectionError")
            return
        path = self.cache.new_path(f"g{gen}_c{chunk_id}", self.audio_ext)
        if self.kind == "edge":
            with self.cache.create(path) as f:
                f.write(chunk_bytes(chunk_id))
        else:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(bytes([chunk_id + 1]) * 2 * 100 * (chunk_id + 1))
        self.chunkReady.emit(gen, chunk_id, path, [])

    def cancel_all(self) -> None:
        self.cancelled += 1
        self._live.clear()

    def cancel_before(self, gen) -> None:
        self._live = {j for j in self._live if j[0] >= gen}

    def shutdown(self, timeout=2.0) -> None:
        self._live.clear()


def chunk_bytes(i: int) -> bytes:
    return b"\xff\xf3\x64\xc4" + bytes([i]) * 50


def wait(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        QCoreApplication.processEvents()
        time.sleep(0.002)


class EstimateTests(unittest.TestCase):
    def test_format_follows_the_voice(self) -> None:
        self.assertEqual(export_format("edge:en-US-AvaNeural"), (".mp3", "MP3 audio"))
        self.assertEqual(export_format("en-US-AvaNeural"), (".mp3", "MP3 audio"))  # a 1.0 bare name
        self.assertEqual(export_format("windows:MSTTS_V110_enUS_ZiraM"), (".wav", "WAV audio"))

    def test_the_spoken_word_estimate(self) -> None:
        d = doc(4)
        self.assertEqual(spoken_words(d), 4 * 5)  # the citations aren't counted: they aren't said
        self.assertEqual(spoken_words(d, 2), 10)
        self.assertEqual(spoken_words(d, 1, 1), 5)
        minutes, mb = estimate(5124, 0.95, "edge:en-US-AvaNeural")
        self.assertAlmostEqual(minutes, 37.98, places=2)
        self.assertEqual(file_label(5124, 0.95, "edge:en-US-AvaNeural"), "MP3 · about 38 min · about 14 MB")
        self.assertEqual(file_label(5124, 0.95, "windows:Zira"), "WAV · about 38 min · about 73 MB")
        self.assertEqual(file_label(60, 1.0, "edge:x"), "MP3 · less than 1 min · less than 1 MB")
        self.assertEqual(file_label(142 * 95, 1.0, "windows:x"), "WAV · about 1 h 35 min · about 182 MB")
        self.assertEqual(remaining_label(None), "")
        self.assertEqual(remaining_label(130), ", about 3 min left")

    def test_the_suggested_name_is_never_made_from_the_text(self) -> None:
        folder = tempfile.mkdtemp(prefix="saylight_export_test_")
        try:
            day = datetime.date(2026, 10, 20)
            self.assertEqual(default_name("Smith 2024", ".mp3", folder, day), "Smith 2024.mp3")
            self.assertEqual(default_name('a/b: "c"?', ".wav", folder, day), "a b c.wav")
            self.assertEqual(default_name("", ".mp3", folder, day), "Saylight audio 2026-10-20.mp3")
            self.assertEqual(default_name("CON", ".mp3", folder, day), "Saylight audio 2026-10-20.mp3")
            for name in ("Saylight audio 2026-10-20.mp3", "Saylight audio 2026-10-20 (2).mp3"):
                open(os.path.join(folder, name), "wb").close()
            self.assertEqual(default_name("", ".mp3", folder, day), "Saylight audio 2026-10-20 (3).mp3")
            self.assertLessEqual(len(default_name("x" * 400, ".mp3", folder, day)), 124)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_the_plan_sends_only_what_is_said(self) -> None:
        chunks = plan(doc(6), 2, 4, limit=70)
        self.assertEqual(" ".join(chunks), "Sentence 2 is about reading. Sentence 3 is about reading. "
                                           "Sentence 4 is about reading.")
        self.assertTrue(all(len(c) <= 70 for c in chunks))
        self.assertNotIn("Smith", " ".join(chunks))


class ExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="saylight_export_test_")
        self.out = os.path.join(self.root, "My audio")
        os.makedirs(self.out)
        self.cache = AudioCache(os.path.join(self.root, "temp"), prefix="saylight_exporttest_")
        self.backends: dict[str, FakeBackend] = {}
        self.events: list[tuple] = []

    def tearDown(self) -> None:
        self.cache.shutdown()
        shutil.rmtree(self.root, ignore_errors=True)

    def exporter(self, **backend_kw) -> AudioExporter:
        def factory(kind: str) -> FakeBackend:
            self.backends[kind] = FakeBackend(kind, **backend_kw)
            return self.backends[kind]
        ex = AudioExporter(factory, cache=self.cache)
        ex.progress.connect(lambda done, total: self.events.append(("progress", done, total)))
        ex.finished.connect(lambda path: self.events.append(("finished", path)))
        ex.failed.connect(lambda kind, detail: self.events.append(("failed", kind, detail)))
        return ex

    def chunk_files(self) -> list[str]:
        return [n for n in os.listdir(self.cache.dir) if n != "saylight.lock"]

    def done(self) -> bool:
        return any(e[0] in ("finished", "failed") for e in self.events)

    def test_mp3_chunks_are_joined_in_order_into_the_picked_file(self) -> None:
        ex = self.exporter()
        target = os.path.join(self.out, "Smith 2024.mp3")
        ex.start(doc(6), target, voice="edge:en-US-AvaNeural", speed=0.95)
        self.assertTrue(ex.busy)
        self.assertEqual(self.events, [])  # nothing is reported inside start()
        self.assertEqual(os.listdir(self.out), ["Smith 2024.mp3" + PART_SUFFIX])
        wait(self.done)
        total = len(self.backends["edge"].sent)
        self.assertEqual(total, 6)
        self.assertEqual(self.events[-1], ("finished", target))
        self.assertEqual([e[1:] for e in self.events[:-1]], [(i, total) for i in range(1, total + 1)])
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"".join(chunk_bytes(i) for i in range(total)))
        self.assertEqual(os.listdir(self.out), ["Smith 2024.mp3"])  # audio only where the person chose
        self.assertEqual(self.chunk_files(), [])                    # each chunk deleted once appended
        self.assertFalse(ex.busy)
        self.assertFalse(ex.can_retry)

    def test_wav_chunks_are_joined_under_one_header(self) -> None:
        ex = self.exporter()
        target = os.path.join(self.out, "notes.wav")
        ex.start(doc(3), target, voice="windows:Zira", speed=1.0)
        wait(self.done)
        self.assertEqual(self.events[-1], ("finished", target))
        with wave.open(target, "rb") as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
            self.assertEqual(w.getnframes(), 100 * (1 + 2 + 3))
            self.assertEqual(w.readframes(100), bytes([1]) * 200)

    def test_cancel_leaves_no_file_and_keeps_an_old_one(self) -> None:
        target = os.path.join(self.out, "Smith 2024.mp3")
        with open(target, "wb") as f:
            f.write(b"older audio")
        ex = self.exporter()
        ex.progress.connect(lambda done, _total: ex.cancel() if done == 2 else None)
        ex.start(doc(6), target, voice="edge:x", speed=1.0)
        wait(lambda: not ex.busy)
        for _ in range(20):  # late results arrive and are dropped
            QCoreApplication.processEvents()
            time.sleep(0.002)
        self.assertEqual(os.listdir(self.out), ["Smith 2024.mp3"])
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"older audio")
        self.assertEqual(self.chunk_files(), [])
        self.assertFalse(any(e[0] in ("finished", "failed") for e in self.events))
        self.assertFalse(ex.can_retry)

    def test_a_failed_chunk_stops_the_save_and_try_again_uses_the_same_path(self) -> None:
        ex = self.exporter(fail_at=3)
        target = os.path.join(self.out, "paper.mp3")
        ex.start(doc(6), target, voice="edge:x", speed=1.0)
        wait(self.done)
        self.assertEqual(self.events[-1], ("failed", "offline", "offline"))
        self.assertEqual(os.listdir(self.out), [])  # no .part left behind
        self.assertTrue(ex.can_retry)
        self.assertEqual(ex.target, target)
        self.events.clear()
        self.assertTrue(ex.retry())
        wait(self.done)
        self.assertEqual(self.events[-1], ("finished", target))
        self.assertEqual(os.listdir(self.out), ["paper.mp3"])

    def test_failure_kinds(self) -> None:
        self.assertEqual(FAIL_KINDS, ("offline", "service", "io", "other"))
        self.assertEqual(set(FAIL_DETAILS), set(FAIL_KINDS))
        for given, expected in (("service", "service"), ("unavailable", "other"), ("other", "other")):
            with self.subTest(given=given):
                self.events.clear()
                ex = self.exporter(fail_at=0, fail_kind=given)
                ex.start(doc(2), os.path.join(self.out, "a.mp3"), voice="edge:x", speed=1.0)
                wait(self.done)
                self.assertEqual(self.events[-1][:2], ("failed", expected))
                self.assertEqual(os.listdir(self.out), [])

    def test_a_folder_that_cant_be_written_is_an_io_failure(self) -> None:
        ex = self.exporter()
        ex.start(doc(2), os.path.join(self.out, "missing", "a.mp3"), voice="edge:x", speed=1.0)
        self.assertFalse(ex.busy)
        self.assertEqual(self.events, [])
        wait(self.done)
        self.assertEqual(self.events[-1][:2], ("failed", "io"))

    def test_one_save_at_a_time(self) -> None:
        ex = self.exporter()
        ex.start(doc(2), os.path.join(self.out, "a.mp3"), voice="edge:x", speed=1.0)
        with self.assertRaises(RuntimeError):
            ex.start(doc(2), os.path.join(self.out, "b.mp3"), voice="edge:x", speed=1.0)
        wait(self.done)

    def test_a_time_left_after_a_few_chunks(self) -> None:
        ex = self.exporter()
        seen = []
        ex.progress.connect(lambda done, _total: seen.append((done, ex.remaining_s())))
        ex.start(doc(6), os.path.join(self.out, "a.mp3"), voice="edge:x", speed=1.0)
        wait(self.done)
        self.assertEqual([s is None for _, s in seen], [True, True, False, False, False, False])
        self.assertEqual(ex.fraction(), 1.0)

    def test_shutdown_deletes_the_unfinished_file(self) -> None:
        ex = self.exporter()
        ex.start(doc(6), os.path.join(self.out, "a.mp3"), voice="edge:x", speed=1.0)
        ex.shutdown()
        self.assertEqual(os.listdir(self.out), [])
        self.assertFalse(ex.busy)

    def test_logs_hold_no_text_titles_or_paths(self) -> None:
        with self.assertLogs("saylight.export", logging.DEBUG) as logs:
            ex = self.exporter()
            ex.start(doc(3), os.path.join(self.out, "Smith 2024.mp3"), voice="edge:x", speed=1.0)
            wait(self.done)
            self.events.clear()
            ex2 = self.exporter(fail_at=0)
            ex2.start(doc(3), os.path.join(self.out, "Lee.mp3"), voice="edge:x", speed=1.0)
            wait(self.done)
        text = "\n".join(logs.output)
        self.assertIn("Export started: mp3, 3 sentences", text)
        self.assertIn("Export finished:", text)
        self.assertIn("Export failed: offline", text)
        for secret in ("Sentence", "reading", "Smith", "Lee", self.out, "My audio"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()

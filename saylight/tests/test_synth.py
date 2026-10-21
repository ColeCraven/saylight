"""Tests for the online voice: voices/edge.py, which synth.py re-exports as Saylight 1.0's SynthWorker.

Every worker test runs twice: on SynthWorker directly, and through SynthRouter, the engine's synth from
v1.2. One real edge-tts request (word timing against the MCI length); a refused local proxy for the
offline path (no request leaves the machine); fakes for everything timing-sensitive. No audio is
played: the MP3 is only opened in MCI to read its length.
"""

import asyncio
import functools
import glob
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import aiohttp
import edge_tts
from edge_tts.exceptions import NoAudioReceived, UnexpectedResponse, UnknownResponse, WebSocketError
from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtNetwork import QNetworkInformation

from .. import player, synth
from ..audio_cache import LOCK_NAME, AudioCache
from ..synth import SynthWorker, Word, classify_error, rate_string
from ..voices import edge
from ..voices.router import SynthRouter

VOICE = "en-US-AvaNeural"
TEXT = "Saylight speaks the text you highlight. It keeps time with the audio."


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def spin(seconds: float) -> None:
    app, end = _app(), time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        time.sleep(0.002)


def spin_until(pred, timeout: float) -> bool:
    app, end = _app(), time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        if pred():
            return True
        time.sleep(0.002)
    QCoreApplication.processEvents()
    return pred()


def wait_files(folder: str, pattern: str, timeout: float = 3.0, *, nonempty: bool = False) -> list[str]:
    """Poll the file system without processing Qt events."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = [p for p in glob.glob(os.path.join(folder, pattern)) if not nonempty or os.path.getsize(p) > 0]
        if found:
            return found
        time.sleep(0.005)
    return []


def mci_length(path: str) -> int:
    """Open without playing, read the length in ms, close."""
    alias = "synth_test_len"
    player._mci(f'open "{player._mci_path(path)}" type mpegvideo alias {alias}')
    try:
        player._mci(f"set {alias} time format milliseconds")
        return int(player._mci(f"status {alias} length"))
    finally:
        player._mci_quiet(f"close {alias}")


# --- Fake edge_tts.Communicate ------------------------------------------------------------

def audio(n: int = 600) -> dict:
    return {"type": "audio", "data": b"\xff" * n}


def word(offset_ms: float, duration_ms: float, text: str) -> dict:
    return {"type": "WordBoundary", "offset": int(offset_ms * 10_000), "duration": int(duration_ms * 10_000),
            "text": text}


class FakeComm:
    """Stands in for edge_tts.Communicate; `script(fake)` is an async generator of stream items."""

    def __init__(self, script, made: list, text: str, voice: str, **kwargs):
        self.script, self.text, self.voice, self.kwargs = script, text, voice, kwargs
        self.closed = False
        made.append(self)

    async def stream(self):
        try:
            async for item in self.script(self):
                yield item
        finally:
            self.closed = True


async def quick(fake):
    yield word(100, 200, "Hello")
    yield audio()
    yield word(320, 300, "world")
    yield audio()


async def slow_first(fake):
    """The job whose text is "first" streams for about 2 s; any other is quick."""
    if fake.text != "first":
        async for item in quick(fake):
            yield item
        return
    for _ in range(100):
        yield audio(9000)          # larger than the file buffer, so the .part grows on disk
        await asyncio.sleep(0.02)


async def raising(fake):
    yield audio()
    if fake.text == "svc":
        raise NoAudioReceived("No audio was received.")
    if fake.text.startswith("quote"):
        raise RuntimeError(f"cannot handle {fake.text!r}")
    if fake.text.startswith("middle"):
        raise RuntimeError(f"bad token near {fake.text[34:70]!r}")
    raise ValueError("boom")


async def confirmed(fake):
    """Like edge-tts 7.x: two parts, each confirmed by turn.end in `state`. "cut short" loses the
    connection during the second part, which aiohttp reports as a normal end of the stream."""
    fake.state = {"cumulative_audio_bytes": 0, "chunk_audio_bytes": 0}
    yield word(100, 200, "Hello")
    yield audio(600)
    fake.state["cumulative_audio_bytes"] = 600
    yield word(400, 300, "world")
    yield audio(600)
    if fake.text != "cut short":
        fake.state["cumulative_audio_bytes"] = 1200


async def hang(fake):
    yield audio()
    await asyncio.sleep(30)


# --- Tests ---------------------------------------------------------------------------------

class PureFunctionTests(unittest.TestCase):
    def test_rate_string(self):
        self.assertEqual([rate_string(v) for v in (0.5, 0.85, 0.95, 1.0, 1.05, 1.15, 1.25, 2.0)],
                         ["-50%", "-15%", "-5%", "+0%", "+5%", "+15%", "+25%", "+100%"])

    def test_classify_error(self):
        cases = [
            (aiohttp.ClientConnectionError(), "offline"),
            (aiohttp.ServerDisconnectedError(), "offline"),
            (aiohttp.ClientOSError(10054, "reset"), "offline"),
            (aiohttp.ClientPayloadError("broken"), "offline"),
            (socket.gaierror(11001, "getaddrinfo failed"), "offline"),
            (TimeoutError(), "offline"),
            (asyncio.TimeoutError(), "offline"),
            (ConnectionResetError(), "offline"),
            (OSError(10051, "network unreachable"), "offline"),
            (OSError(None, "semaphore timeout", None, 121), "offline"),
            (NoAudioReceived(), "service"),
            (UnexpectedResponse(), "service"),
            (UnknownResponse(), "service"),
            (WebSocketError(), "service"),
            (aiohttp.WSServerHandshakeError(None, (), status=403), "service"),
            (aiohttp.ClientResponseError(None, (), status=503), "service"),
            (ValueError("Invalid rate"), "other"),
            (PermissionError(13, "denied"), "other"),
            (OSError(28, "No space left on device"), "other"),
        ]
        for exc, kind in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(classify_error(exc), kind)

    def test_disconnected_turns_service_errors_offline_only(self):
        self.assertEqual(classify_error(NoAudioReceived(), disconnected=True), "offline")
        self.assertEqual(classify_error(aiohttp.ClientConnectionError(), disconnected=True), "offline")
        self.assertEqual(classify_error(ValueError(), disconnected=True), "other")

    def test_incomplete_audio_is_offline(self):
        self.assertEqual(classify_error(synth.IncompleteAudio("closed early")), "offline")

    def test_describe_withholds_any_quoted_stretch_of_the_text(self):
        text = ("The opening clause is harmless enough, but the confidential result appears later "
                "in the second half of this long chunk.")
        mid = text[60:95]
        self.assertNotIn(text.strip()[:32], mid)          # the first-32-characters probe misses this
        detail = edge._describe(RuntimeError(f"cannot parse near {mid!r}"), text)
        self.assertEqual(detail, "RuntimeError: (message withheld: it quoted the text)")
        squeezed = edge._describe(RuntimeError(" ".join(text[40:80].split()) + " is bad"), "\n" + text)
        self.assertNotIn("confidential", squeezed)
        kept = edge._describe(aiohttp.ClientConnectionError("Cannot connect to host speech.platform.bing.com:443"),
                               text)
        self.assertEqual(kept, "ClientConnectionError: Cannot connect to host speech.platform.bing.com:443")
        self.assertEqual(edge._describe(TimeoutError(), text), "TimeoutError")
        self.assertEqual(edge._describe(UnicodeEncodeError("utf-8", "a\ud800", 1, 2, "surrogates"), text),
                         "UnicodeEncodeError")

    def test_describe_leaves_out_file_paths(self):
        part = os.path.join(tempfile.gettempdir(), "saylight_x", "g1_c0_1.part")
        cases = {
            FileNotFoundError(2, "No such file or directory", part):
                "FileNotFoundError: [Errno 2] No such file or directory",
            PermissionError(13, "Access is denied", part, 5, part[:-5] + ".mp3"):
                "PermissionError: [WinError 5] Access is denied",
            OSError(28, "No space left on device"): "OSError: [Errno 28] No space left on device",
            aiohttp.ClientOSError(10054, "reset"): "ClientOSError: [Errno 10054] reset",
        }
        for exc, detail in cases.items():
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(edge._describe(exc, TEXT), detail)
        try:
            open(part, "rb")
        except OSError as e:
            real = edge._describe(e, TEXT)
        self.assertNotIn("saylight_x", real)
        self.assertTrue(real.startswith("FileNotFoundError: [Errno 2] "), real)


class WorkerTestBase(unittest.TestCase):
    """`worker` is what the engine talks to; `backend` is the EdgeBackend doing the work (the same object
    unless the test runs through the router)."""

    via_router = False

    def setUp(self):
        _app()
        self.root = tempfile.mkdtemp(prefix="synth_test_")
        self.addCleanup(shutil.rmtree, self.root, True)
        if self.via_router:
            self.backend = SynthWorker()
            self.cache = AudioCache(self.root)
            self.worker = SynthRouter({"edge": self.backend}, self.cache)
        else:
            self.worker = self.backend = SynthWorker(temp_root=self.root)
        self.addCleanup(self.worker.shutdown)
        self.ready, self.failed = [], []
        self.worker.chunkReady.connect(lambda *a: self.ready.append(a))
        self.worker.chunkFailed.connect(lambda *a: self.failed.append(a))

    def use_fake(self, script) -> list[FakeComm]:
        made: list[FakeComm] = []
        patcher = mock.patch.object(edge.edge_tts, "Communicate", functools.partial(FakeComm, script, made))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.worker.shutdown)   # runs before the patch is removed (cleanups are LIFO)
        return made

    def files(self, pattern: str = "*") -> list[str]:
        """Chunk files in the cache folder (the run's lock file is not one)."""
        names = (os.path.basename(p) for p in glob.glob(os.path.join(self.worker.temp_dir, pattern)))
        return sorted(name for name in names if name != LOCK_NAME)


class RealServiceTests(WorkerTestBase):
    def test_word_offsets_match_mci_length(self):
        real, made = edge_tts.Communicate, []

        def capture(*args, **kwargs):
            made.append(real(*args, **kwargs))
            return made[-1]

        self.worker.start()
        with mock.patch.object(edge.edge_tts, "Communicate", capture), \
                self.assertLogs("saylight", "DEBUG") as logs:
            self.worker.synthesize(1, 0, TEXT, VOICE, "-5%")
            spin_until(lambda: self.ready or self.failed, 25)
        if self.failed and self.failed[0][2] == "offline":
            self.skipTest(f"no network: {self.failed[0][3]}")
        self.assertEqual(self.failed, [])
        gen, chunk_id, path, words = self.ready[0]
        self.assertEqual((gen, chunk_id), (1, 0))
        self.assertRegex(os.path.basename(path), r"^g1_c0_\d+\.mp3$")
        self.assertEqual(os.path.dirname(path), self.worker.temp_dir)
        self.assertEqual(self.files("*.part"), [])
        self.assertTrue(words and all(isinstance(w, Word) for w in words))
        # The completeness check really ran: this edge-tts reports the bytes confirmed by turn.end.
        self.assertEqual(edge._confirmed_bytes(made[0]), os.path.getsize(path))
        # Created as a short-lived file, which Windows tries to keep in memory, and MCI still opens it.
        self.assertTrue(os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_TEMPORARY)

        # Every word is found, in order, in the text that was sent.
        cursor = 0
        for w in words:
            i = TEXT.find(w.text, cursor)
            self.assertGreaterEqual(i, 0, "word not found in order")
            cursor = i + len(w.text)
        offsets = [w.offset_ms for w in words]
        self.assertEqual(offsets, sorted(offsets))

        length = mci_length(path)
        first, last = words[0], words[-1]
        end = last.offset_ms + last.duration_ms
        sys.stderr.write(f"\n  [{len(words)} words; first offset {first.offset_ms:.1f} ms; "
                         f"last word ends {end:.1f} ms; MCI length {length} ms] ")
        self.assertGreaterEqual(first.offset_ms, 50)       # ~100 ms leading silence, kept as-is
        self.assertLessEqual(first.offset_ms, 400)
        self.assertLessEqual(end, length + 50)
        self.assertLess(length - end, 1500)

        # Privacy: nothing logged quotes the text.
        for record in logs.records:
            msg = record.getMessage()
            self.assertNotIn("highlight", msg)
            self.assertNotIn("keeps time", msg)

    def test_refused_proxy_is_offline(self):
        real = edge_tts.Communicate
        refused = functools.partial(real, proxy="http://127.0.0.1:9")
        with mock.patch.object(edge.edge_tts, "Communicate", refused), self.assertLogs("saylight.voices", "WARNING"):
            self.worker.start()
            self.worker.synthesize(3, 4, TEXT, VOICE, "-5%")
            self.assertTrue(spin_until(lambda: self.failed, 15))
            self.worker.shutdown()
        gen, chunk_id, kind, detail = self.failed[0]
        self.assertEqual((gen, chunk_id, kind), (3, 4, "offline"))
        self.assertTrue(detail.startswith("ClientProxyConnectionError"), detail)
        self.assertNotIn("highlight", detail)
        self.assertEqual(self.ready, [])


class QueueAndCancelTests(WorkerTestBase):
    def test_fifo_one_in_flight_and_communicate_args(self):
        state = {"now": 0, "max": 0}

        async def counted(fake):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            try:
                async for item in quick(fake):
                    yield item
                    await asyncio.sleep(0.01)
            finally:
                state["now"] -= 1

        made = self.use_fake(counted)
        self.worker.start()
        for i in range(4):
            self.worker.synthesize(7, i, f"chunk {i}", VOICE, 0.95)
        self.assertTrue(spin_until(lambda: len(self.ready) == 4, 5))
        self.assertEqual([r[1] for r in self.ready], [0, 1, 2, 3])
        self.assertEqual(state["max"], 1)
        self.assertEqual(made[0].kwargs, {"rate": "-5%", "boundary": "WordBoundary",
                                          "connect_timeout": 8, "receive_timeout": 30})
        self.assertEqual(self.ready[0][3], [Word(100.0, 200.0, "Hello"), Word(320.0, 300.0, "world")])
        paths = [r[2] for r in self.ready]
        self.assertEqual(len(set(paths)), 4)
        self.assertTrue(all(os.path.isfile(p) and p.endswith(".mp3") for p in paths))
        self.assertEqual(self.failed, [])

    def test_cancel_before_mid_stream(self):
        made = self.use_fake(slow_first)
        self.worker.start()
        self.worker.synthesize(1, 0, "first", VOICE, "-5%")
        self.worker.synthesize(1, 1, "second", VOICE, "-5%")
        self.worker.synthesize(2, 2, "third", VOICE, "-5%")
        parts = wait_files(self.worker.temp_dir, "*.part", nonempty=True)
        self.assertEqual(len(parts), 1)
        self.assertRegex(os.path.basename(parts[0]), r"^g1_c0_\d+\.part$")

        self.worker.cancel_before(2)
        self.assertTrue(spin_until(lambda: self.ready, 3))
        spin(0.2)
        self.assertEqual([(r[0], r[1]) for r in self.ready], [(2, 2)])
        self.assertEqual(self.failed, [])
        self.assertEqual([f.text for f in made], ["first", "third"])   # the queued gen-1 job never ran
        self.assertTrue(made[0].closed)
        self.assertFalse(os.path.exists(parts[0]))
        self.assertEqual(self.files(), [os.path.basename(self.ready[0][2])])

    def test_cancel_wins_over_an_undelivered_result(self):
        self.use_fake(quick)
        self.worker.start()
        self.worker.synthesize(1, 0, "x", VOICE, "-5%")
        self.assertTrue(wait_files(self.worker.temp_dir, "*.mp3"))   # finished, Qt events not processed yet
        time.sleep(0.05)
        self.worker.cancel_before(2)
        spin(0.3)
        self.assertEqual((self.ready, self.failed), ([], []))
        self.assertEqual(self.files(), [])

    def test_cancel_wins_over_an_undelivered_failure(self):
        self.use_fake(raising)
        self.worker.start()
        with self.assertLogs("saylight.voices", "WARNING") as logs:
            self.worker.synthesize(1, 0, "bad", VOICE, "-5%")
            end = time.monotonic() + 3
            while not logs.records and time.monotonic() < end:
                time.sleep(0.005)                  # no Qt events: the failure waits in the relay
            time.sleep(0.05)
            self.worker.cancel_before(2)
            spin(0.3)
        self.assertEqual((self.ready, self.failed), ([], []))
        self.assertEqual(self.backend._live, [])

    def test_results_arrive_on_the_gui_thread(self):
        self.use_fake(confirmed)
        threads = []
        self.worker.chunkReady.connect(lambda *a: threads.append(threading.current_thread()))
        self.worker.chunkFailed.connect(lambda *a: threads.append(threading.current_thread()))
        self.worker.start()
        with self.assertLogs("saylight.voices", "WARNING"):
            self.worker.synthesize(1, 0, "whole", VOICE, "-5%")
            self.worker.synthesize(1, 1, "cut short", VOICE, "-5%")
            self.assertTrue(spin_until(lambda: len(threads) == 2, 3))
        self.assertEqual(threads, [threading.main_thread()] * 2)

    def test_start_off_the_gui_thread_watches_the_network_on_it(self):
        self.use_fake(quick)
        seen = []
        with mock.patch.object(edge, "QNetworkInformation") as qni:
            qni.loadBackendByFeatures.side_effect = lambda *a: seen.append(threading.current_thread()) and False
            starter = threading.Thread(target=self.worker.start)
            starter.start()
            starter.join(5)
            self.assertTrue(spin_until(lambda: seen, 3))
        self.assertEqual(seen, [threading.main_thread()])
        self.worker.synthesize(1, 0, "x", VOICE, "-5%")
        self.assertTrue(spin_until(lambda: self.ready, 3))

    def test_cancel_all(self):
        made = self.use_fake(slow_first)
        self.worker.start()
        self.worker.synthesize(5, 0, "first", VOICE, "-5%")
        self.worker.synthesize(9, 1, "later", VOICE, "-5%")
        self.assertTrue(wait_files(self.worker.temp_dir, "*.part", nonempty=True))
        self.worker.cancel_all()
        spin(0.3)
        self.assertEqual((self.ready, self.failed), ([], []))
        self.assertEqual(self.files(), [])
        self.worker.synthesize(10, 2, "after", VOICE, "-5%")
        self.assertTrue(spin_until(lambda: self.ready, 3))
        self.assertEqual(self.ready[0][:2], (10, 2))
        self.assertEqual([f.text for f in made], ["first", "after"])

    def test_shutdown_cancels_in_flight_and_removes_cache(self):
        made = self.use_fake(slow_first)
        self.worker.start()
        folder = self.worker.temp_dir
        self.worker.synthesize(1, 0, "first", VOICE, "-5%")
        self.assertTrue(wait_files(folder, "*.part", nonempty=True))
        t0 = time.monotonic()
        self.worker.shutdown()
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertFalse(self.backend._thread.is_alive())
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(made[0].closed)
        self.worker.synthesize(2, 0, "ignored", VOICE, "-5%")
        spin(0.2)
        self.assertEqual((self.ready, self.failed), ([], []))


class FailureTests(WorkerTestBase):
    def test_service_and_other_errors(self):
        self.use_fake(raising)
        self.worker.start()
        middle = "middle: an ordinary opening clause, then the private finding that must never be logged"
        with self.assertLogs("saylight.voices", "WARNING") as logs:
            self.worker.synthesize(1, 0, "svc", VOICE, "-5%")
            self.worker.synthesize(1, 1, "bad", VOICE, "-5%")
            self.worker.synthesize(1, 2, "quote: the secret sentence", VOICE, "-5%")
            self.worker.synthesize(1, 3, middle, VOICE, "-5%")
            self.assertTrue(spin_until(lambda: len(self.failed) == 4, 5))
        self.assertFalse(any("secret" in r.getMessage() or "private" in r.getMessage() for r in logs.records))
        kinds = [(f[1], f[2]) for f in self.failed]
        self.assertEqual(kinds, [(0, "service"), (1, "other"), (2, "other"), (3, "other")])
        self.assertTrue(self.failed[0][3].startswith("NoAudioReceived: "))
        self.assertEqual(self.failed[1][3], "ValueError: boom")
        self.assertTrue(self.failed[2][3].startswith("RuntimeError"))
        self.assertNotIn("secret", self.failed[2][3])
        self.assertEqual(self.failed[3][3], "RuntimeError: (message withheld: it quoted the text)")
        self.assertEqual(self.files(), [])

    def test_stream_that_ends_without_turn_end_fails_offline(self):
        self.use_fake(confirmed)
        self.worker.start()
        with self.assertLogs("saylight.voices", "WARNING"):
            self.worker.synthesize(1, 0, "whole", VOICE, "-5%")
            self.worker.synthesize(1, 1, "cut short", VOICE, "-5%")
            self.assertTrue(spin_until(lambda: self.ready and self.failed, 3))
        self.assertEqual([r[:2] for r in self.ready], [(1, 0)])
        self.assertEqual(self.ready[0][3], [Word(100.0, 200.0, "Hello"), Word(400.0, 300.0, "world")])
        gen, chunk_id, kind, detail = self.failed[0]
        self.assertEqual((gen, chunk_id, kind), (1, 1, "offline"))
        self.assertTrue(detail.startswith("IncompleteAudio: "), detail)
        self.assertEqual(self.files(), [os.path.basename(self.ready[0][2])])   # the partial file is gone

    def test_cache_folder_is_recreated_if_a_cleaner_removed_it(self):
        self.use_fake(quick)
        self.worker.start()
        self.backend._cache._unlock()      # while the run holds its lock, no cleaner can remove the folder
        shutil.rmtree(self.worker.temp_dir)
        self.worker.synthesize(1, 0, "x", VOICE, "-5%")
        self.assertTrue(spin_until(lambda: self.ready or self.failed, 3))
        self.assertEqual(self.failed, [])
        self.assertTrue(os.path.isfile(self.ready[0][2]))

    def test_a_file_error_is_reported_without_its_path(self):
        self.use_fake(quick)
        self.worker.start()

        def denied(path):
            raise PermissionError(13, "Access is denied", path, 5)

        with mock.patch.object(AudioCache, "create", side_effect=denied), \
                self.assertLogs("saylight", "WARNING") as logs:
            self.worker.synthesize(1, 0, "x", VOICE, "-5%")
            self.assertTrue(spin_until(lambda: self.failed, 3))
        self.assertEqual(self.failed[0], (1, 0, "other", "PermissionError: [WinError 5] Access is denied"))
        self.assertFalse(any(self.root in r.getMessage() or "g1_c0" in r.getMessage() for r in logs.records))

    def test_a_job_the_cache_cannot_take_fails_instead_of_vanishing(self):
        made = self.use_fake(quick)
        self.worker.start()
        refused = RuntimeError("the audio cache has not been started")
        with mock.patch.object(AudioCache, "new_path", side_effect=refused), \
                self.assertLogs("saylight.voices", "WARNING"):
            self.worker.synthesize(1, 0, "x", VOICE, "-5%")
            self.assertTrue(spin_until(lambda: self.failed, 3))
        self.assertEqual(self.failed[0], (1, 0, "other", "RuntimeError: the audio cache has not been started"))
        self.assertEqual((made, self.backend._live), ([], []))

    def test_reported_disconnection_makes_service_errors_offline(self):
        self.use_fake(raising)
        self.worker.start()
        self.backend._on_reachability(QNetworkInformation.Reachability.Disconnected)
        with self.assertLogs("saylight.voices", "WARNING"):
            self.worker.synthesize(1, 0, "svc", VOICE, "-5%")
            self.worker.synthesize(1, 1, "bad", VOICE, "-5%")
            self.assertTrue(spin_until(lambda: len(self.failed) == 2, 3))
        self.assertEqual([f[2] for f in self.failed], ["offline", "other"])

    def test_empty_text_fails_without_a_request(self):
        made = self.use_fake(quick)
        self.worker.start()
        with self.assertLogs("saylight.voices", "WARNING"):
            self.worker.synthesize(1, 0, "  \n ", VOICE, "-5%")
            self.assertTrue(spin_until(lambda: self.failed, 3))
        self.assertEqual(self.failed[0], (1, 0, "other", "ValueError: empty text"))
        self.assertEqual(made, [])

    def test_lone_surrogates_are_replaced(self):
        made = self.use_fake(quick)
        self.worker.start()
        self.worker.synthesize(1, 0, "a\ud800b", VOICE, "-5%")
        self.assertTrue(spin_until(lambda: self.ready, 3))
        self.assertEqual(made[0].text, "a b")

    def test_stalled_stream_times_out_as_offline(self):
        made = self.use_fake(hang)
        with mock.patch.object(edge, "IDLE_TIMEOUT_S", 0.3), self.assertLogs("saylight.voices", "WARNING"):
            self.worker.start()
            self.worker.synthesize(1, 0, "x", VOICE, "-5%")
            self.assertTrue(spin_until(lambda: self.failed, 3))
        self.assertEqual(self.failed[0], (1, 0, "offline", "TimeoutError"))
        self.assertTrue(made[0].closed)
        self.assertEqual(self.files(), [])


class TempFileTests(WorkerTestBase):
    def test_stale_dirs_purged_and_cache_removed_on_shutdown(self):
        old_time = time.time() - 2 * 3600

        def make(name: str, *, dir_old: bool, file_old: bool | None = None) -> str:
            path = os.path.join(self.root, name)
            os.mkdir(path)
            if file_old is not None:
                f = os.path.join(path, "0.mp3")
                open(f, "wb").close()
                if file_old:
                    os.utime(f, (old_time, old_time))
            if dir_old:
                os.utime(path, (old_time, old_time))
            return path

        old = make("saylight_old", dir_old=True, file_old=True)
        fresh = make("saylight_fresh", dir_old=False)
        busy = make("saylight_busy", dir_old=True, file_old=False)
        other = make("other_old", dir_old=True)
        legacy_old = make("read_aloud_old", dir_old=True, file_old=True)    # left by the app's earlier name
        legacy_busy = make("read_aloud_busy", dir_old=True, file_old=False)
        self.worker.start()
        folder = self.worker.temp_dir
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(legacy_old))
        for kept in (fresh, busy, other, legacy_busy, folder):
            self.assertTrue(os.path.isdir(kept), kept)
        self.assertEqual(os.path.dirname(folder), self.root)
        self.assertTrue(os.path.basename(folder).startswith("saylight_"))
        self.worker.shutdown()
        self.assertFalse(os.path.exists(folder))

    def test_delete(self):
        self.worker.start()
        inside = os.path.join(self.worker.temp_dir, "g1_c0_1.mp3")
        open(inside, "wb").close()
        self.worker.delete(inside)
        self.assertFalse(os.path.exists(inside))

        outside = os.path.join(self.root, "keep.mp3")
        open(outside, "wb").close()
        with self.assertLogs("saylight.audio_cache", "WARNING"):
            self.worker.delete(outside)
        self.assertTrue(os.path.exists(outside))

        locked = os.path.join(self.worker.temp_dir, "g1_c1_2.mp3")
        handle = open(locked, "wb")        # an open handle blocks deletion on Windows, like MCI does
        self.worker.delete(locked)
        self.assertTrue(os.path.exists(locked))
        handle.close()
        end = time.monotonic() + 3
        while os.path.exists(locked) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertFalse(os.path.exists(locked))


# --- The same tests through SynthRouter --------------------------------------------------------

class RouterRealServiceTests(RealServiceTests):
    via_router = True


class RouterQueueAndCancelTests(QueueAndCancelTests):
    via_router = True


class RouterFailureTests(FailureTests):
    via_router = True


class RouterTempFileTests(TempFileTests):
    via_router = True


if __name__ == "__main__":
    unittest.main()

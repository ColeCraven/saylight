"""Tests for audio_cache.py: the run's playback folder, its lock, the purge of crashed runs' folders and
short-lived chunk files.

Every test works in its own temporary root, never in %TEMP% itself, so a running Saylight's folder is
never touched. No audio is played: files are only opened in MCI to read their length.
"""

import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock

from .. import audio_cache, player
from ..audio_cache import LOCK_NAME, AudioCache, purge_orphans, purge_stale_dirs

REPO = os.path.dirname(os.path.dirname(os.path.abspath(audio_cache.__file__)))
OLD = time.time() - 2 * 3600


def silent_wav(ms: int = 400, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * (rate * ms // 1000))
    return buf.getvalue()


def mci_length(path: str) -> int:
    """Open without playing, read the length in ms, close."""
    alias = "cache_test_len"
    player._mci(f'open "{player._mci_path(path)}" type mpegvideo alias {alias}')
    try:
        player._mci(f"set {alias} time format milliseconds")
        return int(player._mci(f"status {alias} length"))
    finally:
        player._mci_quiet(f"close {alias}")


class CacheTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cache_test_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def cache(self, **kwargs) -> AudioCache:
        c = AudioCache(self.root, **kwargs)
        self.addCleanup(c.shutdown)
        return c

    def make_dir(self, name: str, *, lock: bool = False, old: bool = False, file_old: bool | None = None) -> str:
        path = os.path.join(self.root, name)
        os.mkdir(path)
        if lock:
            open(os.path.join(path, LOCK_NAME), "wb").close()      # a lock nobody holds: its run has ended
        if file_old is not None:
            f = os.path.join(path, "0.mp3")
            open(f, "wb").close()
            if file_old:
                os.utime(f, (OLD, OLD))
        if lock and old:
            os.utime(os.path.join(path, LOCK_NAME), (OLD, OLD))
        if old:
            os.utime(path, (OLD, OLD))
        return path


class LifecycleTests(CacheTestBase):
    def test_start_creates_a_locked_folder_and_shutdown_removes_it(self):
        c = self.cache()
        self.assertIsNone(c.dir)
        folder = c.start()
        self.assertEqual(c.dir, folder)
        self.assertEqual(os.path.dirname(folder), self.root)
        self.assertTrue(os.path.basename(folder).startswith("saylight_"))
        lock = os.path.join(folder, LOCK_NAME)
        self.assertTrue(os.path.isfile(lock))
        with self.assertRaises(PermissionError):    # held: Windows refuses to delete it
            os.remove(lock)
        self.assertEqual(c.start(), folder)        # a second start is a no-op
        c.shutdown()
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(c.start(), folder)        # nothing is created after shutdown
        self.assertFalse(os.path.exists(folder))
        c.shutdown()

    def test_prefix_and_default_root(self):
        c = self.cache(prefix="saylighttest_")
        self.assertTrue(os.path.basename(c.start()).startswith("saylighttest_"))
        self.assertEqual(AudioCache().root, tempfile.gettempdir())   # not started: nothing created

    def test_new_path_is_unique_and_inside(self):
        c = self.cache()
        with self.assertRaises(RuntimeError):
            c.new_path("g1_c0", ".mp3")
        folder = c.start()
        paths = {c.new_path("g1_c0", ".mp3") for _ in range(50)}
        self.assertEqual(len(paths), 50)
        for p in paths:
            self.assertEqual(os.path.dirname(p), folder)
            self.assertRegex(os.path.basename(p), r"^g1_c0_\d+\.mp3$")
        self.assertTrue(c.new_path("x", "wav").endswith(".wav"))
        for bad in ("", "..", "a/b", "a\\b", "c:x", "..\\up"):
            with self.subTest(stem=bad), self.assertRaises(ValueError):
                c.new_path(bad, ".mp3")

    def test_owns(self):
        c = self.cache()
        folder = c.start()
        self.assertTrue(c.owns(os.path.join(folder, "a.mp3")))
        self.assertTrue(c.owns(os.path.join(folder, "sub", "a.mp3")))
        self.assertTrue(c.owns(os.path.join(folder, "A.MP3").upper()))
        for outside in (folder, os.path.join(folder, LOCK_NAME), os.path.join(folder, "..", "a.mp3"),
                        os.path.join(self.root, "a.mp3"), folder + "x\\a.mp3", "Z:\\a.mp3", ""):
            with self.subTest(path=outside):
                self.assertFalse(c.owns(outside))
        self.assertFalse(AudioCache(self.root).owns(os.path.join(folder, "a.mp3")))   # not started


class ChunkFileTests(CacheTestBase):
    def test_chunk_files_are_short_lived_and_open_in_mci(self):
        c = self.cache()
        c.start()
        part = c.new_path("g1_c0", ".part")
        with c.create(part) as f:
            f.write(silent_wav(400))
        self.assertTrue(os.stat(part).st_file_attributes & stat.FILE_ATTRIBUTE_TEMPORARY)
        wav = part[:-len(".part")] + ".wav"
        os.replace(part, wav)
        self.assertTrue(os.stat(wav).st_file_attributes & stat.FILE_ATTRIBUTE_TEMPORARY)  # kept by the rename
        self.assertAlmostEqual(mci_length(wav), 400, delta=5)
        c.delete(wav)
        self.assertFalse(os.path.exists(wav))

    def test_create_refuses_outside_paths_and_existing_files(self):
        c = self.cache()
        folder = c.start()
        with self.assertRaises(ValueError):
            c.create(os.path.join(self.root, "outside.mp3"))
        path = c.new_path("g1_c0", ".part")
        with c.create(path) as f:
            f.write(b"x")
        with self.assertRaises(FileExistsError):
            c.create(path)
        c.shutdown()
        with self.assertRaises(RuntimeError):          # never recreates the folder after shutdown
            c.create(os.path.join(folder, "late.part"))
        self.assertFalse(os.path.exists(folder))

    def test_create_recreates_a_folder_a_cleaner_removed(self):
        c = self.cache()
        folder = c.start()
        c._unlock()                                    # a held lock would stop any cleaner
        shutil.rmtree(folder)
        path = c.new_path("g1_c0", ".part")
        with c.create(path) as f:
            f.write(b"x")
        self.assertTrue(os.path.isfile(path))

    def test_a_chunk_created_while_shutting_down_never_outlives_the_run(self):
        # shutdown() on another thread while create() is between its checks and the open. It must wait for
        # the open, not delete the folder in the middle so that makedirs brings it back without a lock.
        c = self.cache()
        folder = c.start()
        stopper = threading.Thread(target=c.shutdown)
        real_makedirs = os.makedirs

        def racing_makedirs(path, exist_ok=False):
            if path == folder and stopper.ident is None:
                stopper.start()
                stopper.join(0.3)
            real_makedirs(path, exist_ok=exist_ok)

        with self.assertLogs("saylight.audio_cache", "WARNING"):
            with mock.patch.object(audio_cache.os, "makedirs", racing_makedirs):
                held = c.create(c.new_path("g1_c0", ".part"))
            held.write(b"audio")
            stopper.join(5)                  # shutdown finishes while the file is still open
        held.close()
        self.assertFalse(stopper.is_alive())
        self.assertEqual(purge_orphans(self.root), 1)    # the next start removes it at once
        self.assertFalse(os.path.exists(folder))

    def test_a_folder_left_at_quit_goes_at_the_next_start(self):
        c = self.cache()
        folder = c.start()
        held = c.create(c.new_path("g1_c0", ".mp3"))     # still open at quit, as MCI or a scanner may hold it
        with self.assertLogs("saylight.audio_cache", "WARNING"):
            c.shutdown()
        held.close()
        self.assertTrue(os.path.isfile(os.path.join(folder, LOCK_NAME)))   # a released lock, not the age rule
        with self.assertLogs("saylight.audio_cache", "INFO") as logs:
            self.cache().start()
        self.assertFalse(os.path.exists(folder))
        self.assertIn("(1 unlocked)", logs.records[0].getMessage())

    def test_chunk_handles_are_not_inherited(self):
        c = self.cache()
        c.start()
        with c.create(c.new_path("g1_c0", ".part")) as f:
            self.assertFalse(os.get_inheritable(f.fileno()))
        self.assertFalse(os.get_inheritable(c._lock_fd))


class DeleteTests(CacheTestBase):
    def test_delete_refuses_outside_the_folder_and_the_lock(self):
        c = self.cache()
        folder = c.start()
        outside = os.path.join(self.root, "keep.mp3")
        open(outside, "wb").close()
        for path in (outside, os.path.join(folder, LOCK_NAME), folder):
            with self.subTest(path=path), self.assertLogs("saylight.audio_cache", "WARNING"):
                c.delete(path)
        self.assertTrue(os.path.exists(outside))
        self.assertTrue(os.path.exists(os.path.join(folder, LOCK_NAME)))

    def test_a_file_open_elsewhere_is_retried_without_qt_events(self):
        c = self.cache()
        c.start()
        locked = c.new_path("g1_c1", ".mp3")
        handle = open(locked, "wb")        # an open handle blocks deletion on Windows, like MCI does
        c.delete(locked)
        self.assertTrue(os.path.exists(locked))
        time.sleep(0.4)
        self.assertTrue(os.path.exists(locked))
        handle.close()
        end = time.monotonic() + 3
        while os.path.exists(locked) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertFalse(os.path.exists(locked))

    def test_retries_give_up_after_about_two_seconds(self):
        c = self.cache()
        c.start()
        locked = c.new_path("g1_c1", ".mp3")
        handle = open(locked, "wb")
        self.addCleanup(handle.close)
        with self.assertLogs("saylight.audio_cache", "WARNING") as logs:
            c.delete(locked)
            end = time.monotonic() + 4
            while not logs.records and time.monotonic() < end:
                time.sleep(0.02)
        self.assertIn("still in use", logs.records[0].getMessage())
        self.assertEqual(c._timers, set())

    def test_shutdown_cancels_pending_retries(self):
        c = self.cache()
        c.start()
        locked = c.new_path("g1_c1", ".mp3")
        handle = open(locked, "wb")
        c.delete(locked)
        self.assertEqual(len(c._timers), 1)
        handle.close()
        c.shutdown()
        self.assertEqual(c._timers, set())
        self.assertFalse(os.path.exists(c.dir))


class PurgeTests(CacheTestBase):
    def test_unlocked_folders_go_at_once_and_held_ones_stay(self):
        running = self.cache()
        running_dir = running.start()                                            # another copy, still running
        crashed = self.make_dir("saylight_crashed", lock=True, file_old=False)   # fresh, but its run is gone
        crashed_old = self.make_dir("saylight_crashed_old", lock=True, old=True, file_old=True)
        legacy_running = self.make_dir("saylight_v11", file_old=False)    # 1.0/1.1 hold no lock: age rule
        legacy_stale = self.make_dir("saylight_v10", old=True, file_old=True)
        earlier_name = self.make_dir("read_aloud_old", old=True, file_old=True)
        earlier_busy = self.make_dir("read_aloud_busy", old=True, file_old=False)
        other = self.make_dir("other_old", lock=True, old=True)
        mine = self.cache()
        with self.assertLogs("saylight.audio_cache", "INFO") as logs:
            mine_dir = mine.start()
        for gone in (crashed, crashed_old, legacy_stale, earlier_name):
            self.assertFalse(os.path.exists(gone), gone)
        for kept in (running_dir, legacy_running, earlier_busy, other, mine_dir):
            self.assertTrue(os.path.isdir(kept), kept)
        self.assertEqual(logs.records[0].getMessage(),
                         "Removed 4 audio cache folder(s) left by earlier runs (2 unlocked)")
        self.assertNotIn(self.root, logs.output[0])     # counts only, no paths

    def test_a_killed_run_is_purged_at_once(self):
        code = ("import sys, time; from saylight.audio_cache import AudioCache; "
                "print(AudioCache(sys.argv[1]).start(), flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", code, self.root], cwd=REPO, stdout=subprocess.PIPE,
                                 text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.kill)
        folder = child.stdout.readline().strip()
        self.assertTrue(os.path.isfile(os.path.join(folder, LOCK_NAME)), folder)
        self.assertEqual(purge_orphans(self.root), 0)    # alive: its lock is held
        self.assertTrue(os.path.isdir(folder))
        child.kill()                                     # a crash: no shutdown, the folder stays behind
        child.wait(10)
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(purge_orphans(self.root), 1)
        self.assertFalse(os.path.exists(folder))

    def test_a_crashed_folder_that_cannot_go_yet_stays_marked(self):
        crashed = self.make_dir("saylight_crashed", lock=True, file_old=False)
        held = open(os.path.join(crashed, "0.mp3"), "ab")      # a scanner still has a file open
        self.assertEqual(purge_orphans(self.root), 0)
        held.close()
        self.assertTrue(os.path.isfile(os.path.join(crashed, LOCK_NAME)))
        self.assertEqual(purge_orphans(self.root), 1)          # not left for the age rule
        self.assertFalse(os.path.exists(crashed))

    def test_purge_matches_only_its_prefix(self):
        test_dir = self.make_dir("saylighttest_x", lock=True)
        real_dir = self.make_dir("saylight_x", lock=True)
        earlier = self.make_dir("read_aloud_x", old=True, file_old=True)
        self.assertEqual(purge_orphans(self.root, "saylighttest_"), 1)
        self.assertFalse(os.path.exists(test_dir))
        self.assertTrue(os.path.isdir(real_dir))
        self.assertTrue(os.path.isdir(earlier))          # the earlier name only goes with the default prefix

    def test_keep_and_files_are_skipped(self):
        keep = self.make_dir("saylight_keep", lock=True)
        open(os.path.join(self.root, "saylight_file"), "wb").close()
        self.assertEqual(purge_orphans(self.root, keep=keep), 0)
        self.assertTrue(os.path.isdir(keep))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "saylight_file")))

    def test_purge_stale_dirs_is_the_1_0_name(self):
        old = self.make_dir("saylight_old", old=True, file_old=True)
        unlocked = self.make_dir("saylight_unlocked", lock=True)
        self.assertEqual(purge_stale_dirs(self.root), 2)
        self.assertFalse(os.path.exists(old) or os.path.exists(unlocked))

    def test_missing_root(self):
        self.assertEqual(purge_orphans(os.path.join(self.root, "missing")), 0)


class TestCopyTests(unittest.TestCase):
    def test_prefix_constants_are_defined_once_for_the_test_copy(self):
        # make_test_copy replaces these two lines exactly once to give its build its own cache prefix.
        with open(audio_cache.__file__, encoding="utf-8") as f:
            source = f.read()
        self.assertEqual(source.count('DIR_PREFIX = "saylight_"'), 1)
        self.assertEqual(source.count('OLD_DIR_PREFIXES = ("read_aloud_",)'), 1)
        self.assertEqual(AudioCache.__init__.__kwdefaults__["prefix"], audio_cache.DIR_PREFIX)


if __name__ == "__main__":
    unittest.main()

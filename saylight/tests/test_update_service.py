"""Tests for update_service.py: when the check runs, what it stores, the signals the panel and tray get, and the
Install flow (download, check, start Setup) against the local HTTPS server from test_updates.py. Setup is never
really run: the launcher is a fake, or a Python child that only writes down how it was started."""

import _winapi
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QThread, Signal
from PySide6.QtWidgets import QApplication

from .. import settings as settings_module
from .. import update_service as US
from .. import updates as U
from .test_updates import API_PATH, ETAG, ReleaseServer

_app = None
_quiet = logging.NullHandler()


def setUpModule():
    # keep the expected failure warnings out of the test output (logging's last-resort handler)
    logging.getLogger("saylight.updates").addHandler(_quiet)


def tearDownModule():
    logging.getLogger("saylight.updates").removeHandler(_quiet)


def app():
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])
    return _app


def spin_until(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app().processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        if pred():
            return True
        time.sleep(0.002)
    QCoreApplication.processEvents()
    return pred()


def spin(seconds: float) -> None:
    spin_until(lambda: False, seconds)


class FakeSettings(QObject):
    """The four update keys that behave like the real Settings' get and set (KeyError for unknown keys)."""

    changed = Signal(str)

    def __init__(self, values: dict | None = None) -> None:
        super().__init__()
        self.values = {"updates.check": True, "updates.last_check": 0.0, "updates.etag": "",
                       "updates.skipped": ""}
        self.values.update(values or {})

    def get(self, key):
        if key not in self.values:
            raise KeyError(key)
        return self.values[key]

    def set(self, key, value):
        if key not in self.values:
            raise KeyError(key)
        if self.values[key] != value:
            self.values[key] = value
            self.changed.emit(key)


class _ServiceTest(unittest.TestCase):
    server: ReleaseServer

    @classmethod
    def setUpClass(cls):
        app()
        cls.server = ReleaseServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        self.online = True
        self.busy = False
        self.temp_root = tempfile.mkdtemp(prefix="sl_update_svc_")
        self.addCleanup(shutil.rmtree, self.temp_root, True)
        self.settings = FakeSettings()
        self.spawned: list[tuple[list[str], dict]] = []
        self.service = self.make_service(self.settings)
        self.events: list[tuple] = []
        s = self.service
        s.available.connect(lambda r: self.events.append(("available", r)))
        s.checked.connect(lambda status: self.events.append(("checked", status)))
        s.failed.connect(lambda kind: self.events.append(("failed", kind)))
        s.quitRequested.connect(lambda: self.events.append(("quit",)))
        s.downloadProgress.connect(lambda done, total: self.events.append(("progress", done, total)))
        for name, value in (("FIRST_CHECK_DELAY_MS", 30), ("TICK_MS", 30)):
            patcher = mock.patch.object(US, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_service(self, settings) -> US.UpdateService:
        s = US.UpdateService(settings, "1.2.0", online=lambda: self.online, busy=lambda: self.busy)
        s.site = self.server.site()
        s.context = self.server.context()
        s.temp_root = self.temp_root
        s.spawn = self.fake_spawn
        self.addCleanup(s.deleteLater)
        return s

    def fake_spawn(self, cmd, **kw):
        self.spawned.append((cmd, kw))
        return object()

    def of(self, kind: str) -> list[tuple]:
        return [e for e in self.events if e[0] == kind]

    def wait_checked(self, count: int = 1) -> None:
        self.assertTrue(spin_until(lambda: len(self.of("checked")) >= count), self.events)

    def update_folders(self) -> list[str]:
        return [n for n in os.listdir(self.temp_root) if n.startswith(U.UPDATE_DIR_PREFIX)]


class Checking(_ServiceTest):
    def test_first_check_waits_then_reports_the_release(self):
        self.service.start()
        self.assertEqual(self.server.hits(API_PATH), 0)  # never at once
        self.wait_checked()
        (_, release), = self.of("available")
        self.assertEqual((release.version, release.setup_size, release.sha256),
                         ("1.3.0", len(self.server.payload), self.server.sha256))
        self.assertIs(self.service.latest, release)
        self.assertAlmostEqual(self.settings.values["updates.last_check"], time.time(), delta=5)
        self.assertEqual(self.settings.values["updates.etag"], "")  # a 304 would hide the waiting update

    def test_waits_while_switched_off_offline_or_reading(self):
        self.settings.values["updates.check"] = False
        self.online = False
        self.busy = True
        self.service.start()
        for change in ("switched on", "online", "done reading"):
            spin(0.25)
            self.assertEqual(self.server.requests, [], change)
            if change == "switched on":
                self.settings.values["updates.check"] = True
            elif change == "online":
                self.online = True
            else:
                self.busy = False
        self.wait_checked()
        self.assertEqual(self.server.hits(API_PATH), 1)

    def test_once_a_day_with_the_etag(self):
        self.server.tag = "v1.2.0"
        self.service.start()
        self.wait_checked()
        self.assertEqual((self.of("checked"), self.of("available")), ([("checked", "current")], []))
        self.assertEqual(self.settings.values["updates.etag"], ETAG)
        spin(0.3)  # about ten more ticks
        self.assertEqual(self.server.hits(API_PATH), 1)
        self.settings.values["updates.last_check"] = time.time() - 24 * 3600 - 1
        self.wait_checked(2)
        self.assertEqual(self.server.requests[-1][2].get("If-None-Match"), ETAG)
        self.assertEqual(self.of("checked")[-1], ("checked", "current"))  # 304
        self.assertEqual(self.server.hits(API_PATH), 2)

    def test_not_now_hides_that_version_only(self):
        self.service.skip("v1.3")
        self.assertEqual(self.settings.values["updates.skipped"], "v1.3")
        self.service.start()
        self.wait_checked()
        self.assertEqual(self.of("available"), [])
        self.assertEqual(self.service.latest.version, "1.3.0")  # the tray item stays
        self.service.check_now()  # asked for: shown even though skipped
        self.wait_checked(2)
        self.assertEqual(len(self.of("available")), 1)
        self.server.tag = "v1.3.1"
        self.settings.values["updates.last_check"] = 0.0
        self.wait_checked(3)
        self.assertEqual(self.of("available")[-1][1].version, "1.3.1")

    def test_switching_off_during_a_check_stays_quiet(self):
        self.server.slow_s = 0.3
        self.service.start()
        self.assertTrue(spin_until(lambda: self.server.hits(API_PATH)))
        self.settings.values["updates.check"] = False
        self.wait_checked()
        self.assertEqual((self.of("checked"), self.of("available")), ([("checked", "newer")], []))

    def test_check_now_ignores_the_switch_and_the_daily_limit(self):
        self.settings.values.update({"updates.check": False, "updates.last_check": time.time()})
        self.online = False
        self.service.check_now()
        self.wait_checked()
        self.assertEqual(len(self.of("available")), 1)

    def test_a_tick_while_a_result_is_on_its_way_sends_nothing(self):
        self.settings.values["updates.skipped"] = "1.3.0"
        self.service.check_now()
        # The worker has finished and queued its result, but the GUI thread hasn't run it yet.
        end = time.monotonic() + 5
        while self.service._checker.running and time.monotonic() < end:
            time.sleep(0.005)
        self.service._tick()       # last_check isn't stored yet, so the check still looks due
        self.service.check_now()   # and a second click
        time.sleep(0.3)
        self.assertEqual(self.server.hits(API_PATH), 1)
        self.wait_checked()
        spin(0.2)
        self.assertEqual((len(self.of("checked")), len(self.of("available"))), (1, 1))  # shown: asked for
        self.settings.values["updates.last_check"] = 0.0
        self.service._tick()       # a later scheduled check is not mistaken for the manual one
        self.wait_checked(2)
        self.assertEqual((self.server.hits(API_PATH), len(self.of("available"))), (2, 1))

    def test_errors_count_and_fall_back(self):
        self.server.api_mode = "404"  # no release yet, so no fallback either
        self.service.check_now()
        self.wait_checked()
        self.assertEqual((self.of("checked"), self.of("available")), ([("checked", "error")], []))
        self.assertGreater(self.settings.values["updates.last_check"], 0)
        self.server.api_mode = "403"  # rate limited: the releases/latest redirect answers instead
        self.service.check_now()
        self.wait_checked(2)
        (_, release), = self.of("available")
        self.assertEqual((release.version, release.setup_size), ("1.3.0", 0))
        self.assertTrue(release.checksum_url.endswith("/Saylight-Setup.exe.sha256"))

    def test_signals_arrive_on_the_gui_thread(self):
        threads = []
        gui = QCoreApplication.instance().thread()
        for signal in (self.service.available, self.service.checked, self.service.downloadProgress,
                       self.service.quitRequested):
            signal.connect(lambda *a: threads.append(QThread.currentThread() is gui))
        self.service.check_now()
        self.wait_checked()
        self.service.install(self.service.latest)
        self.assertTrue(spin_until(lambda: self.of("quit")))
        self.assertGreater(len(threads), 3)
        self.assertTrue(all(threads))

    @unittest.skipUnless("updates.check" in settings_module.SCHEMA, "the settings file has no update keys yet")
    def test_with_the_real_settings(self):
        path = os.path.join(self.temp_root, "saylight_settings.json")
        real = settings_module.Settings(path)
        self.addCleanup(real.deleteLater)
        service = self.make_service(real)
        done = []
        service.checked.connect(done.append)
        service.start()
        self.assertTrue(spin_until(lambda: done))
        self.assertGreater(real.get("updates.last_check"), 0)
        service.skip("1.3.0")
        self.assertEqual(real.get("updates.skipped"), "1.3.0")
        self.assertTrue(real.flush())


class Installing(_ServiceTest):
    def install(self, release: U.Release | None = None) -> None:
        self.service.install(release or self.server.release())
        self.assertTrue(spin_until(lambda: self.of("quit") or self.of("failed")), self.events)

    def assert_failed(self, kind: str, release: U.Release | None = None) -> None:
        self.install(release)
        self.assertEqual((self.of("failed"), self.of("quit")), ([("failed", kind)], []))
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.update_folders(), [])  # nothing kept after a failure
        self.assertFalse(self.service.installing)    # Install can be tried again

    def test_downloads_checks_and_starts_setup(self):
        self.install()
        self.assertEqual((self.of("quit"), self.of("failed")), ([("quit",)], []))
        (cmd, kw), = self.spawned
        folder, = self.update_folders()
        path = os.path.join(self.temp_root, folder, "Saylight-Setup.exe")
        self.assertEqual(cmd, [path, "/SILENT", "/NORESTART"])
        self.assertEqual(kw["cwd"], self.temp_root)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.server.payload)
        size = len(self.server.payload)
        progress = self.of("progress")
        self.assertEqual((progress[0], progress[-1]), (("progress", 0, size), ("progress", size, size)))
        self.assertEqual(self.server.hits("/storage/Saylight-Setup.exe.sha256"), 1)
        self.service.install(self.server.release())  # a second click while Setup starts does nothing
        spin(0.2)
        self.assertEqual(len(self.spawned), 1)

    def test_starts_a_real_process(self):
        procs = []
        script = ("import os, sys; open(sys.argv[1] + '.ran', 'w').write(os.getcwd() + '|' + ' '.join(sys.argv[2:]))")

        def spawn(cmd, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen([sys.executable, "-c", script, *cmd], **kw)
            procs.append(proc)
            return proc

        self.service.spawn = spawn
        self.install()
        self.assertEqual(self.of("quit"), [("quit",)])
        self.assertEqual(procs[-1].wait(10), 0)
        folder, = self.update_folders()
        with open(os.path.join(self.temp_root, folder, "Saylight-Setup.exe.ran")) as f:
            cwd, args = f.read().split("|")
        self.assertEqual((os.path.normcase(cwd), args), (os.path.normcase(self.temp_root), "/SILENT /NORESTART"))

    def test_wrong_checksum(self):
        self.server.served_setup = bytes(reversed(self.server.payload))
        self.assert_failed("checksum")
        self.assertEqual(self.server.hits("/storage/Saylight-Setup.exe", "GET"), 2)  # .sha256, then the file

    def test_no_checksum_published(self):
        self.server.checksum_text = "missing"
        self.server.asset_overrides = {"digest": None}
        self.assert_failed("checksum")
        self.assertEqual(self.server.hits("/storage/Saylight-Setup.exe", "GET"), 1)  # the installer isn't fetched

    def test_wrong_size(self):
        release = self.server.release()
        self.server.payload += b"\x00"  # the server now publishes and serves a different file
        self.server.checksum_text = f"{hashlib.sha256(self.server.payload).hexdigest()}  Saylight-Setup.exe"
        self.assert_failed("size", U.Release(release.version, release.url, release.setup_url, release.setup_size,
                                             release.checksum_url, ""))

    def test_network_failures(self):
        release = self.server.release()
        self.assert_failed("network", U.Release("1.3.0", release.url, "", 0))  # the release has no installer
        self.events.clear()
        self.server.setup_mode = "missing"
        self.assert_failed("network", release)
        self.events.clear()
        self.server.reset()
        self.server.asset_location = "https://evil.example/Saylight-Setup.exe"
        self.assert_failed("network", release)

    def test_launch_failure(self):
        def broken(cmd, **kw):
            raise OSError(740, "The requested operation requires elevation")
        self.service.spawn = broken
        self.assert_failed("launch")

    def test_logs_hold_no_paths_or_names(self):
        with self.assertLogs("saylight.updates", level="DEBUG") as logs:
            self.service.check_now()
            self.wait_checked()
            self.server.served_setup = b"x" * len(self.server.payload)
            self.assert_failed("checksum")
            self.server.served_setup = None
            self.events.clear()
            self.install()
            self.assertEqual(self.of("quit"), [("quit",)])
        text = "\n".join(logs.output)
        self.assertIn("checksum", text)
        for secret in (self.temp_root, os.path.basename(self.temp_root), U.UPDATE_DIR_PREFIX, "Saylight-Setup",
                       "127.0.0.1", "github"):
            self.assertNotIn(secret, text)


class WhereInstallRuns(unittest.TestCase):
    def test_only_the_installed_app_without_smart_app_control(self):
        app()
        cases = (((True, False), True), ((True, True), False), ((False, False), False), ((False, True), False))
        for (frozen, sac), expected in cases:
            with self.subTest(frozen=frozen, smart_app_control=sac), \
                    mock.patch.object(US.paths, "is_frozen", return_value=frozen), \
                    mock.patch.object(US.win32, "smart_app_control_on", return_value=sac):
                service = US.UpdateService(FakeSettings(), "1.2.0", online=lambda: True, busy=lambda: False)
                self.assertIs(service.can_install, expected)
                service.deleteLater()


class Cleanup(_ServiceTest):
    def test_purge_update_downloads(self):
        for name in ("saylight_update_a", "saylight_update_b", "saylight_other", "keep"):
            os.mkdir(os.path.join(self.temp_root, name))
        with open(os.path.join(self.temp_root, "saylight_update_a", "Saylight-Setup.exe"), "wb") as f:
            f.write(b"old")
        with open(os.path.join(self.temp_root, "saylight_update_note.txt"), "w") as f:
            f.write("a file, not a folder")
        self.assertEqual(US.purge_update_downloads(self.temp_root), 2)
        self.assertEqual(sorted(os.listdir(self.temp_root)), ["keep", "saylight_other", "saylight_update_note.txt"])
        self.assertEqual(US.purge_update_downloads(os.path.join(self.temp_root, "missing")), 0)

    def test_start_clears_old_downloads(self):
        os.mkdir(os.path.join(self.temp_root, "saylight_update_old"))
        self.settings.values["updates.check"] = False
        self.service.start()
        self.assertTrue(spin_until(lambda: not self.update_folders()))

    def test_purge_never_follows_a_junction(self):
        outside = tempfile.mkdtemp(prefix="sl_update_outside_")
        self.addCleanup(shutil.rmtree, outside, True)
        with open(os.path.join(outside, "keep.txt"), "w") as f:
            f.write("not Saylight's")
        link = os.path.join(self.temp_root, "saylight_update_link")
        _winapi.CreateJunction(outside, link)
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))  # removes the link, not the target
        self.assertEqual(US.purge_update_downloads(self.temp_root), 0)
        self.assertEqual(os.listdir(outside), ["keep.txt"])

    def test_install_waits_for_the_start_up_purge(self):
        gate = threading.Event()
        purge = US.purge_update_downloads

        def slow_purge(root):
            gate.wait(5)
            return purge(root)

        self.settings.values["updates.check"] = False
        with mock.patch.object(US, "purge_update_downloads", slow_purge):
            self.service.start()
            self.service.install(self.server.release())  # clicked before the purge got to the folder list
            spin(0.3)
            self.assertEqual((self.update_folders(), self.of("quit")), ([], []))
            gate.set()
            self.assertTrue(spin_until(lambda: self.of("quit") or self.of("failed")), self.events)
        self.assertEqual(self.of("quit"), [("quit",)])
        folder, = self.update_folders()  # the purge didn't take the new download
        (cmd, _), = self.spawned
        self.assertTrue(os.path.isfile(cmd[0]))


if __name__ == "__main__":
    unittest.main()

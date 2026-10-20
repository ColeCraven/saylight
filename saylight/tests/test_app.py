"""Tests for saylight_app.py: moving the settings over from the earlier name, and replacing the earlier install.

No test here touches the registry, named events or the Start menu: the system operations are replaced by a fake,
and the modules that reach the real ones are swapped for stand-ins that fail the test if used.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

import saylight_app as A
from saylight.settings import Settings

_app = None


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class MigrateSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_app_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.new = os.path.join(self.dir, "saylight_settings.json")
        self.old = os.path.join(self.dir, "read_aloud_settings.json")
        self.sleeps: list[float] = []
        patcher = mock.patch.object(A, "time", types.SimpleNamespace(sleep=self.sleeps.append))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path: str, obj: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def read(self, path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_names_match_the_app(self):
        self.assertEqual(os.path.basename(A.SETTINGS_PATH), "saylight_settings.json")
        self.assertEqual(os.path.basename(A.OLD_SETTINGS_PATH), "read_aloud_settings.json")
        self.assertEqual(os.path.dirname(A.SETTINGS_PATH), os.path.dirname(A.OLD_SETTINGS_PATH))

    def test_old_file_is_renamed_and_its_settings_load(self):
        mine = {"speed": 1.25, "volume": 40, "reading_font": "sitka", "panel": {"visible": False}}
        self.write(self.old, mine)
        with self.assertLogs("saylight.app", "INFO") as logs:
            path = A.migrate_settings(self.new, self.old)
        self.assertEqual(path, self.new)
        self.assertFalse(os.path.exists(self.old))
        self.assertEqual(self.read(self.new), mine)
        self.assertIn("read_aloud_settings.json", logs.output[0])
        settings = Settings(path)
        self.assertEqual((settings.get("speed"), settings.get("volume"), settings.get("reading_font")),
                         (1.25, 40, "sitka"))

    def test_existing_new_file_wins_and_old_is_left_alone(self):
        self.write(self.new, {"speed": 0.9})
        self.write(self.old, {"speed": 1.5})
        with self.assertNoLogs("saylight.app", "INFO"):
            self.assertEqual(A.migrate_settings(self.new, self.old), self.new)
        self.assertEqual(self.read(self.new), {"speed": 0.9})
        self.assertEqual(self.read(self.old), {"speed": 1.5})

    def test_nothing_to_migrate(self):
        self.assertEqual(A.migrate_settings(self.new, self.old), self.new)
        self.assertEqual(os.listdir(self.dir), [])

    def test_brief_lock_is_retried(self):
        self.write(self.old, {"speed": 1.1})
        real, calls = os.rename, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 1:
                raise PermissionError(32, "in use")
            real(src, dst)

        with mock.patch.object(A.os, "rename", flaky), self.assertLogs("saylight.app", "INFO"):
            self.assertEqual(A.migrate_settings(self.new, self.old), self.new)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.read(self.new), {"speed": 1.1})

    def test_lasting_lock_uses_the_old_file_this_session(self):
        self.write(self.old, {"speed": 1.1})
        with mock.patch.object(A.os, "rename", side_effect=PermissionError(32, "in use")) as rename, \
                self.assertLogs("saylight.app", "ERROR"):
            self.assertEqual(A.migrate_settings(self.new, self.old), self.old)
        self.assertEqual(rename.call_count, 4)
        self.assertEqual(self.read(self.old), {"speed": 1.1})  # untouched, so the next start can try again
        self.assertFalse(os.path.exists(self.new))


class _Forbidden:
    """Stands in for a module that reaches the real system; any use fails the test."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str):
        raise AssertionError(f"the test reached the real {self._name}.{attr}")


class FakeSystem:
    """The registry, named events and files as the commands see them, with every call recorded."""

    def __init__(self, *, running=(), run_values=(), files=()) -> None:
        self.running, self.run_values, self.files = set(running), set(run_values), set(files)
        self.calls: list[tuple] = []

    def signal_quit(self, name: str) -> bool:
        self.calls.append(("signal", name))
        return name in self.running

    def set_run_value(self, name: str, command: str) -> None:
        self.calls.append(("set_run", name, command))
        self.run_values.add(name)

    def delete_run_value(self, name: str) -> bool:
        self.calls.append(("delete_run", name))
        found = name in self.run_values
        self.run_values.discard(name)
        return found

    def remove_file(self, path: str) -> bool:
        self.calls.append(("remove", path))
        found = path in self.files
        self.files.discard(path)
        return found

    def sleep(self, seconds: float) -> None:
        self.calls.append(("sleep", seconds))

    @contextlib.contextmanager
    def installed(self):
        """Route saylight_app's system operations here; the real modules behind them are made unusable."""
        with mock.patch.multiple(A, signal_quit=self.signal_quit, set_run_value=self.set_run_value,
                                 delete_run_value=self.delete_run_value, remove_file=self.remove_file,
                                 create_shortcut=lambda: self.calls.append(("create_shortcut",)),
                                 start=lambda: self.calls.append(("start",)),
                                 time=types.SimpleNamespace(sleep=self.sleep), winreg=_Forbidden("winreg"),
                                 kernel32=_Forbidden("kernel32"), subprocess=_Forbidden("subprocess")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            yield out


OLD_LNK = A.start_menu_path("Read Aloud.lnk")
NEW_LNK = A.start_menu_path("Saylight.lnk")
OLD_EVENT, NEW_EVENT = "Local\\ReadAloudHotkey.Quit", "Local\\Saylight.Quit"


class OldInstallTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual((A.APP_NAME, A.QUIT_EVENT_NAME, A.SHORTCUT_NAME), ("Saylight", NEW_EVENT, "Saylight.lnk"))
        self.assertEqual((A.OLD_RUN_VALUE, A.OLD_QUIT_EVENT_NAME, A.OLD_SHORTCUT_NAME),
                         ("ReadAloudHotkey", OLD_EVENT, "Read Aloud.lnk"))
        self.assertEqual(A.APP_USER_MODEL_ID, "Saylight.App")
        self.assertTrue(OLD_LNK.endswith(r"Microsoft\Windows\Start Menu\Programs\Read Aloud.lnk"))
        self.assertEqual(os.path.basename(A.OLD_ICON_PATH), "read_aloud.ico")

    def test_start_menu_path_without_appdata_is_still_absolute(self):
        env = {k: v for k, v in os.environ.items() if k.upper() != "APPDATA"}
        with mock.patch.dict(os.environ, env, clear=True):
            path = A.start_menu_path("Read Aloud.lnk")
        self.assertTrue(os.path.isabs(path), path)  # never a path relative to the current folder
        self.assertTrue(path.endswith(r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Read Aloud.lnk"), path)

    def test_remove_old_install_quits_it_then_removes_its_entries(self):
        fake = FakeSystem(running={OLD_EVENT}, run_values={"ReadAloudHotkey", "Other"},
                          files={OLD_LNK, A.OLD_ICON_PATH, NEW_LNK})
        with fake.installed():
            done = A.remove_old_install()
        self.assertEqual(done, ["quit", "run", "shortcut", "icon"])
        self.assertEqual(fake.calls, [("signal", OLD_EVENT), ("sleep", A.QUIT_WAIT_S),
                                      ("delete_run", "ReadAloudHotkey"), ("remove", OLD_LNK),
                                      ("remove", A.OLD_ICON_PATH)])
        self.assertEqual(fake.run_values, {"Other"})
        self.assertEqual(fake.files, {NEW_LNK})

    def test_remove_old_install_with_nothing_there_does_not_wait(self):
        fake = FakeSystem()
        with fake.installed():
            self.assertEqual(A.remove_old_install(), [])
        self.assertNotIn("sleep", [c[0] for c in fake.calls])
        self.assertEqual([c for c in fake.calls if c[0] != "remove"], [("signal", OLD_EVENT),
                                                                      ("delete_run", "ReadAloudHotkey")])

    def test_install_replaces_the_old_install_before_adding_the_new_one(self):
        fake = FakeSystem(running={OLD_EVENT}, run_values={"ReadAloudHotkey"}, files={OLD_LNK})
        with fake.installed() as out:
            A.install()
        kinds = [c[0] for c in fake.calls]
        self.assertLess(kinds.index("delete_run"), kinds.index("set_run"))
        self.assertLess(fake.calls.index(("remove", OLD_LNK)), kinds.index("create_shortcut"))
        self.assertEqual(kinds[-1], "start")
        self.assertIn(("signal", NEW_EVENT), fake.calls)
        set_run = [c for c in fake.calls if c[0] == "set_run"]
        self.assertEqual(len(set_run), 1)
        self.assertEqual(set_run[0][1], "Saylight")
        self.assertTrue(set_run[0][2].endswith('saylight_app.py"'), set_run[0][2])
        self.assertEqual(fake.run_values, {"Saylight"})
        self.assertNotIn(OLD_LNK, fake.files)
        self.assertIn("Replaced the earlier version", out.getvalue())

    def test_install_without_an_old_install_says_nothing_about_it(self):
        fake = FakeSystem()
        with fake.installed() as out:
            A.install()
        self.assertNotIn("earlier version", out.getvalue())
        self.assertEqual(fake.run_values, {"Saylight"})

    def test_uninstall_removes_old_and_new_entries_and_quits_both(self):
        fake = FakeSystem(running={OLD_EVENT, NEW_EVENT}, run_values={"Saylight", "ReadAloudHotkey", "Other"},
                          files={NEW_LNK, OLD_LNK})
        with fake.installed() as out:
            A.uninstall()
        self.assertEqual(fake.run_values, {"Other"})
        self.assertEqual(fake.files, set())
        self.assertIn(("signal", NEW_EVENT), fake.calls)
        self.assertIn(("signal", OLD_EVENT), fake.calls)
        self.assertIn("Saylight has quit.", out.getvalue())

    def test_quit_signals_both_names(self):
        for running, expected in (({OLD_EVENT}, True), ({NEW_EVENT}, True), (set(), False)):
            fake = FakeSystem(running=running)
            with self.subTest(running=running), fake.installed() as out:
                self.assertIs(A.quit_running(), expected)
                self.assertEqual(sorted(c[1] for c in fake.calls if c[0] == "signal"), sorted([OLD_EVENT, NEW_EVENT]))
                self.assertIn("Saylight has quit." if expected else "Saylight isn't running.", out.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Tests for paths.py: the install and data folders, the launch command, and moving 1.0 and 1.1 data over.

Every test works in temporary folders: the data folder comes from SAYLIGHT_DATA_DIR or a stand-in
LOCALAPPDATA, so the real %LOCALAPPDATA%\\Saylight is never created or touched.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from saylight import paths
from saylight.settings import Settings

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def setUpModule() -> None:
    QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="saylight_paths_test_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def env(self, **values: str):
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("SAYLIGHT_DATA_DIR", "LOCALAPPDATA")}
        env.update(values)
        return mock.patch.dict(os.environ, env, clear=True)

    def frozen(self, exe: str):
        return mock.patch.multiple(sys, frozen=True, executable=exe, create=True)


class DataFolderTests(Folder):
    def test_the_override_is_used_and_created(self) -> None:
        target = os.path.join(self.root, "data", "deeper")
        with self.env(SAYLIGHT_DATA_DIR=target):
            self.assertEqual(paths.data_dir(), target)
        self.assertTrue(os.path.isdir(target))

    def test_default_is_saylight_in_local_app_data(self) -> None:
        with self.env(LOCALAPPDATA=self.root):
            self.assertEqual(paths.data_dir(), os.path.join(self.root, "Saylight"))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "Saylight")))

    def test_a_blank_override_is_ignored(self) -> None:
        with self.env(SAYLIGHT_DATA_DIR="  ", LOCALAPPDATA=self.root):
            self.assertEqual(paths.data_dir(), os.path.join(self.root, "Saylight"))

    def test_every_data_file_is_in_the_data_folder(self) -> None:
        with self.env(SAYLIGHT_DATA_DIR=self.root):
            found = {f.__name__: f() for f in (paths.settings_path, paths.log_path, paths.crash_path,
                                               paths.pronunciations_path, paths.resume_path, paths.voices_dir,
                                               paths.engines_dir)}
        self.assertTrue(all(os.path.dirname(p) == self.root for p in found.values()), found)
        self.assertEqual(os.path.basename(found["settings_path"]), "saylight_settings.json")
        self.assertEqual(os.path.basename(found["log_path"]), "saylight.log")
        self.assertEqual(os.path.basename(found["crash_path"]), "saylight_crash.log")
        self.assertEqual(os.listdir(self.root), [])  # only data_dir() itself creates anything

    def test_a_folder_that_cannot_be_made_is_still_named(self) -> None:
        blocker = os.path.join(self.root, "file")
        open(blocker, "w").close()
        with self.env(SAYLIGHT_DATA_DIR=os.path.join(blocker, "data")):
            self.assertEqual(paths.data_dir(), os.path.join(blocker, "data"))


class InstallFolderTests(Folder):
    def test_from_source_it_is_the_folder_holding_saylight_app(self) -> None:
        self.assertFalse(paths.is_frozen())
        self.assertEqual(os.path.normcase(paths.install_dir()), os.path.normcase(REPO))
        self.assertTrue(os.path.isfile(os.path.join(paths.install_dir(), "saylight_app.py")))

    def test_from_source_it_starts_with_pythonw(self) -> None:
        pythonw, script = paths.launch_command()
        self.assertEqual(os.path.basename(pythonw).lower(), "pythonw.exe")
        self.assertEqual(os.path.dirname(pythonw), os.path.dirname(os.path.abspath(sys.executable)))
        self.assertEqual(script, os.path.join(paths.install_dir(), "saylight_app.py"))

    def test_frozen_it_is_the_exe_and_its_folder(self) -> None:
        exe = os.path.join(self.root, "Programs", "Saylight", "Saylight.exe")
        with self.frozen(exe):
            self.assertTrue(paths.is_frozen())
            self.assertEqual(paths.install_dir(), os.path.dirname(exe))
            self.assertEqual(paths.launch_command(), [exe])


class MigrationTests(Folder):
    def setUp(self) -> None:
        super().setUp()
        self.app = os.path.join(self.root, "app")
        self.data = os.path.join(self.root, "data")
        os.makedirs(self.app)
        os.makedirs(self.data)
        self.sleeps: list[float] = []
        patcher = mock.patch.object(paths, "time", types.SimpleNamespace(sleep=self.sleeps.append))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path: str, obj: dict, age_s: float = 0.0) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        if age_s:
            stamp = time.time() - age_s
            os.utime(path, (stamp, stamp))

    def read(self, path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def at(self, folder: str, name: str = "saylight_settings.json") -> str:
        return os.path.join(folder, name)

    def migrate(self) -> list[str]:
        with self.env(SAYLIGHT_DATA_DIR=self.data), mock.patch.object(paths, "install_dir", lambda: self.app):
            return paths.migrate_legacy_data()

    def test_settings_move_into_the_data_folder_and_load_unchanged(self) -> None:
        mine = {"speed": 1.25, "volume": 40, "reading_font": "sitka", "voice": "en-GB-SoniaNeural",
                "panel": {"visible": False}}
        self.write(self.at(self.app), mine)
        with self.assertLogs("saylight.paths", "INFO") as logs:
            self.assertEqual(self.migrate(), ["saylight_settings.json"])
        self.assertFalse(os.path.exists(self.at(self.app)))
        self.assertEqual(self.read(self.at(self.data)), mine)
        self.assertEqual(logs.output, ["INFO:saylight.paths:Moved the earlier settings into the data folder"])
        s = Settings(self.at(self.data))
        self.assertEqual((s.get("speed"), s.get("volume"), s.get("voice")), (1.25, 40, "edge:en-GB-SoniaNeural"))

    def test_the_read_aloud_name_moves_under_the_new_name(self) -> None:
        self.write(self.at(self.app, "read_aloud_settings.json"), {"speed": 1.1})
        self.assertEqual(self.migrate(), ["saylight_settings.json"])
        self.assertEqual(self.read(self.at(self.data)), {"speed": 1.1})
        self.assertEqual(os.listdir(self.app), [])

    def test_with_both_names_the_newer_name_moves_and_the_old_file_stays(self) -> None:
        self.write(self.at(self.app), {"speed": 0.9})
        self.write(self.at(self.app, "read_aloud_settings.json"), {"speed": 1.5})
        self.assertEqual(self.migrate(), ["saylight_settings.json"])
        self.assertEqual(self.read(self.at(self.data)), {"speed": 0.9})
        self.assertEqual(os.listdir(self.app), ["read_aloud_settings.json"])

    def test_newer_data_in_the_data_folder_is_never_overwritten(self) -> None:
        self.write(self.at(self.app), {"speed": 1.5}, age_s=3600)
        self.write(self.at(self.data), {"speed": 0.9})
        with self.assertNoLogs("saylight.paths", "INFO"):
            self.assertEqual(self.migrate(), [])
        self.assertEqual(self.read(self.at(self.data)), {"speed": 0.9})
        self.assertEqual(self.read(self.at(self.app)), {"speed": 1.5})  # left where it is

    def test_an_older_copy_in_the_data_folder_is_replaced(self) -> None:
        # For example 1.1 was run again from source after 1.2 had moved the settings.
        self.write(self.at(self.data), {"speed": 0.9}, age_s=3600)
        self.write(self.at(self.app), {"speed": 1.5})
        self.assertEqual(self.migrate(), ["saylight_settings.json"])
        self.assertEqual(self.read(self.at(self.data)), {"speed": 1.5})
        self.assertFalse(os.path.exists(self.at(self.app)))

    def test_nothing_to_move(self) -> None:
        with self.assertNoLogs("saylight.paths", "INFO"):
            self.assertEqual(self.migrate(), [])
        self.assertEqual((os.listdir(self.app), os.listdir(self.data)), ([], []))

    def test_the_same_folder_is_left_alone(self) -> None:
        self.write(self.at(self.data), {"speed": 1.1})
        with self.env(SAYLIGHT_DATA_DIR=self.data), mock.patch.object(paths, "install_dir", lambda: self.data):
            self.assertEqual(paths.migrate_legacy_data(), [])
        self.assertEqual(self.read(self.at(self.data)), {"speed": 1.1})

    def test_a_brief_lock_is_retried(self) -> None:
        self.write(self.at(self.app), {"speed": 1.1})
        real, calls = os.rename, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 1:
                raise PermissionError(32, "in use")
            real(src, dst)

        with mock.patch.object(paths.os, "rename", flaky):
            self.assertEqual(self.migrate(), ["saylight_settings.json"])
        self.assertEqual((len(calls), self.sleeps), (2, [0.05]))
        self.assertEqual(self.read(self.at(self.data)), {"speed": 1.1})

    def test_a_lasting_lock_is_skipped_and_logged_without_names(self) -> None:
        self.write(self.at(self.app), {"speed": 1.1})
        with mock.patch.object(paths.os, "rename", side_effect=PermissionError(32, "in use", "C:\\secret")) as rename, \
                self.assertLogs("saylight.paths", "WARNING") as logs:
            self.assertEqual(self.migrate(), [])
        self.assertEqual(rename.call_count, 4)
        self.assertEqual(self.read(self.at(self.app)), {"speed": 1.1})  # untouched, for the next start
        self.assertFalse(os.path.exists(self.at(self.data)))
        self.assertEqual(logs.output, ["WARNING:saylight.paths:Couldn't move the earlier settings into the data "
                                       "folder (PermissionError); trying again next start"])

    def test_a_move_to_another_drive_copies_then_removes(self) -> None:
        self.write(self.at(self.app), {"speed": 1.1})
        other_drive = OSError(18, "not same device")
        other_drive.winerror = 17
        real = os.rename

        def rename(src, dst):
            if src == self.at(self.app):
                raise other_drive
            real(src, dst)

        with mock.patch.object(paths.os, "rename", rename):
            self.assertEqual(self.migrate(), ["saylight_settings.json"])
        self.assertEqual(self.read(self.at(self.data)), {"speed": 1.1})
        self.assertEqual((os.listdir(self.app), os.listdir(self.data)), ([], ["saylight_settings.json"]))


if __name__ == "__main__":
    unittest.main()

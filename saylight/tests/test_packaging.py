"""Tests for the packaging scripts in packaging/: the licence gate, the test copy, the build stamp that keeps test
builds and release builds apart, the lifecycle script's sandbox rule and the release workflow. Nothing is built,
installed or started: the installer checks compile with output turned off, against a fake build folder."""

import ctypes
import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from ctypes import wintypes
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACKAGING = os.path.join(REPO, "packaging")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"packaging_{name}", os.path.join(PACKAGING, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


cn = _load("collect_notices")
mtc = _load("make_test_copy")
selftest = _load("selftest")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


BSD3 = """Copyright (c) 2020 Someone. All rights reserved.
Redistribution and use in source and binary forms, with or without modification, are permitted provided that
the following conditions are met: 1. Redistributions of source code must retain the above copyright notice.
2. Redistributions in binary form must reproduce the above copyright notice.
3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or
promote products derived from this software without specific prior written permission."""
BSD2 = BSD3[:BSD3.index("3. Neither")]
BSD4 = BSD3 + """
4. All advertising materials mentioning features or use of this software must display the following
acknowledgement: This product includes software developed by Someone."""
MIT = "Permission is hereby granted, free of charge, to any person obtaining a copy of this software."


class LicenceGateTests(unittest.TestCase):
    def test_pillow_numpy_and_tk_are_never_bundled(self):
        inv = cn.Inventory(
            archive_modules=["PIL.Image", "numpy.core.multiarray", "saylight.engine", "typing_extensions"],
            script_entries=["saylight_app", "pyi_rth_inspect"],
            files=["PIL/_imaging.cp314-win_amd64.pyd", "_tcl_data/init.tcl", "tcl86t.dll", "tk86t.dll",
                   "_tkinter.cp314-win_amd64.pyd", "pillow-12.3.0.dist-info/METADATA", "numpy.libs/x.dll",
                   "PySide6/Qt6Core.dll", "certifi/cacert.pem", "edge_tts/communicate.py"])
        self.assertEqual(cn.never_bundled(inv), [
            "PIL", "_tcl_data", "_tkinter.cp314-win_amd64.pyd", "numpy", "numpy.libs",
            "pillow-12.3.0.dist-info", "tcl86t.dll", "tk86t.dll"])

    def test_a_clean_build_has_nothing_never_bundled(self):
        inv = cn.Inventory(
            archive_modules=["saylight.ui.panel", "aiohttp.client", "tabulate", "tokenize", "tomllib", "typing"],
            script_entries=["saylight_app", "pyiboot01_bootstrap", "struct"],
            files=["PySide6/Qt6Core.dll", "PySide6/plugins/platforms/qwindows.dll", "edge_tts/__init__.py",
                   "python314.dll", "libssl-3.dll", "_ssl.pyd", "base_library.zip", "saylight.ico",
                   "aiohttp-3.14.5.dist-info/licenses/LICENSE.txt"])
        self.assertEqual(cn.never_bundled(inv), [])

    def test_bare_bsd_is_decided_by_the_licence_text(self):
        self.assertNotIn("BSD", cn.ALLOWED)
        with tempfile.TemporaryDirectory() as tmp:
            def variant(text):
                path = os.path.join(tmp, "LICENSE")
                _write(path, text)
                return cn.bsd_variant([("LICENSE", path)])

            self.assertEqual(variant(BSD3), "BSD-3-Clause")
            self.assertEqual(variant(BSD2), "BSD-2-Clause")
            self.assertIsNone(variant(BSD4))  # the advertising clause
            self.assertIsNone(variant(MIT))
            self.assertIsNone(cn.bsd_variant([("LICENSE", os.path.join(tmp, "missing"))]))

    def test_bare_bsd_comes_last_among_alternatives(self):
        self.assertEqual(cn.choose("somepkg", [["BSD"], ["MIT"]]), ["MIT"])
        self.assertEqual(cn.choose("somepkg", [["BSD"]]), ["BSD"])
        self.assertEqual(cn.classify("License :: OSI Approved :: BSD License"), "BSD")

    def test_gpl_never_passes_and_lgpl_only_for_the_replaceable_packages(self):
        self.assertIsNone(cn.choose("somepkg", [["GPL"]]))
        self.assertIsNone(cn.choose("somepkg", [["AGPL"]]))
        self.assertIsNone(cn.choose("somepkg", [["LGPL-3.0"]]))
        self.assertIsNone(cn.choose("edge-tts", [["LGPL"]]))  # LGPL of an unknown version
        self.assertEqual(cn.choose("edge-tts", [["LGPL-3.0"]]), ["LGPL-3.0"])
        self.assertEqual(cn.choose("pyside6-essentials", [["LGPL-3.0"], ["GPL"]]), ["LGPL-3.0"])
        self.assertIsNone(cn.choose("somepkg", [cn.alternatives("GPL-2.0-or-later WITH Bootloader-exception")[0]]))

    def test_a_malformed_licence_expression_is_reported_not_raised(self):
        class Meta(dict):
            def get_all(self, _name):
                return None

        said, alts = cn.declared(Meta({"License-Expression": "MIT AND ("}))
        self.assertEqual(said, "MIT AND (")
        self.assertIsNone(cn.choose("somepkg", alts))
        said, alts = cn.declared(Meta({"License-Expression": "Apache-2.0 AND MIT"}))
        self.assertEqual(cn.choose("aiohttp", alts), ["Apache-2.0", "MIT"])


FAKE_APP = '''"""A stand-in for saylight_app.py with the names the test copy replaces."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "Saylight"
QUIT_EVENT_NAME = "Local\\\\Saylight.Quit"
APP_USER_MODEL_ID = "Saylight.App"
SHORTCUT_NAME = "Saylight.lnk"
SETTINGS_PATH = os.path.join(HERE, "saylight_settings.json")
LOG_PATH = os.path.join(HERE, "saylight.log")
CRASH_PATH = os.path.join(HERE, "saylight_crash.log")
OLD_QUIT_EVENT_NAME = "Local\\\\ReadAloudHotkey.Quit"
'''
FAKE_SETTINGS = '''DEFAULTS = {
    "panel.visible": (True, bool),
    "media_keys": (True, bool),
    "hotkeys.read": ("Ctrl+`", str),
    "hotkeys.toggle_panel": ("", str),
%s}
'''


class TestCopyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.src = os.path.join(self.tmp, "src")
        self.dest = os.path.join(self.tmp, "copy")

    def tearDown(self):
        self._tmp.cleanup()

    def _source(self, update_line: str = '    "updates.check": (True, bool),\n') -> None:
        _write(os.path.join(self.src, "saylight_app.py"), FAKE_APP)
        _write(os.path.join(self.src, "saylight", "__init__.py"), '"""Saylight."""\n')
        _write(os.path.join(self.src, "saylight", "audio_cache.py"), 'DIR_PREFIX = "saylight_"\n')
        _write(os.path.join(self.src, "saylight", "settings.py"), FAKE_SETTINGS % update_line)

    def test_copy_gets_test_names_and_everything_switched_off(self):
        self._source()
        summary = mtc.make_test_copy(self.dest, self.src, "1.2.0")
        self.assertEqual(mtc.source_kind(self.dest), "test")
        app = _read(os.path.join(self.dest, "saylight_app.py"))
        self.assertIn('APP_NAME = "SaylightTest"', app)
        self.assertIn("Local\\\\SaylightTest.Quit", app)
        self.assertNotIn("Local\\\\Saylight.Quit", app)
        settings = _read(os.path.join(self.dest, "saylight", "settings.py"))
        self.assertIn('"updates.check": (False, bool)', settings)
        self.assertIn('"hotkeys.read": ("", str)', settings)
        self.assertIn('"media_keys": (False, bool)', settings)
        self.assertIn('"panel.visible": (False, bool)', settings)
        self.assertEqual(summary["identity"]["QuitEvent"], "Local\\SaylightTest.Quit")

    def test_1_0_settings_without_an_update_check_still_copy(self):
        self._source(update_line="")
        mtc.make_test_copy(self.dest, self.src)
        self.assertEqual(mtc.source_kind(self.dest), "test")

    def test_an_update_check_it_cannot_switch_off_fails_and_leaves_no_copy(self):
        self._source(update_line='    "updates.check": (CHECK_BY_DEFAULT, bool),\n')
        with self.assertRaises(mtc.CopyError):
            mtc.make_test_copy(self.dest, self.src)
        self.assertFalse(os.path.exists(self.dest))

    def test_a_failed_copy_leaves_nothing_to_build(self):
        self._source()
        _write(os.path.join(self.src, "saylight", "audio_cache.py"), 'DIR_PREFIX = "sl_"\n')  # no real prefix
        with self.assertRaises(mtc.CopyError):
            mtc.make_test_copy(self.dest, self.src)
        self.assertFalse(os.path.exists(self.dest))

    def test_source_kind(self):
        self._source()
        self.assertEqual(mtc.source_kind(self.src), "release")
        _write(os.path.join(self.src, mtc.SELFTEST_FILE), "")
        self.assertEqual(mtc.source_kind(self.src), "unfinished")
        _write(os.path.join(self.src, mtc.MARKER), "{}")
        self.assertEqual(mtc.source_kind(self.src), "test")
        os.remove(os.path.join(self.src, mtc.SELFTEST_FILE))
        self.assertEqual(mtc.source_kind(self.src), "unfinished")

    def test_never_replaces_a_folder_it_did_not_make(self):
        self._source()
        keep = os.path.join(self.dest, "notes.txt")
        _write(keep, "mine")
        with self.assertRaises(mtc.CopyError):
            mtc.make_test_copy(self.dest, self.src)
        self.assertEqual(_read(keep), "mine")

    def test_the_real_source_makes_a_clean_test_copy(self):
        summary = mtc.make_test_copy(self.dest, REPO, "1.2.0")
        self.assertEqual(mtc.source_kind(self.dest), "test")
        self.assertGreaterEqual(summary["literals_replaced"], 3)


def _find_iscc() -> str | None:
    folders = [os.environ.get("INNO_SETUP_DIR", ""), cn._inno_registry()[1] or "",
               os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6"),
               os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6")]
    for folder in folders:
        if folder and os.path.isfile(os.path.join(folder, "ISCC.exe")):
            return os.path.join(folder, "ISCC.exe")
    return None


def _fake_exe(path: str, version: str, nums: tuple[int, int, int, int], stamp: str | None) -> None:
    """A copy of python.exe whose only resource is a Saylight-style version resource (never run)."""
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    strings = [StringStruct("ProductName", "Saylight"), StringStruct("FileVersion", version),
               StringStruct("ProductVersion", version)]
    if stamp:
        strings.append(StringStruct("SpecialBuild", stamp))
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x20 if stamp else 0, OS=0x40004,
                          fileType=0x1, subtype=0x0),
        kids=[StringFileInfo([StringTable("040904B0", strings)]), VarFileInfo([VarStruct("Translation",
                                                                                          [0x0409, 1200])])])
    raw = info.toRaw()
    shutil.copyfile(sys.executable, path)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.BeginUpdateResourceW.restype = wintypes.HANDLE
    k32.BeginUpdateResourceW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL)
    k32.UpdateResourceW.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID, wintypes.WORD,
                                    ctypes.c_char_p, wintypes.DWORD)
    k32.EndUpdateResourceW.argtypes = (wintypes.HANDLE, wintypes.BOOL)
    handle = k32.BeginUpdateResourceW(path, True)  # drop python.exe's own resources
    if not handle or not k32.UpdateResourceW(handle, 16, 1, 0x0409, raw, len(raw)):  # RT_VERSION
        raise OSError(ctypes.get_last_error(), "UpdateResourceW failed")
    if not k32.EndUpdateResourceW(handle, False):
        raise OSError(ctypes.get_last_error(), "EndUpdateResourceW failed")


class BuildStampTests(unittest.TestCase):
    def test_every_script_uses_the_same_stamp(self):
        stamp = mtc.BUILD_STAMP
        self.assertEqual(selftest.TEST_BUILD_STAMP, stamp)
        self.assertIn(f'#if BuildStamp != "{stamp}"', _read(os.path.join(PACKAGING, "saylight.iss")))
        self.assertIn(f"$TestBuildStamp = '{stamp}'", _read(os.path.join(PACKAGING, "lifecycle_test.ps1")))
        spec = _read(os.path.join(PACKAGING, "saylight.spec"))
        self.assertIn('StringStruct("SpecialBuild", _test_copy.BUILD_STAMP)', spec)
        self.assertIn('raise SystemExit(f"{SRC} is an unfinished test copy', spec)

    def test_setup_knows_the_update_download_folder(self):
        from .. import updates
        ident = mtc.read_identity()
        self.assertEqual(updates.UPDATE_DIR_PREFIX, ident["real"]["CachePrefix"] + "update_")
        self.assertEqual(mtc.literal_map(ident)[updates.UPDATE_DIR_PREFIX], ident["test"]["CachePrefix"] + "update_")
        self.assertIn("Lowercase('{#CachePrefix}update_')", _read(os.path.join(PACKAGING, "saylight.iss")))

    def test_selftest_never_starts_an_unstamped_exe(self):
        with tempfile.TemporaryDirectory() as dist:
            shutil.copyfile(sys.executable, os.path.join(dist, "Saylight.exe"))
            err = io.StringIO()
            with redirect_stderr(err):
                code = selftest.cli([dist])
            self.assertEqual(code, 2)
            self.assertIn("isn't a build of a test copy", err.getvalue())
            self.assertEqual(os.listdir(dist), ["Saylight.exe"])


@unittest.skipUnless(_find_iscc(), "Inno Setup isn't installed")
class InstallerScriptTests(unittest.TestCase):
    """Compiles packaging/saylight.iss with output off (ISCC /O-) against a fake build folder."""

    def setUp(self):
        try:
            import PyInstaller.utils.win32.versioninfo  # noqa: F401
        except ImportError:
            self.skipTest("PyInstaller isn't installed")
        self._tmp = tempfile.TemporaryDirectory(prefix="sliss_")
        self.dist = os.path.join(self._tmp.name, "Saylight")
        iscc = _find_iscc()
        os.makedirs(os.path.join(self.dist, "_internal"))
        os.makedirs(os.path.join(self.dist, "licenses"))
        shutil.copyfile(os.path.join(os.path.dirname(iscc), "SetupClassicIcon.ico"),
                        os.path.join(self.dist, "_internal", "saylight.ico"))
        for name in ("THIRD_PARTY_NOTICES.txt", "LICENSE.txt", os.path.join("licenses", "MIT.txt")):
            _write(os.path.join(self.dist, name), "text\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _compile(self, *, test: bool, version: str = "1.2.0", nums=(1, 2, 0, 0), stamp: str | None = None):
        _fake_exe(os.path.join(self.dist, "Saylight.exe"), version, nums, stamp)
        # ISCC deletes an earlier installer in the output folder even with output off: keep it away from dist\.
        cmd = [_find_iscc(), "/O-", "/Q", f"/DSourceDir={self.dist}", f"/DOutputDir={self._tmp.name}"] + \
            (["/DTEST"] if test else [])
        r = subprocess.run(cmd + [os.path.join(PACKAGING, "saylight.iss")], capture_output=True, text=True,
                           timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode, re.sub(r"\s+", " ", r.stdout + r.stderr)

    def test_release_installer_takes_a_release_build(self):
        built = os.path.join(REPO, "dist", "Saylight-Setup.exe")
        before = os.stat(built).st_mtime_ns if os.path.exists(built) else None
        code, out = self._compile(test=False)
        self.assertEqual(code, 0, out)
        # A release build in dist\ outlives the tests.
        self.assertEqual(os.stat(built).st_mtime_ns if os.path.exists(built) else None, before)

    def test_release_installer_refuses_a_test_build(self):
        code, out = self._compile(test=False, stamp=mtc.BUILD_STAMP)
        self.assertNotEqual(code, 0)
        self.assertIn("built from a test copy", out)

    def test_test_installer_refuses_a_release_build(self):
        code, out = self._compile(test=True)
        self.assertNotEqual(code, 0)
        self.assertIn("needs a build of a test copy", out)

    def test_test_installer_takes_a_test_build_with_a_prerelease_version(self):
        code, out = self._compile(test=True, version="1.3.0-rc.1", nums=(1, 3, 0, 0), stamp=mtc.BUILD_STAMP)
        self.assertEqual(code, 0, out)

    def test_stamp_reads_back(self):
        exe = os.path.join(self.dist, "Saylight.exe")
        _fake_exe(exe, "1.2.0", (1, 2, 0, 0), mtc.BUILD_STAMP)
        self.assertEqual(selftest.version_string(exe, "SpecialBuild"), mtc.BUILD_STAMP)
        self.assertEqual(selftest.exe_version(exe), "1.2.0")
        _fake_exe(exe, "1.2.0", (1, 2, 0, 0), None)
        self.assertIsNone(selftest.version_string(exe, "SpecialBuild"))


class LifecycleSandboxTests(unittest.TestCase):
    """lifecycle_test.ps1 empties and deletes its sandbox, so it must refuse a folder it didn't make."""

    def _run(self, sandbox: str, setup: str) -> tuple[int, str]:
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
               os.path.join(PACKAGING, "lifecycle_test.ps1"), "-Setup", setup, "-Sandbox", sandbox]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode, re.sub(r"\s+", "", r.stdout + r.stderr)

    def test_refuses_a_folder_it_did_not_make(self):
        with tempfile.TemporaryDirectory(prefix="slsb_") as tmp:
            keep = os.path.join(tmp, "keep.txt")
            _write(keep, "mine")
            code, out = self._run(tmp, os.path.join(tmp, "missing-setup.exe"))
            self.assertNotEqual(code, 0)
            self.assertIn("wasn'tmadebythisscript", out)
            self.assertEqual(_read(keep), "mine")

    def test_an_empty_folder_passes_the_sandbox_rule(self):
        with tempfile.TemporaryDirectory(prefix="slsb_") as tmp:
            sandbox = os.path.join(tmp, "sb")
            os.makedirs(sandbox)
            code, out = self._run(sandbox, os.path.join(tmp, "missing-setup.exe"))
            self.assertNotEqual(code, 0)  # stops at the missing installer, before touching anything
            self.assertNotIn("wasn'tmadebythisscript", out)
            self.assertTrue(os.path.isdir(sandbox))


class BuildReleaseTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("scripts_build_release",
                                                      os.path.join(REPO, "scripts", "build_release.py"))
        self.br = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.br)
        self.folder = tempfile.mkdtemp(prefix="sl_build_release_")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_the_checksum_file_is_one_the_updater_reads(self):
        from saylight import updates
        setup = os.path.join(self.folder, "Saylight-Setup.exe")
        with open(setup, "wb") as f:
            f.write(b"stand-in installer")
        path = self.br.write_checksum(setup)
        self.assertEqual(path, setup + ".sha256")
        self.assertEqual(updates.parse_checksum(_read(path)), hashlib.sha256(b"stand-in installer").hexdigest())

    def test_the_packaging_helpers_load(self):
        # collect_notices defines dataclasses, which need their module registered while it loads.
        _version, licence = self.br._packaging_module("collect_notices").inno_setup()
        self.assertTrue(licence.endswith(".txt"))

    def test_the_version_is_read_from_the_package(self):
        from saylight import __version__
        self.assertEqual(self.br.source_version(), __version__)

    def test_a_build_smart_app_control_blocks_says_so(self):
        blocked = OSError(22, "An Application Control policy has blocked this file", None, self.br.BLOCKED_BY_POLICY)
        with mock.patch.object(self.br.subprocess, "run", side_effect=blocked):
            with self.assertRaisesRegex(self.br.BuildError, r"Windows blocked Saylight\.exe \(Smart App Control\)"):
                self.br.run("The frozen self-test", [os.path.join(self.folder, "Saylight.exe")])
        self.assertEqual(self.br.BLOCKED_BY_POLICY, selftest.BLOCKED_BY_POLICY)


def _section(text: str, name: str) -> str:
    m = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[[A-Za-z]+\]\s*$|\Z)", text, re.M | re.S | re.I)
    return m.group(1) if m else ""


def _entries(section: str) -> list[str]:
    """An Inno section's entries, with backslash continuations joined and comments dropped."""
    out, cur = [], ""
    for line in section.splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        if s.endswith("\\"):
            cur += s[:-1] + " "
            continue
        out.append(cur + s)
        cur = ""
    return out


class SignInEntryTests(unittest.TestCase):
    """The installer's Run value against the "Start with Windows" switch on the General tab.

    UsePreviousTasks=yes makes a silent Setup reuse the last install's task choice, and the update Saylight starts
    passes no tasks, so the tasked entries must leave such an update alone. And a value the switch added was never
    written by Setup, so the uninstall log doesn't know it.
    """

    def setUp(self):
        self.iss = _read(os.path.join(PACKAGING, "saylight.iss"))
        self.run_entries = [e for e in _entries(_section(self.iss, "Registry"))
                            if '"{#RunValue}"' in e and "CurrentVersion\\Run" in e]
        self.assertTrue(self.run_entries)

    def test_an_update_saylight_starts_keeps_the_start_with_windows_switch(self):
        from saylight import updates
        tasked = [e for e in self.run_entries if re.search(r"\bTasks:", e)]
        self.assertEqual(len(tasked), 2)
        for entry in tasked:
            self.assertIn("Check: not StartedByUpdate", entry)
        self.assertIn("function StartedByUpdate", _section(self.iss, "Code"))
        self.assertFalse(any(a.upper().startswith(("/TASKS", "/MERGETASKS")) for a in updates.setup_command("x.exe")))

    def test_uninstall_removes_a_run_value_the_switch_added(self):
        code = _section(self.iss, "Code")
        self.assertRegex(code, r"usPostUninstall then begin\s*(\{[^}]*\}\s*)?RegDeleteValue\(HKEY_CURRENT_USER, "
                               r"'Software\\Microsoft\\Windows\\CurrentVersion\\Run', '\{#RunValue\}'\)")


if __name__ == "__main__":
    unittest.main()

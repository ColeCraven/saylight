"""Frozen self-test: checks what a PyInstaller build of Saylight tends to get wrong, inside the build itself.

Two ways in:

    python packaging/selftest.py <dist folder> [--src <test copy>] [--report out.json] [--offline]
        Runs <dist>\\Saylight.exe --selftest in a clean environment (PATH reduced to the Windows folders, no
        PYTHON* variables, TEMP and SAYLIGHT_DATA_DIR in a scratch folder), then checks the report.
        Exit code 0 when every check passed. It refuses an exe that saylight.spec didn't stamp as a test copy.

    Saylight.exe --selftest <report.json> [--offline]
        Only in a test copy: make_test_copy.py copies this file in as saylight_selftest.py and adds the
        command. It writes the report and exits 0 when every check passed.

The checks: every saylight module imports; Qt finds its windows platform, ico and network-information plugins;
the fonts and the tray are there; certifi and Windows' certificate store load; edge-tts ships as plain .py
files; no Python or Qt DLL is loaded from outside the build. With network: edge-tts lists voices and
synthesises one fixed word into memory, and Windows' MCI opens and measures that audio with the volume at 0
without playing it. The report holds counts, versions and file names from the build, never user text.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
import traceback
from ctypes import wintypes

SAMPLE = "Testing."  # the only text ever sent to the voice service, and only with network checks on
# Never in the build: Pillow is a dev tool only, and Saylight has no use for numpy or Tk. PyInstaller 6 puts
# Tcl/Tk's data in _tcl_data and _tk_data.
NOT_IMPORTABLE = ("PIL", "numpy", "tkinter", "_tkinter")
NOT_BUNDLED = ("PIL", "numpy", "numpy.libs", "tkinter", "_tcl_data", "_tk_data", "tcl", "tk")
# SpecialBuild string saylight.spec writes into a test copy's exe (make_test_copy.BUILD_STAMP). The command line
# below never starts an exe without it: a release build answers to the real quit event and shortcut.
TEST_BUILD_STAMP = "Test copy"
# ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION: Smart App Control (or another app control policy) refused the exe.
# It judges each new unsigned build afresh, so a rebuild can pass where the last one didn't.
BLOCKED_BY_POLICY = 4551
REQUIRED_FONTS = ("Verdana", "Segoe UI")
OTHER_FONTS = ("Segoe UI Variable Text", "Segoe UI Variable Small", "Segoe Fluent Icons", "Segoe MDL2 Assets",
               "Calibri", "Sitka Small", "Sitka Text")
FOREIGN_DLL_PREFIXES = ("python", "qt6", "pyside6", "shiboken6", "libssl", "libcrypto", "libffi", "sqlite3")


# --- Inside the build ------------------------------------------------------------------------------

def _dist_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable))


def _internal_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.join(_dist_dir(), "_internal"))


def _loaded_modules() -> list[str]:
    """Full paths of every DLL loaded in this process."""
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.EnumProcessModulesEx.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
    psapi.GetModuleFileNameExW.argtypes = (wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
    proc = kernel32.GetCurrentProcess()
    needed = wintypes.DWORD()
    mods = (wintypes.HMODULE * 2048)()
    if not psapi.EnumProcessModulesEx(proc, mods, ctypes.sizeof(mods), ctypes.byref(needed), 0x03):
        raise OSError(ctypes.get_last_error(), "EnumProcessModulesEx failed")
    buf = ctypes.create_unicode_buffer(1024)
    paths = []
    for i in range(min(needed.value // ctypes.sizeof(wintypes.HMODULE), len(mods))):
        if psapi.GetModuleFileNameExW(proc, mods[i], buf, len(buf)):
            paths.append(buf.value)
    return paths


def _mci(command: str, buf=None) -> int:
    winmm = ctypes.WinDLL("winmm")
    winmm.mciSendStringW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE)
    return winmm.mciSendStringW(command, buf, len(buf) if buf is not None else 0, None)


def main(argv: list[str]) -> int:
    """Run every check, write the report to argv[0], and return 0 when all passed."""
    if not argv:
        return 2
    out_path = os.path.abspath(argv[0])
    offline = "--offline" in argv[1:]
    t0 = time.perf_counter()
    r: dict = {"ok": {}, "errors": {}, "skipped": [], "offline": offline}

    def check(name, fn, required=True):
        try:
            r["ok"][name] = fn()
        except Exception:
            if required:
                r["errors"][name] = traceback.format_exc(limit=4)
            else:
                r["ok"][name] = {"failed": traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip()}

    r["frozen"] = bool(getattr(sys, "frozen", False))
    r["python"] = sys.version.split()[0]
    r["stdout_is_none"] = sys.stdout is None
    r["data_dir_set"] = bool(os.environ.get("SAYLIGHT_DATA_DIR"))

    # PySide6 first: winrt must never load before it.
    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFontDatabase, QIcon, QImageReader
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon

    r["pyside"] = pyside_version

    def imports():
        import importlib
        import pkgutil

        import saylight
        names = sorted(m.name for m in pkgutil.walk_packages(saylight.__path__, "saylight."))
        failed = {}
        for name in names:
            if ".tests" in name:
                continue
            try:
                importlib.import_module(name)
            except Exception as e:
                failed[name] = type(e).__name__
        for name in ("edge_tts", "aiohttp", "certifi"):
            importlib.import_module(name)
        if failed:
            raise ImportError(f"{len(failed)} modules failed: {failed}")
        return {"modules": ["saylight"] + [n for n in names if ".tests" not in n],
                "version": getattr(saylight, "__version__", None)}
    check("imports", imports)

    app = QApplication.instance() or QApplication(["saylight-selftest"])
    r["qt_ready_ms"] = round((time.perf_counter() - t0) * 1000)

    def platform():
        name = app.platformName()
        if name != "windows":
            raise RuntimeError(f"platform plugin is {name!r}")
        return name
    check("platform", platform)

    def image_formats():
        formats = sorted(bytes(f).decode() for f in QImageReader.supportedImageFormats())
        if "ico" not in formats:
            raise RuntimeError("no ico image format plugin")
        return formats
    check("image_formats", image_formats)

    def icon():
        path = os.path.join(_internal_dir(), "saylight.ico")
        reader = QImageReader(path)
        sizes = []
        for i in range(reader.imageCount()):
            reader.jumpToImage(i)
            sizes.append(reader.size().width())
        if not sizes or 256 not in sizes or QIcon(path).isNull():
            raise RuntimeError(f"icon sizes {sizes}")
        return {"sizes": sizes}
    check("icon", icon)

    def fonts():
        families = set(QFontDatabase.families())
        missing = [f for f in REQUIRED_FONTS if f not in families]
        if missing:
            raise RuntimeError(f"missing fonts {missing}")
        return {"families": len(families), "optional": {f: f in families for f in OTHER_FONTS}}
    check("fonts", fonts)

    def network_information():
        from PySide6.QtNetwork import QNetworkInformation
        if not QNetworkInformation.loadDefaultBackend():
            raise RuntimeError(f"no backend; available {QNetworkInformation.availableBackends()}")
        inst = QNetworkInformation.instance()
        return {"backend": inst.backendName(), "reachability": inst.reachability().name}
    check("network_information", network_information)

    check("tray", lambda: {"available": QSystemTrayIcon.isSystemTrayAvailable()}, required=False)

    def certificates():
        import ssl

        import certifi
        where = certifi.where()
        bundle = len(ssl.create_default_context(cafile=where).get_ca_certs())
        system = len(ssl.create_default_context().get_ca_certs())
        if not os.path.isfile(where) or bundle < 100:
            raise RuntimeError(f"certifi bundle has {bundle} CAs")
        return {"certifi_in_build": os.path.abspath(where).startswith(_internal_dir()), "certifi_cas": bundle,
                "windows_store_cas": system, "openssl": ssl.OPENSSL_VERSION}
    check("certificates", certificates)

    def edge_tts_files():
        import edge_tts
        path = os.path.abspath(edge_tts.__file__)
        folder = os.path.dirname(path)
        files = sorted(f for f in os.listdir(folder) if f.endswith(".py"))
        if not path.endswith(".py") or not path.startswith(_internal_dir()) or "srt_composer.py" not in files:
            raise RuntimeError(f"edge_tts is not shipped as replaceable .py files ({path})")
        return {"py_files": len(files)}
    check("edge_tts_replaceable", edge_tts_files)

    def contents():
        import importlib.util
        internal = _internal_dir()
        present = [n for n in NOT_BUNDLED if os.path.exists(os.path.join(internal, n))]
        present += [f"import {n}" for n in NOT_IMPORTABLE if importlib.util.find_spec(n) is not None]
        qt = [f for f in ("Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Network.dll")
              if os.path.isfile(os.path.join(internal, "PySide6", f))]
        if present or len(qt) != 4:
            raise RuntimeError(f"unexpected {present}; separate Qt DLLs {qt}")
        return {"qt_dlls_separate": qt}
    check("contents", contents)

    mp3: list[bytes] = []
    if offline:
        r["skipped"] += ["edge_tts_network", "mci_decode", "update_check"]
    else:
        def edge_network():
            import asyncio

            import edge_tts

            async def go():
                t = time.perf_counter()
                voices = await edge_tts.list_voices()
                t_voices = time.perf_counter() - t
                t = time.perf_counter()
                comm = edge_tts.Communicate(SAMPLE, "en-US-AvaNeural", boundary="WordBoundary")
                audio, words = bytearray(), 0
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        audio += chunk["data"]  # memory only
                    elif chunk["type"] == "WordBoundary":
                        words += 1
                return len(voices), t_voices, time.perf_counter() - t, bytes(audio), words

            count, tv, ts, audio, words = asyncio.run(go())
            if not count or not audio or not words:
                raise RuntimeError(f"voices {count}, audio bytes {len(audio)}, word boundaries {words}")
            mp3.append(audio)
            return {"voices": count, "voices_s": round(tv, 2), "synth_s": round(ts, 2), "audio_bytes": len(audio),
                    "word_boundaries": words}
        check("edge_tts_network", edge_network)

        def mci_decode():
            if not mp3:
                raise RuntimeError("no audio from edge_tts_network")
            folder = os.environ.get("TEMP") or _dist_dir()
            path = os.path.join(folder, f"selftest_{os.getpid()}.mp3")
            with open(path, "wb") as f:
                f.write(mp3[0])
            buf = ctypes.create_unicode_buffer(128)
            try:
                opened = _mci(f'open "{path}" type mpegvideo alias sltest')
                _mci("setaudio sltest volume to 0")
                _mci("status sltest volume", buf)
                volume = buf.value
                _mci("set sltest time format milliseconds")
                status = _mci("status sltest length", buf)
                length = buf.value
            finally:
                _mci("close sltest")
                os.remove(path)
            if opened or status or not length.isdigit() or int(length) <= 0 or volume not in ("0", ""):
                raise RuntimeError(f"open {opened}, status {status}, length {length!r}, volume {volume!r}")
            return {"length_ms": int(length), "volume": volume, "played": False,
                    "probe_deleted": not os.path.exists(path)}
        check("mci_decode", mci_decode)

        def update_check():
            import importlib.util
            if importlib.util.find_spec("saylight.updates") is None:
                return {"present": False}
            import saylight
            from saylight import updates
            result = updates.check_any(getattr(saylight, "__version__", "0"))
            return {"present": True, "status": getattr(result, "status", None)}
        check("update_check", update_check, required=False)

    def dlls():
        dist = os.path.normcase(_dist_dir()) + os.sep
        loaded = _loaded_modules()
        foreign = [p for p in loaded if not os.path.normcase(p).startswith(dist)
                   and os.path.basename(p).lower().startswith(FOREIGN_DLL_PREFIXES)]
        foreign += [p for p in loaded if p.lower().endswith(".pyd") and not os.path.normcase(p).startswith(dist)]
        if foreign:
            raise RuntimeError(f"loaded from outside the build: {sorted(set(foreign))}")
        windir = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows")) + os.sep
        return {"loaded": len(loaded), "from_build": sum(os.path.normcase(p).startswith(dist) for p in loaded),
                "other": sum(not os.path.normcase(p).startswith((dist, windir)) for p in loaded)}
    check("dlls", dlls)

    QTimer.singleShot(0, app.quit)
    app.exec()
    r["total_ms"] = round((time.perf_counter() - t0) * 1000)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2)
    return 0 if not r["errors"] else 1


# --- From the build machine ------------------------------------------------------------------------

def clean_env(scratch: str) -> dict[str, str]:
    """Only what Windows needs: no Python on PATH, no PYTHON* variables, TEMP and data in scratch."""
    keep = ("SystemRoot", "SystemDrive", "windir", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "USERNAME",
            "USERDOMAIN", "COMPUTERNAME", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "ComSpec",
            "PATHEXT", "HOMEDRIVE", "HOMEPATH", "ProgramData", "ProgramFiles", "ProgramFiles(x86)",
            "CommonProgramFiles", "PUBLIC", "ALLUSERSPROFILE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    root = env.get("SystemRoot", r"C:\Windows")
    env["PATH"] = f"{root}\\system32;{root};{root}\\System32\\Wbem"
    env["TEMP"] = env["TMP"] = os.path.join(scratch, "tmp")
    env["SAYLIGHT_DATA_DIR"] = os.path.join(scratch, "data")
    for folder in (env["TEMP"], env["SAYLIGHT_DATA_DIR"]):
        os.makedirs(folder, exist_ok=True)
    return env


def version_string(path: str, key: str) -> str | None:
    """One string (ProductVersion, SpecialBuild, ...) from the exe's version resource, or None."""
    version = ctypes.WinDLL("version")
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return None
    ptr, length = ctypes.c_wchar_p(), wintypes.UINT()
    if not version.VerQueryValueW(data, rf"\StringFileInfo\040904B0\{key}", ctypes.byref(ptr),
                                  ctypes.byref(length)):
        return None
    return ptr.value


def exe_version(path: str) -> str | None:
    """ProductVersion from the exe's version resource."""
    return version_string(path, "ProductVersion")


def source_modules(src: str) -> list[str]:
    names = []
    root = os.path.join(src, "saylight")
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
        rel = os.path.relpath(folder, src).replace(os.sep, ".")
        for f in files:
            if f.endswith(".py"):
                names.append(rel if f == "__init__.py" else f"{rel}.{f[:-3]}")
    return sorted(names)


def source_version(src: str) -> str | None:
    import re
    with open(os.path.join(src, "saylight", "__init__.py"), encoding="utf-8-sig") as f:
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.MULTILINE)
    return m.group(1) if m else None


def cli(argv: list[str] | None = None) -> int:
    import argparse
    import shutil
    import subprocess
    import tempfile

    parser = argparse.ArgumentParser(description="Run the frozen self-test of a test build in a clean environment.")
    parser.add_argument("dist", help="the build folder holding Saylight.exe (a test copy build; nothing else is run)")
    parser.add_argument("--src", help="the test copy it was built from, to compare modules and version")
    parser.add_argument("--report", help="where to keep the JSON report (default: deleted after reading)")
    parser.add_argument("--offline", action="store_true", help="skip the checks that use the network")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    dist = os.path.abspath(args.dist)
    exe = os.path.join(dist, "Saylight.exe")
    if not os.path.isfile(exe):
        print(f"selftest: no Saylight.exe in {dist}", file=sys.stderr)
        return 2
    if version_string(exe, "SpecialBuild") != TEST_BUILD_STAMP:
        print("selftest: this Saylight.exe isn't a build of a test copy (make_test_copy.py); not starting it",
              file=sys.stderr)
        return 2
    scratch = tempfile.mkdtemp(prefix="slselftest_")
    report = os.path.abspath(args.report) if args.report else os.path.join(scratch, "report.json")
    problems: list[str] = []
    try:
        cmd = [exe, "--selftest", report] + (["--offline"] if args.offline else [])
        t0 = time.perf_counter()
        env = clean_env(scratch)
        assert not any(k.upper().startswith("PYTHON") for k in env)
        try:
            code = subprocess.run(cmd, env=env, cwd=env["TEMP"], timeout=args.timeout).returncode
        except subprocess.TimeoutExpired:  # run() has already ended the process
            print(f"selftest: no result within {args.timeout:.0f} s", file=sys.stderr)
            return 1
        except OSError as e:
            if getattr(e, "winerror", None) != BLOCKED_BY_POLICY:
                raise
            print("selftest: Windows blocked Saylight.exe (Smart App Control); rebuild or sign it", file=sys.stderr)
            return 1
        wall = time.perf_counter() - t0
        if not os.path.isfile(report):
            print(f"selftest: no report (exit {code}); the build is not a test copy, or it failed to start",
                  file=sys.stderr)
            return 1
        with open(report, encoding="utf-8") as f:
            r = json.load(f)
        leftovers = os.listdir(env["TEMP"])
        if leftovers:
            problems.append(f"files left in TEMP: {len(leftovers)}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if code != 0:
        problems.append(f"exit code {code}")
    problems += [f"{name} failed: {detail.strip().splitlines()[-1]}" for name, detail in r["errors"].items()]
    if not r.get("frozen"):
        problems.append("not running frozen")
    if args.src:
        want = source_modules(args.src)
        got = set(r["ok"].get("imports", {}).get("modules", []))
        missing = [m for m in want if m not in got]
        if missing:
            problems.append(f"modules missing from the build: {missing}")
        if source_version(args.src) != exe_version(exe):
            problems.append(f"exe version {exe_version(exe)} != source {source_version(args.src)}")

    passed = sorted(r["ok"])
    print(f"selftest: {len(passed)} checks passed, {len(problems)} problems, {len(r['skipped'])} skipped, "
          f"{wall:.1f} s")
    for name in passed:
        print(f"  ok  {name}")
    for name in r["skipped"]:
        print(f"  --  {name} (offline)")
    for p in problems:
        print(f"  !!  {p}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(cli())

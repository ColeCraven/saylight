# PyInstaller build file for Saylight: one folder, windowed, no UPX, with a version resource and the app icon.
#
#   python -m PyInstaller --noconfirm --clean --distpath <dist> --workpath <work> packaging/saylight.spec
#
# Environment:
#   SAYLIGHT_SRC   folder holding saylight_app.py and saylight/ (default: the repository root). The test copy
#                  from make_test_copy.py is built by pointing this at it.
#   SAYLIGHT_TRIM  "0" keeps the Qt files the DROP filter would remove (for comparing sizes only).
#
# The version comes from saylight/__init__.py (__version__) and is the only place it is set; Inno Setup reads
# it back from Saylight.exe.
import importlib.util
import os
import re
import subprocess
import sys

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

HERE = SPECPATH  # packaging/
SRC = os.path.abspath(os.environ.get("SAYLIGHT_SRC") or os.path.dirname(HERE))
ENTRY = os.path.join(SRC, "saylight_app.py")
if not os.path.isfile(ENTRY):
    raise SystemExit(f"saylight_app.py not found in {SRC}")


def _load(name: str):
    """A helper script from packaging/, loaded without putting packaging/ on the path the analysis searches."""
    spec = importlib.util.spec_from_file_location(f"_saylight_{name}", os.path.join(HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A test copy's exe is stamped (SpecialBuild) so saylight.iss can refuse to put it in the real installer, and the
# test scripts refuse to start a build that isn't one. A copy whose making stopped part-way is never built.
_test_copy = _load("make_test_copy")
KIND = _test_copy.source_kind(SRC)
if KIND == "unfinished":
    raise SystemExit(f"{SRC} is an unfinished test copy; run make_test_copy.py again")
TEST_COPY = KIND == "test"


def read_version(src: str) -> str:
    with open(os.path.join(src, "saylight", "__init__.py"), encoding="utf-8-sig") as f:
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.MULTILINE)
    if not match:
        raise SystemExit("saylight/__init__.py has no __version__; the build needs it for the exe and installer")
    return match.group(1)


VERSION = read_version(SRC)
NUMS = (tuple(int(x) for x in re.findall(r"\d+", VERSION)[:3]) + (0, 0, 0, 0))[:4]

VS_FF_SPECIALBUILD = 0x20
STRINGS = [
    StringStruct("CompanyName", "Cole Craven"),
    StringStruct("FileDescription", "Saylight"),
    StringStruct("FileVersion", VERSION),
    StringStruct("InternalName", "Saylight"),
    StringStruct("LegalCopyright", "(c) 2026 Cole Craven. MIT License."),
    StringStruct("OriginalFilename", "Saylight.exe"),
    StringStruct("ProductName", "Saylight"),
    StringStruct("ProductVersion", VERSION),
]
if TEST_COPY:
    STRINGS.append(StringStruct("SpecialBuild", _test_copy.BUILD_STAMP))

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMS, prodvers=NUMS, mask=0x3F, flags=VS_FF_SPECIALBUILD if TEST_COPY else 0x0,
                      OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[
        StringFileInfo([StringTable("040904B0", STRINGS)]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

# The icon is drawn by the app's own make_icon at build time, in a separate process so this one never loads Qt.
# Setup's wizard images go next to the build folder (<dist>\Saylight-wizard), where saylight.iss looks for them;
# they are not part of the app.
ICON = os.path.join(workpath, "saylight.ico")
subprocess.run([sys.executable, os.path.join(HERE, "make_icon.py"), ICON, "--src", SRC,
                "--wizard", os.path.join(DISTPATH, "Saylight-wizard")], check=True,
               env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))

# Never bundled: GUI toolkits and numeric libraries Saylight doesn't use (some hooks pull them in when
# installed), and Qt modules it doesn't import.
# Pillow is a dev tool only, even though pypdf imports it when it is installed. cryptography (AES-encrypted PDFs)
# is left out too: it would add more than 10 MB, so those PDFs get the "This PDF is protected" notice instead.
# lxml and fontTools are optional extras of other packages that Saylight never needs.
EXCLUDES = ["tkinter", "_tkinter", "PIL", "numpy", "cryptography", "Crypto", "Cryptodome", "lxml", "fontTools",
            "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL", "PySide6.QtSvg", "PySide6.QtPdf",
            "PySide6.QtDBus", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtConcurrent", "PySide6.QtPrintSupport",
            "PySide6.QtXml", "PySide6.QtHelp", "PySide6.QtOpenGLWidgets", "PySide6.QtUiTools",
            "PySide6.QtDesigner", "pydoc", "lib2to3", "xmlrpc", "pdb"]

def _app_modules() -> list[str]:
    """Every module of the saylight package except its tests, so one that nothing imports at start (such as
    saylight.synth, kept for 1.0 imports) still ships."""
    names = []
    for folder, dirs, files in os.walk(os.path.join(SRC, "saylight")):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
        package = os.path.relpath(folder, SRC).replace(os.sep, ".")
        names += [package if f == "__init__.py" else f"{package}.{f[:-3]}" for f in files if f.endswith(".py")]
    return sorted(names)


def _pypdf_modules() -> list[str]:
    """pypdf is imported only when a PDF is opened, so the analysis may not see all of it."""
    from PyInstaller.utils.hooks import collect_submodules
    return collect_submodules("pypdf")


def _winrt_modules() -> list[str]:
    """The WinRT bindings (OCR, PDF rendering, Windows' voices) are loaded by name in winrt_support.py, so the
    analysis can't see them: every winrt module of the pinned packages. They are listed from the files, never
    imported here: winrt crashes a process that hasn't loaded PySide6 first."""
    spec = importlib.util.find_spec("winrt")
    if spec is None:
        raise SystemExit("winrt isn't installed; pip install -r requirements.txt")
    names = []
    for root in spec.submodule_search_locations:
        base = os.path.dirname(root)
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            package = os.path.relpath(folder, base).replace(os.sep, ".")
            for f in files:
                if f.endswith(".py"):
                    names.append(package if f == "__init__.py" else f"{package}.{f[:-3]}")
                elif f.endswith(".pyd"):
                    names.append(f"{package}.{f.split('.')[0]}")
    return sorted(set(names))


# The test copy carries its self-test module next to saylight_app.py; a release build has none.
HIDDEN = _app_modules() + _pypdf_modules() + _winrt_modules() + (["saylight_selftest"] if TEST_COPY else [])

a = Analysis(
    [ENTRY],
    pathex=[SRC],
    binaries=[],
    datas=[(ICON, ".")],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
    # LGPL-3.0 code must stay replaceable: edge-tts and the Python parts of PySide6 and shiboken6 ship as plain
    # .py files in _internal (their DLLs and .pyd files are separate files anyway), never inside the archive.
    # collect_notices.py fails the build if any LGPL module ends up in the archive.
    module_collection_mode={"edge_tts": "py", "PySide6": "py", "shiboken6": "py"},
)

# Files the PySide6 hooks collect that Saylight can't use:
# - Qt translations (the UI is English only) and the software OpenGL fallback (no OpenGL or QML)
# - Qt's TLS plugins and the OpenSSL copy PySide6 ships for them. QtNetwork is only used for
#   QNetworkInformation; HTTPS goes through Python's ssl. If Qt ever does HTTPS, keep qschannelbackend.dll.
# - platform plugins other than qwindows, the generic and icon-engine plugins, image formats other than ico
# - the app-local Universal CRT: Windows 10 and 11 always use the system copy
# - winrt-runtime's own MSVCP140.dll: PySide6 loads its newer copy first, and winrt works with that one
DROP = re.compile(r"(?i)(PySide6[\\/]translations[\\/]|opengl32sw\.dll$|qt6pdf|qt6qml|qt6quick|qt6opengl|"
                  r"winrt[\\/]msvcp140\.dll$|"
                  r"qt6svg|imageformats[\\/]q(?!ico)\w+\.dll$|plugins[\\/]tls[\\/]|"
                  r"(^|[\\/])(libssl|libcrypto)-3-x64\.dll$|platforms[\\/]q(direct2d|minimal|offscreen)\.dll$|"
                  r"plugins[\\/](generic|iconengines)[\\/]|(^|[\\/])api-ms-win-[\w-]+\.dll$|(^|[\\/])ucrtbase\.dll$)")
if os.environ.get("SAYLIGHT_TRIM", "1") == "1":
    a.binaries = [b for b in a.binaries if not DROP.search(b[0])]
    a.datas = [d for d in a.datas if not DROP.search(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Saylight",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX-packed exes are a common antivirus false positive
    console=False,                   # windowed: no console, and sys.stdout/sys.stderr are None
    disable_windowed_traceback=True,  # the app logs its own errors; no PyInstaller error box
    icon=ICON,
    version=version_info,
    uac_admin=False,
    contents_directory="_internal",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Saylight",
)

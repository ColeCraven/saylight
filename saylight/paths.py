"""Where Saylight's files live.

The install folder holds the program and belongs to the installer (or to the source checkout). Everything
Saylight writes goes to the data folder, %LOCALAPPDATA%\\Saylight, or to $SAYLIGHT_DATA_DIR when that is set
(tests and the test copy use it). Saylight 1.0 and 1.1 kept their settings next to the program;
migrate_legacy_data() moves them over on the first start.

Nothing here touches the disk at import, and only data_dir() creates a folder.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable

log = logging.getLogger("saylight.paths")

DATA_DIR_ENV = "SAYLIGHT_DATA_DIR"
DATA_DIR_NAME = "Saylight"
APP_SCRIPT = "saylight_app.py"
SETTINGS_NAME = "saylight_settings.json"
OLD_SETTINGS_NAME = "read_aloud_settings.json"   # the app's earlier name, Read Aloud
LOG_NAME = "saylight.log"
CRASH_NAME = "saylight_crash.log"

# What migrate_legacy_data() moves: (kind for the log, file names next to the program, preferred first,
# file name in the data folder, replace an older file there).
_LEGACY = (
    ("settings", (SETTINGS_NAME, OLD_SETTINGS_NAME), SETTINGS_NAME, True),
)
_MOVE_ATTEMPTS = 4      # a sharing violation (antivirus, OneDrive) is usually gone within moments
_MOVE_GAP_S = 0.05
_ERROR_NOT_SAME_DEVICE = 17


def is_frozen() -> bool:
    """True in the installed build (Saylight.exe), False when running from source."""
    return bool(getattr(sys, "frozen", False))


def install_dir() -> str:
    """The folder holding Saylight.exe, or the source folder holding saylight_app.py."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """%LOCALAPPDATA%\\Saylight, or $SAYLIGHT_DATA_DIR; created on demand.

    A folder that can't be created is still returned: the writes that follow fail and are handled there.
    """
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        path = os.path.abspath(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        path = os.path.join(base, DATA_DIR_NAME)
    with contextlib.suppress(OSError):
        os.makedirs(path, exist_ok=True)
    return path


def settings_path() -> str:
    return os.path.join(data_dir(), SETTINGS_NAME)


def log_path() -> str:
    return os.path.join(data_dir(), LOG_NAME)


def crash_path() -> str:
    return os.path.join(data_dir(), CRASH_NAME)


def pronunciations_path() -> str:
    return os.path.join(data_dir(), "pronunciations.json")


def resume_path() -> str:
    return os.path.join(data_dir(), "resume.json")


def voices_dir() -> str:
    return os.path.join(data_dir(), "voices")


def engines_dir() -> str:
    return os.path.join(data_dir(), "engines")


def launch_command() -> list[str]:
    """How to start this copy of Saylight: [Saylight.exe], or [pythonw.exe, saylight_app.py] from source."""
    if is_frozen():
        return [os.path.abspath(sys.executable)]
    pythonw = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "pythonw.exe")
    return [pythonw, os.path.join(install_dir(), APP_SCRIPT)]


def migrate_legacy_data() -> list[str]:
    """Move Saylight 1.0 and 1.1 data from next to the program into data_dir(); returns the names moved.

    Newer data in the data folder is never overwritten, and a file that stays locked is left where it is for
    the next start to try again.
    """
    return _migrate(install_dir(), data_dir())


def _migrate(src_dir: str, dst_dir: str) -> list[str]:
    if _same_folder(src_dir, dst_dir):
        return []
    moved: list[str] = []
    kinds: list[str] = []
    for kind, names, target, replace_older in _LEGACY:
        src = next((p for p in (os.path.join(src_dir, n) for n in names) if os.path.isfile(p)), None)
        if src is None:
            continue
        try:
            if _move(src, os.path.join(dst_dir, target), replace_older):
                moved.append(target)
                kinds.append(kind)
        except OSError as e:
            log.warning("Couldn't move the earlier %s into the data folder (%s); trying again next start",
                        kind, type(e).__name__)
    if kinds:
        log.info("Moved the earlier %s into the data folder", ", ".join(kinds))
    return moved


def _move(src: str, dst: str, replace_older: bool) -> bool:
    """Move src to dst. False, leaving src alone, when dst already holds data at least as new."""
    if os.path.exists(dst):
        if not replace_older or os.path.getmtime(dst) >= os.path.getmtime(src):
            return False
        action = os.replace
    else:
        action = os.rename  # unlike os.replace, never overwrites a file that appeared meanwhile
    for attempt in range(_MOVE_ATTEMPTS):
        try:
            action(src, dst)
            return True
        except (FileExistsError, FileNotFoundError):
            return False
        except OSError as e:
            if getattr(e, "winerror", None) == _ERROR_NOT_SAME_DEVICE:
                return _copy_across(src, dst, action)
            if attempt == _MOVE_ATTEMPTS - 1:
                raise
            time.sleep(_MOVE_GAP_S)
    return False


def _copy_across(src: str, dst: str, action: Callable[[str, str], None]) -> bool:
    """A move to another drive: copy next to dst, put it in place, then delete src (if that fails, the copy
    has the same time stamp, so the next start leaves src alone)."""
    tmp = dst + ".tmp"
    try:
        shutil.copy2(src, tmp)
        action(tmp, dst)
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)
    with contextlib.suppress(OSError):
        os.remove(src)
    return True


def _same_folder(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

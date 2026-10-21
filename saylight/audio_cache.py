"""The run's temporary playback folder: the only place any voice writes audio.

Each run gets %TEMP%\\saylight_<random>\\ and holds a lock file in it while it runs. On start, folders left
by runs that are no longer running are deleted: at once when their lock is free (the run crashed or was
killed), and after an hour at rest when they have no lock (Saylight 1.0 and 1.1, or the earlier name).

Chunk files are created with O_SHORT_LIVED, so Windows tries to keep them in memory instead of writing them
to disk. Nothing here imports Qt.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import shutil
import tempfile
import threading
import time
from typing import BinaryIO

log = logging.getLogger("saylight.audio_cache")

DIR_PREFIX = "saylight_"
OLD_DIR_PREFIXES = ("read_aloud_",)  # cache folders left by the app's earlier name; purged like our own
STALE_DIR_AGE_S = 3600     # a folder without a lock goes after this long with nothing newer in it
LOCK_NAME = "saylight.lock"
DELETE_RETRIES = 8         # a chunk file still open in MCI is retried this many times ...
DELETE_RETRY_S = 0.25      # ... this far apart

# O_SHORT_LIVED sets FILE_ATTRIBUTE_TEMPORARY. The handle is never inherited by child processes.
_CHUNK_FLAGS = (os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_SHORT_LIVED", 0))
_LOCK_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)


class AudioCache:
    """One run's playback folder. new_path, create and owns are thread-safe; delete runs on the GUI thread."""

    def __init__(self, temp_root: str | None = None, *, prefix: str = DIR_PREFIX):
        self._root = temp_root or tempfile.gettempdir()
        self._prefix = prefix
        self._mutex = threading.Lock()
        self._dir: str | None = None
        self._lock_fd: int | None = None
        self._closed = False
        self._seq = itertools.count(1)
        self._timers: set[threading.Timer] = set()

    @property
    def dir(self) -> str | None:
        """This run's folder (None before start())."""
        return self._dir

    @property
    def root(self) -> str:
        return self._root

    @property
    def prefix(self) -> str:
        return self._prefix

    def start(self) -> str | None:
        """Create the folder and its lock, then purge folders left by earlier runs. Returns the folder.

        A second call returns the same folder; after shutdown() nothing is created.
        """
        with self._mutex:
            if self._dir is not None or self._closed:
                return self._dir
            os.makedirs(self._root, exist_ok=True)
            folder = tempfile.mkdtemp(prefix=self._prefix, dir=self._root)
            try:
                self._lock_fd = os.open(os.path.join(folder, LOCK_NAME), _LOCK_FLAGS)
            except OSError as e:     # still usable; a crash would leave the folder for the age rule
                log.warning("Couldn't create the audio cache lock: %s", type(e).__name__)
            self._dir = folder
        purge_orphans(self._root, self._prefix, keep=folder)
        return folder

    def new_path(self, stem: str, ext: str) -> str:
        """A unique path inside the folder: new_path("g1_c0", ".mp3") -> <dir>\\g1_c0_<n>.mp3."""
        if self._dir is None:
            raise RuntimeError("the audio cache has not been started")
        if not stem or any(c in stem for c in "/\\:") or stem.strip(".") == "":
            raise ValueError("invalid file stem")
        if ext and not ext.startswith("."):
            ext = "." + ext
        return os.path.join(self._dir, f"{stem}_{next(self._seq)}{ext}")

    def create(self, path: str) -> BinaryIO:
        """Open a new chunk file inside the folder for writing (O_SHORT_LIVED). Fails if it already exists."""
        if not self.owns(path):
            raise ValueError("path is outside the audio cache folder")
        # Under the mutex, so a shutdown() on another thread can't delete the folder between the check and
        # the open, only for makedirs to bring it back without its lock.
        with self._mutex:
            if self._closed:
                raise RuntimeError("the audio cache has been shut down")
            os.makedirs(self._dir, exist_ok=True)   # a temp cleaner may have removed it in a long session
            fd = os.open(path, _CHUNK_FLAGS, 0o600)
        return os.fdopen(fd, "wb")

    def owns(self, path: str) -> bool:
        """True if `path` is inside this run's folder (the folder itself and its lock excluded)."""
        if not self._dir or not path:
            return False
        root = os.path.normcase(os.path.abspath(self._dir))
        target = os.path.normcase(os.path.abspath(path))
        try:
            if os.path.commonpath([target, root]) != root:
                return False
        except ValueError:  # different drives
            return False
        return target not in (root, os.path.join(root, os.path.normcase(LOCK_NAME)))

    def delete(self, path: str) -> None:
        """Delete a chunk file. A file still open in MCI is retried for about 2 s on a timer thread."""
        if not self.owns(path):
            log.warning("Refused to delete a file outside the audio cache folder")
            return
        if not _try_remove(path):
            self._retry_later(path, DELETE_RETRIES)

    def shutdown(self) -> None:
        """Release the lock and delete the folder. Later calls do nothing.

        If something still holds a file open (MCI, a scanner), the folder is left with a released lock, so
        the next start deletes it at once instead of waiting for the age rule.
        """
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            timers, self._timers = self._timers, set()
        for timer in timers:
            timer.cancel()
        self._unlock()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            if os.path.exists(self._dir):
                log.warning("Couldn't fully delete the audio cache folder")
                with contextlib.suppress(OSError):
                    open(os.path.join(self._dir, LOCK_NAME), "ab").close()

    # --- Internals ----------------------------------------------------------------------

    def _unlock(self) -> None:
        with self._mutex:
            fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _retry_later(self, path: str, left: int) -> None:
        with self._mutex:
            if self._closed:
                return
            timer = threading.Timer(DELETE_RETRY_S, self._retry, (path, left))
            timer.daemon = True
            self._timers.add(timer)
        timer.start()

    def _retry(self, path: str, left: int) -> None:
        with self._mutex:
            self._timers.discard(threading.current_thread())   # a Timer runs its function on itself
        if _try_remove(path):
            return
        if left > 1:
            self._retry_later(path, left - 1)
        else:
            log.warning("Couldn't delete a cached chunk: still in use")


def purge_orphans(root: str, prefix: str = DIR_PREFIX, *, keep: str | None = None,
                  max_age_s: float = STALE_DIR_AGE_S) -> int:
    """Delete cache folders in `root` that no running copy is using. Returns the count.

    A folder whose lock file no process holds goes at once. A folder without a lock file goes only when
    nothing in it is newer than `max_age_s`: Saylight 1.0 and 1.1 hold no lock, and a starting run creates
    its lock just after its folder. The earlier name's folders are included for the default prefix.
    """
    prefixes = (prefix, *OLD_DIR_PREFIXES) if prefix == DIR_PREFIX else (prefix,)
    cutoff = time.time() - max_age_s
    keep_norm = os.path.normcase(os.path.abspath(keep)) if keep else None
    removed = crashed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(prefixes) or os.path.normcase(os.path.abspath(entry.path)) == keep_norm:
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            lock = _free_lock(entry.path)
            if lock == "held" or (lock == "none" and _newest_mtime(entry.path) > cutoff):
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        if not os.path.exists(entry.path):
            removed += 1
            crashed += lock == "free"
        elif lock == "free":     # a file is still held (a scanner): keep it marked, so the next start retries
            with contextlib.suppress(OSError):
                open(os.path.join(entry.path, LOCK_NAME), "ab").close()
    if removed:
        log.info("Removed %d audio cache folder(s) left by earlier runs (%d unlocked)", removed, crashed)
    return removed


def purge_stale_dirs(root: str, *, keep: str | None = None, max_age_s: float = STALE_DIR_AGE_S) -> int:
    """Saylight 1.0's name for purge_orphans() with the default prefix."""
    return purge_orphans(root, DIR_PREFIX, keep=keep, max_age_s=max_age_s)


def _free_lock(folder: str) -> str:
    """"none" (no lock file), "held" (a running copy has it open) or "free" (its owner is gone; now removed).

    Deleting the lock is the test: Windows refuses while any process has the file open.
    """
    try:
        os.remove(os.path.join(folder, LOCK_NAME))
    except FileNotFoundError:
        return "none"
    except OSError:          # sharing violation: held. Anything else: leave the folder alone too.
        return "held"
    return "free"


def _newest_mtime(path: str) -> float:
    newest = os.stat(path).st_mtime
    with os.scandir(path) as it:
        for child in it:
            with contextlib.suppress(OSError):
                newest = max(newest, child.stat(follow_symlinks=False).st_mtime)
    return newest


def _try_remove(path: str) -> bool:
    """Remove a file. False only if it is locked (still open elsewhere) and worth retrying."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except PermissionError:
        return False
    except OSError as e:
        log.warning("Couldn't delete a cached chunk: %s", type(e).__name__)
    return True

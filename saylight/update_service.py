"""UpdateService: the daily update check and the Install flow, for the panel's update line and the tray item.

The first check runs 2 minutes after start and never while Saylight is reading; after that a due check is looked
for every 15 minutes, and updates.due() keeps it to once a day. Network work runs on daemon threads. Results come
back through queued signals, so every public signal fires on the GUI thread and settings are only touched there.

Install downloads Saylight-Setup.exe into %TEMP%\\saylight_update_<random>\\, checks its size and SHA-256, starts
it with /SILENT and asks the App to quit. Setup starts Saylight again; the next start deletes the folder.
"""

from __future__ import annotations

import logging
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from . import paths, updates, win32
from .updates import CheckResult, DownloadError, Release

log = logging.getLogger("saylight.updates")

FIRST_CHECK_DELAY_MS = 2 * 60 * 1000
TICK_MS = 15 * 60 * 1000
PURGE_WAIT_S = 30.0     # how long Install waits for the start-up purge before going ahead

# Used when the settings file predates the update keys.
_DEFAULTS: dict[str, Any] = {"updates.check": True, "updates.last_check": 0.0, "updates.etag": "",
                             "updates.skipped": ""}


def purge_update_downloads(temp_root: str) -> int:
    """Delete installer folders (saylight_update_*) that earlier runs left in `temp_root`. Returns the count.

    Right after an update, Setup may still be running from the newest one; what it holds open stays until the
    next start."""
    try:
        entries = list(os.scandir(temp_root))
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if not entry.name.startswith(updates.UPDATE_DIR_PREFIX):
            continue
        try:
            if entry.is_junction() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed += not os.path.exists(entry.path)
    if removed:
        log.info("Removed %d old update download folder(s)", removed)
    return removed


class UpdateService(QObject):
    """Checks for a newer release and installs it on request. Create and use on the GUI thread."""

    available = Signal(object)              # a newer Release that isn't skipped
    downloadProgress = Signal(int, int)     # bytes, total (0 when unknown)
    failed = Signal(str)                    # "network" | "size" | "checksum" | "signature" | "launch"
    quitRequested = Signal()                # Setup has started; the App quits
    checked = Signal(str)                   # after every finished check: "newer" | "current" | "error"

    _checkDone = Signal(object)             # worker -> GUI thread relays
    _progress = Signal(int, int)
    _installDone = Signal(str, str)         # failure kind ("" once Setup started), detail

    def __init__(self, settings, current_version: str, *, online: Callable[[], bool],
                 busy: Callable[[], bool], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._current = current_version
        self._online = online
        self._busy = busy
        # Where releases come from, which certificates to trust, where the installer goes and how it starts.
        # Tests point these at a local HTTPS server and a fake launcher.
        self.site: updates.Site = updates.GITHUB
        self.context: ssl.SSLContext | None = None
        self.temp_root: str = tempfile.gettempdir()
        self.spawn: Callable[..., Any] = subprocess.Popen
        # Install runs Setup only for the installed app. The Python version updates from its download page, and
        # so does any copy where Smart App Control is on: it can block a fresh unsigned build outright, which
        # would leave the person with no working Saylight.
        self.can_install: bool = paths.is_frozen() and not win32.smart_app_control_on()
        self._checker = updates.UpdateChecker(current_version, self._checkDone.emit)
        self._manual = False
        # True from starting a check until its result is handled here. The checker frees itself just before the
        # result is queued, so its running flag alone would let a tick in that gap send a second request.
        self._pending = False
        self._installing = False
        self._started = False
        self._purge: threading.Thread | None = None
        self._latest: Release | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._checkDone.connect(self._on_check_done, Qt.ConnectionType.QueuedConnection)
        self._progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._installDone.connect(self._on_install_done, Qt.ConnectionType.QueuedConnection)

    # --- Public API

    @property
    def latest(self) -> Release | None:
        """The newer release found by the last check, even one the person skipped (the tray item stays for it)."""
        return self._latest

    @property
    def installing(self) -> bool:
        return self._installing

    def start(self) -> None:
        """Delete old installer downloads, then look for a due check after 2 minutes and every 15 after that."""
        if self._started:
            return
        self._started = True
        self._purge = threading.Thread(target=purge_update_downloads, args=(self.temp_root,), name="update-purge",
                                       daemon=True)
        self._purge.start()
        self._timer.start(FIRST_CHECK_DELAY_MS)

    def check_now(self) -> None:
        """Check now because the person asked: ignores the daily limit, the switch and a skipped version.
        If a check is already running, its result answers this request."""
        self._manual = True
        self._run_check(force=True)

    def skip(self, version: str) -> None:
        """"Not now": hide the panel line for this version. A later version shows again."""
        self._put("updates.skipped", version)

    def install(self, release: Release) -> None:
        """Download, check and start Setup for `release`. Ends in quitRequested or failed(kind). One at a time."""
        if self._installing:
            return
        self._installing = True
        threading.Thread(target=self._install_worker, args=(release,), name="update-download",
                         daemon=True).start()

    # --- Checking

    @Slot()
    def _tick(self) -> None:
        if self._timer.interval() != TICK_MS:
            self._timer.setInterval(TICK_MS)
        if not self._get("updates.check") or self._busy():
            return
        self._run_check(force=False)

    def _run_check(self, *, force: bool) -> None:
        if self._pending:  # the result on its way answers this request too
            return
        self._checker.site, self._checker.context = self.site, self.context
        online = True if force else self._online()
        self._pending = self._checker.maybe_check(True, online, float(self._get("updates.last_check") or 0.0),
                                                  str(self._get("updates.etag") or ""), force=force)

    @Slot(object)
    def _on_check_done(self, result: CheckResult) -> None:
        manual, self._manual, self._pending = self._manual, False, False
        self._put("updates.last_check", time.time())  # every attempt counts toward the daily limit
        status = "current" if result.status == "not_modified" else result.status
        if status == "current":
            self._latest = None
            if result.etag:
                self._put("updates.etag", result.etag)
        elif status == "newer":
            # A 304 carries no release, so don't ask with an ETag while an update is waiting.
            self._put("updates.etag", "")
        if status == "newer" and result.release is not None:
            self._latest = result.release
            # Stay quiet if checks were switched off while this one ran, unless the person asked for it.
            if manual or (self._get("updates.check") and not self._is_skipped(result.release.version)):
                self.available.emit(result.release)
        self.checked.emit(status)

    def _is_skipped(self, version: str) -> bool:
        skipped = updates.parse_version(str(self._get("updates.skipped") or ""))
        return skipped is not None and skipped == updates.parse_version(version)

    # --- Installing

    def _install_worker(self, release: Release) -> None:
        folder = ""
        kind = detail = ""
        try:
            if not release.setup_url:
                raise DownloadError("network", "no installer")
            expected = updates.installer_sha256(release, site=self.site, context=self.context,
                                                version=self._current)
            if self._purge is not None:  # a purge still listing folders could take the new one
                self._purge.join(PURGE_WAIT_S)
            folder = tempfile.mkdtemp(prefix=updates.UPDATE_DIR_PREFIX, dir=self.temp_root)
            path = os.path.join(folder, updates.SETUP_NAME)
            updates.download(release.setup_url, path, sha256=expected, expected_size=release.setup_size,
                             site=self.site, context=self.context, version=self._current,
                             progress=self._progress.emit)
            updates.launch_setup(path, cwd=self.temp_root, spawn=self.spawn)
        except DownloadError as e:
            kind, detail = e.kind, e.detail
        except Exception as e:  # never leave Install stuck
            kind, detail = "network", type(e).__name__
        if kind and folder:
            shutil.rmtree(folder, ignore_errors=True)
        self._installDone.emit(kind, detail)

    @Slot(int, int)
    def _on_progress(self, done: int, total: int) -> None:
        self.downloadProgress.emit(done, total)

    @Slot(str, str)
    def _on_install_done(self, kind: str, detail: str) -> None:
        if kind:
            self._installing = False
            log.warning("Update install stopped: %s %s", kind, detail)
            self.failed.emit(kind)
            return
        log.info("Update installer started; quitting")
        self.quitRequested.emit()

    # --- Settings

    def _get(self, key: str) -> Any:
        try:
            return self._settings.get(key)
        except KeyError:
            return _DEFAULTS[key]

    def _put(self, key: str, value: Any) -> None:
        try:
            self._settings.set(key, value)
        except (KeyError, ValueError) as e:
            log.debug("Couldn't store %s (%s)", key, type(e).__name__)

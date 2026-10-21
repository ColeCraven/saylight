"""Saylight: highlight it, and it says it.

Highlight text anywhere and press Ctrl+` to hear it in Ava's voice, with a control panel.
Press Ctrl+` with nothing highlighted to show or hide the panel (or to pause and resume while reading).

    pythonw saylight_app.py              run in the background
    python saylight_app.py --install     start automatically at sign-in and start now
    python saylight_app.py --uninstall   stop starting at sign-in, and quit
    python saylight_app.py --start       start it now
    python saylight_app.py --quit        quit the running copy
    python saylight_app.py <file>        open a PDF, Word, text or Markdown file (in the running copy, if any)

The installed Saylight.exe takes the same commands; its installer makes the Start menu entry. Settings and
logs live in %LOCALAPPDATA%\\Saylight (see saylight/paths.py).
"""

from __future__ import annotations

import ctypes
import faulthandler
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import traceback
import winreg
from ctypes import wintypes

from saylight import __version__, paths

APP_NAME = "Saylight"                       # the sign-in (Run) value
QUIT_EVENT_NAME = "Local\\Saylight.Quit"    # keeps it to one copy, and is how --quit reaches the running one
APP_USER_MODEL_ID = "Saylight.App"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SHORTCUT_NAME = "Saylight.lnk"
ICON_PATH = os.path.join(paths.install_dir(), "saylight.ico")  # a source install's Start menu icon
NOTICES_NAME = "THIRD_PARTY_NOTICES.txt"   # written next to Saylight.exe by the build
QUIT_WAIT_S = 1.5                           # time a copy asked to quit gets to save its settings and exit
UNDO_MS = 10_000                            # how long Undo is offered after a Clear
AWAY_SETTLE_MS = 50                         # how long light dismiss lets the foreground settle (as the box does)

# The same app under its earlier name. --install replaces that install, --uninstall and --quit reach it too,
# and paths.migrate_legacy_data() carries its settings file over.
OLD_RUN_VALUE = "ReadAloudHotkey"
OLD_QUIT_EVENT_NAME = "Local\\ReadAloudHotkey.Quit"
OLD_SHORTCUT_NAME = "Read Aloud.lnk"
OLD_ICON_PATH = os.path.join(paths.install_dir(), "read_aloud.ico")  # only its Start menu shortcut used it

EVENT_MODIFY_STATE = 0x0002
ERROR_ALREADY_EXISTS = 183
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
SUPPRESS_STATES = {2, 3, 4}  # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE
MB_OK, MB_YESNO, MB_ICONERROR, MB_SETFOREGROUND, IDYES = 0x0, 0x4, 0x10, 0x10000, 6

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

log = logging.getLogger("saylight.app")


# --- System operations (the commands use these, so tests can replace them) -----------------------

def signal_quit(event_name: str) -> bool:
    """Ask the copy that owns this quit event to quit. True if one was running."""
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, event_name)
    if not handle:
        return False
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    return True


def set_run_value(name: str, command: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)


def delete_run_value(name: str) -> bool:
    """Remove a sign-in entry. True if it was there."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        return False
    return True


def read_run_value(name: str) -> str | None:
    """The command of a sign-in entry, or None if there is none."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    return str(value)


def remove_file(path: str) -> bool:
    """Delete a file if it is there. True if it was deleted."""
    try:
        os.remove(path)
    except OSError:
        return False
    return True


def start_menu_path(name: str) -> str:
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs", name)  # never relative to the cwd


# --- Commands ------------------------------------------------------------------------------------

def run_command() -> str:
    """The sign-in command for this copy, each part quoted, as the installer writes it."""
    return " ".join(f'"{part}"' for part in paths.launch_command())


def quit_running() -> bool:
    """Quit the running copy, including one still running under the earlier name. True if one was running."""
    signalled = [signal_quit(name) for name in (QUIT_EVENT_NAME, OLD_QUIT_EVENT_NAME)]
    if not any(signalled):
        print("Saylight isn't running.")
        return False
    print("Saylight has quit.")
    return True


def start() -> None:
    command = paths.launch_command()
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(command, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        subprocess.Popen(command, creationflags=flags, close_fds=True)
    print("Saylight is running. Highlight text and press Ctrl+` to hear it.")


def make_icon(path: str) -> bool:
    """Render the app icon (white voice bars on a deep-blue rounded square) to an .ico file."""
    try:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter

        _app = QGuiApplication.instance() or QGuiApplication(["icon"])
        size = 256
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QLinearGradient(0, 0, 0, size)
        g.setColorAt(0, QColor("#3A66B0"))
        g.setColorAt(1, QColor("#24467F"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(QRectF(8, 8, size - 16, size - 16), 56, 56)
        p.setBrush(QColor("#FFFFFF"))
        for x, h in zip((58, 102, 146, 190), (72, 132, 104, 52)):
            p.drawRoundedRect(QRectF(x - 13, (size - h) / 2, 26, h), 13, 13)
        p.end()
        return img.save(path, "ICO")
    except Exception:
        return False


def create_shortcut() -> None:
    """A Start menu entry for a source install, so Saylight can be started again after quitting it."""
    if not os.path.exists(ICON_PATH):
        make_icon(ICON_PATH)
    pythonw, script = paths.launch_command()
    script_ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:RA_LNK); "
                 "$s.TargetPath = $env:RA_EXE; $s.Arguments = '\"' + $env:RA_SCRIPT + '\"'; "
                 "$s.WorkingDirectory = $env:RA_DIR; if (Test-Path $env:RA_ICON) { $s.IconLocation = $env:RA_ICON }; "
                 "$s.Description = 'Read highlighted text aloud'; $s.Save()")
    env = dict(os.environ, RA_LNK=start_menu_path(SHORTCUT_NAME), RA_EXE=pythonw, RA_SCRIPT=script,
               RA_DIR=paths.install_dir(), RA_ICON=ICON_PATH)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script_ps], env=env,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def retire_old_copy() -> bool:
    """Quit a copy still running under the earlier name, and give it time to save its settings and let go of
    Ctrl+`, so the two never run together. True if one was running."""
    if not signal_quit(OLD_QUIT_EVENT_NAME):
        return False
    log.info("Asked the copy running under the earlier name to quit")
    time.sleep(QUIT_WAIT_S)
    return True


def remove_old_install() -> list[str]:
    """Undo an install made under the earlier name: quit that copy, then remove its sign-in entry, its Start menu
    shortcut and the icon the shortcut used. Returns what was found and removed ("quit", "run", "shortcut", "icon")."""
    done = []
    if retire_old_copy():
        done.append("quit")
    if delete_run_value(OLD_RUN_VALUE):
        done.append("run")
    if remove_file(start_menu_path(OLD_SHORTCUT_NAME)):
        done.append("shortcut")
    if remove_file(OLD_ICON_PATH):
        done.append("icon")
    return done


def install() -> None:
    if remove_old_install():
        print("Replaced the earlier version (Read Aloud).")
    if signal_quit(QUIT_EVENT_NAME):
        time.sleep(QUIT_WAIT_S)
    set_run_value(APP_NAME, run_command())
    if paths.is_frozen():  # the installer made the Start menu entry, with the app's ID
        print("Saylight will start automatically when you sign in.")
    else:
        create_shortcut()
        print("Saylight will start automatically when you sign in, and is in the Start menu.")
    start()


def uninstall() -> None:
    for name in (APP_NAME, OLD_RUN_VALUE):
        delete_run_value(name)
    # The installed app's Start menu entry belongs to its uninstaller (Settings > Apps).
    shortcuts = (OLD_SHORTCUT_NAME,) if paths.is_frozen() else (SHORTCUT_NAME, OLD_SHORTCUT_NAME)
    for name in shortcuts:
        remove_file(start_menu_path(name))
    print("Saylight will no longer start when you sign in.")
    quit_running()


# --- Bootstrap -----------------------------------------------------------------------------------

CRASH_LOG_LIMIT = 256 * 1024  # bytes


def trim_crash_log(path: str, limit: int = CRASH_LOG_LIMIT) -> None:
    """Keep the crash log small: past `limit`, keep only its newest half, from a line start.

    faulthandler also writes a thread dump for errors that Windows' COM code raises and then handles itself
    (UI Automation does this while a screen reader is on), so the file can grow while Saylight runs fine."""
    try:
        if os.path.getsize(path) <= limit:
            return
        with open(path, "rb") as f:
            f.seek(-(limit // 2), os.SEEK_END)
            tail = f.read()
        with open(path, "wb") as f:
            f.write(tail[tail.find(b"\n") + 1:])
    except OSError:
        pass


def setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    trim_crash_log(paths.crash_path())
    crash = open(paths.crash_path(), "a", encoding="utf-8")  # kept open for faulthandler; stderr is None under pythonw
    faulthandler.enable(crash)
    sys.excepthook = lambda t, v, tb: logging.getLogger("saylight").error("Unhandled exception",
                                                                          exc_info=(t, v, tb))
    threading.excepthook = lambda a: logging.getLogger("saylight").error(
        "Unhandled exception in thread %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def settings_file() -> str:
    """The settings file to load: the data folder's, or for this session an earlier one next to the program
    that couldn't be moved there (the next start tries the move again)."""
    path = paths.settings_path()
    if not os.path.exists(path):
        for name in (paths.SETTINGS_NAME, paths.OLD_SETTINGS_NAME):
            earlier = os.path.join(paths.install_dir(), name)
            if os.path.isfile(earlier):
                log.info("Using the earlier settings file this session")
                return earlier
    return path


def run(open_path: str = "") -> int:
    setup_logging()
    ctypes.set_last_error(0)
    quit_event = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)
    if not quit_event or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        if open_path:  # Open with, or Saylight.exe <path>: the running copy opens it
            from saylight.single_instance import send_open
            sent = send_open(open_path)
            log.info("Already running; %s the file to it", "passed" if sent else "couldn't pass")
            return 0 if sent else 1
        log.info("Already running; exiting")
        return 0
    paths.migrate_legacy_data()  # before Settings reads the file
    settings_path = settings_file()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except OSError:
        pass

    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Saylight")
    app.setApplicationVersion(__version__)
    qt_log = logging.getLogger("saylight.qt")
    qInstallMessageHandler(lambda mode, _ctx, msg: qt_log.warning(msg) if mode != QtMsgType.QtDebugMsg else None)

    ctl = App(app, int(quit_event), settings_path)
    ctl.start()
    log.info("Saylight %s started (%s)", __version__, "installed" if paths.is_frozen() else "from source")
    if open_path:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: ctl.open_path(open_path, "command line"))
    code = app.exec()
    kernel32.CloseHandle(quit_event)
    return code


def run_safely(open_path: str = "") -> int:
    """Build the app and run it (and open `open_path`, if given). A start that fails ends in a plain message
    instead of a silent exit."""
    global App
    try:
        App = _build_app_class()
        return run(open_path) if open_path else run()
    except Exception:
        show_start_failure(record_crash())
        return 1


def record_crash() -> str:
    """Add the exception being handled to the crash log. Returns the log's path, or "" if it can't be written."""
    logging.getLogger("saylight").error("Saylight couldn't start", exc_info=True)
    try:
        path = paths.crash_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Saylight {__version__} couldn't start\n")
            traceback.print_exc(file=f)
    except OSError:
        return ""
    return path


def show_start_failure(path: str) -> None:
    """Say that Saylight couldn't start and where the details are, with a button that opens that folder."""
    text = f"Saylight couldn't start. Details are in {path}" if path else "Saylight couldn't start."
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        _app = QApplication.instance() or QApplication([sys.argv[0]])
        box = QMessageBox(QMessageBox.Icon.Critical, "Saylight", text)
        folder = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole) if path else None
        box.setDefaultButton(box.addButton(QMessageBox.StandardButton.Close))
        box.exec()
        wanted = folder is not None and box.clickedButton() is folder
    except Exception:  # Qt may be what failed: a plain Windows message instead
        flags = MB_ICONERROR | MB_SETFOREGROUND | (MB_YESNO if path else MB_OK)
        prompt = text + "\n\nOpen the folder now?" if path else text
        answer = ctypes.windll.user32.MessageBoxW(None, prompt, "Saylight", flags)
        wanted = bool(path) and answer == IDYES
    if wanted:
        try:
            os.startfile(os.path.dirname(path))
        except OSError:
            pass



# --- The app controller ----------------------------------------------------------------------------

def _build_app_class():
    from PySide6.QtCore import QObject, QRect, QStandardPaths, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QGuiApplication

    from saylight import win32
    from saylight.documents.loader import SUPPORTED, DocumentLoader
    from saylight.engine import ACTIVE, COPYING, OPENING, Engine
    from saylight.export import FAIL_DETAILS, AudioExporter, remaining_label
    from saylight.foreground import ForegroundTracker
    from saylight.input_thread import HK_COMPOSE, HK_PANEL, HK_PAUSE, HK_READ, InputThread
    from saylight.player import PlayerThread
    from saylight.pronunciations import PronunciationStore, base_spoken
    from saylight.resume import ResumeStore, same_path
    from saylight.settings import Settings
    from saylight.single_instance import OpenServer
    from saylight.text_model import BuildOptions, Document
    from saylight.theme import READING_PRESETS, TINT_NAMES, Theme
    from saylight.ui.compose import FOCUS_REASONS, ComposeWindow, reading_prefill, should_resume
    from saylight.ui.controls import G_DONE, G_ERROR, G_NOTHING
    from saylight.ui.hud import Hud
    from saylight.ui.motion import Clock
    from saylight.ui.panel import NOTICE_LOOK, Panel
    from saylight.ui.sheet import DialogHost
    from saylight.ui.tip import TipManager
    from saylight.ui.tray import Tray, speed_label
    from saylight.update_service import UpdateService
    from saylight.voices import make_backend, make_router
    from saylight.voices.base import EDGE, parse_voice_id
    from saylight.voices.catalog import VoiceCatalog
    from saylight.voices.preview import VoicePreview

    SHEET_FOCUS_REASONS = FOCUS_REASONS | {"saved"}   # closes that hand the foreground back
    KEPT_MS = 10 * 60 * 1000   # "Show saved audio in folder" and a failed save's Try again stay this long
    OPEN_FILTERS = ["Files Saylight can read (*.pdf *.docx *.txt *.md *.markdown)", "All files (*.*)"]
    SECTION_KEYS = {"notes": "read_asides", "refs": "read_references"}

    class _Builder:
        """The engine's builder: a reading with the current settings and this file's section chips. Text
        over 20k characters, and rebuilds of big files, go to the loader's worker."""

        def __init__(self, app: "App"):
            self.app = app

        def __call__(self, raw):
            return self.app.build_document(raw)

        def start(self, token: int, raw) -> None:
            self.app.loader.build(token, raw, self.app.build_options(raw))

    class App(QObject):
        """The controller every view talks to (`ctl`).

        For the settings pages: `version`, `router` (the engine's synth), `catalog` (the voice list), `preview`
        (voice samples), `updates` (the update check) and `pronunciations` (the list), plus set_voice(),
        notices_path(), open_notices(), start_with_windows(), set_start_with_windows() and set_resume().
        For files: open_path(), choose_file(), continue_reading(), open_password(), toggle_section(); for the
        flyout and the tray: open_save(), open_pronunciations(), change_word() and the save's state.
        """

        _continueFound = Signal(object)   # from the thread that checks which saved files still exist

        def __init__(self, qapp, quit_event: int, settings_path: str | None = None):
            super().__init__()
            self.qapp = qapp
            self.version = __version__
            self.settings = Settings(settings_path or paths.settings_path(), self)
            self.theme = Theme(self.settings, self)
            self.clock = Clock(self)
            self.tips = TipManager(self.theme)
            self.router = make_router(settings=self.settings, parent=self)
            self.catalog = VoiceCatalog(self, local=self._local_voices)
            self.preview = VoicePreview(make_backend, lambda: int(self.settings.get("volume")), self)
            self.player = PlayerThread(self, volume=int(self.settings.get("volume")))
            self.input = InputThread(quit_event, self._hotkey_map(), self)
            self.foreground = ForegroundTracker(self)
            self.pronunciations = PronunciationStore(paths.pronunciations_path(), self)
            self.resume = ResumeStore(paths.resume_path())
            self.loader = DocumentLoader(self)
            self._overrides: dict[str, bool] = {}  # this file's section chips, in memory only
            self._override_raw = None             # ...for this file's LoadedDocument
            self.engine = Engine(synth=self.router, player=self.player, input_thread=self.input,
                                 settings=self.settings, parent=self, beep=win32.message_beep,
                                 builder=_Builder(self), resume=self.resume, pronunciations=self.pronunciations)
            self.exporter = AudioExporter(make_backend, self)
            self.server = OpenServer(parent=self)
            self._online = True  # until Windows says otherwise; an unknown network state counts as online
            self.updates = UpdateService(self.settings, __version__, online=lambda: self._online,
                                         busy=self._busy, parent=self)
            self.panel = Panel(self)
            self.compose = ComposeWindow(self)  # hidden until opened; unowned, and its flags never change
            self.tray = Tray(self, self.theme)
            self.hud = Hud(self.theme, self.clock)
            # Made on first use: the sheets (each an unowned focusable window) and the native dialogs' owner.
            self._pron_sheet = self._save_sheet = self._password_sheet = None
            self._dialog_host: DialogHost | None = None
            self._pron_paused = None        # the reading the Pronunciations sheet paused
            self._pending: dict | None = None   # the file being opened: token, path, title, ext, started
            self._password_file: tuple[str, str] | None = None  # (path, title) of a PDF that needs a password
            self._last_open = ""            # the last file asked for (Try again)
            self._open_folder = ""          # the Open dialog's last folder, this session only
            self._continue = None           # the ResumePoint the Ready card and tray offer
            self._saved_path = ""           # the last saved audio, for "Show saved audio in folder" (memory only)
            self._save_failed = ""          # why the last save failed, while Try again is offered
            self._kept_timer = QTimer(self)
            self._kept_timer.setSingleShot(True)
            self._kept_timer.timeout.connect(self._forget_save)
            self._state = "ready"
            self._info: dict = {}
            self._suppressed = False
            self._override = False
            self._hud_preparing = False
            self._network = None
            self._compose_hotkey = False  # hotkeys.compose is registered
            self._undo: tuple | None = None  # (engine token, box snapshot) for the undo window after a Clear
            self._undo_timer = QTimer(self)
            self._undo_timer.setSingleShot(True)
            self._undo_timer.timeout.connect(self._on_undo_timeout)
            self._away_timer = QTimer(self)
            self._away_timer.setSingleShot(True)
            self._away_timer.timeout.connect(self._check_away)

            self._theme_timer = QTimer(self)
            self._theme_timer.setSingleShot(True)
            self._theme_timer.timeout.connect(self._refresh_theme)
            self._screen_timer = QTimer(self)
            self._screen_timer.setSingleShot(True)
            self._screen_timer.timeout.connect(self.panel.reclamp)
            self._screen_timer.timeout.connect(self._reanchor_compose)  # after the panel has found its place
            self._fullscreen_timer = QTimer(self)
            self._fullscreen_timer.setInterval(1000)
            self._fullscreen_timer.timeout.connect(self._check_fullscreen)

            e = self.engine
            e.stateChanged.connect(self._on_state)
            e.sessionChanged.connect(self.panel.on_session)
            e.sessionChanged.connect(self._on_session)
            e.position.connect(self.panel.on_position)
            e.timeLeft.connect(self.panel.on_time_left)
            e.speedChanged.connect(self.panel.on_speed)
            e.speedChanged.connect(self.compose.on_speed)
            e.volumeChanged.connect(self.panel.on_volume)
            e.notice.connect(self._on_notice)
            # An empty Ctrl+` from the shell while the box is open must not toggle the panel under it.
            e.panelToggleRequested.connect(lambda: None if self.composing() else self.toggle_panel())
            e.mediaKeysWanted.connect(self.input.set_media_keys)
            self.input.quitRequested.connect(self.quit)
            self.input.composeReadPressed.connect(self._on_compose_read_hotkey)
            self.input.hotkeyPressed.connect(self._on_app_hotkey)  # after the engine's own connection
            self.input.registered.connect(self._on_registered)
            # Queued: the box is closed after the WinEvent callback has returned, never inside it.
            self.foreground.appActivated.connect(self._on_app_activated, Qt.ConnectionType.QueuedConnection)
            c = self.compose
            c.readRequested.connect(self.read_typed)
            c.clearRequested.connect(self.clear_box)
            c.undoRequested.connect(self.undo_clear)
            c.closeRequested.connect(self.close_compose)
            c.sizeStepRequested.connect(self.panel.step_text_size)
            c.textEdited.connect(self._on_compose_edited)
            c.closed.connect(self._on_compose_closed)
            c.pronunciationRequested.connect(self._on_compose_word)
            # Files: the loader's answers go to the engine, and the engine's cancels to the loader.
            self.loader.progress.connect(self._on_open_progress)
            self.loader.built.connect(self._on_built)
            self.loader.failed.connect(self._on_open_failed)
            e.openingCancelled.connect(self.loader.cancel)
            e.resumeApplied.connect(self._on_resumed)
            self.server.openRequested.connect(lambda path: self.open_path(path, "open with"))
            self.server.openRejected.connect(self._on_open_rejected)
            self.pronunciations.changed.connect(self._on_pronunciations_changed)
            self.exporter.progress.connect(self._on_save_progress)
            self.exporter.finished.connect(self._on_saved)
            self.exporter.failed.connect(self._on_save_failed)
            self._continueFound.connect(self._on_continue_found, Qt.ConnectionType.QueuedConnection)
            self.settings.changed.connect(self._on_setting)
            QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_: self.schedule_theme_refresh())
            qapp.screenAdded.connect(self._watch_screen)
            qapp.screenRemoved.connect(lambda *_: self._screen_timer.start(300))
            for screen in QGuiApplication.screens():
                self._watch_screen(screen)
            self.updates.quitRequested.connect(self.quit)  # Setup has started and will replace this copy
            qapp.aboutToQuit.connect(self._shutdown)

        # --- Lifecycle --------------------------------------------------------------------------

        def start(self) -> None:
            self.engine.start()
            self.input.start()
            self.foreground.start()
            self._start_network_monitor()
            self.updates.start()
            self.server.listen()
            self.panel.relayout()
            self.tray.show()
            if self.settings.get("panel.visible"):
                self.panel.show_animated()
                self._fullscreen_timer.start()
            self._refresh_continue()

        def _start_network_monitor(self) -> None:
            try:
                from PySide6.QtNetwork import QNetworkInformation
                if QNetworkInformation.loadDefaultBackend() and QNetworkInformation.instance():
                    info = QNetworkInformation.instance()
                    self._network = info

                    def update(*_):
                        online = info.reachability() != QNetworkInformation.Reachability.Disconnected
                        self._online = online
                        self.engine.set_online(online)
                        self.router.set_reachable(online)

                    info.reachabilityChanged.connect(update)
                    update()
            except Exception:
                log.warning("Network status unavailable", exc_info=True)

        def _shutdown(self) -> None:
            log.info("Shutting down")
            self.close_compose("quit")  # hides it at once; typed text and the undo stash go with the process
            self.close_sheets("quit")
            self._fullscreen_timer.stop()
            self.server.close()
            self.exporter.shutdown()    # an unfinished save is cancelled and its .part file deleted
            self.preview.shutdown()
            self.engine.shutdown()      # saves a file's place first, when Pick up where you left off is on
            self.loader.shutdown()
            self.input.stop(3.0)
            self.foreground.stop()
            self.settings.flush()
            self.tray.hide()

        def quit(self) -> None:
            # Qt asks every window to close before it quits, and the box turns a close into Back, which would
            # cancel the quit: close it (and the sheets) first.
            self.close_compose("quit")
            self.close_sheets("quit")
            self.qapp.quit()

        # --- Panel visibility -------------------------------------------------------------------

        def panel_visible(self) -> bool:
            return self.panel.isVisible() and not self.panel._hiding

        def toggle_panel(self, from_tray: bool = False) -> None:
            if self.composing() or self._sheet_open():  # over the panel: close it and leave the panel as it is
                self.close_compose("tray" if from_tray else "hotkey")
                self.close_sheets("tray" if from_tray else "hotkey")
            else:
                self.set_panel_visible(not self.panel_visible())
            if from_tray:
                QTimer.singleShot(0, self.restore_foreground)

        def set_panel_visible(self, on: bool) -> None:
            self.close_compose("tray")  # only the tray gets here while the box is open, and it restores focus
            self.close_sheets("tray")
            if on:
                self._override = self._suppressed
                self._suppressed = False
                self.hud.dismiss()
                self.panel.show_animated()
                self._fullscreen_timer.start()
            else:
                self.panel.hide_animated()
                self._fullscreen_timer.stop()
                self._suppressed = False
                if not self.settings.get("first_hide_tip_shown"):
                    self.settings.set("first_hide_tip_shown", True)
                    QTimer.singleShot(300, lambda: self.hud.flash(
                        self._hud_anchor(), "idle", "TextSecondary", "Panel hidden",
                        [("key", "Ctrl"), ("key", "`"), ("text", " with nothing highlighted shows it")], seconds=4.0))
            self.settings.set("panel.visible", on)

        def open_settings(self) -> None:
            self.close_compose("tray")
            if not self.panel_visible():
                self.set_panel_visible(True)
            self.panel.set_page("settings")

        def reset_position(self) -> None:
            self.close_compose("tray")
            self.panel.reset_position()

        def restore_foreground(self) -> None:
            # The box, a sheet or an Open dialog has the foreground on purpose.
            if not self.composing() and not self._sheet_open() and not self._dialog_busy():
                self.foreground.restore_foreground()

        def _check_fullscreen(self) -> None:
            if self.compose.isVisible() or self._sheet_open():
                return
            busy = win32.query_user_notification_state() in SUPPRESS_STATES
            if busy and not self._override and self.panel_visible():
                self._suppressed = True
                self.panel.hide_animated()
            elif not busy:
                self._override = False
                if self._suppressed:
                    self._suppressed = False
                    self.panel.show_animated()

        # --- Engine → tray and HUD --------------------------------------------------------------

        def _on_state(self, state: str, info: dict) -> None:
            self._state, self._info = state, info
            self.panel.present(state, info)
            self.tray.set_state(state, info)
            if state == "opening" and info.get("open_kind") == "file" and self._hud_allowed():
                self._flash_opening(info)
            elif state in ("preparing", "copying", "opening") and self._hud_allowed():
                self._flash_preparing()
            elif self._hud_preparing and state not in ("preparing", "copying", "opening", "waiting", "reading"):
                self._hud_preparing = False
                self.hud.dismiss()

        def _flash_preparing(self) -> None:
            self._hud_preparing = True
            self.hud.flash(self._hud_anchor(), "wave", "Accent", "Preparing audio…", seconds=0)

        def _flash_opening(self, info: dict) -> None:
            self._hud_preparing = True
            done, total = info.get("open_done") or 0, info.get("open_total") or 0
            detail = f"page {max(1, done)} of {total}" if total and done < total else ""
            self.hud.flash(self._hud_anchor(), "wave", "Accent", "Opening…", detail, seconds=0)

        def _on_notice(self, kind: str) -> None:
            if not self._hud_allowed():
                return
            info = self._info
            counter = f"{info.get('sentence', 0) + 1} of {info.get('total', 0)}" if info.get("total") else ""
            if kind == "open_failed":  # the notice's label with its glyph; it waits on the panel
                self._hud_preparing = False
                token, glyph = NOTICE_LOOK.get(info.get("kind", ""), ("Critical", G_ERROR))
                long = token == "Critical" or info.get("kind") == "password"
                self.hud.flash(self._hud_anchor(), "glyph", token, info.get("label", "Can't open this file"),
                               glyph=glyph, seconds=5.0 if long else 2.5)
                return
            if kind in ("reading_started", "resumed"):
                self._hud_preparing = False
                self.hud.flash(self._hud_anchor(), "live", "Accent", "Reading", speed_label(self.speed()),
                               seconds=1.5)
            elif kind == "paused":
                self.hud.flash(self._hud_anchor(), "paused", "Accent", "Paused", counter, seconds=1.5)
            elif kind == "nothing_selected":
                self.hud.flash(self._hud_anchor(), "glyph", "Caution", "Nothing selected", glyph=G_NOTHING,
                               seconds=2.5)
            elif kind in ("error", "shortcut_unavailable"):
                self._hud_preparing = False
                if kind == "error":  # the next step: open the panel, where the message and Try again are
                    label = info.get("label", "Something went wrong")
                    detail = [("key", "Ctrl"), ("key", "`"), ("text", " shows the panel")]
                else:  # the shortcut itself is what's broken, so point at the tray instead
                    label, detail = "Shortcut unavailable", "Use the tray icon to open the panel"
                self.hud.flash(self._hud_anchor(), "glyph", "Critical", label, detail, glyph=G_ERROR, seconds=5.0)

        def _hud_allowed(self) -> bool:
            return (not self.panel_visible() and not self.compose.isVisible() and not self._sheet_open()
                    and bool(self.settings.get("hud_when_hidden"))
                    and win32.query_user_notification_state() not in SUPPRESS_STATES)

        def _hud_anchor(self) -> QRect:
            return self.panel.rest_rect()

        # --- Type or paste, Clear and Undo --------------------------------------------------------

        def composing(self) -> bool:
            """The Type or paste box is opening or open (not closing)."""
            return self.compose.session.phase in ("opening", "open")

        def has_draft(self) -> bool:
            return self.compose.has_draft()

        def can_undo_clear(self) -> bool:
            return self._undo is not None

        def open_compose(self, source: str) -> None:
            """Open the box over the panel ("button", "card", "tray" or "hotkey"), or bring it back to the front.

            Call it right in the click or hotkey handler: Windows lets the box take the foreground only then."""
            c, e = self.compose, self.engine
            if self.composing():
                c.activate()
                return
            if e.state == COPYING:  # the internal state, so also the first moments before Copying shows
                return
            fg = win32.get_foreground_window()
            return_to = fg if win32.is_app_window(fg) else self.foreground.last_app()
            p = self.panel
            if p.page == "settings":
                p.set_page("reading")
            p.set_volume_open(False, animate=False)
            p.close_flyout()
            self.close_sheets("switch")  # one focusable window at a time
            self.tips.press(p)
            prefill = reading_prefill(e.document, e.current_sentence)  # never a file's text
            self.input.set_compose_open(True)  # before the box can be in front, so Ctrl+` never copies from it
            c.open_at(p.rest_rect(), panel_visible=self.panel_visible(), panel_lines=8 if p.expanded else 4,
                      prefill=prefill, return_to=return_to)
            # Paused once the box is showing, so the pause never flashes the HUD; leaving without reading resumes.
            c.session.source = source
            c.session.paused_doc = e.document if e.pause() else None
            self._hud_preparing = False
            self.hud.dismiss()
            log.info("Compose opened (%s)", source)

        def close_compose(self, reason: str) -> None:
            """Close the box and keep its text (see compose.FOCUS_REASONS and RESUME_REASONS for what each reason
            does). Does nothing when the box isn't open."""
            if not self.composing():
                return
            c = self.compose
            s = c.session
            if reason in FOCUS_REASONS and c.is_foreground():  # never after you have gone to another app
                target = s.return_to if win32.is_app_window(s.return_to) else self.foreground.last_app()
                if target:
                    win32.set_foreground_window(target)  # before hiding, so Windows never picks a window itself
            self.input.set_compose_open(False)
            if should_resume(s.paused_doc, self.engine.document, self.engine.state, reason):
                self.engine.resume()
            self.panel.set_covered(False)
            c.close_animated(reason)

        def read_typed(self, text: str, scope: str) -> None:
            """Read aloud from the box: the highlighted part ("selection") or all of it ("all")."""
            if not self.engine.read_text(text):  # a new reading also ends any undo window (_on_session)
                self.compose.show_note("nothing")  # the box stays open; the note beeps
                return
            log.info("Compose: read %s characters (%s)", f"{len(text):,}", scope)
            self.close_compose("read")

        def clear_box(self) -> None:
            """Clear: stop, forget the text, its audio and any typed text. Undo is offered for 10 s."""
            c = self.compose
            typed = c.has_draft()
            token = self.engine.clear()
            snap = c.snapshot()
            if token is None and not snap.text:
                return
            c.clear_text()
            c.session.paused_doc = None
            self._undo = (token, snap)
            self._undo_timer.start(UNDO_MS)
            source = getattr(token.doc, "source", None) if token is not None else None
            self.panel.set_cleared(True, getattr(source, "title", "") if getattr(source, "kind", "") == "file" else "")
            self.panel.refresh_box_actions()
            c.set_undo_mode(True)
            log.info("Cleared (%d sentences, typed text: %s)", len(token.doc.sentences) if token else 0,
                     "yes" if typed else "no")

        def undo_clear(self) -> None:
            """Bring back what the last Clear removed, during its undo window."""
            if self._undo is None:
                return
            token, snap = self._undo
            if token is not None and not self.engine.restore(token):
                return  # refused while copying; the window stays open
            if token is None:
                log.info("Clear undone")  # the engine logs it when it restores a reading
            self.compose.restore_snapshot(snap)
            self._end_undo()  # (restoring a reading may already have ended it, before the typed text was back)
            self.panel.refresh_box_actions()

        def _end_undo(self) -> None:
            """End the undo window and drop what it kept; the card and the box's button go back."""
            if self._undo is None:
                return
            self._undo = None
            self._undo_timer.stop()
            self.panel.set_cleared(False)
            self.compose.set_undo_mode(False)
            self.panel.refresh_box_actions()

        def _on_undo_timeout(self) -> None:
            if self.panel.isVisible() and self.panel.view.underMouse():  # never take Undo from under the pointer
                self._undo_timer.start(UNDO_MS)
            else:
                self._end_undo()

        def _on_session(self, doc) -> None:
            if doc is not None:  # a new reading (or the cleared one coming back) ends the undo window
                self._end_undo()
            self._forget_prefill()  # a purge, or a newer reading replacing the one the box was filled from
            if self.engine.raw is not self._override_raw:  # a section chip is for its own file only
                self._overrides, self._override_raw = {}, None
            self._refresh_continue()  # the file that closed may be the one to offer next

        def _forget_prefill(self) -> None:
            """The hidden box holds a reading's text no longer than the engine does (the privacy purge reaches it
            too); the next open loads the current reading again. Typed text stays."""
            c = self.compose
            if not c.isVisible() and not c.has_draft():
                c.clear_text()

        def _on_compose_edited(self) -> None:
            self._end_undo()  # the first edit after a Clear
            self.panel.refresh_box_actions()  # Clear follows the typed text

        def _on_compose_closed(self, reason: str) -> None:
            self._forget_prefill()  # the reading may have been purged while the box was open
            if reason != "quit" and self._state in ("preparing", "copying") and self._hud_allowed():
                self._flash_preparing()

        def _on_compose_read_hotkey(self) -> None:
            if self.composing():
                self.compose.read_from_shortcut()

        def _on_app_hotkey(self, hk_id: int) -> None:
            """The Type or paste shortcut, and the panel shortcut while the box is open (which closes it)."""
            if hk_id == HK_COMPOSE:
                if self.composing():
                    self.close_compose("hotkey")
                else:
                    self.open_compose("hotkey")
            elif hk_id == HK_PANEL and self.composing():
                self.close_compose("hotkey")

        def _on_app_activated(self, _hwnd: int) -> None:
            # Light dismiss: you clicked into another app. Judge by what is in front once things settle, not by
            # the event alone: opening the emoji panel can pass the foreground through the shell and straight back.
            self.panel.close_flyout()
            if self.composing() or self._sheet_open():
                self._away_timer.start(AWAY_SETTLE_MS)

        def _check_away(self) -> None:
            fg = win32.get_foreground_window()
            if self.composing() and self.compose.is_dismissed_by(fg):
                self.close_compose("away")
            for sheet in self._sheets():
                if sheet.is_open and sheet.is_dismissed_by(fg):
                    self.close_sheet(sheet, "away")

        def _reanchor_compose(self) -> None:
            if self.composing():
                self.compose.reanchor(self.panel.rest_rect())
            for sheet in self._sheets():
                sheet.reanchor(self.panel.rest_rect())

        # --- Sheets: Pronunciations, Save as audio, the PDF password ------------------------------

        def _sheets(self) -> list:
            return [s for s in (self._pron_sheet, self._save_sheet, self._password_sheet) if s is not None]

        def _sheet_open(self) -> bool:
            return any(s.is_open for s in self._sheets())

        def _dialog_busy(self) -> bool:
            return self._dialog_host is not None and self._dialog_host.busy

        def _return_to(self) -> int:
            fg = win32.get_foreground_window()
            return fg if win32.is_app_window(fg) else self.foreground.last_app()

        def _open_sheet(self, sheet, source: str) -> bool:
            """Open a sheet over the panel, closing any other focusable window ("switch": it keeps its state).
            Call it right in the click, hotkey or tray handler: Windows lets it take the foreground only then."""
            p = self.panel
            p.close_flyout()
            p.set_volume_open(False, animate=False)
            self.close_compose("switch")
            for other in self._sheets():
                if other is not sheet and other.is_open:
                    self.close_sheet(other, "switch")
            self.tips.press(p)
            self._hud_preparing = False
            self.hud.dismiss()
            log.info("%s opened (%s)", type(sheet).__name__, source)
            return sheet.open_at(p.rest_rect(), return_to=self._return_to(), panel_visible=self.panel_visible(),
                                 source=source)

        def close_sheet(self, sheet, reason: str) -> None:
            """Close a sheet; the foreground goes back where it was for Esc, Back and a finished action."""
            if sheet is None or not sheet.is_open:
                return
            s = sheet.session
            if reason in SHEET_FOCUS_REASONS and sheet.is_foreground():
                target = s.return_to if win32.is_app_window(s.return_to) else self.foreground.last_app()
                if target:
                    win32.set_foreground_window(target)
            sheet.close_animated(reason)

        def close_sheets(self, reason: str) -> None:
            for sheet in self._sheets():
                self.close_sheet(sheet, reason)

        def _pron(self):
            if self._pron_sheet is None:
                from saylight.ui.pronunciation_dialog import PronunciationDialog
                d = self._pron_sheet = PronunciationDialog(self, self.pronunciations, self.preview)
                d.closeRequested.connect(lambda reason: self.close_sheet(d, reason))
                d.closed.connect(self._on_pron_closed)
                d.saved.connect(lambda note: self.panel.show_note(note))
            return self._pron_sheet

        def open_pronunciations(self, source: str = "") -> None:
            """The list of pronunciations (from the flyout, the tray or Settings)."""
            d = self._pron()
            if not d.is_open:
                d.show_list()
            self._open_pron(d, source)

        def change_word(self, word: str, offset: int | None = None) -> None:
            """Change how a word from the reading page is said (the right-click flyout): the edit view, with the
            word's sentence as the voice gets it for the preview."""
            d = self._pron()
            doc, context = self.engine.document, None
            if doc is not None and offset is not None and doc.sentences:
                i = doc.sentence_at_offset(offset) if hasattr(doc, "sentence_at_offset") else 0
                context = base_spoken(doc.sentences[max(0, min(i, len(doc.sentences) - 1))],
                                      self.pronunciations.current())
            d.show_word(word, context)
            self._open_pron(d, "word")

        def _on_compose_word(self, word: str) -> None:
            """The box's context menu: close the box ("switch") and change how the word is said."""
            d = self._pron()
            d.show_word(word, None)
            self._open_pron(d, "editor")

        def _open_pron(self, d, source: str) -> None:
            was_open = d.is_open
            self._open_sheet(d, source)
            if not was_open:  # paused once the sheet shows; leaving it resumes (as the box does)
                self._pron_paused = self.engine.document if self.engine.pause() else None

        def _on_pron_closed(self, reason: str) -> None:
            paused, self._pron_paused = self._pron_paused, None
            if paused is not None and paused is self.engine.document and self.engine.state == "paused" and (
                    reason == "saved" or should_resume(paused, self.engine.document, self.engine.state, reason)):
                self.engine.resume()

        def _on_pronunciations_changed(self) -> None:
            if self.engine.document is not None and self.settings.get("pronunciations.enabled"):
                self.engine.rebuild()

        def has_pronunciation(self, word: str) -> bool:
            return self.pronunciations.find_word(word) is not None

        # --- Files: opening, the Open dialog, Open with, passwords and section chips ---------------

        def build_options(self, raw=None) -> BuildOptions:
            """What a reading leaves out, from the settings, with this file's section chips for its own raw."""
            s = self.settings
            chips = self._overrides if raw is not None and raw is self._override_raw else {}
            plist = self.pronunciations.current() if s.get("pronunciations.enabled") else None
            return BuildOptions(skip_citations=bool(s.get("skip_citations")), skip_urls=bool(s.get("skip_urls")),
                                read_references=chips.get("read_references", bool(s.get("read_references"))),
                                read_asides=chips.get("read_asides", bool(s.get("read_asides"))),
                                pronunciations=plist)

        def build_document(self, raw):
            options = self.build_options(raw)
            if isinstance(raw, str):
                return Document.from_raw(raw, skip_citations=options.skip_citations, skip_urls=options.skip_urls,
                                         pronunciations=options.pronunciations)
            return Document.build(raw, options)

        def can_open(self) -> bool:
            return self.engine.state not in (COPYING, OPENING)

        def open_path(self, path: str, source: str = "") -> None:
            """Open a file and read it, from the start or from its saved place: after the Open dialog, a drop,
            Open with, `Saylight.exe <path>` or Continue. It never shows the panel or takes the foreground."""
            path = os.path.abspath(path)
            self.panel.close_flyout()
            self.close_compose("open")
            self.close_sheets("open")
            if self.panel.page == "settings":
                self.panel.set_page("reading")
            title = os.path.splitext(os.path.basename(path))[0]
            ext = os.path.splitext(path)[1].lower()
            self.panel.notice_ext = ext
            self._last_open = path
            if ext not in SUPPORTED:
                log.info("Open rejected: unsupported")
                self.engine.open_notice("unsupported", title)
                return
            if not os.path.isfile(path):
                self.resume.forget(path)  # a saved place for a file that has gone is no use
                self._refresh_continue()
                self.engine.open_notice("missing", title)
                return
            self._overrides, self._override_raw = {}, None
            self._password_file = None
            token = self.engine.begin_opening(title, "file")
            self._pending = {"token": token, "path": path, "title": title, "ext": ext, "t0": time.perf_counter()}
            self.loader.open(token, path, self.build_options())
            log.info("Open started: %s (%s)", ext[1:], source or "app")

        def _on_open_rejected(self, kind: str, path: str) -> None:
            """Open with handed over something Saylight can't open: a kind it doesn't read, or no file at all."""
            self.panel.notice_ext = os.path.splitext(path)[1].lower()
            self.engine.open_notice(kind, os.path.splitext(os.path.basename(path))[0])

        def _on_open_progress(self, token: int, done: int, total: int) -> None:
            if self._pending is not None and token == self._pending["token"]:
                self.close_sheet(self._password_sheet, "read")  # the password worked
            self.engine.opening_progress(token, done, total)

        def _on_built(self, token: int, doc, raw) -> None:
            pending = self._pending if self._pending is not None and token == self._pending["token"] else None
            if pending is not None:
                self._pending = None
                self.close_sheet(self._password_sheet, "read")
                pages = int(getattr(raw, "pages", 0) or 0)
                log.info("Open: %s, %s%s characters, %s ms", pending["ext"][1:],
                         f"{pages:,} page{'s' if pages != 1 else ''}, " if pages else "", f"{len(doc.text):,}",
                         f"{round((time.perf_counter() - pending['t0']) * 1000):,}")
            if not self.engine.read_document(doc, raw, token=token) or pending is None:
                return
            scanned = len(getattr(raw, "scanned_pages", None) or ())
            if scanned:
                self.panel.show_note(f"{scanned} scanned page{'s' if scanned != 1 else ''} skipped")

        def _on_open_failed(self, token: int, kind: str) -> None:
            pending = self._pending if self._pending is not None and token == self._pending["token"] else None
            if pending is not None:
                self._pending = None
                self.panel.notice_ext = pending["ext"]
                if kind == "password":
                    self._password_file = (pending["path"], pending["title"])
                sheet = self._password_sheet
                if kind == "password" and sheet is not None and sheet.is_open:
                    sheet.wrong_password()  # the notice comes back under the sheet, for when it closes
                else:
                    self.close_sheet(sheet, "read")
            self.engine.opening_failed(token, kind)

        def open_password(self, source: str = "") -> None:
            """The PDF password sheet, from the [Enter password] chip or the tray: only from a click."""
            if self._password_file is None:
                return
            if self._password_sheet is None:
                from saylight.ui.password_sheet import PasswordSheet
                s = self._password_sheet = PasswordSheet(self)
                s.closeRequested.connect(lambda reason: self.close_sheet(s, reason))
                s.submitted.connect(self._submit_password)
            self._password_sheet.prepare(self._password_file[1])
            self._open_sheet(self._password_sheet, source)

        def _submit_password(self, password: str) -> None:
            if self._password_file is None:
                return
            path, title = self._password_file
            if not os.path.isfile(path):
                self.close_sheet(self._password_sheet, "read")
                self.engine.open_notice("missing", title)
                return
            token = self.engine.begin_opening(title, "file")
            self._pending = {"token": token, "path": path, "title": title, "ext": ".pdf", "t0": time.perf_counter()}
            self.loader.open(token, path, self.build_options(), password=password)  # for this one call

        def retry_open(self) -> None:
            """[Try again] on a notice that the file couldn't be read."""
            if self._last_open:
                self.open_path(self._last_open, "retry")

        def _host(self) -> DialogHost:
            if self._dialog_host is None:
                self._dialog_host = DialogHost()
            return self._dialog_host

        def choose_file(self, source: str = "") -> None:
            """Open a file… (the card, the flyout or the tray): the Windows Open dialog, in front of the panel."""
            self.panel.close_flyout()
            if not self.can_open():
                return
            self.close_compose("switch")
            self.close_sheets("switch")
            folder = self._open_folder if os.path.isdir(self._open_folder) else QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DocumentsLocation)
            self._host().open_file(self.panel.rest_rect().center(), self._return_to(),
                                   lambda path: self._file_chosen(path, source), caption="Open a file to read aloud",
                                   filters=OPEN_FILTERS, directory=folder or "")

        def _file_chosen(self, path: str, source: str) -> None:
            self._open_folder = os.path.dirname(path)
            self.open_path(path, source)

        def toggle_section(self, name: str) -> None:
            """A section chip: read (or skip again) this file's notes or reference list. For this file only."""
            raw = self.engine.raw
            key = SECTION_KEYS.get(name)
            if key is None or raw is None or isinstance(raw, str):
                return
            if raw is not self._override_raw:
                self._overrides, self._override_raw = {}, raw
            read = not self._overrides.get(key, bool(self.settings.get(key)))
            self._overrides[key] = read
            self.engine.rebuild(read_section=name if read else None)

        def read_sections(self) -> None:
            """The tray's "Read the notes and references too": both, from the first of them."""
            raw = self.engine.raw
            if raw is None or isinstance(raw, str):
                return
            if raw is not self._override_raw:
                self._overrides, self._override_raw = {}, raw
            self._overrides.update(read_asides=True, read_references=True)
            sections = getattr(self.engine.document, "sections", None) or {}
            self.engine.rebuild(read_section="notes" if "notes" in sections else "refs")

        # --- Pick up where you left off -----------------------------------------------------------

        def continue_point(self) -> tuple[str, int] | None:
            """(title, page) of the file the Ready card and the tray offer to continue, or None."""
            p = self._continue
            if p is None or not self.settings.get("resume.enabled"):
                return None
            return os.path.splitext(os.path.basename(p.path))[0], int(getattr(p, "page", 0) or 0)

        def continue_reading(self, source: str = "") -> None:
            if self._continue is not None and self.settings.get("resume.enabled"):
                self.open_path(self._continue.path, "continue")

        def _refresh_continue(self) -> None:
            """Find the most recent saved place, other than the file being read, whose file still exists. The
            check runs off the GUI thread (a disconnected drive can take seconds); \\\\server paths are offered
            without one."""
            if not self.settings.get("resume.enabled"):
                self._continue = None
                return
            try:
                points = self.resume.recent()
            except Exception as e:
                log.warning("Couldn't read the saved places (%s)", type(e).__name__)
                return
            loaded = getattr(getattr(self.engine.document, "source", None), "path", "")
            points = [p for p in points if not (loaded and same_path(p.path, loaded))]

            def find() -> None:
                found = next((p for p in points if p.path.startswith("\\\\") or os.path.isfile(p.path)), None)
                self._continueFound.emit(found)

            threading.Thread(target=find, name="saylight.resume_check", daemon=True).start()

        def _on_continue_found(self, point) -> None:
            if point is not None and not self.settings.get("resume.enabled"):
                point = None
            if point != self._continue:
                self._continue = point
                self.panel.present(self._state, self._info)

        def set_resume(self, on: bool) -> int:
            """Pick up where you left off. Turning it off forgets every place at once (no Undo); returns how many."""
            forgot = 0
            if not on:
                try:
                    forgot = self.resume.forget(None)
                except Exception as e:
                    log.warning("Couldn't forget the saved places (%s)", type(e).__name__)
                log.info("Resume: off, forgot %d", forgot)
                self._continue = None
            self.settings.set("resume.enabled", on)
            if on:
                log.info("Resume: on")
                self._refresh_continue()
            self.panel.present(self._state, self._info)
            return forgot

        def remember_place(self) -> None:
            """"Remember this page next time": turn resume on; the engine saves this place at once."""
            self.set_resume(True)

        def _on_resumed(self, sentence: int) -> None:
            doc = self.engine.document
            resumed = self.engine.resumed or (sentence, True)
            page_of = getattr(doc, "page_of", None)
            try:
                page = int(page_of(sentence) or 0) if page_of is not None else 0
            except (IndexError, ValueError, TypeError):
                page = 0
            self.panel.on_resumed(sentence, bool(resumed[1]), page)

        # --- Save as audio ------------------------------------------------------------------------

        def _save(self):
            if self._save_sheet is None:
                from saylight.ui.export_dialog import ExportDialog
                d = self._save_sheet = ExportDialog(self)
                d.closeRequested.connect(lambda reason: self.close_sheet(d, reason))
                d.saveChosen.connect(self._start_save)
            return self._save_sheet

        def open_save(self, source: str = "") -> None:
            """Save as audio… (the flyout or the tray): the sheet, then the Windows Save dialog."""
            from saylight.ui.export_dialog import SaveSetup
            e = self.engine
            doc = e.document
            if doc is None or e.state == OPENING or self.exporter.busy:
                return
            voice = str(self.settings.get("voice") or "")
            info = self.catalog.find(voice) if voice else None
            backend = parse_voice_id(voice)[0] if voice else EDGE
            source_ = getattr(doc, "source", None)
            from_file = getattr(source_, "kind", "") == "file"
            current = max(0, e.current_sentence)
            page_of = getattr(doc, "page_of", None)
            page = page_of(current) if from_file and page_of is not None else None
            options = self.build_options(e.raw)
            sections = getattr(doc, "sections", None) or {}
            setup = SaveSetup(doc=doc, title=getattr(source_, "title", "") if from_file else "",
                              from_file=from_file, current=current, page=page, voice=voice,
                              voice_name=getattr(info, "name", "") or "Ava",
                              private=bool(getattr(info, "private", backend != EDGE)), speed=e.speed,
                              has_notes="notes" in sections, has_refs="refs" in sections,
                              read_notes=options.read_asides, read_refs=options.read_references)
            d = self._save()
            d.prepare(setup)
            self._open_sheet(d, source)

        def _start_save(self, path: str, first: int) -> None:
            d = self._save_sheet
            doc = d.setup.doc if d is not None and d.setup is not None else self.engine.document
            self.close_sheet(d, "read")
            if doc is None or self.exporter.busy:
                return
            self._end_failure()
            self._saved_path = ""
            try:
                self.exporter.start(doc, path, voice=str(self.settings.get("voice") or ""), speed=self.engine.speed,
                                    first=first)
            except RuntimeError:
                return
            self.panel.set_save_progress(0.0)
            self.panel.show_note("Saving audio")
            self.tray.refresh()

        def _on_save_progress(self, _done: int, _total: int) -> None:
            if self.exporter.busy:
                left = remaining_label(self.exporter.remaining_s())
                self.panel.set_save_progress(self.exporter.fraction(), left)
                self.tray.refresh()

        def _on_saved(self, path: str) -> None:
            self.panel.set_save_progress(None, done=True)
            self._saved_path = path   # memory only
            self._kept_timer.start(KEPT_MS)
            self.panel.show_note("Audio saved")
            self.tray.refresh()
            if self._hud_allowed():
                self.hud.flash(self._hud_anchor(), "glyph", "TextPrimary", "Audio saved", glyph=G_DONE, seconds=2.5)

        def _on_save_failed(self, kind: str, _detail: str) -> None:
            self.panel.set_save_progress(None)
            self._save_failed = FAIL_DETAILS.get(kind, FAIL_DETAILS.get("other", ""))
            self._kept_timer.start(KEPT_MS)
            self.panel.set_save_failed(True)
            self.panel.show_note("Couldn't save the audio", "Critical")
            self.tray.flash_badge("Critical", 60_000)
            self.tray.refresh()
            if self._hud_allowed():
                self.hud.flash(self._hud_anchor(), "glyph", "Critical", "Couldn't save the audio",
                               self._save_failed, glyph=G_ERROR, seconds=5.0)

        def cancel_save(self) -> None:
            if not self.exporter.busy:
                return
            self.exporter.cancel()  # its .part file is deleted
            self.panel.set_save_progress(None)
            self.panel.show_note("Saving cancelled · no file kept")
            self.tray.refresh()

        def retry_save(self) -> None:
            if self.exporter.retry():
                self._end_failure()
                self.panel.set_save_progress(0.0)
                self.panel.show_note("Saving audio")
                self.tray.refresh()

        def _end_failure(self) -> None:
            if self._save_failed:
                self._save_failed = ""
                self.panel.set_save_failed(False)

        def _forget_save(self) -> None:
            """Ten minutes on: "Show saved audio in folder" and a failed save's Try again go."""
            self._saved_path = ""
            if self._save_failed:
                self.exporter.cancel()  # lets go of the document it kept for Try again
                self._end_failure()
            self.panel.refresh_flyout()

        def show_saved_audio(self) -> None:
            path = self._saved_path
            if not path or not os.path.exists(path):
                return
            try:
                subprocess.Popen(f'explorer /select,"{path}"')
            except OSError as e:
                log.warning("Couldn't show the saved audio (%s)", type(e).__name__)

        def save_fraction(self) -> float | None:
            return self.exporter.fraction() if self.exporter.busy else None

        def saved_audio(self) -> str:
            return self._saved_path

        def flyout_state(self) -> dict:
            e = self.engine
            return {"can_open": self.can_open(),
                    "can_save": e.document is not None and e.state != OPENING and not self.exporter.busy,
                    "saving": self.save_fraction(), "failed": self._save_failed, "saved": bool(self._saved_path)}

        # --- Settings, speed, theme -------------------------------------------------------------

        def speed(self) -> float:
            return self.engine.speed

        def set_speed(self, v: float) -> None:
            self.engine.set_speed(v)

        def has_text(self) -> bool:
            return bool(self._info.get("has_text"))

        def tint_items(self) -> list[tuple[str, str, QColor, QColor]]:
            mode = "dark" if self.theme.dark else "light"
            items = []
            for key, modes in READING_PRESETS.items():
                preset = modes.get(mode, {})
                bg = QColor(preset.get("background") or self.theme.color("Surface").name())
                fg = QColor(preset.get("text") or self.theme.color("TextPrimary").name())
                items.append((key, TINT_NAMES.get(key, key.capitalize()), bg, fg))
            return items

        def _hotkey_map(self) -> dict[int, str]:
            h = self.settings.get("hotkeys") or {}
            return {HK_READ: h.get("read", "Ctrl+`"), HK_PANEL: h.get("toggle_panel", ""),
                    HK_PAUSE: h.get("pause_resume", ""), HK_COMPOSE: h.get("compose", "")}

        def compose_shortcut(self) -> str:
            """The Type or paste shortcut while it is registered, else ""."""
            return (self.settings.get("hotkeys") or {}).get("compose", "") if self._compose_hotkey else ""

        def _on_registered(self, hk_id: int, ok: bool) -> None:
            if hk_id == HK_COMPOSE:
                self._compose_hotkey = ok
                self.panel.refresh_box_actions()  # the Type tooltip names the shortcut

        def _on_setting(self, key: str) -> None:
            if key.startswith("hotkeys"):
                self._compose_hotkey = False  # an unset shortcut is never reported back
                self.panel.refresh_box_actions()
                self.input.set_hotkeys(self._hotkey_map())
            elif key == "reading_tint":
                self.panel.view.update()
            elif key == "resume.enabled":
                if not self.settings.get("resume.enabled"):
                    if os.path.exists(self.resume.path):  # turned off anywhere: the file goes at once
                        self.resume.forget(None)
                    self._continue = None
                self._refresh_continue()

        def retry_shortcut(self) -> None:
            self.input.set_hotkeys(self._hotkey_map())

        def open_log(self) -> None:
            try:
                os.startfile(paths.log_path())
            except OSError as e:
                log.warning("Couldn't open the log (%s)", type(e).__name__)

        # --- Voices, updates, sign-in and notices (the settings pages) ----------------------------

        def set_voice(self, voice_id: str) -> None:
            """Read with this voice (a VoiceInfo.id; a bare 1.0 name means an online voice). The engine uses it
            from the next chunk it asks for. Raises ValueError for an ID that isn't a voice."""
            self.settings.set("voice", voice_id)

        def _local_voices(self) -> list:
            """The voices that need no network to list, apart from the online ones the catalog has itself."""
            return [voice for voice in self.router.voices() if voice.backend != EDGE]

        def _busy(self) -> bool:
            """A reading is running (paused included), a selection is being copied, a file opened, or audio
            saved: no update check now."""
            return self.engine.state in ACTIVE or self.exporter.busy

        def notices_path(self) -> str:
            """THIRD_PARTY_NOTICES.txt next to Saylight.exe (from source: the repository's notices)."""
            path = os.path.join(paths.install_dir(), NOTICES_NAME)
            if not paths.is_frozen() and not os.path.exists(path):
                return os.path.splitext(path)[0] + ".md"
            return path

        def open_notices(self) -> bool:
            """Open the licence notices in the person's viewer. False if that failed."""
            try:
                os.startfile(self.notices_path())
            except OSError as e:
                log.warning("Couldn't open the notices (%s)", type(e).__name__)
                return False
            return True

        def start_with_windows(self) -> bool:
            """Saylight starts when you sign in (its Run value exists)."""
            try:
                return read_run_value(APP_NAME) is not None
            except OSError as e:
                log.warning("Couldn't read the sign-in entry (%s)", type(e).__name__)
                return False

        def set_start_with_windows(self, on: bool) -> bool:
            """Add or remove the sign-in entry for this copy. True if it now matches `on`."""
            try:
                if on:
                    set_run_value(APP_NAME, run_command())
                else:
                    delete_run_value(APP_NAME)
            except OSError as e:
                log.warning("Couldn't change the sign-in entry (%s)", type(e).__name__)
                return False
            log.info("Start with Windows %s", "on" if on else "off")
            return True

        def schedule_theme_refresh(self) -> None:
            self._theme_timer.start(250)

        def _refresh_theme(self) -> None:
            self.theme.refresh()

        def _watch_screen(self, screen) -> None:
            screen.availableGeometryChanged.connect(lambda *_: self._screen_timer.start(300))
            screen.logicalDotsPerInchChanged.connect(lambda *_: self.schedule_theme_refresh())
            self._screen_timer.start(300)

    return App


App = None  # built by run_safely(); Qt is imported only when actually running, so the CLI stays fast


def main() -> None:
    commands = {"--install": install, "--uninstall": uninstall, "--start": start, "--quit": quit_running}
    if len(sys.argv) == 1:
        sys.exit(run_safely())
    elif sys.argv[1] in commands:
        commands[sys.argv[1]]()
    elif not sys.argv[1].startswith("-"):  # a file: Windows' Open with, or Saylight.exe <path>
        sys.exit(run_safely(os.path.abspath(sys.argv[1])))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

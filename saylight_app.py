"""Saylight: highlight it, and it says it.

Highlight text anywhere and press Ctrl+` to hear it in Ava's voice, with a control panel.
Press Ctrl+` with nothing highlighted to show or hide the panel (or to pause and resume while reading).

    pythonw saylight_app.py              run in the background
    python saylight_app.py --install     start automatically at sign-in and start now
    python saylight_app.py --uninstall   stop starting at sign-in, and quit
    python saylight_app.py --start       start it now
    python saylight_app.py --quit        quit the running copy

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


def run() -> int:
    setup_logging()
    ctypes.set_last_error(0)
    quit_event = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)
    if not quit_event or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
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
    code = app.exec()
    kernel32.CloseHandle(quit_event)
    return code


def run_safely() -> int:
    """Build the app and run it. A start that fails ends in a plain message instead of a silent exit."""
    global App
    try:
        App = _build_app_class()
        return run()
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
    from PySide6.QtCore import QObject, QRect, Qt, QTimer
    from PySide6.QtGui import QColor, QGuiApplication

    from saylight import win32
    from saylight.engine import ACTIVE, COPYING, Engine
    from saylight.foreground import ForegroundTracker
    from saylight.input_thread import HK_COMPOSE, HK_PANEL, HK_PAUSE, HK_READ, InputThread
    from saylight.player import PlayerThread
    from saylight.settings import Settings
    from saylight.theme import READING_PRESETS, TINT_NAMES, Theme
    from saylight.ui.compose import FOCUS_REASONS, ComposeWindow, should_resume
    from saylight.ui.controls import G_ERROR, G_NOTHING
    from saylight.ui.hud import Hud
    from saylight.ui.motion import Clock
    from saylight.ui.panel import Panel
    from saylight.ui.tip import TipManager
    from saylight.ui.tray import Tray, speed_label
    from saylight.update_service import UpdateService
    from saylight.voices import make_backend, make_router
    from saylight.voices.base import EDGE
    from saylight.voices.catalog import VoiceCatalog
    from saylight.voices.preview import VoicePreview

    class App(QObject):
        """The controller every view talks to (`ctl`).

        For the settings pages: `version`, `router` (the engine's synth), `catalog` (the voice list), `preview`
        (voice samples) and `updates` (the update check), plus set_voice(), notices_path(), open_notices(),
        start_with_windows() and set_start_with_windows().
        """

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
            self.engine = Engine(synth=self.router, player=self.player, input_thread=self.input,
                                 settings=self.settings, parent=self, beep=win32.message_beep)
            self._online = True  # until Windows says otherwise; an unknown network state counts as online
            self.updates = UpdateService(self.settings, __version__, online=lambda: self._online,
                                         busy=self._busy, parent=self)
            self.panel = Panel(self)
            self.compose = ComposeWindow(self)  # hidden until opened; unowned, and its flags never change
            self.tray = Tray(self, self.theme)
            self.hud = Hud(self.theme, self.clock)
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
            self.panel.relayout()
            self.tray.show()
            if self.settings.get("panel.visible"):
                self.panel.show_animated()
                self._fullscreen_timer.start()

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
            self._fullscreen_timer.stop()
            self.preview.shutdown()
            self.engine.shutdown()
            self.input.stop(3.0)
            self.foreground.stop()
            self.settings.flush()
            self.tray.hide()

        def quit(self) -> None:
            # Qt asks every window to close before it quits, and the box turns a close into Back, which would
            # cancel the quit: close it first.
            self.close_compose("quit")
            self.qapp.quit()

        # --- Panel visibility -------------------------------------------------------------------

        def panel_visible(self) -> bool:
            return self.panel.isVisible() and not self.panel._hiding

        def toggle_panel(self, from_tray: bool = False) -> None:
            if self.composing():  # the box is over the panel: close it and leave the panel as it is
                self.close_compose("tray" if from_tray else "hotkey")
            else:
                self.set_panel_visible(not self.panel_visible())
            if from_tray:
                QTimer.singleShot(0, self.restore_foreground)

        def set_panel_visible(self, on: bool) -> None:
            self.close_compose("tray")  # only the tray gets here while the box is open, and it restores focus
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
            if not self.composing():  # the box has the foreground on purpose
                self.foreground.restore_foreground()

        def _check_fullscreen(self) -> None:
            if self.compose.isVisible():
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
            if state in ("preparing", "copying") and self._hud_allowed():
                self._flash_preparing()
            elif self._hud_preparing and state not in ("preparing", "copying", "waiting", "reading"):
                self._hud_preparing = False
                self.hud.dismiss()

        def _flash_preparing(self) -> None:
            self._hud_preparing = True
            self.hud.flash(self._hud_anchor(), "wave", "Accent", "Preparing audio…", seconds=0)

        def _on_notice(self, kind: str) -> None:
            if not self._hud_allowed():
                return
            info = self._info
            counter = f"{info.get('sentence', 0) + 1} of {info.get('total', 0)}" if info.get("total") else ""
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
            return (not self.panel_visible() and not self.compose.isVisible()
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
            self.tips.press(p)
            doc = e.document
            prefill = (doc.text, doc.sentences[max(0, e.current_sentence)].start) if doc is not None else None
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
            self.panel.set_cleared(True)
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
            if self.composing():
                self._away_timer.start(AWAY_SETTLE_MS)

        def _check_away(self) -> None:
            if self.compose.is_dismissed_by(win32.get_foreground_window()):
                self.close_compose("away")

        def _reanchor_compose(self) -> None:
            if self.composing():
                self.compose.reanchor(self.panel.rest_rect())

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
            """A reading is running (paused included) or a selection is being copied: no update check now."""
            return self.engine.state in ACTIVE

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
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

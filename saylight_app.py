"""Saylight: highlight it, and it says it.

Highlight text anywhere and press Ctrl+` to hear it in Ava's voice, with a control panel.
Press Ctrl+` with nothing highlighted to show or hide the panel (or to pause and resume while reading).

    pythonw saylight_app.py              run in the background
    python saylight_app.py --install     start automatically at sign-in and start now
    python saylight_app.py --uninstall   stop starting at sign-in, and quit
    python saylight_app.py --start       start it now
    python saylight_app.py --quit        quit the running copy
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
import winreg
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(__file__)
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
APP_NAME = "Saylight"                       # the sign-in (Run) value
QUIT_EVENT_NAME = "Local\\Saylight.Quit"    # keeps it to one copy, and is how --quit reaches the running one
APP_USER_MODEL_ID = "Saylight.App"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SHORTCUT_NAME = "Saylight.lnk"
SETTINGS_PATH = os.path.join(HERE, "saylight_settings.json")
LOG_PATH = os.path.join(HERE, "saylight.log")
CRASH_PATH = os.path.join(HERE, "saylight_crash.log")
ICON_PATH = os.path.join(HERE, "saylight.ico")
QUIT_WAIT_S = 1.5                           # time a copy asked to quit gets to save its settings and exit

# The same app under its earlier name. --install replaces that install, --uninstall and --quit reach it too,
# and its settings file is renamed on the first start (migrate_settings).
OLD_RUN_VALUE = "ReadAloudHotkey"
OLD_QUIT_EVENT_NAME = "Local\\ReadAloudHotkey.Quit"
OLD_SHORTCUT_NAME = "Read Aloud.lnk"
OLD_SETTINGS_PATH = os.path.join(HERE, "read_aloud_settings.json")
OLD_ICON_PATH = os.path.join(HERE, "read_aloud.ico")  # only its Start menu shortcut used it

EVENT_MODIFY_STATE = 0x0002
ERROR_ALREADY_EXISTS = 183
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
SUPPRESS_STATES = {2, 3, 4}  # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE

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

def quit_running() -> bool:
    """Quit the running copy, including one still running under the earlier name. True if one was running."""
    signalled = [signal_quit(name) for name in (QUIT_EVENT_NAME, OLD_QUIT_EVENT_NAME)]
    if not any(signalled):
        print("Saylight isn't running.")
        return False
    print("Saylight has quit.")
    return True


def start() -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([PYTHONW, SCRIPT], creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        subprocess.Popen([PYTHONW, SCRIPT], creationflags=flags, close_fds=True)
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
    """A Start menu entry, so Saylight can be started again after quitting it."""
    if not os.path.exists(ICON_PATH):
        make_icon(ICON_PATH)
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:RA_LNK); $s.TargetPath = $env:RA_EXE; "
              "$s.Arguments = '\"' + $env:RA_SCRIPT + '\"'; $s.WorkingDirectory = $env:RA_DIR; "
              "if (Test-Path $env:RA_ICON) { $s.IconLocation = $env:RA_ICON }; "
              "$s.Description = 'Read highlighted text aloud'; $s.Save()")
    env = dict(os.environ, RA_LNK=start_menu_path(SHORTCUT_NAME), RA_EXE=PYTHONW, RA_SCRIPT=SCRIPT, RA_DIR=HERE,
               RA_ICON=ICON_PATH)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
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
    set_run_value(APP_NAME, f'"{PYTHONW}" "{SCRIPT}"')
    create_shortcut()
    print("Saylight will start automatically when you sign in, and is in the Start menu.")
    start()


def uninstall() -> None:
    for name in (APP_NAME, OLD_RUN_VALUE):
        delete_run_value(name)
    for name in (SHORTCUT_NAME, OLD_SHORTCUT_NAME):
        remove_file(start_menu_path(name))
    print("Saylight will no longer start when you sign in.")
    quit_running()


# --- Bootstrap -----------------------------------------------------------------------------------

def setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    crash = open(CRASH_PATH, "a", encoding="utf-8")  # kept open for faulthandler; stderr is None under pythonw
    faulthandler.enable(crash)
    sys.excepthook = lambda t, v, tb: logging.getLogger("saylight").error("Unhandled exception",
                                                                          exc_info=(t, v, tb))
    threading.excepthook = lambda a: logging.getLogger("saylight").error(
        "Unhandled exception in thread %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def migrate_settings(new_path: str, old_path: str) -> str:
    """Carry the settings over from the earlier name: if only the old settings file exists, rename it to the new
    name. Returns the file to load: the new one, or the old one if the rename keeps failing, so nothing is lost
    this session and the next start tries again."""
    if os.path.exists(new_path) or not os.path.exists(old_path):
        return new_path
    for attempt in range(4):  # a sharing violation (antivirus, OneDrive) is usually gone within moments
        try:
            os.rename(old_path, new_path)  # unlike os.replace, never overwrites a file that appeared meanwhile
        except (FileExistsError, FileNotFoundError):
            return new_path
        except OSError as e:
            if attempt == 3:
                log.error("Couldn't rename %s to %s (%s); using the old file this time",
                          os.path.basename(old_path), os.path.basename(new_path), type(e).__name__)
                return old_path
            time.sleep(0.05)
        else:
            log.info("Renamed the settings file %s to %s", os.path.basename(old_path), os.path.basename(new_path))
            return new_path
    return new_path


def run() -> int:
    setup_logging()
    ctypes.set_last_error(0)
    quit_event = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)
    if not quit_event or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log.info("Already running; exiting")
        return 0
    settings_path = migrate_settings(SETTINGS_PATH, OLD_SETTINGS_PATH)  # before Settings reads it
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except OSError:
        pass

    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Saylight")
    qt_log = logging.getLogger("saylight.qt")
    qInstallMessageHandler(lambda mode, _ctx, msg: qt_log.warning(msg) if mode != QtMsgType.QtDebugMsg else None)

    ctl = App(app, int(quit_event), settings_path)
    ctl.start()
    log.info("Saylight started")
    code = app.exec()
    kernel32.CloseHandle(quit_event)
    return code


# --- The app controller ----------------------------------------------------------------------------

def _build_app_class():
    from PySide6.QtCore import QObject, QRect, QTimer
    from PySide6.QtGui import QColor, QGuiApplication

    from saylight import win32
    from saylight.engine import Engine
    from saylight.foreground import ForegroundTracker
    from saylight.input_thread import HK_PANEL, HK_PAUSE, HK_READ, InputThread
    from saylight.player import PlayerThread
    from saylight.settings import Settings
    from saylight.synth import SynthWorker
    from saylight.theme import READING_PRESETS, TINT_NAMES, Theme
    from saylight.ui.controls import G_ERROR, G_NOTHING
    from saylight.ui.hud import Hud
    from saylight.ui.motion import Clock
    from saylight.ui.panel import Panel
    from saylight.ui.tip import TipManager
    from saylight.ui.tray import Tray, speed_label

    class App(QObject):
        """The controller every view talks to (`ctl`)."""

        def __init__(self, qapp, quit_event: int, settings_path: str | None = None):
            super().__init__()
            self.qapp = qapp
            self.settings = Settings(settings_path or SETTINGS_PATH, self)
            self.theme = Theme(self.settings, self)
            self.clock = Clock(self)
            self.tips = TipManager(self.theme)
            self.synth = SynthWorker(self)
            self.player = PlayerThread(self, volume=int(self.settings.get("volume")))
            self.input = InputThread(quit_event, self._hotkey_map(), self)
            self.foreground = ForegroundTracker(self)
            self.engine = Engine(synth=self.synth, player=self.player, input_thread=self.input,
                                 settings=self.settings, parent=self, beep=win32.message_beep)
            self.panel = Panel(self)
            self.tray = Tray(self, self.theme)
            self.hud = Hud(self.theme, self.clock)
            self._state = "ready"
            self._info: dict = {}
            self._suppressed = False
            self._override = False
            self._hud_preparing = False
            self._network = None

            self._theme_timer = QTimer(self)
            self._theme_timer.setSingleShot(True)
            self._theme_timer.timeout.connect(self._refresh_theme)
            self._screen_timer = QTimer(self)
            self._screen_timer.setSingleShot(True)
            self._screen_timer.timeout.connect(self.panel.reclamp)
            self._fullscreen_timer = QTimer(self)
            self._fullscreen_timer.setInterval(1000)
            self._fullscreen_timer.timeout.connect(self._check_fullscreen)

            e = self.engine
            e.stateChanged.connect(self._on_state)
            e.sessionChanged.connect(self.panel.on_session)
            e.position.connect(self.panel.on_position)
            e.timeLeft.connect(self.panel.on_time_left)
            e.speedChanged.connect(self.panel.on_speed)
            e.volumeChanged.connect(self.panel.on_volume)
            e.notice.connect(self._on_notice)
            e.panelToggleRequested.connect(lambda: self.toggle_panel())
            e.mediaKeysWanted.connect(self.input.set_media_keys)
            self.input.quitRequested.connect(self.quit)
            self.settings.changed.connect(self._on_setting)
            QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_: self.schedule_theme_refresh())
            qapp.screenAdded.connect(self._watch_screen)
            qapp.screenRemoved.connect(lambda *_: self._screen_timer.start(300))
            for screen in QGuiApplication.screens():
                self._watch_screen(screen)
            qapp.aboutToQuit.connect(self._shutdown)

        # --- Lifecycle --------------------------------------------------------------------------

        def start(self) -> None:
            self.engine.start()
            self.input.start()
            self.foreground.start()
            self._start_network_monitor()
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
                        self.engine.set_online(online)

                    info.reachabilityChanged.connect(update)
                    update()
            except Exception:
                log.warning("Network status unavailable", exc_info=True)

        def _shutdown(self) -> None:
            log.info("Shutting down")
            self._fullscreen_timer.stop()
            self.engine.shutdown()
            self.input.stop(3.0)
            self.foreground.stop()
            self.settings.flush()
            self.tray.hide()

        def quit(self) -> None:
            self.qapp.quit()

        # --- Panel visibility -------------------------------------------------------------------

        def panel_visible(self) -> bool:
            return self.panel.isVisible() and not self.panel._hiding

        def toggle_panel(self, from_tray: bool = False) -> None:
            self.set_panel_visible(not self.panel_visible())
            if from_tray:
                QTimer.singleShot(0, self.restore_foreground)

        def set_panel_visible(self, on: bool) -> None:
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
            if not self.panel_visible():
                self.set_panel_visible(True)
            self.panel.set_page("settings")

        def reset_position(self) -> None:
            self.panel.reset_position()

        def restore_foreground(self) -> None:
            self.foreground.restore_foreground()

        def _check_fullscreen(self) -> None:
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
                self._hud_preparing = True
                self.hud.flash(self._hud_anchor(), "wave", "Accent", "Preparing audio…", seconds=0)
            elif self._hud_preparing and state not in ("preparing", "copying", "waiting", "reading"):
                self._hud_preparing = False
                self.hud.dismiss()

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
            return (not self.panel_visible() and bool(self.settings.get("hud_when_hidden"))
                    and win32.query_user_notification_state() not in SUPPRESS_STATES)

        def _hud_anchor(self) -> QRect:
            if self.panel._rest.isNull():
                self.panel.restore_position()
            return QRect(self.panel._rest, self.panel.size())

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
                    HK_PAUSE: h.get("pause_resume", "")}

        def _on_setting(self, key: str) -> None:
            if key.startswith("hotkeys"):
                self.input.set_hotkeys(self._hotkey_map())
            elif key == "reading_tint":
                self.panel.view.update()

        def retry_shortcut(self) -> None:
            self.input.set_hotkeys(self._hotkey_map())

        def open_log(self) -> None:
            try:
                os.startfile(LOG_PATH)
            except OSError:
                log.warning("Couldn't open the log", exc_info=True)

        def schedule_theme_refresh(self) -> None:
            self._theme_timer.start(250)

        def _refresh_theme(self) -> None:
            self.theme.refresh()

        def _watch_screen(self, screen) -> None:
            screen.availableGeometryChanged.connect(lambda *_: self._screen_timer.start(300))
            screen.logicalDotsPerInchChanged.connect(lambda *_: self.schedule_theme_refresh())
            self._screen_timer.start(300)

    return App


App = None  # built by main(); Qt is imported only when actually running, so the CLI stays fast


def main() -> None:
    global App
    commands = {"--install": install, "--uninstall": uninstall, "--start": start, "--quit": quit_running}
    if len(sys.argv) == 1:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        App = _build_app_class()
        sys.exit(run())
    elif sys.argv[1] in commands:
        commands[sys.argv[1]]()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

"""Tests for saylight_app.py: choosing the settings file, the crash-on-start message, the commands (also as the
installed Saylight.exe), and the App: its wiring for the settings pages and its handling of the Type or paste
box, Clear and Undo.

No test here touches the registry, named events or the Start menu: the system operations are replaced by a fake,
and the modules that reach the real ones are swapped for stand-ins that fail the test if used. The App tests build
it from fakes (engine, input, foreground, panel, box, tray, HUD, voices and the update check), with Win32
replaced: no window is shown, no hotkey is registered and nothing plays. The data folder is a temporary one.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QObject, QRect, QTimer, Signal
from PySide6.QtNetwork import QNetworkInformation
from PySide6.QtWidgets import QApplication

import saylight
import saylight_app as A
from saylight import paths, voices, win32
from saylight.engine import ClearedReading
from saylight.input_thread import HK_COMPOSE, HK_PANEL, HK_READ_SCREEN, HK_RULER
from saylight.settings import Settings
from saylight.ui.compose import ComposeSession, ComposeSnapshot
from saylight.ui.sheet import SheetSession
from saylight.voices.base import VoiceInfo
from saylight.voices.catalog import VoiceCatalog

_app = None
_data_env = None


def setUpModule() -> None:
    global _app, _data_env
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])
    _data_env = mock.patch.dict(os.environ, {"SAYLIGHT_DATA_DIR": tempfile.mkdtemp(prefix="saylight_app_data_")})
    _data_env.start()


def tearDownModule() -> None:
    folder = os.environ["SAYLIGHT_DATA_DIR"]
    _data_env.stop()
    shutil.rmtree(folder, True)


class SettingsFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app_dir = tempfile.mkdtemp(prefix="saylight_app_test_")
        self.addCleanup(shutil.rmtree, self.app_dir, True)
        patcher = mock.patch.object(A.paths, "install_dir", lambda: self.app_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data_file = paths.settings_path()
        self.addCleanup(lambda: os.path.exists(self.data_file) and os.remove(self.data_file))

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"speed": 1.1}, f)

    def test_the_data_folder_file_is_used(self) -> None:
        self.assertEqual(A.settings_file(), self.data_file)
        self.write(self.data_file)
        self.write(os.path.join(self.app_dir, "saylight_settings.json"))
        self.assertEqual(A.settings_file(), self.data_file)

    def test_an_earlier_file_that_could_not_move_is_used_this_session(self) -> None:
        for name in ("read_aloud_settings.json", "saylight_settings.json"):  # the newer name wins
            earlier = os.path.join(self.app_dir, name)
            self.write(earlier)
            with self.subTest(name=name), self.assertLogs("saylight.app", "INFO") as logs:
                self.assertEqual(A.settings_file(), earlier)
            self.assertNotIn(self.app_dir, " ".join(logs.output))


class CrashLogTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.mkdtemp(prefix="saylight_app_test_")
        self.addCleanup(shutil.rmtree, folder, True)
        self.path = os.path.join(folder, "saylight_crash.log")

    def write(self, data: bytes) -> None:
        with open(self.path, "wb") as f:
            f.write(data)

    def test_a_long_crash_log_keeps_its_newest_lines(self) -> None:
        lines = [f"Windows fatal exception: code 0x8001010d, dump {i:05d}\n".encode() for i in range(20_000)]
        self.write(b"".join(lines))
        A.trim_crash_log(self.path, limit=100_000)
        with open(self.path, "rb") as f:
            kept = f.read()
        self.assertLessEqual(len(kept), 50_000)
        self.assertTrue(kept.endswith(lines[-1]))
        self.assertIn(kept[:kept.index(b"\n") + 1], lines)  # starts on a whole line

    def test_a_short_or_missing_crash_log_is_left_alone(self) -> None:
        A.trim_crash_log(self.path)
        self.assertFalse(os.path.exists(self.path))
        self.write(b"Saylight couldn't start\n")
        A.trim_crash_log(self.path)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"Saylight couldn't start\n")


class StartFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shown: list[str] = []
        self.crash = paths.crash_path()
        self.addCleanup(lambda: os.path.exists(self.crash) and os.remove(self.crash))

    def run_failing(self, **patches) -> int:
        with mock.patch.multiple(A, show_start_failure=self.shown.append, App=A.App, **patches), \
                self.assertLogs("saylight", "ERROR"):
            return A.run_safely()

    def test_a_failed_start_is_recorded_and_explained(self) -> None:
        def boom():
            raise RuntimeError("no screen")

        self.assertEqual(self.run_failing(_build_app_class=lambda: object, run=boom), 1)
        self.assertEqual(self.shown, [self.crash])
        with open(self.crash, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(f"Saylight {saylight.__version__} couldn't start", text)
        self.assertIn("RuntimeError: no screen", text)

    def test_a_failure_while_loading_is_caught_too(self) -> None:
        def broken():
            raise ImportError("missing piece")

        self.assertEqual(self.run_failing(_build_app_class=broken), 1)
        self.assertEqual(self.shown, [self.crash])

    def test_the_message_names_the_file_and_opens_its_folder(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        seen, opened = [], []

        def exec_(box):
            seen.append((box.text(), [b.text() for b in box.buttons()], box.isVisible()))
            return 0

        def clicked(box):
            return next(b for b in box.buttons() if b.text() == "Open folder")

        path = os.path.join(paths.data_dir(), "saylight_crash.log")
        with mock.patch.object(QMessageBox, "exec", exec_), mock.patch.object(QMessageBox, "clickedButton", clicked), \
                mock.patch.object(A.os, "startfile", opened.append, create=True):
            A.show_start_failure(path)
        text, buttons, visible = seen[0]
        self.assertEqual(text, f"Saylight couldn't start. Details are in {path}")
        self.assertIn("Open folder", buttons)
        self.assertFalse(visible)
        self.assertEqual(opened, [paths.data_dir()])

    def test_without_qt_a_windows_message_is_shown(self) -> None:
        calls, opened = [], []

        def message_box(_owner, text, title, flags):
            calls.append((text, title, flags))
            return A.IDYES

        fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(
            user32=types.SimpleNamespace(MessageBoxW=message_box)))
        with mock.patch("PySide6.QtWidgets.QMessageBox", side_effect=RuntimeError("no Qt")), \
                mock.patch.object(A, "ctypes", fake_ctypes), mock.patch.object(A.os, "startfile", opened.append):
            A.show_start_failure("C:\\data\\saylight_crash.log")
        self.assertEqual(calls[0][1], "Saylight")
        self.assertTrue(calls[0][0].startswith("Saylight couldn't start. Details are in C:\\data\\saylight_crash.log"))
        self.assertTrue(calls[0][2] & A.MB_YESNO)
        self.assertEqual(opened, ["C:\\data"])

    def test_with_no_crash_file_there_is_no_folder_to_open(self) -> None:
        calls = []
        fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(
            user32=types.SimpleNamespace(MessageBoxW=lambda *a: calls.append(a) or A.IDYES)))
        with mock.patch("PySide6.QtWidgets.QMessageBox", side_effect=RuntimeError("no Qt")), \
                mock.patch.object(A, "ctypes", fake_ctypes), mock.patch.object(A.os, "startfile") as startfile:
            A.show_start_failure("")
        self.assertEqual(calls[0][1], "Saylight couldn't start.")
        self.assertFalse(calls[0][3] & A.MB_YESNO)
        startfile.assert_not_called()


class _Forbidden:
    """Stands in for a module that reaches the real system; any use fails the test."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str):
        raise AssertionError(f"the test reached the real {self._name}.{attr}")


class FakeSystem:
    """The registry, named events and files as the commands see them, with every call recorded."""

    def __init__(self, *, running=(), run_values=(), files=()) -> None:
        self.running, self.run_values, self.files = set(running), set(run_values), set(files)
        self.calls: list[tuple] = []

    def signal_quit(self, name: str) -> bool:
        self.calls.append(("signal", name))
        return name in self.running

    def set_run_value(self, name: str, command: str) -> None:
        self.calls.append(("set_run", name, command))
        self.run_values.add(name)

    def delete_run_value(self, name: str) -> bool:
        self.calls.append(("delete_run", name))
        found = name in self.run_values
        self.run_values.discard(name)
        return found

    def read_run_value(self, name: str) -> str | None:
        return '"value"' if name in self.run_values else None

    def remove_file(self, path: str) -> bool:
        self.calls.append(("remove", path))
        found = path in self.files
        self.files.discard(path)
        return found

    def sleep(self, seconds: float) -> None:
        self.calls.append(("sleep", seconds))

    @contextlib.contextmanager
    def installed(self):
        """Route saylight_app's system operations here; the real modules behind them are made unusable."""
        with mock.patch.multiple(A, signal_quit=self.signal_quit, set_run_value=self.set_run_value,
                                 delete_run_value=self.delete_run_value, read_run_value=self.read_run_value,
                                 remove_file=self.remove_file,
                                 create_shortcut=lambda: self.calls.append(("create_shortcut",)),
                                 start=lambda: self.calls.append(("start",)),
                                 time=types.SimpleNamespace(sleep=self.sleep), winreg=_Forbidden("winreg"),
                                 kernel32=_Forbidden("kernel32"), subprocess=_Forbidden("subprocess")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            yield out


OLD_LNK = A.start_menu_path("Read Aloud.lnk")
NEW_LNK = A.start_menu_path("Saylight.lnk")
OLD_EVENT, NEW_EVENT = "Local\\ReadAloudHotkey.Quit", "Local\\Saylight.Quit"


class OldInstallTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual((A.APP_NAME, A.QUIT_EVENT_NAME, A.SHORTCUT_NAME), ("Saylight", NEW_EVENT, "Saylight.lnk"))
        self.assertEqual((A.OLD_RUN_VALUE, A.OLD_QUIT_EVENT_NAME, A.OLD_SHORTCUT_NAME),
                         ("ReadAloudHotkey", OLD_EVENT, "Read Aloud.lnk"))
        self.assertEqual(A.APP_USER_MODEL_ID, "Saylight.App")
        self.assertTrue(OLD_LNK.endswith(r"Microsoft\Windows\Start Menu\Programs\Read Aloud.lnk"))
        self.assertEqual(os.path.basename(A.OLD_ICON_PATH), "read_aloud.ico")

    def test_start_menu_path_without_appdata_is_still_absolute(self):
        env = {k: v for k, v in os.environ.items() if k.upper() != "APPDATA"}
        with mock.patch.dict(os.environ, env, clear=True):
            path = A.start_menu_path("Read Aloud.lnk")
        self.assertTrue(os.path.isabs(path), path)  # never a path relative to the current folder
        self.assertTrue(path.endswith(r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Read Aloud.lnk"), path)

    def test_remove_old_install_quits_it_then_removes_its_entries(self):
        fake = FakeSystem(running={OLD_EVENT}, run_values={"ReadAloudHotkey", "Other"},
                          files={OLD_LNK, A.OLD_ICON_PATH, NEW_LNK})
        with fake.installed():
            done = A.remove_old_install()
        self.assertEqual(done, ["quit", "run", "shortcut", "icon"])
        self.assertEqual(fake.calls, [("signal", OLD_EVENT), ("sleep", A.QUIT_WAIT_S),
                                      ("delete_run", "ReadAloudHotkey"), ("remove", OLD_LNK),
                                      ("remove", A.OLD_ICON_PATH)])
        self.assertEqual(fake.run_values, {"Other"})
        self.assertEqual(fake.files, {NEW_LNK})

    def test_remove_old_install_with_nothing_there_does_not_wait(self):
        fake = FakeSystem()
        with fake.installed():
            self.assertEqual(A.remove_old_install(), [])
        self.assertNotIn("sleep", [c[0] for c in fake.calls])
        self.assertEqual([c for c in fake.calls if c[0] != "remove"], [("signal", OLD_EVENT),
                                                                      ("delete_run", "ReadAloudHotkey")])

    def test_install_replaces_the_old_install_before_adding_the_new_one(self):
        fake = FakeSystem(running={OLD_EVENT}, run_values={"ReadAloudHotkey"}, files={OLD_LNK})
        with fake.installed() as out:
            A.install()
        kinds = [c[0] for c in fake.calls]
        self.assertLess(kinds.index("delete_run"), kinds.index("set_run"))
        self.assertLess(fake.calls.index(("remove", OLD_LNK)), kinds.index("create_shortcut"))
        self.assertEqual(kinds[-1], "start")
        self.assertIn(("signal", NEW_EVENT), fake.calls)
        set_run = [c for c in fake.calls if c[0] == "set_run"]
        self.assertEqual(len(set_run), 1)
        self.assertEqual(set_run[0][1], "Saylight")
        self.assertTrue(set_run[0][2].endswith('saylight_app.py"'), set_run[0][2])
        self.assertEqual(fake.run_values, {"Saylight"})
        self.assertNotIn(OLD_LNK, fake.files)
        self.assertIn("Replaced the earlier version", out.getvalue())

    def test_install_without_an_old_install_says_nothing_about_it(self):
        fake = FakeSystem()
        with fake.installed() as out:
            A.install()
        self.assertNotIn("earlier version", out.getvalue())
        self.assertEqual(fake.run_values, {"Saylight"})

    def test_uninstall_removes_old_and_new_entries_and_quits_both(self):
        fake = FakeSystem(running={OLD_EVENT, NEW_EVENT}, run_values={"Saylight", "ReadAloudHotkey", "Other"},
                          files={NEW_LNK, OLD_LNK})
        with fake.installed() as out:
            A.uninstall()
        self.assertEqual(fake.run_values, {"Other"})
        self.assertEqual(fake.files, set())
        self.assertIn(("signal", NEW_EVENT), fake.calls)
        self.assertIn(("signal", OLD_EVENT), fake.calls)
        self.assertIn("Saylight has quit.", out.getvalue())

    def test_the_sign_in_command_quotes_each_part(self):
        pythonw = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "pythonw.exe")
        self.assertEqual(A.run_command(), f'"{pythonw}" "{os.path.join(paths.install_dir(), "saylight_app.py")}"')
        exe = r"C:\Users\Ann Lee\AppData\Local\Programs\Saylight\Saylight.exe"
        with mock.patch.multiple(sys, frozen=True, executable=exe, create=True):
            self.assertEqual(A.run_command(), f'"{exe}"')  # as the installer writes it

    def test_installed_install_and_uninstall_leave_the_start_menu_to_the_installer(self):
        fake = FakeSystem(run_values={"ReadAloudHotkey"}, files={OLD_LNK, NEW_LNK})
        exe = r"C:\Programs\Saylight\Saylight.exe"
        with mock.patch.multiple(sys, frozen=True, executable=exe, create=True), fake.installed():
            A.install()
            self.assertNotIn(("create_shortcut",), fake.calls)
            self.assertIn(("set_run", "Saylight", f'"{exe}"'), fake.calls)
            self.assertEqual(fake.calls[-1], ("start",))
            A.uninstall()
        self.assertEqual(fake.run_values, set())
        self.assertEqual(fake.files, {NEW_LNK})  # Settings > Apps removes it

    def test_quit_signals_both_names(self):
        for running, expected in (({OLD_EVENT}, True), ({NEW_EVENT}, True), (set(), False)):
            fake = FakeSystem(running=running)
            with self.subTest(running=running), fake.installed() as out:
                self.assertIs(A.quit_running(), expected)
                self.assertEqual(sorted(c[1] for c in fake.calls if c[0] == "signal"), sorted([OLD_EVENT, NEW_EVENT]))
                self.assertIn("Saylight has quit." if expected else "Saylight isn't running.", out.getvalue())


# --- The App: Type or paste, Clear and Undo --------------------------------------------------------

BOX, APP_WINDOW, OTHER_APP, SHELL, LAST_APP = 4242, 555, 700, 701, 888
QUNS_ACCEPTS_NOTIFICATIONS, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_BUSY = 5, 3, 2


class World:
    """What the fakes share with a test: one event log, and the foreground as Win32 would report it."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.foreground = APP_WINDOW
        self.apps = {APP_WINDOW, OTHER_APP, LAST_APP}
        self.last_app = LAST_APP
        self.notification_state = QUNS_ACCEPTS_NOTIFICATIONS

    def log(self, *event) -> None:
        self.events.append(event)

    def kinds(self) -> list:
        return [e[0] for e in self.events]

    def set_foreground_window(self, hwnd: int) -> bool:
        self.log("set_foreground", hwnd)
        self.foreground = hwnd
        return True


world = World()


class FakeDoc:
    def __init__(self, text: str = "First one. Second one.") -> None:
        self.text = text
        self.sentences = [types.SimpleNamespace(start=0), types.SimpleNamespace(start=11)]


class FakeEngine(QObject):
    """The engine's API as the App uses it, with Clear, Undo, pause and resume reduced to their effect."""

    stateChanged = Signal(str, dict)
    sessionChanged = Signal(object)
    position = Signal(int, int, int, float, float, bool)
    timeLeft = Signal(int)
    speedChanged = Signal(float, str)
    volumeChanged = Signal(int)
    notice = Signal(str)
    panelToggleRequested = Signal()
    mediaKeysWanted = Signal(bool)
    openingCancelled = Signal(int)
    resumeApplied = Signal(int)

    def __init__(self, *, parent=None, builder=None, resume=None, pronunciations=None, **_parts) -> None:
        super().__init__(parent)
        self.state, self.document, self.cur, self.speed = "ready", None, 0, 1.0
        self.speakable = True
        self.builder, self.store, self.plist = builder, resume, pronunciations
        self.raw, self.resumed, self.token = None, None, 0

    # --- Files, as the App drives them

    def begin_opening(self, title: str, kind: str = "file") -> int:
        self.token += 1
        world.log("begin_opening", title, kind)
        self.before, self.state = self.state, "opening"
        return self.token

    def opening_progress(self, token: int, done: int, total: int, *, phase: str = "read") -> None:
        world.log("opening_progress", token, done, total, *(("ocr",) if phase == "ocr" else ()))

    def opening_choice(self, token: int, scanned: int, pages: int, est_s: int) -> None:
        world.log("opening_choice", token, scanned, pages, est_s)

    def opening_failed(self, token: int, kind: str) -> None:
        world.log("opening_failed", token, kind)
        self.state = getattr(self, "before", "ready") if kind == "cancelled" else "open_failed"

    def ocr_notice(self, kind: str, detail: str = "") -> None:
        world.log("ocr_notice", kind, detail)
        self.state = "ocr_notice"

    def open_notice(self, kind: str, title: str = "") -> None:
        world.log("open_notice", kind, title)
        self.state = "open_failed"

    def read_document(self, doc, raw=None, *, token=None) -> bool:
        world.log("read_document", token)
        self.document, self.raw, self.state = doc, raw, "preparing"
        self.sessionChanged.emit(doc)
        return True

    def rebuild(self, read_section=None) -> None:
        world.log("rebuild", read_section)

    def start_over(self) -> None:
        world.log("start_over")

    @property
    def current_sentence(self) -> int:
        return self.cur if self.document is not None else -1

    def pause(self) -> bool:
        if self.state not in ("preparing", "reading", "waiting"):
            return False
        world.log("pause")
        self.state = "paused"
        self.notice.emit("paused")
        return True

    def resume(self) -> bool:
        if self.state != "paused":
            return False
        world.log("resume")
        self.state = "reading"
        self.notice.emit("resumed")
        return True

    def read_text(self, text: str) -> bool:
        world.log("read_text", text)
        if not self.speakable:
            return False
        self.document, self.state = FakeDoc(text), "preparing"
        self.sessionChanged.emit(self.document)
        return True

    def clear(self) -> ClearedReading | None:
        world.log("clear")
        doc, self.document, self.state = self.document, None, "ready"
        if doc is None:
            return None
        self.sessionChanged.emit(None)
        return ClearedReading(doc, self.cur, False)

    def restore(self, token: ClearedReading | None) -> bool:
        world.log("restore")
        if token is None or self.document is not None or self.state == "copying":
            return False
        self.document, self.state = token.doc, "stopped"
        self.sessionChanged.emit(self.document)
        return True

    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def set_online(self, _online: bool) -> None:
        pass


class FakeInput(QObject):
    ready = Signal()
    registered = Signal(int, bool)
    hotkeyPressed = Signal(int)
    copied = Signal(int, str, str)
    composeReadPressed = Signal()
    quitRequested = Signal()
    overlayCancelPressed = Signal(str)

    def __init__(self, _quit_event, hotkeys: dict, parent=None) -> None:
        super().__init__(parent)
        self.hotkeys = dict(hotkeys)

    def set_compose_open(self, on: bool) -> None:
        world.log("compose_open", on)

    def set_hotkeys(self, hotkeys: dict) -> None:
        self.hotkeys = dict(hotkeys)

    def set_media_keys(self, _on: bool) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self, _timeout: float = 0) -> None:
        pass


class FakeForeground(QObject):
    appActivated = Signal(int)

    def last_app(self) -> int:
        return world.last_app

    def restore_foreground(self) -> bool:
        world.log("restore_foreground")
        return True

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass


class FakeView:
    def __init__(self) -> None:
        self.pointer = False

    def underMouse(self) -> bool:
        return self.pointer

    def update(self) -> None:
        pass


class FakePanel:
    def __init__(self, _ctl) -> None:
        self.page, self.expanded, self.visible, self._hiding, self.cleared = "reading", False, True, False, False
        self.view = FakeView()
        self.rest = QRect(740, 768, 440, 252)
        self.notice_ext, self.cleared_title, self.notes, self.progress = "", "", [], []
        self.failed_badge = False
        self.tabs = Quiet()

    def set_cleared(self, on: bool, title: str = "") -> None:
        self.cleared, self.cleared_title = on, title

    def show_note(self, text: str, color: str = "TextSecondary", **_kw) -> None:
        self.notes.append((text, color))

    def set_save_progress(self, fraction, eta: str = "", *, done: bool = False) -> None:
        self.progress.append((fraction, done))

    def set_save_failed(self, on: bool) -> None:
        self.failed_badge = on

    def on_resumed(self, sentence: int, exact: bool, page: int) -> None:
        world.log("on_resumed", sentence, exact, page)

    def close_flyout(self, now: bool = False) -> None:
        world.log("close_flyout")

    def isVisible(self) -> bool:
        return self.visible

    def rest_rect(self) -> QRect:
        return QRect(self.rest)

    def set_page(self, page: str) -> None:
        world.log("set_page", page)
        self.page = page

    def set_volume_open(self, on: bool, *, animate: bool = True) -> None:
        world.log("volume_open", on)

    def set_covered(self, on: bool) -> None:
        world.log("covered", on)

    def refresh_box_actions(self) -> None:
        world.log("refresh_box_actions")

    def show_animated(self) -> None:
        world.log("panel_show")
        self.visible = True

    def hide_animated(self) -> None:
        world.log("panel_hide")
        self.visible = False

    def reset_position(self) -> None:
        world.log("reset_position")

    def step_text_size(self, direction: int) -> None:
        world.log("text_size", direction)

    def _ignore(self, *_args) -> None:
        pass

    present = on_session = on_position = on_time_left = on_speed = on_volume = reclamp = relayout = _ignore
    refresh_flyout = _ignore


class FakeCompose(QObject):
    """The box: opens at once (activation granted unless `grant` is off) and closes at once unless `instant` is
    off, when the test ends the close fade with finish()."""

    readRequested = Signal(str, str)
    clearRequested = Signal()
    undoRequested = Signal()
    closeRequested = Signal(str)
    sizeStepRequested = Signal(int)
    textEdited = Signal()
    closed = Signal(str)
    pronunciationRequested = Signal(str)

    hwnd = BOX

    def __init__(self, _ctl) -> None:
        super().__init__()
        self.session = ComposeSession()
        self.visible, self.instant, self.grant = False, True, True
        self.text, self.user_text, self.undo_mode = "", False, False
        self._reason = ""

    def open_at(self, rest: QRect, *, panel_visible: bool, panel_lines: int, prefill, return_to: int) -> None:
        world.log("open_at", panel_visible, panel_lines, prefill, return_to)
        self.session.phase, self.session.return_to = "open", return_to
        self.visible = True
        self.session.was_active = self.activate()

    def activate(self) -> bool:
        world.log("activate")
        if self.grant:
            world.foreground = BOX
        return self.grant

    def close_animated(self, reason: str) -> None:
        world.log("close_animated", reason)
        if self.session.phase in ("closed", "closing"):
            return
        self.session.phase, self._reason = "closing", reason
        if self.instant or reason == "quit":
            self.finish()

    def finish(self) -> None:
        self.visible = False
        self.session = ComposeSession()
        self.closed.emit(self._reason)

    def type(self, text: str) -> None:
        self.text += text
        self.user_text = True
        self.textEdited.emit()

    def isVisible(self) -> bool:
        return self.visible

    def is_foreground(self) -> bool:
        return world.foreground == BOX

    def is_dismissed_by(self, hwnd: int) -> bool:
        return self.session.phase == "open" and hwnd == OTHER_APP

    def has_draft(self) -> bool:
        return self.user_text and bool(self.text.strip())

    def snapshot(self) -> ComposeSnapshot:
        return ComposeSnapshot(self.text, 0, 0, 0, self.user_text)

    def restore_snapshot(self, snap: ComposeSnapshot) -> None:
        world.log("restore_snapshot")
        self.text, self.user_text = snap.text, snap.user_text

    def clear_text(self) -> None:
        self.text, self.user_text = "", False

    def set_undo_mode(self, on: bool) -> None:
        self.undo_mode = on

    def show_note(self, kind: str) -> None:
        world.log("note", kind)

    def reanchor(self, _rest: QRect) -> None:
        world.log("reanchor")

    def read_from_shortcut(self) -> None:
        world.log("read_from_shortcut")

    def on_speed(self, _v: float, _note: str = "") -> None:
        pass


class FakeHud:
    def __init__(self, *_args) -> None:
        pass

    def flash(self, _anchor, _mode, _color, label: str, *_args, **_kw) -> None:
        world.log("hud", label)

    def dismiss(self) -> None:
        pass

    def hide(self) -> None:
        world.log("hud_hide")


class Quiet:
    """Any call does nothing: the tray, tooltips and player."""

    def __init__(self, *_args, **_kw) -> None:
        pass

    def __getattr__(self, _name: str):
        return lambda *_args, **_kw: None


ONLINE_VOICE = VoiceInfo("edge:en-US-AvaNeural", "edge", "Ava", "en-US", "Female", False)
LOCAL_VOICE = VoiceInfo("windows:David", "windows", "David", "en-US", "Male", True)


class _RouterSignals(QObject):
    backendChanged = Signal(str, str)


class FakeRouter:
    """The engine's synth, as the App sees it."""

    def __init__(self, *, settings=None, parent=None, temp_root=None) -> None:
        self.settings, self.parent, self.reachable = settings, parent, []
        self._signals = _RouterSignals()
        self.backendChanged = self._signals.backendChanged

    def voices(self) -> list:
        return [ONLINE_VOICE, LOCAL_VOICE]

    def set_reachable(self, on: bool) -> None:
        self.reachable.append(on)


class FakePreview(QObject):
    def __init__(self, factory, volume, parent=None) -> None:
        super().__init__(parent)
        self.factory, self.volume, self.shut = factory, volume, False

    def shutdown(self, timeout: float = 2.0) -> None:
        world.log("preview_shutdown")
        self.shut = True


class FakeUpdates(QObject):
    quitRequested = Signal()

    def __init__(self, settings, current_version: str, *, online, busy, parent=None) -> None:
        super().__init__(parent)
        self.settings, self.current_version, self.online, self.busy = settings, current_version, online, busy
        self.started = 0

    def start(self) -> None:
        self.started += 1


class FakeNetwork(QObject):
    """QNetworkInformation with the reachability a test sets."""

    Reachability = QNetworkInformation.Reachability
    reachabilityChanged = Signal(object)
    current = None

    def __init__(self) -> None:
        super().__init__()
        self.value = QNetworkInformation.Reachability.Unknown

    def reachability(self):
        return self.value

    def set(self, value) -> None:
        self.value = value
        self.reachabilityChanged.emit(value)

    @staticmethod
    def loadDefaultBackend() -> bool:
        return True

    @classmethod
    def instance(cls):
        return cls.current


class FakeServer(QObject):
    """The Open with server: never listens on a real pipe name."""

    openRequested = Signal(str)
    openRejected = Signal(str, str)

    def __init__(self, name=None, parent=None) -> None:
        super().__init__(parent)

    def listen(self) -> bool:
        world.log("listen")
        return True

    def close(self) -> None:
        pass


class FakeLoader(QObject):
    progress = Signal(int, int, int)
    built = Signal(int, object, object)
    failed = Signal(int, str)
    scanChoice = Signal(int, int, int)
    scanProgress = Signal(int, int, int)

    def continue_scan(self, token: int, first: int, last: int) -> None:
        self.jobs.append(("scan", token, first, last))

    def scanned_in(self, token: int, first: int, last: int) -> int:
        return last - first + 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.jobs: list[tuple] = []

    def open(self, token: int, path: str, options, *, password=None) -> None:
        self.jobs.append(("open", token, path, options, password))

    def build(self, token: int, raw, options) -> None:
        self.jobs.append(("build", token, raw, options, None))

    def cancel(self, token: int) -> None:
        self.jobs.append(("cancel", token))

    def shutdown(self, timeout: float = 2.0) -> None:
        pass


class FakeExporter(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    failed = Signal(str, str)

    def __init__(self, factory, parent=None, **_kw) -> None:
        super().__init__(parent)
        self.busy, self.started, self.cancelled, self.retried, self.shut = False, [], 0, 0, False

    def start(self, doc, target: str, *, voice: str, speed: float, first: int = 0, last=None) -> None:
        self.busy = True
        self.started.append((doc, target, voice, speed, first))

    def fraction(self) -> float:
        return 0.42 if self.busy else 0.0

    def remaining_s(self):
        return None

    def cancel(self) -> None:
        self.cancelled += 1
        self.busy = False

    def retry(self) -> bool:
        self.retried += 1
        self.busy = True
        return True

    def shutdown(self, timeout: float = 2.0) -> None:
        self.shut = True


class FakeOcr(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

    def start(self) -> None:
        world.log("ocr_start")

    def shutdown(self, timeout: float = 2.0) -> None:
        world.log("ocr_shutdown")


class FakeOverlay:
    excluded = True

    def capture_excluded(self) -> bool:
        return self.excluded


class FakeScreenReader(QObject):
    """The selection and recognition, as the App drives them: begin() opens at once unless no language."""

    stateChanged = Signal(str)
    textReady = Signal(object, object)
    failed = Signal(str)

    def __init__(self, theme, settings, ocr, *, input_thread=None, build=None, highlight=None, parent=None) -> None:
        super().__init__(parent)
        self.build, self.state, self.languages = build, "idle", True
        self.overlay = FakeOverlay()

    def selecting(self) -> bool:
        return self.state == "selecting"

    def busy(self) -> bool:
        return self.state != "idle"

    def language_ready(self, *, refresh: bool = False) -> bool:
        world.log("language_ready", refresh)
        return self.languages

    def begin(self, *, return_to: int = 0) -> None:
        world.log("begin", return_to)
        self.state = "selecting"
        self.stateChanged.emit("selecting")

    def cancel(self, reason: str = "app") -> None:
        if self.state == "idle":
            return
        world.log("cancel", reason)
        self.state = "idle"
        self.stateChanged.emit("idle")
        self.failed.emit("cancelled")

    def capture(self) -> None:
        self.state = "recognizing"
        self.stateChanged.emit("recognizing")

    def finish(self, doc, text) -> None:
        self.state = "idle"
        self.stateChanged.emit("idle")
        self.textReady.emit(doc, text)

    def fail(self, kind: str) -> None:
        self.state = "idle"
        self.stateChanged.emit("idle")
        self.failed.emit(kind)


class FakeHighlight(QObject):
    pageMoved = Signal(bool)
    shown = Signal()

    def __init__(self, theme, settings, parent=None) -> None:
        super().__init__(parent)
        self.own_rects = None

    def set_own_rects(self, fn) -> None:
        self.own_rects = fn

    def show_for(self, source) -> None:
        world.log("marks_show", source)

    def set_word(self, *_args) -> None:
        pass

    def set_paused(self, on: bool) -> None:
        pass

    def hide(self, animate: bool = True) -> None:
        world.log("marks_hide", animate)

    def forget(self) -> None:
        world.log("marks_forget")

    def is_visible(self) -> bool:
        return False

    def window_id(self) -> int:
        return 0


class FakeRuler(QObject):
    toggled = Signal(bool)
    shown = Signal()

    def __init__(self, theme, settings, own_rects, parent=None) -> None:
        super().__init__(parent)
        self.settings, self.own_rects, self.enabled, self.reasons = settings, own_rects, False, set()

    def set_enabled(self, on: bool) -> None:
        world.log("ruler", on)
        self.enabled = on

    def suppress(self, reason: str, on: bool) -> None:
        (self.reasons.add if on else self.reasons.discard)(reason)

    def shutdown(self) -> None:
        pass


FAKES = {"saylight.engine.Engine": FakeEngine, "saylight.input_thread.InputThread": FakeInput,
         "saylight.foreground.ForegroundTracker": FakeForeground, "saylight.ui.panel.Panel": FakePanel,
         "saylight.ui.compose.ComposeWindow": FakeCompose, "saylight.ui.hud.Hud": FakeHud,
         "saylight.ui.tray.Tray": Quiet, "saylight.ui.tip.TipManager": Quiet, "saylight.voices.make_router": FakeRouter,
         "saylight.voices.preview.VoicePreview": FakePreview, "saylight.update_service.UpdateService": FakeUpdates,
         "saylight.player.PlayerThread": Quiet, "saylight.single_instance.OpenServer": FakeServer,
         "saylight.documents.loader.DocumentLoader": FakeLoader, "saylight.export.AudioExporter": FakeExporter,
         "saylight.ocr.engine.OcrWorker": FakeOcr, "saylight.screen_reader.ScreenReader": FakeScreenReader,
         "saylight.ui.screen_highlight.ScreenHighlight": FakeHighlight,
         "saylight.ui.reading_ruler.ReadingRuler": FakeRuler}


class AppTest(unittest.TestCase):
    """Builds the App from the fakes."""

    App = None

    @classmethod
    def setUpClass(cls) -> None:
        with contextlib.ExitStack() as stack:
            for target, fake in FAKES.items():
                stack.enter_context(mock.patch(target, fake))
            cls.App = A._build_app_class()

    def setUp(self) -> None:
        global world
        world = World()
        folder = tempfile.mkdtemp(prefix="saylight_app_test_")
        self.addCleanup(shutil.rmtree, folder, True)
        patcher = mock.patch.multiple(win32, get_foreground_window=lambda: world.foreground,
                                      set_foreground_window=world.set_foreground_window,
                                      is_app_window=lambda hwnd: hwnd in world.apps,
                                      query_user_notification_state=lambda: world.notification_state,
                                      message_beep=lambda _kind: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = self.App(QCoreApplication.instance(), 0, os.path.join(folder, "settings.json"))
        self.addCleanup(self._stop_timers)
        self.e, self.c, self.p, self.input = self.app.engine, self.app.compose, self.app.panel, self.app.input

    def _stop_timers(self) -> None:
        QCoreApplication.processEvents()  # anything this test queued runs now, not in the next test
        for timer in self.app.findChildren(QTimer):
            timer.stop()

    def load(self, state: str = "reading", doc: FakeDoc | None = None) -> FakeDoc:
        self.e.document, self.e.state = doc or FakeDoc(), state
        return self.e.document

    def focus_changes(self) -> list:
        return [e[1] for e in world.events if e[0] == "set_foreground"]


class SettingsPagesTests(AppTest):
    """What the App offers the settings pages, and how it wires the voices and the update check."""

    def test_the_pages_get_the_voices_updates_and_version(self) -> None:
        app = self.app
        self.assertEqual(app.version, saylight.__version__)
        self.assertEqual(saylight.__version__, "1.5.0")
        self.assertIsInstance(app.router, FakeRouter)
        self.assertIs(app.router.settings, app.settings)
        self.assertIs(app.router.parent, app)
        self.assertIsInstance(app.catalog, VoiceCatalog)
        self.assertIs(app.preview.factory, voices.make_backend)
        app.settings.set("volume", 35)
        self.assertEqual(app.preview.volume(), 35)
        self.assertIsInstance(app.updates, FakeUpdates)
        self.assertIs(app.updates.settings, app.settings)
        self.assertEqual(app.updates.current_version, saylight.__version__)

    def test_the_catalog_lists_the_online_voices_first_then_local_ones_and_the_online_list_once(self) -> None:
        listed = self.app.catalog.voices()
        self.assertEqual(listed[-1], LOCAL_VOICE)  # Windows' voices come after the online list
        self.assertEqual([v.id for v in listed].count(ONLINE_VOICE.id), 1)

    def test_set_voice_stores_the_full_id(self) -> None:
        self.app.set_voice("en-GB-SoniaNeural")
        self.assertEqual(self.app.settings.get("voice"), "edge:en-GB-SoniaNeural")
        self.app.set_voice("edge:en-US-AvaNeural")
        self.assertEqual(self.app.settings.get("voice"), "edge:en-US-AvaNeural")
        with self.assertRaises(ValueError):
            self.app.set_voice("")

    def test_no_update_check_during_a_reading(self) -> None:
        busy = self.app.updates.busy
        for state, expected in (("ready", False), ("finished", False), ("error_offline", False), ("copying", True),
                                ("preparing", True), ("reading", True), ("waiting", True), ("paused", True)):
            self.e.state = state
            self.assertIs(busy(), expected, state)

    def test_start_starts_the_update_check_and_follows_the_network(self) -> None:
        net = FakeNetwork()
        engine_online: list[bool] = []
        self.e.set_online = engine_online.append
        with mock.patch.object(FakeNetwork, "current", net), \
                mock.patch("PySide6.QtNetwork.QNetworkInformation", FakeNetwork):
            self.app.start()
        online = self.app.updates.online
        self.assertEqual(self.app.updates.started, 1)
        self.assertTrue(online())                         # an unknown state counts as online
        R = QNetworkInformation.Reachability
        net.set(R.Disconnected)
        self.assertFalse(online())
        net.set(R.Online)
        self.assertTrue(online())
        self.assertEqual(engine_online, [True, False, True])
        self.assertEqual(self.app.router.reachable, [True, False, True])

    def test_a_reading_that_falls_back_to_a_windows_voice_says_so(self) -> None:
        with mock.patch.object(FakeNetwork, "current", FakeNetwork()), \
                mock.patch("PySide6.QtNetwork.QNetworkInformation", FakeNetwork):
            self.app.start()
        with mock.patch.object(self.app.panel, "show_note") as note:
            self.app.router.backendChanged.emit("edge", "chosen")
            note.assert_not_called()
            self.app.router.backendChanged.emit("windows", "offline_fallback")
            note.assert_called_once_with("Offline · Windows voice", "Caution")
            self.app.router.backendChanged.emit("windows", "service_fallback")
            self.assertEqual(note.call_args.args[0], "Voice service down · Windows voice")

    def test_setup_starting_quits_the_app(self) -> None:
        quits = []
        self.app.qapp = mock.Mock(quit=lambda: quits.append(self.c.isVisible()))
        self.app.open_compose("button")
        self.app.updates.quitRequested.emit()
        self.assertEqual(quits, [False])

    def test_quitting_stops_the_voice_preview(self) -> None:
        self.app._shutdown()
        self.assertTrue(self.app.preview.shut)

    def test_start_with_windows(self) -> None:
        fake = FakeSystem()
        with fake.installed():
            self.assertFalse(self.app.start_with_windows())
            self.assertTrue(self.app.set_start_with_windows(True))
            self.assertTrue(self.app.start_with_windows())
            self.assertIn(("set_run", "Saylight", A.run_command()), fake.calls)
            self.assertTrue(self.app.set_start_with_windows(False))
            self.assertFalse(self.app.start_with_windows())
        self.assertEqual(fake.run_values, set())

    def test_a_registry_failure_is_reported_not_raised(self) -> None:
        def denied(*_args):
            raise PermissionError(5, "denied")

        with mock.patch.multiple(A, set_run_value=denied, read_run_value=denied), \
                self.assertLogs("saylight.app", "WARNING"):
            self.assertFalse(self.app.set_start_with_windows(True))
            self.assertFalse(self.app.start_with_windows())

    def test_the_licence_notices(self) -> None:
        self.assertEqual(self.app.notices_path(), os.path.join(paths.install_dir(), "THIRD_PARTY_NOTICES.md"))
        exe = os.path.join(tempfile.gettempdir(), "Saylight", "Saylight.exe")
        with mock.patch.multiple(sys, frozen=True, executable=exe, create=True):
            self.assertEqual(self.app.notices_path(), os.path.join(os.path.dirname(exe), "THIRD_PARTY_NOTICES.txt"))
        opened = []
        with mock.patch.object(A.os, "startfile", opened.append):
            self.assertTrue(self.app.open_notices())
        self.assertEqual(opened, [self.app.notices_path()])
        with mock.patch.object(A.os, "startfile", side_effect=OSError(2, "gone")), \
                self.assertLogs("saylight.app", "WARNING"):
            self.assertFalse(self.app.open_notices())


class ComposeFlowTests(AppTest):

    # --- Opening

    def test_the_read_shortcut_is_routed_before_the_box_can_be_in_front(self) -> None:
        self.app.open_compose("button")
        kinds = world.kinds()
        self.assertIn(("compose_open", True), world.events)
        self.assertLess(kinds.index("compose_open"), kinds.index("open_at"))
        world.events.clear()
        self.app.close_compose("esc")
        self.assertEqual([e for e in world.events if e[0] in ("compose_open", "close_animated")],
                         [("compose_open", False), ("close_animated", "esc")])

    def test_opening_hands_the_box_the_panel_the_reading_and_where_focus_goes_back(self) -> None:
        doc = self.load("stopped")
        self.e.cur = 1
        self.p.page, self.p.expanded = "settings", True
        with self.assertLogs("saylight.app", "INFO") as logs:
            self.app.open_compose("card")
        self.assertIn(("set_page", "reading"), world.events)
        self.assertIn(("volume_open", False), world.events)
        self.assertIn(("open_at", True, 8, (doc.text, 11), APP_WINDOW), world.events)
        self.assertEqual(self.c.session.source, "card")
        self.assertEqual(logs.records[-1].getMessage(), "Compose opened (card)")
        self.app.close_compose("esc")
        world.foreground = SHELL  # opened from the taskbar: focus goes back to the last app
        self.p.visible, self.e.document = False, None
        self.app.open_compose("tray")
        self.assertEqual(world.events[-2][0], "open_at")
        self.assertEqual(world.events[-2][1:], (False, 8, None, LAST_APP))

    def test_opening_an_open_box_just_brings_it_back(self) -> None:
        self.load("reading")
        self.app.open_compose("button")
        world.events.clear()
        world.foreground = OTHER_APP
        self.app.open_compose("tray")
        self.assertEqual(world.events, [("activate",)])
        self.assertEqual(self.c.session.source, "button")

    def test_opening_is_refused_while_copying(self) -> None:
        self.e.state = "copying"  # also the first moments, before Copying is shown
        self.app.open_compose("hotkey")
        self.assertEqual(world.events, [])
        self.assertFalse(self.app.composing())

    # --- Pause on open and the resume rule

    def test_opening_pauses_and_leaving_without_reading_resumes(self) -> None:
        for reason, resumes in (("esc", True), ("back", True), ("away", True), ("tray", True), ("hotkey", True),
                                ("screen", True), ("read", False), ("quit", False)):
            with self.subTest(reason=reason):
                doc = self.load("reading")
                self.app.open_compose("button")
                self.assertEqual(self.e.state, "paused")
                self.assertIs(self.c.session.paused_doc, doc)
                self.app.close_compose(reason)
                self.assertEqual(self.e.state, "reading" if resumes else "paused")

    def test_no_resume_once_the_reading_has_changed(self) -> None:
        self.load("reading")
        self.app.open_compose("button")
        self.e.document = FakeDoc("Another reading.")
        self.app.close_compose("esc")
        self.assertEqual(self.e.state, "paused")
        self.load("reading")
        self.app.open_compose("button")
        self.e.state = "reading"  # resumed with a media key while the box was open
        self.app.close_compose("esc")
        self.assertNotIn("resume", world.kinds())
        self.load("reading")
        self.app.open_compose("button")
        self.app.clear_box()
        self.assertIsNone(self.c.session.paused_doc)
        self.load("stopped")
        self.app.close_compose("esc")
        self.app.open_compose("button")  # nothing playing: nothing to pause
        self.assertIsNone(self.c.session.paused_doc)

    # --- Focus

    def test_focus_goes_back_only_for_its_reasons(self) -> None:
        for reason, back in (("read", True), ("esc", True), ("back", True), ("hotkey", True), ("screen", True),
                             ("away", False), ("tray", False), ("quit", False)):
            with self.subTest(reason=reason):
                world.foreground = APP_WINDOW
                self.app.open_compose("button")
                self.assertEqual(world.foreground, BOX)
                world.events.clear()
                self.app.close_compose(reason)
                self.assertEqual(self.focus_changes(), [APP_WINDOW] if back else [])

    def test_focus_goes_back_only_from_the_front(self) -> None:
        self.app.open_compose("button")
        world.foreground = OTHER_APP  # you have already gone somewhere else
        self.app.close_compose("esc")
        self.assertEqual(self.focus_changes(), [])
        self.c.grant = False  # Windows refused the box the foreground
        world.foreground = APP_WINDOW
        self.app.open_compose("button")
        self.app.close_compose("esc")
        self.assertEqual(self.focus_changes(), [])

    def test_focus_falls_back_to_the_last_app(self) -> None:
        self.app.open_compose("button")
        world.apps.discard(APP_WINDOW)  # the window you came from has closed
        self.app.close_compose("esc")
        self.assertEqual(self.focus_changes(), [LAST_APP])
        self.app.open_compose("button")  # from LAST_APP, which then closes too, and there is nowhere left
        world.apps.discard(LAST_APP)
        world.last_app = 0
        world.events.clear()
        self.app.close_compose("esc")
        self.assertEqual(self.focus_changes(), [])

    def test_restoring_the_foreground_waits_while_the_box_is_open(self) -> None:
        self.app.open_compose("button")
        self.app.restore_foreground()
        self.assertNotIn("restore_foreground", world.kinds())
        self.c.instant = False
        self.app.close_compose("tray")  # the tray's restore goes ahead during the close fade
        self.app.restore_foreground()
        self.assertIn("restore_foreground", world.kinds())

    def settle_away(self) -> None:
        """Deliver the queued activation and let light dismiss's settle time run out."""
        QCoreApplication.processEvents()
        timer = self.app._away_timer
        self.assertEqual(timer.interval(), A.AWAY_SETTLE_MS)
        if timer.isActive():
            timer.stop()
            timer.timeout.emit()

    def test_clicking_into_another_app_closes_the_box_and_leaves_you_there(self) -> None:
        self.app.open_compose("button")
        world.foreground = SHELL
        self.app.foreground.appActivated.emit(SHELL)
        self.settle_away()
        self.assertTrue(self.app.composing())
        world.foreground = OTHER_APP
        self.app.foreground.appActivated.emit(OTHER_APP)
        self.assertTrue(self.app.composing())  # queued: never closed inside the WinEvent callback
        self.settle_away()
        self.assertIn(("close_animated", "away"), world.events)
        self.assertEqual(self.focus_changes(), [])

    def test_a_window_that_passes_the_foreground_straight_back_leaves_the_box_open(self) -> None:
        # Opening the emoji panel (Win+.) can name another window in a foreground event for a moment.
        self.app.open_compose("button")
        world.foreground = OTHER_APP
        self.app.foreground.appActivated.emit(OTHER_APP)
        world.foreground = BOX  # back already, before light dismiss judges it
        self.settle_away()
        self.assertTrue(self.app.composing())
        self.assertNotIn(("close_animated", "away"), world.events)

    # --- HUD

    def test_the_hud_is_quiet_while_the_box_is_shown(self) -> None:
        self.p.visible = False
        self.load("reading")
        self.app.open_compose("hotkey")  # its pause makes no HUD
        self.e.stateChanged.emit("preparing", {})
        self.e.notice.emit("reading_started")
        self.c.instant = False
        self.app.close_compose("read")
        self.e.notice.emit("resumed")  # during the close fade
        self.assertNotIn("hud", world.kinds())
        self.c.finish()
        self.assertEqual([e for e in world.events if e[0] == "hud"], [("hud", "Preparing audio…")])

    def test_no_hud_after_closing_over_the_shown_panel(self) -> None:
        self.app.open_compose("button")
        self.e.stateChanged.emit("preparing", {})
        self.app.close_compose("read")
        self.assertNotIn("hud", world.kinds())

    # --- The panel under the box

    def test_toggling_the_panel_closes_the_box_and_leaves_the_panel(self) -> None:
        self.app.open_compose("button")
        self.app.toggle_panel(from_tray=True)
        self.assertIn(("close_animated", "tray"), world.events)
        self.app.open_compose("button")
        self.input.hotkeyPressed.emit(HK_PANEL)
        self.assertIn(("close_animated", "hotkey"), world.events)
        self.assertNotIn("panel_hide", world.kinds())
        self.assertTrue(self.p.visible)

    def test_an_empty_read_press_from_the_shell_is_ignored_while_open(self) -> None:
        self.app.open_compose("button")
        self.e.panelToggleRequested.emit()
        self.assertTrue(self.app.composing())
        self.assertNotIn("panel_hide", world.kinds())
        self.app.close_compose("esc")
        self.e.panelToggleRequested.emit()
        self.assertIn("panel_hide", world.kinds())

    def test_other_tray_items_close_the_box_first(self) -> None:
        for action, then in ((self.app.open_settings, "set_page"), (self.app.reset_position, "reset_position"),
                             (lambda: self.app.set_panel_visible(True), "panel_show")):
            with self.subTest(then=then):
                self.p.visible = False
                self.app.open_compose("tray")
                world.events.clear()
                action()
                kinds = world.kinds()
                self.assertEqual(world.events[kinds.index("close_animated")], ("close_animated", "tray"))
                self.assertLess(kinds.index("close_animated"), kinds.index(then))

    def test_the_full_screen_check_waits_while_the_box_is_shown(self) -> None:
        self.app.open_compose("button")
        world.notification_state = QUNS_RUNNING_D3D_FULL_SCREEN
        self.app._check_fullscreen()
        self.assertNotIn("panel_hide", world.kinds())
        self.app.close_compose("esc")
        self.app._check_fullscreen()
        self.assertIn("panel_hide", world.kinds())

    def test_screen_changes_move_the_open_box(self) -> None:
        self.app._reanchor_compose()
        self.app.open_compose("button")
        self.app._reanchor_compose()
        self.assertEqual(world.kinds().count("reanchor"), 1)

    # --- Reading from the box

    def test_reading_from_the_box_closes_it_and_logs_only_the_count(self) -> None:
        self.app.open_compose("button")
        with self.assertLogs("saylight.app", "INFO") as logs:
            self.c.readRequested.emit("Read this part.", "selection")
        self.assertIn(("read_text", "Read this part."), world.events)
        self.assertIn(("close_animated", "read"), world.events)
        self.assertEqual(self.focus_changes(), [APP_WINDOW])
        self.assertEqual([r.getMessage() for r in logs.records], ["Compose: read 15 characters (selection)"])

    def test_nothing_to_read_keeps_the_box_open(self) -> None:
        self.e.speakable = False
        self.app.open_compose("button")
        self.c.readRequested.emit("((( )))", "all")
        self.assertIn(("note", "nothing"), world.events)
        self.assertTrue(self.app.composing())

    def test_the_read_shortcut_in_the_box_reads_from_it(self) -> None:
        self.input.composeReadPressed.emit()  # a press that arrives after the box has closed
        self.assertNotIn("read_from_shortcut", world.kinds())
        self.app.open_compose("button")
        self.input.composeReadPressed.emit()
        self.assertIn(("read_from_shortcut",), world.events)

    def test_the_type_or_paste_shortcut_opens_and_closes_the_box(self) -> None:
        a = self.app
        self.assertEqual(a._hotkey_map()[HK_COMPOSE], "")
        a.settings.set("hotkeys.compose", "Ctrl+Alt+Shift+F23")
        self.assertEqual(self.input.hotkeys[HK_COMPOSE], "Ctrl+Alt+Shift+F23")
        self.assertEqual(a.compose_shortcut(), "")  # not registered yet
        self.input.registered.emit(HK_COMPOSE, True)
        self.assertEqual(a.compose_shortcut(), "Ctrl+Alt+Shift+F23")
        self.input.hotkeyPressed.emit(HK_COMPOSE)
        self.assertTrue(a.composing())
        self.assertEqual(self.c.session.source, "hotkey")
        self.input.hotkeyPressed.emit(HK_COMPOSE)
        self.assertIn(("close_animated", "hotkey"), world.events)
        self.assertEqual(self.focus_changes(), [APP_WINDOW])
        a.settings.set("hotkeys.compose", "")
        self.assertEqual(a.compose_shortcut(), "")

    def test_the_box_signals_reach_the_app(self) -> None:
        self.app.open_compose("button")
        self.c.sizeStepRequested.emit(-1)
        self.assertIn(("text_size", -1), world.events)
        self.load("stopped")
        self.c.clearRequested.emit()
        self.assertTrue(self.app.can_undo_clear())
        self.c.undoRequested.emit()
        self.assertFalse(self.app.can_undo_clear())
        self.c.closeRequested.emit("back")
        self.assertIn(("close_animated", "back"), world.events)

    # --- Clear and Undo

    def test_undo_lasts_ten_seconds_and_waits_for_the_pointer(self) -> None:
        self.load("stopped")
        self.app.clear_box()
        timer = self.app._undo_timer
        self.assertTrue(timer.isActive())
        self.assertEqual(timer.interval(), 10_000)
        self.assertTrue(self.app.can_undo_clear())
        self.assertTrue(self.p.cleared)
        self.assertTrue(self.c.undo_mode)
        self.p.view.pointer = True  # over the card when the time is up: it waits
        self.app._on_undo_timeout()
        self.assertTrue(self.app.can_undo_clear())
        self.assertTrue(timer.isActive())
        self.p.view.pointer = False
        self.app._on_undo_timeout()
        self.assertFalse(self.app.can_undo_clear())
        self.assertFalse(timer.isActive())
        self.assertFalse(self.p.cleared)
        self.assertFalse(self.c.undo_mode)

    def test_a_new_reading_or_typing_ends_the_undo_window(self) -> None:
        self.load("stopped")
        self.app.clear_box()
        self.e.read_text("Something new.")
        self.assertFalse(self.app.can_undo_clear())
        self.c.text, self.c.user_text = "Draft", True
        self.app.clear_box()
        self.assertTrue(self.app.can_undo_clear())
        self.c.type("x")
        self.assertFalse(self.app.can_undo_clear())
        self.assertFalse(self.p.cleared)

    def test_undo_brings_back_the_reading_and_the_typed_text(self) -> None:
        doc = self.load("paused")
        self.c.text, self.c.user_text = "My draft", True
        with self.assertLogs("saylight.app", "INFO") as logs:
            self.app.clear_box()
        self.assertEqual(logs.records[-1].getMessage(), "Cleared (2 sentences, typed text: yes)")
        self.assertEqual((self.e.document, self.c.text), (None, ""))
        self.app.undo_clear()
        self.assertIs(self.e.document, doc)
        self.assertEqual(self.e.state, "stopped")
        self.assertEqual((self.c.text, self.c.user_text), ("My draft", True))
        self.assertFalse(self.app.can_undo_clear())
        self.assertFalse(self.c.undo_mode)
        self.assertEqual(world.events[-2:], [("restore_snapshot",), ("refresh_box_actions",)])  # with the draft back
        self.app.undo_clear()  # nothing left to undo
        self.assertEqual(world.kinds().count("restore"), 1)

    def test_clear_and_undo_with_only_typed_text(self) -> None:
        self.c.text, self.c.user_text = "Typed only", True
        self.assertTrue(self.app.has_draft())
        with self.assertLogs("saylight.app", "INFO") as logs:
            self.app.clear_box()
            self.assertEqual(self.c.text, "")
            self.assertTrue(self.app.can_undo_clear())
            self.assertTrue(self.p.cleared)
            self.app.undo_clear()
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["Cleared (0 sentences, typed text: yes)", "Clear undone"])
        self.assertEqual(self.c.text, "Typed only")
        self.assertNotIn("restore", world.kinds())

    def test_clear_with_nothing_to_clear_offers_no_undo(self) -> None:
        self.app.clear_box()
        self.assertFalse(self.app.can_undo_clear())
        self.assertFalse(self.p.cleared)

    def test_a_refused_undo_keeps_the_window(self) -> None:
        self.load("stopped")
        self.app.clear_box()
        self.e.state = "copying"
        self.app.undo_clear()
        self.assertTrue(self.app.can_undo_clear())
        self.assertIsNone(self.e.document)

    def test_typing_updates_the_panel_buttons(self) -> None:
        self.app.open_compose("button")
        world.events.clear()
        self.c.type("a")
        self.assertIn(("refresh_box_actions",), world.events)
        self.assertTrue(self.app.has_draft())

    def test_quitting_closes_the_box_at_once(self) -> None:
        self.load("reading")
        self.c.instant = False
        self.app.open_compose("button")
        self.app._shutdown()
        self.assertIn(("close_animated", "quit"), world.events)
        self.assertFalse(self.c.isVisible())
        self.assertEqual(self.e.state, "paused")  # no resume on the way out
        self.assertEqual(self.focus_changes(), [])

    def test_quit_closes_an_open_box_before_qt_is_asked_to_quit(self) -> None:
        # Qt closes every visible window on quit, and the open box refuses its close (Alt+F4 is Back), so a quit
        # asked for while it is open (the tray, --quit, --install, --uninstall) would be dropped.
        visible_at_quit = []
        self.app.qapp = mock.Mock(quit=lambda: visible_at_quit.append(self.c.isVisible()))
        self.c.instant = False  # the real box fades out
        self.app.open_compose("button")
        self.app.quit()
        self.assertEqual(visible_at_quit, [False])
        self.assertIn(("close_animated", "quit"), world.events)

    # --- Privacy

    def purge(self) -> None:
        """Ten minutes at rest: the engine forgets the reading and says so."""
        self.e.document, self.e.state = None, "ready"
        self.e.sessionChanged.emit(None)

    def test_the_privacy_purge_also_forgets_an_untouched_prefill(self) -> None:
        doc = self.load("stopped")
        self.app.open_compose("button")
        self.c.text, self.c.user_text = doc.text, False  # the box shows the reading, left as it was
        self.app.close_compose("esc")
        self.purge()
        self.assertEqual(self.c.text, "")  # the reading's text must not outlive the purge in the hidden box

    def test_closing_the_box_forgets_an_untouched_prefill(self) -> None:
        # It is only a copy of the engine's reading; the next open loads whatever the engine holds then.
        doc = self.load("stopped", FakeDoc("Private email text. Second one."))
        self.app.open_compose("button")
        self.c.text, self.c.user_text = doc.text, False
        self.app.close_compose("esc")
        self.assertEqual(self.c.text, "")
        self.app.open_compose("button")
        self.assertTrue(self.app.composing())

    def test_a_newer_reading_forgets_an_untouched_prefill(self) -> None:
        doc = self.load("stopped", FakeDoc("Private email text. Second one."))
        self.app.open_compose("button")
        self.c.instant = False  # the real box is still fading out when the new reading starts
        self.c.text, self.c.user_text = doc.text, False
        self.app.close_compose("esc")
        self.e.read_text("Something else entirely.")
        self.c.finish()
        self.assertNotEqual(self.c.text, doc.text)

    def test_reading_part_of_a_prefill_forgets_the_rest(self) -> None:
        doc = self.load("stopped", FakeDoc("Private email text. Second one."))
        self.app.open_compose("button")
        self.c.text, self.c.user_text = doc.text, False
        self.c.readRequested.emit("Second one.", "selection")
        self.assertFalse(self.app.composing())
        self.assertEqual(self.e.document.text, "Second one.")
        self.assertNotEqual(self.c.text, doc.text)

    def test_a_purge_while_the_box_is_open_is_caught_up_on_close(self) -> None:
        doc = self.load("stopped")
        self.app.open_compose("button")
        self.c.text, self.c.user_text = doc.text, False
        self.purge()
        self.assertEqual(self.c.text, doc.text)  # still on screen while you look at it
        self.app.close_compose("esc")
        self.assertEqual(self.c.text, "")

    def test_the_privacy_purge_keeps_a_draft(self) -> None:
        self.load("stopped")
        self.app.open_compose("button")
        self.c.text, self.c.user_text = "Mine", True
        self.app.close_compose("esc")
        self.purge()
        self.assertEqual(self.c.text, "Mine")

    def test_a_cleared_prefill_still_comes_back_with_undo(self) -> None:
        doc = self.load("stopped")
        self.c.text, self.c.user_text = doc.text, False  # left in the hidden box by an earlier open
        self.app.clear_box()
        self.assertEqual(self.c.text, "")
        self.app.undo_clear()
        self.assertIs(self.e.document, doc)  # the next open loads the reading into the box again



# --- The App: files, Open with, passwords, section chips, resume and Save as audio ------------------

class FakeSheet(QObject):
    """A sheet (Pronunciations, Save as audio, the PDF password): opens and closes at once."""

    closeRequested = Signal(str)
    closed = Signal(str)
    submitted = Signal(str)
    saved = Signal(str)
    saveChosen = Signal(str, int)

    def __init__(self, *_args, **_kw) -> None:
        super().__init__()
        self.session = SheetSession()
        self.setup = None
        self.prepared: list = []

    @property
    def is_open(self) -> bool:
        return self.session.phase in ("opening", "open")

    def open_at(self, rest, *, return_to: int, panel_visible: bool = True, source: str = "") -> bool:
        world.log("sheet_open", type(self).__name__, source, return_to)
        self.session.phase, self.session.return_to = "open", return_to
        world.foreground = SHEET
        return True

    def close_animated(self, reason: str) -> None:
        world.log("sheet_close", reason)
        self.session = SheetSession()
        self.closed.emit(reason)

    def is_foreground(self) -> bool:
        return world.foreground == SHEET

    def is_dismissed_by(self, hwnd: int) -> bool:
        return self.is_open and hwnd == OTHER_APP

    def reanchor(self, _rest) -> None:
        pass

    def prepare(self, what) -> None:
        self.setup = what
        self.prepared.append(what)

    def wrong_password(self) -> None:
        world.log("wrong_password")

    def show_list(self) -> None:
        world.log("show_list")

    def show_word(self, word: str, context=None) -> None:
        world.log("show_word", word, context)


class PasswordSheetFake(FakeSheet):
    pass


class SaveSheetFake(FakeSheet):
    pass


class PronSheetFake(FakeSheet):
    pass


SHEET = 9191


def loaded(text: str = "x" * 10, pages: int = 0, scanned: tuple = ()):
    from saylight.documents.model import LoadedDocument
    return LoadedDocument("pdf", "Secret Title", text, pages=pages, scanned_pages=list(scanned))


class FileFlowTests(AppTest):
    def setUp(self) -> None:
        super().setUp()
        self.files = tempfile.mkdtemp(prefix="saylight_app_files_")
        self.addCleanup(shutil.rmtree, self.files, True)
        for target, fake in (("saylight.ui.password_sheet.PasswordSheet", PasswordSheetFake),
                             ("saylight.ui.export_dialog.ExportDialog", SaveSheetFake),
                             ("saylight.ui.pronunciation_dialog.PronunciationDialog", PronSheetFake)):
            patcher = mock.patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.loader = self.app.loader

    def make(self, name: str) -> str:
        path = os.path.join(self.files, name)
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4")
        return path

    def test_opening_a_file(self) -> None:
        path = self.make("Secret Title.pdf")
        with self.assertLogs("saylight.app", "INFO") as logs:
            self.app.open_path(path, "drop")
        self.assertIn(("begin_opening", "Secret Title", "file"), world.events)
        kind, token, opened, options, password = self.loader.jobs[0]
        self.assertEqual((kind, token, opened, password), ("open", 1, path, None))
        self.assertTrue(options.skip_citations)
        self.assertEqual(self.p.notice_ext, ".pdf")
        self.assertNotIn("Secret", " ".join(logs.output))
        self.assertNotIn(self.files, " ".join(logs.output))
        self.assertNotIn("panel_show", world.kinds())  # never shows the panel
        self.assertNotIn("set_foreground", world.kinds())

    def test_what_cannot_be_opened_is_a_notice(self) -> None:
        self.app.open_path(os.path.join(self.files, "old.doc"), "open with")
        self.app.open_path(os.path.join(self.files, "gone.pdf"), "continue")
        self.assertEqual([e for e in world.events if e[0] == "open_notice"],
                         [("open_notice", "unsupported", "old"), ("open_notice", "missing", "gone")])
        self.assertEqual(self.loader.jobs, [])

    def test_the_loaders_answers_reach_the_engine(self) -> None:
        self.app.open_path(self.make("Secret Title.pdf"))
        raw = loaded("Some text. More text.", pages=18, scanned=(3, 4))
        doc = self.app.build_document(raw)
        self.loader.progress.emit(1, 4, 18)
        with self.assertLogs("saylight.app", "INFO") as logs:
            self.loader.built.emit(1, doc, raw)
        self.assertIn(("opening_progress", 1, 4, 18), world.events)
        self.assertIn(("read_document", 1), world.events)
        line = next(m for m in logs.output if "Open: pdf" in m)
        self.assertIn("18 pages", line)
        self.assertNotIn("Secret", line)
        self.assertEqual(self.p.notes, [("Scanned pages skipped", "Caution")])

    def test_a_failure_becomes_the_engines_notice(self) -> None:
        self.app.open_path(self.make("a.pdf"))
        self.loader.failed.emit(1, "corrupt")
        self.assertIn(("opening_failed", 1, "corrupt"), world.events)

    def test_a_password_pdf(self) -> None:
        path = self.make("Locked.pdf")
        self.app.open_path(path)
        self.loader.failed.emit(1, "password")
        self.assertIn(("opening_failed", 1, "password"), world.events)
        world.foreground = OTHER_APP
        self.app.open_password("card")
        sheet = self.app._password_sheet
        self.assertEqual(sheet.prepared, ["Locked"])
        self.assertIn(("sheet_open", "PasswordSheetFake", "card", OTHER_APP), world.events)
        sheet.submitted.emit("wrong")
        job = self.loader.jobs[-1]
        self.assertEqual((job[0], job[1], job[2], job[4]), ("open", 2, path, "wrong"))
        self.loader.failed.emit(2, "password")
        self.assertIn(("wrong_password",), world.events)
        self.assertTrue(sheet.is_open)  # the sheet stays for another try
        sheet.submitted.emit("right")
        self.loader.progress.emit(3, 1, 9)
        self.assertFalse(sheet.is_open)  # it worked: the sheet goes and the foreground goes back
        self.assertIn(("sheet_close", "read"), world.events)
        self.assertEqual(world.foreground, OTHER_APP)

    def test_the_password_sheet_needs_a_password_notice(self) -> None:
        self.app.open_password("tray")
        self.assertIsNone(self.app._password_sheet)

    def test_section_chips_are_for_this_file_only(self) -> None:
        raw = loaded("Body text here. And more.")
        self.e.raw, self.e.document = raw, self.app.build_document(raw)
        self.assertFalse(self.app.build_options(raw).read_asides)
        self.app.toggle_section("notes")
        self.assertIn(("rebuild", "notes"), world.events)
        self.assertTrue(self.app.build_options(raw).read_asides)
        self.assertFalse(self.app.build_options(loaded()).read_asides)  # another file
        self.assertFalse(self.app.build_options("typed text").read_asides)
        self.app.toggle_section("notes")
        self.assertIn(("rebuild", None), world.events)
        self.assertFalse(self.app.build_options(raw).read_asides)
        self.app.toggle_section("refs")
        self.e.raw = loaded()  # a new file
        self.e.sessionChanged.emit(None)
        self.assertEqual(self.app._overrides, {})

    def test_the_builder_builds_with_the_options_and_long_text_on_the_worker(self) -> None:
        builder = self.e.builder
        self.assertEqual(len(builder("Hello there. Bye now.").sentences), 2)
        builder.start(7, "long text")
        self.assertEqual(self.loader.jobs[-1][:3], ("build", 7, "long text"))
        self.e.openingCancelled.emit(7)
        self.assertEqual(self.loader.jobs[-1], ("cancel", 7))

    def test_open_with_reaches_open_path(self) -> None:
        path = self.make("b.pdf")
        self.app.server.openRequested.emit(path)
        self.assertEqual(self.loader.jobs[0][2], path)
        self.app.server.openRejected.emit("unsupported", os.path.join(self.files, "x.doc"))
        self.assertIn(("open_notice", "unsupported", "x"), world.events)
        self.assertEqual(self.p.notice_ext, ".doc")

    def test_turning_resume_off_forgets_every_place_at_once(self) -> None:
        from saylight.resume import ResumePoint
        path = self.make("Smith 2024.pdf")
        self.app.set_resume(True)
        self.app.resume.remember(ResumePoint(path, 1, 1, "f" * 64, 0, 5, 1.0, 14))
        self.app._refresh_continue()
        for _ in range(200):
            QCoreApplication.processEvents()
            if self.app.continue_point():
                break
        self.assertEqual(self.app.continue_point(), ("Smith 2024", 14))
        with self.assertLogs("saylight.app", "INFO") as logs:
            self.assertEqual(self.app.set_resume(False), 1)
        self.assertIn("Resume: off, forgot 1", " ".join(logs.output))
        self.assertFalse(os.path.exists(self.app.resume.path))
        self.assertIsNone(self.app.continue_point())
        self.assertFalse(self.app.settings.get("resume.enabled"))

    def test_continue_opens_the_file(self) -> None:
        from saylight.resume import ResumePoint
        path = self.make("c.pdf")
        self.app.settings.set("resume.enabled", True)
        self.app._continue = ResumePoint(path, 1, 1, "f" * 64, 0, 5, 1.0, 0)
        self.app.continue_reading("card")
        self.assertEqual(self.loader.jobs[0][2], path)

    def test_save_as_audio(self) -> None:
        from saylight.text_model import Document
        doc = Document.from_raw("One sentence here. Two sentences there.")
        self.e.document, self.e.state = doc, "stopped"
        self.app.open_save("flyout")
        sheet = self.app._save_sheet
        setup = sheet.prepared[0]
        self.assertFalse(setup.from_file)
        self.assertIn(("sheet_open", "SaveSheetFake", "flyout", APP_WINDOW), world.events)
        target = os.path.join(self.files, "out.mp3")
        sheet.saveChosen.emit(target, 0)
        self.assertFalse(sheet.is_open)
        ex = self.app.exporter
        self.assertEqual(ex.started[0][1], target)
        self.assertTrue(self.app._busy())  # no update check while saving
        self.assertEqual(self.app.flyout_state()["saving"], 0.42)
        self.assertEqual(self.p.notes[-1], ("Saving audio", "TextSecondary"))
        ex.busy = False
        ex.failed.emit("offline", "OSError")
        self.assertTrue(self.p.failed_badge)
        self.assertEqual(self.app.flyout_state()["failed"], "No internet connection")
        self.assertEqual(self.p.notes[-1], ("Couldn't save the audio", "Critical"))
        self.app.retry_save()
        self.assertEqual((ex.retried, self.app.flyout_state()["failed"]), (1, ""))
        self.app.cancel_save()
        self.assertEqual(ex.cancelled, 1)
        self.assertEqual(self.p.notes[-1], ("Saving cancelled · no file kept", "TextSecondary"))
        ex.finished.emit(target)
        self.assertEqual(self.app.saved_audio(), target)
        self.app._forget_save()
        self.assertEqual(self.app.saved_audio(), "")

    def test_quitting_cancels_a_save(self) -> None:
        self.app._shutdown()
        self.assertTrue(self.app.exporter.shut)

    def test_pronunciations_pause_and_saving_resumes(self) -> None:
        self.load("reading")
        self.app.open_pronunciations("flyout")
        self.assertEqual(self.e.state, "paused")
        sheet = self.app._pron_sheet
        self.assertIn(("show_list",), world.events)
        sheet.closeRequested.emit("saved")
        self.assertEqual(self.e.state, "reading")
        self.assertEqual(world.foreground, APP_WINDOW)

    def test_one_focusable_window_at_a_time(self) -> None:
        self.load("stopped")
        self.app.open_compose("button")
        self.app.open_pronunciations("tray")
        self.assertFalse(self.app.composing())
        self.assertIn(("close_animated", "switch"), world.events)
        self.app.open_compose("button")
        self.assertFalse(self.app._pron_sheet.is_open)

    def test_hud_for_an_open_notice_while_hidden(self) -> None:
        self.p.visible = False
        self.app.settings.set("hud_when_hidden", True)
        self.app._on_state("open_failed", {"label": "Password needed", "kind": "password"})
        self.e.notice.emit("open_failed")
        self.assertIn(("hud", "Password needed"), world.events)


class ScreenFlowTests(AppTest):
    """Read from screen as the App runs it: the pause, the cover, the overlay, and how it ends."""

    def setUp(self) -> None:
        super().setUp()
        self.sr = self.app.screen_reader

    def test_the_button_pauses_covers_and_opens_the_overlay(self) -> None:
        doc = self.load("reading")
        self.app.read_from_screen("button")
        kinds = world.kinds()
        self.assertLess(kinds.index("pause"), kinds.index("begin"))
        self.assertIn(("covered", True), world.events)
        self.assertIn("hud_hide", kinds)
        self.assertIn(("marks_hide", False), world.events)
        self.assertEqual(self.app.ruler.reasons, {"overlay"})
        self.assertTrue(self.sr.selecting())
        self.assertIs(self.app._screen_paused, doc)
        self.assertTrue(self.app._busy())  # no update check while choosing

    def test_cancelling_the_overlay_resumes_the_reading_it_paused(self) -> None:
        self.load("reading")
        self.app.read_from_screen("hotkey")
        self.input.hotkeyPressed.emit(HK_READ_SCREEN)  # the shortcut again: cancel
        self.assertIn(("cancel", "hotkey"), world.events)
        self.assertEqual(self.e.state, "reading")
        self.assertIn(("covered", False), world.events)
        self.assertEqual(self.app.ruler.reasons, set())
        self.assertNotIn("ocr_notice", world.kinds())

    def test_no_language_shows_the_notice_without_the_overlay(self) -> None:
        self.load("reading")
        self.sr.languages = False
        self.app.read_from_screen("retry")
        self.assertIn(("language_ready", True), world.events)  # Try again asks Windows afresh
        self.assertIn(("ocr_notice", "screen_language", ""), world.events)
        self.assertNotIn("begin", world.kinds())
        self.assertNotIn("pause", world.kinds())

    def test_refused_while_copying_or_opening(self) -> None:
        for state in ("copying", "opening"):
            with self.subTest(state=state):
                self.e.state = state
                with self.assertLogs("saylight.app", "INFO"):
                    self.app.read_from_screen("button")
                self.assertNotIn("begin", world.kinds())

    def test_the_capture_opens_finding_the_text_and_the_result_is_read(self) -> None:
        self.load("paused")
        self.p.page = "settings"
        self.app.read_from_screen("card")
        self.sr.capture()
        self.assertIn(("begin_opening", "", "screen"), world.events)
        self.assertIn(("covered", False), world.events)
        doc, text = FakeDoc("Found it. Read it."), types.SimpleNamespace(text="Found it. Read it.")
        self.sr.finish(doc, text)
        self.assertIn(("read_document", 1), world.events)
        self.assertIs(self.e.document, doc)
        self.assertEqual(self.p.page, "reading")  # the new reading shows on the reading page

    def test_stop_while_finding_the_text_cancels_and_keeps_the_pause(self) -> None:
        self.load("reading")
        self.app.read_from_screen("button")
        self.sr.capture()
        token = self.app._screen_token
        self.e.state = "paused"  # Stop: the engine goes back to the state under Opening
        self.e.openingCancelled.emit(token)
        self.assertIn(("cancel", "stop"), world.events)
        self.assertEqual(self.e.state, "paused")
        self.assertNotIn("resume", world.kinds())

    def test_failures_show_a_notice_and_never_resume(self) -> None:
        self.load("reading")
        self.app.read_from_screen("button")
        self.sr.capture()
        self.sr.fail("nothing_speakable")
        self.assertIn(("ocr_notice", "screen_nothing", "nothing_speakable"), world.events)
        self.assertNotIn("resume", world.kinds())

    def test_the_box_hands_its_pause_over(self) -> None:
        doc = self.load("reading")
        self.app.open_compose("button")
        self.app.read_from_screen("hotkey")
        self.assertIn(("close_animated", "screen_read"), world.events)
        self.assertEqual(self.e.state, "paused")  # never resumed by the box closing
        self.assertIs(self.app._screen_paused, doc)

    def test_the_ruler_and_its_shortcut(self) -> None:
        self.input.hotkeyPressed.emit(HK_RULER)
        self.assertEqual(self.app.ruler.enabled, True)
        self.app.toggle_ruler(False)
        self.assertEqual(self.app.ruler.enabled, False)

    def test_the_optional_shortcuts_are_registered_from_the_settings(self) -> None:
        hotkeys = self.input.hotkeys
        self.assertEqual((hotkeys[HK_READ_SCREEN], hotkeys[HK_RULER]), ("Ctrl+Alt+`", ""))
        self.assertEqual(self.app.screen_shortcut(), "")  # until Windows says it registered
        self.input.registered.emit(HK_READ_SCREEN, True)
        self.assertEqual(self.app.screen_shortcut(), "Ctrl+Alt+`")
        self.input.registered.emit(HK_READ_SCREEN, False)
        self.assertTrue(self.app.shortcut_failed("read_screen"))
        self.app.settings.set("hotkeys.ruler", "Ctrl+Alt+Shift+R")
        self.assertEqual(self.input.hotkeys[HK_RULER], "Ctrl+Alt+Shift+R")

    def test_own_rects_lists_the_visible_windows(self) -> None:
        self.assertEqual(self.app.own_rects(), [])  # the fakes have no windows

    def test_no_hud_while_choosing_an_area(self) -> None:
        self.load("reading")
        self.p.visible = False  # the panel hidden, Status pop-ups while hidden on (the default)
        self.app.read_from_screen("hotkey")
        start = len(world.events)
        self.e.resume()  # the media Play/Pause key keeps its meaning while the overlay is open
        flashes = [e for e in world.events[start:] if e[0] == "hud"]
        self.assertEqual(flashes, [], "the HUD would show over the overlay, and a drag over it would read it")

    def test_marks_wait_while_choosing_and_come_back_after_a_cancel(self) -> None:
        screen = object()
        self.load("paused", FakeDoc("Read from the screen."))
        self.e.document.source = types.SimpleNamespace(kind="screen", screen=screen)
        self.app.read_from_screen("button")
        self.e.state = "reading"
        self.e.stateChanged.emit("reading", {})  # the media Play/Pause key, while the overlay is open
        self.assertNotIn("marks_show", world.kinds())
        self.sr.cancel("esc")
        self.assertIn(("marks_show", screen), world.events)

    def test_no_reading_is_kept_after_the_privacy_purge(self) -> None:
        def held(*docs) -> list[str]:
            return [name for name, value in vars(self.app).items() if any(value is d for d in docs)]
        before = self.load("reading")
        self.app.read_from_screen("button")
        self.sr.capture()
        doc = FakeDoc("Words found on the screen. More of them.")
        self.sr.finish(doc, types.SimpleNamespace(text=doc.text))
        self.assertIs(self.e.document, doc)
        self.app.highlight.pageMoved.emit(True)  # "Page moved" said once for this reading
        # the purge after 10 minutes at rest (or Clear once Undo has gone): the engine forgets the text
        self.e.document = self.e.raw = None
        self.e.state = "ready"
        self.e.sessionChanged.emit(None)
        self.assertEqual(held(doc, before), [], "OCR text and the earlier reading would outlive the purge")

    def test_quitting_while_choosing_an_area_never_resumes(self) -> None:
        self.load("reading")
        self.app.read_from_screen("button")
        self.app._shutdown()
        self.assertIn(("cancel", "app"), world.events)
        self.assertNotIn("resume", world.kinds())

    def test_the_overlay_is_not_taken_for_a_full_screen_app(self) -> None:
        # Windows says busy (a full-screen app) while the overlay covers the screen, and a moment after
        self.load("reading")
        self.app.read_from_screen("button")
        world.notification_state = QUNS_BUSY
        self.app._check_fullscreen()
        self.sr.cancel("esc")
        self.app._check_fullscreen()
        self.assertNotIn("panel_hide", world.kinds())
        self.app._check_fullscreen()  # still busy after that: a real full-screen app
        self.assertIn("panel_hide", world.kinds())


class ScanFlowTests(AppTest):
    """Scanned PDFs: the OCR phase, the page choice, the window and the notes."""

    make = FileFlowTests.make

    def setUp(self) -> None:
        super().setUp()
        self.files = tempfile.mkdtemp(prefix="saylight_app_files_")
        self.addCleanup(shutil.rmtree, self.files, True)
        self.loader = self.app.loader

    def open_scan(self) -> int:
        self.app.open_path(self.make("Scan.pdf"), "drop")
        return self.loader.jobs[-1][1]

    def test_the_ocr_phase_reaches_the_engine(self) -> None:
        token = self.open_scan()
        self.loader.scanProgress.emit(token, 3, 12)
        self.assertIn(("opening_progress", token, 3, 12, "ocr"), world.events)

    def test_a_long_scan_waits_for_a_choice_of_pages(self) -> None:
        token = self.open_scan()
        self.loader.scanChoice.emit(token, 160, 160)
        self.assertIn(("opening_choice", token, 160, 160, 128), world.events)
        self.app.continue_scan(120, 160)
        self.assertEqual(self.loader.jobs[-1], ("scan", token, 120, 160))
        self.assertIn(("opening_progress", token, 0, 41, "ocr"), world.events)

    def test_stop_drops_the_choice(self) -> None:
        token = self.open_scan()
        self.loader.scanChoice.emit(token, 160, 160)
        self.e.openingCancelled.emit(token)
        self.assertIsNone(self.app._scan)
        self.app.continue_scan(1, 50)
        self.assertNotIn("scan", [j[0] for j in self.loader.jobs])

    def test_a_low_resolution_scan_says_so_once_it_reads(self) -> None:
        token = self.open_scan()
        raw = loaded("Text found. More text.", pages=2)
        raw.scan_dpi = 120
        self.loader.built.emit(token, FakeDoc(), raw)
        self.assertEqual(self.p.notes, [("Some words may be wrong in this scan", "Caution")])

    def test_try_again_opens_the_same_file(self) -> None:
        self.open_scan()
        jobs = len(self.loader.jobs)
        self.app.retry_scan()
        self.assertEqual(self.loader.jobs[jobs][0], "open")


class CommandLineTests(unittest.TestCase):
    def test_a_path_opens_in_the_running_copy_or_a_new_one(self) -> None:
        got = []
        with mock.patch.object(A, "run_safely", lambda path="": got.append(path) or 0), \
                mock.patch.object(sys, "argv", ["Saylight.exe", "paper.pdf"]), self.assertRaises(SystemExit):
            A.main()
        self.assertEqual(got, [os.path.abspath("paper.pdf")])

    def test_the_running_copy_gets_the_path(self) -> None:
        sent = []
        with mock.patch.object(A, "setup_logging", lambda: None), \
                mock.patch.object(A.kernel32, "CreateEventW", lambda *a: 1), \
                mock.patch.object(A.ctypes, "get_last_error", lambda: A.ERROR_ALREADY_EXISTS), \
                mock.patch("saylight.single_instance.send_open", lambda p: sent.append(p) or True):
            self.assertEqual(A.run("C:/docs/paper.pdf"), 0)
        self.assertEqual(sent, ["C:/docs/paper.pdf"])


if __name__ == "__main__":
    unittest.main()

"""Tests for saylight_app.py: moving the settings over from the earlier name, replacing the earlier install, and
the App's handling of the Type or paste box, Clear and Undo.

No test here touches the registry, named events or the Start menu: the system operations are replaced by a fake,
and the modules that reach the real ones are swapped for stand-ins that fail the test if used. The App tests build
it from fakes (engine, input, foreground, panel, box, tray, HUD), with Win32 replaced: no window is shown, no
hotkey is registered and nothing plays.
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
from PySide6.QtWidgets import QApplication

import saylight_app as A
from saylight import win32
from saylight.engine import ClearedReading
from saylight.input_thread import HK_COMPOSE, HK_PANEL
from saylight.settings import Settings
from saylight.ui.compose import ComposeSession, ComposeSnapshot

_app = None


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class MigrateSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_app_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.new = os.path.join(self.dir, "saylight_settings.json")
        self.old = os.path.join(self.dir, "read_aloud_settings.json")
        self.sleeps: list[float] = []
        patcher = mock.patch.object(A, "time", types.SimpleNamespace(sleep=self.sleeps.append))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path: str, obj: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def read(self, path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_names_match_the_app(self):
        self.assertEqual(os.path.basename(A.SETTINGS_PATH), "saylight_settings.json")
        self.assertEqual(os.path.basename(A.OLD_SETTINGS_PATH), "read_aloud_settings.json")
        self.assertEqual(os.path.dirname(A.SETTINGS_PATH), os.path.dirname(A.OLD_SETTINGS_PATH))

    def test_old_file_is_renamed_and_its_settings_load(self):
        mine = {"speed": 1.25, "volume": 40, "reading_font": "sitka", "panel": {"visible": False}}
        self.write(self.old, mine)
        with self.assertLogs("saylight.app", "INFO") as logs:
            path = A.migrate_settings(self.new, self.old)
        self.assertEqual(path, self.new)
        self.assertFalse(os.path.exists(self.old))
        self.assertEqual(self.read(self.new), mine)
        self.assertIn("read_aloud_settings.json", logs.output[0])
        settings = Settings(path)
        self.assertEqual((settings.get("speed"), settings.get("volume"), settings.get("reading_font")),
                         (1.25, 40, "sitka"))

    def test_existing_new_file_wins_and_old_is_left_alone(self):
        self.write(self.new, {"speed": 0.9})
        self.write(self.old, {"speed": 1.5})
        with self.assertNoLogs("saylight.app", "INFO"):
            self.assertEqual(A.migrate_settings(self.new, self.old), self.new)
        self.assertEqual(self.read(self.new), {"speed": 0.9})
        self.assertEqual(self.read(self.old), {"speed": 1.5})

    def test_nothing_to_migrate(self):
        self.assertEqual(A.migrate_settings(self.new, self.old), self.new)
        self.assertEqual(os.listdir(self.dir), [])

    def test_brief_lock_is_retried(self):
        self.write(self.old, {"speed": 1.1})
        real, calls = os.rename, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 1:
                raise PermissionError(32, "in use")
            real(src, dst)

        with mock.patch.object(A.os, "rename", flaky), self.assertLogs("saylight.app", "INFO"):
            self.assertEqual(A.migrate_settings(self.new, self.old), self.new)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.read(self.new), {"speed": 1.1})

    def test_lasting_lock_uses_the_old_file_this_session(self):
        self.write(self.old, {"speed": 1.1})
        with mock.patch.object(A.os, "rename", side_effect=PermissionError(32, "in use")) as rename, \
                self.assertLogs("saylight.app", "ERROR"):
            self.assertEqual(A.migrate_settings(self.new, self.old), self.old)
        self.assertEqual(rename.call_count, 4)
        self.assertEqual(self.read(self.old), {"speed": 1.1})  # untouched, so the next start can try again
        self.assertFalse(os.path.exists(self.new))


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
                                 delete_run_value=self.delete_run_value, remove_file=self.remove_file,
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

    def test_quit_signals_both_names(self):
        for running, expected in (({OLD_EVENT}, True), ({NEW_EVENT}, True), (set(), False)):
            fake = FakeSystem(running=running)
            with self.subTest(running=running), fake.installed() as out:
                self.assertIs(A.quit_running(), expected)
                self.assertEqual(sorted(c[1] for c in fake.calls if c[0] == "signal"), sorted([OLD_EVENT, NEW_EVENT]))
                self.assertIn("Saylight has quit." if expected else "Saylight isn't running.", out.getvalue())


# --- The App: Type or paste, Clear and Undo --------------------------------------------------------

BOX, APP_WINDOW, OTHER_APP, SHELL, LAST_APP = 4242, 555, 700, 701, 888
QUNS_ACCEPTS_NOTIFICATIONS, QUNS_RUNNING_D3D_FULL_SCREEN = 5, 3


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

    def __init__(self, *, parent=None, **_parts) -> None:
        super().__init__(parent)
        self.state, self.document, self.cur, self.speed = "ready", None, 0, 1.0
        self.speakable = True

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

    def set_cleared(self, on: bool) -> None:
        self.cleared = on

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


class Quiet:
    """Any call does nothing: the tray, tooltips, synth and player."""

    def __init__(self, *_args, **_kw) -> None:
        pass

    def __getattr__(self, _name: str):
        return lambda *_args, **_kw: None


FAKES = {"saylight.engine.Engine": FakeEngine, "saylight.input_thread.InputThread": FakeInput,
         "saylight.foreground.ForegroundTracker": FakeForeground, "saylight.ui.panel.Panel": FakePanel,
         "saylight.ui.compose.ComposeWindow": FakeCompose, "saylight.ui.hud.Hud": FakeHud,
         "saylight.ui.tray.Tray": Quiet, "saylight.ui.tip.TipManager": Quiet, "saylight.synth.SynthWorker": Quiet,
         "saylight.player.PlayerThread": Quiet}


class ComposeFlowTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

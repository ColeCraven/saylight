"""Tests for input_thread.py.

Hotkeys are harmless chords (Ctrl+Alt+Shift+F24/F23/F22), pressed with SendInput: the foreground app
only sees modifier and unassigned-key events. The real copy job is replaced by a fake in every test,
so no Ctrl+C is ever sent. Media keys are never really registered (a fake registrar is used).
"""

import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEventLoop, QThread
from PySide6.QtWidgets import QApplication

from saylight import clipboard, input_thread, win32
from saylight.input_thread import HK_PANEL, HK_PAUSE, HK_READ, InputThread
from saylight.tests.test_win32 import CrossProcessLock

F24, F23, F22 = "Ctrl+Alt+Shift+F24", "Ctrl+Alt+Shift+F23", "Ctrl+Alt+Shift+F22"
VK_F23, VK_F22 = win32.VK_F24 - 1, win32.VK_F24 - 2
VK_MASK = 0xE8  # unassigned: keeps the Alt release from reaching the foreground app as a lone Alt tap


def press_chord(vk: int) -> bool:
    """Ctrl+Alt+Shift+vk, as one SendInput batch."""
    c, s, a = win32.VK_CONTROL, win32.VK_SHIFT, win32.VK_MENU
    return win32.send_key_events([(c, True), (s, True), (a, True), (vk, True), (vk, False),
                                  (VK_MASK, True), (VK_MASK, False), (a, False), (s, False), (c, False)])


def pump_until(predicate, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if predicate():
            return True
        time.sleep(0.005)
    QCoreApplication.processEvents()
    return predicate()


def pump_for(seconds: float) -> None:
    pump_until(lambda: False, seconds)


def chord_free(spec: str) -> bool:
    """True if nobody holds the chord (checked by briefly registering it on this thread)."""
    mods, vk = win32.parse_hotkey(spec)
    if not win32.register_hotkey(990, mods | win32.MOD_NOREPEAT, vk):
        return False
    win32.unregister_hotkey(990)
    return True


class Recorder:
    """Records every signal with whether it arrived on the GUI thread."""

    def __init__(self, t: InputThread):
        self.events: list[tuple] = []
        self.on_gui: list[bool] = []
        t.ready.connect(lambda: self._add("ready"))
        t.registered.connect(lambda hk, ok: self._add("registered", hk, ok))
        t.hotkeyPressed.connect(lambda hk: self._add("hotkey", hk))
        t.copied.connect(lambda token, text, reason: self._add("copied", token, text, reason))
        t.quitRequested.connect(lambda: self._add("quit"))

    def _add(self, *event):
        self.events.append(event)
        self.on_gui.append(threading.current_thread() is threading.main_thread()
                           and QThread.currentThread() is QCoreApplication.instance().thread())

    def of(self, kind: str) -> list[tuple]:
        return [e for e in self.events if e[0] == kind]


class FakeCopy:
    def __init__(self, result=("Some text.", "ok"), delay=0.0, error: Exception | None = None):
        self.result, self.delay, self.error = result, delay, error
        self.calls: list[dict] = []

    def __call__(self, *, is_click, last_app, key_vks, timeout_s=0.35):
        self.calls.append({"is_click": is_click, "last_app": last_app, "key_vks": list(key_vks),
                           "thread": threading.current_thread().name})
        time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


class InputThreadTests(unittest.TestCase):
    lock: CrossProcessLock

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.lock = CrossProcessLock("hotkeys").__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.lock.__exit__(None, None, None)

    def setUp(self):
        self.quit_event = win32.create_event()
        self.fake = FakeCopy()
        patcher = mock.patch.object(input_thread, "copy_selection", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(win32.close_handle, self.quit_event)

    def start(self, hotkeys: dict[int, str], quit_event: int | None = None) -> tuple[InputThread, Recorder]:
        t = InputThread(self.quit_event if quit_event is None else quit_event, hotkeys)
        rec = Recorder(t)
        self.addCleanup(t.stop)
        t.start()
        self.assertTrue(pump_until(lambda: rec.of("ready")), "ready never arrived")
        return t, rec

    def test_ready_follows_registration_and_disabled_ids_are_silent(self):
        with self.assertLogs("saylight.input_thread", "WARNING") as logs:
            t, rec = self.start({HK_READ: F24, HK_PANEL: "", HK_PAUSE: "Hyper+Q"})
        self.assertIn("not a valid shortcut", logs.output[0])
        self.assertEqual(rec.events, [("registered", HK_READ, True), ("registered", HK_PAUSE, False), ("ready",)])
        self.assertTrue(all(rec.on_gui))
        self.assertFalse(chord_free(F24))

    def press_until_it_arrives(self, rec: Recorder, vk: int, kind: str) -> None:
        """Press Ctrl+Alt+Shift+vk until `kind` arrives. On a live desktop the foreground app or a keyboard
        hook can swallow injected input (seen with Word in front); then skip, as delivery of WM_HOTKEY itself
        is covered by the posted-message tests."""
        for _ in range(3):
            self.assertTrue(press_chord(vk), "SendInput was blocked")
            if pump_until(lambda: rec.of(kind), 1.5):
                return
        fg = win32.get_foreground_window()
        self.skipTest(f"injected chords never reached the hotkey (foreground: {win32.get_class_name(fg)!r})")

    def test_send_input_hotkey_reaches_gui_thread_then_auto_copy(self):
        t, rec = self.start({HK_READ: F24})
        self.assertEqual(rec.of("registered"), [("registered", HK_READ, True)])
        self.press_until_it_arrives(rec, win32.VK_F24, "copied")
        self.assertEqual(rec.events[-2:], [("hotkey", HK_READ), ("copied", 0, "Some text.", "ok")])
        self.assertTrue(all(rec.on_gui), "a signal arrived off the GUI thread")
        self.assertEqual(self.fake.calls, [{"is_click": False, "last_app": 0, "key_vks": [win32.VK_F24],
                                            "thread": "saylight.input"}])

    def test_other_hotkeys_do_not_copy(self):
        t, rec = self.start({HK_PANEL: F24})
        win32.post_thread_message(t._thread_id, win32.WM_HOTKEY, HK_PANEL, 0)
        self.assertTrue(pump_until(lambda: rec.of("hotkey")))
        pump_for(0.1)
        self.assertEqual(rec.of("hotkey"), [("hotkey", HK_PANEL)])
        self.assertEqual((rec.of("copied"), self.fake.calls), ([], []))

    def test_hotkey_for_an_unregistered_id_is_ignored(self):
        t, rec = self.start({HK_READ: F24})
        win32.post_thread_message(t._thread_id, win32.WM_HOTKEY, HK_PAUSE, 0)
        pump_for(0.15)
        self.assertEqual(rec.of("hotkey"), [])

    def test_set_hotkeys_reregisters(self):
        t, rec = self.start({HK_PANEL: F24})
        t.set_hotkeys({HK_PANEL: F23})
        self.assertTrue(pump_until(lambda: len(rec.of("registered")) == 2))
        self.assertEqual(rec.of("registered"), [("registered", HK_PANEL, True)] * 2)
        self.assertTrue(chord_free(F24), "the old chord is still registered")
        self.assertFalse(chord_free(F23), "the new chord isn't registered")
        self.press_until_it_arrives(rec, VK_F23, "hotkey")
        self.assertEqual(rec.of("hotkey"), [("hotkey", HK_PANEL)])

    def test_failed_registration_is_reported_and_retried(self):
        mods, vk = win32.parse_hotkey(F22)
        self.assertTrue(win32.register_hotkey(991, mods | win32.MOD_NOREPEAT, vk))  # someone else holds F22
        try:
            with self.assertLogs("saylight.input_thread", "WARNING") as logs:
                t, rec = self.start({HK_PAUSE: F22})
            self.assertEqual(rec.of("registered"), [("registered", HK_PAUSE, False)])
            self.assertIn("error 1409", logs.output[0])  # ERROR_HOTKEY_ALREADY_REGISTERED
        finally:
            win32.unregister_hotkey(991)
        t.set_hotkeys({HK_PAUSE: F22})  # e.g. "Retry shortcut"
        self.assertTrue(pump_until(lambda: len(rec.of("registered")) == 2))
        self.assertEqual(rec.of("registered")[-1], ("registered", HK_PAUSE, True))
        t.set_hotkeys({HK_PAUSE: F22})  # unchanged: kept, reported again
        self.assertTrue(pump_until(lambda: len(rec.of("registered")) == 3))
        self.assertEqual(rec.of("registered")[-1], ("registered", HK_PAUSE, True))
        t.set_hotkeys({HK_PAUSE: ""})  # disabled: unregistered silently
        pump_for(0.15)
        self.assertEqual(len(rec.of("registered")), 3)
        self.assertTrue(chord_free(F22))

    def test_quit_event_is_reported_once_and_the_thread_keeps_running(self):
        t, rec = self.start({})
        win32.set_event(self.quit_event)
        self.assertTrue(pump_until(lambda: rec.of("quit")))
        pump_for(0.3)  # the manual-reset event stays signalled; it must not be reported again
        self.assertEqual(rec.of("quit"), [("quit",)])
        self.assertTrue(all(rec.on_gui))
        t.request_copy(7, 0)
        self.assertTrue(pump_until(lambda: rec.of("copied")), "the thread stopped after the quit event")
        self.assertTrue(t._thread.is_alive())

    def test_without_a_quit_event(self):
        t, rec = self.start({}, quit_event=0)
        t.request_copy(3, 0)
        self.assertTrue(pump_until(lambda: rec.of("copied")))
        self.assertEqual(rec.of("quit"), [])

    def test_a_broken_quit_handle_is_logged_not_spun_on(self):
        with self.assertLogs("saylight.input_thread", "ERROR"):
            t, rec = self.start({}, quit_event=0x7FFFFFF0)
            pump_for(0.1)
        t.request_copy(4, 0)
        self.assertTrue(pump_until(lambda: rec.of("copied")))

    def test_request_copy_is_a_click_copy(self):
        t, rec = self.start({})
        t.request_copy(12345, 0xABCDEF)
        self.assertTrue(pump_until(lambda: rec.of("copied")))
        self.assertEqual(rec.of("copied"), [("copied", 12345, "Some text.", "ok")])
        self.assertEqual(self.fake.calls, [{"is_click": True, "last_app": 0xABCDEF, "key_vks": [],
                                            "thread": "saylight.input"}])

    def test_copy_reasons_pass_through(self):
        self.fake.result = ("", "console")
        t, rec = self.start({})
        t.request_copy(1, 0)
        self.assertTrue(pump_until(lambda: rec.of("copied")))
        self.assertEqual(rec.of("copied"), [("copied", 1, "", "console")])

    def test_copy_failure_reports_busy_and_the_thread_survives(self):
        self.fake.error = RuntimeError("boom")
        t, rec = self.start({})
        with self.assertLogs("saylight.input_thread", "ERROR"):
            t.request_copy(5, 0)
            self.assertTrue(pump_until(lambda: rec.of("copied")))
        self.assertEqual(rec.of("copied"), [("copied", 5, "", "busy")])
        self.fake.error = None
        t.request_copy(6, 0)
        self.assertTrue(pump_until(lambda: len(rec.of("copied")) == 2))
        self.assertEqual(rec.of("copied")[-1], ("copied", 6, "Some text.", "ok"))

    def test_read_presses_made_while_copying_are_dropped(self):
        self.fake.delay = 0.3
        t, rec = self.start({HK_READ: F24, HK_PANEL: F23})
        for hk in (HK_READ, HK_READ, HK_PANEL, HK_READ):
            win32.post_thread_message(t._thread_id, win32.WM_HOTKEY, hk, 0)
        with self.assertLogs("saylight.input_thread", "INFO") as logs:
            self.assertTrue(pump_until(lambda: rec.of("copied") and len(rec.of("hotkey")) == 2))
            pump_for(0.4)
        self.assertEqual(rec.of("hotkey"), [("hotkey", HK_READ), ("hotkey", HK_PANEL)])
        self.assertEqual(len(rec.of("copied")), 1)
        self.assertIn("Ignored 2 Read presses", "\n".join(logs.output))

    def test_token_range(self):
        t, rec = self.start({})
        for token in (-1, 0, 2 ** 31):  # 0 is reserved for the automatic copy after HK_READ
            with self.assertRaises(ValueError):
                t.request_copy(token, 0)
        t.request_copy(2 ** 31 - 1, 0)
        self.assertTrue(pump_until(lambda: rec.of("copied")))
        self.assertEqual(rec.of("copied")[0][1], 2 ** 31 - 1)

    def test_the_loop_polls_a_late_copy_watch_then_idles(self):
        polls: list[float] = []

        def fake_watch():
            polls.append(time.monotonic())
            return 0.5 if len(polls) <= 5 else None  # watching for a while, then done

        with mock.patch.object(input_thread, "watch_late_copy", fake_watch):
            self.start({})
            self.assertTrue(pump_until(lambda: len(polls) >= 6, 2.0), "the loop didn't wake up to poll")
            pump_for(0.1)
            settled = len(polls)
            pump_for(0.2)
            self.assertEqual(len(polls), settled, "the loop kept waking up after the watch ended")
        # ~LATE_COPY_POLL_S apart, so no spinning (the first wait may end at once on start-up messages)
        gaps = [b - a for a, b in zip(polls[1:5], polls[2:6])]
        self.assertTrue(all(0.01 <= g < 0.2 for g in gaps), gaps)

    def test_a_hotkey_posted_while_the_loop_pumps_is_not_slept_on(self):
        pumping, posted = threading.Event(), threading.Event()
        calls = []

        def fake_watch():
            calls.append(1)
            if len(calls) == 1:  # the first check pumps sent messages, as a late-copy restore does
                pumping.set()
                posted.wait(2)
                win32.wait_pumping_sent(0.05)
            return None

        with mock.patch.object(input_thread, "watch_late_copy", fake_watch):
            t, rec = self.start({HK_PANEL: F24})
            self.assertTrue(pumping.wait(2))
            win32.post_thread_message(t._thread_id, win32.WM_HOTKEY, HK_PANEL, 0)
            posted.set()
            self.assertTrue(pump_until(lambda: rec.of("hotkey"), 2.0), "the queued hotkey was slept on")

    def test_a_late_copy_is_undone_on_the_input_thread_without_any_message(self):
        restored = []

        class SlowApp(clipboard._System):
            seq = 1

            def sequence(self):
                return SlowApp.seq

            def sleep(self, seconds):
                time.sleep(seconds)

            def restore(self, saved):
                restored.append((saved, threading.current_thread().name))
                return True

        def copy_that_times_out(**_):
            clipboard._tls.late = clipboard._LateCopy(1, clipboard.Saved("prev", False), time.monotonic() + 0.8)
            return "", "empty"

        with mock.patch.object(input_thread, "copy_selection", copy_that_times_out), \
                mock.patch.object(clipboard, "_System", SlowApp):
            t, rec = self.start({})
            t.request_copy(1, 0)
            self.assertTrue(pump_until(lambda: rec.of("copied")))
            pump_for(0.1)
            self.assertEqual(restored, [])
            SlowApp.seq = 2  # the slow app's copy lands; nothing is posted to the thread
            self.assertTrue(pump_until(lambda: restored, 1.0), "the late copy wasn't undone")
        self.assertEqual(restored, [(clipboard.Saved("prev", False), "saylight.input")])
        self.assertEqual(rec.of("copied"), [("copied", 1, "", "empty")])

    def test_media_keys_use_fixed_ids_without_real_registration(self):
        registered, unregistered = [], []
        with mock.patch.object(win32, "register_hotkey", lambda *a: registered.append(a) or True), \
                mock.patch.object(win32, "unregister_hotkey", lambda hk: unregistered.append(hk) or True):
            t, rec = self.start({})
            t.set_media_keys(True)
            self.assertTrue(pump_until(lambda: len(rec.of("registered")) == 4))
            t.set_media_keys(True)  # already on: nothing new
            pump_for(0.1)
            t.set_media_keys(False)
            self.assertTrue(pump_until(lambda: len(unregistered) == 4))
            t.stop()
        nr = win32.MOD_NOREPEAT
        self.assertEqual(registered, [(10, nr, 0xB3), (11, nr, 0xB2), (12, nr, 0xB0), (13, nr, 0xB1)])
        self.assertEqual(rec.of("registered"), [("registered", hk, True) for hk in (10, 11, 12, 13)])
        self.assertEqual(unregistered, [10, 11, 12, 13])  # and not again at stop()

    def test_stop_unregisters_on_its_thread_and_joins(self):
        t, rec = self.start({HK_READ: F24, HK_PANEL: F23})
        self.assertFalse(chord_free(F24))
        t.stop()
        self.assertFalse(t._thread.is_alive())
        self.assertTrue(chord_free(F24))
        self.assertTrue(chord_free(F23))
        t.stop()  # twice is fine
        with self.assertLogs("saylight.input_thread", "WARNING"):
            t.request_copy(9, 0)
            t.set_hotkeys({HK_READ: F24})
            t.set_media_keys(True)
        self.assertTrue(pump_until(lambda: rec.of("copied")))
        self.assertEqual(rec.of("copied"), [("copied", 9, "", "busy")])  # never left waiting
        self.assertTrue(chord_free(F24))

    def test_start_twice_is_harmless(self):
        t, rec = self.start({})
        with self.assertLogs("saylight.input_thread", "WARNING"):
            t.start()
        self.assertEqual(rec.of("ready"), [("ready",)])

    def test_set_hotkeys_before_start_sets_the_initial_hotkeys(self):
        t = InputThread(self.quit_event, {HK_PANEL: F23})
        rec = Recorder(t)
        self.addCleanup(t.stop)
        t.set_hotkeys({HK_PANEL: F24})
        t.start()
        self.assertTrue(pump_until(lambda: rec.of("ready")))
        self.assertEqual(rec.of("registered"), [("registered", HK_PANEL, True)])
        self.assertFalse(chord_free(F24))
        self.assertTrue(chord_free(F23))

    def test_methods_before_start_do_not_hang(self):
        t = InputThread(0, {})
        rec = Recorder(t)
        with self.assertLogs("saylight.input_thread", "WARNING"):
            t.request_copy(2, 0)
        t.stop()
        self.assertTrue(pump_until(lambda: rec.of("copied")))
        self.assertEqual(rec.of("copied"), [("copied", 2, "", "busy")])


if __name__ == "__main__":
    unittest.main()

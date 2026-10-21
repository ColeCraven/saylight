"""Tests for foreground.py: hook install/uninstall and last_app filtering with fake windows, plus one real
SetWinEventHook/UnhookWinEvent round trip. Nothing here changes the foreground window."""

import os
import unittest
from unittest import mock

from saylight import foreground, win32
from saylight.foreground import ForegroundTracker

APP, APP2, SHELL, OURS, GONE = 0x1001, 0x1002, 0x2001, 0x3001, 0x4001
OTHER_PID = os.getpid() + 4


class FakeDesktop:
    """Fake windows behind the win32 functions the tracker uses."""

    def __init__(self):
        self.windows = {APP: ("Notepad", OTHER_PID), APP2: ("Chrome_WidgetWin_1", OTHER_PID + 4),
                        SHELL: ("Shell_TrayWnd", OTHER_PID + 8), OURS: ("Qt6110QWindowToolSaveBits", os.getpid())}
        self.fg = APP
        self.hooks: list[tuple] = []
        self.unhooked: list[int] = []
        self.set_fg_calls: list[int] = []
        self.set_fg_result = True

    def patches(self):
        return [
            mock.patch.object(win32, "set_win_event_hook", self.set_win_event_hook),
            mock.patch.object(win32, "unhook_win_event", self.unhook_win_event),
            mock.patch.object(win32, "get_foreground_window", lambda: self.fg),
            mock.patch.object(win32, "set_foreground_window", self.set_foreground_window),
            mock.patch.object(win32, "is_window", lambda h: h in self.windows),
            mock.patch.object(win32, "get_class_name", lambda h: self.windows.get(h, ("", 0))[0]),
            mock.patch.object(win32, "get_window_pid", lambda h: self.windows.get(h, ("", 0))[1]),
        ]

    def set_win_event_hook(self, event_min, event_max, proc, flags):
        self.hooks.append((event_min, event_max, proc, flags))
        return 0xBEEF

    def unhook_win_event(self, hook):
        self.unhooked.append(hook)
        return True

    def set_foreground_window(self, hwnd):
        self.set_fg_calls.append(hwnd)
        if self.set_fg_result:
            self.fg = hwnd
        return self.set_fg_result

    def switch_to(self, hwnd):
        """The user activates hwnd: Windows calls the hook's callback (through its real ctypes thunk)."""
        self.fg = hwnd
        proc = self.hooks[-1][2]
        proc(0xBEEF, win32.EVENT_SYSTEM_FOREGROUND, hwnd, 0, 0, 0, 0)


class FakeHookTests(unittest.TestCase):
    def setUp(self):
        self.desk = FakeDesktop()
        for p in self.desk.patches():
            p.start()
            self.addCleanup(p.stop)
        self.tracker = ForegroundTracker()
        self.addCleanup(self.tracker.stop)

    def test_install_once_with_the_right_event_and_flags(self):
        self.assertTrue(self.tracker.start())
        self.assertTrue(self.tracker.start())
        self.assertEqual(len(self.desk.hooks), 1)
        event_min, event_max, proc, flags = self.desk.hooks[0]
        self.assertEqual((event_min, event_max), (win32.EVENT_SYSTEM_FOREGROUND,) * 2)
        self.assertEqual(flags, win32.WINEVENT_OUTOFCONTEXT | win32.WINEVENT_SKIPOWNPROCESS)
        self.assertIsInstance(proc, win32.WINEVENTPROC)
        self.assertIs(self.tracker._proc, proc)  # kept alive while hooked

    def test_uninstall_once(self):
        self.tracker.start()
        self.tracker.stop()
        self.tracker.stop()
        self.assertEqual(self.desk.unhooked, [0xBEEF])
        self.assertIsNone(self.tracker._proc)

    def test_failed_install(self):
        with mock.patch.object(win32, "set_win_event_hook", lambda *a: 0), \
                self.assertLogs("saylight.foreground", "WARNING"):
            self.assertFalse(self.tracker.start())
        self.assertIsNone(self.tracker._proc)
        self.tracker.stop()
        self.assertEqual(self.desk.unhooked, [])

    def test_start_seeds_with_the_current_app(self):
        self.tracker.start()
        self.assertEqual(self.tracker.last_app(), APP)

    def test_start_does_not_seed_with_the_shell(self):
        self.desk.fg = SHELL
        self.tracker.start()
        self.assertEqual(self.tracker.last_app(), 0)

    def test_filtering(self):
        self.tracker.start()
        self.desk.switch_to(APP2)
        self.assertEqual(self.tracker.last_app(), APP2)
        for hwnd in (SHELL, OURS, 0, None, 0x9999):  # shell, our own window, no window, unknown window
            self.desk.switch_to(hwnd)
            self.assertEqual(self.tracker.last_app(), APP2, hwnd)
        self.desk.switch_to(APP)
        self.assertEqual(self.tracker.last_app(), APP)

    def test_every_shell_class_is_ignored(self):
        self.tracker.start()
        for i, cls in enumerate(sorted(win32.SHELL_CLASSES)):
            hwnd = 0x5000 + i
            self.desk.windows[hwnd] = (cls, OTHER_PID)
            self.desk.switch_to(hwnd)
            self.assertEqual(self.tracker.last_app(), APP, cls)

    def test_other_events_are_ignored(self):
        self.tracker.start()
        self.desk.hooks[-1][2](0xBEEF, 0x8005, APP2, 0, 0, 0, 0)  # EVENT_OBJECT_FOCUS
        self.assertEqual(self.tracker.last_app(), APP)

    def test_a_closed_app_is_forgotten(self):
        self.tracker.start()
        self.desk.switch_to(APP2)
        del self.desk.windows[APP2]
        self.assertEqual(self.tracker.last_app(), 0)

    def test_errors_in_the_callback_are_contained(self):
        self.tracker.start()
        with mock.patch.object(win32, "get_class_name", side_effect=RuntimeError("boom")), \
                self.assertLogs("saylight.foreground", "ERROR"):
            self.desk.switch_to(APP2)
        self.assertEqual(self.tracker.last_app(), APP)

    def test_restore_does_nothing_when_an_app_is_in_front(self):
        self.tracker.start()
        self.desk.switch_to(APP2)
        self.desk.fg = APP
        self.assertFalse(self.tracker.restore_foreground())
        self.assertEqual(self.desk.set_fg_calls, [])

    def test_restore_from_the_shell_ours_or_nothing(self):
        for front in (SHELL, OURS, 0):
            with self.subTest(front=front):
                self.desk.set_fg_calls.clear()
                self.tracker.start()
                self.desk.switch_to(APP2)
                self.desk.fg = front
                self.assertTrue(self.tracker.restore_foreground())
                self.assertEqual(self.desk.set_fg_calls, [APP2])
                self.assertEqual(self.desk.fg, APP2)

    def test_restore_refused(self):
        self.tracker.start()
        self.desk.fg = SHELL
        self.desk.set_fg_result = False
        with self.assertLogs("saylight.foreground", "INFO"):
            self.assertFalse(self.tracker.restore_foreground())
        self.assertEqual(self.desk.set_fg_calls, [APP])

    def test_restore_without_a_last_app(self):
        self.desk.fg = SHELL
        self.tracker.start()
        self.assertFalse(self.tracker.restore_foreground())
        self.assertEqual(self.desk.set_fg_calls, [])


class AppActivatedTests(unittest.TestCase):
    def setUp(self):
        self.desk = FakeDesktop()
        for p in self.desk.patches():
            p.start()
            self.addCleanup(p.stop)
        self.tracker = ForegroundTracker()
        self.addCleanup(self.tracker.stop)
        self.activated: list[int] = []
        self.tracker.appActivated.connect(self.activated.append)

    def test_not_emitted_for_the_start_up_seed(self):
        self.tracker.start()
        self.assertEqual(self.tracker.last_app(), APP)
        self.assertEqual(self.activated, [])

    def test_emitted_for_app_windows(self):
        self.tracker.start()
        self.desk.switch_to(APP2)
        self.desk.switch_to(APP)
        self.assertEqual(self.activated, [APP2, APP])

    def test_emitted_again_when_the_same_app_comes_back(self):
        """Saylight's own windows raise no event, so going back to the app you came from must still count."""
        self.tracker.start()
        self.desk.switch_to(APP)
        self.desk.switch_to(APP)
        self.assertEqual(self.activated, [APP, APP])

    def test_not_emitted_for_the_shell_our_own_windows_or_no_window(self):
        self.tracker.start()
        for hwnd in (SHELL, OURS, 0, None, 0x9999):
            self.desk.switch_to(hwnd)
        for i, cls in enumerate(sorted(win32.SHELL_CLASSES)):
            self.desk.windows[0x5000 + i] = (cls, OTHER_PID)
            self.desk.switch_to(0x5000 + i)
        self.assertEqual(self.activated, [])

    def test_other_events_do_not_emit(self):
        self.tracker.start()
        self.desk.hooks[-1][2](0xBEEF, 0x8005, APP2, 0, 0, 0, 0)  # EVENT_OBJECT_FOCUS
        self.assertEqual(self.activated, [])

    def test_a_failing_receiver_is_contained(self):
        self.tracker.start()

        def boom(hwnd):
            raise RuntimeError("boom")

        self.tracker.appActivated.connect(boom)
        with mock.patch("sys.excepthook"):  # Qt reports a receiver's exception through the excepthook
            self.desk.switch_to(APP2)
        self.assertEqual(self.tracker.last_app(), APP2)
        self.assertEqual(self.activated, [APP2])


class RealHookTests(unittest.TestCase):
    def test_real_install_and_uninstall(self):
        before = win32.get_foreground_window()
        tracker = ForegroundTracker()
        try:
            self.assertTrue(tracker.start())
            self.assertTrue(tracker._hook)
            hook = tracker._hook
        finally:
            tracker.stop()
        self.assertEqual(tracker._hook, 0)
        self.assertFalse(win32.unhook_win_event(hook), "the hook was still installed")
        self.assertEqual(win32.get_foreground_window(), before)

    def test_unhook_logs_a_failure(self):
        tracker = ForegroundTracker()
        tracker.start()
        real_hook = tracker._hook
        with mock.patch.object(foreground.win32, "unhook_win_event", return_value=False), \
                self.assertLogs("saylight.foreground", "WARNING"):
            tracker.stop()
        win32.unhook_win_event(real_hook)


if __name__ == "__main__":
    unittest.main()

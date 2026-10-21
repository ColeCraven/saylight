"""Tests for win32.py. Uses a hidden STATIC window that is never shown; no audio, no focus changes."""

import ctypes
import os
import unittest
from ctypes import wintypes
from unittest import mock

from saylight import win32

# The test's own WinDLL instance, so these prototypes can't clash with win32.py's.
_u = ctypes.WinDLL("user32", use_last_error=True)
_u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_u.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
_u.IsWindowVisible.restype = wintypes.BOOL
_u.IsWindowVisible.argtypes = (wintypes.HWND,)
GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x00000008

_k = ctypes.WinDLL("kernel32", use_last_error=True)
_k.CreateMutexW.restype = wintypes.HANDLE
_k.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_k.WaitForSingleObject.restype = wintypes.DWORD
_k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_k.ReleaseMutex.restype = wintypes.BOOL
_k.ReleaseMutex.argtypes = (wintypes.HANDLE,)
_k.CloseHandle.restype = wintypes.BOOL
_k.CloseHandle.argtypes = (wintypes.HANDLE,)


class CrossProcessLock:
    """A named mutex that serializes tests using global resources (hotkeys, injected keys, the sandbox)
    across concurrent test runs. Acquire and release on one thread."""

    def __init__(self, name: str, timeout_s: float = 180.0):
        self.name, self.timeout_s, self.handle = f"Local\\SaylightTests.{name}", timeout_s, 0

    def __enter__(self) -> "CrossProcessLock":
        self.handle = _k.CreateMutexW(None, False, self.name)
        if not self.handle or _k.WaitForSingleObject(self.handle, int(self.timeout_s * 1000)) not in (0, 0x80):
            raise TimeoutError(f"couldn't take {self.name} within {self.timeout_s:.0f} s")
        return self

    def __exit__(self, *exc) -> None:
        _k.ReleaseMutex(self.handle)
        _k.CloseHandle(self.handle)


def _hidden_window() -> int:
    hwnd = win32.user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    assert hwnd, "CreateWindowExW failed"
    return hwnd


class PrototypeTests(unittest.TestCase):
    def test_input_struct_is_64_bit_layout(self):
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p), 8)
        self.assertEqual(ctypes.sizeof(win32.INPUT), 40)  # sizeof(INPUT) on x64 Windows
        self.assertEqual(ctypes.sizeof(win32.KEYBDINPUT), 24)

    def test_handles_and_params_are_pointer_sized(self):
        self.assertIs(win32.user32.GetForegroundWindow.restype, wintypes.HWND)
        self.assertIs(win32.user32.SetWindowPos.argtypes[1], wintypes.HWND)
        self.assertIs(win32.user32.PostThreadMessageW.argtypes[2], wintypes.WPARAM)
        self.assertIs(win32.user32.PostThreadMessageW.argtypes[3], wintypes.LPARAM)
        self.assertEqual(ctypes.sizeof(wintypes.WPARAM), 8)
        self.assertEqual(ctypes.sizeof(wintypes.LPARAM), 8)
        self.assertIs(win32.kernel32.GlobalLock.restype, wintypes.LPVOID)
        self.assertIs(win32.kernel32.GlobalSize.restype, ctypes.c_size_t)
        self.assertIs(win32.user32.DispatchMessageW.restype, ctypes.c_ssize_t)

    def test_own_dll_instances(self):
        self.assertIsNot(win32.user32, ctypes.windll.user32)


class ParseHotkeyTests(unittest.TestCase):
    def test_defaults_and_test_chords(self):
        mods, vk = win32.parse_hotkey("Ctrl+`")
        self.assertEqual(mods, win32.MOD_CONTROL)
        self.assertIn(vk, (win32.VK_OEM_3, 0xDF, 0xDC))  # US, UK, other layouts
        self.assertEqual(win32.parse_hotkey("Ctrl+Alt+Shift+F24"),
                         (win32.MOD_CONTROL | win32.MOD_ALT | win32.MOD_SHIFT, win32.VK_F24))
        self.assertEqual(win32.parse_hotkey("Ctrl+Alt+`")[0], win32.MOD_CONTROL | win32.MOD_ALT)

    def test_disabled_and_invalid(self):
        for spec in ("", "   ", None, 5, "Ctrl+", "Ctrl+Shift+", "+", "Hyper+A", "Ctrl+AB", "Ctrl+F25",
                     "Ctrl+F0", "a", "`", "Space", "Ctrl+\U0001F600"):
            with self.subTest(spec=spec):
                self.assertIsNone(win32.parse_hotkey(spec))

    def test_case_spacing_and_modifier_names(self):
        expected = (win32.MOD_CONTROL | win32.MOD_ALT | win32.MOD_SHIFT | win32.MOD_WIN, win32.VK_F1 + 22)
        self.assertEqual(win32.parse_hotkey(" control + ALT + shift + Win + f23 "), expected)
        self.assertEqual(win32.parse_hotkey("Ctrl+Ctrl+F1"), (win32.MOD_CONTROL, win32.VK_F1))

    def test_letters_mean_the_key_not_shift(self):
        self.assertEqual(win32.parse_hotkey("Ctrl+A"), (win32.MOD_CONTROL, 0x41))
        self.assertEqual(win32.parse_hotkey("Ctrl+a"), (win32.MOD_CONTROL, 0x41))

    def test_bare_function_and_named_keys(self):
        self.assertEqual(win32.parse_hotkey("F13"), (0, win32.VK_F1 + 12))
        self.assertEqual(win32.parse_hotkey("Pause"), (0, 0x13))
        self.assertEqual(win32.parse_hotkey("Ctrl+Space"), (win32.MOD_CONTROL, 0x20))

    def test_plus_key(self):
        parsed = win32.parse_hotkey("Ctrl++")
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed[0] & win32.MOD_CONTROL)

    def test_shifted_character_adds_shift(self):
        with mock.patch.object(win32.user32, "VkKeyScanW", return_value=0x1BF):  # "?" = Shift + VK_OEM_2
            self.assertEqual(win32.parse_hotkey("Ctrl+?"), (win32.MOD_CONTROL | win32.MOD_SHIFT, 0xBF))

    def test_altgr_character_is_rejected(self):
        with mock.patch.object(win32.user32, "VkKeyScanW", return_value=0x0651):  # Ctrl+Alt (AltGr) + Q
            self.assertIsNone(win32.parse_hotkey("Ctrl+@"))

    def test_backtick_falls_back_to_oem3(self):
        for result in (-1, 0x1DD):  # missing, or needs Shift (e.g. a dead key on a German layout)
            with mock.patch.object(win32.user32, "VkKeyScanW", return_value=result):
                self.assertEqual(win32.parse_hotkey("Ctrl+`"), (win32.MOD_CONTROL, win32.VK_OEM_3))

    def test_missing_character_is_rejected(self):
        with mock.patch.object(win32.user32, "VkKeyScanW", return_value=-1):
            self.assertIsNone(win32.parse_hotkey("Ctrl+é"))


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.hwnd = _hidden_window()

    def tearDown(self):
        fg = win32.get_foreground_window()
        if win32.is_window(self.hwnd):
            win32.user32.DestroyWindow(self.hwnd)
        # The user may switch apps meanwhile; what must never happen is our window taking the foreground.
        self.assertNotEqual(win32.get_window_pid(fg), os.getpid(), "a test window took the foreground")

    def test_identity(self):
        self.assertEqual(win32.get_class_name(self.hwnd), "Static")
        self.assertEqual(win32.get_window_pid(self.hwnd), os.getpid())
        self.assertTrue(win32.is_window(self.hwnd))
        self.assertFalse(win32.is_shell_window(self.hwnd))
        self.assertFalse(win32.is_terminal_window(self.hwnd))
        self.assertFalse(win32.is_app_window(self.hwnd))  # ours

    def test_destroyed_and_null(self):
        win32.user32.DestroyWindow(self.hwnd)
        self.assertFalse(win32.is_window(self.hwnd))
        self.assertEqual(win32.get_class_name(self.hwnd), "")
        self.assertEqual(win32.get_window_pid(self.hwnd), 0)
        for fn in (win32.get_class_name, win32.get_window_pid, win32.is_window, win32.is_app_window,
                   win32.set_foreground_window):
            self.assertFalse(fn(0))

    def test_foreground_window_is_an_int(self):
        self.assertIsInstance(win32.get_foreground_window(), int)

    def test_class_lists(self):
        for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                    "TopLevelWindowForOverflowXamlIsland", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow"):
            with mock.patch.object(win32, "get_class_name", return_value=cls):
                self.assertTrue(win32.is_shell_window(1234), cls)
                self.assertFalse(win32.is_terminal_window(1234), cls)
        for cls in ("CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "mintty"):
            with mock.patch.object(win32, "get_class_name", return_value=cls):
                self.assertTrue(win32.is_terminal_window(1234), cls)
                self.assertFalse(win32.is_shell_window(1234), cls)

    def test_is_app_window(self):
        with mock.patch.object(win32, "is_window", return_value=True), \
                mock.patch.object(win32, "get_class_name", return_value="Notepad"), \
                mock.patch.object(win32, "get_window_pid", return_value=os.getpid() + 1):
            self.assertTrue(win32.is_app_window(1234))
            with mock.patch.object(win32, "get_class_name", return_value="Shell_TrayWnd"):
                self.assertFalse(win32.is_app_window(1234))

    def test_is_own_window(self):
        self.assertTrue(win32.is_own_window(self.hwnd))
        with mock.patch.object(win32, "is_window", return_value=True), \
                mock.patch.object(win32, "get_window_pid", return_value=os.getpid() + 1):
            self.assertFalse(win32.is_own_window(1234))  # another process
        with mock.patch.object(win32, "is_window", return_value=True), \
                mock.patch.object(win32, "get_class_name", return_value="Shell_TrayWnd"), \
                mock.patch.object(win32, "get_window_pid", return_value=os.getpid()):
            self.assertTrue(win32.is_own_window(1234))  # any class, as long as it is ours
        win32.user32.DestroyWindow(self.hwnd)
        self.assertFalse(win32.is_own_window(self.hwnd))
        self.assertFalse(win32.is_own_window(0))

    def test_own_and_app_windows_never_overlap(self):
        for pid in (os.getpid(), os.getpid() + 1):
            with mock.patch.object(win32, "is_window", return_value=True), \
                    mock.patch.object(win32, "get_class_name", return_value="Notepad"), \
                    mock.patch.object(win32, "get_window_pid", return_value=pid):
                self.assertNotEqual(win32.is_own_window(1234), win32.is_app_window(1234), pid)

    def test_dwm_attributes(self):
        self.assertTrue(win32.dwm_set_corner(self.hwnd))
        self.assertTrue(win32.dwm_set_corner(self.hwnd, small=True))
        self.assertTrue(win32.dwm_set_dark(self.hwnd, True))
        self.assertTrue(win32.dwm_set_dark(self.hwnd, False))
        self.assertTrue(win32.dwm_disable_transitions(self.hwnd))
        self.assertTrue(win32.dwm_set_border_color(self.hwnd, (0xDB, 0x9E, 0xE5)))
        self.assertTrue(win32.dwm_set_border_color(self.hwnd, None))
        self.assertTrue(win32.dwm_set_border_color(self.hwnd, win32.BORDER_NONE))
        self.assertFalse(win32.dwm_set_corner(0x7FFF0001))  # not a window

    def test_border_colorref_encoding(self):
        seen = []
        with mock.patch.object(win32, "_dwm_set", side_effect=lambda h, a, v, *t: seen.append((a, v)) or True):
            win32.dwm_set_border_color(1, (0x11, 0x22, 0x33))
            win32.dwm_set_border_color(1, None)
            win32.dwm_set_border_color(1, win32.BORDER_NONE)
        self.assertEqual(seen, [(34, 0x332211), (34, 0xFFFFFFFF), (34, 0xFFFFFFFE)])

    def test_topmost_without_showing_or_activating(self):
        # Windows sometimes accepts HWND_TOPMOST for a hidden window of a background process without setting
        # WS_EX_TOPMOST (seen intermittently here), so check the exact call and that it succeeded instead.
        real = win32.user32.SetWindowPos
        calls = []
        with mock.patch.object(win32.user32, "SetWindowPos", side_effect=lambda *a: calls.append(a) or real(*a)), \
                self.assertNoLogs("saylight.win32", "DEBUG"):
            win32.set_topmost(self.hwnd)
        flags = win32.SWP_NOMOVE | win32.SWP_NOSIZE | win32.SWP_NOACTIVATE
        self.assertEqual(calls, [(self.hwnd, win32.HWND_TOPMOST, 0, 0, 0, 0, flags)])
        self.assertFalse(_u.IsWindowVisible(self.hwnd))

    def test_topmost_failure_is_logged_not_raised(self):
        with self.assertLogs("saylight.win32", "DEBUG"):
            win32.set_topmost(0x7FFF0001)  # not a window

    def test_move_and_rect(self):
        win32.move_window(self.hwnd, 123, 45)
        left, top, right, bottom = win32.get_window_rect(self.hwnd)
        self.assertEqual((left, top), (123, 45))
        self.assertFalse(_u.IsWindowVisible(self.hwnd))

    def test_rect_of_missing_window_raises(self):
        with self.assertRaises(OSError):
            win32.get_window_rect(0x7FFF0001)

    def test_cursor_pos(self):
        x, y = win32.get_cursor_pos()
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)


class SystemStateTests(unittest.TestCase):
    def test_notification_state(self):
        self.assertIn(win32.query_user_notification_state(), range(1, 8))

    def test_flags(self):
        self.assertIsInstance(win32.animations_enabled(), bool)
        self.assertIsInstance(win32.high_contrast(), bool)
        self.assertGreater(win32.small_icon_size(), 0)

    def test_caret_width(self):
        width = win32.caret_width_px()
        self.assertIsInstance(width, int)
        self.assertGreaterEqual(width, 1)

    def test_caret_width_reads_spi_getcaretwidth_and_falls_back_to_1(self):
        calls = []

        def fake_spi(action, ui_param, pv_param, flags, width=4):
            calls.append((action, ui_param, flags))
            ctypes.cast(pv_param, ctypes.POINTER(wintypes.DWORD))[0] = width
            return True

        with mock.patch.object(win32.user32, "SystemParametersInfoW", fake_spi):
            self.assertEqual(win32.caret_width_px(), 4)
        self.assertEqual(calls, [(0x2006, 0, 0)])
        with mock.patch.object(win32.user32, "SystemParametersInfoW", return_value=False):
            self.assertEqual(win32.caret_width_px(), 1)
        with mock.patch.object(win32.user32, "SystemParametersInfoW",
                               lambda a, u, pv, f: fake_spi(a, u, pv, f, width=0)):
            self.assertEqual(win32.caret_width_px(), 1)

    def test_sys_colors(self):
        self.assertEqual((win32.COLOR_WINDOW, win32.COLOR_WINDOWTEXT, win32.COLOR_HIGHLIGHT,
                          win32.COLOR_HIGHLIGHTTEXT, win32.COLOR_GRAYTEXT), (5, 8, 13, 14, 17))
        rgb = win32.sys_color_rgb(win32.COLOR_HIGHLIGHT)
        self.assertEqual(len(rgb), 3)
        self.assertTrue(all(0 <= c <= 255 for c in rgb))
        with mock.patch.object(win32.user32, "GetSysColor", return_value=0x00332211):
            self.assertEqual(win32.sys_color_rgb(5), (0x11, 0x22, 0x33))

    def test_message_beep_maps_kinds_without_sound(self):
        with mock.patch.object(win32, "user32") as fake:
            win32.message_beep("ok")
            win32.message_beep("error")
            self.assertEqual(fake.MessageBeep.call_args_list, [mock.call(0x0), mock.call(0x10)])
            with self.assertRaises(ValueError):
                win32.message_beep("loud")


class RegistryTests(unittest.TestCase):
    def test_real_values_have_the_right_types(self):
        self.assertTrue(1.0 <= win32.text_scale_factor() <= 2.25)
        for fn in (win32.apps_use_light_theme, win32.system_uses_light_theme, win32.color_prevalence):
            self.assertIsInstance(fn(), bool)
        palette = win32.accent_palette()
        if palette is not None:
            self.assertEqual(len(palette), 8)
            self.assertTrue(all(len(c) == 3 and all(0 <= v <= 255 for v in c) for c in palette))

    def test_text_scale_clamp(self):
        for raw, expected in ((None, 1.0), ("x", 1.0), (50, 1.0), (100, 1.0), (150, 1.5), (225, 2.25), (300, 2.25)):
            with mock.patch.object(win32, "_reg_value", return_value=raw):
                self.assertEqual(win32.text_scale_factor(), expected, raw)

    def test_defaults_when_unreadable(self):
        with mock.patch.object(win32.winreg, "OpenKey", side_effect=OSError):
            self.assertTrue(win32.apps_use_light_theme())
            self.assertTrue(win32.system_uses_light_theme())
            self.assertFalse(win32.color_prevalence())
            self.assertIsNone(win32.accent_palette())
            self.assertEqual(win32.text_scale_factor(), 1.0)

    def test_registry_is_opened_read_only(self):
        with mock.patch.object(win32.winreg, "OpenKey", side_effect=OSError) as open_key:
            win32.apps_use_light_theme()
        self.assertEqual(open_key.call_args.args[2:], (0, win32.winreg.KEY_READ))

    def test_accent_palette_parsing(self):
        raw = bytes(range(32))
        with mock.patch.object(win32, "_reg_value", return_value=raw):
            palette = win32.accent_palette()
        self.assertEqual(palette[0], (0, 1, 2))
        self.assertEqual(palette[1], (4, 5, 6))  # Light2
        self.assertEqual(palette[4], (16, 17, 18))  # Dark1
        with mock.patch.object(win32, "_reg_value", return_value=b"short"):
            self.assertIsNone(win32.accent_palette())


class HotkeyAndThreadTests(unittest.TestCase):
    def test_register_and_unregister_on_this_thread(self):
        mods, vk = win32.parse_hotkey("Ctrl+Alt+Shift+F21")
        with CrossProcessLock("hotkeys"):
            self.assertTrue(win32.register_hotkey(901, mods | win32.MOD_NOREPEAT, vk))
            try:
                self.assertFalse(win32.register_hotkey(902, mods | win32.MOD_NOREPEAT, vk))  # same chord: taken
                self.assertEqual(win32.last_error(), 1409)  # ERROR_HOTKEY_ALREADY_REGISTERED
            finally:
                self.assertTrue(win32.unregister_hotkey(901))
            self.assertFalse(win32.unregister_hotkey(901))

    def test_key_down_is_bool(self):
        self.assertIsInstance(win32.key_down(win32.VK_F24), bool)

    def test_send_keys_builds_press_then_reverse_release(self):
        seen = []
        with mock.patch.object(win32, "send_key_events", side_effect=lambda ev: seen.extend(ev) or True):
            self.assertTrue(win32.send_keys([win32.VK_CONTROL, win32.VK_C]))
        self.assertEqual(seen, [(win32.VK_CONTROL, True), (win32.VK_C, True),
                                (win32.VK_C, False), (win32.VK_CONTROL, False)])

    @staticmethod
    def _partial_sends(first_count: int) -> tuple[list, object]:
        """A fake _send_batch that inserts only first_count events of the first batch, then everything."""
        batches: list = []

        def fake(events):
            batches.append(list(events))
            return first_count if len(batches) == 1 else len(events)

        return batches, fake

    def test_partial_send_input_releases_the_keys_it_pressed(self):
        batches, fake = self._partial_sends(2)  # only Ctrl down and C down got in
        with mock.patch.object(win32, "_send_batch", fake), self.assertLogs("saylight.win32", "WARNING"):
            self.assertFalse(win32.send_keys([win32.VK_CONTROL, win32.VK_C]))
        self.assertEqual(batches[1], [(win32.VK_C, False), (win32.VK_CONTROL, False)])

    def test_partial_send_input_after_a_release_releases_only_what_is_still_down(self):
        batches, fake = self._partial_sends(3)  # Ctrl down, C down, C up got in
        with mock.patch.object(win32, "_send_batch", fake), self.assertLogs("saylight.win32", "WARNING"):
            self.assertFalse(win32.send_keys([win32.VK_CONTROL, win32.VK_C]))
        self.assertEqual(batches[1], [(win32.VK_CONTROL, False)])

    def test_send_input_blocked_sends_nothing_more(self):
        batches, fake = self._partial_sends(0)
        with mock.patch.object(win32, "_send_batch", fake), self.assertLogs("saylight.win32", "WARNING"):
            self.assertFalse(win32.send_keys([win32.VK_CONTROL, win32.VK_C]))
        self.assertEqual(len(batches), 1)  # no key-ups for keys that were never pressed

    def test_send_batch_builds_keyboard_inputs(self):
        seen = {}

        def fake_send_input(n, batch, size):
            seen.update(n=n, size=size, items=[(batch[i].type, batch[i].u.ki.wVk, batch[i].u.ki.dwFlags)
                                               for i in range(n)])
            return n

        with mock.patch.object(win32.user32, "SendInput", fake_send_input):
            self.assertTrue(win32.send_keys([win32.VK_CONTROL, win32.VK_C]))
        down, up = 0, win32.KEYEVENTF_KEYUP
        self.assertEqual(seen, {"n": 4, "size": 40, "items": [(1, win32.VK_CONTROL, down), (1, win32.VK_C, down),
                                                               (1, win32.VK_C, up), (1, win32.VK_CONTROL, up)]})

    def test_post_to_no_thread_fails(self):
        self.assertFalse(win32.post_thread_message(0, win32.WM_APP))

    def test_events(self):
        h = win32.create_event()
        self.assertTrue(h)
        self.assertTrue(win32.set_event(h))
        self.assertTrue(win32.close_handle(h))
        self.assertFalse(win32.close_handle(0))

    def test_wait_pumping_sent_waits(self):
        import time
        start = time.monotonic()
        win32.wait_pumping_sent(0.05)
        self.assertGreaterEqual(time.monotonic() - start, 0.045)


if __name__ == "__main__":
    unittest.main()

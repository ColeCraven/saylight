"""Tests for clipboard.py.

- Pure decision functions, and the whole copy job driven by a fake system with a simulated clock.
- Real clipboard I/O in a child process moved into a private window station, which has its own
  clipboard: the user's clipboard is never read or written, and no keys are sent anywhere. The child
  refuses to write unless it sees an empty clipboard with a different sequence number from ours.
- A round trip on the real clipboard, only when it is completely empty (otherwise skipped), always
  emptied again afterwards.
"""

import contextlib
import ctypes
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from saylight import clipboard, win32
from saylight.clipboard import Copied, Saved
from saylight.tests.test_win32 import CrossProcessLock

ROOT = Path(__file__).resolve().parents[2]
APP, OTHER, SHELL, OWN, TERMINAL = 1001, 1002, 2001, 3001, 4001
CTRL, SHIFT, ALT, C, BACKTICK = win32.VK_CONTROL, win32.VK_SHIFT, win32.VK_MENU, win32.VK_C, win32.VK_OEM_3
VSCODE_LINE = b'{"version":1,"isFromEmptySelection":true,"multicursorText":null,"mode":"python"}'
VSCODE_SELECTION = b'{"version":1,"isFromEmptySelection":false,"multicursorText":null,"mode":"python"}'


def chromium_pickle(entries: dict[str, str]) -> bytes:
    """Chromium's custom-data pickle: payload size, count, then UTF-16 (type, value) pairs padded to 4 bytes."""
    body = struct.pack("<I", len(entries))
    for key, value in entries.items():
        for s in (key, value):
            data = s.encode("utf-16-le")
            body += struct.pack("<i", len(s)) + data + b"\0" * (-len(data) % 4)
    return struct.pack("<I", len(body)) + body


# --- Pure functions ------------------------------------------------------------------

class PureFunctionTests(unittest.TestCase):
    def test_copy_keys(self):
        self.assertEqual(clipboard.copy_keys(False), [CTRL, C])
        self.assertEqual(clipboard.copy_keys(True), [CTRL, SHIFT, C])

    def test_keys_to_release(self):
        self.assertEqual(clipboard.keys_to_release([BACKTICK, CTRL]),
                         [BACKTICK, CTRL, SHIFT, ALT, win32.VK_LWIN, win32.VK_RWIN])
        self.assertEqual(clipboard.keys_to_release([]), list(clipboard.MODIFIER_VKS))

    def test_vscode_json(self):
        self.assertTrue(clipboard.vscode_json_is_line_copy(VSCODE_LINE))
        self.assertTrue(clipboard.vscode_json_is_line_copy(VSCODE_LINE + b"\0\xff\xfegarbage"))
        self.assertTrue(clipboard.vscode_json_is_line_copy(b'{"isFromEmptySelection" : true, "mode": '))  # cut off
        self.assertFalse(clipboard.vscode_json_is_line_copy(VSCODE_SELECTION))
        self.assertFalse(clipboard.vscode_json_is_line_copy(b'{"version":1}'))
        self.assertFalse(clipboard.vscode_json_is_line_copy(b'{"isFromEmptySelection":"true"}'))
        self.assertFalse(clipboard.vscode_json_is_line_copy(b"[true]"))
        self.assertFalse(clipboard.vscode_json_is_line_copy(b""))
        self.assertFalse(clipboard.vscode_json_is_line_copy(b"\xff\xfe\x00"))

    def test_chromium_custom_data(self):
        line, sel = VSCODE_LINE.decode(), VSCODE_SELECTION.decode()
        self.assertTrue(clipboard.chromium_custom_is_line_copy(chromium_pickle({"vscode-editor-data": line})))
        self.assertTrue(clipboard.chromium_custom_is_line_copy(
            chromium_pickle({"text/x-other": "{}", "vscode-editor-data": line})))
        self.assertFalse(clipboard.chromium_custom_is_line_copy(chromium_pickle({"vscode-editor-data": sel})))
        self.assertFalse(clipboard.chromium_custom_is_line_copy(chromium_pickle({"text/x-other": line})))
        self.assertFalse(clipboard.chromium_custom_is_line_copy(
            chromium_pickle({"vscode-editor-data": sel, "text/x-other": line})))
        cut_before_flag = chromium_pickle({"vscode-editor-data": line})[:70]
        self.assertFalse(clipboard.chromium_custom_is_line_copy(cut_before_flag))
        self.assertTrue(clipboard.chromium_custom_is_line_copy(chromium_pickle({"vscode-editor-data": line})[:-20]))
        self.assertFalse(clipboard.chromium_custom_is_line_copy(b"\x01\x02\x03"))

    def test_decide(self):
        self.assertEqual(clipboard.decide(Copied("Hello.", False)), ("Hello.", "ok"))
        self.assertEqual(clipboard.decide(Copied("  Hello.\r\n", False)), ("  Hello.\r\n", "ok"))
        self.assertEqual(clipboard.decide(Copied(" \r\n\t", False)), ("", "empty"))
        self.assertEqual(clipboard.decide(Copied(None, False)), ("", "empty"))
        self.assertEqual(clipboard.decide(Copied("whole line\r\n", True)), ("", "line_copy"))
        self.assertEqual(clipboard.REASONS, ("ok", "empty", "line_copy", "console", "shell", "busy"))


class RestoreLogicTests(unittest.TestCase):
    def setUp(self):
        self.set_text = mock.patch.object(clipboard, "set_text", return_value=True).start()
        self.empty = mock.patch.object(clipboard, "empty_clipboard", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def test_previous_text_is_put_back_outside_history(self):
        self.assertTrue(clipboard._restore(Saved("prev", False)))
        self.set_text.assert_called_once_with("prev", history=False)
        self.empty.assert_not_called()

    def test_empty_string_text_is_still_text(self):
        clipboard._restore(Saved("", False))
        self.set_text.assert_called_once_with("", history=False)

    def test_empty_clipboard_is_emptied_again(self):
        clipboard._restore(Saved(None, True))
        self.empty.assert_called_once_with()
        self.set_text.assert_not_called()

    def test_non_text_content_is_left_alone(self):
        self.assertTrue(clipboard._restore(Saved(None, False)))
        self.set_text.assert_not_called()
        self.empty.assert_not_called()


# --- The copy job with a fake system ----------------------------------------------------

class FakeSystem:
    """Fake windows, keyboard and clipboard on a simulated clock (sleep advances it)."""

    def __init__(self, *, fg=APP, terminals=(), saved=Saved("prev", False), copied=Copied("sel", False),
                 replies=True, reply_delay=0.04, send_ok=True, held=(), held_for=0.0, set_fg_works=True,
                 restore_ok=True):
        self.apps, self.own = {APP, OTHER, TERMINAL}, {OWN}
        self.t, self.fg, self.terminals = 0.0, fg, set(terminals)
        self.saved, self.copied, self.replies, self.reply_delay = saved, copied, replies, reply_delay
        self.send_ok, self.held, self.held_for = send_ok, set(held), held_for
        self.set_fg_works, self.restore_ok = set_fg_works, restore_ok
        self.seq, self.replies_at, self.sent_at = 500, [], None
        self.sent, self.restored, self.calls, self.sleeps = [], [], [], []

    def foreground(self):
        return self.fg

    def is_app(self, hwnd):
        return hwnd in self.apps

    def is_own(self, hwnd):
        return hwnd in self.own

    def is_terminal(self, hwnd):
        return hwnd in self.terminals

    def set_foreground(self, hwnd):
        self.calls.append(("set_foreground", hwnd))
        if self.set_fg_works:
            self.fg = hwnd
        return self.set_fg_works

    def key_down(self, vk):
        return vk in self.held and self.t < self.held_for

    def send_keys(self, vks):
        self.sent.append(list(vks))
        self.sent_at = self.t
        if self.replies and self.send_ok:
            self.replies_at.append(self.t + self.reply_delay)
        return self.send_ok

    def sequence(self):
        """Each reply (the app's copy) that has landed by now moves the sequence number on."""
        return self.seq + 3 * sum(self.t >= at for at in self.replies_at)

    def save(self):
        self.calls.append("save")
        return self.saved

    def read(self):
        self.calls.append("read")
        if isinstance(self.copied, Exception):
            raise self.copied
        return self.copied

    def restore(self, saved):
        self.calls.append("restore")
        self.restored.append(saved)
        return self.restore_ok

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.t += seconds


def run(system, *, is_click=False, last_app=0, key_vks=(BACKTICK,), timeout_s=0.35):
    return clipboard._copy(system, is_click=is_click, last_app=last_app, key_vks=list(key_vks), timeout_s=timeout_s)


def forget_late_copy(test: unittest.TestCase) -> None:
    """The late-copy watch is per thread; start and end each test without one."""
    clipboard._tls.late = None
    test.addCleanup(setattr, clipboard._tls, "late", None)


class CopyJobTests(unittest.TestCase):
    def setUp(self):
        forget_late_copy(self)

    def test_hotkey_copies_then_restores_previous_text(self):
        s = FakeSystem()
        self.assertEqual(run(s), ("sel", "ok"))
        self.assertEqual(s.sent, [[CTRL, C]])
        self.assertEqual(s.restored, [Saved("prev", False)])
        self.assertEqual(s.calls, ["save", "read", "restore"])
        self.assertIn(clipboard.SETTLE_S, s.sleeps)
        self.assertLess(s.t, 0.2)

    def test_hotkey_in_terminal_sends_ctrl_shift_c(self):
        s = FakeSystem(fg=TERMINAL, terminals={TERMINAL})
        self.assertEqual(run(s), ("sel", "ok"))
        self.assertEqual(s.sent, [[CTRL, SHIFT, C]])

    def test_click_in_terminal_is_refused(self):
        s = FakeSystem(fg=TERMINAL, terminals={TERMINAL})
        self.assertEqual(run(s, is_click=True, last_app=APP), ("", "console"))
        self.assertEqual((s.sent, s.restored, s.calls), ([], [], []))

    def test_click_with_an_app_in_front_leaves_the_foreground_alone(self):
        s = FakeSystem(fg=APP)
        self.assertEqual(run(s, is_click=True, last_app=OTHER, key_vks=()), ("sel", "ok"))
        self.assertNotIn(("set_foreground", OTHER), s.calls)

    def test_click_from_the_shell_restores_last_app_first(self):
        s = FakeSystem(fg=SHELL)
        self.assertEqual(run(s, is_click=True, last_app=APP, key_vks=()), ("sel", "ok"))
        self.assertEqual(s.calls[0], ("set_foreground", APP))
        self.assertEqual(s.sleeps[0], clipboard.FOREGROUND_RESTORE_S)
        self.assertEqual(s.sent, [[CTRL, C]])

    def test_click_from_our_own_window_restores_last_app(self):
        s = FakeSystem(fg=OWN)
        self.assertEqual(run(s, is_click=True, last_app=OTHER, key_vks=()), ("sel", "ok"))
        self.assertEqual(s.calls[0], ("set_foreground", OTHER))

    def test_click_restore_refused_is_shell(self):
        s = FakeSystem(fg=SHELL, set_fg_works=False)
        self.assertEqual(run(s, is_click=True, last_app=APP, key_vks=()), ("", "shell"))
        self.assertEqual((s.sent, s.restored), ([], []))

    def test_click_without_a_live_last_app_is_shell(self):
        for last_app in (0, 9999, SHELL):
            s = FakeSystem(fg=SHELL)
            self.assertEqual(run(s, is_click=True, last_app=last_app, key_vks=()), ("", "shell"))
            self.assertEqual((s.calls, s.sent), ([], []))

    def test_click_restored_into_a_terminal_is_console(self):
        s = FakeSystem(fg=SHELL, terminals={TERMINAL})
        self.assertEqual(run(s, is_click=True, last_app=TERMINAL, key_vks=()), ("", "console"))
        self.assertEqual(s.sent, [])

    def test_hotkey_with_our_own_window_in_front_sends_nothing(self):
        s = FakeSystem(fg=OWN, held={BACKTICK, CTRL}, held_for=0.2)
        self.assertEqual(run(s, last_app=APP), ("", "empty"))
        self.assertEqual((s.sent, s.calls, s.restored, s.sleeps), ([], [], [], []))  # clipboard never touched
        self.assertEqual(s.t, 0.0)  # and no waiting for the keys to be let go

    def test_hotkey_with_our_own_window_in_front_leaves_a_late_copy_watch_alone(self):
        late = clipboard._LateCopy(500, Saved("prev", False), 0.5)
        clipboard._tls.late = late
        s = FakeSystem(fg=OWN)
        self.assertEqual(run(s), ("", "empty"))
        self.assertIs(clipboard._tls.late, late)  # the input thread's loop still watches it
        self.assertEqual(s.restored, [])

    def test_click_from_our_own_window_is_unchanged_by_the_hotkey_guard(self):
        s = FakeSystem(fg=OWN)
        self.assertEqual(run(s, is_click=True, last_app=APP, key_vks=()), ("sel", "ok"))
        self.assertEqual(s.calls[0], ("set_foreground", APP))
        self.assertEqual(s.sent, [[CTRL, C]])
        self.assertEqual(s.restored, [Saved("prev", False)])

    def test_real_system_recognises_own_windows(self):
        with mock.patch.object(win32, "is_own_window", side_effect=lambda h: h == OWN) as is_own:
            self.assertTrue(clipboard._System().is_own(OWN))
            self.assertFalse(clipboard._System().is_own(APP))
        self.assertEqual(is_own.call_count, 2)

    def test_hotkey_with_the_shell_in_front_does_not_restore(self):
        s = FakeSystem(fg=SHELL)
        self.assertEqual(run(s, last_app=APP), ("sel", "ok"))
        self.assertNotIn(("set_foreground", APP), s.calls)
        self.assertEqual(s.sent, [[CTRL, C]])

    def test_nothing_selected_times_out_without_touching_the_clipboard(self):
        s = FakeSystem(replies=False)
        self.assertEqual(run(s), ("", "empty"))
        self.assertEqual(s.restored, [])
        self.assertNotIn("read", s.calls)
        self.assertAlmostEqual(s.t, 0.35, delta=clipboard.SEQUENCE_POLL_S + 1e-9)

    def test_timeout_is_configurable(self):
        s = FakeSystem(replies=False)
        run(s, timeout_s=1.0)
        self.assertAlmostEqual(s.t, 1.0, delta=clipboard.SEQUENCE_POLL_S + 1e-9)

    def test_a_slow_app_inside_the_timeout_still_counts(self):
        s = FakeSystem(reply_delay=0.30)
        self.assertEqual(run(s), ("sel", "ok"))

    def test_busy_before_copying_sends_nothing(self):
        s = FakeSystem(saved=None)
        self.assertEqual(run(s), ("", "busy"))
        self.assertEqual((s.sent, s.restored), ([], []))

    def test_busy_after_copying_still_restores(self):
        s = FakeSystem(copied=None)
        self.assertEqual(run(s), ("", "busy"))
        self.assertEqual(s.restored, [Saved("prev", False)])

    def test_line_copy_is_not_read_and_is_restored(self):
        s = FakeSystem(copied=Copied("    return x\r\n", True))
        self.assertEqual(run(s), ("", "line_copy"))
        self.assertEqual(s.restored, [Saved("prev", False)])

    def test_whitespace_or_non_text_copy_is_empty_and_restored(self):
        for copied in (Copied(" \r\n", False), Copied(None, False)):
            s = FakeSystem(copied=copied)
            self.assertEqual(run(s), ("", "empty"))
            self.assertEqual(len(s.restored), 1)

    def test_restore_receives_what_was_saved(self):
        for saved in (Saved(None, True), Saved(None, False), Saved("", False)):
            s = FakeSystem(saved=saved)
            run(s)
            self.assertEqual(s.restored, [saved])

    def test_send_failure_is_empty_without_waiting(self):
        s = FakeSystem(send_ok=False)
        self.assertEqual(run(s), ("", "empty"))
        self.assertEqual(s.t, 0.0)

    def test_waits_for_the_hotkey_to_be_released(self):
        s = FakeSystem(held={BACKTICK, CTRL}, held_for=0.2)
        self.assertEqual(run(s), ("sel", "ok"))
        self.assertGreaterEqual(s.sent_at, 0.2)
        self.assertLess(s.sent_at, 0.2 + 2 * clipboard.KEY_POLL_S)

    def test_gives_up_waiting_after_1_5_s(self):
        s = FakeSystem(held={CTRL}, held_for=99)
        with self.assertLogs("saylight.clipboard", "INFO"):
            self.assertEqual(run(s), ("sel", "ok"))
        self.assertAlmostEqual(s.sent_at, clipboard.KEY_RELEASE_WAIT_S, delta=clipboard.KEY_POLL_S + 1e-9)

    def test_click_waits_for_modifiers_too(self):
        s = FakeSystem(held={SHIFT}, held_for=0.1)
        run(s, is_click=True, last_app=APP, key_vks=())
        self.assertGreaterEqual(s.sent_at, 0.1)

    def test_failed_restore_is_logged(self):
        s = FakeSystem(restore_ok=False)
        with self.assertLogs("saylight.clipboard", "WARNING") as logs:
            self.assertEqual(run(s), ("sel", "ok"))
        self.assertIn("restore", logs.output[0])

    def test_restore_runs_even_if_reading_raises(self):
        s = FakeSystem(copied=OSError("boom"))
        with self.assertRaises(OSError):
            run(s)
        self.assertEqual(s.restored, [Saved("prev", False)])

    def test_public_function_logs_lengths_not_text(self):
        with mock.patch.object(clipboard, "_copy", return_value=("my secret thesis sentence", "ok")), \
                self.assertLogs("saylight.clipboard", "INFO") as logs:
            result = clipboard.copy_selection(is_click=False, last_app=0, key_vks=[BACKTICK])
        self.assertEqual(result, ("my secret thesis sentence", "ok"))
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertIn("25 chars", logs.output[0])


class LateCopyTests(unittest.TestCase):
    """A slow app answers Ctrl+C after the 0.35 s timeout: the user's clipboard must still be put back."""

    def setUp(self):
        forget_late_copy(self)

    @staticmethod
    def watch() -> clipboard._LateCopy | None:
        return getattr(clipboard._tls, "late", None)

    def test_a_late_copy_is_undone(self):
        s = FakeSystem(reply_delay=0.5)
        self.assertEqual(run(s), ("", "empty"))
        self.assertEqual(s.restored, [])
        self.assertAlmostEqual(self.watch().deadline, 0.35 + clipboard.LATE_COPY_WATCH_S)
        s.t = 0.45  # the input thread's loop polls: nothing yet
        self.assertAlmostEqual(clipboard._check_late_copy(s), self.watch().deadline - 0.45)
        s.t = 0.52  # the app's copy has landed
        with self.assertLogs("saylight.clipboard", "INFO") as logs:
            self.assertIsNone(clipboard._check_late_copy(s))
        self.assertEqual(s.restored, [Saved("prev", False)])
        self.assertEqual(s.sleeps[-1], clipboard.SETTLE_S)  # let the app finish writing first
        self.assertIn("put back", logs.output[0])
        self.assertIsNone(clipboard._check_late_copy(s))  # done: no second restore
        self.assertEqual(len(s.restored), 1)

    def test_the_watch_ends_without_touching_the_clipboard(self):
        s = FakeSystem(replies=False)
        run(s)
        s.t = self.watch().deadline
        self.assertIsNone(clipboard._check_late_copy(s))
        self.assertIsNone(self.watch())
        s.replies_at.append(0.0)  # a change after the watch ended is someone else's
        self.assertIsNone(clipboard._check_late_copy(s))
        self.assertEqual(s.restored, [])

    def test_a_change_first_seen_long_after_the_watch_is_not_undone(self):
        s = FakeSystem(replies=False)
        run(s)
        s.replies_at.append(0.0)
        s.t = self.watch().deadline + clipboard.LATE_COPY_GRACE_S + 0.01
        self.assertIsNone(clipboard._check_late_copy(s))
        self.assertEqual(s.restored, [])

    def test_only_a_timeout_starts_a_watch(self):
        for s in (FakeSystem(), FakeSystem(send_ok=False), FakeSystem(saved=None), FakeSystem(copied=None),
                  FakeSystem(fg=TERMINAL, terminals={TERMINAL})):
            run(s)
            self.assertIsNone(self.watch())
        run(FakeSystem(replies=False))
        self.assertIsNotNone(self.watch())

    def test_the_next_job_undoes_a_late_copy_before_saving(self):
        s = FakeSystem(reply_delay=0.5)
        run(s)
        s.t, s.reply_delay = 0.6, 0.04  # the late copy landed; the next press came before the loop noticed
        self.assertEqual(run(s), ("sel", "ok"))
        self.assertEqual(s.calls, ["save", "restore", "save", "read", "restore"])
        self.assertIsNone(self.watch())

    def test_the_next_job_ends_an_unchanged_watch(self):
        s = FakeSystem(replies=False)
        run(s)
        s.t, s.replies = 0.5, True
        self.assertEqual(run(s), ("sel", "ok"))
        self.assertIsNone(self.watch())  # this job's own copy isn't "late"
        self.assertEqual(s.calls, ["save", "save", "read", "restore"])

    def test_a_failed_late_restore_is_logged(self):
        s = FakeSystem(reply_delay=0.5, restore_ok=False)
        run(s)
        s.t = 0.6
        with self.assertLogs("saylight.clipboard", "WARNING"):
            self.assertIsNone(clipboard._check_late_copy(s))

    def test_watch_late_copy_never_raises(self):
        class Broken(clipboard._System):
            def sequence(self):
                raise OSError("boom")

        clipboard._tls.late = clipboard._LateCopy(1, Saved("prev", False), time.monotonic() + 1)
        with mock.patch.object(clipboard, "_System", Broken), self.assertLogs("saylight.clipboard", "ERROR"):
            self.assertIsNone(clipboard.watch_late_copy())
        self.assertIsNone(clipboard._tls.late)

    def test_the_watch_is_per_thread(self):
        clipboard._tls.late = clipboard._LateCopy(1, Saved("prev", False), time.monotonic() + 1)
        seen = []
        t = threading.Thread(target=lambda: seen.append(getattr(clipboard._tls, "late", None)))
        t.start()
        t.join()
        self.assertEqual(seen, [None])


class SaveTests(unittest.TestCase):
    """_save() decides whether the copy job may overwrite the clipboard at all."""

    def save(self, *, available, opened=True, text=None, count=1):
        @contextlib.contextmanager
        def fake_opened():
            yield opened

        with mock.patch.object(clipboard, "_available", side_effect=available), \
                mock.patch.object(clipboard, "_opened", fake_opened), \
                mock.patch.object(clipboard, "_read_text", return_value=text), \
                mock.patch.object(win32.user32, "CountClipboardFormats", return_value=count):
            return clipboard._save()

    def test_readable_text(self):
        self.assertEqual(self.save(available=[True], text="prev"), Saved("prev", False))

    def test_text_that_cannot_be_read_is_busy(self):
        self.assertIsNone(self.save(available=[True, True], text=None))

    def test_clipboard_that_cannot_be_opened_is_busy(self):
        self.assertIsNone(self.save(available=[True], opened=False))

    def test_text_that_went_away_meanwhile(self):
        self.assertEqual(self.save(available=[True, False], text=None, count=0), Saved(None, True))
        self.assertEqual(self.save(available=[True, False], text=None, count=2), Saved(None, False))

    def test_no_text(self):
        self.assertEqual(self.save(available=[False], count=0), Saved(None, True))
        self.assertEqual(self.save(available=[False], count=3), Saved(None, False))


class ClipboardOwnerTests(unittest.TestCase):
    def test_create_and_destroy_on_this_thread(self):
        with clipboard.ClipboardOwner() as owner:
            self.assertTrue(win32.is_window(owner.hwnd))
            self.assertEqual(clipboard._owner(), owner.hwnd)
            hwnd = owner.hwnd
        self.assertFalse(win32.is_window(hwnd))
        self.assertEqual(clipboard._owner(), 0)

    def test_owner_is_per_thread(self):
        seen = []
        with clipboard.ClipboardOwner():
            t = threading.Thread(target=lambda: seen.append(clipboard._owner()))
            t.start()
            t.join()
        self.assertEqual(seen, [0])


# --- Real clipboard I/O in a private window station -----------------------------------------

class SandboxTests(unittest.TestCase):
    """Runs the child below once; each test checks one scenario it ran against a real (private) clipboard."""

    results: dict = {}

    @classmethod
    def setUpClass(cls):
        seq = win32.user32.GetClipboardSequenceNumber()
        with CrossProcessLock("clipboard-sandbox"):  # the service station's clipboard is shared by concurrent runs
            proc = subprocess.run([sys.executable, "-m", "saylight.tests.test_clipboard", "--sandbox", str(seq)],
                                  cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise AssertionError(f"sandbox child failed ({proc.returncode}):\n{proc.stderr[-3000:]}")
        cls.results = json.loads(proc.stdout.strip().splitlines()[-1])
        cls.stderr = proc.stderr

    def setUp(self):
        if not self.results.get("isolated"):
            self.skipTest(f"no private clipboard ({self.results.get('why_not')}); real-clipboard sandbox tests skipped")

    def scenario(self, name):
        result = self.results[name]
        if isinstance(result, str):
            self.fail(f"{name} raised in the child: {result}")
        return result

    def test_text_round_trips_exactly(self):
        for name in ("roundtrip", "surrogate", "large"):
            self.assertEqual(self.scenario(name), [True, True], name)

    def test_history_opt_out_formats(self):
        self.assertEqual(self.scenario("history_off"), [True, True])
        self.assertEqual(self.scenario("history_on"), [False, False])

    def test_empty(self):
        self.assertEqual(self.scenario("empty"), [True, True, True])

    def test_copy_restores_previous_text_quickly_while_we_own_the_clipboard(self):
        r = self.scenario("ok_owned")
        self.assertEqual(r["result"], ["Selected text ✓ \U0001F600", "ok"])
        self.assertEqual(r["after"], "previous ✓")
        self.assertEqual(r["sent"], [[CTRL, C]])
        self.assertLess(r["elapsed"], 0.3)  # the app's EmptyClipboard wasn't blocked on our owner window

    def test_terminal_and_console(self):
        self.assertEqual(self.scenario("terminal_keys")["sent"], [[CTRL, SHIFT, C]])
        self.assertEqual(self.scenario("terminal_keys")["result"][1], "ok")
        r = self.scenario("click_console")
        self.assertEqual((r["result"], r["sent"], r["after"]), (["", "console"], [], "previous ✓"))

    def test_line_copy_markers(self):
        for name in ("vscode_json", "chromium", "vs_tag", "scintilla_tag"):
            r = self.scenario(name)
            self.assertEqual(r["result"], ["", "line_copy"], name)
            self.assertEqual(r["after"], "previous ✓", name)

    def test_vscode_with_a_selection_is_ok(self):
        self.assertEqual(self.scenario("vscode_json_selection")["result"], ["def f():", "ok"])

    def test_empty_results_restore(self):
        for name in ("whitespace", "no_text_copied"):
            r = self.scenario(name)
            self.assertEqual((r["result"], r["after"]), (["", "empty"], "previous ✓"), name)

    def test_previously_empty_clipboard_is_emptied_again(self):
        r = self.scenario("prev_empty")
        self.assertEqual((r["result"], r["after"], r["count"]), (["fresh", "ok"], None, 0))

    def test_previous_non_text_is_left_as_copied(self):
        r = self.scenario("prev_non_text")
        self.assertEqual((r["result"], r["after"]), (["fresh", "ok"], "fresh"))

    def test_timeout(self):
        r = self.scenario("timeout")
        self.assertEqual((r["result"], r["after"]), (["", "empty"], "previous ✓"))
        self.assertGreaterEqual(r["elapsed"], 0.34)
        self.assertLess(r["elapsed"], 0.6)

    def test_a_copy_that_lands_after_the_timeout_is_undone(self):
        r = self.scenario("late_copy_watched")
        self.assertEqual(r["result"], ["", "empty"])
        self.assertLess(r["elapsed"], 0.5)  # the result didn't wait for the slow app
        self.assertTrue(r["app_wrote"])
        self.assertGreater(r["polls"], 0)
        self.assertEqual(r["after"], "previous ✓")

    def test_control_without_the_watch_the_late_copy_stays(self):
        r = self.scenario("late_copy_unwatched")
        self.assertEqual((r["result"], r["app_wrote"], r["after"]), (["", "empty"], True, "landed late"))

    def test_busy_clipboard_sends_nothing(self):
        r = self.scenario("busy")
        self.assertEqual((r["result"], r["sent"], r["after"]), (["", "busy"], [], "previous ✓"))
        self.assertGreaterEqual(r["elapsed"], clipboard.OPEN_TIMEOUT_S)
        self.assertLess(r["elapsed"], clipboard.OPEN_TIMEOUT_S + 0.2)

    def test_real_click_path_with_nothing_in_front_is_shell(self):
        r = self.scenario("click_shell_real")
        if r == "skipped":
            self.skipTest("the sandbox reported a foreground window")
        self.assertEqual(r["result"], ["", "shell"])

    def test_data_outlives_the_owner_window(self):
        self.assertEqual(self.scenario("owner_destroyed"), {"after": "kept ✓"})

    def test_control_without_pumping_other_apps_block(self):
        r = self.scenario("control_no_pump")
        if r["result"] != ["", "empty"]:
            self.skipTest("this Windows build doesn't block EmptyClipboard on a busy owner")
        self.assertGreaterEqual(r["elapsed"], 0.34)


# --- The real clipboard, only when it is completely empty -------------------------------

class RealClipboardTests(unittest.TestCase):
    def test_round_trip_when_empty(self):
        """Only on an empty clipboard. The final emptying happens only if, with the clipboard held open, its
        sequence number is still the one our own write left, so a copy the user makes meanwhile survives."""
        if win32.user32.CountClipboardFormats() != 0:
            self.skipTest("the clipboard isn't empty; not touching it (the sandbox tests cover the same code)")
        with clipboard.ClipboardOwner():
            ours = None
            try:
                text = "Saylight test ✓"
                self.assertTrue(clipboard.set_text(text, history=False))
                ours = win32.user32.GetClipboardSequenceNumber()
                self.assertEqual(clipboard.get_text(), text)
                self.assertEqual(clipboard._save(), Saved(text, False))
            finally:
                with clipboard._opened() as ok:
                    if ok and ours is not None and win32.user32.GetClipboardSequenceNumber() == ours:
                        win32.user32.EmptyClipboard()


# --- The sandbox child ---------------------------------------------------------------------

_su = ctypes.WinDLL("user32", use_last_error=True)
_su.CreateWindowStationW.restype = wintypes.HANDLE
_su.CreateWindowStationW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID)
_su.SetProcessWindowStation.restype = wintypes.BOOL
_su.SetProcessWindowStation.argtypes = (wintypes.HANDLE,)
_su.GetProcessWindowStation.restype = wintypes.HANDLE
_su.GetProcessWindowStation.argtypes = ()
_su.GetUserObjectInformationW.restype = wintypes.BOOL
_su.GetUserObjectInformationW.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                          wintypes.LPDWORD)
_su.CreateDesktopW.restype = wintypes.HANDLE
_su.CreateDesktopW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID)
_su.SetThreadDesktop.restype = wintypes.BOOL
_su.SetThreadDesktop.argtypes = (wintypes.HANDLE,)
WINSTA_ALL_ACCESS, GENERIC_ALL, UOI_NAME = 0x037F, 0x10000000, 2


def _station_name() -> str:
    buf = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD(0)
    ok = _su.GetUserObjectInformationW(_su.GetProcessWindowStation(), UOI_NAME, buf, ctypes.sizeof(buf),
                                       ctypes.byref(needed))
    return buf.value if ok else ""


def _enter_private_window_station(parent_seq: int) -> tuple[int, str]:
    """Move this process into a non-interactive window station, which has its own clipboard.

    A standard user can't create a named station, so this is the logon session's unnamed service station
    ("Service-0x…$"), which interactive apps never use. Returns (a desktop there, "") or (0, why not) unless
    the clipboard is provably not the user's: the station isn't WinSta0 and its sequence number differs.
    """
    station = _su.CreateWindowStationW(None, 0, WINSTA_ALL_ACCESS, None)
    if not station or not _su.SetProcessWindowStation(station):
        return 0, f"couldn't enter a window station (error {ctypes.get_last_error()})"
    name = _station_name()
    if name.lower() in ("", "winsta0"):
        return 0, f"the station is {name!r}"
    desktop = _su.CreateDesktopW("saylight_test", None, None, 0, GENERIC_ALL, None)
    if not desktop or not _su.SetThreadDesktop(desktop):
        return 0, f"couldn't enter a desktop (error {ctypes.get_last_error()})"
    if win32.user32.GetClipboardSequenceNumber() == parent_seq:
        return 0, "the station's clipboard sequence number equals the user's"
    return desktop, ""


class _FakeApp(threading.Thread):
    """A pretend app on its own thread and window that copies on request, pumping messages like a real app."""

    def __init__(self, desktop: int):
        super().__init__(daemon=True)
        self.desktop = desktop
        self.wake = win32.create_event(manual_reset=False)
        self.ready, self.done = threading.Event(), threading.Event()
        self.jobs: queue.SimpleQueue = queue.SimpleQueue()

    def copy(self, text, extras=None, delay=0.03):
        self.done.clear()
        self.jobs.put((delay, text, extras or {}))
        win32.set_event(self.wake)

    def run(self):
        _su.SetThreadDesktop(self.desktop)
        self.hwnd = win32.user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
        self.ready.set()
        handles = (wintypes.HANDLE * 1)(self.wake)
        msg = wintypes.MSG()
        while True:
            r = win32.user32.MsgWaitForMultipleObjects(1, handles, False, 200, win32.QS_ALLINPUT)
            while win32.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, win32.PM_REMOVE):
                win32.user32.DispatchMessageW(ctypes.byref(msg))
            if r == win32.WAIT_OBJECT_0:
                delay, text, extras = self.jobs.get()
                win32.wait_pumping_sent(delay)
                self._write(text, extras)
                self.done.set()

    def _write(self, text, extras):
        for _ in range(100):
            if win32.user32.OpenClipboard(self.hwnd):
                break
            win32.wait_pumping_sent(0.01)
        else:
            return
        try:
            win32.user32.EmptyClipboard()
            if text is not None:
                buf = ctypes.create_unicode_buffer(text)
                clipboard._write(clipboard.CF_UNICODETEXT, buf, ctypes.sizeof(buf))
            for name, data in extras.items():
                buf = ctypes.create_string_buffer(data, len(data))
                clipboard._write(win32.user32.RegisterClipboardFormatW(name), buf, len(data))
        finally:
            win32.user32.CloseClipboard()


class _SandboxSystem(clipboard._System):
    """Real clipboard I/O and timing; a pretend foreground app instead of real windows and keystrokes."""

    app: _FakeApp
    reply: tuple | None = None
    terminal = False
    sent: list = []

    def foreground(self):
        return APP

    def is_app(self, hwnd):
        return hwnd == APP

    def is_own(self, hwnd):
        return False

    def is_terminal(self, hwnd):
        return self.terminal

    def key_down(self, vk):
        return False

    def send_keys(self, vks):
        type(self).sent.append(list(vks))
        if self.reply is not None:
            self.app.copy(*self.reply)
        return True


def _hold_clipboard(desktop: int, seconds: float, opened: threading.Event):
    _su.SetThreadDesktop(desktop)
    if win32.user32.OpenClipboard(None):
        opened.set()
        time.sleep(seconds)
        win32.user32.CloseClipboard()


def _set_previous(prev):
    """"text": our own text (so we own the clipboard, as after a restore); None: empty; b"…": non-text only.

    Also drops a late-copy watch left by the previous scenario: here, unlike in the input thread, nothing
    polls it, and our own writes below must not count as that scenario's late copy.
    """
    clipboard._tls.late = None
    if isinstance(prev, str):
        assert clipboard.set_text(prev, history=False)
    elif prev is None:
        assert clipboard.empty_clipboard()
    else:
        with clipboard._opened() as ok:
            assert ok and win32.user32.EmptyClipboard()
            buf = ctypes.create_string_buffer(prev, len(prev))
            assert clipboard._write(win32.user32.RegisterClipboardFormatW("SaylightTestImage"), buf, len(prev))


def _sandbox_main(parent_seq: int) -> None:
    desktop, why_not = _enter_private_window_station(parent_seq)
    out: dict = {"isolated": False, "why_not": why_not}
    if not desktop:
        print(json.dumps(out))
        return
    out["isolated"] = True
    app = _FakeApp(desktop)
    app.start()
    app.ready.wait(5)
    _SandboxSystem.app = app
    real_system = clipboard._System
    clipboard._System = _SandboxSystem
    prev_text = "previous ✓"

    def scenario(name, fn):
        try:
            out[name] = fn()
        except Exception as e:  # report, don't abort the other scenarios
            out[name] = f"{type(e).__name__}: {e}"

    def copy(prev=prev_text, reply=None, *, terminal=False, click=False):
        _set_previous(prev)
        _SandboxSystem.reply, _SandboxSystem.terminal, _SandboxSystem.sent = reply, terminal, []
        start = time.monotonic()
        result = clipboard.copy_selection(is_click=click, last_app=0, key_vks=[BACKTICK])
        elapsed = time.monotonic() - start
        if reply is not None:
            app.done.wait(2)
        return {"result": list(result), "elapsed": elapsed, "after": clipboard.get_text(),
                "count": win32.user32.CountClipboardFormats(), "sent": _SandboxSystem.sent}

    def avail(names):
        return [bool(win32.user32.IsClipboardFormatAvailable(win32.user32.RegisterClipboardFormatW(n)))
                for n in names]

    def round_trip(s):
        return [clipboard.set_text(s), clipboard.get_text() == s]

    def history(on):
        clipboard.set_text("h", history=on)
        return avail(clipboard.HISTORY_OPT_OUT)

    def busy():
        _set_previous(prev_text)
        opened = threading.Event()
        holder = threading.Thread(target=_hold_clipboard, args=(desktop, 1.0, opened))
        holder.start()
        opened.wait(2)
        _SandboxSystem.reply, _SandboxSystem.sent = ("never", {}), []
        start = time.monotonic()
        result = clipboard.copy_selection(is_click=False, last_app=0, key_vks=[])
        elapsed = time.monotonic() - start
        holder.join()
        return {"result": list(result), "elapsed": elapsed, "sent": _SandboxSystem.sent,
                "after": clipboard.get_text()}

    def pump_until(event: threading.Event, seconds: float) -> bool:
        """Wait for the fake app while answering its sent messages (it may be emptying our clipboard)."""
        end = time.monotonic() + seconds
        while not event.is_set() and time.monotonic() < end:
            win32.wait_pumping_sent(0.01)
        return event.is_set()

    def late_copy(watched: bool):
        """The app answers Ctrl+C 0.5 s late, after the 0.35 s timeout; watched: poll like the input thread."""
        _set_previous(prev_text)
        _SandboxSystem.reply, _SandboxSystem.terminal, _SandboxSystem.sent = ("landed late", {}, 0.5), False, []
        start = time.monotonic()
        result = clipboard.copy_selection(is_click=False, last_app=0, key_vks=[BACKTICK])
        elapsed = time.monotonic() - start
        polls = 0
        while watched and (left := clipboard.watch_late_copy()) is not None:
            polls += 1
            win32.wait_pumping_sent(min(left, clipboard.LATE_COPY_POLL_S))
        return {"result": list(result), "elapsed": elapsed, "polls": polls, "app_wrote": pump_until(app.done, 3),
                "after": clipboard.get_text()}

    def click_shell_real():
        if win32.get_foreground_window():
            return "skipped"
        clipboard._System = real_system  # entirely real; returns before any key could be sent
        try:
            return {"result": list(clipboard.copy_selection(is_click=True, last_app=0, key_vks=[]))}
        finally:
            clipboard._System = _SandboxSystem

    def control_no_pump():
        _set_previous(prev_text)

        class NoPump(_SandboxSystem):
            def sleep(self, seconds):
                time.sleep(seconds)  # a plain sleep: the owner window can't answer WM_DESTROYCLIPBOARD

        clipboard._System = NoPump
        try:
            return copy(prev_text, ("late", {}))
        finally:
            clipboard._System = _SandboxSystem
            for _ in range(300):  # let the blocked app finish
                if app.done.is_set():
                    break
                win32.wait_pumping_sent(0.01)

    pickle = chromium_pickle({"vscode-editor-data": VSCODE_LINE.decode()})
    with clipboard.ClipboardOwner():
        clipboard.empty_clipboard()  # start clean, in case an earlier run was killed mid-way
        scenario("roundtrip", lambda: round_trip("Saylight ✓ ünïcödé \U0001F600\r\nline two\t​"))
        scenario("surrogate", lambda: round_trip("a\ud800b"))
        scenario("large", lambda: round_trip("x" * 300_000 + "end"))
        scenario("history_off", lambda: history(False))
        scenario("history_on", lambda: history(True))
        scenario("empty", lambda: [clipboard.empty_clipboard(), win32.user32.CountClipboardFormats() == 0,
                                   clipboard.get_text() is None])
        scenario("ok_owned", lambda: copy(reply=("Selected text ✓ \U0001F600", {})))
        scenario("terminal_keys", lambda: copy(reply=("in a terminal", {}), terminal=True))
        scenario("click_console", lambda: copy(reply=("never", {}), terminal=True, click=True))
        scenario("vscode_json", lambda: copy(reply=("    return x\r\n", {"vscode-editor-data": VSCODE_LINE})))
        scenario("vscode_json_selection", lambda: copy(reply=("def f():", {"vscode-editor-data": VSCODE_SELECTION})))
        scenario("chromium", lambda: copy(reply=("line\r\n", {clipboard.CHROMIUM_CUSTOM_FORMAT: pickle})))
        scenario("vs_tag", lambda: copy(reply=("line\r\n", {clipboard.LINE_COPY_TAGS[0]: b"\0"})))
        scenario("scintilla_tag", lambda: copy(reply=("line\r\n", {clipboard.LINE_COPY_TAGS[1]: b"\0"})))
        scenario("whitespace", lambda: copy(reply=(" \r\n\t", {})))
        scenario("no_text_copied", lambda: copy(reply=(None, {"PNG": b"\x89PNG\r\n\x1a\n"})))
        scenario("prev_empty", lambda: copy(prev=None, reply=("fresh", {})))
        scenario("prev_non_text", lambda: copy(prev=b"\x89PNG not really", reply=("fresh", {})))
        scenario("timeout", lambda: copy(reply=None))
        scenario("late_copy_watched", lambda: late_copy(True))
        scenario("late_copy_unwatched", lambda: late_copy(False))
        scenario("busy", busy)
        scenario("click_shell_real", click_shell_real)
        scenario("control_no_pump", control_no_pump)
        clipboard.set_text("kept ✓")
    scenario("owner_destroyed", lambda: {"after": clipboard.get_text()})
    with clipboard.ClipboardOwner():
        clipboard.empty_clipboard()  # leave the station's clipboard as empty as we found it
    print(json.dumps(out))
    sys.stdout.flush()
    os._exit(0)  # don't wait for the daemon app thread


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--sandbox":
        _sandbox_main(int(sys.argv[2]))
    else:
        unittest.main()

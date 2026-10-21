"""Tests for settings.py: defaults, validation and coercion, debounced atomic saving, .bad and lock handling."""

import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from saylight import settings as S
from saylight.settings import SCHEMA, Settings

_app = None


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="ra_settings_test_")
        self.path = os.path.join(self.dir, "saylight_settings.json")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_json(self, obj: object) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def write_raw(self, data: bytes) -> None:
        with open(self.path, "wb") as f:
            f.write(data)

    def read_json(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def spy(self, settings: Settings) -> list[str]:
        keys: list[str] = []
        settings.changed.connect(keys.append)
        return keys

    def wait_for(self, condition, timeout_ms: int = 3000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if condition():
                return True
            QTest.qWait(20)
        return condition()


class TestDefaults(Base):
    def test_missing_file_gives_defaults_and_writes_nothing(self) -> None:
        s = Settings(self.path)
        for key, (default, _) in SCHEMA.items():
            self.assertEqual(s.get(key), default, key)
        self.assertTrue(s.flush())
        self.assertFalse(os.path.exists(self.path))

    def test_defaults_pass_their_own_validators(self) -> None:
        for key, (default, check) in SCHEMA.items():
            self.assertEqual(check(default), default, key)

    def test_default_values(self) -> None:
        s = Settings(self.path)
        self.assertEqual(s.get("hotkeys"), {"read": "Ctrl+`", "toggle_panel": "", "pause_resume": "", "compose": ""})
        self.assertEqual(s.get("panel"), {"visible": True, "position": None, "expanded": False})
        expected = {"version": 1, "speed": 0.95, "volume": 100, "hud_when_hidden": True, "media_keys": True,
                    "first_hide_tip_shown": False, "skip_citations": True, "skip_urls": True,
                    "text_size": "medium", "reading_tint": "auto", "reading_font": "verdana", "spacing": "wide",
                    "highlight": "word", "voice": "edge:en-US-AvaNeural", "updates.check": True,
                    "updates.last_check": 0.0, "updates.etag": "", "updates.skipped": ""}
        for key, value in expected.items():
            self.assertEqual(s.get(key), value, key)

    def test_speed_list(self) -> None:
        self.assertEqual(S.SPEEDS, (0.5, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.5, 1.75, 2.0))


class TestLoad(Base):
    def test_valid_values_are_read(self) -> None:
        self.write_json({"speed": 1.25, "volume": 40, "voice": "en-GB-SoniaNeural",
                         "panel": {"visible": False, "expanded": True},
                         "hotkeys": {"read": "Ctrl+`", "toggle_panel": "Ctrl+Alt+`"},
                         "text_size": "xlarge", "skip_urls": False})
        s = Settings(self.path)
        self.assertEqual(s.get("speed"), 1.25)
        self.assertEqual(s.get("volume"), 40)
        self.assertEqual(s.get("voice"), "edge:en-GB-SoniaNeural")
        self.assertFalse(s.get("panel.visible"))
        self.assertTrue(s.get("panel.expanded"))
        self.assertEqual(s.get("hotkeys.toggle_panel"), "Ctrl+Alt+`")
        self.assertEqual(s.get("hotkeys.pause_resume"), "")
        self.assertEqual(s.get("text_size"), "xlarge")
        self.assertFalse(s.get("skip_urls"))

    def test_invalid_values_fall_back_and_are_logged(self) -> None:
        bad = {"speed": "fast", "volume": "loud", "voice": "  ", "text_size": "huge", "reading_tint": "neon",
               "panel": {"visible": "yes"}, "hotkeys": {"read": "Ctrl+Nope"}, "media_keys": None}
        self.write_json(bad)
        with self.assertLogs("saylight.settings", "WARNING") as logs:
            s = Settings(self.path)
        for key in ("speed", "volume", "voice", "text_size", "reading_tint", "panel.visible", "hotkeys.read",
                    "media_keys"):
            self.assertEqual(s.get(key), SCHEMA[key][0], key)
            self.assertTrue(any(key in line for line in logs.output), key)

    def test_coercions(self) -> None:
        cases = [("speed", 0.97, 0.95), ("speed", 3, 2.0), ("speed", 0.1, 0.5), ("speed", "1.25", 1.25),
                 ("speed", 1, 1.0), ("volume", 130, 100), ("volume", -4, 0), ("volume", 50.5, 51),
                 ("volume", "70", 70), ("panel.visible", 0, False), ("text_size", "LARGE", "large"),
                 ("voice", " en-US-AvaNeural ", "edge:en-US-AvaNeural")]
        for key, given, expected in cases:
            with self.subTest(key=key, given=given):
                *parents, leaf = key.split(".")
                obj: dict = {leaf: given}
                for parent in reversed(parents):
                    obj = {parent: obj}
                self.write_json(obj)
                self.assertEqual(Settings(self.path).get(key), expected)

    def test_non_finite_and_bool_speeds_are_rejected(self) -> None:
        for raw in (b'{"speed": NaN}', b'{"speed": Infinity}', b'{"speed": true}'):
            with self.subTest(raw=raw):
                self.write_raw(raw)
                with self.assertLogs("saylight.settings", "WARNING"):
                    self.assertEqual(Settings(self.path).get("speed"), 0.95)

    def test_huge_numbers_do_not_crash(self) -> None:
        self.write_raw(b'{"volume": 1' + b"0" * 400 + b', "speed": "1' + b"0" * 400 + b'"}')
        with self.assertLogs("saylight.settings", "WARNING"):
            s = Settings(self.path)
        self.assertEqual((s.get("volume"), s.get("speed")), (100, 0.95))

    def test_logs_never_echo_values(self) -> None:
        self.write_json({"speed": "SECRET-SPEED", "voice": 7, "text_size": "SECRET-SIZE",
                         "hotkeys": {"read": "SECRET+`"}, "panel": {"position": {"fx": "SECRET-FX", "fy": 0}}})
        with self.assertLogs("saylight.settings", "WARNING") as logs:
            Settings(self.path)
        self.assertFalse(any("SECRET" in line for line in logs.output), logs.output)

    def test_groups_that_are_not_objects_get_defaults(self) -> None:
        self.write_json({"panel": 5, "hotkeys": ["Ctrl+`"]})
        s = Settings(self.path)
        self.assertEqual(s.get("panel"), {"visible": True, "position": None, "expanded": False})
        self.assertEqual(s.get("hotkeys.read"), "Ctrl+`")

    def test_utf8_bom_is_accepted(self) -> None:
        self.write_raw(b"\xef\xbb\xbf" + json.dumps({"volume": 33}).encode())
        s = Settings(self.path)
        self.assertEqual(s.get("volume"), 33)
        self.assertFalse(os.path.exists(self.path + ".bad"))

    def test_utf16_from_an_editor_is_accepted(self) -> None:
        for encoding in ("utf-16", "utf-16-le"):  # with and without a BOM
            with self.subTest(encoding=encoding):
                self.write_raw(json.dumps({"volume": 33, "voice": "en-GB-SoniaNeural"}).encode(encoding))
                s = Settings(self.path)
                self.assertEqual((s.get("volume"), s.get("voice")), (33, "edge:en-GB-SoniaNeural"))
                self.assertFalse(os.path.exists(self.path + ".bad"))
                s.set("volume", 34)
                self.assertTrue(s.flush())
                self.assertEqual(self.read_json()["volume"], 34)  # rewritten as UTF-8

    def test_lone_surrogate_escapes_round_trip(self) -> None:
        """\\ud800 escapes are legal JSON but can't be encoded as UTF-8; saving must neither raise nor lose them."""
        self.write_raw(b'{"note": "\\ud800x", "voice": "a\\udfffb"}')
        s = Settings(self.path)
        self.assertEqual(s.get("voice"), "edge:a\udfffb")
        s.set("volume", 41)
        self.assertTrue(s.flush())
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        data = self.read_json()
        self.assertEqual((data["note"], data["volume"]), ("\ud800x", 41))
        self.assertEqual(Settings(self.path).get("voice"), "edge:a\udfffb")

    def test_loading_emits_nothing_and_does_not_rewrite(self) -> None:
        self.write_json({"volume": 130})
        before = os.path.getmtime(self.path)
        s = Settings(self.path)
        self.assertTrue(s.flush())
        self.assertEqual(os.path.getmtime(self.path), before)
        self.assertEqual(self.read_json(), {"volume": 130})


class TestVoice(Base):
    def test_earlier_bare_names_become_online_voice_ids(self) -> None:
        cases = [("en-US-AvaNeural", "edge:en-US-AvaNeural"), (" en-GB-SoniaNeural ", "edge:en-GB-SoniaNeural"),
                 ("edge:en-US-AvaNeural", "edge:en-US-AvaNeural"), ("EDGE: en-IE-EmilyNeural", "edge:en-IE-EmilyNeural"),
                 ("windows:David", "windows:David")]
        for given, expected in cases:
            with self.subTest(given=given):
                self.write_json({"voice": given})
                self.assertEqual(Settings(self.path).get("voice"), expected)

    def test_values_that_are_not_voices_fall_back_to_ava(self) -> None:
        for given in ("", "  ", "edge:", "nosuch:Voice", 7, None, ["en-US-AvaNeural"]):
            with self.subTest(given=given):
                self.write_json({"voice": given})
                with self.assertLogs("saylight.settings", "WARNING"):
                    self.assertEqual(Settings(self.path).get("voice"), "edge:en-US-AvaNeural")

    def test_set_stores_the_full_id(self) -> None:
        s = Settings(self.path)
        s.set("voice", "en-GB-RyanNeural")
        self.assertEqual(s.get("voice"), "edge:en-GB-RyanNeural")
        self.assertTrue(s.flush())
        self.assertEqual(self.read_json()["voice"], "edge:en-GB-RyanNeural")
        with self.assertRaises(ValueError):
            s.set("voice", "nosuch:Voice")


class TestUpdateKeys(Base):
    def test_defaults(self) -> None:
        s = Settings(self.path)
        self.assertEqual(s.get("updates"), {"check": True, "last_check": 0.0, "etag": "", "skipped": ""})

    def test_empty_strings_are_allowed_and_kept(self) -> None:
        s = Settings(self.path)
        for key, value in (("updates.etag", 'W/"abc"'), ("updates.skipped", "1.3.0"), ("updates.check", False),
                           ("updates.last_check", 1_760_000_000.5)):
            s.set(key, value)
        s.set("updates.etag", "")
        self.assertTrue(s.flush())
        self.assertEqual(self.read_json()["updates"],
                         {"check": False, "last_check": 1_760_000_000.5, "etag": "", "skipped": "1.3.0"})
        self.assertEqual(Settings(self.path).get("updates.skipped"), "1.3.0")

    def test_bad_values_fall_back(self) -> None:
        self.write_json({"updates": {"check": "sometimes", "last_check": "SECRET", "etag": 5, "skipped": None}})
        with self.assertLogs("saylight.settings", "WARNING") as logs:
            s = Settings(self.path)
        self.assertEqual(s.get("updates"), {"check": True, "last_check": 0.0, "etag": "", "skipped": ""})
        self.assertFalse(any("SECRET" in line for line in logs.output))

    def test_a_time_before_1970_is_never(self) -> None:
        self.write_json({"updates": {"last_check": -5}})
        self.assertEqual(Settings(self.path).get("updates.last_check"), 0.0)


# The files Saylight 1.0 and 1.1 wrote: every key of their schema, indent 2, in schema order.
EARLIER_FILES = {
    "1.0": {"version": 1, "speed": 1.15, "volume": 70, "voice": "en-GB-SoniaNeural",
            "panel": {"visible": False, "position": {"screen": "\\\\.\\DISPLAY2", "serial": "AB12", "fx": 0.25,
                                                     "fy": 0.9, "snap_x": "right", "snap_y": None},
                      "expanded": True},
            "hud_when_hidden": False, "media_keys": False,
            "hotkeys": {"read": "Ctrl+`", "toggle_panel": "Ctrl+Alt+F23", "pause_resume": ""},
            "first_hide_tip_shown": True, "skip_citations": False, "skip_urls": True, "text_size": "large",
            "reading_tint": "peach", "reading_font": "sitka", "spacing": "extra", "highlight": "sentence"},
}
EARLIER_FILES["1.1"] = {**EARLIER_FILES["1.0"], "voice": "en-US-AvaNeural",
                        "hotkeys": {"read": "Ctrl+`", "toggle_panel": "", "pause_resume": "Ctrl+Alt+F24",
                                    "compose": "Ctrl+Alt+Shift+F24"}}


class TestEarlierFiles(Base):
    def test_files_from_1_0_and_1_1_load_unchanged(self) -> None:
        for release, tree in EARLIER_FILES.items():
            with self.subTest(release=release):
                raw = (json.dumps(tree, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                self.write_raw(raw)
                before = os.path.getmtime(self.path)
                with self.assertNoLogs("saylight.settings", "WARNING"):
                    s = Settings(self.path)
                flat = {}
                self._flatten(tree, "", flat)
                for key, value in flat.items():
                    expected = "edge:" + value if key == "voice" else value
                    self.assertEqual(s.get(key), expected, key)
                self.assertEqual(s.get("updates.check"), True)
                self.assertTrue(s.flush())
                self.assertEqual(os.path.getmtime(self.path), before)
                with open(self.path, "rb") as f:
                    self.assertEqual(f.read(), raw)  # loading never rewrites the file

    def test_the_first_save_keeps_every_earlier_value(self) -> None:
        tree = EARLIER_FILES["1.1"]
        self.write_json(tree)
        s = Settings(self.path)
        s.set("updates.skipped", "1.3.0")
        self.assertTrue(s.flush())
        data = self.read_json()
        self.assertEqual(data["voice"], "edge:en-US-AvaNeural")
        self.assertEqual(data["updates"]["skipped"], "1.3.0")
        for key in tree:
            if key != "voice":
                self.assertEqual(data[key], tree[key], key)

    def _flatten(self, node: dict, prefix: str, out: dict) -> None:
        for key, value in node.items():
            name = prefix + key
            if isinstance(value, dict) and name != "panel.position":
                self._flatten(value, name + ".", out)
            else:
                out[name] = value


class TestBadFile(Base):
    def check_quarantined(self, raw: bytes) -> None:
        self.write_raw(raw)
        with self.assertLogs("saylight.settings", "WARNING"):
            s = Settings(self.path)
        self.assertFalse(os.path.exists(self.path))
        with open(self.path + ".bad", "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertEqual(s.get("speed"), 0.95)
        s.set("volume", 10)
        self.assertTrue(s.flush())
        self.assertEqual(self.read_json()["volume"], 10)

    def test_unparseable_json(self) -> None:
        self.check_quarantined(b'{"speed": 1.0,, oops')

    def test_top_level_not_an_object(self) -> None:
        self.check_quarantined(b'["speed", 1.0]')

    def test_not_utf8(self) -> None:
        self.check_quarantined(b'{"voice": "\xff\xfe"}')

    def test_empty_file(self) -> None:
        self.check_quarantined(b"")

    def test_existing_bad_file_is_replaced(self) -> None:
        self.write_raw(b"old bad")
        os.replace(self.path, self.path + ".bad")
        self.check_quarantined(b"new bad")

    def test_absurdly_deep_nesting_is_quarantined_not_a_crash(self) -> None:
        depth = 200_000  # json raises RecursionError, not ValueError, for this
        self.check_quarantined(b'{"x": ' + b"[" * depth + b"]" * depth + b"}")

    def test_unmovable_bad_file_is_never_overwritten(self) -> None:
        raw = b'{"volume": 42,, "mine": true'
        self.write_raw(raw)
        with mock.patch.object(S.os, "replace", side_effect=PermissionError("locked")), \
                mock.patch.object(S, "_FILE_GAP_S", 0), \
                self.assertLogs("saylight.settings", "ERROR"):
            s = Settings(self.path)
        s.set("volume", 5)
        self.assertFalse(s.flush())
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertFalse(os.path.exists(self.path + ".bad"))
        self.assertFalse(os.path.exists(self.path + ".tmp"))


class TestUnreadableFile(Base):
    def test_locked_file_is_never_overwritten(self) -> None:
        self.write_json({"volume": 42, "mine": True})
        with mock.patch.object(S, "_read_bytes", side_effect=PermissionError("locked")) as read, \
                mock.patch.object(S, "_FILE_GAP_S", 0):
            with self.assertLogs("saylight.settings", "ERROR"):
                s = Settings(self.path)
        self.assertEqual(read.call_count, S._FILE_ATTEMPTS)
        self.assertEqual(s.get("volume"), 100)
        s.set("volume", 5)
        self.assertEqual(s.get("volume"), 5)
        self.assertFalse(s.flush())
        self.assertEqual(self.read_json(), {"volume": 42, "mine": True})

    def test_transient_lock_is_retried(self) -> None:
        self.write_json({"volume": 42})
        real = S._read_bytes
        calls = []

        def flaky(path: str) -> bytes:
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError("locked")
            return real(path)

        with mock.patch.object(S, "_read_bytes", flaky):
            s = Settings(self.path)
        self.assertEqual(len(calls), 3)
        self.assertEqual(s.get("volume"), 42)


class TestPosition(Base):
    GOOD = {"screen": "Odyssey G95NC", "serial": "HNTX501816", "fx": 0.5, "fy": 0.92013,
            "snap_x": "center", "snap_y": "bottom"}

    def test_valid_position_is_normalised(self) -> None:
        self.write_json({"panel": {"position": dict(self.GOOD, extra=1)}})
        self.assertEqual(Settings(self.path).get("panel.position"), dict(self.GOOD, fy=0.9201))

    def test_fractions_are_clamped_and_bad_parts_dropped(self) -> None:
        s = Settings(self.path)
        s.set("panel.position", {"screen": 7, "fx": 1.7, "fy": -0.2, "snap_x": "middle", "snap_y": "top"})
        self.assertEqual(s.get("panel.position"),
                         {"screen": "", "serial": "", "fx": 1.0, "fy": 0.0, "snap_x": None, "snap_y": "top"})

    def test_missing_fraction_invalidates_position(self) -> None:
        self.write_json({"panel": {"position": {"screen": "X", "fx": 0.5}}})
        with self.assertLogs("saylight.settings", "WARNING"):
            s = Settings(self.path)
        self.assertIsNone(s.get("panel.position"))
        with self.assertRaises(ValueError):
            s.set("panel.position", {"fx": 0.5})
        with self.assertRaises(ValueError):
            s.set("panel.position", "centre")

    def test_reset_to_null(self) -> None:
        self.write_json({"panel": {"position": self.GOOD}})
        s = Settings(self.path)
        keys = self.spy(s)
        s.set("panel.position", None)
        self.assertIsNone(s.get("panel.position"))
        self.assertEqual(keys, ["panel.position"])


class TestHotkeys(Base):
    def test_canonical_forms(self) -> None:
        check = SCHEMA["hotkeys.read"][1]
        cases = {"Ctrl+`": "Ctrl+`", "alt+ctrl+`": "Ctrl+Alt+`", "Control + Shift + Alt + `": "Ctrl+Alt+Shift+`",
                 "ctrl+alt+shift+f24": "Ctrl+Alt+Shift+F24", "F9": "F9", "shift+f1": "Shift+F1",
                 "Win+r": "Win+r", "Windows+Ctrl+Ctrl+x": "Ctrl+Win+x", "Ctrl++": "Ctrl++", "": "", "   ": "",
                 "ctrl+alt+space": "Ctrl+Alt+Space", "alt+pageup": "Alt+PageUp", "pause": "Pause",
                 "Shift+ScrollLock": "Shift+ScrollLock"}
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(check(given), expected)

    def test_invalid_shortcuts(self) -> None:
        check = SCHEMA["hotkeys.read"][1]
        for given in ("`", "a", "Shift+a", "Ctrl+Foo", "Ctrl+F25", "Ctrl+F0", "Hyper+`", "Ctrl+", "+",
                      "Ctrl++a", "Ctrl+ ", "Space", "Shift+Home", "Ctrl+\U0001F600", 5, None, ["Ctrl", "`"]):
            with self.subTest(given=given), self.assertRaises(ValueError):
                check(given)

    @unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(S.__file__), "win32.py")),
                         "win32.py is not available")
    def test_every_accepted_shortcut_parses_in_win32(self) -> None:
        """Integration: what settings stores, win32.parse_hotkey accepts (pure parsing; nothing is registered)."""
        from saylight import win32
        check = SCHEMA["hotkeys.read"][1]
        for given in ("Ctrl+`", "Ctrl+Alt+`", "Ctrl+Alt+Shift+`", "Ctrl+Alt+Shift+F24", "F23", "Ctrl+Alt+Space",
                      "Alt+PageUp", "Pause", "Shift+ScrollLock", "Win+r", "Ctrl+a", "Ctrl+Alt+Delete", "Ctrl+1"):
            with self.subTest(given=given):
                self.assertIsNotNone(win32.parse_hotkey(check(given)))
        self.assertIsNone(win32.parse_hotkey(check("")))

    def test_empty_disables(self) -> None:
        s = Settings(self.path)
        s.set("hotkeys.read", "")
        self.assertEqual(s.get("hotkeys.read"), "")

    def test_compose_shortcut_is_off_by_default_and_validated_like_the_others(self) -> None:
        s = Settings(self.path)
        self.assertEqual(s.get("hotkeys.compose"), "")
        self.assertIs(SCHEMA["hotkeys.compose"][1], SCHEMA["hotkeys.read"][1])
        keys = self.spy(s)
        s.set("hotkeys.compose", "alt+ctrl+shift+f23")
        self.assertEqual(s.get("hotkeys.compose"), "Ctrl+Alt+Shift+F23")
        with self.assertRaises(ValueError):
            s.set("hotkeys.compose", "t")
        self.assertEqual(keys, ["hotkeys.compose"])
        self.assertTrue(s.flush())
        self.assertEqual(self.read_json()["hotkeys"]["compose"], "Ctrl+Alt+Shift+F23")

    def test_an_invalid_compose_shortcut_in_the_file_falls_back(self) -> None:
        self.write_json({"hotkeys": {"compose": "Ctrl+Nope"}})
        with self.assertLogs("saylight.settings", "WARNING"):
            s = Settings(self.path)
        self.assertEqual(s.get("hotkeys.compose"), "")

    def test_nothing_typed_or_about_the_box_is_a_setting(self) -> None:
        """Typed text and the box's size are never persisted; its shortcut is the only new key."""
        related = [k for k in SCHEMA if any(w in k for w in ("compose", "draft", "typed", "editor", "text."))]
        self.assertEqual(related, ["hotkeys.compose"])


class TestSet(Base):
    def test_emits_dotted_key_only_on_real_change(self) -> None:
        s = Settings(self.path)
        keys = self.spy(s)
        s.set("speed", 1.25)
        s.set("speed", 1.25)
        s.set("speed", 1.26)  # snaps back to 1.25: no change
        s.set("panel.visible", False)
        s.set("hotkeys.toggle_panel", "alt+ctrl+`")
        s.set("hotkeys.toggle_panel", "Ctrl+Alt+`")
        self.assertEqual(keys, ["speed", "panel.visible", "hotkeys.toggle_panel"])

    def test_unknown_keys_raise_key_error(self) -> None:
        s = Settings(self.path)
        for key in ("nope", "panel", "hotkeys", "panel.nope", "hotkeys.read_stop"):
            with self.subTest(key=key), self.assertRaises(KeyError):
                s.set(key, 1)
        with self.assertRaises(KeyError):
            s.get("nope")

    def test_invalid_value_raises_and_changes_nothing(self) -> None:
        s = Settings(self.path)
        keys = self.spy(s)
        for key, value in (("speed", "fast"), ("volume", None), ("voice", ""), ("text_size", "huge"),
                           ("reading_tint", "neon"), ("reading_font", "Comic Sans"), ("spacing", 3),
                           ("highlight", "yes"), ("hotkeys.read", "`")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                s.set(key, value)
            self.assertEqual(s.get(key), SCHEMA[key][0])
        self.assertEqual(keys, [])
        self.assertTrue(s.flush())
        self.assertFalse(os.path.exists(self.path))

    def test_get_returns_copies(self) -> None:
        s = Settings(self.path)
        s.set("panel.position", dict(TestPosition.GOOD))
        s.get("panel.position")["fx"] = 0.1
        s.get("hotkeys")["read"] = "x"
        s.get("panel")["visible"] = False
        self.assertEqual(s.get("panel.position")["fx"], 0.5)
        self.assertEqual(s.get("hotkeys.read"), "Ctrl+`")
        self.assertTrue(s.get("panel.visible"))


class TestSaving(Base):
    def test_debounced_single_write(self) -> None:
        s = Settings(self.path)
        real, writes = S._atomic_write, []

        def timed(path: str, data: bytes) -> None:
            writes.append(time.monotonic())
            real(path, data)

        with mock.patch.object(S, "_atomic_write", timed):
            s.set("volume", 10)
            s.set("volume", 20)
            time.sleep(0.15)  # no event loop here, so a stall under load can't let the first timer fire early
            s.set("speed", 1.5)
            last = time.monotonic()
            self.assertFalse(os.path.exists(self.path))
            self.assertTrue(self.wait_for(lambda: os.path.exists(self.path)))
            QTest.qWait(100)
        self.assertEqual(len(writes), 1)
        # Measured from the last change; a Qt coarse timer may fire up to 5% early, and Windows rounds to its
        # timer tick. No upper bound: a busy machine may stall the event loop.
        self.assertGreaterEqual(writes[0] - last, 0.45, "wrote before 500 ms of quiet")
        data = self.read_json()
        self.assertEqual((data["volume"], data["speed"]), (20, 1.5))

    def test_quit_flushes_pending_changes(self) -> None:
        class FakeApp(QObject):
            aboutToQuit = Signal()

        app = FakeApp()
        with mock.patch.object(S, "QCoreApplication", types.SimpleNamespace(instance=lambda: app)):
            s = Settings(self.path)
        s.set("volume", 9)
        self.assertFalse(os.path.exists(self.path))
        app.aboutToQuit.emit()
        self.assertEqual(self.read_json()["volume"], 9)

    def test_flush_writes_indented_utf8_now(self) -> None:
        s = Settings(self.path)
        s.set("panel.position", dict(TestPosition.GOOD, screen="Écran 2"))
        self.assertTrue(s.flush())
        with open(self.path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith('{\n  "version": 1,'))
        self.assertTrue(text.endswith("}\n"))
        self.assertIn("Écran 2", text)  # UTF-8, not \u escapes
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertTrue(s.flush())  # nothing pending

    def test_round_trip(self) -> None:
        s = Settings(self.path)
        values = {"speed": 0.8, "volume": 0, "voice": "edge:en-GB-RyanNeural", "panel.visible": False,
                  "panel.position": dict(TestPosition.GOOD, fy=0.9201), "panel.expanded": True,
                  "hotkeys.pause_resume": "Ctrl+Alt+Shift+`", "first_hide_tip_shown": True,
                  "text_size": "small", "highlight": "off", "reading_tint": "peach", "reading_font": "sitka",
                  "spacing": "extra", "skip_citations": False}
        for key, value in values.items():
            s.set(key, value)
        self.assertTrue(s.flush())
        again = Settings(self.path)
        for key, value in values.items():
            self.assertEqual(again.get(key), value, key)

    def test_unknown_keys_are_preserved(self) -> None:
        original = {"future": {"a": [1, 2], "b": None}, "panel": {"visible": True, "custom": "x"},
                    "hotkeys": {"read": "Ctrl+`", "read_stop": "Ctrl+`"}, "version": 1}
        self.write_json(original)
        s = Settings(self.path)
        s.set("volume", 55)
        self.assertTrue(s.flush())
        data = self.read_json()
        self.assertEqual(data["future"], original["future"])
        self.assertEqual(data["panel"]["custom"], "x")
        self.assertEqual(data["hotkeys"]["read_stop"], "Ctrl+`")
        self.assertEqual(data["volume"], 55)

    def test_replace_is_retried(self) -> None:
        s = Settings(self.path)
        s.set("volume", 12)
        real = os.replace
        calls = []

        def flaky(src: str, dst: str) -> None:
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError("locked")
            real(src, dst)

        with mock.patch.object(S.os, "replace", flaky):
            self.assertTrue(s.flush())
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.read_json()["volume"], 12)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_persistent_lock_keeps_old_file_and_retries_later(self) -> None:
        self.write_json({"volume": 42})
        s = Settings(self.path)
        s.set("volume", 7)
        with mock.patch.object(S.os, "replace", side_effect=PermissionError("locked")), \
                mock.patch.object(S, "_FILE_GAP_S", 0), \
                self.assertLogs("saylight.settings", "WARNING"):
            self.assertFalse(s.flush())
        self.assertEqual(self.read_json(), {"volume": 42})  # untouched
        self.assertFalse(os.path.exists(self.path + ".tmp"))  # no stray .tmp while the lock lasts
        # The lock is gone now: the back-off timer (1 s) saves without another flush.
        self.assertTrue(self.wait_for(lambda: self.read_json().get("volume") == 7, 3000))
        self.assertFalse(os.path.exists(self.path + ".tmp"))


if __name__ == "__main__":
    unittest.main()

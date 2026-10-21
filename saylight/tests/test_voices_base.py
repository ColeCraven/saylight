"""Tests for voices/base.py: voice IDs, the data types and the backend protocol."""

import dataclasses
import subprocess
import sys
import unittest

from .. import synth
from ..voices import base
from ..voices.base import (DEFAULT_VOICE_ID, FAIL_KINDS, VoiceBackend, VoiceInfo, Word, make_voice_id,
                           parse_voice_id)


class VoiceIdTests(unittest.TestCase):
    def test_bare_names_from_1_0_are_edge_voices(self):
        self.assertEqual(parse_voice_id("en-US-AvaNeural"), ("edge", "en-US-AvaNeural"))
        self.assertEqual(parse_voice_id("  en-GB-SoniaNeural "), ("edge", "en-GB-SoniaNeural"))
        self.assertEqual(parse_voice_id(""), ("edge", ""))

    def test_prefixed_ids(self):
        cases = {
            "edge:en-US-AvaNeural": ("edge", "en-US-AvaNeural"),
            "windows:MSTTS_V110_enUS_DavidM": ("windows", "MSTTS_V110_enUS_DavidM"),
            "Edge:en-US-AvaNeural": ("edge", "en-US-AvaNeural"),
            "windows:a:b": ("windows", "a:b"),          # only the first colon separates
            "future_kind:x": ("future_kind", "x"),      # unknown kinds parse; the router reports them unavailable
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_voice_id(value), expected)

    def test_a_colon_after_something_that_is_not_a_kind_is_part_of_an_edge_name(self):
        self.assertEqual(parse_voice_id("1x:y"), ("edge", "1x:y"))
        self.assertEqual(parse_voice_id(":y"), ("edge", ":y"))

    def test_make_voice_id_round_trips(self):
        self.assertEqual(make_voice_id("edge", "en-US-AvaNeural"), DEFAULT_VOICE_ID)
        for kind, name in [("edge", "en-IE-EmilyNeural"), ("windows", "MSTTS_V110_enUS_ZiraM"), ("windows", "a:b")]:
            self.assertEqual(parse_voice_id(make_voice_id(kind, name)), (kind, name))
        for bad in ("", "1edge", "ed ge", "edge:"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_voice_id(bad, "x")

    def test_default_voice_is_ava(self):
        self.assertEqual(parse_voice_id(DEFAULT_VOICE_ID), ("edge", "en-US-AvaNeural"))


class DataTypeTests(unittest.TestCase):
    def test_word_is_shared_with_synth(self):
        self.assertIs(synth.Word, Word)
        w = Word(100.0, 250.0, "Hello")
        self.assertEqual((w.offset_ms, w.duration_ms, w.text), (100.0, 250.0, "Hello"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            w.text = "x"

    def test_voice_info_defaults_and_fields(self):
        v = VoiceInfo(id="edge:en-US-AvaNeural", backend="edge", name="Ava", locale="en-US", gender="Female",
                      private=False)
        self.assertTrue(v.word_timing)
        self.assertEqual(v.detail, "")
        self.assertEqual([f.name for f in dataclasses.fields(VoiceInfo)],
                         ["id", "backend", "name", "locale", "gender", "private", "word_timing", "detail"])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            v.name = "x"
        self.assertEqual(hash(v), hash(dataclasses.replace(v)))

    def test_fail_kinds(self):
        self.assertEqual(FAIL_KINDS, ("offline", "service", "other", "unavailable"))


class ProtocolTests(unittest.TestCase):
    def test_edge_backend_implements_the_protocol(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        backend = synth.SynthWorker()
        self.assertIsInstance(backend, VoiceBackend)
        self.assertEqual((backend.kind, backend.audio_ext), ("edge", ".mp3"))
        self.assertEqual(backend.chunk_limits(), (250, 1500))
        self.assertTrue(backend.ready())
        self.assertTrue(backend.voices())
        backend.shutdown()

    def test_an_object_missing_methods_is_not_a_backend(self):
        class Half:
            kind, audio_ext = "x", ".wav"
            chunkReady = chunkFailed = None

            def start(self, cache):
                pass

        self.assertNotIsInstance(Half(), VoiceBackend)

    def test_base_imports_nothing_heavy(self):
        code = ("import sys; import saylight.voices.base; "
                "heavy = [m for m in ('PySide6', 'edge_tts', 'aiohttp') if m in sys.modules]; print(heavy)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                             cwd=_repo_root(), creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)


def _repo_root() -> str:
    import os
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(base.__file__))))


if __name__ == "__main__":
    unittest.main()

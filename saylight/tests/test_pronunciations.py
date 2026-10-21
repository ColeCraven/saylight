"""Tests for pronunciations.py: matching (smart case, whole words, longest match, never inside skipped citations or
"say" replacements), the file (atomic, validated, a bad file moved aside), the store (writes only on a change,
never overwrites on import, a 2,000-word limit), the "How it will be read" helpers, and logs without words.

The alignment layer is used when it is there; otherwise a small stand-in with the same interface."""

import bisect
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from dataclasses import dataclass
from unittest import mock

from PySide6.QtCore import QCoreApplication

from saylight.pronunciations import (MAX_ENTRIES, Entry, PronunciationList, PronunciationStore, base_spoken, parse,
                                     preview_parts, preview_spoken)


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    spoken: str
    kind: str = "skip"


class _Aligned:
    """Display text plus edits -> the spoken text and its map back to the display (the alignment interface)."""

    def __init__(self, display: str, edits=()):
        self.display = display
        self.edits = sorted(edits, key=lambda e: (e.start, e.end))
        self._seg: list[tuple[int, int, int, int, bool]] = []  # spoken start, end, display start, end, unedited
        parts, s, d = [], 0, 0
        for e in self.edits + [_Edit(len(display), len(display), "")]:
            if e.start > d:
                self._seg.append((s, s + e.start - d, d, e.start, True))
                parts.append(display[d:e.start])
                s += e.start - d
            if e.spoken:
                self._seg.append((s, s + len(e.spoken), e.start, e.end, False))
                parts.append(e.spoken)
                s += len(e.spoken)
            d = max(d, e.end)
        self.spoken = "".join(parts)
        self._starts = [g[0] for g in self._seg]

    def _k(self, i: int) -> int:
        return bisect.bisect_right(self._starts, i) - 1

    def range(self, i: int, j: int) -> tuple[int, int]:
        s0, _s1, d0, _d1, exact = self._seg[self._k(i)]
        a = d0 + (i - s0) if exact else d0
        t0, _t1, e0, e1, exact = self._seg[self._k(max(i, j - 1))]
        b = e0 + (j - t0) if exact else e1
        return a, max(a, b)

    def is_exact_range(self, i: int, j: int) -> bool:
        k = self._k(i)
        return k >= 0 and self._seg[k][4] and j <= self._seg[k][1]

    def with_edits(self, extra):
        return type(self)(self.display, list(self.edits) + list(extra))


try:
    from saylight.alignment import Aligned, Edit
    _FAKE = {}
except ImportError:
    Aligned, Edit = _Aligned, _Edit
    _FAKE = {"saylight.alignment": types.SimpleNamespace(Edit=_Edit, Aligned=_Aligned)}


def aligned(display: str, edits=()):
    return Aligned(display, [Edit(*e) for e in edits])


def said(pl: PronunciationList, display: str, edits=()) -> str:
    with mock.patch.dict(sys.modules, _FAKE):
        return pl.apply(aligned(display, edits)).spoken


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])


class MatchingTests(unittest.TestCase):
    def test_smart_case(self) -> None:
        pl = PronunciationList([Entry("fMRI", "F M R I"), Entry("gif", "jif")])
        self.assertEqual(said(pl, "An fMRI and a GIF and a Gif."), "An F M R I and a jif and a jif.")
        self.assertEqual(said(pl, "An FMRI scan."), "An FMRI scan.")  # a capital in the entry: case-sensitive

    def test_explicit_case_flag(self) -> None:
        pl = PronunciationList([Entry("US", "U S", match_case=False)])
        self.assertEqual(said(pl, "In the us and the US."), "In the U S and the U S.")
        pl = PronunciationList([Entry("gif", "jif", match_case=True)])
        self.assertEqual(said(pl, "A GIF, a gif."), "A GIF, a jif.")

    def test_whole_word(self) -> None:
        pl = PronunciationList([Entry("RT", "reaction time")])
        self.assertEqual(said(pl, "RT and RTs and ART and RT-based and RT."),
                         "reaction time and RTs and ART and reaction time-based and reaction time.")
        pl = PronunciationList([Entry("RT", "reaction time", whole_word=False)])
        self.assertEqual(said(pl, "Mean RTs."), "Mean reaction times.")

    def test_longest_match_and_several_words(self) -> None:
        pl = PronunciationList([Entry("et al.", "and colleagues"), Entry("al", "AL"),
                                Entry("Saylight", "Say light"), Entry("p < .05", "p less than point oh five")])
        self.assertEqual(said(pl, "Lee et al. found it with Saylight at p < .05."),
                         "Lee and colleagues found it with Say light at p less than point oh five.")
        self.assertEqual(said(pl, "Lee et\nal. again."), "Lee and colleagues again.")  # any run of spaces

    def test_entries_that_start_or_end_with_punctuation(self) -> None:
        pl = PronunciationList([Entry("C++", "C plus plus"), Entry("R²", "R squared"), Entry(".NET", "dot net")])
        self.assertEqual(said(pl, "Written in C++ with R² of .4 on .NET."),
                         "Written in C plus plus with R squared of .4 on dot net.")

    def test_a_word_that_isnt_said_goes_with_one_space(self) -> None:
        pl = PronunciationList([Entry("ibid.", "")])
        with mock.patch.dict(sys.modules, _FAKE):
            al = pl.apply(aligned("As noted ibid. the effect holds."))
        self.assertEqual(al.spoken, "As noted the effect holds.")
        self.assertEqual([(e.start, e.end, e.kind) for e in al.edits], [(9, 15, "skip")])
        self.assertEqual(said(pl, "It was ibid."), "It was")

    def test_never_inside_skipped_citations_or_replacements(self) -> None:
        display = "Reading (Smith, 2019) is at https://x.org/a and in [3]."
        edits = [(8, 22, "", "skip"), (28, 43, "link", "say"), (51, 54, "reference 3", "say")]
        self.assertEqual(said(PronunciationList([]), display, edits), "Reading is at link and in reference 3.")
        pl = PronunciationList([Entry("Smith", "Smyth"), Entry("link", "LINK"), Entry("reference", "ref"),
                                Entry("Reading", "Reeding")])
        self.assertEqual(said(pl, display, edits), "Reeding is at link and in reference 3.")

    def test_the_highlight_maps_back_to_the_shown_word(self) -> None:
        pl = PronunciationList([Entry("fMRI", "F M R I")])
        with mock.patch.dict(sys.modules, _FAKE):
            al = pl.apply(aligned("Use fMRI now."))
        i = al.spoken.index("M")
        self.assertEqual("Use fMRI now."[slice(*al.range(i, i + 1))], "fMRI")
        self.assertEqual([(e.start, e.end, e.spoken, e.kind) for e in al.edits], [(4, 8, "F M R I", "pron")])

    def test_disabled_entries_are_left_out(self) -> None:
        pl = PronunciationList([Entry("fMRI", "F M R I", enabled=False)])
        self.assertEqual(len(pl), 0)
        self.assertEqual(said(pl, "An fMRI."), "An fMRI.")

    def test_a_full_list_stays_fast(self) -> None:
        pl = PronunciationList([Entry(f"word{i}", f"w {i}") for i in range(MAX_ENTRIES)]
                               + [Entry(f"two words{i}", "x") for i in range(200)])
        text = "The word17 and word1999 appear with two words5 here. " * 2000
        t0 = time.perf_counter()
        found = pl.find(text)
        self.assertEqual(len(found), 6000)
        self.assertLess(time.perf_counter() - t0, 1.0)


class PreviewTests(unittest.TestCase):
    def test_the_spoken_form_with_only_this_entry(self) -> None:
        text, spans = preview_spoken("so the fMRI data showed it", Entry("fMRI", "F M R I"))
        self.assertEqual(text, "so the F M R I data showed it")
        self.assertEqual([text[a:b] for a, b in spans], ["F M R I"])
        self.assertEqual(preview_parts("so the fMRI data", Entry("fMRI", "F M R I")),
                         [("text", "so the "), ("say", "F M R I"), ("text", " data")])

    def test_a_word_that_isnt_said(self) -> None:
        e = Entry("ibid.", "")
        self.assertEqual(preview_spoken("as noted ibid. the effect", e), ("as noted the effect", [(9, 9)]))
        self.assertEqual(preview_parts("as noted ibid. the effect", e),
                         [("text", "as noted "), ("skip", "ibid."), ("text", " the effect")])

    def test_no_match(self) -> None:
        e = Entry("RT", "reaction time")
        self.assertEqual(preview_spoken("ART and RTs", e), ("ART and RTs", []))
        self.assertEqual(preview_parts("ART and RTs", e), [("text", "ART and RTs")])
        self.assertEqual(preview_parts("ART and RTs", Entry("RT", "x", enabled=False))[0], ("text", "ART and RTs"))

    def test_the_base_spoken_text_leaves_out_the_list_but_not_citations(self) -> None:
        silent = PronunciationList([Entry("ibid.", "")])

        @dataclass
        class S:
            text: str
            align: object = None
            spoken: str = ""

            @property
            def say(self) -> str:
                return self.spoken or self.text

        display = "Noted (Lee, 2020) ibid. in fMRI work."
        al = aligned(display, [(6, 18, "", "skip"), (18, 24, "", "skip"), (27, 31, "F M R I", "pron")])
        self.assertEqual(al.spoken, "Noted in F M R I work.")
        self.assertEqual(base_spoken(S(display, al), silent), "Noted ibid. in fMRI work.")
        self.assertEqual(base_spoken(S(display, al)), "Noted in fMRI work.")
        self.assertEqual(base_spoken(S("Plain text.")), "Plain text.")  # a 1.2 sentence: no alignment


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_pron_test_")
        self.path = os.path.join(self.dir, "pronunciations.json")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_round_trip_and_validation(self) -> None:
        self.assertEqual(PronunciationList.load(self.path).entries, [])  # no file: an empty list
        entries = [Entry("fMRI", "F M R I"), Entry("gif", "jif", match_case=False),
                   Entry("ibid.", "", whole_word=False, enabled=False)]
        PronunciationList.save(self.path, entries)
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["entries"][0], {"match": "fMRI", "say": "F M R I"})
        self.assertEqual(raw["entries"][2], {"match": "ibid.", "say": "", "whole_word": False, "enabled": False})
        raw["entries"] += [{"match": ""}, {"match": 5, "say": "x"}, {"match": "x" * 101, "say": "y"},
                           {"match": "...", "say": "dots"}, "junk", {"match": "ok  word", "say": " a  b "},
                           {"match": "long", "say": "z" * 201}]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        loaded = PronunciationList.load(self.path)
        self.assertEqual([e.match for e in loaded.entries], ["fMRI", "gif", "ok word"])  # ibid. is off
        self.assertEqual(loaded.entries[-1].say, "a b")
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_a_bad_file_is_moved_aside_and_the_list_starts_empty(self) -> None:
        for content in (b"{not json", b'{"version": 2, "entries": []}', b"[]"):
            with self.subTest(content=content):
                with open(self.path, "wb") as f:
                    f.write(content)
                with self.assertLogs("saylight.pronunciations", "WARNING"):
                    self.assertEqual(PronunciationList.load(self.path).entries, [])
                self.assertFalse(os.path.exists(self.path))
                with open(self.path + ".bad", "rb") as f:
                    self.assertEqual(f.read(), content)
        with self.assertRaises(ValueError):
            parse(b"{not json")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_pron_test_")
        self.path = os.path.join(self.dir, "data", "pronunciations.json")
        self.store = PronunciationStore(self.path)
        self.changes = 0
        self.store.changed.connect(self._changed)

    def _changed(self) -> None:
        self.changes += 1

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def saved(self) -> list[Entry]:
        return PronunciationStore(self.path).entries()

    def test_written_only_when_the_list_changes(self) -> None:
        self.store.current()
        self.store.entries()
        self.assertFalse(os.path.exists(self.path))
        self.store.add(Entry("fMRI", "F M R I"))
        self.assertEqual(self.saved(), [Entry("fMRI", "F M R I")])
        self.assertEqual(self.changes, 1)

    def test_add_replaces_the_same_word_and_update_merges(self) -> None:
        s = self.store
        self.assertEqual(s.add(Entry("fMRI", "F M R I")), 0)
        self.assertEqual(s.add(Entry("GIF", "jif")), 1)
        before = s.current()
        self.assertEqual(s.add(Entry("fMRI", "eff M R I")), 0)  # same key: replaced, not added
        self.assertIsNot(s.current(), before)
        self.assertEqual(len(s), 2)
        self.assertEqual(s.index_of(Entry("fmri", "x")), None)  # no capital: matches other text
        s.update(1, Entry("fMRI", "F M R I"))  # GIF becomes fMRI, which already exists: one entry left
        self.assertEqual(s.entries(), [Entry("fMRI", "F M R I")])
        removed = s.remove(0)
        s.insert(0, removed)  # Undo
        self.assertEqual(self.saved(), [Entry("fMRI", "F M R I")])
        with self.assertRaises(ValueError):
            s.add(Entry("   ", "nothing"))
        self.assertEqual(s.find_word("fMRI"), 0)
        self.assertIsNone(s.find_word("FMRI"))

    def test_a_full_list_takes_no_new_words(self) -> None:
        s = self.store
        s.import_entries(Entry(f"w{i}", "x") for i in range(MAX_ENTRIES))
        self.assertTrue(s.is_full())
        with self.assertRaises(ValueError):
            s.add(Entry("new", "x"))
        s.add(Entry("w5", "changed"))  # changing a word still works
        added, full = s.import_entries([Entry("a", "b"), Entry("c", "d")])
        self.assertEqual((added, full), ([], 2))

    def test_import_never_overwrites_and_can_be_undone(self) -> None:
        s = self.store
        s.add(Entry("fMRI", "F M R I"))
        other = os.path.join(self.dir, "shared.json")
        PronunciationList.save(other, [Entry("fMRI", "wrong"), Entry("GIF", "jif"), Entry("et al.", "and others")])
        self.assertEqual(s.import_file(other), 2)
        self.assertEqual(s.entries()[0].say, "F M R I")
        s.remove_entries([Entry("GIF", "jif"), Entry("et al.", "and others")])
        self.assertEqual(self.saved(), [Entry("fMRI", "F M R I")])
        with open(other, "w", encoding="utf-8") as f:
            f.write('{"words": []}')
        with self.assertRaises(ValueError):
            s.import_file(other)

    def test_export_writes_the_whole_list(self) -> None:
        self.store.add(Entry("fMRI", "F M R I"))
        self.store.add(Entry("ibid.", "", enabled=False))
        out = os.path.join(self.dir, "Saylight pronunciations.json")
        self.store.export_file(out)
        self.assertEqual(PronunciationStore(out).entries(), self.store.entries())

    def test_logs_hold_counts_never_words(self) -> None:
        with self.assertLogs("saylight.pronunciations", logging.DEBUG) as logs:
            self.store.add(Entry("Schneps-Jonasson", "Schnepps"))
            self.store.update(0, Entry("Schneps-Jonasson", "Shneps"))
            self.store.export_file(os.path.join(self.dir, "out.json"))
            self.store.remove(0)
        text = "\n".join(logs.output)
        self.assertIn("added (now 1)", text)
        for word in ("Schneps", "Shneps", "out.json", self.dir):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()

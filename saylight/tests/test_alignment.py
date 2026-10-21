"""The shown/spoken alignment and the two-text Document: exactness, the 1.2 regression and word highlighting."""

import bisect
import json
import os
import random
import re
import time
import unittest
from types import SimpleNamespace

from saylight import academic_text as at
from saylight import text_model as tm
from difflib import SequenceMatcher

from saylight import alignment
from saylight.alignment import Aligned, Edit, diff_edits
from saylight.text_model import BuildOptions, Document

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "documents")
with open(os.path.join(FIXTURES, "regression_12.json"), encoding="utf-8") as _f:
    GOLDEN = json.load(_f)["cases"]
CORPUS = list(dict.fromkeys(c["raw"] for c in GOLDEN))
SAMPLE = "Dyslexia affects reading (Smith et al., 2019; Jones, 2020). fMRI shows it, as shown in [3]."


class FakePronunciations:
    """Whole-word literal entries, longest first, only where the spoken text is still as shown."""

    def __init__(self, entries):
        self.entries = sorted(entries.items(), key=lambda kv: -len(kv[0]))

    def apply(self, al):
        extra, taken = [], []
        for match, say in self.entries:
            for m in re.finditer(r"(?<!\w)" + re.escape(match) + r"(?!\w)", al.spoken):
                a, b = m.span()
                if any(a < y and x < b for x, y in taken) or not al.is_exact_range(a, b):
                    continue
                taken.append((a, b))
                extra.append(Edit(*al.range(a, b), say, "pron" if say else "skip"))
        return al.with_edits(extra) if extra else al


PRON = FakePronunciations({"fMRI": "F M R I", "et al.": "and colleagues"})


def fake_words(plan):
    """WordBoundary-like events: one per word of the spoken chunk text (edge-tts leaves out punctuation)."""
    words, t = [], 50.0
    for m in re.finditer(r"\w+(?:['’]\w+)?", plan.text):
        words.append(SimpleNamespace(offset_ms=t, duration_ms=200.0, text=m.group()))
        t += 250.0
    return words


def highlights(doc):
    out = []
    for plan in tm.plan_chunks(doc, 0, first_chunk_id=0):
        for w in tm.map_words(plan, fake_words(plan), None).words:
            s = doc.sentences[w.sentence]
            out.append((w.sentence, s.start + w.lo, s.start + w.hi))
    return out


class AlignedTests(unittest.TestCase):
    def test_skip_and_replacement(self):
        d = "reading (Smith, 2019). fMRI rocks"
        al = Aligned(d, [Edit(7, 21, "", "skip"), Edit(23, 27, "F M R I", "pron")])
        self.assertEqual(al.spoken, "reading. F M R I rocks")
        self.assertEqual(al.range(0, 7), (0, 7))
        self.assertEqual(d[slice(*al.range(7, 8))], ".")   # the full stop after the skip
        self.assertEqual(al.range(9, 10), (23, 27))         # "F" -> "fMRI"
        self.assertEqual(al.range(15, 16), (23, 27))        # "I" -> "fMRI"
        self.assertEqual(al.range(9, 16), (23, 27))
        self.assertEqual(d[slice(*al.range(17, 22))], "rocks")
        self.assertEqual(al.to_spoken(10), 7)                # inside the skip -> just after it
        self.assertEqual(al.to_spoken(24), 9)                # inside "fMRI" -> its start

    def test_edges(self):
        al = Aligned("(A, 2019) Text", [Edit(0, 10, "", "skip")])
        self.assertEqual(al.spoken, "Text")
        self.assertEqual(al.range(0, 4), (10, 14))
        al = Aligned("Text (A, 2019)", [Edit(4, 14, "", "skip")])
        self.assertEqual(al.range(0, 4), (0, 4))
        self.assertEqual(al.lo(4), 4)
        al = Aligned("ab", [Edit(1, 1, "X", "say")])        # a zero-width edit: an insertion
        self.assertEqual(al.spoken, "aXb")
        self.assertEqual(al.range(1, 2), (1, 1))
        self.assertEqual(Aligned("", []).range(0, 0), (0, 0))
        with self.assertRaises(ValueError):
            Aligned("abcdef", [Edit(0, 3, ""), Edit(2, 4, "")])

    def test_slice_exact_and_ranges(self):
        al = Aligned("One (A, 2019). Two [3].", [Edit(3, 13, "", "skip"), Edit(19, 22, "reference 3", "say")])
        sub = al.slice(15, 23)
        self.assertEqual((sub.display, sub.spoken), ("Two [3].", "Two reference 3."))
        self.assertEqual(al.ranges("say"), [(19, 22)])
        self.assertTrue(al.is_exact_range(0, 3))
        self.assertFalse(al.is_exact_range(9, 13))            # "reference" is a replacement
        self.assertEqual(al.with_edits([Edit(0, 3, "Uno", "pron")]).spoken, "Uno. Two reference 3.")


class DiffEditsTests(unittest.TestCase):
    def test_exact_on_random_edits(self):
        rng = random.Random(7)
        words = "the of reading (Smith, 2019) [3] dyslexia fMRI et al. 12 children; link".split()
        for _ in range(3000):
            d = " ".join(rng.choice(words) for _ in range(rng.randint(0, 30)))
            toks = re.findall(r"\s+|\w+|[^\w\s]", d)
            s = "".join(t if rng.random() > 0.2 else rng.choice(["", "", "reference 3", "link", " "]) for t in toks)
            self.assertEqual(Aligned(d, diff_edits(d, s)).spoken, s)

    def test_exact_on_unrelated_strings(self):
        rng = random.Random(3)
        alphabet = "ab (),.[]1 é中"
        for _ in range(2000):
            d = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            self.assertEqual(Aligned(d, diff_edits(d, s)).spoken, s)

    def test_reference_is_one_edit(self):
        self.assertEqual(diff_edits("as shown in [3].", "as shown in reference 3."),
                         [Edit(12, 15, "reference 3", "say")])



def dimmed(doc: Document) -> list[str]:
    return [doc.text[a:b] for a, b in doc.skipped()]


class CitationPairTests(unittest.TestCase):
    """Two citations either side of kept punctuation are two removals, never one "said" stretch."""

    def test_two_citations_around_a_kept_comma_are_both_dimmed(self):
        doc = Document.from_raw("Both (Smith, 2019), (Jones, 2020) agree. Next.")
        self.assertEqual(doc.sentences[0].say, "Both, agree.")
        self.assertEqual(dimmed(doc), ["(Smith, 2019)", "(Jones, 2020)"])  # the comma kept is the one between

    def test_two_citations_around_a_kept_full_stop_are_both_dimmed(self):
        doc = Document.from_raw("This holds (Lee, 2010).(Kim, 2011) Next one here.")
        self.assertEqual(doc.sentences[0].say, "This holds.")
        self.assertEqual(dimmed(doc), ["(Lee, 2010)", "(Kim, 2011)"])

    def test_a_citation_after_a_full_stop_stays_out_of_the_sentence_before(self):
        doc = Document.from_raw("This is a claim [1]. [2] Another claim follows here.")
        self.assertEqual([s.say for s in doc.sentences], ["This is a claim.", "Another claim follows here."])
        self.assertEqual(dimmed(doc), ["[1]", "[2]"])
        self.assertNotIn("[2]", doc.sentences[0].text)

    def test_the_full_stop_kept_is_the_one_outside_the_citation(self):
        doc = Document.from_raw("This holds (Smith et al., 2019). (Jones et al., 2020) Next one here.")
        self.assertEqual([s.text for s in doc.sentences], ["This holds (Smith et al., 2019).", "Next one here."])
        self.assertEqual(dimmed(doc), ["(Smith et al., 2019)", "(Jones et al., 2020)"])

    def test_a_said_edit_always_carries_words(self):
        # A "say" edit is drawn in the normal text colour, so it must never be only punctuation.
        doc = Document.from_raw("Both (Smith, 2019), (Jones, 2020) agree. Next.")
        for s in doc.sentences:
            for e in (s.align.edits if s.align else []):
                if e.kind == "say":
                    self.assertTrue(re.search(r"\w", e.spoken), e)


class NormalizePairTests(unittest.TestCase):
    def test_joins_to_both_normalize_outputs(self):
        for text in CORPUS:
            for sc, su in ((True, True), (True, False), (False, True), (False, False)):
                pairs = at.normalize_pair(text, skip_citations=sc, skip_urls=su)
                self.assertEqual("\n\n".join(s for _, s in pairs if s),
                                 at.normalize(text, skip_citations=sc, skip_urls=su), text[:60])
                self.assertEqual("\n\n".join(d for d, _ in pairs),
                                 at.normalize(text, skip_citations=False, skip_urls=False), text[:60])


class RegressionTests(unittest.TestCase):
    """What the voice gets is exactly what Saylight 1.2 sent (the fixture was written by 1.2's own code)."""

    def test_spoken_sentences_and_chunks_match_1_2(self):
        for case in GOLDEN:
            doc = Document.build(case["raw"], BuildOptions(skip_citations=case["skip_citations"],
                                                           skip_urls=case["skip_urls"], spoken_forms=False))
            self.assertEqual([s.say for s in doc.sentences], case["sentences"], case["raw"][:60])
            plans = tm.plan_chunks(doc, 0, first_chunk_id=0) if doc.sentences else []
            self.assertEqual([[p.text, p.offsets, p.ends] for p in plans], case["chunks"], case["raw"][:60])

    def test_spoken_forms_are_the_only_difference(self):
        for case in GOLDEN:
            opts = dict(skip_citations=case["skip_citations"], skip_urls=case["skip_urls"])
            doc = Document.build(case["raw"], BuildOptions(**opts))
            self.assertEqual(len(doc.sentences), len(case["sentences"]))
            for s, old in zip(doc.sentences, case["sentences"]):
                expected = old
                for a, b, said in reversed(at.spoken_forms(old)):
                    expected = expected[:a] + said + expected[b:]
                self.assertEqual(s.say, expected)

    def test_shown_text_keeps_citations_and_sentences_cover_them(self):
        doc = Document.from_raw(SAMPLE)
        self.assertEqual(doc.text, SAMPLE)
        self.assertEqual([doc.text[a:b] for a, b in doc.skipped()], ["(Smith et al., 2019; Jones, 2020)"])
        s0 = doc.sentences[0]
        self.assertEqual(s0.text, "Dyslexia affects reading (Smith et al., 2019; Jones, 2020).")
        self.assertEqual(s0.say, "Dyslexia affects reading.")
        for s in doc.sentences:
            self.assertEqual(doc.text[s.start:s.end], s.text)

    def test_citation_only_paragraph_is_shown_not_spoken(self):
        doc = Document.from_raw("Reading matters.\n\n(Smith, 2019; Jones, 2020)\n\nNext part here.")
        self.assertIn("(Smith, 2019; Jones, 2020)", doc.text)
        self.assertEqual([s.say for s in doc.sentences], ["Reading matters.", "Next part here."])
        self.assertEqual([doc.text[a:b] for a, b in doc.skipped()], ["(Smith, 2019; Jones, 2020)"])

    def test_a_selection_of_only_a_citation_is_read(self):
        doc = Document.from_raw("(Smith, 2019)")
        self.assertEqual([s.say for s in doc.sentences], ["(Smith, 2019)"])
        self.assertEqual(doc.regions, [])


class MapWordsTests(unittest.TestCase):
    def test_highlights_land_on_shown_words(self):
        doc = Document.from_raw(SAMPLE, pronunciations=PRON)
        self.assertEqual([doc.text[a:b] for _, a, b in highlights(doc)],
                         ["Dyslexia", "affects", "reading", "fMRI", "fMRI", "fMRI", "fMRI", "shows", "it",
                          "as", "shown", "in", "[3]", "[3]"])

    def test_never_highlights_skipped_text(self):
        for text in CORPUS:
            doc = Document.from_raw(text, pronunciations=PRON)
            skipped = [(a, b) for a, b in doc.skipped()]
            starts = [a for a, _ in skipped]
            for _, a, b in highlights(doc):
                k = bisect.bisect_right(starts, a) - 1
                if b > a and k >= 0:
                    self.assertFalse(skipped[k][0] <= a < skipped[k][1], (text[:40], doc.text[a:b]))

    def test_words_stay_inside_their_sentence(self):
        for text in CORPUS:
            doc = Document.from_raw(text)
            for sentence, a, b in highlights(doc):
                s = doc.sentences[sentence]
                self.assertTrue(s.start <= a < b <= s.end, (text[:40], a, b))

    def test_spoken_forms_highlight_the_abbreviation(self):
        doc = Document.from_raw("Some tasks, e.g. Stroop, were hard (see Fig. 2).")
        self.assertEqual(doc.sentences[0].say, "Some tasks, for example Stroop, were hard (see Figure 2).")
        shown = [doc.text[a:b] for _, a, b in highlights(doc)]
        self.assertEqual(shown, ["Some", "tasks", "e.g.", "e.g.", "Stroop", "were", "hard", "see", "Fig.", "2"])
        self.assertEqual([(r.kind, doc.text[r.start:r.end]) for r in doc.regions], [("said", "e.g."),
                                                                                     ("said", "Fig.")])


class SpokenFormTests(unittest.TestCase):
    def test_forms(self):
        cases = {
            "See Fig. 2 and Figs. 3 and 4.": "See Figure 2 and Figures 3 and 4.",
            "In Eq. (3), as on pp. 4 to 9.": "In Equation (3), as on pages 4 to 9.",
            "It took approx. 20 min, e.g., in class, i.e. at school.":
                "It took approximately 20 min, for example, in class, that is at school.",
            "E.g. this one. I.e. that one.": "For example this one. That is that one.",
        }
        for shown, said in cases.items():
            doc = Document.from_raw(shown)
            self.assertEqual(" ".join(s.say for s in doc.sentences), said)
            self.assertEqual(doc.text, shown)            # the screen keeps the original

    def test_not_forms(self):
        for text in ("The fig. was ripe.", "A figure of eq. weight.", "She saw pp. on the sign.", "Use i.e.g. now."):
            self.assertEqual(at.spoken_forms(text), [], text)

    def test_pronunciations_win_over_spoken_forms(self):
        doc = Document.from_raw("It works, e.g. here.", pronunciations=FakePronunciations({"e.g.": "e g"}))
        self.assertEqual(doc.sentences[0].say, "It works, e g here.")

    def test_can_be_turned_off(self):
        doc = Document.build("It works, e.g. here.", BuildOptions(spoken_forms=False))
        self.assertEqual(doc.sentences[0].say, "It works, e.g. here.")
        self.assertIsNone(doc.sentences[0].align)


class PerformanceTests(unittest.TestCase):
    def test_100k_characters(self):
        para = ("Phonological awareness predicts reading (Snowling et al., 2020; Hulme, 2014). In a large "
                "sample [12], children with dyslexia (n = 120) read 20% slower, as shown in [3]. fMRI data "
                "were collected at https://osf.io/abc for 40 children (see Peters et al. (2018a) for details).\n\n")
        text = para * (100_000 // len(para) + 1)
        t0 = time.perf_counter()
        doc = Document.from_raw(text, pronunciations=PRON)
        t1 = time.perf_counter()
        plans = tm.plan_chunks(doc, 0, first_chunk_id=0)
        for p in plans:
            tm.map_words(p, fake_words(p), None)
        t2 = time.perf_counter()
        self.assertGreater(len(doc.sentences), 1000)
        self.assertLess(t1 - t0, 3.0)
        self.assertLess(t2 - t1, 3.0)



class LongParagraphTests(unittest.TestCase):
    """A selection copied from a PDF viewer or a hard-wrapped .txt is often one long paragraph, and text up to
    20,000 characters is built on the GUI thread."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            text = re.sub(r"[#*`|>_\[\]()!]", " ", f.read())
        prose = " ".join(text.split())
        while len(prose) < 32_000:
            prose = prose + " " + prose.lower()
        out, i, k = [], 0, 0
        while i < len(prose):  # a citation every few sentences, as in a paper
            j = prose.find(". ", i + 250)
            j = len(prose) if j < 0 else j
            out.append(prose[i:j] + (f" (Smith et al., {1990 + k % 30})" if k % 2 else f" [{k + 1}]"))
            i, k = j, k + 1
        cls.paragraph = "".join(out)

    def test_a_long_paragraph_with_citations_builds_quickly(self):
        t0 = time.perf_counter()
        doc = Document.build(self.paragraph, BuildOptions())
        elapsed = time.perf_counter() - t0
        self.assertGreater(len(doc.sentences), 100)
        self.assertLess(elapsed, 1.5, f"built in {elapsed:.1f} s")

    def test_diff_edits_on_a_long_paragraph_is_quick_and_exact(self):
        (shown, spoken), = at.normalize_pair(self.paragraph[:20_000])
        t0 = time.perf_counter()
        edits = diff_edits(shown, spoken)
        elapsed = time.perf_counter() - t0
        self.assertEqual(Aligned(shown, edits).spoken, spoken)
        self.assertLess(elapsed, 0.5, f"diffed in {elapsed:.1f} s")

    def test_windows_agree_with_comparing_the_whole_paragraph(self):
        (shown, spoken), = at.normalize_pair(self.paragraph[:5_000])
        a, b = alignment._tokens(shown)[0], alignment._tokens(spoken)[0]
        self.assertGreater(len(a), alignment._WHOLE_TOKENS)
        self.assertEqual(alignment._opcodes(a, b), SequenceMatcher(None, a, b, autojunk=False).get_opcodes())

    def test_a_name_inside_a_citation_that_comes_up_again_later_stays_aligned(self):
        # Anchoring on rare words (difflib's autojunk) pairs the "Smith" in the citation with the later one.
        filler = ("The results, in short, suggest that the method works well in practice, and that the effect "
                  "holds across several settings that we tested in the lab over the course of the study. ")
        para = ("Smith argues that reading aloud helps. " + filler * 6 + "This view is widely shared (Smith, "
                "2019). " + filler * 6 + "Later Smith (2021) revisits the claim. " + filler * 4 + "The end.")
        doc = Document.build(para, BuildOptions())
        self.assertGreater(len(alignment._tokens(doc.text)[0]), alignment._WHOLE_TOKENS)
        self.assertEqual(dimmed(doc), ["(Smith, 2019)", "(2021)"])
        self.assertIn("Later Smith revisits the claim.", [s.say for s in doc.sentences])


if __name__ == "__main__":
    unittest.main()

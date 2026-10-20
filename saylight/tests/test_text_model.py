"""Tests for saylight.text_model (pure logic; no Qt, no audio, no network)."""

import random
import unittest
from dataclasses import dataclass

from saylight import text_model as tm


@dataclass(frozen=True)
class W:
    """Stand-in for synth.Word."""
    offset_ms: float
    duration_ms: float
    text: str


def doc_of(*sentences: str) -> tm.Document:
    return tm.Document.from_raw(" ".join(sentences))


class DocumentTests(unittest.TestCase):
    def test_offsets_are_exact(self):
        raw = ("First sentence here.  Second one!\r\n\r\nA new   paragraph? Yes…  Done.\n\n\n"
               "Third paragraph with a long tail")
        doc = tm.Document.from_raw(raw)
        self.assertGreaterEqual(len(doc.sentences), 5)
        for i, s in enumerate(doc.sentences):
            self.assertEqual(s.index, i)
            self.assertEqual(doc.text[s.start:s.end], s.text)
            self.assertEqual(s.end - s.start, len(s.text))

    def test_sentences_joined_by_space_paragraphs_by_blank_line(self):
        doc = tm.Document.from_raw("One. Two.\n\nThree.")
        self.assertEqual([s.text for s in doc.sentences], ["One.", "Two.", "Three."])
        self.assertEqual(doc.text, "One. Two.\n\nThree.")

    def test_whitespace_is_collapsed(self):
        doc = tm.Document.from_raw("Spaced    out\ttext   here.")
        self.assertEqual(doc.sentences[0].text, "Spaced out text here.")

    def test_empty_and_blank_input(self):
        for raw in ("", "   ", "\n\n\n"):
            doc = tm.Document.from_raw(raw)
            self.assertEqual(doc.sentences, [])
            self.assertEqual(doc.text, "")

    def test_no_text_between_sentences_other_than_separators(self):
        doc = tm.Document.from_raw("Alpha. Beta.\n\nGamma. Delta.")
        for a, b in zip(doc.sentences, doc.sentences[1:]):
            self.assertIn(doc.text[a.end:b.start], (" ", "\n\n"))

    def test_skip_flags_are_accepted(self):
        doc = tm.Document.from_raw("Plain text.", skip_citations=False, skip_urls=False)
        self.assertEqual(doc.sentences[0].text, "Plain text.")


class PlanChunksTests(unittest.TestCase):
    def setUp(self):
        sentences = [f"Sentence number {i} has some words in it to make it longer." for i in range(60)]
        self.doc = doc_of(*sentences)

    def test_limits_and_exact_offsets(self):
        plans = tm.plan_chunks(self.doc, 0, first_limit=250, limit=1500, first_chunk_id=7)
        self.assertLessEqual(len(plans[0].text), 250)
        for p in plans[1:]:
            self.assertLessEqual(len(p.text), 1500)
        for p in plans:
            for j, sid in enumerate(p.sentence_ids):
                s = self.doc.sentences[sid]
                self.assertEqual(p.text[p.offsets[j]:p.offsets[j] + len(s.text)], s.text)
                a, b = p.sentence_bounds(j)
                self.assertEqual(p.text[a:b], s.text)

    def test_ids_contiguous_and_cover_everything(self):
        plans = tm.plan_chunks(self.doc, 0, first_chunk_id=7)
        self.assertEqual([p.chunk_id for p in plans], list(range(7, 7 + len(plans))))
        flat = [sid for p in plans for sid in p.sentence_ids]
        self.assertEqual(flat, list(range(60)))
        self.assertEqual(plans[0].first, 0)
        self.assertEqual(plans[-1].last, 59)

    def test_start_sentence(self):
        plans = tm.plan_chunks(self.doc, 42, first_limit=250, limit=1500, first_chunk_id=0)
        self.assertEqual(plans[0].first, 42)
        self.assertLessEqual(len(plans[0].text), 250)
        self.assertEqual([sid for p in plans for sid in p.sentence_ids], list(range(42, 60)))

    def test_chunk_text_joins_with_single_space(self):
        plans = tm.plan_chunks(self.doc, 0, first_chunk_id=0)
        p = plans[1]
        self.assertEqual(p.text, " ".join(self.doc.sentences[i].text for i in p.sentence_ids))

    def test_paragraph_breaks_become_spaces_in_chunk_text(self):
        doc = tm.Document.from_raw("Heading\n\nBody one. Body two.")
        p = tm.plan_chunks(doc, 0, first_chunk_id=0)[0]
        self.assertNotIn("\n", p.text)

    def test_start_at_end_gives_no_chunks(self):
        self.assertEqual(tm.plan_chunks(self.doc, 60, first_chunk_id=0), [])
        self.assertEqual(tm.plan_chunks(self.doc, 60, first_chunk_id=0, max_chunks=1), [])

    def test_max_chunks_gives_exactly_the_first_chunks_of_the_full_plan(self):
        rng = random.Random(3)
        for trial in range(200):
            texts = []
            for _ in range(rng.randint(1, 120)):
                size = rng.choice([3, 20, 60, 200, 900, 1490])
                words = " ".join("w" * rng.randint(1, 9) for _ in range(size // 5 + 1))[:size].strip() or "w"
                texts.append(words + rng.choice([".", "", "!"]))
            sentences, pos = [], 0
            for i, text in enumerate(texts):
                sentences.append(tm.Sentence(i, text, pos, pos + len(text)))
                pos += len(text) + 1
            doc = tm.Document(" ".join(texts), sentences)
            start = rng.randrange(len(doc.sentences))
            first_limit, limit = rng.choice([(250, 1500), (60, 300), (1500, 250)])
            full = tm.plan_chunks(doc, start, first_limit=first_limit, limit=limit, first_chunk_id=5)
            for n in (1, 2):
                part = tm.plan_chunks(doc, start, first_limit=first_limit, limit=limit, first_chunk_id=5, max_chunks=n)
                self.assertEqual(part, full[:n], (trial, n))

    def test_four_field_chunk_plan_derives_sentence_ends(self):
        plan = tm.ChunkPlan(4, [0, 1, 2], "The cat sat. A dog ran fast. Birds fly.", [0, 13, 29])
        self.assertEqual([plan.text[a:b] for a, b in map(plan.sentence_bounds, range(3))],
                         ["The cat sat.", "A dog ran fast.", "Birds fly."])
        t = tm.map_words(plan, [W(100, 50, "dog"), W(300, 50, "fly")], 1000)
        self.assertEqual([(w.sentence, w.lo, w.hi) for w in t.words], [(1, 2, 5), (2, 6, 9)])


class MapWordsTests(unittest.TestCase):
    def setUp(self):
        self.doc = doc_of("The cat sat.", "A dog ran fast.", "Birds fly.")
        self.plan = tm.plan_chunks(self.doc, 0, first_chunk_id=3)[0]
        self.assertEqual(self.plan.sentence_ids, [0, 1, 2])

    def words(self):
        names = ["The", "cat", "sat", "A", "dog", "ran", "fast", "Birds", "fly"]
        return [W(100 + 300 * i, 250, n) for i, n in enumerate(names)]

    def test_words_mapped_to_sentences(self):
        t = tm.map_words(self.plan, self.words(), 3000)
        self.assertFalse(t.proportional)
        self.assertEqual(len(t.words), 9)
        cat = t.words[1]
        self.assertEqual((cat.sentence, cat.lo, cat.hi), (0, 4, 7))
        self.assertEqual(self.doc.sentences[0].text[cat.lo:cat.hi], "cat")
        fast = t.words[6]
        self.assertEqual(self.doc.sentences[1].text[fast.lo:fast.hi], "fast")
        self.assertEqual((fast.start_ms, fast.end_ms), (1900.0, 2150.0))

    def test_sentence_times_from_first_and_last_word(self):
        t = tm.map_words(self.plan, self.words(), 3000)
        self.assertEqual(t.sentence_ms[0], (100.0, 950.0))
        self.assertEqual(t.sentence_ms[1], (1000.0, 2150.0))
        self.assertEqual(t.sentence_ms[2], (2200.0, 2750.0))

    def test_unfound_word_is_skipped(self):
        words = self.words()
        words.insert(2, W(400, 10, "zebra"))
        t = tm.map_words(self.plan, words, 3000)
        self.assertEqual(len(t.words), 9)
        self.assertNotIn("zebra", [self.doc.sentences[w.sentence].text[w.lo:w.hi] for w in t.words])

    def test_search_window_is_bounded(self):
        doc = doc_of("Start here.", "x " * 40 + "target is far.")
        plan = tm.plan_chunks(doc, 0, first_chunk_id=0)[0]
        t = tm.map_words(plan, [W(100, 100, "Start"), W(300, 100, "target")], 5000)
        self.assertEqual(len(t.words), 1)

    def test_skipped_words_widen_the_window(self):
        doc = doc_of("Alpha " + "unmappable " * 6 + "omega end.")
        plan = tm.plan_chunks(doc, 0, first_chunk_id=0)[0]
        words = [W(100, 50, "Alpha")] + [W(200 + i, 1, "??????????") for i in range(6)] + [W(900, 50, "omega")]
        t = tm.map_words(plan, words, 2000)
        self.assertEqual([doc.sentences[0].text[w.lo:w.hi] for w in t.words], ["Alpha", "omega"])

    def test_prefers_word_boundary(self):
        doc = doc_of("That a cat.")
        plan = tm.plan_chunks(doc, 0, first_chunk_id=0)[0]
        t = tm.map_words(plan, [W(100, 50, "a")], 1000)
        self.assertEqual((t.words[0].lo, t.words[0].hi), (5, 6))

    def test_case_insensitive_fallback(self):
        doc = doc_of("Hello WORLD.")
        plan = tm.plan_chunks(doc, 0, first_chunk_id=0)[0]
        t = tm.map_words(plan, [W(100, 50, "hello"), W(200, 50, "World")], 1000)
        self.assertEqual([(w.lo, w.hi) for w in t.words], [(0, 5), (6, 11)])

    def test_unmapped_middle_sentence_is_interpolated(self):
        words = [w for w in self.words() if w.text not in ("A", "dog", "ran", "fast")]
        t = tm.map_words(self.plan, words, 3000)
        self.assertFalse(t.has_words(1))
        lo, hi = t.sentence_ms[1]
        self.assertAlmostEqual(lo, 950.0)
        self.assertAlmostEqual(hi, 2200.0)
        self.assertTrue(t.has_words(0))

    def test_unmapped_last_sentence_uses_length(self):
        words = [w for w in self.words() if w.text not in ("Birds", "fly")]
        t = tm.map_words(self.plan, words, 3000)
        self.assertEqual(t.sentence_ms[2], (2150.0, 3000.0))

    def test_unmapped_last_sentence_without_length_is_estimated(self):
        words = [w for w in self.words() if w.text not in ("Birds", "fly")]
        t = tm.map_words(self.plan, words, None)
        lo, hi = t.sentence_ms[2]
        self.assertEqual(lo, 2150.0)
        self.assertGreater(hi, lo)

    def test_no_words_is_proportional(self):
        t = tm.map_words(self.plan, [], 2700)
        self.assertTrue(t.proportional)
        self.assertEqual(t.words, [])
        self.assertEqual(t.sentence_ms[0][0], 0.0)
        self.assertAlmostEqual(t.sentence_ms[2][1], 2700.0)
        lens = [len(s.text) for s in self.doc.sentences]
        share = (t.sentence_ms[1][1] - t.sentence_ms[1][0]) / 2700
        self.assertAlmostEqual(share, lens[1] / sum(lens), places=6)

    def test_no_words_no_length(self):
        t = tm.map_words(self.plan, [], None)
        self.assertTrue(t.proportional)
        self.assertEqual(t.sentence_ms, {})

    def test_sentence_at_and_word_at(self):
        t = tm.map_words(self.plan, self.words(), 3000)
        self.assertEqual(t.sentence_at(0, default=0), 0)
        self.assertEqual(t.sentence_at(1500, default=0), 1)
        self.assertEqual(t.sentence_at(2900, default=0), 2)
        word, inside = t.word_at(1450, 1)
        self.assertEqual(self.doc.sentences[1].text[word.lo:word.hi], "dog")
        self.assertTrue(inside)
        word, inside = t.word_at(1580, 1)  # between "dog" (1300-1550) and "ran" (1600)
        self.assertEqual(self.doc.sentences[1].text[word.lo:word.hi], "dog")
        self.assertFalse(inside)

    def test_word_at_for_sentence_without_words(self):
        words = [w for w in self.words() if w.text not in ("A", "dog", "ran", "fast")]
        t = tm.map_words(self.plan, words, 3000)
        self.assertEqual(t.word_at(1500, 1), (None, False))

    def test_words_are_in_order_and_within_sentences(self):
        t = tm.map_words(self.plan, self.words(), 3000)
        starts = [w.start_ms for w in t.words]
        self.assertEqual(starts, sorted(starts))
        for w in t.words:
            self.assertLessEqual(w.hi, len(self.doc.sentences[w.sentence].text))
            self.assertLess(w.lo, w.hi)


class AcademicPipelineTests(unittest.TestCase):
    """text_model on top of academic_text: display text vs spoken chunk text."""

    def setUp(self):
        self.doc = tm.Document.from_raw("Introduction\n\nThe study begins here. It continues.")

    def test_heading_is_its_own_sentence_and_display_has_no_added_stop(self):
        self.assertEqual([s.text for s in self.doc.sentences],
                         ["Introduction", "The study begins here.", "It continues."])
        self.assertEqual(self.doc.text, "Introduction\n\nThe study begins here. It continues.")

    def test_heading_gets_a_spoken_full_stop_with_exact_offsets(self):
        p = tm.plan_chunks(self.doc, 0, first_chunk_id=0)[0]
        self.assertEqual(p.text, "Introduction. The study begins here. It continues.")
        for j, sid in enumerate(p.sentence_ids):
            a, b = p.sentence_bounds(j)
            self.assertEqual(p.text[a:b], self.doc.sentences[sid].text)
        self.assertEqual(p.text, tm.chunk_text([s.text for s in self.doc.sentences], [0, 1, 2]))

    def test_words_after_a_heading_map_to_the_right_sentence(self):
        p = tm.plan_chunks(self.doc, 0, first_chunk_id=0)[0]
        words = [W(100, 400, "Introduction"), W(900, 100, "The"), W(1000, 200, "study")]
        t = tm.map_words(p, words, 3000)
        self.assertEqual([(w.sentence, w.lo, w.hi) for w in t.words], [(0, 0, 12), (1, 0, 3), (1, 4, 9)])

    def test_word_only_found_in_an_added_stop_is_not_mapped(self):
        doc = tm.Document.from_raw("Heading\n\nNext one.")
        p = tm.plan_chunks(doc, 0, first_chunk_id=0)[0]
        self.assertEqual(p.text, "Heading. Next one.")
        t = tm.map_words(p, [W(100, 300, "Heading"), W(450, 10, "."), W(600, 100, "Next")], 2000)
        for w in t.words:
            self.assertLess(w.lo, w.hi)
            self.assertLessEqual(w.hi, len(doc.sentences[w.sentence].text))
        self.assertEqual([(w.sentence, w.lo, w.hi) for w in t.words if w.sentence == 0], [(0, 0, 7)])
        self.assertIn((1, 0, 4), [(w.sentence, w.lo, w.hi) for w in t.words])

    def test_chunk_text_respects_limits_with_added_stops(self):
        raw = "\n\n".join(f"Heading number {i}\n\nBody sentence {i} is here." for i in range(80))
        doc = tm.Document.from_raw(raw)
        plans = tm.plan_chunks(doc, 0, first_limit=250, limit=1500, first_chunk_id=0)
        self.assertLessEqual(len(plans[0].text), 250)
        self.assertTrue(all(len(p.text) <= 1500 for p in plans))
        self.assertEqual([sid for p in plans for sid in p.sentence_ids], list(range(len(doc.sentences))))

    def test_skip_flags_reach_normalize(self):
        raw = "Results were strong (Smith, 2019). See https://example.org/page for more."
        self.assertNotIn("Smith", tm.Document.from_raw(raw).text)
        self.assertIn("Smith", tm.Document.from_raw(raw, skip_citations=False).text)
        self.assertNotIn("example.org", tm.Document.from_raw(raw).text)
        self.assertIn("example.org", tm.Document.from_raw(raw, skip_urls=False).text)


if __name__ == "__main__":
    unittest.main()

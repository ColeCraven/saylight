"""Tests for ocr/layout.py: paragraphs from line geometry alone (a new column, a gap, an indent), and word boxes
mapped back onto a built reading's sentences, through citations, misreads and split words."""

import unittest

from PySide6.QtCore import QRectF

from saylight.ocr.layout import map_boxes, paragraphs, to_text
from saylight.ocr.model import OcrLine, OcrResult, OcrText, OcrWord
from saylight.text_model import BuildOptions, Document


def line(text: str, x: float, y: float, h: float = 12.0, adv: float = 7.0) -> OcrLine:
    words, cx = [], x
    for w in text.split():
        words.append(OcrWord(w, QRectF(cx, y, len(w) * adv, h)))
        cx += (len(w) + 1) * adv
    return OcrLine(tuple(words))


def result(*lines: OcrLine) -> OcrResult:
    return OcrResult(tuple(lines), 0.0, "en-US")


class ParagraphTests(unittest.TestCase):
    def test_lines_join_and_gaps_break(self) -> None:
        r = result(line("The first paragraph runs", 10, 10), line("over two lines.", 10, 28),
                   line("A second one starts", 10, 64), line("after a gap.", 10, 82))
        self.assertEqual(to_text(r).text,
                         "The first paragraph runs\nover two lines.\n\nA second one starts\nafter a gap.")

    def test_an_indent_breaks(self) -> None:
        r = result(line("Ends here.", 10, 10), line("Indented start of the next", 40, 28),
                   line("paragraph goes on.", 10, 46))
        self.assertEqual([len(p) for p in paragraphs(r)], [1, 2])

    def test_a_new_column_breaks(self) -> None:
        r = result(line("Left column line one", 10, 10), line("left column line two", 10, 28),
                   line("left column line three", 10, 46), line("Right column top", 300, 10),
                   line("right column next", 300, 28))
        self.assertEqual([len(p) for p in paragraphs(r)], [3, 2])

    def test_words_follow_the_text(self) -> None:
        r = result(line("One two", 10, 10), line("three", 10, 28), line("Four", 10, 80))
        t = to_text(r)
        self.assertEqual([w.text for w in t.words], t.text.split())
        self.assertEqual(to_text(result()), OcrText("", ()))
        self.assertEqual(to_text(result(OcrLine(()))), OcrText("", ()))


class MapBoxesTests(unittest.TestCase):
    def build(self, t: OcrText) -> Document:
        return Document.build(t.text, BuildOptions())

    def check_offsets(self, doc: Document, boxes, words: OcrText) -> None:
        by_rect = {(w.rect.x(), w.rect.y()): w.text for w in words.words}
        for k, items in boxes.items():
            sent = doc.sentences[k]
            for lo, hi, rect in items:
                shown = sent.text[lo:hi]
                self.assertTrue(shown and shown[0].isalnum() and shown[-1].isalnum(), shown)
                self.assertIn(shown.casefold(), by_rect[(rect.x(), rect.y())].casefold())

    def test_every_word_gets_its_box(self) -> None:
        t = to_text(result(line("Reading aloud helps. It also", 10, 10), line("keeps your place on the page.", 10, 28)))
        doc = self.build(t)
        boxes = map_boxes(doc, t)
        self.assertEqual(sorted(boxes), [0, 1])
        self.assertEqual(sum(len(v) for v in boxes.values()), len(t.words))
        self.check_offsets(doc, boxes, t)
        self.assertEqual(boxes[1][0][:2], (0, 2))  # "It"

    def test_citations_stay_mapped_while_dimmed(self) -> None:
        t = to_text(result(line("Effects vary (Smith et al., 2019) in adults.", 10, 10)))
        doc = self.build(t)
        boxes = map_boxes(doc, t)
        shown = [doc.sentences[0].text[lo:hi] for lo, hi, _ in boxes[0]]
        self.assertEqual(shown, ["Effects", "vary", "Smith", "et", "al", "2019", "in", "adults"])

    def test_a_misread_word_keeps_its_box_and_a_split_word_gets_both(self) -> None:
        t = to_text(result(line("The quick brovvn fox jumps over the exam-", 10, 10),
                           line("ple dog.", 10, 28)))
        doc = Document.build("The quick brown fox jumps over the example dog.", BuildOptions())
        boxes = map_boxes(doc, t)[0]
        sent = doc.sentences[0].text
        by_word = {}
        for lo, hi, rect in boxes:
            by_word.setdefault(sent[lo:hi], []).append(rect)
        self.assertEqual(by_word["brown"][0].x(), t.words[2].rect.x())
        self.assertEqual([r.y() for r in by_word["example"]], [10, 28])

    def test_nothing_to_map(self) -> None:
        doc = Document.build("Some text.", BuildOptions())
        self.assertEqual(map_boxes(doc, OcrText("", ())), {})
        self.assertEqual(map_boxes(Document.build("", BuildOptions()), to_text(result(line("Hi", 0, 0)))), {})


if __name__ == "__main__":
    unittest.main()

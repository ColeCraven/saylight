"""Tests for the reading card (ui/sentence_view.py): high-contrast sentence ring, the "Back to current" chip,
no-break glue, the action chips, card hover and message fit rule of the empty card, and text shown but not
read aloud: its colour, tooltips and click targets, section chips and rules, the corner chip, words under the
pointer and the paragraph-by-paragraph layout of long documents.

Widgets are rendered into a QImage and never shown.
"""

import sys
import unittest
from collections import namedtuple
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QWheelEvent
from PySide6.QtWidgets import QApplication

from saylight.text_model import Document
from saylight.theme import Theme
from saylight.ui.controls import measure_parts
from saylight.ui.sentence_view import _GLUE, REGION_TIPS, SentenceView, word_bounds

_app = None

TEXT = ("Working memory capacity predicts reading comprehension in adults with dyslexia. In Experiment 2, "
        "participants (n = 42) read passages while listening to synchronised speech. Comprehension improved.")
HC_BLACK = {"window": (0, 0, 0), "windowtext": (255, 255, 255), "highlight": (26, 235, 255),
            "highlighttext": (0, 0, 0), "graytext": (63, 242, 63)}


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class FakeSettings(QObject):
    changed = Signal(str)

    def __init__(self, **values: str) -> None:
        super().__init__()
        self.values = {"text_size": "medium", "reading_tint": "auto", "reading_font": "verdana", "spacing": "wide",
                       "highlight": "word", **values}

    def get(self, key: str) -> str:
        return self.values[key]


class FakeSystem:
    def __init__(self, *, hc: bool) -> None:
        self.hc = hc

    def color_scheme_dark(self) -> bool:
        return True

    def apps_use_light_theme(self) -> bool:
        return False

    def system_uses_light_theme(self) -> bool:
        return False

    def high_contrast(self) -> bool:
        return self.hc

    def animations_enabled(self) -> bool:
        return False

    def text_scale_factor(self) -> float:
        return 1.0

    def accent_palette(self) -> None:
        return None

    def color_prevalence(self) -> bool:
        return False

    def sys_colors(self) -> dict:
        return dict(HC_BLACK)

    def font_families(self) -> frozenset[str]:
        return frozenset({"Verdana"})


def render(view: SentenceView) -> QImage:
    img = QImage(view.size(), QImage.Format.Format_ARGB32)
    img.fill(0)
    view.render(img)
    return img


class TestHighContrastSentence(unittest.TestCase):
    def test_current_sentence_is_ringed(self) -> None:
        """With Highlight = Sentence, high contrast still marks the current sentence (a WindowText ring)."""
        settings = FakeSettings(highlight="sentence")
        theme = Theme(settings, system=FakeSystem(hc=True))
        view = SentenceView(theme, settings)
        view.resize(416, view.preferred_height())
        doc = Document.from_raw(TEXT)
        view.set_document(doc)
        view.show_text()
        view.swap.snap(1.0)
        view.set_position(1, 0, 0)
        img = render(view)
        s = doc.sentences[1]
        origin = QPointF(view.pad_x(), view.pad_y() - view.scroll.value)
        top = int(view._band_path(origin, s.start, s.end).boundingRect().top())
        surface = theme.color("Surface").rgb()
        # The band's top edge runs above the ink, so on that pixel row only the ring can differ from Surface.
        ring = [QColor(img.pixel(x, top)) for x in range(img.width()) if img.pixel(x, top) != surface]
        self.assertGreater(len(ring), 20)
        self.assertGreater(max(c.lightness() for c in ring), 128)  # WindowText (white), not a Window fill


class TestFollow(unittest.TestCase):
    def wheel(self, view: SentenceView, dy: int) -> None:
        c = QPointF(view.width() / 2, view.height() / 2)
        view.wheelEvent(QWheelEvent(c, c, QPoint(0, 0), QPoint(0, dy), Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))

    def test_back_to_current_chip_only_after_a_real_scroll(self) -> None:
        """A wheel step with nothing to scroll keeps following the voice; one that scrolls shows the chip."""
        settings = FakeSettings()
        view = SentenceView(Theme(settings, system=FakeSystem(hc=False)), settings)
        view.resize(416, view.preferred_height())
        view.set_document(Document.from_raw(TEXT))
        view.show_text()
        view.set_position(0, 0, 0)
        self.wheel(view, 120)  # already at the top
        self.assertFalse(view.chip_visible())
        self.wheel(view, -120)
        self.assertTrue(view.chip_visible())
        view.follow_now()
        self.assertFalse(view.chip_visible())


class TestGlue(unittest.TestCase):
    def glue(self, text: str) -> str:
        out = _GLUE.sub("\u00a0", text)
        self.assertEqual(len(out), len(text))  # offsets into the document stay valid
        return out

    def test_statistics_stay_together(self) -> None:
        self.assertEqual(self.glue("participants (n = 42) read"), "participants (n\u00a0=\u00a042) read")
        self.assertEqual(self.glue("F(2, 57) = 4.31, p = .018, and"),
                         "F(2,\u00a057)\u00a0=\u00a04.31, p\u00a0=\u00a0.018, and")
        self.assertEqual(self.glue("p < .05 and r ≈ 0.4"), "p\u00a0<\u00a0.05 and r\u00a0≈\u00a00.4")

    def test_references_stay_together(self) -> None:
        self.assertEqual(self.glue("Smith et al. showed in Fig. 3 and Table 2"),
                         "Smith et\u00a0al. showed in Fig.\u00a03 and Table\u00a02")

    def test_plain_lists_still_wrap(self) -> None:
        for text in ("sessions 1, 2, 3 and 4", "it was 2, then 3", "pages 12, 13"):
            with self.subTest(text=text):
                self.assertEqual(self.glue(text), text)


TYPE_LINE = [("text", "or "), ("action", "type")]
READY = [[("text", "Highlight text anywhere, then press "), ("key", "Ctrl"), ("key", "`")], TYPE_LINE,
         [("text", "With nothing highlighted, it shows or hides this panel.")]]


def card(*, hc: bool = False, **values: str) -> SentenceView:
    settings = FakeSettings(**values)
    view = SentenceView(Theme(settings, system=FakeSystem(hc=hc)), settings)
    view.resize(416, view.preferred_height())
    return view


class TestActionChips(unittest.TestCase):
    def test_chip_width(self) -> None:
        """Glyph at +10, label at +32 in the line's own font, 10 of right padding."""
        view = card()
        t = view.theme
        for role in ("message", "body"):
            fm = QFontMetricsF(t.font(role))
            want = fm.horizontalAdvance("or ") + 32 + fm.horizontalAdvance("Type or paste text") + 10
            self.assertAlmostEqual(measure_parts(t, TYPE_LINE, role=role), want, places=3)

    def test_action_at_finds_the_painted_chip(self) -> None:
        view = card()
        view.show_messages(READY, 2)
        self.assertIsNone(view.action_at(QPointF(200, 50)))  # nothing painted yet
        render(view)
        r = view._hits["type"]
        self.assertEqual(round(r.height()), 24)  # message role: CAP_H 20 + 4
        self.assertEqual(view.action_at(r.center()), "type")
        self.assertIsNone(view.action_at(QPointF(r.right() + 4, r.center().y())))
        view.set_document(Document.from_raw(TEXT))
        view.show_text()
        self.assertIsNone(view.action_at(r.center()))

    def chip_fill(self, view: SentenceView) -> int:
        img = render(view)
        r = view._hits["type"]
        return img.pixel(int(r.right() - 4), int(r.center().y()))  # the right padding: fill only

    def test_hover_and_press_fill_but_never_when_disabled(self) -> None:
        view = card()
        view.show_messages([TYPE_LINE])
        rest = self.chip_fill(view)
        view.set_action_state("type", True, False, True)
        hover = self.chip_fill(view)
        view.set_action_state("type", True, True, True)
        pressed = self.chip_fill(view)
        self.assertEqual(len({rest, hover, pressed}), 3)
        lightness = [QColor(c).lightness() for c in (rest, hover, pressed)]
        self.assertLess(lightness[0], lightness[1])  # soft dark: the ink is light, so each step lightens
        self.assertLess(lightness[1], lightness[2])
        view.set_action_state("type", True, True, False)
        self.assertEqual(self.chip_fill(view), rest)

    def test_high_contrast_hover_is_an_outline(self) -> None:
        view = card(hc=True)
        view.show_messages([TYPE_LINE])
        rest = self.chip_fill(view)
        view.set_action_state("type", True, False, True)
        self.assertEqual(self.chip_fill(view), rest)  # no blended fill
        img = render(view)
        r = view._hits["type"]
        edge = QColor(img.pixel(int(r.center().x()), int(r.top() + 0.5)))
        self.assertEqual(edge.rgb(), QColor(*HC_BLACK["highlight"]).rgb())


class TestCardHover(unittest.TestCase):
    def test_outline_and_ibeam(self) -> None:
        view = card()
        view.show_messages(READY, 2)
        before = render(view)
        view.set_card_hover(True)
        self.assertEqual(view.cursor().shape(), Qt.CursorShape.IBeamCursor)
        after = render(view)
        mid = view.width() // 2
        self.assertNotEqual(before.pixel(mid, 0), after.pixel(mid, 0))
        self.assertEqual(before.pixel(mid, 30), after.pixel(mid, 30))  # only the edge changes
        view.set_card_hover(False)
        self.assertEqual(view.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.assertEqual(render(view).pixel(mid, 0), before.pixel(mid, 0))


class TestMessageFit(unittest.TestCase):
    def test_message_role_only_when_the_lines_fit_the_rows(self) -> None:
        """Small text with Standard spacing leaves 84 DIP of rows: three 28 DIP message lines fit, four don't."""
        view = card(text_size="small", spacing="standard")
        self.assertEqual(view.height() - 2 * view.pad_y(), 84)
        self.assertEqual(view._message_role(READY), "message")
        self.assertEqual(view._message_role(READY + [[("text", "One more line.")]]), "body")

    def test_body_role_when_a_line_is_too_wide(self) -> None:
        view = card()
        self.assertEqual(view._message_role([[("text", "word " * 30)]]), "body")


# --- Text shown but not read aloud, sections, corner chips, words ---------------------------------

Region = namedtuple("Region", "start end kind")
Edit = namedtuple("Edit", "start end spoken kind")
CITED = "Reading improved with synchronised speech (Smith et al., 2020). The effect lasted for weeks."
NOTES_TAIL = "\n\nNotes\n\nA note that is shown but not read aloud."


def cited_doc() -> Document:
    """One citation in the first sentence, marked as a skip region (as the two-text model gives it)."""
    doc = Document.from_raw(CITED, skip_citations=False)
    a = doc.text.index("(Smith")
    doc.regions = [Region(a, doc.text.index(")", a) + 1, "skip")]
    return doc


def notes_doc(read: bool = False) -> Document:
    """Two sentences, then a Notes section that is skipped (an aside region) or, when `read`, only listed."""
    body = Document.from_raw("The first sentence is here. The second one follows it.")
    doc = Document(body.text + NOTES_TAIL, body.sentences)
    start = doc.text.index("Notes")
    doc.regions = [] if read else [Region(start, len(doc.text), "aside")]
    if read:
        doc.sections = {"notes": start}
    return doc


def text_card(doc: Document, *, hc: bool = False, sentence: int = 0, **values: str) -> SentenceView:
    view = card(hc=hc, **values)
    view.set_document(doc)
    view.show_text()
    view.swap.snap(1.0)
    view.set_position(sentence, 0, 0)
    return view


def point_of(view: SentenceView, offset: int) -> QPointF:
    """The middle of the glyph at `offset`, in view coordinates."""
    lay = view._layout
    row = lay.line_of(offset)
    line = lay.lineAt(row)
    x1, x2 = line.cursorToX(offset), line.cursorToX(offset + 1)
    lh = view.line_height()
    return QPointF(view.pad_x() + (x1 + x2) / 2, view.pad_y() - view.scroll.value + row * lh + lh / 2)


def colour_pixels(img: QImage, color: QColor, rect: QRectF | None = None) -> int:
    rgb = color.rgb()
    r = (rect or QRectF(img.rect())).toAlignedRect().intersected(img.rect())
    return sum(1 for y in range(r.top(), r.bottom() + 1) for x in range(r.left(), r.right() + 1)
               if img.pixel(x, y) == rgb)


def span_rect(view: SentenceView, a: int, b: int) -> QRectF:
    """The rows' rectangle around shown text [a, b) on one row."""
    p1, p2 = point_of(view, a), point_of(view, b - 1)
    lh = view.line_height()
    return QRectF(p1.x() - 6, p1.y() - lh / 2, p2.x() - p1.x() + 12, lh)


class TestSkippedText(unittest.TestCase):
    def test_painted_in_the_skipped_colour_in_every_style(self) -> None:
        for style in ("active", "paused", "muted", "stopped", "finished"):
            with self.subTest(style):
                doc = cited_doc()
                view = text_card(doc, sentence=1)  # the cited sentence is an earlier one: dimmed, then skipped
                view.set_style(style)
                colors = view.theme.reading_colors()
                img = render(view)
                a, b, _ = view._dim[0]
                inside = span_rect(view, a, b)
                self.assertGreater(colour_pixels(img, colors["skipped"], inside), 5)
                self.assertEqual(colour_pixels(img, colors["skipped"]), colour_pixels(img, colors["skipped"], inside))
                if style != "finished":  # the rest of the earlier sentence keeps the earlier-sentence colour
                    self.assertGreater(colour_pixels(img, colors["secondary"], span_rect(view, 0, 7)), 5)

    def test_high_contrast_uses_gray_text(self) -> None:
        view = text_card(cited_doc(), hc=True)
        a, b, _ = view._dim[0]
        gray = QColor(*HC_BLACK["graytext"])
        self.assertEqual(view.theme.reading_colors()["skipped"], gray)
        self.assertGreater(colour_pixels(render(view), gray, span_rect(view, a, b)), 5)

    def test_tooltips(self) -> None:
        doc = cited_doc()
        s = doc.sentences[1]
        said = doc.text.index("weeks")
        doc.regions.append(Region(said, said + 5, "said"))
        s.align = SimpleNamespace(edits=[Edit(said - s.start, said - s.start + 5, "wicks", "say")])
        view = text_card(doc)
        self.assertEqual(view.tip_at(point_of(view, view._dim[0][0] + 2)), REGION_TIPS["skip"])
        self.assertEqual(view.tip_at(point_of(view, said + 1)), "Read aloud as “wicks”.")
        self.assertEqual(view.tip_at(point_of(view, 2)), "")
        view.show_messages([[("text", "Ready")]])
        self.assertEqual(view.tip_at(point_of(view, 2)), "")

    def test_inline_citations_belong_to_their_sentence_but_notes_take_no_clicks(self) -> None:
        view = text_card(cited_doc())
        self.assertEqual(view.sentence_at(point_of(view, view._dim[0][0] + 2)), 0)
        doc = notes_doc()
        view = text_card(doc, sentence=1)
        note = doc.text.index("A note")
        self.assertIsNone(view.sentence_at(point_of(view, note + 3)))
        self.assertEqual(view.tip_at(point_of(view, note + 3)), REGION_TIPS["aside"])
        self.assertEqual(view.sentence_at(point_of(view, 3)), 0)


class TestWords(unittest.TestCase):
    def test_word_bounds_trim_punctuation(self) -> None:
        text = "“Hello,” she said (e.g. fMRI). Don't—no; COVID-19… ok"
        for word, expected in (("Hello", "Hello"), ("e.g", "e.g"), ("fMRI", "fMRI"), ("Don't", "Don't—no"),
                               ("COVID", "COVID-19"), ("ok", "ok")):
            a, b = word_bounds(text, text.index(word) + 1)
            self.assertEqual(text[a:b], expected)
        self.assertIsNone(word_bounds(text, text.index(" she")))
        self.assertIsNone(word_bounds("a , b", 2))
        self.assertIsNone(word_bounds("abc", 5))

    def test_word_at_the_pointer(self) -> None:
        doc = cited_doc()
        view = text_card(doc)
        start = doc.text.index("synchronised")
        self.assertEqual(view.word_at(point_of(view, start + 4)), (start, start + len("synchronised")))
        a = doc.text.index("Smith")
        self.assertEqual(view.word_at(point_of(view, a)), (a, a + 5))  # "(Smith" without the bracket
        self.assertIsNone(view.word_at(point_of(view, doc.text.index(" with"))))  # a space
        self.assertIsNone(view.word_at(QPointF(view.width() - 3, 3)))
        got = []
        view.pronunciationRequested.connect(got.append)
        self.assertTrue(view.request_pronunciation(point_of(view, start)))
        self.assertEqual(got, ["synchronised"])
        view.show_messages([[("text", "Ready")]])
        self.assertIsNone(view.word_at(point_of(view, start)))


class TestSections(unittest.TestCase):
    def test_chip_and_rule_on_the_blank_row_before_the_notes(self) -> None:
        doc = notes_doc()
        view = text_card(doc)
        (section, rect), = view._section_rects()
        heading_row = view._layout.line_of(doc.text.index("Notes"))
        self.assertEqual(view._section_row(section), heading_row - 1)
        lh = view.line_height()
        self.assertAlmostEqual(rect.right(), view.width() - 14)
        self.assertEqual(round(rect.height()), min(24, lh - 2))
        self.assertAlmostEqual(rect.center().y(), view.pad_y() + (heading_row - 1) * lh + lh / 2)
        self.assertEqual(view.action_at(rect.center()), "section:notes")
        self.assertEqual(view.tip_at(rect.center()), "Read the notes after the main text, for this file only.")
        self.assertIn("Read the notes too", view.accessibleDescription())
        img = render(view)
        y = int(rect.center().y())
        skipped = view.theme.reading_colors()["skipped"].rgb()
        self.assertEqual(img.pixel(view.pad_x() + 20, y), skipped)       # the 48 DIP rule
        self.assertNotEqual(img.pixel(view.pad_x() + 60, y), skipped)

    def test_a_section_read_aloud_offers_to_skip_it_again(self) -> None:
        view = text_card(notes_doc(read=True))
        (section, rect), = view._section_rects()
        self.assertTrue(section.read)
        self.assertEqual(view.tip_at(rect.center()), "Skip them again, for this file.")
        self.assertIn("Skip the notes", view.accessibleDescription())

    def test_rebuild_keeps_the_place_and_crossfades_the_label(self) -> None:
        view = text_card(notes_doc(), sentence=1)
        view.scroll.snap(view.line_height())
        layout = view._layout
        view.set_document(notes_doc(read=True))  # same shown text: a rebuild
        self.assertIs(view._layout, layout)
        self.assertEqual((view.sentence, view.scroll.value), (1, view.line_height()))
        self.assertEqual(view._section_prev["notes"], "Read the notes too")
        self.assertEqual(view._sections[0].read, True)

    def test_hidden_under_a_corner_chip(self) -> None:
        doc = notes_doc()
        view = text_card(doc, sentence=1)
        view.set_style("finished")
        (section, _), = view._section_rects()
        view.scroll.snap(view._section_row(section) * view.line_height())  # its row scrolled to the top
        self.assertEqual(len(view._section_rects()), 1)
        view.set_corner_chip("start_over", "Start from the beginning")
        self.assertIsNotNone(view.corner_rect())
        self.assertEqual(view._section_rects(), [])
        self.assertIsNone(view.action_at(view.corner_rect().center() - QPointF(view.corner_rect().width(), 0)))


LONG = (CITED + " Readers kept their place more often when each word was marked as it was spoken. The gain was "
        "largest for the longest passages, and it did not fade over the weeks of the study.")


class TestCornerChip(unittest.TestCase):
    def test_waits_while_it_would_cover_unread_text(self) -> None:
        """Row 1 holds text already read, except at the very start: there the chip waits for the reading."""
        view = text_card(Document.from_raw(LONG), sentence=0)
        view.set_corner_chip("back", "Back to page 2")
        self.assertIsNone(view.corner_rect())  # the first row is still to be read
        self.assertIsNone(view.action_at(QPointF(view.width() - 20, 20)))
        view.set_position(2, 0, 0)
        self.assertIsNotNone(view.corner_rect())
        view.set_style("finished")
        view.set_position(0, 0, 0)
        self.assertIsNotNone(view.corner_rect())  # nothing is unread once finished

    def test_top_right_slot(self) -> None:
        view = text_card(Document.from_raw(LONG), sentence=2)
        self.assertIsNone(view.corner_rect())
        tip = "Read “Smith 2024” from the first page"
        view.set_corner_chip("start_over", "Start from the beginning", tooltip=tip)
        r = view.corner_rect()
        w = QFontMetricsF(view.theme.font("small")).horizontalAdvance("Start from the beginning") + 20
        self.assertEqual((r.right(), r.top(), r.height()), (view.width() - 8, 8, 24))
        self.assertAlmostEqual(r.width(), w)
        self.assertLess(r.bottom(), view.pad_y() + view.line_height() + 1)  # row 1 only: earlier text
        self.assertEqual(view.action_at(r.center()), "corner:start_over")
        self.assertEqual(view.tip_at(r.center()), tip)
        self.assertEqual(view.track_pointer(r.center()), "corner:start_over")
        self.assertEqual(view.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.assertEqual(view._chip_hover["corner:start_over"].target, 1.0)
        view.set_corner_chip("remembered", "Saylight will remember your place", clickable=False)
        self.assertIsNone(view.action_at(view.corner_rect().center()))
        self.assertEqual(view._corner_prev, "Start from the beginning")  # a new label crossfades
        view.show_messages([[("text", "Ready")]])
        self.assertIsNone(view.corner_rect())
        view.show_text()
        view.set_corner_chip(None)
        self.assertIsNone(view.corner_rect())

    def test_hover_and_press_paint(self) -> None:
        view = text_card(Document.from_raw(LONG), sentence=2)
        view.set_corner_chip("start_over", "Start from the beginning")
        r = view.corner_rect()
        at = (int(r.left() + 4), int(r.center().y()))
        rest = render(view).pixel(*at)
        view.set_action_state("corner:start_over", True, False, True)
        hover = render(view).pixel(*at)
        view.set_action_state("corner:start_over", True, True, True)
        pressed = render(view).pixel(*at)
        self.assertEqual(len({rest, hover, pressed}), 3)


class TestActionLabels(unittest.TestCase):
    def test_label_override_sets_the_chip_width(self) -> None:
        view = card()
        line = [("text", "or "), ("action", "continue")]
        view.show_messages([line])
        view.set_action_label("continue", "Continue “Smith 2024” from page 14")
        render(view)
        fm = QFontMetricsF(view.theme.font("message"))
        self.assertAlmostEqual(view._hits["continue"].width(),
                               32 + fm.horizontalAdvance("Continue “Smith 2024” from page 14") + 10, places=3)
        self.assertEqual(view.accessibleDescription(), "Actions: Continue “Smith 2024” from page 14.")


class TestLongDocuments(unittest.TestCase):
    def test_laid_out_as_far_as_needed_then_while_idle(self) -> None:
        para = "Working memory capacity predicts reading comprehension in adults with dyslexia. " * 4
        doc = Document.from_raw("\n\n".join([para.strip()] * 400))
        view = text_card(doc)
        lay = view._layout
        self.assertFalse(lay.done)
        laid = len(lay.rows)
        self.assertLess(laid, 60)
        late = len(doc.sentences) - 3
        view.set_position(late, 0, 0)  # jumping far ahead lays out up to there
        self.assertGreater(len(lay.rows), laid)
        while not lay.step():
            pass
        full = SentenceView(view.theme, view.settings)
        full.resize(view.size())
        full.set_document(doc)
        full._layout.ensure_rows(10 ** 9)
        self.assertEqual(lay.lineCount(), full._layout.lineCount())
        self.assertEqual(lay.starts, full._layout.starts)
        self.assertEqual(lay.line_of(doc.sentences[late].start), full._layout.line_of(doc.sentences[late].start))
        render(view)  # paints only the rows on screen


if __name__ == "__main__":
    unittest.main()

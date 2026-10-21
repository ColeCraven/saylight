"""Tests for the reading card (ui/sentence_view.py): high-contrast sentence ring, the "Back to current" chip,
no-break glue, and the action chips, card hover and message fit rule of the empty card.

Widgets are rendered into a QImage and never shown.
"""

import sys
import unittest

from PySide6.QtCore import QCoreApplication, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QWheelEvent
from PySide6.QtWidgets import QApplication

from saylight.text_model import Document
from saylight.theme import Theme
from saylight.ui.controls import measure_parts
from saylight.ui.sentence_view import _GLUE, SentenceView

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


if __name__ == "__main__":
    unittest.main()

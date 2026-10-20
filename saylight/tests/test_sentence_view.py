"""Tests for the reading card (ui/sentence_view.py): high-contrast sentence ring, the "Back to current" chip
and no-break glue.

Widgets are rendered into a QImage and never shown.
"""

import sys
import unittest

from PySide6.QtCore import QCoreApplication, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QWheelEvent
from PySide6.QtWidgets import QApplication

from saylight.text_model import Document
from saylight.theme import Theme
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


if __name__ == "__main__":
    unittest.main()

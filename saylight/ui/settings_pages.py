"""The panel's settings page: a tab bar in the header and one tab of rows below it.

Every tab shares one height (the tallest tab's), so the tab bar never moves under the pointer. A tab taller than
the room scrolls, with the reading card's 6 DIP fades at its edges. Rows follow the panel's grid: the plate inset
12, labels at the optical inset 22, controls in one column from 132, rows 40 tall on a 42 DIP pitch.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from .controls import FadeLabel, Segmented, SwatchRow, ToggleRow
from .motion import MEDIUM, Tween, decelerate, with_alpha

EDGE = 12        # the plate inset
OPTICAL = 22     # labels and text start here
COLUMN = 132     # every row's control starts here
PITCH = 42       # one 40 DIP row and its 2 DIP gap
FADE = 6         # the edge fades of a scrolled tab

# Reading display options: (setting value, segment label, label px, tooltip[, label font family]).
TEXT_SIZES = (("small", "Aa", 12, "Small text"), ("medium", "Aa", 14, "Medium text (default)"),
              ("large", "Aa", 17, "Large text"), ("xlarge", "Aa", 20, "Extra large text"))
FONTS = (("verdana", "Verdana", 13, "Verdana (default): large letters, clear I l 1 and O 0", "Verdana"),
         ("segoe", "Segoe", 13, "Segoe UI, the Windows interface font", "Segoe UI Variable Text"),
         ("calibri", "Calibri", 14, "Calibri", "Calibri"),
         ("sitka", "Sitka", 13, "Sitka Small, a serif designed for legibility", "Sitka Small"))
SPACINGS = (("standard", "Standard", 13, "Standard letter and word spacing"),
            ("wide", "Wide", 13, "Slightly wider letters and words (default)"),
            ("extra", "Extra wide", 13, "Extra wide spacing and taller lines"))
HIGHLIGHTS = (("word", "Word", 13, "Highlight the sentence and the word being read (default)"),
              ("sentence", "Sentence", 13, "Highlight only the sentence being read"),
              ("off", "Off", 13, "No highlight"))
TINT_SHORT = {"auto": "Automatic", "cream": "Cream", "peach": "Peach", "blue": "Soft blue", "dark": "Soft dark"}


class SettingsPage:
    """One tab. Its widgets are children of the tab host; `layout` places them and returns the height used."""

    title = ""
    tooltip = ""

    def __init__(self, host: QWidget, ctl) -> None:
        self.host = host
        self.ctl = ctl
        self.theme = ctl.theme
        self.tips = ctl.tips
        self.settings = ctl.settings
        self.rules: list[int] = []   # y of each divider, in host coordinates

    def widgets(self) -> list[QWidget]:
        raise NotImplementedError

    def layout(self, x: int, y: int, w: int, fill: int = 0) -> int:
        """Place the rows from (x, y) in a page `w` wide; returns the height from y to the bottom margin.
        `fill` is the height every tab gets, which a page may stretch one element to use."""
        raise NotImplementedError

    def sync(self) -> None:
        """Show the current settings."""

    def on_state(self, state: str, info: dict) -> None:
        """The engine's state, as the panel shows it."""

    def set_shown(self, on: bool) -> None:
        """The tab became the one on screen, or stopped being it."""

    def label(self, text: str) -> FadeLabel:
        lbl = FadeLabel(self.theme, "body", "TextPrimary", self.host)
        lbl.set_text(text, animate=False)
        return lbl


class DisplayPage(SettingsPage):
    """Reading display: text size, font, spacing, highlight and background, and the Reading aloud toggles."""

    title = "Reading display"
    tooltip = "Text size, font, spacing, highlight and background"
    KEYS = ("text_size", "reading_font", "spacing", "highlight", "reading_tint", "skip_citations", "skip_urls")

    def __init__(self, host: QWidget, ctl) -> None:
        super().__init__(host, ctl)
        t, tips = self.theme, self.tips

        def segmented(options, name: str) -> Segmented:
            return Segmented(t, tips, [opt[1:] for opt in options], host, name=name)

        self.option_rows = [
            ("text_size", self.label("Text size"), segmented(TEXT_SIZES, "Text size"), TEXT_SIZES),
            ("reading_font", self.label("Font"), segmented(FONTS, "Font"), FONTS),
            ("spacing", self.label("Spacing"), segmented(SPACINGS, "Spacing"), SPACINGS),
            ("highlight", self.label("Highlight"), segmented(HIGHLIGHTS, "Highlight"), HIGHLIGHTS),
        ]
        self.lbl_bg = self.label("Background")
        self.swatches = SwatchRow(t, tips, host)
        self.lbl_bg_name = FadeLabel(t, "small", "TextSecondary", host)  # names the chosen background
        self.lbl_group = FadeLabel(t, "small", "TextSecondary", host)
        self.lbl_group.set_text("Reading aloud · applies from the next reading", animate=False)
        self.row_citations = ToggleRow(t, tips, "Skip citations", host,
                                       tooltip="Leave out references such as (Smith et al., 2019) and [12]. "
                                               "Applies from the next reading.")
        self.row_links = ToggleRow(t, tips, "Skip links and DOIs", host,
                                   tooltip="Say “link” instead of reading web addresses and DOIs. "
                                           "Applies from the next reading.")
        s = self.settings
        for key, _lbl, seg, opts in self.option_rows:
            seg.selected.connect(lambda i, key=key, opts=opts: s.set(key, opts[i][0]))
        self.swatches.selected.connect(lambda key: s.set("reading_tint", key))
        self.row_citations.toggled.connect(lambda on: s.set("skip_citations", on))
        self.row_links.toggled.connect(lambda on: s.set("skip_urls", on))
        s.changed.connect(lambda key: self.sync() if key in self.KEYS else None)

    def widgets(self) -> list[QWidget]:
        out: list[QWidget] = []
        for _key, lbl, seg, _opts in self.option_rows:
            out += [lbl, seg]
        return out + [self.lbl_bg, self.swatches, self.lbl_bg_name, self.lbl_group, self.row_citations,
                      self.row_links]

    def layout(self, x: int, y: int, w: int, fill: int = 0) -> int:
        px = self.theme.px
        top = y
        row_w = w - 2 * px(EDGE)
        right = w - px(EDGE) - px(10)  # the toggle switches' right edge
        col = px(COLUMN)
        for _key, lbl, seg, _opts in self.option_rows:
            lbl.setGeometry(x + px(OPTICAL), y + px(10), col - px(OPTICAL), px(20))
            seg.setGeometry(x + col, y + px(3), right - col, px(34))
            y += px(PITCH)
        self.lbl_bg.setGeometry(x + px(OPTICAL), y + px(10), col - px(OPTICAL), px(20))
        sw_w = px(36) * max(1, len(self.swatches.items))
        self.swatches.setGeometry(x + col - px(5), y, sw_w, px(40))  # circles' left edge on the column
        name_x = self.swatches.x() + sw_w + px(8)
        self.lbl_bg_name.setGeometry(name_x, y + px(12), x + right - name_x, px(16))
        y += px(PITCH)
        self.rules = [y + px(4)]
        y += px(9)
        self.lbl_group.setGeometry(x + px(OPTICAL), y, w - 2 * px(OPTICAL), px(20))
        y += px(22)
        for row in (self.row_citations, self.row_links):
            row.setGeometry(x + px(EDGE), y, row_w, px(40))
            y += px(PITCH)
        return y - px(2) + px(EDGE) - top

    def sync(self) -> None:
        s = self.settings
        for key, _lbl, seg, opts in self.option_rows:
            keys = [o[0] for o in opts]
            value = s.get(key)
            seg.set_index(keys.index(value) if value in keys else 0)
        self.swatches.set_items(self.ctl.tint_items(), s.get("reading_tint"))
        self.lbl_bg_name.set_text(TINT_SHORT.get(s.get("reading_tint"), ""))
        self.row_citations.set_checked(bool(s.get("skip_citations")))
        self.row_links.set_checked(bool(s.get("skip_urls")))


class TabBar(Segmented):
    """The settings tabs, on the Segmented control. A screen reader hears "Settings, Voice tab"."""

    def __init__(self, theme, tips, items: list[tuple], parent: QWidget | None = None):
        super().__init__(theme, tips, items, parent, name="Settings tabs")
        self._name_tab()

    def set_index(self, i: int) -> None:
        super().set_index(i)
        self._name_tab()

    def _name_tab(self) -> None:
        label, _size, tip = self.items[self.index][:3]
        self.setAccessibleName(f"Settings, {label} tab")
        self.setAccessibleDescription(tip)


class _Fades(QWidget):
    """The 6 DIP Surface fades over a scrolled tab's top and bottom edges."""

    def __init__(self, host: "SettingsTabs"):
        super().__init__(host)
        self.host = host
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event) -> None:
        h = self.host
        t = h.theme
        top, bottom = h.scroll.value > 0.5, h.scroll.value < h.max_scroll() - 0.5
        if not (top or bottom):
            return
        p = QPainter(self)
        bg = t.color("Surface")
        fade = t.px(FADE)
        if top:
            g = QLinearGradient(0, 0, 0, fade)
            g.setColorAt(0, bg)
            g.setColorAt(1, with_alpha(bg, 0.0))
            p.fillRect(0, 0, self.width(), fade, g)
        if bottom:
            g = QLinearGradient(0, self.height() - fade, 0, self.height())
            g.setColorAt(0, with_alpha(bg, 0.0))
            g.setColorAt(1, bg)
            p.fillRect(0, self.height() - fade, self.width(), fade, g)


class SettingsTabs(QWidget):
    """The tab host below the panel's header. The tab bar itself sits in the panel's header (`bar`)."""

    def __init__(self, panel: QWidget, ctl, pages: list[type[SettingsPage]]):
        super().__init__(panel)
        t = self.theme = ctl.theme
        self.pages = [cls(self, ctl) for cls in pages]
        self.bar = TabBar(t, ctl.tips, [(p.title, 13, p.tooltip) for p in self.pages], panel)
        self.bar.selected.connect(self.set_index)
        self.index = 0
        self.active = False
        self._page_h = 0
        self._fill = 0
        self.scroll = Tween(self, t, 0.0, MEDIUM, decelerate())
        self.scroll.on_change = lambda _v: self._place()
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setEnabled(False)
        self.setGraphicsEffect(self._effect)
        self.reveal = Tween(self, t, 1.0, MEDIUM, decelerate())
        self.reveal.on_change = self._apply_reveal
        self._fades = _Fades(self)
        self._show_current()

    @property
    def page(self) -> SettingsPage:
        return self.pages[self.index]

    def set_index(self, i: int) -> None:
        if i == self.index:
            return
        self.page.set_shown(False)
        self.index = i
        self.bar.set_index(i)
        self.scroll.snap(0.0)
        self._show_current()
        self._place()
        if self.active:
            self.page.set_shown(True)
            self._effect.setEnabled(True)
            self.reveal.snap(0.0)
            self.reveal.to(1.0)

    def set_active(self, on: bool) -> None:
        """The settings page is on screen (or no longer is)."""
        if on != self.active:
            self.active = on
            self.page.set_shown(on)

    def _apply_reveal(self, v: float) -> None:
        self._effect.setOpacity(v)
        if v >= 1.0:
            self._effect.setEnabled(False)

    def _show_current(self) -> None:
        for page in self.pages:
            for w in page.widgets():
                w.setVisible(page is self.page)
        self._fades.raise_()

    def sync(self) -> None:
        for page in self.pages:
            page.sync()

    def on_state(self, state: str, info: dict) -> None:
        for page in self.pages:
            page.on_state(state, info)

    def arrange(self, w: int, top: int, max_h: int) -> int:
        """Lay out the tabs under a header ending at `top`; returns the panel height the settings page needs."""
        px = self.theme.px
        y0 = px(2)  # the first row lands on y 46, as on 1.1's page
        content = y0 + max(page.layout(0, y0, w) for page in self.pages)
        view_h = max(px(PITCH), min(content, max_h - top))
        self.setGeometry(0, top, w, view_h)
        self._fades.setGeometry(0, 0, w, view_h)
        self._fill = content - y0
        self._place()
        return top + view_h

    def max_scroll(self) -> float:
        return float(max(0, self._page_h - self.height()))

    def _place(self) -> None:
        px = self.theme.px
        y0 = px(2)
        offset = round(self.scroll.value)
        self._page_h = y0 + self.page.layout(0, y0 - offset, self.width(), self._fill)
        if self.scroll.value > self.max_scroll():
            self.scroll.snap(self.max_scroll())
            return
        self.update()
        self._fades.update()

    def wheelEvent(self, event) -> None:
        limit = self.max_scroll()
        target = max(0.0, min(limit, self.scroll.target - event.angleDelta().y() / 120 * self.theme.px(PITCH)))
        if limit <= 0 or target == self.scroll.target:
            event.ignore()
            return
        self.scroll.to(target)
        event.accept()

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setPen(QPen(t.color("ControlStroke"), 0))
        for y in self.page.rules:
            p.drawLine(QPointF(t.px(OPTICAL), y + 0.5), QPointF(self.width() - t.px(OPTICAL), y + 0.5))

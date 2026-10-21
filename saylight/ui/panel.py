"""The floating mini-player with its inline settings page.

The window is a frameless, always-on-top tool window that never activates: clicks, drags and the
wheel all work while the user's own app keeps the foreground (so Ctrl+C still reaches it).

More opens the flyout (Open a file, Save as audio, Pronunciations, Settings); a right-click on the reading page
opens it at the pointer. A file dropped on the panel opens it. The card carries the chips that open files,
continue one, enter a PDF's password or try again; the header info carries short notes for a few seconds.
"""

from __future__ import annotations

import os
import time
from ctypes import wintypes

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from .. import win32
from ..engine import span_fraction
from .controls import (ACTION_PARTS, G_BACK, G_CLEAR, G_COLLAPSE, G_DOCUMENT, G_ERROR, G_EXPAND, G_HIDE, G_LOCK,
                       G_MORE, G_NEXT, G_NOTHING, G_OFFLINE, G_PAUSE, G_PLAY, G_PREV, G_RETRY, G_TYPE, FadeLabel,
                       IconButton, Indicator, Part, PlayButton, ProgressBar, SpeedStepper, VolumeSlider,
                       measure_parts, paint_stop_square, volume_glyph)
from .flyout import Flyout, MoreButton, menu_items
from .general_page import GeneralPage, shortcut_parts
from .motion import MEDIUM, SLOW, Tween, decelerate, exit_curve, with_alpha
from .sentence_view import SentenceView
from .settings_pages import EDGE, TEXT_SIZES, DisplayPage, SettingsTabs
from .tray import SPEEDS, speed_label
from .update_bar import STRIP_H, NoticeStrip, UpdateBar
from .voice_page import VoicePage

PANEL_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
               | Qt.WindowType.WindowDoesNotAcceptFocus)

WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x0021, 3
SYSTEM_CHANGE_MESSAGES = {0x001A, 0x0015, 0x031A, 0x0320}  # SETTINGCHANGE, SYSCOLORCHANGE, THEMECHANGED, DWMCOLOR

COMPACT_W, EXPANDED_W = 440, 640   # base widths: ~36 and ~56 characters per line at the default reading size
# Larger text widens the reading page to keep the line length (up to the caps), instead of collapsing it.
WIDTH_SAMPLE = "reading comprehension in adults with dyslexia "
COMPACT_CPL, EXPANDED_CPL = 36, 56
MAX_COMPACT_W, MAX_EXPANDED_W = 600, 900
SNAP = 24
TASKBAR_GAP = 60  # bottom margin when the taskbar auto-hides (48 + 12)
WPM_AT_1X = 142   # Ava on academic prose at +0% (measured)

ACTIVE = {"copying", "opening", "preparing", "reading", "waiting", "paused"}
SESSION = {"preparing", "reading", "waiting", "paused"}
IDLE = {"ready", "ready_offline", "shortcut_unavailable"}


def K(*parts: Part) -> list[Part]:
    return list(parts)


READY_LINE = K(("text", "Highlight text anywhere, then press "), ("key", "Ctrl"), ("key", "`"))
READY_ACTIONS = K(("text", "or "), ("action", "type"), ("text", "  "), ("action", "open"))
READY_TIP = K(("text", "With nothing highlighted, it shows or hides this panel."))
CONTINUE_LINE = K(("text", "or "), ("action", "continue"))
READY_HINT = [READY_LINE, READY_ACTIONS, READY_TIP]
READY_MORE = [K(("key", "`"), ("text", " is the key below Esc, left of 1.")),
              K(("text", "Click a sentence to jump to it. Right-click a word to change how it's said.")),
              K(("text", "You can also drop a PDF, Word or text file here."))]
READY_SETTINGS = K(("text", "Text size and background are in Settings, under "), ("glyph", G_MORE), ("text", "."))
READY_HINT_EXPANDED = READY_HINT + READY_MORE + [READY_SETTINGS]
READY_SECONDARY_FROM = 2  # the two lines that say what to do are in the text colour, the notes below them dimmed
CLEARED_HINT = [K(("text", "Text cleared. "), ("action", "undo"))]
# Chips on the card's message lines, and what follows `can_type` (the rest work whenever they show).
CARD_ACTIONS = ("type", "open", "continue", "undo", "password", "retry")
TYPING_ACTIONS = ("type", "open", "continue")
OPEN_TIP = "Open a PDF, Word, text or Markdown file. You can also drop one here."
# The open notices: the indicator's look for each kind (engine.OPEN_LABELS has the labels).
NOTICE_LOOK = {"password": ("Caution", G_LOCK), "protected": ("Caution", G_LOCK), "scanned": ("Caution", G_NOTHING),
               "empty": ("Caution", G_NOTHING), "unsupported": ("Caution", G_NOTHING),
               "too_large": ("Caution", G_NOTHING), "corrupt": ("Critical", G_ERROR), "io": ("Critical", G_ERROR),
               "missing": ("Critical", G_ERROR)}
SUPPORTED = (".pdf", ".docx", ".txt", ".md", ".markdown")
NOTE_MS = 6000          # a header note shows this long
NOTE_WAIT_MS = 3000     # ...once there is room for it; a note that finds none by then is dropped
PAGE_INFO_MS = 300      # an opening PDF's "Page 40 of 160" waits this long, so quick opens never flash it
START_OVER_MS = 20_000  # "Start from the beginning" stays this long into a continued reading
BACK_MS = 10_000        # "Back to page 14" after starting over
REMEMBERED_MS = 3000    # "Saylight will remember your place"
NUDGE_LEFT = 20         # "Remember this page next time" needs at least this many sentences left
NUDGE_TIP = ("Turns on Pick up where you left off. Saylight remembers this file's location and your place, "
             "never its text.")
TYPE_LINE = K(("text", "Or "), ("action", "type"))  # under a message on an empty card a click can type into
UNDO_NOTE = " You can undo this for 10 seconds."
NOTHING_TEXT = {
    "empty": [K(("text", "Highlight some text, then click "), ("glyph", G_PLAY)),
              K(("text", "or press "), ("key", "Ctrl"), ("key", "`"), ("text", "."))],
    "shell": [K(("text", "Click in the app you want to read from,")),
              K(("text", "then click "), ("glyph", G_PLAY), ("text", " again."))],
    "console": [K(("text", "Play can't copy from a terminal.")),
                K(("text", "Select the text there and press "), ("key", "Ctrl"), ("key", "`"), ("text", "."))],
    "busy": [K(("text", "The clipboard was busy. Please try again."))],
}
NOTHING_TEXT["line_copy"] = NOTHING_TEXT["empty"]


def notice_lines(kind: str, title: str, ext: str = "", keys: list[Part] | None = None) -> tuple[list[list[Part]], int]:
    """An open notice's card lines and where the secondary colour starts. `title` is the file's title, already
    fitted to the line; `keys` is the reading shortcut as keycaps."""
    keys = keys or [("key", "Ctrl"), ("key", "`")]
    press = K(("text", "and press "), *keys, ("text", "."))
    if kind == "password":
        first = f"“{title}” needs a password." if title else "This PDF needs a password."
        return [K(("text", first)), K(("action", "password"))], 1
    if kind == "protected":
        lines = [K(("text", "This PDF is protected.")),
                 K(("text", "Open it in your PDF reader, highlight the text")), press]
        return lines, len(lines)
    if kind == "scanned":
        return [K(("text", "This PDF has no text Saylight can read.")),
                K(("text", "It's probably a scan (pictures of pages)."))], 1
    if kind == "empty":
        return [K(("text", f"“{title}” has no text to read." if title else "There's no text to read here."))], 1
    if kind == "unsupported":
        if ext == ".doc":
            return [K(("text", "Saylight can't read older Word files (.doc).")),
                    K(("text", "In Word, save it as .docx, then open that."))], 1
        return [K(("text", "Saylight can't read this kind of file.")),
                K(("text", "It reads PDF, Word, text and Markdown files."))], 1
    if kind == "too_large":
        lines = [K(("text", "This file is too large to open.")),
                 K(("text", "Open it in its own app, highlight the part you want")), press]
        return lines, len(lines)
    if kind == "io":
        return [K(("text", f"Saylight couldn't read “{title}”." if title else "Saylight couldn't read the file.")),
                K(("text", "Check that it isn't open in another app, then try again.")), K(("action", "retry"))], 1
    if kind == "missing":
        return [K(("text", f"Saylight can't find “{title}” any more." if title else "Saylight can't find the file.")),
                K(("text", "It may have been moved or renamed.")), K(("text", "or "), ("action", "open"))], 1
    return [K(("text", f"Saylight couldn't open “{title}”." if title else "Saylight couldn't open the file.")),
            K(("text", "The file may be damaged."))], 1


def drop_kind(paths: list[str]) -> str:
    """What a drag of these local paths would do: "ok", "many", "folder", or "ext:<extension>" (unsupported)."""
    if len(paths) != 1:
        return "many"
    path = paths[0]
    if os.path.isdir(path):
        return "folder"
    ext = os.path.splitext(path)[1].lower()
    return "ok" if ext in SUPPORTED else f"ext:{ext}"


def _path_of(doc) -> str:
    source = getattr(doc, "source", None)
    return getattr(source, "path", "") if getattr(source, "kind", "") == "file" else ""


def _title_of(doc) -> str:
    source = getattr(doc, "source", None)
    return getattr(source, "title", "") if getattr(source, "kind", "") == "file" else ""


def _page_of(doc, i: int) -> int:
    page_of = getattr(doc, "page_of", None)
    try:
        return int(page_of(i) or 0) if page_of is not None else 0
    except (IndexError, ValueError, TypeError):
        return 0


def _has_dimmed(doc) -> bool:
    regions = getattr(doc, "regions", None) or ()
    return any(getattr(r, "kind", "") in ("skip", "aside", "refs") for r in regions)


def progress_marks(doc) -> tuple[list[float], list[float] | None]:
    """The progress bar's sentence starts (for click-to-jump) and the marks it draws: page starts for a file
    with pages, else None (the sentence starts). Both measure the spoken span, which notes and references
    shown after the main text are not part of."""
    if doc is None or not doc.sentences:
        return [], None
    starts = [span_fraction(doc, s.start) for s in doc.sentences]
    pages = list(getattr(doc, "page_starts", None) or ())
    if len(pages) < 2:
        return starts, None
    lo, hi = doc.sentences[0].start, doc.sentences[-1].end
    return starts, sorted(span_fraction(doc, p) for p in pages if lo <= p < hi)


class Panel(QWidget):
    def __init__(self, ctl):
        super().__init__(None, PANEL_FLAGS)
        self.ctl = ctl
        t = self.theme = ctl.theme
        self.settings = ctl.settings
        tips, clock = ctl.tips, ctl.clock
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setWindowTitle("Saylight")
        self.hwnd = int(self.winId())
        win32.dwm_set_corner(self.hwnd)
        win32.dwm_disable_transitions(self.hwnd)

        # Header
        self.indicator = Indicator(t, clock, self)
        self.status = FadeLabel(t, "status", "TextPrimary", self)
        self.info = FadeLabel(t, "small", "TextSecondary", self, align=Qt.AlignmentFlag.AlignRight)
        self.btn_expand = IconButton(t, tips, G_EXPAND, size=32, small_glyph=True, parent=self,
                                     tooltip=self._expand_tip, name="Larger reading view")
        self.btn_more = MoreButton(t, tips, self)
        self.flyout: Flyout | None = None  # made on first use; a window of its own
        self.btn_hide = IconButton(t, tips, G_HIDE, size=32, small_glyph=True, parent=self,
                                   tooltip="Hide panel. Press Ctrl+` with nothing highlighted to bring it back.",
                                   name="Hide panel")
        self.btn_back = IconButton(t, tips, G_BACK, size=32, small_glyph=True, parent=self,
                                   tooltip="Back to reading", name="Back to reading")

        # Reading page
        self.view = SentenceView(t, self.settings, self)
        self.view.setMouseTracking(True)
        self.view.installEventFilter(self)
        self.progress = ProgressBar(t, tips, self)
        self.progress.label_fn = self._sentence_label
        self.btn_prev = IconButton(t, tips, G_PREV, parent=self, tooltip="Previous sentence", name="Previous sentence")
        self.play = PlayButton(t, tips, clock, self)
        self.btn_next = IconButton(t, tips, G_NEXT, parent=self, tooltip="Next sentence", name="Next sentence")
        self.btn_stop = IconButton(t, tips, "", parent=self, tooltip="Stop", name="Stop")
        self.btn_stop.painter_glyph = paint_stop_square(t)
        self.btn_type = IconButton(t, tips, G_TYPE, size=32, parent=self, tooltip=self._type_tip,
                                   name="Type or paste text")
        self.btn_clear = IconButton(t, tips, G_CLEAR, size=32, parent=self, tooltip=self._clear_tip,
                                    name="Clear text")
        self.stepper = SpeedStepper(t, tips, self)
        self.btn_volume = IconButton(t, tips, volume_glyph(100), parent=self, tooltip=self._volume_tip,
                                     name="Volume")
        self.slider = VolumeSlider(t, tips, self)
        self.slider.hide()

        # Settings page: the tab bar in the header, one tab of rows below it.
        self.tabs = SettingsTabs(self, ctl, [DisplayPage, VoicePage, GeneralPage])
        self._settings_widgets = [self.btn_back, self.tabs, self.tabs.bar]
        # The update line between the header and the card, while nothing is being read.
        self.strip = NoticeStrip(t, tips, self)
        self.update_bar = UpdateBar(getattr(ctl, "updates", None), self.strip, self)
        self.update_bar.changed.connect(self.relayout)
        self._reading_widgets = [self.indicator, self.status, self.btn_expand, self.btn_more, self.view,
                                 self.progress, self.btn_prev, self.play, self.btn_next, self.btn_stop, self.btn_type,
                                 self.btn_clear, self.stepper, self.btn_volume]
        self.drop_outline = _DropOutline(t, self)
        self.setAcceptDrops(True)

        self.page = "reading"
        self.expanded = bool(self.settings.get("panel.expanded"))
        self.view.set_expanded(self.expanded)
        self._sync_expand_button()
        self.state = "ready"
        self.info_dict: dict = {}
        self.doc = None
        self.cleared = False   # the undo window after a Clear: the card offers Undo
        self.cleared_title = ""  # ...for a file: "Closed “title”. [Undo]"
        self._undo_card = False  # the card shown is "Text cleared. [Undo]", where only the chip takes clicks
        self.notice_ext = ""   # the extension of a file an open notice is about (".doc" has its own words)
        self._drop: tuple[list[list[Part]], int] | None = None  # the card's lines while a file is dragged over
        self._drop_path = ""
        self._drop_from_settings = False
        self._note: dict | None = None  # a header note: text, colour, when asked and when first shown
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self._end_note)
        self._page_info = False  # an opening PDF's page count may show (after PAGE_INFO_MS)
        self._page_timer = QTimer(self)
        self._page_timer.setSingleShot(True)
        self._page_timer.timeout.connect(self._show_page_info)
        self._view_tip = ""
        # The top-right chip: "start_over", "back", "nudge" or "remembered" (see _sync_corner).
        self._corner_kind: str | None = None
        self._resume_place: tuple[int, int] = (0, 0)   # (sentence, page) a reading continued from
        self._start_over_left = START_OVER_MS
        self._start_over_since: float | None = None
        self._nudged: set[str] = set()                  # files offered "Remember this page" this session
        self._corner_timer = QTimer(self)
        self._corner_timer.setSingleShot(True)
        self._corner_timer.timeout.connect(self._corner_expired)
        self.covered = False   # the Type or paste box is over the panel, which stays shown at opacity 0
        self._card_pointer: str | None = None  # over the clickable empty card: "card", or the chip under it
        self._card_press: str | None = None
        self.volume_open = False
        self._time_left = -1
        self._speed_note = ""
        self._drag: dict | None = None
        self._rest = QPoint()

        # Motion
        self.fade = Tween(self, t, 0.0, MEDIUM)
        self.fade.on_change = self._apply_window_motion
        self.slide = Tween(self, t, 0.0, SLOW)
        self.slide.on_change = self._apply_window_motion
        self.snap_anim = Tween(self, t, 1.0, MEDIUM, decelerate())
        self.snap_anim.on_change = self._apply_snap_step
        self._snap_from = self._snap_to = QPoint()
        self._hiding = False
        self._slide_dir = 1
        self._swap_effects = {w: QGraphicsOpacityEffect(w) for w in (self.stepper, self.slider)}
        for w, eff in self._swap_effects.items():
            eff.setEnabled(False)
            w.setGraphicsEffect(eff)
        self.swap = Tween(self, t, 0.0, MEDIUM, decelerate())
        self.swap.on_change = self._apply_swap
        self._slider_timer = QTimer(self)
        self._slider_timer.setSingleShot(True)
        self._slider_timer.timeout.connect(lambda: self.set_volume_open(False))

        self._wire()
        t.changed.connect(self._on_theme)
        self.settings.changed.connect(self._on_setting)
        self.tabs.sync()
        self._on_theme()

    # --- Wiring -------------------------------------------------------------------------------

    def _wire(self) -> None:
        e = self.ctl.engine
        self.btn_hide.clicked.connect(lambda: self.ctl.set_panel_visible(False))
        self.btn_expand.clicked.connect(self.toggle_expanded)
        self.btn_more.clicked.connect(self.toggle_flyout)
        self.btn_back.clicked.connect(lambda: self.set_page("reading"))
        self.play.clicked.connect(self._on_play)
        self.btn_prev.clicked.connect(lambda: (self._drop_resume_chips(), e.prev()))
        self.btn_next.clicked.connect(lambda: (self._drop_resume_chips(), e.next()))
        self.btn_stop.clicked.connect(e.stop)
        self.btn_type.clicked.connect(lambda: self.ctl.open_compose("button"))
        self.btn_clear.clicked.connect(self.ctl.clear_box)
        self.progress.jumpRequested.connect(lambda i: (self._drop_resume_chips(), e.jump_to(i)))
        self.stepper.stepped.connect(self._step_speed)
        self.stepper.resetRequested.connect(lambda: self.ctl.set_speed(1.0))
        self.btn_volume.clicked.connect(lambda: self.set_volume_open(not self.volume_open))
        self.slider.valueChanged.connect(e.set_volume)
        self.slider.interacted.connect(lambda: self._slider_timer.start(4000))
        self.view.sizeStepRequested.connect(self.step_text_size)

    def step_text_size(self, direction: int) -> None:
        keys = [k for k, *_ in TEXT_SIZES]
        current = self.settings.get("text_size")
        i = keys.index(current) if current in keys else 1
        j = max(0, min(len(keys) - 1, i + direction))
        if j != i:
            self.settings.set("text_size", keys[j])

    def _on_play(self) -> None:
        action = self.info_dict.get("play_action", "read")
        e = self.ctl.engine
        if action == "read":
            e.read_selection_click(self.ctl.foreground.last_app())
        elif action in ("pause", "resume"):
            e.toggle_pause()
        elif action == "retry":
            e.retry()

    def _step_speed(self, direction: int) -> None:
        current = self.ctl.speed()
        i = min(range(len(SPEEDS)), key=lambda j: abs(SPEEDS[j] - current))
        j = max(0, min(len(SPEEDS) - 1, i + direction))
        if j != i:
            self.ctl.set_speed(SPEEDS[j])

    def wheelEvent(self, event) -> None:
        # Scrolling over the volume button changes the volume without opening the slider.
        if self.btn_volume.geometry().contains(event.position().toPoint()):
            self.slider.wheelEvent(event)
            return
        event.ignore()

    # --- Theme, settings and layout -----------------------------------------------------------

    def _on_theme(self) -> None:
        win32.dwm_set_dark(self.hwnd, self.theme.dark)
        for w in self.findChildren(QWidget):
            relayout = getattr(w, "relayout", None)
            if relayout:
                relayout()
        self.tabs.sync()
        self.relayout()
        self.update()

    def _on_setting(self, key: str) -> None:
        if key in ("text_size", "reading_font", "spacing", "panel.expanded"):
            self.relayout()
        if key == "volume":
            self.on_volume(int(self.settings.get("volume")))

    def reading_width(self, expanded: bool) -> int:
        """The reading page's width in the compact or the larger view, widened for larger text."""
        t = self.theme
        per_char = QFontMetricsF(t.font("reading")).horizontalAdvance(WIDTH_SAMPLE) / len(WIDTH_SAMPLE)
        base = t.px(EXPANDED_W if expanded else COMPACT_W)
        chars, cap = (EXPANDED_CPL, MAX_EXPANDED_W) if expanded else (COMPACT_CPL, MAX_COMPACT_W)
        w = max(base, min(t.px(cap), round(per_char * chars) + t.px(52)))  # 52 = 2x12 card inset + 2x14 text pad
        return self._fit_screen(w)

    def panel_width(self) -> int:
        if self.page == "reading":
            return self.reading_width(self.expanded)
        # The settings page keeps the base width so rows never jump under the pointer.
        return self._fit_screen(self.theme.px(EXPANDED_W if self.expanded else COMPACT_W))

    def _fit_screen(self, w: int) -> int:
        screen = self.screen() or QGuiApplication.primaryScreen()
        return min(w, int(screen.availableGeometry().width() * 0.9))

    def _max_height(self) -> int:
        screen = self.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry().height() - 2 * self.theme.px(EDGE)

    def strip_shown(self) -> bool:
        """The update line shows on the reading page while nothing is being read."""
        return self.page == "reading" and self.update_bar.visible() and self.state not in ACTIVE

    def relayout(self) -> None:
        """Place every child on the grid (plate inset 12, optical inset 22) and resize the window."""
        t = self.theme
        W = self.panel_width()
        px = t.px
        settings_page = self.page == "settings"
        for w in self._settings_widgets:
            w.setVisible(settings_page)
        for w in self._reading_widgets:
            w.setVisible(not settings_page)
        self.slider.setVisible(not settings_page and self.volume_open)
        self.stepper.setVisible(not settings_page and not self.volume_open)

        # Header row: y 8, height 32.
        self.btn_hide.move(W - px(EDGE) - px(32), px(8))
        self.btn_more.move(self.btn_hide.x() - px(4) - px(32), px(8))
        self.btn_expand.move(self.btn_more.x() - px(4) - px(32), px(8))
        if settings_page:
            self.btn_back.move(px(14), px(8))  # glyph centre on the indicator's column (30 DIP)
            label_x = px(EDGE) + px(32) + px(6)
        else:
            self.indicator.move(px(22), px(16))
            label_x = px(46)
        status_w = int(self.status.content_width() + 2)
        self.status.setGeometry(label_x, px(14), status_w, px(20))
        right_edge = (self.btn_hide.x() if settings_page else self.btn_expand.x()) - px(8)
        info_x = label_x + status_w + px(12)
        info_w = right_edge - info_x
        self.info.setGeometry(info_x, px(14), max(0, info_w), px(20))  # 20 tall: room for the 16 DIP keycaps
        self.info.setVisible(not settings_page and info_w >= px(48))
        strip = self.strip_shown()
        self.strip.setVisible(strip)

        if settings_page:
            bar_x = px(EDGE) + px(32) + px(6)
            self.tabs.bar.setGeometry(bar_x, px(8), self.btn_hide.x() - px(8) - bar_x, px(32))
            H = self.tabs.arrange(W, px(44), self._max_height())
        else:
            top = px(44)
            if strip:
                self.strip.setGeometry(px(EDGE), top, W - 2 * px(EDGE), px(STRIP_H))
                top += px(STRIP_H) + px(8)
            card_h = self.view.preferred_height()
            self.view.setGeometry(px(EDGE), top, W - 2 * px(EDGE), card_h)
            self.drop_outline.setGeometry(self.view.geometry())
            self.drop_outline.raise_()
            y = top + card_h + px(8)
            self.progress.setGeometry(px(22), y, W - 2 * px(22), px(16))
            yt = y + px(16) + px(4)
            self.btn_prev.move(px(EDGE), yt + px(2))
            self.play.move(px(48), yt - px(4))
            self.btn_next.move(px(96), yt + px(2))
            self.btn_stop.move(px(140), yt + px(2))
            self.btn_type.move(px(192), yt + px(4))   # 32 DIP plates on the transport centre line (yt + 20)
            self.btn_clear.move(px(228), yt + px(4))
            self.btn_volume.move(W - px(EDGE) - px(36), yt + px(2))
            self.stepper.move(self.btn_volume.x() - px(4) - self.stepper.width(), yt + px(4))
            self.slider.move(self.stepper.pos())
            H = yt + px(40) + px(EDGE)
        self._resize_anchored(W, H)
        self.update()

    def _resize_anchored(self, W: int, H: int) -> None:
        if self.size() == QSize(W, H):
            return
        old = self.geometry()
        self.setFixedSize(W, H)
        if not self.isVisible() or not old.isValid() or old.width() <= 1:
            return
        screen = QGuiApplication.screenAt(old.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        snap = (self.settings.get("panel.position") or {})
        sx = snap.get("snap_x")
        if sx == "left":
            x = old.left()
        elif sx == "right":
            x = old.right() + 1 - W
        else:
            x = old.center().x() - W // 2
        y = old.bottom() + 1 - H if old.center().y() > area.center().y() else old.top()
        self._rest = self._clamp(QPoint(x, y), area)
        self.move(self._rest)

    def toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self.settings.set("panel.expanded", self.expanded)
        self.view.set_expanded(self.expanded)
        self._sync_expand_button()
        self.ctl.tips.refresh(self.btn_expand)
        self.relayout()
        self._present_card()

    def _sync_expand_button(self) -> None:
        self.btn_expand.set_glyph(G_COLLAPSE if self.expanded else G_EXPAND)
        self.btn_expand.setAccessibleName("Smaller reading view" if self.expanded else "Larger reading view")

    def _expand_tip(self) -> str:
        return "Smaller reading view" if self.expanded else "Larger reading view with more of the text"

    def set_page(self, page: str) -> None:
        if page == self.page:
            return
        self.page = page
        self.close_flyout()
        self.set_volume_open(False, animate=False)
        self.ctl.tips.press(self)
        if page == "reading":
            self.present(self.state, self.info_dict)
        self.relayout()
        self.tabs.set_active(page == "settings" and self.isVisible())

    # --- Volume slider swap -------------------------------------------------------------------

    def set_volume_open(self, on: bool, *, animate: bool = True) -> None:
        if on == self.volume_open:
            return
        self.volume_open = on
        self.btn_volume.set_selected(on)
        if on:
            self._slider_timer.start(4000)
        else:
            self._slider_timer.stop()
        if self.page != "reading":
            return
        self.slider.show()
        self.stepper.show()
        for eff in self._swap_effects.values():
            eff.setEnabled(True)
        if animate:
            self.swap.to(1.0 if on else 0.0)
        else:
            self.swap.snap(1.0 if on else 0.0)

    def _apply_swap(self, v: float) -> None:
        """Crossfade stepper → slider: the outgoing control fades in the first half, the incoming in the second."""
        self._swap_effects[self.stepper].setOpacity(max(0.0, 1.0 - 2 * v))
        self._swap_effects[self.slider].setOpacity(max(0.0, 2 * v - 1))
        if v >= 1.0 or v <= 0.0:
            self.stepper.setVisible(v <= 0.0 and self.page == "reading")
            self.slider.setVisible(v >= 1.0 and self.page == "reading")
            for eff in self._swap_effects.values():
                eff.setEnabled(False)

    def leaveEvent(self, event) -> None:
        if self.volume_open:
            self._slider_timer.start(1500)
        super().leaveEvent(event)

    def enterEvent(self, event) -> None:
        if self.volume_open:
            self._slider_timer.start(4000)
        super().enterEvent(event)

    # --- Engine → view ------------------------------------------------------------------------

    def _sentence_label(self, i: int) -> str:
        if not self.doc or not (0 <= i < len(self.doc.sentences)):
            return ""
        words = self.doc.sentences[i].text.split()
        preview = " ".join(words[:5]) + ("…" if len(words) > 5 else "")
        page = _page_of(self.doc, i)
        where = f"Page {page} · " if page else ""
        return f"{where}Sentence {i + 1} of {len(self.doc.sentences)} · “{preview}”"

    def on_session(self, doc) -> None:
        same = doc is not None and self.doc is not None and _path_of(doc) and _path_of(doc) == _path_of(self.doc)
        self.doc = doc
        self.view.set_document(doc)
        self.progress.starts, self.progress.marks = progress_marks(doc)
        if not same:
            self.progress.set_progress(0.0, 0.0)
            self._set_corner(None)
        self._present_card()
        if doc is not None and doc.sentences and _has_dimmed(doc) and not self.settings.get("tips.dimmed_note_shown"):
            self.settings.set("tips.dimmed_note_shown", True)
            self.show_note("Dimmed text isn't read aloud")

    def on_position(self, sentence: int, lo: int, hi: int, played: float, buffered: float, in_word: bool) -> None:
        self.view.set_position(sentence, lo, hi)
        self.progress.set_progress(played, buffered)
        self.indicator.set_in_word(in_word)

    def on_time_left(self, seconds: int) -> None:
        self._time_left = seconds
        self._present_info()

    def on_speed(self, v: float, note: str) -> None:
        self._speed_note = note
        i = min(range(len(SPEEDS)), key=lambda j: abs(SPEEDS[j] - v))
        self.stepper.slower_tip = f"Slower ({speed_label(SPEEDS[i - 1])})" if i > 0 else "Slowest speed"
        self.stepper.faster_tip = (f"Faster ({speed_label(SPEEDS[i + 1])})" if i < len(SPEEDS) - 1
                                   else "Fastest speed")
        self.stepper.value_tip = (f"Reading speed {speed_label(v)}, about {round(WPM_AT_1X * v / 5) * 5} words "
                                  "per minute. Scroll to change, double-click for 1.0×. Applies from the next "
                                  "sentence.")
        self.stepper.set_value(speed_label(v), pending=bool(note), can_down=i > 0, can_up=i < len(SPEEDS) - 1)
        self._present_info()

    def on_volume(self, v: int) -> None:
        self.slider.set_value(v)
        self.btn_volume.set_glyph(volume_glyph(v))
        self.ctl.tips.refresh(self.btn_volume)

    def _volume_tip(self) -> str:
        return f"Volume {self.slider.value}% · click to adjust, scroll to change"

    def present(self, state: str, info: dict) -> None:
        """Map an engine state onto every part of the panel."""
        was = self.state
        self.state, self.info_dict = state, info
        self.tabs.on_state(state, info)
        self._follow_corner(was, state, info)
        if state == "opening" and was != "opening":
            self._page_info = False
            self._page_timer.start(PAGE_INFO_MS)
        elif state != "opening":
            self._page_timer.stop()
        if self.page == "settings":
            return
        reduced = self.theme.reduced_motion
        look = NOTICE_LOOK.get(info.get("kind", ""), ("Critical", G_ERROR))
        ind = {
            "ready": ("idle", "TextSecondary", ""),
            "ready_offline": ("glyph", "TextSecondary", G_OFFLINE),
            "copying": ("wave", "Accent", ""),
            "opening": ("wave", "Accent", ""),
            "open_failed": ("glyph", look[0], look[1]),
            "preparing": ("wave", "Accent", ""),
            "waiting": ("wave", "Accent", ""),
            "reading": ("live", "Accent", ""),
            "paused": ("paused", "Accent", ""),
            "finished": ("idle", "TextSecondary", ""),
            "stopped": ("idle", "TextSecondary", ""),
            "nothing_selected": ("glyph", "Caution", G_NOTHING),
            "error_offline": ("glyph", "Critical", G_OFFLINE),
            "error_service": ("glyph", "Critical", G_ERROR),
            "error_playback": ("glyph", "Critical", G_ERROR),
            "shortcut_unavailable": ("glyph", "Critical", G_ERROR),
        }.get(state, ("idle", "TextSecondary", ""))
        self.indicator.set_state(*ind)
        self.status.set_text(info.get("label") or state.replace("_", " ").capitalize(), "TextPrimary")

        action = info.get("play_action", "read")
        glyph, name = {"pause": (G_PAUSE, "Pause"), "resume": (G_PLAY, "Resume"), "retry": (G_RETRY, "Try again"),
                       "none": (G_PLAY, "Read selection")}.get(action, (G_PLAY, "Read selection"))
        self.play.set_glyph(glyph, name)
        self.play.setEnabled(action != "none")
        retry_tip = ("Try again. If it keeps failing, see saylight.log in the Saylight folder."
                     if state == "error_service" else "Try again")
        if state == "opening":
            self.play.set_tooltip("Opening a file…" if info.get("open_kind") == "file" else "Preparing audio…")
        else:
            self.play.set_tooltip({"pause": "Pause (Ctrl+` with nothing highlighted)",
                                   "resume": "Resume (Ctrl+` with nothing highlighted)",
                                   "retry": retry_tip}.get(action, "Read the highlighted text (Ctrl+`)"))
        self.btn_stop.set_tooltip("Stop opening" if state == "opening" else "Stop")
        self.play.set_buffering(state in ("preparing", "waiting") and info.get("show_ring", True) and not reduced)

        prev_action = info.get("prev_action", "none")
        self.btn_prev.setEnabled(prev_action != "none")
        self.btn_prev.set_tooltip({"read_from": "Read from this sentence",
                                   "read_last_again": "Read the last sentence again"}.get(prev_action,
                                                                                         "Previous sentence"))
        self.btn_next.setEnabled(bool(info.get("can_next")))
        self.btn_stop.setEnabled(bool(info.get("can_stop")))
        self.progress.set_interactive(bool(info.get("can_seek")))
        self.progress.dim.to(1.0 if state == "stopped" else 0.0)
        self.refresh_box_actions()
        self._present_card()
        self._present_info()
        self.relayout()

    def _present_card(self) -> None:
        state, info = self.state, self.info_dict
        v = self.view
        # Every message except the ready hint is drawn in one colour (secondary_from=len), so no sentence
        # changes colour halfway through.
        self._undo_card = self.cleared and self.doc is None and state in IDLE
        if self._drop is not None:
            v.show_messages(*self._drop)
        elif self._undo_card:
            v.show_messages(self._cleared_lines())
        elif state == "opening" and (info.get("open_kind") == "file" or self.doc is None):
            v.show_messages(self._opening_lines(info), secondary_from=1)
        elif state == "open_failed":
            v.show_messages(*self._notice_lines(info))
        elif state in ("ready",) or (state == "copying" and self.doc is None):
            v.show_messages(*self._ready_lines())
        elif state == "ready_offline":
            lines = [K(("text", "No internet connection.")), K(("text", "Reading aloud needs internet."))]
            v.show_messages(lines, secondary_from=len(lines))
        elif state == "nothing_selected":
            lines = NOTHING_TEXT.get(info.get("reason", "empty"), NOTHING_TEXT["empty"])
            if self._card_can_type():
                lines = lines + [TYPE_LINE]
            v.show_messages(lines, secondary_from=len(lines))
        elif state == "shortcut_unavailable":
            lines = [K(("text", "Another app is using "), ("key", "Ctrl"), ("key", "`"), ("text", ".")),
                     K(("text", "Use "), ("glyph", G_PLAY), ("text", ", or choose Retry shortcut in the tray menu."))]
            if self._card_can_type():
                lines.append(TYPE_LINE)
            v.show_messages(lines, secondary_from=len(lines))
        elif state.startswith("error"):
            lines = self._error_lines(state, info)
            v.show_messages(lines, secondary_from=len(lines))
        elif self.doc is None:
            v.show_messages(*self._ready_lines())
        else:
            v.show_text()
            v.set_style({"preparing": "muted", "waiting": "muted", "copying": "muted", "opening": "muted",
                         "paused": "paused", "finished": "finished", "stopped": "stopped"}.get(state, "active"))
            sentence = info.get("sentence")
            if isinstance(sentence, int) and sentence >= 0 and v.sentence < 0:
                v.set_position(sentence, 0, 0)
        if self._card_pointer is not None and not self._card_clickable():
            self._set_card_pointer(None)
        else:
            self._sync_card_actions()

    # --- Card lines -----------------------------------------------------------------------------

    def _text_room(self) -> float:
        """The card's text width, for fitting a line (before the first layout: the compact panel's)."""
        room = self.view.text_width()
        return room if room > 0 else float(self.theme.px(COMPACT_W - 52))

    def fit_title(self, title: str, line) -> str:
        """`title` elided in the middle so that line(title) fits the card in the message role."""
        return self._elide(title, measure_parts(self.theme, line(""), role="message"))

    def _elide(self, title: str, fixed: float) -> str:
        """`title` in the room left on a card line after `fixed`, elided in the middle when it doesn't fit."""
        fm = QFontMetricsF(self.theme.font("message"))
        room = max(0.0, self._text_room() - fixed)
        if not title or fm.horizontalAdvance(title) <= room:
            return title
        return fm.elidedText(title, Qt.TextElideMode.ElideMiddle, room)

    def _ready_lines(self) -> tuple[list[list[Part]], int]:
        """The Ready card, with "or [Continue “title” from page N]" while a file can be continued."""
        point = getattr(self.ctl, "continue_point", lambda: None)()
        v = self.view
        if point is None:
            return (READY_HINT_EXPANDED if self.expanded else READY_HINT), READY_SECONDARY_FROM
        title, page = point
        tail = f" from page {page}" if page else " where you stopped"
        fixed = measure_parts(self.theme, CONTINUE_LINE, role="message", labels={"continue": f"Continue “”{tail}"})
        v.set_action_label("continue", f"Continue “{self._elide(title, fixed)}”{tail}")
        lines = [READY_LINE, READY_ACTIONS, CONTINUE_LINE]
        if self.expanded:
            lines += [READY_TIP] + READY_MORE
        return lines, 3

    def continue_tip(self) -> str:
        point = getattr(self.ctl, "continue_point", lambda: None)()
        if point is None:
            return ""
        title, page = point
        return f"Open “{title}” and continue from page {page}" if page else f"Open “{title}” and continue"

    def _cleared_lines(self) -> list[list[Part]]:
        if not self.cleared_title:
            return CLEARED_HINT
        title = self.fit_title(self.cleared_title, lambda t: [("text", f"Closed “{t}”. "), ("action", "undo")])
        return [K(("text", f"Closed “{title}”. "), ("action", "undo"))]

    def _opening_lines(self, info: dict) -> list[list[Part]]:
        stop = K(("text", "To cancel, press Stop."))
        if info.get("open_kind") != "file":
            return [stop]
        title = self.fit_title(info.get("title", ""), lambda t: [("text", f"Opening “{t}”")])
        return [K(("text", f"Opening “{title}”" if title else "Opening the file")), stop]

    def _notice_lines(self, info: dict) -> tuple[list[list[Part]], int]:
        kind = info.get("kind", "")
        title = info.get("title", "")
        keys = shortcut_parts(str((self.settings.get("hotkeys") or {}).get("read", "")))
        line = {"password": lambda t: [("text", f"“{t}” needs a password.")],
                "empty": lambda t: [("text", f"“{t}” has no text to read.")],
                "io": lambda t: [("text", f"Saylight couldn't read “{t}”.")],
                "missing": lambda t: [("text", f"Saylight can't find “{t}” any more.")]}.get(
            kind, lambda t: [("text", f"Saylight couldn't open “{t}”.")])
        return notice_lines(kind, self.fit_title(title, line), self.notice_ext, keys)

    def _error_lines(self, state: str, info: dict) -> list[list[Part]]:
        retry = ("glyph", G_RETRY)
        if state == "error_offline":
            if info.get("back_online"):
                return [K(("text", "You're back online.")), K(("text", "Press "), retry, ("text", " to continue."))]
            then = " to continue." if info.get("mid_session") else " to try again."
            return [K(("text", "Check your internet connection,")), K(("text", "then press "), retry, ("text", then))]
        if state == "error_service":
            return [K(("text", "The voice service didn't return audio.")),
                    K(("text", "Press "), retry, ("text", " to try again."))]
        return [K(("text", "Couldn't play audio. Check your sound output,")),
                K(("text", "then press "), retry, ("text", " to try again."))]

    def _info_parts(self) -> tuple[list[Part], str]:
        """What the header info says for the state, and in which colour."""
        state, info = self.state, self.info_dict
        if state in ("reading", "paused", "waiting"):
            page, pages = info.get("page") or 0, info.get("pages") or 0
            if info.get("source") == "file" and page and pages:
                counter = f"Page {page} of {pages}"
            else:
                counter = info.get("counter_text") or (
                    f"{info.get('sentence', 0) + 1} / {info.get('total', 0)}" if info.get("total") else "")
            left = self._format_left(self._time_left, minutes=info.get("source") == "file")
            return [("text", " · ".join(x for x in (counter, left) if x))], "TextSecondary"
        if state == "opening":
            done, total = info.get("open_done") or 0, info.get("open_total") or 0
            if total and done >= total:
                return [("text", "Preparing the text…")], "TextSecondary"
            if total and self._page_info:
                return [("text", f"Page {max(1, done)} of {total}")], "TextSecondary"
            return [], "TextSecondary"
        if state == "preparing":
            total = info.get("total") or 0
            return [("text", f"{total} sentence{'s' if total != 1 else ''}" if total else "")], "TextSecondary"
        if state == "finished" and info.get("end_of_main_text"):
            return [("text", "End of the main text")], "TextSecondary"
        if state == "stopped" and self.doc is not None and info.get("can_seek"):
            return [("text", "Click a sentence to resume")], "TextSecondary"
        if state in ("finished", "stopped"):
            return [("key", "Ctrl"), ("key", "`"), ("text", " to read")], "TextSecondary"
        return [], "TextSecondary"

    def _present_info(self) -> None:
        state = self.state
        if self.page == "settings":
            return
        note = self._note
        if self._speed_note and state in ACTIVE:  # the pending speed always wins
            self.info.set_text(self._speed_note, "Accent")
        elif note is not None and self.info.content_width([("text", note["text"])]) <= self._info_room():
            if note["shown"] is None:
                note["shown"] = time.monotonic()
                self._note_timer.start(NOTE_MS)
            self.info.set_text(note["text"], note["color"])
        else:
            parts, color = self._info_parts()
            self.info.set_parts([p for p in parts if p[1]], color)
        self.relayout_header()

    def _info_room(self) -> float:
        px = self.theme.px
        status_w = int(self.status.content_width() + 2)
        right_edge = (self.btn_hide.x() if self.page == "settings" else self.btn_expand.x()) - px(8)
        return right_edge - (self.status.x() + status_w + px(12))

    def show_note(self, text: str, color: str = "TextSecondary") -> None:
        """A note in the header info for 6 s: "Audio saved", "Continuing from page 14". It waits up to 3 s for
        room (while "Preparing audio…" leaves too little), then it is dropped."""
        self._note = {"text": text, "color": color, "shown": None}
        self._note_timer.start(NOTE_WAIT_MS)
        self._present_info()

    def _end_note(self) -> None:
        self._note = None
        self._present_info()

    def _show_page_info(self) -> None:
        self._page_info = True
        self._present_info()

    def relayout_header(self) -> None:
        t = self.theme
        px = t.px
        status_w = int(self.status.content_width() + 2)
        self.status.resize(status_w, px(20))
        right_edge = (self.btn_hide.x() if self.page == "settings" else self.btn_expand.x()) - px(8)
        info_x = self.status.x() + status_w + px(12)
        info_w = right_edge - info_x
        need = self.info.content_width()
        self.info.setGeometry(info_x, px(14), max(0, info_w), px(20))
        self.info.setVisible(self.page == "reading" and info_w >= px(48) and need > 0)

    @staticmethod
    def _format_left(seconds: int, *, minutes: bool = False) -> str:
        """ "4:05 left", or for a file "52 min left"; "1 h 5 min left" past an hour."""
        if seconds is None or seconds < 0:
            return ""
        if seconds >= 3600:
            return f"{seconds // 3600} h {(seconds % 3600) // 60} min left"
        if minutes and seconds >= 60:
            return f"{min(59, round(seconds / 60))} min left"
        return f"{seconds // 60}:{seconds % 60:02d} left"

    # --- Type or paste, Clear and the empty card ------------------------------------------------

    def _box_state(self) -> str:
        """The state Type and Clear follow: under the Nothing-selected overlay, the one beneath it."""
        if self.state == "nothing_selected":
            return self.info_dict.get("underlying") or "ready"
        return self.state

    def _can_clear(self) -> bool:
        """Clear works on the box's text, or on typed text alone; never while copying."""
        info = self.info_dict
        return bool(info.get("can_clear")) or (bool(info.get("can_type")) and self.ctl.has_draft())

    def _type_tip(self) -> str:
        chord = self.ctl.compose_shortcut()
        keys = f" ({chord})" if chord else ""
        if self.ctl.has_draft():
            return f"Type or paste text{keys}. The text you typed is still there."
        return f"Type or paste text to read aloud{keys}"

    def _clear_tip(self) -> str:
        title = _title_of(self.doc) if self.doc is not None else ""
        if title:
            stop = "Stop and close" if self._box_state() in SESSION else "Close"
            return f"{stop} “{title}”." + UNDO_NOTE
        if self._box_state() in SESSION:
            return "Stop and clear text." + UNDO_NOTE
        if self.doc is None and self.ctl.has_draft():
            return "Clear the text you typed." + UNDO_NOTE
        return "Clear text." + UNDO_NOTE

    def refresh_box_actions(self) -> None:
        """Re-evaluate Type and Clear (also when the typed text changes)."""
        self.btn_type.setEnabled(bool(self.info_dict.get("can_type")))
        self.btn_clear.setEnabled(self._can_clear())
        self.btn_type.set_tooltip(self._type_tip)  # also refreshes the accessible description
        self.btn_clear.set_tooltip(self._clear_tip)

    def set_cleared(self, on: bool, title: str = "") -> None:
        """During the undo window after a Clear, the empty card reads "Text cleared. [Undo]", or for a file
        "Closed “title”. [Undo]"."""
        if on != self.cleared or (on and title != self.cleared_title):
            self.cleared = on
            self.cleared_title = title if on else ""
            self._present_card()
            self._set_card_pointer(None)  # what the card does under the pointer changes; the next move tells

    def _card_can_type(self) -> bool:
        """The card is empty and nothing is under way, so a click on it could open Type or paste."""
        info = self.info_dict
        if self.page != "reading" or self.doc is not None or not info.get("can_type"):
            return False
        return self.state in IDLE or (self.state == "nothing_selected" and info.get("underlying") in IDLE)

    def _card_chips(self) -> set[str]:
        return {s for line in (self.view.messages or []) for kind, s in line if kind == "action"}

    def _typing_card(self) -> bool:
        """A click anywhere on the card opens Type or paste (the empty card, never after a Clear)."""
        return self._card_can_type() and not self._undo_card and "type" in self._card_chips()

    def _card_clickable(self) -> bool:
        """The card takes clicks: it could open Type or paste and shows a chip that says what a click does (so
        not the Offline card, where reading would fail anyway), or it shows chips of its own (Undo, Enter
        password, Try again, Open a file), which take clicks on themselves."""
        if self.page != "reading":
            return False
        chips = self._card_chips()
        if self._card_can_type() and chips:
            return True
        return bool(chips - set(TYPING_ACTIONS)) or (bool(chips) and self.state == "open_failed")

    def _card_target(self, pos) -> str | None:
        """What a click at `pos` on the clickable card is for: an action chip, or "card" for the rest of it. After
        a Clear, and on a notice, only the chips count, so a near miss never opens the box."""
        hit = self.view.action_at(pos)
        if hit in TYPING_ACTIONS and not self.info_dict.get("can_type"):
            hit = None
        return hit or "card" if self._typing_card() else hit

    def _set_card_pointer(self, where: str | None) -> None:
        """Where the pointer is on the clickable card: None (not on a target), "card", or the action chip under it."""
        was, self._card_pointer = self._card_pointer, where
        self.view.set_card_hover(where is not None and self._typing_card())
        if where in ACTION_PARTS:
            self.view.setCursor(Qt.CursorShape.PointingHandCursor)
        elif not self._typing_card():
            self.view.setCursor(Qt.CursorShape.ArrowCursor)
        if where != was:
            tip = {"open": lambda: OPEN_TIP, "continue": self.continue_tip}.get(where or "")
            if tip is not None:
                self.ctl.tips.hover(self.view, tip)
            else:
                self.ctl.tips.leave(self.view)
        self._sync_card_actions()

    def _sync_card_actions(self) -> None:
        """The Type chip takes hover and press from the whole card, since a click anywhere opens the box;
        every other chip only from itself. Open a file and Continue follow Type."""
        where, press = self._card_pointer, self._card_press
        can_type = bool(self.info_dict.get("can_type"))
        whole = self._typing_card()
        for key in CARD_ACTIONS:
            if key == "type" and whole:
                hover, pressed = where in ("card", "type"), press in ("card", "type")
            else:
                hover, pressed = where == key, press == key
            self.view.set_action_state(key, hover, pressed, can_type if key in TYPING_ACTIONS else True)

    def _card_action(self, key: str) -> None:
        """A click on the card: right here in the click, since Windows lets a window take the foreground only
        from user input."""
        ctl = self.ctl
        if key == "undo":
            ctl.undo_clear()
        elif key in ("card", "type"):
            ctl.open_compose("card")
        elif key == "open":
            ctl.choose_file("card")
        elif key == "continue":
            ctl.continue_reading("card")
        elif key == "password":
            ctl.open_password("card")
        elif key == "retry":
            ctl.retry_open()

    # --- Show / hide motion -------------------------------------------------------------------

    def show_animated(self) -> None:
        self._hiding = False
        if not self.isVisible():
            self.restore_position()
            self.fade.snap(0.0)
            self.slide.snap(1.0)
            self._apply_window_motion()
            self.show()
            win32.set_topmost(self.hwnd)
        self.fade.to(1.0, MEDIUM, decelerate())
        self.slide.to(0.0, SLOW, decelerate())

    def hide_animated(self) -> None:
        if not self.isVisible():
            return
        self._hiding = True
        self.close_flyout()
        self.set_volume_open(False, animate=False)
        self.ctl.tips.press(self)
        self.fade.to(0.0, 120, exit_curve())
        self.slide.to(0.5, 120, exit_curve())

    def _apply_window_motion(self, _v: float = 0.0) -> None:
        if self._drag is None and not self.snap_anim.running:
            dy = round(self.theme.px(8) * self.slide.value) * self._slide_dir
            self.move(self._rest.x(), self._rest.y() + dy)
        self.setWindowOpacity(self._window_opacity())
        if self._hiding and self.fade.value <= 0.0:
            self._hiding = False
            self.hide()
            self.move(self._rest)
            self.setWindowOpacity(0.0 if self.covered else 1.0)

    def _window_opacity(self) -> float:
        o = self.fade.value
        return 0.0 if self.covered else 1.0 if o >= 0.999 else max(0.0, o)

    def set_covered(self, on: bool) -> None:
        """While the Type or paste box covers the panel, the panel stays shown (so its visibility setting
        never changes) but is drawn at opacity 0."""
        if on == self.covered:
            return
        self.covered = on
        if on:
            self.close_flyout(now=True)
        if on and not self._hiding:
            self.fade.snap(1.0)
            self.slide.snap(0.0)
        self.setWindowOpacity(self._window_opacity())

    # --- Positioning --------------------------------------------------------------------------

    def _bottom_margin(self, screen) -> int:
        auto_hidden = screen.availableGeometry() == screen.geometry()
        return self.theme.px(TASKBAR_GAP if auto_hidden else EDGE)

    def rest_rect(self) -> QRect:
        """Where the panel rests, whether or not it is shown."""
        if self._rest.isNull():
            self.restore_position()
        return QRect(self._rest, self.size())

    def _clamp(self, pos: QPoint, area: QRect) -> QPoint:
        x = max(area.left(), min(pos.x(), area.right() + 1 - self.width()))
        y = max(area.top(), min(pos.y(), area.bottom() + 1 - self.height()))
        return QPoint(x, y)

    def default_position(self) -> QPoint:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = area.center().x() - self.width() // 2
        y = area.bottom() + 1 - self._bottom_margin(screen) - self.height()
        return self._clamp(QPoint(x, y), area)

    def restore_position(self) -> None:
        pos = self.settings.get("panel.position")
        screens = QGuiApplication.screens()
        screen = None
        if pos:
            screen = next((s for s in screens if pos.get("serial") and s.serialNumber() == pos.get("serial")), None)
            screen = screen or next((s for s in screens if s.name() == pos.get("screen")), None)
        if not pos or screen is None:
            self._rest = self.default_position()
        else:
            area = screen.availableGeometry()
            cx = area.left() + pos.get("fx", 0.5) * area.width()
            cy = area.top() + pos.get("fy", 0.9) * area.height()
            x, y = round(cx - self.width() / 2), round(cy - self.height() / 2)
            m = self.theme.px(EDGE)
            if pos.get("snap_x") == "left":
                x = area.left() + m
            elif pos.get("snap_x") == "right":
                x = area.right() + 1 - m - self.width()
            elif pos.get("snap_x") == "center":
                x = area.center().x() - self.width() // 2
            if pos.get("snap_y") == "top":
                y = area.top() + m
            elif pos.get("snap_y") == "bottom":
                y = area.bottom() + 1 - self._bottom_margin(screen) - self.height()
            self._rest = self._clamp(QPoint(x, y), area)
        self._update_slide_dir()
        self.move(self._rest)

    def _update_slide_dir(self) -> None:
        """Slide in from (and out toward) the nearer vertical edge."""
        center = QRect(self._rest, self.size()).center()
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        self._slide_dir = 1 if center.y() > screen.availableGeometry().center().y() else -1

    def reclamp(self) -> None:
        if self.isVisible():
            self.restore_position()

    def reset_position(self) -> None:
        self.settings.set("panel.position", None)
        target = self.default_position()
        if self.isVisible():
            self._animate_to(target)
        self._rest = target

    def _snap(self, pos: QPoint) -> tuple[QPoint, str | None, str | None, object]:
        t = self.theme
        geo = QRect(pos, self.size())
        screen = QGuiApplication.screenAt(geo.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        m, threshold, mb = t.px(EDGE), t.px(SNAP), self._bottom_margin(screen)
        x, y = pos.x(), pos.y()
        snap_x = snap_y = None
        if abs(geo.left() - (area.left() + m)) <= threshold:
            x, snap_x = area.left() + m, "left"
        elif abs((area.right() + 1 - m) - (geo.right() + 1)) <= threshold:
            x, snap_x = area.right() + 1 - m - self.width(), "right"
        elif abs(geo.center().x() - area.center().x()) <= threshold:
            x, snap_x = area.center().x() - self.width() // 2, "center"
        if abs(geo.top() - (area.top() + m)) <= threshold:
            y, snap_y = area.top() + m, "top"
        elif abs((area.bottom() + 1 - mb) - (geo.bottom() + 1)) <= threshold:
            y, snap_y = area.bottom() + 1 - mb - self.height(), "bottom"
        return self._clamp(QPoint(x, y), area), snap_x, snap_y, screen

    def _save_position(self, pos: QPoint, snap_x, snap_y, screen) -> None:
        area = screen.availableGeometry()
        center = QRect(pos, self.size()).center()
        self.settings.set("panel.position", {
            "screen": screen.name(), "serial": screen.serialNumber(),
            "fx": round((center.x() - area.left()) / max(1, area.width()), 4),
            "fy": round((center.y() - area.top()) / max(1, area.height()), 4),
            "snap_x": snap_x, "snap_y": snap_y})

    def _animate_to(self, target: QPoint) -> None:
        self._snap_from, self._snap_to = self.pos(), target
        self.snap_anim.snap(0.0)
        self.snap_anim.to(1.0)
        if not self.snap_anim.running:
            self.move(target)

    def _apply_snap_step(self, v: float) -> None:
        a, b = self._snap_from, self._snap_to
        self.move(round(a.x() + (b.x() - a.x()) * v), round(a.y() + (b.y() - a.y()) * v))
        if v >= 1.0:
            self._rest = b

    # --- Dragging and sentence clicks -----------------------------------------------------------

    def _press(self, global_pos: QPoint, source, local) -> None:
        self._drag = {"start": global_pos, "origin": self.pos(), "moving": False, "source": source,
                      "local": local}

    def _move(self, global_pos: QPoint) -> None:
        d = self._drag
        if d is None:
            return
        delta = global_pos - d["start"]
        if not d["moving"] and delta.manhattanLength() >= self.theme.px(4):
            d["moving"] = True
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self.view.set_hover(None)
            self._card_press = None
            self._set_card_pointer(None)
            self.ctl.tips.press(self)
        if d["moving"]:
            self.move(d["origin"] + delta)

    def _release(self, global_pos: QPoint) -> None:
        d, self._drag = self._drag, None
        if d is None:
            return
        self.unsetCursor()
        if d["moving"]:
            target, sx, sy, screen = self._snap(self.pos())
            self._animate_to(target)
            self._rest = target
            self._save_position(target, sx, sy, screen)
            self._update_slide_dir()
        elif d["source"] is self.view:
            pressed, self._card_press = self._card_press, None
            self._sync_card_actions()
            if pressed is not None and self._card_clickable():
                self._card_action(pressed)
                return
            if self.view.chip_visible() and self.view.chip_rect().contains(d["local"]):
                self.view.follow_now()
                return
            chip = self.view.action_at(d["local"]) if self.view.messages is None else None
            if chip is not None:
                self.view.set_action_state(chip, True, False, True)
                if chip.startswith("section:"):
                    self.ctl.toggle_section(chip[8:])
                elif chip.startswith("corner:"):
                    self._corner_clicked(chip[7:])
                return
            # Stopped or finished, the banded sentence is a valid target too: it reads from there.
            resting = self.state in ("stopped", "finished")
            i = self.view.sentence_at(d["local"])
            if i is not None and self.info_dict.get("can_seek") and (resting or i != self.view.sentence):
                self.view.set_hover(None)
                self._drop_resume_chips()
                self.ctl.engine.jump_to(i)

    def mousePressEvent(self, event) -> None:
        if self._flyout_open():  # a press on the panel only closes the flyout
            self.close_flyout()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._press(event.globalPosition().toPoint(), self, event.position())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        self._move(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._release(event.globalPosition().toPoint())
        elif event.button() == Qt.MouseButton.RightButton and self.page == "reading" and not self.covered:
            self.open_flyout("pointer", event.globalPosition().toPoint())

    def eventFilter(self, obj, event) -> bool:
        if obj is self.view:
            et = event.type()
            if et == QEvent.Type.MouseButtonPress and self._flyout_open():
                self.close_flyout()  # consumed: a right-click elsewhere opens it again on release
                if event.button() == Qt.MouseButton.LeftButton:
                    return True
            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
                return True
            if et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.RightButton:
                self._context_menu(event.position(), event.globalPosition().toPoint())
                return True
            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._press(event.globalPosition().toPoint(), self.view, event.position())
                if self._card_clickable():
                    self._card_press = self._card_target(event.position())
                    self._sync_card_actions()
                elif self.view.messages is None:
                    chip = self.view.action_at(event.position())
                    if chip is not None:
                        self.view.set_action_state(chip, True, True, True)
                return True
            if et == QEvent.Type.MouseMove:
                if self._drag is not None:
                    self._move(event.globalPosition().toPoint())
                elif self._card_clickable():
                    self._set_card_pointer(self._card_target(event.position()))
                elif self.view.chip_visible() and self.view.chip_rect().contains(event.position()):
                    self.view.track_pointer(None)
                    self.view.setCursor(Qt.CursorShape.PointingHandCursor)
                    self._set_view_tip("")
                else:  # chips, then a sentence when clicks can seek, else plain (clears a chip's hand cursor)
                    self.view.track_pointer(event.position(), can_seek=bool(self.info_dict.get("can_seek")),
                                            allow_current=self.state in ("stopped", "finished"))
                    self._set_view_tip(self.view.tip_at(event.position()) if self.view.messages is None else "")
                return True
            if et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._release(event.globalPosition().toPoint())
                return True
            if et == QEvent.Type.Leave:
                self.view.track_pointer(None)
                self._set_card_pointer(None)
                self._set_view_tip("")
        return super().eventFilter(obj, event)

    def _set_view_tip(self, tip: str) -> None:
        """The tooltip for dimmed text, a word read differently or a chip under the pointer."""
        if tip == self._view_tip:
            return
        self._view_tip = tip
        if tip:
            self.ctl.tips.hover(self.view, lambda: self._view_tip)
        else:
            self.ctl.tips.leave(self.view)

    def _context_menu(self, local, global_pos: QPoint) -> None:
        """Right-click on the reading page: the flyout at the pointer, starting with the word under it."""
        if self.page != "reading" or self.covered:
            return
        word = None
        span = self.view.word_at(local) if self.view.messages is None and self.doc is not None else None
        if span is not None:
            text = self.doc.text[span[0]:span[1]]
            region = self.view._region_at(span[0])
            known = getattr(self.ctl, "has_pronunciation", lambda _w: False)(text)
            if region is None or known:  # dimmed citations, notes and references only with an entry already
                word = (text, span[0])
        self.open_flyout("pointer", global_pos, word)

    # --- The More flyout ------------------------------------------------------------------------

    def _flyout_open(self) -> bool:
        return self.flyout is not None and self.flyout.is_open

    def toggle_flyout(self) -> None:
        if self._flyout_open():
            self.close_flyout()
        else:
            self.open_flyout("more")

    def flyout_items(self, word: str | None = None) -> list:
        state = getattr(self.ctl, "flyout_state", None)
        if callable(state):
            return menu_items(word, **state())
        return menu_items(word, can_open=bool(self.info_dict.get("can_type", True)), can_save=self.doc is not None)

    def open_flyout(self, mode: str = "more", pos: QPoint | None = None, word: tuple[str, int] | None = None) -> None:
        """Open the flyout below More, or at `pos` (a right-click), with "Change how “word” is said…" on top
        when `word` is (text, shown offset)."""
        if self.page != "reading" or self.covered:
            return
        if self.flyout is None:
            self.flyout = Flyout(self.theme, self.ctl.tips,
                                 inside=lambda p: self.isVisible() and self.frameGeometry().contains(p))
            self.flyout.chosen.connect(self._on_flyout)
            self.flyout.trailingClicked.connect(self._on_flyout_button)
            self.flyout.closed.connect(self._on_flyout_closed)
            self._presses = _FlyoutPresses(self)
        QCoreApplication.instance().installEventFilter(self._presses)
        self._flyout_word = word
        self.ctl.tips.press(self)
        if mode == "more":
            face = self.btn_more.face().toRect()
            anchor = QRect(self.btn_more.mapToGlobal(face.topLeft()), face.size())
        else:
            anchor = QRect(pos or QPoint(), QSize(1, 1))
        self.flyout.open_at(self.flyout_items(word[0] if word else None), anchor, mode)
        self.btn_more.set_selected(True)
        self.btn_more.set_badge(False)  # the flyout shows what failed

    def close_flyout(self, now: bool = False) -> None:
        if self.flyout is not None and self.flyout.isVisible():
            self.flyout.close_now() if now else self.flyout.close_animated()
        self.btn_more.set_selected(False)
        if self.flyout is not None:
            QCoreApplication.instance().removeEventFilter(self._presses)

    def _on_flyout_closed(self) -> None:
        self.btn_more.set_selected(False)
        QCoreApplication.instance().removeEventFilter(self._presses)

    def refresh_flyout(self) -> None:
        """New rows in an open flyout: a save's progress, or how it ended."""
        if self._flyout_open():
            word = getattr(self, "_flyout_word", None)
            self.flyout.set_items(self.flyout_items(word[0] if word else None))

    def _on_flyout(self, key: str) -> None:
        ctl = self.ctl
        if key == "open":
            ctl.choose_file("flyout")
        elif key == "save":
            ctl.open_save("flyout")
        elif key == "saved":
            ctl.show_saved_audio()
        elif key == "save_failed":
            ctl.retry_save()
        elif key == "pron":
            ctl.open_pronunciations("flyout")
        elif key == "settings":
            self.set_page("settings")
        elif key == "word":
            word = getattr(self, "_flyout_word", None)
            if word is not None:
                ctl.change_word(word[0], word[1])

    def _on_flyout_button(self, key: str) -> None:
        if key == "saving":
            self.ctl.cancel_save()
        elif key == "save_failed":
            self.ctl.retry_save()

    def set_save_progress(self, fraction: float | None, eta: str = "", *, done: bool = False) -> None:
        """The ring on More while a save runs; None when it ends (`done` when the file was written)."""
        if fraction is None:
            self.btn_more.finish_progress(done)
        else:
            self.btn_more.set_progress(fraction, eta)
        self.refresh_flyout()

    def set_save_failed(self, on: bool) -> None:
        """The badge on More after a save failed (it goes when the flyout opens)."""
        self.btn_more.set_badge(on and not self._flyout_open())
        self.refresh_flyout()

    # --- The top-right chip: continuing a file, and the nudge to remember a place ------------------

    def on_resumed(self, sentence: int, exact: bool, page: int) -> None:
        """The file continued from its saved place: say so, and offer "Start from the beginning"."""
        self._resume_place = (sentence, page)
        if page:
            self.show_note(f"Continuing {'from' if exact else 'near'} page {page}")
        else:
            self.show_note("Continuing where you stopped" if exact else "Continuing near where you stopped")
        self._start_over_left, self._start_over_since = START_OVER_MS, None
        self._set_corner("start_over")
        self._follow_corner(self.state, self.state, self.info_dict)

    def _set_corner(self, kind: str | None) -> None:
        self._corner_kind = kind
        self._corner_timer.stop()
        if kind == "back":
            self._corner_timer.start(BACK_MS)
        elif kind == "remembered":
            self._corner_timer.start(REMEMBERED_MS)
        self._sync_corner()

    def _sync_corner(self) -> None:
        kind, v = self._corner_kind, self.view
        title = _title_of(self.doc) if self.doc is not None else ""
        if kind == "start_over":
            v.set_corner_chip("start_over", "Start from the beginning",
                              tooltip=f"Read “{title}” from the first page" if title else "Read from the beginning")
        elif kind == "back":
            page = self._resume_place[1]
            v.set_corner_chip("back", f"Back to page {page}" if page else "Back to where you were",
                              tooltip="Go back to where you stopped last time")
        elif kind == "nudge":
            v.set_corner_chip("nudge", "Remember this page next time", tooltip=NUDGE_TIP)
        elif kind == "remembered":
            v.set_corner_chip("remembered", "Saylight will remember your place", clickable=False)
        else:
            v.set_corner_chip(None)

    def _corner_clicked(self, key: str) -> None:
        e = self.ctl.engine
        if key == "start_over":
            e.start_over()
            self._set_corner("back")
        elif key == "back":
            self._set_corner(None)
            e.jump_to(self._resume_place[0])
        elif key == "nudge":
            remember = getattr(self.ctl, "remember_place", None)
            if remember is not None:
                remember()
            self._set_corner("remembered")

    def _corner_expired(self) -> None:
        if self._corner_kind == "start_over" and self.view.underMouse():  # the pointer over the card holds it
            self._corner_timer.start(1000)
            return
        if self._corner_kind in ("start_over", "back", "remembered"):
            self._set_corner(None)

    def _drop_resume_chips(self) -> None:
        """Prev, Next and jumps end "Start from the beginning" and "Back to page N"."""
        if self._corner_kind in ("start_over", "back"):
            self._set_corner(None)

    def _follow_corner(self, was: str, state: str, info: dict) -> None:
        kind = self._corner_kind
        if kind in ("start_over", "back") and state in ("stopped", "finished") and was != state:
            self._set_corner(None)
        elif kind == "start_over":  # it counts 20 s of playing time
            if state == "reading" and self._start_over_since is None:
                self._start_over_since = time.monotonic()
                self._corner_timer.start(max(0, int(self._start_over_left)))
            elif state != "reading" and self._start_over_since is not None:
                self._start_over_left -= (time.monotonic() - self._start_over_since) * 1000
                self._start_over_since = None
                self._corner_timer.stop()
        elif kind == "nudge" and state != "stopped":
            self._set_corner(None)
        if state == "stopped" and was != "stopped" and self._corner_kind is None:
            self._maybe_nudge(info)

    def _maybe_nudge(self, info: dict) -> None:
        """Stopped on a file with Pick up where you left off off: offer to remember the place, once per file a
        session and three times in all."""
        path = _path_of(self.doc) if self.doc is not None else ""
        s = self.settings
        if not path or s.get("resume.enabled") or path in self._nudged or not hasattr(self.ctl, "remember_place"):
            return
        left = (info.get("total") or 0) - (info.get("sentence") or 0) - 1
        count = int(s.get("resume.offer_count") or 0)
        if left < NUDGE_LEFT or count >= 3:
            return
        self._nudged.add(path)
        s.set("resume.offer_count", count + 1)
        self._set_corner("nudge")

    # --- Dropping a file on the panel ------------------------------------------------------------

    def _drag_paths(self, event) -> list[str] | None:
        """The local paths a drag carries, or None for anything else (text, pictures, web links)."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        urls = mime.urls()
        if not urls or not all(u.isLocalFile() for u in urls):
            return None
        return [os.path.normpath(u.toLocalFile()) for u in urls]

    def dragEnterEvent(self, event) -> None:
        paths = self._drag_paths(event)
        if paths is None or self.state == "copying" or self.covered:
            event.ignore()
            return
        event.accept()  # so the card can say why a drop wouldn't work, and gets the leave
        if self.page == "settings":
            self._drop_from_settings = True
            self.set_page("reading")
        kind = drop_kind(paths)
        self._drop_path = paths[0] if kind == "ok" else ""
        if kind == "ok":
            title = os.path.splitext(os.path.basename(paths[0]))[0]
            fixed = measure_parts(self.theme, [("glyph", G_DOCUMENT), ("text", " Drop to read “”")], role="message")
            lines = [K(("glyph", G_DOCUMENT), ("text", f" Drop to read “{self._elide(title, fixed)}”"))]
            if self.doc is not None:
                lines.append(K(("text", "This replaces what you're reading now.")))
            self._drop = (lines, 1)
        elif kind == "many":
            self._drop = ([K(("text", "Drop one file at a time."))], 1)
        elif kind == "folder":
            self._drop = ([K(("text", "Drop a file, not a folder."))], 1)
        elif kind == "ext:.doc":
            self._drop = ([K(("text", "Saylight can't read older Word files (.doc)."))], 1)
        else:
            ext = kind[4:].lstrip(".")
            self._drop = ([K(("text", f"Saylight can't read .{ext} files." if ext else
                                      "Saylight can't read this kind of file."))], 1)
        self.drop_outline.set_on(kind == "ok")
        self._present_card()

    def dragMoveEvent(self, event) -> None:
        if self._drop_path:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._end_drag()
        if self._drop_from_settings:
            self.set_page("settings")
        self._drop_from_settings = False

    def dropEvent(self, event) -> None:
        path = self._drop_path
        self._drop_from_settings = False
        self._end_drag()
        if not path:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.ctl.open_path(path, "drop")

    def _end_drag(self) -> None:
        self._drop, self._drop_path = None, ""
        self.drop_outline.set_on(False)
        self._present_card()

    # --- Native ---------------------------------------------------------------------------------

    def nativeEvent(self, event_type, message):
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_MOUSEACTIVATE:
            return True, MA_NOACTIVATE
        if msg.message in SYSTEM_CHANGE_MESSAGES:
            self.ctl.schedule_theme_refresh()
        return super().nativeEvent(event_type, message)

    def showEvent(self, event) -> None:
        self.tabs.set_active(self.page == "settings")
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._slider_timer.stop()
        self.tabs.set_active(False)
        self.close_flyout(now=True)
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.fillRect(self.rect(), t.color("Surface"))


class _FlyoutPresses(QObject):
    """While the flyout is open, a press anywhere on the panel only closes it (More toggles it itself). A
    right-click's release still reaches the reading page, which opens the flyout again at the new point."""

    def __init__(self, panel: "Panel"):
        super().__init__(panel)
        self.panel = panel

    def eventFilter(self, obj, event) -> bool:
        p = self.panel
        if (event.type() == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget) and obj.window() is p
                and obj is not p.btn_more):
            p.close_flyout()
            return True
        return False


class _DropOutline(QWidget):
    """The card's outline while a file Saylight can read is dragged over the panel: 2 DIP of the text colour at
    60%, inset 1 (Highlight in high contrast). It appears and goes at once."""

    def __init__(self, theme, parent: QWidget):
        super().__init__(parent)
        self.theme = theme
        self.on = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def set_on(self, on: bool) -> None:
        self.on = on
        self.setVisible(on)
        if on:
            self.raise_()
        self.update()

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = 2.0 * t.k
        color = t.color("Accent") if t.high_contrast else with_alpha(t.reading_colors()["text"], 0.60)
        inset = t.k + width / 2
        p.setPen(QPen(color, width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), t.px(6), t.px(6))

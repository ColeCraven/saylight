"""The floating mini-player with its inline settings page.

The window is a frameless, always-on-top tool window that never activates: clicks, drags and the
wheel all work while the user's own app keeps the foreground (so Ctrl+C still reaches it).
"""

from __future__ import annotations

from ctypes import wintypes

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from .. import win32
from .controls import (ACTION_PARTS, G_BACK, G_CLEAR, G_COLLAPSE, G_ERROR, G_EXPAND, G_HIDE, G_NEXT, G_NOTHING,
                       G_OFFLINE, G_PAUSE, G_PLAY, G_PREV, G_RETRY, G_SETTINGS, G_TYPE, FadeLabel, IconButton,
                       Indicator, Part, PlayButton, ProgressBar, SpeedStepper, VolumeSlider, paint_stop_square,
                       volume_glyph)
from .general_page import GeneralPage
from .motion import MEDIUM, SLOW, Tween, decelerate, exit_curve
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

ACTIVE = {"copying", "preparing", "reading", "waiting", "paused"}
SESSION = {"preparing", "reading", "waiting", "paused"}
IDLE = {"ready", "ready_offline", "shortcut_unavailable"}


def K(*parts: Part) -> list[Part]:
    return list(parts)


READY_HINT = [K(("text", "Highlight text anywhere, then press "), ("key", "Ctrl"), ("key", "`")),
              K(("text", "or "), ("action", "type")),
              K(("text", "With nothing highlighted, it shows or hides this panel."))]
READY_HINT_EXPANDED = READY_HINT + [
    K(("key", "`"), ("text", " is the key below Esc, left of 1.")),
    K(("text", "While reading, click any sentence to jump to it.")),
    K(("text", "Text size and background are under Reading display.")),
]
READY_SECONDARY_FROM = 2  # the two lines that say what to do are in the text colour, the notes below them dimmed
CLEARED_HINT = [K(("text", "Text cleared. "), ("action", "undo"))]
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
        self.btn_settings = IconButton(t, tips, G_SETTINGS, size=32, small_glyph=True, parent=self,
                                       tooltip="Settings: reading display, voice and more", name="Settings")
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
        self._reading_widgets = [self.indicator, self.status, self.btn_expand, self.btn_settings, self.view,
                                 self.progress, self.btn_prev, self.play, self.btn_next, self.btn_stop, self.btn_type,
                                 self.btn_clear, self.stepper, self.btn_volume]

        self.page = "reading"
        self.expanded = bool(self.settings.get("panel.expanded"))
        self.view.set_expanded(self.expanded)
        self._sync_expand_button()
        self.state = "ready"
        self.info_dict: dict = {}
        self.doc = None
        self.cleared = False   # the undo window after a Clear: the card offers Undo
        self._undo_card = False  # the card shown is "Text cleared. [Undo]", where only the chip takes clicks
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
        self.btn_settings.clicked.connect(lambda: self.set_page("settings"))
        self.btn_back.clicked.connect(lambda: self.set_page("reading"))
        self.play.clicked.connect(self._on_play)
        self.btn_prev.clicked.connect(e.prev)
        self.btn_next.clicked.connect(e.next)
        self.btn_stop.clicked.connect(e.stop)
        self.btn_type.clicked.connect(lambda: self.ctl.open_compose("button"))
        self.btn_clear.clicked.connect(self.ctl.clear_box)
        self.progress.jumpRequested.connect(e.jump_to)
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
        self.btn_settings.move(self.btn_hide.x() - px(4) - px(32), px(8))
        self.btn_expand.move(self.btn_settings.x() - px(4) - px(32), px(8))
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
        return f"Sentence {i + 1} of {len(self.doc.sentences)} · “{preview}”"

    def on_session(self, doc) -> None:
        self.doc = doc
        self.view.set_document(doc)
        if doc is not None and doc.sentences:
            total = max(1, len(doc.text))
            self.progress.starts = [s.start / total for s in doc.sentences]
        else:
            self.progress.starts = []
        self.progress.set_progress(0.0, 0.0)
        self._present_card()

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
        self.state, self.info_dict = state, info
        self.tabs.on_state(state, info)
        if self.page == "settings":
            return
        reduced = self.theme.reduced_motion
        ind = {
            "ready": ("idle", "TextSecondary", ""),
            "ready_offline": ("glyph", "TextSecondary", G_OFFLINE),
            "copying": ("wave", "Accent", ""),
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
        self.play.set_tooltip({"pause": "Pause (Ctrl+` with nothing highlighted)",
                               "resume": "Resume (Ctrl+` with nothing highlighted)",
                               "retry": retry_tip}.get(action, "Read the highlighted text (Ctrl+`)"))
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
        if self._undo_card:
            v.show_messages(CLEARED_HINT)
        elif state in ("ready",) or (state == "copying" and self.doc is None):
            v.show_messages(READY_HINT_EXPANDED if self.expanded else READY_HINT, READY_SECONDARY_FROM)
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
            v.show_messages(READY_HINT, READY_SECONDARY_FROM)
        else:
            v.show_text()
            v.set_style({"preparing": "muted", "waiting": "muted", "copying": "muted", "paused": "paused",
                         "finished": "finished", "stopped": "stopped"}.get(state, "active"))
            sentence = info.get("sentence")
            if isinstance(sentence, int) and sentence >= 0 and v.sentence < 0:
                v.set_position(sentence, 0, 0)
        if self._card_pointer is not None and not self._card_clickable():
            self._set_card_pointer(None)
        else:
            self._sync_card_actions()

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

    def _present_info(self) -> None:
        state, info = self.state, self.info_dict
        if self.page == "settings":
            return
        if self._speed_note and state in ACTIVE:
            self.info.set_text(self._speed_note, "Accent")
        elif state in ("reading", "paused", "waiting"):
            counter = info.get("counter_text") or (
                f"{info.get('sentence', 0) + 1} / {info.get('total', 0)}" if info.get("total") else "")
            left = self._format_left(self._time_left)
            self.info.set_text(" · ".join(x for x in (counter, left) if x), "TextSecondary")
        elif state == "preparing":
            total = info.get("total") or 0
            self.info.set_text(f"{total} sentence{'s' if total != 1 else ''}" if total else "", "TextSecondary")
        elif state == "stopped" and self.doc is not None and info.get("can_seek"):
            self.info.set_text("Click a sentence to resume", "TextSecondary")
        elif state in ("finished", "stopped"):
            self.info.set_parts([("key", "Ctrl"), ("key", "`"), ("text", " to read")], "TextSecondary")
        else:
            self.info.set_text("", "TextSecondary")
        self.relayout_header()

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
    def _format_left(seconds: int) -> str:
        if seconds is None or seconds < 0:
            return ""
        if seconds >= 3600:
            return f"{seconds // 3600} h {(seconds % 3600) // 60} min left"
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

    def set_cleared(self, on: bool) -> None:
        """During the undo window after a Clear, the empty card reads "Text cleared. [Undo]"."""
        if on != self.cleared:
            self.cleared = on
            self._present_card()
            self._set_card_pointer(None)  # what the card does under the pointer changes; the next move tells

    def _card_can_type(self) -> bool:
        """The card is empty and nothing is under way, so a click on it could open Type or paste."""
        info = self.info_dict
        if self.page != "reading" or self.doc is not None or not info.get("can_type"):
            return False
        return self.state in IDLE or (self.state == "nothing_selected" and info.get("underlying") in IDLE)

    def _card_clickable(self) -> bool:
        """The card takes clicks: it could open Type or paste and shows a chip that says what a click does (so
        not the Offline card, where reading would fail anyway)."""
        lines = self.view.messages or []
        return self._card_can_type() and any(kind == "action" for line in lines for kind, _ in line)

    def _card_target(self, pos) -> str | None:
        """What a click at `pos` on the clickable card is for: an action chip, or "card" for the rest of it. After
        a Clear only the Undo chip counts, so a near miss never opens the box."""
        hit = self.view.action_at(pos)
        return hit if self._undo_card else hit or "card"

    def _set_card_pointer(self, where: str | None) -> None:
        """Where the pointer is on the clickable card: None (not on a target), "card", or the action chip under it."""
        self._card_pointer = where
        self.view.set_card_hover(where is not None and not self._undo_card)
        if where in ACTION_PARTS:
            self.view.setCursor(Qt.CursorShape.PointingHandCursor)
        elif self._undo_card:
            self.view.setCursor(Qt.CursorShape.ArrowCursor)
        self._sync_card_actions()

    def _sync_card_actions(self) -> None:
        """The Type chip takes hover and press from the whole card, since a click anywhere opens the box;
        Undo only from itself."""
        where, press = self._card_pointer, self._card_press
        self.view.set_action_state("type", where in ("card", "type"), press in ("card", "type"),
                                   bool(self.info_dict.get("can_type")))
        self.view.set_action_state("undo", where == "undo", press == "undo", True)

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
                # Right here in the click: Windows lets the box take the foreground only from user input.
                if pressed == "undo":
                    self.ctl.undo_clear()
                else:
                    self.ctl.open_compose("card")
                return
            if self.view.chip_visible() and self.view.chip_rect().contains(d["local"]):
                self.view.follow_now()
                return
            # Stopped or finished, the banded sentence is a valid target too: it reads from there.
            resting = self.state in ("stopped", "finished")
            i = self.view.sentence_at(d["local"])
            if i is not None and self.info_dict.get("can_seek") and (resting or i != self.view.sentence):
                self.view.set_hover(None)
                self.ctl.engine.jump_to(i)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press(event.globalPosition().toPoint(), self, event.position())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        self._move(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._release(event.globalPosition().toPoint())

    def eventFilter(self, obj, event) -> bool:
        if obj is self.view:
            et = event.type()
            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._press(event.globalPosition().toPoint(), self.view, event.position())
                if self._card_clickable():
                    self._card_press = self._card_target(event.position())
                    self._sync_card_actions()
                return True
            if et == QEvent.Type.MouseMove:
                if self._drag is not None:
                    self._move(event.globalPosition().toPoint())
                elif self._card_clickable():
                    self._set_card_pointer(self._card_target(event.position()))
                elif self.view.chip_visible() and self.view.chip_rect().contains(event.position()):
                    self.view.set_hover(None)
                    self.view.setCursor(Qt.CursorShape.PointingHandCursor)
                else:  # off the chip: a sentence when clicks can seek, else plain (clears a chip's hand cursor)
                    target = self.view.sentence_at(event.position()) if self.info_dict.get("can_seek") else None
                    self.view.set_hover(target, allow_current=self.state in ("stopped", "finished"))
                return True
            if et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._release(event.globalPosition().toPoint())
                return True
            if et == QEvent.Type.Leave:
                self.view.set_hover(None)
                self._set_card_pointer(None)
        return super().eventFilter(obj, event)

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
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.fillRect(self.rect(), t.color("Surface"))

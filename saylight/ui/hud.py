"""Status HUD: a brief, click-through status pill shown only while the panel is hidden."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from .. import win32
from .controls import Indicator, Part, measure_parts, paint_parts
from .motion import MEDIUM, SLOW, Tween, decelerate, exit_curve

HUD_FLAGS = (Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
             | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)


class Hud(QWidget):
    def __init__(self, theme, clock):
        super().__init__(None, HUD_FLAGS)
        self.theme = theme
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.indicator = Indicator(theme, clock, self)
        self.label = ""
        self.detail_parts: list[Part] = []  # small secondary text after the label, keycaps allowed
        self._anchor = QRect()
        self._rest = QPoint()
        self.anim = Tween(self, theme, 0.0, MEDIUM)
        self.anim.on_change = self._apply_anim
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.dismiss)
        self._hwnd = int(self.winId())
        win32.dwm_set_corner(self._hwnd)
        win32.dwm_disable_transitions(self._hwnd)
        theme.changed.connect(self._on_theme)
        self._on_theme()

    def _on_theme(self) -> None:
        win32.dwm_set_dark(self._hwnd, self.theme.dark)
        self.indicator.relayout()
        self._relayout()
        self.update()

    def _relayout(self) -> None:
        t = self.theme
        w = t.px(12) + t.px(16) + t.px(8) + QFontMetricsF(t.font("status")).horizontalAdvance(self.label)
        if self.detail_parts:
            w += t.px(8) + measure_parts(t, self.detail_parts, role="small")
        w += t.px(14)
        w = min(t.px(440), int(w + 0.999))
        self.resize(w, t.px(40))
        self.indicator.move(t.px(12), t.px(12))
        a = self._anchor
        if a.isValid():
            screen = QGuiApplication.screenAt(a.center()) or QGuiApplication.primaryScreen()
            area = screen.availableGeometry()
            x = a.center().x() - w // 2
            y = a.bottom() - self.height()
            x = max(area.left(), min(x, area.right() - w + 1))
            y = max(area.top(), min(y, area.bottom() - self.height() + 1))
            self._rest = QPoint(x, y)

    def flash(self, anchor: QRect, mode: str, color_token: str, label: str, detail: str | list[Part] = "",
              glyph: str = "", seconds: float = 1.5) -> None:
        """Show (or update) the HUD; `seconds <= 0` keeps it up until `dismiss()`.

        `detail` is plain text or a list of parts (text and keycaps)."""
        self._anchor = QRect(anchor)
        self.label = label
        self.detail_parts = list(detail) if isinstance(detail, list) else ([("text", detail)] if detail else [])
        self.indicator.set_state(mode, color_token, glyph)
        self._relayout()
        if not self.isVisible():
            self.anim.snap(0.0)
            self._apply_anim(0.0)
            self.show()
            win32.set_topmost(self._hwnd)
        self.anim.to(1.0, MEDIUM, decelerate())
        self.update()
        if seconds > 0:
            self._hide_timer.start(int(seconds * 1000))
        else:
            self._hide_timer.stop()

    def dismiss(self) -> None:
        self._hide_timer.stop()
        if self.isVisible():
            self.anim.to(0.0, SLOW, exit_curve())

    def _apply_anim(self, v: float) -> None:
        if v <= 0.0 and self.anim.target == 0.0:
            self.hide()
            return
        rise = round(self.theme.px(8) * (1 - v))
        self.move(self._rest.x(), self._rest.y() + rise)
        self.setWindowOpacity(1.0 if v >= 0.999 else max(0.0, v))

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), t.color("Surface"))
        x = t.px(12) + t.px(16) + t.px(8)
        font = t.font("status")
        fm = QFontMetricsF(font)
        baseline = (self.height() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
        p.setFont(font)
        p.setPen(t.color("TextPrimary"))
        p.drawText(QPointF(x, baseline), self.label)
        if self.detail_parts:
            x += fm.horizontalAdvance(self.label) + t.px(8)
            paint_parts(p, t, x, 0, self.height(), self.detail_parts, t.color("TextSecondary"), role="small")

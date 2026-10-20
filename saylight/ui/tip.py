"""Non-activating tooltips: Qt's own tooltips misbehave on a window that is never active."""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QPainter, QTextOption
from PySide6.QtWidgets import QWidget

from .. import win32
from .motion import FAST, Tween

TIP_FLAGS = (Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus
             | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowStaysOnTopHint)


class TipWindow(QWidget):
    """A small rounded label that floats above a control without ever taking focus."""

    def __init__(self, theme):
        super().__init__(None, TIP_FLAGS)
        self.theme = theme
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._text = ""
        self._fade = Tween(self, theme, 0.0, FAST)
        self._fade.on_change = lambda v: self.setWindowOpacity(1.0 if v >= 0.999 else v)
        self._hwnd = int(self.winId())
        win32.dwm_set_corner(self._hwnd, small=True)
        win32.dwm_disable_transitions(self._hwnd)
        theme.changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self) -> None:
        win32.dwm_set_dark(self._hwnd, self.theme.dark)
        if self._text:
            self._layout(self._text)
        self.update()

    def _layout(self, text: str) -> QSize:
        t = self.theme
        fm = QFontMetricsF(t.font("small"))
        max_w = t.px(280) - 2 * t.px(8)
        rect = fm.boundingRect(QRectF(0, 0, max_w, 10_000), int(Qt.TextFlag.TextWordWrap), text)
        size = QSize(int(rect.width() + 0.999) + 2 * t.px(8), int(rect.height() + 0.999) + 2 * t.px(5))
        self.resize(size)
        return size

    def show_text(self, text: str, anchor: QRect, *, fade: bool = True) -> None:
        """Show `text` centred above the global rect `anchor` (flipped below when there is no room)."""
        self._text = text
        size = self._layout(text)
        gap = self.theme.px(8)
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = anchor.center().x() - size.width() // 2
        y = anchor.top() - gap - size.height()
        if y < area.top():
            y = anchor.bottom() + gap
        x = max(area.left() + 4, min(x, area.right() - size.width() - 4))
        self.move(x, y)
        self.update()
        if not self.isVisible():
            self._fade.snap(0.0 if fade else 1.0)
            self.setWindowOpacity(self._fade.value if self._fade.value < 0.999 else 1.0)
            self.show()
            win32.set_topmost(self._hwnd)
            self._fade.to(1.0)

    def dismiss(self) -> None:
        if self.isVisible():
            self.hide()
        self._fade.snap(0.0)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), t.color("TipSurface"))
        p.setPen(t.color("TextPrimary"))
        p.setFont(t.font("small"))
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WordWrap)
        inner = QRectF(self.rect()).adjusted(t.px(8), t.px(5), -t.px(8), -t.px(5))
        p.drawText(inner, self._text, opt)


class TipManager:
    """Hover delay rules (500 ms, or 0 ms right after another tip closed) for every control."""

    DELAY_MS = 500
    WARM_MS = 1500

    def __init__(self, theme):
        self.window = TipWindow(theme)
        self._owner: QWidget | None = None
        self._text_fn: Callable[[], str] | None = None
        self._closed_at = 0.0
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show_pending)

    def hover(self, owner: QWidget, text_fn: Callable[[], str]) -> None:
        self._owner, self._text_fn = owner, text_fn
        warm = (time.monotonic() - self._closed_at) * 1000 < self.WARM_MS
        self._timer.start(0 if warm or self.window.isVisible() else self.DELAY_MS)

    def leave(self, owner: QWidget) -> None:
        if owner is self._owner:
            self._timer.stop()
            self._owner = None
            self._close()

    def press(self, owner: QWidget) -> None:
        self._timer.stop()
        self._close()

    def refresh(self, owner: QWidget) -> None:
        """Re-read the text of the visible tip (e.g. after a click changed it)."""
        if owner is self._owner and self.window.isVisible() and self._text_fn:
            self._show_for(owner, self._text_fn(), fade=False)

    def show_at(self, owner: QWidget, text: str, local_rect: QRect) -> None:
        """Show immediately above a specific part of `owner` (used while dragging sliders)."""
        self._timer.stop()
        self._owner = owner
        self._text_fn = lambda: text
        top_left = owner.mapToGlobal(local_rect.topLeft())
        self.window.show_text(text, QRect(top_left, local_rect.size()), fade=False)

    def _show_pending(self) -> None:
        owner, fn = self._owner, self._text_fn
        if owner is None or fn is None or not owner.isVisible() or not owner.underMouse():
            return
        text = fn()
        if text:
            self._show_for(owner, text, fade=True)

    def _show_for(self, owner: QWidget, text: str, *, fade: bool) -> None:
        rect = QRect(owner.mapToGlobal(QPoint(0, 0)), owner.size())
        self.window.show_text(text, rect, fade=fade)

    def _close(self) -> None:
        if self.window.isVisible():
            self._closed_at = time.monotonic()
        self.window.dismiss()

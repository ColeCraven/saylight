"""Animation helpers shared by every widget. All of them honour reduced motion via `theme.ms()`."""

from __future__ import annotations

import time

from PySide6.QtCore import QEasingCurve, QObject, QPointF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget

# Motion tokens, in ms.
FAST = 83
MEDIUM = 167
SLOW = 250


def decelerate() -> QEasingCurve:
    """Fluent "decelerate": cubic-bezier(0, 0, 0, 1)."""
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0, 0), QPointF(0, 1), QPointF(1, 1))
    return curve


def exit_curve() -> QEasingCurve:
    """Fluent "exit": cubic-bezier(0.7, 0, 1, 0.5)."""
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.7, 0), QPointF(1, 0.5), QPointF(1, 1))
    return curve


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """Linear blend of two colours, including alpha."""
    t = 0.0 if t <= 0 else 1.0 if t >= 1 else t
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


def with_alpha(c: QColor, alpha: float) -> QColor:
    out = QColor(c)
    out.setAlphaF(max(0.0, min(1.0, c.alphaF() * alpha)))
    return out


class Tween(QObject):
    """A float that eases toward a target and repaints its widget on every step."""

    def __init__(self, widget: QWidget, theme, value: float = 0.0, duration: int = FAST,
                 curve: QEasingCurve | None = None):
        super().__init__(widget)
        self.value = float(value)
        self._target = float(value)
        self._widget = widget
        self._theme = theme
        self._duration = duration
        self._curve = curve or QEasingCurve(QEasingCurve.Type.Linear)
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_step)
        self.on_change = None  # optional callback(value)

    @property
    def target(self) -> float:
        return self._target

    @property
    def running(self) -> bool:
        return self._anim.state() == QVariantAnimation.State.Running

    def to(self, target: float, duration: int | None = None, curve: QEasingCurve | None = None,
           delay_from: float | None = None) -> None:
        target = float(target)
        if target == self._target and (self.running or self.value == target):
            return
        self._target = target
        ms = self._theme.ms(self._duration if duration is None else duration)
        self._anim.stop()
        if ms <= 0 or not self._widget.isVisible():
            self._set(target)
            return
        self._anim.setDuration(ms)
        self._anim.setEasingCurve(curve or self._curve)
        self._anim.setStartValue(self.value if delay_from is None else float(delay_from))
        self._anim.setEndValue(target)
        self._anim.start()

    def snap(self, value: float) -> None:
        self._anim.stop()
        self._target = float(value)
        self._set(self._target)

    def _on_step(self, v) -> None:
        self._set(float(v))

    def _set(self, v: float) -> None:
        self.value = v
        if self.on_change:
            self.on_change(v)
        self._widget.update()


class Clock(QObject):
    """One shared ~60 fps frame clock that only runs while something subscribes."""

    tick = Signal(float)  # monotonic seconds

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(lambda: self.tick.emit(time.monotonic()))
        self._subscribers: set[int] = set()

    def subscribe(self, owner: QObject) -> None:
        self._subscribers.add(id(owner))
        if not self._timer.isActive():
            self._timer.start()

    def unsubscribe(self, owner: QObject) -> None:
        self._subscribers.discard(id(owner))
        if not self._subscribers:
            self._timer.stop()

    @property
    def active(self) -> bool:
        return self._timer.isActive()

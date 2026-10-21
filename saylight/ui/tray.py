"""Tray icon: crisp voice-bar mark at SM_CXSMICON px, state badges, menu and balloons."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import win32

SPEEDS = (0.5, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.5, 1.75, 2.0)

# Pixel-snapped bar geometry per icon size: bar width, x positions, idle heights.
BAR_TABLE = {
    16: (2, (1, 5, 9, 13), (6, 10, 8, 4)),
    20: (2, (2, 7, 12, 17), (8, 12, 10, 6)),
    24: (3, (2, 8, 14, 20), (8, 14, 10, 6)),
    32: (4, (2, 10, 18, 26), (12, 20, 16, 8)),
}
READING_FRAMES = ((6, 12, 8, 4), (8, 9, 12, 6), (10, 6, 10, 9), (7, 10, 6, 12),
                  (5, 12, 9, 8), (9, 8, 12, 5), (12, 6, 8, 7), (8, 10, 5, 10))
WAVE_FRAMES = ((10, 4, 4, 4), (6, 10, 4, 4), (4, 6, 10, 4), (4, 4, 6, 10), (4, 4, 4, 6), (4, 4, 4, 4))


def speed_label(v: float) -> str:
    text = f"{v:.2f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return f"{text}×"


class TrayIconEngine(QIconEngine):
    """Renders exactly the requested pixel size at DPR 1 so the shell never rescales (blur fix)."""

    def __init__(self, spec: dict):
        super().__init__()
        self.spec = spec

    def clone(self):
        return TrayIconEngine(dict(self.spec))

    def scaledPixmap(self, size, mode, state, scale):
        return self._render(win32.small_icon_size())

    def pixmap(self, size, mode, state):
        return self._render(size.width())

    def paint(self, painter, rect, mode, state):
        painter.drawPixmap(rect, self._render(rect.width()))

    def _bars(self, px: int):
        key = min(BAR_TABLE, key=lambda s: abs(s - px))
        width, xs, idle = BAR_TABLE[key]
        scale = px / key
        return width * scale, [x * scale for x in xs], idle, scale

    def _render(self, px: int) -> QPixmap:
        s = self.spec
        pm = QPixmap(px, px)
        pm.setDevicePixelRatio(1.0)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(s.get("opacity", 1.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(s["color"])
        width, xs, idle, scale = self._bars(px)
        mode = s.get("mode", "idle")
        if mode == "paused":
            bw, bh = round(px * 0.2), round(px * 0.6)
            top = (px - bh) / 2
            for x in (round(px * 0.25), round(px * 0.55)):
                p.drawRoundedRect(QRectF(x, top, bw, bh), bw / 3, bw / 3)
        else:
            if mode == "reading":
                heights = [h * px / 16 for h in READING_FRAMES[s.get("frame", 0) % len(READING_FRAMES)]]
            elif mode == "wave":
                heights = [h * px / 16 for h in WAVE_FRAMES[s.get("frame", 0) % len(WAVE_FRAMES)]]
            else:
                heights = [h * scale for h in idle]
            for x, h in zip(xs, heights):
                h = max(2.0, 2 * round(h / 2))
                p.drawRoundedRect(QRectF(x, (px - h) / 2, width, h), width / 2, width / 2)
        p.setOpacity(1.0)
        badge = s.get("badge")
        if badge is not None:
            d = round(0.4 * px)
            c = 0.75 * px
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.setBrush(Qt.GlobalColor.black)
            p.drawEllipse(QRectF(c - d / 2 - 1, c - d / 2 - 1, d + 2, d + 2))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setBrush(badge)
            p.drawEllipse(QRectF(c - d / 2, c - d / 2, d, d))
        p.end()
        return pm


class Tray:
    """Owns the QSystemTrayIcon and its menu. `ctl` is the app controller (see saylight_app.py)."""

    def __init__(self, ctl, theme):
        self.ctl = ctl
        self.theme = theme
        self.icon = QSystemTrayIcon()
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._build_menu)
        self.menu.aboutToHide.connect(lambda: QTimer.singleShot(0, ctl.restore_foreground))
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self._state = "ready"
        self._info: dict = {}
        self._badge_until_reset = False
        self._frame = 0
        self._anim = QTimer()
        self._anim.setInterval(250)  # 4 fps
        self._anim.timeout.connect(self._next_frame)
        self._badge_timer = QTimer()
        self._badge_timer.setSingleShot(True)
        self._badge_timer.timeout.connect(self._clear_badge)
        self._badge: str | None = None
        theme.changed.connect(self._refresh_icon)
        self._refresh_icon()
        self.icon.setToolTip("Saylight · Ready")

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self._anim.stop()
        self.icon.hide()

    # --- State ------------------------------------------------------------------------------

    def set_state(self, state: str, info: dict) -> None:
        self._state, self._info = state, info
        if state == "nothing_selected":
            self._set_badge("Caution", 3000)
        elif state.startswith("error"):
            self._set_badge("Critical", 60_000)
        elif state == "shortcut_unavailable":
            self._set_badge("Critical", 0)
        elif state in ("copying", "preparing", "reading", "waiting", "paused"):
            self._set_badge(None, 0)
        animated = state in ("copying", "preparing", "reading", "waiting") and not self.theme.reduced_motion
        if animated and not self._anim.isActive():
            self._frame = 0
            self._anim.start()
        elif not animated:
            self._anim.stop()
        self._refresh_icon()
        self.icon.setToolTip(self._tooltip()[:127])

    def _set_badge(self, token: str | None, ms: int) -> None:
        self._badge = token
        if token and ms:
            self._badge_timer.start(ms)
        else:
            self._badge_timer.stop()

    def _clear_badge(self) -> None:
        if self._state != "shortcut_unavailable":
            self._badge = None
            self._refresh_icon()

    def _next_frame(self) -> None:
        self._frame += 1
        self._refresh_icon()

    def _icon_spec(self) -> dict:
        t = self.theme
        fg = t.tray_color("Foreground")
        state = self._state
        spec = {"mode": "idle", "color": fg, "frame": self._frame, "opacity": 1.0}
        if state in ("copying", "preparing", "waiting"):
            spec["mode"] = "wave"
        elif state == "reading":
            spec["mode"] = "reading" if not t.reduced_motion else "idle"
            spec["color"] = t.tray_color("Accent")  # the theme already handles an accent-coloured taskbar
        elif state == "paused":
            spec["mode"] = "paused"
            spec["color"] = t.tray_color("Accent")
        elif state in ("ready_offline", "shortcut_unavailable") or state.startswith("error"):
            spec["opacity"] = 0.55
        if self._badge:
            spec["badge"] = t.tray_color(self._badge)
        return spec

    def _refresh_icon(self) -> None:
        self.icon.setIcon(QIcon(TrayIconEngine(self._icon_spec())))

    def _tooltip(self) -> str:
        i = self._info
        st = self._state
        counter = f"{i.get('sentence', 0) + 1} of {i.get('total', 0)}" if i.get("total") else ""
        if st in ("ready", "finished", "stopped"):
            label = {"ready": "Ready", "finished": "Finished", "stopped": "Stopped"}[st]
            return f"Saylight · {label}\nClick to show or hide · Ctrl+` reads"
        if st == "ready_offline":
            return "Saylight · Offline (reading needs internet)"
        if st in ("copying", "preparing", "waiting"):
            return "Saylight · Preparing audio…"
        if st == "reading":
            return f"Saylight · Reading {counter} · {speed_label(self.ctl.speed())}\nCtrl+` or middle-click to pause"
        if st == "paused":
            return f"Saylight · Paused at {counter}\nCtrl+` or middle-click to resume"
        if st == "nothing_selected":
            return "Saylight · Nothing was selected"
        if st == "shortcut_unavailable":
            return "Saylight · Ctrl+` is in use by another app"
        return f"Saylight · {i.get('label', 'Error')}"

    # --- Interaction --------------------------------------------------------------------------

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger and self.ctl.composing():
            self.ctl.close_compose("tray")  # keeps the text; the panel stays as it was
            QTimer.singleShot(0, self.ctl.restore_foreground)
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.ctl.toggle_panel(from_tray=True)
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.ctl.engine.toggle_pause()

    def balloon(self, title: str, body: str) -> None:
        if self.icon.isVisible() and self.icon.supportsMessages():
            self.icon.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 8000)

    def _build_menu(self) -> None:
        ctl = self.ctl
        if ctl.composing():
            ctl.close_compose("tray")  # keeps the text; aboutToHide gives the foreground back
        m = self.menu
        m.clear()
        header = m.addAction(self._tooltip().split("\n")[0])
        header.setEnabled(False)
        m.addSeparator()
        show = m.addAction("Show panel")
        show.setCheckable(True)
        show.setChecked(ctl.panel_visible())
        show.triggered.connect(lambda: ctl.toggle_panel(from_tray=True))
        hud = m.addAction("Status pop-ups while hidden")
        hud.setCheckable(True)
        hud.setChecked(bool(ctl.settings.get("hud_when_hidden")))
        hud.triggered.connect(lambda on: ctl.settings.set("hud_when_hidden", on))
        m.addSeparator()
        can_type = bool(self._info.get("can_type"))
        compose = m.addAction("Type or paste text…", lambda: ctl.open_compose("tray"))
        compose.setEnabled(can_type)
        if self._state in ("copying", "preparing", "reading", "waiting", "paused"):
            m.addAction("Resume" if self._state == "paused" else "Pause", ctl.engine.toggle_pause)
            m.addAction("Stop", ctl.engine.stop)
        elif ctl.has_text():
            m.addAction("Read again from start", ctl.engine.replay_from_start)
        if self._info.get("can_clear") or (can_type and ctl.has_draft()):
            m.addAction("Clear text", ctl.clear_box)
        if ctl.can_undo_clear():
            m.addAction("Undo clear", ctl.undo_clear)
        m.addSeparator()
        speed_menu = m.addMenu("Speed")
        group = QActionGroup(speed_menu)
        current = ctl.speed()
        for v in SPEEDS:
            a = QAction(speed_label(v), speed_menu)
            a.setCheckable(True)
            a.setChecked(abs(v - current) < 1e-6)
            a.triggered.connect(lambda _=False, v=v: ctl.set_speed(v))
            group.addAction(a)
            speed_menu.addAction(a)
        display = m.addAction("Reading display…")
        display.triggered.connect(lambda: ctl.open_settings())
        m.addSeparator()
        if self._state == "shortcut_unavailable":
            m.addAction("Retry shortcut", ctl.retry_shortcut)
        m.addAction("Reset panel position", ctl.reset_position)
        m.addAction("Open log", ctl.open_log)
        m.addSeparator()
        m.addAction("Quit Saylight", ctl.quit)

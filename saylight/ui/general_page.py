"""The General tab: start with Windows, the shortcut, the update check, privacy and licences, and the version."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from .controls import G_OPEN, FadeLabel, LinkText, NavRow, Part, ToggleRow, measure_parts, spoken_parts
from .settings_pages import COLUMN, EDGE, OPTICAL, PITCH, SettingsPage
from .update_bar import short_version

CHECK_RESULTS = {"current": "You have the latest version", "error": "Couldn't check. Try again later."}
# What follows the keycaps, longest first: a long shortcut keeps the shorter wording, or the keycaps alone.
SHORTCUT_NOTES = {True: (" reads the highlighted text", " reads highlighted text", ""),
                  False: (" · another app is using it", " · in use", "")}


def shortcut_parts(chord: str) -> list[Part]:
    """ "Ctrl+`" -> keycaps Ctrl and `."""
    return [("key", k) for k in chord.split("+") if k] if chord else [("text", "None")]


class GeneralPage(SettingsPage):
    title = "General"
    tooltip = "Start with Windows, the shortcut, updates and licences"

    def __init__(self, host: QWidget, ctl) -> None:
        super().__init__(host, ctl)
        t, tips = self.theme, self.tips
        self.updates = getattr(ctl, "updates", None)
        self.row_start = ToggleRow(t, tips, "Start with Windows", host,
                                   tooltip="Start Saylight when you sign in to Windows")
        self.lbl_shortcut = self.label("Shortcut")
        self.val_shortcut = FadeLabel(t, "body", "TextPrimary", host)
        self.updates_line = LinkText(t, tips, host, role="body", color_token="TextSecondary")
        self.row_notices = NavRow(t, tips, "Privacy and licences", host, glyph=G_OPEN,
                                  tooltip="Saylight doesn't keep your text or audio. This opens the licences of "
                                          "Saylight and the parts it's built from.")
        self.version_line = LinkText(t, tips, host, role="small", color_token="TextSecondary")
        self.version_line.link_tips = {"check": "Ask GitHub now whether there's a newer Saylight"}
        self.shortcut_ok = True
        self.check = ""   # after "Check for updates": "checking", "newer", "current" or "error"
        self.row_start.setEnabled(hasattr(ctl, "set_start_with_windows"))
        self.row_notices.setEnabled(hasattr(ctl, "open_notices"))
        self.row_start.toggled.connect(self._set_start)
        self.row_notices.clicked.connect(lambda: ctl.open_notices())
        self.updates_line.linkClicked.connect(self._toggle_checks)
        self.version_line.linkClicked.connect(self._check_now)
        if self.updates is not None and hasattr(self.updates, "checked"):
            self.updates.checked.connect(self._on_checked)
        self.settings.changed.connect(
            lambda key: self.sync() if key == "updates.check" or key.startswith("hotkeys") else None)

    def widgets(self) -> list[QWidget]:
        return [self.row_start, self.lbl_shortcut, self.val_shortcut, self.updates_line, self.row_notices,
                self.version_line]

    def layout(self, x: int, y: int, w: int, fill: int = 0) -> int:
        px = self.theme.px
        top = y
        row_w = w - 2 * px(EDGE)
        text_w = w - 2 * px(OPTICAL)
        self.row_start.setGeometry(x + px(EDGE), y, row_w, px(40))
        y += px(PITCH)
        col = px(COLUMN)
        self.lbl_shortcut.setGeometry(x + px(OPTICAL), y + px(10), col - px(OPTICAL), px(20))
        self.val_shortcut.setGeometry(x + col, y + px(10), w - px(OPTICAL) - col, px(20))
        self._fit_shortcut()
        y += px(PITCH)
        self.rules = [y + px(4)]
        y += px(9)
        self.updates_line.setGeometry(x + px(OPTICAL), y + px(10), text_w, px(20))
        y += px(PITCH)
        self.row_notices.setGeometry(x + px(EDGE), y, row_w, px(40))
        y += px(PITCH)
        self.version_line.setGeometry(x + px(OPTICAL), y + px(10), text_w, px(20))
        y += px(PITCH)
        return y - px(2) + px(EDGE) - top

    def sync(self) -> None:
        start = getattr(self.ctl, "start_with_windows", None)
        self.row_start.set_checked(bool(start()) if callable(start) else False)
        self._fit_shortcut()
        if self.settings.get("updates.check"):
            line = [("text", "Saylight checks GitHub for updates once a day"), ("sep", " · "), ("toggle", "Turn off")]
            self.updates_line.link_tips = {"toggle": "Stop the daily check. You can still use Check for updates "
                                                     "below."}
        else:
            line = [("text", "Update checks are off"), ("sep", " · "), ("toggle", "Turn on")]
            self.updates_line.link_tips = {"toggle": "Check GitHub for a newer Saylight once a day"}
        self.updates_line.set_parts(line)
        self._sync_version()

    def _fit_shortcut(self) -> None:
        """The keycaps, then the longest wording that fits the row; screen readers always hear all of it."""
        chord = str((self.settings.get("hotkeys") or {}).get("read", ""))
        keys = shortcut_parts(chord)
        notes = SHORTCUT_NOTES[self.shortcut_ok]
        room = self.val_shortcut.width()
        parts = keys + [("text", notes[-1])]
        for note in notes:
            candidate = keys + [("text", note)] if note else keys
            if room <= 0 or measure_parts(self.theme, candidate) <= room:
                parts = candidate
                break
        self.val_shortcut.set_parts(parts, "TextPrimary" if self.shortcut_ok else "Critical")
        self.val_shortcut.setAccessibleName(spoken_parts(keys + [("text", notes[0])]))

    def _sync_version(self) -> None:
        version = str(getattr(self.ctl, "version", "") or "")
        parts: list[Part] = [("text", f"Saylight {version}"), ("sep", " · ")] if version else []
        if self.check == "checking":
            parts.append(("text", "Checking…"))
        elif self.check == "newer":
            latest = getattr(self.updates, "latest", None)
            ready = f"Saylight {short_version(latest.version)} is ready" if latest else "A newer version is ready"
            parts.append(("text", ready))
        elif self.check in CHECK_RESULTS:
            parts.append(("text", CHECK_RESULTS[self.check]))
        elif self.updates is not None:
            parts.append(("check", "Check for updates"))
        elif parts:
            parts.pop()
        self.version_line.set_parts(parts)

    def on_state(self, state: str, info: dict) -> None:
        ok = state != "shortcut_unavailable" and info.get("shortcut_ok", True) is not False
        if ok != self.shortcut_ok:
            self.shortcut_ok = ok
            self.sync()

    def set_shown(self, on: bool) -> None:
        if on:
            self.sync()
        elif self.check != "checking":
            self.check = ""  # a result is shown until the page is left
            self._sync_version()

    # --- Actions

    def _set_start(self, on: bool) -> None:
        self.ctl.set_start_with_windows(on)
        self.row_start.set_checked(bool(self.ctl.start_with_windows()))

    def _toggle_checks(self, _key: str) -> None:
        self.settings.set("updates.check", not self.settings.get("updates.check"))

    def _check_now(self, _key: str) -> None:
        self.check = "checking"
        self._sync_version()
        self.updates.check_now()

    def _on_checked(self, status: str) -> None:
        if self.check == "checking":
            self.check = status if status in ("newer", "current") else "error"
            self._sync_version()

"""The update line under the panel's header.

`NoticeStrip` is a one-line notice: a glyph, text with inline links and an optional progress line, on a quiet
plate the width of the reading card. Other notices can use it too. `UpdateBar` follows the UpdateService and
shows the update on a strip: "Saylight 1.3 is ready · What's new · Install · Not now", the download, and what
went wrong if it failed. The tray reads the same state for its update item.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .controls import G_DOWNLOAD, G_ERROR, LinkText, Part, draw_glyph
from .motion import MEDIUM, Tween, decelerate

STRIP_H = 32   # DIP; the panel leaves 8 below it
GLYPH_X = 10   # the glyph on the optical inset (22 in the panel)
TEXT_X = 34    # the text where the header's status starts (46 in the panel)

SEP: Part = ("sep", " · ")
FAIL_REASONS = {
    "network": "The download didn't finish. Check your internet connection, or get Saylight {v} from the "
               "download page.",
    "size": "The download was damaged, so Saylight deleted it. You can get Saylight {v} from the download page.",
    "launch": "Windows didn't start Setup. You can get Saylight {v} from the download page.",
}
FAIL_REASONS["checksum"] = FAIL_REASONS["signature"] = FAIL_REASONS["size"]


def short_version(version: str) -> str:
    """ "1.3.0" -> "1.3"; "1.3.1" stays as it is."""
    parts = version.split(".")
    while len(parts) > 2 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


def open_url(url: str) -> None:
    """Open a page in the person's browser (only ever on a click)."""
    QDesktopServices.openUrl(QUrl(url))


class NoticeStrip(QWidget):
    """A quiet one-line notice on a SegmentTrack plate: glyph, text with links, and an optional progress line."""

    linkClicked = Signal(str)

    def __init__(self, theme, tips, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.glyph = ""
        self.glyph_token = "Accent"
        self.text = LinkText(theme, tips, self, role="body", color_token="TextPrimary")
        self.text.linkClicked.connect(self.linkClicked)
        self.progress = Tween(self, theme, 0.0, MEDIUM, decelerate())
        self.show_progress = False
        theme.changed.connect(self.update)

    def set_notice(self, glyph: str, glyph_token: str, parts: list[Part], *, progress: float | None = None,
                   tip: str = "", link_tips: dict[str, str] | None = None) -> None:
        """Show a notice. `progress` (0-1) draws the progress line; `tip` explains the text."""
        self.glyph, self.glyph_token = glyph, glyph_token
        self.text.link_tips = dict(link_tips or {})
        self.text.text_tip = tip
        self.text.set_parts(parts)
        self.setAccessibleName(self.text.accessibleName())
        self.setAccessibleDescription(tip)
        self.show_progress = progress is not None
        if progress is None:
            self.progress.snap(0.0)
        else:
            self.progress.to(max(0.0, min(1.0, progress)))
        self.update()

    def relayout(self) -> None:
        px = self.theme.px
        self.text.setGeometry(px(TEXT_X), 0, max(0, self.width() - px(TEXT_X) - px(GLYPH_X)), self.height())

    def resizeEvent(self, event) -> None:
        self.relayout()
        super().resizeEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = t.px(4)
        p.setPen(QPen(t.color("ControlStroke"), 0))
        p.setBrush(t.color("SegmentTrack"))
        p.drawRoundedRect(r, radius, radius)
        g = t.px(16)
        draw_glyph(p, QRectF(t.px(GLYPH_X), (self.height() - g) / 2, g, g), self.glyph, t.font("icon16"),
                   t.color(self.glyph_token))
        if self.show_progress:
            h = t.px(2)
            track = QRectF(t.px(GLYPH_X), self.height() - t.px(3) - h, self.width() - 2 * t.px(GLYPH_X), h)
            p.setPen(QPen(t.color("TrackOutline"), 0) if t.high_contrast else Qt.PenStyle.NoPen)
            p.setBrush(t.color("Track"))
            p.drawRoundedRect(track, h / 2, h / 2)
            if self.progress.value > 0:
                done = QRectF(track.left(), track.top(), track.width() * self.progress.value, h)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(t.color("Accent"))
                p.drawRoundedRect(done, h / 2, h / 2)


class UpdateBar(QObject):
    """The update's state, from the UpdateService's signals, shown on a NoticeStrip.

    state: "none", "ready", "downloading", "starting" (Setup is being started) or "failed". "Not now" skips the
    version and hides the panel line; the tray item stays for it."""

    changed = Signal()

    def __init__(self, updates, strip: NoticeStrip, parent: QObject | None = None):
        super().__init__(parent)
        self.updates = updates
        self.strip = strip
        self.release = None
        self.state = "none"
        self.fail_kind = ""
        self.done = self.total = 0
        self.dismissed = False
        strip.linkClicked.connect(self._on_link)
        if updates is not None:
            updates.available.connect(self._on_available)
            updates.downloadProgress.connect(self._on_progress)
            updates.failed.connect(self._on_failed)

    def visible(self) -> bool:
        """The panel line has something to show."""
        return self.state != "none" and not self.dismissed

    def version(self) -> str:
        return short_version(self.release.version) if self.release is not None else ""

    def install(self, release) -> None:
        """Download and start Setup (Install, or the tray item)."""
        if self.updates is None or self.state in ("downloading", "starting"):
            return
        self.release = release
        self.done = self.total = 0
        self.dismissed = False
        self._set_state("downloading")
        self.updates.install(release)

    def open_page(self) -> None:
        if self.release is not None:
            open_url(self.release.url)

    def tray_item(self):
        """(text, enabled, action) for the tray's update item, or None when there is no newer version."""
        release = self.release or getattr(self.updates, "latest", None)
        if release is None:
            return None
        v = short_version(release.version)
        if self.state == "downloading":
            return f"Downloading Saylight {v}… {self._amount()}".rstrip(), False, None
        if self.state == "starting":
            return "Starting Setup…", False, None
        if self.state == "failed" or not self._installable(release):
            return f"Open the Saylight {v} download page", True, lambda: open_url(release.url)
        return f"Install Saylight {v}…", True, lambda: self.install(release)

    # --- Service signals

    def _on_available(self, release) -> None:
        if self.state in ("downloading", "starting"):
            return
        self.release = release
        self.dismissed = False
        self._set_state("ready")

    def _on_progress(self, done: int, total: int) -> None:
        self.done, self.total = done, total
        self._set_state("starting" if total > 0 and done >= total else "downloading")

    def _on_failed(self, kind: str) -> None:
        if self.release is None:
            return
        self.fail_kind = kind
        self.dismissed = False
        self._set_state("failed")

    def _on_link(self, key: str) -> None:
        if key == "install" and self.release is not None:
            self.install(self.release)
        elif key in ("notes", "page"):
            self.open_page()
        elif key == "later" and self.release is not None:
            if self.updates is not None:
                self.updates.skip(self.release.version)
            self.dismissed = True
            self.changed.emit()

    # --- The line

    def _installable(self, release) -> bool:
        """Install can run Setup for this release here (see UpdateService.can_install); otherwise the line and
        the tray item send the person to the download page."""
        return bool(release is not None and release.setup_url) and getattr(self.updates, "can_install", True)

    def _amount(self) -> str:
        if self.total > 0:
            return f"{min(100, round(100 * self.done / self.total))}%"
        return f"{self.done / 1_000_000:.1f} MB" if self.done else ""

    def _set_state(self, state: str) -> None:
        self.state = state
        self._show()
        self.changed.emit()

    def _show(self) -> None:
        v = self.version()
        s = self.strip
        if self.state == "ready":
            parts: list[Part] = [("text", f"Saylight {v} is ready"), SEP]
            if self._installable(self.release):
                parts += [("notes", "What's new"), SEP, ("install", "Install")]
            else:
                parts += [("page", "Get it")]
            parts += [SEP, ("later", "Not now")]
            s.set_notice(G_DOWNLOAD, "Accent", parts, link_tips={
                "notes": f"See what's new in Saylight {v} (opens your browser)",
                "install": f"Download Saylight {v} and install it. Saylight closes and opens again.",
                "page": f"Open the Saylight {v} download page, with what's new (opens your browser)",
                "later": "Hide this until the next version. The tray menu still offers it."})
        elif self.state == "downloading":
            fraction = self.done / self.total if self.total > 0 else None
            s.set_notice(G_DOWNLOAD, "Accent", [("text", f"Downloading Saylight {v}… {self._amount()}".rstrip())],
                         progress=fraction)
        elif self.state == "starting":
            s.set_notice(G_DOWNLOAD, "Accent", [("text", "Starting Setup…")], progress=1.0)
        elif self.state == "failed":
            reason = FAIL_REASONS.get(self.fail_kind, FAIL_REASONS["network"]).format(v=v)
            s.set_notice(G_ERROR, "Critical",
                         [("text", "Couldn't update"), SEP, ("page", "Open the download page"), SEP,
                          ("later", "Not now")],
                         tip=reason, link_tips={"page": f"Get Saylight {v} from its download page (opens your "
                                                        "browser)",
                                                "later": "Hide this until the next version"})

"""The PDF password sheet: laid exactly over the panel, it asks for the password of a PDF that needs one.

It opens only from a click (the [Enter password] chip, or the tray's Enter PDF password…), never by itself. The
well where the card was says which file needs the password, the field takes it, and the Read aloud pill where
Play was opens the file. The password goes to the loader for that one call and the field is cleared whenever the
sheet closes: it is never stored, logged or put on the clipboard. Reading is never paused.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QLineEdit, QWidget

from .. import win32
from .compose import G_READ, READ_LABEL
from .controls import G_EYE, G_EYE_OFF, G_LOCK, IconButton, PillButton
from .sheet import LineField, Sheet

NATURAL = QSize(440, 252)            # the compact panel; the sheet always takes the panel's own rect
MAX_PASSWORD = 256
TITLE = "Password needed"
NOTE = "Saylight uses it once to open the file and doesn't keep it."
HINT = [("key", "Enter"), ("text", " opens it  ·  "), ("key", "Esc"), ("text", " cancels")]
CHECKING = [("text", "Checking the password…")]
WRONG = "That password didn't work. Check Caps Lock, then try again."
CAPS = "Caps Lock is on."
PILL_TIP = "Open the PDF and read it aloud (Enter)"


def message(title: str) -> str:
    return f"“{title}” needs a password." if title else "This PDF needs a password."


def well_rect(theme, W: int, H: int) -> QRect:
    """Where the panel's card is: from y 44 to 80 DIP above the bottom (progress, the transport row, margins)."""
    px = theme.px
    return QRect(px(12), px(44), W - 2 * px(12), H - px(44) - px(80))


class _Well(QWidget):
    """The card's well: reading background and hairline, the message on top and the note under the field."""

    def __init__(self, theme, parent: QWidget):
        super().__init__(parent)
        self.theme = theme
        self.title = ""
        self.message_top = 0
        self.note_top = 0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event) -> None:
        t = self.theme
        px = t.px
        c = t.reading_colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(c["edge"], 0))
        p.setBrush(c["background"])
        p.drawRoundedRect(r, px(6), px(6))
        room = self.width() - 2 * px(14)
        font = t.font("message")
        fm = QFontMetricsF(font)
        text = message(self.title)
        if fm.horizontalAdvance(text) > room and self.title:  # the title gives way, from its middle
            fixed = fm.horizontalAdvance(message(""))
            short = fm.elidedText(self.title, Qt.TextElideMode.ElideMiddle, max(0.0, room - fixed))
            text = message(short)
        p.setFont(font)
        p.setPen(c["text"])
        line = px(24)
        p.drawText(QPointF(px(14), self.message_top + (line - (fm.ascent() + fm.descent())) / 2 + fm.ascent()), text)
        small = t.font("small")
        sfm = QFontMetricsF(small)
        p.setFont(small)
        p.setPen(c["secondary"])
        p.drawText(QPointF(px(14), self.note_top + (px(16) - (sfm.ascent() + sfm.descent())) / 2 + sfm.ascent()),
                   sfm.elidedText(NOTE, Qt.TextElideMode.ElideRight, room))


class _PasswordField(LineField):
    """The field, with room at its right end for the show/hide button."""

    def apply_theme(self) -> None:
        super().apply_theme()
        t = self.theme
        self.setTextMargins(t.px(10), 0, t.px(10) + t.px(32), 0)


class PasswordSheet(Sheet):
    """Call prepare(title), then the App opens it; `submitted` carries the password for one open."""

    submitted = Signal(str)

    def __init__(self, ctl):
        super().__init__(ctl, name="PDF password", glyph=G_LOCK, title=TITLE, natural=NATURAL, mode="panel")
        t, tips, host = self.theme, self.tips, self.body
        self.well = _Well(t, host)
        self.field = _PasswordField(t, host, style="control", in_well=True, name="PDF password",
                                    placeholder="Password")
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setMaxLength(MAX_PASSWORD)
        self.field.setDragEnabled(False)
        self.field.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)  # nothing to copy or cut
        self.btn_reveal = IconButton(t, tips, G_EYE, size=28, small_glyph=True, parent=host, focusable=True,
                                     tooltip=self._reveal_tip, name="Show password")
        self.pill = PillButton(t, tips, G_READ, READ_LABEL, host, tooltip=PILL_TIP)
        self.checking = False
        self._caps = QTimer(self)
        self._caps.setInterval(250)
        self._caps.timeout.connect(self._sync_caps)
        self.set_hint(HINT)
        self.field.textChanged.connect(lambda _s: self._sync())
        self.btn_reveal.clicked.connect(self._toggle_reveal)
        self.pill.clicked.connect(self.primary)
        QWidget.setTabOrder(self.field, self.btn_reveal)
        QWidget.setTabOrder(self.btn_reveal, self.pill)
        QWidget.setTabOrder(self.pill, self.btn_back)
        self._sync()

    # --- Content -------------------------------------------------------------------------------

    def prepare(self, title: str) -> None:
        """The file's title (its name without the extension) for the message."""
        self.well.title = title
        self.well.update()

    def _reveal_tip(self) -> str:
        return "Hide password" if self.revealed else "Show password"

    @property
    def revealed(self) -> bool:
        return self.field.echoMode() == QLineEdit.EchoMode.Normal

    def _toggle_reveal(self) -> None:
        show = not self.revealed
        self.field.setEchoMode(QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password)
        self.btn_reveal.set_glyph(G_EYE_OFF if show else G_EYE)
        self.btn_reveal.setAccessibleName("Hide password" if show else "Show password")
        self.btn_reveal.set_tooltip(self._reveal_tip)

    def _sync(self) -> None:
        self.pill.setEnabled(bool(self.field.text()) and not self.checking)
        if not self.checking and self.hint.note and self.hint.content[1] == WRONG and self.field.text() == "":
            self.end_note()

    def _sync_caps(self) -> None:
        if self.checking or (self.hint.note and self.hint.content[1] == WRONG):
            return
        caps = win32.caps_lock_on() and self.field.hasFocus()
        if caps and not (self.hint.note and self.hint.content[1] == CAPS):
            self.show_note(CAPS)
        elif not caps and self.hint.note and self.hint.content[1] == CAPS:
            self.end_note()

    # --- The App's answers ---------------------------------------------------------------------

    def wrong_password(self) -> None:
        """The password didn't open the file: say so, and select it so typing replaces it."""
        self.checking = False
        self.set_hint(HINT)
        self.show_note(WRONG)
        self.field.selectAll()
        self.field.setFocus(Qt.FocusReason.OtherFocusReason)
        self._sync()

    # --- Sheet hooks ---------------------------------------------------------------------------

    def focus_target(self) -> QWidget | None:
        return self.field

    def primary(self) -> None:
        password = self.field.text()
        if not password or self.checking:
            return
        self.checking = True
        self.end_note()
        self.set_hint(CHECKING)
        self._sync()
        self.submitted.emit(password)

    def open_at(self, rest, *, return_to: int, panel_visible: bool = True, source: str = "") -> bool:
        self._caps.start()
        return super().open_at(rest, return_to=return_to, panel_visible=panel_visible, source=source)

    def close_animated(self, reason: str) -> None:
        self._caps.stop()
        # The password never outlives the sheet: setText() also empties the undo history, which clear() keeps.
        self.field.setText("")
        if self.revealed:
            self._toggle_reveal()
        self.checking = False
        self.set_hint(HINT)
        self._sync()
        super().close_animated(reason)

    def layout_content(self, W: int, H: int) -> None:
        t = self.theme
        px = t.px
        well = well_rect(t, W, H)
        self.well.setGeometry(well)
        block = px(24) + px(12) + px(34) + px(8) + px(16)
        top = (well.height() - block) // 2
        self.well.message_top = top
        self.well.note_top = top + px(24) + px(12) + px(34) + px(8)
        field = QRect(well.left() + px(14), well.top() + top + px(36), well.width() - 2 * px(14), px(34))
        self.field.setGeometry(field)
        self.btn_reveal.place(QRect(field.right() + 1 - px(4) - px(28), field.top() + (field.height() - px(28)) // 2,
                                    px(28), px(28)))
        self.btn_reveal.raise_()
        size = self.pill.natural_size()
        self.pill.place(QRect(px(12), H - px(52), size.width(), size.height()))
        self.well.update()

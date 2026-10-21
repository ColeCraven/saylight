"""The Voice tab: the voice list, grouped by accent, with a sample for each voice, and "Show all languages".

The built-in English list needs no network. The full list is fetched only when the person turns on "Show all
languages". A preview reads a fixed sample sentence, never the person's text.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..voices.base import DEFAULT_VOICE_ID, parse_voice_id
from ..voices.base import EDGE as EDGE_BACKEND
from ..voices.catalog import online_voice
from .controls import (G_BUSY, G_CHECK, G_ERROR, G_PLAY, Control, FadeLabel, NavRow, ToggleRow, draw_glyph,
                       hc_outline, paint_stop_square)
from .motion import MEDIUM, Tween, decelerate, with_alpha
from .settings_pages import EDGE, FADE, OPTICAL, PITCH, SettingsPage

ROW_H, HEADER_H, ZONE = 32, 28, 28   # DIP: a voice row, a group header, the preview button
NAME_X = 46                          # names start where the header's status does (panel coordinates)
LIST_MIN = 156                       # a header and four rows
READING = {"copying", "preparing", "reading", "waiting"}


def voice_detail(voice, default_id: str = DEFAULT_VOICE_ID) -> str:
    """ "Female · default", "Male · sentence highlight only"."""
    bits = [voice.gender] if voice.gender else []
    if voice.id == default_id:
        bits.append("default")
    if not voice.word_timing:
        bits.append("sentence highlight only")
    return " · ".join(bits)


def stored_voice(voice_id: str):
    """A chosen online voice the shown list doesn't have yet (the full list loads only on request)."""
    backend, name = parse_voice_id(voice_id)
    parts = name.split("-")
    if backend != EDGE_BACKEND or len(parts) < 3:
        return None
    return online_voice(name, "-".join(parts[:2]), "")


class VoiceList(Control):
    """The list well: group headers and voice rows. A click on a row chooses the voice; a click on its preview
    button plays (or stops) a sample. Never takes focus; the wheel scrolls it."""

    chosen = Signal(str)
    previewClicked = Signal(str)

    def __init__(self, theme, tips, parent: QWidget | None = None):
        super().__init__(theme, tips, parent, tooltip=self._tip)
        self.rows: list[tuple[str, object]] = []   # ("header", label) or ("voice", VoiceInfo)
        self.current = ""
        self.preview_id = ""
        self.preview_state = "idle"
        self.preview_enabled = True
        self.scroll = Tween(self, theme, 0.0, MEDIUM, decelerate())
        self._hover: int | None = None
        self._hover_zone = False
        self._press: tuple[int, bool] | None = None
        self.setAccessibleName("Voices")

    # --- Content

    def set_rows(self, rows: list[tuple[str, object]], current: str) -> None:
        if rows == self.rows:
            self.set_current(current)
            return
        self.rows, self.current = rows, current
        self._hover = None
        self.scroll.snap(min(self.scroll.value, self.max_scroll()))
        self.ensure_visible(animate=False)
        self._describe()
        self.update()

    def set_current(self, voice_id: str) -> None:
        if voice_id != self.current:
            self.current = voice_id
            self._describe()
            self.update()

    def set_preview(self, voice_id: str, state: str, enabled: bool) -> None:
        if (voice_id, state, enabled) != (self.preview_id, self.preview_state, self.preview_enabled):
            self.preview_id, self.preview_state, self.preview_enabled = voice_id, state, enabled
            self.tips.refresh(self)
            self._describe()
            self.update()

    def voices(self) -> list:
        return [v for kind, v in self.rows if kind == "voice"]

    def _describe(self) -> None:
        voices = self.voices()
        chosen = next((v for v in voices if v.id == self.current), None)
        text = f"{len(voices)} voices."
        if chosen is not None:
            detail = voice_detail(chosen)
            text += f" Selected: {chosen.name}" + (f", {detail}" if detail else "") + "."
        playing = next((v for v in voices if v.id == self.preview_id), None)
        if playing is not None and self.preview_state in ("loading", "playing"):
            text += f" Playing a sample of {playing.name}."
        self.setAccessibleDescription(text)

    # --- Geometry (widget coordinates; content scrolls under the well's 1 DIP border)

    def _heights(self) -> list[int]:
        px = self.theme.px
        return [px(HEADER_H) if kind == "header" else px(ROW_H) for kind, _ in self.rows]

    def _tops(self) -> list[int]:
        tops, y = [], self.theme.px(2)
        for h in self._heights():
            tops.append(y)
            y += h
        return tops

    def content_height(self) -> int:
        return self.theme.px(4) + sum(self._heights())

    def max_scroll(self) -> float:
        return float(max(0, self.content_height() - self.height()))

    def row_rect(self, i: int) -> QRectF:
        top = self._tops()[i] - self.scroll.value
        return QRectF(0, top, self.width(), self._heights()[i])

    def zone_rect(self, i: int) -> QRectF:
        px = self.theme.px
        r = self.row_rect(i)
        return QRectF(self.width() - px(4) - px(ZONE), r.center().y() - px(ZONE) / 2, px(ZONE), px(ZONE))

    def row_at(self, pos) -> int | None:
        if not QRectF(self.rect()).adjusted(1, 1, -1, -1).contains(pos):
            return None
        y = pos.y() + self.scroll.value
        for i, (top, h) in enumerate(zip(self._tops(), self._heights())):
            if top <= y < top + h:
                return i if self.rows[i][0] == "voice" else None
        return None

    def zone_shown(self, i: int) -> bool:
        voice = self.rows[i][1]
        return i == self._hover or voice.id == self.current or (
            voice.id == self.preview_id and self.preview_state != "idle")

    def ensure_visible(self, *, animate: bool = True) -> None:
        """Scroll the chosen voice into view (with a little of the row before it)."""
        index = next((i for i, (kind, v) in enumerate(self.rows) if kind == "voice" and v.id == self.current), None)
        if index is None:
            return
        tops, heights = self._tops(), self._heights()
        top = tops[index - 1] if index > 0 else tops[index]
        bottom = tops[index] + heights[index]
        target = self.scroll.target
        if top < target:
            target = top - self.theme.px(2)
        elif bottom > target + self.height() - self.theme.px(2):
            target = bottom - self.height() + self.theme.px(2)
        target = max(0.0, min(self.max_scroll(), float(target)))
        if animate:
            self.scroll.to(target)
        else:
            self.scroll.snap(target)

    def resizeEvent(self, event) -> None:
        self.scroll.snap(min(self.scroll.value, self.max_scroll()))
        self.ensure_visible(animate=False)
        super().resizeEvent(event)

    # --- Pointer

    def _tip(self) -> str:
        i = self._hover
        if i is None:
            return ""
        voice = self.rows[i][1]
        if self._hover_zone:
            if not self.preview_enabled:
                return "Pause the reading to hear a sample"
            if voice.id == self.preview_id and self.preview_state in ("loading", "playing"):
                return "Stop the sample"
            if voice.id == self.preview_id and self.preview_state == "failed":
                return "Couldn't play the sample. Click to try again."
            return f"Hear {voice.name} read a sample sentence"
        tip = f"Saylight reads with {voice.name}" if voice.id == self.current else f"Read with {voice.name}"
        if not voice.word_timing:
            tip += ". It highlights the sentence, not each word."
        return tip

    def _set_hover(self, i: int | None, zone: bool) -> None:
        if (i, zone) != (self._hover, self._hover_zone):
            self._hover, self._hover_zone = i, zone
            self.setCursor(Qt.CursorShape.PointingHandCursor if i is not None else Qt.CursorShape.ArrowCursor)
            if i is None:
                self.tips.leave(self)
            else:
                self.tips.hover(self, self._tip)
            self.update()

    def _hit(self, pos) -> tuple[int | None, bool]:
        i = self.row_at(pos)
        return i, i is not None and self.zone_shown(i) and self.zone_rect(i).contains(pos)

    def mouseMoveEvent(self, event) -> None:
        self._set_hover(*self._hit(event.position()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover(None, False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        i, zone = self._hit(event.position())
        if event.button() == Qt.MouseButton.LeftButton and i is not None:
            self._press = (i, zone)
            super().mousePressEvent(event)
            return
        event.ignore()

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._press = self._press, None
        if event.button() != Qt.MouseButton.LeftButton or pressed is None:
            event.ignore()
            return
        self._pressed = False
        self.press.to(0.0)
        event.accept()
        if self._hit(event.position()) != pressed:
            return
        voice = self.rows[pressed[0]][1]
        if pressed[1]:
            if self.preview_enabled:
                self.previewClicked.emit(voice.id)
        elif voice.id != self.current:
            self.chosen.emit(voice.id)

    def wheelEvent(self, event) -> None:
        limit = self.max_scroll()
        step = 3 * self.theme.px(ROW_H)
        target = max(0.0, min(limit, self.scroll.target - event.angleDelta().y() / 120 * step))
        if target == self.scroll.target:
            event.ignore()  # at the end: the page may scroll instead
            return
        self.scroll.to(target)
        self._set_hover(*self._hit(event.position()))
        event.accept()

    # --- Painting

    def paintEvent(self, _event) -> None:
        t = self.theme
        px = t.px
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        well = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        track = t.color("SegmentTrack")
        p.setPen(QPen(t.color("ControlStroke"), 0))
        p.setBrush(track)
        p.drawRoundedRect(well, px(4), px(4))
        inner = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(inner, px(4) - 1, px(4) - 1)
        p.setClipPath(clip)
        name_x = px(NAME_X - EDGE)
        body, small = t.font("body"), t.font("small")
        fb, fs = QFontMetricsF(body), QFontMetricsF(small)
        for i, (kind, item) in enumerate(self.rows):
            r = self.row_rect(i)
            if r.bottom() < inner.top() or r.top() > inner.bottom():
                continue
            if kind == "header":
                p.setFont(small)
                p.setPen(t.color("TextSecondary"))
                top = r.top() + px(8)
                baseline = top + (px(20) - (fs.ascent() + fs.descent())) / 2 + fs.ascent()
                room = r.width() - 2 * px(OPTICAL - EDGE)
                p.drawText(QPointF(px(OPTICAL - EDGE), baseline),
                           fs.elidedText(str(item), Qt.TextElideMode.ElideRight, room))
                continue
            self._paint_row(p, i, item, r, name_x, body, small, fb, fs)
        p.setClipping(False)
        self._paint_fades(p, inner, track)

    def _paint_row(self, p: QPainter, i: int, voice, r: QRectF, name_x: int, body, small, fb, fs) -> None:
        t = self.theme
        px = t.px
        chosen = voice.id == self.current
        hovered = i == self._hover
        plate = r.adjusted(px(2), px(1), -px(2), -px(1))
        radius = px(3)
        ink, detail_ink = t.color("TextPrimary"), t.color("TextSecondary")
        if chosen and t.high_contrast:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.color("Accent"))
            p.drawRoundedRect(plate, radius, radius)
            ink = detail_ink = t.color("OnAccent")
        elif chosen:
            p.setPen(QPen(t.color("ControlStroke"), 0))
            p.setBrush(t.color("SegmentSelected"))
            p.drawRoundedRect(plate, radius, radius)
            if not t.dark:  # the hairline that makes the white plate read as raised, as on Segmented
                p.setPen(QPen(t.color("KeycapStroke"), 0))
                y = plate.bottom() - 0.5
                p.drawLine(QPointF(plate.left() + radius, y), QPointF(plate.right() - radius, y))
        elif hovered and self.hover.value > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(with_alpha(t.color("SegmentSelected"), 0.6 * self.hover.value))
            p.drawRoundedRect(plate, radius, radius)
        if hovered and not chosen:
            hc_outline(p, t, plate, radius, self.hover.value)
        if chosen:
            g = px(12)
            check = t.color("OnAccent") if t.high_contrast else t.color("Accent")
            draw_glyph(p, QRectF(px(OPTICAL - EDGE), r.center().y() - g / 2, g, g), G_CHECK, t.font("icon12"), check)
        right = self.zone_rect(i).left() - px(8)
        p.setFont(body)
        p.setPen(ink)
        baseline = r.top() + (r.height() - (fb.ascent() + fb.descent())) / 2 + fb.ascent()
        name = fb.elidedText(voice.name, Qt.TextElideMode.ElideRight, max(0.0, right - name_x))
        p.drawText(QPointF(name_x, baseline), name)
        detail = voice_detail(voice)
        x = name_x + fb.horizontalAdvance(name) + px(8)
        if detail and x < right:
            p.setFont(small)
            p.setPen(detail_ink)
            sb = r.top() + (r.height() - (fs.ascent() + fs.descent())) / 2 + fs.ascent()
            p.drawText(QPointF(x, sb), fs.elidedText(detail, Qt.TextElideMode.ElideRight, right - x))
        if self.zone_shown(i):
            self._paint_zone(p, i, voice, ink)

    def _paint_zone(self, p: QPainter, i: int, voice, ink) -> None:
        t = self.theme
        z = self.zone_rect(i)
        radius = t.px(4)
        on_zone = i == self._hover and self._hover_zone and self.preview_enabled
        pressed = self._press == (i, True)
        if on_zone and not t.high_contrast:  # a light wash over the row's plate, deeper while pressed
            fill = with_alpha(t.color("TextPrimary"), 0.08 + 0.04 * (self.press.value if pressed else 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(z, radius, radius)
        elif on_zone and voice.id != self.current:
            hc_outline(p, t, z, radius, 1.0)
        state = self.preview_state if voice.id == self.preview_id else "idle"
        if not self.preview_enabled:  # GrayText can't be seen on the high-contrast Highlight plate
            color = ink if t.high_contrast and voice.id == self.current else t.color("TextDisabled")
        elif state == "failed":
            color = ink if t.high_contrast else t.color("Critical")
        else:
            color = ink
        if state == "playing" and self.preview_enabled:
            paint_stop_square(t)(p, z, color)
            return
        glyph = {"loading": G_BUSY, "failed": G_ERROR}.get(state, G_PLAY) if self.preview_enabled else G_PLAY
        g = t.px(12)
        draw_glyph(p, QRectF(z.center().x() - g / 2, z.center().y() - g / 2, g, g), glyph, t.font("icon12"), color,
                   t.k if glyph == G_PLAY else 0.0)

    def _paint_fades(self, p: QPainter, inner: QRectF, bg) -> None:
        fade = self.theme.px(FADE)
        if self.scroll.value > 0.5:
            g = QLinearGradient(0, inner.top(), 0, inner.top() + fade)
            g.setColorAt(0, bg)
            g.setColorAt(1, with_alpha(bg, 0.0))
            p.fillRect(QRectF(inner.left(), inner.top(), inner.width(), fade), g)
        if self.scroll.value < self.max_scroll() - 0.5:
            g = QLinearGradient(0, inner.bottom() - fade, 0, inner.bottom())
            g.setColorAt(0, with_alpha(bg, 0.0))
            g.setColorAt(1, bg)
            p.fillRect(QRectF(inner.left(), inner.bottom() - fade, inner.width(), fade), g)


def pronunciations_detail(count: int, enabled: bool) -> str:
    """The Pronunciations row's detail: "12 words", "1 word", "None yet", "12 words · off"."""
    if not count:
        return "None yet"
    words = f"{count} word{'s' if count != 1 else ''}"
    return words if enabled else f"{words} · off"


# What's read aloud: (setting, label, tooltip, shown inverted). The keys say what is read; the switches what's
# skipped, so the last two show the opposite of their setting.
READ_ALOUD_ROWS = (
    ("skip_citations", "Skip citations", "Leave out references such as (Smith et al., 2019) and [12]. They stay on "
                                         "screen, dimmed. Changes apply at once.", False),
    ("skip_urls", "Skip links and DOIs", "Say “link” instead of reading web addresses and DOIs. Changes apply at "
                                         "once.", False),
    ("read_asides", "Skip notes, captions and tables", "In files, these are shown dimmed in a Notes section after "
                                                       "the main text. Changes apply at once.", True),
    ("read_references", "Skip the reference list", "Reading stops before the References or Bibliography. It stays "
                                                   "on screen, dimmed. Changes apply at once.", True),
)
READ_ALOUD_CAPTION = "What's read aloud · dimmed text is shown but not read"
READ_ALOUD_H = 9 + 22 + 5 * 42   # DIP: the divider, the caption and five rows


class VoicePage(SettingsPage):
    """Voice: the caption that says what the list shows, the list, "Show all languages", then what's read aloud
    (four Skip switches) and Pronunciations."""

    title = "Voice"
    tooltip = "Choose the voice that reads to you, hear a sample, and choose what's read aloud"

    def __init__(self, host: QWidget, ctl) -> None:
        super().__init__(host, ctl)
        t, tips = self.theme, self.tips
        self.catalog = getattr(ctl, "catalog", None)
        self.preview = getattr(ctl, "preview", None)
        self.store = getattr(ctl, "pronunciations", None)
        self.caption = FadeLabel(t, "small", "TextSecondary", host)
        self.list = VoiceList(t, tips, host)
        self.row_all = ToggleRow(t, tips, "Show all languages", host,
                                 tooltip="Load Microsoft's full list of voices in every language. Needs internet.")
        self.lbl_read = FadeLabel(t, "small", "TextSecondary", host)
        self.lbl_read.set_text(READ_ALOUD_CAPTION, animate=False)
        self.read_rows = [(key, ToggleRow(t, tips, label, host, tooltip=tip), inverted)
                          for key, label, tip, inverted in READ_ALOUD_ROWS]
        self.row_pron = NavRow(t, tips, "Pronunciations", host, tooltip="Change how words are said")
        self.row_pron.setEnabled(hasattr(ctl, "open_pronunciations"))
        self.all_languages = False
        self.failed = False      # the full list couldn't be loaded; the English list is shown
        self.reading = False
        self.preview_id = ""
        self.list.chosen.connect(self._choose)
        self.list.previewClicked.connect(self._preview)
        self.row_all.toggled.connect(self._toggle_all)
        self.row_all.setEnabled(self.catalog is not None)
        s = self.settings
        for key, row, inverted in self.read_rows:
            row.toggled.connect(lambda on, key=key, inverted=inverted: s.set(key, (not on) if inverted else on))
        self.row_pron.clicked.connect(lambda: ctl.open_pronunciations("settings"))
        if self.catalog is not None:
            self.catalog.changed.connect(self._on_catalog)
        if self.preview is not None:
            self.preview.stateChanged.connect(lambda _state: self._sync_preview())
        if self.store is not None and hasattr(self.store, "changed"):
            self.store.changed.connect(self._sync_read_aloud)
        keys = {"voice"} | {key for key, *_ in READ_ALOUD_ROWS} | {"pronunciations.enabled"}
        s.changed.connect(lambda key: self.sync() if key in keys else None)

    def widgets(self) -> list[QWidget]:
        return [self.caption, self.list, self.row_all, self.lbl_read] + [row for _k, row, _i in self.read_rows] + [
            self.row_pron]

    def layout(self, x: int, y: int, w: int, fill: int = 0) -> int:
        px = self.theme.px
        fixed = px(22) + px(8) + px(40) + px(READ_ALOUD_H) + px(EDGE)
        list_h = max(px(LIST_MIN), fill - fixed)
        row_w = w - 2 * px(EDGE)
        self.caption.setGeometry(x + px(OPTICAL), y, w - 2 * px(OPTICAL), px(20))
        self.list.setGeometry(x + px(EDGE), y + px(22), row_w, list_h)
        y += px(22) + list_h + px(8)
        self.row_all.setGeometry(x + px(EDGE), y, row_w, px(40))
        y += px(PITCH)
        self.rules = [y + px(4)]
        y += px(9)
        self.lbl_read.setGeometry(x + px(OPTICAL), y, w - 2 * px(OPTICAL), px(20))
        y += px(22)
        for _key, row, _inverted in self.read_rows:
            row.setGeometry(x + px(EDGE), y, row_w, px(40))
            y += px(PITCH)
        self.row_pron.setGeometry(x + px(EDGE), y, row_w, px(40))
        return fixed + list_h

    def _sync_read_aloud(self) -> None:
        s = self.settings
        for key, row, inverted in self.read_rows:
            on = bool(s.get(key))
            row.set_checked((not on) if inverted else on)
        count = len(self.store) if self.store is not None else 0
        self.row_pron.set_detail(pronunciations_detail(count, bool(s.get("pronunciations.enabled"))))

    # --- State

    def current(self) -> str:
        return str(self.settings.get("voice") or "")

    def sync(self) -> None:
        self._sync_read_aloud()
        if self.catalog is None:
            return
        self.row_all.set_checked(self.all_languages)
        voices = self.catalog.voices(all_languages=self.all_languages)
        self.list.set_rows(self._group(voices), self.current())
        self._sync_caption(voices)
        self._sync_preview()

    def _group(self, voices: list) -> list[tuple[str, object]]:
        """Header rows by locale (local voices apart), in list order; the chosen voice on top if it isn't listed."""
        rows: list[tuple[str, object]] = []
        current = self.current()
        if current and all(v.id != current for v in voices):
            found = self.catalog.find(current) or stored_voice(current)
            if found is not None:
                rows += [("header", "Your voice"), ("voice", found)]
        groups: dict[tuple[bool, str], list] = {}
        for v in voices:
            groups.setdefault((v.private, v.locale), []).append(v)
        for (private, locale), members in groups.items():
            name = self.catalog.locale_name(locale)
            # the voices on this PC are Windows' own: private, but plainly less natural than the online ones
            rows.append(("header", f"{name} · on this PC · sounds more robotic" if private else name))
            rows += [("voice", v) for v in members]
        return rows

    def _sync_caption(self, voices: list) -> None:
        status = self.catalog.status
        if status == "loading" and self.all_languages:
            self.caption.set_text("Loading voices in every language…", "TextSecondary")
        elif self.failed:
            self.caption.set_text("Couldn't load other languages, so here are the English voices.", "Critical")
        elif self.all_languages and status == "online":
            languages = len({v.locale.split("-")[0] for v in voices})
            plural = "s" if languages != 1 else ""
            self.caption.set_text(f"{len(voices)} voices in {languages} language{plural}", "TextSecondary")
        else:
            self.caption.set_text(f"{len(voices)} English voices, grouped by accent", "TextSecondary")

    def _sync_preview(self) -> None:
        state = self.preview.state if self.preview is not None else "idle"
        self.list.set_preview(self.preview_id, state, self.preview is not None and not self.reading)

    def on_state(self, state: str, info: dict) -> None:
        reading = state in READING
        if reading and not self.reading:
            self.stop_preview()
        self.reading = reading
        self._sync_preview()

    def set_shown(self, on: bool) -> None:
        if on:
            self.list.ensure_visible(animate=False)
        else:
            self.stop_preview()

    def stop_preview(self) -> None:
        if self.preview is not None and self.preview.state in ("loading", "playing"):
            self.preview.stop()

    # --- Actions

    def _choose(self, voice_id: str) -> None:
        try:
            self.ctl.set_voice(voice_id)
        except ValueError:
            return

    def _preview(self, voice_id: str) -> None:
        if self.preview is None or self.reading:
            return
        if voice_id == self.preview_id and self.preview.state in ("loading", "playing"):
            self.preview.stop()
            return
        self.preview_id = voice_id
        self.preview.play(voice_id, self.ctl.speed())
        self._sync_preview()

    def _toggle_all(self, on: bool) -> None:
        self.all_languages = on
        self.failed = False
        if on and self.catalog.status != "online":
            self.catalog.refresh_online()
        self.sync()

    def _on_catalog(self) -> None:
        if self.catalog.status == "failed" and self.all_languages:
            self.all_languages = False
            self.failed = True
        self.sync()

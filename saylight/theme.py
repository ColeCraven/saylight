"""Theme: colour tokens (dark, light, high contrast), accent + contrast guard, fonts, text scale k, motion.

win32.py is imported lazily through SystemAdapter, so this module imports cleanly without it,
and tests pass a fake adapter instead.
"""

import functools
import importlib
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QCoreApplication, QEasingCurve, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication

if TYPE_CHECKING:
    from .settings import Settings

log = logging.getLogger("saylight.theme")

RGB = tuple[int, int, int]

# --- Motion --------------------------------------------------------------------

FAST_MS, NORMAL_MS, SLOW_MS = 83, 167, 250


def _bezier(x1: float, y1: float, x2: float, y2: float) -> QEasingCurve:
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1.0, 1.0))
    return curve


DECELERATE = functools.partial(_bezier, 0.0, 0.0, 0.0, 1.0)  # call for a fresh QEasingCurve
EXIT = functools.partial(_bezier, 0.7, 0.0, 1.0, 0.5)

# --- Colour --------------------------------------------------------------------

_DARK = {
    "Surface": "#2C2C2C", "TextPrimary": "#FFFFFF", "TextSecondary": "#CFCFCF", "TextDisabled": "#797979",
    "AccentDisabled": "#4D4D4D",
    "SubtleHover": "#393939", "SubtlePressed": "#343434", "SubtleSelected": "#3D3D3D",
    "ControlFill": "#383838", "ControlStroke": "#424242", "StepperHover": "#414141", "StepperPressed": "#3C3C3C",
    "Track": "#4A4A4A", "Buffered": "#676767", "ThumbOuter": "#454545", "ThumbStroke": "#424242",
    "KeycapFill": "#383838", "KeycapStroke": "#4D4D4D", "TipSurface": "#383838",
    "SegmentTrack": "#232323", "SegmentSelected": "#454545", "ProgressInactive": "#9A9A9A",
    "Caution": "#FCE100", "Critical": "#FF99A4",
}
_LIGHT = {
    "Surface": "#F9F9F9", "TextPrimary": "#1A1A1A", "TextSecondary": "#5F5F5F", "TextDisabled": "#9F9F9F",
    "AccentDisabled": "#C3C3C3",
    "SubtleHover": "#F0F0F0", "SubtlePressed": "#F3F3F3", "SubtleSelected": "#EAEAEA",
    "ControlFill": "#FFFFFF", "ControlStroke": "#E5E5E5", "StepperHover": "#F0F0F0", "StepperPressed": "#F5F5F5",
    "Track": "#DBDBDB", "Buffered": "#C2C2C2", "ThumbOuter": "#FFFFFF", "ThumbStroke": "#D1D1D1",
    "KeycapFill": "#FFFFFF", "KeycapStroke": "#D1D1D1", "TipSurface": "#FFFFFF",
    "SegmentTrack": "#F3F3F3", "SegmentSelected": "#FFFFFF", "ProgressInactive": "#7A7A7A",
    "Caution": "#9D5D00", "Critical": "#C42B1C",
}
# Derived from the accent. WordHighlightText is the current word's text colour, and the *Outline tokens
# are transparent except in high contrast, which draws 1 px outlines.
_DERIVED = ("Accent", "AccentHover", "AccentPressed", "OnAccent", "OnAccentPressed", "WordHighlight",
            "WordHighlightPaused", "WordHighlightText", "HoverOutline", "TrackOutline", "DisabledOutline")
TOKENS = tuple(_DARK) + _DERIVED

# High contrast (SPI_GETHIGHCONTRAST): every token comes from a system colour.
_HC = {
    "Surface": "window", "TextPrimary": "windowtext", "TextSecondary": "windowtext", "TextDisabled": "graytext",
    "Accent": "highlight", "AccentHover": "highlight", "AccentPressed": "highlight", "AccentDisabled": "window",
    "OnAccent": "highlighttext", "OnAccentPressed": "highlighttext",
    "SubtleHover": "window", "SubtlePressed": "window", "SubtleSelected": "window",
    "ControlFill": "window", "ControlStroke": "windowtext", "StepperHover": "window", "StepperPressed": "window",
    "Track": "window", "Buffered": "window", "ThumbOuter": "window", "ThumbStroke": "windowtext",
    "WordHighlight": "highlight", "WordHighlightPaused": "highlight", "WordHighlightText": "highlighttext",
    "KeycapFill": "window", "KeycapStroke": "windowtext", "TipSurface": "window",
    "SegmentTrack": "window", "SegmentSelected": "window", "ProgressInactive": "windowtext",
    "Caution": "windowtext", "Critical": "windowtext",
    "HoverOutline": "highlight", "TrackOutline": "windowtext", "DisabledOutline": "graytext",
}
SYS_COLOR_NAMES = ("window", "windowtext", "highlight", "highlighttext", "graytext")
_HC_DEFAULT = {"window": (0, 0, 0), "windowtext": (255, 255, 255), "highlight": (26, 235, 255),
               "highlighttext": (0, 0, 0), "graytext": (63, 242, 63)}  # "High Contrast Black"

TRAY_TOKENS = ("Foreground", "Accent", "Caution", "Critical")

# AccentPalette order: Light3, Light2, Light1, Base, Dark1, Dark2, Dark3, unused. Shades tried in order.
_DARK_SHADES = ((1, "Light2"), (0, "Light3"))
_LIGHT_SHADES = ((4, "Dark1"), (5, "Dark2"), (6, "Dark3"))
FALLBACK_ACCENT = {True: "#60CDFF", False: "#005FB8"}  # keyed by dark
HIGHLIGHT_ALPHA = {True: 0.28, False: 0.18}  # WordHighlight: Accent @ 28% (dark) / 18% (light)
PAUSED_ALPHA = {True: 0.12, False: 0.08}  # WordHighlightPaused: TextPrimary @ 12% / 8%


def relative_luminance(color: QColor) -> float:
    """WCAG 2 relative luminance of an sRGB colour (alpha ignored)."""
    def linear(channel: int) -> float:
        v = channel / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * linear(color.red()) + 0.7152 * linear(color.green()) + 0.0722 * linear(color.blue())


def contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG 2 contrast ratio, 1-21."""
    lo, hi = sorted((relative_luminance(a), relative_luminance(b)))
    return (hi + 0.05) / (lo + 0.05)


def composite(fg: QColor, alpha: float, bg: QColor) -> QColor:
    """fg at `alpha` over an opaque bg, as an opaque colour."""
    mix = (round(alpha * f + (1 - alpha) * b) for f, b in
           ((fg.red(), bg.red()), (fg.green(), bg.green()), (fg.blue(), bg.blue())))
    return QColor(*mix)


def on_accent(accent: QColor) -> QColor:
    """Black or white, whichever contrasts more with the accent."""
    black, white = QColor(0, 0, 0), QColor(255, 255, 255)
    return black if contrast_ratio(black, accent) > contrast_ratio(white, accent) else white


def accent_ok(accent: QColor, *, dark: bool) -> bool:
    """Contrast guard: Accent >= 3:1 against Surface and Track, OnAccent >= 4.5:1 against Accent."""
    base = _DARK if dark else _LIGHT
    return (contrast_ratio(accent, QColor(base["Surface"])) >= 3.0
            and contrast_ratio(accent, QColor(base["Track"])) >= 3.0
            and contrast_ratio(on_accent(accent), accent) >= 4.5)


def pick_accent(palette: tuple[RGB, ...] | None, *, dark: bool) -> tuple[str, QColor]:
    """(shade name or "fallback", colour): the first palette shade that passes the guard, stepping outward."""
    for index, name in _DARK_SHADES if dark else _LIGHT_SHADES:
        if palette is None or index >= len(palette):
            break
        color = QColor(*palette[index])
        if accent_ok(color, dark=dark):
            return name, color
    return "fallback", QColor(FALLBACK_ACCENT[dark])


def _app_tokens(dark: bool, accent: QColor) -> dict[str, QColor]:
    tokens = {name: QColor(value) for name, value in (_DARK if dark else _LIGHT).items()}
    surface, text = tokens["Surface"], tokens["TextPrimary"]
    on, pressed = on_accent(accent), composite(accent, 0.8, surface)
    clear = QColor(Qt.GlobalColor.transparent)
    tokens.update(
        Accent=QColor(accent), AccentHover=composite(accent, 0.9, surface), AccentPressed=pressed,
        OnAccent=on, OnAccentPressed=composite(on, 0.7, pressed),
        WordHighlight=composite(accent, HIGHLIGHT_ALPHA[dark], surface),
        WordHighlightPaused=composite(text, PAUSED_ALPHA[dark], surface), WordHighlightText=QColor(text),
        HoverOutline=clear, TrackOutline=QColor(clear), DisabledOutline=QColor(clear))
    return tokens


# --- Reading presets ---------------------------------------------------------------
#
# READING_PRESETS[tint][mode] with mode "dark" or "light" maps any of the READING_ROLES keys to a colour
# string QColor accepts ("#RRGGBB", or "#AARRGGBB" for a highlight blended over the background).
# A missing key falls back to the theme: background Surface, text TextPrimary, secondary TextSecondary,
# highlight Accent blended over the background, highlight_paused text blended over the background,
# highlight_text the text colour, edge (the card hairline) text blended over the background, keycap_fill and
# keycap_stroke the KeycapFill and KeycapStroke tokens, keycap_text the text colour, skipped (text shown but not
# read aloud) the secondary colour.
# Keep the tint names in step with settings.READING_TINTS.
# High contrast ignores the tint. tests/test_theme.py checks every preset for WCAG contrast.
#
# The tints follow dyslexia readability research. Body text sits at 11-15:1 (never black on white),
# earlier sentences at ~5:1, and the word pill takes the hue opposite the background (blue on warm, amber on
# blue) because blue/yellow differences survive the common colour-vision deficiencies. Skipped text (citations,
# notes and references that are shown but not read aloud) sits at the lightness of the earlier sentences with
# the opposite cast, cool slate on the warm tints and warm taupe on soft blue, so it never looks already read;
# it stays above 4.5:1 on the band too, since a skipped citation can sit inside the current sentence.
_CREAM = {"background": "#FAF5E9", "text": "#24211C", "secondary": "#6B655B", "band": "#F2E7CC",
          "highlight": "#A9C8F2", "highlight_text": "#24211C", "highlight_paused": "#C3D3E5",
          "keycap_fill": "#FFFFFF", "keycap_stroke": "#D3CFC4", "skipped": "#56637A"}
_PEACH = {"background": "#EDD1B0", "text": "#24211C", "secondary": "#5E554A", "band": "#E6C7A2",
          "highlight": "#98BBE8", "highlight_text": "#24211C", "highlight_paused": "#B3BFD0",
          "keycap_fill": "#FFFFFF", "keycap_stroke": "#C9B195", "skipped": "#455366"}
_SOFT_BLUE = {"background": "#E3ECF6", "text": "#1C2430", "secondary": "#556070", "band": "#D4E1F0",
              "highlight": "#E9B949", "highlight_text": "#1C2430", "highlight_paused": "#E2C783",
              "keycap_fill": "#FFFFFF", "keycap_stroke": "#BFC8D2", "skipped": "#6E5A44"}
_SOFT_DARK = {"background": "#1F2023", "text": "#E6E1D6", "secondary": "#9C978D", "band": "#2A2C31",
              "highlight": "#36527F", "highlight_text": "#FFFFFF", "highlight_paused": "#324564",
              "keycap_fill": "#2F2F31", "keycap_stroke": "#474747", "skipped": "#8C9AAE"}
READING_PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "auto": {"light": _CREAM, "dark": _SOFT_DARK},
    "cream": {"light": _CREAM, "dark": _CREAM},
    "peach": {"light": _PEACH, "dark": _PEACH},
    "blue": {"light": _SOFT_BLUE, "dark": _SOFT_BLUE},
    "dark": {"light": _SOFT_DARK, "dark": _SOFT_DARK},
}
TINT_NAMES = {"auto": "Automatic: cream in light mode, soft dark in dark mode", "cream": "Cream",
              "peach": "Peach", "blue": "Soft blue", "dark": "Soft dark"}
_HC_READING = {"background": "Surface", "text": "TextPrimary", "secondary": "TextSecondary",
               "band": "Surface", "highlight": "WordHighlight", "highlight_paused": "WordHighlightPaused",
               "highlight_text": "WordHighlightText", "edge": "ControlStroke",
               "keycap_fill": "KeycapFill", "keycap_stroke": "KeycapStroke", "keycap_text": "TextPrimary",
               "skipped": "TextDisabled"}
READING_ROLES = tuple(_HC_READING)

# --- Fonts ---------------------------------------------------------------------


@dataclass(frozen=True)
class FontRole:
    families: tuple[str, ...]  # preferred first; the first installed family wins, else the last
    px: float  # DIP at k = 1
    line: float  # line height in DIP at k = 1
    semibold: bool = False
    tnum: bool = False


_TEXT = ("Segoe UI Variable Text", "Segoe UI")
_TEXT_SEMIBOLD = ("Segoe UI Variable Text Semibold", "Segoe UI")
_SMALL = ("Segoe UI Variable Small", "Segoe UI")
_SMALL_SEMIBOLD = ("Segoe UI Variable Small Semibold", "Segoe UI")
_ICONS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")

FONT_ROLES = {
    "status": FontRole(_TEXT_SEMIBOLD, 14, 20, semibold=True),
    "body": FontRole(_TEXT, 14, 20),
    "small": FontRole(_SMALL, 12, 16, tnum=True),
    "value": FontRole(_TEXT_SEMIBOLD, 14, 20, semibold=True, tnum=True),
    "keycap": FontRole(_SMALL_SEMIBOLD, 12, 18, semibold=True),
    "keycap_tick": FontRole(_TEXT_SEMIBOLD, 16, 18, semibold=True),
    "message": FontRole(_TEXT, 16, 24),
    "icon16": FontRole(_ICONS, 16, 16),
    "icon12": FontRole(_ICONS, 12, 12),
}
# Extra role "reading": the follow-along text. Verdana by default (large x-height, wide default spacing,
# I/l/1 and O/0 distinct); other families are scaled to Verdana's x-height so switching font keeps the
# visual size. Sizes are px at k = 1.
READING_TEXT = {"small": 15, "medium": 18, "large": 20, "xlarge": 23}
READING_FONT_FAMILIES = {"verdana": ("Verdana",), "segoe": ("Segoe UI Variable Text", "Segoe UI"),
                         "calibri": ("Calibri",), "sitka": ("Sitka Small", "Sitka Text", "Sitka")}
# (letter spacing em, word spacing em, line-height multiple). Letter and word spacing rise together
# (Galliussi 2020; BDA: word gap >= 3.5x the letter gap). "wide" is the default; it is a touch narrower than
# the recommended +0.05/+0.20 em because Verdana's own spacing is already wide and that read as typewriter-like.
READING_SPACING = {"standard": (0.0, 0.06, 1.4), "wide": (0.035, 0.13, 1.5), "extra": (0.12, 0.45, 1.8)}
_XHEIGHT_CACHE: dict[str, float] = {}


def x_height_ratio(family: str) -> float:
    """x-height / pixel size for a family (cached; needs a QGuiApplication)."""
    if family not in _XHEIGHT_CACHE:
        from PySide6.QtGui import QFontMetricsF
        font = QFont(family)
        font.setPixelSize(100)
        _XHEIGHT_CACHE[family] = max(0.3, QFontMetricsF(font).xHeight() / 100)
    return _XHEIGHT_CACHE[family]

K_MIN, K_MAX = 1.0, 1.5  # k = clamp(TextScaleFactor / 100, 1.0, 1.5)


def clamp_k(factor: Any) -> float:
    try:
        k = float(factor)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an int too large for a float
        return K_MIN
    return min(K_MAX, max(K_MIN, k)) if math.isfinite(k) else K_MIN


# --- System adapter ----------------------------------------------------------------

class SystemAdapter:
    """Reads the OS state Theme needs: Qt's colour scheme, plus win32.py imported on first use.

    If win32.py is missing or a call fails, it logs once per function and returns a safe default,
    so the theme always builds."""

    _DEFAULTS = {"apps_use_light_theme": False, "system_uses_light_theme": False, "high_contrast": False,
                 "animations_enabled": True, "text_scale_factor": 1.0, "accent_palette": None,
                 "color_prevalence": False}

    def __init__(self) -> None:
        self._failed: set[str] = set()

    def _win32(self) -> Any:
        return importlib.import_module(".win32", __package__)

    def _call(self, name: str, *args: Any) -> Any:
        try:
            return getattr(self._win32(), name)(*args)
        except Exception as e:
            self._warn_once(name, e)
            return self._DEFAULTS[name]

    def _warn_once(self, name: str, error: Exception) -> None:
        what = "win32.py" if isinstance(error, ImportError) else f"win32.{name}"
        if what not in self._failed:
            self._failed.add(what)
            log.warning("%s unavailable (%s); using defaults", what, type(error).__name__)

    def color_scheme_dark(self) -> bool | None:
        """True or False from QStyleHints.colorScheme(), or None when Qt doesn't know."""
        if not isinstance(QCoreApplication.instance(), QGuiApplication):
            return None
        scheme = QGuiApplication.styleHints().colorScheme()
        return {Qt.ColorScheme.Dark: True, Qt.ColorScheme.Light: False}.get(scheme)

    def apps_use_light_theme(self) -> bool:
        return bool(self._call("apps_use_light_theme"))

    def system_uses_light_theme(self) -> bool:
        return bool(self._call("system_uses_light_theme"))

    def high_contrast(self) -> bool:
        return bool(self._call("high_contrast"))

    def animations_enabled(self) -> bool:
        return bool(self._call("animations_enabled"))

    def text_scale_factor(self) -> float:
        return self._call("text_scale_factor")

    def accent_palette(self) -> list[RGB] | None:
        return self._call("accent_palette")

    def color_prevalence(self) -> bool:
        return bool(self._call("color_prevalence"))

    def sys_colors(self) -> dict[str, RGB]:
        """The five high-contrast system colours by SYS_COLOR_NAMES name."""
        try:
            w = self._win32()
            return {name: tuple(w.sys_color_rgb(getattr(w, "COLOR_" + name.upper()))) for name in SYS_COLOR_NAMES}
        except Exception as e:
            self._warn_once("sys_color_rgb", e)
            return dict(_HC_DEFAULT)

    def font_families(self) -> frozenset[str]:
        if not isinstance(QCoreApplication.instance(), QGuiApplication):
            return frozenset()
        return frozenset(QFontDatabase.families())


def _rgb(value: Any) -> RGB | None:
    try:
        r, g, b = (int(c) for c in tuple(value)[:3])
    except (TypeError, ValueError):
        return None
    return (r, g, b) if all(0 <= c <= 255 for c in (r, g, b)) else None


def _palette(value: Any) -> tuple[RGB, ...] | None:
    """A well-formed AccentPalette (at least 7 RGB entries), or None."""
    try:
        shades = tuple(_rgb(entry) for entry in value)
    except TypeError:
        return None
    return shades if len(shades) >= 7 and None not in shades[:7] else None


@dataclass(frozen=True)
class _State:
    dark: bool
    taskbar_dark: bool
    high_contrast: bool
    reduced_motion: bool
    k: float
    palette: tuple[RGB, ...] | None
    prevalence: bool
    sys_colors: tuple[tuple[str, RGB], ...]
    families: frozenset[str]


class Theme(QObject):
    """Colours, fonts and metrics for the panel, HUD, tooltip and tray. GUI thread only.

    Call refresh() on WM_SETTINGCHANGE, colorSchemeChanged and similar; `changed` fires only when
    something differs, and also when the text_size or reading_tint setting changes."""

    changed = Signal()

    def __init__(self, settings: "Settings", parent: QObject | None = None, *,
                 system: SystemAdapter | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._system = system if system is not None else SystemAdapter()
        self._fonts: dict[str, QFont] = {}
        self._state = self._read_state()
        self._build()
        settings.changed.connect(self._on_setting)

    # --- Properties

    @property
    def dark(self) -> bool:
        return self._state.dark

    @property
    def taskbar_dark(self) -> bool:
        return self._state.taskbar_dark

    @property
    def high_contrast(self) -> bool:
        return self._state.high_contrast

    @property
    def reduced_motion(self) -> bool:
        return self._state.reduced_motion

    @property
    def k(self) -> float:
        return self._state.k

    @property
    def accent_source(self) -> str:
        """Which shade the guard chose: "Light2", "Light3", "Dark1".."Dark3", "fallback" or "high_contrast"."""
        return self._accent_source

    # --- Colours

    def color(self, token: str) -> QColor:
        """A copy of an app-theme token (see TOKENS). KeyError for unknown names."""
        return QColor(self._tokens[token])

    def tray_color(self, token: str) -> QColor:
        """A taskbar-theme colour: "Foreground", "Accent", "Caution" or "Critical"."""
        return QColor(self._tray[token])

    def reading_colors(self) -> dict[str, QColor]:
        """The sentence view's colours (READING_ROLES keys) for the current reading_tint."""
        if self._state.high_contrast:
            return {role: self.color(token) for role, token in _HC_READING.items()}
        dark = self._state.dark
        tint = READING_PRESETS.get(self._settings.get("reading_tint"), READING_PRESETS["auto"])
        preset = tint["dark" if dark else "light"]

        def given(role: str) -> QColor | None:
            if not preset.get(role):
                return None
            color = QColor(preset[role])
            return composite(color, color.alphaF(), background) if color.alpha() < 255 else color

        background = QColor(preset["background"]) if preset.get("background") else self.color("Surface")
        text = given("text") or self.color("TextPrimary")
        return {"background": background, "text": text,
                "secondary": given("secondary") or self.color("TextSecondary"),
                "band": given("band") or composite(text, 0.06, background),
                "highlight": given("highlight") or composite(self.color("Accent"), HIGHLIGHT_ALPHA[dark], background),
                "highlight_paused": given("highlight_paused") or composite(text, PAUSED_ALPHA[dark], background),
                "highlight_text": given("highlight_text") or QColor(text),
                "edge": given("edge") or composite(text, 0.10 if background.lightness() > 128 else 0.06, background),
                "keycap_fill": given("keycap_fill") or self.color("KeycapFill"),
                "keycap_stroke": given("keycap_stroke") or self.color("KeycapStroke"),
                "keycap_text": given("keycap_text") or QColor(text),
                "skipped": given("skipped") or given("secondary") or self.color("TextSecondary")}

    # --- Fonts and metrics

    def font(self, role: str) -> QFont:
        """A copy of the font for a role in FONT_ROLES or "reading". KeyError for unknown roles."""
        font = self._fonts.get(role)
        if font is None:
            font = self._fonts[role] = (self._make_reading_font() if role == "reading"
                                        else self._make_font(self._role(role)))
        return QFont(font)

    def line_height(self, role: str) -> int:
        """The role's line height in DIP x k."""
        return self.px(self._role(role).line)

    def px(self, v: float) -> int:
        """v x k, rounded half up."""
        return math.floor(v * self._state.k + 0.5)

    def ms(self, duration: int) -> int:
        """The duration, or 0 under reduced motion."""
        return 0 if self._state.reduced_motion else int(duration)

    # --- Refresh

    def refresh(self) -> bool:
        """Re-read the system state; emit `changed` and return True if anything differs."""
        state = self._read_state()
        if state == self._state:
            return False
        self._state = state
        self._build()
        self.changed.emit()
        return True

    # --- Internals

    def _read_state(self) -> _State:
        s = self._system
        dark = s.color_scheme_dark()
        if dark is None:
            dark = not s.apps_use_light_theme()
        high_contrast = s.high_contrast()
        colors = s.sys_colors() if high_contrast else _HC_DEFAULT
        return _State(
            dark=bool(dark), taskbar_dark=not s.system_uses_light_theme(), high_contrast=high_contrast,
            reduced_motion=not s.animations_enabled(), k=clamp_k(s.text_scale_factor()),
            palette=_palette(s.accent_palette()), prevalence=s.color_prevalence(),
            sys_colors=tuple((n, _rgb(colors.get(n)) or _HC_DEFAULT[n]) for n in SYS_COLOR_NAMES),
            families=s.font_families())

    def _build(self) -> None:
        st = self._state
        self._fonts.clear()
        if st.high_contrast:
            sys_colors = {name: QColor(*rgb) for name, rgb in st.sys_colors}
            self._tokens = {token: QColor(sys_colors[name]) for token, name in _HC.items()}
            self._tray = {token: QColor(sys_colors["windowtext"]) for token in TRAY_TOKENS}  # monochrome
            self._accent_source = "high_contrast"
        else:
            self._accent_source, accent = pick_accent(st.palette, dark=st.dark)
            self._tokens = _app_tokens(st.dark, accent)
            base = _DARK if st.taskbar_dark else _LIGHT
            foreground = QColor("#FFFFFF" if st.taskbar_dark else "#1A1A1A")
            # Windows only colours the taskbar with the accent in dark mode; ColorPrevalence can stay 1 after
            # the user switches to light, so ignore it there.
            accent_bar = st.prevalence and st.taskbar_dark
            tray_accent = QColor(foreground) if accent_bar else pick_accent(st.palette, dark=st.taskbar_dark)[1]
            self._tray = {"Foreground": foreground, "Accent": tray_accent,
                          "Caution": QColor(base["Caution"]), "Critical": QColor(base["Critical"])}
        log.info("Theme: dark=%s taskbar_dark=%s high_contrast=%s reduced_motion=%s k=%.2f accent=%s",
                 st.dark, st.taskbar_dark, st.high_contrast, st.reduced_motion, st.k, self._accent_source)

    def _role(self, role: str) -> FontRole:
        if role == "reading":
            size = READING_TEXT.get(self._settings.get("text_size"), READING_TEXT["medium"])
            line = READING_SPACING.get(self._settings.get("spacing"), READING_SPACING["wide"])[2]
            families = READING_FONT_FAMILIES.get(self._settings.get("reading_font"), ("Verdana",)) + _TEXT
            family = next((n for n in families if n in self._state.families), families[-1])
            scaled = size * x_height_ratio("Verdana") / x_height_ratio(family)  # the size the family is drawn at
            return FontRole(families, size, round(scaled * line))
        return FONT_ROLES[role]

    def _family(self, role: FontRole) -> str:
        return next((name for name in role.families if name in self._state.families), role.families[-1])

    def _make_reading_font(self) -> QFont:
        role = self._role("reading")
        family = self._family(role)
        size = self.px(role.px) * x_height_ratio("Verdana") / x_height_ratio(family)
        font = QFont(family)
        font.setPixelSize(max(1, round(size)))
        letter, word, _ = READING_SPACING.get(self._settings.get("spacing"), READING_SPACING["wide"])
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter * size)
        font.setWordSpacing(word * size)
        font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
        return font

    def _make_font(self, role: FontRole) -> QFont:
        family = self._family(role)
        font = QFont(family)
        font.setPixelSize(max(1, self.px(role.px)))
        if role.semibold:
            # Safe on the named "... Semibold" families (same face, exact match). Never on "Segoe UI Variable
            # Text" itself: Qt then picks the Bold face.
            font.setWeight(QFont.Weight.DemiBold)
        if role.tnum:
            font.setFeature(QFont.Tag("tnum"), 1)
        return font

    def _on_setting(self, key: str) -> None:
        if key in ("text_size", "reading_tint", "reading_font", "spacing"):
            self._fonts.pop("reading", None)
            self.changed.emit()

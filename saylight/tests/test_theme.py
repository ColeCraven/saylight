"""Tests for theme.py: contrast guard, colour tokens, high contrast, tray colours, k, motion, fonts, presets, and
the colours of the screen-selection overlay, the marks drawn on the screen and the reading ruler.

A fake SystemAdapter stands in for win32.py. Font tests need the real `windows` platform and skip otherwise.
"""

import itertools
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEasingCurve, QObject, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontInfo, QFontMetricsF, QGuiApplication, QRawFont
from PySide6.QtWidgets import QApplication

from saylight import settings as S
from saylight import theme as T
from saylight.theme import Theme, composite, contrast_ratio

_app = None

MACHINE_PALETTE = [(0xF0, 0xC0, 0xF4), (0xDB, 0x9E, 0xE5), (0xB7, 0x63, 0xCB), (0xA9, 0x4D, 0xC1),
                   (0x8E, 0x3A, 0xA7), (0x69, 0x27, 0x82), (0x40, 0x0E, 0x59), (0x88, 0x17, 0x98)]
HC_COLORS = {"window": (1, 2, 3), "windowtext": (250, 251, 252), "highlight": (26, 235, 255),
             "highlighttext": (4, 5, 6), "graytext": (63, 242, 63)}


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


def hexes(color: QColor) -> str:
    return color.name().upper()


class FakeSettings(QObject):
    changed = Signal(str)

    def __init__(self, **values: str) -> None:
        super().__init__()
        self.values = {"text_size": "medium", "reading_tint": "auto", "reading_font": "verdana", "spacing": "wide",
                       "ruler.dim": "medium", **values}

    def get(self, key: str) -> str:
        return self.values[key]

    def set(self, key: str, value: str) -> None:
        self.values[key] = value
        self.changed.emit(key)


class FakeSystem:
    """Stands in for SystemAdapter. families=None means the real font database."""

    def __init__(self, **overrides: object) -> None:
        self.scheme_dark: bool | None = True
        self.apps_light = False
        self.system_light = False
        self.hc = False
        self.animations = True
        self.scale: object = 1.0
        self.palette: object = MACHINE_PALETTE
        self.prevalence = False
        self.colors = dict(HC_COLORS)
        self.families: object = None
        for name, value in overrides.items():
            setattr(self, name, value)

    def color_scheme_dark(self) -> bool | None:
        return self.scheme_dark

    def apps_use_light_theme(self) -> bool:
        return self.apps_light

    def system_uses_light_theme(self) -> bool:
        return self.system_light

    def high_contrast(self) -> bool:
        return self.hc

    def animations_enabled(self) -> bool:
        return self.animations

    def text_scale_factor(self) -> object:
        return self.scale

    def accent_palette(self) -> object:
        return self.palette

    def color_prevalence(self) -> bool:
        return self.prevalence

    def sys_colors(self) -> dict:
        return dict(self.colors)

    def font_families(self) -> frozenset[str]:
        if self.families is None:
            return frozenset(QFontDatabase.families()) if isinstance(_app, QGuiApplication) else frozenset()
        return frozenset(self.families)


def make(settings: QObject | None = None, **overrides: object) -> Theme:
    overrides.setdefault("families", ())
    return Theme(settings or FakeSettings(), system=FakeSystem(**overrides))


def same(palette_color: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    return [palette_color] * 8


class TestContrastMath(unittest.TestCase):
    def test_wcag_values(self) -> None:
        black, white = QColor("#000000"), QColor("#FFFFFF")
        self.assertAlmostEqual(T.relative_luminance(white), 1.0)
        self.assertAlmostEqual(T.relative_luminance(black), 0.0)
        self.assertAlmostEqual(contrast_ratio(black, white), 21.0)
        self.assertAlmostEqual(contrast_ratio(white, black), 21.0)
        self.assertAlmostEqual(contrast_ratio(QColor("#767676"), white), 4.54, places=2)  # WCAG reference
        self.assertAlmostEqual(contrast_ratio(QColor("#123456"), QColor("#123456")), 1.0)

    def test_composite_and_on_accent(self) -> None:
        self.assertEqual(hexes(composite(QColor("#FFFFFF"), 0.5, QColor("#000000"))), "#808080")
        self.assertEqual(hexes(composite(QColor("#FF0000"), 1.0, QColor("#00FF00"))), "#FF0000")
        self.assertEqual(hexes(T.on_accent(QColor("#DB9EE5"))), "#000000")
        self.assertEqual(hexes(T.on_accent(QColor("#8E3AA7"))), "#FFFFFF")


class TestAccentGuard(unittest.TestCase):
    def test_this_machines_accent(self) -> None:
        dark, light = make(), make(scheme_dark=False)
        self.assertEqual((dark.accent_source, hexes(dark.color("Accent"))), ("Light2", "#DB9EE5"))
        self.assertEqual(hexes(dark.color("OnAccent")), "#000000")
        self.assertEqual((light.accent_source, hexes(light.color("Accent"))), ("Dark1", "#8E3AA7"))
        self.assertEqual(hexes(light.color("OnAccent")), "#FFFFFF")

    def test_dark_steps_from_light2_to_light3(self) -> None:
        palette = same((0x80, 0x80, 0x80))
        palette[1] = (0x3A, 0x3A, 0x3A)  # Light2 is too dark on #2C2C2C
        palette[0] = (0xC0, 0xC0, 0xFF)
        theme = make(palette=palette)
        self.assertEqual((theme.accent_source, hexes(theme.color("Accent"))), ("Light3", "#C0C0FF"))

    def test_light_steps_outward_through_dark2_and_dark3(self) -> None:
        palette = same((0xD0, 0xD0, 0xD0))
        palette[5] = (0x30, 0x30, 0x80)
        self.assertEqual(make(scheme_dark=False, palette=palette).accent_source, "Dark2")
        palette[5] = (0xB0, 0xB0, 0xB0)  # still too light
        palette[6] = (0x30, 0x30, 0x80)
        theme = make(scheme_dark=False, palette=palette)
        self.assertEqual((theme.accent_source, hexes(theme.color("Accent"))), ("Dark3", "#303080"))

    def test_very_light_accent(self) -> None:
        self.assertEqual(make(palette=same((0xFA, 0xFA, 0xFA))).accent_source, "Light2")
        theme = make(scheme_dark=False, palette=same((0xFA, 0xFA, 0xFA)))
        self.assertEqual((theme.accent_source, hexes(theme.color("Accent"))), ("fallback", "#005FB8"))

    def test_very_dark_accent(self) -> None:
        theme = make(palette=same((5, 5, 5)))
        self.assertEqual((theme.accent_source, hexes(theme.color("Accent"))), ("fallback", "#60CDFF"))
        self.assertEqual(make(scheme_dark=False, palette=same((5, 5, 5))).accent_source, "Dark1")

    def test_missing_or_malformed_palette_falls_back(self) -> None:
        for palette in (None, [], MACHINE_PALETTE[:5], [None] * 8, ["#DB9EE5"] * 8, b"\x00" * 32,
                        [(300, 0, 0)] * 8, 42):
            with self.subTest(palette=palette):
                self.assertEqual(make(palette=palette).accent_source, "fallback")
                self.assertEqual(hexes(make(scheme_dark=False, palette=palette).color("Accent")), "#005FB8")

    def test_fallbacks_pass_the_guard(self) -> None:
        for dark in (True, False):
            self.assertTrue(T.accent_ok(QColor(T.FALLBACK_ACCENT[dark]), dark=dark))

    def test_every_accent_the_guard_accepts_is_readable(self) -> None:
        levels = (0, 51, 102, 153, 204, 255)
        for rgb in itertools.product(levels, repeat=3):
            for dark in (True, False):
                theme = make(scheme_dark=dark, palette=same(rgb))
                accent, surface, track = (theme.color(n) for n in ("Accent", "Surface", "Track"))
                with self.subTest(rgb=rgb, dark=dark):
                    self.assertGreaterEqual(contrast_ratio(accent, surface), 3.0)
                    self.assertGreaterEqual(contrast_ratio(accent, track), 3.0)
                    self.assertGreaterEqual(contrast_ratio(theme.color("OnAccent"), accent), 4.5)
                    self.assertGreaterEqual(
                        contrast_ratio(theme.color("WordHighlightText"), theme.color("WordHighlight")), 4.5)


class TestTokens(unittest.TestCase):
    DARK = {"Surface": "#2C2C2C", "TextPrimary": "#FFFFFF", "TextSecondary": "#CFCFCF",
            "TextDisabled": "#797979", "AccentDisabled": "#4D4D4D", "SubtleHover": "#393939",
            "SubtlePressed": "#343434", "SubtleSelected": "#3D3D3D", "ControlFill": "#383838",
            "ControlStroke": "#424242", "StepperHover": "#414141", "StepperPressed": "#3C3C3C",
            "Track": "#4A4A4A", "Buffered": "#676767", "ThumbOuter": "#454545", "ThumbStroke": "#424242",
            "KeycapFill": "#383838", "KeycapStroke": "#4D4D4D", "TipSurface": "#383838", "Caution": "#FCE100",
            "Critical": "#FF99A4", "SegmentTrack": "#232323", "SegmentSelected": "#454545",
            "ProgressInactive": "#9A9A9A"}
    LIGHT = {"Surface": "#F9F9F9", "TextPrimary": "#1A1A1A", "TextSecondary": "#5F5F5F",
             "TextDisabled": "#9F9F9F", "AccentDisabled": "#C3C3C3", "SubtleHover": "#F0F0F0",
             "SubtlePressed": "#F3F3F3", "SubtleSelected": "#EAEAEA", "ControlFill": "#FFFFFF",
             "ControlStroke": "#E5E5E5", "StepperHover": "#F0F0F0", "StepperPressed": "#F5F5F5",
             "Track": "#DBDBDB", "Buffered": "#C2C2C2", "ThumbOuter": "#FFFFFF", "ThumbStroke": "#D1D1D1",
             "KeycapFill": "#FFFFFF", "KeycapStroke": "#D1D1D1", "TipSurface": "#FFFFFF", "Caution": "#9D5D00",
             "Critical": "#C42B1C", "SegmentTrack": "#F3F3F3", "SegmentSelected": "#FFFFFF",
             "ProgressInactive": "#7A7A7A"}

    def test_static_token_values(self) -> None:
        for dark, table in ((True, self.DARK), (False, self.LIGHT)):
            theme = make(scheme_dark=dark)
            for token, value in table.items():
                self.assertEqual(hexes(theme.color(token)), value, (dark, token))

    def test_every_token_in_every_mode(self) -> None:
        self.assertEqual(set(T._HC), set(T.TOKENS))
        clear = {"HoverOutline", "TrackOutline", "DisabledOutline", "ModeFrame"}
        see_through = {"Scrim": 0x66, "SelectionHalo": 0x80}  # the selection overlay's scrim and ring halo
        lit = {True: {"SurfaceSheen": 0x0F, "SurfaceEdge": 0x1F},   # the panel's light from above
               False: {"SurfaceSheen": 0xB3, "SurfaceEdge": 0xFF}}
        for overrides in ({}, {"scheme_dark": False}, {"hc": True}):
            hc = bool(overrides.get("hc"))
            theme = make(**overrides)
            for token in T.TOKENS:
                color = theme.color(token)
                self.assertTrue(color.isValid(), (overrides, token))
                if hc:
                    alpha = 0 if token in ("Scrim", "SurfaceSheen", "SurfaceEdge") else 255
                else:
                    alpha = 0 if token in clear else {**see_through, **lit[theme.dark]}.get(token, 255)
                self.assertEqual(color.alpha(), alpha, (overrides, token))

    def test_derived_tokens(self) -> None:
        for dark, hl, paused in ((True, 0.28, 0.12), (False, 0.18, 0.08)):
            theme = make(scheme_dark=dark)
            accent, surface = theme.color("Accent"), theme.color("Surface")
            pressed = composite(accent, 0.8, surface)
            self.assertEqual(theme.color("AccentHover"), composite(accent, 0.9, surface))
            self.assertEqual(theme.color("AccentPressed"), pressed)
            self.assertEqual(theme.color("OnAccentPressed"), composite(theme.color("OnAccent"), 0.7, pressed))
            self.assertEqual(theme.color("WordHighlight"), composite(accent, hl, surface))
            self.assertEqual(theme.color("WordHighlightPaused"),
                             composite(theme.color("TextPrimary"), paused, surface))
            self.assertEqual(theme.color("WordHighlightText"), theme.color("TextPrimary"))

    def test_text_contrast(self) -> None:
        for dark in (True, False):
            theme = make(scheme_dark=dark)
            for token in ("TextPrimary", "TextSecondary", "Critical", "Caution"):
                self.assertGreaterEqual(contrast_ratio(theme.color(token), theme.color("Surface")), 4.5,
                                        (dark, token))

    def test_colors_are_copies_and_unknown_tokens_raise(self) -> None:
        theme = make()
        theme.color("Accent").setRed(0)
        self.assertEqual(hexes(theme.color("Accent")), "#DB9EE5")
        with self.assertRaises(KeyError):
            theme.color("Purple")
        with self.assertRaises(KeyError):
            theme.tray_color("Surface")


class TestHighContrast(unittest.TestCase):
    def test_mapping(self) -> None:
        theme = make(hc=True, scheme_dark=False)
        expect = {"Surface": "window", "TextPrimary": "windowtext", "TextSecondary": "windowtext",
                  "TextDisabled": "graytext", "Accent": "highlight", "OnAccent": "highlighttext",
                  "Caution": "windowtext", "Critical": "windowtext", "Track": "window",
                  "SubtleHover": "window", "HoverOutline": "highlight", "TrackOutline": "windowtext",
                  "WordHighlight": "highlight", "WordHighlightText": "highlighttext", "Buffered": "window",
                  "ProgressInactive": "windowtext", "SegmentTrack": "window", "SegmentSelected": "window",
                  "SelectionEdge": "highlight", "SelectionHalo": "window", "ModeFrame": "highlight"}
        for token, name in expect.items():
            self.assertEqual(theme.color(token), QColor(*HC_COLORS[name]), token)
        self.assertTrue(theme.high_contrast)
        self.assertEqual(theme.accent_source, "high_contrast")

    def test_tray_is_monochrome(self) -> None:
        theme = make(hc=True, prevalence=True)
        for token in T.TRAY_TOKENS:
            self.assertEqual(theme.tray_color(token), QColor(*HC_COLORS["windowtext"]), token)

    def test_reading_colors_ignore_the_tint(self) -> None:
        presets = {"auto": T.READING_PRESETS["auto"], "cream": {"dark": {"background": "#FAF5E9"},
                                                                "light": {"background": "#FAF5E9"}}}
        with mock.patch.dict(T.READING_PRESETS, presets):
            colors = make(FakeSettings(reading_tint="cream"), hc=True).reading_colors()
        self.assertEqual(colors["background"], QColor(*HC_COLORS["window"]))
        self.assertEqual(colors["text"], QColor(*HC_COLORS["windowtext"]))
        self.assertEqual(colors["highlight"], QColor(*HC_COLORS["highlight"]))
        self.assertEqual(colors["highlight_text"], QColor(*HC_COLORS["highlighttext"]))
        self.assertEqual(colors["edge"], QColor(*HC_COLORS["windowtext"]))  # the card frame
        self.assertEqual((colors["keycap_fill"], colors["keycap_stroke"], colors["keycap_text"]),
                         (QColor(*HC_COLORS["window"]), QColor(*HC_COLORS["windowtext"]),
                          QColor(*HC_COLORS["windowtext"])))
        self.assertEqual(colors["skipped"], QColor(*HC_COLORS["graytext"]))  # text shown but not read aloud

    def test_bad_system_colours_use_defaults(self) -> None:
        theme = make(hc=True, colors={"window": None, "windowtext": (1, 2), "highlight": "x"})
        self.assertEqual(theme.color("Surface"), QColor(*T._HC_DEFAULT["window"]))
        self.assertEqual(theme.color("Accent"), QColor(*T._HC_DEFAULT["highlight"]))
        self.assertEqual(theme.color("TextDisabled"), QColor(*T._HC_DEFAULT["graytext"]))


class TestScreenColors(unittest.TestCase):
    """The selection overlay's tokens, the marks drawn on the screen, and the reading ruler."""

    # (page, ink, light page): the reference pages the on-screen marks are checked against.
    PAGES = (("#FFFFFF", "#000000", True), ("#FFFFFF", "#1A1A1A", True), ("#FAF5E9", "#24211C", True),
             ("#1E1E1E", "#E6E6E6", False), ("#000000", "#FFFFFF", False), ("#1E1E1E", "#CCCCCC", False))

    def test_overlay_tokens(self) -> None:
        for dark in (True, False):
            theme = make(scheme_dark=dark)
            self.assertEqual(theme.color("Scrim"), QColor(0, 0, 0, 0x66))  # 40% black
            self.assertEqual(theme.color("SelectionEdge"), QColor("#FFFFFF"))
            self.assertEqual(theme.color("SelectionHalo"), QColor(0, 0, 0, 0x80))
            self.assertEqual(theme.color("ModeFrame").alpha(), 0)
        hc = make(hc=True)
        self.assertEqual(hc.color("Scrim").alpha(), 0)  # high contrast draws the frame instead of a scrim
        self.assertEqual(hc.color("SelectionEdge"), QColor(*HC_COLORS["highlight"]))
        self.assertEqual(hc.color("SelectionHalo"), QColor(*HC_COLORS["window"]))
        self.assertEqual(hc.color("ModeFrame"), QColor(*HC_COLORS["highlight"]))
        self.assertNotIn(T.HC_TRANSPARENT, T.SYS_COLOR_NAMES)  # never read from Windows
        self.assertEqual({name for name in T._HC.values()} - set(T.SYS_COLOR_NAMES), {T.HC_TRANSPARENT})

    def test_mark_colour_follows_the_tint_on_light_pages(self) -> None:
        for dark in (True, False):
            for tint, expected in (("auto", "#A9C8F2"), ("cream", "#A9C8F2"), ("peach", "#98BBE8"),
                                   ("blue", "#E9B949"), ("dark", "#36527F")):
                theme = make(FakeSettings(reading_tint=tint), scheme_dark=dark)
                self.assertEqual(hexes(theme.screen_mark_color(True)), expected, (dark, tint))
                self.assertEqual(hexes(theme.screen_mark_color(False)), "#36527F", (dark, tint))
        self.assertEqual(hexes(make(FakeSettings(reading_tint="neon")).screen_mark_color(True)), "#A9C8F2")
        self.assertEqual(make(hc=True).screen_mark_color(True), QColor(*HC_COLORS["highlight"]))

    def test_page_ink_stays_readable_under_every_mark(self) -> None:
        self.assertEqual(T.SCREEN_MARK_ALPHA,
                         {"band": 0.16, "band_sentence": 0.24, "word": 0.40, "word_paused": 0.20})
        for tint in T.READING_PRESETS:
            for dark in (True, False):
                theme = make(FakeSettings(reading_tint=tint), scheme_dark=dark)
                for page, ink, light in self.PAGES:
                    mark = theme.screen_mark_color(light)
                    for part, alpha in T.SCREEN_MARK_ALPHA.items():
                        with self.subTest(tint=tint, dark=dark, page=page, ink=ink, part=part):
                            under = composite(mark, alpha, QColor(page))
                            self.assertGreaterEqual(contrast_ratio(QColor(ink), under), 7.0)

    def test_ruler_tables_match_settings(self) -> None:
        self.assertEqual(tuple(T.RULER_BAND), S.RULER_HEIGHTS)
        self.assertEqual(tuple(T.RULER_DIM), S.RULER_DIMS)
        self.assertEqual(T.RULER_BAND, {"small": 36, "medium": 72, "large": 120})
        self.assertEqual(T.RULER_DIM, {"light": 0.40, "medium": 0.60, "strong": 0.80})

    def test_ruler_dims_with_the_reading_background(self) -> None:
        for dark, background in ((True, "#1F2023"), (False, "#FAF5E9")):  # Automatic follows the app theme
            settings = FakeSettings()
            theme = make(settings, scheme_dark=dark)
            reading = theme.reading_colors()
            for level, alpha in T.RULER_DIM.items():
                settings.values["ruler.dim"] = level
                for colors in (theme.ruler_colors(), theme.ruler_colors(level)):
                    with self.subTest(dark=dark, level=level):
                        self.assertEqual(colors["dim"].name().upper(), background)
                        self.assertEqual(colors["dim"].alpha(), round(alpha * 255))
                        self.assertEqual(colors["edge"], reading["edge"])
                        self.assertEqual(colors["line"], reading["highlight"])
        peach = make(FakeSettings(reading_tint="peach")).ruler_colors("strong")
        self.assertEqual(peach["dim"].name().upper(), "#EDD1B0")

    def test_high_contrast_ruler_has_lines_and_no_dimming(self) -> None:
        colors = make(hc=True).ruler_colors()
        self.assertEqual(colors["dim"].alpha(), 0)
        self.assertEqual(colors["line"], QColor(*HC_COLORS["highlight"]))
        self.assertEqual(colors["edge"], QColor(*HC_COLORS["highlight"]))


class TestTray(unittest.TestCase):
    def test_follows_taskbar_theme_not_app_theme(self) -> None:
        dark_bar = make(scheme_dark=False, system_light=False)
        self.assertEqual([hexes(dark_bar.tray_color(t)) for t in T.TRAY_TOKENS],
                         ["#FFFFFF", "#DB9EE5", "#FCE100", "#FF99A4"])
        self.assertEqual(hexes(dark_bar.color("Accent")), "#8E3AA7")
        light_bar = make(scheme_dark=True, system_light=True)
        self.assertEqual([hexes(light_bar.tray_color(t)) for t in T.TRAY_TOKENS],
                         ["#1A1A1A", "#8E3AA7", "#9D5D00", "#C42B1C"])
        self.assertFalse(light_bar.taskbar_dark)

    def test_accent_coloured_taskbar_uses_foreground_bars(self) -> None:
        theme = make(prevalence=True)
        self.assertEqual(theme.tray_color("Accent"), theme.tray_color("Foreground"))

    def test_stale_prevalence_on_a_light_taskbar_is_ignored(self) -> None:
        """Windows colours the taskbar only in dark mode; ColorPrevalence=1 can linger after switching to light."""
        theme = make(system_light=True, prevalence=True)
        self.assertEqual(hexes(theme.tray_color("Accent")), "#8E3AA7")

    def test_tray_accent_passes_the_guard(self) -> None:
        theme = make(scheme_dark=True, system_light=True, palette=same((0xFA, 0xFA, 0xFA)))
        self.assertEqual(hexes(theme.tray_color("Accent")), "#005FB8")  # light taskbar: fallback, not #FAFAFA
        self.assertEqual(hexes(theme.color("Accent")), "#FAFAFA")  # the dark panel can use it


class TestSystemState(unittest.TestCase):
    def test_dark_comes_from_qt_then_registry(self) -> None:
        self.assertFalse(make(scheme_dark=False, apps_light=False).dark)
        self.assertTrue(make(scheme_dark=True, apps_light=True).dark)
        self.assertFalse(make(scheme_dark=None, apps_light=True).dark)
        self.assertTrue(make(scheme_dark=None, apps_light=False).dark)

    def test_k_clamp(self) -> None:
        for scale, k in ((0.8, 1.0), (1.0, 1.0), (1.25, 1.25), (1.5, 1.5), (2.25, 1.5), (float("nan"), 1.0),
                         (float("inf"), 1.0), ("x", 1.0), (None, 1.0), (10 ** 400, 1.0)):
            with self.subTest(scale=scale):
                self.assertEqual(make(scale=scale).k, k)

    def test_px_and_line_height(self) -> None:
        big = make(scale=1.5)
        self.assertEqual((big.px(376), big.px(164)), (564, 246))
        self.assertEqual(big.line_height("body"), 30)
        mid = make(scale=1.25)
        self.assertEqual((mid.px(376), mid.px(14), mid.px(10), mid.px(0)), (470, 18, 13, 0))
        self.assertEqual(make().px(7.5), 8)

    def test_reduced_motion(self) -> None:
        still, moving = make(animations=False), make(animations=True)
        self.assertTrue(still.reduced_motion)
        self.assertEqual((still.ms(167), still.ms(83), still.ms(0)), (0, 0, 0))
        self.assertFalse(moving.reduced_motion)
        self.assertEqual((moving.ms(167), moving.ms(250)), (167, 250))

    def test_refresh_emits_only_on_change(self) -> None:
        system = FakeSystem(families=())
        theme = Theme(FakeSettings(), system=system)
        fired: list[int] = []
        theme.changed.connect(lambda: fired.append(1))
        self.assertFalse(theme.refresh())
        self.assertEqual(fired, [])
        system.palette = same((0x30, 0x60, 0xFF))
        system.scheme_dark = False
        self.assertTrue(theme.refresh())
        self.assertEqual(hexes(theme.color("Accent")), "#3060FF")
        self.assertFalse(theme.refresh())
        system.hc = True
        self.assertTrue(theme.refresh())
        system.scale = 1.5
        self.assertTrue(theme.refresh())
        self.assertEqual(theme.font("body").pixelSize(), 21)
        self.assertEqual(len(fired), 3)

    def test_refresh_sees_high_contrast_colour_changes(self) -> None:
        system = FakeSystem(families=(), hc=True)
        theme = Theme(FakeSettings(), system=system)
        system.colors = dict(HC_COLORS, window=(255, 255, 255), windowtext=(0, 0, 0))  # e.g. "Desert"
        self.assertTrue(theme.refresh())
        self.assertEqual((theme.color("Surface"), theme.color("TextPrimary")),
                         (QColor(255, 255, 255), QColor(0, 0, 0)))
        self.assertEqual(theme.tray_color("Foreground"), QColor(0, 0, 0))
        self.assertFalse(theme.refresh())

    def test_text_settings_emit_changed(self) -> None:
        settings = FakeSettings()
        theme = make(settings, families={"Verdana"})
        fired: list[int] = []
        theme.changed.connect(lambda: fired.append(1))
        medium = theme.font("reading").pixelSize()
        self.assertEqual(theme.line_height("reading"), round(T.READING_TEXT["medium"] * 1.5))
        settings.set("text_size", "xlarge")
        self.assertGreater(theme.font("reading").pixelSize(), medium)
        self.assertEqual(theme.line_height("reading"), round(T.READING_TEXT["xlarge"] * 1.5))
        settings.set("spacing", "extra")
        self.assertEqual(theme.line_height("reading"), round(T.READING_TEXT["xlarge"] * 1.8))
        settings.set("reading_tint", "cream")
        settings.set("reading_font", "calibri")
        settings.set("volume", 3)
        self.assertEqual(len(fired), 4)

    def test_with_real_settings(self) -> None:
        folder = tempfile.mkdtemp(prefix="ra_theme_test_")
        try:
            settings = S.Settings(os.path.join(folder, "s.json"))
            theme = Theme(settings, system=FakeSystem(families=()))
            fired: list[int] = []
            theme.changed.connect(lambda: fired.append(1))
            medium = theme.font("reading").pixelSize()
            settings.set("text_size", "large")
            self.assertGreater(theme.font("reading").pixelSize(), medium)
            self.assertEqual(len(fired), 1)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class TestReadingPresets(unittest.TestCase):
    def test_presets_match_settings_tints(self) -> None:
        self.assertEqual(set(T.READING_PRESETS), set(S.READING_TINTS))
        self.assertEqual(set(T.READING_PRESETS), set(T.TINT_NAMES))
        self.assertEqual(set(T.READING_TEXT), set(S.TEXT_SIZES))
        self.assertEqual(set(T.READING_FONT_FAMILIES), set(S.READING_FONTS))
        self.assertEqual(set(T.READING_SPACING), set(S.SPACINGS))
        for tint, modes in T.READING_PRESETS.items():
            self.assertEqual(set(modes), {"dark", "light"}, tint)
            for mode, colors in modes.items():
                self.assertLessEqual(set(colors), set(T.READING_ROLES), (tint, mode))
                for role, value in colors.items():
                    self.assertTrue(QColor(value).isValid(), (tint, mode, role))

    def test_auto_follows_the_app_theme(self) -> None:
        dark = make(scheme_dark=True).reading_colors()
        light = make(scheme_dark=False).reading_colors()
        self.assertEqual(hexes(dark["background"]), T._SOFT_DARK["background"])
        self.assertEqual(hexes(light["background"]), T._CREAM["background"])

    def test_custom_preset_and_unknown_tint(self) -> None:
        presets = {"auto": T.READING_PRESETS["auto"],
                   "cream": {"dark": {}, "light": {"background": "#FAF5E9", "text": "#24211C",
                                                    "highlight": "#80C6DAF8"}}}
        with mock.patch.dict(T.READING_PRESETS, presets, clear=True):
            colors = make(FakeSettings(reading_tint="cream"), scheme_dark=False).reading_colors()
            background = QColor("#FAF5E9")
            self.assertEqual(colors["background"], background)
            self.assertEqual(hexes(colors["text"]), "#24211C")
            self.assertEqual(colors["highlight"], composite(QColor("#C6DAF8"), 128 / 255, background))
            self.assertEqual(colors["highlight_paused"], composite(QColor("#24211C"), 0.08, background))
            self.assertEqual(colors["band"], composite(QColor("#24211C"), 0.06, background))
            self.assertEqual(hexes(colors["highlight_text"]), "#24211C")
            self.assertEqual(hexes(colors["secondary"]), "#5F5F5F")
            self.assertEqual(colors["edge"], composite(QColor("#24211C"), 0.10, background))
            self.assertEqual((colors["keycap_fill"], colors["keycap_stroke"]),
                             (QColor(T._LIGHT["KeycapFill"]), QColor(T._LIGHT["KeycapStroke"])))
            self.assertEqual(hexes(colors["keycap_text"]), "#24211C")
            self.assertEqual(colors["skipped"], colors["secondary"])
            unknown = make(FakeSettings(reading_tint="gone"), scheme_dark=False).reading_colors()
            self.assertEqual(hexes(unknown["background"]), T._CREAM["background"])

    def test_card_edge_and_keycaps(self) -> None:
        """The hairline is text over the background (10% on light cards, 6% on dark); chips use tint keycaps."""
        light = make(scheme_dark=False).reading_colors()
        dark = make(scheme_dark=True).reading_colors()
        self.assertEqual((hexes(light["edge"]), hexes(dark["edge"])), ("#E5E0D5", "#2B2C2E"))
        self.assertEqual((hexes(light["keycap_fill"]), hexes(light["keycap_stroke"])), ("#FFFFFF", "#D3CFC4"))
        self.assertEqual((hexes(dark["keycap_fill"]), hexes(dark["keycap_stroke"])), ("#2F2F31", "#474747"))
        for colors in (light, dark):
            self.assertGreaterEqual(contrast_ratio(colors["keycap_text"], colors["keycap_fill"]), 4.5)

    def test_skipped_text_is_readable_and_unlike_earlier_sentences(self) -> None:
        """Text shown but not read aloud reaches 4.5:1 on the background and on the band (a skipped citation can sit
        in the current sentence), at about the lightness of the earlier sentences but with the opposite hue cast."""
        for tint in T.READING_PRESETS:
            for dark in (True, False):
                colors = make(FakeSettings(reading_tint=tint), scheme_dark=dark).reading_colors()
                with self.subTest(tint=tint, dark=dark):
                    self.assertIn("skipped", T.READING_PRESETS[tint]["dark" if dark else "light"])
                    for bg in ("background", "band"):
                        self.assertGreaterEqual(contrast_ratio(colors["skipped"], colors[bg]), 4.5, bg)
                    gap = abs(colors["skipped"].hsvHue() - colors["secondary"].hsvHue()) % 360
                    self.assertGreaterEqual(min(gap, 360 - gap), 90)
                    ratio = (contrast_ratio(colors["skipped"], colors["background"])
                             / contrast_ratio(colors["secondary"], colors["background"]))
                    self.assertLess(abs(ratio - 1), 0.15)

    def test_every_preset_is_readable(self) -> None:
        """Every preset, including any added later, meets WCAG AA (4.5:1) for all reading text."""
        for tint in T.READING_PRESETS:
            for dark, palette in itertools.product((True, False), (MACHINE_PALETTE, None)):
                colors = make(FakeSettings(reading_tint=tint), scheme_dark=dark, palette=palette).reading_colors()
                with self.subTest(tint=tint, dark=dark, palette=palette is not None):
                    for fg, bg in (("text", "background"), ("secondary", "background"), ("text", "band"),
                                   ("highlight_text", "highlight"), ("highlight_text", "highlight_paused")):
                        self.assertGreaterEqual(contrast_ratio(colors[fg], colors[bg]), 4.5, (fg, bg))


class TestFonts(unittest.TestCase):
    GLYPHS = (0xF5B0, 0xF8AE, 0xE72C, 0xF8AC, 0xF8AD, 0xE921, 0xE738, 0xE710, 0xE74F, 0xE993, 0xE994, 0xE995,
              0xE992, 0xF384, 0xE783, 0xE946, 0xF16A,
              0xF683, 0xE9A6, 0xF2B7, 0xE8FE, 0xE7C3)  # every icon the UI draws

    def setUp(self) -> None:
        if not isinstance(_app, QGuiApplication) or QGuiApplication.platformName() != "windows":
            self.skipTest("font checks need the real 'windows' platform")

    def test_roles_resolve_to_the_named_families(self) -> None:
        theme = make(families=None)
        for role, spec in T.FONT_ROLES.items():
            with self.subTest(role=role):
                font = theme.font(role)
                info = QFontInfo(font)
                self.assertEqual(info.family(), spec.families[0])
                self.assertTrue(info.exactMatch())
                self.assertEqual(font.pixelSize(), spec.px)
                self.assertEqual(info.weight(), 600 if spec.semibold else 400)
                self.assertEqual(font.featureValue(QFont.Tag("tnum")), 1 if spec.tnum else 0)

    def test_sizes_scale_with_k(self) -> None:
        theme = make(families=None, scale=1.5)
        self.assertEqual({r: theme.font(r).pixelSize() for r in T.FONT_ROLES},
                         {"status": 21, "body": 21, "small": 18, "value": 21, "keycap": 18, "keycap_tick": 24,
                          "message": 24, "icon16": 24, "icon12": 18})

    def test_icon_font_has_every_glyph(self) -> None:
        metrics = QFontMetricsF(make(families=None).font("icon16"))
        for cp in self.GLYPHS:
            self.assertTrue(metrics.inFontUcs4(cp), hex(cp))

    def test_segoe_mdl2_lacks_only_the_screen_glyph(self) -> None:
        """Windows 10 without Segoe Fluent Icons: controls.icon() swaps U+F683 for U+E9A6."""
        if "Segoe MDL2 Assets" not in QFontDatabase.families():
            self.skipTest("Segoe MDL2 Assets is not installed")
        font = QFont("Segoe MDL2 Assets")
        font.setPixelSize(16)
        raw = QRawFont.fromFont(font)  # the font file itself; QFontMetricsF would count fallback fonts too
        self.assertFalse(raw.supportsCharacter(0xF683))
        for cp in self.GLYPHS:
            if cp != 0xF683:
                self.assertTrue(raw.supportsCharacter(cp), hex(cp))

    def test_fallback_families(self) -> None:
        theme = make(families={"Segoe UI", "Segoe MDL2 Assets"})
        status = QFontInfo(theme.font("status"))
        self.assertEqual((status.family(), status.weight()), ("Segoe UI", 600))
        self.assertEqual(QFontInfo(theme.font("body")).weight(), 400)
        self.assertEqual(QFontInfo(theme.font("icon16")).family(), "Segoe MDL2 Assets")

    def test_reading_role_sizes(self) -> None:
        for size, px in T.READING_TEXT.items():
            theme = make(FakeSettings(text_size=size), families=None)
            self.assertEqual((theme.font("reading").pixelSize(), theme.line_height("reading")),
                             (px, round(px * 1.5)))
            self.assertEqual(QFontInfo(theme.font("reading")).family(), "Verdana")

    def test_reading_fonts_keep_the_visual_size(self) -> None:
        """Other families are scaled to Verdana's x-height, so switching font keeps the apparent size."""
        verdana = make(FakeSettings(reading_font="verdana"), families=None).font("reading")
        target = QFontMetricsF(verdana).xHeight()
        for key in ("segoe", "calibri", "sitka"):
            font = make(FakeSettings(reading_font=key), families=None).font("reading")
            self.assertAlmostEqual(QFontMetricsF(font).xHeight(), target, delta=1.0, msg=key)

    def test_reading_line_height_follows_the_drawn_size(self) -> None:
        """A family drawn larger than Verdana's nominal size gets a proportionally taller line."""
        heights = {key: make(FakeSettings(reading_font=key), families=None).line_height("reading")
                   for key in T.READING_FONT_FAMILIES}
        self.assertEqual(heights["verdana"], 27)
        for key in ("segoe", "calibri", "sitka"):
            font = make(FakeSettings(reading_font=key), families=None).font("reading")
            self.assertAlmostEqual(heights[key], font.pixelSize() * 1.5, delta=1.0, msg=key)  # the drawn size
            self.assertGreater(heights[key], heights["verdana"], key)

    def test_fonts_are_copies_and_unknown_roles_raise(self) -> None:
        theme = make(families=None)
        theme.font("body").setPixelSize(99)
        self.assertEqual(theme.font("body").pixelSize(), 14)
        with self.assertRaises(KeyError):
            theme.font("heading")


class TestEasing(unittest.TestCase):
    def test_curves(self) -> None:
        self.assertIsNot(T.DECELERATE(), T.DECELERATE())
        for factory in (T.DECELERATE, T.EXIT):
            curve = factory()
            self.assertEqual(curve.type(), QEasingCurve.Type.BezierSpline)
            self.assertAlmostEqual(curve.valueForProgress(0.0), 0.0, places=3)
            self.assertAlmostEqual(curve.valueForProgress(1.0), 1.0, places=3)
        self.assertGreater(T.DECELERATE().valueForProgress(0.25), 0.6)  # fast start, slow end
        self.assertLess(T.EXIT().valueForProgress(0.5), 0.3)  # slow start
        self.assertEqual((T.FAST_MS, T.NORMAL_MS, T.SLOW_MS), (83, 167, 250))


class TestSystemAdapter(unittest.TestCase):
    def test_missing_win32_gives_defaults_and_one_warning(self) -> None:
        adapter = T.SystemAdapter()
        with mock.patch.object(T.importlib, "import_module", side_effect=ModuleNotFoundError("win32")), \
                self.assertLogs("saylight.theme", "WARNING") as logs:
            values = (adapter.apps_use_light_theme(), adapter.system_uses_light_theme(), adapter.high_contrast(),
                      adapter.animations_enabled(), adapter.text_scale_factor(), adapter.accent_palette(),
                      adapter.color_prevalence(), adapter.sys_colors())
            Theme(FakeSettings(), system=adapter)
        self.assertEqual(values, (False, False, False, True, 1.0, None, False, T._HC_DEFAULT))
        self.assertEqual(sum("win32" in line for line in logs.output), 1)

    def test_calls_pass_through_and_failures_are_contained(self) -> None:
        def broken() -> None:
            raise OSError("registry")

        fake = types.SimpleNamespace(
            apps_use_light_theme=lambda: True, system_uses_light_theme=lambda: True, high_contrast=lambda: True,
            animations_enabled=lambda: False, text_scale_factor=lambda: 1.75, accent_palette=broken,
            color_prevalence=lambda: True, COLOR_WINDOW=5, COLOR_WINDOWTEXT=8, COLOR_HIGHLIGHT=13,
            COLOR_HIGHLIGHTTEXT=14, COLOR_GRAYTEXT=17,
            sys_color_rgb=lambda index: {5: (1, 1, 1), 8: (2, 2, 2), 13: (3, 3, 3), 14: (4, 4, 4), 17: (5, 5, 5)}[index])
        adapter = T.SystemAdapter()
        with mock.patch.object(T.importlib, "import_module", return_value=fake), \
                self.assertLogs("saylight.theme", "WARNING") as logs:
            theme = Theme(FakeSettings(), system=adapter)
            self.assertIsNone(adapter.accent_palette())
            colors = adapter.sys_colors()
        self.assertEqual(sum("accent_palette" in line for line in logs.output), 1)
        self.assertTrue(theme.high_contrast and theme.reduced_motion and not theme.taskbar_dark)
        self.assertEqual(theme.k, 1.5)
        self.assertEqual(colors, {"window": (1, 1, 1), "windowtext": (2, 2, 2), "highlight": (3, 3, 3),
                                  "highlighttext": (4, 4, 4), "graytext": (5, 5, 5)})
        self.assertEqual(theme.color("Surface"), QColor(1, 1, 1))

    def test_qt_colour_scheme(self) -> None:
        self.assertIn(T.SystemAdapter().color_scheme_dark(), (True, False, None))


_WIN32_PATH = os.path.join(os.path.dirname(T.__file__), "win32.py")


@unittest.skipUnless(os.path.exists(_WIN32_PATH), "win32.py is not available")
class TestWin32Integration(unittest.TestCase):
    def test_real_adapter(self) -> None:
        from saylight import win32
        adapter = T.SystemAdapter()
        self.assertIsInstance(win32.apps_use_light_theme(), bool)
        self.assertIsInstance(win32.system_uses_light_theme(), bool)
        self.assertIsInstance(win32.high_contrast(), bool)
        self.assertIsInstance(win32.animations_enabled(), bool)
        self.assertIsInstance(win32.color_prevalence(), bool)
        self.assertTrue(1.0 <= win32.text_scale_factor() <= 2.25)
        palette = win32.accent_palette()
        self.assertTrue(palette is None or (len(palette) >= 7 and all(len(c) == 3 for c in palette)))
        self.assertEqual(set(adapter.sys_colors()), set(T.SYS_COLOR_NAMES))
        with self.assertNoLogs("saylight.theme", "WARNING"):
            theme = Theme(FakeSettings(), system=adapter)
        if not theme.high_contrast:
            source, accent = T.pick_accent(T._palette(palette), dark=theme.dark)
            self.assertEqual((theme.accent_source, theme.color("Accent")), (source, accent))
        self.assertTrue(T.K_MIN <= theme.k <= T.K_MAX)


if __name__ == "__main__":
    unittest.main()

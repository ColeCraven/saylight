"""Tests for the Voice tab (ui/voice_page.py): the built-in list grouped by accent, choosing a voice, the sample
preview, "Show all languages" (fetched only when asked, with loading and failure states) and scrolling.

The voice list is a real VoiceCatalog with a stand-in fetch; the preview is a fake. Nothing reaches the network.
"""

import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QPointF

from saylight.tests.test_panel import FakeSystem
from saylight.tests.test_settings_pages import ServicesCase, setUpModule  # noqa: F401 (setUpModule runs here)
from saylight.ui import voice_page
from saylight.ui.controls import G_PLAY
from saylight.voices.base import VoiceInfo
from saylight.voices.catalog import CURATED

FRENCH = [{"ShortName": "fr-FR-DeniseNeural", "Locale": "fr-FR", "Gender": "Female", "LocaleName": "French (France)"},
          {"ShortName": "fr-FR-HenriNeural", "Locale": "fr-FR", "Gender": "Male", "LocaleName": "French (France)"}]


class VoiceCase(ServicesCase):
    def setUp(self) -> None:
        super().setUp()
        self.page = self.open_tab(1)
        self.list = self.page.list

    def row(self, voice_id: str) -> int:
        return next(i for i, (kind, v) in enumerate(self.list.rows) if kind == "voice" and v.id == voice_id)

    def headers(self) -> list[str]:
        return [label for kind, label in self.list.rows if kind == "header"]

    def wait_for(self, status: str) -> None:
        deadline = time.monotonic() + 5
        while self.ctl.catalog.status != status and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        self.assertEqual(self.ctl.catalog.status, status)


class BuiltInListTests(VoiceCase):
    def test_english_voices_grouped_by_accent_with_no_network(self) -> None:
        self.assertEqual(len(self.list.voices()), len(CURATED))
        self.assertEqual(self.headers()[:3], ["English (United States)", "English (United Kingdom)",
                                              "English (Australia)"])
        self.assertEqual(len(self.headers()), 8)
        self.assertEqual(self.list.rows[1][1].id, "edge:en-US-AvaNeural")
        self.assertEqual(self.page.caption.accessibleName(), "35 English voices, grouped by accent")
        self.assertEqual(self.ctl.fetches, 0)

    def test_windows_voices_are_listed_last_and_said_to_sound_more_robotic(self) -> None:
        from saylight.voices.windows import describe_voice
        token = r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\MSTTS_V110_enUS_ZiraM"
        zira = describe_voice(token, "Microsoft Zira", "en-US", 1)
        self.ctl.catalog._local = lambda: [zira]
        self.page.sync()
        self.assertEqual(self.headers()[-1], "English (United States) · on this PC · sounds more robotic")
        self.assertIs(self.list.rows[-1][1], zira)

    def test_ava_is_the_marked_default_and_chosen(self) -> None:
        from saylight.ui.voice_page import voice_detail
        ava = self.list.rows[1][1]
        self.assertEqual(voice_detail(ava), "Female · default")
        self.assertEqual(self.list.current, "edge:en-US-AvaNeural")
        self.assertIn("Selected: Ava, Female · default.", self.list.accessibleDescription())

    def test_voices_without_word_timing_say_so(self) -> None:
        from saylight.ui.voice_page import voice_detail
        voice = VoiceInfo(id="windows:zira", backend="windows", name="Zira", locale="en-US", gender="Female",
                          private=True, word_timing=False)
        self.assertEqual(voice_detail(voice), "Female · sentence highlight only")

    def test_clicking_a_row_chooses_the_voice(self) -> None:
        i = self.row("edge:en-GB-SoniaNeural")
        self.list.scroll.snap(min(self.list.max_scroll(), self.list._tops()[i] - 10))
        r = self.list.row_rect(i)
        self.click(self.list, QPointF(r.left() + 60, r.center().y()))
        self.assertIn(("set_voice", "edge:en-GB-SoniaNeural"), self.ctl.calls)
        self.assertEqual(self.list.current, "edge:en-GB-SoniaNeural")

    def test_the_chosen_voice_is_scrolled_into_view(self) -> None:
        self.settings.set("voice", "edge:en-ZA-LukeNeural")
        self.page.set_shown(True)
        r = self.list.row_rect(self.row("edge:en-ZA-LukeNeural"))
        self.assertGreaterEqual(r.top(), 0)
        self.assertLessEqual(r.bottom(), self.list.height())

    def test_a_chosen_voice_that_isnt_listed_stays_on_top(self) -> None:
        self.ctl.online = FRENCH
        self.page.row_all.set_checked(True, emit=True)
        self.wait_for("online")
        self.settings.set("voice", "edge:fr-FR-HenriNeural")
        self.page.row_all.set_checked(False, emit=True)
        self.assertEqual(self.headers()[0], "Your voice")
        self.assertEqual(self.list.rows[1][1].id, "edge:fr-FR-HenriNeural")

    def test_the_list_keeps_its_minimum_with_whats_read_aloud_below(self) -> None:
        self.assertEqual(self.list.height(), 156)
        p = self.page
        y = lambda w: w.geometry().y() + self.tabs.y()  # noqa: E731
        self.assertEqual(y(p.row_all), y(self.list) + 156 + 8)
        self.assertEqual(p.rules, [p.row_all.geometry().y() + 42 + 4])
        self.assertEqual(y(p.lbl_read), y(p.row_all) + 42 + 9)
        rows = [row for _key, row, _inv in p.read_rows] + [p.row_pron]
        self.assertEqual([y(r) for r in rows], [y(p.lbl_read) + 22 + 42 * i for i in range(5)])
        self.assertEqual(rows[-1].geometry().bottom() + 1 + self.tabs.y() + 12, self.panel.height())
        self.assertEqual(rows[-1].geometry().bottom() + 1 - (p.row_all.geometry().bottom() + 1), 241)


class ReadAloudTests(VoiceCase):
    def test_the_skip_switches_and_their_settings(self) -> None:
        p, s = self.page, self.settings
        rows = {key: row for key, row, _inv in p.read_rows}
        self.assertEqual([row.label for _key, row, _inv in p.read_rows],
                         ["Skip citations", "Skip links and DOIs", "Skip notes, captions and tables",
                          "Skip the reference list"])
        self.assertTrue(all(row.checked for row in rows.values()))  # the defaults skip all four
        rows["skip_urls"].toggled.emit(False)
        self.assertFalse(s.get("skip_urls"))
        rows["read_asides"].toggled.emit(False)   # "Skip notes" off reads them
        self.assertTrue(s.get("read_asides"))
        s.set("read_references", True)
        self.assertFalse(rows["read_references"].checked)
        self.assertIn("Changes apply at once.", rows["skip_citations"].tooltip_text())

    def test_pronunciations_row(self) -> None:
        from saylight.ui.voice_page import pronunciations_detail
        self.assertEqual(pronunciations_detail(0, True), "None yet")
        self.assertEqual(pronunciations_detail(1, True), "1 word")
        self.assertEqual(pronunciations_detail(12, False), "12 words · off")
        self.assertEqual(self.page.row_pron.detail, "None yet")
        self.assertEqual(self.page.row_pron.tooltip_text(), "Change how words are said")

    def test_the_wheel_scrolls_the_list_then_passes_through(self) -> None:
        self.assertTrue(self.wheel(self.list, 1))
        self.assertGreater(self.list.scroll.value, 0)
        self.wheel(self.list, 100)
        self.assertEqual(self.list.scroll.value, self.list.max_scroll())
        self.assertFalse(self.wheel(self.list, 1))


class PreviewTests(VoiceCase):
    def zone_click(self, voice_id: str) -> None:
        i = self.row(voice_id)
        self.list._set_hover(i, False)  # the button shows under the pointer
        self.click(self.list, self.list.zone_rect(i).center())

    def test_preview_plays_the_sample_then_stops(self) -> None:
        self.zone_click("edge:en-US-AvaNeural")
        self.assertEqual(self.ctl.preview.calls, [("play", "edge:en-US-AvaNeural", 1.0)])
        self.assertEqual(self.list.preview_state, "loading")
        self.ctl.preview.set("playing")
        self.assertIn("Playing a sample of Ava.", self.list.accessibleDescription())
        self.zone_click("edge:en-US-AvaNeural")
        self.assertEqual(self.ctl.preview.calls[-1], ("stop",))
        self.assertNotIn(("set_voice", "edge:en-US-AvaNeural"), self.ctl.calls)  # the button never chooses

    def test_preview_another_voice_without_choosing_it(self) -> None:
        self.zone_click("edge:en-US-AndrewNeural")
        self.assertEqual(self.ctl.preview.calls, [("play", "edge:en-US-AndrewNeural", 1.0)])
        self.assertEqual(self.settings.get("voice"), "edge:en-US-AvaNeural")

    def test_no_preview_while_reading(self) -> None:
        self.zone_click("edge:en-US-AvaNeural")
        self.panel.present("reading", {"label": "Reading", "play_action": "pause"})
        self.assertEqual(self.ctl.preview.calls[-1], ("stop",))
        self.assertFalse(self.list.preview_enabled)
        self.zone_click("edge:en-US-AvaNeural")
        self.assertEqual(self.ctl.preview.calls[-1], ("stop",))
        self.list._hover_zone = True
        self.assertEqual(self.list._tip(), "Pause the reading to hear a sample")
        self.panel.present("paused", {"label": "Paused", "play_action": "resume"})
        self.assertTrue(self.list.preview_enabled)

    def test_leaving_the_tab_or_the_page_stops_the_sample(self) -> None:
        self.zone_click("edge:en-US-AvaNeural")
        self.tabs.set_index(0)
        self.assertEqual(self.ctl.preview.calls[-1], ("stop",))
        self.open_tab(1)
        self.zone_click("edge:en-US-AvaNeural")
        self.page.set_shown(False)
        self.assertEqual(self.ctl.preview.calls[-1], ("stop",))


class AllLanguagesTests(VoiceCase):
    def test_fetched_only_when_asked(self) -> None:
        self.ctl.online = FRENCH
        self.assertEqual(self.ctl.fetches, 0)
        self.page.row_all.set_checked(True, emit=True)
        self.assertEqual(self.page.caption.accessibleName(), "Loading voices in every language…")
        self.wait_for("online")
        QCoreApplication.processEvents()
        self.assertEqual(self.ctl.fetches, 1)
        self.assertIn("French (France)", self.headers())
        self.assertEqual(self.page.caption.accessibleName(), "2 voices in 1 language")
        self.assertEqual(self.headers()[0], "Your voice")  # Ava isn't in this list, so she stays on top
        self.page.row_all.set_checked(False, emit=True)
        self.assertEqual(len(self.list.voices()), len(CURATED))
        self.page.row_all.set_checked(True, emit=True)
        self.assertEqual(self.ctl.fetches, 1)  # kept for the session

    def test_a_failure_falls_back_to_the_english_list(self) -> None:
        self.ctl.online = None
        self.page.row_all.set_checked(True, emit=True)
        self.wait_for("failed")
        QCoreApplication.processEvents()
        self.assertFalse(self.page.row_all.checked)
        self.assertEqual(self.page.caption.accessibleName(),
                         "Couldn't load other languages, so here are the English voices.")
        self.assertEqual(self.page.caption.color_token, "Critical")
        self.assertEqual(len(self.list.voices()), len(CURATED))
        self.ctl.online = FRENCH
        self.page.row_all.set_checked(True, emit=True)  # trying again fetches again
        self.wait_for("online")
        self.assertEqual(self.ctl.fetches, 2)


class ChosenVoiceAfterRestartTests(ServicesCase):
    def test_a_voice_from_another_language_is_still_shown_after_a_restart(self) -> None:
        # Chosen from the full list last session; this session has only the built-in list (nothing fetched).
        self.settings.set("voice", "edge:fr-FR-HenriNeural")
        page = self.open_tab(1)
        page.sync()
        rows = page.list.rows
        self.assertEqual(self.ctl.fetches, 0)
        self.assertEqual(rows[0], ("header", "Your voice"))
        self.assertEqual((rows[1][0], rows[1][1].id, rows[1][1].name), ("voice", "edge:fr-FR-HenriNeural", "Henri"))
        self.assertIn("Selected: Henri.", page.list.accessibleDescription())

    def test_an_unknown_kind_of_voice_is_not_made_up(self) -> None:
        self.assertIsNone(voice_page.stored_voice("windows:MSTTS_V110_enUS_ZiraM"))
        self.assertIsNone(voice_page.stored_voice("edge:Ava"))


HC_BLACK = {"window": (0, 0, 0), "windowtext": (255, 255, 255), "highlight": (26, 235, 255),
            "highlighttext": (0, 0, 0), "graytext": (63, 242, 63)}
HC_WHITE = {"window": (255, 255, 255), "windowtext": (0, 0, 0), "highlight": (55, 0, 110),
            "highlighttext": (255, 255, 255), "graytext": (96, 0, 0)}


def contrast(a, b) -> float:
    def lum(c) -> float:
        def ch(v: float) -> float:
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class _HighContrastCase(ServicesCase):
    colors = HC_BLACK

    def setUp(self) -> None:
        system = type("System", (FakeSystem,), {"high_contrast": lambda _self: True,
                                                 "sys_colors": lambda _self, c=self.colors: dict(c)})
        with mock.patch("saylight.tests.test_settings_pages.FakeSystem", system):
            super().setUp()
        self.assertTrue(self.theme.high_contrast)

    def test_the_disabled_preview_button_on_the_selected_row_can_be_seen(self) -> None:
        page = self.open_tab(1)
        page.on_state("reading", {})  # previews are off while reading
        self.assertFalse(page.list.preview_enabled)
        chosen = next(i for i, (kind, v) in enumerate(page.list.rows) if kind == "voice" and v.id == page.list.current)
        zone = page.list.zone_rect(chosen)
        drawn = []
        real = voice_page.draw_glyph

        def record(p, rect, glyph, font, color, *args):
            drawn.append((rect, glyph, color))
            return real(p, rect, glyph, font, color, *args)

        with mock.patch.object(voice_page, "draw_glyph", record):
            page.list.grab()
        colors = [color for rect, glyph, color in drawn if glyph == G_PLAY and zone.contains(rect.center())]
        self.assertEqual(len(colors), 1, "the selected row shows its preview button")
        self.assertGreaterEqual(contrast(colors[0], self.theme.color("Accent")), 3.0)  # the selected row's plate


class HighContrastBlackTests(_HighContrastCase):
    colors = HC_BLACK


class HighContrastWhiteTests(_HighContrastCase):
    colors = HC_WHITE


del _HighContrastCase


if __name__ == "__main__":
    unittest.main()

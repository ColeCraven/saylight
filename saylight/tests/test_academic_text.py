"""Tests for academic_text (run: python -I -m unittest discover -s <this folder> -v)."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from saylight.academic_text import (  # noqa: E402
    chunk_sentences, chunk_text, normalize, normalize_pair, prepare, references_end, references_start,
    split_sentences, spoken_forms,
)

N = normalize
S = split_sentences


# ---------------------------------------------------------------------------
# normalize: characters
# ---------------------------------------------------------------------------

class CharacterCleanup(unittest.TestCase):
    def test_ligatures(self):
        self.assertEqual(N("signi\ufb01cant e\ufb00ect, \ufb02uency, o\ufb03ce, ba\ufb04ed, \ufb05ore \ufb06ep"),
                         "significant effect, fluency, office, baffled, store step")

    def test_soft_hyphen_inside_word_and_at_line_end(self):
        self.assertEqual(N("meta\u00adcognition and compre\u00ad\nhension"), "metacognition and comprehension")

    def test_zero_width_characters_removed(self):
        self.assertEqual(N("work\u200bing mem\u200dory\ufeff load\u2060"), "working memory load")

    def test_non_breaking_and_thin_spaces(self):
        self.assertEqual(N("p\u00a0<\u00a0.05 and N\u202f=\u2009120"), "p < .05 and N = 120")

    def test_crlf_and_tabs(self):
        self.assertEqual(N("First line of a\r\nparagraph.\r\n\r\nSecond\tparagraph."),
                         "First line of a paragraph.\n\nSecond paragraph.")

    def test_smart_quotes_are_kept(self):
        self.assertEqual(N("Students called it \u201cbrain fog\u201d and \u2018tiring\u2019."),
                         "Students called it \u201cbrain fog\u201d and \u2018tiring\u2019.")

    def test_pdf_cid_glyph_codes_removed(self):
        self.assertEqual(N("The (cid:3)model fit well."), "The model fit well.")

    def test_unicode_minus_becomes_hyphen_minus(self):
        self.assertEqual(N("a slope of \u22120.42"), "a slope of -0.42")

    def test_empty_and_whitespace_only(self):
        self.assertEqual(N(""), "")
        self.assertEqual(N("  \n\n\t \u200b "), "")

    def test_rejects_non_string(self):
        with self.assertRaises(TypeError):
            N(None)


# ---------------------------------------------------------------------------
# normalize: PDF line structure
# ---------------------------------------------------------------------------

class LineBreaks(unittest.TestCase):
    def test_syllable_hyphen_joined(self):
        self.assertEqual(N("Reading compre-\nhension improved after the inter-\nvention."),
                         "Reading comprehension improved after the intervention.")

    def test_real_compound_hyphen_kept(self):
        self.assertEqual(N("This is a well-\nknown finding in self-\nreport measures."),
                         "This is a well-known finding in self-report measures.")

    def test_compound_second_element_kept(self):
        self.assertEqual(N("We used an evidence-\nbased, domain-\nspecific approach."),
                         "We used an evidence-based, domain-specific approach.")

    def test_document_evidence_decides(self):
        text = ("The scores were cor-\nrelated with age. Scores correlated strongly.")
        self.assertEqual(N(text), "The scores were correlated with age. Scores correlated strongly.")
        text2 = "Large-scale studies are rare. We ran a large-\nscale replication."
        self.assertIn("large-scale replication", N(text2))

    def test_suffix_fragment_joined(self):
        self.assertEqual(N("the high-\nest scores and their well-\nness"), "the highest scores and their wellness")

    def test_capital_or_digit_after_hyphen_kept(self):
        self.assertEqual(N("during COVID-\n19 and in non-\nHispanic samples"),
                         "during COVID-19 and in non-Hispanic samples")

    def test_existing_compound_and_single_letter(self):
        self.assertEqual(N("a state-of-the-\nart model and a t-\ntest"), "a state-of-the-art model and a t-test")

    def test_doubled_vowel_keeps_hyphen(self):
        self.assertEqual(N("an anti-\ninflammatory drug and a meta-\nanalysis"),
                         "an anti-inflammatory drug and a meta-analysis")

    def test_suspended_hyphen(self):
        self.assertEqual(N("We compared pre-\nand post-test scores."), "We compared pre- and post-test scores.")

    def test_single_newlines_become_spaces(self):
        text = ("Working memory capacity predicts reading\ncomprehension in adolescents\n"
                "with and without dyslexia.")
        self.assertEqual(N(text), "Working memory capacity predicts reading comprehension in "
                                  "adolescents with and without dyslexia.")

    def test_paragraph_breaks_preserved(self):
        self.assertEqual(N("First paragraph ends here.\n\n\n\nSecond one\nstarts here."),
                         "First paragraph ends here.\n\nSecond one starts here.")

    def test_dash_at_line_end_is_not_dehyphenated(self):
        self.assertEqual(N("the effect \u2014\nwhich was small \u2014\nvanished"),
                         "the effect, which was small, vanished")


class PageFurniture(unittest.TestCase):
    def test_page_number_line_removed_and_sentence_rejoined(self):
        text = "Participants were recruited from\n\n14\n\nthree secondary schools in Leeds."
        self.assertEqual(N(text), "Participants were recruited from three secondary schools in Leeds.")

    def test_page_x_of_y_and_dashed_numbers(self):
        self.assertEqual(N("The task was easy.\nPage 3 of 12\n- 4 -\nIt took ten minutes."),
                         "The task was easy. It took ten minutes.")

    def test_caps_running_header_with_page_number(self):
        text = ("Children in the intervention group made larger\n12 SMITH ET AL.\n"
                "gains in decoding than controls.")
        self.assertEqual(N(text), "Children in the intervention group made larger gains in decoding than controls.")

    def test_repeated_running_header_removed(self):
        text = ("Bacterial growth was measured every hour using\n"
                "Journal of Microbial Ecology 7\n"
                "optical density at 600 nm. Cultures were kept at a\n"
                "Journal of Microbial Ecology 8\n"
                "constant temperature throughout.")
        self.assertEqual(N(text), "Bacterial growth was measured every hour using optical density at 600 nm. "
                                  "Cultures were kept at a constant temperature throughout.")

    def test_repeated_section_heading_survives(self):
        text = ("Study 1\n\nGeneral Results\n\nAccuracy was high.\n\nStudy 2\n\n"
                "General Results\n\nAccuracy was lower.")
        self.assertEqual(N(text).count("General Results"), 2)

    def test_boilerplate_lines(self):
        text = ("Downloaded from https://academic.oup.com/brain by guest on 3 March 2024\n"
                "Running head: READING AND ATTENTION\n"
                "\u00a9 2021 American Psychological Association\n"
                "The sample was large.")
        self.assertEqual(N(text), "The sample was large.")

    def test_journal_header_line(self):
        text = ("Journal of Educational Psychology 2019, Vol. 111, No. 3, 456\u2013470\n\n"
                "Feedback improved learning.")
        self.assertEqual(N(text), "Feedback improved learning.")

    def test_wrapped_line_starting_with_number_is_kept(self):
        text = ("The final sample comprised a total of\n"
                "42 participants who completed both sessions of the\n"
                "experiment in the laboratory.")
        self.assertIn("42 participants", N(text))


class HeadingsAndBullets(unittest.TestCase):
    def test_numbered_heading_becomes_own_paragraph(self):
        text = ("2.3 Participants\nForty undergraduates (28 women) took part in exchange for course\n"
                "credit and were tested individually in a quiet room.")
        self.assertEqual(N(text).split("\n\n")[0], "2.3 Participants")

    def test_plain_heading_after_sentence(self):
        text = ("all of which is consistent with prior work on reading fluency.\nMethod\n"
                "We recruited 64 children from four primary schools in the region.")
        self.assertEqual(N(text).split("\n\n"),
                         ["all of which is consistent with prior work on reading fluency.", "Method",
                          "We recruited 64 children from four primary schools in the region."])

    def test_unicode_bullets_removed_and_separated(self):
        text = "Key findings:\n\u2022 Accuracy improved\n\u2022 Reading speed was unchanged\n\u25aa Effects persisted"
        self.assertEqual(N(text).split("\n\n"),
                         ["Key findings:", "Accuracy improved", "Reading speed was unchanged", "Effects persisted"])

    def test_hyphen_bullets_and_wrapped_items(self):
        text = "Limitations:\n- the sample was small and\n  drawn from one school\n- no follow-up"
        self.assertEqual(N(text).split("\n\n"),
                         ["Limitations:", "the sample was small and drawn from one school", "no follow-up"])

    def test_word_document_paragraphs_on_single_lines(self):
        text = ("Introduction\nDyslexia affects decoding.\nIt also affects fluency.\n"
                "Many adults develop compensatory strategies.")
        self.assertEqual(N(text).split("\n\n")[0], "Introduction")


# ---------------------------------------------------------------------------
# normalize: citations
# ---------------------------------------------------------------------------

class NumericCitations(unittest.TestCase):
    def test_bracket_forms(self):
        text = "Transformers dominate NLP [1], vision [1, 2], audio [3\u20137] and RL [12; 15]."
        self.assertEqual(N(text), "Transformers dominate NLP, vision, audio and RL.")

    def test_ieee_range_of_brackets(self):
        self.assertEqual(N("as reported earlier [1]\u2013[3]."), "as reported earlier.")

    def test_narrative_numeric_citation_read_as_reference(self):
        self.assertEqual(N("The loss is defined as in [3]. See Ref. [12] for details."),
                         "The loss is defined as in reference 3. See reference 12 for details.")
        self.assertEqual(N("as shown in [4, 5]"), "as shown in references 4 and 5")

    def test_intervals_are_not_citations(self):
        self.assertEqual(N("Weights were sampled in [0, 1] and k in the range [1, 10]."),
                         "Weights were sampled in [0, 1] and k in the range [1, 10].")

    def test_alphabetic_keys(self):
        self.assertEqual(N("Earlier work [SB18, Knu84] used tabular methods."), "Earlier work used tabular methods.")

    def test_unicode_superscript_citations(self):
        self.assertEqual(N("Tumour cells\u00b9\u00b2 migrate faster.\u00b3 Others disagree."),
                         "Tumour cells migrate faster. Others disagree.")

    def test_superscript_units_and_statistics_kept(self):
        self.assertEqual(N("The plot was 4 m\u00b2 and R\u00b2 = .41, \u03c7\u00b2 = 3.2."),
                         "The plot was 4 m\u00b2 and R\u00b2 = .41, \u03c7\u00b2 = 3.2.")

    def test_plain_digits_after_punctuation(self):
        self.assertEqual(N("This is common in reading disorders.12 However, children,3 and adults differ."),
                         "This is common in reading disorders. However, children, and adults differ.")

    def test_nature_style_digits_stuck_to_words(self):
        self.assertEqual(N("as reported previously12,13 and in mice14."), "as reported previously and in mice14.")
        self.assertEqual(N("as reported previously12."), "as reported previously.")

    def test_decimals_and_page_refs_not_mistaken(self):
        self.assertEqual(N("The ratio was 3:1 and r = .12 (see p.12)."), "The ratio was 3:1 and r = .12 (see p.12).")


class AuthorYearCitations(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(N("Phonological awareness predicts decoding (Smith, 2019)."),
                         "Phonological awareness predicts decoding.")

    def test_multiple_with_et_al_and_ampersand(self):
        self.assertEqual(N("Feedback helps (Smith et al., 2019; Jones & Lee, 2020a) in class."),
                         "Feedback helps in class.")

    def test_see_and_page_locator(self):
        self.assertEqual(N("This is contested (see Smith, 2019, p. 4)."), "This is contested.")

    def test_prefixes_suffixes_and_harvard(self):
        self.assertEqual(N("A (e.g., Hattie & Timperley 2007; cf. Shute, 2008, pp. 153\u2013189) B."), "A B.")
        self.assertEqual(N("A (for a review, see van der Berg, 2018) B (Smith 2019: 45) C."), "A B C.")
        self.assertEqual(N("A (Baddeley, 1986, 2000, for an overview) B."), "A B.")

    def test_narrative_citation_keeps_names(self):
        self.assertEqual(N("Smith et al. (2019) showed a robust effect."), "Smith et al. showed a robust effect.")
        self.assertEqual(N("According to Gathercole and Baddeley (1993, 2014a), memory matters."),
                         "According to Gathercole and Baddeley, memory matters.")

    def test_narrative_possessive_and_sentence_end(self):
        self.assertEqual(N("Kintsch\u2019s (1998) model is influential."), "Kintsch\u2019s model is influential.")
        self.assertEqual(N("This was first noted by Smith et al. (2019). Later work agreed."),
                         "This was first noted by Smith et al. Later work agreed.")

    def test_ordinary_parentheticals_are_kept(self):
        text = ("The sample (n = 42) differed (p < .05) as shown (see Figure 2), "
                "especially one subgroup (e.g., the control group).")
        self.assertEqual(N(text), text)

    def test_mixed_parenthetical_keeps_non_citation_part(self):
        self.assertEqual(N("Diagnosis followed the manual (DSM-5; American Psychiatric Association, 2013)."),
                         "Diagnosis followed the manual (DSM-5).")

    def test_dates_and_lifespans_are_not_citations(self):
        self.assertEqual(N("Schools closed (March 2020) and Vygotsky (1896\u20131934) was cited."),
                         "Schools closed (March 2020) and Vygotsky (1896 to 1934) was cited.")

    def test_trailing_year_inside_prose_parenthetical(self):
        self.assertEqual(N("The scale is reliable (as argued by Smith & Jones, 2019)."),
                         "The scale is reliable (as argued by Smith & Jones).")

    def test_square_bracket_author_year(self):
        self.assertEqual(N("Crowdsourcing is noisy [Snow et al. 2008; Callison-Burch 2009]."),
                         "Crowdsourcing is noisy.")

    def test_skip_citations_false_keeps_everything(self):
        text = "Effects were large (Smith, 2019) [3]."
        self.assertEqual(N(text, skip_citations=False), text)


# ---------------------------------------------------------------------------
# normalize: URLs, symbols, whitespace
# ---------------------------------------------------------------------------

class Urls(unittest.TestCase):
    def test_inline_url_becomes_link(self):
        self.assertEqual(N("Materials are available at https://osf.io/x7k2q/ for reuse."),
                         "Materials are available at link for reuse.")

    def test_parenthetical_url_removed(self):
        self.assertEqual(N("We used PsychoPy (https://www.psychopy.org) to run the task."),
                         "We used PsychoPy to run the task.")

    def test_dois_after_punctuation_removed(self):
        self.assertEqual(N("Child Development, 90(2), 45\u201367. doi:10.1111/cdev.13045"),
                         "Child Development, 90(2), 45 to 67.")
        self.assertEqual(N("Psychol. Sci. https://doi.org/10.1177/0956797618773394."), "Psychol. Sci.")

    def test_url_broken_across_lines(self):
        self.assertEqual(N("Code is available at https://github.com/lab/read-\naloud/tree/main and online."),
                         "Code is available at link and online.")
        self.assertEqual(N("See https://doi.org/10.1037/\nxge0000123.\nThe next sentence."),
                         "See link. The next sentence.")

    def test_retrieved_from_removed(self):
        # A bare "(2022)" after a full stop is left alone (no author name precedes it).
        self.assertEqual(N("National Literacy Trust. (2022). Annual survey. Retrieved from https://literacytrust.org.uk/research"),
                         "National Literacy Trust. (2022). Annual survey.")
        self.assertEqual(N("Annual survey. Available at: https://literacytrust.org.uk/research"), "Annual survey.")

    def test_skip_urls_false_keeps_url_intact(self):
        text = "Data: https://example.org/a--b~1 (see 10\u201320)."
        self.assertEqual(N(text, skip_urls=False), "Data: https://example.org/a--b~1 (see 10 to 20).")

    def test_email_untouched(self):
        self.assertEqual(N("Contact j.smith@uni.ac.uk for access."), "Contact j.smith@uni.ac.uk for access.")


class Symbols(unittest.TestCase):
    def test_numeric_ranges(self):
        self.assertEqual(N("Children aged 7\u201311 read pp. 45\u201367 in 2019\u20132020 (10%\u201320%)."),
                         "Children aged 7 to 11 read pp. 45 to 67 in 2019 to 2020 (10% to 20%).")

    def test_parenthetical_em_dashes(self):
        self.assertEqual(N("The effect\u2014which was small\u2014vanished at follow-up."),
                         "The effect, which was small, vanished at follow-up.")
        self.assertEqual(N("The effect -- surprisingly -- vanished."), "The effect, surprisingly, vanished.")

    def test_spaced_en_dash_and_word_ranges(self):
        self.assertEqual(N("Mother\u2013child dyads \u2013 all from Leeds \u2013 took part."),
                         "Mother-child dyads, all from Leeds, took part.")

    def test_math_symbols(self):
        self.assertEqual(N("M \u2248 3.2 \u00b1 0.4, p \u2264 .05, d \u2265 0.5"),
                         "M approximately 3.2 plus or minus 0.4, p less than or equal to .05, "
                         "d greater than or equal to 0.5")

    def test_times_symbol(self):
        self.assertEqual(N("A 2 \u00d7 2 design with 3 \u00d7 10\u2075 cells at 40\u00d7 magnification."),
                         "A 2 by 2 design with 3 times 10\u2075 cells at 40 times magnification.")

    def test_arrow_degrees_tilde(self):
        self.assertEqual(N("pretest \u2192 posttest at 37 \u00b0C, rotated 45\u00b0, took ~20 ms"),
                         "pretest to posttest at 37 degrees Celsius, rotated 45 degrees, took approximately 20 ms")

    def test_percent_and_footnote_marks(self):
        self.assertEqual(N("Accuracy rose by 12%.\u2020 Results\u2021 were significant*."),
                         "Accuracy rose by 12%. Results were significant.")

    def test_spaced_hyphen_between_words_is_a_pause_but_minus_kept(self):
        self.assertEqual(N("Reading - like writing - is learned; 5 - 3 = 2"),
                         "Reading, like writing, is learned; 5 - 3 = 2")

    def test_whitespace_collapsed(self):
        self.assertEqual(N("Too    many   spaces ,here ( really )."), "Too many spaces,here (really).")

    def test_idempotent_on_typical_text(self):
        text = ("Introduction\nWorking memory (WM) supports reading com-\nprehension (Smith, 2019). "
                "Effects range 10\u201320% [3].\n\n2.1 Participants\nForty adults took part.")
        once = N(text)
        self.assertEqual(N(once), once)


# ---------------------------------------------------------------------------
# split_sentences
# ---------------------------------------------------------------------------

class SentenceSplitting(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(S("Reading is hard. Listening helps! Does it? Yes."),
                         ["Reading is hard.", "Listening helps!", "Does it?", "Yes."])

    def test_et_al_not_split(self):
        self.assertEqual(S("Smith et al. showed this. Smith et al. (2019) agreed. Smith et al. 2019 did too."),
                         ["Smith et al. showed this.", "Smith et al. (2019) agreed.", "Smith et al. 2019 did too."])

    def test_et_al_at_sentence_end_before_starter(self):
        self.assertEqual(S("This was shown by Smith et al. However, it failed to replicate."),
                         ["This was shown by Smith et al.", "However, it failed to replicate."])

    def test_eg_ie_not_split(self):
        self.assertEqual(S("Some tasks, e.g. Stroop and flanker, i.e. Conflict tasks, were used. Next we tested."),
                         ["Some tasks, e.g. Stroop and flanker, i.e. Conflict tasks, were used.", "Next we tested."])

    def test_etc_mid_sentence_and_at_end(self):
        self.assertEqual(S("Fruit, vegetables, etc. were rated. We used apples, pears, etc. The ratings varied."),
                         ["Fruit, vegetables, etc. were rated.", "We used apples, pears, etc.", "The ratings varied."])

    def test_latin_and_approximation_abbreviations(self):
        self.assertEqual(S("Group A vs. Group B differed, cf. Table 2. It took approx. 20 min, ca. 1850 style."),
                         ["Group A vs. Group B differed, cf. Table 2.", "It took approx. 20 min, ca. 1850 style."])

    def test_reference_abbreviations(self):
        text = ("See Fig. 3 and Figs. 4 and 5, Eq. 2, Eqs. 3 and 4, Tab. 1, Sec. 4, Ch. 2, "
                "No. 7, Vol. 12, pp. 4 and p. 9 for details. Done.")
        self.assertEqual(len(S(text)), 2)

    def test_reference_abbreviation_before_capital(self):
        self.assertEqual(S("Results appear in Fig. S1 and Eq. A3 below."), ["Results appear in Fig. S1 and Eq. A3 below."])

    def test_titles(self):
        self.assertEqual(S("Dr. Patel and Prof. Okafor met Mr. Li, Mrs. Hale and Ms. Ruiz at St. Andrews. Jr. staff came."),
                         ["Dr. Patel and Prof. Okafor met Mr. Li, Mrs. Hale and Ms. Ruiz at St. Andrews.",
                          "Jr. staff came."])

    def test_company_suffixes(self):
        self.assertEqual(S("Software came from Acme Inc. Microsoft Ltd. supplied laptops. The study ended."),
                         ["Software came from Acme Inc. Microsoft Ltd. supplied laptops.", "The study ended."])
        self.assertEqual(S("Funding came from Acme Ltd. The study ended."),
                         ["Funding came from Acme Ltd.", "The study ended."])

    def test_country_abbreviations(self):
        self.assertEqual(S("Data came from the U.S. Census Bureau and the U.K. Biobank. Most were adults."),
                         ["Data came from the U.S. Census Bureau and the U.K. Biobank.", "Most were adults."])
        self.assertEqual(S("The trial ran in the U.S. The results were mixed."),
                         ["The trial ran in the U.S.", "The results were mixed."])

    def test_degree_abbreviation(self):
        self.assertEqual(S("She holds a Ph.D. in linguistics. Her Ph.D. Thesis was cited."),
                         ["She holds a Ph.D. in linguistics.", "Her Ph.D. Thesis was cited."])

    def test_initials(self):
        self.assertEqual(S("J. R. R. Tolkien and C. S. Lewis were friends. They taught at Oxford."),
                         ["J. R. R. Tolkien and C. S. Lewis were friends.", "They taught at Oxford."])

    def test_label_letter_ends_sentence(self):
        self.assertEqual(S("Children were assigned to Condition A. Participants in Condition B waited."),
                         ["Children were assigned to Condition A.", "Participants in Condition B waited."])

    def test_decimals_and_versions(self):
        self.assertEqual(S("The mean was 3.5 on version 2.3.1 of the scale. It rose to 4.25."),
                         ["The mean was 3.5 on version 2.3.1 of the scale.", "It rose to 4.25."])

    def test_ellipsis_inside_sentence(self):
        self.assertEqual(S("The results were... surprising to everyone. Readers said \u201cit was [...] Hard work\u201d too."),
                         ["The results were... surprising to everyone.",
                          "Readers said \u201cit was [...] Hard work\u201d too."])
        self.assertEqual(S("It faded\u2026 Then it returned."), ["It faded\u2026", "Then it returned."])

    def test_numbered_headings(self):
        self.assertEqual(S("2.3.1. Methods We tested adults."), ["2.3.1. Methods We tested adults."])
        self.assertEqual(S("1. Introduction\n\n2.3.1 Methods\n\nWe tested adults."),
                         ["1. Introduction", "2.3.1 Methods", "We tested adults."])

    def test_closing_quotes_and_brackets(self):
        self.assertEqual(S("She said \u201cread it again.\u201d Then she left. (This was rare.) Next day was calm."),
                         ["She said \u201cread it again.\u201d", "Then she left.", "(This was rare.)",
                          "Next day was calm."])

    def test_split_before_digit_and_opening_quote(self):
        self.assertEqual(S("Attrition was 12%. 15 children left. \u201cIt was too long,\u201d one said."),
                         ["Attrition was 12%.", "15 children left.", "\u201cIt was too long,\u201d one said."])

    def test_no_split_before_lowercase(self):
        self.assertEqual(S("The value was 4. and then it changed."), ["The value was 4. and then it changed."])

    def test_paragraph_breaks_split(self):
        self.assertEqual(S("Abstract\n\nBackground without a full stop\n\nFinal sentence."),
                         ["Abstract", "Background without a full stop", "Final sentence."])

    def test_text_is_trimmed_but_otherwise_original(self):
        self.assertEqual(S("   Spaces  inside   are kept.   Next one.  "), ["Spaces  inside   are kept.", "Next one."])

    def test_empty(self):
        self.assertEqual(S(""), [])
        self.assertEqual(S(" \n\n "), [])

    def test_long_sentence_split_at_semicolons(self):
        clause = "participants read a passage aloud while their eye movements were recorded"
        text = "; ".join([clause] * 10) + "."
        parts = S(text, max_len=200)
        self.assertTrue(all(len(p) <= 200 for p in parts))
        self.assertTrue(all(p.endswith(";") for p in parts[:-1]))
        self.assertEqual(" ".join(parts).split(), text.split())

    def test_long_sentence_split_at_commas_then_spaces(self):
        text = ", ".join(["word " * 12] * 8).replace("  ", " ")
        parts = S(text, max_len=150)
        self.assertTrue(all(len(p) <= 150 for p in parts))
        self.assertTrue(parts[0].endswith(","))
        no_punct = "word " * 200
        parts = S(no_punct, max_len=100)
        self.assertTrue(all(len(p) <= 100 for p in parts))
        self.assertEqual(" ".join(parts).split(), no_punct.split())

    def test_unbreakable_token_hard_cut(self):
        parts = S("x" * 250, max_len=100)
        self.assertEqual([len(p) for p in parts], [100, 100, 50])

    def test_default_max_len_is_1500(self):
        text = ", ".join(["the children read the text"] * 120) + "."
        self.assertTrue(all(len(p) <= 1500 for p in S(text)))
        self.assertGreater(len(S(text)), 1)

    def test_invalid_max_len(self):
        with self.assertRaises(ValueError):
            S("text", max_len=0)


# ---------------------------------------------------------------------------
# chunk_sentences / chunk_text / prepare
# ---------------------------------------------------------------------------

def _sentences(lengths):
    return ["a" * (n - 1) + "." for n in lengths]


class Chunking(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(chunk_sentences([]), [])

    def test_first_chunk_small_later_chunks_large(self):
        sents = _sentences([100] * 40)
        chunks = chunk_sentences(sents, first_limit=250, limit=1500)
        self.assertEqual(chunks[0], [0, 1])
        self.assertEqual(len(chunks[1]), 14)  # 14 * 100 + 13 spaces = 1413

    def test_limits_respected_and_all_indices_once_in_order(self):
        sents = _sentences([37, 220, 15, 480, 90, 300, 1200, 60, 75, 33, 999, 12] * 5)
        chunks = chunk_sentences(sents, first_limit=250, limit=1500)
        self.assertEqual([i for c in chunks for i in c], list(range(len(sents))))
        for n, c in enumerate(chunks):
            cap = 250 if n == 0 else 1500
            self.assertLessEqual(len(chunk_text(sents, c)), cap)

    def test_oversize_sentence_gets_own_chunk(self):
        sents = _sentences([50, 2000, 50])
        self.assertEqual(chunk_sentences(sents, first_limit=250, limit=1500), [[0], [1], [2]])

    def test_long_first_sentence_alone_in_first_chunk(self):
        sents = _sentences([400, 50, 50])
        self.assertEqual(chunk_sentences(sents), [[0], [1, 2]])

    def test_invalid_limits(self):
        with self.assertRaises(ValueError):
            chunk_sentences(["a."], first_limit=0)
        with self.assertRaises(ValueError):
            chunk_sentences(["a."], limit=-5)

    def test_chunk_text_adds_pause_to_headings(self):
        sents = ["2.3 Participants", "Forty adults took part.", "Items included", "reading"]
        self.assertEqual(chunk_text(sents, [0, 1]), "2.3 Participants. Forty adults took part.")

    def test_chunk_text_collapses_whitespace(self):
        self.assertEqual(chunk_text(["One  \n two.", "Three."], [0, 1]), "One two. Three.")

    def test_prepare_pipeline(self):
        text = ("Abstract\nDyslexia is a common learning difference (Snowling, 2019). It affects decoding "
                "and spelling [1, 2].\n\n1 Introduction\nReading is a complex skill. " * 20)
        result = prepare(text)
        self.assertNotIn("Snowling", result.text)
        self.assertEqual([i for c in result.chunks for i in c], list(range(len(result.sentences))))
        self.assertLessEqual(len(chunk_text(result.sentences, result.chunks[0])), 250)
        for c in result.chunks[1:]:
            self.assertLessEqual(len(chunk_text(result.sentences, c)), 1500)

    def test_prepare_splits_long_first_sentence_at_clause(self):
        first = ("Although many adults with dyslexia develop effective compensatory strategies during "
                 "secondary school, the demands of doctoral study, with its dense theoretical texts and "
                 "long reading lists, often exceed what those strategies can support in practice "
                 "without additional assistive technology.")
        result = prepare(first + " Second sentence.")
        self.assertTrue(result.sentences[0].endswith(","))
        self.assertLessEqual(len(result.sentences[0]), 250)
        self.assertEqual(" ".join(result.sentences[:2]), first)


# ---------------------------------------------------------------------------
# Realistic end-to-end snippets
# ---------------------------------------------------------------------------

class RealisticSnippets(unittest.TestCase):
    def test_psychology_pdf_snippet(self):
        text = ("Working memory has long been considered central to reading compre-\n"
                "hension (Baddeley, 1986; Daneman & Carpenter, 1980). Swanson et al. (2009)\n"
                "reported a moderate association (r = .45, p < .001) across 30 studies.\n"
                "12 SWANSON ET AL.\n"
                "However, the effect was smaller in adults (see Peng et al., 2018, p. 52).\n")
        out = N(text)
        self.assertEqual(out, "Working memory has long been considered central to reading comprehension. "
                              "Swanson et al. reported a moderate association (r = .45, p < .001) across 30 "
                              "studies. However, the effect was smaller in adults.")
        self.assertEqual(len(S(out)), 3)

    def test_computer_science_snippet(self):
        text = ("3.2 Model Architecture\nWe fine-tune a pre-trained transformer [4, 7] with\n"
                "learning rate 3 \u00d7 10\u207b\u2075 for ~10 epochs. As in [12], the batch size is 32. "
                "Code: https://github.com/lab/model.")
        self.assertEqual(N(text).split("\n\n"),
                         ["3.2 Model Architecture",
                          "We fine-tune a pre-trained transformer with learning rate 3 times 10\u207b\u2075 for "
                          "approximately 10 epochs. As in reference 12, the batch size is 32. Code: link."])

    def test_biology_snippet(self):
        text = ("Cells were incubated at 37\u00b0C for 24\u201348 h. Expression of Sox2 increased\u00b3\u2074 "
                "while p53 levels fell (Fig. 2B). Approx. 5 \u00d7 10\u2074 cells were plated per well.")
        out = N(text)
        self.assertEqual(out, "Cells were incubated at 37 degrees Celsius for 24 to 48 h. Expression of Sox2 "
                              "increased while p53 levels fell (Fig. 2B). Approx. 5 times 10\u2074 cells were "
                              "plated per well.")
        self.assertEqual(len(S(out)), 3)

    def test_education_snippet_with_bullets(self):
        text = ("Teachers reported three benefits of the programme:\n"
                "\u2022 improved engagement during guided reading;\n"
                "\u2022 more confident oral contributions (Ofsted, 2019);\n"
                "\u2022 better homework completion.\n"
                "These are discussed in Sec. 5 below.")
        out = N(text)
        self.assertEqual(out.split("\n\n"),
                         ["Teachers reported three benefits of the programme:",
                          "improved engagement during guided reading;",
                          "more confident oral contributions;",
                          "better homework completion.",
                          "These are discussed in Sec. 5 below."])

    def test_web_page_paragraphs(self):
        text = ("Why dyslexia is not about intelligence\n\nDyslexia is a difference in how the brain "
                "processes written language. It is not linked to intelligence (BDA, 2023).\n\n"
                "Read more: https://www.bdadyslexia.org.uk/dyslexia")
        out = N(text)
        self.assertEqual(out.split("\n\n")[1], "Dyslexia is a difference in how the brain processes "
                                               "written language. It is not linked to intelligence.")
        self.assertNotIn("http", out)

    def test_large_input_is_fast(self):
        para = ("Phonological deficits are well-\ndocumented (Snowling, 2000; Ramus et al., 2003) and "
                "persist into adulthood [4\u20136]. See https://example.org/x for data.\n")
        text = para * 3000  # about 600 KB
        start = time.perf_counter()
        out = N(text)
        S(out)
        self.assertLess(time.perf_counter() - start, 10.0)
        self.assertNotIn("Snowling", out)

    def test_pathological_input_does_not_hang(self):
        nasty = ("(" + "Aa " * 120 + ")") * 50 + "[" * 500 + "a" * 20000 + "." * 3000
        start = time.perf_counter()
        N(nasty)
        S(nasty)
        self.assertLess(time.perf_counter() - start, 10.0)


# ---------------------------------------------------------------------------
# Regression tests from adversarial testing (each one failed before its fix)
# ---------------------------------------------------------------------------

class RegressionStatistics(unittest.TestCase):
    def test_list_of_leading_dot_decimals_not_merged(self):
        # was "Correlations were .32.45, ..." (", ." cleanup ate the comma)
        self.assertEqual(N("Correlations were .32, .45, and .51, respectively."),
                         "Correlations were .32, .45, and .51, respectively.")
        self.assertEqual(N("The model fit well (CFI = .95, 90% CI [.03, .07])."),
                         "The model fit well (CFI = .95, 90% CI [.03, .07]).")

    def test_integer_confidence_interval_is_not_a_citation(self):
        # was deleted as the numeric citation "[1, 10]"
        self.assertEqual(N("The 95% CI [1, 10] excluded zero."), "The 95% CI [1, 10] excluded zero.")
        self.assertEqual(N("Scores were higher, 95% CI [12, 15]."), "Scores were higher, 95% CI [12, 15].")

    def test_true_minus_sign_is_never_a_pause(self):
        # U+2212 between variables became ", " ("(y_i, z_i)")
        self.assertEqual(N("We minimise (y_i − ŷ_i)² and x - y."),
                         "We minimise (y_i - ŷ_i)² and x - y.")
        self.assertEqual(N("Temperatures ranged from −10 to 5 and 10–−5."),
                         "Temperatures ranged from -10 to 5 and 10 to -5.")

    def test_spaced_hyphen_after_single_letter_word_is_still_a_pause(self):
        self.assertEqual(N("Group A - the control group - waited."), "Group A, the control group, waited.")

    def test_command_line_flags_kept(self):
        # "--input" was read as a dash pause
        self.assertEqual(N("Run main.py --input data.csv --verbose to start."),
                         "Run main.py --input data.csv --verbose to start.")


class RegressionCitations(unittest.TestCase):
    def test_ieee_bracket_range_read_as_range(self):
        # was "references 2 and 4"
        self.assertEqual(N("It was extended in [2]-[4]."), "It was extended in references 2 to 4.")

    def test_names_starting_with_non_ascii_capitals(self):
        self.assertEqual(N("Łukasz Kowalski and Żaneta Świątek (2021) disagreed."),
                         "Łukasz Kowalski and Żaneta Świątek disagreed.")
        self.assertEqual(N("İnalcık (1973) argued this. Ó Ríordáin (2010) agreed."),
                         "İnalcık argued this. Ó Ríordáin agreed.")
        self.assertEqual(N("Language shapes thought (Müller, 2018; Åström & Öztürk, 2020)."),
                         "Language shapes thought.")

    def test_caps_running_header_with_non_ascii_name(self):
        self.assertEqual(N("Children improved\n12 MÜLLER ET AL.\nin decoding over time."),
                         "Children improved in decoding over time.")

    def test_quote_followed_by_citation_has_one_full_stop(self):
        # was '"It works.". Then'
        self.assertEqual(N("He said, “It works.” (Smith, 2019). Then he left."),
                         "He said, “It works.” Then he left.")

    def test_chicago_note_numbers_after_closing_quotes(self):
        out = N("As Foucault put it, “visibility is a trap.”14 This idea shaped later work.")
        self.assertEqual(out, "As Foucault put it, “visibility is a trap.” This idea shaped later work.")
        self.assertEqual(len(S(out)), 2)
        self.assertEqual(N("the “docile body”15 is central."), "the “docile body” is central.")


class RegressionSentences(unittest.TestCase):
    def test_et_al_before_any_capitalised_word_ends_sentence(self):
        self.assertEqual(S("This was shown by Smith et al. A replication failed."),
                         ["This was shown by Smith et al.", "A replication failed."])
        self.assertEqual(S("This was shown by Müller et al. Ngũgĩ disagreed."),
                         ["This was shown by Müller et al.", "Ngũgĩ disagreed."])
        self.assertEqual(S("Smith et al. 2019 reported this."), ["Smith et al. 2019 reported this."])

    def test_et_al_sentence_end_survives_citation_removal(self):
        # "Smith et al. (2019). Later" -> "Smith et al. Later": the boundary was lost
        self.assertEqual(S(N("This was first noted by Smith et al. (2019). Later work agreed.")),
                         ["This was first noted by Smith et al.", "Later work agreed."])

    def test_abbreviation_before_article_a(self):
        self.assertEqual(S("He moved to the U.S. A year later he returned."),
                         ["He moved to the U.S.", "A year later he returned."])
        self.assertEqual(S("It was made by Apple Inc. A new version came later."),
                         ["It was made by Apple Inc.", "A new version came later."])
        self.assertEqual(S("J. A. Smith and B. A. Jones met."), ["J. A. Smith and B. A. Jones met."])

    def test_street_abbreviation(self):
        self.assertEqual(S("We met on Main St. The day was sunny."), ["We met on Main St.", "The day was sunny."])
        self.assertEqual(S("We met in St. Louis. Then St. Andrews."), ["We met in St. Louis.", "Then St. Andrews."])

    def test_lower_case_scientific_sentence_starts(self):
        self.assertEqual(S("The effect was large. mRNA levels rose. p-values were small. iPhones were used."),
                         ["The effect was large.", "mRNA levels rose.", "p-values were small.", "iPhones were used."])
        self.assertEqual(S("Many cells, e.g. mRNA-rich ones, grew."), ["Many cells, e.g. mRNA-rich ones, grew."])

    def test_no_split_inside_short_parentheses(self):
        self.assertEqual(S("The effect was large (this was expected. It replicated.) and stable."),
                         ["The effect was large (this was expected. It replicated.) and stable."])
        self.assertEqual(S("We tested adults (aged 18 to 65. Most were students). Then children."),
                         ["We tested adults (aged 18 to 65. Most were students).", "Then children."])
        # an unbalanced bracket protects nothing
        self.assertEqual(len(S("The effect (was large. It replicated. Then we stopped.")), 3)


class RegressionLayout(unittest.TestCase):
    def test_hyphen_join_does_not_create_a_heading(self):
        # was "The socalled\n\nMatthew effect is real." (spoken "The socalled. Matthew ...")
        out = N("The so-\ncalled Matthew effect is real.")
        self.assertEqual(out, "The so-called Matthew effect is real.")
        self.assertEqual(N(out), out)

    def test_hyphen_compounds_across_lines(self):
        self.assertEqual(N("the twenty-\nfirst century, a peer-\nreviewed journal, a mother-\nchild dyad and an in-\n"
                           "depth analysis"),
                         "the twenty-first century, a peer-reviewed journal, a mother-child dyad and an "
                         "in-depth analysis")
        self.assertEqual(N("a so-\ncial and trans-\nparent in-\ndependent so-\nlution"),
                         "a social and transparent independent solution")

    def test_lettered_and_roman_lists(self):
        self.assertEqual(N("Excluded if they:\na) had a hearing impairment;\nb) were bilingual; or\nc) had ADHD.")
                         .split("\n\n"),
                         ["Excluded if they:", "a) had a hearing impairment;", "b) were bilingual; or", "c) had ADHD."])
        self.assertEqual(len(N("The model has three parts:\ni. an encoder,\nii. a decoder, and\n"
                               "iii. an attention layer.").split("\n\n")), 4)
        self.assertEqual(N("i. first point\nii. second point\niii. third point").split("\n\n"),
                         ["i. first point", "ii. second point", "iii. third point"])
        self.assertEqual(N("Participants aged 7, 8, and\n9. Most were boys."), "Participants aged 7, 8, and 9. Most were boys.")

    def test_word_private_use_and_o_bullets(self):
        self.assertEqual(N(" First item\n Second item").split("\n\n"), ["First item", "Second item"])
        self.assertEqual(N("o First item\no Second item").split("\n\n"), ["First item", "Second item"])
        self.assertEqual(N("o is a letter.\nThe next line."), "o is a letter. The next line.")

    def test_private_use_input_cannot_reach_internal_placeholders(self):
        # U+E001 used to become the word "link"; U+E010..U+E011 could pull in a saved URL
        self.assertEqual(N("Text  with  marks."), "Text with marks.")
        self.assertEqual(N("Data at https://x.org/a and 0 here.", skip_urls=False),
                         "Data at https://x.org/a and 0 here.")

    def test_short_heading_before_short_body(self):
        self.assertEqual(N("1. Introduction\nDyslexia is common.").split("\n\n"), ["1. Introduction", "Dyslexia is common."])
        self.assertEqual(N("Participants and Procedure\nForty adults took part.").split("\n\n"),
                         ["Participants and Procedure", "Forty adults took part."])
        self.assertEqual(N("Data were collected by the National Health\nService in 2019 and 2020."),
                         "Data were collected by the National Health Service in 2019 and 2020.")
        # justified column: a full-width title-case line is not a heading
        column = ("The previous paragraph ended with a sentence.\n"
                  "The University of Leeds Research Ethics\n"
                  "Committee approved the study and all the\n"
                  "participants gave written consent to it\n"
                  "before the first session began in the lab\n"
                  "and were debriefed at the end of the day.")
        self.assertEqual(len(N(column).split("\n\n")), 1)

    def test_numbered_all_caps_heading_is_not_a_running_header(self):
        text = ("the end of the introduction.\n2 METHODS AND MATERIALS\n"
                "We recruited forty adults from a university service in the north of England.")
        self.assertEqual(N(text).split("\n\n")[1], "2 METHODS AND MATERIALS")
        repeated = "the effect was\n4 READING AND ATTENTION\nlarge. It was\n6 READING AND ATTENTION\nrobust."
        self.assertEqual(N(repeated), "the effect was large. It was robust.")

    def test_pdf_table_one_cell_per_line_keeps_integers(self):
        text = "Measure\nT1\nT2\nAccuracy\n12\n15\nSpeed\n31\n29"
        self.assertEqual(N(text).split("\n\n"), ["Measure", "T1", "T2", "Accuracy", "12", "15", "Speed", "31", "29"])

    def test_manuscript_line_numbers_removed(self):
        text = ("121 Dyslexia is a common learning difference that affects\n"
                "122 reading and spelling. Adults with dyslexia often report\n"
                "123 fatigue when reading for long periods of time.\n124 Introduction\n"
                "125 Prior work has examined this in children.")
        self.assertEqual(N(text).split("\n\n"),
                         ["Dyslexia is a common learning difference that affects reading and spelling. Adults with "
                          "dyslexia often report fatigue when reading for long periods of time.",
                          "Introduction", "Prior work has examined this in children."])
        every_fifth = ("5 Adults with dyslexia often report\nfatigue when reading for long\nperiods of time and also\n"
                       "difficulties with taking notes\nduring lectures and seminars.\n10 Prior work examined children.")
        self.assertTrue(N(every_fifth).startswith("Adults with dyslexia"))
        self.assertIn(" Prior work examined children.", N(every_fifth))
        timeline = "2019 Study designed\n2020 Data collected\n2021 Paper published"
        self.assertEqual(N(timeline).split("\n\n"), ["2019 Study designed", "2020 Data collected", "2021 Paper published"])

    def test_spaced_bar_heading_separator(self):
        self.assertEqual(N("1 | INTRODUCTION\nDyslexia is a common learning difference affecting reading skills.")
                         .split("\n\n")[0], "1, INTRODUCTION")
        self.assertEqual(N("The value |x| and P(A|B) matter."), "The value |x| and P(A|B) matter.")
        self.assertEqual(N("| Group | n |\n| Dyslexia | 42 |"), "Group, n, Dyslexia, 42")

    def test_url_in_angle_brackets_is_part_of_the_sentence(self):
        self.assertEqual(N("See <https://example.com> for details."), "See link for details.")


class RegressionPdfJoins(unittest.TestCase):
    """Line joins that went wrong on text copied from an arXiv PDF."""

    def test_numbered_heading_is_not_glued_onto_a_url(self):
        # was "... available at link Related Work There is ..." (the "2" went into the URL)
        text = ("The code is available at https://github.com/\ngoogle-research/bert.\n2 Related Work\n"
                "There is a long history of pre-training.")
        self.assertEqual(N(text).split("\n\n"),
                         ["The code is available at link.", "2 Related Work",
                          "There is a long history of pre-training."])
        self.assertEqual(N(text, skip_urls=False).split("\n\n")[:2],
                         ["The code is available at https://github.com/google-research/bert.", "2 Related Work"])
        unbroken = "Code is at https://github.com/lab/tool.\n2 Related Work\nThere is a long history."
        self.assertEqual(N(unbroken).split("\n\n"), ["Code is at link.", "2 Related Work", "There is a long history."])

    def test_sub_numbered_heading_and_url_ending_in_slash(self):
        self.assertEqual(N("Code at https://example.org/page.\n2.1 Data Sets\nWe used three data sets.").split("\n\n"),
                         ["Code at link.", "2.1 Data Sets", "We used three data sets."])
        self.assertEqual(N("Code at https://example.org/\n3 Methods\nWe recruited forty adults.").split("\n\n"),
                         ["Code at link", "3 Methods", "We recruited forty adults."])

    def test_real_url_continuations_still_join(self):
        self.assertEqual(N("See the preprint at https://arxiv.org/abs/1810.\n04805 for details."),
                         "See the preprint at link for details.")
        self.assertEqual(N("See https://osf.io/\nx7k2q/ for the data.", skip_urls=False),
                         "See https://osf.io/x7k2q/ for the data.")

    def test_footnote_number_stuck_to_url(self):
        # was read out raw: no word boundary between "1" and "https"
        text = ("Each token attends only to context to its left.\n1https://github.com/tensorflow/tensor2tensor\n"
                "We primarily report results on two model sizes.")
        self.assertEqual(N(text), "Each token attends only to context to its left.\n\n"
                                  "We primarily report results on two model sizes.")
        self.assertEqual(N("It was shown before. 10http://example.org/x We then ran it."),
                         "It was shown before. We then ran it.")
        self.assertEqual(N("Data are online.\n12www.example.org/data\nWe then ran the model."),
                         "Data are online.\n\nWe then ran the model.")
        self.assertEqual(N("See the guide (1https://example.org/guide) for details."), "See the guide for details.")

    def test_footnote_url_is_removed_not_read_as_link(self):
        text = ("Every token can only attend to context to its left.4\n"
                "1https://github.com/tensorflow/tensor2tensor\n"
                "2http://nlp.seas.harvard.edu/2018/04/03/attention.html\n"
                "3In all cases we set the size to 4H.")
        self.assertEqual(N(text), "Every token can only attend to context to its left.\n\n3In all cases we set the size to 4H.")

    def test_footnote_url_kept_when_urls_are_read(self):
        text = "Its left.\n1https://github.com/tensorflow/tensor2tensor\nWe report results."
        self.assertEqual(N(text, skip_urls=False),
                         "Its left.\n\n1https://github.com/tensorflow/tensor2tensor\n\nWe report results.")
        # a lone footnote URL is still not silenced
        self.assertEqual(N("1https://github.com/google-research/bert"), "1https://github.com/google-research/bert")

    def test_number_before_a_spaced_url_is_kept(self):
        self.assertEqual(N("Version 2 https://example.org/x is out."), "Version 2 link is out.")
        self.assertEqual(N("Values at 10.1037/abc0000123 are shown."), "Values at link are shown.")

    def test_footnote_url_inside_a_running_line_is_removed_not_read_as_link(self):
        # after a word an ordinary URL is read as "link"; a footnote URL there is still removed
        self.assertEqual(N("The batch size was 32 https://example.org/pricing and more."),
                         "The batch size was 32 link and more.")
        for url in ("13https://example.org/pricing", "13HTTPS://EXAMPLE.ORG/PRICING",
                    "13ftp://ftp.example.org/pub", "13www.example.org/pricing", "13https://x.org/10.1037/abc"):
            with self.subTest(url=url):
                self.assertEqual(N(f"The batch size was 32\n{url}\nand the rate was small."),
                                 "The batch size was 32 and the rate was small.")

    def test_footnote_url_in_square_brackets_and_doi_link(self):
        self.assertEqual(N("A footnote [2https://example.org/x] here."), "A footnote here.")
        # was "5https://doi.org/" read out: the DOI pattern took the tail and left the head behind
        for sc in (True, False):
            with self.subTest(skip_citations=sc):
                self.assertEqual(
                    N("Shown before.\n5https://doi.org/10.1037/abc0000123\nWe then ran it.", skip_citations=sc),
                    "Shown before.\n\nWe then ran it.")

    def test_longer_number_before_url_is_not_a_footnote_mark(self):
        # only 1-3 digits count as a footnote mark; a year stays with the sentence
        self.assertEqual(N("Data for 2019www.example.org/data are online."), "Data for link are online.")

    def test_heading_after_url_ending_in_question_mark(self):
        # "?" and ":" can end the sentence, so the heading after them is not part of the URL
        self.assertEqual(N("Have you seen https://x.org/a?\n2 Methods\nWe did.").split("\n\n"),
                         ["Have you seen link?", "2 Methods", "We did."])
        self.assertEqual(N("Is it at https://x.org/a?\nIV. Results\nWe found it.").split("\n\n"),
                         ["Is it at link?", "IV. Results", "We found it."])

    def test_hyphen_not_joined_across_blank_line_onto_capital(self):
        # was "Without TriviaQA fine-Table 2: ..."
        text = "Without TriviaQA fine-\n\nTable 2: SQuAD 1.1 results.\n\ntuning data, we lose only 0.1 F1."
        self.assertEqual(N(text).split("\n\n"),
                         ["Without TriviaQA fine-", "Table 2: SQuAD 1.1 results.", "tuning data, we lose only 0.1 F1."])
        self.assertEqual(N("the model architec-\n\n2.3 Pre-training\n\nWe train it.").split("\n\n")[:2],
                         ["the model architec-", "2.3 Pre-training"])
        self.assertEqual(N("Ein Über-\n\nÜbersicht folgt."), "Ein Über-\n\nÜbersicht folgt.")

    def test_hyphen_still_joined_across_blank_line_onto_lower_case(self):
        self.assertEqual(N("Reading compre-\n\nhension improved."), "Reading comprehension improved.")
        self.assertEqual(N("The so-\n\n\n\ncalled effect is real."), "The so-called effect is real.")
        self.assertEqual(N("ein über-\n\nblick"), "ein überblick")
        # a single line break still keeps the hyphen before a capital or digit
        self.assertEqual(N("during COVID-\n19 and in non-\nHispanic samples"), "during COVID-19 and in non-Hispanic samples")


class RegressionWholeSelection(unittest.TestCase):
    def test_selection_that_is_only_a_number_citation_or_url_is_not_silenced(self):
        self.assertEqual(N("42"), "42")
        self.assertEqual(N("Page 3 of 12"), "Page 3 of 12")
        self.assertEqual(N("(Smith, 2019)"), "(Smith, 2019)")
        self.assertEqual(N("https://example.com"), "https://example.com")
        self.assertEqual(prepare("(Smith, 2019)").sentences, ["(Smith, 2019)"])
        self.assertEqual(N("• • •"), "")


class RegressionPerformance(unittest.TestCase):
    """normalize + split_sentences on 30,000 characters must stay well under 0.2 s."""

    LIMIT = 0.2

    @staticmethod
    def _best_time(text, runs=5):  # best of several runs: robust to a busy machine
        best = float("inf")
        for _ in range(runs):
            start = time.perf_counter()
            S(N(text))
            best = min(best, time.perf_counter() - start)
        return best

    @staticmethod
    def _fit(unit, size=30000):
        return (unit * (size // len(unit) + 1))[:size]

    def test_realistic_30k_document(self):
        para = ("Working memory has long been considered central to reading compre-\n"
                "hension (Baddeley, 1986; Daneman & Carpenter, 1980). Swanson et al. (2009)\n"
                "reported a moderate association (r = .45, p < .001, 95% CI [.32, .58]) across 30 studies [4–6].\n"
                "12 SWANSON ET AL.\n"
                "However, the effect was smaller in adults (see Peng et al., 2018, p. 52), e.g. Fig. 2 shows this.\n"
                "See https://doi.org/10.1037/xge0000123 for data; F(2, 57) = 4.31, p = .018, η² = .13.\n\n")
        self.assertLess(self._best_time(self._fit(para)), self.LIMIT)

    def test_pathological_30k_documents(self):
        # "....." took 13 s, "A. A. A." 23 s and "Smith et al. " 7.7 s before the fix
        for unit in (".", "A. ", "Smith et al. ", "... ", "Ab\n", "A.\n\n", "• item\n", "(Aa Bb, ",
                     "e.g. ", "x" * 50 + " ", "1. Item\n", "ab-\n", "12,13,", "[1]-[2]-", "mRNA. ",
                     "ab-\n\nAb ", "ab-\n\n\n\n", "1https://a.org/b ", "https://a.org/b.\n2 A\n"):
            with self.subTest(unit=unit[:12]):
                self.assertLess(self._best_time(self._fit(unit)), self.LIMIT)

class NormalizePairTests(unittest.TestCase):
    def test_shown_keeps_citations_and_links(self):
        pairs = normalize_pair("Reading is hard (Smith, 2019). See https://osf.io/x for data.\n\n(Lee, 2001)")
        self.assertEqual(pairs, [("Reading is hard (Smith, 2019). See https://osf.io/x for data.",
                                  "Reading is hard. See link for data."),
                                 ("(Lee, 2001)", "")])

    def test_flags_off_give_identical_pairs(self):
        self.assertEqual(normalize_pair("A (Smith, 2019) b.", skip_citations=False, skip_urls=False),
                         [("A (Smith, 2019) b.", "A (Smith, 2019) b.")])

    def test_nothing_left_reads_the_citation(self):
        self.assertEqual(normalize_pair("(Smith, 2019)"), [("(Smith, 2019)", "(Smith, 2019)")])
        self.assertEqual(normalize_pair("   \n "), [])
        with self.assertRaises(TypeError):
            normalize_pair(None)


class ReferencesTests(unittest.TestCase):
    BODY = "\n".join(f"Body line {i} of the paper." for i in range(30))

    def test_last_heading_in_the_last_part(self):
        text = "References are discussed below.\n" + self.BODY + "\nReferences\nLee, A. 2001. A book."
        at = references_start(text)
        self.assertEqual(text[at:].split("\n")[0], "References")
        for heading in ("7 References", "REFERENCES", "Bibliography", "Works Cited", "Literature Cited",
                        "IV. References", "Reference list"):
            text = self.BODY + "\n" + heading + "\nLee, A. 2001."
            self.assertIsNotNone(references_start(text), heading)

    def test_too_early_or_missing(self):
        self.assertIsNone(references_start("References\n" + self.BODY))
        self.assertIsNone(references_start(self.BODY))
        self.assertIsNone(references_start(self.BODY + "\nThe references below are short."))

    def test_end_at_an_appendix(self):
        refs = "References\nLee, A. 2001. A book.\nSmith, B. 2019. A paper.\n"
        for heading in ("Appendix A. More results", "Appendices", "Supplementary Material"):
            text = refs + heading + "\nProse again."
            self.assertEqual(text[references_end(text, 0):].split("\n")[0], heading)
        text = refs + "A Additional Details\nSome prose.\nA.1 Training\nMore prose."
        self.assertEqual(text[references_end(text, 0):].split("\n")[0], "A Additional Details")
        text = refs + "A Study of Reading. Journal, 2019.\nNo appendix here."
        self.assertEqual(references_end(text, 0), len(text))


class SpokenFormsTests(unittest.TestCase):
    def test_found_with_capitals_kept(self):
        text = "Fig. 2, figs. 3 and Eq. (4); e.g. this, E.g. that, i.e. so; approx. 5 on pp. 6."
        self.assertEqual([(text[a:b], said) for a, b, said in spoken_forms(text)],
                         [("Fig.", "Figure"), ("figs.", "figures"), ("Eq.", "Equation"), ("e.g.", "for example"),
                          ("E.g.", "For example"), ("i.e.", "that is"), ("approx.", "approximately"),
                          ("pp.", "pages")])

    def test_only_before_what_they_name(self):
        self.assertEqual(spoken_forms("A fig. tree. The eq. sign. Mrs. pp. Pop."), [])
        self.assertEqual(spoken_forms(""), [])


if __name__ == "__main__":
    unittest.main()

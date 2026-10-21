"""academic_text - prepare academic prose for a text-to-speech voice.

Part of "Saylight" (Windows 11, edge-tts en-US-AvaNeural).  Standard library
only (Python 3.14).  The goal is comfortable, faithful listening for long
sessions: remove things a listener cannot use (PDF debris, citations, URLs),
make symbols speakable, and never silently drop real prose.

Public API
    normalize(text, *, skip_citations=True, skip_urls=True) -> str
    normalize_pair(text, *, skip_citations=True, skip_urls=True) -> [(shown, spoken)]
    split_sentences(text, *, max_len=1500) -> list[str]
    chunk_sentences(sentences, first_limit=250, limit=1500) -> list[list[int]]

Convenience helpers
    chunk_text(sentences, chunk) -> str     the string to synthesise for a chunk
    references_start(text), references_end(text, start)   a file's reference list
    spoken_forms(text) -> [(start, end, said)]            "e.g." said as "for example"
    prepare(text, ...) -> Prepared          normalize + split + chunk in one call

Design summary (details next to each section below)
  * normalize() works in three layers: characters (ligatures, invisible
    characters, odd spaces, private-use glyphs), lines (manuscript line
    numbers, page numbers, running headers, hyphenated line breaks, broken
    URLs, headings, bullets, lists, paragraph joining) and inline text
    (URLs, citations, symbols, tidy-up).
  * A selection that would clean to nothing (only a citation, URL or page
    number) is returned with citations/URLs kept rather than as silence.
  * Everything is linear-time: no regex can backtrack catastrophically and
    sentence splitting looks back a bounded window, so 30,000 characters of
    even pathological input ("....", "A. A. A.") take a few hundredths of a
    second.
  * Output paragraphs are separated by exactly "\\n\\n" and contain no other
    newlines.  Headings and bullet items become their own paragraphs so the
    sentence splitter (and the voice) treat them as separate units.
  * When a heuristic is unsure it keeps the text: hearing a stray word is
    better than silently losing content.
  * Over-long sentences are split inside split_sentences() (at ';', then
    ',' ':' or a dash, then spaces) so every list entry is <= max_len.  The
    pieces are ordinary consecutive entries; chunk_sentences() therefore only
    ever groups whole entries and returns plain indices.
"""

import re
import unicodedata
from typing import NamedTuple

__all__ = [
    "normalize", "normalize_pair", "split_sentences", "chunk_sentences", "chunk_text",
    "prepare", "Prepared", "references_start", "references_end", "spoken_forms",
]

_OPEN_QUOTES = "\"'\u201c\u2018\u00ab\u201e\u201a"
_CLOSERS = "\"'\u2019\u201d\u00bb)]}"
_ENDS_SENTENCE_RE = re.compile(r"[.!?:;][\"'\u2019\u201d)\]]*$")
_ENDS_CLAUSE_RE = _ENDS_SENTENCE_RE
# Upper-case letters for regexes: ASCII plus accented Latin, Greek and
# Cyrillic capitals, so names such as \u015awi\u0105tek, \u00c5str\u00f6m or
# \u00d6zt\u00fcrk count as capitalised words ([A-Z] alone would miss them).
_UC = "A-Z" + "".join(
    re.escape(c) for c in map(chr, range(0xC0, 0x530)) if c.isupper()
) + "".join(re.escape(c) for c in map(chr, range(0x1E00, 0x1F00)) if c.isupper())

# Private-use code points are reserved for the module's own placeholders (see
# _DROP/_LINK, _CONT, _protect_urls), so they are removed from the input.  The
# Symbol/Wingdings bullets that Word-made PDFs paste as private-use characters
# become an ordinary bullet first.
_PUA_BULLETS = str.maketrans(dict.fromkeys(
    "\uf0b7\uf0a7\uf076\uf0d8\uf0fc\uf0a8\uf0e0\uf06e\uf075\uf071\uf0de\uf0f0\uf0be\uf0d0", "\u2022"))
_PUA_RE = re.compile("[\ue000-\uf8ff]")

# ---------------------------------------------------------------------------
# 1. Character level
# ---------------------------------------------------------------------------

_LIGATURES = str.maketrans({
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
})
# U+2212 (a real minus sign) is kept until the symbol stage so that it is
# never mistaken for a hyphen or a parenthetical dash ("y \u2212 x").
_CHAR_MAP = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "\u2013", "\u2015": "\u2014",
    "\ufe63": "\u2212", "\uff0d": "-",
    "\u2028": "\n", "\u2029": "\n\n", "\x85": "\n", "\x0b": "\n", "\x0c": "\n",
    "\t": " ", "\u223c": "~", "\u2a7d": "\u2264", "\u2a7e": "\u2265",
})
_SOFT_HYPHEN_BREAK_RE = re.compile("\u00ad *\n *")
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060-\u2064\ufeff\u180e]")
_SPACES_RE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
_CONTROL_RE = re.compile("[\x00-\x08\x0e-\x1f\x7f]")
_CID_RE = re.compile(r"\(cid:\d+\)")


def _clean_chars(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # NFC (not NFKC): NFKC would also flatten superscripts such as m\u00b2.
    text = unicodedata.normalize("NFC", text)
    text = _PUA_RE.sub("", text.translate(_PUA_BULLETS))
    text = text.translate(_LIGATURES).translate(_CHAR_MAP)
    text = _SOFT_HYPHEN_BREAK_RE.sub("", text)  # discretionary hyphen: always join
    text = text.replace("\u00ad", "")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _SPACES_RE.sub(" ", text)
    text = _CONTROL_RE.sub("", text)
    return _CID_RE.sub("", text)


# ---------------------------------------------------------------------------
# 2. Page furniture: page numbers, running headers, boilerplate lines
# ---------------------------------------------------------------------------
# Manuscript line numbers ("121 text\n122 text", or every fifth line) are
# stripped first (see _strip_line_numbers).  Nothing is dropped from a
# one-line selection.  A line is dropped when it is
#   * only a page number ("12", "Page 3 of 12", "- 4 -", "xii"), except a
#     bare number within three lines of another number-only line: that is a
#     PDF table pasted one cell per line;
#   * known boilerplate ("Downloaded from ...", "Running head: ...",
#     "(c) 2019 ...", "Author manuscript ...", arXiv side stamps);
#   * an ALL-CAPS header with a page number ("12 SMITH ET AL.").  A small
#     leading number without ET AL. between two sentences is a numbered
#     heading ("2 METHODS AND MATERIALS") and is kept;
#   * a journal header ("Journal of X, 2019, Vol. 111, No. 3, 45-67");
#   * a short line repeated in the selection (ignoring a page number at
#     either edge) where at least one copy carries a page number or sits
#     mid-sentence (previous line unfinished, next line lower-case).
#     Section headings ("General Discussion") repeat too, but they never sit
#     mid-sentence, so they survive;
#   * a single short line with a page number at its edge, sitting
#     mid-sentence and clearly shorter than its neighbours.

_PAGE_NUMBER_LINE_RE = re.compile(
    r"^(?:(?:page|pg\.?|p\.)\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$|^[-\u2013\u2014]\s*\d{1,4}\s*[-\u2013\u2014]$",
    re.I)
_ROMAN_PAGE_RE = re.compile(r"^(?=[ivxl]+$)x{0,3}(?:ix|iv|v?i{0,3})$")
_BOILERPLATE_LINE_RE = re.compile(r"""^(?:
      downloaded\s+from\b.*
    | running\s+head\s*:.*
    | this\s+(?:article|document)\s+is\s+protected\s+by\s+copyright.*
    | author\s+manuscript\b.*
    | accepted\s+(?:article|manuscript)\b.{0,80}
    | (?:copyright\s*)?(?:\u00a9|\(c\))\s*(?:\d{4}|the\s+authors?).{0,150}
    | copyright\s+(?:\u00a9\s*)?\d{4}\b.{0,150}
    | all\s+rights\s+reserved\.?
    | arxiv:\s?\d{4}\.\d{4,5}(?:v\d+)?\b.{0,60}
    | electronic\s+copy\s+available\s+at\b.*
    | (?:\S.{0,60}\s)?page\s+\d{1,4}\s+of\s+\d{1,4}\b.{0,60}
)$""", re.I | re.X)
_CAPS_WORD = rf"[{_UC}][{_UC}'\u2019.,&:\-]*"
_CAPS_HEADER_RE = re.compile(
    rf"^(?:(?P<lead>\d{{1,4}})\s+{_CAPS_WORD}(?:\s+(?:{_CAPS_WORD}|&))+|{_CAPS_WORD}(?:\s+(?:{_CAPS_WORD}|&))+\s+\d{{1,4}})$")
_VOL_RE = re.compile(r"\bVol(?:\.|ume)\s*\d+", re.I)
_JOURNAL_EXTRA_RE = re.compile(r"\bNo\.\s*\d+|\b(?:19|20)\d{2}\b|\d+\s*[\u2013-]\s*\d+|\bpp\.\s*\d+", re.I)
_EDGE_NUM_RE = re.compile(r"^(?:\d{1,4}\s+(?P<a>\D.*?)|(?P<b>.*?\D)\s+\d{1,4})$")
_CELL_NUM_RE = re.compile(r"[-+−]?(?:\d+(?:[.,]\d+)*|\.\d+)%?")
_MINOR_WORDS = frozenset("of and the in for on at to & a an".split())


def _is_journal_header(s):
    if len(s) > 140 or s.endswith(".") or not _VOL_RE.search(s) or not _JOURNAL_EXTRA_RE.search(s):
        return False
    words = [w for w in s.split() if w[0].isalpha() and w.lower() not in _MINOR_WORDS]
    lower = sum(1 for w in words if w[0].islower())
    return lower <= 0.2 * max(1, len(words))


_LINE_NO_RE = re.compile(r"^(\d{1,5})\s+(?=\S)")


def _strip_line_numbers(lines):
    """Remove manuscript line numbers ("121 Dyslexia is ...\\n122 reading ...").

    Numbered lines form a chain when each number advances by exactly the
    distance to the previous numbered line (counting blank lines or not, as
    Word and LaTeX differ).  A chain of three or more (two or more when every
    fifth or tenth line is numbered) is line numbering, not content.
    """
    numbered = [(i, int(m.group(1))) for i, s in enumerate(lines) if (m := _LINE_NO_RE.match(s))]
    if len(numbered) < 2:
        return lines
    nb_before = []  # non-blank lines before index i
    count = 0
    for s in lines:
        nb_before.append(count)
        count += bool(s)

    def follows(a, b):
        (i, x), (j, y) = a, b
        return nb_before[j] - nb_before[i] <= y - x <= j - i

    strip = set()
    chain = [numbered[0]]
    for item in numbered[1:] + [None]:
        if item is not None and follows(chain[-1], item):
            chain.append(item)
            continue
        step = chain[-1][1] - chain[0][1] if len(chain) > 1 else 0
        years = all(1800 <= x <= 2099 for _, x in chain)  # a timeline "2019 ...\n2020 ..." is content
        if not years and (len(chain) >= 3 or (len(chain) == 2 and step >= 5
                                               and all(x % 5 == 0 for _, x in chain))):
            strip.update(i for i, _ in chain)
        chain = [item] if item is not None else []
    if not strip:
        return lines
    return [_LINE_NO_RE.sub("", s) if i in strip else s for i, s in enumerate(lines)]


def _strip_page_furniture(lines):
    n = len(lines)
    if sum(1 for s in lines if s) <= 1:
        # A one-line selection ("42", "Page 3 of 12") is what the user chose
        # to hear; furniture only exists between lines of real text.
        return lines
    drop = [False] * n
    pagenum = [False] * n
    caps_heading = []  # "2 METHODS AND MATERIALS": a heading or a running header
    cell = [bool(_CELL_NUM_RE.fullmatch(s)) for s in lines]
    nonblank = [i for i, s in enumerate(lines) if s]
    rank = {i: r for r, i in enumerate(nonblank)}

    def in_number_cluster(i):
        # Page numbers are pages apart; a PDF table pasted one cell per line
        # puts numbers next to each other ("Dyslexia\n42\n21.3"): keep those.
        r = rank[i]
        near = nonblank[max(0, r - 3):r] + nonblank[r + 1:r + 4]
        return any(cell[j] for j in near)

    for i, s in enumerate(lines):
        if not s:
            continue
        if _PAGE_NUMBER_LINE_RE.match(s) or _ROMAN_PAGE_RE.match(s):
            if not ((s.isdigit() or _ROMAN_PAGE_RE.match(s)) and in_number_cluster(i)):
                drop[i] = pagenum[i] = True
        elif _BOILERPLATE_LINE_RE.match(s) or _is_journal_header(s):
            drop[i] = True
        elif m := _CAPS_HEADER_RE.match(s):
            if m.group("lead") and int(m.group("lead")) <= 20 and not re.search(r"\bET\.?\s+AL\b", s):
                caps_heading.append(i)
            else:
                drop[i] = True

    def neighbour(i, step):
        j = i + step
        while 0 <= j < n and (not lines[j] or drop[j]):
            j += step
        return lines[j] if 0 <= j < n else None

    def mid_sentence(i):
        prev, nxt = neighbour(i, -1), neighbour(i, 1)
        return (prev is not None and nxt is not None
                and not _ENDS_SENTENCE_RE.search(prev) and nxt[0].islower())

    def near_pagenum(i):
        for step in (-1, 1):
            j = i + step
            while 0 <= j < n and not lines[j]:
                j += step
            if 0 <= j < n and pagenum[j]:
                return True
        return False

    # A small leading number + capitals is a numbered section heading when it
    # sits between sentences ("...introduction.\n2 METHODS\nWe recruited");
    # inside a sentence it is a running header.  Repeats are handled below.
    for i in caps_heading:
        prev, nxt = neighbour(i, -1), neighbour(i, 1)
        between = ((prev is None or _ENDS_SENTENCE_RE.search(prev))
                   and nxt is not None and _starts_unit(nxt))
        drop[i] = not between

    groups = {}
    for i, s in enumerate(lines):
        if not s or drop[i] or len(s) > 100:
            continue
        m = _EDGE_NUM_RE.match(s)
        core = (m.group("a") or m.group("b")).strip() if m else s
        words = core.split()
        if len(words) < 2 or not re.search(r"[A-Za-z]{2}", core):
            continue
        if _ENDS_SENTENCE_RE.search(core) and not core.endswith("al."):
            continue
        groups.setdefault(" ".join(words).lower(), []).append((i, bool(m) or near_pagenum(i)))
    for occ in groups.values():
        if len(occ) >= 2:
            if any(num for _, num in occ) or any(mid_sentence(i) for i, _ in occ):
                for i, _ in occ:
                    drop[i] = True
        else:
            i, num = occ[0]
            if num and mid_sentence(i):
                prev, nxt = neighbour(i, -1), neighbour(i, 1)
                if len(lines[i]) < 0.6 * max(len(prev), len(nxt)):
                    drop[i] = True
    return [s for s, d in zip(lines, drop) if not d]


# ---------------------------------------------------------------------------
# 3. Hyphenated line breaks
# ---------------------------------------------------------------------------
# "word-\nword": decide join ("compre-\nhension" -> "comprehension") or keep
# the hyphen ("well-\nknown" -> "well-known").  In order:
#   1. keep if the right part starts with a capital or digit (COVID-19,
#      pre-COVID), the left part ends in a digit, is one letter (t-test),
#      or already contains a hyphen (state-of-the-art);
#   2. evidence from the selection itself: if the joined word occurs
#      elsewhere, join; if the hyphenated form occurs elsewhere, keep;
#   3. join if the right part is a bare suffix (-ing, -ness, -tion, ...);
#   4. keep after common compound-forming first parts (self, well, non, ...);
#   5. keep when a vowel is doubled at the junction (anti-inflammatory,
#      meta-analysis, co-occurrence);
#   6. keep when the right part is a typical compound second element
#      (based, related, term, specific, ...) and the left part is a word
#      of 4+ letters (or a known short word such as "age");
#   7. otherwise join (most PDF line-end hyphens are syllable breaks).
# Suspended hyphens ("pre-\nand post-test") are left as "pre- and".
# Across a blank line the break is only joined when the next text starts
# lower-case.  A capital or digit there belongs to something that interrupted
# the sentence (a caption, table or heading: "fine-\n\nTable 2: ..."), so the
# hyphen is left where it is.
# The joined word stays on the first line and the rest of the second line
# keeps its own line, but the first line is marked with _CONT: it is the
# front half of a wrapped line, so it can never be a heading or the end of a
# paragraph ("The so-\ncalled Matthew effect" must not become a heading
# "The so-called" followed by "Matthew effect").

_CONT = ""
_LB_HYPHEN_RE = re.compile(
    r"(?<![\w/.:@\-])([^\W_]+(?:-[^\W_]+)*)-[ ]*\n"
    rf"(?:[ ]*(?=[^\W_])|(?:[ ]*\n)++[ ]*+(?=(?![{_UC}])[^\W\d_]))(\S+)")
_WORD_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")
_HYPHEN_PREFIXES = frozenset(
    "self well ill cross quasi half non all high low long short full first second third "
    "two three four five six seven eight nine twenty thirty forty fifty sixty seventy eighty "
    "ninety".split())
_HYPHEN_COMPOUNDS = frozenset("so-called in-depth in-person in-house".split())
_SUFFIXES = frozenset(
    "er ers est ing ings ed ly ness ment ments age ages ity ities ation ations tion tions "
    "sion sions ance ances ence ences ency ancy ish ism isms ist ists ous ful less able ible "
    "ive ives ize ise ized ised izes ises izing ising ization isation ary ory al ally ical "
    "ically ty ties teen teens ure ures ward wards ship ships".split())
_COMPOUND_SECOND = frozenset(
    "based related term scale level order specific dependent independent oriented driven "
    "focused centered centred known being esteem efficacy report reported reports regulation "
    "regulated rated paced making solving taking free wide like aware sensitive controlled "
    "matched blind blinded sectional size sized value step time point test retest group "
    "response choice trial sample world life class school income quality analysis analyses "
    "analytic cultural linguistic cognitive social economic emotional motor spatial verbal "
    "visual auditory semantic grade year month week item items task tasks up down benefit "
    "effects intensive informed generated specificity reviewed child children teacher student "
    "students".split())
_SHORT_FIRST = frozenset("age sex low top end key map web eye two six one set use day job gap net raw".split())


def _keep_hyphen(left, right, vocab, vocab_h):
    if not right or not right[0].isalpha() or right[0].isupper():
        return True
    if "-" in left:
        return True
    if left[-1].isdigit() or len(left) == 1:
        return True
    lw, rw = left.lower(), right.lower()
    joined, hyph = lw + rw, lw + "-" + rw
    if joined in vocab:
        return False
    if hyph in vocab_h or hyph in _HYPHEN_COMPOUNDS:
        return True
    if rw in _SUFFIXES:
        return False
    if lw in _HYPHEN_PREFIXES:
        return True
    if lw[-1] in "aeiou" and rw[0] == lw[-1]:
        return True
    if rw in _COMPOUND_SECOND and (len(lw) >= 4 or lw in _SHORT_FIRST):
        return True
    return False


def _dehyphenate(text):
    words = _WORD_RE.findall(text)
    vocab = {w.lower() for w in words if "-" not in w}
    vocab_h = {w.lower() for w in words if "-" in w}

    def repl(m):
        left, right = m.group(1), m.group(2)
        rword_m = re.match(r"[^\W\d_]+", right)
        rword = rword_m.group(0) if rword_m else ""
        if right in ("and", "or"):  # suspended hyphen: "pre- and post-test"
            return m.group(0)
        keep = _keep_hyphen(left, rword, vocab, vocab_h)
        joined = left + ("-" if keep else "") + right
        nxt = m.string[m.end():m.end() + 1]
        return joined + ("" if nxt in ("", "\n") else _CONT + "\n")

    return _LB_HYPHEN_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# 4. URLs broken across lines
# ---------------------------------------------------------------------------

_URL_TAIL_RE = re.compile(r"(?:https?://|ftp://|www\.|doi:\s*|(?:dx\.)?doi\.org/|\b10\.\d{4,9}/)\S*$", re.I)


def _url_continues(line, nxt):
    # A numbered heading ("2 Related Work", "3.1 Data") starts a new unit even
    # when the URL before it ends in "." or "/", so it must not be glued on.
    if _SECTION_HEADING_RE.match(nxt):
        return False
    tok = nxt.split()[0]
    core = tok.rstrip(".,;:!?)")
    if not core or not re.fullmatch(r"[\w\-./?&=#%~+:]+", core):
        return False
    has_url_chars = bool(re.search(r"[\d/._=?&#%]", core))
    last = line[-1]
    if last == "." and core[0].isupper():
        return False
    if last in "-_?&=#%~.:":
        return True
    if last == "/":
        return has_url_chars
    return has_url_chars and bool(re.search(r"[./_=?&#%]", core)) and not core[0].isupper()


def _join_broken_urls(lines):
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        while (line and i + 1 < len(lines) and lines[i + 1]
               and _URL_TAIL_RE.search(line) and _url_continues(line, lines[i + 1])):
            line += lines[i + 1]
            i += 1
        out.append(line)
        i += 1
    return out


# ---------------------------------------------------------------------------
# 5. Lines -> paragraphs
# ---------------------------------------------------------------------------
# Within a block (lines between blank lines) each line break becomes a space,
# except that a paragraph break is inserted
#   * before a bullet line (the bullet glyph is removed; Word's "o" counts
#     when at least two lines use it);
#   * before a numbered item ("1. ", "(a) ") when the previous line ends a
#     clause, or - inside a list - ends with ", and" / "; or" or the marker
#     is the next in sequence ("i. first\nii. second");
#   * never after a line that was cut inside a hyphenated word (_CONT);
#   * around a heading: a line that starts a sentence, has no closing
#     punctuation, <= 12 words, starts with a capital or a section number,
#     does not end in a function word, is followed by a capitalised line and
#     is at most 70% of the block's longest line (or is a short numbered or
#     title-case line and the block is tiny or the line clearly short);
#   * after a short line ending in . ! ? (last line of a paragraph);
#   * at every unfinished-looking line break in "logical-line" blocks, i.e.
#     blocks where most lines start with a capital (Word documents, web lists).
# Blocks are then re-joined when a page break split a sentence: the earlier
# paragraph has no closing punctuation and the next starts lower-case.

_BULLET_RE = re.compile(
    "^(?:[\u2022\u25e6\u25aa\u25ab\u2023\u2043\u25cf\u25cb\u25a0\u25a1\u25ba\u25b8\u25b9\u25b6\u25b7"
    "\u27a2\u27a4\u2794\u2192\u2713\u2714\u2717\u25c6\u25c7\u2756\u2219\u00b7]|[-\u2013\u2014*+])\\s+")
_NUMBERED_ITEM_RE = re.compile(r"^\(?(?P<mk>\d{1,2}|[a-z]|[ivx]{1,4})[.)]\s+\S")
_ROMAN_VALUES = {r: v for v, r in enumerate(
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split(), 1)}


def _marker_values(text):
    """Possible (kind, value) readings of a list marker: "c)" -> letter 3, "iv." -> roman 4."""
    m = _NUMBERED_ITEM_RE.match(text)
    if not m:
        return set()
    mk = m.group("mk")
    if mk.isdigit():
        return {("n", int(mk))}
    vals = {("r", _ROMAN_VALUES[mk])} if mk in _ROMAN_VALUES else set()
    if len(mk) == 1:
        vals.add(("a", ord(mk) - 96))
    return vals


def _next_marker(prev_vals, text):
    """True when ``text`` starts with the marker that follows ``prev_vals``."""
    return any((k, v - 1) in prev_vals for k, v in _marker_values(text))
_SECTION_NUM_RE = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2})*\.?|[IVX]{1,5}\.|[A-Z]\.)\s+\S")
_HEADING_BAD_END_RE = re.compile("[.!?,;:\\-\u2013\u2014]$")
_FUNCTION_WORDS = frozenset("""a an the of and or but nor to in on at by for with from as into onto
than that which who whom whose is are was were be been being has have had not its their our his her
this these those between among about over under after before during while if when where whether both
either neither such more less most least very also can could may might must should would will shall
do does did we they it via per versus vs. e.g. i.e.""".split())


class _Para(NamedTuple):
    text: str
    bullet: bool
    heading: bool


def _starts_unit(t):
    c = t[:1]
    return bool(c) and (c.isupper() or c.isdigit() or c in _OPEN_QUOTES)


def _could_end_unit(line):
    if line.endswith((",", "-", "\u2013", "\u2014")):
        return False
    words = line.split()
    return bool(words) and words[-1].lower() not in _FUNCTION_WORDS


_O_BULLET_RE = re.compile(r"^o\s+(?=\S)")  # Word's second-level "o" bullet
_ENDS_STOP_RE = re.compile(r"[.!?][\"'’”)\]]*$")
_LIST_CONT_RE = re.compile(r"[,;](?:\s+(?:and|or))?$")
_SECTION_HEADING_RE = re.compile(rf"^(?:\d{{1,2}}(?:\.\d{{1,2}})*\.?|[IVX]{{1,5}}\.|[A-Z]\.)\s+[{_UC}]")


def _title_like(s, words):
    """Short heading-shaped line: a numbered heading of <= 4 words, or <= 6
    words in title case ("Participants and Procedure"), without , or ;."""
    if "," in s or ";" in s:
        return False
    numbered = bool(_SECTION_HEADING_RE.match(s))
    body = words[1:] if numbered else words
    if not body or len(body) > 6:
        return False
    if numbered and len(body) <= 4:
        return True
    return all(w[0].isupper() or w[0].isdigit() or w.lower() in _FUNCTION_WORDS for w in body)


def _is_heading(s, starts_sentence, nxt, block_max, block_lines=3):
    if not starts_sentence or not nxt or len(s) > 80 or _HEADING_BAD_END_RE.search(s):
        return False
    if not (s[0].isupper() or (s[0].isdigit() and _SECTION_NUM_RE.match(s))):
        return False
    words = s.split()
    if len(words) > 12:
        return False
    if words[-1].lower() in _FUNCTION_WORDS or not _starts_unit(nxt):
        return False
    if len(s) <= 0.7 * block_max:
        return True
    # A full-width wrapped line is not a heading; a short title-like line
    # still is when the body text after it is short too ("1. Introduction\n
    # Dyslexia is common.").  In a longer block it must still be clearly
    # shorter than a full line (justified columns make full lines similar).
    return _title_like(s, words) and (block_lines <= 2 or len(s) <= 0.85 * block_max)


def _split_block(block, o_bullets=False):
    n = len(block)
    cont = [line.endswith(_CONT) for line in block]
    block = [line.replace(_CONT, "") for line in block]
    bullets = [bool(_BULLET_RE.match(line)) or (o_bullets and bool(_O_BULLET_RE.match(line)))
               for line in block]
    texts = [_O_BULLET_RE.sub("", _BULLET_RE.sub("", line, count=1), count=1).strip() if b else line
             for line, b in zip(block, bullets)]
    block_max = max(len(t) for t in texts)
    upper = [b or _starts_unit(t) for t, b in zip(texts, bullets)]
    logical = n >= 3 and sum(upper[1:]) >= 0.6 * (n - 1)
    out = []
    cur, cur_bullet = [texts[0]], bullets[0]
    prev_break = True
    item = _marker_values(texts[0])  # marker of the list item being built, if any
    for i in range(n - 1):
        line, nxt = texts[i], texts[i + 1]
        starts_sentence = prev_break or bool(_ENDS_SENTENCE_RE.search(texts[i - 1]))
        heading = False
        if bullets[i + 1]:
            brk = True
        elif cont[i]:
            brk = False  # the line was cut inside a hyphenated word
        elif _NUMBERED_ITEM_RE.match(nxt) and (
                _ENDS_CLAUSE_RE.search(line)
                or (item and (_LIST_CONT_RE.search(line) or _next_marker(item, nxt)))):
            # "(a) ...;\n(b) ..." and, once inside a list, "i. x,\nii. y, and\niii. z"
            # or the next marker in sequence ("i. first\nii. second")
            brk = True
        elif line and _is_heading(line, starts_sentence, nxt, block_max, n):
            brk = heading = True
        elif logical and _could_end_unit(line) and upper[i + 1]:
            brk = True
        elif n >= 3 and _ENDS_STOP_RE.search(line) and len(line) < 0.6 * block_max and upper[i + 1]:
            brk = True
        else:
            brk = False
        if brk:
            if heading and len(cur) > 1:
                out.append(_Para(" ".join(cur[:-1]), cur_bullet, False))
                cur, cur_bullet = [line], False
            out.append(_Para(" ".join(cur), cur_bullet, heading))
            cur, cur_bullet = [nxt], bullets[i + 1]
            item = _marker_values(nxt)
        else:
            cur.append(nxt)
        prev_break = brk
    out.append(_Para(" ".join(cur), cur_bullet, False))
    return out


def _build_paragraphs(lines):
    paras = []
    block = []
    # "o First item" is a bullet only when several lines use it.
    o_bullets = sum(1 for line in lines if _O_BULLET_RE.match(line)) >= 2
    for line in lines + [""]:
        if line:
            block.append(line)
        elif block:
            paras.extend(p for p in _split_block(block, o_bullets) if p.text.strip())
            block = []
    merged = []
    for p in paras:
        if (merged and not p.bullet and p.text[:1].islower() and not merged[-1].heading
                and not _ENDS_CLAUSE_RE.search(merged[-1].text)
                and not (_NUMBERED_ITEM_RE.match(p.text) and _NUMBERED_ITEM_RE.match(merged[-1].text))):
            last = merged[-1]
            merged[-1] = last._replace(text=last.text + " " + p.text)
        else:
            merged.append(p)
    return merged


# ---------------------------------------------------------------------------
# 6a. URLs and DOIs
# ---------------------------------------------------------------------------
# Choice: a URL/DOI that is grammatically part of the sentence ("data are
# available at <url>", "Code: <url>") becomes the word "link", so the
# sentence still sounds complete and the listener knows there is something
# to look up.  A URL/DOI that is not part of the grammar (alone in brackets,
# after a full stop or comma, after a "doi:" label, "Retrieved from" or
# "Available at:") is removed entirely,
# because "link" there is pure noise.  Raw URLs are never read out.
# A PDF footnote pastes its number straight onto the URL ("1https://...",
# "10www.example.org"): a short number standing alone before http(s)://, ftp://
# or www. is taken as part of the URL.  Such a URL is the footnote itself, not
# part of the sentence around it, so it is removed rather than read as "link".

_DROP, _LINK = "\ue000", "\ue001"
_URL_RE = re.compile(r"""
    (?P<doi>(?:\bdoi\s*:?\s*|\bhttps?://(?:dx\.)?doi\.org/|(?<![\w.@/-])(?:dx\.)?doi\.org/)?\b10\.\d{4,9}/[^\s"<>]+)
  | (?P<url>(?:(?<![^\s(\[])\d{1,3}|(?<!\w))(?:https?|ftp)://[^\s"<>]+
     | (?:(?<![^\s(\[])\d{1,3}|(?<![\w.@/-]))www\.[^\s"<>]+
     | (?<![\w.@/-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|edu|gov|io|ai|uk|de|ca|au|info|eu|int|dev
         |app|co|us|nl|fr|jp|ch|se|no|fi|dk|nz|ie|be|at|es|it|ly|me|tv)/[^\s"<>]*)
""", re.I | re.X)
_SENT_CLASS = f"[{_DROP}{_LINK}]"
_FOOTNOTE_URL_RE = re.compile(r"\d{1,3}(?:(?:https?|ftp)://|www\.)", re.I)


def _trim_url(u):
    while u and u[-1] in ".,;:!?'\"\u2019\u201d)]}>":
        if u[-1] == ")" and u.count("(") >= u.count(")"):
            break
        u = u[:-1]
    return u


def _remove_urls(text):
    def repl(m):
        s = m.group(0)
        url = _trim_url(s)
        # "<https://...>" is wrapped, not detached: look past the "<"
        before = m.string[max(0, m.start() - 4):m.start()].rstrip().rstrip("<").rstrip()
        token = _LINK if before and (before[-1].isalnum() or before[-1] == ":") else _DROP
        if _FOOTNOTE_URL_RE.match(s):
            token = _DROP
        return token + s[len(url):]

    if not _URL_RE.search(text):
        return text
    text = _URL_RE.sub(repl, text)
    for rx, rep in _URL_CONTEXT_RULES:
        text = rx.sub(rep, text)
    return text.replace(_LINK, "link")


_URL_CONTEXT_RULES = [
    (re.compile(rf"\b[Rr]etrieved\s+(?:[\w,]+\s+){{0,4}}?from\s*:?\s*{_SENT_CLASS}"), _DROP),
    (re.compile(rf"\b(?:[Aa]vailable|[Aa]ccessed)\s+(?:online\s+)?(?:at|from)\s*:\s*{_SENT_CLASS}"), _DROP),
    (re.compile(rf"\s*[(\[]\s*(?:(?:see|at|available\s+at|online\s+at|via)\s*:?\s*)?{_SENT_CLASS}\s*[)\]]",
                re.I), ""),
    (re.compile(rf"\s*{_DROP}"), ""),
    (re.compile(rf"<\s*({_LINK})\s*>"), r"\1"),
]


def _protect_urls(text):
    saved = []

    def repl(m):
        url = _trim_url(m.group(0))
        saved.append(url)
        return f"\ue010{len(saved) - 1}\ue011" + m.group(0)[len(url):]

    return _URL_RE.sub(repl, text), saved


def _restore_urls(text, saved):
    return re.sub("\ue010(\\d+)\ue011", lambda m: saved[int(m.group(1))], text)


# ---------------------------------------------------------------------------
# 6b. Citations
# ---------------------------------------------------------------------------
# Numeric:  [1] [1, 2] [3-7] [12; 15] [1]-[3] are removed.  When the number
#   is the object of the sentence ("as shown in [3]", "see Ref. [4]") it is
#   read as "reference 3" so the sentence stays grammatical.  Intervals are
#   protected: anything containing 0, or two numbers after range/interval/
#   scale/between/=, or "x in [1, 10]".
# Alphabetic keys: [SB18] [Knu84].
# Author-year in ( ) or [ ]: the content is split at ';' and each part is a
#   citation if it is  [prefix] authors [,] year(s) [locator] [", for ..."]
#   where prefix is see / e.g. / cf. / for a review, see / as cited in ...,
#   authors are capitalised name tokens (plus et al., &, and, van/de/...,
#   initials, corporate words of/the/for), years are 1600-2099 with optional
#   letter, n.d., in press; locator is p. 4 / pp. 4-5 / : 45.  Citation parts
#   are dropped, other parts kept: "(DSM-5; APA, 2013)" -> "(DSM-5)".
#   Months, seasons, Study, Experiment, Figure ... are never authors, so
#   "(March 2020)" survives; "(n = 42)", "(p < .05)" have no author+year.
# Narrative: "Smith et al. (2019) showed" -> "Smith et al. showed": a
#   parenthesis holding only years is dropped when it follows a capitalised
#   name or "al.".
# Superscripts: Unicode superscript digits after punctuation or a word of 3+
#   letters (not after m, cm, R, x, so m\u00b2 and R\u00b2 stay); plain
#   digits stuck after punctuation ("disorders.12 However", "children,3 and");
#   digit lists stuck to a lower-case word of 5+ letters ("cells12,13") or a
#   single number between such a word and punctuation ("previously12.");
#   Chicago-style note numbers after a closing quote ('a trap."14 This').
# Numeric intervals that look like citations are kept: anything after CI,
#   CrI, HDI, IQR, range, interval, "=", "x in" ... ("95% CI [12, 15]").

_NUM_CITE = (r"\[\s*\d{1,4}[a-z]?(?:\s*[-\u2013\u2014]\s*\d{1,4}[a-z]?)?"
             r"(?:\s*[,;]\s*\d{1,4}[a-z]?(?:\s*[-\u2013\u2014]\s*\d{1,4}[a-z]?)?)*\s*\]")
_NUM_CITE_SEQ = rf"{_NUM_CITE}(?:\s*[-\u2013\u2014,]\s*{_NUM_CITE})*"
_NUM_REF_RE = re.compile(rf"\b(?:[Rr]efs?\.|[Rr]eferences?)\s*(?P<cite>{_NUM_CITE_SEQ})")
_NUM_NARR_RE = re.compile(
    rf"(?P<lead>\b(?:[Ii]n|[Ss]ee|[Ff]rom|[Bb]y|[Oo]f|[Cc]f\.|[Ww]ith|[Tt]o|[Ff]ollowing)\s+)(?P<cite>{_NUM_CITE_SEQ})")
_NUM_RE = re.compile(rf"\s*(?P<cite>{_NUM_CITE_SEQ})")
_ALPHA_CITE_RE = re.compile(r"\s*\[[A-Z][A-Za-z+]{1,7}\d{2}[a-z]?(?:\s*[,;]\s*[A-Z][A-Za-z+]{1,7}\d{2}[a-z]?)*\]")

_YEAR = r"(?:1[6-9]\d\d|20\d\d)[a-z]?"
_YEAR_TOKEN = (rf"(?:{_YEAR}(?:\s*/\s*{_YEAR})?|n\.\s?d\.|in\s+press|in\s+prep(?:aration|\.)?"
               r"|forthcoming|submitted|under\s+review|unpublished)")
_YEARS = rf"{_YEAR_TOKEN}(?:\s*,\s*(?:{_YEAR_TOKEN}|[a-z](?![\w.])))*"
_LOC = (r"(?:\s*[,:]\s*(?:(?i:pp?|pgs?|paras?|chaps?|ch|secs?|figs?|tabs?)\.?\s*"
        r"|(?i:pages?|chapters?|sections?|figures?|tables?)\s+|\u00a7\s*)?\d+[a-z]?"
        r"(?:\s*[-\u2013\u2014]\s*\d+[a-z]?)?)?")
_CITE_PREFIX = (
    r"(?:(?i:see\s+also|see|but\s+see|cf\.|compare|e\.g\.|i\.e\.|for\s+example|for\s+instance"
    r"|as\s+cited\s+in|cited\s+in|quoted\s+in|reviewed\s+in|reviewed\s+by|adapted\s+from|reprinted\s+from"
    r"|based\s+on|following|also|and|or|in"
    r"|for\s+(?:a|an)\s+(?:[a-z\-]+\s+){0,2}(?:review|reviews|overview|meta-analysis|meta-analyses|discussion"
    r"|summary|critique)\s*,?\s*see(?:\s+also)?)\s*,?\s+)*")
_CITE_ITEM_RE = re.compile(
    rf"\s*(?P<prefix>{_CITE_PREFIX})(?P<authors>[^\d();]+?)\s*,?\s*(?P<years>{_YEARS}){_LOC}"
    r"(?:\s*,?\s+for\s+[a-z][^;()]{0,60})?\s*")
_CITE_SUBSPLIT_RE = re.compile(rf"(?:(?<=\d{{4}})|(?<=\d{{4}}[a-z])|(?<=n\.d\.)|(?<=press))\s*,\s*(?=[{_UC}])")
_YEARS_ONLY_RE = re.compile(rf"\s*{_YEARS}{_LOC}\s*")
_NARRATIVE_TAIL_RE = re.compile(rf"(?<![\w'\u2019\-])([{_UC}][\w'\u2019\-]*|al\.)\s*$")
_TRAILING_YEAR_RE = re.compile(
    rf"(?<![\w'\u2019\-])(?P<name>[{_UC}][\w'\u2019\-]*|al\.)\s*,?\s+(?:{_YEARS}){_LOC}\s*$")
_NAME_PARTICLES = frozenset("van von de der den del della da di du la le al el ter ten dos das bin ibn y".split())
_CORPORATE_WORDS = frozenset("of the for on".split())
_NOT_AUTHOR = frozenset("""January February March April May June July August September October
November December Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec Spring Summer Fall Autumn Winter
Study Studies Experiment Experiments Figure Fig Table Appendix Wave Phase Session Version Since Until
From Before After During Circa Year Years Born Published Founded Grade Class Cohort Time Term Semester
Quarter Week Day Model Sample Dataset Data Census Act Law""".split())


def _possessive_free(word):
    return re.sub(r"['\u2019]s$", "", word)


def _looks_like_authors(s):
    s = s.strip().rstrip(",").strip()
    if not s or len(s) > 150:
        return False
    tokens = [t for t in re.split(r"[\s,]+", re.sub(r"\bet\s+al\.?", " ", s)) if t]
    if not tokens or len(tokens) > 15:
        return False
    first, has_name = None, False
    for t in tokens:
        if t in ("&", "and") or t.lower() in _NAME_PARTICLES or t in _CORPORATE_WORDS:
            continue
        core = t.strip("[]().")
        if not core or not core[0].isupper():
            return False
        if not all(ch.isalpha() or ch in "'\u2019-." for ch in core):
            return False
        first = first or core
        if len(core.replace(".", "")) >= 2:
            has_name = True
    return has_name and first not in _NOT_AUTHOR


def _is_citation_part(part):
    for sub in _CITE_SUBSPLIT_RE.split(part):
        m = _CITE_ITEM_RE.fullmatch(sub)
        if not m or not _looks_like_authors(m.group("authors")):
            return False
    return True


def _strip_trailing_year(part):
    m = _TRAILING_YEAR_RE.search(part)
    if m and _possessive_free(m.group("name")) not in _NOT_AUTHOR:
        return part[:m.end("name")]
    return part


def _cite_group(content, before, narrative):
    """None = remove the whole bracket; otherwise the (possibly edited) content."""
    c = content.strip()
    if _YEARS_ONLY_RE.fullmatch(c):
        if narrative:
            m = _NARRATIVE_TAIL_RE.search(_possessive_free(before.rstrip()))
            if m and m.group(1) not in _NOT_AUTHOR:
                return None
        return content
    kept, changed = [], False
    for part in re.split(r"\s*;\s*", c):
        if not part:
            continue
        if _is_citation_part(part):
            changed = True
            continue
        stripped = _strip_trailing_year(part)
        changed = changed or stripped != part
        kept.append(stripped)
    if not changed:
        return content
    return "; ".join(kept) if kept else None


def _paren_repl(m):
    before = m.string[max(0, m.start() - 120):m.start()]
    result = _cite_group(m.group("c"), before, narrative=True)
    if result is None:
        return ""
    if result == m.group("c"):
        return m.group(0)
    return f"{m.group('ws')}({result})"


def _bracket_repl(m):
    return "" if _cite_group(m.group("c"), "", narrative=False) is None else m.group(0)


_PAREN_RE = re.compile(r"(?P<ws>\s*)\((?P<c>[^()]{1,400})\)")
_BRACKET_RE = re.compile(r"(?P<ws>\s*)\[(?P<c>[^\[\]]{1,400})\]")


_INTERVAL_BEFORE_RE = re.compile(
    r"(?:\b(?:range|interval|scale|domain|bounded|between|values?|CIs?|CrIs?|HDIs?|HPDIs?|IQR|PIs?"
    r"|confidence|credible|percentiles?)|\u2208|=|\u2264|\u2265|<|>)\W*(?:\w+\W+){0,2}$", re.I)
_VAR_IN_RE = re.compile(r"(?<![\w])[A-Za-z\u03b1-\u03c9]\s+(?:in|\u2208)\s*$")


def _is_interval(before, cite):
    nums = re.findall(r"\d+", cite)
    if any(x.startswith("0") for x in nums):
        return True
    # "95% CI [12, 15]", "in the range [1, 10]", "x = [2, 5]"
    if len(nums) == 2 and "," in cite and _INTERVAL_BEFORE_RE.search(before):
        return True
    return bool(_VAR_IN_RE.search(before))


def _spoken_refs(cite):
    cite = re.sub(r"\]\s*[-\u2013\u2014]\s*\[", "-", cite)  # IEEE "[2]-[4]" is the range 2 to 4
    items = [x for x in re.split(r"[\[\]]|\s*[,;]\s*", cite) if x.strip(" -\u2013\u2014")]
    spoken = []
    for it in items:
        rng = re.fullmatch(r"\s*(\d+[a-z]?)\s*[-\u2013\u2014]\s*(\d+[a-z]?)\s*", it)
        spoken.append(f"{rng[1]} to {rng[2]}" if rng else it.strip())
    word = "references" if len(spoken) > 1 or " to " in spoken[0] else "reference"
    listed = spoken[0] if len(spoken) == 1 else ", ".join(spoken[:-1]) + " and " + spoken[-1]
    return f"{word} {listed}"


_SUP = "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079"
_SUP_RE = re.compile(rf"(?<![\s\d{_SUP}\u207b\u207a^])[{_SUP}]+(?:[,\-\u2013\u2012\u207b][{_SUP}]+)*")
_MATH_WORDS = frozenset("sin cos tan log exp sec cot csc sinh cosh tanh".split())
_PLAIN_SUP_RE = re.compile(r"(?<=[a-z]{2})(?P<p>[.,;:])(?P<d>\d{1,3}(?:[,\u2013\-]\d{1,3})*)(?=\s|$)")
_STUCK_SUP_RE = re.compile(
    r"(?<![\w\-])(?P<w>[a-z]{5,})(?P<d>\d{1,3}(?:[,\u2013\-]\d{1,3})+(?=[.,;:!?)]?(?:\s|$))"
    r"|\d{1,3}(?=[.,;:!?)](?:\s|$)))")
_STUCK_EXCEPTIONS = frozenset("python covid windows ubuntu android iphone layer".split())
_ANY_DIGIT_RE = re.compile(rf"[\d{_SUP}]")
# Chicago-style note numbers after a closing quote: 'a trap."14 This', 'body"15 is'
_QUOTE_NOTE_RE = re.compile(
    r"(?:(?<=[.,;:!?][\"”’'])|(?<=[^\W\d_][\"”]))\d{1,3}(?:[,–\-]\d{1,3})*(?=[\s.,;:)]|$)")
_REF_ABBREVIATIONS = frozenset(
    "p pp fig figs eq eqs no nos vol vols ref refs sec secs ch chap tab tabs art exp suppl ver vs approx ca".split())


def _sup_repl(m):
    before = m.string[max(0, m.start() - 40):m.start()]
    if not before:
        return m.group(0)
    if before[-1] in ".,;:\u2019\u201d\"'":
        return ""
    w = re.search(r"[^\W\d_]+$", before)
    if w and len(w.group(0)) >= 3 and w.group(0).lower() not in _MATH_WORDS:
        return ""
    return m.group(0)


def _plain_sup_repl(m):
    before = m.string[max(0, m.start() - 40):m.start()]
    word = re.search(r"([A-Za-z]+)$", before).group(1)
    et_al = word.lower() == "al" and re.search(r"\bet\s+al$", before)
    if not et_al and (len(word) < 3 or word.lower() in _REF_ABBREVIATIONS):
        return m.group(0)
    if m.group("p") == ".":
        after = m.string[m.end():m.end() + 3]
        if after and not re.match(r"\s+[A-Z\"\u201c\u2018(\[]", after):
            return m.group(0)
    return m.group("p")


def _stuck_sup_repl(m):
    return m.group(0) if m.group("w") in _STUCK_EXCEPTIONS else m.group("w")


def _remove_citations(text):
    if "[" in text:
        text = _NUM_REF_RE.sub(lambda m: _spoken_refs(m.group("cite")), text)

    def narr(m):
        before = m.string[max(0, m.start() - 60):m.start("cite")]
        if _is_interval(before, m.group("cite")):
            return m.group(0)
        return m.group("lead") + _spoken_refs(m.group("cite"))

    def num(m):
        before = m.string[max(0, m.start() - 60):m.start("cite")]
        return m.group(0) if _is_interval(before, m.group("cite")) else ""

    # Cheap character checks skip the patterns that cannot match (most
    # paragraphs have no brackets), which keeps short paragraphs fast.
    if "[" in text:
        text = _NUM_NARR_RE.sub(narr, text)
        text = _NUM_RE.sub(num, text)
        text = _ALPHA_CITE_RE.sub("", text)
    if "(" in text:
        for _ in range(2):  # the second pass handles one level of nesting
            text = _PAREN_RE.sub(_paren_repl, text)
    if "[" in text:
        text = _BRACKET_RE.sub(_bracket_repl, text)
    if _ANY_DIGIT_RE.search(text):
        text = _SUP_RE.sub(_sup_repl, text)
        text = _PLAIN_SUP_RE.sub(_plain_sup_repl, text)
        text = _QUOTE_NOTE_RE.sub("", text)
        text = _STUCK_SUP_RE.sub(_stuck_sup_repl, text)
    return text


# ---------------------------------------------------------------------------
# 6c. Symbols (only replacements that are safe in academic prose)
# ---------------------------------------------------------------------------

# (pattern, replacement) pairs applied in order; compiled once because
# normalize() runs them for every paragraph.
_SYMBOL_RULES = [(re.compile(p), r) for p, r in [
    # a CLI flag ("run --max_len=5") is not a dash: hide it from the dash rules
    (r"(?:(?<=\s)|^)--(?=[a-z])", "\ue031"),
    # numeric ranges with en/em dash or -- : "10-20" -> "10 to 20"
    (r"(\d%?)\s*(?:\u2013|\u2014|-{2,3})\s*(?=[$\u00a3\u20ac]?[-\u2212]?\.?\d)", r"\1 to "),
    # word-word en dash (mother-child, dose-response) -> ordinary hyphen
    (r"(?<=[^\W_])\u2013(?=[^\W_])", "-"),
    # parenthetical dashes -> comma-like pause
    (r"\s*(?:\u2014|-{2,3})\s*", ", "),
    (r"\s+\u2013\s+|\s+\u2013|\u2013\s+", ", "),
    # "reading - like writing - is" is a pause, but "x - y" or "y_i - z_i" is a minus
    (r"(?<=[^\W\d_])\s+-\s+(?=[^\W\d_])", lambda m: _spaced_hyphen(m)),
    (r"(\d)\s*\u00d7\s*(?=10\s*[\u207b" + _SUP + r"^])", r"\1 times "),
    (r"(\d)\s*\u00d7\s*(?=\d)", r"\1 by "),
    (r"\s*\u00b0\s*C\b", " degrees Celsius"),
    (r"\s*\u00b0\s*F\b", " degrees Fahrenheit"),
    (r"\s*\u00b0", " degrees"),
    (r"(?<![\w~])~\s*(?=[-\u2212]?\.?\d)", "approximately "),
    ("[\u2020\u2021]", ""),
    (r"\u00a7+\s*", "section "),
    (r"\s+[\u2022\u25cf\u25aa\u25e6]\s+", ", "),
    # a free-standing bar separates ("1 | INTRODUCTION", "| a | b |" tables); |x| and P(A|B) stay
    (r"\s*(?<!\S)\|(?!\S)\s*", ", "),
    (r"(?<=\d)\s*\*\s*(?=\d)", " times "),
]]
_SYMBOL_WORDS = str.maketrans({
    "\u2248": " approximately ", "\u00b1": " plus or minus ", "\u2264": " less than or equal to ",
    "\u2265": " greater than or equal to ", "\u2260": " not equal to ", "\u2192": " to ",
    "\u00d7": " times ", "*": "", "\u2212": "-", "\ue031": "--",
})


_LAST_WORD_RE = re.compile(r"\w+$")
_FIRST_WORD_RE = re.compile(r"\w+")


def _spaced_hyphen(m):
    """" - " between letters: a pause in prose, a minus between variables."""
    left = _LAST_WORD_RE.search(m.string, max(0, m.start() - 40), m.start()).group()
    right = _FIRST_WORD_RE.match(m.string, m.end()).group()
    if "_" in left or "_" in right or (len(left) == 1 and len(right) == 1):
        return m.group()
    return ", "


_SYMBOL_TRIGGER_RE = re.compile("[-–—×°~†‡§•●▪◦*|]")


def _replace_symbols(t):
    if _SYMBOL_TRIGGER_RE.search(t):  # most short paragraphs need no rule at all
        for rx, rep in _SYMBOL_RULES:
            t = rx.sub(rep, t)
    return t.translate(_SYMBOL_WORDS)


# (pattern, replacement, characters that must occur for the rule to apply)
_TIDY_RULES = [(re.compile(p), r, frozenset(need) if need else None) for p, r, need in [
    (r"[ \t]+", " ", " \t"),
    (r"<\s*>|\(\s*[,;:]?\s*\)|\[\s*\]", "", "<(["),
    (r"\(\s*[,;]\s*", "(", "("),
    (r"\s*[,;]\s*\)", ")", ")"),
    (r"\s+([,;!?%)\]])", r"\1", ",;!?%)]"),
    (r"\s+([.:])(?!\d)", r"\1", ".:"),
    (r"([(\[])\s+", r"\1", "(["),
    (r",(?:\s*,)+", ",", ","),
    # ", ." left by a removed item -> "."; but ", .45" is a list of decimals
    (r"[,;:]\s*([.!?])(?![.\d])", r"\1", ",;:"),
    (r"(?<!\.)\.\.(?!\.)", ".", "."),
    # '"It works." (Smith, 2019).' leaves '"It works.".': one full stop is enough
    ("(?<=[.!?][\"'’”])\\.(?!\\.)", "", "\"'’”"),
    (r"^[\s,;:]+", "", None),
    (r"[\s,]+$", "", None),
    (r" {2,}", " ", " "),
]]


def _tidy(t):
    chars = set(t)
    for rx, rep, need in _TIDY_RULES:
        if need is None or not chars.isdisjoint(need):
            t = rx.sub(rep, t)
    return t.strip()


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

_HAS_WORD_RE = re.compile(r"[^\W_]")


def normalize(text: str, *, skip_citations: bool = True, skip_urls: bool = True) -> str:
    """Clean copied text for speech.  Paragraphs are separated by "\\n\\n".

    If the cleaning would remove everything although the selection has
    letters or digits (the user selected only a citation, a URL or a DOI),
    the selection is returned with citations and URLs kept, so pressing the
    hotkey never results in unexplained silence.
    """
    if not isinstance(text, str):
        raise TypeError("normalize() expects a str")
    text = _clean_chars(text)
    out = _normalize_clean(text, skip_citations, skip_urls)
    if not out and (skip_citations or skip_urls) and _HAS_WORD_RE.search(text):
        out = _normalize_clean(text, False, False)
    return out


def _normalize_clean(text, skip_citations, skip_urls):
    out = []
    for para in _paragraphs(text):
        t = _inline(para.text, skip_citations, skip_urls)
        if _HAS_WORD_RE.search(t):
            out.append(t)
    return "\n\n".join(out)


def _paragraphs(text):
    """The line layer: furniture, hyphenation and broken URLs out, then paragraphs (headings and items apart)."""
    lines = [line.strip() for line in text.split("\n")]
    lines = _strip_page_furniture(_strip_line_numbers(lines))
    text = _dehyphenate("\n".join(lines))
    lines = _join_broken_urls([line.strip() for line in text.split("\n")])
    return _build_paragraphs(lines)


def _inline(t, skip_citations, skip_urls):
    """The inline layer for one paragraph: URLs, citations, symbols and tidy-up."""
    saved = []
    if skip_urls:
        t = _remove_urls(t)
    else:
        t, saved = _protect_urls(t)
    if skip_citations:
        t = _remove_citations(t)
    t = _tidy(_replace_symbols(t))
    if saved:
        t = _restore_urls(t, saved)
    return t


def normalize_pair(text: str, *, skip_citations: bool = True, skip_urls: bool = True) -> list[tuple[str, str]]:
    """normalize() twice over one line pass: (shown, spoken) for each paragraph.

    The shown paragraph keeps citations and URLs; the spoken one is exactly what normalize() reads. A paragraph
    that is only a citation has spoken "" (shown, never read). Joining the non-empty spoken paragraphs with
    "\\n\\n" gives normalize(text, ...), and joining the shown ones gives normalize(text, skip_citations=False,
    skip_urls=False). As in normalize(), a selection that would be read as silence is read with its citations.
    """
    if not isinstance(text, str):
        raise TypeError("normalize_pair() expects a str")
    pairs = []
    for para in _paragraphs(_clean_chars(text)):
        shown = _inline(para.text, False, False)
        if not _HAS_WORD_RE.search(shown):
            continue
        spoken = _inline(para.text, skip_citations, skip_urls) if (skip_citations or skip_urls) else shown
        pairs.append((shown, spoken if _HAS_WORD_RE.search(spoken) else ""))
    if pairs and not any(spoken for _, spoken in pairs):
        pairs = [(shown, shown) for shown, _ in pairs]
    return pairs


# ---------------------------------------------------------------------------
# The reference list
# ---------------------------------------------------------------------------
# A paper's reference list is often more than half its text (about 30
# minutes of listening), so a file reading stops before it unless the
# person asks for it.  It starts at the last References-type heading line
# in the last 60% of the text (a two-column paper's list can begin at 42%).
# An appendix after it ends it, because that is prose again: "Appendix A",
# "Supplementary Material", or a lettered heading ("A Additional Details")
# with an "A.1 ..." heading somewhere after it.

_REFS_HEADING_RE = re.compile(
    r"(?:\d{1,2}\.?\s+|[IVX]{1,4}\.\s+)?(?:references?(?:\s+cited)?|bibliography|works\s+cited"
    r"|literature\s+cited|reference\s+list)\s*:?", re.I)
_APPENDIX_HEADING_RE = re.compile(
    r"(?:appendix|appendices|supplementary\s+(?:materials?|information|data|methods))\b.{0,80}", re.I)
_LETTER_HEADING_RE = re.compile(r"(?P<l>[A-H])\.?\s+[A-Z][\w\-]*(?:\s+[\w\-]+){0,10}")
REFS_MIN_SHARE = 0.4


def _lines_with_offsets(text, start=0):
    pos = start
    for line in text[start:].split("\n"):
        yield pos, line.strip()
        pos += len(line) + 1


def references_start(text: str) -> int | None:
    """Offset of the line holding the last References-type heading, if it lies in the last 60% of `text`."""
    best = None
    for pos, line in _lines_with_offsets(text):
        if line and len(line) <= 40 and _REFS_HEADING_RE.fullmatch(line):
            best = pos
    if best is not None and best >= len(text) * REFS_MIN_SHARE:
        return best
    return None


def references_end(text: str, start: int) -> int:
    """Where the reference list that starts at `start` ends: at an appendix heading after it, or at the end."""
    lines = list(_lines_with_offsets(text, start))
    for k in range(1, len(lines)):
        pos, line = lines[k]
        if not line or len(line) > 90 or _ENDS_STOP_RE.search(line):
            continue
        if _APPENDIX_HEADING_RE.fullmatch(line):
            return pos
        m = _LETTER_HEADING_RE.fullmatch(line)
        if m:
            sub = m.group("l") + ".1 "
            if any(later.startswith(sub) for _, later in lines[k + 1:k + 400]):
                return pos
    return len(text)


# ---------------------------------------------------------------------------
# Spoken forms
# ---------------------------------------------------------------------------
# Abbreviations a voice may stumble over or pause after are said in full:
# "Fig. 2" -> "figure 2", "e.g." -> "for example".  They are found in the
# spoken text after sentence splitting, so they never move a sentence
# boundary, and the screen keeps the original.  (Number ranges and "~"
# are already said as words by normalize().)

_SPOKEN_FORMS = [(re.compile(p), said) for p, said in [
    (r"(?<![\w.])Figs\.(?=\s*\d)", "Figures"),
    (r"(?<![\w.])figs\.(?=\s*\d)", "figures"),
    (r"(?<![\w.])Fig\.(?=\s*\d)", "Figure"),
    (r"(?<![\w.])fig\.(?=\s*\d)", "figure"),
    (r"(?<![\w.])Eqs\.(?=\s*\(?\d)", "Equations"),
    (r"(?<![\w.])eqs\.(?=\s*\(?\d)", "equations"),
    (r"(?<![\w.])Eq\.(?=\s*\(?\d)", "Equation"),
    (r"(?<![\w.])eq\.(?=\s*\(?\d)", "equation"),
    (r"(?<![\w.])pp\.(?=\s*\d)", "pages"),
    (r"(?<![\w.])Approx\.(?=\s)", "Approximately"),
    (r"(?<![\w.])approx\.(?=\s)", "approximately"),
    (r"(?<![\w.])E\.\s?g\.(?=[\s,;:)]|$)", "For example"),
    (r"(?<![\w.])e\.\s?g\.(?=[\s,;:)]|$)", "for example"),
    (r"(?<![\w.])I\.\s?e\.(?=[\s,;:)]|$)", "That is"),
    (r"(?<![\w.])i\.\s?e\.(?=[\s,;:)]|$)", "that is"),
]]
_SPOKEN_TRIGGER_RE = re.compile(r"[FfEeIiAp][a-z]*\.")


def spoken_forms(text: str) -> list[tuple[int, int, str]]:
    """(start, end, said) for each abbreviation in `text` that is said in full, in order and never overlapping."""
    if not _SPOKEN_TRIGGER_RE.search(text):
        return []
    found = sorted((m.start(), m.end(), said) for rx, said in _SPOKEN_FORMS for m in rx.finditer(text))
    out, end = [], -1
    for a, b, said in found:
        if a >= end:
            out.append((a, b, said))
            end = b
    return out


# ---------------------------------------------------------------------------
# split_sentences
# ---------------------------------------------------------------------------
# Candidate boundary: . ! ? (or an ellipsis), optional closing quotes or
# brackets, whitespace, then an upper-case letter, a digit, an opening
# quote, or an opening bracket followed by a capital ("(This was rare.)").  Paragraph breaks (blank lines) always split.  A full stop is NOT a
# boundary when the word before it is
#   * an abbreviation that is always followed by more text (e.g., i.e., cf.,
#     vs., approx., ca., Fig., Eq., Tab., Sec., Ch., No., Vol., pp., p., Dr.,
#     Prof., Mr., Mrs., Ms., St., ...);
#   * the numbering at the very start of a sentence ("2.3.1. Methods",
#     "1. Introduction", "IV. Results", "A. Participants");
# and it is only a boundary when the next word is a typical sentence starter
# (The, This, However, In, We, A, An, ...) after
#   * abbreviations that can also end a sentence (Inc., Ltd., Jr., St.,
#     U.S., U.K., Ph.D., a.m., months ...) and dotted forms like "U.S.A.";
#   * single-letter initials ("J. R. R. Tolkien"), unless the letter is a
#     label such as "Condition A." / "Appendix B." which do end sentences
#     (A, An and I are not starters here: "J. A. Smith");
#   * an ellipsis ("..." or the ellipsis character) - never after "[...]".
# "et al." is a boundary before any capitalised word ("...by Smith et al.
# A replication failed") but not before a year ("Smith et al. 2019 found").
# Mid-sentence it is followed by a verb, a year, "(2019)" or "'s".
# "etc." behaves like an ordinary word: mid-sentence it is followed by a
# lower-case word or a comma, so it never splits there.
# ! and ? always split.  Decimals and versions (3.5, v2.3.1) have no space
# after the dot, so they are never candidates.
# A sentence may also start with a lower-case scientific token: mRNA, pH,
# iPhone, siRNA (lower-case then a capital) or p-values, t-tests, k-means
# (one letter and a hyphen).
# A boundary inside a balanced (...) or [...] of at most 400 characters is
# ignored, so "(this was expected. It replicated.)" stays in its sentence.

_SENT_BOUNDARY_RE = re.compile(
    r"(?<![.!?\u2026])(?P<punct>\.{2,}+|\u2026++|[.!?]++)(?P<close>[\"'\u2019\u201d\u00bb)\]]*+)(?=\s)")
_NEVER_SPLIT = frozenset("""e.g E.g i.e I.e cf Cf vs Vs v approx Approx ca Fig fig Figs figs Eq eq Eqs eqs
Tab Tabs Sec Secs Ch ch Chap chap No Nos Vol vol Vols vols pp p Pp Dr Prof Mr Mrs Ms Mx Ref Refs ref refs
viz Viz Art Exp Expt Suppl Supp Fn Nr Pt Thm Prop Def Lem Eqn eqn""".split())
_AMBIGUOUS = frozenset("""Inc Ltd Co Corp Jr Sr Bros Ed ed Eds eds Univ Dept Assoc Ave St Mt Jan Feb Mar Apr Jun
Jul Aug Sep Sept Oct Nov Dec ibid Ibid resp""".split())
_LOWER_START_RE = re.compile(r"[a-z]{1,4}[A-Z]|[a-z]-[a-z]")
_STARTERS = frozenset("""The This These Those That There Their They Then Thus Therefore Third It Its In
Instead Indeed Importantly Interestingly If However Here Hence We Our While Whereas When Where Which What
Why How Who Whether With Without Within Although Also As At After Across Among Another Any All Accordingly
Additionally Again Because But Both By Before Between Beyond Consequently Conversely Despite During Each
Especially Every Finally First Firstly For From Further Furthermore Following Given He She His Her Lastly
Many Moreover Most Much Nevertheless Nonetheless Notably Not Nor Now No Of On One Only Other Others Overall
Second Secondly Several Similarly Since So Some Specifically Such Taken To Together Under Unlike Using Yet
You Your Participants Results Findings Future Recent Recently Previous Previously Next Neither Either
Unfortunately Fortunately Surprisingly Critically Crucially Clearly Ultimately Typically Generally
Collectively Subsequently Meanwhile Altogether Though""".split())
_STARTERS_AFTER_ABBR = _STARTERS | {"A", "An", "I"}
_LABEL_WORDS = frozenset("""condition conditions group groups type types vitamin appendix table figure fig study
experiment part section phase model plan option category class level grade factor block session list set
version form wave cohort site task trial panel item step stage unit hepatitis protein gene chromosome point
letter row column hypothesis scenario schedule sample subtype strain region area zone line plate series
exhibit annex chapter note case method approach test scale subscale sector room round route""".split())
_DOTTED_RE = re.compile(r"(?:[A-Za-z]{1,3}\.)+[A-Za-z]{1,3}")
_NUMBERING_RE = re.compile(r"\d+(?:\.\d+)*|[IVXLC]+|[A-Z]")


def _next_word(p, k):
    m = re.match(r"[\"'\u201c\u2018\u00ab(\[]*([\w\u2019'\-]+)", p[k:])
    return m.group(1) if m else ""


_LOOKBACK = 200  # characters before a full stop that the abbreviation checks need


def _is_boundary(p, start, m, k):
    punct, close = m.group("punct"), m.group("close")
    nxt_word = _next_word(p, k)
    if "!" in punct or "?" in punct:
        return True
    if punct.startswith("..") or punct.startswith("\u2026"):
        if punct.startswith("...."):
            return True
        if p[max(0, m.start() - 20):m.start()].rstrip().endswith(("[", "(")):
            return False
        return nxt_word in _STARTERS
    if close:
        return True
    # Only a bounded window is inspected, so a long run of non-boundaries
    # ("A. A. A. ...") stays linear instead of quadratic.
    lo = max(start, m.start() - _LOOKBACK)
    before = p[lo:m.start()]
    if not before or before[-1].isspace():
        return True
    parts = before.rsplit(None, 2)  # [..., previous word, token]
    token = parts[-1].lstrip("([{\"'\u201c\u2018").rstrip(")]}\"'\u201d\u2019")
    first = lo == start and len(parts) == 1
    prev_word = parts[-2].strip(",;:()\"'") if len(parts) > 1 else ""
    if token in _NEVER_SPLIT:
        return False
    if token == "al" and prev_word == "et":
        return not p[k].isdigit()
    if token in _AMBIGUOUS:
        return nxt_word in _STARTERS_AFTER_ABBR
    if first and _NUMBERING_RE.fullmatch(token):
        return False
    if len(token) == 1 and token.isupper():
        if nxt_word in _STARTERS:
            return True
        return prev_word.lower() in _LABEL_WORDS
    if _DOTTED_RE.fullmatch(token):
        return nxt_word in _STARTERS_AFTER_ABBR
    return True


_BRACKET_CHARS_RE = re.compile(r"[()\[\]]")


def _bracket_spans(p, limit=400):
    """Outermost balanced (...) / [...] spans of at most ``limit`` characters."""
    stack, spans = [], []
    for b in _BRACKET_CHARS_RE.finditer(p):
        ch, i = b.group(), b.start()
        if ch in "([":
            stack.append((ch, i))
        elif stack and stack[-1][0] == ("(" if ch == ")" else "["):
            s = stack.pop()[1]
            if i - s <= limit:
                spans.append((s, i))
    spans.sort()
    outer = []
    for s, e in spans:
        if not outer or s > outer[-1][1]:
            outer.append((s, e))
    return outer


def _split_paragraph(p):
    out, start = [], 0
    spans = _bracket_spans(p) if "(" in p or "[" in p else []
    si = 0
    n = len(p)
    for m in _SENT_BOUNDARY_RE.finditer(p):
        j = k = m.end()
        while k < n and p[k].isspace():
            k += 1
        if k >= n:
            continue
        c = p[k]
        if not (c.isupper() or c.isdigit()
                or (c in _OPEN_QUOTES and k + 1 < n and p[k + 1].isalnum())
                or (c in "([" and k + 1 < n and p[k + 1].isupper())
                or (c.islower() and _LOWER_START_RE.match(p, k))):
            continue
        while si < len(spans) and spans[si][1] < m.start():
            si += 1
        if si < len(spans) and spans[si][0] < m.start() and k < spans[si][1]:
            continue  # inside a short parenthesis: "(this was expected. It replicated.)"
        if not _is_boundary(p, start, m, k):
            continue
        sentence = p[start:j].strip()
        if sentence:
            out.append(sentence)
        start = k
    tail = p[start:].strip()
    if tail:
        out.append(tail)
    return out


def _best_cut(s, max_len):
    """Index at which to end the first piece of an over-long sentence."""
    window = s[:max_len + 1]
    min_pos = max(1, int(max_len * 0.3))
    depth = 0
    best = [None, None, None, None]  # ';'  ', : dash'  'space depth 0'  'any space'
    for i, ch in enumerate(window):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if i == 0 or i > max_len or not ch.isspace() or i < min_pos:
            continue
        prev = window[i - 1]
        if depth == 0 and prev == ";":
            best[0] = i
        elif depth == 0 and (prev in ",:" or prev in "\u2014\u2013"):
            best[1] = i
        if depth == 0:
            best[2] = i
        best[3] = i
    for cut in best:
        if cut is not None:
            return cut
    return max_len


def _split_long(s, max_len):
    pieces = []
    while len(s) > max_len:
        cut = _best_cut(s, max_len)
        head, s = s[:cut].rstrip(), s[cut:].lstrip()
        if head:
            pieces.append(head)
    if s:
        pieces.append(s)
    return pieces


def split_sentences(text: str, *, max_len: int | None = 1500) -> list[str]:
    """Split text into sentences (original wording, trimmed).

    Paragraph breaks always split.  Sentences longer than ``max_len`` are
    broken at clause boundaries (';' first, then ',' ':' or a dash, then
    spaces, at bracket depth 0 when possible, never in the first 30%), so
    every returned entry is at most ``max_len`` characters.  The pieces of a
    split sentence are simply consecutive entries; the first pieces end with
    their clause punctuation.  Pass ``max_len=None`` to disable.
    """
    if not isinstance(text, str):
        raise TypeError("split_sentences() expects a str")
    if max_len is not None and max_len < 1:
        raise ValueError("max_len must be positive")
    sentences = []
    for para in re.split(r"\n[ \t]*\n\s*", text):
        if not para.strip():
            continue
        for s in _split_paragraph(para):
            sentences.extend(_split_long(s, max_len) if max_len else [s])
    return sentences


# ---------------------------------------------------------------------------
# Chunking for synthesis
# ---------------------------------------------------------------------------

_ENDS_SPOKEN_RE = re.compile(r"[.!?\u2026:;,][\"'\u2019\u201d\u00bb)\]]*$")


def _speech_len(s):
    s = " ".join(s.split())
    return len(s) + (0 if _ENDS_SPOKEN_RE.search(s) else 1)


def chunk_sentences(sentences: list[str], first_limit: int = 250, limit: int = 1500) -> list[list[int]]:
    """Group consecutive sentence indices into synthesis chunks.

    Greedy: the first chunk holds at most ``first_limit`` characters (fast
    time-to-first-audio), later chunks at most ``limit``.  Length is measured
    as the text chunk_text() will produce (sentences joined by one space, plus
    a full stop for units without closing punctuation).  Every index appears
    exactly once, in order.  A single sentence longer than the cap gets a
    chunk of its own rather than being dropped or cut; split_sentences()
    already guarantees sentences are at most its max_len (1500 by default).
    """
    if first_limit < 1 or limit < 1:
        raise ValueError("limits must be positive")
    chunks, cur, cur_len = [], [], 0
    for i, s in enumerate(sentences):
        n = _speech_len(s)
        cap = first_limit if not chunks else limit
        if cur and cur_len + 1 + n > cap:
            chunks.append(cur)
            cur, cur_len = [], 0
        if cur:
            cur.append(i)
            cur_len += 1 + n
        else:
            cur, cur_len = [i], n
    if cur:
        chunks.append(cur)
    return chunks


def chunk_text(sentences: list[str], chunk: list[int]) -> str:
    """The text to send to the synthesiser for one chunk.

    Sentences are joined with single spaces and internal whitespace is
    collapsed.  A unit without closing punctuation (a heading, a bullet item)
    gets a full stop so the voice pauses instead of running it into the next
    sentence.
    """
    parts = []
    for i in chunk:
        s = " ".join(sentences[i].split())
        if s and not _ENDS_SPOKEN_RE.search(s):
            s += "."
        if s:
            parts.append(s)
    return " ".join(parts)


class Prepared(NamedTuple):
    text: str                 # normalised text
    sentences: list[str]      # navigation units for the panel
    chunks: list[list[int]]   # sentence indices per synthesis request


def prepare(text: str, *, skip_citations: bool = True, skip_urls: bool = True,
            first_limit: int = 250, limit: int = 1500) -> Prepared:
    """normalize -> split_sentences -> chunk_sentences in one call.

    If the first sentence is longer than ``first_limit`` it is split once at
    the last clause boundary (; , : or a dash) inside the first ``first_limit``
    characters (and at least 60 characters in), so audio can start quickly.
    It is never split at a bare space, which would sound like a hiccup.
    """
    clean = normalize(text, skip_citations=skip_citations, skip_urls=skip_urls)
    sentences = split_sentences(clean, max_len=limit)
    if sentences and _speech_len(sentences[0]) > first_limit:
        s = sentences[0]
        cut = None
        for m in re.finditer("[;,:\u2014\u2013](?=\\s)", s[:first_limit]):
            if m.end() >= 60:
                cut = m.end()
        if cut is not None:
            sentences[0:1] = [s[:cut].rstrip(), s[cut:].lstrip()]
    return Prepared(clean, sentences, chunk_sentences(sentences, first_limit, limit))


if __name__ == "__main__":  # quick manual check: python academic_text.py file.txt
    import sys
    with open(sys.argv[1], encoding="utf-8") as fh:
        result = prepare(fh.read())
    for n, sentence in enumerate(result.sentences):
        print(f"{n:4d}  {sentence}")

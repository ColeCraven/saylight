"""Text model for Saylight: shown text, sentences, chunk plans and word timing.

The text pipeline is `academic_text`. `normalize_pair` gives each paragraph twice: as shown (citations and links
kept, to be dimmed) and as spoken (exactly what `normalize` reads). `split_sentences` runs on the spoken text it
was tuned for, and `alignment` maps each spoken sentence back onto the shown text, so the card highlights the
word the voice is saying while the citations stay on screen, unread. `chunk_sentences` groups the spoken
sentences for synthesis, and `chunk_text`'s rule gives the spoken form (a unit without closing punctuation, such
as a heading, gets a full stop so the voice pauses). A file's notes and reference list follow the main text as
sections that are read only when the person asks. Nothing here logs text content.
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .academic_text import (chunk_sentences, chunk_text, normalize_pair, references_end, spoken_forms,
                            split_sentences)
from .alignment import Aligned, Edit, diff_edits
from .documents.model import DocSource, LoadedDocument
from .ocr.model import OcrText

log = logging.getLogger("saylight.text_model")

WORD_WINDOW = 40  # a word must be found within this many characters of the cursor

REGION_KINDS = ("skip", "said", "aside", "refs")
SECTION_KINDS = ("notes", "refs")
NOTES_HEADING = "Notes"
PAGE_LABEL = "Page {}"
PAGE_ANCHOR_WORDS = 5    # consecutive words that find a page's start in the shown text
PAGE_ANCHOR_TRIES = 40   # how far into a page to look for them (its first lines may be a running header)


@dataclass
class Sentence:
    """One sentence as shown: `start`/`end` are global offsets into `Document.text`.

    `spoken` is what the voice gets when it differs from `text` (citations left out, "e.g." said in full, a
    pronunciation); `align` maps offsets in `say` back onto `text`.
    """
    index: int
    text: str
    start: int
    end: int
    spoken: str = ""                 # "" means the same as text
    align: Aligned | None = None     # None means no difference

    @property
    def say(self) -> str:
        return self.spoken or self.text


@dataclass(frozen=True)
class Region:
    """Shown text to style: "skip" (dimmed, not read), "said" (read differently; not styled), "aside" (the notes
    section, not read) or "refs" (the reference list, not read)."""
    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class BuildOptions:
    skip_citations: bool = True
    skip_urls: bool = True
    read_references: bool = False
    read_asides: bool = False
    pronunciations: Any = None   # a PronunciationList (anything with apply(Aligned) -> Aligned), or None
    spoken_forms: bool = True    # "Fig. 2" said "figure 2", "e.g." said "for example"


@dataclass
class Document:
    """The shown text, its sentences with exact offsets, and what to style or skip."""
    text: str
    sentences: list[Sentence]
    regions: list[Region] = field(default_factory=list)
    source: DocSource | None = None
    # A file's notes and reference list after the main text: SECTION_KINDS name -> shown offset of its heading.
    # The blank row before it holds the card's rule and "Read the ... too" chip. A section with an "aside" or
    # "refs" region is skipped; one without is read aloud.
    sections: dict[str, int] = field(default_factory=dict)
    page_starts: list[int] = field(default_factory=list)  # shown offset of each page's start (page k + 1 at k)
    _page_marks: list[tuple[int, int]] = field(default_factory=list, repr=False, compare=False)
    _spoken_tail: list[int] | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def from_raw(cls, raw: str, *, skip_citations: bool = True, skip_urls: bool = True,
                 pronunciations: Any = None) -> Document:
        """A Document from highlighted or typed text: paragraphs joined with "\\n\\n"."""
        return cls.build(raw, BuildOptions(skip_citations=skip_citations, skip_urls=skip_urls,
                                           pronunciations=pronunciations))

    @classmethod
    def build(cls, raw: str | LoadedDocument | OcrText, options: BuildOptions) -> Document:
        """A Document from text (or the text OCR found), or from a file's text with its notes and reference list
        after the main text."""
        b = _Builder(options)
        if isinstance(raw, LoadedDocument):
            b.loaded(raw)
        else:
            b.body(getattr(raw, "text", raw))  # text, or an OcrText from the screen
        return b.document()

    # --- queries ----------------------------------------------------------------------------------------------

    def skipped(self) -> list[tuple[int, int]]:
        """Shown ranges that are not read aloud (citations, unread notes and references), in order."""
        return [(r.start, r.end) for r in self.regions if r.kind != "said"]

    def sentence_at_offset(self, offset: int) -> int:
        """The sentence holding shown `offset`, else the next one after it, else the last; -1 with none."""
        if not self.sentences:
            return -1
        k = bisect.bisect_right([s.start for s in self.sentences], offset) - 1
        if k >= 0 and offset < self.sentences[k].end:
            return k
        return min(k + 1, len(self.sentences) - 1)

    def fingerprint(self) -> str:
        """SHA-256 of the shown text: the same file read the same way gives the same value."""
        return hashlib.sha256(self.text.encode("utf-8", "surrogatepass")).hexdigest()

    def spoken_chars_from(self, sentence: int) -> int:
        """Characters the voice still has to say from `sentence` on, counting one space between sentences."""
        if self._spoken_tail is None:
            tail = [0] * (len(self.sentences) + 1)
            for k in range(len(self.sentences) - 1, -1, -1):
                tail[k] = tail[k + 1] + len(self.sentences[k].say) + (1 if k + 1 < len(self.sentences) else 0)
            self._spoken_tail = tail
        return self._spoken_tail[max(0, min(sentence, len(self.sentences)))]

    def page_of(self, sentence: int) -> int | None:
        """The 1-based page a sentence is on, or None when the text has no pages."""
        if not self._page_marks or not 0 <= sentence < len(self.sentences):
            return None
        k = bisect.bisect_right(self._page_marks, (self.sentences[sentence].start, 1 << 30)) - 1
        return self._page_marks[max(0, k)][1]


# --- Building --------------------------------------------------------------------------------------------------

_WORDS_RE = re.compile(r"[^\W\d_]{2,}")


def _squash(s: str) -> str:
    return s if "  " not in s and not any(c.isspace() and c != " " for c in s) else " ".join(s.split())


def _trimmed(text: str, a: int, b: int) -> tuple[int, int]:
    while a < b and text[a].isspace():
        a += 1
    while b > a and text[b - 1].isspace():
        b -= 1
    return a, b


class _Builder:
    """Lays out paragraphs one after another ("\\n\\n" between them) and collects sentences and regions."""

    def __init__(self, options: BuildOptions) -> None:
        self.o = options
        self.parts: list[str] = []
        self.pos = 0
        self.sentences: list[Sentence] = []
        self.regions: list[Region] = []
        self.sections: dict[str, int] = {}
        self.marks: list[tuple[int, int]] = []
        self.page_starts: list[int] = []
        self.speak = True
        self.fallbacks = 0
        self._grams: dict[tuple[int, int], dict[tuple[str, ...], list[int]]] = {}

    # --- text and files

    def body(self, raw: str) -> tuple[int, int]:
        start = self.pos + (2 if self.parts else 0)
        for shown, spoken in normalize_pair(raw, skip_citations=self.o.skip_citations, skip_urls=self.o.skip_urls):
            self.paragraph(shown, spoken)
        return start, self.pos

    def loaded(self, ld: LoadedDocument) -> None:
        text = ld.text
        refs = None
        if ld.references_at is not None and 0 <= ld.references_at < len(text):
            refs = (ld.references_at, references_end(text, ld.references_at))
        pieces = [(0, refs[0]), (refs[1], len(text))] if refs else [(0, len(text))]
        spans = []  # (raw start, raw end, shown start, shown end)
        for a, b in pieces:
            if a < b:
                spans.append((a, b, *self.body(text[a:b])))
        if ld.asides:
            self.notes(ld)
        if refs:
            self.section("refs", self.o.read_references, lambda: spans.append((*refs, *self.body(text[slice(*refs)]))))
        if ld.page_starts:
            self.pages(ld, spans)

    def notes(self, ld: LoadedDocument) -> None:
        groups: dict[int | None, list[str]] = defaultdict(list)
        for aside in ld.asides:
            groups[aside.page].append(aside.text)

        def write():
            first = None
            heading = self.paragraph(NOTES_HEADING, NOTES_HEADING)
            for page in sorted(groups, key=lambda p: (p is not None, p or 0)):
                at = self.pos + 2
                if page is not None:
                    self.paragraph(PAGE_LABEL.format(page), PAGE_LABEL.format(page))
                    self.marks.append((at, page))
                    first = page if first is None else first
                for aside in groups[page]:
                    self.body(aside)
            if first is not None:
                self.marks.append((heading, first))
        self.section("notes", self.o.read_asides, write)

    def section(self, kind: str, read: bool, write) -> None:
        start = self.pos + (2 if self.parts else 0)
        speak, self.speak = self.speak, read
        try:
            write()
        finally:
            self.speak = speak
        if self.pos > start:
            self.sections[kind] = start
            if not read:
                self.regions.append(Region(start, self.pos, "aside" if kind == "notes" else kind))

    # --- one paragraph

    def paragraph(self, shown: str, spoken: str) -> int:
        """Append one paragraph; returns its shown start."""
        if self.parts:
            self.parts.append("\n\n")
            self.pos += 2
        base = self.pos
        shown, spoken = _squash(shown), _squash(spoken)
        self.parts.append(shown)
        self.pos += len(shown)
        if not self.speak:
            return base
        al = Aligned(shown, diff_edits(shown, spoken))
        if al.spoken != spoken:  # cannot happen (the comparison is exact); read the paragraph as normalize() says
            self.fallbacks += 1
            al = Aligned(shown, [Edit(0, len(shown), spoken, "say" if spoken else "skip")])
        for a, b in al.ranges("skip"):
            a, b = _trimmed(shown, a, b)
            if a < b:
                self.regions.append(Region(base + a, base + b, "skip"))
        cursor = 0
        for piece in split_sentences(spoken) if spoken else []:
            a = spoken.find(piece, cursor)
            b = a + len(piece)
            cursor = b
            ds, de = al.range(a, b)
            sub = al.slice(ds, de)
            if sub.spoken != piece:  # an edit straddles the boundary: say the piece, map it coarsely
                sub = Aligned(shown[ds:de], [Edit(0, de - ds, piece, "say")])
            sub = self.respell(sub, base + ds)
            text = shown[ds:de]
            self.sentences.append(Sentence(len(self.sentences), text, base + ds, base + de,
                                           "" if sub.spoken == text else sub.spoken, sub if sub.edits else None))
            for x, y in sub.ranges("say") + sub.ranges("pron"):
                self.regions.append(Region(base + ds + x, base + ds + y, "said"))
        return base

    def respell(self, sub: Aligned, at: int) -> Aligned:
        """The person's pronunciations first, then the built-in spoken forms where the text is still as shown."""
        if self.o.pronunciations is not None:
            before = set(sub.ranges("skip"))
            sub = self.o.pronunciations.apply(sub)
            for x, y in sub.ranges("skip"):  # an entry said as "" is dimmed like a citation
                if (x, y) not in before:
                    a, b = _trimmed(sub.display, x, y)
                    if a < b:
                        self.regions.append(Region(at + a, at + b, "skip"))
        if self.o.spoken_forms:
            extra = [Edit(*sub.range(a, b), said, "say") for a, b, said in spoken_forms(sub.spoken)
                     if sub.is_exact_range(a, b)]
            if extra:
                sub = sub.with_edits(extra)
        return sub

    # --- pages

    def pages(self, ld: LoadedDocument, spans: list[tuple[int, int, int, int]]) -> None:
        """Find where each page starts in the shown text: its first few words, searched within its own part."""
        text = "".join(self.parts)
        starts = list(ld.page_starts) + [len(ld.text)]
        found: list[int | None] = []
        for k, p in enumerate(starts[:-1]):
            span = next((s for s in spans if s[0] <= p < s[1]), None)
            if span is None:
                found.append(None)
                continue
            if p == span[0]:
                found.append(span[2])
                continue
            words = [w.casefold() for w in _WORDS_RE.findall(ld.text, p, min(starts[k + 1], p + 4000))]
            found.append(self._anchor(text, words, span, found))
        for k, at in enumerate(found):
            if at is not None:
                self.marks.append((at, k + 1))
            else:  # not found: no mark, so the sentences there keep the page before
                found[k] = found[k - 1] if k else 0
        self.page_starts = [int(x) for x in found]
        for raw_a, _, shown_a, _ in spans:  # each part starts on the page its raw text starts on
            self.marks.append((shown_a, bisect.bisect_right(starts, raw_a) or 1))

    def _anchor(self, text, words, span, found) -> int | None:
        key = (span[2], span[3])
        index = self._grams.get(key)
        if index is None:
            shown = [(m.start(), m.group().casefold()) for m in _WORDS_RE.finditer(text, span[2], span[3])]
            index = defaultdict(list)
            n = PAGE_ANCHOR_WORDS
            for i in range(len(shown) - n + 1):
                index[tuple(w for _, w in shown[i:i + n])].append(shown[i][0])
            self._grams[key] = index
        after = max([f for f in found if f is not None and span[2] <= f <= span[3]], default=span[2])
        for i in range(min(PAGE_ANCHOR_TRIES, max(0, len(words) - PAGE_ANCHOR_WORDS + 1))):
            hits = index.get(tuple(words[i:i + PAGE_ANCHOR_WORDS]))
            if hits:
                j = bisect.bisect_left(hits, after)
                if j < len(hits):
                    at = hits[j]
                    if i == 0:  # a heading number before the first word ("3 Results") is on the page too
                        while at > after and hits[j] - at < 12 and text[at - 1] in " 0123456789.":
                            at -= 1
                        at = at if at == span[2] or text[at - 1] in "\n " else hits[j]
                    return at
        return None

    # --- result

    def document(self) -> Document:
        if self.fallbacks:
            log.warning("Shown and spoken text disagreed in %d paragraphs; read them as cleaned", self.fallbacks)
        self.regions.sort(key=lambda r: (r.start, r.end))
        self.marks.sort()
        doc = Document("".join(self.parts), self.sentences, self.regions, None, self.sections, self.page_starts)
        doc._page_marks = self.marks
        return doc


@dataclass
class ChunkPlan:
    """One synthesis request: whole sentences in their spoken form, joined with single spaces."""
    chunk_id: int
    sentence_ids: list[int]
    text: str
    offsets: list[int]  # each sentence's start offset within `text`
    ends: list[int] = field(default_factory=list)  # each sentence's end offset (before any added ".")
    aligns: list[Aligned | None] = field(default_factory=list)  # each sentence's spoken -> shown map

    def __post_init__(self) -> None:
        if not self.ends:  # built from the first four fields only: sentences joined by single spaces
            self.ends = [nxt - 1 for nxt in self.offsets[1:]] + ([len(self.text)] if self.offsets else [])

    @property
    def first(self) -> int:
        return self.sentence_ids[0]

    @property
    def last(self) -> int:
        return self.sentence_ids[-1]

    def sentence_bounds(self, j: int) -> tuple[int, int]:
        """(start, end) of the j-th sentence of this chunk, as offsets within `text`."""
        return self.offsets[j], self.ends[j]


def plan_chunks(doc: Document, start_sentence: int, *, first_limit: int = 250, limit: int = 1500,
                first_chunk_id: int, max_chunks: int | None = None) -> list[ChunkPlan]:
    """Group the sentences from `start_sentence` to the end into chunks (ids from first_chunk_id).

    The chunk text is what academic_text.chunk_text() produces from the spoken sentences (`Sentence.say`), so
    its length matches what chunk_sentences() budgeted; each sentence's spoken characters sit at
    text[offsets[j]:ends[j]], and `aligns[j]` maps them onto the shown sentence.
    `max_chunks` returns only the first chunks, exactly as the full plan has them, without
    planning the rest of a long document.
    """
    tail = doc.sentences[start_sentence:]
    if max_chunks is not None:
        tail = tail[:_prefix_for(tail, max_chunks, max(first_limit, limit))]
    groups = chunk_sentences([s.say for s in tail], first_limit=first_limit, limit=limit)
    if max_chunks is not None:
        groups = groups[:max_chunks]  # a shortened prefix's last group may be cut short; never used
    plans = []
    for k, group in enumerate(groups):
        ids = [start_sentence + i for i in group]
        parts, offsets, ends, aligns, pos = [], [], [], [], 0
        for sid in ids:
            sentence = doc.sentences[sid].say
            aligns.append(doc.sentences[sid].align)
            spoken = chunk_text([sentence], [0])
            if not spoken.startswith(sentence):  # cannot happen for collapsed text; keep offsets exact
                spoken = sentence
            parts.append(spoken)
            offsets.append(pos)
            ends.append(pos + len(sentence))
            pos += len(spoken) + 1
        plans.append(ChunkPlan(first_chunk_id + k, ids, " ".join(parts), offsets, ends, aligns))
    return plans


def _prefix_for(tail: list[Sentence], chunks: int, cap: int) -> int:
    """How many sentences of `tail` are enough to group its first `chunks` chunks exactly.

    chunk_sentences() is greedy: a chunk closes when the next sentence would not fit. Every chunk holds
    at most max(cap, longest sentence + 1) spoken characters, so a prefix longer than chunks + 2 such
    chunks contains more than `chunks` closed chunks, each grouped as in the whole tail.
    """
    total = longest = 0
    for k, sentence in enumerate(tail):
        n = len(sentence.say)
        total += n + 1
        longest = max(longest, n)
        if total > (chunks + 2) * (max(cap, longest + 1) + 1):
            return k + 1
    return len(tail)


@dataclass
class WordSpan:
    """A spoken word: `lo`/`hi` are character offsets within its shown sentence; times in chunk ms."""
    sentence: int
    lo: int
    hi: int
    start_ms: float
    end_ms: float


@dataclass
class ChunkTiming:
    """Word and sentence timing of one chunk's audio, in ms from the start of its file."""
    words: list[WordSpan]
    sentence_ms: dict[int, tuple[float, float]]
    proportional: bool
    _word_starts: list[float] = field(init=False, repr=False, compare=False)
    _sentence_order: list[int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._word_starts = [w.start_ms for w in self.words]
        self._sentence_order = sorted(self.sentence_ms, key=lambda s: (self.sentence_ms[s][0], s))

    def has_words(self, sentence: int) -> bool:
        """True when the sentence has at least one mapped word (real timing, not interpolated)."""
        return any(w.sentence == sentence for w in self.words)

    def sentence_at(self, pos_ms: float, default: int) -> int:
        """The last sentence whose start is at or before pos_ms (default if none has started)."""
        current = default
        for sid in self._sentence_order:
            if self.sentence_ms[sid][0] <= pos_ms:
                current = sid
            else:
                break
        return current

    def word_at(self, pos_ms: float, sentence: int) -> tuple[WordSpan | None, bool]:
        """(the last word of `sentence` that started at or before pos_ms, whether pos is inside it)."""
        i = bisect.bisect_right(self._word_starts, pos_ms) - 1
        while i >= 0 and self.words[i].sentence != sentence:
            if self.words[i].sentence < sentence:
                return None, False
            i -= 1
        if i < 0:
            return None, False
        word = self.words[i]
        return word, pos_ms < word.end_ms


def _is_boundary(text: str, i: int) -> bool:
    return i == 0 or not text[i - 1].isalnum()


def _find_in(haystack: str, needle: str, text: str, lo: int, hi: int) -> int:
    """First match of needle starting in [lo, hi), preferring one at a word boundary of `text`."""
    first = -1
    i = haystack.find(needle, lo)
    while 0 <= i < hi:
        if _is_boundary(text, i):
            return i
        if first < 0:
            first = i
        i = haystack.find(needle, i + 1)
    return first


def _find_word(text: str, word: str, lo: int, hi: int) -> int:
    """Index of `word` starting in [lo, hi) of text: exact match first, then case-insensitive."""
    i = _find_in(text, word, text, lo, hi)
    if i < 0:
        folded = text.casefold()
        if len(folded) == len(text):  # otherwise folded offsets would not line up with text
            i = _find_in(folded, word.casefold(), text, lo, hi)
    return i


def _proportional(plan: ChunkPlan, lo_ms: float, hi_ms: float, js: list[int]) -> dict[int, tuple[float, float]]:
    """Spread [lo_ms, hi_ms] over the chunk's sentences `js` by character share."""
    weights = [max(1, len(plan.text[a:b])) for a, b in (plan.sentence_bounds(j) for j in js)]
    total = sum(weights)
    out, t = {}, lo_ms
    for j, w in zip(js, weights):
        nxt = t + (hi_ms - lo_ms) * w / total
        out[plan.sentence_ids[j]] = (t, nxt)
        t = nxt
    return out


def map_words(plan: ChunkPlan, words: list, length_ms: int | float | None) -> ChunkTiming:
    """Map WordBoundary events onto the chunk's sentences, with a proportional fallback.

    `words` are synth.Word-like objects (offset_ms, duration_ms, text). They are found in the spoken chunk text,
    then mapped onto the shown sentence, so "F", "M", "R" and "I" all highlight "fMRI" and a skipped citation
    is never highlighted. Words that cannot be found within WORD_WINDOW characters of the cursor are skipped.
    Sentences with no mapped words are interpolated by character share. With no words at all the timing is
    proportional to `length_ms` (empty when the length is unknown) and the engine must not cut inside the chunk.
    """
    spans: list[WordSpan] = []
    cursor = slack = 0
    for word in words:
        token = word.text.strip()
        if not token:
            continue
        i = _find_word(plan.text, token, cursor, cursor + WORD_WINDOW + slack + 1)
        j = bisect.bisect_right(plan.offsets, i) - 1 if i >= 0 else -1
        if j < 0 or i >= plan.sentence_bounds(j)[1]:  # not found, or only in an added "." or separator
            slack += len(token) + 1  # keep searching past the words we could not place
            continue
        slack = 0
        cursor = i + len(token)
        start, end = plan.sentence_bounds(j)
        lo, hi = i - start, min(cursor, end) - start   # spoken offsets within the sentence
        if plan.aligns and plan.aligns[j] is not None:
            lo, hi = plan.aligns[j].range(lo, hi)      # -> shown offsets
        spans.append(WordSpan(plan.sentence_ids[j], lo, hi,
                              float(word.offset_ms), float(word.offset_ms + word.duration_ms)))
    skipped = sum(1 for w in words if w.text.strip()) - len(spans)
    if skipped:
        log.debug("map_words: %d of %d words not placed in chunk %d", skipped, len(words), plan.chunk_id)

    js = list(range(len(plan.sentence_ids)))
    if not spans:
        timing = _proportional(plan, 0.0, float(length_ms), js) if length_ms else {}
        return ChunkTiming([], timing, True)

    sentence_ms: dict[int, tuple[float, float]] = {}
    for span in spans:
        lo, hi = sentence_ms.get(span.sentence, (span.start_ms, span.end_ms))
        sentence_ms[span.sentence] = (min(lo, span.start_ms), max(hi, span.end_ms))

    mapped_chars = sum(len(plan.text[a:b]) for a, b in (plan.sentence_bounds(j) for j in js)
                       if plan.sentence_ids[j] in sentence_ms)
    mapped_ms = sum(b - a for a, b in sentence_ms.values())
    ms_per_char = mapped_ms / mapped_chars if mapped_chars else 0.0

    run: list[int] = []
    for j in js + [None]:
        if j is not None and plan.sentence_ids[j] not in sentence_ms:
            run.append(j)
            continue
        if run:
            before = plan.sentence_ids[run[0] - 1] if run[0] > 0 else None
            lo_ms = sentence_ms[before][1] if before is not None else 0.0
            if j is not None:
                hi_ms = sentence_ms[plan.sentence_ids[j]][0]
            elif length_ms:
                hi_ms = float(length_ms)
            else:
                chars = sum(len(plan.text[a:b]) for a, b in (plan.sentence_bounds(k) for k in run))
                hi_ms = lo_ms + chars * ms_per_char
            sentence_ms.update(_proportional(plan, lo_ms, max(lo_ms, hi_ms), run))
            run = []
    return ChunkTiming(spans, sentence_ms, False)

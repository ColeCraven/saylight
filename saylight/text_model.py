"""Text model for Saylight: display text, sentences, chunk plans and word timing.

The text pipeline is `academic_text`: `normalize` gives the display text,
`split_sentences` the navigation units, `chunk_sentences` the synthesis groups, and `chunk_text`'s
rule the spoken form (a unit without closing punctuation, such as a heading, gets a full stop so
the voice pauses). Nothing here logs text content.
"""

import bisect
import logging
from dataclasses import dataclass, field

from .academic_text import chunk_sentences, chunk_text, normalize, split_sentences

log = logging.getLogger("saylight.text_model")

WORD_WINDOW = 40  # a word must be found within this many characters of the cursor


@dataclass
class Sentence:
    """One sentence; `start`/`end` are global character offsets into `Document.text`."""
    index: int
    text: str
    start: int
    end: int


@dataclass
class Document:
    """The display text (normalized) and its sentences, with exact offsets."""
    text: str
    sentences: list[Sentence]

    @classmethod
    def from_raw(cls, raw: str, *, skip_citations: bool = True, skip_urls: bool = True) -> "Document":
        """Normalize, split and join: sentences joined with " ", paragraphs with "\\n\\n"."""
        normalized = normalize(raw, skip_citations=skip_citations, skip_urls=skip_urls)
        paragraphs = []
        for paragraph in normalized.split("\n\n"):
            pieces = [" ".join(p.split()) for p in split_sentences(paragraph)]
            pieces = [p for p in pieces if p]
            if pieces:
                paragraphs.append(pieces)
        parts: list[str] = []
        sentences: list[Sentence] = []
        pos = 0
        for pieces in paragraphs:
            for j, piece in enumerate(pieces):
                if sentences:
                    sep = " " if j else "\n\n"
                    parts.append(sep)
                    pos += len(sep)
                sentences.append(Sentence(len(sentences), piece, pos, pos + len(piece)))
                parts.append(piece)
                pos += len(piece)
        return cls("".join(parts), sentences)


@dataclass
class ChunkPlan:
    """One synthesis request: whole sentences in their spoken form, joined with single spaces."""
    chunk_id: int
    sentence_ids: list[int]
    text: str
    offsets: list[int]  # each sentence's start offset within `text`
    ends: list[int] = field(default_factory=list)  # each sentence's end offset (before any added ".")

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

    The chunk text is what academic_text.chunk_text() produces, so its length matches what
    chunk_sentences() budgeted; each sentence's own characters sit at text[offsets[j]:ends[j]].
    `max_chunks` returns only the first chunks, exactly as the full plan has them, without
    planning the rest of a long document.
    """
    tail = doc.sentences[start_sentence:]
    if max_chunks is not None:
        tail = tail[:_prefix_for(tail, max_chunks, max(first_limit, limit))]
    groups = chunk_sentences([s.text for s in tail], first_limit=first_limit, limit=limit)
    if max_chunks is not None:
        groups = groups[:max_chunks]  # a shortened prefix's last group may be cut short; never used
    plans = []
    for k, group in enumerate(groups):
        ids = [start_sentence + i for i in group]
        parts, offsets, ends, pos = [], [], [], 0
        for sid in ids:
            sentence = doc.sentences[sid].text
            spoken = chunk_text([sentence], [0])
            if not spoken.startswith(sentence):  # cannot happen for collapsed text; keep offsets exact
                spoken = sentence
            parts.append(spoken)
            offsets.append(pos)
            ends.append(pos + len(sentence))
            pos += len(spoken) + 1
        plans.append(ChunkPlan(first_chunk_id + k, ids, " ".join(parts), offsets, ends))
    return plans


def _prefix_for(tail: list[Sentence], chunks: int, cap: int) -> int:
    """How many sentences of `tail` are enough to group its first `chunks` chunks exactly.

    chunk_sentences() is greedy: a chunk closes when the next sentence would not fit. Every chunk holds
    at most max(cap, longest sentence + 1) spoken characters, so a prefix longer than chunks + 2 such
    chunks contains more than `chunks` closed chunks, each grouped as in the whole tail.
    """
    total = longest = 0
    for k, sentence in enumerate(tail):
        total += len(sentence.text) + 1
        longest = max(longest, len(sentence.text))
        if total > (chunks + 2) * (max(cap, longest + 1) + 1):
            return k + 1
    return len(tail)


@dataclass
class WordSpan:
    """A spoken word: `lo`/`hi` are character offsets within its sentence; times in chunk ms."""
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

    `words` are synth.Word-like objects (offset_ms, duration_ms, text). Words that cannot be found
    within WORD_WINDOW characters of the cursor are skipped. Sentences with no mapped words are
    interpolated by character share. With no words at all the timing is proportional to
    `length_ms` (empty when the length is unknown) and the engine must not cut inside the chunk.
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
        spans.append(WordSpan(plan.sentence_ids[j], i - start, min(cursor, end) - start,
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

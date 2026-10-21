"""The shown text and the spoken text, and how positions in one map to the other.

The reading card shows a paragraph as the file has it, citations and links included; the voice gets a cleaned
version. An `Aligned` holds the shown text plus non-overlapping `Edit`s in shown coordinates:

    Edit(start, end, "", "skip")             shown (dimmed), never spoken: "(Smith et al., 2019)"
    Edit(start, end, "link", "say")          shown as is, spoken differently: a URL, "[3]", "e.g."
    Edit(start, end, "F M R I", "pron")      the person's pronunciation list

The spoken text and a piecewise map between the two are derived from the edits. Lookups are one binary search;
building is linear. Standard library only; nothing here logs text.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

EDIT_KINDS = ("skip", "say", "pron")


@dataclass(frozen=True)
class Edit:
    """Shown text [start, end) is spoken as `spoken` ("" for not at all)."""
    start: int
    end: int
    spoken: str
    kind: str = "skip"   # one of EDIT_KINDS


class Aligned:
    """Shown text plus edits: the spoken text, and spoken <-> shown offsets.

    The spoken text is made of stretches of unedited text and the replacements of "say" and "pron" edits (a
    skip takes no spoken room). A position inside a replacement maps to its whole shown range, so each of
    "F", "M", "R" and "I" highlights "fMRI".
    """

    __slots__ = ("display", "edits", "spoken", "_s0", "_s1", "_d0", "_d1", "_exact")

    def __init__(self, display: str, edits: Sequence[Edit] = ()) -> None:
        edits = sorted(edits, key=lambda e: (e.start, e.end))
        prev = 0
        for e in edits:
            if e.start < prev or e.end < e.start or e.end > len(display):
                raise ValueError(f"overlapping or out-of-range edit at {e.start}-{e.end}")
            prev = e.end
        self.display = display
        self.edits = edits
        s0: list[int] = []
        s1: list[int] = []
        d0: list[int] = []
        d1: list[int] = []
        exact: list[bool] = []
        parts: list[str] = []
        s = d = 0

        def stretch(spoken_len, a, b, is_exact):
            nonlocal s
            s0.append(s)
            s += spoken_len
            s1.append(s)
            d0.append(a)
            d1.append(b)
            exact.append(is_exact)

        for e in edits:
            if e.start > d:
                stretch(e.start - d, d, e.start, True)
                parts.append(display[d:e.start])
            if e.spoken:
                stretch(len(e.spoken), e.start, e.end, False)
                parts.append(e.spoken)
            d = e.end
        if d < len(display):
            stretch(len(display) - d, d, len(display), True)
            parts.append(display[d:])
        self.spoken = "".join(parts)
        self._s0, self._s1, self._d0, self._d1, self._exact = s0, s1, d0, d1, exact

    def __repr__(self) -> str:
        return f"Aligned({len(self.display)} shown, {len(self.spoken)} spoken, {len(self.edits)} edits)"

    # --- spoken -> shown ------------------------------------------------------------------------------------

    def lo(self, i: int) -> int:
        """Shown offset where spoken character i starts (a replacement maps to the start of its shown range)."""
        if not self._s0:
            return 0
        k = bisect.bisect_right(self._s0, i) - 1
        if k < 0:
            return self._d0[0]
        if i >= self._s1[k]:
            return self._d1[k]
        return self._d0[k] + (i - self._s0[k]) if self._exact[k] else self._d0[k]

    def hi(self, j: int) -> int:
        """Shown offset where a spoken range ending at j ends."""
        if not self._s1:
            return 0
        k = bisect.bisect_left(self._s1, j)
        if k >= len(self._s1):
            return self._d1[-1]
        if j <= self._s0[k]:
            return self._d0[k]
        return self._d0[k] + (j - self._s0[k]) if self._exact[k] else self._d1[k]

    def range(self, i: int, j: int) -> tuple[int, int]:
        """The shown range of spoken [i, j)."""
        a = self.lo(i)
        return a, max(a, self.hi(j))

    # --- shown -> spoken (clicks, resume positions) -------------------------------------------------------

    def to_spoken(self, d: int) -> int:
        """Spoken offset of shown offset d; inside a skip it is the spoken position just after it."""
        if not self._d0:
            return 0
        k = bisect.bisect_right(self._d0, d) - 1
        if k < 0:
            return 0
        if d < self._d1[k]:
            return self._s0[k] + (d - self._d0[k]) if self._exact[k] else self._s0[k]
        return self._s1[k]

    def is_exact_range(self, i: int, j: int) -> bool:
        """True when spoken [i, j) lies inside one stretch of unedited shown text."""
        k = bisect.bisect_right(self._s0, i) - 1
        return 0 <= k and self._exact[k] and j <= self._s1[k]

    # --- edits ----------------------------------------------------------------------------------------------

    def ranges(self, kind: str) -> list[tuple[int, int]]:
        """Shown ranges of the edits of one kind."""
        return [(e.start, e.end) for e in self.edits if e.kind == kind]

    def slice(self, a: int, b: int) -> Aligned:
        """Shown [a, b) with the edits inside it, re-based to 0."""
        inside = [Edit(e.start - a, e.end - a, e.spoken, e.kind) for e in self.edits
                  if e.start >= a and e.end <= b and not (e.start == e.end == b and b != a)]
        return Aligned(self.display[a:b], inside)

    def with_edits(self, extra: Sequence[Edit]) -> Aligned:
        """The same shown text with more edits (they must not overlap the existing ones)."""
        return Aligned(self.display, list(self.edits) + list(extra))


# --- Deriving the edits: compare a shown paragraph with its spoken form, token by token -----------------------

_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]")


def _tokens(s: str) -> tuple[list[str], list[int]]:
    toks = _TOKEN_RE.findall(s)
    offs = [0]
    for t in toks:
        offs.append(offs[-1] + len(t))
    return toks, offs


_WHOLE_TOKENS = 400    # a paragraph this short is compared in one go
_WINDOW_TOKENS = 64    # a longer one a window at a time, from each change ...
_SYNC_TOKENS = 8       # ... to the first unchanged run this long (about four words)


def _opcodes(a: list[str], b: list[str]) -> list[tuple[str, int, int, int, int]]:
    """SequenceMatcher opcodes from token list `a` to `b`.

    Comparing a whole paragraph costs roughly the cube of its length (spaces and common words match
    everywhere), and autojunk isn't a way out: it anchors on the first rare word, which can be an author's name
    inside a citation, and misaligns the rest. normalize()'s changes are local, so a long paragraph walks the
    unchanged text and compares a small window at each change, widening it until it reaches an unchanged run
    long enough to trust. The opcodes still cover both lists end to end.
    """
    if len(a) <= _WHOLE_TOKENS and len(b) <= _WHOLE_TOKENS:
        return SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    ops: list[tuple[str, int, int, int, int]] = []
    i = j = 0
    while True:
        i0, j0 = i, j
        while i < len(a) and j < len(b) and a[i] == b[j]:
            i, j = i + 1, j + 1
        if i > i0:
            ops.append(("equal", i0, i, j0, j))
        if i == len(a) or j == len(b):
            if i < len(a):
                ops.append(("delete", i, len(a), j, j))
            elif j < len(b):
                ops.append(("insert", i, i, j, len(b)))
            return ops
        w = _WINDOW_TOKENS
        while True:
            ea, eb = min(len(a), i + w), min(len(b), j + w)
            sub = SequenceMatcher(None, a[i:ea], b[j:eb], autojunk=False).get_opcodes()
            cut = next((k for k, (tag, i1, i2, j1, j2) in enumerate(sub) if tag == "equal" and (
                i2 - i1 >= _SYNC_TOKENS or (i + i2 == len(a) and j + j2 == len(b)))), None)
            if cut is not None or (ea == len(a) and eb == len(b)):
                break
            w *= 2
        for tag, i1, i2, j1, j2 in (sub if cut is None else sub[:cut]):
            ops.append((tag, i + i1, i + i2, j + j1, j + j2))
        i, j = ops[-1][2], ops[-1][4]


_OPEN, _CLOSE = frozenset("([{"), frozenset(")]}")


def _unbracketed(tokens: list[str], lo: int, hi: int, piece: list[str]) -> int | None:
    """The first place in tokens[lo:hi] where `piece` sits outside brackets, else None."""
    depth = 0
    for q in range(lo, hi - len(piece) + 1):
        if depth == 0 and tokens[q:q + len(piece)] == piece:
            return q
        if tokens[q] in _OPEN:
            depth += 1
        elif tokens[q] in _CLOSE:
            depth = max(0, depth - 1)
    return None


def diff_edits(display: str, spoken: str) -> list[Edit]:
    """Edits that turn `display` into exactly `spoken`.

    normalize() only removes or replaces stretches, so a token comparison finds them. A short unchanged piece
    between two changes (at most 2 tokens and 3 characters, no space) joins them into one edit, so "[3]" ->
    "reference 3" is one "say" edit rather than three. Two removals either side of kept punctuation stay apart,
    and the copy kept is the one outside brackets: "(Smith et al., 2019). [2]" keeps both citations dimmed and
    the full stop after "2019)" with its sentence, rather than the one in "al.". Exact by construction:
    Aligned(display, diff_edits(display, spoken)).spoken == spoken for any two strings.
    """
    if display == spoken:
        return []
    if not spoken:
        return [Edit(0, len(display), "", "skip")]
    dt, do = _tokens(display)
    st, so = _tokens(spoken)
    merged: list[tuple[str, int, int, int, int]] = []
    for op in _opcodes(dt, st):
        if op[0] == "delete" and len(merged) >= 2 and merged[-1][0] == "equal" and merged[-2][0] == "delete":
            (_, a1, _, j1, _), (_, e1, e2, _, j2) = merged[-2], merged[-1]
            q = _unbracketed(dt, a1, op[2], dt[e1:e2])
            if q is not None and q != e1:  # the same tokens, so the spoken text doesn't change
                q2 = q + e2 - e1
                del merged[-2:]
                if q > a1:
                    merged.append(("delete", a1, q, j1, j1))
                elif merged and merged[-1][0] == "equal":
                    prev = merged.pop()
                    q, j1 = prev[1], prev[3]
                merged.append(("equal", q, q2, j1, j2))
                op = ("delete", q2, op[2], j2, j2)
                if q2 == op[2]:
                    continue
        if op[0] == "equal" and merged and merged[-1][0] == "equal":
            prev = merged.pop()
            op = ("equal", prev[1], op[2], prev[3], op[4])
        if (op[0] != "equal" and len(merged) >= 2 and merged[-1][0] == "equal" and merged[-2][0] != "equal"
                and not (op[0] == "delete" and merged[-2][0] == "delete")):
            _, i1, i2, _, _ = merged[-1]
            text = display[do[i1]:do[i2]]
            if i2 - i1 <= 2 and len(text) <= 3 and not text.isspace() and " " not in text:
                merged.pop()
                prev = merged.pop()
                merged.append(("replace", prev[1], op[2], prev[3], op[4]))
                continue
        if merged and op[0] != "equal" and merged[-1][0] != "equal":
            prev = merged.pop()
            merged.append(("replace", prev[1], op[2], prev[3], op[4]))
            continue
        merged.append(op)
    edits = []
    for tag, i1, i2, j1, j2 in merged:
        if tag == "equal":
            continue
        said = spoken[so[j1]:so[j2]]
        edits.append(Edit(do[i1], do[i2], said, "say" if said.strip() else "skip"))
    return edits

"""The pronunciation list: words the person wants said another way ("fMRI" said "F M R I"), or not said at all.

File: pronunciations.json in the data folder. It is written only when the person adds, changes, removes or
imports words, and holds only the words they typed or picked, never document text.

    {"version": 1, "entries": [
      {"match": "fMRI", "say": "F M R I"},
      {"match": "GIF", "say": "jif", "match_case": false},
      {"match": "et al.", "say": "and colleagues"},
      {"match": "ibid.", "say": ""}]}

An entry: match (1-100 characters of literal text, whitespace collapsed; never a pattern, so no entry can hang
reading), say (0-200 characters; "" means don't say it: the word stays on screen, dimmed like a citation),
match_case (None: smart case, so case-sensitive only when the match has a capital), whole_word (no letter or
digit right before or after) and enabled. At most 2,000 entries.

Matching runs on each sentence's spoken text, after cleaning and sentence splitting, so hyphenation and line
breaks are already fixed and an entry can never move a sentence boundary or reach into a skipped citation or a
"link" / "reference 3" replacement. The longest match wins and matches never overlap. Single words are a dict
lookup per word of the text, so a long list costs almost nothing. Logs hold counts only, never words.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Signal

from .settings import _atomic_write, _dump, _retry

log = logging.getLogger("saylight.pronunciations")

VERSION = 1
MAX_ENTRIES = 2000
MAX_MATCH = 100
MAX_SAY = 200
FILE_FILTER = "Pronunciation lists (*.json)"
EXPORT_NAME = "Saylight pronunciations.json"

_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Entry:
    match: str
    say: str
    match_case: bool | None = None  # None: smart case
    whole_word: bool = True
    enabled: bool = True

    @property
    def case_sensitive(self) -> bool:
        return self.match_case if self.match_case is not None else any(c.isupper() for c in self.match)

    @property
    def key(self) -> str:
        """Two entries with the same key match the same text; a new one replaces the old."""
        return self.match if self.case_sensitive else self.match.casefold()


def clean(e: Entry) -> Entry | None:
    """`e` with its whitespace collapsed, or None if it can't be an entry (empty, too long, no letter or digit)."""
    match, say = " ".join(str(e.match).split()), " ".join(str(e.say).split())
    if not (1 <= len(match) <= MAX_MATCH) or len(say) > MAX_SAY or not re.search(r"\w", match):
        return None
    mc = e.match_case if isinstance(e.match_case, bool) else None
    return Entry(match, say, mc, bool(e.whole_word), bool(e.enabled))


def parse(data: bytes) -> list[Entry]:
    """The entries in a pronunciation file's bytes; ValueError if it isn't a Saylight pronunciation list.
    Entries that aren't valid are dropped one by one."""
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("not a pronunciation list") from e
    if not isinstance(raw, dict) or raw.get("version") != VERSION or not isinstance(raw.get("entries"), list):
        raise ValueError("not a pronunciation list")
    out = []
    for item in raw["entries"]:
        if not isinstance(item, dict) or not isinstance(item.get("match"), str):
            continue
        say = item.get("say", "")
        if not isinstance(say, str):
            continue
        e = clean(Entry(item["match"], say, item.get("match_case"), item.get("whole_word", True) is not False,
                        item.get("enabled", True) is not False))
        if e is not None:
            out.append(e)
        if len(out) == MAX_ENTRIES:
            break
    return out


def read_list(path: str) -> list[Entry]:
    """The entries in the file at `path`. OSError if it can't be read, ValueError if it isn't a list."""
    with open(path, "rb") as f:
        return parse(f.read())


def _to_json(entries: Iterable[Entry]) -> dict:
    items = []
    for e in entries:
        item = {"match": e.match, "say": e.say}
        if e.match_case is not None:
            item["match_case"] = e.match_case
        if not e.whole_word:
            item["whole_word"] = False
        if not e.enabled:
            item["enabled"] = False
        items.append(item)
    return {"version": VERSION, "entries": items}


def _load_entries(path: str) -> list[Entry]:
    """Every entry in the list file (none if there is no file). A file that won't parse is moved aside to .bad,
    as settings are, and the list starts empty."""
    try:
        return read_list(path)
    except FileNotFoundError:
        return []
    except ValueError:
        bad = path + ".bad"
        try:
            _retry(lambda: os.replace(path, bad))
            log.warning("Pronunciation file was unreadable; moved it to %s and starting empty",
                        os.path.basename(bad))
        except OSError as e:
            log.warning("Pronunciation file was unreadable and couldn't be moved aside (%s)", type(e).__name__)
        return []
    except OSError as e:
        log.warning("Couldn't read the pronunciation file (%s)", type(e).__name__)
        return []


class PronunciationList:
    """The enabled entries, indexed for matching. Immutable once made; the store makes a new one on each change."""

    def __init__(self, entries: Iterable[Entry]):
        self.entries = [e for e in entries if e.enabled]
        # Whole-word single words ("fMRI", "GIF", "R²"): one dict lookup per word of the text.
        self._word_cs: dict[str, Entry] = {}
        self._word_ci: dict[str, Entry] = {}
        # Whole-word entries with spaces or punctuation inside ("et al.", "p < .05", "C++"), indexed by their
        # first word and each tried with its own anchored pattern only where that word occurs.
        self._first_cs: dict[str, list] = {}
        self._first_ci: dict[str, list] = {}
        # The rest (whole word off, or starting with punctuation): one alternation, slower and rare.
        self._rest: dict[str, Entry] = {}
        alts = []
        for e in sorted(self.entries, key=lambda e: -len(e.match)):
            first = _WORD.match(e.match)
            if e.whole_word and _WORD.fullmatch(e.match):
                (self._word_cs if e.case_sensitive else self._word_ci).setdefault(
                    e.match if e.case_sensitive else e.match.casefold(), e)
                continue
            body = re.escape(e.match).replace(r"\ ", r"\s+")
            if e.whole_word and e.match[-1].isalnum():
                body += r"(?!\w)"
            if not e.case_sensitive:
                body = f"(?i:{body})"
            if e.whole_word and first:
                table = self._first_cs if e.case_sensitive else self._first_ci
                key = first.group() if e.case_sensitive else first.group().casefold()
                table.setdefault(key, []).append((re.compile(body), e))
            else:
                if e.whole_word and e.match[0].isalnum():
                    body = r"(?<!\w)" + body
                name = f"g{len(alts)}"
                alts.append(f"(?P<{name}>{body})")
                self._rest[name] = e
        self._rest_re = re.compile("|".join(alts)) if alts else None

    def __len__(self) -> int:
        return len(self.entries)

    # --- Loading and saving --------------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> PronunciationList:
        """The list in `path`; a bad file is moved to .bad and the list is empty."""
        return cls(_load_entries(path))

    @staticmethod
    def save(path: str, entries: list[Entry]) -> None:
        """Write `entries` to `path` atomically (a .tmp, then a rename)."""
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        _atomic_write(path, _dump(_to_json(entries)))

    # --- Matching ------------------------------------------------------------------------------

    def find(self, s: str) -> list[tuple[int, int, str]]:
        """Non-overlapping (start, end, say) in `s`: leftmost first, then longest."""
        found = []
        if self._word_cs or self._word_ci or self._first_cs or self._first_ci:
            for m in _WORD.finditer(s):
                w = m.group()
                f = w.casefold()
                e = self._word_cs.get(w) or self._word_ci.get(f)
                if e is not None:
                    found.append((m.start(), m.end(), e.say))
                for table, key in ((self._first_cs, w), (self._first_ci, f)):
                    for rx, e in table.get(key, ()):
                        mm = rx.match(s, m.start())
                        if mm:
                            found.append((m.start(), mm.end(), e.say))
        if self._rest_re is not None:
            for m in self._rest_re.finditer(s):
                found.append((m.start(), m.end(), self._rest[m.lastgroup].say))
        if not found:
            return found
        found.sort(key=lambda t: (t[0], -(t[1] - t[0])))
        out, end = [], -1
        for a, b, say in found:
            if a >= end:
                out.append((a, b, say))
                end = b
        return out

    def apply(self, al):
        """`al` (an alignment.Aligned) with "pron" edits for the matches in its spoken text that lie in unedited
        text; a word that isn't said becomes a "skip", taking one neighbouring space with it."""
        from .alignment import Edit
        extra = []
        for a, b, say in self.find(al.spoken):
            if not say:  # not said at all: take one neighbouring space too ("noted ibid. the")
                if al.spoken[b:b + 1] == " " and al.is_exact_range(a, b + 1):
                    b += 1
                elif a and al.spoken[a - 1] == " " and al.is_exact_range(a - 1, b):
                    a -= 1
            if al.is_exact_range(a, b):  # never inside a "say" edit ("reference 3", "link")
                da, db = al.range(a, b)
                extra.append(Edit(da, db, say, "pron" if say else "skip"))
        return al.with_edits(extra) if extra else al


# --- How it will be read ---------------------------------------------------------------------------

def _matches(spoken: str, entry: Entry) -> list[tuple[int, int, str]]:
    return PronunciationList([replace(entry, enabled=True)]).find(spoken)


def preview_parts(spoken: str, entry: Entry) -> list[tuple[str, str]]:
    """`spoken` cut into ("text" | "say" | "skip", text) pieces with only `entry` applied: "say" pieces hold the
    replacement, and a word that isn't said stays as a "skip" piece, shown dimmed. No "say" or "skip" piece means
    the entry doesn't match this sentence."""
    out, pos = [], 0
    for a, b, say in _matches(spoken, entry):
        if a > pos:
            out.append(("text", spoken[pos:a]))
        out.append(("say", say) if say else ("skip", spoken[a:b]))
        pos = b
    if pos < len(spoken):
        out.append(("text", spoken[pos:]))
    return out


def preview_spoken(spoken: str, entry: Entry) -> tuple[str, list[tuple[int, int]]]:
    """What the voice gets for this sentence with only `entry` applied, and where each replacement landed in it.
    A word that isn't said goes with one neighbouring space, so no double space is left; its span is empty."""
    out, spans, pos, n = [], [], 0, 0
    for a, b, say in _matches(spoken, entry):
        if not say:
            if spoken[b:b + 1] == " ":
                b += 1
            elif a - 1 >= pos and spoken[a - 1] == " ":
                a -= 1
        piece = spoken[pos:a]
        out.append(piece)
        n += len(piece)
        spans.append((n, n + len(say)))
        out.append(say)
        n += len(say)
        pos = b
    out.append(spoken[pos:])
    return "".join(out), spans


def base_spoken(sentence, plist: PronunciationList | None = None) -> str:
    """A sentence's spoken text without any pronunciation applied: what a new or changed entry is matched
    against in the preview. `plist` is the list the sentence was built with, so the words it left unsaid can be
    told from skipped citations."""
    al = getattr(sentence, "align", None)
    edits, display = getattr(al, "edits", None), getattr(al, "display", None)
    if al is None or edits is None or display is None:
        return getattr(sentence, "say", "") or sentence.text
    silent = PronunciationList([e for e in plist.entries if not e.say]) if plist is not None else None

    def from_list(e) -> bool:
        if e.kind == "pron":
            return True
        if e.kind != "skip" or silent is None or not len(silent):
            return False
        core = display[e.start:e.end].strip(" ")
        hits = silent.find(core)
        return len(hits) == 1 and hits[0][0] == 0 and hits[0][1] == len(core)

    kept = [e for e in edits if not from_list(e)]
    return al.spoken if len(kept) == len(edits) else type(al)(display, kept).spoken


# --- The app-wide list -----------------------------------------------------------------------------

class PronunciationStore(QObject):
    """The person's list: read once, written only when they add, change, remove or import words. GUI thread."""

    changed = Signal()

    def __init__(self, path: str, parent: QObject | None = None):
        super().__init__(parent)
        self._path = path
        self._entries = _load_entries(path)
        self._list: PronunciationList | None = None

    @property
    def path(self) -> str:
        return self._path

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def current(self) -> PronunciationList:
        """The enabled entries, ready for matching (the same object until the list changes)."""
        if self._list is None:
            self._list = PronunciationList(self._entries)
        return self._list

    def is_full(self) -> bool:
        return len(self._entries) >= MAX_ENTRIES

    def index_of(self, entry: Entry) -> int | None:
        """The index of the entry that `entry` would replace (the same key), or None."""
        key = entry.key
        return next((i for i, e in enumerate(self._entries) if e.key == key), None)

    def find_word(self, word: str) -> int | None:
        """The index of the entry that says `word` (a word from the reading view), or None."""
        for i, e in enumerate(self._entries):
            if _matches(word, e) == [(0, len(word), e.say)]:
                return i
        return None

    def add(self, e: Entry) -> int:
        """Add `e`, replacing an entry with the same key. Returns its index; ValueError if it isn't valid or the
        list is full."""
        e = self._valid(e)
        i = self.index_of(e)
        if i is not None:
            self._entries[i] = e
            self._commit("changed")
            return i
        if self.is_full():
            raise ValueError("the pronunciation list is full")
        self._entries.append(e)
        self._commit("added")
        return len(self._entries) - 1

    def update(self, i: int, e: Entry) -> None:
        """Replace entry `i`; another entry with the new key goes, so no two match the same text."""
        e = self._valid(e)
        other = self.index_of(e)
        self._entries[i] = e
        if other is not None and other != i:
            del self._entries[other]
        self._commit("changed")

    def remove(self, i: int) -> Entry:
        e = self._entries.pop(i)
        self._commit("removed")
        return e

    def insert(self, i: int, e: Entry) -> None:
        """Put `e` back at `i` (Undo after a delete)."""
        e = self._valid(e)
        if self.index_of(e) is not None:
            return
        self._entries.insert(max(0, min(i, len(self._entries))), e)
        self._commit("restored")

    def import_entries(self, entries: Iterable[Entry]) -> tuple[list[Entry], int]:
        """Add the entries the list doesn't have yet; existing ones are never overwritten. Returns the added
        entries and how many were left out because the list is full."""
        keys = {e.key for e in self._entries}
        added, full = [], 0
        for e in entries:
            e = clean(e)
            if e is None or e.key in keys:
                continue
            if len(self._entries) >= MAX_ENTRIES:
                full += 1
                continue
            keys.add(e.key)
            self._entries.append(e)
            added.append(e)
        if added:
            self._commit(f"imported {len(added)}")
        return added, full

    def remove_entries(self, entries: Iterable[Entry]) -> None:
        """Take out exactly these entries (Undo after an import)."""
        drop = set(entries)
        kept = [e for e in self._entries if e not in drop]
        if len(kept) != len(self._entries):
            self._entries = kept
            self._commit("import undone")

    def import_file(self, path: str) -> int:
        """Add the words in a pronunciation file; returns how many were added. ValueError if it isn't a list."""
        return len(self.import_entries(read_list(path))[0])

    def export_file(self, path: str) -> None:
        """Save the whole list to `path`, a file the person chose."""
        PronunciationList.save(path, self._entries)
        log.info("Pronunciations: exported %d", len(self._entries))

    # --- Internals -----------------------------------------------------------------------------

    @staticmethod
    def _valid(e: Entry) -> Entry:
        out = clean(e)
        if out is None:
            raise ValueError("not a valid pronunciation")
        return out

    def _commit(self, what: str) -> None:
        self._list = None
        try:
            PronunciationList.save(self._path, self._entries)
        except OSError as e:
            log.warning("Couldn't save the pronunciation list (%s)", type(e).__name__)
        log.info("Pronunciations: %s (now %d)", what, len(self._entries))
        self.changed.emit()

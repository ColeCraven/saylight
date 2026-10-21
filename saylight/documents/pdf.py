"""Read a PDF's text with pypdf (BSD-3, pure Python), in reading order and without the clutter.

pypdf's plain text mode follows the content stream, which for papers made with LaTeX or Word is column by
column; its layout mode puts two columns side by side and glues the words of justified lines, so it is never
used. On top of it, each piece of text's font size, rotation and position decide where it goes:

* the font size with the most characters is the body text;
* smaller print (below 0.85 x the body size) is a footnote or a label: it goes to the asides, except short
  inline letters (a subscript such as BERT-base) and footnote marks ("left.4"), which are dropped;
* text inside an embedded figure is figure labelling and is dropped, unless figures hold most of the page (some
  producers wrap whole pages in one);
* rotated text (an arXiv side stamp, rotated table headers) is dropped;
* a change of font-size class or a vertical gap of more than 1.5 x the font size starts a new block, so the
  title, the authors and "Abstract" don't run together;
* caption blocks ("Figure 2: ...", "Table 1: ...") go to the asides: they sit wherever the float landed, often
  in the middle of a sentence.

pypdf is imported only when a PDF is opened (importing it takes about 150 ms). Pillow is never needed: text
extraction gives the same result without it. Nothing here logs text, titles or paths.
"""

from __future__ import annotations

import collections
import logging
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .model import Aside, LoadedDocument, LoadError

log = logging.getLogger("saylight.documents")

SMALL_RATIO = 0.85          # below this x body size: small print (footnotes, labels)
LARGE_RATIO = 1.15          # above: a heading or the title
GAP_RATIO = 1.5             # a line more than this x its font size below the previous one starts a new block
XOBJECT_PAGE_SHARE = 0.5    # more than this share of a page inside figures: the figure is the page
MIN_PAGE_CHARS = 25         # a page with fewer readable characters has no text to read (a scan)
UNREADABLE_SHARE = 0.3      # a page where more than this share of characters has no Unicode mapping
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_PAGES = 5000
MAX_CHARS = 4_000_000       # about 70 hours of listening

_CAPTION_RE = re.compile(r"(?:Figure|Fig\.|Table|Tab\.|Algorithm|Listing)\s+[A-Z]?\d+(?:\.\d+)?\s*[:.|]\s")
_FOOTNOTE_SPLIT_RE = re.compile(r"\n(?=\d{1,2}\s?[A-Za-z(])")
_MARK_RE = re.compile(r"[\d,\-–*†‡]+")
_TABLE_LINE_RE = re.compile(r"(?:[-+]?\d[\d.,%]*\s+){2,}[-+]?\d[\d.,%]*")
_UNMAPPED_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f�-]")
_SPACES_RE = re.compile(r"\n{3,}")


class Cancelled(Exception):
    """Opening was cancelled (Stop while opening)."""


@dataclass
class _Run:
    text: str
    size: float
    rotated: bool
    in_figure: bool
    y: float


def _mult(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return [a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F]


def _page_runs(page) -> list[_Run]:
    runs: list[_Run] = []
    depth = [0]

    def before(op, operands, cm, tm):
        if op == b"Do":
            depth[0] += 1

    def after(op, operands, cm, tm):
        if op == b"Do":
            depth[0] -= 1

    def visit(text, cm, tm, font_dict, font_size):
        if not text:
            return
        m = _mult(tm, cm)
        scale = math.hypot(m[2], m[3]) or math.hypot(m[0], m[1])
        rotated = abs(m[1]) > 1e-3 or abs(m[2]) > 1e-3
        runs.append(_Run(text, round(font_size * scale, 1), rotated, depth[0] > 0, m[5]))

    # pypdf reports a figure's text twice (piece by piece, then as a whole, still inside the Do): both
    # copies are flagged as inside the figure, so both go together.
    page.extract_text(visitor_text=visit, visitor_operand_before=before, visitor_operand_after=after)
    return runs


def _open(path: str, password: str | None):
    """A decrypted PdfReader, or LoadError("password" | "protected" | "corrupt" | "io" | "too_large")."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise LoadError("io", type(e).__name__) from None
    if size > MAX_FILE_BYTES:
        raise LoadError("too_large")
    import pypdf
    from pypdf.errors import DependencyError, FileNotDecryptedError, PdfReadError
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # its warnings can quote a file's content
    try:
        reader = pypdf.PdfReader(path)
        if reader.is_encrypted:
            if _uses_aes(reader) and not aes_available():  # don't ask for a password that can't help
                raise LoadError("protected", "aes")
            # Restrictions only (no user password) open with the empty password.
            if not reader.decrypt(password or ""):
                raise LoadError("password", "retry" if password else "")
        return reader
    except LoadError:
        raise
    except (DependencyError, NotImplementedError) as e:
        # AES without the cryptography package, or a security handler pypdf can't use.
        raise LoadError("protected", type(e).__name__) from None
    except FileNotDecryptedError:
        raise LoadError("password") from None
    except OSError as e:
        raise LoadError("io", type(e).__name__) from None
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError) as e:
        raise LoadError("corrupt", type(e).__name__) from None


def aes_available() -> bool:
    """True when pypdf can decrypt AES (the cryptography package is installed or bundled)."""
    from pypdf._crypt_providers import crypt_provider
    return crypt_provider[0] != "local_crypt_fallback"


def _uses_aes(reader) -> bool:
    try:
        encrypt = reader.trailer["/Encrypt"].get_object()
        if int(encrypt.get("/V", 0)) >= 5:
            return True
        filters = encrypt.get("/CF")
        filters = filters.get_object() if filters is not None else {}
        return any(str(f.get_object().get("/CFM", "")) in ("/AESV2", "/AESV3") for f in filters.values())
    except Exception:
        return False


def _has_image(page) -> bool:
    try:
        xobjects = page["/Resources"].get_object().get("/XObject")
        if xobjects is None:
            return False
        return any(x.get_object().get("/Subtype") == "/Image" for x in xobjects.get_object().values())
    except Exception:
        return False


def _readable(text: str) -> int:
    """Characters of real text on a page: 0 when most of them have no Unicode mapping (a font without one)."""
    chars = sum(1 for c in text if not c.isspace())
    if not chars:
        return 0
    if len(_UNMAPPED_RE.findall(text)) > UNREADABLE_SHARE * chars:
        return 0
    return chars


def _aside_kind(text: str) -> str:
    if _CAPTION_RE.match(text):
        return "table" if text.startswith(("Table", "Tab.")) else "caption"
    if re.match(r"\d{1,2}\s?[A-Za-z(]", text):
        return "footnote"
    lines = [line for line in text.split("\n") if line.strip()]
    if lines and sum(1 for line in lines if _TABLE_LINE_RE.search(line)) * 2 >= len(lines):
        return "table"
    return "note"


def extract_pdf(path: str, *, password: str | None = None,
                progress: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] = lambda: False) -> LoadedDocument:
    """The PDF's body text, with footnotes, captions and small print as asides, and where each page starts.

    Raises LoadError: "password" (needs one, or the one given didn't work), "protected" (encrypted in a way
    Saylight can't open), "scanned" (no page has text Saylight can read, only pictures of pages or fonts without
    a text mapping), "empty", "too_large", "corrupt" or "io"; and Cancelled. `scanned_pages` lists the pages
    with no readable text but a picture or unmapped text, for OCR to read later.
    """
    t0 = time.perf_counter()
    reader = _open(path, password)
    from pypdf.errors import DependencyError
    try:
        pages = reader.pages
        total = len(pages)
    except Exception as e:  # a broken page tree
        raise LoadError("corrupt", type(e).__name__) from None
    if total > MAX_PAGES:
        raise LoadError("too_large")
    all_runs: list[list[_Run]] = []
    images: list[bool] = []
    failed_pages = 0
    for k in range(total):
        if cancelled():
            raise Cancelled()
        try:
            page = pages[k]
            all_runs.append(_page_runs(page))
            images.append(_has_image(page))
        except DependencyError:  # AES-encrypted, and the cryptography package isn't there
            raise LoadError("protected", "aes") from None
        except Exception:  # one bad page must not lose the rest of the file
            failed_pages += 1
            all_runs.append([])
            images.append(False)
        if progress is not None:
            progress(k + 1, total)

    sizes: collections.Counter[float] = collections.Counter()
    for runs in all_runs:
        for r in runs:
            if not r.in_figure and not r.rotated:
                sizes[r.size] += len(r.text.strip())
    body = max(sizes, key=lambda s: (sizes[s], s)) if sizes else 10.0

    def size_class(size):
        if size < body * SMALL_RATIO:
            return "small"
        if size > body * LARGE_RATIO:
            return "large"
        return "body"

    text_parts: list[str] = []
    page_starts: list[int] = []
    asides: list[Aside] = []
    scanned: list[int] = []
    pos = 0
    dropped: collections.Counter[str] = collections.Counter()
    readable_total = 0
    laid = [_lay_out(runs, size_class, dropped) for runs in all_runs]
    smalls = _without_furniture([small for _, small in laid])
    for number, (page_text, _) in enumerate(laid, 1):
        small = smalls[number - 1]
        readable = _readable(page_text) + _readable(small)
        unmapped = not readable and (page_text.strip() or small.strip())
        if readable < MIN_PAGE_CHARS and (images[number - 1] or unmapped):
            scanned.append(number)  # a picture of a page, or a font without a text mapping: OCR can read it
        if not readable:
            page_text = small = ""
        readable_total += readable
        page_asides = [s.strip() for s in _FOOTNOTE_SPLIT_RE.split(small) if s.strip()]
        kept = []
        for block in re.split(r"\n[ \t]*\n", page_text):
            if _CAPTION_RE.match(block.lstrip()):
                page_asides.append(block.strip())
                dropped["caption"] += len(block.strip())
            else:
                kept.append(block)
        page_text = _SPACES_RE.sub("\n\n", "\n\n".join(kept).strip("\n"))
        if text_parts:
            text_parts.append("\n\n")
            pos += 2
        page_starts.append(pos)
        text_parts.append(page_text)
        pos += len(page_text)
        asides.extend(Aside(a, _aside_kind(a), number) for a in page_asides)
    text = "".join(text_parts)
    if not readable_total:
        raise LoadError("scanned" if scanned else "empty")
    if len(text) + sum(len(a.text) for a in asides) > MAX_CHARS:
        raise LoadError("too_large")
    log.info("PDF read: %d pages, %d characters, %d asides, %d pages without text, %d pages failed, "
             "%d figure and %d rotated characters dropped, %.0f ms", total, len(text), len(asides), len(scanned),
             failed_pages, dropped["figure"], dropped["rotated"], (time.perf_counter() - t0) * 1000)
    from ..academic_text import references_start
    return LoadedDocument("pdf", _title(path), text, references_start(text), asides, total, scanned, "", [],
                          page_starts)


_NUMBER_KEY_RE = re.compile(r"\d+")
_PAGE_NUMBER_RE = re.compile(r"(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?|[ivxlc]{1,6}", re.I)


def _without_furniture(smalls: list[str]) -> list[str]:
    """Small print without running headers, footers and page numbers: a line that recurs on other pages
    (numbers aside), or a line that is only a page number."""
    def key(line):
        return _NUMBER_KEY_RE.sub("#", " ".join(line.split()).casefold())

    seen: collections.Counter[str] = collections.Counter()
    for small in smalls:
        seen.update({key(line) for line in small.split("\n") if line.strip()})
    repeated = 2 if len(smalls) >= 2 else 1 << 30
    out = []
    for small in smalls:
        lines = [line for line in small.split("\n")
                 if not line.strip() or (seen[key(line)] < repeated and not _PAGE_NUMBER_RE.fullmatch(line.strip()))]
        out.append(_SPACES_RE.sub("\n\n", "\n".join(lines)).strip("\n"))
    return out


def _lay_out(runs: list[_Run], size_class, dropped) -> tuple[str, str]:
    """(the page's flow text, its small print)."""
    page_chars = sum(len(r.text.strip()) for r in runs) or 1
    figure_chars = sum(len(r.text.strip()) for r in runs if r.in_figure)
    figure_is_page = figure_chars / page_chars > XOBJECT_PAGE_SHARE
    flow: list[str] = []
    small: list[str] = []
    prev = None
    last_y = None
    for r in runs:
        if r.rotated:
            dropped["rotated"] += len(r.text.strip())
            continue
        if r.in_figure and not figure_is_page:
            dropped["figure"] += len(r.text.strip())
            continue
        c = size_class(r.size)
        if not r.text.strip():  # whitespace and newline runs carry the line structure
            (small if prev == "small" else flow).append(r.text)
            continue
        if c == "small":
            inline = flow and not flow[-1].endswith("\n") and "\n" not in r.text and len(r.text) <= 12
            if inline and r.text.strip().isalpha() and flow[-1][-1:].isalnum():
                flow.append(r.text)  # a subscript or small capitals: BERT_BASE
                continue
            if inline and _MARK_RE.fullmatch(r.text.strip()):
                dropped["marks"] += len(r.text.strip())  # a footnote mark: "left.4"
                continue
            small.append(r.text)
            prev = c
            continue
        starts_line = not flow or flow[-1].endswith("\n")
        if starts_line and flow and not flow[-1].endswith("\n\n"):
            gap = last_y - r.y if last_y is not None else 0.0
            if (prev is not None and c != prev and prev != "small") or gap > GAP_RATIO * r.size:
                flow.append("\n")  # a blank line: the size class changed, or a vertical gap
        if starts_line:
            last_y = r.y
        flow.append(r.text)
        prev = c
    return "".join(flow), "".join(small)


def _title(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]

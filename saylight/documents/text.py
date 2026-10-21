"""Read plain text (.txt) and Markdown (.md, .markdown) files, whatever their encoding.

The encoding is found in this order: a byte-order mark (UTF-32 first, because its mark starts with UTF-16's),
strict UTF-8, UTF-16 without a mark (recognised by its zero bytes), the Windows code page, cp1252, and latin-1,
which never fails. Line endings become "\\n". Markdown markup is reduced to readable text, and its footnotes
become asides. Nothing here logs text, titles or paths.
"""

from __future__ import annotations

import codecs
import locale
import os
import re

from .model import Aside, LoadedDocument, LoadError

MAX_TEXT_BYTES = 32 * 1024 * 1024   # a text file larger than this is not a reading document
MARKDOWN_SUFFIXES = (".md", ".markdown")

_BOMS = [(codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF8, "utf-8"),
         (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")]


def decode_text(data: bytes) -> tuple[str, str]:
    """(text, the encoding it was read as). Never raises."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data[len(bom):].decode(enc, errors="replace"), enc + "-bom"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    sample = data[:4096]
    if len(sample) >= 4:
        even = sample[0::2].count(0) / max(1, len(sample[0::2]))
        odd = sample[1::2].count(0) / max(1, len(sample[1::2]))
        if odd > 0.3 and even < 0.05:
            return data.decode("utf-16-le", errors="replace"), "utf-16-le"
        if even > 0.3 and odd < 0.05:
            return data.decode("utf-16-be", errors="replace"), "utf-16-be"
    for enc in dict.fromkeys((locale.getencoding(), "cp1252")):
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1"), "latin-1"


# Line-anchored rules use [ \t], never \s, which would swallow the blank lines between paragraphs.
_MD_RULES = [(re.compile(p, f), r) for p, r, f in [
    (r"\A---\n.*?\n---\n", "", re.S),                                     # YAML front matter
    (r"^(`{3,}|~{3,}).*?^\1[ \t]*$", "", re.S | re.M),                    # fenced code blocks
    (r"^ {0,3}\[[^\]^][^\]]*\]:[ \t]+\S+.*$", "", re.M),                  # link reference definitions
    (r"^ {0,3}\[\^[^\]]+\]:.*$", "", re.M),                               # footnote definitions -> asides
    (r"!\[([^\]]*)\]\([^)]*\)", "", 0),                                   # images (alt text is not prose)
    (r"\[([^\]]+)\]\([^)]*\)", r"\1", 0),                                  # [text](url)
    (r"\[([^\]^][^\]]*)\]\[[^\]]*\]", r"\1", 0),                           # [text][ref]
    (r"\[\^[^\]]+\]", "", 0),                                              # footnote marks
    (r"<[^>\n]+>", "", 0),                                                # inline HTML and autolinks
    (r"^ {0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", r"\1", re.M),            # ATX headings
    (r"^(.+)\n(?:=+|-+)[ \t]*$", r"\1", re.M),                            # setext headings
    (r"^ {0,3}>[ \t]?", "", re.M),                                        # block quotes
    (r"^ {0,3}(?:[-*_][ \t]*){3,}$", "", re.M),                            # horizontal rules
    (r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*\n?", "", re.M),  # table rules
    (r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", 0),                          # bold
    (r"(?<![\w*])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*])", r"\2", 0),          # italics
    (r"~~(.+?)~~", r"\1", 0),                                              # strikethrough
    (r"`([^`\n]+)`", r"\1", 0),                                            # inline code
]]
_FOOTNOTE_DEF_RE = re.compile(r"(?m)^ {0,3}\[\^[^\]]+\]:[ \t]*(.*)$")
_TABLE_ROW_RE = re.compile(r"(?m)^[ \t]*\|(.+)\|[ \t]*$")


def markdown_to_text(md: str) -> tuple[str, list[str]]:
    """(readable text, footnote texts). List markers are kept: academic_text reads them as list items."""
    t = md.replace("\r\n", "\n").replace("\r", "\n")
    notes = [n.strip() for n in _FOOTNOTE_DEF_RE.findall(t) if n.strip()]
    for rx, rep in _MD_RULES:
        t = rx.sub(rep, t)
    # Table rows: "| a | b |" -> "a; b." (academic_text would read the bars as pauses anyway).
    t = _TABLE_ROW_RE.sub(lambda m: "; ".join(c.strip() for c in m.group(1).split("|")) + ".", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip(), notes


def read_text_file(path: str) -> LoadedDocument:
    """A .txt or Markdown file. Raises LoadError: "too_large", "empty" or "io"."""
    try:
        if os.path.getsize(path) > MAX_TEXT_BYTES:
            raise LoadError("too_large")
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise LoadError("io", type(e).__name__) from None
    text, encoding = decode_text(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    title = os.path.splitext(os.path.basename(path))[0]
    kind, notes = "txt", []
    if path.lower().endswith(MARKDOWN_SUFFIXES):
        kind = "md"
        text, notes = markdown_to_text(text)
    if not re.search(r"[^\W_]", text):
        raise LoadError("empty")
    from ..academic_text import references_start
    return LoadedDocument(kind, title, text, references_start(text), [Aside(n, "footnote", None) for n in notes],
                          encoding=encoding)

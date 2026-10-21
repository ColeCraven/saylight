"""What a file reader returns, where a reading came from, and why a file couldn't be opened.

A LoadedDocument lives in memory only: it is never saved, cached or logged. Its title is for the screen; logs
give counts and kinds, never titles, paths or text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ocr.model import SCAN_NOTICES, OcrText, ScreenText

DOCUMENT_KINDS = ("pdf", "docx", "txt", "md")
ASIDE_KINDS = ("footnote", "caption", "table", "note")
SOURCE_KINDS = ("selection", "file", "screen")
# "password": the PDF needs a password (or the one given didn't work); "protected": it is encrypted in a way
# Saylight can't open at all. The scan kinds are a scanned PDF whose pages couldn't be read with OCR; the
# engine shows them as OCR notices over the reading underneath.
LOAD_ERROR_KINDS = ("password", "protected", "scanned", "unsupported", "too_large", "corrupt", "empty",
                    "io") + SCAN_NOTICES


@dataclass(frozen=True)
class Aside:
    """A footnote, caption, table or note kept out of the body. It is shown dimmed in a Notes section after the
    main text, labelled by page, and read only when the person asks for it."""
    text: str
    kind: str          # one of ASIDE_KINDS
    page: int | None   # 1-based; None for files without pages


@dataclass
class LoadedDocument:
    """A file's text as its reader found it, before the reading text is built from it."""
    kind: str                          # one of DOCUMENT_KINDS
    title: str                         # the file name without its extension: for the screen only, never logged
    text: str                          # the body; paragraphs are separated by blank lines
    references_at: int | None = None   # offset in `text` where the reference list starts
    asides: list[Aside] = field(default_factory=list)
    pages: int = 0                     # 0 for files without pages
    scanned_pages: list[int] = field(default_factory=list)          # 1-based pages with no text to read
    encoding: str = ""                 # what a text file was decoded as; "" for PDF and Word
    citation_spans: list[tuple[int, int]] = field(default_factory=list)  # Word's own citation fields
    page_starts: list[int] = field(default_factory=list)            # offset in `text` where each page starts
    scan_dpi: int = 0                  # the lowest effective dpi of the pages read with OCR; 0 for no OCR


@dataclass(frozen=True)
class DocSource:
    """Where the reading came from: highlighted text, a file, or an area of the screen."""
    kind: str                          # one of SOURCE_KINDS
    path: str = ""                     # files only; never logged
    title: str = ""                    # files only; never logged
    screen: ScreenText | None = None   # "screen" only: where its words are on the screen


RawInput = str | LoadedDocument | OcrText  # what a reading is built from, kept so it can be rebuilt


class LoadError(Exception):
    """A file that couldn't be opened, as one of LOAD_ERROR_KINDS. `detail` is never text from the file: at
    most the extension of an unsupported file (".doc") or an exception's class name."""

    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in LOAD_ERROR_KINDS:
            raise ValueError(f"unknown load error kind: {kind}")
        super().__init__(kind)
        self.kind = kind
        self.detail = detail

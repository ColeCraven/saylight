"""Opening files: PDF, Word, plain text and Markdown.

The readers turn a file into a LoadedDocument on a worker thread; the text model builds the reading from it.
Nothing here saves, caches or logs a file's text, title or path.
"""

from .model import (ASIDE_KINDS, DOCUMENT_KINDS, LOAD_ERROR_KINDS, SOURCE_KINDS, Aside, DocSource, LoadedDocument,
                    LoadError, RawInput)

__all__ = ["ASIDE_KINDS", "DOCUMENT_KINDS", "LOAD_ERROR_KINDS", "SOURCE_KINDS", "Aside", "DocSource",
           "LoadedDocument", "LoadError", "RawInput"]

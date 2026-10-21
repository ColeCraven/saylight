"""The only module that imports winrt (pywinrt, MIT).

winrt must load after PySide6: importing it first kills the process with an access violation (winrt ships an
older msvcp140.dll than PySide6, and whichever loads first wins). So it is imported lazily, and load() refuses
to run before PySide6.QtCore has loaded. Blocking WinRT calls (.get()) belong on worker threads only: on Qt's GUI
thread they raise, and the operation keeps running anyway.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
import threading
from types import ModuleType

log = logging.getLogger("saylight.winrt")

# What reading the screen and scanned PDFs use. Foundation and its collections come in with them.
OCR_NAMESPACES = ("media.ocr", "graphics.imaging", "storage.streams", "data.pdf", "globalization")

_NAMESPACE_RE = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)*")
_lock = threading.Lock()
_available: bool | None = None


def module_name(namespace: str) -> str:
    """"media.ocr" -> "winrt.windows.media.ocr"; "system" (the runtime's own types) -> "winrt.system"."""
    ns = namespace.strip().lower()
    if not _NAMESPACE_RE.fullmatch(ns) or ns.split(".")[0] in ("winrt", "windows"):
        raise ValueError(f"not a WinRT namespace: {namespace!r}")
    return "winrt.system" if ns == "system" else f"winrt.windows.{ns}"


def load(namespace: str) -> ModuleType:
    """The winrt module for a short namespace: "media.ocr" -> winrt.windows.media.ocr.

    Raises RuntimeError if PySide6.QtCore isn't loaded yet, and ImportError if the module is missing."""
    name = module_name(namespace)
    if "PySide6.QtCore" not in sys.modules:
        raise RuntimeError("winrt can only be loaded after PySide6.QtCore")
    return importlib.import_module(name)


def available() -> bool:
    """True if the winrt packages Saylight uses can be imported here. The first call imports them (after
    PySide6.QtCore, which it loads first if nothing has yet); the answer is kept for the rest of the run."""
    global _available
    with _lock:
        if _available is None:
            import PySide6.QtCore  # noqa: F401  (always before winrt)
            try:
                for namespace in OCR_NAMESPACES:
                    load(namespace)
                _available = True
            except (ImportError, OSError) as e:
                log.info("Windows OCR unavailable (%s)", type(e).__name__)
                _available = False
        return _available

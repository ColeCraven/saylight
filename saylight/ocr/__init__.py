"""Reading text from pictures with Windows OCR: the screen, and scanned PDF pages.

The model types import cleanly anywhere. The OCR itself (engine.py, through winrt_support.py) runs on one
worker thread. Captured images stay in memory and are dropped once OCR is done; nothing here saves or logs
pixels or text.
"""

from .model import (OCR_FAILURES, OCR_NOTICE_KINDS, SCAN_NOTICES, SCREEN_NOTICES, OcrLine, OcrResult, OcrText,
                    OcrWord, ScreenText)

__all__ = ["OCR_FAILURES", "OCR_NOTICE_KINDS", "SCAN_NOTICES", "SCREEN_NOTICES", "OcrLine", "OcrResult", "OcrText",
           "OcrWord", "ScreenText"]

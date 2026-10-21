"""Tests for winrt_support.py: the only door to winrt, which never opens before PySide6.QtCore has loaded
(winrt first kills the process). The order checks run in fresh interpreters."""

import os
import subprocess
import sys
import unittest

from saylight import winrt_support as W

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class NamespaceTests(unittest.TestCase):
    def test_short_names(self) -> None:
        self.assertEqual(W.module_name("media.ocr"), "winrt.windows.media.ocr")
        self.assertEqual(W.module_name("Data.Pdf"), "winrt.windows.data.pdf")
        self.assertEqual(W.module_name("system"), "winrt.system")

    def test_anything_else_is_refused(self) -> None:
        for bad in ("", "winrt.windows.media.ocr ", ".media", "media..ocr", "media/ocr", "os; import x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                W.module_name(bad)


class ImportOrderTests(unittest.TestCase):
    def test_load_refuses_before_qt_and_imports_nothing(self) -> None:
        done = run("import sys\n"
                   "from saylight import winrt_support as w\n"
                   "try:\n"
                   "    w.load('media.ocr')\n"
                   "except RuntimeError:\n"
                   "    print('refused')\n"
                   "print('PySide6.QtCore' in sys.modules, any(m.startswith('winrt') for m in sys.modules))\n")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.split(), ["refused", "False", "False"])

    def test_available_loads_qt_first(self) -> None:
        # Called with nothing loaded, it brings in QtCore itself before any winrt module, so it can't crash.
        done = run("import sys\n"
                   "from saylight import winrt_support as w\n"
                   "ok = w.available()\n"
                   "order = [m for m in sys.modules if m == 'PySide6.QtCore' or m.startswith('winrt')]\n"
                   "print(ok, order[0] if order else '-')\n")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        ok, first = done.stdout.split()
        self.assertIn(ok, ("True", "False"))
        if ok == "True":
            self.assertEqual(first, "PySide6.QtCore")

    def test_after_qt_it_loads(self) -> None:
        import PySide6.QtCore  # noqa: F401
        if not W.available():
            self.skipTest("winrt is not installed here")
        self.assertEqual(W.load("media.ocr").__name__, "winrt.windows.media.ocr")
        with self.assertRaises(ImportError):
            W.load("not.a.namespace")

    def test_the_answer_is_kept(self) -> None:
        import PySide6.QtCore  # noqa: F401
        first = W.available()
        self.assertIs(W.available(), first)


if __name__ == "__main__":
    unittest.main()

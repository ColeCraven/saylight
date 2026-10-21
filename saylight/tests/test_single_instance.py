"""Tests for saylight/single_instance.py: the path a second copy hands over, checked before anything opens it.

The server listens on a name made for this test only (never the app's own), and the second copy is a second
Python process, as it is when Windows starts one for Open with.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from PySide6.QtCore import QCoreApplication

from saylight import single_instance as S
from saylight.tests.test_controls import setUpModule  # noqa: F401 (the QApplication)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CheckPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_si_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make(self, name: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        return path

    def test_kinds(self) -> None:
        self.assertEqual(S.check_path(self.make("a.PDF")), "")
        for ext in (".docx", ".txt", ".md", ".markdown"):
            self.assertEqual(S.check_path(self.make("a" + ext)), "", ext)
        self.assertEqual(S.check_path(self.make("a.doc")), "unsupported")
        self.assertEqual(S.check_path(self.dir), "unsupported")
        self.assertEqual(S.check_path(os.path.join(self.dir, "gone.pdf")), "missing")
        self.assertEqual(S.check_path("relative.pdf"), "missing")
        self.assertEqual(S.check_path(""), "missing")

    def test_the_name_is_per_user(self) -> None:
        with mock.patch.dict(os.environ, {"USERDOMAIN": "PC", "USERNAME": "alex"}):
            a = S.server_name()
        with mock.patch.dict(os.environ, {"USERDOMAIN": "PC", "USERNAME": "sam"}):
            b = S.server_name()
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith(S.SERVER_BASE + "."))
        self.assertEqual(S.SERVER_BASE, "Saylight.Open")  # make_test_copy.py swaps this literal for the test name


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_si_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.name = f"SaylightUnitTest.{uuid.uuid4().hex}"
        self.server = S.OpenServer(self.name)
        self.addCleanup(self.server.close)
        self.assertTrue(self.server.listen())
        self.got: list[tuple] = []
        self.server.openRequested.connect(lambda p: self.got.append(("open", p)))
        self.server.openRejected.connect(lambda k, p: self.got.append(("rejected", k, p)))

    def send(self, path: str) -> bool:
        """send_open from a second process, as Open with starts one, while this one serves."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from saylight.single_instance import send_open; "
                "sys.exit(0 if send_open(sys.argv[2], name=sys.argv[3], timeout_ms=4000) else 1)")
        proc = subprocess.Popen([sys.executable, "-c", code, REPO, path, self.name])
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        for _ in range(20):
            QCoreApplication.processEvents()
        if proc.poll() is None:
            proc.kill()
        return proc.wait() == 0

    def test_a_file_is_handed_over(self) -> None:
        path = os.path.join(self.dir, "Smith 2024 – notes.pdf")
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4")
        self.assertTrue(self.send(path))
        self.assertEqual(self.got, [("open", os.path.normpath(path))])

    def test_anything_else_is_rejected_with_its_kind(self) -> None:
        self.assertTrue(self.send(os.path.join(self.dir, "gone.docx")))
        self.assertEqual(self.got[0][:2], ("rejected", "missing"))

    def test_with_nobody_listening_it_gives_up(self) -> None:
        start = time.monotonic()
        self.assertFalse(S.send_open("x.pdf", name=self.name + ".nobody", timeout_ms=300))
        self.assertLess(time.monotonic() - start, 3)


if __name__ == "__main__":
    unittest.main()

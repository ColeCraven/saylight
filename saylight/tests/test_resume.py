"""Tests for resume.py: what the resume file holds (paths and integers, never text), matching a reopened file,
the 20-file limit, forgetting one file on Finish, and turning the feature off deleting the file at once."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from saylight.resume import ResumePoint, ResumeStore, same_path

TEXT = "Dyslexia affects reading in many ways. The second sentence is about fMRI studies of reading."


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point(path: str, *, text: str = TEXT, offset: int = 40, sentence: int = 1, page: int = 3,
          saved_at: float = 1_760_000_000.5, size: int = 2048, mtime_ns: int = 1_700_000_000_000_000_000):
    return ResumePoint(path, size, mtime_ns, fingerprint(text), offset, sentence, saved_at, page)


class ResumeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_resume_test_")
        self.file = os.path.join(self.dir, "resume.json")
        self.doc = os.path.join(self.dir, "Smith 2024 reading study.pdf")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_file_holds_paths_and_integers_never_text(self) -> None:
        ResumeStore(self.file).remember(point(self.doc))
        with open(self.file, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
        self.assertEqual(set(data), {"version", "points"})
        (stored,) = data["points"]
        self.assertEqual(set(stored), {"path", "size", "mtime_ns", "fingerprint", "offset", "sentence", "page",
                                       "saved_at"})
        for name, value in stored.items():
            if name == "path":
                self.assertTrue(same_path(value, self.doc))
            elif name == "fingerprint":
                self.assertRegex(value, r"^[0-9a-f]{64}$")
            else:
                self.assertIs(type(value), int, name)
        for word in ("Dyslexia", "affects", "fMRI", "studies"):
            self.assertNotIn(word, raw)

    def test_a_reopened_store_reads_the_point_back(self) -> None:
        ResumeStore(self.file).remember(point(self.doc, page=14))
        got = ResumeStore(self.file).lookup(self.doc, 2048, 1_700_000_000_000_000_000, fingerprint(TEXT))
        self.assertIsNotNone(got)
        p, exact = got
        self.assertTrue(exact)
        self.assertEqual((p.offset, p.sentence, p.page), (40, 1, 14))
        self.assertEqual(p.saved_at, 1_760_000_000.0)  # whole seconds on disk

    def test_matching_a_reopened_file(self) -> None:
        store = ResumeStore(self.file)
        store.remember(point(self.doc))
        mtime = 1_700_000_000_000_000_000
        # The same shown text: exact, even if the file was copied or touched since.
        self.assertTrue(store.lookup(self.doc.upper(), 9, 1, fingerprint(TEXT))[1])
        # The same file on disk, read a little differently by this version: near, not exact.
        self.assertEqual(store.lookup(self.doc, 2048, mtime, fingerprint(TEXT + " "))[1], False)
        # A file that changed on disk has no point; nor has an unknown file.
        self.assertIsNone(store.lookup(self.doc, 4096, mtime + 1, fingerprint("other")))
        self.assertIsNone(store.lookup(os.path.join(self.dir, "other.pdf"), 2048, mtime, fingerprint(TEXT)))

    def test_one_point_per_file_and_the_oldest_go_first(self) -> None:
        store = ResumeStore(self.file, limit=20)
        for i in range(25):
            store.remember(point(os.path.join(self.dir, f"paper {i}.pdf"), sentence=i))
        store.remember(point(os.path.join(self.dir, "paper 24.pdf"), sentence=99))
        self.assertEqual(len(store), 20)
        names = [os.path.basename(p.path) for p in ResumeStore(self.file).recent()]
        self.assertEqual(names[0], "paper 24.pdf")
        self.assertEqual(names[-1], "paper 5.pdf")
        self.assertEqual(store.get(os.path.join(self.dir, "paper 24.pdf")).sentence, 99)
        self.assertIsNone(store.get(os.path.join(self.dir, "paper 4.pdf")))

    def test_finishing_a_file_forgets_only_that_file(self) -> None:
        store = ResumeStore(self.file)
        other = os.path.join(self.dir, "other.docx")
        store.remember(point(self.doc))
        store.remember(point(other))
        self.assertEqual(store.forget(self.doc), 1)
        self.assertEqual(store.forget(self.doc), 0)
        self.assertEqual([p.path for p in ResumeStore(self.file).recent()], [os.path.abspath(other)])
        store.forget(other)
        self.assertFalse(os.path.exists(self.file))  # nothing left to keep

    def test_turning_it_off_deletes_the_file_at_once(self) -> None:
        store = ResumeStore(self.file)
        for i in range(7):
            store.remember(point(os.path.join(self.dir, f"paper {i}.pdf")))
        self.assertEqual(ResumeStore(self.file).forget(None), 7)  # counts what was on disk, unread so far
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual(store.forget(None), 7)
        self.assertEqual(len(store), 0)  # no copy kept in memory
        self.assertEqual(ResumeStore(self.file).forget(None), 0)

    def test_an_unreadable_file_is_dropped_and_the_store_starts_empty(self) -> None:
        for content in (b"{not json", b'{"version": 1}', b"\xff\xfe\x00"):
            with self.subTest(content=content):
                with open(self.file, "wb") as f:
                    f.write(content)
                store = ResumeStore(self.file)
                with self.assertLogs("saylight.resume", "WARNING"):
                    self.assertEqual(store.recent(), [])
                self.assertFalse(os.path.exists(self.file))

    def test_bad_entries_are_skipped_one_by_one(self) -> None:
        good = {"path": self.doc, "size": 1, "mtime_ns": 2, "fingerprint": fingerprint(TEXT), "offset": 3,
                "sentence": 4, "page": 0, "saved_at": 5}
        data = {"version": 1, "points": [good, {"path": 3}, {"path": "x.pdf", "size": "big"}, "nope",
                                         dict(good, path=os.path.join(self.dir, "b.pdf"), fingerprint="text!")]}
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        points = ResumeStore(self.file).recent()
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].fingerprint, "")  # anything but a hex digest is never kept
        self.assertEqual(points[1].sentence, 4)

    def test_no_file_until_something_is_remembered(self) -> None:
        store = ResumeStore(self.file)
        self.assertIsNone(store.lookup(self.doc, 1, 1, fingerprint(TEXT)))
        self.assertEqual(store.forget(self.doc), 0)
        self.assertFalse(os.path.exists(self.file))


if __name__ == "__main__":
    unittest.main()

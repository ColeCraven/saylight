"""Pick up where you left off: the place you stopped in each file, for files only and only when it is on.

A point holds the file's path, size and modification time, a fingerprint of the shown text and integer
positions. Never the text itself. The store keeps up to `limit` files, newest last, and writes the whole file
atomically on every change. Turning the feature off deletes the file at once (forget(None)), with no copy kept
in memory. GUI thread only. Logs carry counts only, never a path.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from .settings import _atomic_write, _dump, _retry

log = logging.getLogger("saylight.resume")

VERSION = 1
LIMIT = 20
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ResumePoint:
    path: str
    size: int
    mtime_ns: int
    fingerprint: str      # SHA-256 of the shown text, as hex
    offset: int           # shown-text offset of the sentence's start
    sentence: int
    saved_at: float       # seconds since 1970
    page: int = 0         # 1-based page of that sentence; 0 when the file has no pages


def same_path(a: str, b: str) -> bool:
    """Whether two paths name the same file, as Windows compares them (case and separators ignored)."""
    return _key(a) == _key(b)


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)) if path else ""


def _int(value, lo: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    return max(lo, int(value))


def _to_json(p: ResumePoint) -> dict:
    """A point as stored: the path, the hex fingerprint and integers. Nothing else ever reaches the file."""
    return {"path": os.path.abspath(p.path), "size": int(p.size), "mtime_ns": int(p.mtime_ns),
            "fingerprint": p.fingerprint if _HEX64.fullmatch(p.fingerprint or "") else "",
            "offset": max(0, int(p.offset)), "sentence": max(0, int(p.sentence)),
            "page": max(0, int(p.page)), "saved_at": max(0, int(p.saved_at))}


def _from_json(item) -> ResumePoint | None:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
        return None
    fp = item.get("fingerprint", "")
    try:
        return ResumePoint(item["path"], _int(item.get("size")), _int(item.get("mtime_ns")),
                           fp if isinstance(fp, str) and _HEX64.fullmatch(fp) else "",
                           _int(item.get("offset", 0)), _int(item.get("sentence", 0)),
                           float(_int(item.get("saved_at", 0))), _int(item.get("page", 0)))
    except (ValueError, OverflowError):
        return None


class ResumeStore:
    """The resume file: up to `limit` points, the oldest dropped first."""

    def __init__(self, path: str, *, limit: int = LIMIT) -> None:
        self._path = path
        self._limit = max(1, int(limit))
        self._points: list[ResumePoint] | None = None  # oldest first; read on first use

    @property
    def path(self) -> str:
        return self._path

    def __len__(self) -> int:
        return len(self._load())

    def recent(self) -> list[ResumePoint]:
        """Every remembered point, newest first (the Ready card and the tray offer the first whose file exists)."""
        return list(reversed(self._load()))

    def get(self, path: str) -> ResumePoint | None:
        """The point remembered for `path`, whatever the file looks like now."""
        key = _key(path)
        return next((p for p in self._load() if _key(p.path) == key), None)

    def lookup(self, path: str, size: int, mtime_ns: int, fingerprint: str) -> tuple[ResumePoint, bool] | None:
        """The point saved for this file and whether it matches exactly, or None.

        Exact: the shown text has the same fingerprint, so the saved sentence is the same sentence. Not exact:
        the file is unchanged on disk (same size and time) but this version reads it slightly differently, so
        only the offset is a good guide ("Continuing near page 14"). A file that changed on disk has no point."""
        point = self.get(path)
        if point is None:
            return None
        if fingerprint and point.fingerprint == fingerprint:
            return point, True
        if point.size == int(size) and point.mtime_ns == int(mtime_ns):
            return point, False
        return None

    def remember(self, point: ResumePoint) -> None:
        """Save `point` as the newest, replacing the file's earlier point; the oldest go past the limit."""
        key = _key(point.path)
        points = [p for p in self._load() if _key(p.path) != key]
        points.append(_from_json(_to_json(point)) or point)
        self._points = points[-self._limit:]
        self._save()

    def forget(self, path: str | None = None) -> int:
        """Forget one file's point; with None, delete the resume file at once. Returns how many were forgotten."""
        if path is None:
            n = len(self._points) if self._points is not None else self._count_on_disk()
            self._points = []
            self._delete()
            log.info("Resume: forgot %d", n)
            return n
        key = _key(path)
        points = self._load()
        kept = [p for p in points if _key(p.path) != key]
        if len(kept) == len(points):
            return 0
        self._points = kept
        self._save()
        return 1

    # --- File --------------------------------------------------------------------------------

    def _load(self) -> list[ResumePoint]:
        if self._points is None:
            self._points = self._read()
        return self._points

    def _read(self) -> list[ResumePoint]:
        try:
            with open(self._path, "rb") as f:
                data = json.loads(f.read().decode("utf-8-sig"))
            if not isinstance(data, dict) or not isinstance(data.get("points"), list):
                raise ValueError("not a resume file")
        except FileNotFoundError:
            return []
        except (OSError, ValueError, UnicodeDecodeError) as e:
            # It holds nothing worth keeping: start again rather than keep a file we can't read.
            log.warning("Resume file was unreadable (%s); starting empty", type(e).__name__)
            self._delete()
            return []
        points = [p for p in map(_from_json, data["points"]) if p is not None]
        return points[-self._limit:]

    def _count_on_disk(self) -> int:
        try:
            return len(self._read())
        except Exception:  # noqa: BLE001 - only a count for the log and the detail line
            return 0

    def _save(self) -> None:
        points = self._points or []
        if not points:
            self._delete()
            return
        data = {"version": VERSION, "points": [_to_json(p) for p in points]}
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            _atomic_write(self._path, _dump(data))
        except OSError as e:
            log.warning("Couldn't save the resume file (%s)", type(e).__name__)

    def _delete(self) -> None:
        for path in (self._path, self._path + ".tmp"):
            try:
                _retry(lambda p=path: os.remove(p))
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("Couldn't delete the resume file (%s)", type(e).__name__)

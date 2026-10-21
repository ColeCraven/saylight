"""Licence gate and third-party notices for a Saylight build.

    python packaging/collect_notices.py <dist\\Saylight> [--check] [--json]

Run it with the same Python that built the folder. It lists everything inside the build (the archive inside
Saylight.exe and every file in _internal) and traces each piece to the installed distribution it came from,
using the distributions' RECORD files. The gate fails when:
- a piece can't be traced to Python, Saylight, PyInstaller or an installed distribution;
- a distribution declares no licence, or ships no licence text and none is kept in licenses/;
- a licence is outside the allow-list (MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, PSF-2.0, MPL-2.0, 0BSD,
  ISC). GPL and AGPL always fail. LGPL-3.0 passes only for PySide6*, shiboken6 and edge-tts, and only when
  none of their modules sits inside the archive, so they stay replaceable. A bare "BSD" passes only when the
  licence text shows it is the two- or three-clause form;
- Pillow, numpy or Tk are in the build at all (they must never ship, whatever their licence).

Without --check it then writes, next to Saylight.exe: LICENSE.txt (Saylight's), THIRD_PARTY_NOTICES.txt and a
licenses folder with every full licence text. --json prints a summary for the build script.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import importlib.metadata as md
import json
import os
import re
import shutil
import ssl
import sys
import zipfile
from ctypes import wintypes
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXED = os.path.join(REPO, "licenses")  # licence texts kept in the repository

ALLOWED = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "MPL-2.0", "0BSD", "ISC"}
# Declared as just "BSD" (often the "BSD License" classifier): could be the four-clause form with the
# advertising clause, so the licence text decides (bsd_variant).
BARE_BSD = "BSD"
APP_MODULES = ("saylight", "saylight_app", "saylight_selftest")
# Never shipped, whatever pulls them in: Pillow is a dev tool only (pypdf imports it optionally), and Saylight
# has no use for numpy or Tk. saylight.spec excludes them; the gate makes sure. Matched against the top-level
# module name and the first part of each path under _internal.
NEVER_BUNDLED = re.compile(r"(?i)^(pil|numpy|numpy\.libs|tkinter|_tkinter|_tcl_data|_tk_data|tcl[\d.]*|tk[\d.]*|"
                           r"_tkinter\.[\w.-]*pyd|(tcl|tk)\d+t?\.dll|(pillow|numpy)-[^/]*\.dist-info)$")
PYINSTALLER_MODULES = re.compile(r"^(pyimod\d\d_\w+|pyiboot\d\d_\w+|pyi_rth_\w+|_pyi_rth_utils|pyi_splash|struct)$")
TEXT_NAME = re.compile(r"(?i)(licen[cs]e|copying|notice|copyright|authors)")

# Texts kept in licenses/ for distributions whose own metadata isn't enough. PySide6 and shiboken6 only ship a
# pointer to Qt's commercial terms, so they get the LGPL-3.0 text, the GPL-3.0 text it builds on, and Qt's
# third-party list instead; edge-tts ships the LGPL text, which refers to the GPL text.
EXTRA_TEXTS = {
    "pyside6-essentials": ("LGPL-3.0.txt", "GPL-3.0.txt", "Qt-third-party.txt"),
    "shiboken6": ("LGPL-3.0.txt", "GPL-3.0.txt"),
    "edge-tts": ("GPL-3.0.txt",),
}
SKIP_DIST_TEXTS = {"pyside6-essentials", "shiboken6"}

_ALIASES = {
    "MIT": "MIT", "MIT LICENSE": "MIT", "EXPAT": "MIT", "EXPAT LICENSE": "MIT",
    "BSD-2-CLAUSE": "BSD-2-Clause", "BSD 2-CLAUSE": "BSD-2-Clause", "SIMPLIFIED BSD": "BSD-2-Clause",
    "BSD-3-CLAUSE": "BSD-3-Clause", "BSD 3-CLAUSE": "BSD-3-Clause", "NEW BSD": "BSD-3-Clause",
    "MODIFIED BSD": "BSD-3-Clause", "REVISED BSD": "BSD-3-Clause", "BSD": "BSD", "BSD LICENSE": "BSD",
    "APACHE-2.0": "Apache-2.0", "APACHE 2.0": "Apache-2.0", "APACHE 2": "Apache-2.0",
    "APACHE LICENSE 2.0": "Apache-2.0", "APACHE LICENSE, VERSION 2.0": "Apache-2.0",
    "APACHE LICENSE VERSION 2.0": "Apache-2.0", "APACHE SOFTWARE LICENSE": "Apache-2.0",
    "PSF-2.0": "PSF-2.0", "PSF": "PSF-2.0", "PSFL": "PSF-2.0", "PYTHON SOFTWARE FOUNDATION LICENSE": "PSF-2.0",
    "MPL-2.0": "MPL-2.0", "MPL 2.0": "MPL-2.0", "MOZILLA PUBLIC LICENSE 2.0 (MPL 2.0)": "MPL-2.0",
    "0BSD": "0BSD", "ISC": "ISC", "ISC LICENSE": "ISC", "ISC LICENSE (ISCL)": "ISC",
}


class GateError(Exception):
    """The build can't ship: the message lists every problem."""


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def classify(token: str) -> str:
    """One licence name or SPDX id -> a short id; GPL-family names keep their family so the gate can see them."""
    t = re.sub(r"\s+", " ", token.strip().replace("LICENCE", "LICENSE").replace("Licence", "License")).upper()
    t = t.removeprefix("LICENSE :: OSI APPROVED :: ").removeprefix("LICENSE :: ")
    if "AFFERO" in t or "AGPL" in t:
        return "AGPL"
    if "LESSER" in t or "LGPL" in t or "LIBRARY GENERAL" in t:
        return "LGPL-3.0" if re.search(r"(LGPL-?V?3|V3|VERSION 3|3\.0)", t) else "LGPL"
    if "GPL" in t or "GENERAL PUBLIC" in t:
        return "GPL"
    return _ALIASES.get(t, "unknown:" + token.strip())


def alternatives(expr: str) -> list[list[str]]:
    """An SPDX-style expression as alternatives: "(A OR B) AND C" -> [[A, C], [B, C]]."""
    tokens = re.findall(r"\(|\)|[^\s()]+", expr)
    pos = 0

    def parse_or() -> list[list[str]]:
        nonlocal pos
        result = parse_and()
        while pos < len(tokens) and tokens[pos].upper() == "OR":
            pos += 1
            result = result + parse_and()
        return result

    def parse_and() -> list[list[str]]:
        nonlocal pos
        result = parse_atom()
        while pos < len(tokens) and tokens[pos].upper() == "AND":
            pos += 1
            right = parse_atom()
            result = [a + b for a in result for b in right]
        return result

    def parse_atom() -> list[list[str]]:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("incomplete expression")
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            inner = parse_or()
            if pos >= len(tokens) or tokens[pos] != ")":
                raise ValueError("unbalanced parentheses")
            pos += 1
            return inner
        if pos < len(tokens) and tokens[pos].upper() == "WITH":
            pos += 2
            return [[classify(tok) + "+exception"]]
        return [[classify(tok)]]

    result = parse_or()
    if pos != len(tokens):
        raise ValueError("unexpected text")
    return result


def declared(meta) -> tuple[str, list[list[str]]]:
    """(what the metadata says, its alternatives), from License-Expression, then License, then classifiers."""
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        try:
            return expr, alternatives(expr)
        except ValueError:  # not a valid expression: the gate reports it as a licence it can't allow
            return expr, [["unknown:" + expr]]
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 200:
        try:
            alts = alternatives(text) if re.search(r"\s(OR|AND)\s", text) else [[classify(text)]]
        except ValueError:
            alts = [[classify(text)]]
        if not all(lic.startswith("unknown:") for alt in alts for lic in alt):
            return text, alts
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    classifiers = [c for c in classifiers if c.strip() != "License :: OSI Approved"]
    if classifiers:  # several licence classifiers mean a choice
        return " | ".join(c.split(" :: ")[-1] for c in classifiers), [[classify(c)] for c in classifiers]
    return "", []


def lgpl_ok(name: str) -> bool:
    return name.startswith("pyside6") or name in ("shiboken6", "edge-tts")


def choose(name: str, alts: list[list[str]]) -> list[str] | None:
    """The first alternative the allow-list accepts for this distribution, or None. One with a bare "BSD" is
    taken only when nothing else fits, and still needs bsd_variant() to pass."""
    for alt in sorted(alts, key=lambda a: BARE_BSD in a):
        if all(lic in ALLOWED or lic == BARE_BSD or (lic == "LGPL-3.0" and lgpl_ok(name)) for lic in alt):
            return alt
    return None


def bsd_variant(texts: list[tuple[str, str]]) -> str | None:
    """BSD-3-Clause or BSD-2-Clause, read from a distribution's licence texts; None for the four-clause form (its
    advertising clause) or when no text is a BSD licence."""
    body = []
    for _rel, path in texts:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                body.append(f.read())
        except OSError:
            continue
    flat = re.sub(r"\s+", " ", " ".join(body)).lower()
    if "redistribution and use in source and binary forms" not in flat or "advertising materials" in flat:
        return None
    return "BSD-3-Clause" if "to endorse or promote products" in flat else "BSD-2-Clause"


def never_bundled(inv: Inventory) -> list[str]:
    """The top-level names in the build that must never ship (NEVER_BUNDLED)."""
    found = set()
    for mod in inv.archive_modules + inv.script_entries:
        if NEVER_BUNDLED.match(mod.split(".")[0]):
            found.add(mod.split(".")[0])
    for rel in inv.files:
        top = rel.split("/")[0]
        if NEVER_BUNDLED.match(top):
            found.add(top)
    return sorted(found)


# --- What is inside the build ----------------------------------------------------------------------

@dataclass
class Inventory:
    archive_modules: list[str] = field(default_factory=list)  # in the PYZ inside Saylight.exe
    script_entries: list[str] = field(default_factory=list)  # other entries in the exe's archive
    files: list[str] = field(default_factory=list)  # under _internal, "/"-separated


def read_inventory(dist: str) -> Inventory:
    from PyInstaller.archive.readers import CArchiveReader

    exe = os.path.join(dist, "Saylight.exe")
    internal = os.path.join(dist, "_internal")
    if not os.path.isfile(exe) or not os.path.isdir(internal):
        raise GateError(f"{dist} is not a one-folder Saylight build")
    inv = Inventory()
    pkg = CArchiveReader(exe)
    for name, entry in pkg.toc.items():
        if entry[-1] == "z":
            inv.archive_modules += list(pkg.open_embedded_archive(name).toc)
        else:
            inv.script_entries.append(name)
    for folder, _dirs, files in os.walk(internal):
        for f in files:
            inv.files.append(os.path.relpath(os.path.join(folder, f), internal).replace(os.sep, "/"))
    return inv


def record_index() -> tuple[dict[str, set[str]], dict[str, md.Distribution]]:
    """RECORD path (lower case, relative to site-packages) -> distribution names, and name -> distribution."""
    index: dict[str, set[str]] = collections.defaultdict(set)
    dists: dict[str, md.Distribution] = {}
    for d in md.distributions():
        name = canonical(d.metadata["Name"] or "")
        if not name or name in dists:
            continue  # the first one on sys.path is the one that was imported
        dists[name] = d
        for f in d.files or []:
            p = str(f).replace("\\", "/")
            if not p.startswith("../"):
                index[p.lower()].add(name)
    return index, dists


def _python_dirs() -> list[str]:
    return [sys.base_prefix, os.path.join(sys.base_prefix, "DLLs")]


def attribute(inv: Inventory, index: dict[str, set[str]]) -> tuple[dict[str, set[str]], list[str]]:
    """Component -> the pieces it supplied, plus the pieces nobody owns."""
    owners: dict[str, set[str]] = collections.defaultdict(set)
    unknown: list[str] = []
    stdlib = set(sys.stdlib_module_names)
    python_files = {f.lower() for d in _python_dirs() if os.path.isdir(d) for f in os.listdir(d)}

    for mod in inv.archive_modules:
        top = mod.split(".")[0]
        path = mod.replace(".", "/")
        names = index.get(path.lower() + ".py") or index.get(path.lower() + "/__init__.py")
        if names:
            for n in names:
                owners[n].add("archive:" + mod)
        elif top in APP_MODULES:
            owners[":saylight"].add("archive:" + mod)
        elif top in stdlib:
            owners[":python"].add("archive:" + mod)
        elif PYINSTALLER_MODULES.match(top):
            owners[":pyinstaller"].add("archive:" + mod)
        else:
            prefix = path.lower() + "/"
            names = {n for p, ns in index.items() if p.startswith(prefix) for n in ns}
            if names:  # a namespace package
                for n in names:
                    owners[n].add("archive:" + mod)
            else:
                unknown.append("archive:" + mod)
    for entry in inv.script_entries:
        if entry in APP_MODULES:
            owners[":saylight"].add("exe:" + entry)
        elif PYINSTALLER_MODULES.match(entry):
            owners[":pyinstaller"].add("exe:" + entry)
        else:
            unknown.append("exe:" + entry)

    for rel in inv.files:
        low = rel.lower()
        top = low.split("/")[0]
        if top.endswith(".dist-info"):
            owners[canonical(top.removesuffix(".dist-info").rsplit("-", 1)[0])].add(rel)
        elif low in index:
            for n in index[low]:
                owners[n].add(rel)
        elif low == "base_library.zip":
            owners[":python"].add(rel)
        elif low == "saylight.ico":
            owners[":saylight"].add(rel)
        elif "/" not in low and low in python_files:
            owners[":python"].add(rel)
        else:
            unknown.append(rel)
    return owners, unknown


def check_base_library(dist: str) -> list[str]:
    path = os.path.join(dist, "_internal", "base_library.zip")
    if not os.path.isfile(path):
        return []
    stdlib = set(sys.stdlib_module_names)
    with zipfile.ZipFile(path) as z:
        tops = {n.split("/")[0].removesuffix(".pyc") for n in z.namelist()}
    return [f"base_library.zip:{t}" for t in sorted(tops) if t not in stdlib]


# --- Licence texts ---------------------------------------------------------------------------------

def dist_texts(d: md.Distribution) -> list[tuple[str, str]]:
    """(path relative to the dist-info folder, absolute path) for each licence-like file it ships."""
    found = []
    info = None
    for f in d.files or []:
        parts = str(f).replace("\\", "/").split("/")
        if parts[0].endswith(".dist-info") and len(parts) > 1 and TEXT_NAME.search(parts[-1]):
            info = parts[0]
            rel = "/".join(parts[2:] if parts[1] == "licenses" else parts[1:])
            found.append((rel, str(d.locate_file(f))))
    return [(rel, path) for rel, path in found if info and os.path.isfile(path)]


def _product_version(path: str) -> str | None:
    """ProductVersion from an exe's version resource (the string saylight.spec writes)."""
    version = ctypes.WinDLL("version")
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return None
    ptr, length = ctypes.c_wchar_p(), wintypes.UINT()
    if not version.VerQueryValueW(data, r"\StringFileInfo\040904B0\ProductVersion", ctypes.byref(ptr),
                                  ctypes.byref(length)):
        return None
    return ptr.value


def _inno_registry() -> tuple[str | None, str | None]:
    """(version, folder) of an installed Inno Setup 6, from its uninstall entry (ISCC.exe has no version)."""
    import winreg

    key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"
    for root, view in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY)):
        try:
            with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as k:
                version = winreg.QueryValueEx(k, "DisplayVersion")[0]
                folder = winreg.QueryValueEx(k, "InstallLocation")[0]
                return str(version), str(folder)
        except OSError:
            continue
    return None, None


def inno_setup() -> tuple[str | None, str]:
    """(Inno Setup version, path to its licence text): the installed copy when found, else licenses/."""
    version, registered = _inno_registry()
    candidates = [os.environ.get("INNO_SETUP_DIR", ""), registered or "",
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6"),
                  os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6"),
                  os.path.join(os.environ.get("ProgramFiles", ""), "Inno Setup 6")]
    for folder in candidates:
        if folder and os.path.isfile(os.path.join(folder, "ISCC.exe")) and os.path.isfile(
                os.path.join(folder, "license.txt")):
            return version, os.path.join(folder, "license.txt")
    return version, os.path.join(FIXED, "Inno-Setup.txt")


# --- The gate --------------------------------------------------------------------------------------

@dataclass
class Component:
    key: str
    title: str
    version: str
    licence: str
    folder: str
    texts: list[tuple[str, str]]  # (name in the folder, source path)
    pieces: int = 0


def gate(dist: str) -> tuple[list[Component], dict]:
    inv = read_inventory(dist)
    index, dists = record_index()
    owners, unknown = attribute(inv, index)
    problems = [f"can't tell where this came from: {u}" for u in unknown]
    problems += [f"not part of Python's standard library: {u}" for u in check_base_library(dist)]
    problems += [f"{top} must never be bundled (saylight.spec excludes it)" for top in never_bundled(inv)]

    components: list[Component] = []
    for name in sorted(n for n in owners if not n.startswith(":")):
        d = dists.get(name)
        if d is None:
            problems.append(f"{name}: bundled, but not installed in this Python")
            continue
        meta = d.metadata
        said, alts = declared(meta)
        if not alts:
            problems.append(f"{name}: declares no licence")
            continue
        chosen = choose(name, alts)
        if chosen is None:
            problems.append(f"{name} {d.version}: licence {said!r} is not allowed")
            continue
        if "LGPL-3.0" in chosen:
            inside = sorted(p for p in owners[name] if p.startswith("archive:"))
            if inside:
                problems.append(f"{name}: LGPL modules inside the archive can't be replaced: {inside[:5]}")
        texts = [] if name in SKIP_DIST_TEXTS else dist_texts(d)
        for extra in EXTRA_TEXTS.get(name, ()):
            path = os.path.join(FIXED, extra)
            if not os.path.isfile(path):
                problems.append(f"{name}: licenses/{extra} is missing from the repository")
            texts.append((extra, path))
        if not texts:
            problems.append(f"{name}: ships no licence text and none is kept in licenses/")
        if BARE_BSD in chosen:
            exact = bsd_variant(texts)
            if exact is None:
                problems.append(f"{name} {d.version}: declares only {said!r}, and its licence text isn't the "
                                "BSD-2-Clause or BSD-3-Clause form")
                continue
            chosen = [exact if lic == BARE_BSD else lic for lic in chosen]
        components.append(Component(name, meta["Name"], d.version, " AND ".join(chosen), meta["Name"], texts,
                                    len(owners[name])))

    py_licence = os.path.join(sys.base_prefix, "LICENSE.txt")
    if not os.path.isfile(py_licence):
        problems.append(f"Python's LICENSE.txt not found in {sys.base_prefix}")
    else:
        with open(py_licence, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" not in text or "Windows binary build" not in text:
            problems.append("Python's LICENSE.txt lacks the PSF licence or the Windows build conditions")
    try:
        pyi = md.distribution("pyinstaller")
        pyi_texts = [(rel, p) for rel, p in dist_texts(pyi) if os.path.basename(rel).upper().startswith("COPYING")]
    except md.PackageNotFoundError:
        pyi, pyi_texts = None, []
    if not pyi_texts:
        problems.append("PyInstaller's COPYING.txt not found")
    inno_version, inno_text = inno_setup()
    if not os.path.isfile(inno_text):
        problems.append("Inno Setup's licence text not found")
    for fixed in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        if not os.path.isfile(os.path.join(FIXED, fixed)):
            problems.append(f"licenses/{fixed} is missing from the repository")
    if problems:
        raise GateError("\n".join(problems))

    fixed = {
        "python": Component(":python", "Python", sys.version.split()[0], "PSF-2.0", "Python",
                            [("LICENSE.txt", py_licence)], len(owners.get(":python", ()))),
        "pyinstaller": Component(":pyinstaller", "PyInstaller bootloader", pyi.version if pyi else "?",
                                 "GPL-2.0-or-later WITH Bootloader-exception", "PyInstaller", pyi_texts,
                                 len(owners.get(":pyinstaller", ()))),
        "inno": Component(":inno", "Inno Setup", inno_version or "6", "Inno Setup License", "Inno-Setup",
                          [("license.txt", inno_text)]),
    }
    summary = {"components": len(components) + 3, "pieces": sum(len(v) for v in owners.values()),
               "archive_modules": len(inv.archive_modules), "files": len(inv.files),
               "licences": {c.title: c.licence for c in components}}
    return components + list(fixed.values()), summary


# --- Writing the notices ---------------------------------------------------------------------------

QT_SOURCE = "https://download.qt.io/official_releases/qt/{mm}/{v}/"
PYSIDE_SOURCE = "https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{v}-src/"
EDGE_SOURCE = "https://github.com/rany2/edge-tts/tree/{v}"
CERTIFI_SOURCE = "https://github.com/certifi/python-certifi"


def notices_text(app_version: str, comps: list[Component]) -> str:
    by_key = {c.key: c for c in comps}
    python, pyi, inno = by_key[":python"], by_key[":pyinstaller"], by_key[":inno"]
    qt = by_key.get("pyside6-essentials")
    shib = by_key.get("shiboken6")
    edge = by_key.get("edge-tts")
    certifi = by_key.get("certifi")
    special = {":python", ":pyinstaller", ":inno", "pyside6-essentials", "shiboken6", "edge-tts", "certifi"}
    others = sorted((c for c in comps if c.key not in special), key=lambda c: c.title.lower())

    out = [f"Saylight {app_version}: third-party notices", "",
           "Saylight is free software under the MIT licence. It is built with the parts listed below.",
           "Each part keeps its own licence. The full licence texts are in the \"licenses\" folder",
           "next to this file.", ""]
    n = 1

    def section(title: str, *lines: str) -> None:
        nonlocal n
        out.append(f"{n}. {title}")
        out.extend("   " + line if line else "" for line in lines)
        out.append("")
        n += 1

    section("Saylight", "Copyright (c) 2026 Cole Craven. MIT licence.", "Text: LICENSE.txt")
    section(f"Python {python.version}",
            "Python Software Foundation License Version 2 (PSF-2.0).",
            f"The Windows build of Python also includes {ssl.OPENSSL_VERSION.split(' ')[0]} "
            f"{ssl.OPENSSL_VERSION.split(' ')[1]} (Apache-2.0),",
            "libffi (MIT), zlib, bzip2, xz (0BSD), expat (MIT), mpdecimal (BSD-2-Clause), HACL* (MIT)",
            "and the Microsoft Visual C++ runtime, under the terms in its licence file.",
            "Text: licenses\\Python\\LICENSE.txt")
    if qt:
        mm = ".".join(qt.version.split(".")[:2])
        section(f"Qt {qt.version}, PySide6 {qt.version} and shiboken6 {shib.version if shib else qt.version}",
                "Used under the GNU Lesser General Public License v3 (LGPL-3.0). The LGPL builds on",
                "the GNU General Public License v3 (GPL-3.0), so both texts are included.",
                "Qt, PySide6 and shiboken6 are separate files in the _internal folder (the PySide6",
                "and shiboken6 folders, with Qt6*.dll and *.pyd). You may replace them with your own",
                "build of the same version.",
                "Source code:",
                "  " + QT_SOURCE.format(mm=mm, v=qt.version),
                "  " + PYSIDE_SOURCE.format(v=qt.version),
                "Qt includes code from other projects; they are listed in",
                f"licenses\\{qt.folder}\\Qt-third-party.txt.",
                f"Texts: licenses\\{qt.folder}\\" + (f" and licenses\\{shib.folder}\\" if shib else ""))
    if edge:
        section(f"edge-tts {edge.version}",
                "GNU Lesser General Public License v3 (LGPL-3.0), except srt_composer.py, which is",
                "under the MIT licence. The LGPL text refers to the GPL-3.0 text, which is included.",
                "It ships as plain .py files in _internal\\edge_tts, which you may replace.",
                "Source code: " + EDGE_SOURCE.format(v=edge.version),
                f"Texts: licenses\\{edge.folder}\\")
    if others:
        width = max(len(f"{c.title} {c.version}") for c in others) + 2
        lines = [f"{(c.title + ' ' + c.version).ljust(width)}{c.licence}" for c in others]
        lines += ["", "aiohttp includes llhttp (MIT). Each package's texts are in licenses\\<package name>\\."]
        section("Python packages", *lines)
    if certifi:
        section(f"certifi {certifi.version}",
                "Mozilla Public License 2.0 (MPL-2.0). The certificate list (cacert.pem) ships unmodified",
                "in _internal\\certifi.",
                "Source code: " + CERTIFI_SOURCE,
                f"Text: licenses\\{certifi.folder}\\")
    section(f"PyInstaller {pyi.version} bootloader",
            "GPL-2.0-or-later with the PyInstaller bootloader exception, which lets programs built with",
            "PyInstaller use any licence. Its run-time hooks are under Apache-2.0. Included as a courtesy.",
            "Text: licenses\\PyInstaller\\COPYING.txt")
    section(f"Inno Setup {inno.version} (Saylight's installer and uninstaller)",
            "Copyright (C) 1997-2026 Jordan Russell. Portions Copyright (C) 2000-2026 Martijn Laan.",
            "Inno Setup License. https://jrsoftware.org/",
            "Text: licenses\\Inno-Setup\\license.txt")
    return "\r\n".join(out).rstrip() + "\r\n"


def app_version(dist: str) -> str:
    exe = os.path.join(dist, "Saylight.exe")
    return _product_version(exe) or "?"


def write_notices(dist: str, comps: list[Component], licence: str) -> None:
    folder = os.path.join(dist, "licenses")
    shutil.rmtree(folder, ignore_errors=True)
    for c in comps:
        for rel, src in c.texts:
            dest = os.path.join(folder, c.folder, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest)
    shutil.copyfile(licence, os.path.join(dist, "LICENSE.txt"))
    text = notices_text(app_version(dist), comps)
    with open(os.path.join(dist, "THIRD_PARTY_NOTICES.txt"), "w", encoding="utf-8", newline="") as f:
        f.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Licence gate and third-party notices for a Saylight build.")
    parser.add_argument("dist", help="the one-folder build holding Saylight.exe")
    parser.add_argument("--check", action="store_true", help="run the gate only; write nothing")
    parser.add_argument("--json", action="store_true", help="print a JSON summary")
    parser.add_argument("--license", default=os.path.join(REPO, "LICENSE"), help="Saylight's own licence file")
    args = parser.parse_args(argv)
    dist = os.path.abspath(args.dist)
    try:
        comps, summary = gate(dist)
        if not os.path.isfile(args.license):
            raise GateError(f"Saylight's licence not found at {args.license}")
    except GateError as e:
        print("licence gate failed:\n  " + str(e).replace("\n", "\n  "), file=sys.stderr)
        return 1
    if not args.check:
        write_notices(dist, comps, args.license)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"licence gate passed: {summary['components']} components, {summary['pieces']} pieces traced")
    return 0


if __name__ == "__main__":
    sys.exit(main())

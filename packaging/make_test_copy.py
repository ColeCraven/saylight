"""Make a copy of the Saylight source with the TEST identity, for test builds that never meet the real app.

    python packaging/make_test_copy.py <dest> [--src <repo root>] [--version 1.2.0]

The copy gets the TEST names from packaging/identity.iss in place of the real ones: quit event, app user
model ID, Run value, Start menu shortcut, audio-cache prefix, open-file pipe and the earlier-name ("Read Aloud")
names, so its own clean-ups can never reach the real app's files or windows. It also:
- turns every shortcut off (no Ctrl+`), the media keys off, the panel hidden and the update check off;
- keeps its data in SAYLIGHT_DATA_DIR, or in test_data next to the exe when that isn't set;
- adds "--selftest <report.json>", which runs packaging/selftest.py inside the build.

Every rule must match what it expects, and afterwards no real name may be left in the copy; otherwise this
script fails and removes the half-made folder. test_identity.json is written last, so saylight.spec can tell a
finished test copy (it stamps that exe as a test build) from a release source or an unfinished copy (it refuses
to build one).
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import shutil
import sys
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
IDENTITY_FILE = os.path.join(HERE, "identity.iss")
_DEFINE = re.compile(r'^\s*#define\s+(\w+)\s+"([^"]*)"\s*$')
MARKER = "test_identity.json"          # written last: the copy is finished
SELFTEST_FILE = "saylight_selftest.py"
# SpecialBuild string saylight.spec writes into a test copy's exe. saylight.iss, selftest.py and
# lifecycle_test.ps1 check for it, so a release never wraps a test build and tests never start a release build.
BUILD_STAMP = "Test copy"
_UPDATE_CHECK_KEY = re.compile(r'"updates\.check"\s*:')

PROLOGUE = '''
# --- Test copy (added by packaging/make_test_copy.py) ---
import os as _t_os
import sys as _t_sys

_TEST_HOME = _t_os.path.dirname(_t_os.path.abspath(
    _t_sys.executable if getattr(_t_sys, "frozen", False) else __file__))
_TEST_DATA_DIR = _t_os.environ.setdefault("SAYLIGHT_DATA_DIR", _t_os.path.join(_TEST_HOME, "test_data"))
_t_os.makedirs(_TEST_DATA_DIR, exist_ok=True)
if _t_sys.argv[1:2] == ["--selftest"]:
    import saylight_selftest
    _t_sys.exit(saylight_selftest.main(_t_sys.argv[2:]))
# --- end of test copy prologue ---
'''


class CopyError(Exception):
    """A rule didn't match as expected, or a real name survived."""


def read_identity(path: str = IDENTITY_FILE) -> dict[str, dict[str, str]]:
    """The REAL and TEST name sets from identity.iss, with Inno's "{{" unescaped."""
    sets: dict[str, dict[str, str]] = {"test": {}, "real": {}}
    current = None
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("#ifdef TEST"):
                current = "test"
            elif stripped.startswith("#else"):
                current = "real"
            elif stripped.startswith("#endif"):
                current = None
            elif current and (m := _DEFINE.match(line)):
                sets[current][m.group(1)] = m.group(2).replace("{{", "{")
    if not sets["test"] or set(sets["test"]) != set(sets["real"]):
        raise CopyError("identity.iss must define the same names in its TEST and real blocks")
    return sets


def literal_map(ident: dict[str, dict[str, str]]) -> dict[str, str]:
    """Real string literal -> test string literal, for names that only ever appear as whole literals."""
    real, test = ident["real"], ident["test"]
    pairs = {
        real["QuitEvent"]: test["QuitEvent"],
        real["AumId"]: test["AumId"],
        real["ShortcutName"] + ".lnk": test["ShortcutName"] + ".lnk",
        real["CachePrefix"]: test["CachePrefix"],
        real["CachePrefix"] + "update_": test["CachePrefix"] + "update_",
        real["OpenPipe"]: test["OpenPipe"],
        real["OldRunValue"]: test["OldRunValue"],
        real["OldQuitEvent"]: test["OldQuitEvent"],
        real["OldShortcut"] + ".lnk": test["OldShortcut"] + ".lnk",
        real["OldCachePrefix"]: test["OldCachePrefix"],
    }
    if any(k == v for k, v in pairs.items()):
        raise CopyError("a TEST name equals its real name")
    return pairs


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _string_tokens(text: str):
    """(start offset, end offset, value) for every plain str literal in the source."""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.STRING:
            continue
        try:
            value = ast.literal_eval(tok.string)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str):
            (r0, c0), (r1, c1) = tok.start, tok.end
            yield starts[r0 - 1] + c0, starts[r1 - 1] + c1, value


def replace_literals(text: str, pairs: dict[str, str], counts: dict[str, int]) -> str:
    out, last = [], 0
    for a, b, value in _string_tokens(text):
        if value in pairs:
            out.append(text[last:a])
            out.append(_quote(pairs[value]))
            last = b
            counts[value] = counts.get(value, 0) + 1
    out.append(text[last:])
    return "".join(out)


def sub_exact(text: str, pattern: str, repl: str, expect: range, what: str) -> str:
    new, n = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if n not in expect:
        raise CopyError(f"{what}: matched {n} times")
    return new


def insert_prologue(text: str) -> str:
    """Add PROLOGUE after the module docstring and any `from __future__` imports."""
    tree = ast.parse(text)
    end = 0
    for node in tree.body:
        is_doc = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(
            node.value.value, str)
        is_future = isinstance(node, ast.ImportFrom) and node.module == "__future__"
        if is_doc or is_future:
            end = node.end_lineno or end
        else:
            break
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    return "".join(lines[:end]) + PROLOGUE.replace("\n", newline) + "".join(lines[end:])


def set_version(init_path: str, version: str) -> None:
    with open(init_path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    line = f'__version__ = "{version}"'
    if re.search(r"^__version__\s*=", text, re.MULTILINE):
        text = re.sub(r"^__version__\s*=[^\r\n]*", line, text, count=1, flags=re.MULTILINE)
    else:
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text.rstrip("\r\n") + newline + line + newline
    with open(init_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _read(path: str) -> str:
    with open(path, encoding="utf-8-sig", newline="") as f:  # keeps the file's own line endings
        return f.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def py_files(root: str) -> list[str]:
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
        found += [os.path.join(folder, f) for f in files if f.endswith(".py")]
    return sorted(found)


def check_no_real_names(dest: str, ident: dict[str, dict[str, str]]) -> None:
    """Fail if any real name, an enabled shortcut or the real Run value is left in the copy."""
    pairs = literal_map(ident)
    left = []
    for path in py_files(dest):
        if os.path.basename(path) == SELFTEST_FILE:
            continue
        text = _read(path)
        for a, _b, value in _string_tokens(text):
            if value in pairs:
                left.append(f"{os.path.relpath(path, dest)}:{text.count(chr(10), 0, a) + 1} {value!r}")
        if re.search(r'^\s*APP_NAME\s*=\s*"' + re.escape(ident["real"]["RunValue"]) + '"', text, re.MULTILINE):
            left.append(f"{os.path.relpath(path, dest)}: APP_NAME is still the real Run value")
        if os.path.basename(path) == "settings.py":
            if re.search(r'"hotkeys\.[\w.]+"\s*:\s*\(\s*"[^"]+"', text):
                left.append("settings.py: a shortcut is still on by default")
            if re.search(r'"media_keys"\s*:\s*\(\s*True', text):
                left.append("settings.py: media keys are still on by default")
            if _UPDATE_CHECK_KEY.search(text) and not re.search(r'"updates\.check"\s*:\s*\(\s*False\b', text):
                left.append("settings.py: the update check is still on by default")
    if left:
        raise CopyError("real names left in the test copy:\n  " + "\n  ".join(left))


def source_kind(src: str) -> str:
    """"release" for the real source, "test" for a finished test copy, "unfinished" for a test copy whose making
    stopped part-way (real names may be left in it, so it must never be built)."""
    selftest = os.path.isfile(os.path.join(src, SELFTEST_FILE))
    marker = os.path.isfile(os.path.join(src, MARKER))
    if selftest and marker:
        return "test"
    return "unfinished" if selftest or marker else "release"


def make_test_copy(dest: str, src: str = REPO, version: str | None = None) -> dict:
    """Build the test copy in dest (replacing it) and return a summary of what was changed."""
    ident = read_identity()
    src, dest = os.path.abspath(src), os.path.abspath(dest)
    if os.path.normcase(src + os.sep).startswith(os.path.normcase(dest + os.sep)):
        raise CopyError("dest must not be the source folder or one of its parents")
    if not os.path.isfile(os.path.join(src, "saylight_app.py")):
        raise CopyError(f"saylight_app.py not found in {src}")
    if os.path.isdir(dest) and os.listdir(dest) and source_kind(dest) == "release":
        raise CopyError("dest exists and isn't an earlier test copy; choose an empty folder")
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    try:
        return _fill(dest, src, ident, version)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # never leave a half-converted copy that could be built
        raise


def _fill(dest: str, src: str, ident: dict[str, dict[str, str]], version: str | None) -> dict:
    shutil.copy2(os.path.join(src, "saylight_app.py"), dest)
    shutil.copytree(os.path.join(src, "saylight"), os.path.join(dest, "saylight"),
                    ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"))
    if os.path.isfile(os.path.join(src, "LICENSE")):
        shutil.copy2(os.path.join(src, "LICENSE"), dest)
    shutil.copy2(os.path.join(HERE, "selftest.py"), os.path.join(dest, SELFTEST_FILE))

    pairs = literal_map(ident)
    counts: dict[str, int] = {}
    for path in py_files(dest):
        if os.path.basename(path) == SELFTEST_FILE:
            continue
        text = _read(path)
        new = replace_literals(text, pairs, counts)
        if new != text:
            _write(path, new)
    real = ident["real"]
    for needed in (real["QuitEvent"], real["AumId"], real["CachePrefix"]):
        if not counts.get(needed):
            raise CopyError(f"no literal {needed!r} found to replace; the source layout changed")

    app_py = os.path.join(dest, "saylight_app.py")
    text = _read(app_py)
    text = sub_exact(text, r'^(APP_NAME\s*=\s*)"' + re.escape(real["RunValue"]) + '"',
                     r'\1"' + ident["test"]["RunValue"] + '"', range(1, 2), "APP_NAME")
    legacy_paths = not os.path.isfile(os.path.join(dest, "saylight", "paths.py"))
    if legacy_paths:
        # 1.0 keeps settings and logs next to saylight_app.py, which is _internal in a build.
        text = sub_exact(text, r"^((?:SETTINGS|LOG|CRASH)_PATH\s*=\s*os\.path\.join\()HERE,",
                         r"\1_TEST_DATA_DIR,", range(3, 4), "1.0 data paths")
    text = insert_prologue(text)
    _write(app_py, text)

    settings_py = os.path.join(dest, "saylight", "settings.py")
    text = _read(settings_py)
    text = sub_exact(text, r'("hotkeys\.read"\s*:\s*\(\s*)"[^"]*"', r'\1""', range(1, 2), "hotkeys.read")
    text = sub_exact(text, r'("hotkeys\.[\w.]+"\s*:\s*\(\s*)"[^"]+"', r'\1""', range(0, 100), "other shortcuts")
    text = sub_exact(text, r'("media_keys"\s*:\s*\(\s*)True', r"\1False", range(1, 2), "media_keys")
    text = sub_exact(text, r'("panel\.visible"\s*:\s*\(\s*)True', r"\1False", range(1, 2), "panel.visible")
    # 1.0 has no update check. Once the key exists it must be switched off, or a test copy would ask GitHub
    # about (and offer to install) the real Saylight.
    has_update_check = bool(_UPDATE_CHECK_KEY.search(text))
    text = sub_exact(text, r'("updates\.check"\s*:\s*\(\s*)(?:True|False)\b', r"\1False",
                     range(1, 2) if has_update_check else range(0, 1), "updates.check")
    _write(settings_py, text)

    if version:
        set_version(os.path.join(dest, "saylight", "__init__.py"), version)

    check_no_real_names(dest, ident)
    summary = {"dest": dest, "legacy_paths": legacy_paths, "literals_replaced": sum(counts.values()),
               "identity": ident["test"]}
    with open(os.path.join(dest, MARKER), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest")
    parser.add_argument("--src", default=REPO, help="repository root to copy from")
    parser.add_argument("--version", help="set saylight.__version__ in the copy")
    args = parser.parse_args(argv)
    try:
        summary = make_test_copy(args.dest, args.src, args.version)
    except CopyError as e:
        print(f"make_test_copy: {e}", file=sys.stderr)
        return 1
    print(f"test copy ready: {summary['dest']} ({summary['literals_replaced']} names replaced)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

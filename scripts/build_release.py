"""Build the Windows release: dist/Saylight-Setup.exe and dist/Saylight-Setup.exe.sha256.

    python scripts/build_release.py [--tag v1.2.0] [--out dist] [--legacy-ref v1.1.0] [--skip-tests]
                                    [--skip-lifecycle] [--keep-work]

Steps, stopping at the first failure:
 1. the version in saylight/__init__.py must match the tag (when one is given);
 2. the unit tests;
 3. the release build (PyInstaller, one folder) and its notices, which run the licence gate;
 4. a build of a test copy (the TEST names from packaging/identity.iss), its notices and its frozen self-test
    in a clean environment;
 5. the installers: Saylight-Setup.exe from the release build, SaylightTest-Setup.exe from the test build;
 6. the installer lifecycle test on the test installer (never on the real one). With --legacy-ref it also
    upgrades over a 1.1 source install made from that git ref;
 7. the SHA-256 file next to Saylight-Setup.exe.

Builds happen in a short folder under %TEMP%: Inno Setup fails on paths over 260 characters. Run it with the
Python that has the packages from requirements.txt and requirements-build.txt.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(REPO, "packaging")
SETUP_NAME = "Saylight-Setup.exe"
TEST_SETUP_NAME = "SaylightTest-Setup.exe"
BLOCKED_BY_POLICY = 4551  # ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION, as Smart App Control reports a refused file


class BuildError(Exception):
    pass


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def run(what: str, cmd: list[str], **kw) -> None:
    try:
        code = subprocess.run(cmd, **kw).returncode
    except OSError as e:
        if getattr(e, "winerror", None) != BLOCKED_BY_POLICY:
            raise
        raise BuildError(f"{what}: Windows blocked {os.path.basename(cmd[0])} (Smart App Control); "
                         "rebuild or sign it") from None
    if code != 0:
        raise BuildError(f"{what} failed (exit code {code})")


def source_version() -> str:
    with open(os.path.join(REPO, "saylight", "__init__.py"), encoding="utf-8-sig") as f:
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.MULTILINE)
    if not m:
        raise BuildError("saylight/__init__.py has no __version__")
    return m.group(1)


def _packaging_module(name: str):
    spec = importlib.util.spec_from_file_location(f"_build_{name}", os.path.join(PACKAGING, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


def pyinstaller(src: str, dist: str, work: str) -> str:
    """One-folder build of `src`; returns <dist>\\Saylight."""
    run("PyInstaller", [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", dist,
                        "--workpath", work, os.path.join(PACKAGING, "saylight.spec")],
        env=dict(os.environ, SAYLIGHT_SRC=src))
    return os.path.join(dist, "Saylight")


def notices(build: str) -> None:
    run("The licence gate", [sys.executable, os.path.join(PACKAGING, "collect_notices.py"), build])


def iscc(build: str, out: str, *, test: bool) -> str:
    _version, licence = _packaging_module("collect_notices").inno_setup()  # found next to ISCC.exe when installed
    compiler = os.path.join(os.path.dirname(licence), "ISCC.exe")
    if not os.path.isfile(compiler):
        raise BuildError("Inno Setup 6 not found (install it, or set INNO_SETUP_DIR)")
    defines = ["/DTEST"] if test else []
    run("Inno Setup", [compiler, "/Q", *defines, f"/DSourceDir={build}", f"/O{out}",
                       os.path.join(PACKAGING, "saylight.iss")])
    path = os.path.join(out, TEST_SETUP_NAME if test else SETUP_NAME)
    if not os.path.isfile(path):
        raise BuildError(f"ISCC wrote no {os.path.basename(path)}")
    return path


def legacy_copy(ref: str, work: str) -> str:
    """A test copy of the source at git `ref` (the 1.1 install the lifecycle test upgrades over)."""
    tar = os.path.join(work, "legacy.tar")
    run("git archive", ["git", "-C", REPO, "archive", "--format=tar", "-o", tar, ref])
    raw = os.path.join(work, "legacy_src")
    with tarfile.open(tar) as t:
        t.extractall(raw, filter="data")
    dest = os.path.join(work, "legacy")
    run("make_test_copy", [sys.executable, os.path.join(PACKAGING, "make_test_copy.py"), dest, "--src", raw])
    return dest


def write_checksum(setup: str) -> str:
    h = hashlib.sha256()
    with open(setup, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    path = setup + ".sha256"
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write(f"{h.hexdigest()}  {os.path.basename(setup)}\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Saylight-Setup.exe and its SHA-256 file.")
    parser.add_argument("--tag", help="the release tag, for example v1.2.0; must match __version__")
    parser.add_argument("--out", default=os.path.join(REPO, "dist"), help="where the installer goes")
    parser.add_argument("--legacy-ref", help="also test an upgrade over a 1.1 source install from this git ref")
    parser.add_argument("--skip-tests", action="store_true", help="skip the unit tests")
    parser.add_argument("--skip-lifecycle", action="store_true", help="skip the installer lifecycle test")
    parser.add_argument("--keep-work", action="store_true", help="keep the build folder")
    args = parser.parse_args(argv)

    version = source_version()
    work = tempfile.mkdtemp(prefix="slb_")
    try:
        if len(work) > 60:
            raise BuildError(f"the temp folder {work} is too long for Inno Setup; set TEMP to a short folder")
        step(f"Saylight {version}")
        if args.tag and args.tag.removeprefix("v") != version:
            raise BuildError(f"tag {args.tag} doesn't match __version__ {version}")
        if not args.skip_tests:
            step("Unit tests")
            run("The unit tests", [sys.executable, "-m", "unittest", "discover", "-s", "saylight/tests", "-t", "."],
                cwd=REPO)

        step("Release build")
        release = pyinstaller(REPO, os.path.join(work, "rel"), os.path.join(work, "build_rel"))
        notices(release)

        step("Test build and frozen self-test")
        test_src = os.path.join(work, "test_src")
        run("make_test_copy", [sys.executable, os.path.join(PACKAGING, "make_test_copy.py"), test_src, "--src", REPO])
        test_build = pyinstaller(test_src, os.path.join(work, "test"), os.path.join(work, "build_test"))
        notices(test_build)
        run("The frozen self-test", [sys.executable, os.path.join(PACKAGING, "selftest.py"), test_build,
                                     "--src", test_src, "--report", os.path.join(work, "selftest.json")])

        step("Installers")
        os.makedirs(args.out, exist_ok=True)
        setup = iscc(release, args.out, test=False)
        test_setup = iscc(test_build, work, test=True)

        if not args.skip_lifecycle:
            step("Installer lifecycle (test installer)")
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   os.path.join(PACKAGING, "lifecycle_test.ps1"), "-Setup", test_setup,
                   "-Sandbox", os.path.join(work, "lc"), "-Report", os.path.join(work, "lifecycle.json")]
            if args.legacy_ref:
                cmd += ["-Legacy", legacy_copy(args.legacy_ref, work), "-Python", sys.executable]
            run("The lifecycle test", cmd)

        step("Checksum")
        checksum = write_checksum(setup)
        print(f"{setup}: {os.path.getsize(setup):,} bytes")
        print(f"{checksum}")
        return 0
    finally:
        if args.keep_work:
            print(f"Build folder kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        sys.exit(f"build_release: {e}")

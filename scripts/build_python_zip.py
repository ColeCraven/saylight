"""Build Saylight-Python.zip: the app run by Python, with a one-click installer and uninstaller and a short guide.

This is the version for PCs where Smart App Control blocks the unsigned Saylight.exe: Python itself is signed,
so Windows runs it. Only source files are copied (no settings, logs, caches or tests). The requirements are
looser than requirements.txt, so any Python from 3.10 can install them.
Usage: python scripts/build_python_zip.py [output.zip]   (default: dist/Saylight-Python.zip)
"""

import os
import shutil
import sys
import tempfile
import zipfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_FILES = ["saylight_app.py", "LICENSE", "THIRD_PARTY_NOTICES.md"]
PACKAGE = "saylight"
SKIP_DIRS = {"tests", "__pycache__"}
REQUIREMENTS = """\
# What Saylight needs from PyPI (Install Saylight.cmd installs these for you).
edge-tts>=7.3.1,<8
PySide6-Essentials>=6.12,<7
"""

INSTALL_CMD = r"""@echo off
setlocal EnableExtensions
title Saylight setup
cd /d "%~dp0"
echo.
echo   Saylight setup
echo   --------------
echo.

set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY goto :nopython

set "DEST=%LOCALAPPDATA%\Programs\Saylight"
echo   1/3  Copying Saylight to "%DEST%"
if exist "%DEST%\saylight_app.py" %PY% "%DEST%\saylight_app.py" --quit >nul 2>nul
robocopy "%~dp0app" "%DEST%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail

echo   2/3  Installing the voice and window components (about 80 MB, one time only)
%PY% -m pip install --user --upgrade --disable-pip-version-check -q -r "%DEST%\requirements.txt"
if errorlevel 1 goto :fail

echo   3/3  Starting Saylight and adding it to sign-in and the Start menu
%PY% "%DEST%\saylight_app.py" --install >nul
if errorlevel 1 goto :fail

echo.
echo   All done!
echo.
echo   Highlight text in any app and press  Ctrl + `  (the key left of 1) to hear it.
echo   Press it with nothing highlighted to show or hide the panel.
echo   You can now delete the folder you unzipped.
echo.
pause
exit /b 0

:nopython
echo   Saylight needs Python 3.10 or newer, which is free.
echo.
echo   1. Install it from https://www.python.org/downloads/
echo      (on the first screen, tick "Add python.exe to PATH").
echo   2. Then run this installer again.
echo.
pause
exit /b 1

:fail
echo.
echo   Something went wrong (see the messages above). Check your internet connection and try again.
echo.
pause
exit /b 1
"""

UNINSTALL_CMD = r"""@echo off
setlocal EnableExtensions
title Uninstall Saylight
set "DEST=%LOCALAPPDATA%\Programs\Saylight"
set "PY=python"
python -c "" >nul 2>nul || set "PY=py -3"
if exist "%DEST%\saylight_app.py" %PY% "%DEST%\saylight_app.py" --uninstall
timeout /t 2 /nobreak >nul
rmdir /s /q "%DEST%" >nul 2>nul
echo.
echo   Saylight has been removed. Your settings are kept in "%LOCALAPPDATA%\Saylight"; delete that folder
echo   too if you don't want them. (The Python components it installed are left in place.)
echo.
pause
"""

README = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Saylight</title>
<style>
:root { --bg:#faf5e9; --card:#fffdf8; --text:#24211c; --muted:#6b655b; --accent:#2b4f8c; --line:#e8dfcc; }
@media (prefers-color-scheme: dark) { :root { --bg:#1f2023; --card:#26282c; --text:#e6e1d6; --muted:#9c978d;
  --accent:#8fb4f0; --line:#34363b; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:17px/1.6 Verdana, "Segoe UI", sans-serif;
  letter-spacing:.01em; word-spacing:.08em; }
main { max-width:680px; margin:0 auto; padding:40px 20px 60px; }
h1 { font:600 30px/1.2 "Segoe UI Variable Display", "Segoe UI", sans-serif; margin:0 0 6px; letter-spacing:0; }
h2 { font:600 19px/1.3 "Segoe UI Variable Text", "Segoe UI", sans-serif; margin:34px 0 10px; letter-spacing:0; }
.lead { color:var(--muted); margin:0 0 26px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px 22px; }
kbd { font:600 14px "Segoe UI", sans-serif; border:1px solid var(--line); border-bottom-width:2px; border-radius:6px;
  padding:1px 7px; background:var(--card); }
ol, ul { padding-left:22px; } li { margin:6px 0; }
.note { color:var(--muted); font-size:15px; }
a { color:var(--accent); }
</style></head>
<body><main>
<h1>Saylight</h1>
<p class="lead"><b>Highlight it, and it says it.</b> Highlight text anywhere on your PC and hear it read in a natural voice, with the words highlighted as
they are spoken. Made with dyslexic readers in mind.</p>

<div class="card">
<h2 style="margin-top:0">Install</h2>
<ol>
<li>Unzip this folder anywhere.</li>
<li>Double-click <b>Install Saylight.cmd</b>. It takes a minute and needs an internet connection.
 If Windows asks whether to run it, choose <i>More info</i> then <i>Run anyway</i>.</li>
<li>If it says Python is missing, install it free from <a href="https://www.python.org/downloads/">python.org</a>
 (tick <i>Add python.exe to PATH</i>), then run the installer again.</li>
</ol>
</div>

<h2>Use it</h2>
<ul>
<li>Highlight text in any app, then press <kbd>Ctrl</kbd> <kbd>`</kbd> (the key left of <kbd>1</kbd>).</li>
<li>Press <kbd>Ctrl</kbd> <kbd>`</kbd> with nothing highlighted to <b>show or hide the panel</b>, or to
 <b>pause and resume</b> while it is reading.</li>
<li>In the panel: go back or forward a sentence, change the speed, or click any sentence to read from there.
 Scroll over the text to look back; <kbd>Ctrl</kbd>+scroll changes the text size.</li>
<li>Click &#9998; (or the empty reading box) to <b>type or paste your own text</b>, then press <b>Read aloud</b> or
 <kbd>Ctrl</kbd> <kbd>Enter</kbd>. <kbd>Esc</kbd> goes back and keeps your text.</li>
<li>The eraser button <b>clears</b> the reading box. <b>Undo</b> appears for 10 seconds if you change your mind.</li>
<li>The gear button opens <b>Settings</b>. <b>Reading display</b> has text size, font, spacing, highlight style
 and background colour, plus options to skip citations and web links in academic papers. <b>Voice</b> lets you
 choose and preview other natural voices. <b>General</b> covers starting with Windows and updates.</li>
<li>The tray icon (near the clock) shows when it is reading. Right-click it for more options, including Quit.</li>
</ul>

<h2>Good to know</h2>
<ul>
<li>It starts automatically when you sign in. To stop that, use <i>Task Manager &rarr; Startup apps</i>, or run
 <b>Uninstall Saylight.cmd</b> to remove it completely.</li>
<li><b>Privacy:</b> the voice is Microsoft's online neural voice (the same voices as Microsoft Edge's Read Aloud,
 via the open-source <a href="https://github.com/rany2/edge-tts">edge-tts</a> project), so the text you have read
 aloud is sent to Microsoft to be spoken. Avoid using it on confidential text. Nothing is stored or logged by
 Saylight itself.</li>
<li>It needs an internet connection to speak.</li>
<li>This is the Python version of Saylight, for PCs where Smart App Control blocks <b>Saylight-Setup.exe</b>.
 When a new version is out, the panel offers <b>Get it</b>, which opens the download page.</li>
</ul>
<p class="note">Saylight is a personal project and is not affiliated with Microsoft.</p>
</main></body></html>
"""


def stage(root: str) -> None:
    app = os.path.join(root, "app")
    os.makedirs(app)
    for name in APP_FILES:
        shutil.copy2(os.path.join(SRC, name), app)
    for dirpath, dirnames, filenames in os.walk(os.path.join(SRC, PACKAGE)):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = os.path.relpath(dirpath, SRC)
        os.makedirs(os.path.join(app, rel), exist_ok=True)
        for f in filenames:
            if f.endswith(".py"):
                shutil.copy2(os.path.join(dirpath, f), os.path.join(app, rel, f))
    with open(os.path.join(app, "requirements.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(REQUIREMENTS)
    for name, text in (("Install Saylight.cmd", INSTALL_CMD), ("Uninstall Saylight.cmd", UNINSTALL_CMD)):
        with open(os.path.join(root, name), "w", encoding="ascii", newline="\r\n") as f:
            f.write(text)
    with open(os.path.join(root, "Read Me.html"), "w", encoding="utf-8") as f:
        f.write(README)


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SRC, "dist", "Saylight-Python.zip")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    work = tempfile.mkdtemp(prefix="saylight_pkg_")
    root = os.path.join(work, "Saylight")
    stage(root)
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                full = os.path.join(dirpath, f)
                z.write(full, os.path.relpath(full, work))
    shutil.rmtree(work, ignore_errors=True)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    print(f"{out}: {len(names)} files, {os.path.getsize(out) // 1024} KB")
    for n in names:
        if not n.startswith("Saylight/app/saylight/"):
            print("  ", n)


if __name__ == "__main__":
    main()

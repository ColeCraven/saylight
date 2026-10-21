# Third-party notices

Saylight is free software under the [MIT licence](LICENSE). The Windows app and its installer are built with
the parts below. Each part keeps its own licence.

The installed app has the complete notices: `THIRD_PARTY_NOTICES.txt` and a `licenses` folder next to
`Saylight.exe`, with every full licence text. `packaging/collect_notices.py` writes them for each build from
the packages that are actually inside it. The same script is the build's licence gate: the build fails if
any part can't be traced, has no licence text, or uses a licence outside the list at the end of this page.

The texts kept in this repository are in [`licenses/`](licenses).

## Inside Saylight.exe and its folder

| Part | Version | Licence | How it ships |
|---|---|---|---|
| Saylight | see the release | MIT, © 2026 Cole Craven | `Saylight.exe` and its archive |
| Python | 3.14.6 | PSF-2.0. The Windows build also includes OpenSSL (Apache-2.0), libffi (MIT), zlib, bzip2, xz (0BSD), expat (MIT), mpdecimal (BSD-2-Clause), HACL* (MIT) and the Microsoft Visual C++ runtime | Inside the build; its `LICENSE.txt` ships unchanged |
| Qt (Core, GUI, Widgets, Network and four plugins) | 6.12.0 | LGPL-3.0 | Separate DLLs in `_internal\PySide6`, which can be replaced |
| PySide6-Essentials and shiboken6 | 6.12.0 | LGPL-3.0 | Separate `.pyd`, DLL and `.py` files in `_internal`, which can be replaced |
| edge-tts | 7.3.1 | LGPL-3.0; `srt_composer.py` is MIT | Plain `.py` files in `_internal\edge_tts`, which can be replaced |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT (includes llhttp, MIT) | Inside the archive |
| aiosignal, frozenlist, multidict, propcache, yarl | 1.4.0, 1.8.0, 7.1.0, 0.5.4, 1.25.1 | Apache-2.0 | Inside the archive |
| aiohappyeyeballs, typing_extensions | 2.7.1, 4.16.0 | PSF-2.0 | Inside the archive |
| attrs | 26.1.0 | MIT | Inside the archive |
| idna | 3.19 | BSD-3-Clause | Inside the archive |
| certifi | 2026.7.22 | MPL-2.0 | `cacert.pem` ships unmodified in `_internal\certifi` |
| pypdf | 6.14.2 | BSD-3-Clause | Inside the archive; loaded only when a PDF is opened |
| pywinrt: winrt-runtime and its Windows.Foundation, Foundation.Collections, Storage, Storage.Streams, Graphics.Imaging, Media.Ocr, Globalization, Data.Pdf, Media.SpeechSynthesis and Media.Core packages | 3.2.1 | MIT, © Microsoft Corporation and David Lechner. Text: [`licenses/pywinrt-MIT.txt`](licenses/pywinrt-MIT.txt) | `.pyd` files in `_internal\winrt`; winrt-runtime's own `MSVCP140.dll` is left out |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later with the bootloader exception, which allows any licence for the app | Inside `Saylight.exe` |

The LGPL-3.0 builds on the GPL-3.0, so both texts ship: [`licenses/LGPL-3.0.txt`](licenses/LGPL-3.0.txt) and
[`licenses/GPL-3.0.txt`](licenses/GPL-3.0.txt).

Source code for the LGPL parts:
- Qt 6.12.0: <https://download.qt.io/official_releases/qt/6.12/6.12.0/>
- PySide6 and shiboken6 6.12.0: <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/>
- edge-tts 7.3.1: <https://github.com/rany2/edge-tts/tree/7.3.1>

Qt includes code from other projects. They are listed in
[`licenses/Qt-third-party.txt`](licenses/Qt-third-party.txt).

Windows provides the rest and Saylight doesn't ship it: Microsoft's online voices, Windows' own voices, text
recognition (OCR) and PDF rendering, Windows audio (MCI), and the Segoe, Verdana, Calibri and Sitka fonts.

pypdf can use two optional packages that Saylight leaves out: Pillow (for pictures, which Saylight doesn't read)
and cryptography (for AES-encrypted PDFs, which would add more than 10 MB). Without cryptography, an
AES-encrypted PDF shows "This PDF is protected" and how to read it from your PDF reader instead.

## Inside Saylight-Setup.exe

| Part | Version | Licence |
|---|---|---|
| Inno Setup (the setup and uninstall program) | 6.7.3 | Inno Setup License: Copyright (C) 1997-2026 Jordan Russell. Portions Copyright (C) 2000-2026 Martijn Laan. Text: [`licenses/Inno-Setup.txt`](licenses/Inno-Setup.txt) |

## Licences the build accepts

MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, PSF-2.0, MPL-2.0, 0BSD and ISC. A package that only says "BSD"
passes when its licence text is the two- or three-clause form. LGPL-3.0 only for PySide6, shiboken6 and
edge-tts, and only as replaceable files. GPL and AGPL code is never imported, linked or bundled. Pillow,
numpy and Tk are never bundled, whatever their licence.

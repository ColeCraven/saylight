"""Write saylight.ico for the build: the app's own icon drawing at every size Windows asks for.

    python packaging/make_icon.py <out.ico> [--src <folder holding saylight_app.py>] [--wizard <folder>]

The 256 px master comes from saylight_app.make_icon, so the exe, the installer and the Start menu entry use
the same drawing as the app. Smaller sizes are scaled down from it in halving steps, which keeps the voice
bars crisp at 16 and 24 px. Sizes up to 64 px are stored as 32-bit bitmaps and 256 px as PNG, the layout
Explorer, the taskbar and Inno Setup all read.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile

SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _master(src: str):
    """The 256 px icon as a QImage, drawn by the app's make_icon."""
    from PySide6.QtGui import QImage

    if src not in sys.path:
        sys.path.insert(0, src)
    import saylight_app

    with tempfile.TemporaryDirectory(prefix="slicon_") as tmp:
        path = os.path.join(tmp, "master.ico")
        if not saylight_app.make_icon(path):
            raise SystemExit("saylight_app.make_icon could not draw the icon")
        img = QImage(path)
    if img.isNull() or img.width() != 256:
        raise SystemExit("the icon master is not a 256 px image")
    return img.convertToFormat(QImage.Format.Format_ARGB32)


def _scaled(master, size: int):
    """Halve with smooth filtering until close, then one final smooth step to the exact size."""
    from PySide6.QtCore import Qt

    img = master
    while img.width() >= size * 2:
        img = img.scaled(img.width() // 2, img.height() // 2, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    if img.width() != size:
        img = img.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return img


def _bmp_entry(img) -> bytes:
    """A 32-bit DIB with straight alpha, bottom-up, followed by an all-clear AND mask."""
    from PySide6.QtGui import QImage

    img = img.convertToFormat(QImage.Format.Format_ARGB32)  # smooth scaling may hand back premultiplied
    size = img.width()
    rows = []
    for y in range(size - 1, -1, -1):
        rows.append(bytes(img.constScanLine(y))[: size * 4])  # Format_ARGB32 is BGRA in memory
    mask_row = ((size + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    return header + b"".join(rows) + bytes(mask_row * size)


def _png_entry(img) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        raise SystemExit("could not encode the 256 px icon as PNG")
    buf.close()
    return bytes(data)


def write_ico(path: str, src: str = REPO) -> list[int]:
    """Write a multi-size .ico to path and return the sizes it holds."""
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication.instance() or QGuiApplication(["make_icon"])
    master = _master(src)
    images = [(s, _png_entry(master) if s == 256 else _bmp_entry(_scaled(master, s))) for s in SIZES]
    offset = 6 + 16 * len(images)
    directory = struct.pack("<HHH", 0, 1, len(images))
    blobs = []
    for size, blob in images:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs.append(blob)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(directory + b"".join(blobs))
    os.replace(tmp, path)
    return list(SIZES)


# Setup's wizard images at 100, 125, 150, 200 and 250 % scaling; Setup picks the closest. Both are the icon on a
# transparent background, so they suit the light and the dark wizard.
WIZARD_SCALES = (1.0, 1.25, 1.5, 2.0, 2.5)
WIZARD_SMALL = 55   # the corner image on inner pages
WIZARD_LARGE = (164, 314)  # the side panel on the finish page


def write_wizard_images(folder: str, src: str = REPO) -> list[str]:
    """Write wizard_small_<pct>.png and wizard_large_<pct>.png into folder; returns the file names."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication, QImage, QPainter

    _app = QGuiApplication.instance() or QGuiApplication(["make_icon"])
    master = _master(src)
    os.makedirs(folder, exist_ok=True)
    names = []
    for scale in WIZARD_SCALES:
        pct = round(scale * 100)
        small = _scaled(master, round(WIZARD_SMALL * scale))
        name = f"wizard_small_{pct}.png"
        small.save(os.path.join(folder, name), "PNG")
        names.append(name)
        w, h = round(WIZARD_LARGE[0] * scale), round(WIZARD_LARGE[1] * scale)
        canvas = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(0)
        icon = _scaled(master, round(96 * scale))
        p = QPainter(canvas)
        p.drawImage(QPoint((w - icon.width()) // 2, round(h * 0.42) - icon.height() // 2), icon)
        p.end()
        name = f"wizard_large_{pct}.png"
        canvas.convertToFormat(QImage.Format.Format_ARGB32).save(os.path.join(folder, name), "PNG")
        names.append(name)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out")
    parser.add_argument("--src", default=REPO, help="folder holding saylight_app.py")
    parser.add_argument("--wizard", metavar="FOLDER", help="also write Setup's wizard images into FOLDER")
    args = parser.parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # drawing only; never shows a window
    sizes = write_ico(args.out, os.path.abspath(args.src))
    print(f"icon: {len(sizes)} sizes")
    if args.wizard:
        print(f"wizard images: {len(write_wizard_images(args.wizard, os.path.abspath(args.src)))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

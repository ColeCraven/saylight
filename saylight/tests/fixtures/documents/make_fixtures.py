"""Write the document fixtures in this folder (run it again only to change them).

    python make_fixtures.py

* paper.pdf: a three-page paper laid out the way LaTeX and publishers do it: a title, authors and "Abstract"
  in larger type, body text with author-year and numbered citations, a footnote mark and its footnote in small
  type, a figure whose labels sit inside an embedded form, a caption, a small-type table, a rotated side stamp,
  a running header, a "Downloaded from" footer and page numbers on every page, a word hyphenated across a page
  break, a reference list and an appendix after it.
* scan.pdf: a page that is only a picture. blank.pdf: two empty pages. unmapped.pdf: text in a font with no
  Unicode mapping (what other tools show as "(cid:3)(cid:4)").
* rc4_user.pdf / rc4_owner.pdf / aes_user.pdf / aes_owner.pdf: paper.pdf's first page encrypted with RC4-128
  or AES-128, with the user password "reading", or with restrictions only. pypdf needs the cryptography
  package to write AES; here Windows' own AES (bcrypt.dll) stands in for it, for writing these files only.
* handmade.docx: WordprocessingML written by hand with what Word and citation managers produce (split runs,
  tracked changes, hidden text, footnotes, lists, fields, tables, text boxes, an equation, a TOC field).

The text is made up for these tests and is MIT-licensed with Saylight.
"""

from __future__ import annotations

import ctypes
import os
import sys
import zipfile
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
PASSWORD = "reading"


# --- A minimal PDF writer ---------------------------------------------------------------------------------------

def _esc(s: str) -> bytes:
    return s.encode("cp1252").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Pdf:
    def __init__(self) -> None:
        self.objects: list[bytes | None] = []
        self.pages: list[int] = []
        self.fonts = {
            "F1": self.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman /Encoding /WinAnsiEncoding >>"),
            "F2": self.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Bold /Encoding /WinAnsiEncoding >>"),
            "F3": self.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"),
        }

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def stream(self, dictionary: bytes, data: bytes) -> int:
        return self.add(dictionary[:-2] + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    def page(self, content: bytes, xobjects: dict[str, int] | None = None, fonts: dict[str, int] | None = None) -> None:
        fonts = {**self.fonts, **(fonts or {})}
        res = b"<< /Font << " + b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in fonts.items()) + b" >>"
        if xobjects:
            res += b" /XObject << " + b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in xobjects.items()) + b" >>"
        res += b" >>"
        c = self.stream(b"<< >>", content)
        self.pages.append(self.add(b"<< /Type /Page /Parent PAGES /MediaBox [0 0 612 792] /Resources " + res +
                                   b" /Contents %d 0 R >>" % c))

    def save(self, path: str) -> None:
        pages_id = len(self.objects) + 1
        kids = b" ".join(b"%d 0 R" % p for p in self.pages)
        self.objects = [o.replace(b"PAGES", b"%d 0 R" % pages_id) if o else o for o in self.objects]
        self.add(b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(self.pages))
        catalog = self.add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for n, body in enumerate(self.objects, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(self.objects) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(self.objects) + 1, catalog,
                                                                                   xref)
        with open(path, "wb") as f:
            f.write(out)


class Page:
    """Lines of text at given positions and sizes, as a content stream."""

    def __init__(self) -> None:
        self.ops: list[bytes] = []

    def line(self, x: float, y: float, text: str, size: float = 10, font: str = "F1") -> Page:
        self.ops.append(b"BT /%s %g Tf 1 0 0 1 %g %g Tm (%s) Tj ET" % (font.encode(), size, x, y, _esc(text)))
        return self

    def runs(self, x: float, y: float, parts: list[tuple[str, float]], font: str = "F1") -> Page:
        """One line made of runs in different sizes ("text", 10), ("1", 6): a footnote mark or a subscript."""
        ops = [b"BT 1 0 0 1 %g %g Tm" % (x, y)]
        for text, size in parts:
            ops.append(b"/%s %g Tf (%s) Tj" % (font.encode(), size, _esc(text)))
        ops.append(b"ET")
        self.ops.append(b" ".join(ops))
        return self

    def rotated(self, x: float, y: float, text: str, size: float = 10) -> Page:
        self.ops.append(b"BT /F1 %g Tf 0 1 -1 0 %g %g Tm (%s) Tj ET" % (size, x, y, _esc(text)))
        return self

    def para(self, x: float, y: float, lines: list[str], size: float = 10, lead: float = 12) -> float:
        for line in lines:
            self.line(x, y, line, size)
            y -= lead
        return y

    def raw(self, op: bytes) -> Page:
        self.ops.append(op)
        return self

    def data(self) -> bytes:
        return b"\n".join(self.ops)


def furniture(p: Page, number: int) -> None:
    p.line(72, 760, "Journal of Reading Research 12 (2026) 101–103", 8, "F3")
    p.line(72, 40, "Downloaded from https://journals.example.org on 1 January 2026", 7, "F3")
    p.line(300, 52, str(100 + number), 8, "F3")
    p.rotated(30, 250, "arXiv:2601.00001v1 [cs.CL] 1 Jan 2026", 18)


def paper_pages() -> list[tuple[Page, dict[str, bytes] | None]]:
    p1 = Page()
    furniture(p1, 1)
    p1.line(110, 720, "Reading Aloud Helps Readers with Dyslexia", 17, "F2")
    p1.line(190, 696, "Ada Lovelace and Grace Hopper", 12)
    p1.line(280, 670, "Abstract", 12, "F2")
    y = p1.para(72, 650, [
        "We test whether hearing a text while reading it helps adults with dyslexia. In two studies",
        "(n = 120), listening along improved comprehension by 12%, as shown in [3]. The effect held",
        "across ages and texts.",
    ])
    p1.line(72, y - 10, "1 Introduction", 12, "F2")
    y = p1.para(72, y - 30, [
        "Dyslexia affects reading for about one in ten people (Snowling et al., 2020; Peters, 2018a).",
        "Many readers use text-to-speech programs to keep up with long texts.",
    ])
    p1.runs(72, y, [("Some programs mark each word as it is spoken,", 10), ("1", 6),
                    (" which may help readers keep their place.", 10)])
    y -= 12
    y = p1.para(72, y, [
        "Earlier work found mixed effects of such support on accuracy (Wood et al., 2018, p. 12),",
        "but few studies looked at the compre-",
    ])
    p1.raw(b"q 1 0 0 1 380 250 cm /Fm1 Do Q")
    p1.line(72, 190, "Figure 1: Accuracy by condition. Bars show the mean of each group.", 10)
    p1.line(72, 90, "1 See the project page for the programs we tested and their settings.", 8)
    figure = (b"<< /Type /XObject /Subtype /Form /BBox [0 0 200 120] /Resources << /Font << /F3 FONT3 >> >> >>",
              b"BT /F3 7 Tf 1 0 0 1 10 100 Tm (Accuracy) Tj ET\nBT /F3 7 Tf 1 0 0 1 10 60 Tm (0.8) Tj ET\n"
              b"BT /F3 7 Tf 1 0 0 1 10 20 Tm (0.6) Tj ET\nBT /F3 7 Tf 1 0 0 1 80 5 Tm (Time \\(s\\)) Tj ET\n"
              b"0 0 m 200 0 l S")

    p2 = Page()
    furniture(p2, 2)
    y = p2.para(72, 720, [
        "hension of long texts. We therefore asked whether reading along helps, e.g. in university",
        "courses, and whether the help lasts. Code and data are at https://osf.io/abc12 for anyone.",
    ])
    p2.line(72, y - 10, "2 Method", 12, "F2")
    y = p2.para(72, y - 30, [
        "Participants read twelve passages, half with the voice and half without (see Fig. 1).",
        "Each passage was followed by ten questions; we report the share answered correctly.",
    ])
    p2.line(72, y - 20, "Table 1: Mean accuracy and reading time by group.", 10)
    y = p2.para(72, y - 36, ["Group Accuracy Time n", "Dyslexic 0.81 1.2 40", "Control 0.93 0.9 40"], 8, 10)
    y = p2.para(72, y - 20, [
        "Listening along raised accuracy in both groups, and the gain was larger for readers with",
        "dyslexia than for the control group.",
    ])

    p3 = Page()
    furniture(p3, 3)
    p3.line(72, 720, "3 Results", 12, "F2")
    y = p3.para(72, 700, [
        "The gain held a month later. Readers said the voice helped them keep their place.",
    ])
    p3.runs(72, y, [("A model trained on the answers, BERT", 10), ("BASE", 7), (", agreed with the raters.", 10)])
    y -= 12
    p3.line(72, y - 12, "References", 12, "F2")
    y = p3.para(72, y - 32, [
        "Peters, M. 2018a. Deep contextual word representations. In Proceedings of NAACL,",
        "pages 2227–2237.",
        "Snowling, M. J., Hulme, C., and Nation, K. 2020. Defining and understanding dyslexia.",
        "Oxford Review of Education, 46(4):501–513.",
        "Wood, S. G., Moxley, J. H., Tighe, E. L., and Wagner, R. K. 2018. Does use of text-to-speech",
        "and related read-aloud tools improve reading comprehension? Journal of Learning",
        "Disabilities, 51(1):73–84.",
    ])
    p3.line(72, y - 12, "Appendix A. Passages", 12, "F2")
    p3.para(72, y - 32, ["The twelve passages came from first-year textbooks, four from each of three subjects."])
    return [(p1, {"Fm1": figure}), (p2, None), (p3, None)]


def write_paper(path: str, pages: int = 3) -> None:
    pdf = Pdf()
    for page, forms in paper_pages()[:pages]:
        xobjects = {}
        for name, (dictionary, data) in (forms or {}).items():
            xobjects[name] = pdf.stream(dictionary.replace(b"FONT3", b"%d 0 R" % pdf.fonts["F3"]), data)
        pdf.page(page.data(), xobjects)
    pdf.save(path)


def write_scan(path: str) -> None:
    pdf = Pdf()
    image = pdf.stream(b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
                       b"/BitsPerComponent 8 >>", b"\x00\xff\xff\x00")
    pdf.page(b"q 500 0 0 700 50 50 cm /Im1 Do Q", {"Im1": image})
    pdf.save(path)


def write_blank(path: str) -> None:
    pdf = Pdf()
    pdf.page(b"")
    pdf.page(b"")
    pdf.save(path)


def write_unmapped(path: str) -> None:
    """Glyph codes in a two-byte font with no ToUnicode map: the text has no Unicode meaning."""
    pdf = Pdf()
    descendant = pdf.add(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /AAAAAA+Calibri "
                         b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 500 >>")
    font = pdf.add(b"<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+Calibri /Encoding /Identity-H "
                   b"/DescendantFonts [%d 0 R] >>" % descendant)
    lines = []
    for k in range(12):
        codes = b"".join(b"%04X" % (3 + (k * 7 + j) % 60) for j in range(40))
        lines.append(b"BT /F9 10 Tf 1 0 0 1 72 %d Tm <%s> Tj ET" % (700 - 14 * k, codes))
    pdf.page(b"\n".join(lines), fonts={"F9": font})
    pdf.save(path)


# --- AES from Windows, for writing the AES fixtures only -----------------------------------------------------

def _bcrypt_aes(mode: str, key: bytes, iv: bytes | None, data: bytes, encrypt: bool) -> bytes:
    bc = ctypes.WinDLL("bcrypt")
    alg = ctypes.c_void_p()
    hkey = ctypes.c_void_p()
    assert bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0) == 0
    try:
        chain = ctypes.create_unicode_buffer("ChainingModeCBC" if mode == "cbc" else "ChainingModeECB")
        assert bc.BCryptSetProperty(alg, "ChainingMode", chain, ctypes.sizeof(chain), 0) == 0
        kbuf = ctypes.create_string_buffer(key, len(key))
        assert bc.BCryptGenerateSymmetricKey(alg, ctypes.byref(hkey), None, 0, kbuf, len(key), 0) == 0
        try:
            src = ctypes.create_string_buffer(data, len(data))
            out = ctypes.create_string_buffer(len(data))
            got = wintypes.ULONG()
            ivbuf = ctypes.create_string_buffer(iv, len(iv)) if iv else None
            fn = bc.BCryptEncrypt if encrypt else bc.BCryptDecrypt
            assert fn(hkey, src, len(data), None, ivbuf, len(iv) if iv else 0, out, len(data), ctypes.byref(got),
                      0) == 0
            return out.raw[:got.value]
        finally:
            bc.BCryptDestroyKey(hkey)
    finally:
        bc.BCryptCloseAlgorithmProvider(alg, 0)


def _use_windows_aes() -> None:
    import secrets

    from pypdf import _encryption

    class WindowsAES:
        def __init__(self, key: bytes) -> None:
            self.key = key

        def encrypt(self, data: bytes) -> bytes:
            iv = secrets.token_bytes(16)
            pad = 16 - len(data) % 16
            return iv + _bcrypt_aes("cbc", self.key, iv, data + bytes([pad]) * pad, True)

        def decrypt(self, data: bytes, *, strict: bool = True) -> bytes:
            iv, data = data[:16], data[16:]
            if not data:
                return data
            plain = _bcrypt_aes("cbc", self.key, iv, data, False)
            return plain[:-plain[-1]] if 0 < plain[-1] <= 16 else plain

    _encryption.CryptAES = WindowsAES
    _encryption.aes_cbc_encrypt = lambda key, iv, data: _bcrypt_aes("cbc", key, iv, data, True)
    _encryption.aes_cbc_decrypt = lambda key, iv, data: _bcrypt_aes("cbc", key, iv, data, False)
    _encryption.aes_ecb_encrypt = lambda key, data: _bcrypt_aes("ecb", key, None, data, True)
    _encryption.aes_ecb_decrypt = lambda key, data: _bcrypt_aes("ecb", key, None, data, False)


def write_encrypted(source: str) -> None:
    import pypdf
    _use_windows_aes()
    for name, user, algorithm in (("rc4_user", PASSWORD, "RC4-128"), ("rc4_owner", "", "RC4-128"),
                                  ("aes_user", PASSWORD, "AES-128"), ("aes_owner", "", "AES-128")):
        writer = pypdf.PdfWriter(clone_from=source)
        writer.encrypt(user, "owner-" + PASSWORD, algorithm=algorithm)
        with open(os.path.join(HERE, name + ".pdf"), "wb") as f:
            writer.write(f)


# --- A Word file written by hand -----------------------------------------------------------------------------

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:v="urn:schemas-microsoft-com:vml" '
      'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
      'mc:Ignorable="wps"')


def _p(*runs, style=None, num=None):
    ppr = ""
    if style or num:
        ppr = "<w:pPr>"
        if style:
            ppr += f'<w:pStyle w:val="{style}"/>'
        if num:
            ppr += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
        ppr += "</w:pPr>"
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def _r(text, hidden=False):
    rpr = "<w:rPr><w:vanish/></w:rPr>" if hidden else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _field(instr, result):
    return (f'<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r><w:instrText xml:space="preserve">{instr}</w:instrText></w:r>'
            f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>{_r(result)}'
            f'<w:r><w:fldChar w:fldCharType="end"/></w:r>')


def _cell(text, merge=None):
    tcpr = f'<w:tcPr><w:vMerge w:val="{merge}"/></w:tcPr>' if merge == "restart" else (
        "<w:tcPr><w:vMerge/></w:tcPr>" if merge == "cont" else "")
    return f"<w:tc>{tcpr}{_p(_r(text)) if text else _p()}</w:tc>"


def write_handmade_docx(path: str) -> None:
    box = ('<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic><a:graphicData>'
           '<wps:wsp><wps:txbx><w:txbxContent>' + _p(_r("Box: sidebar note.")) +
           '</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>'
           '<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>' + _p(_r("Box: sidebar note.")) +
           '</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>')
    inner = "<w:tbl><w:tr>" + _cell("inner A") + _cell("inner B") + "</w:tr></w:tbl>"
    body = "".join([
        _p(_r("Reading in Dyslexia"), style="Title"),
        _field('TOC \\o "1-3"', "Introduction 1"),
        _p(_r("Introduction"), style="Heading1", num=("3", "0")),
        _p(_r("Phonological deficits affect reading compre"), _r("hension in many children"),
           _field('ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"x"}', " (Snowling et al., 2020)"), _r(". fMRI "),
           _r("studies"), '<w:r><w:footnoteReference w:id="2"/></w:r>',
           _r(" show differences in the left hemisphere.")),
        _p(_r("Readers were "), '<w:del><w:r><w:delText>always </w:delText></w:r></w:del>',
           '<w:ins><w:r><w:t xml:space="preserve">not </w:t></w:r></w:ins>', _r("slower on nonwords"),
           _r(" (hidden note)", hidden=True), _field("ADDIN EN.CITE &lt;EndNote&gt;", " [3]"), _r(".")),
        _p(_r("Page 4 shows the well"), "<w:r><w:noBreakHyphen/></w:r>", _r("known effect of phono"),
           "<w:r><w:softHyphen/></w:r>", _r("logy"), '<w:r><w:sym w:font="Symbol" w:char="F0B1"/></w:r>',
           _r(" noise.")),
        _p(_r("Recruit participants"), num=("1", "0")),
        _p(_r("Screen for vision problems"), num=("1", "1")),
        _p(_r("Screen for hearing problems"), num=("1", "1")),
        _p(_r("Run the reading task"), num=("1", "0")),
        _p(_r("Bullet one"), num=("2", "0")),
        _p(_r("Bullet two"), num=("2", "0")),
        _p(_r("Data are at the "), '<w:hyperlink r:id="rId9">' + _r("OSF page") + "</w:hyperlink>", _r(".")),
        "<w:tbl><w:tr>" + _cell("Group") + _cell("Accuracy") + _cell("Speed") + "</w:tr>"
        "<w:tr>" + _cell("Dyslexic", "restart") + _cell("0.81") + _cell("1.2 s") + "</w:tr>"
        "<w:tr>" + _cell("", "cont") + _cell("0.79") +
        "<w:tc>" + inner + _p(_r("after inner")) + "</w:tc></w:tr></w:tbl>",
        _p(_r("Figure text follows."), box),
        _p(_r("We fit "), "<m:oMath><m:r><m:t>y=ax+b</m:t></m:r></m:oMath>", _r(" to the data.")),
        _p(_r("References"), style="Heading1"),
        _p('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
           '<w:r><w:instrText xml:space="preserve">ADDIN ZOTERO_BIBL {"uncited":[]} CSL_BIBLIOGRAPHY</w:instrText>'
           '</w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>' +
           _r("Snowling, M. J. (2020). Dyslexia: A very short introduction. Oxford University Press.")),
        _p(_r("Shaywitz, S. (2003). Overcoming dyslexia. Knopf."),
           '<w:r><w:fldChar w:fldCharType="end"/></w:r>'),
        "<w:sectPr/>",
    ])
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document {NS}><w:body>{body}</w:body></w:document>')
    styles = ('<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="http://schemas.openxmlformats.org/'
              'wordprocessingml/2006/main">'
              '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
              '<w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style></w:styles>')
    numbering = ('<?xml version="1.0" encoding="UTF-8"?><w:numbering xmlns:w="http://schemas.openxmlformats.org/'
                 'wordprocessingml/2006/main">'
                 '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
                 '<w:lvlText w:val="%1."/></w:lvl><w:lvl w:ilvl="1"><w:start w:val="1"/>'
                 '<w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%2)"/></w:lvl></w:abstractNum>'
                 '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/>'
                 '<w:lvlText w:val="•"/></w:lvl></w:abstractNum>'
                 '<w:abstractNum w:abstractNumId="2"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
                 '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
                 '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
                 '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
                 '<w:num w:numId="3"><w:abstractNumId w:val="2"/></w:num></w:numbering>')
    footnotes = ('<?xml version="1.0" encoding="UTF-8"?><w:footnotes xmlns:w="http://schemas.openxmlformats.org/'
                 'wordprocessingml/2006/main">'
                 '<w:footnote w:type="separator" w:id="0"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
                 '<w:footnote w:id="2"><w:p><w:r><w:footnoteRef/></w:r>'
                 '<w:r><w:t xml:space="preserve"> Functional MRI measures blood flow.</w:t></w:r></w:p></w:footnote>'
                 '</w:footnotes>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    types = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
             'content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" '
             'ContentType="application/vnd.openxmlformats-package.relationships+xml"/></Types>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/numbering.xml", numbering)
        z.writestr("word/footnotes.xml", footnotes)


def main() -> None:
    paper = os.path.join(HERE, "paper.pdf")
    write_paper(paper)
    write_scan(os.path.join(HERE, "scan.pdf"))
    write_blank(os.path.join(HERE, "blank.pdf"))
    write_unmapped(os.path.join(HERE, "unmapped.pdf"))
    first = os.path.join(HERE, "_first_page.pdf")
    write_paper(first, pages=1)
    try:
        write_encrypted(first)
    finally:
        os.remove(first)
    write_handmade_docx(os.path.join(HERE, "handmade.docx"))
    print("fixtures written to", HERE)


if __name__ == "__main__":
    sys.exit(main())

"""Read a Word (.docx) file with the standard library: zipfile and ElementTree.

* The body is read in order, following the package relationship to the main part. Content controls, custom
  XML, smart tags and inserted text are read; deleted text, moved-from text, deleted table rows and hidden
  text are skipped.
* Headings (a style's outline level, or "heading N", "Title", "Subtitle", following basedOn) are their own
  paragraphs. List items get the label Word shows ("1.", "a)", "iii."); bullet glyphs are not read.
* Field instructions are never read, only their results; a table of contents or index is skipped.
  Citation-manager fields (Zotero, Mendeley, EndNote, Papers, Citavi, RefWorks, Word's own) are kept as exact
  ranges, and a bibliography field marks where the reference list starts.
* Footnote and endnote marks are dropped and their text becomes asides. Tables are read one row at a time
  ("Group; Accuracy; Speed."), repeating a vertically merged cell's label on each row. Text boxes follow the
  paragraph that holds them, once (Word stores a second, fallback copy).
* A part that unpacks to more than 64 MB, or any DTD or entity, is refused.

An older Word file (.doc), or an encrypted .docx, is an OLE container rather than a zip; those are reported as
"unsupported" (with ".doc") and "protected". Nothing here logs text, titles or paths.
"""

from __future__ import annotations

import os
import re
import zipfile
from xml.etree import ElementTree as ET

from .model import Aside, LoadedDocument, LoadError

MAX_XML_BYTES = 64 * 1024 * 1024    # refuse zip bombs: an uncompressed part larger than this
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _w(tag):
    return f"{{{W}}}{tag}"


_CITATION_FIELD_RE = re.compile(
    r"^\s*ADDIN\s+(?:ZOTERO_ITEM|CSL_CITATION|EN\.CITE|Mendeley\s+Citation|PAPERS2_CITATIONS|CITAVI\.PLACEHOLDER"
    r"|REFWORKS)|^\s*CITATION\s", re.I)
_BIBLIOGRAPHY_FIELD_RE = re.compile(
    r"^\s*(?:ADDIN\s+(?:ZOTERO_BIBL|EN\.REFLIST|Mendeley\s+Bibliography|CSL_BIBLIOGRAPHY|CITAVI\.BIBLIOGRAPHY)"
    r"|BIBLIOGRAPHY\b)", re.I)
_SKIP_FIELD_RE = re.compile(r"^\s*(?:TOC|INDEX|XE|TC)\b", re.I)  # a table of contents / index is not prose

_ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
          (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]


def _roman(n):
    out = ""
    for v, s in _ROMAN:
        while n >= v:
            out, n = out + s, n - v
    return out


def _letters(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return s


def _format_number(fmt, n):
    if fmt in ("decimal", "decimalZero", "ordinal", "cardinalText", "ordinalText", "decimalEnclosedCircle"):
        return str(n)
    if fmt == "lowerLetter":
        return _letters(n)
    if fmt == "upperLetter":
        return _letters(n).upper()
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    return None  # bullet, none, ...


def _read_part(z, name):
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML_BYTES:  # a zip bomb, or not a reading document
        raise LoadError("too_large")
    data = z.read(info)
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:65536]:
        raise LoadError("corrupt", "dtd")  # Word never writes these: no entity tricks
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise LoadError("corrupt", "xml") from None


class _Styles:
    def __init__(self, root):
        self.heading_level = {}  # styleId -> outline level (0 = Heading 1 / Title)
        self.numbering = {}      # styleId -> (numId, ilvl) of a list style
        self.based_on = {}
        if root is None:
            return
        for st in root.iter(_w("style")):
            sid = st.get(_w("styleId"))
            name_el = st.find(_w("name"))
            name = (name_el.get(_w("val")) if name_el is not None else "") or ""
            lvl = st.find(f"{_w('pPr')}/{_w('outlineLvl')}")
            base = st.find(_w("basedOn"))
            if base is not None:
                self.based_on[sid] = base.get(_w("val"))
            low = name.lower()
            if lvl is not None and lvl.get(_w("val")) not in (None, "9"):
                self.heading_level[sid] = int(lvl.get(_w("val")))
            elif low.startswith("heading ") and low[8:].isdigit():
                self.heading_level[sid] = int(low[8:]) - 1
            elif low in ("title", "subtitle"):
                self.heading_level[sid] = 0
            num = st.find(f"{_w('pPr')}/{_w('numPr')}")
            if num is not None:  # a list style ("List Number"): its paragraphs are numbered without their own numPr
                nid = num.find(_w("numId"))
                il = num.find(_w("ilvl"))
                if nid is not None:
                    self.numbering[sid] = (nid.get(_w("val")), int(il.get(_w("val"), "0")) if il is not None else 0)

    def level(self, sid):
        return self._inherited(self.heading_level, sid)

    def list_number(self, sid):
        """(numId, ilvl) a paragraph style numbers its paragraphs with, or None."""
        return self._inherited(self.numbering, sid)

    def _inherited(self, table, sid):
        seen = set()
        while sid and sid not in seen:
            if sid in table:
                return table[sid]
            seen.add(sid)
            sid = self.based_on.get(sid)
        return None


class _Numbering:
    def __init__(self, root):
        self.num_to_abs = {}
        self.overrides = {}  # (numId, ilvl) -> start override
        self.levels = {}     # (absId, ilvl) -> (fmt, lvlText, start)
        self.counters = {}   # numId -> list of counts per level
        if root is None:
            return
        for an in root.iter(_w("abstractNum")):
            aid = an.get(_w("abstractNumId"))
            for lvl in an.iter(_w("lvl")):
                il = int(lvl.get(_w("ilvl"), "0"))
                fmt = lvl.find(_w("numFmt"))
                txt = lvl.find(_w("lvlText"))
                start = lvl.find(_w("start"))
                self.levels[(aid, il)] = ((fmt.get(_w("val")) if fmt is not None else "decimal"),
                                          (txt.get(_w("val")) if txt is not None else "%1."),
                                          int(start.get(_w("val"))) if start is not None else 1)
        for num in root.iter(_w("num")):
            nid = num.get(_w("numId"))
            an = num.find(_w("abstractNumId"))
            if an is not None:
                self.num_to_abs[nid] = an.get(_w("val"))
            for ov in num.iter(_w("lvlOverride")):
                so = ov.find(_w("startOverride"))
                if so is not None:
                    self.overrides[(nid, int(ov.get(_w("ilvl"), "0")))] = int(so.get(_w("val")))

    def label(self, nid, ilvl):
        """The list label Word would show ("2.", "b)", "iii."), or "" for bullets."""
        aid = self.num_to_abs.get(nid)
        if aid is None or nid == "0":
            return ""
        counts = self.counters.setdefault(nid, [0] * 9)
        fmt, text, start = self.levels.get((aid, ilvl), ("decimal", f"%{ilvl + 1}.", 1))
        start = self.overrides.get((nid, ilvl), start)
        counts[ilvl] = (counts[ilvl] + 1) if counts[ilvl] else start
        for deeper in range(ilvl + 1, 9):
            counts[deeper] = 0
        if _format_number(fmt, 1) is None:
            return ""  # a bullet: the bullet glyph is not read

        def sub(m):
            k = int(m.group(1)) - 1
            f, _, st = self.levels.get((aid, k), ("decimal", "", 1))
            n = counts[k] or self.overrides.get((nid, k), st)
            return _format_number(f, n) or ""

        return re.sub(r"%(\d)", sub, text).strip()


class _DocxWalker:
    def __init__(self, styles, numbering, footnote_ids_seen):
        self.styles = styles
        self.numbering = numbering
        self.paras: list[tuple[str, list[tuple[int, int]], bool]] = []  # (text, citation spans, heading)
        self.footnote_refs = footnote_ids_seen
        self.references_para = None
        # field state, a stack because fields nest
        self.fields: list[dict] = []
        self.cur: list[str] = []
        self.cur_cites: list[tuple[int, int]] = []
        self.pending_boxes: list[list] = []

    # -- helpers
    def _len(self):
        return sum(len(s) for s in self.cur)

    def _emit(self, s):
        if any(f["state"] == "code" for f in self.fields):
            return  # inside a field code (between begin and separate)
        if any(f.get("skip") for f in self.fields):
            return
        self.cur.append(s)

    def _start_field(self):
        self.fields.append({"state": "code", "instr": [], "start": None})

    def _separate_field(self):
        if not self.fields:
            return
        f = self.fields[-1]
        f["state"] = "result"
        instr = "".join(f["instr"])
        f["kind"] = ("cite" if _CITATION_FIELD_RE.match(instr) else "bib" if _BIBLIOGRAPHY_FIELD_RE.match(instr)
                     else "skip" if _SKIP_FIELD_RE.match(instr) else "")
        f["skip"] = f["kind"] == "skip"
        f["start"] = self._len()
        if f["kind"] == "bib" and self.references_para is None:
            self.references_para = len(self.paras)

    def _end_field(self):
        if not self.fields:
            return
        f = self.fields.pop()
        if f.get("kind") == "cite" and f["start"] is not None:
            self.cur_cites.append((f["start"], self._len()))

    def flush(self, heading=False, label=""):
        text = "".join(self.cur)
        spans = list(self.cur_cites)
        if label and text.strip():
            pre = label + " "
            text = pre + text
            spans = [(a + len(pre), b + len(pre)) for a, b in spans]
        # collapse whitespace but keep span offsets right: do it with a running map
        out, mapping = [], []
        prev_space = True
        for ch in text:
            if ch in " \t ":
                if prev_space:
                    mapping.append(len(out))
                    continue
                ch = " "
                prev_space = True
            else:
                prev_space = False
            mapping.append(len(out))
            out.append(ch)
        mapping.append(len(out))
        clean = "".join(out)
        stripped = clean.rstrip()
        spans = [(mapping[a], min(mapping[b], len(stripped))) for a, b in spans if mapping[a] < mapping[b]]
        if stripped:
            self.paras.append((stripped, spans, heading))
        self.cur, self.cur_cites = [], []
        for box in self.pending_boxes:
            self.paras.extend(box)
        self.pending_boxes = []

    # -- walking
    def block(self, el):
        """A block-level container: body, cell, text box content, sdtContent, ..."""
        for child in el:
            tag = child.tag
            if tag == _w("p"):
                self.paragraph(child)
            elif tag == _w("tbl"):
                self.table(child)
            elif tag in (_w("sdt"),):
                content = child.find(_w("sdtContent"))
                if content is not None:
                    self.block(content)
            elif tag in (_w("customXml"), _w("sdtContent"), _w("ins"), _w("moveTo"), _w("smartTag")):
                self.block(child)
            elif tag == f"{{{MC}}}AlternateContent":
                self._alternate(child, self.block)
            # w:del / w:moveFrom / w:sectPr / bookmarks: skipped

    def _alternate(self, el, fn):
        part = el.find(f"{{{MC}}}Choice")
        if part is None:
            part = el.find(f"{{{MC}}}Fallback")
        if part is not None:
            fn(part)

    def paragraph(self, p):
        ppr = p.find(_w("pPr"))
        heading = False
        label = ""
        if ppr is not None:
            st = ppr.find(_w("pStyle"))
            sid = st.get(_w("val")) if st is not None else None
            lvl = self.styles.level(sid)
            ol = ppr.find(_w("outlineLvl"))
            if ol is not None and ol.get(_w("val")) != "9":
                lvl = int(ol.get(_w("val")))
            heading = lvl is not None
            num = ppr.find(_w("numPr"))
            styled = self.styles.list_number(sid)
            if num is not None:
                nid = num.find(_w("numId"))
                il = num.find(_w("ilvl"))
                label = self.numbering.label(nid.get(_w("val")) if nid is not None else (styled or ("0",))[0],
                                             int(il.get(_w("val"))) if il is not None else 0)
            elif styled is not None:
                label = self.numbering.label(*styled)
        self.inline(p)
        self.flush(heading, label)

    def inline(self, el):
        for child in el:
            tag = child.tag
            if tag == _w("r"):
                self.run(child)
            elif tag in (_w("hyperlink"), _w("ins"), _w("moveTo"), _w("smartTag"), _w("customXml"),
                         _w("fldSimple"), _w("bdo"), _w("dir")):
                if tag == _w("fldSimple"):
                    instr = child.get(_w("instr"), "")
                    if _SKIP_FIELD_RE.match(instr):
                        continue
                    start = self._len()
                    self.inline(child)
                    if _CITATION_FIELD_RE.match(instr):
                        self.cur_cites.append((start, self._len()))
                    continue
                self.inline(child)
            elif tag == _w("sdt"):
                content = child.find(_w("sdtContent"))
                if content is not None:
                    self.inline(content)
            elif tag == f"{{{MC}}}AlternateContent":
                self._alternate(child, self.inline)
            elif tag == f"{{{M}}}oMathPara" or tag == f"{{{M}}}oMath":
                self._emit(" " + "".join(t.text or "" for t in child.iter(f"{{{M}}}t")) + " ")
            # w:del, w:moveFrom, w:proofErr, w:bookmarkStart, w:commentRangeStart ...: nothing to read

    def run(self, r):
        rpr = r.find(_w("rPr"))
        hidden = rpr is not None and (rpr.find(_w("vanish")) is not None or rpr.find(_w("specVanish")) is not None)
        for child in r:
            tag = child.tag
            if tag == _w("fldChar"):
                kind = child.get(_w("fldCharType"))
                if kind == "begin":
                    self._start_field()
                elif kind == "separate":
                    self._separate_field()
                elif kind == "end":
                    self._end_field()
            elif tag == _w("instrText"):
                if self.fields:
                    self.fields[-1]["instr"].append(child.text or "")
            elif hidden:
                continue
            elif tag == _w("t"):
                self._emit(child.text or "")
            elif tag == _w("tab") or tag == _w("ptab"):
                self._emit(" ")
            elif tag in (_w("br"), _w("cr")):
                self._emit(" " if child.get(_w("type")) not in ("page",) else " ")
            elif tag == _w("noBreakHyphen"):
                self._emit("-")
            elif tag == _w("softHyphen"):
                pass
            elif tag == _w("sym"):
                ch = child.get(_w("char"), "")
                try:
                    code = int(ch, 16)
                    if code >= 0xF000:
                        code -= 0xF000  # Symbol-font private range
                    if 0x20 <= code < 0xD800:
                        self._emit(chr(code))
                except ValueError:
                    pass
            elif tag in (_w("footnoteReference"), _w("endnoteReference")):
                self.footnote_refs.append(child.get(_w("id")))
            elif tag == _w("drawing") or tag == _w("pict") or tag == f"{{{MC}}}AlternateContent":
                self._boxes(child)
            # w:commentReference, w:delText, w:lastRenderedPageBreak, ...: nothing

    def _boxes(self, el):
        """Text boxes inside a drawing: read after the paragraph, as their own paragraphs."""
        if el.tag == f"{{{MC}}}AlternateContent":
            choice = el.find(f"{{{MC}}}Choice")
            el = choice if choice is not None else el.find(f"{{{MC}}}Fallback")
            if el is None:
                return
        for content in el.iter(_w("txbxContent")):
            sub = _DocxWalker(self.styles, self.numbering, self.footnote_refs)
            sub.block(content)
            self.pending_boxes.append(sub.paras)
            break  # one copy is enough (VML fallbacks repeat the same box)

    def table(self, tbl):
        rows = [c for c in tbl if c.tag == _w("tr")]
        for wrapper in tbl:  # rows wrapped in a content control or custom XML
            if wrapper.tag in (_w("sdt"), _w("customXml")):
                rows.extend(wrapper.iter(_w("tr")))
        merged = {}  # grid column -> text of the vertically merged cell that started there
        for tr in rows:
            if tr.find(f"{_w('trPr')}/{_w('del')}") is not None:
                continue
            cells = []
            col = 0
            for tc in tr.findall(_w("tc")):
                sub = _DocxWalker(self.styles, self.numbering, self.footnote_refs)
                sub.block(tc)
                txt = " ".join(t for t, _, _ in sub.paras).strip()
                span_el = tc.find(f"{_w('tcPr')}/{_w('gridSpan')}")
                span = int(span_el.get(_w("val"), "1")) if span_el is not None else 1
                vm = tc.find(f"{_w('tcPr')}/{_w('vMerge')}")
                if vm is not None and vm.get(_w("val")) == "restart":
                    merged[col] = txt
                elif vm is not None and not txt:
                    txt = merged.get(col, "")  # merged cell: repeat its label, a listener needs it per row
                else:
                    merged.pop(col, None)
                col += span
                if txt:
                    cells.append(txt.rstrip(".;"))
            if cells:
                self.paras.append(("; ".join(cells) + ".", [], False))

    def walk_body(self, body):
        self.block(body)


def _footnotes(z, name):
    root = _read_part(z, name)
    out = {}
    if root is None:
        return out
    tag = "footnote" if "foot" in name else "endnote"
    for fn in root.iter(_w(tag)):
        if fn.get(_w("type")) in ("separator", "continuationSeparator", "continuationNotice"):
            continue
        walker = _DocxWalker(_Styles(None), _Numbering(None), [])
        walker.block(fn)
        txt = " ".join(t for t, _, _ in walker.paras).strip()
        if txt:
            out[fn.get(_w("id"))] = txt
    return out


def _ole_kind(path: str) -> LoadError:
    """An OLE file named .docx: an encrypted Word file, or an older one (.doc) with a new name."""
    try:
        with open(path, "rb") as f:
            head = f.read(1 << 20)
    except OSError as e:
        return LoadError("io", type(e).__name__)
    if _ENCRYPTED_PACKAGE in head:
        return LoadError("protected", ".docx")
    return LoadError("unsupported", ".doc")


def read_docx(path: str) -> LoadedDocument:
    """The document's body, its footnotes as asides, its citation fields and its reference list.

    Raises LoadError: "unsupported" (an older .doc file), "protected" (an encrypted file), "too_large",
    "corrupt" or "io".
    """
    try:
        with open(path, "rb") as f:
            magic = f.read(8)
        if magic == OLE_MAGIC:
            raise _ole_kind(path)
        with zipfile.ZipFile(path) as z:
            main = "word/document.xml"
            rels = _read_part(z, "_rels/.rels")
            if rels is not None:  # the main part can be renamed; follow the package relationship
                for rel in rels.iter(f"{{{PKG_REL}}}Relationship"):
                    if rel.get("Type", "").endswith("/officeDocument"):
                        main = rel.get("Target", main).lstrip("/")
            doc = _read_part(z, main)
            if doc is None:
                raise LoadError("corrupt", "main")
            styles = _Styles(_read_part(z, "word/styles.xml"))
            numbering = _Numbering(_read_part(z, "word/numbering.xml"))
            refs: list[str] = []
            walker = _DocxWalker(styles, numbering, refs)
            body = doc.find(_w("body"))
            walker.walk_body(body if body is not None else doc)
            notes = {**_footnotes(z, "word/footnotes.xml"), **_footnotes(z, "word/endnotes.xml")}
    except LoadError:
        raise
    except zipfile.BadZipFile:
        raise LoadError("corrupt", "zip") from None
    except OSError as e:
        raise LoadError("io", type(e).__name__) from None
    except (ValueError, KeyError, RuntimeError, NotImplementedError, RecursionError, EOFError) as e:
        raise LoadError("corrupt", type(e).__name__) from None

    parts, spans, pos = [], [], 0
    references_at = None
    for k, (t, cites, _heading) in enumerate(walker.paras):
        if k == walker.references_para:
            references_at = pos
        parts.append(t)
        spans.extend((pos + a, pos + b) for a, b in cites)
        pos += len(t) + 2
    rp = walker.references_para
    if references_at is not None and rp and walker.paras[rp - 1][2]:
        references_at -= len(walker.paras[rp - 1][0]) + 2  # start at the "References" heading
    text = "\n\n".join(parts)
    if references_at is None:
        from ..academic_text import references_start
        references_at = references_start(text)
    asides = [Aside(notes[i], "footnote", None) for i in dict.fromkeys(refs) if i in notes]
    return LoadedDocument("docx", os.path.splitext(os.path.basename(path))[0], text, references_at, asides,
                          citation_spans=spans)

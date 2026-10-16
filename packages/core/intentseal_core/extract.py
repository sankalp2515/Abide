"""CORE-2 Content extraction: "see what the model sees".

Turns raw content from any channel into a list of Segments: every piece of text a model could end up
reading, tagged with where it came from and whether a human would see it. Non-visible text (hidden HTML,
white or tiny PDF text, hidden Word runs, HTML-only email parts, comments, metadata, alt text...) is not
evidence of an attack by itself, but it is where hidden instructions live, so it is surfaced and flagged.
"""

from __future__ import annotations

import email
import email.policy
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

FORMATS = ("text", "markdown", "html", "pdf", "docx", "email", "json", "code", "image")


@dataclass
class Segment:
    text: str
    origin: str                 # e.g. "html:body", "html:hidden-style", "pdf:invisible", "docx:hidden-run"
    visible: bool = True
    start: int = 0              # offsets in ExtractedDoc.text
    end: int = 0


@dataclass
class ExtractedDoc:
    format: str
    segments: list[Segment] = field(default_factory=list)
    text: str = ""              # all segments joined; spans refer to this
    warnings: list[str] = field(default_factory=list)

    @property
    def hidden_segments(self) -> list[Segment]:
        return [s for s in self.segments if not s.visible]

    def finalise(self) -> "ExtractedDoc":
        parts, pos = [], 0
        for s in self.segments:
            s.start, s.end = pos, pos + len(s.text)
            parts.append(s.text)
            pos = s.end + 1
        self.text = "\n".join(parts)
        return self


def _short_metadata(value: object) -> bool:
    """Titles and author names are ordinary (shown in viewers); long metadata text is unusual, so flag it."""
    return value is not None and len(str(value).split()) <= 12


_WORD = re.compile(r"[^\W\d_]{2,}")


def _page_furniture(value: object) -> bool:
    """Short non-visible web-page text that every real page has: alt text, tooltips, ARIA labels, technical <meta>
    values, build comments, collapsed menu items. Measured on 14 real sites (8 Oct): every page had some, so treating
    it as hidden text quarantined every page (D-051). It stays in the document view and is scored like visible text
    (Tier 1, and the judge when the policy asks); only longer hidden prose is flagged structurally."""
    return value is not None and len(_WORD.findall(str(value))) <= 12


def _add(doc: ExtractedDoc, text: str | None, origin: str, visible: bool = True) -> None:
    if text is None:
        return
    text = str(text).strip()
    if text:
        doc.segments.append(Segment(text, origin, visible))


def sniff_format(data: bytes | str, filename: str | None = None, source: str | None = None) -> str:
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    by_ext = {"pdf": "pdf", "docx": "docx", "eml": "email", "html": "html", "htm": "html", "md": "markdown",
              "markdown": "markdown", "json": "json", "png": "image", "jpg": "image", "jpeg": "image",
              "py": "code", "js": "code", "ts": "code", "java": "code", "go": "code", "rb": "code", "c": "code",
              "cpp": "code", "cs": "code", "sh": "code", "txt": "text"}
    if ext in by_ext:
        return by_ext[ext]
    if isinstance(data, bytes):
        if data[:4] == b"%PDF":
            return "pdf"
        if data[:4] == b"PK\x03\x04":
            return "docx"
        if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
            return "image"
        data = data.decode("utf-8", errors="replace")
    head = data.lstrip()[:500].lower()
    if head.startswith(("{", "[")):
        try:
            json.loads(data)
            return "json"
        except ValueError:
            pass
    if "<html" in head or "<body" in head or re.search(r"<(div|p|span|a|table)\b", head):
        return "html"
    if re.match(r"^(from|to|subject|received|return-path|mime-version|date|reply-to|message-id|content-type):", head):
        return "email"
    # A bare body sent with source="email" (no RFC 822 headers) is plain text, not a message to parse.
    return "text"


def extract(data: bytes | str, fmt: str | None = None, filename: str | None = None,
            source: str | None = None) -> ExtractedDoc:
    fmt = fmt or sniff_format(data, filename, source)
    fn = {"pdf": _pdf, "docx": _docx, "html": _html, "email": _email, "json": _json, "markdown": _markdown,
          "code": _code, "image": _image}.get(fmt, _text)
    doc = ExtractedDoc(fmt)
    try:
        fn(data, doc)
    except Exception as e:  # never fail open silently: fall back to raw text and flag it
        doc.segments = []
        if fmt == "image":   # binary pixels are not text: return nothing and let the policy hold it
            doc.warnings.append(f"image extraction failed ({type(e).__name__}); no text extracted")
        else:
            doc.warnings.append(f"{fmt} extraction failed ({type(e).__name__}); raw text used")
            _text(data, doc)
    if not doc.segments and fmt not in ("image", "pdf", "docx"):
        # Never hand the tiers an empty view of non-empty text: inspect the raw input instead.
        raw = _as_text(data)
        if raw.strip():
            _add(doc, raw, f"{fmt}:raw-fallback")
    return doc.finalise()


def _as_text(data: bytes | str) -> str:
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data


def _as_bytes(data: bytes | str) -> bytes:
    return data if isinstance(data, bytes) else data.encode("utf-8")


# ---- plain text / markdown / code / json ------------------------------------------------------
def _text(data: bytes | str, doc: ExtractedDoc) -> None:
    _add(doc, _as_text(data), "text:body")


_MD_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")
_MD_LINK_TITLE = re.compile(r"\[[^\]]*\]\([^)\s]+\s+\"([^\"]*)\"\)")
_MD_REFDEF = re.compile(r"^\s*\[[^\]]+\]:\s*\S+(?:\s+[\"'(](.*?)[\"')])?\s*$", re.M)


def _markdown(data: bytes | str, doc: ExtractedDoc) -> None:
    text = _as_text(data)
    for m in _MD_COMMENT.finditer(text):
        _add(doc, m.group(1), "markdown:html-comment", visible=False)
    for m in _MD_IMAGE.finditer(text):
        _add(doc, m.group(1), "markdown:image-alt", visible=False)
        _add(doc, m.group(3), "markdown:image-title", visible=False)
        _add(doc, m.group(2), "markdown:image-url", visible=False)
    for m in _MD_LINK_TITLE.finditer(text):
        _add(doc, m.group(1), "markdown:link-title", visible=False)
    for m in _MD_REFDEF.finditer(text):
        _add(doc, m.group(1), "markdown:ref-definition", visible=False)
    visible = _MD_COMMENT.sub("", text)
    if "<" in visible and re.search(r"<\w+[^>]*>", visible):
        sub = ExtractedDoc("html")
        _html(visible, sub)
        doc.segments.extend(Segment(s.text, "markdown+" + s.origin, s.visible) for s in sub.segments)
    else:
        _add(doc, visible, "markdown:body")







def _code(data: bytes | str, doc: ExtractedDoc) -> None:
    """Comments and docstrings are visible to anyone reading the file, so they stay in the source segment
    (and are scored with it); they are not separate non-visible segments (D-020)."""
    _add(doc, _as_text(data), "code:source")


def _json(data: bytes | str, doc: ExtractedDoc) -> None:
    obj = json.loads(_as_text(data))

    def walk(o: Any, path: str) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
        elif isinstance(o, str) and o.strip():
            _add(doc, o, f"json:{path}")

    walk(obj, "")


# ---- HTML -------------------------------------------------------------------------------------
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|font-size\s*:\s*0|"
    r"(?:^|;)\s*color\s*:\s*(?:#fff(?:fff)?|white|transparent)\b|left\s*:\s*-\d{3,}px|"
    r"height\s*:\s*0(?:px)?\s*;.*overflow\s*:\s*hidden|clip\s*:\s*rect\(0", re.I)


def _html(data: bytes | str, doc: ExtractedDoc) -> None:
    from bs4 import BeautifulSoup, Comment

    soup = BeautifulSoup(_as_text(data), "html.parser")
    # Non-visible text is surfaced either way; short page furniture is ordinary text, longer hidden prose is flagged.
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        _add(doc, str(c), "html:comment", visible=_page_furniture(c))
        c.extract()
    if soup.title:
        _add(doc, soup.title.get_text(), "html:title")
    for m in soup.find_all("meta"):
        _add(doc, m.get("content"), f"html:meta:{m.get('name') or m.get('property') or 'meta'}",
             visible=_page_furniture(m.get("content")))
    for tag in soup.find_all(["script", "style", "template", "noscript"]):
        if tag.name in ("template", "noscript"):
            text = tag.get_text(" ")
            _add(doc, text, f"html:{tag.name}", visible=_page_furniture(text))
        elif tag.name == "script" and tag.get("type", "").lower() in ("application/ld+json", "application/json"):
            text = tag.get_text(" ")
            _add(doc, text, "html:script-data", visible=_page_furniture(text))
        tag.decompose()
    hidden = [t for t in soup.find_all(True)
              if t.has_attr("hidden") or t.get("aria-hidden") == "true"
              or _HIDDEN_STYLE.search(t.get("style", "") or "")]
    hidden_ids = {id(t) for t in hidden}
    outermost = [t for t in hidden if not any(id(p) in hidden_ids for p in t.parents)]
    for t in outermost:
        text = t.get_text(" ")
        _add(doc, text, "html:hidden-element", visible=_page_furniture(text))
        t.decompose()
    for t in soup.find_all(True):
        for attr in ("alt", "title", "aria-label", "placeholder", "data-prompt", "data-instructions"):
            if t.has_attr(attr):
                # attributes named for a model are never furniture
                ordinary = attr not in ("data-prompt", "data-instructions") and _page_furniture(t.get(attr))
                _add(doc, t.get(attr), f"html:attr:{attr}", visible=ordinary)
    body = soup.body or soup
    _add(doc, re.sub(r"\n\s*\n+", "\n", body.get_text("\n")), "html:body")


# ---- PDF --------------------------------------------------------------------------------------
def _is_white(color: Any) -> bool:
    if color is None:
        return False
    if isinstance(color, (int, float)):
        return color >= 0.95
    vals = list(color) if isinstance(color, (list, tuple)) else []
    if len(vals) == 1:
        return vals[0] >= 0.95
    if len(vals) == 3:
        return all(v >= 0.95 for v in vals)
    if len(vals) == 4:  # CMYK
        return all(v <= 0.05 for v in vals)
    return False


def _pdf(data: bytes | str, doc: ExtractedDoc) -> None:
    import pdfplumber
    from pypdf import PdfReader

    raw = _as_bytes(data)
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            vis_chars, hid_chars = [], []
            w, h = page.width, page.height
            for ch in page.chars:
                off_page = ch["x1"] < 0 or ch["x0"] > w or ch["bottom"] < 0 or ch["top"] > h
                tiny = (ch.get("size") or 10) < 2.5
                white = _is_white(ch.get("non_stroking_color"))
                (hid_chars if (off_page or tiny or white) else vis_chars).append(ch)
            _add(doc, _chars_to_text(vis_chars), f"pdf:page{pno}")
            _add(doc, _chars_to_text(hid_chars), f"pdf:page{pno}:invisible", visible=False)
    reader = PdfReader(io.BytesIO(raw))
    meta = reader.metadata or {}
    for k, v in meta.items():
        if k not in ("/Producer", "/CreationDate", "/ModDate", "/Creator"):
            _add(doc, v, f"pdf:metadata:{k.strip('/')}", visible=_short_metadata(v))
    for pno, page in enumerate(reader.pages, 1):
        for a in page.get("/Annots") or []:
            try:
                obj = a.get_object()
                _add(doc, obj.get("/Contents"), f"pdf:page{pno}:annotation", visible=False)
            except Exception:
                continue


def _chars_to_text(chars: list[dict]) -> str:
    if not chars:
        return ""
    chars = sorted(chars, key=lambda c: (round(c["top"]), c["x0"]))
    lines, cur, last_top, last_x1 = [], [], None, None
    for c in chars:
        if last_top is not None and abs(c["top"] - last_top) > 2:
            lines.append("".join(cur))
            cur, last_x1 = [], None
        if last_x1 is not None and c["x0"] - last_x1 > max(1.0, (c.get("size") or 10) * 0.2):
            cur.append(" ")
        cur.append(c["text"])
        last_top, last_x1 = c["top"], c["x1"]
    lines.append("".join(cur))
    return "\n".join(lines)


# ---- DOCX -------------------------------------------------------------------------------------
def _docx(data: bytes | str, doc: ExtractedDoc) -> None:
    import zipfile

    import docx
    from docx.shared import RGBColor

    raw = _as_bytes(data)
    d = docx.Document(io.BytesIO(raw))

    def run_hidden(run) -> bool:
        f = run.font
        if f.hidden:
            return True
        if f.size is not None and f.size.pt < 2.5:
            return True
        try:
            return f.color is not None and f.color.rgb in (RGBColor(0xFF, 0xFF, 0xFF),)
        except (AttributeError, ValueError):
            return False

    def paragraphs():
        yield from d.paragraphs
        for t in d.tables:
            for row in t.rows:
                for cell in row.cells:
                    yield from cell.paragraphs
        for s in d.sections:
            for part in (s.header, s.footer):
                yield from part.paragraphs

    for p in paragraphs():
        vis = "".join(r.text for r in p.runs if not run_hidden(r))
        hid = "".join(r.text for r in p.runs if run_hidden(r))
        _add(doc, vis or (p.text if not p.runs else ""), "docx:body")
        _add(doc, hid, "docx:hidden-run", visible=False)
    cp = d.core_properties
    for k in ("title", "subject", "keywords", "comments", "author", "category"):
        val = getattr(cp, k, None)
        _add(doc, val, f"docx:property:{k}", visible=_short_metadata(val))
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if "word/comments.xml" in z.namelist():
            from bs4 import BeautifulSoup
            xml = BeautifulSoup(z.read("word/comments.xml"), "html.parser")
            for c in xml.find_all("w:comment"):
                _add(doc, c.get_text(" "), "docx:comment", visible=False)


# ---- Email ------------------------------------------------------------------------------------
def _email(data: bytes | str, doc: ExtractedDoc) -> None:
    m = email.message_from_bytes(_as_bytes(data), policy=email.policy.default)
    for h in ("From", "Reply-To", "Subject"):
        _add(doc, m.get(h), f"email:header:{h.lower()}")
    for h, v in m.items():
        if h.lower().startswith("x-"):
            _add(doc, v, f"email:header:{h.lower()}", visible=False)
    plain = m.get_body(preferencelist=("plain",))
    html = m.get_body(preferencelist=("html",))
    plain_text = plain.get_content() if plain else ""
    _add(doc, plain_text, "email:text-part")
    if html:
        sub = ExtractedDoc("html")
        _html(html.get_content(), sub)
        norm_plain = re.sub(r"\s+", " ", plain_text).lower()
        for s in sub.segments:
            in_plain = re.sub(r"\s+", " ", s.text).lower() in norm_plain
            if not s.visible:
                doc.segments.append(Segment(s.text, "email:html:" + s.origin, False))
            elif not plain or not in_plain:
                # visible in the HTML view but missing from the plain-text view (or vice versa)
                # Shown to HTML mail readers; genuinely hidden HTML elements are caught above as non-visible.
                doc.segments.append(Segment(s.text, "email:html-only", visible=True))
    for part in m.iter_attachments():
        name = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True) or b""
        sub = extract(payload, filename=name)
        doc.segments.extend(Segment(s.text, f"email:attachment:{name}:" + s.origin, s.visible) for s in sub.segments)


# ---- Images -----------------------------------------------------------------------------------
_OCR = None


def _image(data: bytes | str, doc: ExtractedDoc) -> None:
    global _OCR
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        doc.warnings.append("OCR unavailable (install rapidocr_onnxruntime)")
        return
    if _OCR is None:
        _OCR = RapidOCR()
    res, _ = _OCR(_as_bytes(data))
    for box, text, score in res or []:
        _add(doc, text, "image:ocr")

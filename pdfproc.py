"""PyMuPDF helpers: render slides to PNG, extract text + titles, crop regions, read context PDFs.

These are blocking functions; call them via asyncio.to_thread from the server.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pymupdf

import storage

LG_WIDTH = 1600   # image sent to the model + main view
SM_WIDTH = 320    # thumbnails
REGION_LONG_EDGE = 1568  # Claude's vision input is downscaled beyond ~1568 px anyway


def _render(page: pymupdf.Page, width: int, clip: pymupdf.Rect | None = None) -> bytes:
    src = clip or page.rect
    zoom = width / src.width
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    return pix.tobytes("png")


def _title(page: pymupdf.Page, text: str) -> str:
    """Largest-font text in the top 40% of the slide; fallback: first non-empty line."""
    try:
        d = page.get_text("dict")
        limit = page.rect.y0 + page.rect.height * 0.4
        spans = []
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = span.get("text", "").strip()
                    if t and span["bbox"][1] <= limit:
                        spans.append((round(span["size"], 1), span["bbox"][1], span["bbox"][0], t))
        if spans:
            top = max(s[0] for s in spans)
            parts = [s for s in spans if s[0] >= top - 0.6]
            parts.sort(key=lambda s: (round(s[1]), s[2]))
            title = " ".join(p[3] for p in parts)
            if len(title) >= 2:
                return " ".join(title.split())[:120]
    except Exception:
        pass
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def _compact(text: str) -> str:
    """Layout-sorted text has big runs of spaces/blank lines; shrink them (keeps modest indentation for code)."""
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        stripped = line.lstrip(" ")
        indent = min(len(line) - len(stripped), 8)
        lines.append(" " * indent + re.sub(r" {4,}", "   ", stripped))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def process_deck(lid: str) -> list[dict]:
    """Render every slide (skipping PNGs that already exist) and write text.json. Returns the text list."""
    doc = pymupdf.open(storage.deck_path(lid))
    slides = []
    try:
        for i, page in enumerate(doc):
            n = i + 1
            for size, width in (("lg", LG_WIDTH), ("sm", SM_WIDTH)):
                p = storage.slide_png(lid, n, size)
                if not p.exists():
                    storage.atomic_write_bytes(p, _render(page, width))
            text = _compact(page.get_text("text", sort=True))
            entry = {"n": n, "title": _title(page, text) or f"Slide {n}", "text": text}
            if len(text.split()) < SPARSE_WORDS and ocr_available():
                ocr = _ocr(page)
                if ocr and len(ocr[1].split()) > len(text.split()):
                    entry.update(text=ocr[1], ocr=True)
                    if ocr[0] and _title_is_weak(page, entry["title"], n):
                        entry["title"] = ocr[0]
            slides.append(entry)
    finally:
        doc.close()
    storage.write_json(storage.lecture_dir(lid) / "text.json", slides)
    return slides


# ---------------------------------------------------------------- OCR fallback for image-only slides

SPARSE_WORDS = 15  # fewer words than this in the text layer -> slide is (mostly) an image
_ocr_ok: bool | None = None


def ocr_available() -> bool:
    """Tesseract installed (brew install tesseract) and found by PyMuPDF."""
    global _ocr_ok
    if _ocr_ok is None:
        try:
            _ocr_ok = bool(shutil.which("tesseract")) and bool(pymupdf.get_tessdata())
        except Exception:
            _ocr_ok = False
    return _ocr_ok


def _ocr(page: pymupdf.Page) -> tuple[str, str] | None:
    """(title, text) recognised by Tesseract, or None on failure."""
    try:
        tp = page.get_textpage_ocr(language="eng", dpi=200, full=False)  # OCR images only, keep real text
        text = _compact(page.get_text("text", textpage=tp, sort=True))
        return _ocr_title(page, tp), text
    except Exception:
        return None


def _title_is_weak(page: pymupdf.Page, title: str, n: int) -> bool:
    """The text-layer title is only a fallback or a corner tag (e.g. "Quick Recap!" top-right)."""
    if not title or title == f"Slide {n}":
        return True
    first = title.split()[0]
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip().startswith(first):
                    return span["bbox"][0] > 0.6 * page.rect.width
    return True


def _ocr_title(page: pymupdf.Page, tp) -> str:
    """OCR span sizes are noisy (logos/shapes read as huge glyphs), so: the biggest line in the title band
    (7–35% from the top), left of corner tags, with at least two real words."""
    W, H = page.rect.width, page.rect.height
    best = None
    for block in page.get_text("dict", textpage=tp).get("blocks", []):
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            x0 = min(s["bbox"][0] for s in spans)
            y0 = min(s["bbox"][1] for s in spans)
            text = " ".join(s["text"].strip() for s in spans)
            words = [w for w in text.split() if sum(c.isalpha() for c in w) >= 2]
            if not (0.07 * H <= y0 <= 0.35 * H) or x0 > 0.6 * W or len(words) < 2:
                continue
            key = (round(max(s["size"] for s in spans)), -y0)
            if best is None or key > best[0]:
                best = (key, " ".join(text.split()))
    return best[1][:120] if best else ""


def page_count(pdf: Path) -> int:
    with pymupdf.open(pdf) as doc:
        return doc.page_count


def render_region(lid: str, n: int, rect: list[float]) -> bytes:
    """Crop of slide n given as fractions [x0,y0,x1,y1] of the page, rendered sharply from the vector PDF."""
    x0, y0, x1, y1 = (min(max(float(v), 0.0), 1.0) for v in rect)
    if x1 - x0 < 0.005 or y1 - y0 < 0.005:
        raise ValueError("region too small")
    with pymupdf.open(storage.deck_path(lid)) as doc:
        page = doc[n - 1]
        r = page.rect
        clip = pymupdf.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height,
                            r.x0 + x1 * r.width, r.y0 + y1 * r.height)
        long_edge = max(clip.width, clip.height)
        zoom = min(8.0, REGION_LONG_EDGE / long_edge)
        width = int(clip.width * zoom)
        return _render(page, max(width, 1), clip=clip)


def extract_pdf_text(pdf: Path, ocr: bool = False) -> str:
    """Plain text of a whole PDF with page markers (context files, past exams). With ocr=True, pages without a
    usable text layer (scans) are run through Tesseract when it is installed."""
    parts = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc):
            t = _compact(page.get_text("text", sort=True))
            if ocr and len(t) < 40 and ocr_available():
                try:
                    tp = page.get_textpage_ocr(language="eng", dpi=200, full=True)
                    t = _compact(page.get_text("text", textpage=tp, sort=True)) or t
                except Exception:
                    pass
            if t:
                parts.append(f"[page {i + 1}]\n{t}")
    return "\n\n".join(parts)

"""PDF validation, page extraction (pdfplumber), OCR for scanned pages (pypdfium2 + Tesseract) and
removal of repeated headers and footers.

Each page becomes a list of blocks in reading order: headings, paragraphs (lines) and tables (rows joined
with " | "). Font size and weight are used to recognise headings; OCR pages fall back to numbering patterns.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import pdfplumber
import pypdfium2 as pdfium
import pytesseract
from pypdf import PdfReader
from pypdf.generic import DictionaryObject

SCANNED_PAGE_MIN_CHARS = 25
_PAGE_NUMBER = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$", re.I)
_NUMBERED_HEADING = re.compile(r"^(section \d+|part [a-z]|\d{1,2}\.)\s+\S", re.I)
_CID = re.compile(r"\(cid:\d+\)")  # glyphs without a Unicode mapping, e.g. some bullet characters


class InvalidPdf(Exception):
    """The upload is not an acceptable PDF. ``code`` is shown to the user."""

    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


# --- Upload validation --------------------------------------------------------------------------------


def save_upload(src: BinaryIO, dest: Path, max_bytes: int) -> tuple[int, str]:
    """Stream an upload to disk, enforcing the size limit. Returns (size, sha256)."""
    sha, size = hashlib.sha256(), 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        while block := src.read(1024 * 1024):
            size += len(block)
            if size > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise InvalidPdf("file_too_large", f"The file is larger than {max_bytes // 2**20} MB.", 413)
            sha.update(block)
            out.write(block)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise InvalidPdf("empty_file", "The file is empty.")
    return size, sha.hexdigest()


def validate_pdf(path: Path, max_pages: int) -> int:
    """Reject non-PDFs, encrypted or oversized files and PDFs with active content. Returns the page count."""
    with path.open("rb") as f:
        if b"%PDF-" not in f.read(1024):
            raise InvalidPdf("not_a_pdf", "The file is not a PDF.", 415)
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise InvalidPdf("encrypted_pdf", "Password-protected PDFs are not supported.")
        pages = len(reader.pages)
        root = reader.trailer["/Root"].get_object()
    except InvalidPdf:
        raise
    except Exception as exc:
        raise InvalidPdf("malformed_pdf", "The PDF could not be read.") from exc
    if pages == 0:
        raise InvalidPdf("empty_pdf", "The PDF has no pages.")
    if pages > max_pages:
        raise InvalidPdf("too_many_pages", f"The PDF has {pages} pages; the limit is {max_pages}.")
    names = root.get("/Names")
    names = names.get_object() if names is not None else None
    if (
        "/OpenAction" in root
        or "/AA" in root
        or (isinstance(names, DictionaryObject) and ("/JavaScript" in names or "/EmbeddedFiles" in names))
    ):
        raise InvalidPdf(
            "active_content", "PDFs with scripts, automatic actions or attachments are not accepted."
        )
    return pages


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


# --- Extraction ---------------------------------------------------------------------------------------


@dataclass
class Block:
    kind: str  # heading1 | heading2 | text | table
    text: str
    top: float = 0.0


@dataclass
class PageText:
    number: int
    blocks: list[Block] = field(default_factory=list)
    ocr: bool = False

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


@dataclass
class _Line:
    text: str
    top: float
    size: float
    bold: bool


def _line(d: dict) -> _Line:
    chars = [c for c in d.get("chars", []) if str(c.get("text", "")).strip()]
    sizes = [round(float(c["size"]), 1) for c in chars] or [0.0]
    bold = sum("bold" in str(c.get("fontname", "")).lower() for c in chars) >= 0.6 * max(len(chars), 1)
    text = re.sub(r"\s+", " ", _CID.sub("", d["text"])).strip()
    return _Line(text, float(d["top"]), statistics.median(sizes), bold)


def _inside(obj: dict, box: tuple[float, float, float, float]) -> bool:
    x0, top, x1, bottom = box
    return (
        obj["x0"] >= x0 - 1 and obj["x1"] <= x1 + 1 and obj["top"] >= top - 1 and obj["bottom"] <= bottom + 1
    )


def extract_pages(path: Path, ocr_language: str = "eng", ocr_dpi: int = 300) -> list[PageText]:
    pages: list[PageText] = []
    lines_by_page: dict[int, list[_Line]] = {}
    tables_by_page: dict[int, list[Block]] = {}
    scanned: list[int] = []
    with pdfplumber.open(str(path)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            if sum(1 for c in page.chars if c["text"].strip()) < SCANNED_PAGE_MIN_CHARS:
                scanned.append(number)
                continue
            tables, boxes = [], []
            for t in page.find_tables():
                rows = [[re.sub(r"\s+", " ", c or "").strip() for c in row] for row in t.extract()]
                rows = [r for r in rows if any(r)]
                if len(rows) >= 2:
                    tables.append(Block("table", "\n".join(" | ".join(r) for r in rows), float(t.bbox[1])))
                    boxes.append(t.bbox)
            view = page.filter(lambda o, b=boxes: not any(_inside(o, x) for x in b)) if boxes else page
            lines_by_page[number] = [
                ln for ln in map(_line, view.extract_text_lines(return_chars=True)) if ln.text
            ]
            tables_by_page[number] = tables
            page.flush_cache()

    _drop_headers_and_footers(lines_by_page)
    sizes = [ln.size for lines in lines_by_page.values() for ln in lines]
    body_size = Counter(sizes).most_common(1)[0][0] if sizes else 10.0

    for number in sorted([*lines_by_page, *scanned]):
        if number in scanned:
            pages.append(_ocr_page(path, number, ocr_language, ocr_dpi))
            continue
        blocks: list[Block] = []
        prev: _Line | None = None
        for ln in lines_by_page[number]:
            if ln.size >= body_size + 3:
                blocks.append(Block("heading1", ln.text, ln.top))
            elif ln.size >= body_size + 0.5 and ln.bold and len(ln.text) < 120:
                blocks.append(Block("heading2", ln.text, ln.top))
            elif (
                prev is not None
                and blocks
                and blocks[-1].kind == "text"
                and ln.top - prev.top < body_size * 1.6  # wrapped line, not a new paragraph
                and not ln.text.startswith("•")
            ):
                blocks[-1].text += " " + ln.text
            else:
                blocks.append(Block("text", ln.text, ln.top))
            prev = ln
        blocks = sorted([*blocks, *tables_by_page[number]], key=lambda b: b.top)
        pages.append(PageText(number, blocks))
    return pages


def _drop_headers_and_footers(lines_by_page: dict[int, list[_Line]]) -> None:
    """Remove page numbers and lines repeated in the top/bottom two lines of at least half the pages."""

    def key(text: str) -> str:
        return re.sub(r"\bpage\s+\d+(\s+of\s+\d+)?\b", "", text.lower()).strip()

    edges = {n: lines[:2] + lines[-2:] for n, lines in lines_by_page.items()}
    counts = Counter(k for lines in edges.values() for k in {key(ln.text) for ln in lines})
    threshold = max(2, len(lines_by_page) // 2)
    repeated = {k for k, c in counts.items() if c >= threshold} if len(lines_by_page) > 1 else set()
    for n, lines in lines_by_page.items():
        edge_ids = {id(ln) for ln in edges[n]}
        lines_by_page[n] = [
            ln
            for ln in lines
            if id(ln) not in edge_ids or not (key(ln.text) in repeated or _PAGE_NUMBER.match(ln.text))
        ]


def _ocr_page(path: Path, number: int, language: str, dpi: int) -> PageText:
    pdf = pdfium.PdfDocument(str(path))
    try:
        image = pdf[number - 1].render(scale=dpi / 72).to_pil()
    finally:
        pdf.close()
    text = pytesseract.image_to_string(image, lang=language, timeout=120)
    # Tesseract separates paragraphs with blank lines; join the wrapped lines inside each paragraph.
    lines = [re.sub(r"\s+", " ", para).strip() for para in re.split(r"\n\s*\n", text)]
    lines = [ln for ln in lines if ln and not _PAGE_NUMBER.match(ln)]
    # A single scanned page has no repetition to detect running headers and footers from, so drop a
    # "Company | Title" style first line and any such trailing lines (they carry page numbers or references).
    if lines and "|" in lines[0]:
        lines = lines[1:]
    while lines and ("|" in lines[-1] or re.search(r"\bpage \d+\b", lines[-1], re.I)):
        lines = lines[:-1]
    blocks = [
        Block("heading2" if _NUMBERED_HEADING.match(ln) and len(ln) < 80 else "text", ln, float(i))
        for i, ln in enumerate(lines)
    ]
    return PageText(number, blocks, ocr=True)

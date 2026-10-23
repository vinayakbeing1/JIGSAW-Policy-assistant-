"""Render the sample policy documents (Markdown + YAML front matter) to PDF.

    docker compose run --rm tests python sample_data/build_pdfs.py

Writes sample_data/pdfs/<key>.pdf and sample_data/manifest.json. Documents with ``scanned: true`` are
rasterised into image-only PDFs so the OCR path is exercised. Output is reproducible (fixed PDF ids and
timestamps), so checksums only change when the content changes.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import pypdfium2 as pdfium
import yaml
from PIL import ImageFilter
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

rl_config.invariant = 1

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "documents"
OUT = ROOT / "pdfs"

BRAND = colors.HexColor("#0b5d4f")
BODY = ParagraphStyle("Body", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=6)
BULLET = ParagraphStyle("Bullet", parent=BODY, leftIndent=6 * mm, firstLineIndent=-4 * mm, spaceAfter=3)
H1 = ParagraphStyle(
    "H1", parent=BODY, fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=12, spaceAfter=6,
    textColor=BRAND,
)  # fmt: skip
H2 = ParagraphStyle("H2", parent=BODY, fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=6)
TITLE = ParagraphStyle(
    "Title", parent=BODY, fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=BRAND
)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=9, leading=11, spaceAfter=0)


def parse(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    return yaml.safe_load(front), body.strip()


def inline(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def table(rows: list[list[str]]) -> Table:
    data = [[Paragraph(f"<b>{inline(c)}</b>", CELL) for c in rows[0]]]
    data += [[Paragraph(inline(c), CELL) for c in row] for row in rows[1:]]
    width = A4[0] - 36 * mm
    first = 0.45 if len(rows[0]) == 2 else 0.34
    rest = (1 - first) / (len(rows[0]) - 1)
    t = Table(data, colWidths=[width * first] + [width * rest] * (len(rows[0]) - 1), repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#8a9a96")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e3efec")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return t


def flowables(meta: dict, body: str) -> list:
    out: list = [
        Paragraph(inline(meta["title"]), TITLE),
        Paragraph(
            inline(
                f"Version {meta['version']} ({meta['region']}) - effective from {meta['effective_from']:%d %B %Y}"
                f" - reference {meta['reference']}"
            ),
            BODY,
        ),
        Spacer(1, 4 * mm),
    ]
    rows: list[list[str]] = []
    for line in [*body.splitlines(), ""]:
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(set(c) <= {"-", ":"} for c in cells):
                rows.append(cells)
            continue
        if rows:
            out += [table(rows), Spacer(1, 3 * mm)]
            rows = []
        if not line.strip():
            continue
        if line.startswith("# "):
            out.append(Paragraph(inline(line[2:]), H1))
        elif line.startswith("## "):
            out.append(Paragraph(inline(line[3:]), H2))
        elif line.startswith("- "):
            out.append(Paragraph("•&nbsp;&nbsp;" + inline(line[2:]), BULLET))
        else:
            out.append(Paragraph(inline(line), BODY))
    # Keep each heading on the same page as the block that follows it.
    merged: list = []
    i = 0
    while i < len(out):
        if getattr(out[i], "style", None) in (H1, H2) and i + 1 < len(out):
            merged.append(KeepTogether(out[i : i + 2]))
            i += 2
        else:
            merged.append(out[i])
            i += 1
    return merged


def render(meta: dict, body: str) -> bytes:
    buf = io.BytesIO()
    header = f"Jigsaw Insurance  |  {meta['title']}"
    footer = f"Jigsaw Insurance Limited  |  {meta['reference']}"

    def decorate(canvas, doc):  # type: ignore[no-untyped-def]
        w, h = A4
        canvas.saveState()
        canvas.setFillColor(BRAND)
        canvas.rect(0, h - 9 * mm, w, 9 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.drawString(18 * mm, h - 6 * mm, header)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.setFont("Helvetica", 8)
        canvas.drawString(18 * mm, 10 * mm, footer)
        canvas.drawRightString(w - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=meta["title"], author="Jigsaw Insurance Limited", subject=meta["reference"],
    )  # fmt: skip
    doc.build(flowables(meta, body), onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()


def rasterise(pdf_bytes: bytes, dpi: int = 200) -> bytes:
    """Turn a text PDF into an image-only 'scan' (slightly blurred and skewed)."""
    src = pdfium.PdfDocument(pdf_bytes)
    out = io.BytesIO()
    c = rl_canvas.Canvas(out, pagesize=A4, invariant=1)
    try:
        for page in src:
            img = page.render(scale=dpi / 72).to_pil().convert("L").filter(ImageFilter.GaussianBlur(0.4))
            img = img.rotate(0.3, fillcolor=255)
            c.drawImage(ImageReader(img), 0, 0, width=A4[0], height=A4[1])
            c.showPage()
    finally:
        src.close()
    c.save()
    return out.getvalue()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    products = {p["code"]: p for p in yaml.safe_load((ROOT / "products.yaml").read_text(encoding="utf-8"))}
    manifest = []
    for path in sorted(SOURCES.glob("*.md")):
        meta, body = parse(path)
        data = render(meta, body)
        if meta.get("scanned"):
            data = rasterise(data)
        filename = f"{meta['key']}.pdf"
        (OUT / filename).write_bytes(data)
        product = products[meta["product"]]
        manifest.append(
            {
                "key": meta["key"],
                "product_code": product["code"],
                "product_name": product["name"],
                "line_of_business": product["line_of_business"],
                "title": meta["title"],
                "version": str(meta["version"]),
                "region": meta["region"],
                "effective_from": meta["effective_from"].isoformat(),
                "effective_to": meta["effective_to"].isoformat() if meta.get("effective_to") else None,
                "kind": meta["kind"],
                "applies_to": meta.get("applies_to"),
                "allowed_roles": meta["roles"],
                "reference": meta["reference"],
                "filename": filename,
                "scanned": bool(meta.get("scanned")),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        print(f"{filename:42} {len(data) // 1024:>5} KB{'  (scanned)' if meta.get('scanned') else ''}")
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"manifest.json: {len(manifest)} documents")


if __name__ == "__main__":
    main()

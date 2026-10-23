import io
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.config import PROJECT_ROOT
from app.ingestion.chunking import build_chunks
from app.ingestion.pdf import InvalidPdf, extract_pages, save_upload, validate_pdf

PDFS = PROJECT_ROOT / "sample_data" / "pdfs"


def test_wording_is_extracted_without_headers_and_footers():
    pages = extract_pages(PDFS / "home-2026-uk.pdf")
    text = "\n".join(p.text for p in pages)
    assert "Page 2" not in text and "JIG-HOME-PW-2026.1-UK" not in text.split("\n", 2)[2]
    assert "Escape of water | £350" in text  # table rows keep their cells together
    headings = [b.text for p in pages for b in p.blocks if b.kind == "heading1"]
    assert "Section 1 - Buildings" in headings and "Excesses" in headings


def test_chunks_follow_sections_and_record_pages():
    pages = extract_pages(PDFS / "home-2026-uk.pdf")
    chunks = build_chunks(
        pages, chunk_size=1200, chunk_overlap=150, title="Jigsaw Home Insurance Policy Wording"
    )
    assert not any(c.section.startswith("Jigsaw Home Insurance Policy Wording") for c in chunks)
    excess = next(c for c in chunks if c.section == "Excesses")
    assert "Escape of water | £350" in excess.text
    page_text = next(p.text for p in pages if p.number == excess.page_start)
    assert "Escape of water | £350" in page_text
    assert all(len(c.text) <= 1200 for c in chunks)
    assert all(c.page_start <= c.page_end for c in chunks)


def test_scanned_endorsement_is_read_with_ocr():
    [page] = extract_pages(PDFS / "motor-2026-uk-m07.pdf")
    assert page.ocr
    assert "additional excess of £1,000" in page.text
    assert any(b.kind == "heading2" and "Night-time driving" in b.text for b in page.blocks)


def test_upload_size_limit(tmp_path):
    with pytest.raises(InvalidPdf) as exc:
        save_upload(io.BytesIO(b"%PDF-" + b"x" * 2000), tmp_path / "f.pdf", max_bytes=1000)
    assert exc.value.code == "file_too_large"
    assert not (tmp_path / "f.pdf").exists()


def _pdf(tmp_path: Path, writer: PdfWriter) -> Path:
    path = tmp_path / "test.pdf"
    with path.open("wb") as f:
        writer.write(f)
    return path


def test_rejects_non_pdf(tmp_path):
    path = tmp_path / "x.pdf"
    path.write_text("not a pdf")
    with pytest.raises(InvalidPdf) as exc:
        validate_pdf(path, max_pages=10)
    assert exc.value.code == "not_a_pdf"


@pytest.mark.filterwarnings("ignore:add_js is deprecated")
def test_rejects_javascript(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.add_js("app.alert('hi');")
    with pytest.raises(InvalidPdf) as exc:
        validate_pdf(_pdf(tmp_path, writer), max_pages=10)
    assert exc.value.code == "active_content"


def test_rejects_password_protected(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.encrypt("secret")
    with pytest.raises(InvalidPdf) as exc:
        validate_pdf(_pdf(tmp_path, writer), max_pages=10)
    assert exc.value.code == "encrypted_pdf"


def test_page_limit(tmp_path):
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(200, 200)
    with pytest.raises(InvalidPdf) as exc:
        validate_pdf(_pdf(tmp_path, writer), max_pages=2)
    assert exc.value.code == "too_many_pages"

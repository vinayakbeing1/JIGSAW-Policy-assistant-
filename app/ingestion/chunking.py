"""Section-aware chunking.

Text is grouped by the policy's own headings ("Section 1 - Buildings > What is not covered"), then each
section is split with LangChain's RecursiveCharacterTextSplitter, which prefers paragraph and line breaks.
Chunks never cross a section boundary, and every chunk records the pages it came from for citations.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.ingestion.pdf import PageText


@dataclass
class ChunkDraft:
    section: str
    page_start: int
    page_end: int
    text: str


@dataclass
class _Section:
    path: str
    parts: list[tuple[int, str]]  # (page number, block text)


def _sections(pages: list[PageText], title: str) -> list[_Section]:
    sections: list[_Section] = []
    h1 = h2 = ""
    for page in pages:
        for block in page.blocks:
            if block.kind == "heading1":
                # The document title printed on the first page is not a section of its own.
                h1 = "" if block.text.casefold() == title.casefold() else block.text
                h2 = ""
                continue
            if block.kind == "heading2":
                h2 = block.text
                continue
            path = " > ".join(h for h in (h1, h2) if h)
            if not sections or sections[-1].path != path:
                sections.append(_Section(path, []))
            sections[-1].parts.append((page.number, block.text))
    return sections


def build_chunks(
    pages: list[PageText], chunk_size: int, chunk_overlap: int, title: str = ""
) -> list[ChunkDraft]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, add_start_index=True
    )
    chunks: list[ChunkDraft] = []
    for section in _sections(pages, title):
        text, starts, page_of = "", [], []
        for page, part in section.parts:
            if text:
                text += "\n\n"
            starts.append(len(text))
            page_of.append(page)
            text += part
        for doc in splitter.create_documents([text]):
            start = doc.metadata["start_index"]
            end = start + len(doc.page_content) - 1
            chunks.append(
                ChunkDraft(
                    section=section.path,
                    page_start=page_of[bisect.bisect_right(starts, start) - 1],
                    page_end=page_of[bisect.bisect_right(starts, end) - 1],
                    text=doc.page_content,
                )
            )
    return chunks

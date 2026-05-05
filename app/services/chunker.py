"""
PDF chunking for the project's reference documents — produces
embedding-ready text fragments with structural metadata.

Two chunking strategies live here, picked per document type:

  • CLASS-SHEET strategy  — for the master Piping Material Spec
    (40801-SPE-...-0001), which contains 91 single-page class data
    sheets after a short body. We chunk one-class-per-chunk so a query
    like "A25 SDSS notes" retrieves the entire data sheet for that
    class, not a fragment of it.

    The class data sheets render their class code in a graphical header
    that pypdf can't read — page-text extraction on a class sheet does
    NOT include the class code. To map class → page reliably, we parse
    the document's own "PMS Index" (Appendix 2, pages 23-26) which
    lists every class with its Sheet No. That index IS readable as
    text, so we use it as the source of truth and ignore any class
    detection on the data-sheet pages themselves.

  • SECTION strategy — for ASME / API / NACE standards. Chunks are
    cut at section/paragraph boundaries (§304.1, §345.4.2, etc.) so
    a query like "wall thickness equation" lands on the actual
    paragraph rather than fragmenting tables across chunks.

Both strategies preserve metadata (doc, page, section, class_code)
so retrieval results can be cited back to the source.

This module deliberately has NO external API dependencies — it works
on local PDFs only. The embedding step happens elsewhere (see
embedding_service.py and rag_service.py).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader

logger = logging.getLogger(__name__)

# Tunable: maximum target chunk length (in characters, NOT tokens — we
# trade a small approximation for not having a tokenizer dependency in
# the chunker itself). Most embedding models accept ~8K tokens; we cap
# well under that so a single chunk never gets truncated.
_MAX_CHUNK_CHARS = 6000
_MIN_CHUNK_CHARS = 100      # below this, skip — likely whitespace artefacts


@dataclass
class Chunk:
    """A single embedding-ready text fragment with provenance metadata.

    Fields are kept flat (no nested dicts) so the SQL insert is simple
    and the metadata fields can each be indexed independently.
    """
    doc_name: str           # 'PMS' | 'VMS' | 'B31.3' | 'B16.5' | …
    doc_revision: str       # source revision string (e.g. 'A0', '2020')
    page_number: int        # 1-based; 0 if the chunk spans pages
    section: str            # '§5.5' | 'A25 data sheet' | 'Appx-1' …
    class_code: str = ""    # populated for class-sheet chunks ('A1', 'T80A')
    text: str = ""          # the actual chunk content
    extra: dict = field(default_factory=dict)


# ── Master PMS document chunker ─────────────────────────────────────

# The master PMS PDF (40801-SPE-...-0001) follows a fixed layout:
#   pages 1-22  : spec body (objective, scope, references, §5 requirements)
#   pages 23-26 : Appendix 2 — PMS Index (class → sheet number table)
#   pages 27-117: 91 class data sheets, one per page
# We parse the index to build a (class_code → page_number) map, then
# extract each class's data sheet as one chunk.

# Each row in the index begins with a 1-2 digit serial number followed
# by the class code (e.g. "1 A1 150#", "32 A2LN 150#"). The class code
# is followed by the rating, ASME group, material, etc., and ends with
# the sheet number on its own line break. We pull (class_code, sheet_no)
# pairs by anchoring on the row start.
#
# Pattern explanation:
#   ^\s*\d{1,3}\s+              — line starts with serial number
#   ([A-KT]\d{1,2}[A-Z]{0,3})   — class code (e.g. A1, A1LN, T80A)
#   \s+\S+#                      — rating like "150#" or "1500#" (or "EEMUA"
#                                  for the CuNi class — handled separately)
#   .*?                          — class metadata (lazy)
#   (\d{2,3})\s*$                — sheet number at end of the row's text
_INDEX_ROW_RE = re.compile(
    r"^\s*\d{1,3}\s+"
    r"([A-KT]\d{1,2}[A-Z]{0,3})\s+"
    r"(?:\d+\#|EEMUA[^\n]*?)"
    r"[\s\S]*?"
    r"\s(\d{2,3})\s*$",
    re.MULTILINE,
)


def _parse_pms_index(reader: PdfReader, index_pages: Iterable[int]) -> dict[str, int]:
    """Read the index pages and return {class_code: sheet_number} for
    every row found. `index_pages` is 1-based.

    Each row of the index is "spread" across multiple physical lines in
    the extracted text (PDF tables don't extract linearly), so we join
    and re-split aggressively. The strategy: walk the text line-by-line,
    detect the start of a new index row (numeric serial + class code),
    accumulate everything until the next row, and grab the trailing
    sheet number from the row's text.
    """
    mapping: dict[str, int] = {}
    full_text = ""
    for p in index_pages:
        full_text += "\n" + (reader.pages[p - 1].extract_text() or "")

    # Walk line-by-line, building one logical row at a time.
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]
    row_buf: list[str] = []
    row_start_re = re.compile(r"^\d{1,3}\s+([A-KT]\d{1,2}[A-Z]{0,3})\s+")

    # Pattern that anchors on the row's own structure: each index row
    # ends with a "@ <max-temp>°C <sheet-no>" tail. Pulling the sheet
    # number off that anchor — instead of "last 2-3 digit token in the
    # joined buffer" — keeps page-break headers like "MY-K-20-PI-SP-0001"
    # from injecting a stray "20" that shadows the real sheet number.
    sheet_tail_re = re.compile(
        r"@\s*\-?\d+\s*°?C\s+(\d{2,3})\b"
    )

    def _flush(buf: list[str]):
        """Process one accumulated row buffer; extract (class, page).

        Page-break headers ("Appendix 2 - PIPING MATERIAL SPECIFICATION
        INDEX / MY-K-20-PI-SP-0001 / 40801-SPE-…") get appended to a
        row's buffer when an index row sits at the bottom of one page
        and the next row is on the following page. We tolerate that by
        anchoring sheet-number extraction on the @-temp-°C pattern that
        ONLY appears in real row content, not in the page header.
        """
        if not buf:
            return
        joined = " ".join(buf)
        m_start = row_start_re.match(joined)
        if not m_start:
            return
        cls = m_start.group(1).upper()
        # Primary path: anchor on the @ <temp>°C <sheet> pattern. Take
        # the LAST occurrence so even malformed rows that contain two
        # @-temp markers (rare but seen in the GRE rows) yield the
        # correct trailing sheet number.
        tails = sheet_tail_re.findall(joined)
        if tails:
            sheet = int(tails[-1])
        else:
            # Fallback — only triggers on truly malformed rows. Use
            # 2-3 digit tokens but exclude any that came from a
            # page-header artefact ("MY-K-20-...", "40801-...").
            cleaned = re.sub(
                r"(?:Appendix\s*\d.*?$|MY-K-\d.*?$|\d{4,}-SPE-.*?$|Rev\.\s*:\s*\S+\s*$)",
                " ", joined, flags=re.MULTILINE,
            )
            all_nums = [int(n) for n in re.findall(r"\b(\d{2,3})\b", cleaned)]
            if not all_nums:
                return
            sheet = all_nums[-1]
        # Sanity: sheet numbers in this PDF are 27-117. Reject anything
        # outside that range — defends against any remaining edge case.
        if 25 <= sheet <= 130:
            mapping[cls] = sheet

    for line in lines:
        if row_start_re.match(line):
            _flush(row_buf)
            row_buf = [line]
        else:
            row_buf.append(line)
    _flush(row_buf)

    return mapping


def chunk_master_pms(
    pdf_path: str | Path,
    doc_name: str = "PMS",
    doc_revision: str = "A0",
) -> list[Chunk]:
    """Chunk the master Piping Material Specification PDF.

    Returns one Chunk per class data sheet (the entire sheet text in one
    chunk so a class query retrieves coherent context) plus one Chunk
    per spec-body section. The class index pages are NOT chunked — they
    only serve to map class → page and aren't useful for retrieval.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"Master PMS PDF not found at {path}")

    reader = PdfReader(str(path))
    n_pages = len(reader.pages)
    logger.info("Chunking %s (%d pages)", path.name, n_pages)

    # Step 1 — parse the index pages to map class → sheet page.
    # Layout for this document: pages 23-26 inclusive carry the index.
    # If a future revision shifts these, override via the function arg.
    index_map = _parse_pms_index(reader, range(23, 27))
    logger.info(
        "Parsed PMS index: %d classes mapped to data-sheet pages",
        len(index_map),
    )

    chunks: list[Chunk] = []

    # Step 2 — body chunk (pages 1-22). The body is short enough to be
    # one chunk; if it ever grows past _MAX_CHUNK_CHARS we'll split per
    # section. Skip pages 1-2 (cover + revision) as they carry no
    # retrieval-worthy content.
    body_pages: list[str] = []
    for p in range(3, 23):
        text = (reader.pages[p - 1].extract_text() or "").strip()
        if text:
            body_pages.append(text)
    if body_pages:
        full_body = _normalize_whitespace("\n\n".join(body_pages))
        # Split on top-level section headers ("1.0 OBJECTIVE", "5.5 …")
        # so retrieval can land on §5.5 (naming philosophy) directly
        # rather than dragging the whole body in.
        body_chunks = _split_body_into_sections(
            full_body, doc_name, doc_revision,
        )
        chunks.extend(body_chunks)

    # Step 3 — one chunk per class data sheet
    for cls, page in sorted(index_map.items(), key=lambda x: x[1]):
        if page < 1 or page > n_pages:
            logger.warning("Class %s maps to page %d which is out of range", cls, page)
            continue
        text = (reader.pages[page - 1].extract_text() or "").strip()
        if len(text) < _MIN_CHUNK_CHARS:
            logger.warning(
                "Class %s page %d extracted only %d chars; skipping",
                cls, page, len(text),
            )
            continue
        # Augment with the class code so retrieval matches even though
        # pypdf can't pull it from the data sheet's graphical header.
        # Format: leading "Class: <CODE>" line so embedding sees the
        # class identity, then the data-sheet body.
        augmented = f"Class: {cls}\n{_normalize_whitespace(text)}"
        chunks.append(Chunk(
            doc_name=doc_name,
            doc_revision=doc_revision,
            page_number=page,
            section=f"{cls} data sheet",
            class_code=cls,
            text=augmented[:_MAX_CHUNK_CHARS],
        ))

    logger.info(
        "Chunked %s into %d chunks (%d class sheets, %d body sections)",
        path.name, len(chunks),
        sum(1 for c in chunks if c.class_code),
        sum(1 for c in chunks if not c.class_code),
    )
    return chunks


# Section-header pattern for the spec body. The body uses "<num>.<num>...
# TITLE" headers (e.g. "5.5 Piping Material Specification Naming
# Philosophy"). We split on these so retrieval can isolate §5.5 from
# the rest of the body. Section IDs are required to be ≤ 9 (top-level
# numbering doesn't go higher in this doc); higher numbers in the body
# would always be either pressure values or branch-table row labels.
_BODY_SECTION_RE = re.compile(
    r"(?:^|\n)\s*([1-9](?:\.\d+){0,2})\s+([A-Z][A-Za-z][A-Za-z0-9 ,/\-&\.()]{3,80})",
    re.MULTILINE,
)


def _split_body_into_sections(
    body_text: str,
    doc_name: str,
    doc_revision: str,
) -> list[Chunk]:
    """Split the body text into per-section chunks. Falls back to one
    big chunk if no section markers are found.

    A few false-positive section markers slip through the regex even
    after the [1-9] prefix tightening — TOC dot-leader lines like
    "5.2 Material Identification ......" (which look like a header but
    are really an index entry), and branch-table row fragments that
    start with a number. We filter those out here:

      • Reject chunks whose body is mostly dot-leader runs ('....').
      • Reject chunks under _MIN_CHUNK_CHARS (already excluded but
        kept for safety).
      • Reject chunks whose body matches "single letter pattern" of
        branch-table rows (e.g. "30 W W W W T T T T").
    """
    matches = list(_BODY_SECTION_RE.finditer(body_text))
    if not matches:
        return [Chunk(
            doc_name=doc_name, doc_revision=doc_revision,
            page_number=3, section="Body",
            text=body_text[:_MAX_CHUNK_CHARS],
        )]

    chunks: list[Chunk] = []
    for i, m in enumerate(matches):
        section_id = m.group(1)
        section_title = m.group(2).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body_text)
        text = body_text[start:end].strip()

        # Filters: drop noise that the regex picked up incorrectly.
        if len(text) < _MIN_CHUNK_CHARS:
            continue
        if text.count(".") > len(text) * 0.15:
            # TOC dot-leader line ("....."): mostly dots, drop.
            continue
        # Branch-table row residue ("30 W W W T T T T"): consists almost
        # entirely of single-letter tokens after the leading number.
        toks = text.split()
        single_letter_ratio = sum(1 for t in toks if len(t) == 1) / max(len(toks), 1)
        if single_letter_ratio > 0.5:
            continue

        chunks.append(Chunk(
            doc_name=doc_name,
            doc_revision=doc_revision,
            page_number=0,    # body sections span pages — 0 means "see section"
            section=f"§{section_id} {section_title}",
            text=text[:_MAX_CHUNK_CHARS],
        ))
    return chunks


# ── ASME / API / NACE standards chunker ────────────────────────────

# Standards have section markers of the form "1.0 ", "2.0 ", "§304.1.2", etc.
# We split per top-level section then sub-chunk if any individual section
# exceeds _MAX_CHUNK_CHARS. The section identifier is preserved as
# Chunk.section so retrieval results can cite the actual paragraph.

_STANDARD_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(§?\s*\d+(?:\.\d+){0,3})\s+([A-Z][A-Za-z0-9 ,/\-&\.()]{2,100})",
    re.MULTILINE,
)


def chunk_standard(
    pdf_path: str | Path,
    doc_name: str,
    doc_revision: str,
) -> list[Chunk]:
    """Chunk an ASME / API / NACE standards PDF by section header.

    Heuristic-based — works well on well-structured standards (B31.3,
    B16.5) and degrades gracefully on poorly-OCR'd ones (each page
    becomes one chunk).
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"Standard PDF not found at {path}")

    reader = PdfReader(str(path))
    n_pages = len(reader.pages)
    logger.info("Chunking standard %s (%d pages)", path.name, n_pages)

    # First pass — concatenate all text with page-marker comments so we
    # can attribute each chunk back to a page later.
    page_texts: list[tuple[int, str]] = []
    for i in range(n_pages):
        txt = (reader.pages[i].extract_text() or "").strip()
        if txt:
            page_texts.append((i + 1, txt))
    full = "\n\n".join(t for _, t in page_texts)

    matches = list(_STANDARD_SECTION_RE.finditer(full))
    if not matches:
        # Total fallback — one chunk per page
        return [
            Chunk(
                doc_name=doc_name, doc_revision=doc_revision,
                page_number=p, section=f"page {p}",
                text=_normalize_whitespace(t)[:_MAX_CHUNK_CHARS],
            )
            for p, t in page_texts
            if len(t) >= _MIN_CHUNK_CHARS
        ]

    chunks: list[Chunk] = []
    for i, m in enumerate(matches):
        section_id = m.group(1).strip()
        section_title = m.group(2).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full)
        text = full[start:end].strip()
        if len(text) < _MIN_CHUNK_CHARS:
            continue
        # Estimate page by counting page break markers up to this offset
        # (best-effort — exact paginate-aware extraction would need
        # PDF-layout-aware parsing, which is out of scope).
        page = _estimate_page(start, page_texts)
        section_label = f"§{section_id}" if not section_id.startswith("§") else section_id
        chunk = Chunk(
            doc_name=doc_name,
            doc_revision=doc_revision,
            page_number=page,
            section=f"{section_label} {section_title}",
            text=_normalize_whitespace(text),
        )
        chunks.extend(_split_long(chunk))

    logger.info("Chunked %s into %d chunks", path.name, len(chunks))
    return chunks


def _estimate_page(offset: int, page_texts: list[tuple[int, str]]) -> int:
    """Return the page number whose text contains the byte at `offset`
    in the concatenated full text. Approximate — close enough for
    retrieval-citation purposes."""
    cumulative = 0
    for p, t in page_texts:
        cumulative += len(t) + 2  # +2 for the "\n\n" join
        if offset <= cumulative:
            return p
    return page_texts[-1][0] if page_texts else 0


def _split_long(chunk: Chunk) -> Iterable[Chunk]:
    """If a chunk exceeds _MAX_CHUNK_CHARS, split it on paragraph breaks
    so each piece fits the embedding limit. Each piece keeps the same
    section label so retrieval can still group them."""
    if len(chunk.text) <= _MAX_CHUNK_CHARS:
        if len(chunk.text) >= _MIN_CHUNK_CHARS:
            yield chunk
        return
    parts = chunk.text.split("\n\n")
    buf: list[str] = []
    buf_len = 0
    part_idx = 1
    for p in parts:
        if buf_len + len(p) + 2 > _MAX_CHUNK_CHARS and buf:
            yield Chunk(
                doc_name=chunk.doc_name,
                doc_revision=chunk.doc_revision,
                page_number=chunk.page_number,
                section=f"{chunk.section} (part {part_idx})",
                class_code=chunk.class_code,
                text="\n\n".join(buf),
            )
            buf = []
            buf_len = 0
            part_idx += 1
        buf.append(p)
        buf_len += len(p) + 2
    if buf:
        yield Chunk(
            doc_name=chunk.doc_name,
            doc_revision=chunk.doc_revision,
            page_number=chunk.page_number,
            section=f"{chunk.section} (part {part_idx})" if part_idx > 1 else chunk.section,
            class_code=chunk.class_code,
            text="\n\n".join(buf),
        )


def _normalize_whitespace(s: str) -> str:
    """Collapse whitespace runs and trim. Keeps double-newlines as
    paragraph separators so downstream paragraph splits still work."""
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

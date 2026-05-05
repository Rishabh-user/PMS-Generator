"""
One-shot RAG ingestion script — chunks the master PMS PDF, embeds the
chunks with the configured embedding provider, and inserts them into
the doc_chunks table.

Run from the project root:

    python -m app.scripts.ingest_pms

The script is idempotent: it clears any existing chunks for the same
(doc_name, doc_revision) before re-inserting, so re-running after a
chunker tweak produces a clean corpus rather than accumulating
duplicates. Safe to invoke any time you change:

  • The chunker (chunker.py logic, e.g. better section detection)
  • The embedding model (voyage-3 → voyage-3-large)
  • The source PDF (revision bump, e.g. A0 → A1)

Prerequisites the script will check before it does anything:

  1. DATABASE_URL is set and reachable
  2. pgvector extension is installed on that database
  3. The configured embedding provider (Voyage or OpenAI) has a key
  4. The source PDF exists at the expected path

Each prerequisite produces a clear error message pointing at how to
fix it; the script never silently skips a step.

By default ingests the master Piping Material Specification only.
Pass --pdf <path> to ingest a different document, or --doc-name and
--doc-revision to override the labels stored in doc_chunks.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure `app.*` imports work whether the script is run as
# `python -m app.scripts.ingest_pms` (preferred) or `python ingest_pms.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.config import settings
from app.services import db_service
from app.services.chunker import Chunk, chunk_master_pms
from app.services.embedding_service import EmbeddingError, embed_batch

logger = logging.getLogger("ingest_pms")


_PDF_FILENAME = "40801-SPE-80000-PP-SP-0001-Rev A0-PIPING MATERIAL SPECIFICATION-Clean Copy.pdf"

# The project is laid out with reference PDFs in a SIBLING directory:
#     <repo-parent>/pms-files/40801-SPE-...0001-Rev A0-….pdf      ← actual location
#     <repo-parent>/pms-generator/                                 ← this project
# but a more conventional layout puts pms-files INSIDE the project. We
# probe both so users can drop the PDFs wherever feels natural without
# editing the script. First match wins; --pdf overrides everything.
_REPO_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_PDF_SEARCH_PATHS = [
    _REPO_ROOT_DIR.parent / "pms-files" / _PDF_FILENAME,    # sibling layout
    _REPO_ROOT_DIR / "pms-files" / _PDF_FILENAME,           # nested layout
]


def _resolve_default_pdf() -> Path | None:
    """Return the first existing default PDF location, or None if neither
    layout has the file."""
    for p in _PDF_SEARCH_PATHS:
        if p.exists():
            return p
    return None


_DEFAULT_PMS_PDF = _resolve_default_pdf() or _PDF_SEARCH_PATHS[0]


async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # ─── Pre-flight checks ─────────────────────────────────────────
    if not settings.database_url:
        print("ERROR: DATABASE_URL not set in .env", file=sys.stderr)
        print("       The ingestion script writes to Postgres; it cannot run", file=sys.stderr)
        print("       without a connection string.", file=sys.stderr)
        return 2

    pdf_path = Path(args.pdf or _DEFAULT_PMS_PDF)
    if not pdf_path.exists():
        print(f"ERROR: PDF not found at {pdf_path}", file=sys.stderr)
        if not args.pdf:
            print("Tried these default locations:", file=sys.stderr)
            for p in _PDF_SEARCH_PATHS:
                print(f"  {'✓' if p.exists() else '✗'} {p}", file=sys.stderr)
            print("Either move the PDF to one of those paths, or pass "
                  "--pdf <path> explicitly.", file=sys.stderr)
        return 2

    provider = (settings.embedding_provider or "voyage").lower()
    if provider == "voyage" and not settings.voyage_api_key:
        print("ERROR: VOYAGE_API_KEY not set in .env", file=sys.stderr)
        print("       Either set the key, or switch to OpenAI by setting", file=sys.stderr)
        print("       embedding_provider=openai and OPENAI_API_KEY in .env.", file=sys.stderr)
        return 2
    if provider == "openai" and not settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set in .env", file=sys.stderr)
        return 2

    # ─── Connect ────────────────────────────────────────────────────
    logger.info("Initialising Postgres pool…")
    await db_service.init_pool()
    if not db_service.is_rag_available():
        print(
            "ERROR: pgvector is not available on this database.\n"
            "       Install the extension with `CREATE EXTENSION vector;`\n"
            "       (requires Postgres 11+ and superuser, or pgvector\n"
            "       must be permitted by your managed-host provider).",
            file=sys.stderr,
        )
        await db_service.close_pool()
        return 2

    try:
        # ─── Chunk ─────────────────────────────────────────────────
        logger.info("Chunking %s …", pdf_path.name)
        chunks = chunk_master_pms(
            pdf_path,
            doc_name=args.doc_name,
            doc_revision=args.doc_revision,
        )
        if not chunks:
            print("ERROR: chunker produced 0 chunks — aborting", file=sys.stderr)
            return 1
        logger.info("Produced %d chunks", len(chunks))

        # ─── Embed ─────────────────────────────────────────────────
        logger.info(
            "Embedding %d chunks via %s/%s (dim=%d)…",
            len(chunks), provider,
            settings.embedding_model, settings.embedding_dimensions,
        )
        try:
            vectors = await embed_batch(
                [c.text for c in chunks],
                input_type="document",
            )
        except EmbeddingError as e:
            print(f"ERROR: embedding call failed — {e}", file=sys.stderr)
            return 1

        if len(vectors) != len(chunks):
            print(
                f"ERROR: embedding count mismatch ({len(vectors)} != {len(chunks)})",
                file=sys.stderr,
            )
            return 1

        # ─── Clear stale rows (idempotent re-ingest) ───────────────
        if not args.no_clear:
            cleared = await db_service.clear_doc_chunks(
                doc_name=args.doc_name, doc_revision=args.doc_revision,
            )
            if cleared:
                logger.info(
                    "Cleared %d stale chunks for %s rev=%s",
                    cleared, args.doc_name, args.doc_revision,
                )

        # ─── Insert ────────────────────────────────────────────────
        rows = [
            {
                "doc_name":     c.doc_name,
                "doc_revision": c.doc_revision,
                "page_number":  c.page_number,
                "section":      c.section,
                "class_code":   c.class_code,
                "text":         c.text,
                "embedding":    v,
            }
            for c, v in zip(chunks, vectors)
        ]
        inserted = await db_service.insert_doc_chunks(rows)
        logger.info("Inserted %d chunks into doc_chunks", inserted)

        # ─── Summary ───────────────────────────────────────────────
        total = await db_service.count_doc_chunks(args.doc_name)
        class_chunks = sum(1 for c in chunks if c.class_code)
        body_chunks = len(chunks) - class_chunks
        print()
        print("=" * 60)
        print(f"Ingestion complete — {pdf_path.name}")
        print("=" * 60)
        print(f"  Document:     {args.doc_name} rev {args.doc_revision}")
        print(f"  Chunks:       {len(chunks)} ({class_chunks} class sheets, {body_chunks} body sections)")
        print(f"  Embeddings:   {provider}/{settings.embedding_model} ({settings.embedding_dimensions} dim)")
        print(f"  doc_chunks total for {args.doc_name}: {total}")
        print()
        print("Next: enable RAG in the app by setting in .env:")
        print("  rag_enabled=true")
        print("  rag_use_for_notes=true")
        print()
        return 0
    finally:
        await db_service.close_pool()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ingest the master PMS PDF into the RAG corpus.",
    )
    p.add_argument(
        "--pdf",
        help=(
            "Path to the source PDF. Defaults to "
            "pms-files/40801-SPE-80000-PP-SP-0001-Rev A0-…-Clean Copy.pdf"
        ),
    )
    p.add_argument(
        "--doc-name", default="PMS",
        help="Logical document name stored in doc_chunks.doc_name "
             "(default: 'PMS'). Used by retrieve() filters.",
    )
    p.add_argument(
        "--doc-revision", default="A0",
        help="Revision string stored in doc_chunks.doc_revision (default: 'A0').",
    )
    p.add_argument(
        "--no-clear", action="store_true",
        help="Do NOT clear existing rows for (doc_name, doc_revision) "
             "before inserting. Use with care — repeated runs without "
             "--no-clear is the safe default.",
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))

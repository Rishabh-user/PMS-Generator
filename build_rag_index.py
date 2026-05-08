#!/usr/bin/env python3
"""
Build (or rebuild) the FAISS vector index from documents in app/standard_data/.

Rule:
  For each PDF found in standard_data/:
    - If a same-named .md file exists  → embed the .md  (preferred — higher quality parse)
    - If no .md file exists            → extract markdown from the PDF via pymupdf4llm (fallback)

Saves the dense FAISS index to app/faiss_index/ (used at runtime by rag_service.py).

Usage:
    python build_rag_index.py
    OPENAI_API_KEY=sk-... python build_rag_index.py
"""

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT    = Path(__file__).resolve().parent
STANDARD_DATA   = PROJECT_ROOT / "app" / "standard_data"
FAISS_INDEX_DIR = PROJECT_ROOT / "app" / "faiss_index"
HEADERS_TO_SPLIT = [("#", "Header 1")]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_pdf_markdown(pdf_path: Path) -> str:
    """Extract markdown from a PDF using pymupdf4llm."""
    try:
        import pymupdf4llm  # type: ignore
    except ImportError:
        logger.error(
            "pymupdf4llm is not installed. "
            "Run:  pip install pymupdf4llm"
        )
        raise
    logger.info("  Extracting PDF via pymupdf4llm: %s", pdf_path.name)
    return pymupdf4llm.to_markdown(str(pdf_path))


def _collect_documents(standard_data: Path) -> list:
    """
    Scan standard_data for PDFs.  For each:
      - use the .md counterpart when available
      - fall back to pymupdf4llm PDF extraction otherwise

    Returns a list of langchain Document objects with source metadata set to
    the file stem (e.g. "ASME B16.5_2020") so rag_service can display a clean
    document name in the context block.
    """
    from langchain_core.documents import Document  # type: ignore

    pdf_files = sorted(standard_data.glob("*.pdf"))
    if not pdf_files:
        logger.warning("No PDF files found in %s", standard_data)
        return []

    documents: list[Document] = []
    for pdf_path in pdf_files:
        md_path = pdf_path.with_suffix(".md")
        stem    = pdf_path.stem          # e.g. "ASME B16.5_2020"

        if md_path.exists():
            logger.info("[md ] %s", md_path.name)
            text   = md_path.read_text(encoding="utf-8")
            source = str(md_path)
        else:
            logger.info("[pdf] %s  (no .md found — using pymupdf4llm)", pdf_path.name)
            text   = _extract_pdf_markdown(pdf_path)
            source = str(pdf_path)

        documents.append(Document(page_content=text, metadata={"source": source, "stem": stem}))

    logger.info("Collected %d document(s)", len(documents))
    return documents


def _split_documents(documents: list) -> list:
    """Split each document on Markdown H1 headers."""
    from langchain_text_splitters import MarkdownHeaderTextSplitter  # type: ignore

    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=HEADERS_TO_SPLIT,
        strip_headers=False,
    )
    splits: list = []
    for doc in documents:
        chunks = splitter.split_text(doc.page_content)
        for chunk in chunks:
            chunk.metadata.setdefault("source", doc.metadata.get("source", ""))
        splits.extend(chunks)

    logger.info("Split into %d chunk(s)", len(splits))
    return splits


def _build_and_save(splits: list, api_key: str, output_dir: Path) -> None:
    """Embed chunks with OpenAI and persist the FAISS index."""
    from langchain_openai import OpenAIEmbeddings               # type: ignore
    from langchain_community.vectorstores import FAISS          # type: ignore

    embeddings = OpenAIEmbeddings(
        model="text-embedding-3-small",
        api_key=api_key,
    )
    logger.info("Embedding %d chunks (model: text-embedding-3-small) …", len(splits))
    vectorstore = FAISS.from_documents(splits, embeddings)

    output_dir.mkdir(parents=True, exist_ok=True)
    vectorstore.save_local(str(output_dir))
    logger.info("FAISS index saved → %s", output_dir)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # Resolve API key from environment or .env
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv(PROJECT_ROOT / ".env")
            api_key = os.environ.get("OPENAI_API_KEY", "")
        except ImportError:
            pass

    if not api_key:
        # Last resort: try pulling from app settings
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from app.config import settings  # type: ignore
            api_key = settings.openai_api_key or ""
        except Exception:
            pass

    if not api_key:
        logger.error(
            "OPENAI_API_KEY is not set.\n"
            "Export it:  export OPENAI_API_KEY=sk-...\n"
            "or prefix:  OPENAI_API_KEY=sk-... python build_rag_index.py"
        )
        sys.exit(1)

    docs   = _collect_documents(STANDARD_DATA)
    if not docs:
        logger.error("No documents to index. Aborting.")
        sys.exit(1)

    splits = _split_documents(docs)
    if not splits:
        logger.error("No chunks produced after splitting. Aborting.")
        sys.exit(1)

    _build_and_save(splits, api_key, FAISS_INDEX_DIR)
    logger.info("Done.")


if __name__ == "__main__":
    main()

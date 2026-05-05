"""
RAG (Retrieval-Augmented Generation) orchestration.

Public API surface:

    retrieve(query, top_k=N, doc_name=…, class_code=…)
        Embed the query, run a similarity search against doc_chunks,
        return a ranked list of relevant chunks for the AI to ground on.

    retrieve_for_class(piping_class)
        Convenience wrapper that pulls the master-PMS data sheet for a
        specific class — used by the notes-generation feature flag.

    format_context(chunks)
        Pretty-print retrieved chunks for inclusion in an AI prompt.
        Each chunk is wrapped with a `[doc_name p.X §section]` header
        so the AI knows where to cite from.

The module is deliberately defensive: every public function returns a
safe empty result when RAG is disabled (master switch off, pgvector
unavailable, embedding key missing, etc.) so callers can wire it in
without try/except. The non-RAG path keeps working untouched.
"""
from __future__ import annotations

import logging

from app.config import settings
from app.services import db_service
from app.services.embedding_service import EmbeddingError, embed_query

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """True when the RAG pipeline has everything it needs to retrieve.

    Three conditions must hold:
      • Master switch (settings.rag_enabled) is True
      • pgvector + doc_chunks are ready (db_service.is_rag_available())
      • The configured embedding provider has a key set

    Any False short-circuits retrieval to a no-op so the rest of the
    PMS generation pipeline is unaffected.
    """
    if not settings.rag_enabled:
        return False
    if not db_service.is_rag_available():
        return False
    provider = (settings.embedding_provider or "voyage").lower()
    if provider == "voyage" and not settings.voyage_api_key:
        return False
    if provider == "openai" and not settings.openai_api_key:
        return False
    return True


async def retrieve(
    query: str,
    top_k: int | None = None,
    min_similarity: float | None = None,
    doc_name: str | None = None,
    class_code: str | None = None,
) -> list[dict]:
    """Embed `query` and return the top-K most similar chunks.

    Returns an empty list when RAG is disabled or unconfigured (NEVER
    raises) so callers don't have to guard. Each result is a dict with
    `doc_name`, `doc_revision`, `page_number`, `section`, `class_code`,
    `text`, `similarity`.

    Optional filters:
      • doc_name="PMS"  scopes search to one document (avoids pulling
        a B31.3 paragraph when you wanted a class data sheet).
      • class_code="A25"  retrieves only chunks tagged for that class.
    """
    if not is_enabled():
        return []
    if not query.strip():
        return []
    k = top_k if top_k is not None else settings.rag_top_k
    sim_floor = min_similarity if min_similarity is not None else settings.rag_min_similarity

    try:
        qvec = await embed_query(query)
    except EmbeddingError as e:
        logger.warning("RAG retrieve: embedding failed (%s) — returning empty", e)
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning("RAG retrieve: unexpected embedding error (%s)", e)
        return []

    chunks = await db_service.retrieve_doc_chunks(
        query_vec=qvec,
        top_k=k,
        min_similarity=sim_floor,
        doc_name=doc_name,
        class_code=class_code,
    )
    if not chunks:
        logger.info(
            "RAG retrieve: no chunks above similarity %.2f for query=%r",
            sim_floor, query[:80],
        )
    return chunks


async def retrieve_for_class(
    piping_class: str,
    top_k: int = 3,
) -> list[dict]:
    """Pull the master-PMS data-sheet chunk for a specific class.

    Two-stage strategy:
      1. Exact match on class_code — should always hit because the
         master-PMS chunker stamps the code into Chunk.class_code.
      2. If no exact match (e.g. a custom class not yet in the corpus),
         fall back to a semantic search scoped to doc_name='PMS'.

    Used primarily by the notes-grounding feature flag in pms_service.
    """
    cls = (piping_class or "").upper().strip()
    if not cls or not is_enabled():
        return []

    # Stage 1 — direct lookup. We still embed a query (a no-op vector
    # search is faster than embedding skip) so the SQL stays uniform.
    query = f"Class {cls} data sheet — bolts, gaskets, valves, notes"
    direct = await retrieve(
        query, top_k=top_k, doc_name="PMS", class_code=cls,
        min_similarity=0.0,  # exact match → don't filter on similarity
    )
    if direct:
        return direct

    # Stage 2 — semantic search when the class isn't pre-indexed.
    return await retrieve(query, top_k=top_k, doc_name="PMS")


def format_context(chunks: list[dict], max_total_chars: int = 8000) -> str:
    """Render retrieved chunks as a single string for prompt injection.

    Each chunk gets a header line so the AI can cite its source:
        [PMS p.93 §A25 data sheet  (similarity 0.91)]
        <chunk text>

    Truncates to `max_total_chars` total to keep the prompt under
    Claude's context window. Higher-similarity chunks come first so
    they're never the ones dropped on truncation.
    """
    if not chunks:
        return ""

    parts: list[str] = []
    used = 0
    for c in chunks:
        header = (
            f"[{c.get('doc_name', '?')} "
            f"p.{c.get('page_number', '?')} "
            f"§{c.get('section', '')}"
            f"  similarity {c.get('similarity', 0):.2f}]"
        )
        body = c.get("text", "").strip()
        block = f"{header}\n{body}\n"
        # Reserve room for at least the header — never include a chunk
        # we'd have to truncate mid-sentence.
        if used + len(block) > max_total_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)

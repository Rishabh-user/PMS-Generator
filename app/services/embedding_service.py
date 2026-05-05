"""
Provider-agnostic embedding wrapper.

Supports three providers selected via `settings.embedding_provider`:

  • local   — sentence-transformers/all-MiniLM-L6-v2 served locally
              via fastembed (ONNX runtime, ~100MB, no API key, no rate
              limits). DEFAULT. 384-dim vectors. Best for projects
              that want full data privacy and zero per-token cost.

  • voyage  — Voyage AI (high-quality cloud embeddings, requires API
              key + payment method on file for production rate limits).

  • openai  — OpenAI embeddings (cloud, requires API key).

The `local` provider runs the same `all-MiniLM-L6-v2` model that
sentence-transformers ships, but via fastembed's ONNX runtime — which
sidesteps the PyTorch dependency (700MB) and the Windows long-path
issue that plagues `pip install sentence-transformers`. Quality is
identical to the upstream model; install footprint is ~7× smaller.

If a configured cloud provider has no key, the cloud functions raise
EmbeddingError with a clear message; callers (the ingestion script
and rag_service) handle this gracefully so the rest of the system
keeps working. The `local` provider has no key requirement and
basically never raises in normal operation.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Raised when an embedding call fails (auth, quota, network, etc.).

    Caller should treat this as "RAG retrieval unavailable" and fall
    back to the non-RAG path. We never want a transient embedding
    failure to take down the whole PMS generation pipeline."""


# ── Local model singleton ──────────────────────────────────────────
#
# `fastembed.TextEmbedding` loads the ONNX model on first instantiation
# (~80MB download, cached under the user's HF cache after that). We
# load lazily and cache the instance globally so subsequent embed calls
# don't pay the load cost. fastembed's encode() is synchronous CPU-bound;
# we wrap it with asyncio.to_thread() so it doesn't block the event loop
# when called from FastAPI handlers.

_LOCAL_MODEL = None  # populated on first call
_LOCAL_LOAD_LOCK = asyncio.Lock()


async def _get_local_model():
    """Lazy-init the fastembed model. Idempotent + concurrency-safe."""
    global _LOCAL_MODEL
    if _LOCAL_MODEL is not None:
        return _LOCAL_MODEL
    async with _LOCAL_LOAD_LOCK:
        if _LOCAL_MODEL is not None:  # check again after acquiring lock
            return _LOCAL_MODEL
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise EmbeddingError(
                "fastembed is not installed. Run: pip install fastembed"
            ) from e
        model_name = settings.embedding_model or "sentence-transformers/all-MiniLM-L6-v2"
        logger.info("Loading local embedding model %s (first use, ~10s)", model_name)
        # The first call downloads the ONNX weights + tokenizer (~80MB)
        # and caches them. Subsequent loads are instant.
        _LOCAL_MODEL = await asyncio.to_thread(TextEmbedding, model_name=model_name)
        logger.info("Local embedding model ready: %s", model_name)
    return _LOCAL_MODEL


async def _embed_local(texts: list[str]) -> list[list[float]]:
    """Encode a batch of texts with the local fastembed model."""
    model = await _get_local_model()
    # fastembed.embed() is a generator; materialise it on a worker
    # thread so the event loop stays responsive during the (CPU-bound)
    # embedding pass. For a 32-chunk batch this takes ~200ms on CPU.
    def _run() -> list[list[float]]:
        return [list(map(float, vec)) for vec in model.embed(texts)]
    return await asyncio.to_thread(_run)


# ── Public API ──────────────────────────────────────────────────────

async def embed_query(text: str) -> list[float]:
    """Embed a single short text (a user query). Returns a list[float]
    of length `settings.embedding_dimensions`."""
    if not text.strip():
        raise EmbeddingError("Cannot embed empty text")
    vecs = await embed_batch([text], input_type="query")
    return vecs[0]


async def embed_batch(
    texts: list[str],
    input_type: str = "document",
) -> list[list[float]]:
    """Embed a batch of texts. `input_type` is "document" for chunks
    we're indexing, "query" for retrieval queries — Voyage embeds the
    two cases slightly differently to optimise asymmetric search; OpenAI
    ignores the parameter.

    Returns a list of vectors in the same order as `texts`."""
    if not texts:
        return []

    provider = (settings.embedding_provider or "local").lower()
    if provider == "local":
        return await _embed_local(texts)
    if provider == "voyage":
        return await _embed_voyage(texts, input_type=input_type)
    if provider == "openai":
        return await _embed_openai(texts)
    raise EmbeddingError(
        f"Unknown embedding_provider: {provider!r}. "
        f"Expected 'local', 'voyage', or 'openai'."
    )


# ── Voyage AI ───────────────────────────────────────────────────────
#
# API docs: https://docs.voyageai.com/reference/embeddings-api
# Endpoint: POST https://api.voyageai.com/v1/embeddings
# Auth:     Authorization: Bearer <VOYAGE_API_KEY>
# Models:   voyage-3 (1024 dim, $0.06/1M), voyage-3-large (1024, $0.18/1M)
#
# Voyage applies different optimisation passes for "document" inputs
# (the corpus) vs "query" inputs (search queries) so the same text
# embedded as one type vs the other produces slightly different
# vectors. Indexing → input_type="document"; retrieval → "query".

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
# Voyage's hard cap is 128 inputs per request, but the free-tier rate
# limit is 10K tokens/minute. Class-data-sheet chunks average ~500
# tokens each, so 32 inputs/batch keeps each request well under the
# free-tier cap. Paid tiers tolerate the full 128 fine.
_VOYAGE_BATCH_LIMIT = 32
_VOYAGE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Conservative pacing for the free tier (3 RPM = one request every 20s).
# When rate-limited, we wait this long before the next request rather
# than busy-retrying. Paid-tier accounts won't see 429s and won't pay
# this latency cost.
_VOYAGE_FREE_TIER_PACING_SEC = 22.0
_VOYAGE_MAX_RETRIES = 5


async def _embed_voyage(
    texts: list[str],
    input_type: str = "document",
) -> list[list[float]]:
    if not settings.voyage_api_key:
        raise EmbeddingError(
            "VOYAGE_API_KEY is not configured. Set it in .env to enable "
            "Voyage embeddings, or set embedding_provider=openai to use OpenAI."
        )
    headers = {
        "Authorization": f"Bearer {settings.voyage_api_key}",
        "Content-Type": "application/json",
    }

    batches = list(_chunked(texts, _VOYAGE_BATCH_LIMIT))
    out: list[list[float]] = []
    rate_limited_seen = False  # once True, pace every following request

    async with httpx.AsyncClient(timeout=_VOYAGE_TIMEOUT) as client:
        for batch_idx, batch in enumerate(batches):
            payload = {
                "input": batch,
                "model": settings.embedding_model or "voyage-3",
                "input_type": input_type,
            }
            attempt = 0
            while True:
                # Pre-emptive pacing on follow-up batches if we've already
                # been rate-limited once in this run.
                if rate_limited_seen and batch_idx > 0 and attempt == 0:
                    logger.info(
                        "Voyage free-tier pacing: sleeping %.0fs before "
                        "batch %d/%d", _VOYAGE_FREE_TIER_PACING_SEC,
                        batch_idx + 1, len(batches),
                    )
                    await asyncio.sleep(_VOYAGE_FREE_TIER_PACING_SEC)

                try:
                    r = await client.post(_VOYAGE_URL, json=payload, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                    break
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and attempt < _VOYAGE_MAX_RETRIES:
                        # Free-tier rate limit. Voyage's body includes a
                        # human-readable explanation; we fall back to a
                        # fixed cooldown rather than parsing it. The
                        # 22-second wait covers the 3-RPM ceiling.
                        rate_limited_seen = True
                        attempt += 1
                        logger.warning(
                            "Voyage rate limit hit (batch %d/%d, attempt %d) "
                            "— sleeping %.0fs before retry",
                            batch_idx + 1, len(batches), attempt,
                            _VOYAGE_FREE_TIER_PACING_SEC,
                        )
                        await asyncio.sleep(_VOYAGE_FREE_TIER_PACING_SEC)
                        continue
                    body = e.response.text[:500] if e.response is not None else ""
                    raise EmbeddingError(
                        f"Voyage API error {e.response.status_code}: {body}"
                    ) from e
                except httpx.HTTPError as e:
                    if attempt < _VOYAGE_MAX_RETRIES:
                        attempt += 1
                        await asyncio.sleep(2.0 * attempt)
                        continue
                    raise EmbeddingError(f"Voyage HTTP error: {e}") from e

            for item in data.get("data", []):
                out.append([float(x) for x in item["embedding"]])

    if len(out) != len(texts):
        raise EmbeddingError(
            f"Voyage returned {len(out)} vectors for {len(texts)} inputs"
        )
    return out


# ── OpenAI ──────────────────────────────────────────────────────────
#
# API docs: https://platform.openai.com/docs/api-reference/embeddings
# Endpoint: POST https://api.openai.com/v1/embeddings
# Auth:     Authorization: Bearer <OPENAI_API_KEY>
# Models:   text-embedding-3-small (1536 dim, $0.02/1M)
#           text-embedding-3-large (3072 dim, $0.13/1M)
#
# IMPORTANT: text-embedding-3-small returns 1536-dim vectors by default,
# but accepts a `dimensions` parameter to truncate to fewer dims (e.g.
# 1024 to match our schema). pgvector stores vectors at the column's
# declared dimension — mismatched dim raises a Postgres error at insert
# time. Set `embedding_dimensions=1024` to keep schema/model in sync.

_OPENAI_URL = "https://api.openai.com/v1/embeddings"
_OPENAI_BATCH_LIMIT = 2048
_OPENAI_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def _embed_openai(texts: list[str]) -> list[list[float]]:
    if not settings.openai_api_key:
        raise EmbeddingError(
            "OPENAI_API_KEY is not configured. Set it in .env to enable "
            "OpenAI embeddings, or set embedding_provider=voyage to use Voyage."
        )
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=_OPENAI_TIMEOUT) as client:
        for batch in _chunked(texts, _OPENAI_BATCH_LIMIT):
            payload = {
                "input": batch,
                "model": settings.embedding_model or "text-embedding-3-small",
                "dimensions": settings.embedding_dimensions,
            }
            try:
                r = await client.post(_OPENAI_URL, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                body = e.response.text[:500] if e.response is not None else ""
                raise EmbeddingError(
                    f"OpenAI API error {e.response.status_code}: {body}"
                ) from e
            except httpx.HTTPError as e:
                raise EmbeddingError(f"OpenAI HTTP error: {e}") from e

            for item in data.get("data", []):
                out.append([float(x) for x in item["embedding"]])

    if len(out) != len(texts):
        raise EmbeddingError(
            f"OpenAI returned {len(out)} vectors for {len(texts)} inputs"
        )
    return out


# ── Internal helpers ────────────────────────────────────────────────

def _chunked(seq: Iterable, size: int):
    """Yield successive `size`-length sublists from `seq`."""
    buf = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf

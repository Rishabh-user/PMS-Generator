# RAG (Retrieval-Augmented Generation) — Setup Guide

This guide walks you through enabling the RAG layer that grounds AI
generation in the project's reference documents (master PMS spec,
Valve Material Spec, ASME standards, etc.).

The RAG pipeline is **additive and behind feature flags** — every flag
defaults `OFF`. An unconfigured deployment behaves exactly like the
pre-RAG version, so you can merge these changes without enabling
anything until you're ready.

---

## Architecture (one paragraph)

PDFs are chunked into per-class data sheets (and per-section body
chunks) by `app/services/chunker.py`. Each chunk is embedded by a
provider (Voyage AI or OpenAI) via `embedding_service.py`, then stored
in the `doc_chunks` Postgres table (pgvector extension). At PMS
generation time, `pms_service` calls `rag_service.retrieve_for_class()`
which embeds the request, runs a cosine-similarity search, and feeds
the top-K chunks to the AI as grounding context. AI now cites passages
from the actual PDF instead of relying on training-data memory.

---

## Prerequisites

You need three things in place before the pipeline does anything:

### 1. Postgres with `pgvector` extension

The RAG corpus lives in your existing project Postgres alongside
`pms_cache` and `pms_agent_sessions`. No new database to provision.

Check pgvector availability:

```sql
-- Connect with psql or your favourite client and run:
SELECT * FROM pg_available_extensions WHERE name = 'vector';
```

If the row exists, install:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

If it returns no rows:

| Postgres host | pgvector status |
|---------------|------------------|
| Self-hosted / Docker | install with `apt install postgresql-NN-pgvector` then `CREATE EXTENSION` |
| AWS RDS Postgres ≥ 15.2 | supported — just `CREATE EXTENSION` |
| Azure Database for Postgres | supported (must enable in server parameter `azure.extensions`) |
| Supabase | built-in — already enabled |
| Heroku Postgres | **NOT supported** — use an external vector store or migrate to Supabase |

Once the extension is installed, the application will pick it up
automatically the next time it starts (`db_service._init_rag_schema`
runs on startup).

### 2. Embedding API key

Pick one provider:

| Provider | Env var | Sign-up |
|----------|---------|---------|
| Voyage AI *(recommended)* | `VOYAGE_API_KEY` | <https://www.voyageai.com/> |
| OpenAI | `OPENAI_API_KEY` | <https://platform.openai.com/> |

Add to your `.env`:

```dotenv
# Voyage (default — best for technical text)
EMBEDDING_PROVIDER=voyage
EMBEDDING_MODEL=voyage-3
EMBEDDING_DIMENSIONS=1024
VOYAGE_API_KEY=<your-key>

# Or OpenAI:
# EMBEDDING_PROVIDER=openai
# EMBEDDING_MODEL=text-embedding-3-small
# EMBEDDING_DIMENSIONS=1024
# OPENAI_API_KEY=<your-key>
```

> **NOTE:** if you change `EMBEDDING_DIMENSIONS`, you must drop and
> recreate the `doc_chunks` table — pgvector cannot ALTER a vector
> column's declared dimension.

### 3. Source PDF in `pms-files/`

The default ingestion target is the master PMS document:

```
pms-files/40801-SPE-80000-PP-SP-0001-Rev A0-PIPING MATERIAL SPECIFICATION-Clean Copy.pdf
```

Already in the repo. No action needed unless you're indexing a new
revision or a different document.

---

## First-time setup

```bash
# 1. Install the new requirement (pypdf for chunking)
pip install -r requirements.txt

# 2. Update .env with the lines above (provider + key)

# 3. Run the ingestion script ONCE to populate doc_chunks
python -m app.scripts.ingest_pms

# 4. Enable the master switch in .env:
#    rag_enabled=true
#    rag_use_for_notes=true
```

Expected ingestion output:

```
Ingestion complete — 40801-SPE-80000-PP-SP-0001-Rev A0-…-Clean Copy.pdf
==============================================================
  Document:     PMS rev A0
  Chunks:       109 (91 class sheets, 18 body sections)
  Embeddings:   voyage/voyage-3 (1024 dim)
  doc_chunks total for PMS: 109

Next: enable RAG in the app by setting in .env:
  rag_enabled=true
  rag_use_for_notes=true
```

---

## Verifying it works

After enabling, hit the health endpoint:

```bash
curl http://localhost:8001/api/rag/status
```

A healthy response looks like:

```json
{
  "rag_enabled": true,
  "ready_for_retrieval": true,
  "pgvector_available": true,
  "provider": "voyage",
  "model": "voyage-3",
  "dimensions": 1024,
  "provider_key_set": true,
  "indexed_chunks_total": 109,
  "indexed_chunks_PMS": 109,
  "feature_flags": { "rag_use_for_notes": true }
}
```

If any field is `false` or 0, the corresponding step above wasn't
completed. The system will gracefully fall back to the non-RAG path
and PMS generation continues to work.

---

## Operational tasks

### Re-ingest after a chunker change or PDF revision

```bash
python -m app.scripts.ingest_pms
```

Idempotent — clears stale chunks for `(doc_name, doc_revision)` before
re-inserting.

### Index a different document

```bash
python -m app.scripts.ingest_pms \
    --pdf "/path/to/some-other.pdf" \
    --doc-name "VMS" \
    --doc-revision "A0"
```

Currently the chunker is tuned for the master PMS layout. The
`chunk_standard()` function in `chunker.py` exists for ASME / NACE
standards but hasn't been wired into the ingestion script yet — that's
Phase 2.

### Disable RAG without removing data

In `.env`:

```dotenv
rag_enabled=false
```

The corpus stays in `doc_chunks`; retrieval becomes a no-op until you
flip the flag back on.

### Clear the corpus

```bash
# Via Python REPL (pms-generator root):
python -c "
import asyncio
from app.services import db_service
async def go():
    await db_service.init_pool()
    n = await db_service.clear_doc_chunks()
    print(f'Deleted {n} rows')
    await db_service.close_pool()
asyncio.run(go())
"
```

---

## How RAG affects PMS generation

When `rag_enabled=true`, every `_generate_from_ai` call runs:

1. `rag_service.retrieve_for_class(piping_class)` — pulls the master-PMS
   data sheet for the requested class plus a few related passages.
2. The retrieved chunks are formatted as a `REFERENCE CONTEXT` block
   appended to the AI prompt.
3. The AI sees the canonical project rules + the actual PDF passage,
   and grounds its narrative output (notes, design code, branch chart
   citation) on the retrieved text.

Token cost: ~+2 KB per request for the retrieved context (roughly
~$0.001/req at current Anthropic pricing). Latency: +200-500 ms for
the embedding lookup.

When `rag_enabled=false` (default) the pipeline runs exactly as
before — zero overhead.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `pgvector_available: false` in `/api/rag/status` | extension not installed on this DB | run `CREATE EXTENSION vector;` |
| `provider_key_set: false` | missing API key in `.env` | set `VOYAGE_API_KEY` or `OPENAI_API_KEY` |
| Ingestion says `pgvector is not available on this database` | same as above | install extension first |
| Ingestion fails with `Voyage API error 401` | bad key | verify key at voyageai.com |
| Ingestion fails with `dimension mismatch` | model dim ≠ table dim | drop `doc_chunks`, set correct `EMBEDDING_DIMENSIONS`, re-run ingestion |
| `indexed_chunks_total: 0` after ingestion | ingestion ran without errors but inserted nothing | check the script's logs for the `Inserted N chunks` line |
| AI response doesn't seem to use the retrieved context | `rag_enabled=false` or retrieval returned nothing | check `/api/rag/status` and log line "RAG: retrieved N chunks for …" |

---

## Phased roadmap (what's next after this works)

| Phase | Status | What |
|-------|--------|------|
| 1 — RAG infra + master PMS indexed | ✅ this PR | foundation for all later phases |
| 2 — Index B31.3, B16.5, VMS | TODO | grounds wall thickness, flange, valve fields with real paragraphs |
| 3 — Free-form class generation | TODO | user inputs → system synthesizes a new class via RAG |
| 4 — Standards registry + upload UI | TODO | engineering can drop in new PDFs without touching code |
| 5 — Suggestion engine | TODO | "newer ASME edition available — switch?" |

Phase 1 is fully working with the steps above.

---

## File map

| File | Purpose |
|------|---------|
| `app/config.py` | RAG feature flags + provider keys |
| `app/services/chunker.py` | PDF → Chunk objects (no external API) |
| `app/services/embedding_service.py` | Voyage / OpenAI HTTP wrappers |
| `app/services/rag_service.py` | High-level retrieve / format_context API |
| `app/services/db_service.py` | pgvector schema + doc_chunks CRUD helpers |
| `app/scripts/ingest_pms.py` | One-shot CLI: chunk → embed → insert |
| `app/services/pms_service.py` | Calls retrieve_for_class before AI generation |
| `app/services/ai_service.py` | Accepts `retrieved_context` kwarg, appends to prompt |
| `app/routes/pms_routes.py` | `/api/rag/status` health endpoint |

Total addition: ~1,000 lines across 4 new files + small edits to 3
existing files. Zero changes to behaviour when flags are off.

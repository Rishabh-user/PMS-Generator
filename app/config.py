from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv(override=True)

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "PMS Generator API"
    app_version: str = "1.0.0"
    app_host: str = "0.0.0.0"
    app_port: int = 8001
    log_level: str = "INFO"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    templates_dir: Path = BASE_DIR / "app" / "templates"
    static_dir: Path = BASE_DIR / "app" / "static"

    # NOTE: the former `cache_ttl` + `cache_max_size` settings have been
    # removed — the L1 PMS cache no longer time-expires or size-caps.
    # Entries live in the L1 dict for the process lifetime and in the L2
    # PostgreSQL table forever. Regenerate-pms overwrites; the Admin UI
    # trash button or /api/clear-cache is the only way to remove entries.

    database_url: str = ""  # PostgreSQL DSN, e.g. postgresql://user:pass@localhost:5432/pms_generator

    # External SPE Valvesheet backend — when a new PMS is generated (POST) or
    # regenerated (PUT), we mirror the data to this endpoint so the main
    # Valvesheet project stays in sync. Defaults to the shared staging URL
    # so local dev and production both sync without per-environment setup;
    # override in .env only if you need to point at a different instance
    # (or set it to an empty string to disable sync entirely).
    external_valvesheet_api_url: str = (
        "https://spe-valvesheet-backend-staging.onrender.com/api/pms"
    )
    # Optional auth header value (e.g. "Bearer xxx" or "ApiKey xxx"). Only
    # sent when non-empty. The external API is currently open per the
    # curl examples, but this hook is cheap insurance for when it isn't.
    external_valvesheet_auth: str = ""
    # HTTP timeout (seconds) for each sync request. Kept conservative so
    # a slow external API can't stall PMS generation for long.
    external_valvesheet_timeout: float = 20.0

    # ── RAG (Retrieval-Augmented Generation) ─────────────────────────
    # The RAG layer is purely additive — every flag defaults OFF so an
    # unconfigured deployment behaves identically to the pre-RAG version.
    # Turn `rag_enabled` ON only after pgvector is installed on the DB
    # AND an embedding API key is set, then enable individual feature
    # flags (rag_use_for_notes, etc.) one at a time as you validate.
    rag_enabled: bool = False
    """Master switch for the RAG pipeline. When False, nothing in
    rag_service.py is active and the system runs exactly as before."""

    rag_use_for_notes: bool = False
    """Replace the AI-generated/hardcoded notes with retrieved notes from
    the indexed PMS document, per-class. First feature to ground via RAG."""

    # Embedding provider selection.
    #
    #   "local"  → sentence-transformers/all-MiniLM-L6-v2 served via
    #              fastembed (ONNX runtime). DEFAULT. No API key, no
    #              rate limits, ~80MB model download on first use.
    #              384-dim vectors. Recommended for production until
    #              you have a clear reason to use a cloud provider.
    #
    #   "voyage" → Voyage AI (cloud, paid past free tier).
    #              voyage-3 / voyage-3-large, 1024-dim.
    #
    #   "openai" → OpenAI (cloud, paid).
    #              text-embedding-3-small/large, configurable dim.
    embedding_provider: str = "local"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimensions: int = 384      # MUST match the model's native dim

    voyage_api_key: str = ""             # only used when embedding_provider="voyage"
    openai_api_key: str = ""             # only used when embedding_provider="openai"

    # Top-K retrieved chunks injected into the AI prompt at generation
    # time. Higher = more context, more tokens, more likely to ground
    # correctly. Lower = cheaper, faster, less context.
    rag_top_k: int = 5
    rag_min_similarity: float = 0.35
    """Cosine similarity floor — chunks below this score are dropped from
    retrieval so we never feed the AI noise. Tune empirically: too high
    means good chunks get filtered; too low means the AI gets distracted."""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

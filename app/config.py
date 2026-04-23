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
    # Valvesheet project stays in sync. Leave empty to disable sync entirely
    # (generation still works, it just doesn't forward the payload).
    external_valvesheet_api_url: str = ""
    # Optional auth header value (e.g. "Bearer xxx" or "ApiKey xxx"). Only
    # sent when non-empty. The external API is currently open per the
    # curl examples, but this hook is cheap insurance for when it isn't.
    external_valvesheet_auth: str = ""
    # HTTP timeout (seconds) for each sync request. Kept conservative so
    # a slow external API can't stall PMS generation for long.
    external_valvesheet_timeout: float = 20.0

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

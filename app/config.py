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

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

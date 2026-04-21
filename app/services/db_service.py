"""
PostgreSQL database service for caching AI-generated PMS data.

Stores full PMSResponse JSON so repeated requests are served instantly
without calling the AI. Uses asyncpg with a connection pool.

If DATABASE_URL is not configured, all operations gracefully return None
and the system falls back to AI-only generation.
"""
import json
import logging

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pms_cache (
    id            SERIAL PRIMARY KEY,
    cache_key     VARCHAR(64) UNIQUE NOT NULL,
    piping_class  VARCHAR(32) NOT NULL,
    material      VARCHAR(128) NOT NULL DEFAULT '',
    corrosion_allowance VARCHAR(32) NOT NULL DEFAULT '',
    service       TEXT NOT NULL DEFAULT '',
    response_json JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pms_cache_piping_class ON pms_cache (piping_class);
"""


def is_available() -> bool:
    """Check if the database connection pool is initialized and ready."""
    return _pool is not None


async def init_pool():
    """Initialize the asyncpg connection pool and create table if needed."""
    global _pool
    if not settings.database_url:
        logger.info("DATABASE_URL not set — DB caching disabled, using AI-only mode")
        return

    try:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        async with _pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
            await conn.execute(CREATE_INDEX_SQL)
        logger.info("PostgreSQL pool initialized and pms_cache table ready")
    except Exception as e:
        logger.error("Failed to initialize PostgreSQL pool: %s", e)
        _pool = None


async def close_pool():
    """Close the connection pool on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL pool closed")


async def get_cached_pms(cache_key: str) -> dict | None:
    """Fetch cached PMS response from DB. Returns parsed dict or None."""
    if not _pool:
        return None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT response_json FROM pms_cache WHERE cache_key = $1",
                cache_key,
            )
            if row:
                data = row["response_json"]
                # asyncpg returns JSONB as a string or dict depending on version
                if isinstance(data, str):
                    return json.loads(data)
                return data
    except Exception as e:
        logger.error("DB read error for key %s: %s", cache_key, e)
    return None


async def store_pms(
    cache_key: str,
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    response: dict,
) -> None:
    """Store or update PMS response in DB (UPSERT)."""
    if not _pool:
        return
    try:
        response_json = json.dumps(response, default=str)
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pms_cache (cache_key, piping_class, material, corrosion_allowance, service, response_json, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW(), NOW())
                ON CONFLICT (cache_key)
                DO UPDATE SET response_json = $6::jsonb, updated_at = NOW()
                """,
                cache_key, piping_class, material, corrosion_allowance, service, response_json,
            )
        logger.info("Stored PMS for %s in database (key=%s)", piping_class, cache_key[:8])
    except Exception as e:
        logger.error("DB write error for %s: %s", piping_class, e)


async def delete_cached_pms(cache_key: str) -> None:
    """Delete a single cached PMS entry."""
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute("DELETE FROM pms_cache WHERE cache_key = $1", cache_key)
    except Exception as e:
        logger.error("DB delete error for key %s: %s", cache_key, e)


async def list_cached_classes() -> list[dict]:
    """Return every cached PMS entry (one row per cache_key), newest first.

    Used by the frontend's Piping Class Specification page to show a direct
    download button only for classes that have a stored result — so we can
    serve Excel without triggering an AI generation.
    """
    if not _pool:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (piping_class)
                    piping_class, material, corrosion_allowance, service, updated_at
                FROM pms_cache
                ORDER BY piping_class, updated_at DESC
                """
            )
        return [
            {
                "piping_class": r["piping_class"],
                "material": r["material"],
                "corrosion_allowance": r["corrosion_allowance"],
                "service": r["service"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("DB list_cached_classes error: %s", e)
        return []


async def clear_all_cache() -> int:
    """Delete all cached PMS entries. Returns count deleted."""
    if not _pool:
        return 0
    try:
        async with _pool.acquire() as conn:
            result = await conn.execute("DELETE FROM pms_cache")
            count = int(result.split()[-1])
            logger.info("Cleared %d entries from pms_cache", count)
            return count
    except Exception as e:
        logger.error("DB clear error: %s", e)
        return 0

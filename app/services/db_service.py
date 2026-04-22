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

# ── pms_agent_sessions — saved PMS-Agent chats ─────────────────────
# One row per user+session_id. `blocks_json` holds the entire MessageBlock[]
# array from the frontend so the chat can be restored on load. Scoped by
# `user_id` (typically passed via X-User-Id header); requests without a
# user id fall into the reserved 'anonymous' bucket.

CREATE_AGENT_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS pms_agent_sessions (
    id            VARCHAR(32) NOT NULL,
    user_id       VARCHAR(128) NOT NULL DEFAULT 'anonymous',
    title         TEXT NOT NULL DEFAULT 'New chat',
    blocks_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
    message_count INT  NOT NULL DEFAULT 0,
    last_preview  TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, id)
);
"""

CREATE_AGENT_SESSIONS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pms_agent_sessions_user_updated
    ON pms_agent_sessions (user_id, updated_at DESC);
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
            await conn.execute(CREATE_AGENT_SESSIONS_SQL)
            await conn.execute(CREATE_AGENT_SESSIONS_INDEX_SQL)
        logger.info(
            "PostgreSQL pool initialized — pms_cache + pms_agent_sessions tables ready"
        )
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


# ─────────────────────────────────────────────────────────────────
# PMS Agent chat sessions
# ─────────────────────────────────────────────────────────────────
# All five functions return safe defaults (empty list / None / False) when
# the DB pool is unavailable so route handlers can surface "history sync
# off" without crashing.

def _normalize_user_id(user_id: str | None) -> str:
    uid = (user_id or "").strip()
    if not uid or len(uid) > 128:
        return "anonymous"
    return uid


async def list_agent_sessions(user_id: str) -> list[dict]:
    """Return a user's saved PMS-agent sessions (summaries only — no blocks
    payload), newest first."""
    if not _pool:
        return []
    uid = _normalize_user_id(user_id)
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, message_count, last_preview, created_at, updated_at
                FROM pms_agent_sessions
                WHERE user_id = $1
                ORDER BY updated_at DESC
                LIMIT 200
                """,
                uid,
            )
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "message_count": r["message_count"],
                "last_message_preview": r["last_preview"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("DB list_agent_sessions error (user=%s): %s", uid, e)
        return []


async def get_agent_session(user_id: str, session_id: str) -> dict | None:
    """Fetch the full session (with blocks). Returns None if not found or
    DB unavailable."""
    if not _pool:
        return None
    uid = _normalize_user_id(user_id)
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, title, blocks_json, message_count, last_preview,
                       created_at, updated_at
                FROM pms_agent_sessions
                WHERE user_id = $1 AND id = $2
                """,
                uid, session_id,
            )
        if not row:
            return None
        blocks = row["blocks_json"]
        if isinstance(blocks, str):
            blocks = json.loads(blocks)
        return {
            "id": row["id"],
            "title": row["title"],
            "blocks": blocks or [],
            "message_count": row["message_count"],
            "last_message_preview": row["last_preview"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
    except Exception as e:
        logger.error("DB get_agent_session error (%s/%s): %s", uid, session_id, e)
        return None


async def upsert_agent_session(
    user_id: str,
    session_id: str,
    title: str,
    blocks: list,
    message_count: int,
    last_message_preview: str,
) -> bool:
    """Create or overwrite a session. Returns True on success."""
    if not _pool:
        return False
    uid = _normalize_user_id(user_id)
    try:
        blocks_json = json.dumps(blocks, default=str)
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pms_agent_sessions
                    (id, user_id, title, blocks_json, message_count, last_preview, created_at, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, NOW(), NOW())
                ON CONFLICT (user_id, id)
                DO UPDATE SET
                    title         = EXCLUDED.title,
                    blocks_json   = EXCLUDED.blocks_json,
                    message_count = EXCLUDED.message_count,
                    last_preview  = EXCLUDED.last_preview,
                    updated_at    = NOW()
                """,
                session_id, uid, title, blocks_json, message_count, last_message_preview,
            )
        return True
    except Exception as e:
        logger.error("DB upsert_agent_session error (%s/%s): %s", uid, session_id, e)
        return False


async def rename_agent_session(user_id: str, session_id: str, title: str) -> bool:
    """Rename a session without touching blocks."""
    if not _pool:
        return False
    uid = _normalize_user_id(user_id)
    try:
        async with _pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE pms_agent_sessions
                SET title = $3, updated_at = NOW()
                WHERE user_id = $1 AND id = $2
                """,
                uid, session_id, title.strip() or "New chat",
            )
        # asyncpg returns e.g. "UPDATE 1"; split to check affected rows
        return result.split()[-1] != "0"
    except Exception as e:
        logger.error("DB rename_agent_session error (%s/%s): %s", uid, session_id, e)
        return False


async def delete_agent_session(user_id: str, session_id: str) -> bool:
    if not _pool:
        return False
    uid = _normalize_user_id(user_id)
    try:
        async with _pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM pms_agent_sessions WHERE user_id = $1 AND id = $2",
                uid, session_id,
            )
        return result.split()[-1] != "0"
    except Exception as e:
        logger.error("DB delete_agent_session error (%s/%s): %s", uid, session_id, e)
        return False

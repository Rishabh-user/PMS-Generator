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
    piping_class  VARCHAR(32) PRIMARY KEY,
    version       VARCHAR(8)  NOT NULL DEFAULT 'A0',
    material      VARCHAR(128) NOT NULL DEFAULT '',
    corrosion_allowance VARCHAR(32) NOT NULL DEFAULT '',
    service       TEXT NOT NULL DEFAULT '',
    response_json JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pms_cache_updated ON pms_cache (updated_at DESC);
"""

# ── pms_cache schema migration ──────────────────────────────────────
# The table was originally keyed by an MD5(cache_key) computed from
# (class, material, CA, service) — meaning the same piping_class with
# a different service string created a second row. The project owner
# wants one row PER CLASS, overwritten on regenerate, with a bumped
# `version` (A0 → A1 → A2 → …).
#
# This migration is idempotent — it detects the old schema and converts
# it in a single transaction:
#   1. Add `version` column if missing (default 'A0').
#   2. Drop the old `id` serial PK and `cache_key` column.
#   3. Dedupe: for each piping_class, keep the most-recently-updated
#      row and delete the rest.
#   4. Promote `piping_class` to the new PRIMARY KEY.
# On a fresh database (no prior schema) it's a no-op — `CREATE TABLE
# IF NOT EXISTS` above already produces the correct shape.

MIGRATION_SQL = """
DO $$
DECLARE
    has_cache_key   BOOLEAN;
    has_id_col      BOOLEAN;
    has_version_col BOOLEAN;
    pk_col          TEXT;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'pms_cache' AND column_name = 'cache_key'
    ) INTO has_cache_key;
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'pms_cache' AND column_name = 'id'
    ) INTO has_id_col;
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'pms_cache' AND column_name = 'version'
    ) INTO has_version_col;

    -- Step 1: add version column if missing
    IF NOT has_version_col THEN
        ALTER TABLE pms_cache ADD COLUMN version VARCHAR(8) NOT NULL DEFAULT 'A0';
        RAISE NOTICE 'pms_cache: added version column';
    END IF;

    -- Step 2: dedupe — keep newest row per piping_class
    DELETE FROM pms_cache a
    USING pms_cache b
    WHERE a.piping_class = b.piping_class
      AND (a.updated_at, COALESCE(a.ctid::text, ''))
          < (b.updated_at, COALESCE(b.ctid::text, ''));

    -- Step 3: drop old cache_key and id columns if present
    IF has_cache_key THEN
        ALTER TABLE pms_cache DROP CONSTRAINT IF EXISTS pms_cache_cache_key_key;
        ALTER TABLE pms_cache DROP COLUMN cache_key;
        RAISE NOTICE 'pms_cache: dropped cache_key column';
    END IF;
    IF has_id_col THEN
        ALTER TABLE pms_cache DROP CONSTRAINT IF EXISTS pms_cache_pkey;
        ALTER TABLE pms_cache DROP COLUMN id;
        RAISE NOTICE 'pms_cache: dropped id column';
    END IF;

    -- Step 4: promote piping_class to PRIMARY KEY if not already
    SELECT a.attname INTO pk_col
    FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'pms_cache'::regclass AND i.indisprimary
    LIMIT 1;
    IF pk_col IS NULL OR pk_col <> 'piping_class' THEN
        IF pk_col IS NOT NULL THEN
            ALTER TABLE pms_cache DROP CONSTRAINT pms_cache_pkey;
        END IF;
        ALTER TABLE pms_cache ADD PRIMARY KEY (piping_class);
        RAISE NOTICE 'pms_cache: piping_class is now the primary key';
    END IF;
END $$;
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
            await conn.execute(MIGRATION_SQL)
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


async def get_cached_pms(piping_class: str) -> dict | None:
    """Fetch cached PMS response from DB. Returns parsed dict or None."""
    if not _pool:
        return None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT response_json FROM pms_cache WHERE piping_class = $1",
                piping_class,
            )
            if row:
                data = row["response_json"]
                # asyncpg returns JSONB as a string or dict depending on version
                if isinstance(data, str):
                    return json.loads(data)
                return data
    except Exception as e:
        logger.error("DB read error for %s: %s", piping_class, e)
    return None


async def store_pms(
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    response: dict,
) -> str | None:
    """Store or update PMS response in DB. On conflict, bumps the version
    (A0 → A1 → A2 …) rather than creating a new row. Returns the version
    string that was written so callers can surface it in the response."""
    if not _pool:
        return None
    try:
        response_json = json.dumps(response, default=str)
        async with _pool.acquire() as conn:
            version = await conn.fetchval(
                """
                INSERT INTO pms_cache
                    (piping_class, version, material, corrosion_allowance,
                     service, response_json, created_at, updated_at)
                VALUES ($1, 'A0', $2, $3, $4, $5::jsonb, NOW(), NOW())
                ON CONFLICT (piping_class)
                DO UPDATE SET
                    version             = 'A' ||
                        ((SUBSTRING(pms_cache.version FROM 2))::int + 1)::text,
                    material            = EXCLUDED.material,
                    corrosion_allowance = EXCLUDED.corrosion_allowance,
                    service             = EXCLUDED.service,
                    response_json       = EXCLUDED.response_json,
                    updated_at          = NOW()
                RETURNING version
                """,
                piping_class, material, corrosion_allowance, service, response_json,
            )
        logger.info("Stored PMS for %s in database (version=%s)", piping_class, version)
        return version
    except Exception as e:
        logger.error("DB write error for %s: %s", piping_class, e)
        return None


async def delete_cached_pms(piping_class: str) -> None:
    """Delete a single cached PMS entry."""
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM pms_cache WHERE piping_class = $1", piping_class,
            )
    except Exception as e:
        logger.error("DB delete error for %s: %s", piping_class, e)


async def list_cached_classes() -> list[dict]:
    """Return every cached PMS entry (one row per piping_class), newest first.

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
                SELECT piping_class, version, material, corrosion_allowance,
                       service, updated_at
                FROM pms_cache
                ORDER BY updated_at DESC
                """
            )
        return [
            {
                "piping_class": r["piping_class"],
                "version": r["version"],
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


# ─────────────────────────────────────────────────────────────────
# Admin DB browser — list / inspect / delete entries across both
# tables. These helpers power the "PMS Database" admin UI. Unlike the
# user-scoped agent-session functions above, these read across ALL
# users and skip user-id scoping.
# ─────────────────────────────────────────────────────────────────

async def admin_get_stats() -> dict:
    """Return row counts + DB connectivity info for the admin UI header."""
    if not _pool:
        return {"db_available": False, "pms_cache_count": 0, "agent_sessions_count": 0}
    try:
        async with _pool.acquire() as conn:
            cache_count = await conn.fetchval("SELECT COUNT(*) FROM pms_cache")
            sess_count = await conn.fetchval("SELECT COUNT(*) FROM pms_agent_sessions")
            distinct_users = await conn.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM pms_agent_sessions"
            )
        return {
            "db_available": True,
            "pms_cache_count": cache_count or 0,
            "agent_sessions_count": sess_count or 0,
            "distinct_users": distinct_users or 0,
        }
    except Exception as e:
        logger.error("DB admin_get_stats error: %s", e)
        return {"db_available": False, "pms_cache_count": 0, "agent_sessions_count": 0}


async def admin_list_cache_entries(
    limit: int = 100, offset: int = 0, search: str = ""
) -> list[dict]:
    """List pms_cache rows for the admin browser — summary fields only
    (no response_json payload, which can be 50+ KB per row). Newest
    first. `search` matches substring against piping_class / material
    (case-insensitive) so the UI can filter without an extra endpoint."""
    if not _pool:
        return []
    try:
        async with _pool.acquire() as conn:
            if search.strip():
                pat = f"%{search.strip().lower()}%"
                rows = await conn.fetch(
                    """
                    SELECT piping_class, version, material, corrosion_allowance,
                           service, created_at, updated_at,
                           octet_length(response_json::text) AS payload_bytes
                    FROM pms_cache
                    WHERE LOWER(piping_class) LIKE $1
                       OR LOWER(material) LIKE $1
                       OR LOWER(service) LIKE $1
                    ORDER BY updated_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    pat, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT piping_class, version, material, corrosion_allowance,
                           service, created_at, updated_at,
                           octet_length(response_json::text) AS payload_bytes
                    FROM pms_cache
                    ORDER BY updated_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
        return [
            {
                "piping_class": r["piping_class"],
                "version": r["version"],
                "material": r["material"],
                "corrosion_allowance": r["corrosion_allowance"],
                "service": r["service"],
                "payload_bytes": r["payload_bytes"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("DB admin_list_cache_entries error: %s", e)
        return []


async def admin_get_cache_entry(piping_class: str) -> dict | None:
    """Fetch a full pms_cache row (including the response_json payload)
    for the admin drawer detail view."""
    if not _pool:
        return None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT piping_class, version, material, corrosion_allowance,
                       service, response_json, created_at, updated_at
                FROM pms_cache WHERE piping_class = $1
                """,
                piping_class,
            )
        if not row:
            return None
        resp = row["response_json"]
        if isinstance(resp, str):
            resp = json.loads(resp)
        return {
            "piping_class": row["piping_class"],
            "version": row["version"],
            "material": row["material"],
            "corrosion_allowance": row["corrosion_allowance"],
            "service": row["service"],
            "response_json": resp,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
    except Exception as e:
        logger.error("DB admin_get_cache_entry error (%s): %s", piping_class, e)
        return None


async def admin_delete_cache_entry(piping_class: str) -> bool:
    """Remove one pms_cache row by piping_class. Returns True iff a row
    was deleted (False means the class didn't exist)."""
    if not _pool:
        return False
    try:
        async with _pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM pms_cache WHERE piping_class = $1", piping_class,
            )
        return result.split()[-1] != "0"
    except Exception as e:
        logger.error("DB admin_delete_cache_entry error (%s): %s", piping_class, e)
        return False


async def admin_list_all_agent_sessions(
    limit: int = 200, offset: int = 0, search: str = ""
) -> list[dict]:
    """List pms_agent_sessions across ALL users (admin view)."""
    if not _pool:
        return []
    try:
        async with _pool.acquire() as conn:
            if search.strip():
                pat = f"%{search.strip().lower()}%"
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, title, message_count, last_preview,
                           created_at, updated_at,
                           octet_length(blocks_json::text) AS blocks_bytes
                    FROM pms_agent_sessions
                    WHERE LOWER(title) LIKE $1 OR LOWER(user_id) LIKE $1
                    ORDER BY updated_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    pat, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, title, message_count, last_preview,
                           created_at, updated_at,
                           octet_length(blocks_json::text) AS blocks_bytes
                    FROM pms_agent_sessions
                    ORDER BY updated_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "title": r["title"],
                "message_count": r["message_count"],
                "last_message_preview": r["last_preview"],
                "blocks_bytes": r["blocks_bytes"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("DB admin_list_all_agent_sessions error: %s", e)
        return []


async def admin_delete_any_agent_session(user_id: str, session_id: str) -> bool:
    """Admin-level delete of an agent session — doesn't require the
    caller to be the session owner."""
    if not _pool:
        return False
    try:
        async with _pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM pms_agent_sessions WHERE user_id = $1 AND id = $2",
                user_id, session_id,
            )
        return result.split()[-1] != "0"
    except Exception as e:
        logger.error("DB admin_delete_any_agent_session error: %s", e)
        return False

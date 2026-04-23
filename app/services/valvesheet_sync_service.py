"""
Valvesheet sync service — mirrors PMS cache rows to the external SPE
Valvesheet backend so both systems stay in lockstep.

Three entry points:

  • push_created(pms)   — fire-and-forget POST after a NEW generation
                          (version='A0'). Called from pms_service when
                          _store_in_caches inserts a fresh row.

  • push_updated(pms)   — fire-and-forget PUT after a REGENERATION
                          (version A1+). Same caller; decided by the
                          version returned from the UPSERT.

  • push_all_cached()   — one-shot bulk backfill. Fetches every row
                          from pms_cache and POSTs them as a JSON array
                          to the external endpoint. Invoked by the
                          POST /api/sync/valvesheet admin route.

If EXTERNAL_VALVESHEET_API_URL is unset, every function is a no-op and
logs a single info line on startup — the generator still works, just
without the mirror.

All failures are LOGGED and swallowed by the fire-and-forget path so a
flaky downstream never breaks PMS generation for the user. The bulk
endpoint returns a structured {synced, failed, failures} report so the
admin UI can surface per-class errors.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings
from app.models.pms_models import PMSResponse
from app.services import db_service

logger = logging.getLogger(__name__)


# ── Internals ──────────────────────────────────────────────────────

def _is_configured() -> bool:
    """True when the external URL is set. All public functions short-
    circuit on False — the project deliberately makes sync opt-in so
    local/dev deployments don't blast requests at a staging backend."""
    url = (settings.external_valvesheet_api_url or "").strip()
    return bool(url)


def _auth_headers() -> dict[str, str]:
    """Attach Authorization header if configured. Safe to call even
    when no auth is set — returns {} in that case."""
    auth = (settings.external_valvesheet_auth or "").strip()
    return {"Authorization": auth} if auth else {}


def _payload_from_pms(pms: PMSResponse) -> dict[str, Any]:
    """Build the wire payload for a single PMS. The external API gets
    the full PMS dict plus the explicit cache-row metadata (version,
    piping_class, material, CA, service) at the top level so it can
    key rows without parsing the nested response. If the external
    schema differs, this is the one function to adjust."""
    data = pms.model_dump()
    return {
        "piping_class": pms.piping_class,
        "version": getattr(pms, "version", None) or "A0",
        "rating": pms.rating,
        "material": pms.material,
        "corrosion_allowance": pms.corrosion_allowance,
        "service": pms.service,
        "data": data,
    }


def _payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Same shape as _payload_from_pms but sourced from a raw
    admin_get_cache_entry() DB row (used by the bulk endpoint)."""
    response = row.get("response_json") or {}
    return {
        "piping_class": row.get("piping_class", ""),
        "version": row.get("version") or "A0",
        "rating": response.get("rating", ""),
        "material": row.get("material") or response.get("material", ""),
        "corrosion_allowance": (
            row.get("corrosion_allowance") or response.get("corrosion_allowance", "")
        ),
        "service": row.get("service") or response.get("service", ""),
        "data": response,
    }


async def _send_one(
    client: httpx.AsyncClient, method: str, payload: dict
) -> tuple[bool, str]:
    """Single HTTP request with unified error handling. Returns
    (ok, detail) — detail is an empty string on success, or an error
    message on failure (used by the bulk report)."""
    url = settings.external_valvesheet_api_url
    try:
        resp = await client.request(
            method, url, json=payload, headers=_auth_headers()
        )
        if 200 <= resp.status_code < 300:
            return True, ""
        body_preview = (resp.text or "")[:200]
        return False, f"HTTP {resp.status_code}: {body_preview}"
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"


# ── Public API ─────────────────────────────────────────────────────

async def push_created(pms: PMSResponse) -> None:
    """POST a newly-generated PMS to the external API. Awaited internally
    but called by pms_service as a background task (see
    _sync_in_background below) so the user's response isn't delayed."""
    if not _is_configured():
        return
    payload = _payload_from_pms(pms)
    async with httpx.AsyncClient(timeout=settings.external_valvesheet_timeout) as c:
        ok, detail = await _send_one(c, "POST", payload)
    if ok:
        logger.info(
            "Valvesheet sync: POST %s (v=%s) ✓",
            pms.piping_class, payload["version"],
        )
    else:
        logger.warning(
            "Valvesheet sync: POST %s failed — %s", pms.piping_class, detail,
        )


async def push_updated(pms: PMSResponse) -> None:
    """PUT a regenerated PMS to the external API. Same as push_created
    but uses PUT — the external API is expected to upsert by
    piping_class, so the version bump is visible on its side."""
    if not _is_configured():
        return
    payload = _payload_from_pms(pms)
    async with httpx.AsyncClient(timeout=settings.external_valvesheet_timeout) as c:
        ok, detail = await _send_one(c, "PUT", payload)
    if ok:
        logger.info(
            "Valvesheet sync: PUT %s (v=%s) ✓",
            pms.piping_class, payload["version"],
        )
    else:
        logger.warning(
            "Valvesheet sync: PUT %s failed — %s", pms.piping_class, detail,
        )


def sync_in_background(pms: PMSResponse, is_regenerate: bool) -> None:
    """Fire-and-forget wrapper — schedules push_created/push_updated as
    a background task so the calling request returns immediately.
    pms_service._store_in_caches uses this after every successful
    UPSERT so no code path has to await the external call."""
    if not _is_configured():
        return
    coro = push_updated(pms) if is_regenerate else push_created(pms)
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        # No running event loop (e.g. sync call site) — fall back to
        # running it synchronously. Shouldn't happen in FastAPI but
        # makes this safe if someone calls it from a script.
        asyncio.run(coro)


async def push_all_cached() -> dict[str, Any]:
    """Bulk backfill: POST every row from pms_cache to the external API
    as a JSON array. Returns a {synced, failed, failures[]} report.

    If the external API only accepts single objects, the second block
    below falls back to a per-row loop. We try the array first because
    that's the user's stated intent ("send all the JSON in array").
    """
    if not _is_configured():
        return {
            "ok": False,
            "error": "EXTERNAL_VALVESHEET_API_URL is not configured on this server.",
        }
    if not db_service.is_available():
        return {
            "ok": False,
            "error": "DATABASE_URL is not configured — no cached PMS rows to sync.",
        }

    # Fetch full rows (with response_json) — we pull summaries first then
    # hydrate each, reusing admin helpers so the schema stays consistent
    # with the Admin UI.
    summaries = await db_service.admin_list_cache_entries(limit=500, offset=0)
    full_rows: list[dict[str, Any]] = []
    for s in summaries:
        row = await db_service.admin_get_cache_entry(s["piping_class"])
        if row:
            full_rows.append(row)

    if not full_rows:
        return {"ok": True, "synced": 0, "failed": 0, "message": "No cached rows."}

    payloads = [_payload_from_row(r) for r in full_rows]

    # ── Attempt 1: array POST ──────────────────────────────────────
    async with httpx.AsyncClient(
        timeout=settings.external_valvesheet_timeout * 2
    ) as client:
        ok, detail = await _send_one(client, "POST", payloads)  # type: ignore[arg-type]
        if ok:
            logger.info(
                "Valvesheet bulk sync: posted %d rows as a single array",
                len(payloads),
            )
            return {
                "ok": True,
                "synced": len(payloads),
                "failed": 0,
                "mode": "array",
                "classes": [p["piping_class"] for p in payloads],
            }

        # ── Attempt 2: fall back to per-row POST ───────────────────
        logger.info(
            "Valvesheet bulk sync: array POST rejected (%s) — retrying per-row",
            detail,
        )
        successes: list[str] = []
        failures: list[dict[str, str]] = []
        for p in payloads:
            ok1, detail1 = await _send_one(client, "POST", p)
            if ok1:
                successes.append(p["piping_class"])
            else:
                failures.append(
                    {"piping_class": p["piping_class"], "error": detail1}
                )

    logger.info(
        "Valvesheet bulk sync (per-row): %d ok, %d failed",
        len(successes), len(failures),
    )
    return {
        "ok": True,
        "synced": len(successes),
        "failed": len(failures),
        "mode": "per-row",
        "classes": successes,
        "failures": failures,
    }

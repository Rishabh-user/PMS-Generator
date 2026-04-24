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
                          from pms_cache and POSTs them one-by-one
                          (each as a single-key `{code: spec}` dict)
                          because the valvesheet API rejects array
                          payloads. Invoked by POST /api/sync/valvesheet.

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


def _spec_value_from_pms(pms: PMSResponse) -> dict[str, Any]:
    """Build the per-spec value object — MINIMAL 3-field shape.

    The valvesheet API accepts a sparse payload per spec. Previous
    attempts to send the full PMS (pressure_rating, material, data,
    etc.) were rejected — the only confirmed-working shape so far is
    the 3-field form:

      {
        "D1N": {
          "notes": [...],
          "service": "General",
          "version": "A1"
        }
      }

    If the valvesheet side later wants richer data, add fields here
    one at a time and watch the db_failed response for any new KeyErrors.
    """
    return {
        "notes": list(pms.notes or []),
        "service": pms.service or "",
        "version": getattr(pms, "version", None) or "A0",
    }


def _spec_value_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Same minimal shape as _spec_value_from_pms but sourced from a
    raw admin_get_cache_entry() DB row."""
    response = row.get("response_json") or {}
    return {
        "notes": list(response.get("notes") or []),
        "service": row.get("service") or response.get("service", "") or "",
        "version": row.get("version") or response.get("version") or "A0",
    }


def _payload_from_pms(pms: PMSResponse) -> dict[str, Any]:
    """Wire payload for a single-PMS POST. Wrapped as a single-key dict
    keyed by piping_class — the external valvesheet API iterates
    top-level keys and treats each as a separate spec. Wire shape:

      {
        "A1": {
          "notes": ["..."],
          "service": "General",
          "version": "A0"
        }
      }

    This is intentionally minimal — previous attempts to send the full
    PMS were rejected by the external API's validator. See
    _spec_value_from_pms for the exact field list.
    """
    return {pms.piping_class: _spec_value_from_pms(pms)}


def _payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Single-row equivalent of _payload_from_pms, used by the bulk
    endpoint when it has already read rows from the DB. Same
    `{code: {notes, service, version}}` wire shape."""
    return {row.get("piping_class", "UNKNOWN"): _spec_value_from_row(row)}


async def _send_one(
    client: httpx.AsyncClient, method: str, payload: dict
) -> tuple[bool, str]:
    """Single HTTP request with unified error handling. Returns
    (http_ok, body_text):

      • http_ok   — True if HTTP status is 2xx, False otherwise.
      • body_text — raw response text ALWAYS, even on HTTP 2xx.

    CRITICAL: we return the body on success too, not just on failure.
    The valvesheet API returns HTTP 200 with
    `{"ok": true, "db_succeeded": 0, "db_failed": [...]}` when it
    saved the JSON to disk but failed the DB insert — so a 2xx alone
    doesn't mean the sheet persisted. Callers MUST inspect the body to
    know whether the row actually landed in valvesheet's DB.
    """
    url = settings.external_valvesheet_api_url
    try:
        resp = await client.request(
            method, url, json=payload, headers=_auth_headers()
        )
        http_ok = 200 <= resp.status_code < 300
        body_text = resp.text or ""
        if not http_ok:
            # Prefix non-2xx responses so the caller's log line stays
            # informative even without parsing.
            return False, f"HTTP {resp.status_code}: {body_text[:200]}"
        return True, body_text
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"


def _parse_valvesheet_response(
    body_text: str, code: str
) -> tuple[bool, str]:
    """Interpret the valvesheet API's JSON response for a single-spec
    POST. Returns (db_ok, detail):

      • db_ok  — True iff the spec actually persisted on the valvesheet
                 side (i.e. present in db_succeeded / not in db_failed).
      • detail — human-readable detail (empty on success, or the
                 specific db_failed error message).

    Response shape we're parsing:
      {"ok": true, "sheets_total": N,
       "db_succeeded": M, "db_failed": [{"spec_code": "A1", "error": "..."}]}

    A body we can't parse is treated as success (we already know HTTP
    was 2xx) but with a note — avoids false-positive failures when the
    valvesheet side evolves its response shape.
    """
    import json as _json
    try:
        parsed = _json.loads(body_text) if body_text else None
    except Exception:
        return True, "(non-JSON response, assuming success)"

    if not isinstance(parsed, dict):
        return True, ""

    failed = parsed.get("db_failed") or []
    if isinstance(failed, list):
        for f in failed:
            if isinstance(f, dict) and f.get("spec_code") == code:
                return False, str(f.get("error", "unknown"))

    # No failure for our code → success. Prefer the structured
    # db_succeeded count for the log, but only in the per-spec case.
    succeeded = parsed.get("db_succeeded", 0)
    if isinstance(succeeded, int) and succeeded > 0:
        return True, ""
    # Some responses only echo sheets_total — still treat as OK if no
    # db_failed entry for us was returned.
    return True, ""


# ── Public API ─────────────────────────────────────────────────────

async def _push_with_method(pms: PMSResponse, method: str) -> None:
    """Shared POST/PUT send path for push_created and push_updated.
    Keeps both routes in sync — any change to logging, parsing, or
    error handling happens once."""
    if not _is_configured():
        logger.info(
            "Valvesheet sync: skipped for %s — EXTERNAL_VALVESHEET_API_URL not set",
            pms.piping_class,
        )
        return
    version = getattr(pms, "version", None) or "A0"
    payload = _payload_from_pms(pms)
    logger.info(
        "Valvesheet sync: %s %s (v=%s) → %s …",
        method, pms.piping_class, version, settings.external_valvesheet_api_url,
    )
    async with httpx.AsyncClient(timeout=settings.external_valvesheet_timeout) as c:
        http_ok, body = await _send_one(c, method, payload)

    if not http_ok:
        logger.warning(
            "Valvesheet sync: %s %s (v=%s) HTTP FAILED — %s",
            method, pms.piping_class, version, body,
        )
        return

    # HTTP 2xx — now inspect the response body to confirm DB persistence.
    # The valvesheet API returns 200 even when it rejects the DB insert,
    # so this step is what prevents false-positive "✓" logs.
    db_ok, detail = _parse_valvesheet_response(body, pms.piping_class)
    if db_ok:
        logger.info(
            "Valvesheet sync: %s %s (v=%s) ✓", method, pms.piping_class, version,
        )
    else:
        logger.warning(
            "Valvesheet sync: %s %s (v=%s) DB FAILED — %s",
            method, pms.piping_class, version, detail,
        )


async def push_created(pms: PMSResponse) -> None:
    """POST a newly-generated PMS to the external API. Awaited internally
    but called by pms_service as a background task (see
    sync_in_background below) so the user's response isn't delayed.

    The payload shape matches the manual-sync button exactly —
    `{code: {notes, service, version}}` — so the external API gets the
    same single-spec dict whether the sync is triggered by generate-pms,
    regenerate-pms, or the admin button.
    """
    await _push_with_method(pms, "POST")


async def push_updated(pms: PMSResponse) -> None:
    """PUT a regenerated PMS to the external API. Same as push_created
    but uses PUT — the external API is expected to upsert by
    piping_class, so the version bump is visible on its side."""
    await _push_with_method(pms, "PUT")


async def _safe_run(coro, piping_class: str) -> None:
    """Run a sync coroutine and swallow exceptions with a loud log.
    Without this wrapper, an exception in the background task just
    vanishes — asyncio.create_task() doesn't surface uncaught errors
    anywhere visible. Every failure now lands in the server log."""
    try:
        await coro
    except Exception as e:
        logger.exception(
            "Valvesheet sync: background task crashed for %s — %s",
            piping_class, e,
        )


def sync_in_background(pms: PMSResponse, is_regenerate: bool) -> None:
    """Fire-and-forget wrapper — schedules push_created/push_updated as
    a background task so the calling request returns immediately.
    pms_service._store_in_caches uses this after every successful
    UPSERT so no code path has to await the external call.

    Failures are logged via _safe_run (not raised) so a flaky external
    API never breaks PMS generation for the end user. Check the
    server's Valvesheet sync: ... log lines to see what happened."""
    if not _is_configured():
        logger.info(
            "Valvesheet sync: not configured — skipping auto-sync for %s",
            pms.piping_class,
        )
        return
    coro = push_updated(pms) if is_regenerate else push_created(pms)
    wrapped = _safe_run(coro, pms.piping_class)
    try:
        asyncio.create_task(wrapped)
        logger.info(
            "Valvesheet sync: queued %s (%s) for background push",
            pms.piping_class, "PUT" if is_regenerate else "POST",
        )
    except RuntimeError:
        # No running event loop (e.g. sync call site) — fall back to
        # running it synchronously. Shouldn't happen in FastAPI but
        # makes this safe if someone calls it from a script.
        asyncio.run(wrapped)


async def push_all_cached() -> dict[str, Any]:
    """Backfill: iterate every row from pms_cache and POST each to the
    external API as a single-key dict `{"A1": {...}}` — one request per
    spec. Returns a {synced, failed, failures[]} report aggregated from
    the per-request responses.

    Why per-spec instead of a bulk dict? The valvesheet API's DB
    persistence loop is per-sheet — shipping them individually means
    one flaky sheet can't poison the others and the per-request
    response gives us a clean ok/fail signal per class.
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

    import json as _json
    successes: list[str] = []
    failures: list[dict[str, str]] = []

    async with httpx.AsyncClient(
        timeout=settings.external_valvesheet_timeout * 2
    ) as client:
        for row in full_rows:
            code = row.get("piping_class", "")
            if not code:
                continue
            single_payload = {code: _spec_value_from_row(row)}
            ok, detail = await _send_one(client, "POST", single_payload)

            # Parse the per-request response if possible — the valvesheet
            # API returns {ok, sheets_total, db_succeeded, db_failed} per
            # call, and a single-spec POST that lands a db_failed for
            # that spec is logically a failure even though HTTP returned
            # 200. We reconcile here.
            parsed: Any = None
            try:
                parsed = _json.loads(detail) if detail else None
            except Exception:
                parsed = None

            db_failed_for_spec = []
            if isinstance(parsed, dict):
                lst = parsed.get("db_failed") or []
                if isinstance(lst, list):
                    db_failed_for_spec = [
                        f for f in lst
                        if isinstance(f, dict) and f.get("spec_code") == code
                    ]

            if ok and not db_failed_for_spec:
                successes.append(code)
            else:
                if db_failed_for_spec:
                    err = db_failed_for_spec[0].get("error", "unknown")
                else:
                    err = detail or "request failed"
                failures.append({"piping_class": code, "error": str(err)[:200]})

    logger.info(
        "Valvesheet per-spec sync: %d ok, %d failed out of %d",
        len(successes), len(failures), len(full_rows),
    )
    return {
        "ok": len(successes) > 0 and len(failures) == 0,
        "synced": len(successes),
        "failed": len(failures),
        "mode": "per-spec",
        "sheets_posted": len(full_rows),
        "classes_posted": successes,
        "failures": failures,
    }

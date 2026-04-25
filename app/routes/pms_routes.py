"""
FastAPI routes for PMS generation, engineering calculations, and downloads.
"""
import io
import logging
import re

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import settings
from app.models.pms_models import PMSRequest, PMSResponse, BulkDownloadRequest
from app.models.thickness_models import ComputeThicknessRequest, ComputeThicknessResponse
from app.models.pms_agent_models import (
    PMSAgentRequest,
    PMSAgentResponse,
    AgentSessionSummary,
    AgentSessionDetail,
    UpsertAgentSessionRequest,
    RenameAgentSessionRequest,
)
from app.models.validation_models import ValidationReport
from app.services.pms_service import generate_excel, generate_pms, regenerate_pms, clear_cache
from app.services.thickness_service import compute_thickness
from app.services.pms_agent_service import chat as pms_agent_chat
from app.services.validation_service import validate as validate_pms
from app.services.branch_chart_service import get_all_charts, get_branch_chart
from app.services import data_service, db_service, valvesheet_sync_service
from app.utils.engineering import interpolate_pressure_at_temp
from app.utils.engineering_constants import (
    HYDROTEST_FACTOR, OPERATING_PRESSURE_FACTOR, OPERATING_TEMP_FACTOR,
    MILL_TOLERANCE_PERCENT, MILL_TOLERANCE_FRACTION,
    JOINT_EFFICIENCY_E, WELD_STRENGTH_W, Y_COEFFICIENT,
    SMALL_BORE_CUTOFF_NPS,
    DEFAULT_CORROSION_ALLOWANCE, DEFAULT_SERVICE,
    STRESS_CS, STRESS_SS316L, STRESS_SS316, STRESS_SS304L,
    STRESS_DSS, STRESS_SDSS, STRESS_CUNI, STRESS_API5LX60,
    STRESS_TITANIUM_B861_GR2, STRESS_COPPER_C12200_H80, STRESS_COPPER_C12200_H55,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["PMS"])


@router.get("/pipe-classes")
async def list_pipe_classes():
    return data_service.get_pipe_class_list()


@router.get("/services", response_model=list[str])
async def list_services():
    """Canonical service-description list for the Service picker. Both the
    standalone HTML UI and the Valvesheet frontend fetch from here so the
    options stay in sync without hard-coding either side."""
    from app.data.service_options import SERVICE_OPTIONS
    return SERVICE_OPTIONS


@router.get("/pipe-classes/codes", response_model=list[str])
async def list_pipe_class_codes():
    return data_service.get_available_classes()


@router.get("/index-data")
async def api_index_data():
    """Full data for cascading dropdowns."""
    return data_service.get_index_data()


@router.post("/preview-pms")
async def api_preview_pms(req: PMSRequest):
    """Step 1: Return class metadata + P-T data from JSON only (no AI call).

    Also returns recommended defaults for the "Actual Process Design Conditions"
    form: the highest rated temperature as the default design T, and the P-T
    table value interpolated at that temperature as the default design P. This
    lets the frontend pre-fill the form without duplicating interpolation logic.
    """
    entry = data_service.find_entry(req.piping_class)
    if not entry:
        raise HTTPException(
            status_code=422,
            detail=f"Piping class '{req.piping_class}' not found in database.",
        )
    pt = entry.get("pressure_temperature", {})
    pressures = pt.get("pressures", [])
    temperatures = pt.get("temperatures", [])
    hydrotest = str(round(max(pressures) * HYDROTEST_FACTOR, 2)) if pressures else ""

    # Recommended defaults for the Design Conditions form
    default_design_temp_c = temperatures[-1] if temperatures else None
    default_design_pressure_barg = (
        interpolate_pressure_at_temp(temperatures, pressures, default_design_temp_c)
        if default_design_temp_c is not None
        else None
    )
    # MDMT: parse the first signed integer from the first temp label
    # (labels like "-29 to 38" → -29), falling back to the first numeric
    # breakpoint if no label is available.
    temp_labels = pt.get("temp_labels", [])
    default_mdmt_c: float | None = None
    if temp_labels:
        match = re.search(r"-?\d+", temp_labels[0])
        if match:
            default_mdmt_c = float(match.group())
    if default_mdmt_c is None and temperatures:
        default_mdmt_c = temperatures[0]

    return {
        "piping_class": req.piping_class,
        "rating": entry.get("rating", ""),
        "material": req.material,
        "corrosion_allowance": req.corrosion_allowance,
        "service": req.service,
        "hydrotest_pressure": hydrotest,
        "pressure_temperature": pt,
        "default_design_pressure_barg": default_design_pressure_barg,
        "default_design_temp_c": default_design_temp_c,
        "default_mdmt_c": default_mdmt_c,
    }


@router.post("/generate-pms", response_model=PMSResponse)
async def api_generate_pms(req: PMSRequest):
    """Step 2: Full AI-powered PMS generation."""
    try:
        return await generate_pms(req)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error generating PMS")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@router.post("/regenerate-pms", response_model=PMSResponse)
async def api_regenerate_pms(req: PMSRequest):
    """Force re-generation via AI, bypassing DB cache, and update stored result."""
    try:
        return await regenerate_pms(req)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error regenerating PMS")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@router.post("/download-excel")
async def api_download_excel(req: PMSRequest):
    try:
        pms = await generate_pms(req)
        excel_bytes = generate_excel(pms)
        filename = f"PMS_{pms.piping_class}_{pms.rating.replace('#', '').replace(' ', '_')}.xlsx"
        return StreamingResponse(
            io.BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error generating Excel")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@router.post("/download-excel-zip")
async def api_download_excel_zip(req: BulkDownloadRequest):
    """Generate an Excel PMS for each class in the request and return them
    all packed into a single ZIP archive. Used by the AI-Agent multi-select
    download flow — user picks N classes, gets one ZIP back."""
    import zipfile
    if not req.classes:
        raise HTTPException(status_code=400, detail="No classes selected")
    if len(req.classes) > 50:
        raise HTTPException(status_code=400, detail="Too many classes in one ZIP request (max 50)")

    buf = io.BytesIO()
    failures: list[str] = []
    successes: list[str] = []
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for cls_req in req.classes:
                try:
                    pms = await generate_pms(cls_req)
                    excel_bytes = generate_excel(pms)
                    safe_rating = pms.rating.replace("#", "").replace(" ", "_").replace("/", "_") or "NA"
                    fname = f"PMS_{pms.piping_class}_{safe_rating}.xlsx"
                    zf.writestr(fname, excel_bytes)
                    successes.append(pms.piping_class)
                except Exception as e:
                    logger.warning("Bulk ZIP: failed to generate %s: %s", cls_req.piping_class, e)
                    failures.append(f"{cls_req.piping_class}: {e}")

            # Include a short manifest so the user knows what succeeded / failed
            manifest = "PMS Bulk Download Manifest\n" + "=" * 40 + "\n\n"
            manifest += f"Requested: {len(req.classes)} class{'es' if len(req.classes) != 1 else ''}\n"
            manifest += f"Generated: {len(successes)}\n"
            manifest += f"Failed:    {len(failures)}\n\n"
            if successes:
                manifest += "SUCCEEDED:\n" + "\n".join(f"  - {s}" for s in successes) + "\n\n"
            if failures:
                manifest += "FAILED:\n" + "\n".join(f"  - {f}" for f in failures) + "\n"
            zf.writestr("_manifest.txt", manifest)

        if not successes:
            raise HTTPException(
                status_code=422,
                detail=f"Could not generate any of the {len(req.classes)} classes. "
                       f"First error: {failures[0] if failures else 'unknown'}",
            )

        buf.seek(0)
        filename = f"PMS_Bulk_{len(successes)}_classes.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error creating bulk ZIP")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/pms/{piping_class}", response_model=PMSResponse)
async def get_pms_by_class(
    piping_class: str,
    material: str = Query(default=""),
    corrosion_allowance: str = Query(default=""),
    service: str = Query(default=""),
):
    req = PMSRequest(
        piping_class=piping_class,
        material=material or piping_class,
        corrosion_allowance=corrosion_allowance or DEFAULT_CORROSION_ALLOWANCE,
        service=service or DEFAULT_SERVICE,
    )
    try:
        return await generate_pms(req)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/branch-charts")
async def api_branch_charts():
    """Get all branch connection charts."""
    return [c.model_dump() for c in get_all_charts()]


@router.get("/branch-charts/{chart_id}")
async def api_branch_chart(chart_id: str):
    """Get a specific branch connection chart by ID."""
    chart = get_branch_chart(chart_id)
    if not chart:
        raise HTTPException(status_code=404, detail=f"Chart {chart_id} not found")
    return chart.model_dump()


@router.get("/engineering-constants")
async def api_engineering_constants():
    """Return all engineering constants so the frontend uses the same values as backend."""
    return {
        "hydrotest_factor": HYDROTEST_FACTOR,
        "operating_pressure_factor": OPERATING_PRESSURE_FACTOR,
        "operating_temp_factor": OPERATING_TEMP_FACTOR,
        "mill_tolerance_percent": MILL_TOLERANCE_PERCENT,
        "mill_tolerance_fraction": MILL_TOLERANCE_FRACTION,
        "joint_efficiency_E": JOINT_EFFICIENCY_E,
        "weld_strength_W": WELD_STRENGTH_W,
        "y_coefficient": Y_COEFFICIENT,
        "small_bore_cutoff_nps": SMALL_BORE_CUTOFF_NPS,
        "default_corrosion_allowance": DEFAULT_CORROSION_ALLOWANCE,
        "default_service": DEFAULT_SERVICE,
        "stress_tables": {
            "CS": STRESS_CS,
            "API5LX60": STRESS_API5LX60,
            "SS316L": STRESS_SS316L,
            "SS316": STRESS_SS316,
            "SS304L": STRESS_SS304L,
            "DSS": STRESS_DSS,
            "SDSS": STRESS_SDSS,
            "CUNI": STRESS_CUNI,
            "TITANIUM_B861_GR2": STRESS_TITANIUM_B861_GR2,
            "COPPER_C12200_H80": STRESS_COPPER_C12200_H80,
            "COPPER_C12200_H55": STRESS_COPPER_C12200_H55,
        },
    }


@router.post("/clear-cache")
async def api_clear_cache():
    """Clear the PMS generation cache to force fresh AI re-generation."""
    await clear_cache()
    return {"status": "ok", "message": "Cache cleared. Next generation will use fresh AI data."}


# ── External valvesheet sync ───────────────────────────────────────
# Mirrors the local pms_cache to the SPE Valvesheet staging backend.
# Auto-sync on generate/regenerate is wired inside pms_service; this
# endpoint exists for one-shot backfills (e.g. when a deploy changes
# the downstream schema and every row needs re-pushing) and for ops
# visibility — the response reports how many rows synced vs failed so
# the admin UI can surface it.

@router.get("/sync/valvesheet/payload")
async def api_sync_valvesheet_payload():
    """Return the ready-to-POST bulk payload for the frontend's "Push
    to Valvesheet" button. Shape:

      {
        "count": 12,
        "target_url": "https://...",
        "payload": {
          "A1": { "pressure_rating": "150#", ...full spec... },
          "A1N": { ... },
          ...
        }
      }

    The browser then POSTs `payload` as-is to the external valvesheet
    URL. The valvesheet API iterates top-level keys and treats each as
    a separate spec_code, so one POST covers every cached row.
    """
    if not db_service.is_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured.")
    from app.services.valvesheet_sync_service import _spec_value_from_row
    summaries = await db_service.admin_list_cache_entries(limit=500, offset=0)
    payload: dict[str, dict] = {}
    for s in summaries:
        row = await db_service.admin_get_cache_entry(s["piping_class"])
        if row and row.get("piping_class"):
            payload[row["piping_class"]] = _spec_value_from_row(row)
    return {
        "count": len(payload),
        "target_url": settings.external_valvesheet_api_url or None,
        "payload": payload,
    }


@router.post("/sync/valvesheet")
async def api_sync_valvesheet_all():
    """POST every cached PMS row to the external valvesheet API, one
    spec per request. The external API rejects array payloads, so we
    send N single-key `{code: spec}` dicts and aggregate the results.

    Returns 503 when either:
      • DATABASE_URL is not configured (nothing to sync), or
      • EXTERNAL_VALVESHEET_API_URL is not configured (no destination), or
      • Every row failed (HTTP or DB-level) — so the caller knows nothing
        persisted on the valvesheet side.

    Returns 200 with `{synced, failed, failures[]}` when at least some
    rows went through. A partial-success response lets the caller
    decide whether to retry the failed subset.
    """
    result = await valvesheet_sync_service.push_all_cached()
    # Distinguish pre-flight failures (missing config, DB down) from
    # per-spec failures. The former deserves a 503 with the error;
    # the latter is a normal partial-success report.
    if "error" in result:
        raise HTTPException(
            status_code=503,
            detail=result["error"],
        )
    return result


@router.post("/sync/valvesheet/{piping_class}")
async def api_sync_valvesheet_one(piping_class: str):
    """Manually push ONE cached class to the valvesheet API. Treated as
    an UPDATE (PUT) because the row already exists in our cache —
    matches the user's mental model of 'resync this specific one'.

    Useful when the auto-sync failed (network blip, schema mismatch
    that's since been fixed) and you want to retry a single row
    without re-running a full bulk backfill. Returns both HTTP-level
    success and the valvesheet DB-level outcome so the caller can tell
    whether the sheet actually persisted."""
    if not db_service.is_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured.")
    entry = await db_service.admin_get_cache_entry(piping_class)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"No cached PMS for class '{piping_class}'.",
        )
    # Reuse the same helpers the auto-sync path uses — guarantees the
    # wire format stays identical regardless of which endpoint fires it.
    from app.services.valvesheet_sync_service import (
        _payload_from_row, _is_configured, _send_one,
        _parse_valvesheet_response,
    )
    if not _is_configured():
        raise HTTPException(
            status_code=503,
            detail="EXTERNAL_VALVESHEET_API_URL is not configured on this server.",
        )
    import httpx
    from app.config import settings as _settings
    payload = _payload_from_row(entry)
    async with httpx.AsyncClient(
        timeout=_settings.external_valvesheet_timeout
    ) as client:
        http_ok, body = await _send_one(client, "PUT", payload)
    if not http_ok:
        raise HTTPException(status_code=502, detail=f"Valvesheet PUT failed: {body}")
    # HTTP succeeded — check the valvesheet response body to confirm
    # the sheet actually landed in their DB. HTTP 200 with a db_failed
    # entry for this spec counts as a functional failure.
    db_ok, detail = _parse_valvesheet_response(body, piping_class)
    if not db_ok:
        raise HTTPException(
            status_code=502,
            detail=f"Valvesheet accepted the request but DB insert failed: {detail}",
        )
    return {
        "ok": True,
        "piping_class": piping_class,
        "version": entry.get("version") or "A0",  # read from DB row, not payload
    }


@router.get("/cached-classes")
async def api_list_cached_classes():
    """List piping classes that have a PMS result stored in the database.

    Used by the Piping Class Specification page to show a direct "Download
    Excel" button only for classes that are already cached — so the user can
    download without waiting for (or paying for) a fresh AI generation.
    """
    from app.services import db_service
    rows = await db_service.list_cached_classes()
    return {"cached": rows, "total": len(rows)}


@router.post("/validate-pms", response_model=ValidationReport)
async def api_validate_pms(req: PMSRequest):
    """
    Audit an AI-generated PMS against engineering standards.

    Checks (all deterministic, no external data source):
      - Class-code vs rating naming convention
      - NACE suffix vs material consistency
      - Mill tolerance vs ASME B36.10M standard 12.5%
      - Flange standard recognised (ASME B16.5 / B16.47)
      - Wall thickness lookup vs ASME B36.10M / B36.19M for each (OD, schedule)
      - Wall thickness adequacy per ASME B31.3 Eq. 3a at the class's P-T max
      - Valve code prefix matches class code

    Each finding is categorised ok / warning / error with a detailed
    explanation so the engineer can verify the AI output.
    """
    try:
        pms = await generate_pms(req)
        return validate_pms(pms)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Validation error")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@router.post("/pms-agent/chat", response_model=PMSAgentResponse)
async def api_pms_agent_chat(req: PMSAgentRequest):
    """
    Natural-language PMS search. Parses a free-text prompt (e.g.
    "generate A1 CS sour service" or "show 600# SS316L") into structured
    filters, matches against the pipe-class catalogue, and returns both a
    human-readable reply and a suggested action the frontend can execute.

    Deterministic parsing — no LLM call, works regardless of AI credit status.
    """
    try:
        return await pms_agent_chat(req)
    except Exception as e:
        logger.exception("PMS agent chat error")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


# ── PMS Agent conversation sessions ────────────────────────────────
# Persistent chat history, scoped per user via X-User-Id header.
# If the header is missing, sessions fall into a shared 'anonymous' bucket.
# Requires DATABASE_URL to be configured; without it, endpoints return
# empty lists / 503 for writes so the frontend can degrade gracefully.
#
# NOTE: The X-User-Id header is trusted as-is — the PMS backend has no
# authentication of its own. This is acceptable for internal tooling where
# the React frontend is the only caller, but for production multi-tenant
# use, replace with verified JWT / session cookies.

def _service_unavailable():
    raise HTTPException(
        status_code=503,
        detail="Chat history is currently unavailable — DATABASE_URL is not "
               "configured on the server, or the connection failed. The chat "
               "itself still works, but saved conversations cannot be listed "
               "or persisted.",
    )


@router.get(
    "/pms-agent/sessions",
    response_model=list[AgentSessionSummary],
)
async def api_list_agent_sessions(
    x_user_id: str | None = Header(default=None),
):
    """List the caller's saved PMS-agent chat sessions (summaries, no
    blocks). Returns 503 when DB isn't configured so the frontend can
    distinguish 'truly empty history' from 'history sync off'."""
    if not db_service.is_available():
        _service_unavailable()
    rows = await db_service.list_agent_sessions(x_user_id or "anonymous")
    return rows


@router.get(
    "/pms-agent/sessions/{session_id}",
    response_model=AgentSessionDetail,
)
async def api_get_agent_session(
    session_id: str,
    x_user_id: str | None = Header(default=None),
):
    """Fetch a single session with its full block list."""
    if not db_service.is_available():
        _service_unavailable()
    data = await db_service.get_agent_session(x_user_id or "anonymous", session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


@router.put("/pms-agent/sessions/{session_id}")
async def api_upsert_agent_session(
    session_id: str,
    req: UpsertAgentSessionRequest,
    x_user_id: str | None = Header(default=None),
):
    """Create or overwrite a session. Called by the frontend on every
    meaningful chat update (debounced)."""
    if not db_service.is_available():
        _service_unavailable()
    if not session_id or len(session_id) > 32:
        raise HTTPException(status_code=400, detail="Invalid session id")
    ok = await db_service.upsert_agent_session(
        user_id=x_user_id or "anonymous",
        session_id=session_id,
        title=req.title,
        blocks=req.blocks,
        message_count=req.message_count,
        last_message_preview=req.last_message_preview,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save session")
    return {"ok": True}


@router.patch("/pms-agent/sessions/{session_id}")
async def api_rename_agent_session(
    session_id: str,
    req: RenameAgentSessionRequest,
    x_user_id: str | None = Header(default=None),
):
    if not db_service.is_available():
        _service_unavailable()
    ok = await db_service.rename_agent_session(
        user_id=x_user_id or "anonymous",
        session_id=session_id,
        title=req.title,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@router.delete("/pms-agent/sessions/{session_id}")
async def api_delete_agent_session(
    session_id: str,
    x_user_id: str | None = Header(default=None),
):
    if not db_service.is_available():
        _service_unavailable()
    ok = await db_service.delete_agent_session(
        user_id=x_user_id or "anonymous",
        session_id=session_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


# ── Admin: browse everything in the database ──────────────────────
# Read-only views (+ row-level delete) over the two tables this
# backend owns (`pms_cache` and `pms_agent_sessions`). Powers the
# "PMS Database" page in the frontend. Not auth-protected yet — the
# route group is prefixed `/admin/db/*` so it's easy to gate later
# with middleware (e.g. require an admin role on X-User-Id).

@router.get("/admin/db/stats")
async def api_admin_db_stats():
    """Row counts + DB connectivity summary for the database browser header."""
    return await db_service.admin_get_stats()


@router.get("/admin/db/pms-cache")
async def api_admin_list_pms_cache(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default=""),
):
    """List pms_cache rows (newest first). `search` matches piping_class
    / material / service case-insensitively. Response excludes the
    response_json payload for speed; use the detail endpoint to fetch."""
    if not db_service.is_available():
        _service_unavailable()
    return await db_service.admin_list_cache_entries(
        limit=limit, offset=offset, search=search,
    )


@router.get("/admin/db/pms-cache/{piping_class}")
async def api_admin_get_pms_cache_entry(piping_class: str):
    """Fetch one pms_cache row INCLUDING the full response_json."""
    if not db_service.is_available():
        _service_unavailable()
    entry = await db_service.admin_get_cache_entry(piping_class)
    if not entry:
        raise HTTPException(status_code=404, detail="Cache entry not found")
    return entry


@router.delete("/admin/db/pms-cache/{piping_class}")
async def api_admin_delete_pms_cache_entry(piping_class: str):
    if not db_service.is_available():
        _service_unavailable()
    ok = await db_service.admin_delete_cache_entry(piping_class)
    if not ok:
        raise HTTPException(status_code=404, detail="Cache entry not found")
    return {"ok": True}


@router.get("/admin/db/agent-sessions")
async def api_admin_list_all_agent_sessions(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default=""),
):
    """List pms_agent_sessions across ALL users. `search` matches
    title / user_id case-insensitively."""
    if not db_service.is_available():
        _service_unavailable()
    return await db_service.admin_list_all_agent_sessions(
        limit=limit, offset=offset, search=search,
    )


@router.get("/admin/db/agent-sessions/{session_id}")
async def api_admin_get_agent_session(
    session_id: str,
    user_id: str = Query(..., description="Session owner user_id"),
):
    """Fetch one session with its full blocks payload. user_id is
    required as a query param because sessions are keyed by
    (user_id, id) — without it we can't uniquely address the row."""
    if not db_service.is_available():
        _service_unavailable()
    entry = await db_service.get_agent_session(user_id, session_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Session not found")
    return entry


@router.delete("/admin/db/agent-sessions/{session_id}")
async def api_admin_delete_agent_session(
    session_id: str,
    user_id: str = Query(..., description="Session owner user_id"),
):
    if not db_service.is_available():
        _service_unavailable()
    ok = await db_service.admin_delete_any_agent_session(user_id, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@router.post("/compute-thickness", response_model=ComputeThicknessResponse)
async def api_compute_thickness(req: ComputeThicknessRequest):
    """
    Compute per-size wall thickness, MAWP, margins, stress and engineering flags
    for a given piping class + user design inputs (design P, design T, MDMT, joint,
    optional Case 1 / stress overrides).

    Reuses the PMS cache for the underlying pipe schedule data and the shared
    ASME B31.3 engineering utilities — the frontend simply renders the response.
    """
    try:
        return await compute_thickness(req)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error computing thickness")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")

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
# thickness models removed in the SCH/WT hard-wipe — no replacement
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
# thickness_service deleted in the SCH/WT hard-wipe
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


@router.get("/options/ratings", response_model=list[str])
async def list_options_ratings():
    """Pressure-rating dropdown options. Sourced from
    `app/data/pressure_ratings.json` via `rating_lookup.all_labels()` —
    single source of truth for §5.5 PART-1 ratings."""
    from app.services import rating_lookup
    return rating_lookup.all_labels()


@router.get("/options/materials", response_model=list[str])
async def list_options_materials():
    """Material dropdown options. Sourced from `app/data/spec_options.py`,
    matching the §5.5 PART-2 material families."""
    from app.data.spec_options import SPEC_MATERIALS
    return SPEC_MATERIALS


@router.get("/options/corrosion-allowances", response_model=list[str])
async def list_options_corrosion_allowances():
    """Corrosion-allowance dropdown options. Standard increments per project
    convention; the §5.5 material digit is determined by (material, CA)
    pair at derivation time."""
    from app.data.spec_options import SPEC_CORROSION_ALLOWANCES
    return SPEC_CORROSION_ALLOWANCES


@router.get("/pipe-classes/codes", response_model=list[str])
async def list_pipe_class_codes():
    return data_service.get_available_classes()


@router.get("/index-data")
async def api_index_data():
    """Full data for cascading dropdowns."""
    return data_service.get_index_data()


@router.post("/preview-custom-class")
async def api_preview_custom_class(payload: dict):
    """Frontend opt-in flow for uncatalogued classes.

    Body: `{rating, material, corrosion_allowance, service?}`.

    Returns the *mode* the (rating, material, CA) combination resolves
    to. The frontend picks a panel UI based on the mode:

      • "catalogued"        → fast path; frontend should use /preview-pms
      • "standards_derived" → §5.5 + ASME B16.5 fully cover this combo
                              (Slice 1 fast path, Group 1.1 today).
                              Preview includes the derived P-T table.
      • "ai_only"           → §5.5 class code is valid, but the project
                              has no indexed standards data for this
                              rating/material/group yet (e.g. 5000#
                              wellhead service in API 6A territory, or
                              SS316L pre-Slice-2). The AI generates
                              everything from its own knowledge. Preview
                              has no P-T (the AI fills it in at
                              generation time). Frontend MUST collect
                              explicit design_pressure_barg + design_temp_c
                              from the user — those flow into the
                              §345.4.2(b) hydrotest correction since
                              there's no rated ceiling to fall back on.
      • "unsupported"       → §5.5 itself can't make sense of the
                              inputs (e.g. material/CA combo not in
                              the digit table). Frontend shows the
                              reason; user fixes the inputs.
    """
    from app.services.class_derivation import (
        classify_combination,
        MODE_CATALOGUED, MODE_STANDARDS, MODE_AI_ONLY, MODE_UNSUPPORTED,
        derive_synthetic_entry,
    )

    rating = payload.get("rating", "")
    material = payload.get("material", "")
    ca = payload.get("corrosion_allowance", "")
    service = payload.get("service", "")

    classification = classify_combination(rating, material, ca)
    mode = classification["mode"]
    class_code = classification.get("class_code")
    reason = classification["reason"]

    # MODE_UNSUPPORTED — frontend shows the message and the user fixes
    # the inputs. No `supported` field for backwards compat: instead
    # return mode='unsupported' and the frontend reads that.
    if mode == MODE_UNSUPPORTED:
        return {
            "mode":         MODE_UNSUPPORTED,
            "class_code":   None,
            "reason":       reason,
            "in_catalogue": False,
            "preview":      None,
        }

    # If the §5.5-derived class code already exists in the catalogue,
    # short-circuit to MODE_CATALOGUED so the frontend uses /preview-pms.
    if data_service.find_entry(class_code) is not None:
        return {
            "mode":         MODE_CATALOGUED,
            "class_code":   class_code,
            "reason":       f"Class {class_code} already exists in the "
                            f"catalogue — no derivation needed.",
            "in_catalogue": True,
            "preview":      None,
        }

    # MODE_STANDARDS — full standards-derived preview (Slice 1 fast path).
    if mode == MODE_STANDARDS:
        synth = derive_synthetic_entry(rating, material, ca, service)
        return {
            "mode":         MODE_STANDARDS,
            "class_code":   class_code,
            "reason":       reason,
            "in_catalogue": False,
            "preview": {
                "piping_class":        synth["piping_class"],
                "rating":              synth["rating"],
                "material":            synth["material"],
                "corrosion_allowance": synth["corrosion_allowance"],
                "service":             synth["service"],
                "pressure_temperature": synth["pressure_temperature"],
                "_derived":            True,
                "_ai_only":            False,
            },
        }

    # MODE_AI_ONLY — class code is valid per §5.5 but there's no indexed
    # standards data. The frontend renders a different (warning-coloured)
    # panel and requires the user to supply explicit design P/T before
    # opting in. There's no P-T preview to show — the AI builds it
    # from scratch at generation time.
    return {
        "mode":         MODE_AI_ONLY,
        "class_code":   class_code,
        "reason":       reason,
        "in_catalogue": False,
        "preview": {
            "piping_class":        class_code,
            "rating":              rating,
            "material":            material,
            "corrosion_allowance": ca,
            "service":             service or "Generic — AI-only",
            "pressure_temperature": {
                "temperatures": [],
                "pressures":    [],
                "temp_labels":  [],
            },
            "_derived":            True,
            "_ai_only":            True,
        },
    }


@router.post("/preview-pms")
async def api_preview_pms(req: PMSRequest):
    """Step 1: Return class metadata + P-T data from JSON only (no AI call).

    Also returns recommended defaults for the "Actual Process Design Conditions"
    form: the highest rated temperature as the default design T, and the P-T
    table value interpolated at that temperature as the default design P. This
    lets the frontend pre-fill the form without duplicating interpolation logic.

    Resolution order matches `_generate_from_ai` exactly so the preview and
    the actual generation can never disagree about which path will run:

      1. Catalogue lookup (fast path — 91 curated classes).
      2. Standards-derivation (Slice 1: §5.5 + ASME B16.5 Group 1.1).
      3. AI-only synthetic entry (§5.5 class code valid but no indexed
         standards data — preview shows empty P-T because the AI
         generates it at request time; `hydrotest` is computed from
         the user-supplied design P/T, which the frontend's AI-only
         panel makes mandatory).
    """
    entry = data_service.find_entry(req.piping_class)

    # Catalogue miss → try derivation, same fallback chain as
    # `pms_service._generate_from_ai`. We do this in the route handler
    # rather than calling pms_service so the preview stays AI-free.
    if not entry:
        from app.services.class_derivation import (
            derive_synthetic_entry, derive_synthetic_entry_ai_only,
            rating_from_class_code, DerivationError,
        )
        rating = rating_from_class_code(req.piping_class)
        if not rating:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Piping class '{req.piping_class}' not found in catalogue, "
                    f"and the class code doesn't start with a §5.5 rating "
                    f"letter (A/B/D/E/F/G/J/K/T) so it can't be derived."
                ),
            )
        try:
            entry = derive_synthetic_entry(
                rating=rating,
                material=req.material,
                corrosion_allowance=req.corrosion_allowance,
                service=req.service,
            )
        except DerivationError:
            # No standards data for this material/rating — fall back to
            # AI-only mode. We REQUIRE the user to have supplied design
            # P/T in the request (the frontend's AI-only panel enforces
            # this); without it, hydrotest can't be computed.
            if req.design_pressure_barg is None or req.design_temp_c is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Piping class '{req.piping_class}' has no indexed "
                        f"standards data. AI-only generation requires "
                        f"explicit design_pressure_barg and design_temp_c "
                        f"in the request; the Custom-Class panel gathers "
                        f"these from the user."
                    ),
                )
            entry = derive_synthetic_entry_ai_only(
                rating=rating,
                material=req.material,
                corrosion_allowance=req.corrosion_allowance,
                service=req.service,
            )
    pt = entry.get("pressure_temperature", {})
    pressures = pt.get("pressures", [])
    temperatures = pt.get("temperatures", [])
    is_ai_only = bool(entry.get("_ai_only"))

    # Hydrotest preview: catalogue / standards-derived classes use the
    # P-T table ceiling × 1.5 (matches the simple §345.4.2(a) formula —
    # the full §345.4.2(b) correction happens at generation time).
    # AI-only classes have no P-T to multiply by, so we use the user's
    # design pressure × 1.5 as the rough preview value.
    if is_ai_only:
        hydrotest = (
            str(round((req.design_pressure_barg or 0) * HYDROTEST_FACTOR, 2))
            if req.design_pressure_barg
            else ""
        )
    else:
        hydrotest = str(round(max(pressures) * HYDROTEST_FACTOR, 2)) if pressures else ""

    # Recommended defaults for the Design Conditions form. AI-only mode
    # has no rated table to draw from, so we echo back whatever the user
    # supplied (or None if not supplied — the frontend already requires
    # these in AI-only mode).
    if is_ai_only:
        default_design_temp_c = req.design_temp_c
        default_design_pressure_barg = req.design_pressure_barg
        default_mdmt_c = None      # AI-only doesn't have a rated cold endpoint
    else:
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
        # Surface the generation mode so the frontend banner / preview
        # card can show a "Custom — derived" or "Custom — AI-only" tag
        # rather than masquerading as a normal catalogued class.
        "is_derived": bool(entry.get("_derived")),
        "is_ai_only": is_ai_only,
    }


@router.post(
    "/generate-pms",
    response_model=PMSResponse,
    response_model_exclude={
        "pipe_data", "pressure_temperature",
        "fittings", "fittings_welded", "fittings_by_size",
    },
)
async def api_generate_pms(req: PMSRequest):
    """Full PMS generation. Server-built derived fields are omitted from
    the response — the frontend fetches them on demand via dedicated
    endpoints:
      • pipe_data           → /api/pipe-data
      • pressure_temperature → /api/pressure-temperature
      • fittings_by_size    → /api/fittings-by-size
    The internal `pms.*` fields are still populated (Excel download reads
    them) but aren't shipped to the JSON client."""
    try:
        return await generate_pms(req)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error generating PMS")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@router.post(
    "/regenerate-pms",
    response_model=PMSResponse,
    response_model_exclude={
        "pipe_data", "pressure_temperature",
        "fittings", "fittings_welded", "fittings_by_size",
    },
)
async def api_regenerate_pms(req: PMSRequest):
    """Force re-generation via AI, bypassing DB cache. Server-built
    derived fields excluded from response — see `/api/generate-pms`
    docstring for the dedicated endpoints."""
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


@router.get(
    "/pms/{piping_class}",
    response_model=PMSResponse,
    response_model_exclude={
        "pipe_data", "pressure_temperature",
        "fittings", "fittings_welded", "fittings_by_size",
    },
)
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
    """Return all engineering constants so the frontend uses the same values
    as backend. Sources the OD table from engineering_constants.ASME_PIPE_OD
    (loaded once at import time) and the WT table from schedule_selector's
    lru-cached `_wt_table()` — neither does I/O on this hot endpoint."""
    from app.utils.engineering_constants import ASME_PIPE_OD
    from app.services.schedule_selector import _wt_table
    from app.services import schedule_floor_lookup
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
        "asme_pipe_od": ASME_PIPE_OD,
        "asme_wall_thicknesses_mm": _wt_table(),
        "project_schedule_floors": schedule_floor_lookup.floor_lookup_dict(),
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


@router.get("/pipe-data")
async def api_pipe_data(
    piping_class: str = Query(...),
    material: str = Query(default=""),
    corrosion_allowance: str = Query(default=""),
    design_pressure_barg: float | None = Query(default=None),
    design_temp_c: float | None = Query(default=None),
):
    """Return the pipe-data array for a class on demand.

    Replaces the `pipe_data` field that used to be on `/api/generate-pms`
    responses. Computed fresh from `class_metadata.json` + dual-case
    Eq. 3a + project-floor lookup, using the request's design P/T (so
    different design points produce different SCH/WT — no stale cached
    values).

    Tubing classes (T80*/T90*) use `tubing_service.build_tubing_pms()`'s
    own row generation since their dimensions don't follow the
    metadata's per-NPS schema.
    """
    from app.services.pipe_data_builder import build_pipe_data_rows
    from app.services.tubing_service import is_tubing_class, build_tubing_pms
    from app.services.pt_lookup import lookup_pt
    from app.services.class_derivation import _MATERIAL_TO_GROUP, _material_token
    from app.services import rating_lookup
    from app.utils.pipe_data import correct_pipe_data
    from app.models.pms_models import PMSRequest

    code = (piping_class or "").upper().strip()

    # Tubing path — defer to tubing_service for the deterministic build.
    if is_tubing_class(code):
        req = PMSRequest(
            piping_class=code,
            material=material or "",
            corrosion_allowance=corrosion_allowance or "NIL",
            service="General",
        )
        pms = build_tubing_pms(req)
        return [p.model_dump() for p in pms.pipe_data]

    # ASME / non-ASME path — builder + correct_pipe_data.
    try:
        rows = build_pipe_data_rows(code)
    except KeyError as e:
        raise HTTPException(
            status_code=404,
            detail=f"Class '{code}' not in class_metadata.json. Add it or use a tubing-class code.",
        )

    # Look up P-T curve for dual-case Case 1 (cold-end of B16.5 curve).
    # Derive (rating, group) from the class code itself.
    rating = rating_lookup.letter_to_label(code[0]) if code else None
    pt_pressures: list[float] = []
    pt_temperatures: list[float] = []
    if rating and material:
        base_mat, _, _ = _material_token(material)
        group = _MATERIAL_TO_GROUP.get(base_mat)
        curve = lookup_pt(group, rating) if group else None
        if curve:
            pt_pressures = list(curve.get("pressures_barg") or [])
            pt_temperatures = list(curve.get("temperatures_c") or [])

    correct_pipe_data(
        rows,
        pipe_code=ai_data_pipe_code_for(code),
        material=material or "",
        pt_pressures=pt_pressures,
        pt_temperatures=pt_temperatures,
        design_pressure_barg=design_pressure_barg,
        design_temp_c=design_temp_c,
        corrosion_allowance=corrosion_allowance,
        piping_class=code,
    )
    return rows


def ai_data_pipe_code_for(class_code: str) -> str:
    """Helper for /api/pipe-data — gets the pipe_code from class_metadata.json
    so correct_pipe_data correctly identifies ASME vs non-ASME (it gates
    OD lookup on the pipe_code string)."""
    from app.services.pipe_data_builder import get_class_pipe_code
    return get_class_pipe_code(class_code) or ""


@router.get("/fittings-by-size")
async def api_fittings_by_size(piping_class: str = Query(...)):
    """Return the fittings_by_size array for a class on demand.

    Replaces the `fittings_by_size` field that used to be on
    `/api/generate-pms` responses. Computed fresh from `class_metadata.json`'s
    `fitting_groups` + `fitting_standards` (no AI). Same Path-C pattern as
    `/api/pipe-data`: deterministic, fast (in-memory JSON read), and
    returns the same row shape the response model used to ship.

    Returns 404 for classes without fitting metadata wired in
    `class_metadata.json` (uncatalogued / tubing). The frontend's
    `attachPipeData` helper falls back to an empty list in that case."""
    from app.services.fittings_builder import build_fittings_by_size, has_fitting_metadata
    code = (piping_class or "").upper().strip()
    if not has_fitting_metadata(code):
        raise HTTPException(
            status_code=404,
            detail=f"Class '{code}' has no fitting metadata in class_metadata.json. "
                   f"Add it or use a tubing-class code.",
        )
    return build_fittings_by_size(code)


@router.get("/class-metadata")
async def api_class_metadata():
    """Return the full class_metadata.json — every class's size list,
    pipe-type transition NPS, MOC rules, ends, and (for non-ASME) explicit
    OD/WT tables. Used by the frontend's class browser if it wants to
    display class structure without computing pipe_data."""
    from app.services.pipe_data_builder import _metadata
    return _metadata()


@router.get("/pressure-temperature")
async def api_pressure_temperature(
    piping_class: str = Query(...),
    material: str = Query(default=""),
):
    """Return the ASME B16.5 P-T curve for a class on demand.

    Replaces the `pressure_temperature` field that used to be on
    `/api/generate-pms` responses. Sources from `pt_by_class.json` via
    `pt_lookup` — same data the dual-case Eq. 3a uses for Case 1
    cold-end derivation, so the UI's WT calculation matches what the
    server computes when building pipe_data.

    Body shape:
      {
        "temperatures": [38, 50, 100, ...],
        "pressures":    [19.6, 19.2, 17.7, ...],
        "temp_labels":  ["-29 to 38", "50", "100", ...]
      }

    Returns 404 when the rating isn't indexed in pt_by_class.json
    (5000#/10000# stubs) or the material doesn't map to any group.
    """
    from app.services.pt_lookup import lookup_pt
    from app.services.class_derivation import _MATERIAL_TO_GROUP, _material_token
    from app.services import rating_lookup

    code = (piping_class or "").upper().strip()
    if not code:
        raise HTTPException(status_code=400, detail="piping_class is required")

    rating = rating_lookup.letter_to_label(code[0])
    if not rating:
        raise HTTPException(
            status_code=400,
            detail=f"Class code {code!r} doesn't start with a §5.5 rating letter",
        )

    base_mat, _, _ = _material_token(material)
    group = _MATERIAL_TO_GROUP.get(base_mat)
    if not group:
        raise HTTPException(
            status_code=404,
            detail=f"Material {material!r} has no indexed B16.5 group",
        )

    curve = lookup_pt(group, rating)
    if not curve:
        raise HTTPException(
            status_code=404,
            detail=f"No P-T curve indexed for rating={rating} group={group}",
        )

    return {
        "temperatures": list(curve.get("temperatures_c") or []),
        "pressures": list(curve.get("pressures_barg") or []),
        "temp_labels": list(curve.get("temp_labels") or []),
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


# /api/compute-thickness route removed in the SCH/WT hard-wipe.
# The thickness_service module and its models were deleted; the frontend
# Wall Thickness Calculation Table that consumed this endpoint is also
# being removed. If a future replacement is built, define a new route
# here with its own request/response models.

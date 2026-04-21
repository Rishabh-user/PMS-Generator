"""
FastAPI routes for PMS generation, engineering calculations, and downloads.
"""
import io
import logging
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.models.pms_models import PMSRequest, PMSResponse
from app.models.thickness_models import ComputeThicknessRequest, ComputeThicknessResponse
from app.models.pms_agent_models import PMSAgentRequest, PMSAgentResponse
from app.models.validation_models import ValidationReport
from app.services.pms_service import generate_excel, generate_pms, regenerate_pms, clear_cache
from app.services.thickness_service import compute_thickness
from app.services.pms_agent_service import chat as pms_agent_chat
from app.services.validation_service import validate as validate_pms
from app.services.branch_chart_service import get_all_charts, get_branch_chart
from app.services import data_service
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

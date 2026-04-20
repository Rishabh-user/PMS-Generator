"""
FastAPI routes for PMS generation, engineering calculations, and downloads.
"""
import io
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.models.pms_models import PMSRequest, PMSResponse
from app.models.thickness_models import ComputeThicknessRequest, ComputeThicknessResponse
from app.services.pms_service import generate_excel, generate_pms, regenerate_pms, clear_cache
from app.services.thickness_service import compute_thickness
from app.services.branch_chart_service import get_all_charts, get_branch_chart
from app.services import data_service
from app.utils.engineering import (
    check_pt_adequacy,
)
from app.utils.engineering_constants import (
    HYDROTEST_FACTOR, OPERATING_PRESSURE_FACTOR, OPERATING_TEMP_FACTOR,
    MILL_TOLERANCE_PERCENT, MILL_TOLERANCE_FRACTION,
    JOINT_EFFICIENCY_E, WELD_STRENGTH_W, Y_COEFFICIENT,
    SMALL_BORE_CUTOFF_NPS, AI_MAX_TOKENS,
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
    """Step 1: Return class metadata + P-T data from JSON only (no AI call)."""
    entry = data_service.find_entry(req.piping_class)
    if not entry:
        raise HTTPException(
            status_code=422,
            detail=f"Piping class '{req.piping_class}' not found in database.",
        )
    pt = entry.get("pressure_temperature", {})
    pressures = pt.get("pressures", [])
    hydrotest = str(round(max(pressures) * HYDROTEST_FACTOR, 2)) if pressures else ""
    return {
        "piping_class": req.piping_class,
        "rating": entry.get("rating", ""),
        "material": req.material,
        "corrosion_allowance": req.corrosion_allowance,
        "service": req.service,
        "hydrotest_pressure": hydrotest,
        "pressure_temperature": pt,
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

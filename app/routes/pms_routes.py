"""
FastAPI routes for PMS generation, engineering calculations, and downloads.
"""
import io
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.models.pms_models import PMSRequest, PMSResponse
from app.services.pms_service import generate_excel, generate_pms
from app.services import data_service
from app.utils.engineering import (
    check_pt_adequacy,
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


@router.post("/generate-pms", response_model=PMSResponse)
async def api_generate_pms(req: PMSRequest):
    try:
        return await generate_pms(req)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error generating PMS")
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
        corrosion_allowance=corrosion_allowance or "3 mm",
        service=service or "General",
    )
    try:
        return await generate_pms(req)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))



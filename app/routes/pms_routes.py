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
    barg_to_psig,
    celsius_to_fahrenheit,
    check_pt_adequacy,
    get_material_group,
    get_pipe_grade,
    hydrotest_pressure,
    operating_pressure_estimate,
    operating_temp_estimate,
    JOINT_TYPES,
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


@router.post("/engineering/design-conditions")
async def calculate_design_conditions(
    design_pressure: float = Query(...),
    design_temperature: float = Query(...),
    mdmt: float = Query(default=-29),
    material: str = Query(default="CS"),
    joint_type: str = Query(default="Seamless"),
):
    mat_group = get_material_group(material)
    pipe_grade = get_pipe_grade(material)
    joint_info = JOINT_TYPES.get(joint_type, JOINT_TYPES["Seamless"])

    return {
        "pressure": {
            "design_barg": design_pressure,
            "design_psig": barg_to_psig(design_pressure),
            "hydrotest_barg": hydrotest_pressure(design_pressure),
            "hydrotest_psig": barg_to_psig(hydrotest_pressure(design_pressure)),
            "operating_barg": operating_pressure_estimate(design_pressure),
            "operating_psig": barg_to_psig(operating_pressure_estimate(design_pressure)),
        },
        "temperature": {
            "design_c": design_temperature,
            "design_f": celsius_to_fahrenheit(design_temperature),
            "operating_c": operating_temp_estimate(design_temperature),
            "operating_f": celsius_to_fahrenheit(operating_temp_estimate(design_temperature)),
            "mdmt_c": mdmt,
            "mdmt_f": celsius_to_fahrenheit(mdmt),
        },
        "material": {
            "type": material,
            "group": mat_group["group"],
            "table": mat_group["table"],
            "description": mat_group["description"],
            "pipe_grade": pipe_grade,
            "is_nace": "NACE" in material.upper(),
            "is_low_temp": "LT" in material.upper() or mdmt < -29,
        },
        "joint": {
            "type": joint_type,
            "factor_E": joint_info["E"],
            "reference": joint_info["ref"],
        },
    }


@router.post("/engineering/pt-check")
async def check_pt_rating(
    design_pressure: float = Query(...),
    design_temperature: float = Query(...),
    piping_class: str = Query(...),
    material: str = Query(default="CS"),
    corrosion_allowance: str = Query(default="3 mm"),
    service: str = Query(default="General"),
):
    req = PMSRequest(
        piping_class=piping_class,
        material=material,
        corrosion_allowance=corrosion_allowance,
        service=service,
    )
    try:
        pms = await generate_pms(req)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))

    pt = pms.pressure_temperature
    result = check_pt_adequacy(design_pressure, design_temperature, pt.temperatures, pt.pressures)

    mat_group = get_material_group(material)
    result["rating"] = pms.rating
    result["standard"] = (
        f"ASME B16.5-2020 | Table: Group {mat_group['group']} ({mat_group['table']}) "
        f"| Class: {pms.rating} | Material: {material}"
    )
    return result


@router.get("/joint-types")
async def get_joint_types():
    return JOINT_TYPES

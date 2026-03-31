"""
Core PMS service — orchestrates P-T lookup from JSON and AI generation.

Flow:
  1. Look up P-T data + identifiers from embedded JSON (pipe_classes.json)
  2. Call Claude AI to generate all other fields (pipe data, fittings, flanges, etc.)
  3. Merge P-T from JSON with AI-generated data
  4. Cache the result to avoid repeated API calls
"""
import hashlib
import logging

from cachetools import TTLCache

from app.config import settings
from app.models.pms_models import (
    PMSRequest, PMSResponse, PressureTemperature,
    PipeSize, FittingsData, FittingBySize, ExtraFittings, FlangeData,
    SpectacleBlind, BoltsNutsGaskets, ValveData,
)
from app.services.ai_service import generate_pms_with_ai
from app.services.excel_generator import generate_pms_excel_bytes
from app.services import data_service

logger = logging.getLogger(__name__)

_pms_cache: TTLCache = TTLCache(maxsize=settings.cache_max_size, ttl=settings.cache_ttl)


def _cache_key(req: PMSRequest) -> str:
    raw = f"{req.piping_class}|{req.material}|{req.corrosion_allowance}|{req.service}"
    return hashlib.md5(raw.encode()).hexdigest()


def _build_pms_response(entry: dict, ai_data: dict, req: PMSRequest) -> PMSResponse:
    """Merge JSON P-T data with AI-generated fields into a PMSResponse."""
    pt_data = entry.get("pressure_temperature", {})
    pt = PressureTemperature(
        temperatures=pt_data.get("temperatures", []),
        pressures=pt_data.get("pressures", []),
        temp_labels=pt_data.get("temp_labels", []),
    )

    # Parse pipe_data from AI
    pipe_data = []
    for p in ai_data.get("pipe_data", []):
        pipe_data.append(PipeSize(
            size_inch=str(p.get("size_inch", "")),
            od_mm=float(p.get("od_mm", 0)),
            schedule=str(p.get("schedule", "")),
            wall_thickness_mm=float(p.get("wall_thickness_mm", 0)),
            pipe_type=p.get("pipe_type", "Seamless"),
            material_spec=p.get("material_spec", ""),
            ends=p.get("ends", "BE"),
        ))

    # Parse fittings from AI
    f = ai_data.get("fittings", {})
    fittings = FittingsData(
        fitting_type=f.get("fitting_type", ""),
        material_spec=f.get("material_spec", ""),
        elbow_standard=f.get("elbow_standard", ""),
        tee_standard=f.get("tee_standard", ""),
        reducer_standard=f.get("reducer_standard", ""),
        cap_standard=f.get("cap_standard", ""),
        plug_standard=f.get("plug_standard", ""),
        weldolet_spec=f.get("weldolet_spec", ""),
    )

    # Parse fittings_welded from AI
    fw = ai_data.get("fittings_welded")
    fittings_welded = None
    if fw and isinstance(fw, dict):
        fittings_welded = FittingsData(
            fitting_type=fw.get("fitting_type", ""),
            material_spec=fw.get("material_spec", ""),
            elbow_standard=fw.get("elbow_standard", ""),
            tee_standard=fw.get("tee_standard", ""),
            reducer_standard=fw.get("reducer_standard", ""),
            cap_standard=fw.get("cap_standard", ""),
            plug_standard=fw.get("plug_standard", ""),
            weldolet_spec=fw.get("weldolet_spec", ""),
        )

    # Parse fittings_by_size from AI
    fittings_by_size = []
    for fb in ai_data.get("fittings_by_size", []):
        fittings_by_size.append(FittingBySize(
            size_inch=str(fb.get("size_inch", "")),
            type=fb.get("type", ""),
            fitting_type=fb.get("fitting_type", ""),
            material_spec=fb.get("material_spec", ""),
            elbow_standard=fb.get("elbow_standard", ""),
            tee_standard=fb.get("tee_standard", ""),
            reducer_standard=fb.get("reducer_standard", ""),
            cap_standard=fb.get("cap_standard", ""),
            plug_standard=fb.get("plug_standard", ""),
            weldolet_spec=fb.get("weldolet_spec", ""),
        ))

    # Parse extra_fittings from AI
    ef = ai_data.get("extra_fittings", {})
    extra_fittings = ExtraFittings(
        coupling=ef.get("coupling", ""),
        hex_plug=ef.get("hex_plug", ""),
        union=ef.get("union", ""),
        union_large=ef.get("union_large", ""),
        olet=ef.get("olet", ""),
        olet_large=ef.get("olet_large", ""),
        swage=ef.get("swage", ""),
    )

    # Parse flange from AI
    fl = ai_data.get("flange", {})
    flange = FlangeData(
        material_spec=fl.get("material_spec", ""),
        face_type=fl.get("face_type", ""),
        flange_type=fl.get("flange_type", ""),
        standard=fl.get("standard", ""),
    )

    # Parse spectacle_blind from AI
    sb = ai_data.get("spectacle_blind", {})
    spectacle = SpectacleBlind(
        material_spec=sb.get("material_spec", ""),
        standard=sb.get("standard", ""),
    )

    # Parse bolts_nuts_gaskets from AI
    bg = ai_data.get("bolts_nuts_gaskets", {})
    bng = BoltsNutsGaskets(
        stud_bolts=bg.get("stud_bolts", ""),
        hex_nuts=bg.get("hex_nuts", ""),
        gasket=bg.get("gasket", ""),
    )

    # Parse valves from AI
    v = ai_data.get("valves", {})
    valves = ValveData(
        rating=v.get("rating", ""),
        ball=v.get("ball", ""),
        gate=v.get("gate", ""),
        globe=v.get("globe", ""),
        check=v.get("check", ""),
        butterfly=v.get("butterfly", ""),
    )

    return PMSResponse(
        piping_class=req.piping_class,
        rating=entry.get("rating", ""),
        material=req.material,
        corrosion_allowance=req.corrosion_allowance,
        mill_tolerance=ai_data.get("mill_tolerance", ""),
        design_code=ai_data.get("design_code", ""),
        service=req.service,
        branch_chart=ai_data.get("branch_chart", ""),
        hydrotest_pressure=ai_data.get("hydrotest_pressure", ""),
        pressure_temperature=pt,
        pipe_code=ai_data.get("pipe_code", ""),
        pipe_data=pipe_data,
        fittings=fittings,
        fittings_welded=fittings_welded,
        fittings_by_size=fittings_by_size,
        extra_fittings=extra_fittings,
        flange=flange,
        spectacle_blind=spectacle,
        bolts_nuts_gaskets=bng,
        valves=valves,
        notes=ai_data.get("notes", []),
    )


async def generate_pms(req: PMSRequest) -> PMSResponse:
    """
    Generate PMS:
      1. Look up P-T data from JSON by piping_class
      2. Call Claude AI to generate all other data
      3. Merge and return
    """
    key = _cache_key(req)

    if key in _pms_cache:
        logger.info("Cache hit for %s", req.piping_class)
        return _pms_cache[key]

    # Step 1: Find P-T data from JSON
    entry = data_service.find_entry(req.piping_class)
    if not entry:
        raise RuntimeError(
            f"Piping class '{req.piping_class}' not found in database. "
            "Only classes with P-T data in the system can be generated."
        )

    # Step 2: Get reference entries for AI context (same rating)
    all_entries = data_service.get_all_entries()
    rating = entry.get("rating", "")
    reference_entries = [
        e for e in all_entries
        if e.get("rating") == rating and e["piping_class"] != req.piping_class
    ][:3]
    # Add a couple from different ratings for broader context
    other_entries = [
        e for e in all_entries
        if e.get("rating") != rating
    ][:2]
    reference_entries.extend(other_entries)

    # Step 3: Call AI to generate everything except P-T
    ai_data = await generate_pms_with_ai(
        piping_class=req.piping_class,
        material=req.material,
        corrosion_allowance=req.corrosion_allowance,
        service=req.service,
        rating=rating,
        reference_entries=reference_entries,
    )

    if not ai_data:
        raise RuntimeError(
            f"AI generation failed for class '{req.piping_class}'. "
            "Please check that ANTHROPIC_API_KEY is configured correctly."
        )

    # Step 4: Merge P-T from JSON + AI-generated data
    pms = _build_pms_response(entry, ai_data, req)
    _pms_cache[key] = pms
    logger.info("Generated PMS for %s (P-T from JSON, rest from AI)", req.piping_class)
    return pms


def generate_excel(pms: PMSResponse) -> bytes:
    return generate_pms_excel_bytes(pms)

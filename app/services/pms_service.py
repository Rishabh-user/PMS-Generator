"""
Core PMS service — orchestrates P-T lookup from JSON, DB cache, and AI generation.

Flow:
  1. Check in-memory cache (L1) and PostgreSQL cache (L2)
  2. If cached, return instantly
  3. If not cached, call Claude AI to generate all fields
  4. Correct wall thickness using ASME lookup tables
  5. Store result in both DB and memory cache
  6. Regenerate endpoint bypasses cache and forces fresh AI call
"""
import logging

from app.models.pms_models import (
    PMSRequest, PMSResponse, PressureTemperature,
    PipeSize, FittingsData, FittingBySize, ExtraFittings, FlangeData,
    SpectacleBlind, BoltsNutsGaskets, ValveData, ValveSizeEntry,
)
from app.services.ai_service import generate_pms_with_ai, AIGenerationError
from app.services.branch_chart_service import get_charts_for_class
from app.services.excel_generator import generate_pms_excel_bytes
from app.services import data_service
from app.services import db_service
from app.utils.pipe_data import correct_pipe_data
from app.utils.engineering_constants import HYDROTEST_FACTOR, MILL_TOLERANCE_PERCENT

logger = logging.getLogger(__name__)

# L1 (in-memory) PMS cache — unbounded plain dict, NO TTL and NO size cap.
# Rationale per the project owner's directive: an entry should only ever
# change when the same (piping_class, material, CA, service) combination
# is re-generated — in which case `_store_in_caches` overwrites the
# existing key. It should NEVER silently disappear due to age or size
# pressure. The L2 PostgreSQL cache already has no expiry either, so
# both layers are permanent unless something explicitly deletes them
# (the Admin UI trash button, or POST /api/clear-cache).
#
# Earlier versions used cachetools.TTLCache with a 1-hour TTL + 256-entry
# cap, which is why users saw the in-memory cache appear to "empty
# itself" over time. The DB entries were never lost — but once the L1
# entry expired, the next request took the slower L2 path.
_pms_cache: dict[str, PMSResponse] = {}


def _cache_key(req: PMSRequest) -> str:
    """Cache key is the normalized piping_class.

    Previously this hashed (class, material, CA, service) into an MD5 so
    the same class with a different `service` blurb created a second row.
    The project owner explicitly wants one row per class with a bumped
    version (A0 → A1 → A2 …) on regenerate, so the key collapses to the
    uppercased, trimmed piping_class.
    """
    return req.piping_class.upper().strip()


def _determine_class_type(piping_class: str) -> str:
    """Determine the class type from the piping class name."""
    cls = piping_class.upper()
    if cls.startswith("T"):
        return "tubing"
    if cls.startswith("A30"):
        return "cuni"
    if cls.startswith("A40"):
        return "copper"
    if any(cls.startswith(pfx) for pfx in ["A50", "A51", "A52"]):
        return "gre"
    if cls.startswith("A60"):
        return "cpvc"
    if cls.startswith("A70"):
        return "titanium"
    if any(cls == pfx or cls.startswith(pfx) and len(cls) == len(pfx)
           for pfx in ["A3", "A4", "A5", "A6", "B4", "D4"]):
        return "galv_screwed"
    return "standard"


def _build_pms_response(entry: dict, ai_data: dict, req: PMSRequest) -> PMSResponse:
    """Merge JSON P-T data with AI-generated fields into a PMSResponse."""
    pt_data = entry.get("pressure_temperature", {})
    pt = PressureTemperature(
        temperatures=pt_data.get("temperatures", []),
        pressures=pt_data.get("pressures", []),
        temp_labels=pt_data.get("temp_labels", []),
    )

    pressures = pt_data.get("pressures", [])
    if pressures:
        hydrotest_str = str(round(max(pressures) * HYDROTEST_FACTOR, 2))
    else:
        hydrotest_str = ai_data.get("hydrotest_pressure", "")

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
            id_mm=float(p.get("id_mm", 0) or 0),
        ))

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
        rating=f.get("rating", ""),
    )

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
            coupling_standard=fb.get("coupling_standard", ""),
            union_standard=fb.get("union_standard", ""),
            sockolet_standard=fb.get("sockolet_standard", ""),
            nipple_standard=fb.get("nipple_standard", ""),
            swage_standard=fb.get("swage_standard", ""),
            mold_tee_standard=fb.get("mold_tee_standard", ""),
            red_saddle_standard=fb.get("red_saddle_standard", ""),
            adaptor_standard=fb.get("adaptor_standard", ""),
        ))

    # Extra Fittings intentionally emitted as empty — the section was removed
    # from the Excel output and the AI prompt. The Pydantic field is kept so
    # previously-cached PMS entries still deserialize cleanly.
    extra_fittings = ExtraFittings()

    fl = ai_data.get("flange", {})
    flange = FlangeData(
        material_spec=fl.get("material_spec", ""),
        face_type=fl.get("face_type", ""),
        flange_type=fl.get("flange_type", ""),
        standard=fl.get("standard", ""),
        compact_flange=fl.get("compact_flange", ""),
        hub_connector=fl.get("hub_connector", ""),
    )

    sb = ai_data.get("spectacle_blind", {})
    spectacle = SpectacleBlind(
        material_spec=sb.get("material_spec", ""),
        standard=sb.get("standard", ""),
        standard_large=sb.get("standard_large", ""),
    )

    bg = ai_data.get("bolts_nuts_gaskets", {})
    bng = BoltsNutsGaskets(
        stud_bolts=bg.get("stud_bolts", ""),
        hex_nuts=bg.get("hex_nuts", ""),
        gasket=bg.get("gasket", ""),
        washers=bg.get("washers", ""),
        gasket_2=bg.get("gasket_2", ""),
    )

    v = ai_data.get("valves", {})

    def _parse_valve_by_size(entries) -> list[ValveSizeEntry]:
        if not entries or not isinstance(entries, list):
            return []
        return [
            ValveSizeEntry(size_inch=str(e.get("size_inch", "")), code=e.get("code", ""))
            for e in entries if isinstance(e, dict)
        ]

    valves = ValveData(
        rating=v.get("rating", ""),
        ball=v.get("ball", ""),
        gate=v.get("gate", ""),
        globe=v.get("globe", ""),
        check=v.get("check", ""),
        butterfly=v.get("butterfly", ""),
        dbb=v.get("dbb", ""),
        dbb_inst=v.get("dbb_inst", ""),
        needle=v.get("needle", ""),
        ball_by_size=_parse_valve_by_size(v.get("ball_by_size")),
        gate_by_size=_parse_valve_by_size(v.get("gate_by_size")),
        globe_by_size=_parse_valve_by_size(v.get("globe_by_size")),
        check_by_size=_parse_valve_by_size(v.get("check_by_size")),
        butterfly_by_size=_parse_valve_by_size(v.get("butterfly_by_size")),
        dbb_by_size=_parse_valve_by_size(v.get("dbb_by_size")),
        dbb_inst_by_size=_parse_valve_by_size(v.get("dbb_inst_by_size")),
    )

    class_type = _determine_class_type(req.piping_class)

    return PMSResponse(
        piping_class=req.piping_class,
        rating=entry.get("rating", ""),
        class_type=class_type,
        material=req.material,
        corrosion_allowance=req.corrosion_allowance,
        # Mill tolerance is a fixed ASME B36.10M standard (12.5% for seamless
        # pipe) — set deterministically from engineering_constants so it never
        # depends on whether the AI remembered to emit it, and so old cached
        # entries get the value filled in on the next regenerate.
        mill_tolerance=f"{MILL_TOLERANCE_PERCENT}%",
        design_code=ai_data.get("design_code", ""),
        service=req.service,
        branch_chart=ai_data.get("branch_chart", ""),
        hydrotest_pressure=hydrotest_str,
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
        branch_charts=get_charts_for_class(req.piping_class),
        notes=ai_data.get("notes", []),
    )


async def _generate_from_ai(req: PMSRequest) -> PMSResponse:
    """Core AI generation logic — shared by generate_pms and regenerate_pms."""
    # Find P-T data from JSON
    entry = data_service.find_entry(req.piping_class)
    if not entry:
        raise RuntimeError(
            f"Piping class '{req.piping_class}' not found in database. "
            "Only classes with P-T data in the system can be generated."
        )

    # Get reference entries for AI context
    all_entries = data_service.get_all_entries()
    rating = entry.get("rating", "")
    reference_entries = [
        e for e in all_entries
        if e.get("rating") == rating and e["piping_class"] != req.piping_class
    ][:3]
    other_entries = [
        e for e in all_entries
        if e.get("rating") != rating
    ][:2]
    reference_entries.extend(other_entries)

    # Call AI to generate everything except P-T.
    # generate_pms_with_ai raises AIGenerationError with a specific reason on
    # failure — we re-raise as RuntimeError so the route handler turns it
    # into a 422 with the exact cause (credit balance, rate limit, etc.).
    try:
        ai_data = await generate_pms_with_ai(
            piping_class=req.piping_class,
            material=req.material,
            corrosion_allowance=req.corrosion_allowance,
            service=req.service,
            rating=rating,
            reference_entries=reference_entries,
        )
    except AIGenerationError as e:
        raise RuntimeError(
            f"Unable to generate PMS for class '{req.piping_class}': {e}"
        ) from e

    if not ai_data:
        raise RuntimeError(
            f"AI returned no data for class '{req.piping_class}'. "
            "Try regenerating, or contact support if the issue persists."
        )

    # Correct OD and wall thickness values.
    #   - ASME-coded classes with standard schedules: WT/OD replaced from
    #     ASME B36.10M / B36.19M lookup tables.
    #   - ASME-coded classes with Schedule "-": WT is COMPUTED per ASME
    #     B31.3 §304.1.2 Eq. 3a using the class's design P/T envelope (max
    #     pressure/temperature from pipe_classes.json) and the request's
    #     material / CA. OD is replaced from the OD table.
    #   - Non-ASME pipe codes (CuNi, Copper, GRE, CPVC, Tubing): untouched.
    if "pipe_data" in ai_data:
        pt_data = entry.get("pressure_temperature", {}) or {}
        pressures = pt_data.get("pressures") or []
        temperatures = pt_data.get("temperatures") or []
        design_pressure = max(pressures) if pressures else None
        design_temp = max(temperatures) if temperatures else None
        material_for_correction = req.material or entry.get("material", "")
        correct_pipe_data(
            ai_data["pipe_data"],
            pipe_code=ai_data.get("pipe_code", ""),
            material=material_for_correction,
            design_pressure_barg=design_pressure,
            design_temp_c=design_temp,
            corrosion_allowance=req.corrosion_allowance,
        )
        logger.info(
            "Corrected pipe_data for %s (pipe_code='%s', material='%s', "
            "P=%s barg, T=%s°C, CA=%s) — WT/OD from ASME tables where "
            "schedule is a standard code; WT computed per B31.3 Eq. 3a "
            "where schedule is '-'",
            req.piping_class, ai_data.get("pipe_code", ""),
            material_for_correction, design_pressure, design_temp,
            req.corrosion_allowance,
        )

    # Merge P-T from JSON + AI-generated data
    pms = _build_pms_response(entry, ai_data, req)
    logger.info("Generated PMS for %s via AI (P-T from JSON, rest from AI)", req.piping_class)
    return pms


async def _store_in_caches(key: str, req: PMSRequest, pms: PMSResponse):
    """Store PMS in both in-memory cache and PostgreSQL. The DB layer
    upserts by piping_class and bumps `version` on each write, so the
    L2 row for a class is overwritten in place rather than duplicated.
    The returned version string is written back onto the PMSResponse
    so the frontend + Excel header can show the current revision."""
    if db_service.is_available():
        version = await db_service.store_pms(
            piping_class=key,
            material=req.material,
            corrosion_allowance=req.corrosion_allowance,
            service=req.service,
            response=pms.model_dump(),
        )
        if version:
            pms.version = version
    _pms_cache[key] = pms


async def generate_pms(req: PMSRequest) -> PMSResponse:
    """
    Generate PMS with layered caching:
      L1: In-memory dict (fast, process-scoped, NO TTL or eviction)
      L2: PostgreSQL (persistent across restarts, NO TTL)
      L3: Claude AI (expensive, only if not cached)

    Both cache layers are write-through on regenerate — calling
    regenerate_pms() overwrites the existing L1 entry for this class and
    bumps the L2 `version` column (A0 → A1 → A2 …) via UPSERT, so the
    table never holds two rows for the same piping_class. The only way
    an entry "disappears" is:
      * The Admin UI delete button / /api/admin/db/pms-cache/{piping_class}
      * POST /api/clear-cache (nukes both L1 and L2)
      * Manual SQL DELETE on the pms_cache table
    """
    key = _cache_key(req)

    # L1: In-memory cache
    if key in _pms_cache:
        logger.info("L1 memory cache HIT for %s (key=%s)", req.piping_class, key)
        return _pms_cache[key]

    # L2: PostgreSQL cache
    if db_service.is_available():
        cached = await db_service.get_cached_pms(key)
        if cached:
            # A row exists for this class — honour the "don't regenerate
            # unless user explicitly asks" contract. If the stored payload
            # fails to deserialize against the current PMSResponse schema
            # (rare — e.g. a required field was renamed after the row was
            # written), log LOUDLY and fall through to AI rather than
            # silently return a corrupt object. Regenerate will overwrite
            # the row and self-heal.
            try:
                pms = PMSResponse(**cached)
            except Exception as e:
                logger.warning(
                    "L2 DB row for %s failed to deserialize (%s) — falling "
                    "back to AI. Row will be overwritten on next store.",
                    req.piping_class, e,
                )
            else:
                logger.info(
                    "L2 database cache HIT for %s (key=%s, version=%s)",
                    req.piping_class, key, cached.get("version", "?"),
                )
                _pms_cache[key] = pms  # Promote to L1
                return pms
        else:
            logger.info(
                "L2 database cache MISS for %s (key=%s) — no row found",
                req.piping_class, key,
            )
    else:
        logger.info("L2 database disabled — skipping cache check")

    # L3: AI generation (only reached when BOTH caches missed)
    logger.info("Generating %s via AI (cache miss)", req.piping_class)
    pms = await _generate_from_ai(req)
    await _store_in_caches(key, req, pms)
    return pms


async def regenerate_pms(req: PMSRequest) -> PMSResponse:
    """Force fresh AI generation, bypassing all caches. Overwrites cache."""
    key = _cache_key(req)
    logger.info("Regenerating PMS for %s via AI (forced, bypassing cache)", req.piping_class)
    pms = await _generate_from_ai(req)
    await _store_in_caches(key, req, pms)
    return pms


async def clear_cache():
    """Clear both in-memory and database caches."""
    _pms_cache.clear()
    count = 0
    if db_service.is_available():
        count = await db_service.clear_all_cache()
    logger.info("Cache cleared — memory + %d DB entries", count)


def generate_excel(pms: PMSResponse) -> bytes:
    return generate_pms_excel_bytes(pms)

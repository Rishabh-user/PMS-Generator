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
    PipeSize, FittingsData, FittingBySize, FlangeData,
    SpectacleBlind, BoltsNutsGaskets, ValveData, ValveSizeEntry,
)
from app.services.ai_service import generate_pms_with_ai, AIGenerationError
from app.services.branch_chart_service import get_charts_for_class
from app.services.excel_generator import generate_pms_excel_bytes
from app.services.tubing_service import build_tubing_pms, is_tubing_class
from app.services import data_service
from app.services import db_service
from app.services import valvesheet_sync_service
from app.utils.pipe_data import correct_pipe_data
from app.utils.engineering_constants import HYDROTEST_FACTOR, MILL_TOLERANCE_PERCENT
from app.utils.engineering import hydrotest_pressure_corrected

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


# Classes whose pipe-size table extends past NPS 24 (B16.5's upper bound).
# For these classes the flange-Standard cell must cite both B16.5 (for sizes
# ≤24") AND B16.47A (for sizes ≥26") because a single B16.5 reference is
# wrong above 24". Source-of-truth list comes from the AI prompt's "PIPE
# SIZES — STANDARD NPS RANGES" block; cross-check before adding entries.
# A50/A52/A51 (GRE) handle their own dimensional reference string in the
# AI prompt and are deliberately excluded here.
_FLANGE_NEEDS_B1647_DUAL: frozenset[str] = frozenset({
    "A1", "A1N",     # CS 150# — sizes to 36"
    "A1L", "A1LN",   # LTCS 150# — sizes to 30"
    "A2N", "A2LN",   # CS NACE 150# — sizes to 30"
    "A20", "A20N",   # DSS 150# — sizes to 32"
    "B20",           # DSS 300# — sizes to 32"
    "B25",           # SDSS 300# — sizes to 32"
    "A30",           # CuNi EEMUA 234 — sizes to 28"
})


# Classes whose flange MOC must be A105N (B16.5 Group 1.1 forging), not the
# B16.47 pipeline-flange material A694 F60. Earlier prompt revisions told
# the AI to use A694 F60 for all 1500#/2500# CS forgings; that's wrong
# because none of those classes exceeds NPS 24, so they live entirely
# inside B16.5 territory where Table 1A specifies A105 / A350 LF2 / A182 F1.
# A694 F60 still belongs to the hub_connector row (which is a B16.47-style
# component) — that field is not touched by this enforcer.
_FLANGE_FORCE_A105N: frozenset[str] = frozenset({
    "F1", "G1",       # 1500# / 2500# CS — to 24"
    "F2N", "G2N",     # 1500# / 2500# CS NACE — to 24"
})


def _enforce_flange_material(piping_class: str, ai_value: str) -> str:
    """Override A694 F60 with A105N for 1500#/2500# CS classes ≤24".

    Same defensive pattern as `_enforce_flange_standard`: we only
    intervene when the AI emitted exactly the wrong material for one
    of the affected classes. Any other string (custom MOC, project
    override, NACE qualifier added by the AI) passes through.
    """
    cls = (piping_class or "").upper().strip()
    value = (ai_value or "").strip()
    if cls not in _FLANGE_FORCE_A105N:
        return value
    if "A 694" in value.upper() or "A694" in value.upper():
        upgraded = "ASTM A 105N"
        logger.info(
            "Flange.material_spec for %s upgraded from %r to %r "
            "(B16.5 Table 1A Group 1.1 — A694 is a B16.47 material)",
            cls, value, upgraded,
        )
        return upgraded
    return value


def _enforce_flange_standard(piping_class: str, ai_value: str) -> str:
    """Make sure the flange.standard string is correct for the class.

    The AI prompt now teaches the dual-citation rule for classes that
    span past NPS 24, but model output drifts and a regenerated cache
    entry from before the prompt update can still carry a bare
    "ASME B16.5". This is a last-line server-side enforcer: if the
    class is on the >24" list AND the AI's emitted value doesn't
    already mention B16.47, upgrade the string to the dual citation.

    We only intervene when the AI value is empty OR cites only B16.5
    without B16.47 — anything else (GRE classes, manufacturer-standard
    strings, edge cases) passes through untouched so the AI's intent
    isn't second-guessed.
    """
    cls = (piping_class or "").upper().strip()
    value = (ai_value or "").strip()
    if cls not in _FLANGE_NEEDS_B1647_DUAL:
        return value
    # Already cites B16.47 (in any form) → trust it.
    norm = value.upper().replace(" ", "")
    if "16.47" in norm or "B1647" in norm:
        return value
    # Empty / bare B16.5 / something we don't recognise → upgrade.
    upgraded = "ASME B16.5 / B16.47A"
    if value:
        logger.info(
            "Flange.standard for %s upgraded from %r to %r "
            "(class spans >24\"; B16.47A required per B16.5 §1.1)",
            cls, value, upgraded,
        )
    return upgraded


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

    # Hydrotest pressure per ASME B31.3 §345.4.2(b) — Eq. 24:
    #     P_T = 1.5 × P × (S_T / S_design)
    # When the line runs hot, the steel is weaker at the design
    # temperature than at the test (≈ ambient) temperature, so the cold
    # hydrotest must be *higher* than 1.5·P to prove the line can carry
    # the rated pressure once it heats up. The previous flat 1.5·P
    # under-tested every high-temperature class. Catalogue's max(P) is
    # the design pressure; max(T) (highest column with a non-zero P) is
    # the design temperature feeding the S(T) lookup.
    pressures = pt_data.get("pressures", [])
    temperatures = pt_data.get("temperatures", [])
    is_ai_only = bool(entry.get("_ai_only"))

    # AI-only path: no indexed standards data, so there's no rated-P
    # ceiling to validate against. The user MUST supply design P/T in
    # the request (the frontend AI-only panel makes them required). We
    # use those values directly for the §345.4.2(b) hydrotest correction
    # and skip the over-rating guard (no ceiling exists to compare to).
    if is_ai_only:
        if req.design_pressure_barg is None or req.design_temp_c is None:
            raise RuntimeError(
                f"AI-only generation for class {req.piping_class!r} requires "
                f"explicit design_pressure_barg and design_temp_c in the "
                f"request — no standards-indexed P-T table is available to "
                f"derive a default design point."
            )
        # Hydrotest = 1.5 × user's design_pressure_barg, FLAT (consistent
        # with the class-documentation convention used elsewhere). For
        # AI-only there's no curve to read max(P) from, so the user's
        # supplied design_pressure_barg IS the class envelope. Pass
        # design_temp_c=38°C so the §345.4.2(b) correction collapses to
        # 1.0 and the multiplier is a clean 1.5×.
        from app.utils.engineering import HYDROTEST_TEST_TEMP_C
        ht = hydrotest_pressure_corrected(
            design_pressure=req.design_pressure_barg,
            design_temp_c=HYDROTEST_TEST_TEMP_C,
            material_spec=entry.get("material") or req.material or "",
        )
        hydrotest_str = str(ht["pressure_barg"])
        if ht.get("correction_applied"):
            logger.info(
                "Hydrotest §345.4.2(b) [AI-only] for %s: %s barg "
                "(flat 1.5·P would be %.2f, S_T/S=%.3f at T=%s°C)",
                req.piping_class, hydrotest_str,
                round(req.design_pressure_barg * HYDROTEST_FACTOR, 2),
                ht["ratio_st_over_s"], req.design_temp_c,
            )
    elif pressures:
        # Standards / catalogued path — has a P-T table to reason about.
        rated_temps = [
            t for t, p in zip(temperatures, pressures)
            if (p or 0) > 0 and t is not None
        ]
        ceiling_p = max(pressures)
        ceiling_t = max(rated_temps) if rated_temps else (max(temperatures) if temperatures else 0)

        # Determine the design point used by the OVER-RATING GUARD.
        # User-supplied design conditions (from the Custom-Class panel)
        # take precedence; otherwise fall back to the P-T-table ceiling.
        design_p = req.design_pressure_barg if req.design_pressure_barg is not None else ceiling_p
        design_t = req.design_temp_c if req.design_temp_c is not None else ceiling_t

        # Over-rating guard: refuse design conditions above the rated
        # allowable pressure at the design temperature. Gated on the user
        # having supplied an explicit design point so legacy catalogue
        # requests with no overrides aren't affected.
        if (req.design_pressure_barg is not None
                or req.design_temp_c is not None):
            from app.utils.engineering import interpolate_pressure_at_temp
            rated_at_design_t = interpolate_pressure_at_temp(
                temperatures, pressures, design_t,
            )
            if design_p > rated_at_design_t * 1.001:  # 0.1% tolerance for FP
                raise RuntimeError(
                    f"Design pressure {design_p} barg exceeds the rated "
                    f"allowable pressure {rated_at_design_t} barg at "
                    f"{design_t}°C for class {req.piping_class}. "
                    f"Pick a lower design pressure or a higher rating."
                )

        # Hydrotest = 1.5 × cold-rated, FLAT (project class-documentation
        # convention). Independent of user design point so the value
        # printed on the PMS spec sheet describes the CLASS envelope, not
        # a specific line. We pass design_temp_c=38°C (test temperature)
        # so the §345.4.2(b) S_T/S correction collapses to 1.0 — the
        # multiplier becomes a clean 1.5×. The over-rating guard above
        # already protects against unsafe design points; this hydrotest
        # value is always ≥ §345.4.2(a) flat 1.5·P_design because
        # ceiling_p ≥ any allowed design_p by construction.
        from app.utils.engineering import HYDROTEST_TEST_TEMP_C
        ht = hydrotest_pressure_corrected(
            design_pressure=ceiling_p,
            design_temp_c=HYDROTEST_TEST_TEMP_C,
            material_spec=entry.get("material") or req.material or "",
        )
        hydrotest_str = str(ht["pressure_barg"])
        if ht.get("correction_applied"):
            logger.info(
                "Hydrotest §345.4.2(b) correction for %s: %s barg "
                "(flat 1.5·P would be %.2f, S_T/S=%.3f at T=%s°C)",
                req.piping_class,
                hydrotest_str,
                round(design_p * HYDROTEST_FACTOR, 2),
                ht["ratio_st_over_s"],
                design_t,
            )
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

    fl = ai_data.get("flange", {})
    flange = FlangeData(
        material_spec=_enforce_flange_material(req.piping_class, fl.get("material_spec", "")),
        face_type=fl.get("face_type", ""),
        flange_type=fl.get("flange_type", ""),
        standard=_enforce_flange_standard(req.piping_class, fl.get("standard", "")),
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

    # Decide how this PMS was produced. The flags on `entry` are set by
    # the catalogue-miss fallbacks in `_generate_from_ai`:
    #   • catalogued lookup       → no `_derived` flag
    #   • derive_synthetic_entry  → `_derived=True`, `_ai_only=False`
    #   • derive_synthetic_entry_ai_only → `_derived=True`, `_ai_only=True`
    if entry.get("_ai_only"):
        generation_mode = "ai_only"
    elif entry.get("_derived"):
        generation_mode = "standards_derived"
    else:
        generation_mode = "catalogue"

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
        flange=flange,
        spectacle_blind=spectacle,
        bolts_nuts_gaskets=bng,
        valves=valves,
        branch_charts=get_charts_for_class(req.piping_class),
        notes=ai_data.get("notes", []),
        generation_mode=generation_mode,
    )


async def _generate_from_ai(req: PMSRequest) -> PMSResponse:
    """Core AI generation logic — shared by generate_pms and regenerate_pms.

    Two paths:
      1. Catalogue hit  → use the curated `pipe_classes.json` entry
         (existing behaviour for the 91 catalogued classes).
      2. Catalogue miss → derive a synthetic entry from §5.5 + B16.5 via
         `class_derivation.derive_synthetic_entry()`. The derived entry
         has the same shape as a catalogue entry, so the rest of this
         function and the AI generation path don't care which way the
         data came from.

    The catalogue is the fast path AND the source of truth for the
    91 hand-curated classes. Derivation only kicks in when the user
    explicitly asks for a class that isn't in the catalogue (typically
    after the frontend's opt-in prompt: "this combination isn't
    catalogued — generate from standards?").
    """
    # Find P-T data from JSON catalogue first
    entry = data_service.find_entry(req.piping_class)

    # Catalogue miss — try standards-derivation, then AI-only fallback.
    if not entry:
        try:
            from app.services.class_derivation import (
                derive_synthetic_entry, derive_synthetic_entry_ai_only,
                rating_from_class_code, DerivationError,
            )
            # PMSRequest doesn't carry an explicit `rating` field — it
            # was historically derived from the catalogue entry. For
            # uncatalogued classes, reverse-derive the rating from the
            # class code's §5.5 first letter (A→150#, B→300#, F→1500#,…).
            # If the class code isn't a §5.5 letter, derivation fails
            # cleanly with the DerivationError below.
            rating = rating_from_class_code(req.piping_class)
            if not rating:
                from app.services import rating_lookup
                valid_letters = "/".join(rating_lookup.all_letters())
                raise DerivationError(
                    f"Class code {req.piping_class!r} doesn't start with a "
                    f"§5.5 rating letter ({valid_letters}). Cannot derive "
                    f"the rating; please use a valid §5.5 class code."
                )
            # Try standards-derivation first (Slice 1 fast path with full
            # B16.5 P-T data). If that's not available for this combo
            # (e.g. 5000#/J-series, SS316L pre-Slice-2, etc.), fall back
            # to AI-only mode — the §5.5 class code is still valid, the
            # AI just has to fill in the P-T from its training data
            # rather than from indexed standards. We mark the response
            # as `_ai_only` so the frontend can show the user a clear
            # "this output isn't standards-verified" notice.
            try:
                entry = derive_synthetic_entry(
                    rating=rating,
                    material=req.material,
                    corrosion_allowance=req.corrosion_allowance,
                    service=req.service,
                )
            except DerivationError as standards_gap:
                logger.info(
                    "Standards data missing for %s (rating=%s material=%s "
                    "CA=%s) — falling back to AI-only generation. Reason: %s",
                    req.piping_class, rating, req.material,
                    req.corrosion_allowance, standards_gap,
                )
                entry = derive_synthetic_entry_ai_only(
                    rating=rating,
                    material=req.material,
                    corrosion_allowance=req.corrosion_allowance,
                    service=req.service,
                )
            # Sanity-check: the user-supplied class code should match what
            # §5.5 derives from the inputs. If not, the request is internally
            # inconsistent (e.g. user said class=A2 but inputs say A1) — we
            # return what the inputs derive, so downstream code uses the
            # correctly-named class. The frontend should be syncing inputs
            # → class code BEFORE calling this endpoint.
            if entry["piping_class"] != (req.piping_class or "").upper().strip():
                logger.info(
                    "Class %s not in catalogue; derived %s from §5.5 inputs "
                    "(rating=%s material=%s CA=%s)",
                    req.piping_class, entry["piping_class"],
                    req.rating, req.material, req.corrosion_allowance,
                )
                # Update the request's class code to the derived one so the
                # downstream cache key + Excel filename match.
                req.piping_class = entry["piping_class"]
        except DerivationError as e:
            raise RuntimeError(
                f"Piping class '{req.piping_class}' is not in the catalogue, "
                f"and cannot be derived from §5.5 / B16.5 standards: {e}"
            ) from e
        except Exception as e:  # noqa: BLE001 — defensive
            raise RuntimeError(
                f"Piping class '{req.piping_class}' not found in database, "
                f"and standards-derivation failed: {e}"
            ) from e

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
    # Build pipe_data deterministically from class_metadata.json — replaces
    # whatever the AI emitted (and overrides pipe_code from the metadata
    # too). For ASME classes, the rows from build_pipe_data_rows() have
    # blank OD/SCH/WT; correct_pipe_data fills those via dual-case Eq. 3a.
    # For non-ASME (CuNi/Copper/GRE/CPVC) the builder fills everything;
    # correct_pipe_data is a no-op for those (non-ASME pipe_code).
    from app.services.pipe_data_builder import (
        build_pipe_data_rows,
        get_class_pipe_code,
    )
    try:
        ai_data["pipe_data"] = build_pipe_data_rows(req.piping_class)
        meta_pipe_code = get_class_pipe_code(req.piping_class)
        if meta_pipe_code:
            ai_data["pipe_code"] = meta_pipe_code
        logger.info(
            "Built pipe_data deterministically for %s — %d rows from class_metadata.json",
            req.piping_class, len(ai_data["pipe_data"]),
        )
    except KeyError:
        # Class isn't in class_metadata.json (e.g. tubing T80*/T90* — handled
        # by tubing_service before we reach here, OR a custom uncatalogued
        # class). Fall back to whatever AI emitted.
        logger.info(
            "Class %s not in class_metadata.json — using AI-emitted pipe_data",
            req.piping_class,
        )

    if "pipe_data" in ai_data:
        pt_data = entry.get("pressure_temperature", {}) or {}
        pressures = pt_data.get("pressures") or []
        temperatures = pt_data.get("temperatures") or []
        material_for_correction = req.material or entry.get("material", "")
        correct_pipe_data(
            ai_data["pipe_data"],
            pipe_code=ai_data.get("pipe_code", ""),
            material=material_for_correction,
            pt_pressures=pressures,
            pt_temperatures=temperatures,
            design_pressure_barg=req.design_pressure_barg,
            design_temp_c=req.design_temp_c,
            corrosion_allowance=req.corrosion_allowance,
            piping_class=req.piping_class,
        )
        logger.info(
            "Corrected pipe_data for %s (pipe_code='%s', material='%s', "
            "design=req(P=%s barg, T=%s°C), CA=%s) — dual-case Eq. 3a + project floor",
            req.piping_class, ai_data.get("pipe_code", ""),
            material_for_correction,
            req.design_pressure_barg, req.design_temp_c,
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
    so the frontend + Excel header can show the current revision.

    After a successful DB write, mirror the payload to the external
    SPE Valvesheet backend — POST for a new row (version A0), PUT for
    a regeneration (A1+). The sync runs as a background task so the
    user's response isn't delayed by the downstream HTTP call, and all
    errors are swallowed+logged (a flaky mirror must not break local
    generation)."""
    synced_version: str | None = None
    if db_service.is_available():
        synced_version = await db_service.store_pms(
            piping_class=key,
            material=req.material,
            corrosion_allowance=req.corrosion_allowance,
            service=req.service,
            response=pms.model_dump(),
            # Tag the row with how this PMS was produced so the admin DB
            # browser can badge derived / AI-only entries differently
            # from catalogued ones. Default falls through to 'catalogue'
            # if pms.generation_mode isn't set (older response objects).
            generation_mode=getattr(pms, "generation_mode", "catalogue"),
        )
        if synced_version:
            pms.version = synced_version
    _pms_cache[key] = pms

    # Mirror to the external valvesheet backend. Treat anything other
    # than exactly "A0" as an update — the UPSERT returns A0 only on
    # the very first insert, so this cleanly distinguishes POST vs PUT
    # even when the caller (regenerate_pms) forced a bump.
    if synced_version is not None:
        is_regenerate = synced_version != "A0"
        valvesheet_sync_service.sync_in_background(pms, is_regenerate=is_regenerate)


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
    # ── Tubing classes (T80A/B/C, T90A/B/C) bypass the AI entirely ──
    # Tubing specs are fully deterministic per the project sheet workbook
    # and don't have any flexibility the AI could add value to. We build
    # the response from app/data/tubing_specs.json instead. Still cache
    # it so subsequent requests hit L1 directly.
    key = _cache_key(req)
    if is_tubing_class(req.piping_class):
        if key in _pms_cache:
            logger.info("L1 memory cache HIT for %s (tubing)", req.piping_class)
            return _pms_cache[key]
        logger.info("Building %s deterministically from tubing_specs.json", req.piping_class)
        pms = build_tubing_pms(req)
        _pms_cache[key] = pms
        # Note: tubing classes are NOT persisted to the L2 Postgres cache
        # because the JSON file IS the source of truth — re-deploys with
        # an updated spec sheet should pick up the new values immediately,
        # not be shadowed by stale DB rows.
        return pms

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
    """Force fresh AI generation, bypassing all caches. Overwrites cache.

    Tubing classes (T80A/B/C, T90A/B/C) are still deterministic — Regenerate
    just refreshes the L1 entry from tubing_specs.json (in case the file
    was edited at runtime).
    """
    key = _cache_key(req)
    if is_tubing_class(req.piping_class):
        logger.info("Regenerating %s deterministically (tubing — refresh from JSON)", req.piping_class)
        pms = build_tubing_pms(req)
        _pms_cache[key] = pms
        return pms

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

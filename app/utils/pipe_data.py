"""
Small engineering helpers used by pms_service + validation_service.

OD and WT values for ASME B36.10M / B36.19M pipe classes are looked up from
the tables in engineering_constants.py after AI generation — the AI picks
the Schedule per class-specific prompt rules, and this module overwrites
the AI's (potentially hallucinated) wall thickness with the authoritative
standard value.

For non-ASME systems (CuNi EEMUA 234, Copper ASTM B42, GRE manufacturer
std, CPVC ASTM F441, Tubing ASTM A269), correct_pipe_data is a pass-through
and the AI's emitted values stand.
"""
from __future__ import annotations

import logging
import re

from app.utils.engineering_constants import (
    lookup_od,
    lookup_wall_thickness,
)

logger = logging.getLogger(__name__)


def _parse_corrosion_allowance_mm(ca: str | float | int | None) -> float:
    """Parse a CA string like '3 mm' or 'NIL' to a float in mm."""
    if ca is None:
        return 0.0
    if isinstance(ca, (int, float)):
        return float(ca)
    m = re.search(r"([\d.]+)", str(ca))
    return float(m.group(1)) if m else 0.0


def calculate_wall_thickness_mm(
    od_mm: float,
    design_pressure_barg: float,
    design_temp_c: float,
    material_spec: str,
    corrosion_allowance_mm: float,
    joint_factor: float = 1.0,
) -> float | None:
    """Minimum required wall thickness per ASME B31.3 §304.1.2 Eq. 3a.

        t      = (P × OD) / (2 × (S × E × W + P × Y))
        t_m    = t + c
        t_min  = t_m / (1 − mill_tolerance)

    Returns t_min in mm (rounded to 2 decimals), or None if required inputs
    are missing. Used by the validator's pressure-adequacy check — NOT used
    to populate the PMS output (the AI does that).
    """
    if not od_mm or not design_pressure_barg or design_pressure_barg <= 0:
        return None
    try:
        from app.utils.engineering import calculate_wall_thickness
        from app.utils.engineering_constants import get_allowable_stress

        stress = get_allowable_stress(material_spec or "", design_temp_c)
        result = calculate_wall_thickness(
            od_mm=od_mm,
            design_pressure_barg=design_pressure_barg,
            allowable_stress_mpa=stress["S_mpa"],
            joint_factor=joint_factor,
            corrosion_allowance_mm=corrosion_allowance_mm,
        )
        return round(result["t_minimum_mm"], 2)
    except Exception as e:
        logger.warning(
            "WT calc failed for OD=%s P=%s T=%s: %s",
            od_mm, design_pressure_barg, design_temp_c, e,
        )
        return None


def _is_calc_schedule(schedule) -> bool:
    """True when `schedule` indicates 'no schedule — calculate WT'.
    Matches plain hyphens, em-dashes, and empty strings (the AI sometimes
    omits the field entirely for calc-WT sizes)."""
    s = str(schedule or "").strip()
    return s in ("", "-", "--", "—", "— ")


def _round2(x) -> float | None:
    """Round a numeric value to 2 decimals. Returns None if x isn't a
    finite number. Used as the final normalization so every od_mm /
    wall_thickness_mm in the PMS response has consistent 2-decimal
    precision regardless of origin (lookup table, B31.3 calc, AI
    pass-through for non-ASME classes)."""
    try:
        val = float(x)
    except (TypeError, ValueError):
        return None
    # NaN / inf guard
    if val != val or val in (float("inf"), float("-inf")):
        return None
    return round(val, 2)


def correct_pipe_data(
    pipe_data: list[dict],
    pipe_code: str | None = None,
    material: str | None = None,
    design_pressure_barg: float | None = None,
    design_temp_c: float | None = None,
    corrosion_allowance: str | float | None = None,
    **_unused,
) -> list[dict]:
    """Post-process AI-generated pipe rows.

    Three code paths, picked per-row:

      1. ASME class + standard Schedule (e.g. "SCH 160", "80S", "STD", "XS"):
         od_mm and wall_thickness_mm are replaced with authoritative values
         from the ASME B36.10M / B36.19M tables in engineering_constants.

      2. ASME class + Schedule == "-" (calculated WT, e.g. F1LN 10-24",
         G2N 1-24"): od_mm is replaced from the OD table and
         wall_thickness_mm is COMPUTED per ASME B31.3 §304.1.2 Eq. 3a using
         the class's design pressure, design temperature, material stress,
         and corrosion allowance. Requires `design_pressure_barg`,
         `design_temp_c`, `material`, and `corrosion_allowance` to be
         provided by the caller (pms_service passes them from
         pipe_classes.json P-T data + the request).

      3. Non-ASME pipe code (CuNi EEMUA 234, Copper ASTM B42, GRE
         manufacturer std, CPVC ASTM F441, Tubing ASTM A269): row is left
         untouched — the AI's emitted values stand.

    FINAL PASS — every row has both od_mm and wall_thickness_mm rounded
    to 2 decimal places before returning, regardless of source. This
    guarantees clean engineering-spec output even when a value arrives
    via the AI-pass-through path (non-ASME classes) or when the calc
    path falls through due to missing context.
    """
    ca_mm = _parse_corrosion_allowance_mm(corrosion_allowance)

    for row in pipe_data:
        nps = row.get("size_inch") or row.get("nps")
        schedule = row.get("schedule")

        # OD correction (ASME-only)
        od = lookup_od(nps, pipe_code=pipe_code)
        if od is not None:
            row["od_mm"] = od

        # WT correction — standard-schedule lookup first
        wt = lookup_wall_thickness(nps, schedule, pipe_code=pipe_code)
        if wt is not None:
            row["wall_thickness_mm"] = wt
            continue

        # Calculated-WT path: "-" schedule on an ASME class.
        # Only runs when (a) we have an OD for this NPS (standard ASME size),
        # (b) schedule explicitly says "-", and (c) the caller gave us P/T/
        # material so we can evaluate Eq. 3a. If any piece is missing, fall
        # through and leave the AI's value (still normalized to 2 decimals
        # below).
        if (od is not None
                and _is_calc_schedule(schedule)
                and design_pressure_barg is not None
                and design_temp_c is not None
                and material):
            computed = calculate_wall_thickness_mm(
                od_mm=od,
                design_pressure_barg=design_pressure_barg,
                design_temp_c=design_temp_c,
                material_spec=material,
                corrosion_allowance_mm=ca_mm,
                joint_factor=1.0,
            )
            if computed is not None and computed > 0:
                row["wall_thickness_mm"] = computed

    # ── Final normalization: every numeric cell to 2 decimals ──
    # Covers: AI-emitted values on non-ASME classes (CuNi/Copper/GRE/CPVC/
    # Tubing), calc-path fallbacks when context was missing, and any stray
    # float noise. Matches the Calculated-Thickness precision used by the
    # /PMS_generator UI so downstream consumers see consistent output.
    for row in pipe_data:
        rounded_od = _round2(row.get("od_mm"))
        if rounded_od is not None:
            row["od_mm"] = rounded_od
        rounded_wt = _round2(row.get("wall_thickness_mm"))
        if rounded_wt is not None:
            row["wall_thickness_mm"] = rounded_wt

    return pipe_data

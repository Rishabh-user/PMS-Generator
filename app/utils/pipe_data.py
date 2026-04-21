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


def correct_pipe_data(
    pipe_data: list[dict],
    pipe_code: str | None = None,
    **_unused,
) -> list[dict]:
    """Post-process AI-generated pipe rows.

    For each row, if the class uses ASME B36.10M or B36.19M (standard steel
    pipe), replace:
      - od_mm              with the standard OD for that NPS
      - wall_thickness_mm  with the standard WT for (NPS, Schedule) —
                           only when Schedule maps to a known code.

    Rows with Schedule="-" (calculated WT), non-matching NPS, or non-ASME
    pipe codes (CuNi / Copper / GRE / CPVC / Tubing) are left untouched so
    the AI-generated value stands.

    Extra **kwargs are accepted (and ignored) for backwards compatibility
    with the previous signature that took material / pressure / temp args.
    """
    for row in pipe_data:
        nps = row.get("size_inch") or row.get("nps")

        od = lookup_od(nps, pipe_code=pipe_code)
        if od is not None:
            row["od_mm"] = od

        wt = lookup_wall_thickness(nps, row.get("schedule"), pipe_code=pipe_code)
        if wt is not None:
            row["wall_thickness_mm"] = wt

    return pipe_data

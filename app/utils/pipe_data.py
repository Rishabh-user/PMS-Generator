"""Pipe-data post-processor.

Runs after the AI emits each pipe row and corrects the row against
authoritative ASME data sourced from `pipe_dimensions.json`:

  • OD                — looked up by NPS, overwrites whatever the AI emitted
  • Wall thickness    — computed via ASME B31.3 §304.1.2 Eq. 3a using the
                        line's design P / design T / material / CA
  • Schedule          — picked as the smallest standard B36.10M schedule
                        whose nominal wall meets the calculated t_min

Order of operations per row (ASME pipe codes only):
  1. Replace `od_mm` from `lookup_od()` (single source of truth).
  2. Compute Eq. 3a `t_min_mm` using design conditions.
  3. Call `schedule_selector.select_schedule_for_thickness(nps, t_min_mm)` to
     pick the smallest schedule whose nominal WT ≥ t_min_mm.
  4. Write `schedule` and `wall_thickness_mm` from that pick.

Non-ASME pipe codes (CuNi EEMUA 234, Copper ASTM B42, GRE manufacturer
std, CPVC ASTM F441, Tubing ASTM A269) are passed through untouched —
their dimensions come from per-standard tables baked into the AI prompt.

When design context is incomplete (no P / no T / no material), the row's
OD is corrected but the schedule + wall thickness pass through unchanged.
"""
from __future__ import annotations

import logging
import re

from app.utils.engineering_constants import lookup_od

logger = logging.getLogger(__name__)


def _round2(x) -> float | None:
    """Round to 2 decimals; None if not finite."""
    try:
        val = float(x)
    except (TypeError, ValueError):
        return None
    if val != val or val in (float("inf"), float("-inf")):
        return None
    return round(val, 2)


def _round_dims(row: dict) -> None:
    """Normalise od_mm and wall_thickness_mm to 2 decimals in place. Used at
    every loop exit so the UI sees consistent precision regardless of which
    code path produced the row's dimensions."""
    od = _round2(row.get("od_mm"))
    if od is not None:
        row["od_mm"] = od
    wt = _round2(row.get("wall_thickness_mm"))
    if wt is not None:
        row["wall_thickness_mm"] = wt


def _parse_corrosion_allowance_mm(ca: str | float | int | None) -> float:
    """'3 mm' / 'NIL' / 0 / None → numeric mm."""
    if ca is None:
        return 0.0
    if isinstance(ca, (int, float)):
        return float(ca)
    s = str(ca)
    if "nil" in s.lower() or "none" in s.lower():
        return 0.0
    m = re.search(r"([\d.]+)", s)
    return float(m.group(1)) if m else 0.0


def _is_asme_pipe_code(pipe_code: str | None) -> bool:
    """True for B36.10M / B36.19M ASME codes; False for CuNi / Cu / GRE / CPVC / tubing."""
    code = (pipe_code or "").upper()
    return ("B 36.10M" in code or "B36.10M" in code
            or "B 36.19M" in code or "B36.19M" in code)


def correct_pipe_data(
    pipe_data: list[dict],
    pipe_code: str | None = None,
    material: str | None = None,
    design_pressure_barg: float | None = None,
    design_temp_c: float | None = None,
    corrosion_allowance: str | float | None = None,
    piping_class: str | None = None,
    **_unused,
) -> list[dict]:
    """Post-process AI-generated pipe rows.

    For ASME-rated classes:
      1. Overwrite `od_mm` with the canonical value from `pipe_dimensions.json`.
      2. Compute Eq. 3a minimum wall thickness from design conditions.
      3. Look up project floor schedule for (piping_class, NPS) from
         `project_schedule_floors.json` and resolve to a floor WT.
      4. Compute required_wt = MAX(eq3a, floor_wt).
      5. Pick the smallest standard schedule meeting required_wt.
      6. Write `schedule` and `wall_thickness_mm` from the pick.

    Engineering rule: the project's conventional minimum schedule wins
    when Eq. 3a alone would pick a thinner one; Eq. 3a wins when the
    design pressure pushes above the project floor.

    For non-ASME pipe codes, the row is left untouched (OD lookup returns
    None and the function skips the rest of per-row logic).

    All rows get a final 2-decimal normalisation on `od_mm` and
    `wall_thickness_mm` before returning so the UI sees consistent
    precision.
    """
    # Lazy imports to keep module-load order clean
    from app.services.schedule_selector import (
        select_schedule_for_thickness,
        lookup_wall_thickness,
    )
    from app.services import schedule_floor_lookup
    from app.utils.engineering import calculate_wall_thickness
    from app.utils.engineering_constants import (
        JOINT_EFFICIENCY_E,
        get_allowable_stress,
    )

    ca_mm = _parse_corrosion_allowance_mm(corrosion_allowance)
    is_asme = _is_asme_pipe_code(pipe_code)

    # Per the AI's "single unified MOC" rule (ai_service.py §PIPE TYPE
    # TRANSITION), every row in a class shares the same material_spec —
    # so the stress at design temp is loop-invariant. Resolve once here
    # using the first row's spec (if any) with a class-material fallback,
    # avoiding 20× redundant regex chains in `_detect_stress_table`.
    have_design_ctx = (
        is_asme and design_pressure_barg is not None
        and design_temp_c is not None and material
    )
    s_mpa = None
    if have_design_ctx:
        first_spec = next(
            ((r.get("material_spec") or "").strip() for r in pipe_data
             if (r.get("material_spec") or "").strip()),
            "",
        )
        try:
            s_mpa = get_allowable_stress(first_spec or material, design_temp_c)["S_mpa"]
        except Exception as e:
            logger.warning("Stress lookup failed for %s/%s: %s", first_spec or material, design_temp_c, e)
            have_design_ctx = False

    for row in pipe_data:
        nps = row.get("size_inch") or row.get("nps")

        # OD correction (ASME-only). Non-ASME codes return None and we
        # skip the rest of the per-row logic. We still need to round any
        # AI-supplied od_mm / wall_thickness_mm before continuing.
        od = lookup_od(nps, pipe_code=pipe_code)
        if od is None:
            _round_dims(row)
            continue
        row["od_mm"] = od

        if not have_design_ctx:
            _round_dims(row)
            continue

        # Eq. 3a: P × D / (2(SEW + PY)) + CA, then × 1/(1-mill_tol)
        try:
            calc = calculate_wall_thickness(
                od_mm=od,
                design_pressure_barg=design_pressure_barg,
                allowable_stress_mpa=s_mpa,
                joint_factor=JOINT_EFFICIENCY_E,
                corrosion_allowance_mm=ca_mm,
            )
            t_min = calc["t_minimum_mm"]
        except Exception as e:
            logger.warning("Eq. 3a failed for NPS %s: %s — leaving AI value", nps, e)
            _round_dims(row)
            continue

        # Project floor — class-conventional minimum schedule per (class, NPS).
        # `floor_for` returns None when no rule applies (custom class, NPS out
        # of declared range, or rule explicitly "calc-only"); resolve that
        # to a 0 floor so Eq. 3a alone wins.
        floor_sched = schedule_floor_lookup.floor_for(piping_class, nps)
        floor_wt = lookup_wall_thickness(nps, floor_sched)

        # required_wt = MAX(Eq. 3a, floor). Pick smallest schedule meeting it.
        chosen = select_schedule_for_thickness(nps, max(t_min, floor_wt))
        if chosen is not None:
            row["schedule"] = chosen[0]
            row["wall_thickness_mm"] = chosen[1]

        _round_dims(row)

    return pipe_data

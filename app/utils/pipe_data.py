"""Pipe-data post-processor.

Runs after the AI emits each pipe row and corrects the row against
authoritative ASME data sourced from `pipe_dimensions.json`. The
schedule + wall thickness are computed dual-case to match the UI's
Wall Thickness Calculation Table:

  • Case 1 — Min T / Max P:  the cold-end of the B16.5 P-T curve
                              (max P, paired with its matching min T;
                              S is highest at min T → lowest t_press).
  • Case 2 — Design Point:    the user's requested design pressure +
                              temperature (S evaluated at design T).

  • t_press = max(case1_t_press, case2_t_press)  per ASME B31.3 §304.1.2 Eq. 3a
  • t_m     = t_press + corrosion_allowance
  • t_min   = t_m / (1 - mill_tolerance)
  • schedule = smallest B36.10M schedule whose WT ≥ MAX(t_min, project_floor)

Non-ASME pipe codes (CuNi EEMUA 234, Copper ASTM B42, GRE manufacturer
std, CPVC ASTM F441, Tubing ASTM A269) are passed through untouched —
their dimensions come from per-standard tables baked into the AI prompt.

When design context is incomplete (no P-T data / no material), the row's
OD is corrected but the schedule + wall thickness pass through unchanged.

Earlier versions used a single-case computation that mixed `max(P-T
pressures)` with `max(P-T temperatures)`, producing conservatively-thick
WT (those two values don't co-occur on the curve). The dual-case fix
makes the persisted pipe_data match the UI's WT calculation table — so
the Excel download and the on-screen table show the same numbers.
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
    pt_pressures: list[float] | None = None,
    pt_temperatures: list[float] | None = None,
    design_pressure_barg: float | None = None,
    design_temp_c: float | None = None,
    corrosion_allowance: str | float | None = None,
    piping_class: str | None = None,
    **_unused,
) -> list[dict]:
    """Post-process AI-generated pipe rows with dual-case Eq. 3a.

    Inputs:
      • `pt_pressures` / `pt_temperatures` — class P-T curve from the
        catalogue (B16.5 / API 6A). Used to derive Case 1 (Min T / Max P).
      • `design_pressure_barg` / `design_temp_c` — user's request design
        point. Used as Case 2.

    Either case can be omitted; if both are missing, schedule + WT are
    left as the AI emitted them.

    Engineering rule (matches the UI's WT calculation table):
      t_press = max(case1_t_press, case2_t_press)  per Eq. 3a
      t_m     = t_press + corrosion_allowance
      t_min   = t_m / (1 - mill_tolerance)
      schedule = smallest B36.10M schedule whose WT ≥ max(t_min, project_floor)

    For non-ASME pipe codes, the row is left untouched; only OD is
    corrected from `pipe_dimensions.json`.

    All rows get a final 2-decimal normalisation on `od_mm` and
    `wall_thickness_mm` before returning.
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

    # Resolve Case 1 (Min T / Max P) from the P-T curve. The cold-end row
    # has the highest pressure paired with its actual minimum temperature
    # — using these together (rather than max-P with max-T) is what makes
    # the calculation engineering-correct.
    case1: dict | None = None
    if (is_asme and pt_pressures and pt_temperatures
            and len(pt_pressures) == len(pt_temperatures) and material):
        idx = pt_pressures.index(max(pt_pressures))
        case1_p_barg = float(pt_pressures[idx])
        case1_t_c = float(pt_temperatures[idx])
        try:
            first_spec = next(
                ((r.get("material_spec") or "").strip() for r in pipe_data
                 if (r.get("material_spec") or "").strip()),
                "",
            )
            case1_s_mpa = get_allowable_stress(first_spec or material, case1_t_c)["S_mpa"]
            case1 = {"P_barg": case1_p_barg, "T_c": case1_t_c, "S_mpa": case1_s_mpa}
        except Exception as e:
            logger.warning(
                "Case 1 stress lookup failed for %s/%s: %s",
                first_spec or material, case1_t_c, e,
            )

    # Resolve Case 2 (Design Point) from the request.
    case2: dict | None = None
    if (is_asme and design_pressure_barg is not None
            and design_temp_c is not None and material):
        try:
            first_spec = next(
                ((r.get("material_spec") or "").strip() for r in pipe_data
                 if (r.get("material_spec") or "").strip()),
                "",
            )
            case2_s_mpa = get_allowable_stress(first_spec or material, design_temp_c)["S_mpa"]
            case2 = {
                "P_barg": float(design_pressure_barg),
                "T_c": float(design_temp_c),
                "S_mpa": case2_s_mpa,
            }
        except Exception as e:
            logger.warning(
                "Case 2 stress lookup failed for %s/%s: %s",
                first_spec or material, design_temp_c, e,
            )

    have_any_case = case1 is not None or case2 is not None

    for row in pipe_data:
        nps = row.get("size_inch") or row.get("nps")

        # OD correction (ASME-only). Non-ASME codes return None and we
        # skip the rest of the per-row logic.
        od = lookup_od(nps, pipe_code=pipe_code)
        if od is None:
            _round_dims(row)
            continue
        row["od_mm"] = od

        if not have_any_case:
            _round_dims(row)
            continue

        # Eq. 3a, dual-case. Compute t_press for whichever cases are
        # available; if both are present we take the larger (matches the
        # UI's `Math.max(t_press_1, t_press_2)`).
        try:
            t_press_candidates = []
            if case1 is not None:
                t_press_candidates.append(_eq3a_t_press(
                    od_mm=od, p_barg=case1["P_barg"], s_mpa=case1["S_mpa"],
                    e_factor=JOINT_EFFICIENCY_E,
                ))
            if case2 is not None:
                t_press_candidates.append(_eq3a_t_press(
                    od_mm=od, p_barg=case2["P_barg"], s_mpa=case2["S_mpa"],
                    e_factor=JOINT_EFFICIENCY_E,
                ))
            t_press = max(t_press_candidates)
            # Use calculate_wall_thickness only for the CA + mill-tol step
            # (it accepts a pre-computed t_press equivalent via design pressure
            # — easier to just compute t_m and t_min inline here).
            from app.utils.engineering_constants import MILL_TOLERANCE_FRACTION
            t_m = t_press + ca_mm
            t_min = t_m / (1 - MILL_TOLERANCE_FRACTION)
        except Exception as e:
            logger.warning("Eq. 3a failed for NPS %s: %s — leaving AI value", nps, e)
            _round_dims(row)
            continue

        # Project floor — class-conventional minimum schedule per (class, NPS).
        floor_sched = schedule_floor_lookup.floor_for(piping_class, nps)
        floor_wt = lookup_wall_thickness(nps, floor_sched)

        # required_wt = MAX(Eq. 3a, floor). Pick smallest schedule meeting it.
        required_wt = max(t_min, floor_wt)
        chosen = select_schedule_for_thickness(nps, required_wt)
        if chosen is not None:
            chosen_label, chosen_wt = chosen
            # SUBSTD detection — mirror the UI's wall-thickness table.
            # `select_schedule_for_thickness` returns the THICKEST available
            # when nothing meets the requirement (per its docstring); we have
            # to honour that contract here. If the chosen WT is still short,
            # mark the row substandard: blank schedule + the calculated t_req
            # as the wall thickness, so the engineer sees the wall they
            # actually need to procure (custom-machined / spec-upgraded).
            if chosen_wt + 1e-3 >= required_wt:
                row["schedule"] = chosen_label
                row["wall_thickness_mm"] = chosen_wt
            else:
                row["schedule"] = ""
                row["wall_thickness_mm"] = required_wt

        _round_dims(row)

    return pipe_data


def _eq3a_t_press(
    od_mm: float,
    p_barg: float,
    s_mpa: float,
    e_factor: float = 1.0,
    w_factor: float = 1.0,
    y_coefficient: float = 0.4,
) -> float:
    """ASME B31.3 §304.1.2 Eq. 3a pressure-thickness term:
        t = (P × D) / (2 × (S × E × W + P × Y))

    Returns t in mm (input D in mm, P in barg, S in MPa). The CA + mill-
    tolerance steps are applied by the caller — this is just the bare
    pressure-thickness term, matching the UI's `t_press_1` / `t_press_2`."""
    p_mpa = p_barg * 0.1
    return (p_mpa * od_mm) / (2 * (s_mpa * e_factor * w_factor + p_mpa * y_coefficient))

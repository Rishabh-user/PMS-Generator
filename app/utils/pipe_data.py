"""
Pipe dimensional lookup — OD + schedule-to-WT lookup.

All data lives in `app/data/asme_pipe_standards.json`:
  • od_groups   — NPS → OD tables for IPS, EEMUA 234, GRE manufacturer, etc.
  • wt_tables   — (OD, schedule) → WT tables per ASME B36.10M / B36.19M,
                  EEMUA 234, ASTM F 441, ASTM A 269.

Wall-thickness lookup is the PRIMARY source for the PMS output — it returns
the manufactured dimension that pipe mills actually roll (e.g. SCH 80 at
OD 60.3 mm = 5.54 mm exactly). These values cannot be derived from a formula.

The ASME B31.3 Eq. 3a formula (`calculate_wall_thickness_mm`) is ALSO exposed
here as a helper, but it is only used by the *validator* to check that the
selected WT is adequate for the design pressure. It is NOT used to populate
the PMS output.

To update any standard: edit the JSON — no code change needed.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_STANDARDS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "asme_pipe_standards.json"
)


def _load_standards() -> dict:
    if not _STANDARDS_PATH.exists():
        logger.warning("asme_pipe_standards.json not found at %s", _STANDARDS_PATH)
        return {"od_groups": {}, "wt_tables": {}, "standards": {}}
    return json.loads(_STANDARDS_PATH.read_text(encoding="utf-8"))


_STANDARDS = _load_standards()
_OD_GROUPS: dict[str, dict] = _STANDARDS.get("od_groups", {})
_WT_TABLES: dict[str, dict] = _STANDARDS.get("wt_tables", {})


# ── OD tables exposed for external imports ────────────────────────

NPS_TO_OD: dict[str, float] = {
    k: v for k, v in _OD_GROUPS.get("IPS", {}).items() if isinstance(v, (int, float))
}
OD_TO_NPS: dict[float, str] = {v: k for k, v in NPS_TO_OD.items()}
EEMUA_234_OD: dict[str, float] = {
    k: v
    for k, v in _OD_GROUPS.get("EEMUA_234", {}).items()
    if isinstance(v, (int, float))
}
GRE_MFR_STD_OD: dict[str, float] = {
    k: v
    for k, v in _OD_GROUPS.get("GRE_MFR_STD", {}).items()
    if isinstance(v, (int, float))
}
GRE_BONSTRAND_50000C_OD: dict[str, float] = {
    k: v
    for k, v in _OD_GROUPS.get("GRE_BONSTRAND_50000C", {}).items()
    if isinstance(v, (int, float))
}

# Reconstruct (OD, schedule) -> WT from the IPS_STEEL table
_WT_TABLE: dict[tuple[float, str], float] = {}
for od_str, sch_map in _WT_TABLES.get("IPS_STEEL", {}).items():
    try:
        od_f = float(od_str)
    except (TypeError, ValueError):
        continue
    for sch, wt in sch_map.items():
        _WT_TABLE[(od_f, sch)] = wt


# ── Helpers ────────────────────────────────────────────────────────

def _tubing_od(nps: str) -> float | None:
    try:
        return round(float(nps) * 25.4, 2)
    except (TypeError, ValueError):
        return None


def _normalize_schedule(schedule: str) -> str:
    """Normalize schedule names like 'SCH 80', 'Sch80', 'STANDARD', etc."""
    s = schedule.strip().upper()
    for prefix in ("SCH ", "SCH"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    if s in ("STANDARD", "STD/40S"):
        return "STD"
    if s in ("EXTRA STRONG", "EXTRA-STRONG", "XS/80S"):
        return "XS"
    if s in ("DOUBLE EXTRA STRONG", "DOUBLE EXTRA-STRONG"):
        return "XXS"
    return s


def get_od_mm(nps: str) -> float | None:
    """Default IPS (ASME B36.10M/B36.19M) OD for a given NPS."""
    nps = str(nps).strip().strip('"')
    return NPS_TO_OD.get(nps)


def get_od_for_material(nps: str, material: str | None) -> float | None:
    """Material-aware OD lookup.

    Dispatches to the right OD table based on material keywords:
      - Tubing  → OD = nominal NPS × 25.4
      - CuNi    → EEMUA 234
      - GRE A51 → Bondstrand 50000C
      - GRE A50/A52 → generic "Manufacturer's std." (GRE_MFR_STD)
      - CPVC / plastic → returns None (preserve AI value)
      - everything else (steel) → IPS
    """
    nps_str = str(nps).strip().strip('"')
    mat = (material or "").upper()

    if "TUBING" in mat or nps_str.startswith(("T80", "T90")):
        return _tubing_od(nps_str)

    if any(k in mat for k in ("CUNI", "CU-NI", "CU NI", "C70600", "EEMUA")):
        return EEMUA_234_OD.get(nps_str)

    if any(k in mat for k in ("GRE", "CPVC", "PVC", "PLASTIC", "FRP", "EPOXY")):
        # GRE-specific OD table selection by class is handled by the caller;
        # here we return None so the AI-generated OD is preserved.
        return None

    return NPS_TO_OD.get(nps_str)


def get_wall_thickness(od_mm: float, schedule: str) -> float | None:
    """Look up manufactured WT (mm) for an (OD, schedule) pair per ASME
    B36.10M / B36.19M. Returns None for '-' schedules or unknown combos.
    """
    if not schedule or schedule.strip() in ("-", "–", "—", ""):
        return None
    sch = _normalize_schedule(schedule)
    return _WT_TABLE.get((od_mm, sch))


def get_wall_thickness_by_nps(nps: str, schedule: str) -> float | None:
    """Convenience: NPS → OD → WT."""
    od = get_od_mm(nps)
    return get_wall_thickness(od, schedule) if od is not None else None


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

    Returns t_min = (t_pressure + CA) / (1 − mill%) in mm.  Used by the
    validator to check the selected (lookup) WT is adequate for the design
    pressure. NOT used to populate the PMS output.
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
    material: str | None = None,
    design_pressure_barg: float | None = None,
    design_temp_c: float | None = None,
    corrosion_allowance: str | float | None = None,
) -> list[dict]:
    """Post-process AI-generated pipe_data, enforcing standard-compliant OD
    and WT values.

      - Steel (CS/LTCS/SS/DSS/SDSS/GALV): enforce IPS OD and look up
        manufactured WT for the AI-selected schedule.
      - CuNi: enforce EEMUA 234 OD; keep AI-generated WT.
      - GRE / CPVC / plastic / tubing: preserve AI-generated OD and WT
        (manufacturer-specific).
      - '-' schedule sizes (calculated wall): if design P/T are supplied,
        compute WT via ASME B31.3 Eq. 3a; otherwise preserve AI value.

    Mutates pipe_data in place and returns it.
    """
    mat = (material or "").upper()
    is_non_asme_steel = any(
        k in mat
        for k in (
            "CUNI", "CU-NI", "CU NI", "C70600", "EEMUA",
            "GRE", "CPVC", "PVC", "PLASTIC", "FRP", "EPOXY", "TUBING",
        )
    )
    ca_mm = _parse_corrosion_allowance_mm(corrosion_allowance)

    for p in pipe_data:
        nps = str(p.get("size_inch", "")).strip()
        schedule = str(p.get("schedule", "")).strip()

        od_to_use = get_od_for_material(nps, material) if mat else get_od_mm(nps)
        if od_to_use is not None:
            p["od_mm"] = od_to_use

        if is_non_asme_steel:
            continue

        od = p.get("od_mm", 0)
        is_dash_schedule = schedule in ("-", "–", "—", "")

        if not is_dash_schedule:
            # Standard ASME schedule → look up manufactured WT from B36.10M/19M
            if od and schedule:
                wt = get_wall_thickness(od, schedule)
                if wt is not None:
                    p["wall_thickness_mm"] = wt
        else:
            # Calculated schedule → compute via ASME B31.3 Eq. 3a
            if design_pressure_barg and design_temp_c is not None and od:
                wt_calc = calculate_wall_thickness_mm(
                    od_mm=od,
                    design_pressure_barg=design_pressure_barg,
                    design_temp_c=design_temp_c,
                    material_spec=p.get("material_spec") or material or "",
                    corrosion_allowance_mm=ca_mm,
                )
                if wt_calc is not None:
                    p["wall_thickness_mm"] = wt_calc

    return pipe_data


# Legacy alias — kept so any external callers still work
_calculated_wt_for_pipe = calculate_wall_thickness_mm

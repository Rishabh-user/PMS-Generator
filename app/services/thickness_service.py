"""
Thickness & Engineering computation service.

Takes user design inputs + a piping class, and computes per-size:
  • t_req via ASME B31.3 Eq. 3a, Case 1 (Min T / Max P) and Case 2 (Design T / Design P)
  • Governing case per size
  • MAWP from the selected schedule thickness (reverse Eq. 3a)
  • Margin % vs design pressure
  • Status (OK / SUBSTD) vs calculated required thickness

Also builds the allowable-stress info and the engineering-requirements flags so
the frontend doesn't need to duplicate any calculation logic.
"""
import logging
import re

from app.models.pms_models import PMSRequest
from app.models.thickness_models import (
    CaseInfo,
    ComputeThicknessRequest,
    ComputeThicknessResponse,
    EngineeringFlag,
    PerSizeResult,
    StressInfo,
    SummaryStats,
)
from app.services.pms_service import generate_pms
from app.services import data_service
from app.utils.engineering_constants import (
    HYDROTEST_FACTOR,
    MILL_TOLERANCE_FRACTION,
    MILL_TOLERANCE_PERCENT,
    WELD_STRENGTH_W,
    Y_COEFFICIENT,
    get_allowable_stress,
)
from app.utils.pipe_data import _parse_corrosion_allowance_mm

logger = logging.getLogger(__name__)

# Unit conversions
BAR_PER_PSI = 0.0689476
PSI_PER_BAR = 14.5038


def _first_temperature_c(pt: dict) -> tuple[float, str]:
    """Return (representative T1 in °C, label). For a label like '-29 to 38',
    take 38 as T1 — conservatively higher end gives lower stress-at-temp? No,
    lower temp = higher allowable stress; ASME convention uses the high end
    (38°C / 100°F) for Case 1 Min T / Max P evaluation."""
    temps = pt.get("temperatures") or []
    labels = pt.get("temp_labels") or []
    if not temps:
        return 38.0, "38"
    # Use first breakpoint (lowest T in the table, typically 38°C)
    t1 = float(temps[0])
    label = labels[0] if labels else str(t1)
    return t1, label


def _max_pt_pressure(pt: dict) -> float:
    pressures = pt.get("pressures") or []
    return max(pressures) if pressures else 0.0


def _joint_factor_for(joint_type: str) -> float:
    """E factor per ASME B31.3 Table A-1B."""
    jt = (joint_type or "").upper().strip()
    if jt in ("SEAMLESS",) or "SEAMLESS" in jt:
        return 1.0
    if "100% RT" in jt and "EFW" in jt:
        return 1.0
    if "EFW" in jt:
        return 0.85
    if "ERW" in jt:
        return 0.85
    return 1.0


def _t_req_mm(p_barg: float, od_mm: float, s_mpa: float, E: float, W: float, Y: float) -> float:
    """ASME B31.3 Eq. 3a pressure-thickness component (no CA, no mill)."""
    p_mpa = p_barg * 0.1
    denom = 2.0 * (s_mpa * E * W + p_mpa * Y)
    if denom <= 0:
        return 0.0
    return (p_mpa * od_mm) / denom


def _mawp_barg_from_sel(
    od_mm: float,
    wt_nom_mm: float,
    s_mpa: float,
    E: float,
    W: float,
    Y: float,
    ca_mm: float,
    mill_fraction: float,
) -> float:
    """Reverse Eq. 3a: compute MAWP from nominal selected WT.
       t_eff = wt_nom × (1 − mill%) − CA
       MAWP  = (2 × S × E × W × t_eff) / (OD − 2 × Y × t_eff)     [MPa]
    """
    t_eff = wt_nom_mm * (1.0 - mill_fraction) - ca_mm
    if t_eff <= 0:
        return 0.0
    num = 2.0 * s_mpa * E * W * t_eff
    denom = od_mm - 2.0 * Y * t_eff
    if denom <= 0:
        return 0.0
    mawp_mpa = num / denom
    return mawp_mpa * 10.0  # MPa → barg


def _build_engineering_flags(
    pms,
    req: ComputeThicknessRequest,
    per_size: list[PerSizeResult],
) -> list[EngineeringFlag]:
    flags: list[EngineeringFlag] = []

    service_l = (req.service or "").lower()
    material_l = (req.material or "").lower()
    class_l = (req.piping_class or "").upper()

    is_sour = any(k in service_l or k in material_l for k in ("sour", "h2s", "nace", "mr0175"))
    is_steam = "steam" in service_l
    is_hydrogen = bool(re.search(r"\bhydrogen\b|\bh2\b", service_l))
    is_ltcs = "ltcs" in material_l or class_l.endswith("L") or class_l.endswith("LN")
    is_galv = "galv" in material_l or "galv" in service_l
    design_temp_c = req.design_temp_c

    # NOTE: hydrostatic test pressure is intentionally not emitted as a flag here
    # because it is already shown in the top-of-result badges and the Summary
    # Statistics card — keeping it here would be redundant noise.

    # Sour / NACE requirements
    if is_sour:
        flags.append(
            EngineeringFlag(
                kind="project-spec",
                label="PROJECT SPEC",
                title="NDE: 100% RT or UT — Commonly Specified for Sour Service",
                body=(
                    "100% Radiographic (RT) or Ultrasonic (UT) examination of butt welds is typically "
                    "required by client specifications for sour service (ExxonMobil GP 03-02-01, Shell "
                    "DEP 31.38.01, Aramco SAES-L). ASME B31.3 itself does not mandate 100% RT for sour "
                    "service — default per §341.4.1 is 5% random RT for Normal Fluid Service."
                ),
            )
        )
        # Material-specific sour requirements
        if "cs" in material_l and "ltcs" not in material_l:
            flags.append(
                EngineeringFlag(
                    kind="project-spec",
                    label="NACE — CS",
                    title="CS NACE: Max hardness 22 HRC / 250 HBW · Sch 160/XS minimum · PWHT required",
                    body=(
                        "Per NACE MR0175 / ISO 15156-2: carbon-steel parent metal and welds shall meet "
                        "22 HRC (250 HBW) maximum. Post-Weld Heat Treatment (PWHT) is required for most "
                        "welds. Minimum schedule Sch 160 or XS for sizes ≤ 1½\"."
                    ),
                )
            )
        if "dss" in material_l and "sdss" not in material_l:
            flags.append(
                EngineeringFlag(
                    kind="project-spec",
                    label="NACE — DSS",
                    title="DSS NACE: Max hardness 28 HRC · Ferrite 35–65% · PREN ≥ 34 · No PWHT",
                    body=(
                        "NACE MR0175 duplex requirements: max hardness 28 HRC; ferrite content 35–65% "
                        "in weldments; PREN ≥ 34. PWHT is not permitted (risk of σ-phase precipitation)."
                    ),
                )
            )
        if "sdss" in material_l:
            flags.append(
                EngineeringFlag(
                    kind="project-spec",
                    label="NACE — SDSS",
                    title="SDSS NACE: Max hardness 32 HRC · PREN ≥ 40 · No PWHT",
                    body=(
                        "Super duplex NACE: max hardness 32 HRC; PREN ≥ 40. No PWHT. "
                        "Max service T ≤ 300 °C to avoid 475 °C embrittlement."
                    ),
                )
            )

    # LTCS — impact testing
    if is_ltcs:
        flags.append(
            EngineeringFlag(
                kind="project-spec",
                label="LTCS",
                title="Low-Temperature CS: Charpy impact testing required per ASME B31.3 §323.2.2",
                body=(
                    "LTCS piping (typically ASTM A333 Gr. 6) requires Charpy V-notch impact testing "
                    "when the design minimum temperature is below −29 °C per ASME B31.3 §323.2.2 and "
                    "Table A-1/Figure 323.2.2A. Verify MDMT and qualification curve for each component."
                ),
            )
        )

    # Steam — thermal fatigue / drainage
    if is_steam:
        flags.append(
            EngineeringFlag(
                kind="note",
                label="NOTE",
                title="Steam / Condensate — Thermal Fatigue & Drainage",
                body=(
                    "Provide adequate drain points and thermal insulation. Check for water hammer "
                    "and thermal cycling fatigue. For steam > 250 °C apply ASME B31.1 Power Piping "
                    "if applicable. ERW pipe not recommended; specify seamless."
                ),
            )
        )

    # Hydrogen — API 941
    if is_hydrogen:
        flags.append(
            EngineeringFlag(
                kind="project-spec",
                label="HYDROGEN",
                title="Hydrogen Service — check API 941 Nelson Curves",
                body=(
                    "For hydrogen partial pressures above the 2016 Nelson curve thresholds, Cr-Mo "
                    "alloy steels may be required to prevent High-Temperature Hydrogen Attack (HTHA). "
                    "Consult API RP 941 latest edition."
                ),
            )
        )

    # Galvanised
    if is_galv:
        flags.append(
            EngineeringFlag(
                kind="note",
                label="NOTE",
                title="Galvanised CS — temperature limit 200 °C",
                body="Hot-dip galvanised pipe is not suitable above 200 °C (zinc embrittlement).",
            )
        )

    # Y coefficient warning if high temperature
    if design_temp_c >= 482:
        flags.append(
            EngineeringFlag(
                kind="note",
                label="Y COEFFICIENT",
                title="Design T ≥ 482 °C — Y coefficient may need adjustment",
                body=(
                    "This calculation uses Y = 0.4 (ferritic at T < 482 °C). For T ≥ 482 °C use "
                    "Y = 0.5; for T ≥ 510 °C use Y = 0.7 per ASME B31.3 Table 304.1.1."
                ),
            )
        )

    # Sub-standard selection alert (if any size failed)
    substd = [p.size_inch for p in per_size if p.status != "OK"]
    if substd:
        flags.append(
            EngineeringFlag(
                kind="mandatory",
                label="ACTION REQUIRED",
                title=f"Selected schedule is SUB-STANDARD for: {', '.join(substd)}",
                body=(
                    "For these NPS sizes the selected (nominal) wall thickness is LESS than the "
                    "calculated required thickness per Eq. 3a. Increase the schedule or consult the "
                    "project piping specialist."
                ),
            )
        )

    return flags


async def compute_thickness(req: ComputeThicknessRequest) -> ComputeThicknessResponse:
    """Main entry point — returns fully computed thickness + flags + summary."""
    # Get the PMS (cached if available) for pipe_data, rating, and material_spec
    pms = await generate_pms(
        PMSRequest(
            piping_class=req.piping_class,
            material=req.material,
            corrosion_allowance=req.corrosion_allowance,
            service=req.service,
        )
    )

    # Also read the raw entry for the full P-T table
    entry = data_service.find_entry(req.piping_class)
    pt = entry.get("pressure_temperature", {}) if entry else {}

    # --- Case 1: Min T / Max P from P-T table (optionally overridden) ---
    t1_c, t1_label = _first_temperature_c(pt)
    p1_barg_default = _max_pt_pressure(pt)
    if req.case1_pressure_psig is not None:
        p1_barg = req.case1_pressure_psig / PSI_PER_BAR
    else:
        p1_barg = p1_barg_default

    # --- Case 2: Design P / Design T (user-provided) ---
    p2_barg = req.design_pressure_barg
    t2_c = req.design_temp_c

    # --- Allowable stress at each case temperature ---
    stress_material = pms.pipe_data[0].material_spec if pms.pipe_data else req.material
    s1 = get_allowable_stress(stress_material, t1_c)
    s2 = get_allowable_stress(stress_material, t2_c)
    s1_psi = int(req.case1_stress_psi) if req.case1_stress_psi else s1["S_psi"]
    s1_mpa = round(s1_psi * BAR_PER_PSI * 10 / 14.5038, 2) if req.case1_stress_psi else s1["S_mpa"]
    s2_psi = int(req.case2_stress_psi) if req.case2_stress_psi else s2["S_psi"]
    s2_mpa = round(s2_psi * BAR_PER_PSI * 10 / 14.5038, 2) if req.case2_stress_psi else s2["S_mpa"]

    # Determine which case dominates (very rough — size-level governs is per-row)
    # At class level, pick higher P × lower S → larger t_req. Use Case 1 by default unless Case 2 is clearly worse.
    governs_class_level = 1 if (p1_barg / max(s1_mpa, 0.001)) >= (p2_barg / max(s2_mpa, 0.001)) else 2

    # --- Factors ---
    E = _joint_factor_for(req.joint_type)
    W = WELD_STRENGTH_W
    Y = Y_COEFFICIENT
    ca_mm = _parse_corrosion_allowance_mm(req.corrosion_allowance)
    mill_frac = MILL_TOLERANCE_FRACTION

    # --- Per-size rows ---
    per_size: list[PerSizeResult] = []
    for p in pms.pipe_data:
        od = p.od_mm
        nominal = p.wall_thickness_mm

        # Case 1 & Case 2 pressure-thickness (in mm, no CA yet)
        t1 = _t_req_mm(p1_barg, od, s1_mpa, E, W, Y)
        t2 = _t_req_mm(p2_barg, od, s2_mpa, E, W, Y)
        t_press = max(t1, t2)
        case_num = 1 if t1 >= t2 else 2

        # Mill tolerance / CA / calc thickness
        t_m = t_press + ca_mm
        calc_thk = t_m / (1.0 - mill_frac)
        d_over_6 = od / 6.0
        flag_t_lt_d6 = t_press < d_over_6  # OK when True (Eq. 3a is applicable)

        # Status
        status = "OK" if nominal >= calc_thk else "SUBSTD"

        # MAWP — use design-temp stress (S2) since that's the operating condition
        mawp_barg = _mawp_barg_from_sel(od, nominal, s2_mpa, E, W, Y, ca_mm, mill_frac)
        design_p_for_margin = req.design_pressure_barg
        if design_p_for_margin > 0:
            margin_pct = ((mawp_barg - design_p_for_margin) / design_p_for_margin) * 100.0
        else:
            margin_pct = 0.0

        # Governing label
        if status != "OK":
            governs_label = "calculated thickness governs"
            governs_case = case_num
        elif nominal > calc_thk * 1.05:
            # Selected schedule is much thicker than required → PMS schedule is the driver
            governs_label = "PMS minimum schedule governs"
            governs_case = -1
        else:
            governs_label = f"Case {case_num}"
            governs_case = case_num

        per_size.append(
            PerSizeResult(
                size_inch=p.size_inch,
                od_mm=round(od, 3),
                t_req_mm=round(t_press, 3),
                d_over_6_mm=round(d_over_6, 2),
                flag_t_lt_d6=flag_t_lt_d6,
                t_m_mm=round(t_m, 3),
                mill_tolerance_percent=MILL_TOLERANCE_PERCENT,
                calc_thk_mm=round(calc_thk, 3),
                sel_thk_mm=round(nominal, 3),
                schedule=p.schedule,
                status=status,
                mawp_barg=round(mawp_barg, 1),
                margin_percent=round(margin_pct, 1),
                governs=governs_label,
                governing_case=governs_case,
            )
        )

    # --- Summary ---
    mawps = [r.mawp_barg for r in per_size if r.mawp_barg > 0]
    margins = [r.margin_percent for r in per_size]
    summary = SummaryStats(
        min_mawp_barg=round(min(mawps), 1) if mawps else 0.0,
        max_mawp_barg=round(max(mawps), 1) if mawps else 0.0,
        min_margin_percent=round(min(margins), 1) if margins else 0.0,
        hydrotest_barg=round(_max_pt_pressure(pt) * HYDROTEST_FACTOR, 2),
    )

    # --- Engineering flags ---
    flags = _build_engineering_flags(pms, req, per_size)

    return ComputeThicknessResponse(
        piping_class=pms.piping_class,
        material=pms.material,
        rating=pms.rating,
        design_pressure_barg=req.design_pressure_barg,
        design_temp_c=req.design_temp_c,
        cases=CaseInfo(
            p1_barg=round(p1_barg, 2),
            p1_psig=round(p1_barg * PSI_PER_BAR, 1),
            t1_c=t1_c,
            t1_label=t1_label,
            p2_barg=round(p2_barg, 2),
            p2_psig=round(p2_barg * PSI_PER_BAR, 1),
            t2_c=t2_c,
        ),
        stress=StressInfo(
            s1_psi=s1_psi,
            s1_mpa=s1_mpa,
            t1_c=t1_c,
            t1_label=t1_label,
            s2_psi=s2_psi,
            s2_mpa=s2_mpa,
            t2_c=t2_c,
            governs=governs_class_level,
        ),
        per_size=per_size,
        engineering_flags=flags,
        summary=summary,
        joint_efficiency_E=E,
        weld_strength_W=W,
        y_coefficient=Y,
        mill_tolerance_percent=MILL_TOLERANCE_PERCENT,
        corrosion_allowance_mm=ca_mm,
    )

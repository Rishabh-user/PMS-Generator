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
from app.utils.pipe_data import _is_calc_schedule, _parse_corrosion_allowance_mm

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
    """Build the full engineering-flag list for a PMS spec.

    Single source of truth — both the standalone HTML UI (via /api/compute-
    thickness) and the Valvesheet frontend render whatever this function
    returns. Order of flag categories mirrors the legacy JS layout so
    review continuity is preserved:

      1. NACE compliance (material-family specific)
      2. Min schedule recommendation (CS NACE) / Schedule-per-design note (CRA NACE)
      3. NACE bolting grades (Table 7)
      4. Bolting coating recommendation (CS NACE only)
      5. PWHT — conditional (CS) / not required (Duplex)
      6. Steam / condensate — thermal fatigue
      7. Corrosive / acid service — material-specific guidance
      8. NDE 100% RT recommendation (sour)
      9. LTCS impact testing
     10. Hydrostatic test pressure
     11. Hydrogen API 941 (Nelson curves)
     12. Galvanised temperature limit
     13. Y coefficient warning (high T)
     14. Sub-standard wall alert (computed)
    """
    flags: list[EngineeringFlag] = []

    service_l  = (req.service or pms.service or "").lower()
    material_u = (req.material or pms.material or "").upper()
    material_l = material_u.lower()
    class_u    = (req.piping_class or pms.piping_class or "").upper()

    # Material family detection (mirrors the JS detector)
    is_sdss = ("SDSS" in material_u) or ("SUPER DUPLEX" in material_u) or ("S32750" in material_u)
    is_dss  = ("DSS"  in material_u) and not is_sdss
    is_ss   = ("SS"   in material_u) or ("STAINLESS" in material_u)
    is_cs   = ("CS"   in material_u) and not (is_sdss or is_dss or is_ss)
    is_duplex_family = is_dss or is_sdss

    is_nace      = (
        class_u.endswith("N") or class_u.endswith("LN")
        or any(k in service_l or k in material_l for k in ("sour", "h2s", "nace", "mr0175"))
    )
    is_ltcs      = ("ltcs" in material_l) or class_u.endswith("L") or class_u.endswith("LN")
    is_steam     = ("steam" in service_l) or ("condensate" in service_l)
    is_corrosive = ("corrosive" in service_l) or ("acid" in service_l) or ("chemical" in service_l)
    is_hydrogen  = bool(re.search(r"\bhydrogen\b|\bh2\b", service_l))
    is_galv      = ("galv" in material_l) or ("galv" in service_l)
    design_temp_c = req.design_temp_c

    # ── 1. NACE compliance (material-family specific) ────────────────
    if is_nace:
        if is_duplex_family:
            mat_label = "Super Duplex (S32750)" if is_sdss else "Duplex (S31803)"
            hardness  = "32 HRC (SDSS)"          if is_sdss else "28 HRC (DSS)"
            pren      = "40 (SDSS)"              if is_sdss else "34 (DSS)"
            flags.append(EngineeringFlag(
                kind="mandatory", label="CRITICAL",
                title="NACE MR0175 / ISO 15156-3 — Duplex Sour Service Compliance",
                body=(
                    f"All {mat_label} pipe, fittings, flanges, and welds must comply with "
                    f"NACE MR0175 / ISO 15156-3 Annex A. Max hardness: {hardness}. "
                    f"Solution annealing required. Ferrite content: 35–65%. PREN ≥ {pren}. "
                    f"No PWHT required for DSS/SDSS (solution-annealed condition)."
                ),
            ))
        elif is_ss:
            flags.append(EngineeringFlag(
                kind="mandatory", label="CRITICAL",
                title="NACE MR0175 / ISO 15156-3 — Austenitic SS Sour Service Compliance",
                body=(
                    "All SS316L pipe, fittings, flanges, and welds must comply with NACE "
                    "MR0175 / ISO 15156-3. Max hardness: 22 HRC (solution annealed). Cold "
                    "work limit applies. No PWHT typically required for austenitic SS."
                ),
            ))
        else:
            flags.append(EngineeringFlag(
                kind="mandatory", label="CRITICAL",
                title="NACE MR0175 / ISO 15156 — Sour Service Compliance",
                body=(
                    "All pipe, fittings, flanges, and welds must comply with NACE MR0175 / "
                    "ISO 15156. Max hardness: CS ≤ 22 HRC / 250 HBW (base metal, weld metal, "
                    "HAZ). HIC testing per NACE TM0284 if H₂S partial pressure > 0.0003 MPa "
                    "(0.05 psia). SSC testing per NACE TM0177 Method A may also be required."
                ),
            ))

        # ── 2. Min schedule (project-spec, CS only) / Schedule-per-design note ──
        if is_cs:
            flags.append(EngineeringFlag(
                kind="project-spec", label="PROJECT SPEC",
                title='Minimum Schedule Recommended — Sch 160 (≤ NPS 1½") / XS (≥ NPS 2")',
                body=(
                    "Common oil & gas project specs (Shell DEP 31.38.01, Aramco SAES-L, "
                    "Total GS EP PVV) require minimum Sch 160 (NPS ≤ 1½\") / Extra Strong "
                    "(NPS ≥ 2\") for CS sour service — for mechanical robustness and "
                    "lifecycle margin. NOTE: NACE MR0175 itself does NOT mandate any "
                    "minimum schedule; this is a project / company standard. Verify "
                    "against your project's Piping Design Basis (PDS)."
                ),
            ))
        elif is_duplex_family or is_ss:
            mat_short = "Duplex" if is_duplex_family else "SS"
            mat_long  = "Duplex/Super Duplex" if is_duplex_family else "Stainless Steel"
            ca_mat    = "DSS/SDSS" if is_duplex_family else "SS"
            flags.append(EngineeringFlag(
                kind="note", label="NOTE",
                title=f"Schedule per Design Calculation — {mat_short} NACE",
                body=(
                    f"For {mat_long} NACE service, schedule is governed by "
                    f"pressure/mechanical design calculation — no project-standard "
                    f"minimum schedule override (unlike CS sour). Corrosion allowance "
                    f"is typically NIL for {ca_mat} in sour service."
                ),
            ))

        # ── 3. NACE bolting grades ──
        bng = getattr(pms, "bolts_nuts_gaskets", None)
        stud = (getattr(bng, "stud_bolts", "") or "").strip() or "ASTM A320 Gr. L7M"
        nut  = (getattr(bng, "hex_nuts",   "") or "").strip() or "ASTM A194 Gr. 7ML"
        flags.append(EngineeringFlag(
            kind="mandatory", label="NACE REQ",
            title=f"NACE Bolting Grades — {stud} + {nut}",
            body=(
                f"Per NACE MR0175 Table 7: max hardness 22 HRC (studs) / 22 HRC (nuts) "
                f"for sour service exposure. Studs: {stud}. Nuts: {nut}. Alternative "
                f"grades (B7M + 2HM) also NACE-compliant."
            ),
        ))

        # ── 4. Bolting coating (CS only, project-spec) ──
        if is_cs:
            flags.append(EngineeringFlag(
                kind="project-spec", label="PROJECT SPEC",
                title="Bolting Coating — XYLAR 2 + XYLAN 1070 (Project Optional)",
                body=(
                    "XYLAR 2 + XYLAN 1070 coating (min 50 µm combined) is a common "
                    "offshore / splash-zone project spec for corrosion and galling "
                    "protection. NOTE: NACE MR0175 does NOT mandate coatings. Uncoated "
                    "B7M / 2HM bolts are fully NACE-compliant for onshore applications. "
                    "Verify against your project's bolting spec."
                ),
            ))

        # ── 5. PWHT — conditional (CS) / not required (Duplex) ──
        if is_cs:
            flags.append(EngineeringFlag(
                kind="project-spec", label="CONDITIONAL",
                title="PWHT — Required Based on Thickness / Hardness",
                body=(
                    "Per ASME B31.3 Table 331.1.1 (P-Number 1 / CS): PWHT required when "
                    "nominal wall thickness > 19 mm (¾\"). For thinner sections, PWHT may "
                    "be waived if HAZ hardness ≤ 250 HBW is demonstrated in PQR. Per NACE "
                    "MR0175 §7.2.1.3, PWHT is NOT mandatory if hardness limits are met "
                    "via: low-hydrogen electrodes + proper preheat + PQR hardness "
                    "testing. WPS/PQR must include hardness survey regardless."
                ),
            ))
        elif is_duplex_family:
            mat_label = "Super Duplex (S32750)" if is_sdss else "Duplex (S31803)"
            flags.append(EngineeringFlag(
                kind="note", label="NOTE",
                title="No PWHT Required — Duplex / Super Duplex",
                body=(
                    f"PWHT is NOT required for {mat_label}. Material is supplied in "
                    f"solution-annealed condition. Ferrite/austenite balance must be "
                    f"maintained in HAZ (35–65% ferrite)."
                ),
            ))

    # ── 6. Steam / condensate — thermal fatigue ──
    if is_steam:
        flags.append(EngineeringFlag(
            kind="note", label="NOTE",
            title="Steam / Condensate — Thermal Fatigue & Drainage",
            body=(
                "Provide adequate drain points and thermal insulation. Check for water "
                "hammer and thermal cycling fatigue. For steam > 250°C apply ASME B31.1 "
                "Power Piping if applicable. ERW pipe not recommended; specify seamless."
            ),
        ))

    # ── 7. Corrosive / acid service — material-specific guidance ──
    if is_corrosive:
        if is_sdss:
            flags.append(EngineeringFlag(
                kind="note", label="NOTE",
                title="Corrosive / Acid Service — Super Duplex (PREN ≥ 40)",
                body=(
                    "SDSS (S32750) has PREN ≥ 40 — one of the highest corrosion-resistant "
                    "CRAs. CA typically NIL. Suitable for chloride, sour, and dilute acid "
                    "exposure. Limits: avoid sustained service above 300°C (475°C "
                    "embrittlement risk). Monitor crevice corrosion at gaskets/flange "
                    "faces. NDE: 100% RT or UT for butt welds; maintain ferrite 35–55% in HAZ."
                ),
            ))
        elif is_dss:
            flags.append(EngineeringFlag(
                kind="note", label="NOTE",
                title="Corrosive / Acid Service — Duplex (PREN ≥ 34)",
                body=(
                    "DSS (S31803) has PREN ≥ 34 — superior to SS 316L in chloride/sour "
                    "environments. CA typically NIL. Avoid prolonged service above 300°C "
                    "(475°C embrittlement). For highly aggressive acids (pH < 2) or "
                    "high-chloride + high-temp combinations, consider SDSS or nickel "
                    "alloys. NDE: 100% RT or UT for butt welds; maintain ferrite balance "
                    "35–65% in HAZ."
                ),
            ))
        elif is_ss:
            flags.append(EngineeringFlag(
                kind="project-spec", label="WARNING",
                title="Corrosive / Acid Service — SS (Chloride SCC Risk)",
                body=(
                    "SS 316L is susceptible to chloride stress corrosion cracking (SCC) "
                    "above ~60°C or when Cl⁻ > 50 ppm. Consider upgrading to DSS/SDSS "
                    "if: pH < 4, chloride > 50 ppm, or T > 60°C. Typical CA: 1–1.5 mm. "
                    "100% RT or UT for all butt welds. Monitor crevice corrosion at "
                    "flanges and dead-legs."
                ),
            ))
        elif is_cs or is_ltcs:
            if is_nace:
                flags.append(EngineeringFlag(
                    kind="note", label="NOTE",
                    title="Corrosive Service — Verify CS NACE Application Limits",
                    body=(
                        f"CS NACE class ({pms.piping_class}) is already qualified for "
                        f"sour service. Verify process chemistry is within CS operating "
                        f"envelope: H₂S partial pressure, pH (typically > 4 for CS), "
                        f"chloride, temperature. For very aggressive sour (pH < 4, "
                        f"high H₂S, high Cl⁻, T > 60°C), consider switching to a CRA "
                        f"class (DSS/SDSS) at material selection stage. Monitor "
                        f"corrosion rate at turnarounds."
                    ),
                ))
            else:
                flags.append(EngineeringFlag(
                    kind="project-spec", label="WARNING",
                    title="Corrosive / Acid Service — CS May Be Insufficient",
                    body=(
                        "For aggressive corrosive service, consider upgrading to SS "
                        "316L, DSS, or nickel alloy (especially if pH < 4, T > 60°C, "
                        "or chloride-bearing). Minimum CA: 3.0 mm if CS is retained. "
                        "100% RT or UT typically specified by project. Monitor "
                        "corrosion rate; review CA at major turnarounds. For sour + "
                        "corrosive combined, NACE MR0175-compliant class required."
                    ),
                ))
        else:
            flags.append(EngineeringFlag(
                kind="note", label="NOTE",
                title="Corrosive / Acid Service — Verify Material Compatibility",
                body=(
                    f"Verify that {pms.material} is compatible with the specific "
                    f"process fluid, concentration, and temperature. Consult material "
                    f"datasheet and corrosion tables. 100% RT or UT for butt welds "
                    f"where applicable."
                ),
            ))

    # ── 8. NDE 100% RT recommendation (sour service) ──
    if is_nace:
        flags.append(EngineeringFlag(
            kind="project-spec", label="PROJECT SPEC",
            title="NDE: 100% RT or UT — Commonly Specified for Sour Service",
            body=(
                "100% Radiographic (RT) or Ultrasonic (UT) examination of butt welds is "
                "typically required by client specifications for sour service (e.g., "
                "ExxonMobil GP 03-02-01, Shell DEP 31.38.01, Aramco SAES-L). NOTE: ASME "
                "B31.3 does NOT mandate 100% RT for sour service — default per §341.4.1 "
                "is 5% random RT for Normal Fluid Service. 100% RT is codified only "
                "for: Category M (high-toxicity fluids, §M341.4), Severe Cyclic "
                "(§341.4.3), or when specified by the owner. Verify against your "
                "project's inspection test plan (ITP)."
            ),
        ))

    # ── 9. LTCS — impact testing ──
    if is_ltcs:
        flags.append(EngineeringFlag(
            kind="mandatory", label="MANDATORY",
            title="Low Temperature Service — Impact Testing Required",
            body=(
                "Impact testing per ASME B31.3 §323.2 required for LTCS materials at "
                "MDMT. Charpy V-notch test: minimum 27J (20 ft-lbs) at MDMT. Materials "
                "must be A333 Gr.6 / A350 LF2 / A352 LCB or equivalent."
            ),
        ))

    # ── 10. Hydrostatic test pressure ──
    ht_barg = None
    try:
        if pms.hydrotest_pressure:
            # hydrotest_pressure may be "29.4 barg" or just "29.4"
            ht_barg = float(str(pms.hydrotest_pressure).split()[0])
    except (ValueError, AttributeError, IndexError):
        ht_barg = None
    if ht_barg is None and req.design_pressure_barg > 0:
        ht_barg = req.design_pressure_barg * HYDROTEST_FACTOR
    if ht_barg is not None and ht_barg > 0:
        ht_base = ht_barg / HYDROTEST_FACTOR
        flags.append(EngineeringFlag(
            kind="mandatory", label="MANDATORY",
            title=(f"Hydrostatic Test Pressure: {ht_barg:.1f} barg "
                   f"(≈ 1.5 × {ht_base:.1f} barg max rated pressure)"),
            body=(
                f"Shop test: {ht_barg:.1f} barg per ASME B31.3 §345.4.2. Base: max "
                f"P-T rated pressure = {ht_base:.1f} barg (at ambient). Medium: "
                f"potable water (deionised for SS; chloride ≤ 50 ppm). Duration: "
                f"minimum 10 minutes. Verify all flanges rated ≥ {ht_barg:.1f} barg "
                f"at test temperature. NOTE: Strict §345.4.2(b) formula is "
                f"P_test = 1.5 × P_design × (S_T_ambient / S_T_design) — may yield "
                f"higher pressure when design temperature significantly reduces "
                f"allowable stress. This display uses the simplified 1.5 × rated "
                f"pressure form as a conservative default."
            ),
        ))

    # ── 11. Hydrogen — API 941 (preserved from existing Python rules) ──
    if is_hydrogen:
        flags.append(EngineeringFlag(
            kind="project-spec", label="HYDROGEN",
            title="Hydrogen Service — check API 941 Nelson Curves",
            body=(
                "For hydrogen partial pressures above the 2016 Nelson curve "
                "thresholds, Cr-Mo alloy steels may be required to prevent "
                "High-Temperature Hydrogen Attack (HTHA). Consult API RP 941 latest "
                "edition."
            ),
        ))

    # ── 12. Galvanised — temperature limit (preserved) ──
    if is_galv:
        flags.append(EngineeringFlag(
            kind="note", label="NOTE",
            title="Galvanised CS — temperature limit 200°C",
            body="Hot-dip galvanised pipe is not suitable above 200°C (zinc embrittlement).",
        ))

    # ── 13. Y coefficient warning (preserved) ──
    if design_temp_c >= 482:
        flags.append(EngineeringFlag(
            kind="note", label="Y COEFFICIENT",
            title="Design T ≥ 482°C — Y coefficient may need adjustment",
            body=(
                "This calculation uses Y = 0.4 (ferritic at T < 482°C). For "
                "T ≥ 482°C use Y = 0.5; for T ≥ 510°C use Y = 0.7 per ASME B31.3 "
                "Table 304.1.1."
            ),
        ))

    # ── 14. Sub-standard wall alert (computed from per_size) ──
    substd = [p.size_inch for p in per_size if p.status != "OK"]
    if substd:
        flags.append(EngineeringFlag(
            kind="mandatory", label="ACTION REQUIRED",
            title=f"Selected schedule is SUB-STANDARD for: {', '.join(substd)}",
            body=(
                "For these NPS sizes the selected (nominal) wall thickness is LESS "
                "than the calculated required thickness per Eq. 3a. Increase the "
                "schedule or consult the project piping specialist."
            ),
        ))

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

        # Selected Thickness display:
        #   - When a standard schedule is present (e.g. SCH 160, SCH XXS,
        #     80S, STD, XS), show the nominal wall thickness from the PMS
        #     as-is to 3 decimals — the authoritative value looked up from
        #     the ASME B36.10M/B36.19M table.
        #   - When schedule is "-" (or blank), there is no table-based
        #     selection — per the project owner, mirror the Calc. Thk T
        #     value rounded to 2 decimals with NO additional math.
        if _is_calc_schedule(p.schedule):
            sel_thk_display = round(calc_thk, 2)
        else:
            sel_thk_display = round(nominal, 3)

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
                sel_thk_mm=sel_thk_display,
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

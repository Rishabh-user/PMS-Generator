"""
Deterministic PMS validation service.

Takes an AI-generated PMSResponse and audits it against engineering
standards — NOT against any curated catalog or Excel file. Every finding
is backed by a formula or a published standard:

  • ASME B36.10M / B36.19M  — nominal wall-thickness tables
  • ASME B31.3 Eq. 3a        — minimum wall thickness for pressure
  • ASME B16.5               — flange pressure/rating convention
  • ASME B31.3 Table A-1     — allowable stress at temperature
  • Class-code naming rules  — internal project convention

The validation output is a list of findings — error / warning / ok — that
the frontend renders so the engineer can verify AI output before trusting
it.
"""
from __future__ import annotations

import logging
import re

from app.models.pms_models import PMSResponse
from app.models.validation_models import ValidationFinding, ValidationReport
from app.utils.engineering import calculate_wall_thickness
from app.utils.engineering_constants import (
    JOINT_EFFICIENCY_E,
    MILL_TOLERANCE_FRACTION,
    WELD_STRENGTH_W,
    Y_COEFFICIENT,
    get_allowable_stress,
)
from app.utils.pipe_data import get_wall_thickness

logger = logging.getLogger(__name__)


# ── Naming-convention rules (project internal) ────────────────────

# PART 1 (rating letter) → ASME class
_RATING_LETTER = {
    "A": "150#", "B": "300#", "C": "400#", "D": "600#",
    "E": "900#", "F": "1500#", "G": "2500#", "J": "5000#",
    "K": "10000#", "T": "Tubing",
}

# PART 2 (material digit) → material family label
_MATERIAL_DIGIT = {
    "1": "CS",
    "2": "CS heavy wall",
    "3": "CS Galvanized",
    "4": "CS Galvanized thin wall",
    "5": "CS Galvanized 6mm",
    "6": "CS Epoxy",
    "10": "SS316L",
    "11": "SS304L",
    "20": "DSS",
    "25": "SDSS",
    "30": "CuNi",
    "40": "Copper",
    "50": "GRE",
    "51": "GRE",
    "52": "GRE",
    "60": "CPVC",
    "70": "Titanium",
    "80": "Tubing SS316L",
    "90": "Tubing 6Mo",
}


def _parse_class_code(cls: str) -> dict:
    """Split a class code into (letter, digits, suffix)."""
    m = re.match(r"^([A-KT])(\d+)([A-Z]*)$", cls.upper())
    if not m:
        return {"letter": "", "digits": "", "suffix": ""}
    return {"letter": m.group(1), "digits": m.group(2), "suffix": m.group(3)}


# ── Individual check functions ────────────────────────────────────

def _check_class_code_vs_rating(pms: PMSResponse) -> list[ValidationFinding]:
    parsed = _parse_class_code(pms.piping_class)
    expected = _RATING_LETTER.get(parsed["letter"])
    if not expected:
        return [ValidationFinding(
            kind="warning",
            rule="CLASS_CODE_FORMAT",
            title="Class code does not match naming convention",
            detail=(
                f"Class '{pms.piping_class}' — first character '{parsed['letter']}' "
                "is not in the set A/B/C/D/E/F/G/J/K/T."
            ),
        )]
    if pms.rating and expected != pms.rating and expected != "Tubing":
        return [ValidationFinding(
            kind="error",
            rule="CLASS_CODE_VS_RATING",
            title=f"Class letter '{parsed['letter']}' implies {expected}, "
                  f"but rating reported is {pms.rating}",
            detail=(
                f"Per naming convention: A=150#, B=300#, D=600#, E=900#, F=1500#, "
                f"G=2500#. Class '{pms.piping_class}' starts with '{parsed['letter']}' "
                f"→ expected {expected} but PMS shows '{pms.rating}'."
            ),
        )]
    return [ValidationFinding(
        kind="ok",
        rule="CLASS_CODE_VS_RATING",
        title=f"Class letter '{parsed['letter']}' correctly matches rating {pms.rating}",
        detail=f"'{parsed['letter']}' → {expected} per internal naming convention.",
    )]


def _check_nace_consistency(pms: PMSResponse) -> list[ValidationFinding]:
    parsed = _parse_class_code(pms.piping_class)
    suffix = parsed["suffix"]
    class_says_nace = "N" in suffix
    material_says_nace = "NACE" in (pms.material or "").upper()

    if class_says_nace and not material_says_nace:
        return [ValidationFinding(
            kind="error",
            rule="NACE_CONSISTENCY",
            title="Class code has NACE suffix but material does not",
            detail=(
                f"Class '{pms.piping_class}' ends with 'N' → NACE/sour service variant. "
                f"Material shows '{pms.material}' — should include 'NACE' per "
                "NACE MR0175 / ISO 15156."
            ),
        )]
    if material_says_nace and not class_says_nace:
        return [ValidationFinding(
            kind="warning",
            rule="NACE_CONSISTENCY",
            title="Material is NACE but class code has no 'N' suffix",
            detail=(
                f"Material '{pms.material}' says NACE but class '{pms.piping_class}' "
                "has no N suffix. Verify class naming."
            ),
        )]
    return [ValidationFinding(
        kind="ok",
        rule="NACE_CONSISTENCY",
        title="NACE suffix and material are consistent",
        detail="",
    )]


def _check_wt_vs_b3610m(pms: PMSResponse) -> list[ValidationFinding]:
    """For each pipe row, verify the reported WT matches ASME B36.10M/B36.19M
    lookup for the given (OD, schedule)."""
    findings: list[ValidationFinding] = []
    for p in pms.pipe_data:
        sch = (p.schedule or "").strip()
        # Skip non-standard schedules ("-" used for custom calc thickness)
        if not sch or sch in ("-", "–", "—"):
            findings.append(ValidationFinding(
                kind="warning",
                rule="WT_LOOKUP_B3610M",
                title=f"{p.size_inch}\": schedule is '-' (calculated wall)",
                detail=(
                    f"No ASME lookup available for this row; WT "
                    f"{p.wall_thickness_mm} mm is not verified against B36.10M/19M. "
                    "Trust the pressure adequacy check instead."
                ),
                size_inch=p.size_inch,
            ))
            continue

        std_wt = get_wall_thickness(p.od_mm, sch)
        if std_wt is None:
            findings.append(ValidationFinding(
                kind="warning",
                rule="WT_LOOKUP_B3610M",
                title=f"{p.size_inch}\": (OD {p.od_mm} mm, {sch}) not in B36.10M/19M table",
                detail=(
                    "The (OD, schedule) combination is not a standard ASME pair. "
                    "Could be a typo in the schedule, or a non-standard OD "
                    "(CuNi/GRE/CPVC/tubing)."
                ),
                size_inch=p.size_inch,
            ))
            continue

        if abs(std_wt - p.wall_thickness_mm) > 0.05:
            findings.append(ValidationFinding(
                kind="error",
                rule="WT_LOOKUP_B3610M",
                title=f"{p.size_inch}\": WT {p.wall_thickness_mm} mm != ASME B36.10M/19M {std_wt} mm for {sch}",
                detail=(
                    f"Schedule {sch} @ OD {p.od_mm} mm must have exactly {std_wt} mm "
                    f"wall per ASME B36.10M/B36.19M. PMS reports {p.wall_thickness_mm} mm. "
                    f"Either the schedule or the WT value is wrong."
                ),
                size_inch=p.size_inch,
            ))
        else:
            findings.append(ValidationFinding(
                kind="ok",
                rule="WT_LOOKUP_B3610M",
                title=f"{p.size_inch}\": WT matches ASME B36.10M/19M for {sch}",
                detail=f"OD {p.od_mm} mm × {sch} = {std_wt} mm (matches PMS value).",
                size_inch=p.size_inch,
            ))
    return findings


def _parse_ca_mm(ca: str) -> float:
    if not ca:
        return 0.0
    if "nil" in ca.lower() or "none" in ca.lower():
        return 0.0
    m = re.search(r"([\d.]+)", ca)
    return float(m.group(1)) if m else 0.0


def _check_wt_pressure_adequacy(pms: PMSResponse) -> list[ValidationFinding]:
    """For each pipe row, verify the selected WT is adequate for the class's
    maximum P-T pressure per ASME B31.3 Eq. 3a. Uses the representative
    ambient-temp stress (first P-T breakpoint, typically 38 °C)."""
    findings: list[ValidationFinding] = []

    pt = pms.pressure_temperature
    pressures = pt.pressures or []
    temps = pt.temperatures or []
    if not pressures or not temps:
        return [ValidationFinding(
            kind="warning",
            rule="WT_PRESSURE_ADEQUACY",
            title="No P-T data available to verify wall-thickness adequacy",
            detail="Skipping ASME B31.3 Eq. 3a check.",
        )]

    p_max_barg = max(pressures)
    t_ref_c = temps[0]
    material_spec = (
        pms.pipe_data[0].material_spec if pms.pipe_data else pms.material
    )
    stress = get_allowable_stress(material_spec or "", t_ref_c)
    s_mpa = stress["S_mpa"]
    ca_mm = _parse_ca_mm(pms.corrosion_allowance)

    for p in pms.pipe_data:
        if not p.od_mm:
            continue
        calc = calculate_wall_thickness(
            od_mm=p.od_mm,
            design_pressure_barg=p_max_barg,
            allowable_stress_mpa=s_mpa,
            joint_factor=JOINT_EFFICIENCY_E,
            corrosion_allowance_mm=ca_mm,
        )
        t_min_required = calc["t_minimum_mm"]
        # The selected nominal WT must meet this after mill tolerance + CA
        if p.wall_thickness_mm + 0.001 < t_min_required:
            shortfall = round(t_min_required - p.wall_thickness_mm, 3)
            findings.append(ValidationFinding(
                kind="error",
                rule="WT_PRESSURE_ADEQUACY",
                title=f"{p.size_inch}\": WT {p.wall_thickness_mm} mm below B31.3 Eq. 3a minimum {t_min_required} mm",
                detail=(
                    f"At {p_max_barg} barg / {t_ref_c} °C with S={stress['S_psi']} psi, "
                    f"c={ca_mm} mm, mill tol 12.5%: required t_min = {t_min_required} mm. "
                    f"PMS schedule {p.schedule} gives {p.wall_thickness_mm} mm — "
                    f"short by {shortfall} mm. Upgrade schedule."
                ),
                size_inch=p.size_inch,
            ))
        else:
            margin = round(p.wall_thickness_mm - t_min_required, 3)
            findings.append(ValidationFinding(
                kind="ok",
                rule="WT_PRESSURE_ADEQUACY",
                title=f"{p.size_inch}\": WT adequate for pressure per B31.3 Eq. 3a",
                detail=(
                    f"t_req={t_min_required} mm at P_max={p_max_barg} barg, "
                    f"S={stress['S_psi']} psi. PMS WT={p.wall_thickness_mm} mm "
                    f"(margin {margin} mm)."
                ),
                size_inch=p.size_inch,
            ))
    return findings


def _check_valve_code_prefix(pms: PMSResponse) -> list[ValidationFinding]:
    """Valve codes should embed the class code (e.g. class A1 → 'BLRTA1R')."""
    findings: list[ValidationFinding] = []
    cls = pms.piping_class.upper()

    groups = [
        ("Ball", pms.valves.ball),
        ("Gate", pms.valves.gate),
        ("Globe", pms.valves.globe),
        ("Check", pms.valves.check),
    ]
    for label, code_str in groups:
        if not code_str:
            continue
        codes = [c.strip() for c in re.split(r"[,/]", code_str) if c.strip()]
        bad = [c for c in codes if cls not in c.upper()]
        if bad:
            findings.append(ValidationFinding(
                kind="warning",
                rule="VALVE_CODE_PREFIX",
                title=f"{label} valve code(s) do not embed class '{cls}'",
                detail=(
                    f"Convention: the class code should appear in the VDS. "
                    f"Found: {', '.join(bad)}. Verify the codes reference the correct class."
                ),
            ))
        else:
            findings.append(ValidationFinding(
                kind="ok",
                rule="VALVE_CODE_PREFIX",
                title=f"{label} valve code(s) embed class '{cls}'",
                detail=f"Codes: {code_str}",
            ))
    return findings


def _check_flange_rating(pms: PMSResponse) -> list[ValidationFinding]:
    """Flange standard should be ASME B16.5 (≤ 24") or B16.47 (large bore)."""
    std = (pms.flange.standard or "").upper()
    if not std:
        return [ValidationFinding(
            kind="warning",
            rule="FLANGE_STANDARD",
            title="No flange standard reported",
            detail="Expected 'ASME B 16.5' (NPS ≤ 24) or 'ASME B 16.47' (large bore).",
        )]
    if "16.5" in std or "16.47" in std or "B16.5" in std.replace(" ", ""):
        return [ValidationFinding(
            kind="ok",
            rule="FLANGE_STANDARD",
            title=f"Flange standard '{pms.flange.standard}' is recognised",
            detail="ASME B16.5 covers NPS ≤ 24\"; B16.47 covers larger.",
        )]
    return [ValidationFinding(
        kind="warning",
        rule="FLANGE_STANDARD",
        title=f"Flange standard '{pms.flange.standard}' is unusual",
        detail="Expected ASME B16.5 or B16.47. Verify the spec.",
    )]


def _check_mill_tolerance(pms: PMSResponse) -> list[ValidationFinding]:
    mt = (pms.mill_tolerance or "").replace(" ", "").lower()
    expected_frac = MILL_TOLERANCE_FRACTION
    expected_pct = expected_frac * 100  # 12.5
    ok = any(tok in mt for tok in (str(expected_pct), f"{expected_pct:g}", "12.5"))
    if not pms.mill_tolerance:
        return [ValidationFinding(
            kind="warning",
            rule="MILL_TOLERANCE",
            title="Mill tolerance not reported",
            detail=f"Expected {expected_pct}% (ASME B36.10M standard for seamless).",
        )]
    if not ok:
        return [ValidationFinding(
            kind="warning",
            rule="MILL_TOLERANCE",
            title=f"Mill tolerance '{pms.mill_tolerance}' differs from 12.5%",
            detail="ASME B36.10M seamless pipe standard mill tolerance is 12.5%.",
        )]
    return [ValidationFinding(
        kind="ok",
        rule="MILL_TOLERANCE",
        title=f"Mill tolerance is 12.5% (ASME B36.10M)",
        detail="",
    )]


# ── Entrypoint ──────────────────────────────────────────────────

def validate(pms: PMSResponse) -> ValidationReport:
    """Run every Layer-1 check and return a consolidated report."""
    findings: list[ValidationFinding] = []
    findings.extend(_check_class_code_vs_rating(pms))
    findings.extend(_check_nace_consistency(pms))
    findings.extend(_check_mill_tolerance(pms))
    findings.extend(_check_flange_rating(pms))
    findings.extend(_check_wt_vs_b3610m(pms))
    findings.extend(_check_wt_pressure_adequacy(pms))
    findings.extend(_check_valve_code_prefix(pms))

    ok = sum(1 for f in findings if f.kind == "ok")
    warn = sum(1 for f in findings if f.kind == "warning")
    err = sum(1 for f in findings if f.kind == "error")

    return ValidationReport(
        piping_class=pms.piping_class,
        material=pms.material,
        corrosion_allowance=pms.corrosion_allowance,
        service=pms.service,
        total_checks=len(findings),
        ok_count=ok,
        warning_count=warn,
        error_count=err,
        findings=findings,
    )

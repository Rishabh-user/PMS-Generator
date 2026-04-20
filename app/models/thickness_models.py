"""
Pydantic models for the /api/compute-thickness endpoint.

Computes per-size wall thickness, MAWP, margins, and engineering flags
so the frontend can display the same numbers as the original app.js
without duplicating the math.
"""
from typing import Optional
from pydantic import BaseModel, Field


class ComputeThicknessRequest(BaseModel):
    # PMS identification (same shape as PMSRequest)
    piping_class: str = Field(..., description="Piping class code, e.g. 'A1'")
    material: str = Field(..., description="Material, e.g. 'CS', 'SS316L'")
    corrosion_allowance: str = Field(..., description="e.g. '3 mm'")
    service: str = Field(..., description="Service description")

    # Design inputs (user-provided in Actual Process Design Conditions)
    design_pressure_barg: float = Field(..., description="Design pressure in barg")
    design_temp_c: float = Field(..., description="Design temperature in °C")
    mdmt_c: Optional[float] = Field(default=None, description="Min Design Metal Temp in °C")
    joint_type: str = Field(default="Seamless", description="Joint type")

    # Optional overrides — when not provided, backend auto-derives from P-T table / stress tables
    case1_pressure_psig: Optional[float] = Field(default=None, description="Case 1 pressure override (psig)")
    case1_stress_psi: Optional[float] = Field(default=None, description="Case 1 allowable stress override (psi)")
    case2_stress_psi: Optional[float] = Field(default=None, description="Case 2 allowable stress override (psi)")


class PerSizeResult(BaseModel):
    """One row of the Wall Thickness Calculation Table."""
    size_inch: str
    od_mm: float = Field(..., description="Outside diameter (mm)")
    t_req_mm: float = Field(..., description="Required pressure thickness T per Eq. 3a (mm)")
    d_over_6_mm: float = Field(..., description="OD/6 (mm) — branch/reinforcement check reference")
    flag_t_lt_d6: bool = Field(..., description="True when T < D/6 (limit for Eq. 3a applicability)")
    t_m_mm: float = Field(..., description="t + c = T_M (mm)")
    mill_tolerance_percent: float = Field(..., description="Mill undertolerance percent, e.g. 12.5")
    calc_thk_mm: float = Field(..., description="Final calculated thickness after mill tol: (t+c)/(1-mill) (mm)")
    sel_thk_mm: float = Field(..., description="Selected nominal WT from ASME B36.10M/19M (mm)")
    schedule: str = Field(..., description="Selected schedule, e.g. 'SCH 80'")
    status: str = Field(..., description="'OK' | 'SUBSTD' (selected < calculated)")
    mawp_barg: float = Field(..., description="Maximum Allowable Working Pressure (barg)")
    margin_percent: float = Field(..., description="(MAWP − design P) / design P × 100")
    governs: str = Field(
        ...,
        description="'Case 1 (Min T / Max P)' | 'Case 2 (Design P @ Design T)' | 'PMS minimum schedule governs'",
    )
    governing_case: int = Field(..., description="1 or 2; -1 if schedule override governs")


class StressInfo(BaseModel):
    """Allowable stress S(T) at Case 1 and Case 2 temperatures."""
    s1_psi: int = Field(..., description="Allowable stress at T1 (psi)")
    s1_mpa: float = Field(..., description="Allowable stress at T1 (MPa)")
    t1_c: float = Field(..., description="Case 1 temperature (°C)")
    t1_label: str = Field(..., description="Human label for T1, e.g. '-29 to 38'")

    s2_psi: int = Field(..., description="Allowable stress at T2 (psi)")
    s2_mpa: float = Field(..., description="Allowable stress at T2 (MPa)")
    t2_c: float = Field(..., description="Case 2 temperature = design T (°C)")

    governs: int = Field(..., description="1 or 2 — which case dominates for worst-case t_req")


class CaseInfo(BaseModel):
    """Pressure / temperature values for Case 1 and Case 2 as shown in Design Parameters."""
    p1_barg: float
    p1_psig: float
    t1_c: float
    t1_label: str
    p2_barg: float
    p2_psig: float
    t2_c: float


class EngineeringFlag(BaseModel):
    """A single engineering-requirement banner (NACE, LTCS, hydrotest, etc.)."""
    kind: str = Field(..., description="'mandatory' | 'project-spec' | 'note'")
    label: str = Field(..., description="Tag shown, e.g. 'MANDATORY', 'NOTE'")
    title: str
    body: str


class SummaryStats(BaseModel):
    min_mawp_barg: float
    max_mawp_barg: float
    min_margin_percent: float
    hydrotest_barg: float


class ComputeThicknessResponse(BaseModel):
    piping_class: str
    material: str
    rating: str
    design_pressure_barg: float
    design_temp_c: float

    cases: CaseInfo
    stress: StressInfo
    per_size: list[PerSizeResult]
    engineering_flags: list[EngineeringFlag]
    summary: SummaryStats

    # Code factors actually used (echoed so frontend shows them)
    joint_efficiency_E: float
    weld_strength_W: float
    y_coefficient: float
    mill_tolerance_percent: float
    corrosion_allowance_mm: float

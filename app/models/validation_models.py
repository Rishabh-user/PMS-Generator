"""
Pydantic models for the PMS validation endpoint.

Validation runs deterministic engineering checks on AI-generated PMS output —
no hand-curated data. The only references are ASME standards (B31.3, B36.10M,
B36.19M, B16.5), material stress tables, and naming-convention rules.
"""
from typing import Literal

from pydantic import BaseModel, Field


class ValidationFinding(BaseModel):
    """A single validation result for one rule + one scope (class or per-size)."""
    kind: Literal["ok", "warning", "error"] = "ok"
    rule: str = Field(..., description="Machine-readable rule identifier")
    title: str = Field(..., description="Short human-readable title")
    detail: str = Field(..., description="Full explanation + expected vs actual")
    size_inch: str = Field(default="", description="Pipe size this applies to, if any")


class ValidationReport(BaseModel):
    piping_class: str
    material: str
    corrosion_allowance: str
    service: str

    total_checks: int
    ok_count: int
    warning_count: int
    error_count: int

    findings: list[ValidationFinding] = Field(default_factory=list)

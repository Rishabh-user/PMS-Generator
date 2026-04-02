from typing import Optional
from pydantic import BaseModel, Field


class PMSRequest(BaseModel):
    piping_class: str = Field(..., description="Piping class code, e.g. A1LN, B1N", examples=["A1LN"])
    material: str = Field(..., description="Material type, e.g. LTCS NACE, CS, SS316L", examples=["LTCS NACE"])
    corrosion_allowance: str = Field(..., description="Corrosion allowance, e.g. 3 mm", examples=["3 mm"])
    service: str = Field(
        ...,
        description="Service description",
        examples=["Flare, Corrosive HC service (Low Temp)"],
    )


class PressureTemperature(BaseModel):
    temperatures: list[float] = Field(default_factory=list, description="Temperature values in deg C")
    pressures: list[float] = Field(default_factory=list, description="Pressure values in barg")
    temp_labels: list[str] = Field(default_factory=list, description="Display labels for temperatures (e.g. '-29 TO 38')")


class PipeSize(BaseModel):
    size_inch: str = Field(..., description="Nominal pipe size in inches")
    od_mm: float = Field(..., description="Outside diameter in mm")
    schedule: str = Field(..., description="Schedule designation")
    wall_thickness_mm: float = Field(..., description="Wall thickness in mm")
    pipe_type: str = Field(..., description="Seamless or welded")
    material_spec: str = Field(..., description="ASTM material specification")
    ends: str = Field(..., description="End type")


class FittingsData(BaseModel):
    fitting_type: str = Field(default="", description="Fitting connection type")
    material_spec: str = Field(default="", description="ASTM material specification")
    elbow_standard: str = Field(default="", description="Elbow standard code")
    tee_standard: str = Field(default="", description="Tee standard code")
    reducer_standard: str = Field(default="", description="Reducer standard code")
    cap_standard: str = Field(default="", description="Cap standard code")
    plug_standard: str = Field(default="", description="Plug standard code")
    weldolet_spec: str = Field(default="", description="Weldolet specification")


class FittingBySize(BaseModel):
    size_inch: str = Field(..., description="Nominal pipe size in inches")
    type: str = Field(..., description="Seamless or Welded")
    fitting_type: str = Field(default="", description="Fitting connection type")
    material_spec: str = Field(default="", description="ASTM material specification")
    elbow_standard: str = Field(default="", description="Elbow standard code")
    tee_standard: str = Field(default="", description="Tee standard code")
    reducer_standard: str = Field(default="", description="Reducer standard code")
    cap_standard: str = Field(default="", description="Cap standard code")
    plug_standard: str = Field(default="", description="Plug standard code")
    weldolet_spec: str = Field(default="", description="Weldolet specification")


class ExtraFittings(BaseModel):
    coupling: str = Field(default="", description="Coupling standard")
    hex_plug: str = Field(default="", description="Hex head plug standard")
    union: str = Field(default="", description="Union standard (small bore)")
    union_large: str = Field(default="", description="Union standard (large bore)")
    olet: str = Field(default="", description="Olet spec (small bore)")
    olet_large: str = Field(default="", description="Olet spec (large bore)")
    swage: str = Field(default="", description="Swage specification")


class FlangeData(BaseModel):
    material_spec: str = Field(default="", description="ASTM material specification")
    face_type: str = Field(default="", description="Flange face type")
    flange_type: str = Field(default="", description="Flange type and standard")
    standard: str = Field(default="", description="Flange standard")


class SpectacleBlind(BaseModel):
    material_spec: str = Field(default="", description="ASTM material specification")
    standard: str = Field(default="", description="Standard code for standard sizes")
    standard_large: str = Field(default="", description="Standard code for large sizes (e.g. Spacer and blind as per ASME B 16.48)")


class BoltsNutsGaskets(BaseModel):
    stud_bolts: str = Field(default="", description="Stud bolt specification")
    hex_nuts: str = Field(default="", description="Hex nut specification")
    gasket: str = Field(default="", description="Gasket specification")


class ValveSizeEntry(BaseModel):
    """Valve VDS code for a specific size or size range."""
    size_inch: str = Field(default="", description="Pipe size in inches (e.g. '0.5', '2', '6')")
    code: str = Field(default="", description="VDS code(s) for this size, e.g. 'CHPMA1R' or 'CHSMA1R, CHDMA1R'")


class ValveData(BaseModel):
    rating: str = Field(default="", description="Valve rating")
    ball: str = Field(default="", description="Ball valve code (class-level fallback)")
    gate: str = Field(default="", description="Gate valve code (class-level fallback)")
    globe: str = Field(default="", description="Globe valve code (class-level fallback)")
    check: str = Field(default="", description="Check valve code (class-level fallback)")
    butterfly: str = Field(default="", description="Butterfly valve code (class-level fallback)")
    ball_by_size: list[ValveSizeEntry] = Field(default_factory=list, description="Ball valve codes by size")
    gate_by_size: list[ValveSizeEntry] = Field(default_factory=list, description="Gate valve codes by size")
    globe_by_size: list[ValveSizeEntry] = Field(default_factory=list, description="Globe valve codes by size")
    check_by_size: list[ValveSizeEntry] = Field(default_factory=list, description="Check valve codes by size")
    butterfly_by_size: list[ValveSizeEntry] = Field(default_factory=list, description="Butterfly valve codes by size")


class BranchChartCell(BaseModel):
    """Single cell in a branch connection chart: run_size × branch_size → connection type."""
    run_size: str = Field(..., description="Run pipe size (NPS)")
    branch_size: str = Field(..., description="Branch pipe size (NPS)")
    connection: str = Field(default="", description="Connection type: T=Tee, W=Weldolet, H=Threadolet, S=Sockolet, RT=Reducing Tee, -=Not applicable")


class BranchChart(BaseModel):
    """Branch connection chart (Appendix-1)."""
    chart_id: str = Field(default="1", description="Chart number (1, 2, 3, 4)")
    title: str = Field(default="", description="Chart title, e.g. 'CS, LTCS, SS, DSS, SDSS'")
    run_sizes: list[str] = Field(default_factory=list, description="Row headers — run pipe sizes")
    branch_sizes: list[str] = Field(default_factory=list, description="Column headers — branch pipe sizes")
    grid: list[list[str]] = Field(default_factory=list, description="2D grid [run_idx][branch_idx] of connection types")
    legend: dict[str, str] = Field(default_factory=dict, description="Legend mapping, e.g. {'W': 'WELDOLET', 'T': 'TEE'}")


class PMSResponse(BaseModel):
    piping_class: str
    rating: str = ""
    material: str
    corrosion_allowance: str
    mill_tolerance: str = ""
    design_code: str
    service: str
    branch_chart: str = ""
    hydrotest_pressure: str = ""
    pressure_temperature: PressureTemperature
    pipe_code: str = ""
    pipe_data: list[PipeSize] = Field(default_factory=list)
    fittings: FittingsData = Field(default_factory=FittingsData)
    fittings_welded: Optional[FittingsData] = None
    fittings_by_size: list[FittingBySize] = Field(default_factory=list)
    extra_fittings: ExtraFittings = Field(default_factory=ExtraFittings)
    flange: FlangeData = Field(default_factory=FlangeData)
    spectacle_blind: SpectacleBlind = Field(default_factory=SpectacleBlind)
    bolts_nuts_gaskets: BoltsNutsGaskets = Field(default_factory=BoltsNutsGaskets)
    valves: ValveData = Field(default_factory=ValveData)
    branch_charts: list[BranchChart] = Field(default_factory=list, description="Branch connection charts (Appendix-1)")
    notes: list[str] = Field(default_factory=list)


class PMSListItem(BaseModel):
    piping_class: str
    rating: str
    material: str
    corrosion_allowance: str
    service: str
    design_code: str
    min_temp: str = ""
    max_pressure_at_min_temp: str = ""
    max_pressure_at_max_temp: str = ""

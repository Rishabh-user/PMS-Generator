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
    standard: str = Field(default="", description="Standard code")


class BoltsNutsGaskets(BaseModel):
    stud_bolts: str = Field(default="", description="Stud bolt specification")
    hex_nuts: str = Field(default="", description="Hex nut specification")
    gasket: str = Field(default="", description="Gasket specification")


class ValveData(BaseModel):
    rating: str = Field(default="", description="Valve rating")
    ball: str = Field(default="", description="Ball valve code")
    gate: str = Field(default="", description="Gate valve code")
    globe: str = Field(default="", description="Globe valve code")
    check: str = Field(default="", description="Check valve code")
    butterfly: str = Field(default="", description="Butterfly valve code")


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

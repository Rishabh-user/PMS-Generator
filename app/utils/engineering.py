"""
Engineering calculation utilities for PMS generation.
Unit conversions, P-T ratings, adequacy checks per ASME standards.
"""
import math

# === Unit Conversions ===

def barg_to_psig(barg: float) -> float:
    """Convert barg to psig (1 bar = 14.5038 psi)."""
    return round(barg * 14.5038, 1)


def celsius_to_fahrenheit(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return round(c * 9 / 5 + 32, 1)


def mm_to_inch(mm: float) -> float:
    """Convert mm to inches."""
    return round(mm / 25.4, 4)


def inch_to_mm(inch: float) -> float:
    """Convert inches to mm."""
    return round(inch * 25.4, 2)


# === Design Calculations ===

def hydrotest_pressure(design_pressure: float, factor: float = 1.5) -> float:
    """Calculate hydrotest pressure per ASME B31.3 (typically 1.5x DP)."""
    return round(design_pressure * factor, 2)


def operating_pressure_estimate(design_pressure: float, factor: float = 0.8) -> float:
    """Estimate operating pressure (typically 80% of design)."""
    return round(design_pressure * factor, 2)


def operating_temp_estimate(design_temp: float, factor: float = 0.8) -> float:
    """Estimate operating temperature (typically 80% of design)."""
    return round(design_temp * factor, 1)


# === ASME B16.5 Material Groups ===

MATERIAL_GROUPS = {
    "CS": {"group": "1.1", "table": "A105/A216 WCB", "description": "Carbon Steel"},
    "CS NACE": {"group": "1.1", "table": "A105/A216 WCB", "description": "Carbon Steel (NACE)"},
    "LTCS": {"group": "1.1", "table": "A350 LF2/A352 LCB", "description": "Low Temp Carbon Steel"},
    "LTCS NACE": {"group": "1.1", "table": "A350 LF2/A352 LCB", "description": "Low Temp Carbon Steel (NACE)"},
    "SS316L": {"group": "2.3", "table": "A182 F316L/A351 CF3M", "description": "Stainless Steel 316L"},
    "SS316L NACE": {"group": "2.3", "table": "A182 F316L/A351 CF3M", "description": "Stainless Steel 316L (NACE)"},
    "Alloy 625": {"group": "3.12", "table": "B564 N06625", "description": "Nickel Alloy 625"},
    "Super Duplex": {"group": "2.6", "table": "A182 F55/A995 CD3MWCuN", "description": "Super Duplex SS"},
    "Duplex SS": {"group": "2.4", "table": "A182 F51/A995 CD3MN", "description": "Duplex Stainless Steel"},
    "GRE": {"group": "N/A", "table": "N/A", "description": "Glass Reinforced Epoxy"},
}

# ASTM Pipe Grades
PIPE_GRADES = {
    "CS": "A106 Gr.B (ASTM A106)",
    "CS NACE": "A106 Gr.B (ASTM A106)",
    "LTCS": "A333 Gr.6 (ASTM A333)",
    "LTCS NACE": "A333 Gr.6 (ASTM A333)",
    "SS316L": "A312 TP316L (ASTM A312)",
    "SS316L NACE": "A312 TP316L (ASTM A312)",
    "Alloy 625": "B444 N06625 (ASTM B444)",
    "Super Duplex": "A790 S32750 (ASTM A790)",
    "Duplex SS": "A790 S31803 (ASTM A790)",
}

# Joint Type Factors per ASME B31.3 Table A-1B
JOINT_TYPES = {
    "Seamless": {"E": 1.0, "ref": "ASME B31.3 Table A-1B"},
    "ERW": {"E": 0.85, "ref": "ASME B31.3 Table A-1B"},
    "EFW, 100% RT": {"E": 1.0, "ref": "ASME B31.3 Table A-1B"},
    "EFW": {"E": 0.85, "ref": "ASME B31.3 Table A-1B"},
    "Furnace Butt Weld": {"E": 0.60, "ref": "ASME B31.3 Table A-1B"},
}


def get_material_group(material: str) -> dict:
    """Get ASME B16.5 material group info."""
    key = material.strip()
    if key in MATERIAL_GROUPS:
        return MATERIAL_GROUPS[key]
    # Fuzzy match
    for k, v in MATERIAL_GROUPS.items():
        if k.upper() in key.upper() or key.upper() in k.upper():
            return v
    return {"group": "1.1", "table": "Unknown", "description": material}


def get_pipe_grade(material: str) -> str:
    """Get ASTM pipe grade for a material."""
    key = material.strip()
    if key in PIPE_GRADES:
        return PIPE_GRADES[key]
    for k, v in PIPE_GRADES.items():
        if k.upper() in key.upper():
            return v
    return material


def check_pt_adequacy(
    design_pressure: float,
    design_temperature: float,
    temperatures: list[float],
    pressures: list[float],
) -> dict:
    """
    Check if the P-T rating is adequate for design conditions.
    Returns the allowable pressure at design temperature and adequacy status.
    """
    if not temperatures or not pressures:
        return {"adequate": False, "allowable_pressure": 0, "message": "No P-T data available"}

    # Find the allowable pressure at the design temperature by interpolation
    temps = sorted(zip(temperatures, pressures), key=lambda x: x[0])
    temp_list = [t[0] for t in temps]
    press_list = [t[1] for t in temps]

    allowable = 0.0

    if design_temperature <= temp_list[0]:
        allowable = press_list[0]
    elif design_temperature >= temp_list[-1]:
        allowable = press_list[-1]
    else:
        # Linear interpolation
        for i in range(len(temp_list) - 1):
            if temp_list[i] <= design_temperature <= temp_list[i + 1]:
                t1, t2 = temp_list[i], temp_list[i + 1]
                p1, p2 = press_list[i], press_list[i + 1]
                # Conservative: use lower pressure at higher temp boundary
                fraction = (design_temperature - t1) / (t2 - t1)
                allowable = p1 + fraction * (p2 - p1)
                allowable = math.floor(allowable * 10) / 10  # Round down conservatively
                break

    adequate = allowable >= design_pressure
    return {
        "adequate": adequate,
        "allowable_pressure": round(allowable, 1),
        "design_pressure": design_pressure,
        "design_temperature": design_temperature,
        "message": (
            f"ADEQUATE: {allowable} barg ≥ Design {design_pressure} barg at {design_temperature}°C"
            if adequate
            else f"NOT ADEQUATE: {allowable} barg < Design {design_pressure} barg at {design_temperature}°C — Consider higher rating class"
        ),
    }


def calculate_wall_thickness(
    od_mm: float,
    design_pressure_barg: float,
    allowable_stress_mpa: float,
    joint_factor: float,
    corrosion_allowance_mm: float,
    mill_tolerance: float = 0.125,
    coefficient_y: float = 0.4,
) -> dict:
    """
    Calculate minimum wall thickness per ASME B31.3 Eq. 3a.
    t = (P × D) / (2 × (S × E × W + P × Y)) + c
    where c = corrosion allowance, and add mill tolerance.
    """
    P = design_pressure_barg * 0.1  # Convert barg to MPa
    D = od_mm
    S = allowable_stress_mpa
    E = joint_factor
    W = 1.0  # Weld strength reduction factor (1.0 for non-longitudinal welds)
    Y = coefficient_y

    # Minimum required thickness
    t_calc = (P * D) / (2 * (S * E * W + P * Y))
    t_with_ca = t_calc + corrosion_allowance_mm
    t_with_mill = t_with_ca / (1 - mill_tolerance)

    return {
        "t_calculated_mm": round(t_calc, 3),
        "t_with_ca_mm": round(t_with_ca, 3),
        "t_minimum_mm": round(t_with_mill, 3),
        "formula": "ASME B31.3 Eq. 3a: t = (P×D)/(2×(S×E×W + P×Y)) + CA",
    }

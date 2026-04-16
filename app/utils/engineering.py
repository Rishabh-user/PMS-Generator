"""
Engineering calculation utilities for PMS generation.
Unit conversions, P-T ratings, adequacy checks per ASME standards.
"""
import math

from app.utils.engineering_constants import (
    HYDROTEST_FACTOR, OPERATING_PRESSURE_FACTOR, OPERATING_TEMP_FACTOR,
    MILL_TOLERANCE_FRACTION, Y_COEFFICIENT, WELD_STRENGTH_W,
)

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

def hydrotest_pressure(design_pressure: float, factor: float = HYDROTEST_FACTOR) -> float:
    """Calculate hydrotest pressure per ASME B31.3 (typically 1.5x DP)."""
    return round(design_pressure * factor, 2)


def operating_pressure_estimate(design_pressure: float, factor: float = OPERATING_PRESSURE_FACTOR) -> float:
    """Estimate operating pressure (typically 80% of design)."""
    return round(design_pressure * factor, 2)


def operating_temp_estimate(design_temp: float, factor: float = OPERATING_TEMP_FACTOR) -> float:
    """Estimate operating temperature (typically 80% of design)."""
    return round(design_temp * factor, 1)


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
    mill_tolerance: float = MILL_TOLERANCE_FRACTION,
    coefficient_y: float = Y_COEFFICIENT,
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
    W = WELD_STRENGTH_W  # Weld strength reduction factor per ASME B31.3 Table 302.3.5
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

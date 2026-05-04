"""
Engineering calculation utilities for PMS generation.
Unit conversions, P-T ratings, adequacy checks per ASME standards.
"""
import math

from app.utils.engineering_constants import (
    HYDROTEST_FACTOR, OPERATING_PRESSURE_FACTOR, OPERATING_TEMP_FACTOR,
    MILL_TOLERANCE_FRACTION, Y_COEFFICIENT, WELD_STRENGTH_W,
    get_allowable_stress,
)

# Hydrotest reference (cold) temperature — water test is performed at
# ambient. ASME B31.3 §345.4.2(b) defines the correction factor relative
# to this temperature; we treat 38 °C (≈100 °F) as the test temperature
# baseline so it lines up exactly with the first column of Table A-1.
HYDROTEST_TEST_TEMP_C = 38.0

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
    """Calculate hydrotest pressure per ASME B31.3 §345.4.2(a) — flat 1.5×P.

    DEPRECATED for hot service: this overload ignores the §345.4.2(b)
    temperature correction. Prefer `hydrotest_pressure_corrected` whenever
    the design temperature is known, so high-temperature classes get the
    correct P_T = 1.5·P·(S_T / S) value rather than a non-conservative
    flat 1.5×P. Kept for callers that legitimately don't know the design
    temperature (e.g. early CLI utilities, ad-hoc helper scripts).
    """
    return round(design_pressure * factor, 2)


def hydrotest_pressure_corrected(
    design_pressure: float,
    design_temp_c: float,
    material_spec: str,
    test_temp_c: float = HYDROTEST_TEST_TEMP_C,
    factor: float = HYDROTEST_FACTOR,
) -> dict:
    """Calculate hydrotest pressure per ASME B31.3 §345.4.2(b) (Eq. 24).

    Formula:
        P_T = 1.5 × P × (S_T / S)

      P    : design pressure (barg)
      S_T  : allowable stress at the *test* temperature (≈ ambient)
      S    : allowable stress at the *design* temperature

    Because S decreases as temperature rises, the ratio S_T / S is ≥ 1
    for any hot service — so the corrected hydrotest is *higher* than
    the flat 1.5·P value. For ambient-or-cold service the ratio
    collapses to 1 and the formula degenerates to §345.4.2(a)'s
    1.5·P, matching the old behaviour exactly.

    Returns a dict so callers can show the correction breakdown to the
    user (and so audits stay traceable) — `pressure_barg` is the value
    to print on the PMS sheet.

    Falls back to the un-corrected 1.5·P (with `correction_applied=False`
    in the result) if material lookup fails or the test/design temps
    don't justify a correction. We never return a value LOWER than
    1.5·P, even if the stress lookup glitches — that would silently
    under-test the line.
    """
    base = design_pressure * factor
    result = {
        "pressure_barg": round(base, 2),
        "ratio_st_over_s": 1.0,
        "s_test_psi": None,
        "s_design_psi": None,
        "test_temp_c": test_temp_c,
        "design_temp_c": design_temp_c,
        "correction_applied": False,
        "reason": "design temp ≤ test temp; flat 1.5·P per §345.4.2(a)",
    }

    # No correction needed when the line never sees temperatures above
    # ambient — keeps cryogenic / cold-only classes (e.g. LNG service)
    # producing the same numbers they always have.
    if design_temp_c is None or design_temp_c <= test_temp_c:
        return result

    try:
        s_test = get_allowable_stress(material_spec or "", test_temp_c)
        s_design = get_allowable_stress(material_spec or "", design_temp_c)
        s_t_psi = s_test.get("S_psi", 0)
        s_psi = s_design.get("S_psi", 0)
        # Defensive: if either lookup returned 0 we can't safely correct
        # — fall back to the flat factor rather than divide-by-zero or
        # return a wrong number.
        if s_psi <= 0 or s_t_psi <= 0:
            result["reason"] = "stress lookup unavailable; flat 1.5·P fallback"
            return result
        ratio = s_t_psi / s_psi
        # Floor at 1.0 so we never under-test. The Code formula is a
        # *minimum* test pressure; corrections that would go below 1.5·P
        # (theoretically possible if a material gets stronger when
        # heated, which doesn't happen for any B31.3 alloy but the
        # interpolation can flutter near breakpoints) are clamped.
        ratio = max(1.0, ratio)
        result["s_test_psi"] = s_t_psi
        result["s_design_psi"] = s_psi
        result["ratio_st_over_s"] = round(ratio, 3)
        result["pressure_barg"] = round(base * ratio, 2)
        if ratio > 1.0001:
            result["correction_applied"] = True
            result["reason"] = (
                f"§345.4.2(b) Eq. 24: 1.5·P·(S_T/S) = "
                f"1.5×{design_pressure}×({s_t_psi}/{s_psi}) "
                f"= {result['pressure_barg']} barg"
            )
        else:
            result["reason"] = "S_T ≈ S at this temperature; correction negligible"
    except Exception:
        # Any unexpected stress-lookup failure → flat 1.5·P (safe). Keep
        # the call site uncluttered by try/except.
        result["reason"] = "stress lookup raised; flat 1.5·P fallback"

    return result


def operating_pressure_estimate(design_pressure: float, factor: float = OPERATING_PRESSURE_FACTOR) -> float:
    """Estimate operating pressure (typically 80% of design)."""
    return round(design_pressure * factor, 2)


def operating_temp_estimate(design_temp: float, factor: float = OPERATING_TEMP_FACTOR) -> float:
    """Estimate operating temperature (typically 80% of design)."""
    return round(design_temp * factor, 1)


def interpolate_pressure_at_temp(
    temperatures: list[float],
    pressures: list[float],
    target_temp_c: float,
) -> float:
    """Return the allowable pressure (barg) at a given temperature, linearly
    interpolated between P-T breakpoints. Floors to 1 decimal conservatively.
    Used to populate a sensible default for the "Design Point" pressure when
    the frontend first loads a class (so a 300°C design temp on class A1
    defaults to 10.2 barg, not the overall Max P of 19.6 barg)."""
    if not temperatures or not pressures:
        return 0.0
    pairs = sorted(zip(temperatures, pressures), key=lambda x: x[0])
    temp_list = [t[0] for t in pairs]
    press_list = [t[1] for t in pairs]
    if target_temp_c <= temp_list[0]:
        return round(press_list[0], 1)
    if target_temp_c >= temp_list[-1]:
        return round(press_list[-1], 1)
    for i in range(len(temp_list) - 1):
        if temp_list[i] <= target_temp_c <= temp_list[i + 1]:
            t1, t2 = temp_list[i], temp_list[i + 1]
            p1, p2 = press_list[i], press_list[i + 1]
            fraction = (target_temp_c - t1) / (t2 - t1)
            allowable = p1 + fraction * (p2 - p1)
            return math.floor(allowable * 10) / 10
    return round(press_list[-1], 1)


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

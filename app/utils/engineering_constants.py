"""
Centralized Engineering Constants for PMS Generator.

All engineering parameters used across the project are defined here.
This is the SINGLE SOURCE OF TRUTH — no other file should hardcode these values.

References:
  - ASME B31.3: Process Piping (2022 Edition)
  - ASME B36.10M: Welded and Seamless Wrought Steel Pipe
  - ASME B36.19M: Stainless Steel Pipe
  - ASME B16.5: Pipe Flanges and Flanged Fittings
"""

# ============================================================
# DESIGN FACTORS
# ============================================================

HYDROTEST_FACTOR = 1.5
"""Hydrotest pressure factor per ASME B31.3 §345.4.2 (1.5 × design pressure at ambient)."""

OPERATING_PRESSURE_FACTOR = 0.8
"""Typical operating pressure as fraction of design pressure (engineering estimate)."""

OPERATING_TEMP_FACTOR = 0.8
"""Typical operating temperature as fraction of design temperature (engineering estimate)."""


# ============================================================
# FABRICATION FACTORS
# ============================================================

MILL_TOLERANCE_PERCENT = 12.5
"""Standard mill undertolerance for seamless pipe (ASTM A106/A333/A312), as percentage."""

MILL_TOLERANCE_FRACTION = MILL_TOLERANCE_PERCENT / 100  # 0.125
"""Mill tolerance as a decimal fraction (0.125)."""

JOINT_EFFICIENCY_E = 1.0
"""Longitudinal joint efficiency for seamless pipe per ASME B31.3 Table A-1B."""

WELD_STRENGTH_W = 1.0
"""Weld strength reduction factor per ASME B31.3 Table 302.3.5 (W=1.0 for T < 510°C)."""

Y_COEFFICIENT = 0.4
"""Y coefficient per ASME B31.3 Table 304.1.1 for ferritic/alloy steel at T < 482°C (900°F).
For T ≥ 482°C: Y = 0.5; T ≥ 510°C: Y = 0.7. Currently fixed at 0.4 for standard applications."""


# ============================================================
# BORE CLASSIFICATION
# ============================================================

SMALL_BORE_CUTOFF_NPS = 2.0
"""NPS cutoff for small bore vs large bore classification (inches).
NPS ≤ this value = small bore; NPS > this value = large bore."""


# ============================================================
# AI / API CONFIGURATION
# ============================================================

AI_MAX_TOKENS = 16384
"""Maximum response tokens for Claude AI generation."""


# ============================================================
# DEFAULT VALUES (used when user doesn't specify)
# ============================================================

DEFAULT_CORROSION_ALLOWANCE = "3 mm"
"""Default corrosion allowance when not specified via API."""

DEFAULT_SERVICE = "General"
"""Default service description when not specified via API."""


# ============================================================
# ALLOWABLE STRESS TABLES — ASME B31.3 Table A-1 (psi)
# Key: temperature °C → allowable stress in psi
# ============================================================

STRESS_CS = {
    38: 20000, 93: 20000, 149: 20000, 204: 20000, 260: 18900,
    316: 17300, 343: 17000, 371: 16500, 399: 14400, 427: 10800,
}
"""CS (ASTM A106 Gr.B) and LTCS (ASTM A333 Gr.6) — per ASME B31.3 Table A-1 (USCS).
Breakpoints match Fahrenheit values: 100, 200, 300, 400, 500, 600, 650, 700, 750, 800 °F."""

STRESS_API5LX60 = {
    38: 25000, 93: 25000, 149: 25000, 204: 25000, 260: 25000,
    316: 25000, 343: 24500, 371: 23600, 399: 22500, 427: 20200,
}
"""API 5L Gr. X60 PSL-2 — higher-strength line pipe. Flat at 25,000 psi up to ~316°C.
Breakpoints at 100, 200, 300, 400, 500, 600, 650, 700, 750, 800 °F."""

STRESS_SS316L = {
    38: 16700, 93: 16700, 149: 16700, 204: 15700, 260: 14300,
    316: 13100, 371: 12000, 427: 10900, 482: 10100,
}
"""SS 316L (ASTM A312 TP316L) — per ASME B31.3 Table A-1. Flat up to 149°C, drops at 204°C."""

STRESS_SS316 = {
    38: 20000, 93: 20000, 149: 19500, 204: 19300, 260: 18900,
    316: 18600, 371: 18400, 427: 18200, 482: 17900,
}
"""SS 316 (ASTM A312 TP316) — higher stress than 316L variant."""

STRESS_SS304L = {
    38: 16700, 93: 16700, 149: 16700, 204: 15700, 260: 14100,
    316: 12800, 371: 11700, 427: 10500, 482: 9700,
}
"""SS 304L (ASTM A312 TP304L)."""

STRESS_DSS = {
    38: 31700, 93: 31000, 149: 30300, 204: 29400, 260: 28500,
    316: 27200,
}
"""DSS — Duplex (ASTM A790 UNS S31803 / S32205) — ASME B31.3 Table A-1.
Rated only up to 316°C (600°F) due to 475°C embrittlement risk."""

STRESS_SDSS = {
    38: 38700, 93: 38100, 149: 36700, 204: 35100, 260: 33800,
    316: 32700,
}
"""SDSS — Super Duplex (ASTM A790 UNS S32750) — ASME B31.3 Table A-1.
Rated only up to 316°C (600°F)."""

STRESS_CUNI = {
    38: 10000, 93: 10000, 149: 9700, 204: 9400, 260: 8900, 316: 8600,
}
"""CuNi 90/10 (ASTM B466 C70600) — ASME B31.3."""

STRESS_TITANIUM_B861_GR2 = {
    16: 16700, 38: 16700, 93: 14200, 149: 12900, 175: 11200, 204: 10100, 260: 8900,
}
"""Titanium B861 Gr. 2 — commercial-pure titanium line pipe."""

STRESS_COPPER_C12200_H80 = {
    38: 15000, 66: 15000, 93: 13600, 121: 12000, 149: 10400, 177: 8700,
}
"""Copper DHP (ASTM B42 UNS C12200) H80 temper — hard-drawn condition."""

STRESS_COPPER_C12200_H55 = {
    38: 12000, 66: 11400, 93: 10900, 121: 10200, 149: 9500, 177: 8700,
}
"""Copper DHP (ASTM B42 UNS C12200) H55 temper — half-hard condition."""

# Mapping: material keyword → stress table (used by both Python and JS via API)
STRESS_TABLES = {
    "CS": STRESS_CS,
    "LTCS": STRESS_CS,
    "GALV": STRESS_CS,
    "API5LX60": STRESS_API5LX60,
    "SS316L": STRESS_SS316L,
    "SS316": STRESS_SS316,
    "SS304L": STRESS_SS304L,
    "SS": STRESS_SS316L,       # default SS
    "DSS": STRESS_DSS,
    "SDSS": STRESS_SDSS,
    "CUNI": STRESS_CUNI,
    "TITANIUM": STRESS_TITANIUM_B861_GR2,
    "COPPER_H80": STRESS_COPPER_C12200_H80,
    "COPPER_H55": STRESS_COPPER_C12200_H55,
}


def _detect_stress_table(material: str):
    """Return the appropriate stress table based on material keywords.
    Uses careful matching to avoid false positives like 'CLASS' matching 'SS'."""
    import re
    mat = material.upper()

    # SDSS — super duplex (check FIRST, most specific)
    if re.search(r"\bSDSS\b|S32750|SUPER\s*DUPLEX", mat):
        return STRESS_SDSS
    # DSS — duplex (S31803, S32205 are both A790 duplex grades)
    if re.search(r"\bDSS\b|S31803|S32205|\bDUPLEX\b", mat):
        return STRESS_DSS
    # API 5L Gr. X60 line pipe — check before CS since it contains "API"
    if re.search(r"API\s*5L.*X\s*60|X60\s*PSL", mat):
        return STRESS_API5LX60
    # Stainless: check which grade is PRIMARY (first "TP 3xx" in the string)
    # "TP 316/316L" → primary 316 (higher stress); "TP 316L/316L" → 316L
    tp_match = re.search(r"TP\s*(316L|304L|316|304)", mat)
    if tp_match:
        grade = tp_match.group(1)
        if grade == "316L": return STRESS_SS316L
        if grade == "304L": return STRESS_SS304L
        if grade == "316":  return STRESS_SS316
        if grade == "304":  return STRESS_SS316  # fallback near-match
    # Fallback: match 316L or 304L elsewhere in string
    if re.search(r"\b316L\b", mat):
        return STRESS_SS316L
    if re.search(r"\b304L\b", mat):
        return STRESS_SS304L
    if re.search(r"\b316\b", mat):
        return STRESS_SS316
    # Titanium
    if re.search(r"TITANIUM|\bB\s*861\b|\bTI\s+GR", mat):
        return STRESS_TITANIUM_B861_GR2
    # Copper DHP H80 / H55
    if re.search(r"C\s*12200.*H\s*80|H80\b", mat):
        return STRESS_COPPER_C12200_H80
    if re.search(r"C\s*12200.*H\s*55|H55\b", mat):
        return STRESS_COPPER_C12200_H55
    if re.search(r"C\s*12200|\bB\s*42\b", mat):
        return STRESS_COPPER_C12200_H80  # default copper temper
    # CuNi
    if re.search(r"CU\s*NI|CU-NI|C70600|C71500|\bB\s*466\b", mat):
        return STRESS_CUNI
    # Generic stainless fallback — but only match "STAINLESS" word or "TP 3xx" pattern
    if re.search(r"STAINLESS|TP\s*3\d\d", mat):
        return STRESS_SS316L
    # Galvanised carbon steel — falls back to CS table
    if re.search(r"GALV", mat):
        return STRESS_CS
    # Default: Carbon steel (covers A106, A333, A350, A234, A671, API 5L Gr. B, etc.)
    return STRESS_CS


def get_allowable_stress(material: str, temp_c: float) -> dict:
    """
    Get allowable stress S(T) for a material at a given temperature.
    Returns {'S_psi': int, 'S_mpa': float}.

    Uses linear interpolation between ASME B31.3 Table A-1 breakpoints.
    Breakpoints in our tables use Celsius values corresponding to ASME's
    Fahrenheit rows (38, 93, 149, 204, 260, 316°C = 100, 200, 300, 400, 500, 600°F).
    """
    table = _detect_stress_table(material)

    temps = sorted(table.keys())
    T = temp_c

    if T <= temps[0]:
        S = table[temps[0]]
    elif T >= temps[-1]:
        S = table[temps[-1]]
    else:
        S = table[temps[0]]  # fallback
        for i in range(len(temps) - 1):
            if temps[i] <= T <= temps[i + 1]:
                frac = (T - temps[i]) / (temps[i + 1] - temps[i])
                S = table[temps[i]] + frac * (table[temps[i + 1]] - table[temps[i]])
                S = round(S / 100) * 100  # round to nearest 100 psi
                break

    return {"S_psi": int(S), "S_mpa": round(S * 0.00689476, 1)}

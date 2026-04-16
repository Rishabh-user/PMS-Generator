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

STRESS_CS = {38: 20000, 50: 20000, 100: 20000, 150: 18900, 200: 17700, 250: 16500, 300: 15600, 350: 14800, 400: 12100}
"""CS (ASTM A106 Gr.B) and LTCS (ASTM A333 Gr.6) — same stress values."""

STRESS_SS316L = {38: 16700, 50: 16700, 100: 16700, 150: 14500, 200: 13300, 250: 12500, 300: 11800, 350: 11300, 400: 10900}
"""SS 316L (ASTM A312 TP316L)."""

STRESS_SS304L = {38: 16700, 50: 16700, 100: 16700, 150: 13800, 200: 12700, 250: 11800, 300: 11200, 350: 10700, 400: 10300}
"""SS 304L (ASTM A312 TP304L)."""

STRESS_DSS = {38: 25000, 50: 25000, 100: 23300, 150: 22000, 200: 21000, 250: 20400, 300: 20000}
"""DSS — Duplex (ASTM A790 UNS S31803)."""

STRESS_SDSS = {38: 36700, 50: 36700, 100: 35000, 150: 33100, 200: 31900, 250: 31000, 300: 30500}
"""SDSS — Super Duplex (ASTM A790 UNS S32750)."""

STRESS_CUNI = {38: 10000, 50: 10000, 100: 10000, 150: 10000, 200: 9400, 250: 8600}
"""CuNi 90/10 (ASTM B466 C70600)."""

# Mapping: material keyword → stress table (used by both Python and JS via API)
STRESS_TABLES = {
    "CS": STRESS_CS,
    "LTCS": STRESS_CS,
    "GALV": STRESS_CS,
    "SS316L": STRESS_SS316L,
    "SS304L": STRESS_SS304L,
    "SS": STRESS_SS316L,       # default SS
    "DSS": STRESS_DSS,
    "SDSS": STRESS_SDSS,
    "CUNI": STRESS_CUNI,
}


def get_allowable_stress(material: str, temp_c: float) -> dict:
    """
    Get allowable stress S(T) for a material at a given temperature.
    Returns {'S_psi': int, 'S_mpa': float}.

    Uses linear interpolation between ASME table breakpoints.
    """
    mat = material.upper()

    # Determine which table to use
    table = STRESS_CS  # default
    if "SDSS" in mat or "S32750" in mat or "SUPER DUPLEX" in mat:
        table = STRESS_SDSS
    elif "DSS" in mat or "S31803" in mat or "DUPLEX" in mat:
        table = STRESS_DSS
    elif "316L" in mat:
        table = STRESS_SS316L
    elif "304L" in mat:
        table = STRESS_SS304L
    elif "SS" in mat or "STAINLESS" in mat:
        table = STRESS_SS316L
    elif "CUNI" in mat or "CU-NI" in mat or "COPPER" in mat or "C70600" in mat:
        table = STRESS_CUNI
    elif "GALV" in mat:
        table = STRESS_CS

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

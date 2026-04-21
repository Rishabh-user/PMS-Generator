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
    # Generic copper: ASTM B42, ASTM B88 (Type K/L/M), or plain "Copper"
    if re.search(r"C\s*12200|\bB\s*42\b|\bB\s*88\b|\bCOPPER\b", mat):
        return STRESS_COPPER_C12200_H80  # default copper temper
    # CuNi — check BEFORE plain "Copper" is already matched above (CuNi doesn't contain "COPPER")
    if re.search(r"CU\s*NI|CU-NI|C70600|C71500|\bB\s*466\b", mat):
        return STRESS_CUNI
    # 6 MO / AL-6XN / UNS N08367 super-austenitic tubing — closest Table A-1
    # allowable-stress profile in our tables is SDSS (both peak ~36 ksi @ 38 °C
    # and drop smoothly through 316 °C). Approximate; used by validator only.
    if re.search(r"\b6\s*MO\b|\bN08367\b|\bAL\s*6XN\b|UNS\s*N08367", mat):
        return STRESS_SDSS
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


# ============================================================
# ASME B36.10M / B36.19M — PIPE OD AND WALL THICKNESS TABLES
# ============================================================
# Single source of truth for standard-schedule wall thicknesses and outside
# diameters. Used post-AI to overwrite AI-generated WT/OD with the correct
# standard values. The AI selects the SCHEDULE per class rules in the prompt;
# this code looks up the standard WT for that (NPS, schedule) pair.
#
# NPS keys are strings matching how the AI emits size_inch: "0.5", "0.75",
# "1", "1.5", "2", "3", ... Schedule keys are the bare form ("160", "80",
# "STD", "XS", "XXS", "5S", "10S", "40S", "80S"). The lookup helper strips
# any "SCH " prefix from the AI's schedule string before matching.
#
# Non-ASME pipe codes (CuNi EEMUA 234, Copper ASTM B42, GRE manufacturer
# std, CPVC ASTM F441, Tubing ASTM A269) are NOT corrected — the AI's
# values stand. Gate on pipe_code prefix in the lookup helper below.

ASME_B3610M_WT = {
    "0.5":  {"10": 1.24, "20": 1.65, "30": 2.11, "40": 2.77, "STD": 2.77, "80": 3.73, "XS": 3.73, "160": 4.78, "XXS": 7.47},
    "0.75": {"10": 1.65, "20": 1.65, "30": 2.11, "40": 2.87, "STD": 2.87, "80": 3.91, "XS": 3.91, "160": 5.56, "XXS": 7.82},
    "1":    {"10": 1.65, "20": 2.11, "30": 2.41, "40": 3.38, "STD": 3.38, "80": 4.55, "XS": 4.55, "160": 6.35, "XXS": 9.09},
    "1.5":  {"10": 1.65, "20": 2.11, "30": 2.41, "40": 3.68, "STD": 3.68, "80": 5.08, "XS": 5.08, "160": 7.14, "XXS": 10.16},
    "2":    {"10": 2.11, "20": 2.77, "30": 2.90, "40": 3.91, "STD": 3.91, "80": 5.54, "XS": 5.54, "160": 8.74, "XXS": 11.07},
    "3":    {"10": 2.11, "20": 3.05, "30": 4.78, "40": 5.49, "STD": 5.49, "80": 7.62, "XS": 7.62, "160": 11.13, "XXS": 15.24},
    "4":    {"10": 2.11, "20": 3.05, "30": 4.78, "40": 6.02, "STD": 6.02, "80": 8.56, "XS": 8.56, "120": 11.13, "160": 13.49, "XXS": 17.12},
    "6":    {"10": 2.77, "20": 3.40, "30": 6.35, "40": 7.11, "STD": 7.11, "80": 10.97, "XS": 10.97, "120": 14.27, "160": 18.26, "XXS": 21.95},
    "8":    {"10": 3.76, "20": 6.35, "30": 7.04, "40": 8.18, "STD": 8.18, "60": 10.31, "80": 12.70, "XS": 12.70, "100": 15.09, "120": 18.26, "140": 20.62, "160": 23.01, "XXS": 22.23},
    "10":   {"10": 4.19, "20": 6.35, "30": 7.80, "40": 9.27, "STD": 9.27, "60": 12.70, "XS": 12.70, "80": 15.09, "100": 18.26, "120": 21.44, "140": 25.40, "160": 28.58, "XXS": 25.40},
    "12":   {"10": 4.57, "20": 6.35, "30": 8.38, "STD": 9.53, "40": 10.31, "60": 14.27, "XS": 12.70, "80": 17.48, "100": 21.44, "120": 25.40, "140": 28.58, "160": 33.32, "XXS": 25.40},
    # NPS 14-24 SCH 10/20/30 corrections: the prior values were conflated
    # with B36.19M 10S/20S. Authoritative ASME B36.10M values below.
    "14":   {"10": 6.35, "20": 7.92, "30": 9.53, "STD": 9.53, "40": 11.13, "60": 15.09, "XS": 12.70, "80": 19.05, "100": 23.83, "120": 27.79, "140": 31.75, "160": 35.71},
    "16":   {"10": 6.35, "20": 7.92, "30": 9.53, "STD": 9.53, "40": 12.70, "60": 16.66, "XS": 12.70, "80": 21.44, "100": 26.19, "120": 30.96, "140": 36.53, "160": 40.49},
    "18":   {"10": 6.35, "20": 7.92, "30": 11.13, "STD": 9.53, "40": 14.27, "60": 19.05, "XS": 12.70, "80": 23.83, "100": 29.36, "120": 34.93, "140": 39.67, "160": 45.24},
    "20":   {"10": 6.35, "20": 9.53, "30": 12.70, "STD": 9.53, "40": 15.09, "60": 20.62, "XS": 12.70, "80": 26.19, "100": 32.54, "120": 38.10, "140": 44.45, "160": 50.01},
    "22":   {"10": 6.35, "20": 9.53, "STD": 9.53, "60": 22.23, "XS": 12.70, "80": 28.58, "100": 34.93, "120": 41.28, "140": 47.63, "160": 53.98},
    "24":   {"10": 6.35, "20": 9.53, "STD": 9.53, "30": 14.27, "40": 17.48, "60": 24.61, "XS": 12.70, "80": 30.96, "100": 38.89, "120": 46.02, "140": 52.37, "160": 59.54},
    "26":   {"10": 7.92, "STD": 9.53, "20": 12.70, "XS": 12.70},
    "28":   {"10": 7.92, "STD": 9.53, "20": 12.70, "XS": 12.70, "30": 15.88},
    "30":   {"10": 7.92, "STD": 9.53, "20": 12.70, "XS": 12.70, "30": 15.88},
    "32":   {"10": 7.92, "STD": 9.53, "20": 12.70, "XS": 12.70, "30": 15.88, "40": 17.48},
    "36":   {"10": 7.92, "STD": 9.53, "20": 12.70, "XS": 12.70, "30": 15.88, "40": 19.05},
}
"""ASME B36.10M wall thicknesses in mm. Key: NPS string → {schedule: WT_mm}."""

ASME_B3619M_WT = {
    "0.5":  {"5S": 1.65, "10S": 2.11, "40S": 2.77, "80S": 3.73},
    "0.75": {"5S": 1.65, "10S": 2.11, "40S": 2.87, "80S": 3.91},
    "1":    {"5S": 1.65, "10S": 2.77, "40S": 3.38, "80S": 4.55},
    "1.5":  {"5S": 1.65, "10S": 2.77, "40S": 3.68, "80S": 5.08},
    "2":    {"5S": 1.65, "10S": 2.77, "40S": 3.91, "80S": 5.54},
    "3":    {"5S": 2.11, "10S": 3.05, "40S": 5.49, "80S": 7.62},
    "4":    {"5S": 2.11, "10S": 3.05, "40S": 6.02, "80S": 8.56},
    "6":    {"5S": 2.77, "10S": 3.40, "40S": 7.11, "80S": 10.97},
    "8":    {"5S": 2.77, "10S": 3.76, "40S": 8.18, "80S": 12.70},
    "10":   {"5S": 3.40, "10S": 4.19, "40S": 9.27, "80S": 12.70},
    "12":   {"5S": 3.96, "10S": 4.57, "40S": 9.53, "80S": 12.70},
    # NPS 14-24: B36.19M 40S = STD = 9.53, 80S = XS = 12.70 (identical to
    # B36.10M STD/XS values at these sizes). Needed for A10/A10N large
    # sizes where the prompt rule uses 40S up to 20" and 24".
    "14":   {"5S": 3.96, "10S": 4.78, "40S": 9.53, "80S": 12.70},
    "16":   {"5S": 4.19, "10S": 4.78, "40S": 9.53, "80S": 12.70},
    "18":   {"5S": 4.19, "10S": 4.78, "40S": 9.53, "80S": 12.70},
    "20":   {"5S": 4.78, "10S": 5.54, "40S": 9.53, "80S": 12.70},
    "22":   {"5S": 4.78, "10S": 5.54, "40S": 9.53, "80S": 12.70},
    "24":   {"5S": 5.54, "10S": 6.35, "40S": 9.53, "80S": 12.70},
    "30":   {"5S": 6.35, "10S": 7.92},
}
"""ASME B36.19M S-schedule wall thicknesses in mm (SS/DSS/SDSS). For non-S
schedules (STD, XS, 40, 80, 160, etc.) on SS pipe, the lookup falls through
to ASME_B3610M_WT — B36.19M and B36.10M share the same OD series, so
non-S schedule WTs are identical for sizes covered by both."""

ASME_PIPE_OD = {
    "0.5": 21.3, "0.75": 26.7, "1": 33.4, "1.5": 48.3, "2": 60.3,
    "3": 88.9, "4": 114.3, "6": 168.3, "8": 219.1, "10": 273.0,
    "12": 323.8, "14": 355.6, "16": 406.4, "18": 457.0, "20": 508.0,
    "22": 559.0, "24": 610.0, "26": 660.4, "28": 711.2, "30": 762.0,
    "32": 812.8, "36": 914.4,
}
"""ASME B36.10M / B36.19M standard outside diameters in mm (identical
between the two standards for sizes covered by both)."""


def _normalize_nps(nps) -> str:
    """Normalize NPS input to match the string keys used in the OD/WT tables."""
    if nps is None:
        return ""
    s = str(nps).strip().replace('"', "").replace("'", "").replace(" ", "")
    # Fraction → decimal (some specs show "1/2" or "1-1/2")
    if s in ("1/2",): return "0.5"
    if s in ("3/4",): return "0.75"
    if s in ("1-1/2", "1 1/2"): return "1.5"
    if s in ("2-1/2", "2 1/2"): return "2.5"
    # Strip trailing .0 ("1.0" → "1", "24.0" → "24") to match keys
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return s


def _normalize_schedule_key(schedule) -> str:
    """Strip any 'SCH ' prefix and uppercase the schedule name."""
    if schedule is None:
        return ""
    s = str(schedule).strip().upper()
    # "SCH 160" / "SCH160" / "SCHEDULE 160" → "160"
    for prefix in ("SCHEDULE ", "SCH ", "SCH"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    return s


def lookup_wall_thickness(nps, schedule, pipe_code: str | None = None) -> float | None:
    """Look up standard ASME B36.10M / B36.19M wall thickness in mm.

    Returns None if:
      - schedule is "-", blank, or an unknown code (calculated WT → preserve AI value)
      - NPS is not in the table (non-standard size)
      - pipe_code indicates a non-ASME system (CuNi, Copper, GRE, CPVC, Tubing)

    S-suffix schedules (5S, 10S, 40S, 80S) → B36.19M table.
    Non-S schedules → B36.10M table (same OD/WT as B36.19M for covered sizes).
    """
    # Skip non-ASME pipe codes — their WT comes from different standards
    code = (pipe_code or "").upper()
    if code and not ("B 36.10M" in code or "B36.10M" in code or "B 36.19M" in code or "B36.19M" in code):
        return None

    nps_key = _normalize_nps(nps)
    sched_key = _normalize_schedule_key(schedule)
    if not nps_key or not sched_key or sched_key == "-":
        return None

    # S-suffix schedules = B36.19M. Be careful: "XS" ends in "S" but is
    # NOT an S-schedule, it's B36.10M XS. The S-schedules are numeric + "S".
    _S_SCHEDULES = {"5S", "10S", "40S", "80S"}
    if sched_key in _S_SCHEDULES:
        row = ASME_B3619M_WT.get(nps_key, {})
        return row.get(sched_key)

    row = ASME_B3610M_WT.get(nps_key, {})
    return row.get(sched_key)


def lookup_od(nps, pipe_code: str | None = None) -> float | None:
    """Look up standard ASME pipe OD in mm. Returns None for non-ASME pipe codes."""
    code = (pipe_code or "").upper()
    if code and not ("B 36.10M" in code or "B36.10M" in code or "B 36.19M" in code or "B36.19M" in code):
        return None
    return ASME_PIPE_OD.get(_normalize_nps(nps))

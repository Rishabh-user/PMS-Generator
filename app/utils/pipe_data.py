"""
Deterministic pipe wall thickness lookup tables from ASME B36.10M and B36.19M.

These tables replace AI-generated WT values with exact standard values.
Source: Project wall thickness calculation sheet (20171-SPOG-80000-PP-CL-0001_Rev03).

Usage:
    from app.utils.pipe_data import get_wall_thickness, get_od_mm

    od = get_od_mm("2")           # -> 60.3
    wt = get_wall_thickness(60.3, "80")  # -> 5.54
"""

# === NPS (inch string) -> OD (mm) per ASME B36.10M / B36.19M (steel pipe) ===
NPS_TO_OD: dict[str, float] = {
    "0.5":  21.3,
    "0.75": 26.7,
    "1":    33.4,
    "1.5":  48.3,
    "2":    60.3,
    "2.5":  73.0,
    "3":    88.9,
    "4":    114.3,
    "6":    168.3,
    "8":    219.1,
    "10":   273.1,
    "12":   323.9,
    "14":   355.6,
    "16":   406.4,
    "18":   457.0,
    "20":   508.0,
    "22":   559.0,
    "24":   610.0,
    "26":   660.0,
    "28":   711.0,
    "30":   762.0,
    "32":   813.0,
    "36":   914.4,
}

# Reverse lookup: OD -> NPS
OD_TO_NPS: dict[float, str] = {v: k for k, v in NPS_TO_OD.items()}


# === EEMUA 234 OD table for CuNi 90/10 ===
# Small-bore sizes have different OD from ASME. Large sizes converge or diverge.
EEMUA_234_OD: dict[str, float] = {
    "0.5":  16.0,
    "0.75": 25.0,
    "1":    30.0,
    "1.5":  44.5,
    "2":    57.0,
    "2.5":  73.0,    # ASME equivalent
    "3":    88.9,    # ASME equivalent
    "4":    108.0,
    "6":    159.0,
    "8":    219.1,   # ASME equivalent
    "10":   267.0,
    "12":   323.9,   # ASME equivalent
    "14":   368.0,
    "16":   419.0,
    "18":   457.2,
    "20":   508.0,
    "22":   559.0,
    "24":   610.0,
    "28":   711.0,
    "30":   762.0,
}

# === Tubing OD — nominal size IS the OD in inches ===
def _tubing_od(nps: str) -> float | None:
    """Tubing OD in mm: nominal NPS inches × 25.4."""
    try:
        return round(float(nps) * 25.4, 2)
    except (TypeError, ValueError):
        return None


def get_od_for_material(nps: str, material: str | None) -> float | None:
    """Material-aware OD lookup.

    Returns OD in mm for the given NPS, selecting the appropriate OD table
    based on the material standard:
      - CuNi 90/10 → EEMUA 234
      - GRE / CPVC / Plastic → returns None (use AI-generated value as-is)
      - Tubing → nominal NPS × 25.4
      - All steel (CS/LTCS/SS/DSS/SDSS/GALV) → ASME B36.10M/B36.19M
    """
    nps_str = str(nps).strip().strip('"')
    mat = (material or "").upper()

    # Tubing: OD = nominal NPS in inches converted to mm
    if "TUBING" in mat or nps_str.startswith("T80") or nps_str.startswith("T90"):
        return _tubing_od(nps_str)

    # CuNi 90/10 uses EEMUA 234 ODs
    if "CUNI" in mat or "CU-NI" in mat or "CU NI" in mat or "C70600" in mat or "EEMUA" in mat:
        return EEMUA_234_OD.get(nps_str)

    # GRE / CPVC / plastic — ODs are manufacturer-specific, preserve AI value
    if "GRE" in mat or "CPVC" in mat or "PVC" in mat or "PLASTIC" in mat or "FRP" in mat or "EPOXY" in mat:
        return None

    # Default: ASME steel pipe
    return NPS_TO_OD.get(nps_str)


# === ASME B36.10M Wall Thickness Table (CS/LTCS/GALV) ===
# Key: (OD_mm, schedule_name) -> WT_mm
# Schedule names: "5S", "10", "10S", "20", "30", "40", "40S", "60", "80", "80S",
#                 "100", "120", "140", "160", "STD", "XS", "XXS"
#
# Note: For NPS < 8", SCH 40 = STD and SCH 80 = XS (per ASME B36.10M).
#       For NPS >= 8", STD, XS are distinct from numbered schedules.
#       "S" suffix schedules (10S, 40S, 80S) are from ASME B36.19M (stainless).

_WT_TABLE: dict[tuple[float, str], float] = {
    # ---- NPS 0.5" (OD 21.3mm) ----
    (21.3, "5S"):   1.65,
    (21.3, "10S"):  2.11,
    (21.3, "40"):   2.77,
    (21.3, "40S"):  2.77,
    (21.3, "STD"):  2.77,
    (21.3, "80"):   3.73,
    (21.3, "80S"):  3.73,
    (21.3, "XS"):   3.73,
    (21.3, "160"):  4.78,
    (21.3, "XXS"):  7.47,

    # ---- NPS 0.75" (OD 26.7mm) ----
    (26.7, "5S"):   1.65,
    (26.7, "10S"):  2.11,
    (26.7, "40"):   2.87,
    (26.7, "40S"):  2.87,
    (26.7, "STD"):  2.87,
    (26.7, "80"):   3.91,
    (26.7, "80S"):  3.91,
    (26.7, "XS"):   3.91,
    (26.7, "160"):  5.56,
    (26.7, "XXS"):  7.82,

    # ---- NPS 1" (OD 33.4mm) ----
    (33.4, "5S"):   1.65,
    (33.4, "10S"):  2.77,
    (33.4, "40"):   3.38,
    (33.4, "40S"):  3.38,
    (33.4, "STD"):  3.38,
    (33.4, "80"):   4.55,
    (33.4, "80S"):  4.55,
    (33.4, "XS"):   4.55,
    (33.4, "160"):  6.35,
    (33.4, "XXS"):  9.09,

    # ---- NPS 1.5" (OD 48.3mm) ----
    (48.3, "5S"):   1.65,
    (48.3, "10S"):  2.77,
    (48.3, "40"):   3.68,
    (48.3, "40S"):  3.68,
    (48.3, "STD"):  3.68,
    (48.3, "80"):   5.08,
    (48.3, "80S"):  5.08,
    (48.3, "XS"):   5.08,
    (48.3, "160"):  7.14,
    (48.3, "XXS"):  10.16,

    # ---- NPS 2" (OD 60.3mm) ----
    (60.3, "5S"):   1.65,
    (60.3, "10S"):  2.77,
    (60.3, "40"):   3.91,
    (60.3, "40S"):  3.91,
    (60.3, "STD"):  3.91,
    (60.3, "80"):   5.54,
    (60.3, "80S"):  5.54,
    (60.3, "XS"):   5.54,
    (60.3, "160"):  8.74,
    (60.3, "XXS"):  11.07,

    # ---- NPS 2.5" (OD 73mm) ----
    (73.0, "5S"):   2.11,
    (73.0, "10S"):  3.05,
    (73.0, "40"):   5.16,
    (73.0, "40S"):  5.16,
    (73.0, "STD"):  5.16,
    (73.0, "80"):   7.01,
    (73.0, "80S"):  7.01,
    (73.0, "XS"):   7.01,
    (73.0, "160"):  9.53,
    (73.0, "XXS"):  14.02,

    # ---- NPS 3" (OD 88.9mm) ----
    (88.9, "5S"):   1.65,
    (88.9, "10S"):  3.05,
    (88.9, "40"):   5.49,
    (88.9, "40S"):  5.49,
    (88.9, "STD"):  5.49,
    (88.9, "80"):   7.62,
    (88.9, "80S"):  7.62,
    (88.9, "XS"):   7.62,
    (88.9, "160"):  11.13,
    (88.9, "XXS"):  15.24,

    # ---- NPS 4" (OD 114.3mm) ----
    (114.3, "5S"):   1.65,
    (114.3, "10S"):  3.05,
    (114.3, "40"):   6.02,
    (114.3, "40S"):  6.02,
    (114.3, "STD"):  6.02,
    (114.3, "80"):   8.56,
    (114.3, "80S"):  8.56,
    (114.3, "XS"):   8.56,
    (114.3, "120"):  11.13,
    (114.3, "160"):  13.49,
    (114.3, "XXS"):  17.12,

    # ---- NPS 6" (OD 168.3mm) ----
    (168.3, "5S"):   1.65,
    (168.3, "10S"):  3.40,
    (168.3, "40"):   7.11,
    (168.3, "40S"):  7.11,
    (168.3, "STD"):  7.11,
    (168.3, "80"):   10.97,
    (168.3, "80S"):  10.97,
    (168.3, "XS"):   10.97,
    (168.3, "120"):  14.27,
    (168.3, "160"):  18.26,
    (168.3, "XXS"):  21.95,

    # ---- NPS 8" (OD 219.1mm) ----
    (219.1, "5S"):   2.77,
    (219.1, "10"):   3.76,
    (219.1, "10S"):  3.76,
    (219.1, "20"):   6.35,
    (219.1, "30"):   7.04,
    (219.1, "40"):   8.18,
    (219.1, "40S"):  8.18,
    (219.1, "STD"):  8.18,
    (219.1, "60"):   10.31,
    (219.1, "80"):   12.70,
    (219.1, "80S"):  12.70,
    (219.1, "XS"):   12.70,
    (219.1, "100"):  15.09,
    (219.1, "120"):  18.26,
    (219.1, "140"):  20.62,
    (219.1, "160"):  23.01,
    (219.1, "XXS"):  22.23,

    # ---- NPS 10" (OD 273.1mm) ----
    (273.1, "5S"):   3.40,
    (273.1, "10"):   3.40,
    (273.1, "10S"):  4.19,
    (273.1, "20"):   6.35,
    (273.1, "30"):   7.80,
    (273.1, "40"):   9.27,
    (273.1, "40S"):  9.27,
    (273.1, "STD"):  9.27,
    (273.1, "60"):   12.70,
    (273.1, "80"):   15.09,
    (273.1, "80S"):  12.70,
    (273.1, "XS"):   12.70,
    (273.1, "100"):  18.26,
    (273.1, "120"):  21.44,
    (273.1, "140"):  25.40,
    (273.1, "160"):  28.58,

    # ---- NPS 12" (OD 323.9mm) ----
    (323.9, "5S"):   3.96,
    (323.9, "10"):   3.96,
    (323.9, "10S"):  4.57,
    (323.9, "20"):   6.35,
    (323.9, "30"):   8.38,
    (323.9, "STD"):  9.53,
    (323.9, "40"):   10.31,
    (323.9, "40S"):  9.53,
    (323.9, "XS"):   12.70,
    (323.9, "60"):   14.27,
    (323.9, "80"):   17.48,
    (323.9, "80S"):  12.70,
    (323.9, "100"):  21.44,
    (323.9, "120"):  25.40,
    (323.9, "140"):  28.58,
    (323.9, "160"):  33.32,

    # ---- NPS 14" (OD 355.6mm) ----
    (355.6, "5S"):   3.96,
    (355.6, "10"):   6.35,
    (355.6, "10S"):  4.78,
    (355.6, "20"):   7.92,
    (355.6, "30"):   9.53,
    (355.6, "STD"):  9.53,
    (355.6, "40"):   11.13,
    (355.6, "40S"):  9.53,
    (355.6, "XS"):   12.70,
    (355.6, "60"):   15.09,
    (355.6, "80"):   19.05,
    (355.6, "100"):  23.83,
    (355.6, "120"):  27.79,
    (355.6, "140"):  31.75,
    (355.6, "160"):  35.71,

    # ---- NPS 16" (OD 406.4mm) ----
    (406.4, "5S"):   4.19,
    (406.4, "10"):   6.35,
    (406.4, "10S"):  4.78,
    (406.4, "20"):   7.92,
    (406.4, "30"):   9.53,
    (406.4, "STD"):  9.53,
    (406.4, "40"):   12.70,
    (406.4, "40S"):  9.53,
    (406.4, "XS"):   12.70,
    (406.4, "60"):   16.66,
    (406.4, "80"):   21.44,
    (406.4, "80S"):  12.70,
    (406.4, "100"):  26.19,
    (406.4, "120"):  30.96,
    (406.4, "140"):  36.53,
    (406.4, "160"):  40.49,

    # ---- NPS 18" (OD 457mm) ----
    (457.0, "5S"):   4.19,
    (457.0, "10"):   6.35,
    (457.0, "10S"):  4.78,
    (457.0, "20"):   7.92,
    (457.0, "30"):   11.13,
    (457.0, "STD"):  9.53,
    (457.0, "40"):   14.27,
    (457.0, "40S"):  9.53,
    (457.0, "XS"):   12.70,
    (457.0, "60"):   19.05,
    (457.0, "80"):   23.83,
    (457.0, "80S"):  12.70,
    (457.0, "100"):  29.36,
    (457.0, "120"):  34.93,
    (457.0, "140"):  39.67,
    (457.0, "160"):  45.24,

    # ---- NPS 20" (OD 508mm) ----
    (508.0, "5S"):   4.78,
    (508.0, "10"):   6.35,
    (508.0, "10S"):  5.54,
    (508.0, "20"):   9.53,
    (508.0, "30"):   12.70,
    (508.0, "STD"):  9.53,
    (508.0, "40"):   15.09,
    (508.0, "40S"):  9.53,
    (508.0, "XS"):   12.70,
    (508.0, "60"):   20.62,
    (508.0, "80"):   26.19,
    (508.0, "80S"):  12.70,
    (508.0, "100"):  32.54,
    (508.0, "120"):  38.10,
    (508.0, "140"):  44.45,
    (508.0, "160"):  50.01,

    # ---- NPS 22" (OD 559mm) ----
    (559.0, "5S"):   4.78,
    (559.0, "10"):   6.35,
    (559.0, "10S"):  5.54,
    (559.0, "20"):   9.53,
    (559.0, "30"):   12.70,
    (559.0, "STD"):  9.53,
    (559.0, "40S"):  9.53,
    (559.0, "XS"):   12.70,
    (559.0, "60"):   22.23,
    (559.0, "80"):   28.58,
    (559.0, "100"):  34.93,
    (559.0, "120"):  41.28,
    (559.0, "140"):  47.63,
    (559.0, "160"):  53.98,

    # ---- NPS 24" (OD 610mm) ----
    (610.0, "5S"):   5.54,
    (610.0, "10"):   6.35,
    (610.0, "10S"):  6.35,
    (610.0, "20"):   9.53,
    (610.0, "30"):   14.27,
    (610.0, "STD"):  9.53,
    (610.0, "40"):   17.48,
    (610.0, "40S"):  9.53,
    (610.0, "XS"):   12.70,
    (610.0, "60"):   24.61,
    (610.0, "80"):   30.96,
    (610.0, "80S"):  12.70,
    (610.0, "100"):  38.89,
    (610.0, "120"):  46.02,
    (610.0, "140"):  52.37,
    (610.0, "160"):  59.54,

    # ---- NPS 26" (OD 660mm) ----
    (660.0, "10"):   7.92,
    (660.0, "STD"):  9.53,
    (660.0, "20"):   12.70,
    (660.0, "XS"):   12.70,

    # ---- NPS 28" (OD 711mm) ----
    (711.0, "10"):   7.92,
    (711.0, "STD"):  9.53,
    (711.0, "20"):   12.70,
    (711.0, "XS"):   12.70,
    (711.0, "30"):   15.88,

    # ---- NPS 30" (OD 762mm) ----
    (762.0, "5S"):   6.35,
    (762.0, "10"):   7.92,
    (762.0, "10S"):  7.92,
    (762.0, "STD"):  9.53,
    (762.0, "20"):   12.70,
    (762.0, "30"):   15.88,
    (762.0, "XS"):   12.70,

    # ---- NPS 32" (OD 813mm) ----
    (813.0, "10"):   7.92,
    (813.0, "STD"):  9.53,
    (813.0, "20"):   12.70,
    (813.0, "XS"):   12.70,
    (813.0, "30"):   15.88,
    (813.0, "40"):   17.48,

    # ---- NPS 36" (OD 914.4mm) ----
    (914.4, "10"):   7.92,
    (914.4, "STD"):  9.53,
    (914.4, "20"):   12.70,
    (914.4, "XS"):   12.70,
    (914.4, "30"):   15.88,
    (914.4, "40"):   19.05,
}


# === CuNi (EEMUA 234) OD and WT tables ===
# CuNi uses non-standard ODs for some sizes
CUNI_OD: dict[str, float] = {
    "0.5": 16.0, "0.75": 19.0, "1": 25.0, "1.5": 38.0,
    "2": 57.0, "3": 76.0, "4": 108.0, "6": 159.0,
    "8": 219.0, "10": 267.0, "12": 324.0, "14": 356.0,
    "16": 406.0, "18": 457.0, "20": 508.0, "24": 610.0,
    "28": 711.0, "32": 813.0, "36": 914.0,
}

CUNI_WT: dict[str, float] = {
    "0.5": 2.0, "0.75": 2.0, "1": 2.0, "1.5": 2.0,
    "2": 2.5, "3": 3.0, "4": 3.0, "6": 3.5,
    "8": 4.0, "10": 4.5, "12": 5.0, "14": 5.0,
    "16": 6.0, "18": 6.0, "20": 6.5, "24": 8.0,
    "28": 8.0, "32": 10.0, "36": 12.0,
}


# === Tubing WT ===
TUBING_WT: dict[str, float] = {
    "T80A": 1.245, "T90A": 1.245,  # 125 Barg
    "T80B": 1.651, "T90B": 1.651,  # 200 Barg
    "T80C": 2.108, "T90C": 2.108,  # 325 Barg
}


def _normalize_schedule(schedule: str) -> str:
    """Normalize schedule name for lookup.

    Handles variations like 'SCH 80', 'Sch80', 'SCH80S', 'SCH 160' etc.
    """
    s = schedule.strip().upper()
    # Remove "SCH" or "SCH " prefix
    for prefix in ("SCH ", "SCH"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    # Common aliases
    if s in ("STANDARD", "STD/40S"):
        return "STD"
    if s in ("EXTRA STRONG", "EXTRA-STRONG", "XS/80S"):
        return "XS"
    if s in ("DOUBLE EXTRA STRONG", "DOUBLE EXTRA-STRONG"):
        return "XXS"
    return s


def get_od_mm(nps: str) -> float | None:
    """Get OD in mm for a given NPS string (e.g. '2', '0.5', '1.5')."""
    nps = str(nps).strip().strip('"')
    return NPS_TO_OD.get(nps)


def get_wall_thickness(od_mm: float, schedule: str) -> float | None:
    """Look up wall thickness in mm for a given OD and schedule.

    Returns None if no match found (e.g. for '-' schedule or unknown combo).
    """
    if not schedule or schedule.strip() in ("-", "–", "—", ""):
        return None
    sch = _normalize_schedule(schedule)
    return _WT_TABLE.get((od_mm, sch))


def get_wall_thickness_by_nps(nps: str, schedule: str) -> float | None:
    """Look up wall thickness by NPS and schedule name."""
    od = get_od_mm(nps)
    if od is None:
        return None
    return get_wall_thickness(od, schedule)


def correct_pipe_data(pipe_data: list[dict], material: str | None = None) -> list[dict]:
    """Post-process AI-generated pipe_data to fix OD and wall thickness values.

    Material-aware behavior:
      - Steel (CS/LTCS/SS/DSS/SDSS/GALV): enforce ASME B36.10M/B36.19M ODs and WT
      - CuNi 90/10: enforce EEMUA 234 ODs; keep AI-generated WT (EEMUA has no schedule)
      - GRE / CPVC / plastic: keep AI-generated OD and WT (manufacturer-specific)
      - Tubing: enforce OD = nominal NPS × 25.4; keep AI-generated WT
      - '-' schedule: keep AI-generated WT as-is

    Returns the corrected pipe_data list (mutated in place).
    """
    mat = (material or "").upper()
    # Steel pipe materials follow ASME B36.10M/B36.19M strictly
    is_steel = any(k in mat for k in ("CS", "LTCS", "SS", "DSS", "SDSS", "GALV", "STEEL", "A106", "A333", "A312", "A790", "A671"))
    is_non_asme = any(k in mat for k in ("CUNI", "CU-NI", "CU NI", "C70600", "EEMUA", "GRE", "CPVC", "PVC", "PLASTIC", "FRP", "EPOXY", "TUBING"))

    for p in pipe_data:
        nps = str(p.get("size_inch", "")).strip()
        schedule = str(p.get("schedule", "")).strip()

        # Correct OD using material-aware lookup
        od_to_use = get_od_for_material(nps, material) if mat else get_od_mm(nps)
        if od_to_use is not None:
            p["od_mm"] = od_to_use
        # else: preserve AI-generated OD (e.g., GRE manufacturer-specific)

        # Correct WT ONLY for steel pipe with standard ASME schedule
        # For CuNi, GRE, CPVC, Tubing — preserve AI-generated WT
        if is_non_asme:
            continue
        od = p.get("od_mm", 0)
        if od and schedule and schedule not in ("-", "–", "—", ""):
            wt = get_wall_thickness(od, schedule)
            if wt is not None:
                p["wall_thickness_mm"] = wt

    return pipe_data

"""
Claude AI service for generating PMS data.
Generates all PMS fields EXCEPT pressure-temperature data (which comes from JSON).

The AI is trained via comprehensive prompt rules that encode the engineering patterns
from the master spec sheets. No data is hardcoded — the AI uses its knowledge of
ASME/ASTM standards combined with project-specific conventions taught in the prompt.
"""
import json
import logging

import anthropic

from app.config import settings
from app.utils.engineering_constants import AI_MAX_TOKENS, MILL_TOLERANCE_PERCENT

logger = logging.getLogger(__name__)


class AIGenerationError(RuntimeError):
    """Raised when the Anthropic call fails. The message is user-safe and
    describes the actual failure mode (credits, rate limit, auth, etc.) so
    the frontend can show something useful instead of a generic 'check your
    API key'."""

SYSTEM_PROMPT = """You are a senior piping materials engineer with deep expertise in:
- ASME B31.3 (Process Piping), B36.10M (Welded/Seamless Wrought Steel Pipe), B36.19M (Stainless Steel Pipe)
- ASME B16.5 (Flanges), B16.9 (BW Fittings), B16.11 (Forged Fittings), B16.20 (Gaskets), B16.47 (Large Flanges), B16.48 (Line Blanks)
- ASTM material standards for CS, LTCS, SS316L, Duplex, Super Duplex, CuNi, Titanium, GRE, CPVC, Copper
- EEMUA 234 (CuNi piping systems)
- NACE MR-01-75 / ISO 15156 sour service requirements
- Industrial valve specifications and coding conventions

You generate PMS (Piping Material Specification) data with 100% accuracy to ASME standards.
Return ONLY valid JSON. No markdown, no explanation, no extra text."""


def _build_generation_prompt(
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    rating: str,
    reference_entries: list[dict],
) -> str:
    """Build the prompt that teaches the AI the project rules and patterns."""

    return f"""Generate a complete PMS JSON for:
- Piping Class: {piping_class}
- Rating: {rating}
- Material: {material}
- Corrosion Allowance: {corrosion_allowance}
- Service: {service}

Do NOT generate P-T data or hydrotest_pressure (handled separately). Set hydrotest_pressure to "".

=== CLASS NAMING CONVENTION (3-Part System per PMS Doc) ===
Format: [PART1][PART2][PART3]

PART 1 — RATING (letter):
  A=150# | B=300# | D=600# | E=900# | F=1500# | G=2500# | J=5000# | K=10000# | T=Tubing

PART 2 — MATERIAL (number):
  1  = CS, 3mm Corrosion Allowance
  2  = CS, 6mm Corrosion Allowance (heavy wall)
  3  = CS Galvanized, 3mm CA (screwed fittings)
  4  = CS Galvanized, 1.5mm CA (screwed fittings)
  5  = CS Galvanized, 6mm CA
  6  = CS Internally Epoxy Coated
  9  = SS316 (not used in this project)
  10 = SS316L
  20 = Duplex SS (DSS) UNS S31803
  25 = Super Duplex SS (SDSS) UNS S32750
  30 = 90/10 CuNi (Copper-Nickel)
  31 = Copper
  40 = GRE (Glass Reinforced Epoxy)
  41 = GRV — BONSTRAND Series 5000C
  42 = CPVC
  50 = SS316L/SS316 Tubing
  60 = 6Mo Tubing
  70 = Titanium

PART 3 — IDENTIFIER (optional suffix):
  N = NACE (sour service, adds NACE-MR-01-75/ISO-15156 to design code)
  L = Low Temperature variant
  LN = Low Temp + NACE combined
  A = 125 Barg Pressure (tubing)
  B = 200 Barg Pressure (tubing)
  C = 325 Barg Pressure (tubing)

Examples: A1 = 150# CS 3mm CA | B1N = 300# CS 3mm CA NACE | A2LN = 150# CS 6mm CA LTCS+NACE | T80A = SS316L Tubing 125 Barg

=== PIPE SIZES — STANDARD NPS RANGES ===
Generate ALL standard NPS sizes for the class. Typical ranges:
  A-series 150# CS (1/1N): 0.5" to 36" (22 sizes: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36)
  A-series 150# LTCS (1L/1LN): 0.5" to 30" (20 sizes)
  A-series 150# SS/DSS/SDSS (10/20/25): 0.5" to 24-32" (17-21 sizes)
  A-series 150# 2-series (A2/A2N): 0.5" to 30" (20 sizes)
  B-series 300#: 0.5" to 24" (17 sizes) — for DSS/SDSS up to 32" (21 sizes)
  D-series 600#: 0.5" to 24" (17 sizes)
  E-series 900#: 0.5" to 24" (17 sizes) — 2N/2LN start at 1" (15 sizes)
  F-series 1500#: 0.5" to 24" (17 sizes) — 2N/2LN start at 1" (15 sizes)
  G-series 2500#: 0.5" to 24" (17 sizes) — G10: to 12" (11), G20: to 18" (14)
  GALV / Epoxy (A3/A4/B4/D4/A5/A6): 0.5" to 24" (17 sizes)
  CuNi (A30): 0.5" to 28" (17 sizes: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28 — per EEMUA 234. No 2.5", no 22", no 30" — do NOT emit those sizes)
  Copper (A40): 0.5" to 4" ONLY (7 sizes: 0.5, 0.75, 1, 1.5, 2, 3, 4) — do NOT emit 6"+
  Titanium (A70): 0.5" to 6" ONLY (8 sizes: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6) — do NOT emit 8"+
  GRE (A50/A51/A52):
    A50/A52: 20 sizes 1"-40" (1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28, 30, 32, 34, 36, 40)
    A51: 6 sizes 1"-6" only (1, 1.5, 2, 3, 4, 6) — BONSTRAND Series 50000C range
  CPVC (A60): 0.5" to 8" (10 sizes: 0.5, 0.75, 1, 1.5, 2, 2.5, 3, 4, 6, 8)
  Tubing (T80/T90): Short size range per rating — T*A = 0.5"-1.5", T*B/C similar

Standard NPS sequence: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36

=== PIPE SCHEDULES — RULES BY MATERIAL FAMILY AND RATING ===
Use ASME B36.10M for CS/LTCS/GALV, ASME B36.19M for SS/DSS/SDSS.
Wall thicknesses must be EXACT standard values from the appropriate ASME table.

All per-class schedule rules below are verbatim from the project Pipe Wall
Thickness Calculation workbook (20171-SPOG-80000-PP-CL-0001 Rev 03) —
specifically the "Selected Thickness" lookup table (cols N/O/P) on each
class sheet. Follow them EXACTLY — do not substitute "standard" ASME
schedules by interpolation, and do not change a schedule because it looks
unusual. These values reflect specific project engineering choices
incorporating corrosion allowance, material limits, and fabrication
standards.

CS 1-series (A1/B1/D1/E1/F1/G1 and NACE variants — N/non-N identical schedule per Excel):
  A1  / A1N  (150#):  0.5-1.5"→160 | 2-6"→80 | 8-28"→STD | 30-36"→XS
  B1  / B1N  (300#):  0.5-1.5"→160 | 2-6"→80 | 8-20"→40 | 22"→"-" | 24"→40
  D1  / D1N  (600#):  0.5-2"→160 | 3-24"→80
  E1  / E1N  (900#):  0.5-3"→160 | 4-24"→120
  F1  / F1N  (1500#): 0.5-1.5"→XXS | 2-6"→160 | 8"→XXS | 10"→140 | 12-14"→160 | 16"→140 | 18"→160 | 20"→140 | 22"→160 | 24"→140
  G1  / G1N  (2500#): 0.5-1.5"→XXS | 2"→"-" | 3"→XXS | 4-24"→"-" (calc WT)

LTCS 1L-series and 1LN-series (A1L–G1L, A1LN–G1LN — L/LN identical schedule per Excel):
  A1L  / A1LN  (150#):  0.5-1.5"→160 | 2-28"→XS | 30"→30
  B1L  / B1LN  (300#):  0.5-1.5"→160 | 2-6"→XS | 8-20"→40 | 22"→"-" | 24"→40
  D1L  / D1LN  (600#):  0.5-2"→160 | 3-8"→XS | 10-24"→80
  E1L  / E1LN  (900#):  0.5-1.5"→XXS | 2-3"→160 | 4-24"→120
  F1L  / F1LN  (1500#): 0.5-8"→XXS | 10-24"→"-" (calc WT)
  G1L  / G1LN  (2500#): 0.5-1"→XXS | 1.5-24"→"-" (calc WT)

CS 2-series NACE (heavy-wall, 6mm CA — A2N–G2N) and LTCS 2LN-series (A2LN–G2LN):
  A2N  (150#):  0.5-1.5"→XXS | 2"→160 | 3-6"→80 | 8"→60 | 10-16"→40 | 18"→30 | 20-24"→XS
  A2LN (150#):  0.5-1.5"→XXS | 2"→160 | 3-28"→XS | 30"→30
  B2N  / B2LN  (300#):  0.5-1.5"→XXS | 2-3"→160 | 4"→120 | 6-10"→XS | 12-24"→60
  D2N  / D2LN  (600#):  0.5-0.75"→"-" | 1-2"→XXS | 3-4"→160 | 6-8"→120 | 10-24"→100
  E2N  / E2LN  (900#):  starts at 1" | 1-4"→XXS | 6"→160 | 8-16"→140 | 18-24"→120
  F2N  / F2LN  (1500#): starts at 3" | 3-6"→XXS | 8-10"→"-" | 12-24"→160
  G2N  / G2LN  (2500#): starts at 1" | 1-24"→"-" (all calc WT)

SS 316L 10-series — A10/A10N are pure B36.19M; B10/D10/E10/F10/G10 are
MIXED ("ASME B 36.19M / B 36.10M" pipe_code — seamless small-bore uses
B36.19M S-schedules, welded large-bore uses B36.10M non-S schedules):
  A10  (150#, 17 sizes 0.5"-24"): 0.5-0.75"→160 | 1-2"→80S | 3-20"→40S | 22"→STD | 24"→40S
  A10N (150#, 17 sizes 0.5"-24"): same as A10
  B10  (300#, 17 sizes 0.5"-24"): 0.5-1.5"→160 | 2"→80S | 3-18"→40S | 20"→80S | 22"→XS | 24"→80S
  B10N (300#, 17 sizes 0.5"-24"): same as B10
  D10  (600#, 17 sizes 0.5"-24"): 0.5-1.5"→160 | 2-10"→80S | 12-20"→60 | 22"→"-" (calc WT) | 24"→60
  D10N (600#, 17 sizes 0.5"-24"): same as D10
  E10  (900#, 17 sizes 0.5"-24"): 0.5-1.5"→160 | 2-6"→80S | 8-24"→100
  E10N (900#, 17 sizes 0.5"-24"): same as E10
  F10  (1500#, 17 sizes 0.5"-24"): 0.5-24"→160 (uniform SCH 160 across ALL sizes)
  F10N (1500#, 17 sizes 0.5"-24"): same as F10
  G10  (2500#, 11 sizes 0.5"-12" ONLY — do NOT emit 14"+): 0.5-3"→XXS | 4-12"→"-" (calc WT)
  G10N (2500#, 11 sizes 0.5"-12" ONLY): same as G10

DSS 20-series (UNS S31803, use "S" suffix):
  A20  (150#): 0.5-2"→80S | 3-24"→10S | 26-28"→10 | 30"→10S | 32"→10
  A20N (150#): same as A20
  B20  (300#): 0.5-2"→80S | 3-10"→10S | 12"→20 | 14-16"→10 | 18"→20 | 20"→40S | 22"→STD | 24"→40S | 26"→STD | 28-32"→XS
  B20N (300#): same as B20
  D20  (600#): 0.5-2"→80S | 3-12"→40S | 14"→40 | 16"→80S | 18-20"→40 | 22"→"-" | 24"→40
  D20N (600#): same as D20
  E20  (900#): 0.5-2"→80S | 3-6"→40S | 8"→60 | 10"→80S | 12-24"→60
  E20N (900#): same as E20
  F20  (1500#): 0.5-4"→80S | 6"→120 | 8"→100 | 10-24"→120
  F20N (1500#): same as F20
  G20  (2500#): 0.5-1"→80S | 1.5-10"→160 | 12"→"-" (calc WT) | 14"→120
  G20N (2500#): same as G20

SDSS 25-series (UNS S32750) — A25/A25N pure B36.19M; B25/D25/E25/F25 +
their N variants are MIXED ("ASME B 36.19M / B 36.10M"); G25/G25N pure B36.10M:
  A25  (150#, 21 sizes 0.5"-32"): 0.5-2"→80S | 3-24"→10S | 26-28"→10 | 30"→10S | 32"→10
  A25N (150#, 21 sizes 0.5"-32"): same as A25
  B25  (300#, 21 sizes 0.5"-32"): 0.5-2"→80S | 3-16"→10S | 18-22"→10 | 24"→40S | 26-32"→STD
  B25N (300#, 21 sizes 0.5"-32"): same as B25
  D25  (600#, 17 sizes 0.5"-24"): 0.5-2"→80S | 3-6"→40S | 8-10"→20 | 12-16"→40S | 18-20"→80S | 22"→XS | 24"→30
  D25N (600#, 17 sizes 0.5"-24"): same as D25
  E25  (900#, 17 sizes 0.5"-24"): 0.5-2"→80S | 3-10"→40S | 12-14"→80S | 16-24"→60
  E25N (900#, 17 sizes 0.5"-24"): same as E25
  F25  (1500#, 17 sizes 0.5"-24"): 0.5-2"→80S | 3"→40S | 4-8"→80S | 10-14"→80 | 16-24"→100
  F25N (1500#, 17 sizes 0.5"-24"): same as F25
  G25  (2500#, 17 sizes 0.5"-24"): 0.5-2"→80S | 3"→160 | 4"→120 | 6"→160 | 8-20"→140 | 22-24"→160
  G25N (2500#, 17 sizes 0.5"-24"): same as G25

GALV / Epoxy-coated classes:
  A3 (150# GALV screwed):           0.5"→XXS | 0.75-1.5"→160 | 2-6"→80 | 8-24"→STD
  A4 (150# GALV 1.5mm CA screwed):  0.5"→XXS | 0.75-1.5"→160 | 2-6"→80 | 8-24"→STD
  A5 (150# GALV 6mm CA):            0.5-1.5"→XXS | 2"→160 | 3-6"→80 | 8"→60 | 10-16"→40 | 18"→30 | 20-24"→XS
  A6 (150# CS Internally Epoxy Coated, 6mm CA):
                                    0.5-1.5"→XXS | 2"→160 | 3-6"→80 | 8"→60 | 10-16"→40 | 18"→30 | 20-24"→XS
  B4 (300# GALV):                   0.5-1.5"→160 | 2-6"→80 | 8-20"→40 | 22"→"-" | 24"→40
  D4 (600# GALV):                   0.5-2"→160 | 3-24"→80

Titanium (A70, 150# — pipe_code = "ASME B 36.10M" per spec):
  A70 (150#): 0.5-3"→40 | 4-6"→10   (size range 0.5"-6" ONLY — do NOT emit 8"+)

CuNi 30-series (EEMUA 234) — USE THESE EXACT ODs AND WTs (Pipe Class Sheet A30):
  A30: No ASME schedule — uses EEMUA 234 wall thickness tables. 17 sizes total.
  NPS → OD (mm):
    0.5"=16, 0.75"=25, 1"=30, 1.5"=44.5, 2"=57, 3"=88.9, 4"=108, 6"=159,
    8"=219.1, 10"=267, 12"=323.9, 14"=368, 16"=419, 18"=457.2, 20"=508,
    24"=610, 28"=711
  NPS → WT (mm):
    0.5"=2.0, 0.75"=2.0, 1"=2.5, 1.5"=2.5, 2"=2.5, 3"=2.5, 4"=3.0, 6"=3.5,
    8"=4.5, 10"=5.5, 12"=7.0, 14"=8.0, 16"=9.0, 18"=9.5, 20"=11.0,
    24"=13.0, 28"=15.0
  Schedule: "-" for all (EEMUA uses its own thickness system; no ASME schedule applies).
  IMPORTANT 1: Use the exact OD and WT values above. The post-processor does NOT
    correct EEMUA classes (pipe_code = "EEMUA 234 20 BAR" is non-ASME), so the
    values the AI emits are what the user sees. Get them right the first time.
  IMPORTANT 2: Do NOT emit NPS 2.5", 22", or 30" for A30 — those sizes are not
    in the EEMUA 234 range used by this project. The 17 sizes above are the
    complete list.

GRE (A50/A51/A52) — Manufacturer's Standard (NOT ASME). NON-ASME pipe_code,
so post-processor does NOT correct OD or WT — the AI's values are final.
Use these exact values:

  A50 / A52 (20 sizes 1"-40", pipe_code = "Manufacturer's Std."):
    NPS → OD (mm): 1"=34.1, 1.5"=49.1, 2"=57.8, 3"=86.4, 4"=110.6, 6"=166.6,
                   8"=218.4, 10"=274.5, 12"=327.3, 14"=359.2, 16"=410.5,
                   18"=452.2, 20"=502.3, 24"=602.8, 28"=728.6, 30"=780.6,
                   32"=832.6, 34"=884.6, 36"=936.4, 40"=1040.6
    NPS → ID (mm) — emit in pipe_data[i].id_mm:
                   1"=27.1, 1.5"=42.1, 2"=53.2, 3"=81.8, 4"=105.2, 6"=159.0,
                   8"=208.8, 10"=262.9, 12"=313.7, 14"=344.4, 16"=393.7,
                   18"=433.8, 20"=482.1, 24"=578.6, 28"=700.0, 30"=750.0,
                   32"=800.0, 34"=850.0, 36"=900.0, 40"=1000.0
    NPS → WT (mm): 1"=3.5, 1.5"=3.5, 2"=2.3, 3"=2.3, 4"=2.7, 6"=3.8, 8"=4.8,
                   10"=5.8, 12"=6.8, 14"=7.4, 16"=8.4, 18"=9.2, 20"=10.1,
                   24"=12.1, 28"=14.3, 30"=15.3, 32"=16.3, 34"=17.3,
                   36"=18.2, 40"=20.3
    Schedule = "-" for all sizes.

    Pipe-data fields:
      pipe_type     = "Manufacturer standard (TBA)"
      material_spec = "Filament wound Glassfiber Reinforced Epoxy (GRE) pipe, Conductive, ASTM D2996: RTRP-11AW"
      ends          = "Taper / Taper Socket x Spigot, Adhesive bonded"

    Fittings-data fields (emit on pms.fittings AND the same values on EVERY
    fittings_by_size entry — merged rows will collapse identical values):
      fitting_type      = "Taper / Taper Socket x Spigot, Adhesive bonded"
      rating            = "20 bar, 93degC"    (fittings-section Rating row — GRE-specific)
      material_spec     = "Filament wound Glassfiber Reinforced Epoxy (GRE) fitting, Conductive, ASTM D5685: RTRF, 11F1, or equivalent"
      elbow_standard    = "22.5°, 45°, 90° elbow"
      tee_standard      = "Tee or Reducing Tee"
      mold_tee_standard = "Molded Tee"
      reducer_standard  = "Conc and Ecc Reducer"
      red_saddle_standard = "Reducing Saddle - Flat Face (FF)"
      cap_standard      = ""       (A50/A52 Excel has no Cap row — leave empty)
      plug_standard     = ""       (not applicable — leave empty)
      weldolet_spec     = ""       (not applicable — leave empty)
      coupling_standard = "Coupler"
      adaptor_standard  = "Adapter"

    Bolts/Nuts/Gaskets fields:
      stud_bolts = "ASTM A 193 Gr. B7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm"
      hex_nuts   = "ASTM A 194 Gr. 2HM, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm"
      washers    = "ASTM A 307 Gr. B HDG"
      gasket     = "EPDM Rubber Full Face Gasket with SS insert Shore A Hardness 70 ± 5, #150 (e.g. Kroll & Ziller G-ST/PS)"
      gasket_2   = "EPDM Rubber Flat Ring Gasket with SS insert Shore A Hardness 70 ± 5, #150 (e.g. Kroll & Ziller G-ST/PS)"

  A51 (6 sizes 1"-6", pipe_code = "Manufacturer's Std (BONSTRAND Series 50000C)"):
    NPS → OD (mm): 1"=31.5, 1.5"=46.5, 2"=57.4, 3"=86.2, 4"=111.6, 6"=165.4
    NPS → ID (mm) — emit in pipe_data[i].id_mm:
                   1"=27.1, 1.5"=42.1, 2"=53.2, 3"=81.8, 4"=105.2, 6"=159.0
    NPS → WT (mm): 1"=2.2, 1.5"=2.2, 2"=2.1, 3"=2.2, 4"=3.2, 6"=3.2
    Schedule = "-" for all sizes.

    A51 is intentionally generic — BONSTRAND is a proprietary GRE system, so the
    spec defers nearly every field to the manufacturer. Use this literal string
    for most fields:
      _M = "Manufacturer standard (BONSTRAND Series 50000C)"

    Pipe-data fields:
      pipe_type = _M ; material_spec = _M ; ends = _M

    Fittings-data fields (same on pms.fittings and every fittings_by_size entry):
      fitting_type = _M ; rating = _M ; material_spec = _M
      elbow_standard = _M ; tee_standard = _M ; mold_tee_standard = _M
      reducer_standard = _M ; red_saddle_standard = _M
      cap_standard = "" ; plug_standard = "" ; weldolet_spec = ""
      coupling_standard = _M ; adaptor_standard = _M

    Bolts/Nuts/Gaskets fields:
      stud_bolts = "ASTM A 193 Gr. B7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm"
      hex_nuts   = "ASTM A 194 Gr. 2HM, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm"
      washers    = "ASTM A 307 Gr. B HDG"
      gasket     = "ASME B16.21, Flat Ring, 3mm, CNAF, Oil Resistant, Glass Fibre Composite with NBR Binder"
      gasket_2   = ""   (A51 has only a single gasket row — leave gasket_2 empty)

CPVC (A60) — ASTM F441 (uses Iron Pipe Size ODs — SAME as ASME B36.10M):
  Sizes: 0.5"-8" (typical)
  Schedule: SCH 80 for all sizes (per ASTM F441)
  OD per ASME B36.10M (IPS): 0.5"=21.3, 0.75"=26.7, 1"=33.4, 1.5"=48.3, 2"=60.3,
                              2.5"=73.0, 3"=88.9, 4"=114.3, 6"=168.3, 8"=219.1

Tubing (T80/T90) — OD EQUALS NOMINAL SIZE × 25.4:
  0.5" tube: OD = 12.7 mm
  0.75" tube: OD = 19.05 mm
  1" tube: OD = 25.4 mm
  1.5" tube: OD = 38.1 mm
  T80A/T90A: 0.5"-1.5" OD, wall thickness varies by size (125 Barg rating)
  T80B/T90B: 0.5"-1.5" OD, heavier WT (200 Barg rating)
  T80C/T90C: 0.5"-1.5" OD, heaviest WT (325 Barg rating)
  Schedule: "-" for all (tubing uses thickness classes, not schedules)

=== PIPE TYPE TRANSITION (Seamless → Welded) ===
All mainstream classes have TWO pipe types with a size-based transition:
  CS 1-series (A1/A1N): Seamless → LSAW, 100% RT (transition at ~20")
  CS 1-series (B1/D1/E1 and N): Seamless → EFW, 100% RT (transition at ~14-18")
  CS F1/G1 series (1500#/2500#): Seamless → LSAW, 100% RT (transition at 18"). MOC is API 5L Gr, X60 PSL-2 for ALL sizes (single unified MOC, but TWO pipe_types).
    - Sizes 0.5"-16": pipe_type = "Seamless"
    - Sizes 18" and larger: pipe_type = "LSAW, 100% RT"
    - material_spec (MOC) = "API 5L Gr, X60 PSL-2" for every size
  LTCS 1L-series: Seamless → EFW, 100% RT (transition at ~14")
  SS316L 10-series: Seamless → EFW, 100% RT (transition at ~10")
  DSS 20-series: Seamless → Welded (Longitudinally) with 100% RT (transition at ~10")
  SDSS 25-series: Seamless → Welded (Longitudinally) with 100% RT (transition at ~10")
  GALV 3/4/5/6/B4/D4: Seamless → LSAW, 100% RT (transition at ~14")
  CuNi (A30): Seamless (0.5"-16") → Seam Welded (18"-28"). Transition at 18".
    Pipe MOC is the same string for both types: "Annealed tube 90-10 CU-NI ALLOY
    UNS 7060X EEMUA 234 20 BAR / ASTM B 466 Copper Alloy UNS No. 70600 / BS 2871 CN 102".
  GRE (40/41/42): ALL manufacturer standard (single type)
  CPVC (60): ALL manufacturer standard
  Tubing (T80/T90): ALL Seamless

=== PIPE MOC RULES ===
CS (1-series, 2-series):
  Seamless: ASTM A 106 Gr. B
  Welded (A-series 150#, GALV): API 5L Gr. B
  Welded (B/D/E-series): ASTM A 671 - CC60 Class 22
  F1/G1 (1500#/2500#): API 5L Gr, X60 PSL-2 (ALL sizes, single MOC)

LTCS (1L-series, 2LN-series):
  Seamless: ASTM A 333 Gr.6
  Welded: ASTM A 671 - CC60 Class 22

SS316L (10-series):
  Seamless: ASTM A 312 TP 316L
  Welded: ASTM A 358 TP 316L

DSS (20-series):
  Seamless: ASTM A 790 Gr. S31803
  Welded: ASTM A 928 Class 1, Gr. S31803

SDSS (25-series):
  ALL sizes: ASTM A 790 Gr. S32750 (same for seamless and welded)

GALV (3/4/5/6,B4,D4):
  Seamless: ASTM A 106 Gr. B (Galvanized)
  Welded: API 5L Gr. B (Galvanized)

CuNi (30):
  Annealed tube 90-10 CU-NI ALLOY UNS 7060X EEMUA 234 20 BAR / ASTM B 466 Copper Alloy UNS No. 70600 / BS 2871 CN 102

Copper (A40) — ASTM B 42 "Regular" copper pipe, pipe_code = "ASTM B42 (Regular)":
  USE THESE EXACT ODs AND WTs (Pipe Class Sheet A40 — NON-ASME, post-processor
  does NOT correct these values, so AI's emission is what the user sees):
    NPS → OD (mm):
      0.5"=21.3, 0.75"=26.7, 1"=33.4, 1.5"=48.3, 2"=60.3, 3"=88.9, 4"=114.0
      (Note: 4" OD is 114.0 per ASTM B42, NOT 114.3 like ASME B36.10M.)
    NPS → WT (mm):
      0.5"=2.72, 0.75"=2.9, 1"=3.2, 1.5"=3.81, 2"=3.96, 3"=5.56, 4"=6.35
    Schedule: "-" for all sizes (no ASME schedule applies to ASTM B 42).
  Pipe MOC: "ASTM B 42 UNS C12200"
  Pipe type split by size (boundary at 2"):
    0.5"-1.5" → "Seamless Hard Drawn H80 (Regular)"
    2"-4"     → "Seamless light Drawn H55 (Regular)"
  Ends: "BE" (butt end — the spec says BE even though soldered ends are common)
  Size range is LIMITED: 0.5" through 4" ONLY (7 sizes total). No 6"+ sizes.

GRE (50/51/52):
  Manufacturer standard per GRE system rating

CPVC (42/60):
  ASTM F441/F442

Titanium (70):
  ASTM B 861 Gr. 2

Tubing:
  T80 (SS316L Tubing): ASTM A269 Type 316/316L SML, Annealed, Hardness <= 90 HRB
  T90 (6Mo Tubing): ASTM B 677 UNS N08926

=== FITTINGS RULES ===
TYPE split mirrors pipe type: "Butt Weld (SCH to match pipe), Seamless" for small sizes, "Butt Weld (SCH to match pipe), Welded" for large sizes.
GALV screwed classes (3/4): Small sizes = "Screwed (SCRD), #3000", larger = "Butt Weld (SCH to match pipe), Seamless/Welded"
CuNi (A30): THREE-way fittings TYPE split per Excel:
    0.5"-1.5" → "SW"                                     (Socket Weld)
    2"-16"    → "Butt Weld (SCH to match pipe), Seamless"
    18"-28"   → "BW, Welded"                             (Butt Weld, Welded pipe — matches the 18" seamless→welded pipe transition)
Copper (A40): Small sizes (0.5-1.5) = "Brazed Fittings (SCH to match pipe), Seamless", larger (2-4) = "Butt Weld (SCH to match pipe), Seamless"
GRE/CPVC: Manufacturer standard (adhesive bonded / laminated)
Tubing (T80/T90): "Compression Fitting" — body AISI 316, ferrules and nuts in AISI 316

FITTINGS MOC BY MATERIAL:
  CS (A1/A1N/A2/A2N): ASTM A 234 Gr. WPB (same for ALL sizes — seamless AND welded)
  CS (B1/D1/E1 + N/2N variants): Seamless=ASTM A 234 Gr. WPB, Welded=ASTM A 420 Gr. WPL6
  CS (F1/G1 + N/2N): ASTM A 860 WPHY 60 (all sizes)
  LTCS (all 1L/2LN): ASTM A 420 Gr. WPL6 (all sizes)
  SS316L (10-series): ASTM A 403 Gr. WP 316L (all sizes)
  DSS (20-series): Seamless=ASTM A 815 Gr.WP-S UNS S31803, Welded=ASTM A 815 Gr.WP-WX UNS S31803
  SDSS (25-series): Seamless=ASTM A 815 Gr.WP-S UNS S32750, Welded=ASTM A 815 Gr.WP-WX UNS S32750
  GALV (3/4/5/6,B4,D4): Screwed=ASTM A 105N-Galvanized, BW=ASTM A 234 Gr. WPB, Seamless Galvanized
  CuNi (A30): 90-10 Cu-Ni per EEMUA 234
  Copper (A40): Small sizes (0.5-1.5) = "ASTM B 124 UNS C11000",
                Large sizes (2-4)     = "ASTM B 42 UNS C12200"
  Titanium (70): ASTM B 363 Gr. 2

STANDARDS (apply to ALL material families unless noted):
  Elbow: ASME B 16.9 | Tee: ASME B 16.9 | Reducer: ASME B 16.9 | Cap: ASME B 16.9
  Plug: Hex Head Plug, ASME B 16.11 (or "Hex Head, ASME B 16.11")
  Weldolet: MSS SP 97, [flange MOC] (e.g., "MSS SP 97, ASTM A 105N" for CS)
  GALV screwed classes: Elbow/Tee/Red/Cap = ASME B 16.11
  CuNi classes: All fittings per EEMUA 234; additional: Coupling, Union, Sockolet, Nipple, Swage per EEMUA 234
  Copper (A40) — fitting standards carry the MOC split values per Excel.
    For EVERY fittings_by_size entry, populate these fields with the MATERIAL
    MOC for that size (not an engineering standard like B 16.22):

    Sizes 0.5"-1.5" → every field below = "ASTM B 124 UNS C11000"
    Sizes 2"-4"     → every field below = "ASTM B 42 UNS C12200"

    Fields to populate on each fittings_by_size entry:
      material_spec, elbow_standard, tee_standard, reducer_standard, cap_standard,
      coupling_standard, union_standard, sockolet_standard, weldolet_spec,
      nipple_standard, swage_standard

    EXCEPTION for plug_standard (threaded plugs are small-bore only):
      Sizes 0.5"-1.5" → plug_standard = "ASTM B 124 UNS C11000"
      Sizes 2"-4"     → plug_standard = ""   (leave blank — plug row stops at 1.5")
  GRE classes: All per manufacturer/GRE system standard

fittings_by_size: One entry per pipe size. Each entry includes size_inch, type (Seamless/Welded), fitting_type, material_spec, and all standards. material_spec may differ between seamless and welded sizes.

=== FLANGE RULES ===
MOC by material family:
  CS (A1-E1, N variants): ASTM A 105N
  CS (F1/G1, F2N/G2N): ASTM A 694 F60
  LTCS (all 1L/2LN): ASTM A 350 Gr. LF2
  SS316L (10-series): ASTM A 182 F 316L
  DSS (20-series): ASTM A 182 Gr. F51
  SDSS (25-series): ASTM A 182 Gr. F53 (or Gr. F55 in some variants)
  GALV (3/4/5/6,B4,D4): ASTM A 105N Galvanized (screwed flanges for small, WN for large)
  CuNi (A30): 90-10Cu-Ni per EEMUA 234 20 BAR; Blind Flange = ASTM A 105N FF with 3mm 90-10 CuNi weld deposit
  Copper (A40): "ASTM B61 UNS C92200" (bronze cast flange per ASME B 16.24);
                Blind Flange MOC = "ASTM A 105N RF With 3mm Copper over lay"
  CPVC  (A60): manufacturer CPVC flange; face FF
  GRE (A50/A52) — face FF (Flat Face):
      material_spec = "Filament Wound Fibre reinforced epoxy flange, conductive, Heavy duty, ASTM D4024"
      flange_type   = "Taper / Taper Socket x Spigot, Adhesive bonded"
      standard      = "Drilled to ASME B 16.5/ 16.47A, 150#"
  GRE (A51) — face FF, BONSTRAND system:
      material_spec = "Manufacturer standard (BONSTRAND Series 50000C)"
      flange_type   = "Manufacturer standard (BONSTRAND Series 50000C)"
      standard      = "Drilled to ASME B 16.5, 150#"   (A51 is 1"-6" only, so B 16.47A does NOT apply)

FACE by rating / material:
  150#: "150# RF, Serrated Finish"
  300#: "300# RF, Serrated Finish"
  600#: "600# RF, Serrated Finish"
  900# (E-series): Small bore (0.5-1.5") = "1500#, RTJ", Larger sizes (2"+) = "900#, RTJ"
  1500# (F-series): "1500#, RTJ"
  2500# (G-series): "2500#, RTJ"
  CuNi (A30) EEMUA: "EEMUA 20 bar, FF" (Flat Face)
  Copper (A40): "FF" — Flat Face, per ASME B 16.24 bronze flanges
  GRE (A50/A51/A52): "FF" — Flat Face, manufacturer std
  CPVC (A60): "FF" — Flat Face, per ASTM F 441 socket-flange
  GALV: "150# RF, Serrated Finish" (same as 150#)

TYPE — compose the flange_type string from these components; do not copy a fixed template:
  Connection: Weld Neck flange type per ASME B16.5
  End prep: butt-welding ends per ASME B16.25
  Size-dependent: for sizes >24", also cite ASME B16.47A (Series A large-diameter flanges)
  Face suffix: include "RTJ" for 900#/1500#/2500# ratings (E/F/G-series); omit for RF/FF
  Note references: if the class has a numbered notes list that describes flange-specific requirements, cite those note positions at the end (e.g. ", Note 8,9").
    — 900#/1500#/2500# (RTJ) classes: cite the jackscrew/WNRTJ note and the gasket roughness note.
    — 150#/300#/600# classes: do not cite flange notes unless class-specific.

  GALV screwed (A3/A4): small sizes use screwed-end flanges (SCRD), larger sizes use WN butt-welded
  CuNi (A30): SW Flange for 0.5"-1.5"; WN Flange for 2"-28" (boundary at 2", per EEMUA 234 / Excel spec)
  Copper (A40): TYPE = "Solid slip on flange" (all sizes) per ASME B 16.24; STD = "ASME B 16.24"

F/G-series (1500#/2500#) additional flange rows (populate compact_flange and hub_connector):
  compact_flange — describe the Norsok L-005 WN Compact Flange used for layout-constrained installations. Include the Norsok L-005 reference and a short note that it is for layout constraint.
  hub_connector — describe the hub-connector assembly: seal ring material (ASTM A 182 F 316L), hub and blind-hub material (ASTM A 694 F60), clamp material (AISI 4140), and indicate bolt material per the bolts/nuts section. Add a note that it is used where ANSI or Compact Flange are unsuitable.

=== SPECTACLE BLIND ===
MOC: Same as flange MOC
Standard: "ASME B 16.48" (standard sizes)
Standard (large): "Spacer and blind as per ASME B 16.48 (Note 5)" (sizes not covered by B16.48)

PROJECT SIZE BOUNDARY (class-family specific — do NOT guess, use these rules):
  SS 316L 10-series (A10, A10N, B10, B10N, D10, D10N, E10, E10N, F10, F10N, G10, G10N):
    Sizes 0.5"-12" → "ASME B 16.48"
    Sizes 14"+     → "Spacer and blind as per ASME B 16.48 (Note 5)"
  ALL OTHER CLASSES (CS/LTCS/GALV/Epoxy/DSS/SDSS — A1/A1N/A1L/A1LN, A20/A20N, A25/A25N, etc.):
    Sizes 0.5"-14" → "ASME B 16.48"
    Sizes 16"+     → "Spacer and blind as per ASME B 16.48 (Note 5)"
  The Excel renderer enforces this boundary by size, so the strings you
  emit in spectacle_blind.standard and spectacle_blind.standard_large are
  positioned on the correct side of the cutoff automatically.

For 900#/1500#/2500# (E/F/G RTJ classes), drop the "(Note 5)" suffix since
the RTJ note-list does not include a note 5 for spectacle blinds; use
"Spacer and blind as per ASME B 16.48" without parenthetical.

F/G series (1500#/2500#): MOC = ASTM A 694 F60, Standard = "ASME B 16.48",
  Standard_large = "Spacer and blind as per ASME B 16.48" (ALWAYS populate this for F/G classes — the reference splits the row with B16.48 on the small-size side (≤14") and "Spacer and blind as per ASME B 16.48" on the large-size side (≥16")).
GALV classes: MOC = "ASTM A 105N Galvanized"

=== BOLTS / NUTS / GASKETS ===
STUD BOLTS by material family:
  CS (1/2-series, 150#-2500#): ASTM A 193 Gr. B7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  LTCS (1L-series, 150#-2500#): ASTM A 320 Gr. L7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  SS316L (10-series): ASTM A 320 Gr. L7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  DSS + SDSS (20/25-series): ASTM A 453 Gr. 660
  CuNi (30): ASTM A 193 Gr. B7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  GALV classes: ASTM A 193 Gr. B7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm

HEX NUTS:
  CS: ASTM A 194 Gr. 2HM, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  LTCS + SS316L: ASTM A 194 Gr. 7ML, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  DSS + SDSS: ASTM A 453 Gr. 660
  CuNi + GALV: ASTM A 194 Gr. 2HM, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm

GASKETS:
  RF classes (150#-600#):
    CS/LTCS/SS: ASME B 16.20, 4.5mm, SS316/SS316L Spiral Wound with Flexible Graphite (F.G.) filler
    DSS: ASME B 16.20, 4.5mm, DSS UNS S31803 Spiral Wound with Flexible Graphite (F.G.) filler
    SDSS: ASME B 16.20, 4.5mm, DSS UNS S32750 Spiral Wound with Flexible Graphite (F.G.) filler
    GALV: 3mm thick flat ring of neoprene/ EPDM rubber as ASME B 16.21
    CuNi (A30): 3mm thick flat ring of neoprene/ EPDM rubber as ASME B 16.21
    Copper (A40): ASME B 16.21, Full face gasket, 2mm, CNAF
      (CNAF = Compressed Non-Asbestos Fiber. Use this exact gasket string for A40;
       do NOT use the CuNi neoprene rule — A40 has its own spec per Excel.)
    GRE (A50/A52): "EPDM Rubber Full Face Gasket with SS insert Shore A Hardness 70 ± 5, #150 (e.g. Kroll & Ziller G-ST/PS)"
      (The A50/A52 Excel sheet lists TWO gaskets — the second is a "Flat Ring" variant of the same spec.
       The current model carries a single gasket field, so emit the Full Face one. A follow-up model
       extension is needed to render both rows.)
    GRE (A51): "ASME B16.21, Flat Ring, 3mm, CNAF, Oil Resistant, Glass Fibre Composite with NBR Binder"
  RTJ classes (900#+):
    CS/LTCS: ASME B 16.20, OCT ring of Soft Iron with Max. Hardness of 90 BHN, HDG
    SS: OCT Ring, SS316L, Max 160 BHN Hardness, ASME B16.20
    DSS: OCT Ring, DSS UNS S31803, Max 22 HRC Hardness, ASME B16.20
    SDSS: OCT Ring, SDSS UNS S32750, Max 22 HRC Hardness, ASME B16.20

=== VALVE CODES (VDS) — per 40801-SPE-80000-PP-SP-0002 ===
OFFICIAL VDS format:    [Type] + [Bore/Design] + [Seat] + [SPEC] + [EndConn]
  Type     (2 chars):  BL=Ball | BF=Butterfly | GA=Gate | GL=Globe |
                       CH=Check | DB=DBB | NE=Needle
  Bore     (Ball ONLY, 1 char): R=Reduced Bore | F=Full Bore
  Design   (non-Ball, 1 char):  P=Piston (Check) | S=Swing (Check) |
                                D=Dual-Plate (Check) | W=Wafer (BF) |
                                T=Triple-Offset (BF) | Y=Screw-and-Yoke (Gate/Globe) |
                                I=Straight-Inline (Needle) | A=Angle (Needle)
  Seat     (1 char):   M=Metal | P=PEEK | T=PTFE
  SPEC:                exact piping class code (A1, A1LN, F20N, G25N, T90C, etc.)
  EndConn:             R=RF | J=RTJ | F=FF | H=Hub | JT=RTJ with NPT female (inst.)

GOTCHA — letter T has two meanings:
  • in Seat  position → T = PTFE
  • in Design position (Butterfly only) → T = Triple-Offset
  Example: BLRTA1R = BL · R · T · A1 · R = Ball / Reduced bore / PTFE seat / A1 / RF
           BFTPA1R = BF · T · P · A1 · R = Butterfly / Triple-Offset / Peek seat / A1 / RF

Pre-built prefixes (Type+Bore/Design+Seat):
  Ball:       BLRT (R bore, PTFE seat)     BLFT (F bore, PTFE)
              BLRP (R bore, PEEK — 900#+)  BLFP (F bore, PEEK)
              BLRM (R bore, Metal — all G-series)  BLFM (F bore, Metal)
  Gate:       GAYM (Y-body, Metal)
  Globe:      GLYM (Y-body, Metal)
  Check:      CHPM (Piston, Metal)   CHSM (Swing, Metal)   CHDM (Dual-Plate, Metal)
  Butterfly:  BFWT (Wafer, PTFE)     BFTP (Triple-Offset, PEEK)
  DBB:        DBRP (R bore, PEEK — 900#+)  DBRM (R bore, Metal — all G-series)
  DBB (Inst): add T suffix (RTJ + NPT female), e.g. DBRPE20NJT — soft-seat (DBRP) only.
  Needle:     NEIP (Straight-Inline, PEEK) | NEAP (Angle, PEEK) — tubing only
  Tubing:     BLFP, CHPM, DBFP, NEIP — all with JT end suffix

EndConn (last char/s — must match piping rating face):
  150#/300#/600#   → R (RF)  — standard steel classes
  900#/1500#/2500# → J (RTJ)
  CuNi (A30)       → F (FF)  — Flat Face face-type, per EEMUA 234
  Copper (A40)     → F (FF)  — Flat Face, per ASME B 16.24
  GRE (A50/51/52)  → F (FF)  — Flat Face, manufacturer std
  CPVC (A60)       → F (FF)  — Flat Face, ASTM F 441 socket/flange
  Tubing (T80/T90) → F or JT (per inst. isolation)
  Hub-connected    → H (rare — only when spec explicitly calls for compact flange)

valves.rating field MUST include face-type:
  "150#, RF" / "300#, RF" / "600#, RF"
  "900#, RTJ" / "1500#, RTJ" / "2500#, RTJ"
  CuNi (A30):    "150#, FF"  (actually EEMUA 20 bar but use 150# convention)
  Copper (A40):  "150#, FF"
  GRE (A50-52):  "150#, FF"
  CPVC (A60):    "150#, FF"
  Tubing: "10000# (69 Mpa)" or as specified

=== VALVE DESIGN STANDARDS (from VMS Section 6) ===
Every VDS emitted MUST follow these standards. The AI should reference them
when populating design_code / standards-related notes fields.

  • P-T rating basis (all valves): ASME B16.34
  • Face-to-face / end-to-end:     ASME B16.10 OR API 6D
  • Ball valve (≤ 24" AND ≤ 600#): API 6D OR ISO 17292
  • Ball valve (> 600#):           API 6D
  • Gate valve:                    API 600 / API 602 / API 603 (as applicable)
  • Globe valve:                   API 602 OR BS EN ISO 15761 OR BS 1873
  • Check valve:                   API 594 / API 6D / BS 1868 / BS 5352 /
                                   BS EN ISO 15761 / API 602 (as applicable)
  • Sour service (any valve):      NACE MR0175 / ISO 15156
  • Forged construction:           required for DN 40 (NPS 1½) AND below

Material / construction rules the AI must respect:
  • Metal-seated ball: tungsten-carbide coated, min 1050 Vickers, 150–250 µm thick
  • Trunnion-mounted ball: Double-Block-and-Bleed (DBB) with spring-loaded seats
  • Gate valves: for "clean" non-hydrocarbon service; for 900#+ HC, allowed ≤ 1.5"
  • Gate wedge: solid ≤ 1.5", flexible > 1.5"
  • Wafer-type valves: NOT allowed in flammable/combustible service
  • Full-bore ball required: PSV inlet/outlet, piggable lines

Special valve rules:
  E-series (900#) Ball: 0.5"-1.5" → "USE GATE VALVE" (small-bore only); 2"+ → BLRP/BLFP codes (no ball valve between 2" and the spec boundary; the renderer caps "USE GATE VALVE" at 1.5" regardless)
  F-series (1500#) Ball: 0.5"-1.5" → "USE GATE VALVE"; 2"+ → BLRP/BLFP codes (soft-seat only)
  ****** MANDATORY RULE FOR G-SERIES 2500# (G1, G1N, G1LN, G2N, G7LN, G9, G10, G20N, G23, G24, G25, G25N, D25N, etc.) ******
  For ANY piping class starting with the letter "G" (2500# rating):
    The "ball" field MUST contain exactly FOUR codes, comma-separated in this order:
       BLRP + class-code-with-J + ", " + BLFP + class-code-with-J + ", " + BLFM + class-code-with-J + ", " + BLRM + class-code-with-J
       Example for G25N: "BLRPG25NJ, BLFPG25NJ, BLFMG25NJ, BLRMG25NJ"
       Example for G1  : "BLRPG1J, BLFPG1J, BLFMG1J, BLRMG1J"
       Example for G20N: "BLRPG20NJ, BLFPG20NJ, BLFMG20NJ, BLRMG20NJ"
       (For small sizes 0.5"-1.5", ball_by_size entries should still use "USE GATE VALVE";
        the renderer caps "USE GATE VALVE" text at 1.5" even if LVCF would otherwise carry it forward.)
    The "dbb" field MUST contain exactly TWO codes comma-separated:
       DBRP + class-code-with-J + ", " + DBRM + class-code-with-J
       Example for G25N: "DBRPG25NJ, DBRMG25NJ"
       Example for G1  : "DBRPG1J, DBRMG1J"
    The "dbb_inst" field: soft-seat variant only with T suffix (e.g. "DBRPG25NJT"). Do NOT add metal-seat T.
  ****** END MANDATORY RULE ******
  CuNi (30): Use F suffix (FF face). Codes: BLRTA30F, BLFTA30F, GAYMA30F, GLYMA30F, CHPMA30F, etc.
  GALV (3/4/5/6,B4,D4): Use R suffix. Codes: BLRTA3R, BLFTA3R, GAYMA3R, GLYMA3R, CHPMA3R, etc.

IMPORTANT — SIZE-SPECIFIC VALVE CODES:
Valve VDS codes are NOT uniform across all sizes. Different codes apply at different size ranges.
Example for class A1:
  - Check: 0.5"-3" → "CHPMA1R", 4"-24" → "CHSMA1R, CHDMA1R" (swing/dual-plate for larger sizes)
  - Butterfly: Only available for 3"+ (typically 6"+) → "BFWTA1R, BFTPA1R" (empty for smaller sizes)
  - Ball: 0.5"-2" → "BLRTA1R" (reduced trunnion), 2.5"-24" → "BLRTA1R, BLFTA1R" (both reduced + full)

You MUST provide valve codes using the *_by_size arrays to capture these size-specific differences.
Each entry is {{"size_inch": "...", "code": "..."}}. One entry per pipe size in the class.
If a valve type is not available at a given size, set code to "".
The class-level string fields (ball, gate, globe, check, butterfly) serve as fallback descriptions only.

Multiple valve types in one field → comma-separated: "BLRT{{piping_class}}R, BLFT{{piping_class}}R"

=== MISC ===
Design Code:
  Standard: "ASME B 31.3"
  + NACE suffix: ", NACE-MR-01-75/ISO-15156-1/2/3" if N or LN in class name
  CuNi (A30): "ASME B 31.3 / EEMUA 234"
  GRE (A50/A51/A52): "ASME B 31.3 / ISO 14692"

Pipe Code (exact string per spec sheet row "Code"):
  CS / LTCS / GALV / Epoxy (A1…G1 series, A1N…G1N, LTCS 1L/1LN, 2-series, 3/4/5/6): "ASME B 36.10M"
  SS 316L 10-series (A10, A10N):                              "ASME B 36.19M"
  DSS 20-series (A20, A20N):                                  "ASME B 36.19M"
  SDSS 25-series (A25, A25N):                                 "ASME B 36.19M"
  Mixed seamless-small-bore + welded-large-bore variants — use BOTH codes separated by " / ":
    B10, B10N, D10, D10N, E10, E10N, F10, F10N, G10, G10N:    "ASME B 36.19M / B 36.10M"
    B20, B20N, D20, D20N, E20, E20N, F20, F20N:               "ASME B 36.19M / B 36.10M"
    B25, B25N, D25, D25N, E25, E25N, F25, F25N:               "ASME B 36.19M / B 36.10M"
  G20, G20N, G25, G25N, G2N (2500# duplex/SDSS variants with welded pipe only): "ASME B 36.10M"
  CuNi (A30):                                                 "EEMUA 234 20 BAR"
  Copper (A40):                                               "ASTM B42 (Regular)"
  GRE A50 / A52:                                              "Manufacturer's Std."
  GRE A51:                                                    "Manufacturer's Std (BONSTRAND Series 50000C)"
  CPVC (A60):                                                 "ASTM F 441"
  Tubing (T80A/B/C, T90A/B/C):                                "ASTM A 269"
Mill Tolerance: {MILL_TOLERANCE_PERCENT}% (standard) — {MILL_TOLERANCE_PERCENT / 100}
Branch Chart:
  CS/LTCS/SS/DSS/SDSS (all numbered/N/L/LN variants): Ref. APPENDIX-1, Chart 1
  GALV (A3/A4/B4/D4/A5):                              Ref. APPENDIX-1, Chart 2
  CuNi (A30):                                         Ref. APPENDIX-1, Chart 3
  Copper (A40):                                       Ref. APPENDIX-1, Chart 3  (same as CuNi)
  GRE (A50/A51/A52), Epoxy-lined CS (A6):             Ref. APPENDIX-1, Chart 4
  CPVC (A60):                                         Ref. APPENDIX-1, Chart 4  (manufacturer sockets)
  Tubing (T80/T90):                                   "" (no branch chart — compression fittings only)
Ends: "BE" (bevel end for standard piping), "PE" (plain end for CuNi/tubing), special for GRE/CPVC

=== NOTES (STANDARD NUMBERED LIST) ===
The "notes" array must be a numbered list in position order (the Excel writer renders items as 1, 2, 3, ...). flange_type and spectacle_blind strings reference notes by position, so the notes list MUST contain items at every referenced position, and the content at that position MUST match the citation.

Compose each note in your own wording from the REQUIREMENT at each position. Do not invent requirements; do not drop required positions. The positions below describe what each numbered note must cover.

Positions 1-7 — apply to ALL standard piping classes (A/B/D/E/F/G series, 1/1N/2/2N/1L/1LN/2LN variants):
  Position 1: Reference to the Project Piping Design Basis and Valve Material Specification as companion documents to this PMS.
  Position 2: Weld joint factor for welded pipe follows ASME B31.3.
  Position 3: Welded fittings require 100% radiographic examination.
  Position 4: Spectacle blind and spacer sizes / ratings outside ASME B16.48 scope follow manufacturer standard, with design submitted to Company for review and approval.
  Position 5: Soft-seat ball valves have a maximum service temperature of 250°C.
  Position 6: Wafer check valves are avoided unless space constraints prevent use of a standard check valve.
  Position 7: Wafer-type butterfly valves are limited to water service and excluded from hydrocarbon service.

RF classes (150#/300#/600# — A1/B1/D1 and variants) — add positions 8-9:
  Position 8: Two jackscrews (180° apart) required in one flange of every orifice flange assembly and every specified spectacle-blind assembly.
  Position 9: Triple Offset Butterfly Valve (BFTT type) is permitted for Hydrocarbon (HM) service.

RTJ classes (900#/1500#/2500# — E/F/G series and variants) — add positions 8-10:
  Position 8: Two jackscrews (180° apart) required in one flange of every orifice flange assembly, every WNRTJ flange of size 3" and larger, and every specified spectacle-blind assembly.
  Position 9: Gasket contact surface must have maximum roughness of 63 AARH.
  Position 10: RTJ groove hardness must be minimum 120 BHN.

NACE classes (class name contains N or LN): append a final note position citing NACE MR-01-75 / ISO-15156 compliance for sour-service components.

CuNi / GRE / CPVC / Tubing classes: keep positions 1-4 where applicable; add material-specific positions covering EEMUA 234 (CuNi) or manufacturer-standard requirements as relevant.

IMPORTANT:
- The flange_type string references notes BY POSITION (e.g. a trailing ", Note 8,9" citation). If flange_type cites any note number, that position MUST exist in the notes array and the content MUST match what the citation implies.
- Spectacle_blind.standard_large may also cite note positions (e.g. "(Note 5)"). Ensure every cited position exists in the notes list.
- Return the notes as PLAIN STRINGS in order (position 1 first, then position 2, etc.). Do NOT prefix them with numbers — the renderer adds numbering.

=== OUTPUT JSON SCHEMA ===
{{
    "design_code": "...",
    "pipe_code": "...",
    "branch_chart": "Ref. APPENDIX-1, Chart 1",
    "hydrotest_pressure": "",
    "pipe_data": [
        {{"size_inch": "0.5", "od_mm": 21.3, "schedule": "SCH 160", "wall_thickness_mm": 7.47,
          "pipe_type": "Seamless", "material_spec": "ASTM A 106 Gr. B", "ends": "BE",
          "id_mm": 0}},
        ...for ALL sizes in the class...
    ],
    // id_mm is optional and defaults to 0 (row hidden). Populate it only for GRE
    // classes (A50/A51/A52) where the spec sheet carries an Inside Diameter row.
    "fittings": {{"fitting_type": "...", "material_spec": "...",
                  "elbow_standard": "...", "tee_standard": "...", "reducer_standard": "...",
                  "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "...",
                  "rating": ""}},
    // fittings.rating is optional (only GRE A50/A52 use it — e.g. "20 bar, 93degC").
    // Most classes should emit rating as empty string.
    "fittings_welded": {{"fitting_type": "...", "material_spec": "...",
                  "elbow_standard": "...", "tee_standard": "...", "reducer_standard": "...",
                  "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "..."}},
    "fittings_by_size": [
        {{"size_inch": "0.5", "type": "Seamless", "fitting_type": "...", "material_spec": "...",
          "elbow_standard": "...", "tee_standard": "...", "reducer_standard": "...",
          "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "...",
          "coupling_standard": "", "union_standard": "", "sockolet_standard": "",
          "nipple_standard": "", "swage_standard": "",
          "mold_tee_standard": "", "red_saddle_standard": "", "adaptor_standard": ""}}
    ],
    // Optional fittings fields (auto-hidden row when ALL sizes empty):
    //   Copper A40  → populate coupling_standard / union_standard / sockolet_standard /
    //                 nipple_standard / swage_standard with MOC split values.
    //   CuNi A30    → similar pattern (EEMUA 234 values).
    //   GRE A50/A52 → populate mold_tee_standard / red_saddle_standard / adaptor_standard.
    //   GRE A51     → populate the same GRE fields but with the generic
    //                 "Manufacturer standard (BONSTRAND Series 50000C)" string.
    //   All other classes → leave these empty.
    "flange": {{"material_spec": "...", "face_type": "...", "flange_type": "...", "standard": "...",
                 "compact_flange": "", "hub_connector": ""}},
    "spectacle_blind": {{"material_spec": "...", "standard": "...", "standard_large": "..."}},
    "bolts_nuts_gaskets": {{"stud_bolts": "...", "hex_nuts": "...", "gasket": "...",
                            "washers": "", "gasket_2": ""}},
    // washers and gasket_2 are optional (only GRE A50/A51/A52 populate them).
    // Most classes: leave both as empty strings → rows hidden.
    "valves": {{
        "rating": "...",
        "ball": "...", "gate": "...", "globe": "...", "check": "...", "butterfly": "...",
        "dbb": "...", "dbb_inst": "...",
        "//_boundary_note": "Each _by_size list should use the spec-accurate boundary. Check valves: piston-check (CHPM) for small-bore, swing/dual-plate (CHSM, CHDM) for large-bore — boundary typically at 2\\" for most CS classes. Ball: BLRT for small-bore, BLRT + BLFT for large-bore — boundary typically at 2\\" (150#, 300#) or class-specific. If the spec shows ONE entry (e.g. BLRTA1R, BLFTA1R applies to all sizes), emit ONE entry at size 0.5 and it will propagate via LVCF.",
        "ball_by_size": [{{"size_inch": "0.5", "code": "BLRTA1R, BLFTA1R"}}],
        "gate_by_size": [{{"size_inch": "0.5", "code": "GAYMA1R"}}],
        "globe_by_size": [{{"size_inch": "0.5", "code": "GLYMA1R"}}],
        "check_by_size": [{{"size_inch": "0.5", "code": "CHPMA1R"}}, {{"size_inch": "2", "code": "CHSMA1R, CHDMA1R"}}],
        "butterfly_by_size": [{{"size_inch": "3", "code": "BFWTA1R, BFTPA1R"}}],
        "dbb_by_size": [{{"size_inch": "0.5", "code": "DBRPE20NJ"}}],
        "dbb_inst_by_size": [{{"size_inch": "0.5", "code": "DBRPE20NJT"}}]
    }},
    "notes": ["<position 1 text>", "<position 2 text>", "<position 3 text>", ...]
}}

CRITICAL:
1. Valve *_by_size arrays MUST have one entry per pipe size (matching pipe_data count). Use "" for sizes where valve type is not available.
2. The top-level valve string fields (ball, gate, dbb, dbb_inst, etc.) are fallback descriptions — the *_by_size arrays hold the actual per-size codes.
3. For 900#+ classes (E/F/G-series), include dbb and dbb_inst fields with DBRP prefix codes. dbb_inst code = dbb code + "T" suffix. Omit dbb/dbb_inst for 150#-600# classes.
3. fittings_by_size count MUST match pipe_data count.
4. fittings_welded MUST be populated (not null) if class has welded fittings.
5. The od_mm and wall_thickness_mm fields for ASME-coded pipe classes (B36.10M / B36.19M — CS, LTCS, GALV, SS, DSS, SDSS, Titanium) are OVERWRITTEN after generation. Your ONLY job for those classes is to pick the correct SCHEDULE per the per-class rules above. Put any reasonable number in wall_thickness_mm — it WILL be replaced:
   - When schedule maps to a standard code (SCH 160, 80S, STD, XS, etc.): post-processor looks up WT from the ASME B36.10M / B36.19M tables.
   - When schedule is "-" (calculated WT case, e.g. F1LN 10-24", G2N 1-24"): post-processor COMPUTES WT server-side per ASME B31.3 §304.1.2 Eq. 3a using the class's design pressure (max from P-T table), design temperature (max from P-T table), material stress, joint factor E=1.0, Y=0.4, W=1.0, corrosion allowance from the request, and mill tolerance 12.5%. So your emitted WT for "-" schedule rows will ALSO be replaced — emit any plausible value.
   For NON-ASME pipe codes (CuNi EEMUA 234, Copper ASTM B42, GRE manufacturer std, CPVC ASTM F441, Tubing ASTM A269), the AI's od_mm and wall_thickness_mm ARE preserved — be accurate for those.
6. Return ONLY JSON. No markdown fences, no commentary.
7. For GALV classes, gasket is neoprene/EPDM rubber (NOT spiral wound).
8. For CuNi classes, use EEMUA 234 standards throughout.
9. For Tubing classes, use compression fitting data, NOT standard piping format.

Generate PMS for class **{piping_class}** now."""


async def generate_pms_with_ai(
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    rating: str,
    reference_entries: list[dict],
) -> dict:
    """Call Claude API to generate PMS data (everything except P-T).
    Returns a dict of generated fields. Raises AIGenerationError on failure
    with a message describing the actual cause (credit balance, rate limit,
    auth error, model-not-found, etc.)."""

    if not settings.anthropic_api_key:
        raise AIGenerationError(
            "ANTHROPIC_API_KEY is not configured on the server."
        )

    prompt = _build_generation_prompt(
        piping_class, material, corrosion_allowance, service,
        rating, reference_entries,
    )

    logger.info("Calling Anthropic API for class %s with model %s", piping_class, settings.anthropic_model)

    response_text = ""
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        message = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=AI_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()

        # Clean up potential markdown fences
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            response_text = "\n".join(lines)

        data = json.loads(response_text)
        logger.info("AI successfully generated PMS data for class %s", piping_class)
        return data

    except json.JSONDecodeError as e:
        logger.error(
            "AI returned invalid JSON for %s: %s\nRaw response: %s",
            piping_class, e, response_text[:500],
        )
        raise AIGenerationError(
            "AI returned a malformed response. Try again, or regenerate."
        ) from e
    except anthropic.AuthenticationError as e:
        logger.error("Anthropic auth error for %s: %s", piping_class, e)
        raise AIGenerationError(
            "Anthropic API key was rejected. Please check that ANTHROPIC_API_KEY "
            "is set correctly on the server."
        ) from e
    except anthropic.NotFoundError as e:
        logger.error(
            "Anthropic model not found for %s: model='%s' — %s",
            piping_class, settings.anthropic_model, e,
        )
        raise AIGenerationError(
            f"Anthropic model '{settings.anthropic_model}' was not found. "
            "Update anthropic_model in config.py to a currently available model."
        ) from e
    except anthropic.RateLimitError as e:
        logger.error("Anthropic rate limit hit for %s: %s", piping_class, e)
        raise AIGenerationError(
            "Anthropic API rate limit reached. Please wait a minute and retry."
        ) from e
    except anthropic.APIError as e:
        msg = str(e)
        logger.error("Anthropic API error for %s: %s", piping_class, msg)
        low = msg.lower()
        if "credit balance" in low or "billing" in low:
            raise AIGenerationError(
                "Anthropic API credit balance is exhausted. Add credits at "
                "https://console.anthropic.com/settings/billing and try again."
            ) from e
        if "overloaded" in low:
            raise AIGenerationError(
                "Anthropic service is temporarily overloaded. Please retry."
            ) from e
        raise AIGenerationError(f"Anthropic API error: {msg}") from e
    except Exception as e:
        logger.error("Unexpected error in AI generation for %s: %s", piping_class, e, exc_info=True)
        raise AIGenerationError(f"Unexpected error during AI generation: {e}") from e

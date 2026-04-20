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
  GALV (3/4/5/6): 0.5" to 24" (17 sizes)
  CuNi (30): 0.5" to 36" (22 sizes — per EEMUA 234 size range)
  GRE (40/41/42): 0.75" to 24" (14 sizes)
  Tubing (T80/T90): Single size 0.5" only

Standard NPS sequence: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36

=== PIPE SCHEDULES — RULES BY MATERIAL FAMILY AND RATING ===
Use ASME B36.10M for CS/LTCS/GALV, ASME B36.19M for SS/DSS/SDSS.
Wall thicknesses must be EXACT standard values from the appropriate ASME table.

CS 1-series (A1/B1/D1/E1/F1/G1 and N variants):
  A1 (150#): 0.5"→XXS | 0.75-1.5"→160 | 2-6"→80 | 8-28"→STD | 30-36"→XS
  B1 (300#): 0.5-0.75"→XXS | 1-1.5"→160 | 2-6"→80 | 8-20"→40 | 22"→"-" | 24"→40
  D1 (600#): 0.5-1.5"→XXS | 2"→160 | 3-24"→80
  E1 (900#): 0.5-1.5"→XXS | 2-3"→160 | 4-24"→120
  F1 (1500#): 0.5-1.5"→XXS | 2-6"→160 | 8"→XXS | 10"→140 | 12,14"→160 | 16,18"→varies(140/160) | 20-24"→140/160 alternating
  G1 (2500#): 0.5-1.5"→XXS | 2"→"-" | 3"→XXS | 4-24"→"-" (special calc wall thickness)

CS 1-series NACE (A1N/B1N/D1N/E1N/F1N/G1N):
  A1N (150#): 0.5-1.5"→XXS | 2"→160 | 3-6"→80 | 8"→60 | 10"→40 | 12-16"→40 | 18"→30 | 20-24"→XS | 30"→30
  B1N (300#): 0.5-1.5"→XXS | 2"→160 | 3"→160 | 4"→120 | 6-10"→XS | 12-24"→60
  D1N (600#): 0.5-0.75"→"-" (calc WT) | 1-1.5"→XXS | 2"→XXS | 3-24"→80
  E1N (900#): 0.5-0.75"→"-" (calc WT) | 1-1.5"→XXS | 2-3"→160 | 4-24"→120
  F1N (1500#): 0.5-2"→"-" (calc WT) | 3-6"→160 | 8"→XXS | 10"→140 | 12-24"→varies(140/160)
  G1N (2500#): all sizes→"-" (special calculated wall thickness)

LTCS 1L-series:
  A1L (150#): 0.5"→XXS | 0.75-1.5"→160 | 2-28"→XS | 30"→30
  B1L (300#): 0.5"→XXS | 0.75-1.5"→160 | 2-6"→XS | 8-20"→40 | 22"→"-" | 24"→40
  D1L (600#): 0.5-1.5"→XXS | 2"→160 | 3-6"→XS | 8"→XS | 10-24"→80
  E1L (900#): 0.5-1.5"→XXS | 2"→160 | 3"→160 | 4-24"→120
  F1L (1500#): 0.5-1.5"→XXS | 2-8"→XXS | 10-24"→"-" (special wall thickness)
  G1L (2500#): 0.5-1"→XXS | 1.5-24"→"-" (special wall thickness)

LTCS NACE 1LN-series (same schedule as 1L, follow same rules):
  A1LN: same as A1L
  B1LN: same as B1L
  D1LN: 0.5"→160 | 0.75-1.5"→XXS | 2"→160 | 3-24"→80
  E1LN: 0.5-1.5"→XXS | 2-3"→160 | 4-24"→120
  F1LN: 0.5-1.5"→XXS | 2-8"→XXS | 10-24"→"-" (special wall thickness)
  G1LN: 0.5-1"→XXS | 1.5-24"→"-" (special wall thickness)

CS 2-series (heavy wall — A2/A2N/B2N/D2N/E2N/F2N/G2N + LN variants):
  A2 (150#): 0.5"→XXS | 0.75-1.5"→160 | 2"→80 | 3-6"→80 | 8"→STD | 10-16"→STD | 18"→STD | 20-28"→XS | 30"→30
  B2N (300#): 0.5-0.75"→XXS | 1-1.5"→160 | 2"→80 | 3-6"→80 | 8-20"→40 | 22"→60 | 24"→40
  D2N (600#): 0.5-0.75"→XXS(OD shifted: 0.5"=26.7, 0.75"=33.4) | 1-2"→XXS | 3-24"→80
  E2N (900#): starts at 1" | 1-1.5"→XXS | 2-6"→XXS | 8"→XXS | 10"→160 | 12-24"→140/120 mix
  F2N (1500#): starts at 1" | 1-1.5"→XXS | 2-4"→"-" | 6-10"→XXS | 12-24"→"-"/160 mix
  G2N (2500#): starts at 1" | 1-1.5"→XXS | 2-24"→"-" (special calculated wall thicknesses)

SS 316L 10-series (use "S" suffix for schedules from B36.19M):
  A10 (150#): 0.5-1.5"→80S | 2-6"→40S | 8"→40S | 10-14"→10S | 16-24"→40S (22"→STD) | 30"→STD
  B10 (300#): 0.5-1.5"→160 | 2-8"→40S | 10"→40S | 12-18"→40S | 20,24"→80S | 22"→60 | 30"→"-"
  D10 (600#): 0.5"→XXS | 0.75-1.5"→160 | 2-6"→80S | 8-24"→80 (22"→80)
  E10 (900#): 0.5" starts at 0.75" | 0.75"→XXS | 1-1.5"→160 | 2-6"→80S | 8-24"→100
  F10 (1500#): 0.5-1.5"→XXS | 2-24"→160
  G10 (2500#): 0.5-2"→XXS | 3"→XXS | 4-12"→"-" (11 sizes max to 12")

DSS 20-series (UNS S31803, use "S" suffix):
  A20 (150#): 0.5-2"→80S | 3-24"→10S | 26-30"→10 | 32"→10
  B20 (300#): 0.5-2"→80S | 3-8"→10S | 10"→10S | 12-14"→10/20 | 16-18"→10/20 | 20-24"→40S | 26"→STD | 28-32"→XS
  D20 (600#): 0.5-2"→80S | 3-12"→40S | 14"→40 | 16"→80S | 18-24"→40
  E20 (900#): 0.5-2"→80S | 3-6"→40S | 8"→60 | 10"→80S | 12-24"→60
  F20 (1500#): 0.5-2"→80S | 3"→80S | 4-6"→80S/120 | 8"→100 | 10-24"→120
  G20 (2500#): 0.5-1"→80S | 1.5-10"→160 | 12"→"-" | 14-18"→120 (Note 10)

SDSS 25-series (UNS S32750, use "S" suffix):
  A25 (150#): same as A20
  B25 (300#): 0.5-2"→80S | 3-12"→10S | 14-16"→10 | 18-22"→10 | 24"→40S | 26-32"→STD
  D25 (600#): 0.5-2"→80S | 3-6"→40S | 8-10"→20 | 12-18"→40S/80S | 20"→80S | 22"→XS | 24"→30
  E25 (900#): 0.5-2"→80S | 3-6"→40S | 8-10"→40S | 12"→80S | 14-24"→60
  F25 (1500#): 0.5-2"→80S | 3"→40S | 4-8"→80S | 10-12"→80 | 14-24"→100
  G25 (2500#): 0.5-2"→80S | 3"→160 | 4"→120 | 6-10"→160 | 12-14"→140 | 16-24"→140/160 mix

GALV classes (A3/A4/B4/D4/A5/A6):
  A3 (150# GALV screwed): 0.5-1.5"→XXS | 2-6"→80 | 8-24"→STD
  A4 (150# GALV screwed): 0.5-1.5"→160 | 2-6"→80 | 8-24"→STD
  B4 (300# GALV): 0.5-0.75"→XXS | 1-1.5"→160 | 2-6"→80 | 8-24"→40
  D4 (600# GALV): 0.5-1.5"→XXS | 2"→160 | 3-24"→80
  A5 (150# GALV 6mm): same as A3
  A6 (150# Epoxy): 0.5"→80S | 0.75-1.5"→80 | 2-6"→80 | 8-24"→STD

CuNi 30-series (EEMUA 234) — USE THESE EXACT ODs (from Pipe Class Sheet A30):
  A30: No standard schedule — uses EEMUA 234 wall thickness tables
  NPS → OD (mm):
    0.5"=16, 0.75"=25, 1"=30, 1.5"=44.5, 2"=57, 2.5"=73, 3"=88.9, 4"=108,
    6"=159, 8"=219.1, 10"=267, 12"=323.9, 14"=368, 16"=419, 18"=457.2,
    20"=508, 22"=559, 24"=610, 28"=711, 30"=762
  NPS → WT (mm):
    0.5"=2.0, 0.75"=2.0, 1"=2.5, 1.5"=2.5, 2"=2.5, 2.5"=2.5, 3"=2.5, 4"=3.0,
    6"=3.5, 8"=4.5, 10"=5.5, 12"=7.0, 14"=8.0, 16"=9.0, 18"=9.5, 20"=10.0,
    24"=11.0, 28"=12.5
  Schedule: "-" for all (EEMUA has its own thickness system; no ASME schedule applies)
  IMPORTANT: Use the exact OD values above; do NOT substitute ASME B36.10M/B36.19M ODs.

GRE (A50/A51/A52) — Manufacturer's Standard (NOT ASME):
  A50/A52 ODs (mm): 1"=34.1, 1.5"=49.1, 2"=57.8, 3"=86.4, 4"=110.6, 6"=166.6,
                    8"=218.4, 10"=274.5, 12"=327.3, 14"=359.2, 16"=410.5,
                    18"=452.2, 20"=502.3, 24"=602.8, 28"=728.6, 30"=780.6,
                    32"=832.6, 34"=884.6, 36"=936.4, 40"=1040.6
  A51 ODs (mm):     1"=31.5, 1.5"=46.5, 2"=57.4, 3"=86.2, 4"=111.6, 6"=165.4
  Schedule = "-" (no ASME schedule). WT per manufacturer standard.

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
  CuNi (30): ALL Seamless (no transition)
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

Copper (31/40):
  ASTM B 88 Type K or ASTM B 75

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
CuNi (30): Small sizes = "SW" (Socket Weld), larger = "Butt Weld (SCH to match pipe), Seamless"
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
  CuNi (30): 90-10 Cu-Ni per EEMUA 234
  Titanium (70): ASTM B 363 Gr. 2

STANDARDS (apply to ALL material families unless noted):
  Elbow: ASME B 16.9 | Tee: ASME B 16.9 | Reducer: ASME B 16.9 | Cap: ASME B 16.9
  Plug: Hex Head Plug, ASME B 16.11 (or "Hex Head, ASME B 16.11")
  Weldolet: MSS SP 97, [flange MOC] (e.g., "MSS SP 97, ASTM A 105N" for CS)
  GALV screwed classes: Elbow/Tee/Red/Cap = ASME B 16.11
  CuNi classes: All fittings per EEMUA 234; additional: Coupling, Union, Sockolet, Nipple, Swage per EEMUA 234
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
  CuNi (30): 90-10Cu-Ni per EEMUA 234 20 BAR; Blind Flange = ASTM A 105N FF with 3mm 90-10 CuNi weld deposit

FACE by rating:
  150#: "150# RF, Serrated Finish"
  300#: "300# RF, Serrated Finish"
  600#: "600# RF, Serrated Finish"
  900# (E-series): Small bore (0.5-1.5") = "1500#, RTJ", Larger sizes (2"+) = "900#, RTJ"
  1500# (F-series): "1500#, RTJ"
  2500# (G-series): "2500#, RTJ"
  CuNi EEMUA: "EEMUA 20 bar, FF" (Flat Face)
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
  CuNi (A30): small sizes use socket-weld (SW) flange, larger sizes use WN flange per EEMUA 234

F/G-series (1500#/2500#) additional flange rows (populate compact_flange and hub_connector):
  compact_flange — describe the Norsok L-005 WN Compact Flange used for layout-constrained installations. Include the Norsok L-005 reference and a short note that it is for layout constraint.
  hub_connector — describe the hub-connector assembly: seal ring material (ASTM A 182 F 316L), hub and blind-hub material (ASTM A 694 F60), clamp material (AISI 4140), and indicate bolt material per the bolts/nuts section. Add a note that it is used where ANSI or Compact Flange are unsuitable.

=== SPECTACLE BLIND ===
MOC: Same as flange MOC
Standard: "ASME B 16.48" (standard sizes)
Standard (large): "Spacer and blind as per ASME B 16.48 (Note 5)" (sizes not covered by B16.48)
F/G series (1500#/2500#): MOC = ASTM A 694 F60, Standard = "ASME B 16.48",
  Standard_large = "Spacer and blind as per ASME B 16.48" (ALWAYS populate this for F/G classes — the reference splits the row with B16.48 on the small-size side and "Spacer and blind as per ASME B 16.48" on the large-size side)
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
    CuNi: 3mm thick flat ring of neoprene/ EPDM rubber as ASME B 16.21
  RTJ classes (900#+):
    CS/LTCS: ASME B 16.20, OCT ring of Soft Iron with Max. Hardness of 90 BHN, HDG
    SS: OCT Ring, SS316L, Max 160 BHN Hardness, ASME B16.20
    DSS: OCT Ring, DSS UNS S31803, Max 22 HRC Hardness, ASME B16.20
    SDSS: OCT Ring, SDSS UNS S32750, Max 22 HRC Hardness, ASME B16.20

=== VALVE CODES ===
Pattern: [TYPE PREFIX][CLASS CODE][FACE SUFFIX]
  CLASS CODE = exact piping class name (A1, B1N, D20, E25N, etc.)
  FACE SUFFIX: R = RF (150#-600#) | J = RTJ (900#-2500#) | F = FF (CuNi/GRE/CPVC) | JT = (Tubing)

Standard valve prefixes:
  Ball Reduced Trunnion: BLRT | Ball Full Trunnion: BLFT
  Ball Reduced Port (soft-seat):  BLRP | Ball Full Port (soft-seat):  BLFP  (E/F/G-series 900#+)
  Ball Reduced Port (metal-seat): BLRM | Ball Full Port (metal-seat): BLFM  (ALL G-series 2500# classes)
  Gate Y-body: GAYM
  Globe Y-body: GLYM
  Check Piston: CHPM | Check Swing: CHSM | Check Dual-Plate: CHDM
  Butterfly Wafer: BFWT | Butterfly Triple-Offset: BFTP
  DBB Reduced Port (soft-seat):  DBRP  (available in 900#+ classes)
  DBB Reduced Port (metal-seat): DBRM  (ALL G-series 2500# classes — list alongside DBRP)
  DBB (Inst): DBB code with T suffix appended (e.g., DBRPE20NJ → DBRPE20NJT). Use soft-seat (DBRP) only.
  Tubing valves: DBB=DBFP, Needle=NEIP, Ball=BLFP, Check=CHPM (with JT suffix)

valves.rating field MUST include face-type suffix:
  150# / 300# / 600#: "150#, RF" / "300#, RF" / "600#, RF"
  900# / 1500# / 2500#: "900#, RTJ" / "1500#, RTJ" / "2500#, RTJ"
  CuNi: "150#, FF"
  Tubing: "10000# (69 Mpa)" or as specified

Special valve rules:
  E-series (900#) Ball: Small sizes → "USE GATE VALVE", larger sizes (typically 6"+) → BLRP/BLFP codes
  F-series (1500#) Ball: Small sizes → "USE GATE VALVE", larger sizes → BLRP/BLFP codes (soft-seat only)
  ****** MANDATORY RULE FOR G-SERIES 2500# (G1, G1N, G1LN, G2N, G7LN, G9, G10, G20N, G23, G24, G25, G25N, D25N, etc.) ******
  For ANY piping class starting with the letter "G" (2500# rating):
    The "ball" field MUST contain exactly FOUR codes, comma-separated in this order:
       BLRP + class-code-with-J + ", " + BLFP + class-code-with-J + ", " + BLFM + class-code-with-J + ", " + BLRM + class-code-with-J
       Example for G25N: "BLRPG25NJ, BLFPG25NJ, BLFMG25NJ, BLRMG25NJ"
       Example for G1  : "BLRPG1J, BLFPG1J, BLFMG1J, BLRMG1J"
       Example for G20N: "BLRPG20NJ, BLFPG20NJ, BLFMG20NJ, BLRMG20NJ"
       (For small sizes, ball_by_size entries should still use "USE GATE VALVE")
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

=== EXTRA FITTINGS ===
Standard piping classes:
  Coupling: "ASME B 16.11, sizes 0.5\\" to 2.0\\" only" — ONLY for 150#/300# classes
  Union: "ASME B 16.11, sizes 0.5\\" to 2.0\\" only" — ONLY for 150# classes
  Hex Plug: "ASME B 16.11, all sizes" — ALL classes
  Olet: "MSS SP 97, [flange MOC], all sizes" — ALL classes
  Swage: empty

CuNi (30) extra fittings:
  Coupling: EEMUA 234
  Union: EEMUA 234
  Sockolet: EEMUA 234
  Nipple: EEMUA 234, MOC Same as pipe
  Swage: EEMUA 234, MOC Same as pipe
  Weldolet: EEMUA 234

Tubing (T80/T90):
  Only "Compression Fitting" — body AISI 316, ferrules and nuts in AISI 316
  Ends: OD X THD, OD X OD, & OD X SW (Manufacturer Standard)

=== MISC ===
Design Code:
  Standard: "ASME B 31.3"
  + NACE suffix: ", NACE-MR-01-75/ISO-15156-1/2/3" if N or LN in class name
  CuNi: "ASME B 31.3 / EEMUA 234"

Pipe Code: ASME B 36.10M (CS/LTCS/GALV) or ASME B 36.19M (SS/DSS/SDSS) or EEMUA 234 (CuNi)
Mill Tolerance: {MILL_TOLERANCE_PERCENT}% (standard) — {MILL_TOLERANCE_PERCENT / 100}
Branch Chart:
  CS/LTCS/SS/DSS/SDSS: Ref. APPENDIX-1, Chart 1
  GALV: Ref. APPENDIX-1, Chart 2
  CuNi: Ref. APPENDIX-1, Chart 3
  GRE: Ref. APPENDIX-1, Chart 4
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
    "mill_tolerance": "{MILL_TOLERANCE_PERCENT}%",
    "branch_chart": "Ref. APPENDIX-1, Chart 1",
    "hydrotest_pressure": "",
    "pipe_data": [
        {{"size_inch": "0.5", "od_mm": 21.3, "schedule": "SCH 160", "wall_thickness_mm": 7.47,
          "pipe_type": "Seamless", "material_spec": "ASTM A 106 Gr. B", "ends": "BE"}},
        ...for ALL sizes in the class...
    ],
    "fittings": {{"fitting_type": "...", "material_spec": "...",
                  "elbow_standard": "...", "tee_standard": "...", "reducer_standard": "...",
                  "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "..."}},
    "fittings_welded": {{"fitting_type": "...", "material_spec": "...",
                  "elbow_standard": "...", "tee_standard": "...", "reducer_standard": "...",
                  "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "..."}},
    "fittings_by_size": [
        {{"size_inch": "0.5", "type": "Seamless", "fitting_type": "...", "material_spec": "...",
          "elbow_standard": "...", "tee_standard": "...", "reducer_standard": "...",
          "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "..."}}
    ],
    "extra_fittings": {{"coupling": "...", "hex_plug": "...", "union": "...", "union_large": "", "olet": "...", "olet_large": "", "swage": ""}},
    "flange": {{"material_spec": "...", "face_type": "...", "flange_type": "...", "standard": "...",
                 "compact_flange": "", "hub_connector": ""}},
    "spectacle_blind": {{"material_spec": "...", "standard": "...", "standard_large": "..."}},
    "bolts_nuts_gaskets": {{"stud_bolts": "...", "hex_nuts": "...", "gasket": "..."}},
    "valves": {{
        "rating": "...",
        "ball": "...", "gate": "...", "globe": "...", "check": "...", "butterfly": "...",
        "dbb": "...", "dbb_inst": "...",
        "ball_by_size": [{{"size_inch": "0.5", "code": "BLRTA1R"}}, {{"size_inch": "2", "code": "BLRTA1R, BLFTA1R"}}, ...],
        "gate_by_size": [{{"size_inch": "0.5", "code": "GAYMA1R"}}, ...],
        "globe_by_size": [{{"size_inch": "0.5", "code": "GLYMA1R"}}, ...],
        "check_by_size": [{{"size_inch": "0.5", "code": "CHPMA1R"}}, {{"size_inch": "4", "code": "CHSMA1R, CHDMA1R"}}, ...],
        "butterfly_by_size": [{{"size_inch": "6", "code": "BFWTA1R, BFTPA1R"}}, ...],
        "dbb_by_size": [{{"size_inch": "0.5", "code": "DBRPE20NJ"}}, ...],
        "dbb_inst_by_size": [{{"size_inch": "0.5", "code": "DBRPE20NJT"}}, ...]
    }},
    "notes": ["<position 1 text>", "<position 2 text>", "<position 3 text>", ...]
}}

CRITICAL:
1. Valve *_by_size arrays MUST have one entry per pipe size (matching pipe_data count). Use "" for sizes where valve type is not available.
2. The top-level valve string fields (ball, gate, dbb, dbb_inst, etc.) are fallback descriptions — the *_by_size arrays hold the actual per-size codes.
3. For 900#+ classes (E/F/G-series), include dbb and dbb_inst fields with DBRP prefix codes. dbb_inst code = dbb code + "T" suffix. Omit dbb/dbb_inst for 150#-600# classes.
3. fittings_by_size count MUST match pipe_data count.
4. fittings_welded MUST be populated (not null) if class has welded fittings.
5. Wall thickness values will be auto-corrected from ASME B36.10M/B36.19M lookup tables after generation. Focus on selecting the CORRECT SCHEDULE for each size — the WT will be looked up. For "-" schedule sizes (calculated WT), provide the calculated WT value.
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

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

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior piping materials engineer with deep expertise in:
- ASME B31.3 (Process Piping), B36.10M (Welded/Seamless Wrought Steel Pipe), B36.19M (Stainless Steel Pipe)
- ASME B16.5 (Flanges), B16.9 (BW Fittings), B16.11 (Forged Fittings), B16.20 (Gaskets), B16.47 (Large Flanges), B16.48 (Line Blanks)
- ASTM material standards for CS, LTCS, SS316L, Duplex, Super Duplex, CuNi, Titanium, GRE, CPVC
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

=== CLASS NAMING CONVENTION (decode the class name) ===
Format: [Letter][Number][Suffix]

LETTER = Rating:
  A=150# | B=300# | D=600# | E=900# | F=1500# | G=2500# | T=Tubing

NUMBER = Material Family:
  1 = Carbon Steel (CS): Pipe=ASTM A 106 Gr. B (seamless), API 5L Gr. B or ASTM A 671 CC60 Class 22 (welded)
  1L = Low-Temp CS (LTCS): Pipe=ASTM A 333 Gr.6 (seamless), ASTM A 671 CC60 Class 22 (welded)
  2 = CS Heavy Wall (same MOC as 1-series but heavier schedules)
  3,4 = CS Galvanized Screwed: Pipe=ASTM A 106 Gr. B (Galvanized)
  5 = CS Galvanized: Pipe=ASTM A 106 Gr. B (Galvanized)
  6 = CS Epoxy Lined: Pipe=ASTM A 106 Gr. B (Galvanized)
  10 = SS 316L: Pipe=ASTM A 312 TP 316L (seamless), ASTM A 358 TP 316L (welded)
  20 = Duplex SS (DSS) UNS S31803: Pipe=ASTM A 790 Gr. S31803 (both seamless & welded)
  25 = Super Duplex SS (SDSS) UNS S32750: Pipe=ASTM A 790 Gr. S32750 (both seamless & welded)
  30 = CuNi 90/10 | 40 = Copper | 50,52 = GRE | 51 = GRE Bonstrand | 60 = CPVC | 70 = Titanium
  80A/B/C = SS316L Tubing | 90A/B/C = 6MO Tubing

SUFFIX:
  N = NACE (sour service) — adds "NACE-MR-01-75/ISO-15156-1/2/3" to design code
  L = Low Temperature variant
  LN = Low Temp + NACE

=== PIPE SIZES — STANDARD NPS RANGES ===
Generate ALL standard NPS sizes for the class. Typical ranges:
  A-series 150# CS (1/1N): 0.5" to 36" (A1=22 sizes, A1N=21 sizes to 32")
  A-series 150# LTCS (1L/1LN): 0.5" to 30" (20 sizes)
  A-series 150# SS/DSS/SDSS (10/20/25): 0.5" to 24-32" (17-21 sizes)
  B-series 300#: 0.5" to 24" (17 sizes) — for DSS/SDSS up to 32" (21 sizes)
  D-series 600#: 0.5" to 24" (17 sizes)
  E-series 900#: 0.5" to 24" (17 sizes) — 2N/2LN start at 1" (15 sizes)
  F-series 1500#: 0.5" to 24" (17 sizes) — 2N/2LN start at 1" (15 sizes)
  G-series 2500#: 0.5" to 24" (17 sizes) — G10: to 12" (11), G20: to 18" (14), 2N/2LN start at 1" (15 sizes)
Standard NPS sequence: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36

=== PIPE SCHEDULES — RULES BY MATERIAL FAMILY AND RATING ===
Use ASME B36.10M for CS/LTCS, ASME B36.19M for SS/DSS/SDSS.
Wall thicknesses must be EXACT standard values from the appropriate ASME table.

CS 1-series (A1/B1/D1/E1/F1/G1 and N variants):
  A1 (150#): 0.5-1.5"→160 | 2-6"→80 | 8-28"→STD | 30-36"→XS
  B1 (300#): 0.5-1.5"→160 | 2-6"→80 | 8-20"→40 | 22"→"-" | 24"→40
  D1 (600#): 0.5-2"→160 | 3-24"→80
  E1 (900#): 0.5-2"→160 | 3"→160 | 4-24"→120
  F1 (1500#): 0.5-1.5"→XXS | 2-6"→160 | 8"→XXS | 10"→140 | 12,14"→160 | 16,18"→varies(140/160) | 20-24"→140/160 alternating
  G1 (2500#): 0.5-1.5"→XXS | 2"→"-" | 3"→XXS | 4-24"→"-" (special calc wall thickness)

LTCS 1L-series:
  A1L (150#): 0.5-1.5"→160 | 2-28"→XS | 30"→30
  B1L (300#): 0.5-1.5"→160 | 2-6"→XS | 8-20"→40 | 22"→"-" | 24"→40
  D1L (600#): 0.5-2"→160 | 3-6"→XS | 8"→XS | 10-24"→80
  E1L (900#): 0.5-1.5"→XXS | 2"→160 | 3"→160 | 4-24"→120
  F1L (1500#): 0.5-1.5"→XXS | 2-8"→XXS | 10-24"→"-" (special wall thickness)
  G1L (2500#): 0.5-1"→XXS | 1.5-24"→"-" (special wall thickness)

CS 2-series (heavy wall/NACE — A2/A2N/B2N/D2N/E2N/F2N/G2N + LN variants):
  A2 (150#): 0.5-1.5"→XXS | 2"→160 | 3-6"→80 | 8"→60 | 10-14"→40 | 16"→40 | 18"→30 | 20-28"→XS | 30"→30
  B2N (300#): 0.5-1.5"→XXS | 2"→160 | 3"→160 | 4"→120 | 6"→XS | 8-10"→XS | 12-24"→60
  D2N (600#): 0.5-0.75"→"-" | 1-1.5"→XXS | 2"→XXS | 3-4"→160 | 6"→120 | 8-24"→100/120 mix
  E2N (900#): starts at 1" | 2-6"→XXS | 8"→XXS | 10"→160 | 12-24"→140/120 mix
  F2N (1500#): starts at 1" | 2-4"→"-" | 6-10"→XXS | 12-24"→"-"/160 mix
  G2N (2500#): starts at 1" | all "-" (special calculated wall thicknesses)

SS 316L 10-series (use "S" suffix for schedules from B36.19M):
  A10 (150#): 0.5-0.75"→160 | 1-6"→80S | 8-24"→40S (22"→STD)
  B10 (300#): 0.5-1.5"→160 | 2"→80S | 3-18"→40S | 20,24"→80S | 22"→XS
  D10 (600#): 0.5-1.5"→160 | 2-6"→80S | 8-24"→60 (22"→"-")
  E10 (900#): 0.5-1.5"→160 | 2-6"→80S | 8-24"→100
  F10 (1500#): all sizes→160
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

=== PIPE TYPE TRANSITION (Seamless → Welded) ===
All mainstream classes have TWO pipe types with a size-based transition:
  CS 1-series (A1/A1N): Seamless → LSAW, 100% RT (transition at ~20")
  CS 1-series (B1/D1/E1 and N): Seamless → EFW, 100% RT (transition at ~14-18")
  CS F1/G1 series: ALL sizes use API 5L Gr. X60 PSL-2 (single type for both)
  LTCS 1L-series: Seamless → EFW, 100% RT (transition at ~14")
  SS316L 10-series: Seamless → EFW, 100% RT (transition at ~10")
  DSS 20-series: Seamless → Welded (Longitudinally) with 100% RT (transition at ~10")
  SDSS 25-series: Seamless → Welded (Longitudinally) with 100% RT (transition at ~10")
  GALV 3/4/5/6/B4/D4: Seamless → LSAW, 100% RT (transition at ~14")
  Special classes (30,40,50-52,60,70,T-series): Single type (manufacturer standard or seamless only)

IMPORTANT: The exact transition size depends on the class. Use your ASME knowledge to determine the correct transition point. Generally:
  150# classes transition later (larger sizes still seamless)
  Higher ratings transition earlier (need welded sooner for larger pipe availability)

=== PIPE MOC RULES ===
CS (1-series, 2-series):
  Seamless: ASTM A 106 Gr. B
  Welded (A-series 150#, GALV): API 5L Gr. B
  Welded (B/D/E-series): ASTM A 671 - CC60 Class 22
  D1/E1 mid-range sizes (12-14"): ASTM A 333 Gr.6 (intermediate transition)
  F1/G1 (1500#/2500#): API 5L Gr, X60 PSL-2 (ALL sizes, single MOC)

LTCS (1L-series):
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

=== FITTINGS RULES ===
TYPE split mirrors pipe type: "Butt Weld (SCH to match pipe), Seamless" for small sizes, "Butt Weld (SCH to match pipe), Welded" for large sizes.
GALV screwed classes (3/4): Small sizes = "Screwed (SCRD), #3000", larger = "Butt Weld (SCH to match pipe), Seamless/Welded"

FITTINGS MOC BY MATERIAL:
  CS (A1/A1N/A2/A2N): ASTM A 234 Gr. WPB (same for ALL sizes — seamless AND welded)
  CS (B1/D1/E1 + N/2N variants): Seamless=ASTM A 234 Gr. WPB, Welded=ASTM A 420 Gr. WPL6
  CS (F1/G1 + N/2N): ASTM A 860 WPHY 60 (all sizes)
  LTCS (all 1L/2LN): ASTM A 420 Gr. WPL6 (all sizes)
  SS316L (10-series): ASTM A 403 Gr. WP 316L (all sizes)
  DSS (20-series): Seamless=ASTM A 815 Gr.WP-S UNS S31803, Welded=ASTM A 815 Gr.WP-WX UNS S31803
  SDSS (25-series): Seamless=ASTM A 815 Gr.WP-S UNS S32750, Welded=ASTM A 815 Gr.WP-WX UNS S32750
  GALV (3/4/5/6,B4,D4): Screwed=ASTM A 105N-Galvanized, BW=ASTM A 234 Gr. WPB, Seamless Galvanized

STANDARDS (apply to ALL material families unless noted):
  Elbow: ASME B 16.9 | Tee: ASME B 16.9 | Reducer: ASME B 16.9 | Cap: ASME B 16.9
  Plug: Hex Head Plug, ASME B 16.11 (or "Hex Head, ASME B 16.11")
  Weldolet: MSS SP 97, [flange MOC] (e.g., "MSS SP 97, ASTM A 105N" for CS)
  GALV screwed classes: Elbow/Tee/Red/Cap = ASME B 16.11

fittings_by_size: One entry per pipe size. Each entry includes size_inch, type (Seamless/Welded), fitting_type, material_spec, and all standards. material_spec may differ between seamless and welded sizes.

=== FLANGE RULES ===
MOC by material family:
  CS (A1-E1, N variants): ASTM A 105N
  CS (F1/G1, F2N/G2N): ASTM A 694 F60
  LTCS (all 1L/2LN): ASTM A 350 Gr. LF2
  SS316L (10-series): ASTM A 182 F 316L
  DSS (20-series): ASTM A 182 Gr. F51
  SDSS (25-series): ASTM A 182 Gr. F53
  GALV (3/4/5/6,B4,D4): MSS SP 97

FACE by rating:
  150#: "150# RF, Serrated Finish"
  300#: "300# RF, Serrated Finish"
  600#: "600# RF, Serrated Finish"
  900#: "1500#, RTJ" (note: 900# classes use 1500# RTJ face)
  1500#: "1500#, RTJ"
  2500#: "2500#, RTJ"

TYPE:
  Classes with sizes >24": "Weld Neck, ASME B 16.5/ 16.47A, Butt Welding ends as per ASME B 16.25"
  Classes with sizes ≤24": "Weld Neck, ASME B 16.5, Butt Welding ends as per ASME B 16.25"
  E/F series (900#/1500#): Add RTJ groove reference and Note 6,7 or 8,9
  G series (2500#): "Weld Neck, ASME B16.5, Butt welding ends as per ASME 16.25, RTJ, Note 8,9"

=== SPECTACLE BLIND ===
MOC: Same as flange MOC
Standard: "ASME B 16.48" (small/mid sizes)
Standard (large): "Spacer and blind as per ASME B 16.48 (Note 5)" (large sizes not covered by B16.48)
F/G series (1500#/2500#): Often EMPTY (no spectacle blind data)
GALV classes: "150# RF, Serrated Finish"

=== BOLTS / NUTS / GASKETS ===
STUD BOLTS by material family:
  CS (1/2-series, 150#-900#): ASTM A 193 Gr. B7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  LTCS + SS316L (1L/10-series, 150#-900#): ASTM A 320 Gr. L7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  DSS + SDSS (20/25-series): ASTM A 453 Gr. 660
  F/G series (1500#/2500#): ASME B 16.48
  GALV/T-series: empty

HEX NUTS:
  CS: ASTM A 194 Gr. 2HM, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  LTCS + SS316L: ASTM A 194 Gr. 7ML, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  DSS + SDSS: ASTM A 453 Gr. 660
  F/G series: empty

GASKETS:
  RF classes (150#-600#):
    CS/LTCS/SS: ASME B 16.20, 4.5mm, SS316/SS316L Spiral Wound with Flexible Graphite (F.G.) filler
    DSS: ASME B 16.20, 4.5mm, DSS UNS S31803 Spiral Wound with Flexible Graphite (F.G.) filler
    SDSS: ASME B 16.20, 4.5mm, DSS UNS S32750 Spiral Wound with Flexible Graphite (F.G.) filler
  RTJ classes (900#+):
    CS/LTCS: OCT Ring, Soft Iron, HDG, Max 90 BHN Hardness, ASME B16.20
    SS: OCT Ring, SS316L, Max 160 BHN Hardness, ASME B16.20
    DSS: OCT Ring, DSS UNS S31803, Max 22 HRC Hardness, ASME B16.20
    SDSS: OCT Ring, SDSS UNS S32750, Max 22 HRC Hardness, ASME B16.20

=== VALVE CODES ===
Pattern: [TYPE PREFIX][CLASS CODE][FACE SUFFIX]
  CLASS CODE = exact piping class name (A1, B1N, D20, E25N, etc.)
  FACE SUFFIX: R = RF (150#-600#) | J = RTJ (900#-2500#) | F = FF (GRE/CPVC)

Standard valve prefixes:
  Ball Reduced Trunnion: BLRT | Ball Full Trunnion: BLFT
  Ball Reduced Port: BLRP | Ball Full Port: BLFP (used in D-series and some E+ classes)
  Gate Y-body: GAYM | Globe Y-body: GLYM
  Check Piston: CHPM | Check Swing: CHSM | Check Dual-Plate: CHDM
  Butterfly Wafer: BFWT | Butterfly Triple-Offset: BFTP
  DSS/SDSS 25-series uses: BSR/BSF (ball), GAW (gate), GLS (globe), CSW/CDP (check)
  Double Block: DBFP/DBRP (used in E-series and tubing)

Special valve rules:
  E-series (900#) Ball: Small sizes → "USE GATE VALVE", larger sizes → actual ball codes (BLRP/BLFP)
  F/G-series (1500#/2500#) Globe: "USE GATE VALVE" for small sizes, BLRP/BLFP codes for larger
  F/G-series Gate: Show as rating text "1500#, RTJ" or "2500#, RTJ"
  F/G-series Butterfly: Uses GLYM prefix (globe-style in butterfly slot)

IMPORTANT — SIZE-SPECIFIC VALVE CODES:
Valve VDS codes are NOT uniform across all sizes. Different codes apply at different size ranges.
Example for class A1:
  - Check: 0.5"-3" → "CHPMA1R", 4"-24" → "CHSMA1R, CHDMA1R" (swing/dual-plate for larger sizes)
  - Butterfly: Only available for 6"+ → "BFWTA1R, BFTPA1R" (empty for smaller sizes)
  - Ball: 0.5"-2" → "BLRTA1R" (reduced trunnion), 2.5"-24" → "BLRTA1R, BLFTA1R" (both reduced + full)

You MUST provide valve codes using the *_by_size arrays to capture these size-specific differences.
Each entry is {{"size_inch": "...", "code": "..."}}. One entry per pipe size in the class.
If a valve type is not available at a given size, set code to "".
The class-level string fields (ball, gate, globe, check, butterfly) serve as fallback descriptions only.

Multiple valve types in one field → comma-separated: "BLRT{piping_class}R, BLFT{piping_class}R"

=== EXTRA FITTINGS ===
Coupling: "ASME B 16.11, sizes 0.5\" to 2.0\" only" — ONLY for 150#/300# classes
Union: "ASME B 16.11, sizes 0.5\" to 2.0\" only" — ONLY for 150# classes
Hex Plug: "ASME B 16.11, all sizes" — ALL classes
Olet: "MSS SP 97, [flange MOC], all sizes" — ALL classes
Swage: empty

=== MISC ===
Design Code: "ASME B 31.3" + ", NACE-MR-01-75/ISO-15156-1/2/3" if N suffix in class name
Pipe Code: ASME B 36.10M (CS/LTCS) or ASME B 36.19M (SS/DSS/SDSS)
Mill Tolerance: 12.5% (standard) or 0.125
Branch Chart: Ref. APPENDIX-1, Chart 1
Ends: "BE" (mainstream), special for GRE/CPVC/Tubing/CuNi/Copper

=== OUTPUT JSON SCHEMA ===
{{
    "design_code": "...",
    "pipe_code": "...",
    "mill_tolerance": "12.5%",
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
    "flange": {{"material_spec": "...", "face_type": "...", "flange_type": "...", "standard": "..."}},
    "spectacle_blind": {{"material_spec": "...", "standard": "...", "standard_large": "..."}},
    "bolts_nuts_gaskets": {{"stud_bolts": "...", "hex_nuts": "...", "gasket": "..."}},
    "valves": {{
        "rating": "...",
        "ball": "...", "gate": "...", "globe": "...", "check": "...", "butterfly": "...",
        "ball_by_size": [{{"size_inch": "0.5", "code": "BLRTA1R"}}, {{"size_inch": "2", "code": "BLRTA1R, BLFTA1R"}}, ...],
        "gate_by_size": [{{"size_inch": "0.5", "code": "GAYMA1R"}}, ...],
        "globe_by_size": [{{"size_inch": "0.5", "code": "GLYMA1R"}}, ...],
        "check_by_size": [{{"size_inch": "0.5", "code": "CHPMA1R"}}, {{"size_inch": "4", "code": "CHSMA1R, CHDMA1R"}}, ...],
        "butterfly_by_size": [{{"size_inch": "6", "code": "BFWTA1R, BFTPA1R"}}, ...]
    }},
    "notes": ["PMS to be read in conjunction with Project Piping Design Basis, and Valve Material Specification.", ...]
}}

CRITICAL:
1. Valve *_by_size arrays MUST have one entry per pipe size (matching pipe_data count). Use "" for sizes where valve type is not available.
2. The top-level valve string fields (ball, gate, etc.) are fallback descriptions — the *_by_size arrays hold the actual per-size codes.
3. fittings_by_size count MUST match pipe_data count.
4. fittings_welded MUST be populated (not null) if class has welded fittings.
5. Wall thickness must be EXACT ASME B36.10M / B36.19M values — do NOT estimate.
6. Return ONLY JSON. No markdown fences, no commentary.

Generate PMS for class **{piping_class}** now."""


async def generate_pms_with_ai(
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    rating: str,
    reference_entries: list[dict],
) -> dict | None:
    """Call Claude API to generate PMS data (everything except P-T).
    Returns a dict of generated fields, or None on failure."""

    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key configured, cannot generate PMS data")
        return None

    prompt = _build_generation_prompt(
        piping_class, material, corrosion_allowance, service,
        rating, reference_entries,
    )

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        message = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=16384,
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
        logger.error("AI returned invalid JSON for %s: %s", piping_class, e)
        return None
    except anthropic.APIError as e:
        logger.error("Anthropic API error for %s: %s", piping_class, e)
        return None
    except Exception as e:
        logger.error("Unexpected error in AI generation for %s: %s", piping_class, e)
        return None

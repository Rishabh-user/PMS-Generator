"""
Claude AI service for generating PMS data.

Responsibility split with the post-processor (`app/utils/pipe_data.py`):

  AI generates  — class naming, NPS size list, pipe MOC, pipe-type
                  transitions, fittings (MOC + standards), flange
                  (MOC + face + type), spectacle blind, bolts/nuts/
                  gaskets, valve VDS codes, design code, pipe code,
                  branch chart, ends, notes. For non-ASME pipe codes
                  (EEMUA 234, ASTM B42, manufacturer GRE, ASTM F 441,
                  ASTM A 269) the AI also emits the final OD / WT / SCH
                  values from authoritative tables baked into the prompt.

  Post-processor — for ASME-coded pipe classes (B36.10M / B36.19M only):
                  overwrites od_mm with `pipe_dimensions.json` values,
                  computes Eq. 3a minimum wall, looks up the project-
                  conventional schedule floor in `project_schedule_floors.json`,
                  and picks the smallest standard schedule meeting
                  MAX(Eq. 3a, floor) — replacing both `schedule` and
                  `wall_thickness_mm`. The AI's emitted values for these
                  three fields are discarded for ASME classes.

  Pressure-temperature curves come from `pt_by_class.json` (B16.5 + project
  caps), not from the AI.
"""
import json
import logging

import anthropic

from app.config import settings
from app.utils.engineering_constants import (
    AI_MAX_TOKENS,
    MILL_TOLERANCE_PERCENT,
)

logger = logging.getLogger(__name__)


class AIGenerationError(RuntimeError):
    """Raised when the Anthropic call fails. The message is user-safe and
    describes the actual failure mode (credits, rate limit, auth, etc.) so
    the frontend can show something useful instead of a generic 'check your
    API key'."""


def _format_rating_letters_inline() -> str:
    """Render the §5.5 rating list as a prompt-friendly pipe-separated
    line: 'A=150# | B=300# | D=600# | ...'. Sourced from
    `app/data/pressure_ratings.json` via `rating_lookup`, so the prompt
    line agrees with the dropdown options + every other consumer."""
    from app.services import rating_lookup
    return " | ".join(f"{letter}={label}" for letter, label in rating_lookup.all_pairs())


_RATING_LETTERS_LINE = _format_rating_letters_inline()
"""§5.5 rating-letter mapping built once at module load. Spliced into
SYSTEM_PROMPT's CLASS NAMING CONVENTION section so the prompt's rating
list never drifts from `pressure_ratings.json`."""

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

Do NOT generate P-T data, hydrotest_pressure, pipe_data, or
fittings_by_size (all built deterministically by the server from
class_metadata.json). Set pipe_data to [], fittings_by_size to [],
and hydrotest_pressure to "" — they will be overwritten before the
response is shipped.

=== CLASS NAMING CONVENTION (3-Part System per PMS Doc) ===
Format: [PART1][PART2][PART3]

PART 1 — RATING (letter):
  {_RATING_LETTERS_LINE}

PART 2 — MATERIAL (number) — verbatim from §5.5 of the project PMS doc
(40801-SPE-80000-PP-SP-0001 Rev A0, page 18). Keep this list in lock-step
with validation_service._MATERIAL_DIGIT and any reviewer shares of the §5.5
screenshot:
  1  = CS-3mm CA
  2  = CS-6mm CA (heavy wall)
  3  = CS GALV-3mm CA (screwed fittings)
  4  = CS GALV-1.5mm CA (screwed fittings)
  5  = CS GALV-6mm CA
  6  = CS Internally coated
  9  = SS316 (defined in the spec; no class currently uses this digit)
  10 = SS316L
  20 = Duplex SS (DSS) UNS S31803
  25 = Super Duplex SS (SDSS) UNS S32750
  30 = 90/10 CuNi (Copper-Nickel)
  40 = Copper
  50 = GRE (Glass Reinforced Epoxy)
  51 = GRV — BONSTRAND Series 5000C
  52 = GRE (for special service)
  60 = CPVC
  70 = Titanium
  80 = SS316L/SS316 Tubing
  90 = 6 Mo Tubing

PART 3 — IDENTIFIER (optional suffix):
  N = NACE (sour service, adds NACE-MR-01-75/ISO-15156 to design code)
  L = Low Temperature variant
  LN = Low Temp + NACE combined
  A = 125 Barg Pressure (tubing)
  B = 200 Barg Pressure (tubing)
  C = 325 Barg Pressure (tubing)

Examples:
  A1   = 150# CS 3mm CA
  B1N  = 300# CS 3mm CA NACE
  A2LN = 150# CS 6mm CA LTCS+NACE
  A40  = 150# Copper
  A50  = 150# GRE
  A60  = 150# CPVC
  A70  = 150# Titanium
  T80A = SS316L Tubing, 125 Barg
  T90C = 6 Mo Tubing, 325 Barg
  J1   = 5000# CS 3mm CA  (rating reserved for HP service; no class currently catalogued)
  K1   = 10000# CS 3mm CA (rating reserved for HP service; no class currently catalogued)

=== PIPE SIZES — STANDARD NPS RANGES ===
Generate ALL standard NPS sizes for the class. Typical ranges:
  A-series 150# CS (1/1N): 0.5" to 36" (22 sizes: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36)
  A-series 150# LTCS (1L/1LN): 0.5" to 36" (22 sizes — extended for project-wide NPS 36 support)
  A-series 150# SS/DSS/SDSS (10/20/25): 0.5" to 36" (22 sizes — extended for project-wide NPS 36 support)
  A-series 150# 2-series (A2/A2N): 0.5" to 36" (22 sizes — extended for project-wide NPS 36 support)
  B-series 300# (all materials): 0.5" to 36" (22 sizes — extended for project-wide NPS 36 support)
  D-series 600# (all materials): 0.5" to 36" (22 sizes — extended for project-wide NPS 36 support)
  E-series 900#: 0.5" to 24" (17 sizes) — 2N/2LN start at 1" (15 sizes). NPS 26"+ NOT emitted at 900# — wall would exceed buildable plate thickness.
  F-series 1500#: 0.5" to 24" (17 sizes) — 2N/2LN start at 1" (15 sizes). NPS 26"+ NOT emitted at 1500#.
  G-series 2500#: 0.5" to 24" (17 sizes) — G10: to 12" (11), G20: to 18" (14). NPS 26"+ NOT emitted at 2500#.
  J-series 5000#  / K-series 10000# (high-pressure ratings reserved in §5.5):
    No J* / K* class exists in the current catalogue. If the user requests
    one, follow F-series sizing (0.5"-24", 17 sizes) as the safe default
    and explicitly note the assumption in the response.
  GALV / Epoxy (A3/A4/B4/D4/A5/A6): 0.5" to 36" (22 sizes — galv/epoxy ≤ 600# extended for project-wide NPS 36 support)

=== ADDITIONAL SIZE-RANGE NOTES ===
NPS 36: supported for ratings ≤ 600# (A/B/D series). NOT in range for E/F/G
(≥ 900#) — heavy-wall large-bore pipe at high rating is rarely buildable.
  CuNi (A30): 0.5" to 28" (17 sizes: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28 — per EEMUA 234. No 2.5", no 22", no 30")
  Copper (A40): 0.5" to 4" ONLY (7 sizes: 0.5, 0.75, 1, 1.5, 2, 3, 4) — do NOT emit 6"+
  Titanium (A70): 0.5" to 6" ONLY (8 sizes: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6) — do NOT emit 8"+
  GRE (A50/A51/A52):
    A50/A52: 20 sizes 1"-40" (1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28, 30, 32, 34, 36, 40)
    A51: 6 sizes 1"-6" only (1, 1.5, 2, 3, 4, 6) — BONSTRAND Series 50000C range
  CPVC (A60): 0.5" to 8" (10 sizes: 0.5, 0.75, 1, 1.5, 2, 2.5, 3, 4, 6, 8)
  Tubing (T80/T90): Short size range per rating — T*A = 0.5"-1.5", T*B/C similar

Standard NPS sequence: 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36

=== PIPE SCHEDULE & WALL THICKNESS — SERVER-BUILT ===
Schedule, wall_thickness_mm, and od_mm are NOT generated by you. The
server post-processor (`app/utils/pipe_data.py::correct_pipe_data`) fills
them via dual-case ASME B31.3 §304.1.2 Eq. 3a:
  1. od_mm comes from pipe_dimensions.json (ASME B36.10M / B36.19M).
  2. t_press = MAX(Eq. 3a Case 1: Min T at design pressure;
                    Eq. 3a Case 2: Max P at design temperature)
  3. t_min = (t_press + corrosion_allowance) / (1 - mill_tolerance)
  4. Schedule is the smallest standard meeting MAX(t_min, project floor).

Non-ASME pipe codes (CuNi A30 / Copper A40 / GRE A50/A51/A52 / CPVC A60
/ Titanium A70 / Tubing T80/T90) take their OD/WT directly from
class_metadata.json explicit_dimensions or the dedicated tubing builder
— again, you do not generate these.

Emit `pipe_data: []` — the server replaces it entirely from
class_metadata.json + the post-processor.

=== PIPE TYPE / MOC / TRANSITION — SERVER-BUILT, DO NOT GENERATE ===
pipe_data is built deterministically by the server from
class_metadata.json. For every NPS in the class's size list the server
populates pipe_type, material_spec, ends (and id_mm / od_mm / WT for
non-ASME classes) from the metadata's small_bore / large_bore blocks
and the per-class transition_nps. Anything you put in pipe_data will be
replaced; emit `pipe_data: []` and let the server build it.

OD/WT/Schedule for ASME-coded classes are then computed by the
post-processor (dual-case Eq. 3a) — see the PIPE SCHEDULE & WALL
THICKNESS section above for the contract.

=== FITTINGS RULES — SERVER-BUILT, DO NOT GENERATE ===
fittings_by_size is built deterministically by the server from
class_metadata.json (fitting_groups + fitting_standards). Emit
`fittings_by_size: []` — anything you put there will be replaced.

=== FLANGE RULES ===
MOC by material family:
  CS (A1-E1, N variants):  ASTM A 105N
  CS (F1/G1, F2N/G2N):     ASTM A 105N
    (1500#/2500# CS forgings per ASME B16.5 Table 1A Group 1.1 are A105N
    — same as the lower ratings. ASTM A 694 F60 is a B16.47 pipeline
    flange material and only appears in the hub_connector row below,
    NOT as the main flange MOC. Earlier project sheets showed A 694 F60
    here; that was a B16.47/B16.5 mix-up — A 105N is the correct
    Table 1A entry for sizes ≤24".)
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
  900# (E-series): "900#, RTJ" for all sizes per ASME B16.5
    (Some operator specs upsize small-bore 900# flanges to 1500# RTJ
     for handling robustness — that is a project-specific overlay,
     not a B16.5 requirement. Default to the rated 900# unless the
     project specification explicitly calls for the upsize.)
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

STANDARD field (`flange.standard`) — the dimensional reference cited on the
sheet's "Standard" row.  ASME B16.5 covers NPS ½ through NPS 24 only;
classes that emit pipe sizes ≥26" need a dual citation so the printed
reference is correct across the whole size range:

  Single citation — class tops out at ≤24" (most classes):
    standard = "ASME B16.5"

  Dual citation — class spans sizes ≥26" (B16.47 takes over above 24"):
    standard = "ASME B16.5 / B16.47A"
    Affected classes (per the SIZE RANGES table above):
      • A1, A1N        (CS 150#, to 36")
      • A1L, A1LN      (LTCS 150#, to 30")
      • A2N, A2LN      (CS NACE 150#, to 30")
      • A20, A20N      (DSS 150#, to 32")
      • B20            (DSS 300#, to 32")
      • B25            (SDSS 300#, to 32")
      • A30            (CuNi EEMUA 234, to 28")
    A50 / A52 (GRE) use the existing GRE-specific string
    "Drilled to ASME B 16.5/ 16.47A, 150#" — do not change.
    A51 (GRE BONSTRAND, 1"-6" only) stays "Drilled to ASME B 16.5, 150#".

  All other classes (B-series 300#, D-series 600#, E/F/G-series 900-2500#,
  10-series SS, A40 Copper, A60 CPVC, A70 Titanium, GALV) top out at
  ≤24" and use the single-citation form.

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
  DSS + SDSS (20/25-series): ASTM A 193 Gr. B8M Cl. 2
    (industry-standard Mo-bearing austenitic stainless bolting for
     duplex/super-duplex flanged joints. ASTM A 453 Gr. 660 is a
     high-temperature precipitation-hardened alloy used only on
     specialty/hot service — not the general DSS bolting default.
     Verify against project specification if hot service applies.)
  CuNi (30): ASTM A 193 Gr. B7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  GALV classes: ASTM A 193 Gr. B7M, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm

HEX NUTS:
  CS: ASTM A 194 Gr. 2HM, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  LTCS + SS316L: ASTM A 194 Gr. 7ML, XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50μm
  DSS + SDSS: ASTM A 194 Gr. 8M (industry-standard nut to pair with A 193 B8M Cl. 2 stud bolts)
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

=== VALVE SELECTION LOGIC (STANDARDIZED PER VALVE TYPE) ===
Use this matrix to populate ball_by_size, gate_by_size, globe_by_size,
check_by_size, butterfly_by_size, dbb_by_size, dbb_inst_by_size. Empty
string ("") means the valve type is not applicable at that size / class /
service. The rules below are the project default — class-specific
overrides in "Special valve rules" further down take precedence.

1. BALL (BL) — primary on/off valve
   Governing std: ASME B16.34 (P-T), API 6D / ISO 17292 (≤24" ≤600#),
                  API 6D (>600#); NACE MR-01-75 in sour service.
   Size availability: ALL pipe sizes the class supports (0.5"-24").
   Bore selection by size (boundary at 2"):
     0.5"-2"   → Reduced bore only      (BLRT/BLRP/BLRM)
     2.5"-24"  → Reduced AND Full bore  ("BLRT…, BLFT…" comma-joined)
   Seat selection by class rating:
     150#-600#  → PTFE (T)              codes BLRT / BLFT
     900#-1500# → PEEK (P)              codes BLRP / BLFP
     2500#      → PEEK + Metal (P + M)  codes BLRP, BLFP, BLFM, BLRM
   Full-bore mandatory for: piggable lines, PSV inlet/outlet, sample lines.
   Soft-seat (PTFE/PEEK) max temperature 250°C — beyond, switch to Metal.
   Construction (cite in design_code / notes when relevant):
     ≤ 2"  → Floating ball (single-seat carries pressure, simpler)
     ≥ 2.5" → Trunnion-mounted (dual seats, bearing-supported stem,
              MANDATORY for DBB and 900#+ class sizes)
     Metal-seat: tungsten-carbide coated, min 1050 Vickers, 150-250 µm.
     Anti-static device + fire-safe (API 6FA / API 607) required for HC.
     Stem: blow-out proof per API 6D §4.13.

2. GATE (GA) — clean service / large-bore on/off / small-bore high-pressure
   Governing std: API 600 (≥2" cast), API 602 (≤4" forged),
                  API 603 (CRA materials).
   Size availability:
     Clean / utility / steam / water / non-HC: ALL sizes (0.5"-24")
     Hydrocarbon service (900#+ classes E/F/G): ≤ 1.5" ONLY (small-bore)
     Hydrocarbon service (150#-600# classes A/B/D): permitted, typically
       used as alternative to ball for clean HC duty.
   Wedge type by size:
     0.5"-1.5" → Solid wedge (forged, API 602)
     2"-24"    → Flexible wedge (cast, API 600)
   Seat: ALWAYS Metal (M) — no soft-seat gates in industrial practice.
   Code: GAYM (Y-pattern, Metal).
   Construction (cite in design_code / notes when relevant):
     Bonnet design by class:
       150#-600#  → Bolted bonnet (API 600)
       900#-2500# → Pressure-sealed bonnet (PSB) per API 6D, OR
                    Welded bonnet for 1500#+ HC service
     Stem operation:
       ≤ 1.5" forged (API 602) → Inside Screw, Non-Rising Stem (ISRS / NRS)
       ≥ 2"   cast   (API 600) → Outside Screw and Yoke (OS&Y), rising stem
     Stem packing: live-loaded for HC / sour / fugitive-emission service
                   (per ISO 15848, API 622 low-emission test).
     Trim hardness: NACE 22 HRC limit applies to wedge + body seat ring.

3. GLOBE (GL) — throttling / flow regulation
   Governing std: API 602 (≤4"), BS EN ISO 15761, BS 1873 (≥2").
   Size availability: 0.5"-8" typically. Larger globes are rare; emit ""
                      for sizes ≥ 10" unless project spec says otherwise.
   Seat: ALWAYS Metal (M).
   Code: GLYM (Y-pattern, Metal).
   Use globe ONLY where throttling is required; for on/off use ball/gate.
   Construction (cite in design_code / notes when relevant):
     Trim type:
       Plug-type        → general throttling (default)
       Cage-trim        → high ΔP / cavitation-prone service
       Needle-trim      → fine flow control (≤ 1")
     Trim facing: Stellite-faced (CoCr-A) for HC / sour / steam service.
     Body pattern: Y-pattern (GLY) standard; Angle (GLA) where the
                   line geometry dictates change-of-direction throttling.
     Stem: rising, OS&Y for ≥ 2"; live-loaded packing for fugitive-emission
           compliance (API 622 / ISO 15848-1).
     Bonnet: bolted standard; pressure-sealed for 900#+ rated globes.

4. CHECK (CH) — backflow prevention (mandatory wherever flow may reverse)
   Governing std: API 594 (wafer/lug), API 6D (pipeline),
                  BS 1868 (swing), BS 5352 (small-bore), BS EN ISO 15761.
   Size availability: ALL pipe sizes (0.5"-24").
   Type selection by size (boundary at 3"-4"):
     0.5"-3"   → Piston (CHPM)              — forged, small-bore
     4"-24"    → Swing AND Dual-plate       ("CHSM…, CHDM…" comma-joined)
                 Swing = default for general service
                 Dual-plate = where short face-to-face needed
   ### NACE / sour-service guidance ###
   For classes with N or LN suffix (e.g. A1N, B1N, D1N, E1N, F1N, F2LN,
   G1N, G2LN, G10N, G20N, G25N) at sizes ≥ 4", swing check is the
   typical preferred selection — dual-plate hinge-pin fatigue and
   sulfide deposit risk make it less common in continuous H₂S service
   per several major operator overlays. Many project specifications
   therefore drop dual-plate on N/LN classes; default to Swing only
   unless the project specification explicitly permits dual-plate.
   Small-bore Piston (CHPM) at ≤ 3" is generally accepted — forged
   construction is sour-tolerant.
   Wafer (CHWM): generally avoided in flammable/sour service due to
                 limited fire-safe certification on wafer bodies; use
                 only where space constraints rule out swing/dual-plate
                 and the project spec permits.
   Seat: ALWAYS Metal (M).
   Construction (cite in design_code / notes when relevant):
     Installation orientation:
       Swing (CHSM)        → HORIZONTAL flow only — disc swings on hinge
       Piston (CHPM)       → either orientation (vertical or horizontal)
       Dual-plate (CHDM)   → either orientation; spring-assisted plates
       Wafer (CHWM)        → either orientation; short face-to-face
     Spring-assisted swing: required for low-flow / low-ΔP service to
                            ensure positive seating against pulsating flow.
     Bolted cover; soft-seal eliminator on hinge pin where sour service
     applies (NACE-classes use Inconel hinge-pin coating).

5. BUTTERFLY (BF) — large-bore on/off, low-cost
   Governing std: API 609 (concentric/double-offset/triple-offset),
                  ISO 5752, BS 5155, API 6FA (fire-safe).
   Size availability: ≥ 3" minimum, typically ≥ 6". Emit "" for sizes
                      0.5"-2.5". Class-by-class boundary may move higher
                      per project spec — verify in pipe_classes data.

   ### High-pressure classes (900#+) — typical-not-used guidance ###
   For classes starting with E, F, or G (900#, 1500#, 2500# ratings):
   triple-offset butterflies are rated to 1500# under API 609, but in
   most oil & gas process piping the trunnion-mounted ball is the
   industry-default isolation valve at these ratings. Many operator
   specifications (Shell DEP, ExxonMobil GP, Aramco SAES, ADNOC etc.)
   exclude butterfly from 900#+ HC service. Default to emitting "" for
   butterfly_by_size on E/F/G classes unless the project specification
   explicitly permits triple-offset at the rating in question.

   ### NACE / sour-service classes — typical-not-used guidance ###
   For classes ending in "N" or "LN", butterfly is generally avoided
   in continuous sour HC service due to sulfide-stress-cracking risk
   on the disc/seat/stem trim. Most operator specs (Shell DEP, ExxonMobil
   GP, Aramco SAES, ADNOC) exclude butterfly here; default to "" for
   butterfly_by_size on N/LN classes unless the project specification
   explicitly permits an SSC-qualified butterfly. The empty-row helper
   in the Excel writer hides the Butterfly row automatically.

   ### Net butterfly availability (typical) ###
   After the guidance above, butterfly is typically used on classes
   starting with A, B, or D (150#-600#) that do NOT end in N/LN. That
   covers: A1, A2, A10, A20, A25, A30 (CuNi), A50/A51/A52 (GRE), A60
   (CPVC), B1, B2, B10, B20, B25, D1, D2, D10, D20, D25, plus the GALV
   variants (A3/A4/A5/A6/B4/D4) and Epoxy-lined (A6).

   ### CRITICAL — service-based exclusion (read carefully) ###
   The Service Description string is a comma-separated list of SERVICE
   TOKENS (e.g. "Cooling Media, Diesel, Steam"). Scan EVERY token. If
   ANY single token is flammable / combustible / hydrocarbon — including
   but not limited to: hydrocarbon, HC, oil, diesel, gas, fuel, condensate,
   crude, naphtha, gasoline, hydraulic oil, lubricating oil, fuel gas,
   methanol, glycol (when carrying HC), sour, H2S, NACE — then WAFER
   butterfly (BFWT / BFW*) is FORBIDDEN for the entire class. It does not
   matter that other tokens in the same service string are water-like
   (Cooling Media, Steam, Fresh Water). API 6FA fire-safe testing requires
   the wafer body's lack of flange-bolt protection to be ruled out
   altogether. ONE flammable token in the list = wafer NEVER allowed.

   Type selection rules (apply in order):
     a) Service contains ANY flammable token (per the list above):
          → emit Triple-Offset only — code "BFTPCLASS_R" (PEEK) or
            "BFTTCLASS_R" (PTFE, permitted in HM service per project
            Note 9 on RF classes). Do NOT include BFWT.
     b) Service is purely water / utility / non-flammable
        (e.g. only Cooling Media, Steam, Fresh Water, Fire Water,
         Potable Water, Raw Sea Water, Cooling Water / Seawater):
          → emit Wafer + Triple-Offset — "BFWTCLASS_R, BFTPCLASS_R"
     c) Service is empty / "General":
          → conservative default: Triple-Offset only.

   Class limits: Wafer common 150#-300#; Triple-offset OK to 600#+;
                 ≥ 900# only triple-offset, and verify against API 609.

6. DBB — Double Block & Bleed (positive isolation)
   Governing std: API 6D §3, ISO 14313, operator overlays (Shell DEP,
                  ExxonMobil GP, Aramco SAES). NACE for sour.
   When required:
     900#+ classes (E/F/G series): MANDATORY — populate dbb_by_size +
                                   dbb_inst_by_size for ALL sizes.
     150#-600# classes (A/B/D series): NOT used — emit "" for both
                                       dbb_by_size and dbb_inst_by_size
                                       (single ball valve is sufficient
                                       for low-pressure isolation).
   Size availability (within applicable classes): ALL sizes 0.5"-24".
   Bore: Reduced (R) only — DBB is isolation, not flow.
   Seat selection by class:
     900#-1500# (E/F): PEEK (P)              code DBRP
     2500# (G):        PEEK + Metal (P + M)  codes "DBRP…, DBRM…"
   DBB Instrument variant (dbb_inst_by_size):
     Soft-seat (PEEK) ONLY — append T to RTJ end suffix → e.g. DBRPE20NJT.
     NEVER emit metal-seat instrument variant (no DBRM…JT).
   Construction (cite in design_code / notes when relevant):
     Body: ALWAYS Trunnion-mounted (single body holding two ball valves
           with a bleed valve between — never floating).
     Seats: Spring-loaded, self-relieving — auto-vents body cavity if
            pressure exceeds set point (API 6D §4.13 / ISO 14313).
     Bleed valve: needle valve, ¼" or ½" NPT female, isolated by upstream
                  seat first then drained through downstream seat.
     Stem: blow-out proof; anti-static device on each stem.
     Fire-safe: API 6FA / API 607 certified for HC service.
     For instrument variant (JT suffix): bleed connection ½" NPT female
     for tubing tee-in — soft-seat (PEEK) only, never metal-seat.

=== UNIVERSAL NACE / SOUR-SERVICE COMPLIANCE ===
For ANY class with N or LN suffix in its code (A1N, A1LN, B1N, D1N, E1N,
F1N, F2LN, G1N, G2LN, G10N, G20N, G25N, etc.), every valve emitted MUST
use materials and trim hardness compliant with NACE MR-01-75 / ISO 15156:
  • Carbon steel (CS) trim:           ≤ 22 HRC max hardness
  • Stainless steel SS316L:           ≤ 22 HRC; full-anneal condition
  • Duplex SS (DSS UNS S31803):       ≤ 28 HRC; ferrite 35-65%
  • Super-duplex SS (SDSS UNS S32750): ≤ 32 HRC; PREN ≥ 40
  • CuNi (UNS C70600 / C71500):       no specific HRC; alloy-grade compliant
  • Titanium B861 Gr.2:               sour-tolerant by alloy chemistry
The VDS letter codes do not change — compliance is enforced at the
material spec level during procurement. Mention this in the design_code
field for NACE classes and in the relevant note positions.

Cross-cutting valve restrictions already encoded above:
  • Butterfly: HARD BAN on (a) ALL 900#+ classes (E/F/G series, any
    suffix), AND (b) ALL NACE classes (any rating, suffix N/LN).
    Net effect: butterfly permitted ONLY on A/B/D classes without
    N/LN suffix.
  • Check valve dual-plate: BAN at sizes ≥ 4" on all NACE classes.
  • Wafer-type valves (any kind): NEVER on NACE classes.
  • DBB metal-seat instrument variant: NEVER (soft-seat PEEK only).

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
    "pipe_data": [],
    // pipe_data is built server-side from app/data/class_metadata.json +
    // dual-case Eq. 3a. Just emit [] — anything you put here is discarded.
    "fittings": {{"fitting_type": "...", "material_spec": "...",
                  "elbow_standard": "...", "tee_standard": "...", "reducer_standard": "...",
                  "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "...",
                  "rating": ""}},
    // fittings.rating is optional (only GRE A50/A52 use it — e.g. "20 bar, 93degC").
    // Most classes should emit rating as empty string.
    "fittings_welded": {{"fitting_type": "...", "material_spec": "...",
                  "elbow_standard": "...", "tee_standard": "...", "reducer_standard": "...",
                  "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "..."}},
    "fittings_by_size": [],
    // fittings_by_size is built server-side from class_metadata.json's
    // fitting_groups + fitting_standards. Emit [] — anything you put
    // here will be replaced.
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
1. Valve *_by_size arrays MUST have one entry per NPS in the class's size list (see PIPE SIZES section). Use "" for sizes where the valve type is not available.
2. The top-level valve string fields (ball, gate, dbb, dbb_inst, etc.) are fallback descriptions — the *_by_size arrays hold the actual per-size codes.
3. For 900#+ classes (E/F/G-series), include dbb and dbb_inst fields with DBRP prefix codes. dbb_inst code = dbb code + "T" suffix. Omit dbb/dbb_inst for 150#-600# classes.
4. pipe_data and fittings_by_size are server-built. Emit `[]` for both — anything you put there will be replaced. fittings_welded MUST be populated (not null) if class has welded fittings.
5. (server-built fields — see #4)
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

"""
Claude AI service for generating PMS data.
Generates all PMS fields EXCEPT pressure-temperature data (which comes from JSON).
Uses reference data from existing pipe classes for context and consistency.
"""
import json
import logging

import anthropic

from app.config import settings
from app.models.pms_models import PMSResponse, PressureTemperature

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior piping materials engineer with expertise in ASME standards (B31.3, B36.10M, B36.19M, B16.5, B16.9, B16.11, B16.20, B16.48), ASTM materials, and NACE requirements.

CRITICAL RULES FOR PMS GENERATION:
1. Always use seamless PIPE + welded FITTINGS (no pipe type variations based on size)
2. Generate fittings MOC based on EXACT material specifications (see MOC rules below)
3. Valves use direct class code in valve name: CLASS from input becomes valve code CLASS
4. Flanges: Use RF (Raised Face) for 150#-600#, RTJ (Ring Type Joint) for 900#+
5. No screwed/threaded connections except in 150# classes (coupling, union, hex plug)
6. Union ONLY in 150#; exclude from 600#+ (thread stress limits)
7. Include: hex plug (all sizes), weldolet (MSS SP 97), cap (ASME B16.9)
8. Coupling ONLY in 150#/300# sizes 0.5-2"; exclude 600#+
9. Apply NACE requirements (MR-01-75/ISO 15156-1/2/3) if "NACE" in material
10. Corrosion allowance: CS/LTCS = 3mm (or 6mm if "High-Temp"), SS/Duplex = 0mm

Return ONLY valid JSON matching the schema. No markdown, no explanation, no extra structures, no descriptions or comments in data fields."""


def _build_generation_prompt(
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    rating: str,
    reference_entries: list[dict],
) -> str:
    """Build the prompt for Claude to generate all PMS data except P-T, using rules-based conventions."""

    return f"""Generate a complete Piping Material Specification (PMS) for:
- Piping Class: {piping_class}
- Rating: {rating}
- Material: {material}
- Corrosion Allowance: {corrosion_allowance}
- Service: {service}

Do NOT generate Pressure-Temperature data (already provided separately).

CRITICAL RULES - Apply these conventions EXACTLY based on material family and pressure rating:

=== SCHEDULE (ASME B 36.10M / B 36.19M) - EXACT RULES ===
Carbon Steel (A1-G1 series):
  A1 (150#): 0.5-1.5"→SCH 160 | 2-6"→SCH 80 | 8-18"→STD | 20-22"→STD | 24-30"→XS | 32-36"→XS
  B1 (300#): 0.5-1.5"→SCH 160 | 2-6"→SCH 80 | 8-18"→SCH 40 | 20"+→SCH 40
  D1 (600#): 0.5-2"→SCH 160 | 3-6"→SCH 80 | 8-18"→SCH 80 | 20"+→SCH 80
  E1 (900#): 0.5-2"→SCH 160 | 3-6"→SCH 120 | 8-18"→SCH 120 | 20"+→SCH 120
  F1 (1500#): 0.5-1.5"→XXS | 2-3"→SCH 160 | 4-6"→SCH 160 | 8-16"→SCH 140/160 | 18"+→Varies
  G1 (2500#): 0.5-1.5"→XXS | 2-6"→SCH 160 | 8"+→Varies per ASME B36.10M

Stainless Steel 316L (A10-G10): Use "S" suffix (80S/60S/40S/10S by size, RF flanges, A182 F316L MOC)
Duplex S31803 (A20-G20): Use "S" suffix (80S-40S, very light schedule due to high strength)
Super Duplex S32750 (A25-G25): Exclusive 80S (highest strength, lightest schedule)

=== PIPE SIZES (ALL STANDARD NPS) ===
MANDATORY: Generate ALL standard NPS sizes from 0.5" to 36":
0.5", 0.75", 1.0", 1.5", 2.0", 3.0", 4.0", 6.0", 8.0", 10.0", 12.0", 14.0", 16.0", 18.0", 20.0", 22.0", 24.0", 26.0", 28.0", 30.0", 32.0", 36.0"

**CRITICAL - EXACT AUTHORITATIVE ASME B36.10M WALL THICKNESS VALUES:**
The values below are EXACT and AUTHORITATIVE. Use them EXACTLY as shown.
Do NOT calculate, estimate, derive, or invent different values.
If a size/schedule combination is listed, use the EXACT value shown.

A1 (150#) CARBON STEEL - MANDATORY EXACT VALUES:
  SCH 160: 0.5"=7.47 | 0.75"=7.62 | 1.0"=8.74 | 1.5"=9.65
  SCH 80:  2.0"=5.54 | 3.0"=7.62 | 4.0"=8.56 | 6.0"=10.97
  STD:     8.0"=8.74 | 10.0"=9.27 | 12.0"=9.53 | 14.0"=9.53 | 16.0"=9.53 | 18.0"=9.53 | 20.0"=9.53 | 22.0"=9.53
  XS:      24.0"=10.31 | 26.0"=10.31 | 28.0"=10.31 | 30.0"=10.31 | 32.0"=10.31 | 36.0"=10.31 (all in mm)

For other ratings/materials not listed:
  Consult official ASME B36.10M or B36.19M tables
  Use published standard values ONLY - never estimate or calculate
  Do NOT guess or invent wall thickness values

=== PIPE TYPE ===
IMPORTANT: Pipe is ALWAYS seamless. Fittings are ALWAYS welded.
Pipe Type field: Use "Seamless" for all pipe data.
Fittings Type: Butt Weld (SCH to match pipe), Seamless [for small/mid] or Welded [for large]

=== PIPE & FITTINGS MOC ===
CARBON STEEL (A1-G1, except F1):
  Pipe Seamless: ASTM A 106 Gr. B
  Fittings: ASTM A 234 Gr. WPB
  Flanges/Caps/Plugs: ASTM A 105N
  Weldolet: MSS SP 97 + A 105N body

CARBON STEEL F1 (1500#) — HIGH-YIELD:
  Pipe Seamless: ASTM A 106 Gr. B
  Fittings: ASTM A 234 Gr. WPB
  Flanges/Caps/Plugs/Weldolet: ASTM A 694 F60 (all components)

STAINLESS STEEL 316L (A10-G10):
  Pipe Seamless: ASTM A 312 TP 316L
  Fittings: ASTM A 403 Gr. WP 316L
  Flanges/Caps/Plugs/Weldolet: ASTM A 182 F 316L

DUPLEX S31803 (A20-G20):
  Pipe Seamless: ASTM A 790 Gr. S31803
  Fittings: ASTM A 815 Gr. WP-S UNS S31803
  Flanges/Caps/Plugs/Weldolet: ASTM A 182 Gr. F51

SUPER DUPLEX S32750 (A25-G25):
  Pipe/Fittings/Flanges/All: ASTM A 182 Gr. F53

=== FITTINGS (SIZE-BASED VARIATION) ===
**CRITICAL:** Fittings vary by SIZE THRESHOLD, NOT globally:
- SEAMLESS FITTINGS: Sizes ≤ 2.0" (0.5", 0.75", 1.0", 1.5", 2.0")
- WELDED FITTINGS: Sizes > 2.0" (3.0", 4.0", 6.0", 8.0", 10.0", 12.0", 14.0", 16.0", 18.0", 20.0", 22.0", 24.0", 26.0", 28.0", 30.0", 32.0", 36.0")

SEAMLESS COLUMN (≤2"):
- TYPE: "Butt Weld (SCH to match pipe), Seamless"
- MOC: (CS: ASTM A 234 Gr. WPB | SS316L: ASTM A 403 WP316L | Duplex: ASTM A 815 WP-S S31803)
- Elbow/Tee/Reducer/Cap: (CS: ASME B 16.9 | SS: ASME B 16.9)
- Plug: (CS: ASME B 16.11 Hex Head | SS: ASME B 16.11 Hex Head)
- Weldolet: (CS: MSS SP 97 + A 105N | SS: MSS SP 97 + A 182)

WELDED COLUMN (>2"):
- TYPE: "Butt Weld (SCH to match pipe), Welded"
- MOC: Same as Seamless (MOC doesn't change, only production method)
- Elbow/Tee/Reducer/Cap: Same standards (ASME B 16.9)
- Plug: Same spec (ASME B 16.11 Hex Head)
- Weldolet: Same spec (MSS SP 97 + appropriate MOC)

FITTINGS_BY_SIZE ARRAY - PER-SIZE BREAKDOWN:
Generate fittings_by_size as an array with 22 entries (one per size).
Each entry must include:
- size_inch: "0.5", "0.75", ..., "36.0" (matching pipe_data sizes)
- type: "Seamless" for sizes 0.5-2.0", "Welded" for sizes 3.0-36.0"
- fitting_type, material_spec, elbow_standard, tee_standard, reducer_standard, cap_standard, plug_standard, weldolet_spec
All specs are IDENTICAL within each type category (seamless or welded)

Standard Fittings by Rating:
  150#: Include Coupling (0.5-2" only), Union (0.5-2" only), all others at all sizes
  300#: Include Coupling (0.5-2" only), NO Union, all others at all sizes
  600#+: NO Coupling, NO Union, all others at all sizes

=== FLANGE (ASME B 16.5, Weld Neck) ===
FACE TYPE by Rating:
  150#-600#: "RF, Serrated Finish" (Raised Face, serrated)
  900#+: "RTJ" (Ring Type Joint groove, metal-to-metal sealing)

MOC:
  CS (A1-E1): ASTM A 105N
  CS F1 (1500#): ASTM A 694 F60
  CS G1 (2500#): ASTM A 105N
  SS316L (A10-G10): ASTM A 182 F 316L
  Duplex (A20-G20): ASTM A 182 Gr. F51
  Super Duplex (A25-G25): ASTM A 182 Gr. F53

TYPE: "Weld Neck, ASME B 16.5" (all cases)
STANDARD: "ASME B 16.5" (all cases)

=== VALVE CODES (PREFIX + SUBTYPE + CLASS_CODE + FACE_SUFFIX) ===
PATTERN: Ball→BLRT/BLFT | Gate→GAYM | Globe→GLYM | Check→CHPM/CHSM/CHDM | Butterfly→BFWT/BFTP

Ball (Trunnion): BLRT + CLASS + FACE → BLRTA1R, BLRTB1R, BLRTD1R, BLRTE1R, BLRTF1R, BLRTA10R, BLRTA20R, BLRTA25R
Ball (Floating): BLFT + CLASS + FACE → BLFTA1R, BLFTD1R, BLFTA20R
Gate (Y-body): GAYM + CLASS + FACE → GAYMA1R, GAYMB1R, GAYMD1R, GAYMA10R, GAYMA20R
Globe (Y-body): GLYM + CLASS + FACE → GLYMA1R, GLYMB1R, GLYMD1R
Check (Poppet): CHPM + CLASS + FACE → CHPMA1R, CHPMD1R, CHPME1R
Check (Swing): CHSM + CLASS + FACE → CHSMA1R, CHSMD1R
Butterfly (Wafer): BFWT + CLASS + FACE → BFWTA1R, BFWTA10R, BFWTA20R
Butterfly (Lug): BFTP + CLASS + FACE → BFTPA1R, BFTPA20R

FACE_SUFFIX: R (RF for 150#-600#), T (RTJ for 900#+) — BUT this Excel file shows ALL use "R"
CLASS_CODE: Use EXACT piping class from input (A1, B1, D1, E1, F1, A10, B10, D10, A20, A25, etc.)

**CRITICAL - VALVE CODE FORMAT:**
- ONLY output the valve CODE STRING (e.g., "GAYMA1R")
- DO NOT output descriptions, comments, or dict objects
- DO NOT create objects with 'code' and 'desc' keys
- DO NOT add markdown formatting or explanations
- ALL valve fields (ball, gate, globe, check, butterfly) are STRINGS ONLY
- For multiple types (e.g., ball has Trunnion AND Floating), use comma-separated string: "BLRTA1R, BLFTA1R"

=== BOLTS/NUTS (UNIVERSAL ACROSS ALL MATERIALS) ===
Stud Bolts: ASTM A 193 Gr. B7M + coating (XYLAR 2 + XYLAN 1070, ≥50 micrometers combined)
Hex Nuts: ASTM A 194 Gr. 2HM + same coating (≥50 micrometers)
NOTE: Even SS316L and Duplex classes use carbon steel bolts with XYLAN coating (cost optimization)

=== GASKETS (UNIVERSAL) ===
All Ratings (150#-900#+): ASME B 16.20, 4.5mm spiral wound, SS316/SS316L with Flexible Graphite (F.G.) filler
Flange Type: RF (150#-600#) or RTJ (900#+)
RTJ classes use OCT ring: Soft Iron for CS (HDG, max 90 BHN), UNS S31803 for Duplex (max 22 HRC)

=== EXTRA FITTINGS INCLUSION RULES ===
Hex Plug: Include for ALL classes, ALL sizes (ASME B 16.11)
Weldolet: Include for ALL classes, ALL sizes (MSS SP 97, branch outlet)
Cap: Include for ALL classes, ALL sizes (ASME B 16.9)
Coupling: ONLY for 150#/300# classes, sizes 0.5-2" (ASME B 16.11); exclude 600#+
Union: ONLY for 150# classes (ASME B 16.11); exclude 300# and 600#+ (thread stress limits)
Swage: Special order only, not standard in specification

=== MISC ===
Spectacle Blind MOC: Same as flange MOC (A105N, A694 F60, A182 F316L/F51/F53, per material)
Design Code: ASME B 31.3 | +NACE-MR-01-75/ISO-15156-1/2/3 if "NACE" in material
Pipe Code: ASME B 36.10M
Mill Tolerance: 0.125"
Branch Chart: Ref. APPENDIX-1, Chart 1
Corrosion Allowance: Use input value (default: 3mm for CS/LTCS, 0mm for SS/Duplex)

**CRITICAL FOR VALVE CODES:** Always use the exact piping class in valve code names (for this request: {piping_class}).
Examples with {piping_class}:
  - If {piping_class}=A1: Use BLRTA1R, GAYMA1R, CHPMA1R, etc.
  - If {piping_class}=F20N: Use BLRTF20NR, GAYMF20NR, CHPMF20NR, etc.
Use CLASS_CODE directly from input, no substitution.

Return JSON schema (use actual ASME B36.10M values, not "..."):
{{
    "design_code": "ASME B 31.3 [+ NACE if applicable]",
    "pipe_code": "ASME B 36.10M",
    "mill_tolerance": "0.125",
    "branch_chart": "APPENDIX-1, Chart 1",
    "hydrotest_pressure": "[calculated as 1.5x design pressure]",
    "pipe_data": [
        {{"size_inch": "0.5", "od_mm": 21.3, "schedule": "SCH 160", "wall_thickness_mm": 7.47,
          "pipe_type": "Seamless", "material_spec": "ASTM A 106 Gr. B", "ends": "BE"}},
        {{"size_inch": "0.75", "od_mm": 26.7, "schedule": "SCH 160", "wall_thickness_mm": 7.62,
          "pipe_type": "Seamless", "material_spec": "ASTM A 106 Gr. B", "ends": "BE"}},
        ... (continue for ALL 22 standard sizes up to 36") ...
        {{"size_inch": "36.0", "od_mm": 914.4, "schedule": "[per rating]", "wall_thickness_mm": "[exact ASME value]",
          "pipe_type": "Seamless", "material_spec": "ASTM A 106 Gr. B", "ends": "BE"}}
    ],
    "fittings": {{"fitting_type": "Butt Weld (SCH to match pipe), Seamless", "material_spec": "ASTM A 234 Gr. WPB",
                  "elbow_standard": "ASME B 16.9", "tee_standard": "ASME B 16.9", "reducer_standard": "ASME B 16.9",
                  "cap_standard": "ASME B 16.9", "plug_standard": "ASME B 16.11", "weldolet_spec": "MSS SP 97 + A 105N"}},
    "fittings_welded": null,
    "fittings_by_size": [
        {{"size_inch": "0.5", "type": "Seamless", "fitting_type": "Butt Weld (SCH to match pipe), Seamless",
          "material_spec": "ASTM A 234 Gr. WPB", "elbow_standard": "ASME B 16.9", "tee_standard": "ASME B 16.9",
          "reducer_standard": "ASME B 16.9", "cap_standard": "ASME B 16.9", "plug_standard": "ASME B 16.11", "weldolet_spec": "MSS SP 97 + A 105N"}},
        ... (continue for all 22 sizes, with "type": "Seamless" for 0.5-2.0", "type": "Welded" for 3.0-36.0") ...
        {{"size_inch": "36.0", "type": "Welded", "fitting_type": "Butt Weld (SCH to match pipe), Welded",
          "material_spec": "ASTM A 234 Gr. WPB", "elbow_standard": "ASME B 16.9", "tee_standard": "ASME B 16.9",
          "reducer_standard": "ASME B 16.9", "cap_standard": "ASME B 16.9", "plug_standard": "ASME B 16.11", "weldolet_spec": "MSS SP 97 + A 105N"}}
    ],
    "extra_fittings": {{"coupling": "ASME B 16.11", "hex_plug": "ASME B 16.11", "union": "ASME B 16.11", "union_large": "",
                        "olet": "", "olet_large": "", "swage": ""}},
    "flange": {{"material_spec": "ASTM A 105N", "face_type": "RF, Serrated Finish", "flange_type": "Weld Neck, ASME B 16.5", "standard": "ASME B 16.5"}},
    "spectacle_blind": {{"material_spec": "ASTM A 105N", "standard": "ASME B 16.48"}},
    "bolts_nuts_gaskets": {{"stud_bolts": "ASTM A 193 Gr. B7M + XYLAN 1070", "hex_nuts": "ASTM A 194 Gr. 2HM + XYLAN 1070", "gasket": "ASME B 16.20, 4.5mm SS316 Spiral Wound with F.G."}},
    "valves": {{"rating": "150#", "ball": "BLRTA1R, BLFTA1R", "gate": "GAYMA1R", "globe": "GLYMA1R", "check": "CHPMA1R, CHSMA1R", "butterfly": "BFWTA1R"}},

    VALVE CODES ARE STRINGS ONLY - NOT OBJECTS. Example formats:
    - Single type: "gate": "GAYMA1R"
    - Multiple types: "ball": "BLRTA1R, BLFTA1R"
    - NEVER: "gate": {{"code": "GAYMA1R", "desc": "..."}}
    - NEVER output dict objects, descriptions, or additional keys
    "notes": ["Include hex plug and weldolet for all sizes", "Union only in 150#"]
}}

**CRITICAL REQUIREMENTS - MUST COMPLY:**
1. MANDATORY: Generate ALL 22 standard NPS sizes (0.5" through 36") — not 0.5"-24"
2. MANDATORY: Use EXACT wall thickness values from the lookup table above
   - A1 SCH 160 0.5"=7.47mm EXACTLY (not 4.78mm, not 5.56mm, not estimates)
   - A1 XS 36.0"=10.31mm EXACTLY (not 12.7mm, not calculations)
   - For unlisted combinations, research ASME B36.10M standard
3. MANDATORY: Apply correct schedule rules (SCH 160→80→STD→XS progression)
4. MANDATORY: Include all 22 sizes in pipe_data array (verify count=22)
5. MANDATORY: Fittings split into two objects by SIZE THRESHOLD only
   - fittings: 0.5"-2.0" (seamless)
   - fittings_welded: 3.0"-36.0" (welded)
   - Both use same MOC; type designation is only difference
6. MANDATORY: fittings_by_size array MUST have 22 entries (one per size)
   - Each entry includes "size_inch" and "type" ("Seamless" for 0.5-2.0", "Welded" for 3.0-36.0")
   - All other fields (material_spec, standards) are identical per size
7. MANDATORY: Valve codes MUST be STRINGS ONLY (no dict objects)
   - "GAYMA1R" not {{"code": "GAYMA1R", "desc": "..."}}
8. Return ONLY JSON. No markdown, no extra commentary."""


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
            max_tokens=8192,
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
        logger.error("AI returned invalid JSON: %s", e)
        return None
    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        return None
    except Exception as e:
        logger.error("Unexpected error in AI generation: %s", e)
        return None

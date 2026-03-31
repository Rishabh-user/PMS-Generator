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

SYSTEM_PROMPT = """You are a senior piping materials engineer with deep expertise in ASME standards (B31.3, B36.10M, B36.19M, B16.5/9/11/20/25/47/48), ASTM materials, API standards, and NACE requirements.

When generating a PMS, you MUST:
1. Follow the schedule/wall thickness/material rules provided in the prompt for each material/rating combination
2. Ensure physical consistency per ASME B36.10M/B36.19M
3. Use correct ASTM material grades
4. Select appropriate standards for pipe type, fittings, flanges, bolts, gaskets
5. Apply NACE requirements if material contains "NACE"
6. For LTCS, use impact-tested materials (A333, A350, A420)
7. For GALV classes, include BOTH screwed (≤1.5") AND butt weld (≥2") fittings; for others, butt weld only
8. Include extra fittings (coupling, hex plug, union, olet, swage) where applicable
9. Generate correct valve codes: TYPE+MATERIAL_CODE+CLASS+FACE_SUFFIX

Return ONLY valid JSON matching the schema. No markdown, no explanation."""


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

=== SCHEDULE (ASME B 36.10M / B 36.19M) ===
CS 150#: 0.5-1.5"→SCH 160 | 2-6"→SCH 80 | 8-18"→STD | 20"+→XS
CS 300#: 0.5-1.5"→SCH 160 | 2-6"→SCH 80 | 8"+→SCH 40
CS 600#: 0.5-1.5"→SCH 160 | 2"→SCH 160 | 3"+→SCH 80
CS 900#: 0.5-3"→SCH 160 | 4"+→SCH 120
CS 1500#: 0.5-1.5"→XXS | 2-6"→SCH 160 | 8"→XXS | 10-12"→SCH 140 | 14-16"→SCH 160 | 18"+→varies
CS 2500#: 0.5-1.5"→XXS | 2"+→special (verify Excel)
DSS 1500#: 0.5-4"→80S | 6"→120 | 8"→100 | 10"+→120
LTCS 150#: 0.5-1.5"→SCH 160 | 2"+→XS
GALV 150#: 0.5"→XXS | 0.75-1.5"→SCH 160 | 2-6"→SCH 80 | 8"+→STD
GALV 300#/600#: 0.5-1.5"→SCH 160 | 2-6"→SCH 80 | 8"+→SCH 40/80

=== PIPE TYPE (seamless vs welded transitions) ===
CS 150#: Seamless ≤18" | LSAW ≥20"
CS 300#/600#/900#: Seamless ≤16" | EFW ≥18"
CS 1500#/2500#: Seamless ≤16" | LSAW ≥18"
DSS: Seamless ≤8" | Welded (Longitudinally) 100% RT ≥10"
LTCS: Seamless ≤16" | EFW ≥18"
GALV: Seamless ≤16" | LSAW ≥18"

=== PIPE MOC ===
CS Seamless: ASTM A 106 Gr. B
CS LSAW/EFW 300#: ASTM A 671 - CC60 Class 22
CS LSAW/EFW 600#-900#: ASTM A 671 - CC60 Class 22 (or ASTM A 333 Gr.6 mid-sizes ~8-12")
CS LSAW 1500#/2500#: API 5L Gr, X60 PSL-2
LTCS Seamless: ASTM A 333 Gr.6
LTCS Welded: ASTM A 671 - CC60 Class 22
DSS Seamless: ASTM A 790 Gr. S31803
DSS Welded: ASTM A 928 Class 1, Gr. S31803
GALV: ASTM A 106 Gr. B (seamless) | API 5L Gr. B (welded)

=== FITTINGS ===
GALV classes only: DUAL—Screwed (#3000, B 16.11) for ≤1.5" | Butt Weld (B 16.9) for ≥2"
All others: Butt Weld (B 16.9) ONLY
TYPE field: "Butt Weld (SCH to match pipe), Seamless" (small/mid) | "Butt Weld (SCH to match pipe), Welded" (large)
CS MOC: ASTM A 234 Gr. WPB
LTCS MOC: ASTM A 420 Gr. WPL6
DSS Seamless MOC: ASTM A 815 Gr.WP-S UNS S31803
DSS Welded MOC: ASTM A 815 Gr.WP-WX UNS S31803
Weldolet: MSS SP 97 + (A 105N for CS low-P | A 350 Gr. LF2 for LTCS | A 182 Gr. F51 for DSS | A 694 F60 for CS 1500#/2500#)

=== FLANGE ===
MOC: ASTM A 105N (CS low/mid-P) | ASTM A 350 Gr. LF2 (LTCS) | ASTM A 182 Gr. F51 (DSS) | ASTM A 694 F60 (CS 1500#/2500#)
FACE: 150#-600#→"RF, Serrated Finish" | 900#+→"RTJ" [Exception: 900# CS small bore→"1500#, RTJ", large→"900#, RTJ"]
TYPE: "Weld Neck, ASME B 16.5" (or B 16.47A for certain conditions)

=== VALVE CODES (Format: TYPE + MATERIAL_CODE + CLASS + FACE_SUFFIX) ===
Ball: 150#/300#→"T" (trunnion) | 600#+→"P" (pressure), R(educed)=R, F(ull)=F, M(etal)=M
Gate Y: GAYM
Globe Y: GLYM
Check: CHPM (piston) | CHSM (swing) | CHDM (dual)
Butterfly: BFW (wafer) for 150#-300# | BFT (triple offset) for 150#-600#
DBB: DBRP (900#+ reduced bore only)
Face suffix: R (RF) | J (RTJ for 900#+)
Example: BLRTA1R=Ball Reduced Trunnion A1 RF | GAYMF20NJ=Gate Y Material F20N RTJ
900#+: Small bore→"USE GATE VALVE" | Large→BLRP[CLASS]J/BLFP[CLASS]J (use actual piping_class in code)
CLASS CODE RULE: Use the exact piping_class value in valve codes (A1, B1, D1, E1, F1, F20, F20N, etc.)

=== BOLTS/NUTS ===
CS/GALV: ASTM A 193 Gr. B7M & ASTM A 194 Gr. 2HM + XYLAR 2 + XYLAN 1070 (≥50μm)
LTCS: ASTM A 320 Gr. L7M & ASTM A 194 Gr. 7ML + XYLAR 2 + XYLAN 1070
DSS: ASTM A 453 Gr. 660 (both)

=== GASKETS ===
RF (150#-600#): ASME B 16.20, 4.5mm, SS316/SS316L Spiral Wound with Flexible Graphite (F.G.) filler
RTJ CS (900#-2500#): ASME B 16.20, OCT ring Soft Iron, Max. Hardness 90 BHN, HDG
RTJ DSS (1500#+): ASME B 16.20, OCT ring UNS S 31803, Max. Hardness 22 HRC
GALV: 3mm thick flat ring neoprene/EPDM rubber, ASME B 16.21

=== MISC ===
Spectacle Blind MOC: Same as flange
Design Code: ASME B 31.3 | +NACE-MR-01-75/ISO-15156-1/2/3 if NACE material
Pipe Code: ASME B 36.10M (CS/LTCS/GALV) | ASME B 36.19M / 36.10 (DSS)
Mill Tolerance: 0.125
Branch Chart: Ref. APPENDIX-1, Chart 1 (or Chart 2 for utility/low-P GALV)

**CRITICAL FOR VALVE CODES:** Always use the exact piping class in valve code names (for this request: {piping_class}).
Example: Use codes like BLRP{piping_class}J, GAYM{piping_class}J (replace {{piping_class}} with {piping_class}).

Return JSON schema (use actual values, not "..."):
{{
    "design_code": "...",
    "pipe_code": "...",
    "pipe_data": [
        {{"size_inch": "0.5", "od_mm": 21.3, "schedule": "...", "wall_thickness_mm": ...,
          "pipe_type": "Seamless|EFW|LSAW|Welded (Longitudinally) with 100% RT", "material_spec": "...", "ends": "BE"}}
    ],
    "fittings": {{"fitting_type": "Butt Weld (SCH to match pipe), Seamless|Welded", "material_spec": "...",
                  "elbow_standard": "ASME B 16.9", "tee_standard": "...", "reducer_standard": "...",
                  "cap_standard": "...", "plug_standard": "...", "weldolet_spec": "..."}},
    "fittings_welded": null or {{...}},
    "extra_fittings": {{"coupling": "...", "hex_plug": "...", "union": "...", "union_large": "...",
                        "olet": "...", "olet_large": "...", "swage": "..."}},
    "flange": {{"material_spec": "...", "face_type": "...", "flange_type": "Weld Neck, ASME B 16.5", "standard": "..."}},
    "spectacle_blind": {{"material_spec": "...", "standard": "ASME B 16.48"}},
    "bolts_nuts_gaskets": {{"stud_bolts": "...", "hex_nuts": "...", "gasket": "..."}},
    "valves": {{"rating": "...", "ball": "...", "gate": "...", "globe": "...", "check": "...", "butterfly": "..."}},
    "notes": ["..."]
}}

IMPORTANT: Return ONLY JSON. No markdown, no explanation. All schedule, wall thickness, material specs, and valve codes MUST follow the rules above exactly. Valve code example for {piping_class}: BLRP{piping_class}J, GAYM{piping_class}J."""


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

"""
PMS Agent service — natural-language chat over the piping-class catalogue.

Two-stage pipeline:
  1. Deterministic parser (regex) extracts structured parameters — class
     code, rating, material, CA, service, design conditions — from the
     user's prompt. Also produces the matched_classes and suggested_action
     the frontend needs for deep-linking into the generator.
  2. Claude (Anthropic) turns the user's prompt + parsed intent + match
     list + conversation history into a warm, conversational reply.

If the Anthropic call fails (no key, rate limit, credit balance, etc.)
the service falls back to a deterministic template reply so the agent
never hard-fails.
"""
import json
import logging
import re

import anthropic

from app.config import settings
from app.models.pms_agent_models import (
    AgentAction,
    AgentHistoryTurn,
    ClassMatch,
    FieldSuggestion,
    ParsedQuery,
    PMSAgentRequest,
    PMSAgentResponse,
    SlotState,
)
from app.services import data_service

logger = logging.getLogger(__name__)


# ── Parsing patterns ────────────────────────────────────────────────

# Class code: letter(A-K,T) + digit(s) + optional suffix (e.g. A1, F20N, B1LN, T1)
_CLASS_PATTERN = re.compile(r"\b([ABCDEFGJKT]\d{1,2}[A-Z]*)\b")

# Rating: 150#, 300#, 600, class 600, 1500#, etc.
_RATING_PATTERN = re.compile(
    r"\b(?:class\s*)?(150|300|400|600|900|1500|2500|5000|10000)\s*#?\b",
    re.IGNORECASE,
)

# Material keywords (checked in priority order — most specific first)
_MATERIAL_PATTERNS = [
    ("SDSS", r"\b(super\s*duplex|sdss|s32750|uns\s*s32750)\b"),
    ("DSS", r"\b(duplex|dss|s31803|s32205|uns\s*s31803)\b"),
    ("SS316L", r"\b(ss\s*316\s*l|316\s*l|astm\s*a\s*312\s*tp\s*316\s*l)\b"),
    ("SS304L", r"\b(ss\s*304\s*l|304\s*l|astm\s*a\s*312\s*tp\s*304\s*l)\b"),
    ("SS316", r"\b(ss\s*316|tp\s*316|astm\s*a\s*312\s*tp\s*316)\b"),
    ("SS304", r"\b(ss\s*304|tp\s*304)\b"),
    ("LTCS", r"\b(ltcs|low[\s-]?temp(?:erature)?\s*cs|a333)\b"),
    ("CUNI", r"\b(cuni|cu[\s-]?ni|copper[\s-]?nickel|c70600|90[\s/]?10|b\s*466)\b"),
    ("TITANIUM", r"\b(titanium|\bti\b|b861)\b"),
    ("COPPER", r"\b(copper|c12200|dhp|b\s*42)\b"),
    ("GALV", r"\b(galv(?:anised|anized)?)\b"),
    ("GRE", r"\b(gre|glass[\s-]?reinforced|epoxy)\b"),
    ("CPVC", r"\b(cpvc)\b"),
    ("API5LX60", r"\b(api\s*5l.*x\s*60|x60|x\s*60\s*psl)\b"),
    ("CS", r"\b(cs|carbon[\s-]?steel|a106|a\s*106)\b"),
]

# Corrosion allowance
_CA_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*mm(?:\s*(?:ca|corrosion))?",
    re.IGNORECASE,
)
_NIL_CA_PATTERN = re.compile(r"\b(?:nil|no)\s*(?:ca|corrosion)\b", re.IGNORECASE)

# Service keywords → canonical service string
_SERVICE_PATTERNS = [
    (r"\b(sour|h2s|nace|mr[\s-]?0175)\b", "Sour / H2S Service (NACE)"),
    (r"\bsteam\b", "Steam"),
    (r"\bhydrogen\b|\bh2\b", "Hydrogen Service"),
    (r"\b(low[\s-]?temp|cryogenic|ltcs)\b", "Low Temperature Service"),
    (r"\b(cooling\s*water|seawater|sea\s*water)\b", "Cooling Water / Seawater"),
    (r"\b(fire[\s-]?water|firewater)\b", "Fire Water"),
    (r"\b(hydrocarbon|\bhc\b|oil|diesel|gas|condensate)\b", "Hydrocarbon Service"),
    (r"\b(instrument|utility|nitrogen|air)\b", "Utility / Instrument"),
    (r"\bgeneral\b", "General"),
]

# Design conditions
_DESIGN_PRESSURE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(barg|bar|psig|psi)",
    re.IGNORECASE,
)
_DESIGN_TEMP_PATTERN = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(?:°|deg)?\s*(c|celsius|f|fahrenheit)\b",
    re.IGNORECASE,
)

# Intent keywords
_INTENT_PATTERNS = [
    (r"\b(generate|create|build|make|produce|get)\b", "generate"),
    (r"\b(list|show|find|search|which|what|available|browse)\b", "list"),
    (r"\b(tell|describe|info|details|explain|about)\b", "info"),
]


def parse_prompt(prompt: str) -> ParsedQuery:
    """Extract structured parameters from a free-text PMS query."""
    raw = prompt.strip()
    up = raw.upper()
    low = raw.lower()

    # Class code
    class_match = _CLASS_PATTERN.search(up)
    piping_class = class_match.group(1) if class_match else None

    # Rating
    rating_match = _RATING_PATTERN.search(low)
    rating = f"{rating_match.group(1)}#" if rating_match else None

    # Material
    material: str | None = None
    for code, pat in _MATERIAL_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            material = code
            break

    # Corrosion allowance
    ca: str | None = None
    if _NIL_CA_PATTERN.search(low):
        ca = "NIL"
    else:
        ca_match = _CA_PATTERN.search(low)
        if ca_match:
            value = ca_match.group(1)
            # Normalize to match data store format: "3 mm", "1.5 mm"
            ca = f"{value} mm"

    # Service
    service: str | None = None
    for pat, label in _SERVICE_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            service = label
            break

    # Design pressure
    design_pressure_barg: float | None = None
    dp_match = _DESIGN_PRESSURE_PATTERN.search(low)
    if dp_match:
        val = float(dp_match.group(1))
        unit = dp_match.group(2).lower()
        if "psi" in unit:
            design_pressure_barg = round(val / 14.5038, 2)
        else:
            design_pressure_barg = val

    # Design temperature
    design_temp_c: float | None = None
    dt_match = _DESIGN_TEMP_PATTERN.search(low)
    if dt_match:
        val = float(dt_match.group(1))
        unit = dt_match.group(2).lower()
        if unit.startswith("f"):
            design_temp_c = round((val - 32) * 5 / 9, 1)
        else:
            design_temp_c = val

    # Intent
    intent: str = "unknown"
    for pat, label in _INTENT_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            intent = label
            break
    if intent == "unknown" and piping_class:
        # A bare class code implies "info"
        intent = "info"

    return ParsedQuery(
        piping_class=piping_class,
        rating=rating,
        material=material,
        corrosion_allowance=ca,
        service=service,
        design_temp_c=design_temp_c,
        design_pressure_barg=design_pressure_barg,
        intent=intent,  # type: ignore[arg-type]
    )


# ── Matching ────────────────────────────────────────────────────────

def _material_matches(entry_material: str, query_material: str) -> bool:
    """Fuzzy material match: query 'CS' should match entry 'CS' or 'Carbon Steel'."""
    e = (entry_material or "").upper()
    q = query_material.upper()
    if q in e or e in q:
        return True
    # Common synonyms
    aliases = {
        "CS": ("CS", "CARBON", "A106", "A333"),  # LTCS also is A333 carbon
        "SS316L": ("316L", "SS316L"),
        "SS316": ("316", "SS316"),
        "SS304L": ("304L", "SS304L"),
        "DSS": ("DSS", "DUPLEX", "S31803", "S32205"),
        "SDSS": ("SDSS", "SUPER DUPLEX", "S32750"),
        "CUNI": ("CUNI", "CU-NI", "COPPER NICKEL", "C70600", "90/10"),
        "LTCS": ("LTCS", "LOW TEMP", "A333"),
        "GALV": ("GALV",),
        "TITANIUM": ("TITANIUM", "B861"),
    }
    if q in aliases:
        return any(a in e for a in aliases[q])
    return False


def _build_pt_preview(entry: dict) -> str:
    pt = entry.get("pressure_temperature", {}) or {}
    temps = pt.get("temperatures") or []
    pressures = pt.get("pressures") or []
    labels = pt.get("temp_labels") or []
    if not temps or not pressures:
        return "—"
    first = f"{pressures[0]} barg @ {labels[0] if labels else temps[0]}°C"
    last = f"{pressures[-1]} barg @ {labels[-1] if labels else temps[-1]}°C"
    return f"{first} · {last}"


def _score_match(entry: dict, q: ParsedQuery) -> float:
    """Score how well a pipe-class entry matches the parsed query (0..1).
    Higher = better. 1.0 means every specified field matched exactly."""
    score = 0.0
    checks = 0

    if q.piping_class:
        checks += 1
        if entry.get("piping_class", "").upper() == q.piping_class.upper():
            score += 1.0

    if q.rating:
        checks += 1
        if (entry.get("rating") or "").replace(" ", "") == q.rating.replace(" ", ""):
            score += 1.0

    if q.material:
        checks += 1
        if _material_matches(entry.get("material", ""), q.material):
            score += 1.0

    if q.corrosion_allowance:
        checks += 1
        if (entry.get("corrosion_allowance") or "").lower() == q.corrosion_allowance.lower():
            score += 1.0

    return score / checks if checks else 0.0


def find_matches(q: ParsedQuery, limit: int = 8) -> list[ClassMatch]:
    """Return the best matching pipe classes for the parsed query."""
    all_entries = data_service.get_all_entries()

    # Direct class-code hit always wins and short-circuits
    if q.piping_class:
        for e in all_entries:
            if e.get("piping_class", "").upper() == q.piping_class.upper():
                return [
                    ClassMatch(
                        piping_class=e["piping_class"],
                        rating=e.get("rating", ""),
                        material=e.get("material", ""),
                        corrosion_allowance=e.get("corrosion_allowance", ""),
                        pt_preview=_build_pt_preview(e),
                        score=1.0,
                    )
                ]

    # Otherwise score every entry and keep the best
    scored: list[tuple[float, dict]] = []
    any_filter = any([q.rating, q.material, q.corrosion_allowance])
    for e in all_entries:
        if any_filter:
            s = _score_match(e, q)
            if s > 0:
                scored.append((s, e))
        # If no filters provided, don't return every class — caller handles that
    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]
    return [
        ClassMatch(
            piping_class=e["piping_class"],
            rating=e.get("rating", ""),
            material=e.get("material", ""),
            corrosion_allowance=e.get("corrosion_allowance", ""),
            pt_preview=_build_pt_preview(e),
            score=round(s, 2),
        )
        for s, e in top
    ]


def _compose_reply(q: ParsedQuery, matches: list[ClassMatch]) -> str:
    """Generate a friendly natural-language reply from the parsed intent + matches."""
    if not matches:
        parts = []
        if q.rating:
            parts.append(q.rating)
        if q.material:
            parts.append(q.material)
        if q.corrosion_allowance:
            parts.append(q.corrosion_allowance)
        if q.service:
            parts.append(q.service)
        filters = " + ".join(parts) if parts else "your query"
        return (
            f"I couldn't find any piping class matching **{filters}**. "
            "Try being more specific — e.g. `A1`, `150# CS 3mm`, or `sour service SS316L`."
        )

    if len(matches) == 1 and matches[0].score >= 0.95:
        m = matches[0]
        intent = q.intent
        if intent == "generate":
            return (
                f"Got it — generating the PMS for class **{m.piping_class}** "
                f"({m.rating} · {m.material} · {m.corrosion_allowance}"
                + (f" · {q.service}" if q.service else "")
                + "). Click below to open the generator."
            )
        return (
            f"Matched class **{m.piping_class}** ({m.rating} · {m.material} · "
            f"{m.corrosion_allowance}). P-T: {m.pt_preview}. "
            "Open the generator to see the full spec."
        )

    # Multiple matches
    return (
        f"Found **{len(matches)} matching** piping class"
        f"{'es' if len(matches) > 1 else ''}. Pick one to open it in the generator."
    )


def _build_action(q: ParsedQuery, matches: list[ClassMatch]) -> AgentAction:
    if not matches:
        return AgentAction(type="none")
    best = matches[0]
    # Only auto-open when we're confident AND the user asked to generate
    if len(matches) == 1 and best.score >= 0.95 and q.intent in ("generate", "info"):
        return AgentAction(
            type="open_generator",
            piping_class=best.piping_class,
            material=best.material,
            corrosion_allowance=best.corrosion_allowance,
            service=q.service,
            design_pressure_barg=q.design_pressure_barg,
            design_temp_c=q.design_temp_c,
        )
    return AgentAction(type="list_only")


# ── Slot-filling helpers ────────────────────────────────────────────

def _available_values() -> dict[str, list[str]]:
    """Return the canonical valid values for each required field, derived from
    the pipe-class catalogue. Used both for the agent's system prompt and for
    the /api/pms-agent/chat response so the frontend can render autocomplete
    chips."""
    data = data_service.get_all_entries()
    ratings = sorted({e.get("rating", "") for e in data if e.get("rating") and e.get("rating") != "-"})
    materials = sorted({e.get("material", "") for e in data if e.get("material")})
    cas = sorted({e.get("corrosion_allowance", "") for e in data if e.get("corrosion_allowance")})
    return {"rating": ratings, "material": materials, "corrosion_allowance": cas}


def _similarity(a: str, b: str) -> float:
    """Quick-and-cheap similarity score in [0..1] — substring bonus + char-set
    overlap. Used to rank 'did you mean …?' suggestions."""
    a, b = a.upper().strip(), b.upper().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    set_a, set_b = set(a.replace(" ", "")), set(b.replace(" ", ""))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _suggest_values(provided: str, field: str, top: int = 5) -> list[str]:
    """Return up to `top` valid values for `field` ranked by similarity to
    `provided`. Empty list if `provided` already matches exactly."""
    options = _available_values().get(field, [])
    ranked = sorted(options, key=lambda v: -_similarity(provided, v))
    # Filter to meaningful matches (similarity > 0.2) to avoid random noise
    return [v for v in ranked if _similarity(provided, v) > 0.2][:top]


def _build_slot_state(parsed: ParsedQuery, matches: list[ClassMatch]) -> SlotState:
    """Compute slot-filling state. When the user hasn't typed a specific class
    code (e.g. 'generate PMS' with no parameters), the three required fields
    are Rating + Material + CA. If the user already referenced a class code
    directly (e.g. 'A1'), slots are auto-filled from the matching entry."""
    rating = parsed.rating
    material = parsed.material
    ca = parsed.corrosion_allowance

    # If the user pointed at a specific class, we can fill slots from the match
    if parsed.piping_class and matches and matches[0].score >= 0.95:
        m = matches[0]
        rating = rating or m.rating
        material = material or m.material
        ca = ca or m.corrosion_allowance

    # Normalize CA (parser returns '3 mm' but catalogue uses '3 mm', '6 mm', 'NIL', '1.5 mm')
    missing = []
    if not rating:
        missing.append("rating")
    if not material:
        missing.append("material")
    if not ca:
        missing.append("corrosion_allowance")
    return SlotState(
        rating=rating,
        material=material,
        corrosion_allowance=ca,
        missing=missing,
        complete=not missing,
    )


def _build_field_suggestions(parsed: ParsedQuery, raw_prompt: str) -> list[FieldSuggestion]:
    """For any field the parser extracted that doesn't match a valid catalogue
    value, return suggestions. Also flag unrecognized material/rating tokens
    that failed to parse at all (best-effort substring match on the raw prompt)."""
    suggestions: list[FieldSuggestion] = []
    values = _available_values()

    # Check parsed material
    if parsed.material:
        mats = [m.upper() for m in values["material"]]
        if parsed.material.upper() not in mats and not any(parsed.material.upper() in m for m in mats):
            suggestions.append(FieldSuggestion(
                field="material",
                provided=parsed.material,
                suggestions=_suggest_values(parsed.material, "material"),
            ))

    # Check parsed rating
    if parsed.rating:
        ratings_norm = [r.replace(" ", "") for r in values["rating"]]
        if parsed.rating.replace(" ", "") not in ratings_norm:
            suggestions.append(FieldSuggestion(
                field="rating",
                provided=parsed.rating,
                suggestions=_suggest_values(parsed.rating, "rating"),
            ))

    # Check parsed CA
    if parsed.corrosion_allowance:
        cas = [c.upper() for c in values["corrosion_allowance"]]
        if parsed.corrosion_allowance.upper() not in cas:
            suggestions.append(FieldSuggestion(
                field="corrosion_allowance",
                provided=parsed.corrosion_allowance,
                suggestions=_suggest_values(parsed.corrosion_allowance, "corrosion_allowance"),
            ))

    return suggestions


_AGENT_SYSTEM_PROMPT = """You are the "PMS Generator AI Agent" — a friendly, expert assistant for
a Piping Material Specification (PMS) tool used by process-piping engineers on
an oil & gas project.

You help users find and generate PMS sheets for piping classes. The project
has 92 piping classes using codes like A1, B1N, D1L, E1LN, F10, G20N, A30
(CuNi), A40 (Copper), A50/A51/A52 (GRE), A60 (CPVC), A70 (Titanium), plus
tubing classes T80A/B/C and T90A/B/C.

Class code structure: [Rating letter][Material number][Suffix]
  Rating: A=150# | B=300# | D=600# | E=900# | F=1500# | G=2500#
  Material: 1=CS 3mm CA | 2=CS 6mm CA | 1L=LTCS | 10=SS316L | 20=DSS | 25=SDSS |
            30=CuNi | 40=Copper | 50-52=GRE | 60=CPVC | 70=Titanium
  Suffix:  N=NACE (sour) | L=Low Temp | LN=LT+NACE

TO GENERATE A PMS EXCEL REPORT, the user MUST provide three fields:
  1. Pressure Rating  (required) — e.g. 150#, 300#, 600#, 900#, 1500#, 2500#, or EEMUA 20 bar for CuNi
  2. Material         (required) — CS, CS NACE, LTCS, LTCS NACE, SS316L, SS316L NACE, DSS, DSS NACE, SDSS, SDSS NACE, CS GALV, CS - Epoxy Lined, CuNi, Copper, GRE, CPVC, Titanium, 6 MO Tubing, SS 316/316L (Tubing)
  3. Corrosion Allowance (required) — NIL, 1.5 mm, 3 mm, or 6 mm

SLOT-FILLING BEHAVIOUR:

• If the user just says "generate PMS" with NO fields provided → ask for ALL
  three explicitly in a friendly numbered list. Example:
    "Happy to generate a PMS sheet! I need three things:
       1. **Pressure Rating** (e.g. 150#, 300#, 600# …)
       2. **Material** (e.g. CS, SS316L, DSS …)
       3. **Corrosion Allowance** (NIL, 1.5 mm, 3 mm, or 6 mm)
     You can send them all in one message — e.g. '150# CS 3mm'."

• If the user provides SOME but not all fields → acknowledge what you got,
  then ask ONLY for the missing ones. Never ask for fields already given.

• If a user-provided value doesn't match any valid option (e.g. "Platinum"
  as material, or "200#" as rating), DON'T just reject it — show the closest
  valid alternatives. Example:
    "I don't recognize **Platinum** as a catalogue material. Did you mean
     one of these? Titanium · CuNi · SS316L · DSS. Pick one and I'll pull
     the matching classes."

• If all three fields are filled and matches were found, describe the
  matches briefly (rating, material family, suffix meaning) and tell the
  user they can **select one or many** and download the Excel(s). If they
  select multiple, they get a single ZIP archive.

• If the user names a specific class directly (A1, F20N, etc.), that
  short-circuits slot-filling — jump straight to describing that class
  and offering the download.

OTHER GOALS:
1. Interpret informal phrasing ("give me a 300 sour class" = 300#, NACE,
   likely CS/SS).
2. For follow-ups ("what about 600#?", "the NACE version?") use
   conversation history to stay on topic and preserve earlier slots.
3. Describe matched classes in useful context — why they fit, what service
   they're for, notable traits (e.g. "F10 is a 1500# SS316L spec with a
   mixed seamless/welded pipe transition").

STYLE:
- Warm and conversational — you're a helpful colleague, not a form.
- Short paragraphs, 1–3 sentences per idea.
- Use **bold** for class codes, key ratings, and required-field names.
- For multiple matches, briefly contrast them so the user can pick.
- Never invent piping classes or values. If unsure, say so.
- Never echo JSON or mention "parser" / "matched_classes" — the UI renders
  the class cards separately; you're just the conversational layer.
- Keep replies under ~120 words unless the user explicitly asks for detail.
"""


def _format_matches_for_ai(matches: list[ClassMatch]) -> str:
    if not matches:
        return "NONE — no piping class matched the user's query."
    lines = []
    for m in matches:
        lines.append(
            f"- {m.piping_class}: {m.rating} · {m.material} · CA {m.corrosion_allowance} · "
            f"P-T: {m.pt_preview} (match score: {m.score:.2f})"
        )
    return "\n".join(lines)


def _format_parsed_for_ai(parsed: ParsedQuery) -> str:
    parts = []
    if parsed.piping_class: parts.append(f"class={parsed.piping_class}")
    if parsed.rating: parts.append(f"rating={parsed.rating}")
    if parsed.material: parts.append(f"material={parsed.material}")
    if parsed.corrosion_allowance: parts.append(f"CA={parsed.corrosion_allowance}")
    if parsed.service: parts.append(f"service={parsed.service}")
    if parsed.design_pressure_barg is not None: parts.append(f"P={parsed.design_pressure_barg} barg")
    if parsed.design_temp_c is not None: parts.append(f"T={parsed.design_temp_c}°C")
    parts.append(f"intent={parsed.intent}")
    return ", ".join(parts) if parts else "(nothing specific extracted)"


async def _compose_ai_reply(
    prompt: str,
    parsed: ParsedQuery,
    matches: list[ClassMatch],
    history: list[AgentHistoryTurn],
    slots: SlotState,
    field_suggestions: list[FieldSuggestion],
) -> str | None:
    """Ask Claude to write a conversational reply grounded in the matched
    classes. Returns None on any failure (no key, rate limit, etc.) so the
    caller can fall back to the deterministic template."""
    if not settings.anthropic_api_key:
        return None

    # Slot context
    slot_lines = [
        f"  Rating: {slots.rating or '❓ MISSING'}",
        f"  Material: {slots.material or '❓ MISSING'}",
        f"  Corrosion Allowance: {slots.corrosion_allowance or '❓ MISSING'}",
        f"  Complete (all 3 filled): {slots.complete}",
    ]
    slot_block = "\n".join(slot_lines)

    # Field-suggestion context (did-you-mean)
    fs_block = ""
    if field_suggestions:
        fs_lines = []
        for fs in field_suggestions:
            fs_lines.append(
                f"  - {fs.field}: user said '{fs.provided}' which is NOT in the catalogue. "
                f"Nearest valid values: {', '.join(fs.suggestions) if fs.suggestions else '(no close match)'}"
            )
        fs_block = "\nUSER-PROVIDED VALUES THAT DON'T MATCH THE CATALOGUE:\n" + "\n".join(fs_lines) + "\n"

    context = (
        f"USER PROMPT:\n{prompt}\n\n"
        f"WHAT THE PARSER EXTRACTED:\n{_format_parsed_for_ai(parsed)}\n\n"
        f"SLOT-FILLING STATE (the 3 required fields for generating a PMS):\n{slot_block}\n"
        f"{fs_block}\n"
        f"MATCHED PIPING CLASSES (from the deterministic catalogue search):\n"
        f"{_format_matches_for_ai(matches)}\n\n"
        f"Now write your reply to the user. Rules:\n"
        f"  • If slots are incomplete AND no class code was given → ask for the missing field(s) by name.\n"
        f"  • If field suggestions above are non-empty → present 'did you mean …?' with those values.\n"
        f"  • If matches exist → describe them briefly and invite the user to select one or many for download.\n"
        f"  • Ground every claim about a class in the MATCHED list above — do NOT invent values."
    )

    # Build messages list: prior history, then the current turn with context
    messages = []
    # Keep the last 10 history turns to cap latency and token use
    for turn in history[-10:]:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": context})

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=512,
            system=_AGENT_SYSTEM_PROMPT,
            messages=messages,
        )
        text = response.content[0].text.strip() if response.content else ""
        return text or None
    except anthropic.AuthenticationError:
        logger.warning("PMS agent: Anthropic auth error — falling back to deterministic reply")
        return None
    except anthropic.RateLimitError:
        logger.warning("PMS agent: Anthropic rate limit — falling back to deterministic reply")
        return None
    except anthropic.APIError as e:
        logger.warning("PMS agent: Anthropic API error (%s) — falling back to deterministic reply", e)
        return None
    except Exception as e:
        logger.exception("PMS agent: unexpected error generating AI reply: %s", e)
        return None


async def chat(req: PMSAgentRequest) -> PMSAgentResponse:
    """Entry point for POST /api/pms-agent/chat.

    Flow:
      1. Parse the user's prompt deterministically (regex).
      2. Search the catalogue for matching classes.
      3. Compute slot-filling state (Rating / Material / CA) and
         field-suggestion hints for any values that didn't match the
         catalogue.
      4. Ask Claude to compose a warm, conversational reply grounded in the
         parsed intent + matches + slot state + conversation history.
      5. If Claude is unavailable, fall back to a deterministic template reply.
      6. Build a suggested_action + return full slot / suggestion state so
         the frontend can render progress pills, did-you-mean chips, and
         the multi-select download UI.
    """
    parsed = parse_prompt(req.prompt)
    matches = find_matches(parsed)
    slots = _build_slot_state(parsed, matches)
    field_suggestions = _build_field_suggestions(parsed, req.prompt)

    # Prefer the AI-generated reply; fall back to the deterministic one.
    reply = await _compose_ai_reply(
        req.prompt, parsed, matches, req.history, slots, field_suggestions,
    )
    if not reply:
        reply = _compose_reply(parsed, matches)

    action = _build_action(parsed, matches)
    return PMSAgentResponse(
        reply=reply,
        interpreted=parsed,
        matched_classes=matches,
        suggested_action=action,
        slots=slots,
        field_suggestions=field_suggestions,
        available_values=_available_values(),
        allow_bulk_download=len(matches) > 0,
    )

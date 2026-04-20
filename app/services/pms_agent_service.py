"""
PMS Agent service — natural-language prompt parsing for the PMS generator.

This is intentionally deterministic (no LLM call) so the feature works
regardless of AI credit status and returns instantly. The parser extracts
piping class codes, ratings, materials, corrosion allowances, services,
and design conditions from free-text queries, then matches against the
embedded pipe-class data.
"""
import logging
import re

from app.models.pms_agent_models import (
    AgentAction,
    ClassMatch,
    ParsedQuery,
    PMSAgentRequest,
    PMSAgentResponse,
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


async def chat(req: PMSAgentRequest) -> PMSAgentResponse:
    """Entry point for POST /api/pms-agent/chat."""
    parsed = parse_prompt(req.prompt)
    matches = find_matches(parsed)
    reply = _compose_reply(parsed, matches)
    action = _build_action(parsed, matches)
    return PMSAgentResponse(
        reply=reply,
        interpreted=parsed,
        matched_classes=matches,
        suggested_action=action,
    )

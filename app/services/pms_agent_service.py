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
    # LTCS — accepts "ltcs", "low-temp cs", "low temperature cs",
    # "low-temperature carbon steel", "low temperature carbon steel",
    # and "a333". Critical: the "carbon steel" variant must be here
    # (not just in the CS pattern below) because LTCS is listed
    # BEFORE CS — otherwise a prompt like "low temperature carbon
    # steel" falls through to the plain CS pattern and the LTCS
    # intent is lost.
    ("LTCS", r"\b(ltcs|low[\s-]?temp(?:erature)?\s*(?:cs|carbon[\s-]?steel)|a333)\b"),
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
# Matches both "NIL CA" / "no corrosion" AND "CA: NIL" / "CA = nil"
# The latter form is what the frontend sends when the user clicks a "NIL"
# quick-pick chip, so the pattern has to be bidirectional.
_NIL_CA_PATTERN = re.compile(
    r"\b(?:nil|no)\s*(?:ca|corrosion)\b"
    r"|\b(?:ca|corrosion(?:\s*allowance)?)\s*[:=]?\s*nil\b",
    re.IGNORECASE,
)

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

# Intent keywords. List-signal phrases are checked FIRST because they
# dominate — "Generate all PMS of rating 150" is a browse/list request
# even though it contains "generate". If we evaluated "generate" first
# the slot-filling gate would kick in and refuse to return matches
# until Material + CA + Service were also filled, which is exactly the
# opposite of what the user wants when they ask for "all".
_INTENT_PATTERNS = [
    # Explicit list verbs
    (r"\b(list|show|find|search|which|what|available|browse)\b", "list"),
    # "all/every X" quantifiers — catches "all PMS", "all classes",
    # "every piping class", "all of them", "list them all", etc.
    (r"\b(?:all|every)\b(?:\s+(?:the\s+)?(?:pms|piping|classes?|entries?|matches?|results?|of))?", "list"),
    # Single-class generate verbs (gated)
    (r"\b(generate|create|build|make|produce|get)\b", "generate"),
    # Info / describe verbs (never gated)
    (r"\b(tell|describe|info|details|explain|about)\b", "info"),
]


def _match_catalogue_service(prompt_upper: str) -> str | None:
    """Return the first catalogue service name that appears verbatim in
    the prompt (case-insensitive). Runs BEFORE the regex patterns so
    catalogue services like "Glycol", "Hypochlorite", "Potable Water",
    "Raw Sea Water" are matched correctly even when the user's prompt
    doesn't hit any of the regex keywords.

    Catalogue services come from `_available_values()["service"]`
    which splits each class's comma-joined `service` field into its
    individual terms (so "Cooling Media, Heating Media, Diesel" becomes
    three separate pickable/matchable services).

    Longest-match-first to prevent "Water" from swallowing a more
    specific "Raw Sea Water" hit.
    """
    try:
        catalogue = _available_values().get("service", [])
    except Exception:
        return None
    for name in sorted(catalogue, key=lambda s: -len(s)):
        n = name.upper().strip()
        if not n or len(n) < 3:
            continue  # skip tiny tokens (e.g. "FG") — too noisy
        if n in prompt_upper:
            idx = prompt_upper.index(n)
            before = prompt_upper[idx - 1] if idx > 0 else " "
            after = (
                prompt_upper[idx + len(n)]
                if idx + len(n) < len(prompt_upper)
                else " "
            )
            if not before.isalnum() and not after.isalnum():
                return name  # catalogue-cased form, picker-compatible
    return None


def _match_catalogue_material(prompt_upper: str) -> str | None:
    """Return the longest catalogue material name that appears verbatim in
    the prompt (case-insensitive). This runs BEFORE the regex patterns so
    compound names like "CS NACE", "LTCS NACE", "CS - Epoxy Lined",
    "GRE (Valve: NAB)" resolve to their exact canonical string instead of
    getting swallowed by the shorter "CS" / "LTCS" / "GRE" pattern.

    We sort by length descending so "CS NACE" wins over "CS" when both
    would technically match. That's why the NarrowDownPicker chips (which
    send "Material: CS NACE") correctly narrow to CS NACE only.
    """
    try:
        catalogue = _available_values().get("material", [])
    except Exception:
        return None
    # Longest name first so compound variants beat their base form
    for name in sorted(catalogue, key=lambda s: -len(s)):
        n = name.upper().strip()
        if not n:
            continue
        # Whole-token match — avoids "CS" matching inside "ACES" etc.
        # We look for the uppercased name preceded/followed by a non-word
        # boundary. Parentheses in names ("(Valve: NAB)") aren't word
        # chars so a plain substring search is safer than \b here.
        if n in prompt_upper:
            # Require a separator (start/end/whitespace/punct) on both sides
            idx = prompt_upper.index(n)
            before = prompt_upper[idx - 1] if idx > 0 else " "
            after = (
                prompt_upper[idx + len(n)]
                if idx + len(n) < len(prompt_upper)
                else " "
            )
            if not before.isalnum() and not after.isalnum():
                return name  # return the catalogue-cased version
    return None


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

    # Material — try catalogue verbatim first (handles "CS NACE",
    # "LTCS NACE", "GRE (Valve: NAB)", etc.), then fall back to regex
    # patterns for informal phrasings like "carbon steel".
    material: str | None = _match_catalogue_material(up)
    if not material:
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

    # Detect "only <material>" / "no other material" / "exactly" /
    # "pure" / "just" phrasings that signal the user wants a STRICT
    # material match — no variants, no NACE sub-types. Example:
    # "LTCS only" should return only the 6 pure-LTCS classes, not the
    # 18 LTCS-family classes that include LTCS NACE. Without this flag
    # the fuzzy matcher treats "LTCS NACE" as a token-match hit for
    # "LTCS" (correct in general, wrong when the user said "only").
    strict_material = bool(
        re.search(r"\b(?:only|exactly|pure|solely|strictly)\b", low)
        or re.search(
            r"\bno\s+other\s+(?:material|materials|type|types|variant|variants)\b",
            low,
        )
        or re.search(
            r"\bjust\s+(?:the\s+)?"
            r"(?:cs|ltcs|ss316l?|ss304l?|dss|sdss|cuni|titanium|gre|cpvc|copper|carbon\s+steel)\b",
            low,
        )
    )

    # Detect "corrosion-resistant material" intent. Matches the common
    # phrasings users reach for: "corrosion-resistant", "corrosion
    # resistant", "CRA" (the oil-and-gas acronym for Corrosion-Resistant
    # Alloy), "stainless only", "non-corrosive", "anti-corrosion",
    # "corrosion proof". When true, find_matches filters out every
    # carbon-steel family (CS, CS NACE, LTCS, LTCS NACE, CS GALV,
    # CS - Epoxy Lined) and keeps only CRAs + non-metals.
    prefer_corrosion_resistant = bool(
        re.search(
            r"\bcorrosion[\s\-]?(?:resistant|resisting|resist|proof)\b"
            r"|\bcorrosion[\s\-]?resist",
            low,
        )
        or re.search(r"\bnon[\s\-]?corrosive\b|\banti[\s\-]?corrosion\b", low)
        or re.search(r"\bcra\b", low)
        or re.search(r"\bstainless\s+only\b", low)
    )

    # Detect NACE negation BEFORE the service match — catches all of:
    #   "no NACE", "non NACE", "non-NACE", "without NACE",
    #   "exclude NACE", "excluding NACE", "not NACE".
    # The separator between the negation word and "nace"/"sour"/"h2s"
    # can be any mix of whitespace + hyphens (so "non-NACE" works too,
    # not just "non NACE"). When negated, we flip to exclude-NACE mode
    # AND suppress the NACE/sour/H2S service match — otherwise the
    # user's "no NACE" would still get parsed as service=Sour/H2S
    # because the service pattern matches "nace" regardless of the
    # preceding negation.
    exclude_nace = bool(re.search(
        r"\b(?:no|non|not|without|exclude|excluding)[\s\-]+"
        r"(?:nace|sour|h2s)\b",
        low,
    ))

    # Detect "with NACE" / "including NACE" / "plus NACE" / "and NACE".
    # Semantically this is FAMILY INCLUSION *only when paired with a
    # specific material*: the user is saying "LTCS and its NACE
    # variants" or "CS including NACE". Without a material slot the
    # phrase is just a service filter ("Generate all 300# with NACE"
    # really does want NACE-only classes), so we gate the flag on
    # `material` being populated. Negation still takes precedence.
    _has_with_nace_phrasing = bool(re.search(
        r"\b(?:with|including|include|plus|and|also)\s+"
        r"(?:the\s+)?(?:nace|sour|h2s)\b",
        low,
    ))
    include_nace_variant = (
        _has_with_nace_phrasing
        and bool(material)
        and not exclude_nace
    )

    # Service — detection chain:
    #   1. Catalogue-verbatim hit (e.g. "Glycol", "Hypochlorite",
    #      "Raw Sea Water") — wins even when the user types it
    #      casually like "generate PMS for Service GLYCol". This is the
    #      broadest check because the picker only offers values from
    #      this list.
    #   2. Regex keyword patterns — normalise informal words
    #      (e.g. "hydrocarbon" → "Hydrocarbon Service", "nace" →
    #      "Sour / H2S Service (NACE)"). These are broader than
    #      catalogue values so they also cover phrasings that aren't
    #      literal service names.
    #   3. Structured fallback `Service: X` — the chip-click format
    #      from the picker, takes X verbatim when it's not a known
    #      catalogue term (e.g. a user-typed custom service).
    service: str | None = _match_catalogue_service(up)
    if not service:
        for pat, label in _SERVICE_PATTERNS:
            if re.search(pat, low, re.IGNORECASE):
                service = label
                break
    if not service:
        m = re.search(
            r"\bservice\s*[:=]\s*(.+?)\s*$",
            raw,
            re.IGNORECASE,
        )
        if m:
            val = m.group(1).strip().rstrip(".,;")
            if val and val.lower() not in ("", "skip", "none"):
                service = val

    # If NACE was negated, clear the matched NACE/sour service — the
    # user is saying they DON'T want sour service, so the service
    # slot shouldn't be filled with "Sour / H2S Service (NACE)".
    if exclude_nace and service:
        s_lc = service.lower()
        if "nace" in s_lc or "sour" in s_lc or "h2s" in s_lc:
            service = None

    # If "with NACE" / "including NACE" was said, this is family
    # inclusion (keep NACE variants in the fuzzy material match),
    # NOT a service filter. Clearing the NACE/sour service here means
    # the matcher won't narrow to NACE-only rows, and the fuzzy
    # material match naturally returns the full family (both plain
    # and NACE). Example: "LTCS with NACE" → material="LTCS", service
    # cleared → 18 LTCS-family matches instead of 12 NACE-only.
    if include_nace_variant and service:
        s_lc = service.lower()
        if "nace" in s_lc or "sour" in s_lc or "h2s" in s_lc:
            service = None

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
        exclude_nace=exclude_nace,
        prefer_corrosion_resistant=prefer_corrosion_resistant,
        strict_material=strict_material,
        design_temp_c=design_temp_c,
        design_pressure_barg=design_pressure_barg,
        intent=intent,  # type: ignore[arg-type]
    )


# ── Matching ────────────────────────────────────────────────────────

def _material_matches(
    entry_material: str, query_material: str, *, strict: bool = False,
) -> bool:
    """Fuzzy material match using whole-token (word-boundary) matching.

    Why word boundaries matter: naive substring matching makes "CS"
    match inside "LTCS" (the characters `CS` are a suffix), so a user
    asking for LTCS would incorrectly pull in every plain-CS class too.
    With \\b boundaries we require CS to be a standalone token — it
    matches "CS" and "CS NACE" but not "LTCS".

    Accepts query match both on the full material string and against
    known aliases (e.g. "SS316L" ↔ "316L", "DSS" ↔ "DUPLEX"). Aliases
    also use word-boundary matching.

    `strict=True` disables every fuzzy mechanism — only case-insensitive
    exact string equality counts. Used when the user said "LTCS only" /
    "just CS" / "no other material" etc., so "LTCS" no longer matches
    "LTCS NACE" and "CS" no longer matches "CS NACE" / "CS GALV".
    """
    e = (entry_material or "").upper().strip()
    q = query_material.upper().strip()
    if not e or not q:
        return False

    # Exact match — the only path that fires in strict mode
    if e == q:
        return True
    if strict:
        return False

    # Query appears as a whole token inside the entry material name
    # (e.g. q="CS" matches e="CS NACE", q="GRE" matches
    # e="GRE (VALVE: NAB)"). Uses \b which respects word boundaries
    # so q="CS" does NOT match e="LTCS".
    if re.search(rf"\b{re.escape(q)}\b", e):
        return True

    # Common synonyms — each alias is also matched as a whole token.
    # Note: LTCS doesn't include "CS" as an alias (that was the old
    # behaviour that caused the LTCS → CS bug).
    aliases = {
        "CS": ("CARBON", "A106"),
        "SS316L": ("316L",),
        "SS316": ("316",),
        "SS304L": ("304L",),
        "SS304": ("304",),
        "DSS": ("DUPLEX", "S31803", "S32205"),
        "SDSS": ("SUPER DUPLEX", "S32750"),
        "CUNI": ("CU-NI", "COPPER NICKEL", "C70600", "90/10"),
        "LTCS": ("LOW TEMP", "A333"),
        "GALV": (),  # covered by q-as-token match
        "TITANIUM": ("B861",),
    }
    if q in aliases:
        for a in aliases[q]:
            if re.search(rf"\b{re.escape(a)}\b", e):
                return True
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
        if _material_matches(
            entry.get("material", ""), q.material, strict=q.strict_material,
        ):
            score += 1.0

    if q.corrosion_allowance:
        checks += 1
        if (entry.get("corrosion_allowance") or "").lower() == q.corrosion_allowance.lower():
            score += 1.0

    return score / checks if checks else 0.0


def _is_corrosion_resistant_material(material: str) -> bool:
    """Return True when the material spec is a corrosion-resistant alloy
    (CRA) or a non-metal, False when it's a carbon-steel family member.

    CRAs / non-metals kept when user asks for "corrosion-resistant":
      • Stainless: SS316L, SS316L NACE, SS316, SS304L, 316/316L Tubing,
                   6 MO Tubing
      • Duplex / super duplex: DSS, DSS NACE, SDSS, SDSS NACE
      • Copper alloys: CuNi (good for seawater), Copper (for potable)
      • Exotics: Titanium
      • Non-metals: GRE, CPVC

    Rejected (carbon-steel family — susceptible to corrosion even with
    NACE / galvanized / epoxy-lined variants):
      • CS, CS NACE, LTCS, LTCS NACE, CS GALV, CS GALV (Valve: SS),
        CS - Epoxy Lined
    """
    m = (material or "").upper().strip()
    if not m:
        return False
    # Any material whose token set includes CS / LTCS / GALV / EPOXY
    # is carbon-steel family — reject.
    cs_markers = ("CS NACE", "LTCS", "CS GALV", "CS - EPOXY", "EPOXY LINED")
    for marker in cs_markers:
        if marker in m:
            return False
    # Bare "CS" is also carbon steel. Check with word boundaries so
    # "SS" (in SS316L) doesn't trip.
    if re.search(r"\bCS\b", m):
        return False
    # Everything else (SS*, DSS*, SDSS*, CuNi, Copper, GRE, CPVC,
    # Titanium, 6 MO, 316/316L Tubing) is corrosion-resistant.
    return True


def _apply_cra_filter(rows: list[dict]) -> list[dict]:
    """Keep only rows whose material spec is a corrosion-resistant
    alloy / non-metal. Invoked when `prefer_corrosion_resistant` is
    set on the query. Uses `_is_corrosion_resistant_material` so the
    classification is consistent wherever CRA is referenced."""
    return [
        e for e in rows
        if _is_corrosion_resistant_material(e.get("material", ""))
    ]


def _apply_nace_exclusion(rows: list[dict]) -> list[dict]:
    """Drop every row whose MATERIAL spec contains "NACE". Invoked when
    the user said "no NACE" / "non-NACE" / "without NACE" etc. — the
    opposite of the include-only NACE filter handled inside
    `_filter_by_service`. Kept as a separate helper so it can also run
    on list-intent queries where the user specifies no service at all
    (`"all 300# non-NACE"` → no service slot, just the exclusion)."""
    return [
        e for e in rows
        if "nace" not in (e.get("material") or "").lower()
    ]


def _filter_by_service(rows: list[dict], service: str | None) -> list[dict]:
    """Narrow candidate rows based on the user-specified service.

    Two-layer logic — catalogue services live in two different places:

      1. NACE / sour / H2S service → filter by MATERIAL name containing
         "NACE". The NACE compliance flag is part of the material spec
         ("CS NACE", "SS316L NACE", "DSS NACE", "SDSS NACE", "LTCS NACE")
         and is NOT mentioned in any row's service field. Without this
         special case, "list all 300# NACE" returns both NACE and
         non-NACE classes.

      2. Any other non-broad service → substring-match against the row's
         service field (a comma-joined list like "Cooling Media,
         Heating Media, Diesel"). Broad labels like "General" are
         treated as wildcards — see _BROAD_SERVICES.

    Soft fallback: if the filter would eliminate every row, we return
    the unfiltered input instead. The user's rating/material/CA
    filters are stronger signals than an exact service-string match,
    so we don't dead-end the list on a service typo.

    NACE EXCLUSION (the opposite direction) is handled separately by
    `_apply_nace_exclusion`, triggered by `parsed.exclude_nace` in
    `find_matches` — the two can't both fire for the same query so
    they're kept apart.
    """
    if not service:
        return rows
    s_needle = service.strip().lower()
    if not s_needle or s_needle in _BROAD_SERVICES:
        return rows

    # NACE / sour / H2S — filter by material name
    if "nace" in s_needle or "sour" in s_needle or "h2s" in s_needle:
        refined = [
            e for e in rows
            if "nace" in (e.get("material") or "").lower()
        ]
        return refined if refined else rows

    # Regular service — substring match against catalogue service field.
    # Whitespace is normalised on BOTH sides because the catalogue is
    # inconsistent about whether multi-word services are one word or
    # two (e.g. "Seawater" in A5/A6 vs "Raw Sea Water" in A30/A50 —
    # both should match a user typing "seawater"). Compressing
    # whitespace-free also handles "firewater" vs "fire water",
    # "Potable Water" vs "potablewater", etc.
    s_needle_compact = re.sub(r"\s+", "", s_needle)
    refined = [
        e for e in rows
        if s_needle_compact in re.sub(r"\s+", "", (e.get("service") or "").lower())
    ]
    return refined if refined else rows


def find_matches(q: ParsedQuery, limit: int | None = None) -> list[ClassMatch]:
    """Return the best matching pipe classes for the parsed query.

    Result shape depends on how much the user has pinned down:

      • Direct class-code hit ("A1") → that one class, exact.
      • All three slots filled (Rating + Material + CA) → only ENTRIES
        that match all three EXACTLY. Typically 1 row. No partial/fuzzy
        matches — the user has already been explicit, so we don't show
        "close" results (A1 CS vs A1N CS NACE) as noise.
      • Partial filters (exploratory / list queries) → every fuzzy-scored
        hit, sorted best-first.

    `limit` is an optional safety cap. Pass None (the default) to return
    every match — the chat UI wants the full list so users can browse
    without hidden rows. Callers that want a top-N cut (e.g. an
    autocomplete dropdown) can still pass an integer.
    """
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

    # Strict path: all three required slots filled → require exact match
    # on all three. This is the common "user finished slot-picking" path
    # and we don't want to dump 8 near-misses; 1 precise answer is what
    # the user asked for.
    #
    # Two-phase material matching:
    #   1. Exact (case-insensitive). Handles full catalogue values like
    #      "CS", "SS316L NACE", etc. picked directly from the picker.
    #   2. Fuzzy (_material_matches). Only runs if phase 1 returned
    #      nothing — handles short labels like "GRE" (from the regex
    #      parser) matching catalogue rows like "GRE (Valve: NAB)".
    if q.rating and q.material and q.corrosion_allowance:
        exact: list[dict] = []
        qr = q.rating.replace(" ", "")
        qm = q.material.strip().upper()
        qc = q.corrosion_allowance.strip().upper()
        for e in all_entries:
            er = (e.get("rating") or "").replace(" ", "")
            em = (e.get("material") or "").strip().upper()
            ec = (e.get("corrosion_allowance") or "").strip().upper()
            if er == qr and em == qm and ec == qc:
                exact.append(e)
        if not exact and not q.strict_material:
            # Fall back to fuzzy material match — lets "GRE" hit the
            # catalogue's "GRE (Valve: NAB)" entries (A50/A51/A52).
            # Skipped when strict_material is set: the user explicitly
            # asked for the exact material and has accepted that there
            # may be zero matches.
            for e in all_entries:
                er = (e.get("rating") or "").replace(" ", "")
                ec = (e.get("corrosion_allowance") or "").strip().upper()
                if (
                    er == qr
                    and _material_matches(e.get("material", ""), q.material)
                    and ec == qc
                ):
                    exact.append(e)

        # Further narrow by service — handles both NACE (material-name
        # filter) and regular catalogue services (service-field
        # substring). See _filter_by_service for the logic.
        exact = _filter_by_service(exact, q.service)

        # NACE EXCLUSION — user said "no NACE" / "non-NACE": drop every
        # row whose material spec contains "NACE" so the result set has
        # only standard materials.
        if q.exclude_nace:
            exact = _apply_nace_exclusion(exact)

        # CORROSION-RESISTANT filter — user asked for "corrosion-
        # resistant material" / "CRA": keep only CRA + non-metal rows,
        # drop every carbon-steel family (CS, CS NACE, LTCS, CS GALV,
        # CS - Epoxy Lined).
        if q.prefer_corrosion_resistant:
            exact = _apply_cra_filter(exact)

        rows = exact if limit is None else exact[:limit]
        return [
            ClassMatch(
                piping_class=e["piping_class"],
                rating=e.get("rating", ""),
                material=e.get("material", ""),
                corrosion_allowance=e.get("corrosion_allowance", ""),
                pt_preview=_build_pt_preview(e),
                score=1.0,
            )
            for e in rows
        ]

    # Fuzzy path: partial filters — score every entry and keep the best.
    # `any_filter` must include service AND the exclude_nace flag —
    # otherwise a pure-service query like "generate all PMS of
    # Hydraulic Oil" sees no filters, returns zero matches, and the
    # user gets gated despite intent=list. Service rows that don't
    # match the other filters still score 0 via _score_match (no
    # rating/material/CA to score against) and then get filtered in
    # by `_filter_by_service` below.
    scored: list[tuple[float, dict]] = []
    any_filter = any([
        q.rating, q.material, q.corrosion_allowance,
        q.service, q.exclude_nace, q.prefer_corrosion_resistant,
    ])
    # When only service / exclude_nace is specified (no rating /
    # material / CA to score against), _score_match would divide by
    # zero-checks; skip scoring and include every row at score 1.0 so
    # the service filter below can narrow from the full catalogue.
    score_by_fields = bool(q.rating or q.material or q.corrosion_allowance)
    for e in all_entries:
        if not any_filter:
            continue
        # Strict-material: HARD filter. When the user said "LTCS only"
        # etc., a material mismatch must eliminate the row outright,
        # not merely contribute 0 to the fuzzy score. Otherwise a row
        # like B1 (300#, CS) would still score 0.5 on a query of
        # "CS only at 150#" (rating mismatch = 0, material match = 1
        # → 0.5) and slip through.
        if q.strict_material and q.material:
            if not _material_matches(
                e.get("material", ""), q.material, strict=True,
            ):
                continue
        if score_by_fields:
            s = _score_match(e, q)
            if s > 0:
                scored.append((s, e))
        else:
            # Service-only / exclusion-only query — every row is a
            # candidate; _filter_by_service and the exclusion step
            # below do the actual narrowing.
            scored.append((1.0, e))

    # Service filter applies in the fuzzy/list path too — critical for
    # queries like "list all 300# NACE" where the user expects only
    # NACE-compliant classes, not the full 300# set. Same helper used
    # by the strict path so the behaviour is consistent.
    if q.service:
        scored_rows = [e for _, e in scored]
        filtered = _filter_by_service(scored_rows, q.service)
        kept_ids = {id(e) for e in filtered}
        scored = [(s, e) for s, e in scored if id(e) in kept_ids]

    # NACE exclusion in the fuzzy/list path — mirrors the strict path.
    # Example: "Generate all PMS of rating 300 with no NACE" → no
    # service slot set, but every NACE-material row must still be
    # dropped from the list.
    if q.exclude_nace:
        scored = [
            (s, e) for s, e in scored
            if "nace" not in (e.get("material") or "").lower()
        ]

    # CORROSION-RESISTANT filter in the fuzzy/list path. Drops every
    # carbon-steel family row (CS, CS NACE, LTCS, CS GALV, CS - Epoxy
    # Lined) so queries like "glycol service with corrosion-resistant
    # material" return only SS / DSS / SDSS / CuNi / Titanium / GRE
    # / CPVC classes.
    if q.prefer_corrosion_resistant:
        scored = [
            (s, e) for s, e in scored
            if _is_corrosion_resistant_material(e.get("material", ""))
        ]

    scored.sort(key=lambda x: -x[0])
    top = scored if limit is None else scored[:limit]
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


_FIELD_LABELS = {
    "rating": "Pressure Rating",
    "material": "Material",
    "corrosion_allowance": "Corrosion Allowance",
    "service": "Service Description",
}


def _compose_gated_reply(slots: SlotState) -> str:
    """Deterministic fallback reply when slot-filling is blocking results.
    Used only when Anthropic is unavailable — the AI reply usually phrases
    the ask more naturally. Kept short and structured so the user sees
    EXACTLY which fields are still missing."""
    filled = []
    if slots.rating: filled.append(f"Rating = **{slots.rating}**")
    if slots.material: filled.append(f"Material = **{slots.material}**")
    if slots.corrosion_allowance:
        filled.append(f"Corrosion Allowance = **{slots.corrosion_allowance}**")
    if slots.service: filled.append(f"Service = **{slots.service}**")

    missing_labels = [_FIELD_LABELS[f] for f in slots.missing]

    lead = (
        f"Got it — so far I have {', '.join(filled)}. "
        if filled
        else "Happy to generate a PMS sheet! "
    )
    if len(missing_labels) == 1:
        return (
            f"{lead}I still need the **{missing_labels[0]}** before I can pull "
            "the matching piping classes. Pick a value from the chips below or "
            "type it in."
        )
    if len(missing_labels) == 2:
        return (
            f"{lead}I still need **{missing_labels[0]}** and "
            f"**{missing_labels[1]}**. Pick from the chips below or send them "
            "in one message."
        )
    return (
        f"{lead}To generate a PMS I need four things:\n"
        f"  1. **Pressure Rating** (e.g. 150#, 300#, 600# …)\n"
        f"  2. **Material** (e.g. CS, SS316L, DSS …)\n"
        f"  3. **Corrosion Allowance** (NIL, 1.5 mm, 3 mm, or 6 mm)\n"
        f"  4. **Service Description** (General, Hydrocarbon, Sour / H2S, …)\n"
        "Pick from the chips below or send them in one message."
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
    # list_only actions still need to carry the user-picked service
    # (and design P/T) — otherwise Download Excel / View Details
    # requests built from this action fall back to service="General"
    # in the frontend, and the generated PMS shows "General" in its
    # Service field instead of what the user picked. Bug was only
    # visible in multi-match / chip-pick flows because those return
    # list_only, never open_generator.
    return AgentAction(
        type="list_only",
        service=q.service,
        design_pressure_barg=q.design_pressure_barg,
        design_temp_c=q.design_temp_c,
    )


# ── Slot-filling helpers ────────────────────────────────────────────

# Services that are effectively wildcards — the user picked a broad
# category and doesn't want it used as a filter. We still accept them
# as a slot value (so slot-fill can complete) but won't narrow results.
_BROAD_SERVICES = {"general", "none", "n/a", "na", ""}


def _available_values(parsed: ParsedQuery | None = None) -> dict[str, list[str]]:
    """Return catalogue-valid values for each required field, optionally
    FILTERED by the slots the user has already locked in.

    When `parsed` is None (or all slots empty) → returns every value
    that exists in the catalogue. That's what the first-turn picker
    shows before the user has committed to anything.

    When `parsed` carries, say, material="GRE" → the returned `rating`,
    `corrosion_allowance` AND `service` lists are narrowed to only those
    values that co-occur with GRE in a real catalogue row. Prevents the
    "user picked GRE, then 150# + 3 mm, ended up with no matches" loop.

    The field being picked is intentionally NOT self-filtered — we need
    to show every option for it. Only the OTHER slots act as filters.

    Service values come from the catalogue itself: each class's service
    field is a comma-separated list of service names (e.g.
    "Cooling Media, Heating Media, Diesel, …"). We split those and
    collect uniques so the picker offers exactly the services real
    classes are flagged for — including class-specific ones like
    "Hypochlorite" that a curated list would miss. A short fallback
    roster is merged in so early-turn pickers (no slots yet) still
    show broad categories like "General".
    """
    data = data_service.get_all_entries()

    # Short curated list shown when the picker has nothing specific
    # from the catalogue (or to guarantee common categories are always
    # present regardless of what's cached).
    curated_services = {
        "General",
        "Hydrocarbon Service",
        "Sour / H2S Service (NACE)",
        "Cooling Water / Seawater",
        "Fire Water",
        "Steam",
        "Low Temperature Service",
        "Utility / Instrument",
        "Hydrogen Service",
    }

    if parsed is None:
        parsed = ParsedQuery()

    def _norm_rating(r: str | None) -> str:
        return (r or "").replace(" ", "").strip()

    def _apply_other_filters(exclude: str) -> list[dict]:
        """Return catalogue rows filtered by every filled slot EXCEPT
        the one we're listing values for."""
        rows = data
        if exclude != "material" and parsed.material:
            q_mat = parsed.material
            rows = [
                e for e in rows
                if _material_matches(
                    e.get("material", ""), q_mat,
                    strict=parsed.strict_material,
                )
            ]
        if exclude != "rating" and parsed.rating:
            qr = _norm_rating(parsed.rating)
            rows = [e for e in rows if _norm_rating(e.get("rating")) == qr]
        if exclude != "corrosion_allowance" and parsed.corrosion_allowance:
            qc = (parsed.corrosion_allowance or "").strip().upper()
            rows = [
                e for e in rows
                if (e.get("corrosion_allowance") or "").strip().upper() == qc
            ]
        if exclude != "service" and parsed.service:
            sn = parsed.service.strip().lower()
            if sn and sn not in _BROAD_SERVICES:
                # Soft filter: only apply service narrowing if at least
                # one row still matches. If the catalogue doesn't have
                # the service populated for this combo (or the user
                # typed a service no row mentions), keep the rows
                # unfiltered — the picker staying populated matters
                # more than strict service narrowing. `find_matches`
                # does its own strict service filter when the user
                # actually asks for results, so nothing leaks through
                # to the final answer.
                sn_compact = re.sub(r"\s+", "", sn)
                filtered = [
                    e for e in rows
                    if sn_compact in re.sub(
                        r"\s+", "", (e.get("service") or "").lower()
                    )
                ]
                if filtered:
                    rows = filtered
        # Corrosion-resistant and NACE filters always apply regardless
        # of `exclude`, since they constrain MATERIAL across all pickers.
        # Without these, the Material picker would still offer carbon
        # steel options even after the user asked for "corrosion-
        # resistant" — exactly the bug we're fixing.
        if parsed.prefer_corrosion_resistant:
            rows = [
                e for e in rows
                if _is_corrosion_resistant_material(e.get("material", ""))
            ]
        if parsed.exclude_nace:
            rows = [
                e for e in rows
                if "nace" not in (e.get("material") or "").lower()
            ]
        return rows

    rating_rows = _apply_other_filters("rating")
    material_rows = _apply_other_filters("material")
    ca_rows = _apply_other_filters("corrosion_allowance")
    service_rows = _apply_other_filters("service")

    ratings = sorted({
        e.get("rating", "") for e in rating_rows
        if e.get("rating") and e.get("rating") != "-"
    })
    materials = sorted({
        e.get("material", "") for e in material_rows if e.get("material")
    })
    cas = sorted({
        e.get("corrosion_allowance", "") for e in ca_rows
        if e.get("corrosion_allowance")
    })

    # Services: split each class's comma-joined service field into
    # individual terms. A class like A1 with
    # "Cooling Media, Heating Media, Diesel, Steam, …" becomes five
    # separate pickable options.
    catalogue_services: set[str] = set()
    for e in service_rows:
        raw_svc = e.get("service") or ""
        for part in raw_svc.split(","):
            token = part.strip()
            if token:
                catalogue_services.add(token)

    # When the user has narrowed by material/rating/CA, show ONLY the
    # catalogue services for that intersection — no padding with
    # generic curated options that may not apply. When nothing's
    # narrowed yet, merge the curated roster in so the first-turn
    # picker still has the common broad categories.
    if (
        parsed.material or parsed.rating or parsed.corrosion_allowance
    ) and catalogue_services:
        services = sorted(catalogue_services)
    else:
        services = sorted(catalogue_services | curated_services)

    return {
        "rating": ratings,
        "material": materials,
        "corrosion_allowance": cas,
        "service": services,
    }


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


def _suggest_values(
    provided: str,
    field: str,
    top: int = 5,
    pool: list[str] | None = None,
) -> list[str]:
    """Return up to `top` valid values for `field` ranked by similarity
    to `provided`. Empty list if `provided` already matches exactly.

    `pool` optionally overrides the candidate list — used by
    `_build_field_suggestions` to suggest only values that co-occur
    with the user's already-filled slots (so we never recommend a
    rating that has no class for the chosen material)."""
    options = pool if pool is not None else _available_values().get(field, [])
    ranked = sorted(options, key=lambda v: -_similarity(provided, v))
    # Filter to meaningful matches (similarity > 0.2) to avoid random noise
    return [v for v in ranked if _similarity(provided, v) > 0.2][:top]


def _merge_slots_from_history(
    parsed: ParsedQuery, history: list[AgentHistoryTurn]
) -> ParsedQuery:
    """Carry forward slot values from earlier user turns in the conversation.

    The current turn always wins — history only fills GAPS. This is what
    makes multi-turn slot-filling work: turn 1 "rating 150", turn 2 "CS",
    turn 3 "3mm" should collapse to (rating=150#, material=CS, CA=3 mm)
    by the third turn, without the client having to re-send the whole
    prompt each time.

    Assistant turns are ignored — only user-supplied values count as slots.

    ── Fresh-request guard ────────────────────────────────────────────
    Any turn whose intent is "generate" is treated as a NEW PMS request
    and DOES NOT inherit slots from earlier turns. The reasoning:

      • "Generate a PMS for rating 150" is the user explicitly starting
        a fresh spec. If we silently borrow Material/CA from whatever
        they said two chats ago, they get results without being asked —
        exactly the behaviour the gate exists to prevent.

      • "Generate PMS" with nothing else is an empty request. Better to
        ask for all three fields than to auto-restore stale picks.

    Pure follow-ups (intent=unknown / "list" / "info", or bare slot-pick
    turns like "Material: CS") still merge from history — that's how the
    chat converges across multiple picks. The quick-pick chip flow
    sends intent=unknown prompts, so multi-turn slot filling works as
    expected.
    """
    if not history:
        return parsed

    if parsed.intent == "generate":
        return parsed

    for turn in history:
        if turn.role != "user":
            continue
        prior = parse_prompt(turn.content)
        if not parsed.piping_class and prior.piping_class:
            parsed.piping_class = prior.piping_class
        if not parsed.rating and prior.rating:
            parsed.rating = prior.rating
        if not parsed.material and prior.material:
            parsed.material = prior.material
        if not parsed.corrosion_allowance and prior.corrosion_allowance:
            parsed.corrosion_allowance = prior.corrosion_allowance
        if not parsed.service and prior.service:
            parsed.service = prior.service
        if parsed.design_pressure_barg is None and prior.design_pressure_barg is not None:
            parsed.design_pressure_barg = prior.design_pressure_barg
        if parsed.design_temp_c is None and prior.design_temp_c is not None:
            parsed.design_temp_c = prior.design_temp_c
    return parsed


def _auto_fill_unique_slots(parsed: ParsedQuery) -> ParsedQuery:
    """Catalogue-driven auto-fill: when a slot is empty but the available
    values (given the slots that ARE filled) narrow to exactly one,
    fill it automatically. Typical trigger: user picks a niche
    material like GRE which has only one rating (150#) and one CA
    (NIL) in the catalogue — no point making them click through
    pickers with a single option each.

    Runs in up to 3 passes because each fill can narrow the others
    further. Service is NOT auto-filled — it's descriptive user input
    that changes the generated PMS document, so the user must declare
    it explicitly even if only one class matches.
    """
    for _ in range(3):
        values = _available_values(parsed)
        changed = False
        if not parsed.rating and len(values.get("rating", [])) == 1:
            parsed.rating = values["rating"][0]
            changed = True
        if not parsed.material and len(values.get("material", [])) == 1:
            parsed.material = values["material"][0]
            changed = True
        if (
            not parsed.corrosion_allowance
            and len(values.get("corrosion_allowance", [])) == 1
        ):
            parsed.corrosion_allowance = values["corrosion_allowance"][0]
            changed = True
        if not changed:
            break
    return parsed


def _should_gate_matches(parsed: ParsedQuery, slots: SlotState) -> bool:
    """Return True when we must HIDE matched_classes and push the user to
    finish slot-filling first. Rule:

      • An explicit "list/show/find" query → never gate.
      • A direct class-code lookup (e.g. "A1") → never gate.
      • All four required slots filled (Rating + Material + CA + Service)
        → never gate.
      • Otherwise → gate, so the reply nudges for missing fields instead
        of dumping dozens of partial matches.
    """
    if parsed.intent == "list":
        return False
    if parsed.piping_class:
        return False
    if slots.complete:
        return False
    return True


def _build_slot_state(parsed: ParsedQuery, matches: list[ClassMatch]) -> SlotState:
    """Compute slot-filling state. When the user hasn't typed a specific
    class code (e.g. 'generate PMS' with no parameters), the four
    required fields are Rating + Material + CA + Service. If the user
    already referenced a class code directly (e.g. 'A1'), slots are
    auto-filled from the matching entry.

    Service is required to prevent the generated PMS from silently
    defaulting to "General" — if the user has a specific service in
    mind they should declare it; if they genuinely mean general, they
    pick "General" from the chip list.
    """
    rating = parsed.rating
    material = parsed.material
    ca = parsed.corrosion_allowance
    service = parsed.service

    # If the user pointed at a specific class, we can fill slots from the match
    if parsed.piping_class and matches and matches[0].score >= 0.95:
        m = matches[0]
        rating = rating or m.rating
        material = material or m.material
        ca = ca or m.corrosion_allowance
        # Note: ClassMatch doesn't carry service — we don't auto-fill it
        # from the class, so the user still gets prompted if they only
        # gave a class code.

    missing = []
    if not rating:
        missing.append("rating")
    if not material:
        missing.append("material")
    if not ca:
        missing.append("corrosion_allowance")
    if not service:
        missing.append("service")
    return SlotState(
        rating=rating,
        material=material,
        corrosion_allowance=ca,
        service=service,
        missing=missing,
        complete=not missing,
    )


def _build_field_suggestions(parsed: ParsedQuery, raw_prompt: str) -> list[FieldSuggestion]:
    """For any field the parser extracted that doesn't match a valid
    catalogue value, return suggestions. The suggestion universe is
    narrowed by whatever OTHER slots are already filled — so if the
    user picked GRE + typed a bad rating, the 'did you mean…' chips
    only offer ratings that actually have a GRE class."""
    suggestions: list[FieldSuggestion] = []
    # Unfiltered values — used for basic 'is this token valid in the
    # catalogue at all?' check. Filtered values (narrowed by other
    # slots) are what we surface as suggestions so the user is pushed
    # toward a combination that will actually match.
    values_all = _available_values()
    values_filtered = _available_values(parsed)

    # Check parsed material
    if parsed.material:
        mats = [m.upper() for m in values_all["material"]]
        if parsed.material.upper() not in mats and not any(
            parsed.material.upper() in m for m in mats
        ):
            suggestions.append(FieldSuggestion(
                field="material",
                provided=parsed.material,
                suggestions=_suggest_values(
                    parsed.material, "material", pool=values_filtered["material"],
                ),
            ))

    # Check parsed rating
    if parsed.rating:
        ratings_norm = [r.replace(" ", "") for r in values_all["rating"]]
        if parsed.rating.replace(" ", "") not in ratings_norm:
            suggestions.append(FieldSuggestion(
                field="rating",
                provided=parsed.rating,
                suggestions=_suggest_values(
                    parsed.rating, "rating", pool=values_filtered["rating"],
                ),
            ))

    # Check parsed CA
    if parsed.corrosion_allowance:
        cas = [c.upper() for c in values_all["corrosion_allowance"]]
        if parsed.corrosion_allowance.upper() not in cas:
            suggestions.append(FieldSuggestion(
                field="corrosion_allowance",
                provided=parsed.corrosion_allowance,
                suggestions=_suggest_values(
                    parsed.corrosion_allowance,
                    "corrosion_allowance",
                    pool=values_filtered["corrosion_allowance"],
                ),
            ))

    # Service doesn't have a catalogue constraint (free-text), so no
    # did-you-mean list — user-typed strings are always accepted. The
    # service picker still shows common options via available_values.

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

TO GENERATE A PMS EXCEL REPORT, the user MUST provide four fields:
  1. Pressure Rating  (required) — e.g. 150#, 300#, 600#, 900#, 1500#, 2500#, or EEMUA 20 bar for CuNi
  2. Material         (required) — CS, CS NACE, LTCS, LTCS NACE, SS316L, SS316L NACE, DSS, DSS NACE, SDSS, SDSS NACE, CS GALV, CS - Epoxy Lined, CuNi, Copper, GRE, CPVC, Titanium, 6 MO Tubing, SS 316/316L (Tubing)
  3. Corrosion Allowance (required) — NIL, 1.5 mm, 3 mm, or 6 mm
  4. Service Description (required) — General, Hydrocarbon Service, Sour / H2S Service (NACE), Cooling Water / Seawater, Fire Water, Steam, Low Temperature Service, Utility / Instrument, Hydrogen Service (or any free-text service the user describes)

Every one of the four is blocking. If the user says "generate PMS"
without Service, ask for it just like you'd ask for a missing Rating
— don't silently default to General, because that ends up in the
generated Excel sheet and is confusing.

IMPORTANT: The available-values lists given below are already filtered
to values that EXIST for the slots the user has already picked. If a
list looks short (e.g. Rating only has two options after the user
picked GRE material), that's because only those ratings have a class
available for the chosen material. Don't invent other values —
the user picked a material that just doesn't have all rating options.

GATING RULE (very important):
When the backend tells you MATCHED PIPING CLASSES = NONE AND the slot
state is not Complete, the user has NOT given you enough to pull real
results yet. In this case you MUST NOT invent or speculate class codes.
Your only job is to ask for the remaining required field(s) clearly. The
frontend renders picker chips for each missing field separately — you
don't need to list every possible value yourself; just name the fields
and maybe give a couple of examples.

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
    "I don't have **Platinum** as a supported material. Did you mean one of
     these? Titanium · CuNi · SS316L · DSS. Pick one and I'll pull the
     matching classes."

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
- Don't refer to the data as a "catalogue" or "catalog" in replies — the
  user doesn't think of it that way. Say "supported", "available", or just
  describe what does/doesn't exist without naming the source.
- When the search returns no matches, just say so plainly (one or two
  sentences). Do NOT volunteer example queries like "try: list 150#
  classes / tell me about A1 / show 600# SS316L" — those are random
  suggestions disconnected from what the user actually asked, and the UI
  already shows the right pickers / next steps.
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
        f"  Service Description: {slots.service or '❓ MISSING'}",
        f"  Complete (all 4 filled): {slots.complete}",
    ]
    slot_block = "\n".join(slot_lines)

    # Field-suggestion context (did-you-mean)
    fs_block = ""
    if field_suggestions:
        fs_lines = []
        for fs in field_suggestions:
            fs_lines.append(
                f"  - {fs.field}: user said '{fs.provided}' which isn't a supported value. "
                f"Nearest valid values: {', '.join(fs.suggestions) if fs.suggestions else '(no close match)'}"
            )
        fs_block = "\nUSER-PROVIDED VALUES THAT AREN'T SUPPORTED:\n" + "\n".join(fs_lines) + "\n"

    context = (
        f"USER PROMPT:\n{prompt}\n\n"
        f"WHAT THE PARSER EXTRACTED:\n{_format_parsed_for_ai(parsed)}\n\n"
        f"SLOT-FILLING STATE (the 3 required fields for generating a PMS):\n{slot_block}\n"
        f"{fs_block}\n"
        f"MATCHED PIPING CLASSES (from the deterministic search):\n"
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

    # Merge slot values carried over from earlier user turns BEFORE matching —
    # so turn 2 of a multi-turn conversation benefits from what the user
    # already said in turn 1. Without this, each turn parses in isolation
    # and slot-filling never converges.
    parsed = _merge_slots_from_history(parsed, req.history)

    # Catalogue-driven auto-fill: if the combo of filled slots narrows
    # a remaining slot to a single catalogue value (e.g. material=GRE
    # → rating=150# and CA=NIL are the only options), fill it silently
    # so the user doesn't see a picker with just one chip.
    parsed = _auto_fill_unique_slots(parsed)

    matches = find_matches(parsed)
    slots = _build_slot_state(parsed, matches)
    field_suggestions = _build_field_suggestions(parsed, req.prompt)

    # Gate: if the user hasn't given us enough to pin down a class
    # (and isn't explicitly browsing / naming a class), suppress the
    # match list so the reply focuses on asking for missing slots. Without
    # this the backend would happily return every 150# class the moment
    # the user types "rating 150" — which defeats the slot-filling UX.
    gated = _should_gate_matches(parsed, slots)
    if gated:
        matches = []

    # Prefer the AI-generated reply; fall back to the deterministic one.
    reply = await _compose_ai_reply(
        req.prompt, parsed, matches, req.history, slots, field_suggestions,
    )
    if not reply:
        reply = _compose_reply(parsed, matches) if not gated else _compose_gated_reply(slots)

    action = _build_action(parsed, matches) if not gated else AgentAction(type="none")
    # Pass the merged `parsed` into available_values so the frontend
    # pickers only show combinations that actually exist in the
    # catalogue — e.g. if user already picked GRE, rating and CA
    # pickers are narrowed to values that co-occur with GRE.
    return PMSAgentResponse(
        reply=reply,
        interpreted=parsed,
        matched_classes=matches,
        suggested_action=action,
        slots=slots,
        field_suggestions=field_suggestions,
        available_values=_available_values(parsed),
        allow_bulk_download=len(matches) > 0,
    )

"""
Data service — reads P-T rating data from embedded JSON.
Only pressure-temperature data and class identifiers are stored locally.
All other PMS data (pipe sizes, fittings, flanges, etc.) comes from AI.
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "pipe_classes.json"

_data: list[dict] | None = None

_RATING_ORDER = ["150#", "300#", "400#", "600#", "900#", "1500#", "2500#", "5000#", "10000#"]
_RATING_INDEX = {rating: idx for idx, rating in enumerate(_RATING_ORDER)}
_SERVICE_TRAIT_PATTERNS: list[tuple[str, str]] = [
    (r"\b(sour|nace|h2s|mr[\s-]?0175|iso[\s-]?15156)\b", "sour"),
    (r"\b(low[\s-]?temp|low temperature|cryogenic|ltcs)\b", "low_temp"),
    (r"\b(raw\s*sea\s*water|sea\s*water|seawater)\b", "seawater"),
    (r"\bfire\s*water\b", "fire_water"),
    (r"\bwater\s*injection\b", "water_injection"),
    (r"\bsteam\b", "steam"),
    (r"\b(hydrocarbon|\bhc\b|oil|diesel|gas|condensate|fuel)\b", "hydrocarbon"),
    (r"\b(chemical|hypochlorite|ferric\s*chloride|coagulant)\b", "chemical"),
    (r"\b(air|nitrogen|instrument)\b", "utility"),
    (r"\bhydraulic\b", "hydraulic"),
    (r"\bsewage\b", "sewage"),
    (r"\bcooling\b", "cooling"),
]


def _load_data() -> list[dict]:
    global _data
    if _data is None:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                _data = json.load(f)
            logger.info("Loaded %d pipe classes from %s", len(_data), DATA_FILE.name)
        else:
            _data = []
            logger.warning("Data file not found: %s", DATA_FILE)
    return _data


def reload_data():
    """Force reload from disk."""
    global _data
    _data = None
    _load_data()


def normalize_rating(rating: str) -> str:
    """Normalize ratings like '150', 'class 150', '150#' -> '150#'."""
    text = (rating or "").upper().replace("CLASS", " ").strip()
    m = re.search(r"\b(10000|5000|2500|1500|900|600|400|300|150)\b", text)
    if not m:
        return (rating or "").strip()
    return f"{m.group(1)}#"


def material_family(material: str) -> str:
    """Collapse project material names to a stable family key."""
    m = (material or "").upper().strip()
    if "6 MO" in m or "6MO" in m:
        return "6MO_TUBING"
    if "TUBING" in m:
        return "TUBING"
    if "SDSS" in m or "S32750" in m or "SUPER DUPLEX" in m:
        return "SDSS"
    if "DSS" in m or "S31803" in m or "S32205" in m or "DUPLEX" in m:
        return "DSS"
    if "316L" in m:
        return "SS316L"
    if re.search(r"\b316\b", m):
        return "SS316"
    if "304L" in m:
        return "SS304L"
    if re.search(r"\b304\b", m):
        return "SS304"
    if "LTCS" in m or "LOW TEMP" in m or "LOW-TEMP" in m or "A333" in m:
        return "LTCS"
    if "GALV" in m:
        return "GALV"
    if "EPOXY" in m or "COATED" in m:
        return "EPOXY"
    if (
        "CUNI" in m or "CU-NI" in m or "COPPER-NICKEL" in m
        or "C70600" in m or "90/10" in m
    ):
        return "CUNI"
    if "COPPER" in m or "C12200" in m:
        return "COPPER"
    if "GRE" in m or "GRV" in m or "BONSTRAND" in m:
        return "GRE"
    if "CPVC" in m:
        return "CPVC"
    if "TITANIUM" in m or re.search(r"\bTI\b", m):
        return "TITANIUM"
    if re.search(
        r"INCONEL|INCOLOY|HASTELLOY|MONEL|ALLOY\s*(625|718|800|825|C-276|C276)|"
        r"UNS\s*N0\d+|N06625|N08825|N10276|N04400|N07718",
        m,
    ):
        return "NICKEL_CRA"
    if "CS" in m or "CARBON STEEL" in m or "A106" in m or "API 5L" in m:
        return "CS"
    return m


def parse_ca_mm(ca: str) -> float:
    """Parse corrosion allowance text into millimetres."""
    text = (ca or "").strip()
    if not text:
        return 0.0
    if "nil" in text.lower() or "none" in text.lower():
        return 0.0
    m = re.search(r"([\d.]+)", text)
    return float(m.group(1)) if m else 0.0


def service_traits(service: str) -> set[str]:
    """Extract coarse service traits for similarity scoring."""
    text = (service or "").lower()
    traits: set[str] = set()
    for pattern, label in _SERVICE_TRAIT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            traits.add(label)
    return traits


def has_nace_trait(
    material: str,
    service: str = "",
    piping_class: str = "",
) -> bool:
    combined = " ".join([material or "", service or "", piping_class or ""]).upper()
    return (
        "NACE" in combined
        or "H2S" in combined
        or "SOUR" in combined
        or combined.endswith("N")
        or combined.endswith("LN")
    )


def has_low_temp_trait(
    material: str,
    service: str = "",
    piping_class: str = "",
) -> bool:
    combined = " ".join([material or "", service or "", piping_class or ""]).upper()
    return (
        "LTCS" in combined
        or "LOW TEMP" in combined
        or "LOW-TEMP" in combined
        or "CRYOGENIC" in combined
        or combined.endswith("L")
        or combined.endswith("LN")
    )


def score_reference_entry(
    *,
    target_piping_class: str,
    target_material: str,
    target_corrosion_allowance: str,
    target_service: str,
    target_rating: str,
    candidate: dict,
) -> float:
    """Rank how well a catalogue or cached entry matches a target request."""
    candidate_class = (candidate.get("piping_class") or "").upper().strip()
    if candidate_class and candidate_class == (target_piping_class or "").upper().strip():
        return float("-inf")

    score = 0.0
    target_family = material_family(target_material)
    candidate_family = material_family(candidate.get("material", ""))

    if target_family == candidate_family:
        score += 45.0
    elif {target_family, candidate_family} <= {"CS", "LTCS", "GALV", "EPOXY"}:
        score += 18.0
    elif {target_family, candidate_family} <= {"NICKEL_CRA", "SDSS"}:
        score += 24.0
    elif {target_family, candidate_family} <= {"NICKEL_CRA", "DSS"}:
        score += 20.0
    elif {target_family, candidate_family} <= {"NICKEL_CRA", "SS316L", "SS316"}:
        score += 16.0
    elif {target_family, candidate_family} <= {"SS316", "SS316L", "SS304", "SS304L", "DSS", "SDSS"}:
        score += 12.0
    elif {target_family, candidate_family} <= {"TUBING", "6MO_TUBING"}:
        score += 18.0

    target_nace = has_nace_trait(target_material, target_service, target_piping_class)
    candidate_nace = has_nace_trait(
        candidate.get("material", ""),
        candidate.get("service", ""),
        candidate_class,
    )
    if target_nace == candidate_nace:
        score += 14.0
    elif target_nace:
        score -= 10.0
    else:
        score -= 8.0

    target_low_temp = has_low_temp_trait(target_material, target_service, target_piping_class)
    candidate_low_temp = has_low_temp_trait(
        candidate.get("material", ""),
        candidate.get("service", ""),
        candidate_class,
    )
    if target_low_temp == candidate_low_temp:
        score += 10.0
    elif target_low_temp:
        score -= 6.0
    else:
        score -= 4.0

    target_ca = parse_ca_mm(target_corrosion_allowance)
    candidate_ca = parse_ca_mm(candidate.get("corrosion_allowance", ""))
    if _normalise_text(target_corrosion_allowance) == _normalise_text(candidate.get("corrosion_allowance", "")):
        score += 12.0
    else:
        score += max(-4.0, 8.0 - abs(target_ca - candidate_ca) * 2.0)

    target_rating_norm = normalize_rating(target_rating)
    candidate_rating_norm = normalize_rating(candidate.get("rating", ""))
    if target_rating_norm and target_rating_norm == candidate_rating_norm:
        score += 28.0
    else:
        target_idx = _RATING_INDEX.get(target_rating_norm)
        candidate_idx = _RATING_INDEX.get(candidate_rating_norm)
        if target_idx is not None and candidate_idx is not None:
            score += max(0.0, 18.0 - abs(target_idx - candidate_idx) * 4.0)
            if _rating_system(target_rating_norm) == _rating_system(candidate_rating_norm):
                score += 8.0

    target_traits = service_traits(target_service)
    candidate_traits = service_traits(candidate.get("service", ""))
    overlap = len(target_traits & candidate_traits)
    score += overlap * 4.0

    if candidate_class and target_piping_class:
        if candidate_class[:1] == target_piping_class[:1]:
            score += 6.0
        if _class_digits(candidate_class) and _class_digits(candidate_class) == _class_digits(target_piping_class):
            score += 4.0

    return score


def select_reference_entries(
    *,
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    rating: str,
    limit: int = 6,
) -> list[dict]:
    """Pick the strongest catalogue anchors for AI generation."""
    scored: list[dict] = []
    for entry in _load_data():
        score = score_reference_entry(
            target_piping_class=piping_class,
            target_material=material,
            target_corrosion_allowance=corrosion_allowance,
            target_service=service,
            target_rating=rating,
            candidate=entry,
        )
        if score == float("-inf"):
            continue
        enriched = dict(entry)
        enriched["_similarity_score"] = round(score, 2)
        scored.append(enriched)

    scored.sort(
        key=lambda e: (
            e.get("_similarity_score", float("-inf")),
            e.get("piping_class", ""),
        ),
        reverse=True,
    )
    return scored[:limit]


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).upper()


def _class_digits(piping_class: str) -> str:
    m = re.search(r"\d+", (piping_class or "").upper())
    return m.group(0) if m else ""


def _rating_system(rating: str) -> str:
    rating = normalize_rating(rating)
    if rating in {"2500#", "5000#", "10000#"}:
        return "API6A"
    if rating in _RATING_INDEX:
        return "ASME"
    return "OTHER"


def get_all_entries() -> list[dict]:
    return _load_data()


def get_index_data() -> list[dict]:
    """Return data for cascading dropdowns."""
    data = _load_data()
    result = []
    for entry in data:
        pt = entry.get("pressure_temperature", {})
        result.append({
            "piping_class": entry["piping_class"],
            "rating": entry.get("rating", ""),
            "material": entry.get("material", ""),
            "corrosion_allowance": entry.get("corrosion_allowance", ""),
            "pt_temperatures": pt.get("temperatures", []),
            "pt_pressures": pt.get("pressures", []),
            "pt_temp_labels": pt.get("temp_labels", []),
        })
    return result


def get_pipe_class_list() -> list[dict]:
    """Return list for browse table (only fields stored in JSON)."""
    data = _load_data()
    return [
        {
            "piping_class": e["piping_class"],
            "rating": e.get("rating", ""),
            "material": e.get("material", ""),
            "corrosion_allowance": e.get("corrosion_allowance", ""),
        }
        for e in data
    ]


def get_available_classes() -> list[str]:
    return [e["piping_class"] for e in _load_data()]


def find_entry(piping_class: str) -> dict | None:
    """Find entry by piping class (case-insensitive)."""
    key = piping_class.upper()
    for entry in _load_data():
        if entry["piping_class"].upper() == key:
            return entry
    return None


def find_by_rating_material(rating: str, material: str) -> dict | None:
    """Find a reference entry with same rating and similar material."""
    data = _load_data()
    mat_upper = material.upper().strip()
    target_family = material_family(mat_upper)

    # Exact match first
    for e in data:
        if e.get("rating") == rating and e.get("material", "").upper().strip() == mat_upper:
            return e

    # Family match
    for e in data:
        if e.get("rating") == rating and material_family(e.get("material", "")) == target_family:
            return e

    # Any with same rating
    for e in data:
        if e.get("rating") == rating:
            return e

    return None

"""
Standards-driven class derivation — the foundation of the "generic PMS"
path.

When a user asks for a piping class that isn't in `pipe_classes.json`,
this module derives the missing pieces from the indexed standards data:

  • Class code, per the §5.5 naming convention from the project PMS
    (40801-SPE-80000-PP-SP-0001 Rev A0, page 18).

  • Pressure-temperature table, per ASME B16.5 Tables 2-1.x by material
    group + rating class (data in app/data/standards/pt_by_class.json,
    accessed via app.services.pt_lookup).

  • Material temperature limits, per the same B16.5 group entry plus
    the project's NACE-cap rule (250 °C max for any sour-service line).

The `derive_synthetic_entry()` function returns a dict that LOOKS like
a `pipe_classes.json` entry, so downstream code (pms_service, ai_service,
the Excel renderer, the JSON cache) can consume it without knowing it
came from derivation rather than the curated catalogue.

This is **Slice 1** scope — supports the 150# CS family today (Group 1.1
data is loaded; other groups are stubs). Future slices add more groups
as their data files land.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ── §5.5 naming-convention tables ──────────────────────────────────
# Rating letter ↔ rating label is owned by `rating_lookup`, which reads
# `app/data/pressure_ratings.json`. This module just imports the lookup;
# it doesn't keep its own copy. The MATERIAL digit table below is still
# local — it lives in lock-step with validation_service._MATERIAL_DIGIT.

from app.services import rating_lookup

# Material+CA fingerprint → §5.5 digit. The §5.5 system encodes CA INTO
# the material digit (digit 1 = CS-3mm CA, digit 2 = CS-6mm CA, etc.),
# so the same material with a different CA is a DIFFERENT digit.
#
# The matcher is normalisation-tolerant: "CS"/"Carbon Steel"/"A106 Gr B"
# all map to "CS"; "3 mm"/"3mm"/"3" all map to "3".
_MATERIAL_DIGIT_RULES: list[tuple[str, str, str]] = [
    # (material_norm, ca_norm, digit)
    ("CS",                 "3",   "1"),
    ("CS",                 "6",   "2"),
    ("CS GALV",            "3",   "3"),
    ("CS GALV",            "1.5", "4"),
    ("CS GALV",            "6",   "5"),
    ("CS INTERNALLY COATED","6",  "6"),
    ("CS - EPOXY LINED",   "6",   "6"),    # catalogue alias for digit 6
    ("SS316",              "NIL", "9"),
    ("SS316L",             "NIL", "10"),
    ("DSS",                "NIL", "20"),
    ("SDSS",               "NIL", "25"),
    ("90/10 CUNI",         "NIL", "30"),
    ("CUNI",               "NIL", "30"),    # alias
    ("COPPER",             "NIL", "40"),
    ("GRE",                "NIL", "50"),
    ("GRV",                "NIL", "51"),
    ("CPVC",               "NIL", "60"),
    ("TITANIUM",           "NIL", "70"),
    ("SS316L/SS316 TUBING","NIL", "80"),
    ("6 MO TUBING",        "NIL", "90"),
]


# ── B16.5 P-T data load ────────────────────────────────────────────
# Routed through `pt_lookup`, which reads `pt_by_class.json` — the
# single source of truth for standards P-T data, organised by
# (rating, group). The drift-check test in tests/ keeps the catalogue
# (`pipe_classes.json`) in sync with this file on every commit.
from app.services import pt_lookup


# Material → B16.5 group mapping. The clean (NACE / LTCS-stripped)
# material name is the key; group keys must match what `pt_by_class.json`
# uses. Adding a new group key here does NOT add any data — the data has
# to land in `pt_by_class.json` first, otherwise lookups for the new
# group fall through to AI-only.
_MATERIAL_TO_GROUP: dict[str, str] = {
    # Group 1.1 — carbon-steel family (NACE substrate is still Group 1.1).
    # `_material_token` strips "NACE" and converts "LTCS" to "CS", so the
    # bare "CS" / "CS GALV" / "CS - EPOXY LINED" entries cover every
    # NACE / LTCS / NACE+LTCS variant naturally.
    "CS":                  "1.1",
    "CS GALV":             "1.1",
    "CS - EPOXY LINED":    "1.1",
    "CS INTERNALLY COATED":"1.1",
    # Group 2.3 — F316L stainless (covers SS316L and SS316L NACE).
    "SS316L":              "2.3",
    # Group 2.8 — Duplex S31803. Project shares this group with Super
    # Duplex S32750 (SDSS) — strict B16.5 puts SDSS in a different group,
    # but the project Excel uses the same curve, so we follow project
    # policy here. If you ever index a separate SDSS group, switch
    # "SDSS" to its own key.
    "DSS":                 "2.8",
    "SDSS":                "2.8",
}


# ── Public API ─────────────────────────────────────────────────────

class DerivationError(ValueError):
    """Raised when an input combination can't be derived from standards.

    Carries a short user-safe message describing what's missing so the
    frontend can show a meaningful prompt instead of a generic 500."""


def _norm(s: str) -> str:
    """Uppercase + collapse whitespace + strip — used for case-insensitive
    matching of material / CA strings."""
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def _ca_token(ca: str) -> str:
    """Extract the canonical CA token: '3 mm' → '3'; 'NIL' → 'NIL'.
    Returns 'NIL' for empty/missing values. Used as the key for
    _MATERIAL_DIGIT_RULES matching."""
    s = _norm(ca)
    if not s or s in ("NIL", "NONE", "0", "0 MM", "0MM"):
        return "NIL"
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return m.group(1) if m else "NIL"


def _material_token(material: str) -> tuple[str, bool, bool]:
    """Strip NACE / LowTemp markers from the material string and return
    (clean_material, is_nace, is_low_temp).

    Examples:
      'CS NACE'      → ('CS', True, False)
      'LTCS NACE'    → ('CS', True, True)   # LTCS implies low-temp
      'CS GALV'      → ('CS GALV', False, False)
      'SS316L NACE'  → ('SS316L', True, False)
    """
    raw = _norm(material)
    is_nace = "NACE" in raw
    is_low_temp = raw.startswith("LTCS")
    cleaned = raw.replace("NACE", "").replace("LTCS", "CS").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Drop trailing parenthetical ("(Valve: SS)" etc.)
    cleaned = re.sub(r"\(.*?\)", "", cleaned).strip()
    return cleaned, is_nace, is_low_temp


def derive_class_code(rating: str, material: str, corrosion_allowance: str) -> str:
    """Derive the §5.5 class code from (rating, material, CA).

    Examples:
      ('150#', 'CS',        '3 mm')  → 'A1'
      ('150#', 'CS NACE',   '6 mm')  → 'A2N'
      ('300#', 'LTCS NACE', '3 mm')  → 'B1LN'
      ('900#', 'DSS',       'NIL')   → 'E20'

    Raises `DerivationError` if the combination doesn't match §5.5 rules.
    """
    # PART 1: rating letter
    rating_letter = rating_lookup.label_to_letter(rating)
    if not rating_letter:
        raise DerivationError(
            f"Unknown rating {rating!r}. §5.5 valid ratings: "
            f"{', '.join(rating_lookup.all_labels())}"
        )

    # PART 2: material digit (encodes both material + CA)
    base_mat, is_nace, is_low_temp = _material_token(material)
    ca_tok = _ca_token(corrosion_allowance)

    digit = None
    for rule_mat, rule_ca, rule_digit in _MATERIAL_DIGIT_RULES:
        if base_mat == _norm(rule_mat) and ca_tok == _norm(rule_ca):
            digit = rule_digit
            break
    if digit is None:
        raise DerivationError(
            f"Material/CA combination not in §5.5 table: "
            f"material={base_mat!r}, CA={ca_tok!r}. "
            f"Add a new material-digit rule if the project standard supports it."
        )

    # Project convention: 6 mm CA on plain CS or LTCS implies sour service.
    # Auto-promote to NACE so (CS, 6 mm) -> A2N, (LTCS, 6 mm) -> A2LN, etc.
    # Scoped to base_mat=="CS" so galv (CS GALV) and coated (CS - Epoxy Lined)
    # at 6 mm CA stay non-NACE — galv/coatings are incompatible with H2S
    # service, and the project Excel keeps A5 / A6 as non-NACE classes.
    if base_mat == _norm("CS") and ca_tok == "6" and not is_nace:
        is_nace = True

    # PART 3: optional suffix (L for low-temp, N for NACE; concatenate as LN)
    suffix = ""
    if is_low_temp:
        suffix += "L"
    if is_nace:
        suffix += "N"

    return f"{rating_letter}{digit}{suffix}"


def derive_p_t_table(
    rating: str,
    material: str,
    is_nace: bool = False,
) -> dict:
    """Look up the B16.5 P-T table for (material_group, rating).

    Routes through `pt_lookup.lookup_pt`, which reads `pt_by_class.json`.
    Returns a dict in the catalogue's `pressure_temperature` shape so
    downstream consumers (`pms_service`, the Excel renderer, the cache)
    don't need translation:
        { temperatures, pressures, temp_labels }

    Raises `DerivationError` when:
      • the cleaned material name has no group mapping in `_MATERIAL_TO_GROUP`
        (e.g. GRE, CPVC, Copper, CuNi, Titanium, tubing — none of these
        live in the standards JSON), OR
      • the (group, rating) pair isn't indexed in `pt_by_class.json` yet
        (e.g. 5000# / 10000# — API 6A territory we haven't populated).

    NACE classes get the project's 250 °C cap applied here, by truncation.
    Same numbers below the cap, no points above. The standards JSON
    itself stays pure — caps are a project-policy layer, not a B16.5
    fact, so we apply them at lookup time rather than baking them in.
    """
    base_mat, mat_is_nace, _is_low_temp = _material_token(material)
    is_nace = is_nace or mat_is_nace

    group_key = _MATERIAL_TO_GROUP.get(base_mat)
    if not group_key:
        raise DerivationError(
            f"No B16.5 group mapping for material {base_mat!r}. "
            f"Standards JSON covers CS / SS316L / DSS / SDSS families "
            f"today; other materials (GRE, CPVC, Cu, CuNi, Ti, tubing) "
            f"are not standards-derivable and need catalogue entries."
        )

    curve = pt_lookup.lookup_pt(group_key, rating)
    if curve is None:
        raise DerivationError(
            f"No P-T data indexed for (group={group_key}, rating={rating!r}). "
            f"Either add it to pt_by_class.json or fall back to AI-only mode."
        )

    # Catalogue shape uses bare keys ("temperatures"/"pressures"); the
    # standards file uses suffixed keys ("temperatures_c"/"pressures_barg").
    # Translate on the way out so we don't propagate the suffixed shape.
    temps = list(curve["temperatures_c"])
    pressures = list(curve["pressures_barg"])
    labels = list(curve.get("temp_labels") or [])

    # NACE 250 °C cap removed at project request — every class now exposes
    # the full underlying curve from pt_by_class.json. Note: SS316L (Group
    # 2.3) and DSS (Group 2.8) entries in the JSON only have data up to
    # 250 °C, so those classes still effectively cap there until 300 °C
    # rows are added to the data file. Group 1.1 (CS / LTCS / NACE / etc.)
    # classes now show the full 7-point curve to 300 °C.

    return {
        "temperatures": temps,
        "pressures":    pressures,
        "temp_labels":  labels,
    }


def derive_synthetic_entry(
    rating: str,
    material: str,
    corrosion_allowance: str,
    service: str = "",
) -> dict:
    """Derive a synthetic catalogue-shaped entry for an uncatalogued class.

    Returns a dict with the same keys as a `pipe_classes.json` entry so
    downstream code (pms_service, AI generator, Excel renderer) can
    consume it identically.

    Sets `_derived: True` so the caller can flag the response as derived
    rather than curated — useful for audit logging and UI indicators.
    """
    cls = derive_class_code(rating, material, corrosion_allowance)
    pt = derive_p_t_table(rating, material)

    return {
        "piping_class":        cls,
        "rating":              rating,
        "material":            material,
        "corrosion_allowance": corrosion_allowance,
        "service":             service or "Generic — derived from §5.5 inputs",
        "pressure_temperature": pt,
        "_derived":            True,
        "_derivation_source":  "B16.5 Group 1.1 + §5.5 naming",
    }


def rating_from_class_code(class_code: str) -> str:
    """Reverse-derive the rating string from a §5.5 class code.

    Examples: 'A1' → '150#'; 'F2N' → '1500#'; 'T80A' → 'Tubing'.

    Used by pms_service when a request comes in for a class not in the
    catalogue — the frontend hands us the class code, we recover the
    rating to feed `derive_p_t_table()`. Returns an empty string if the
    class code's first letter isn't a §5.5 rating letter (caller should
    treat that as a derivation failure).
    """
    cls = (class_code or "").upper().strip()
    if not cls:
        return ""
    return rating_lookup.letter_to_label(cls[0]) or ""


def derive_synthetic_entry_ai_only(
    rating: str,
    material: str,
    corrosion_allowance: str,
    service: str = "",
) -> dict:
    """AI-only synthetic entry — used when the §5.5 class code can be
    derived but the standards data (B16.5 P-T table) isn't indexed for
    this material/rating combination yet.

    Returns a catalogue-shaped entry with EMPTY pressure_temperature.
    The downstream `_build_pms_response` recognises the empty P-T and
    falls back to the user-supplied design pressure / design temperature
    (from the request) for hydrotest computation. The AI generates the
    full P-T table from its own knowledge of relevant standards.

    Tagged with `_ai_only=True` so the response carries an explicit
    "AI-only generation, no standards grounding" marker the frontend
    surfaces to the user.
    """
    cls = derive_class_code(rating, material, corrosion_allowance)
    return {
        "piping_class":        cls,
        "rating":              rating,
        "material":            material,
        "corrosion_allowance": corrosion_allowance,
        "service":             service or "Generic — AI-only generation (no standards data)",
        "pressure_temperature": {
            "temperatures": [],
            "pressures":    [],
            "temp_labels":  [],
        },
        "_derived":            True,
        "_ai_only":            True,
        "_derivation_source":  "§5.5 class code only — no B16.5 / API 6A data",
    }


# Combination-mode constants surfaced to the frontend so it can render
# the right Custom-Class panel state (standards-derived vs AI-only vs
# blocked outright).
MODE_CATALOGUED       = "catalogued"
MODE_STANDARDS        = "standards_derived"
MODE_AI_ONLY          = "ai_only"
MODE_UNSUPPORTED      = "unsupported"


def classify_combination(
    rating: str,
    material: str,
    corrosion_allowance: str,
) -> dict:
    """Determine how a (rating, material, CA) combination should be
    handled. The frontend opt-in flow calls this via
    /api/preview-custom-class to render the right panel state.

    Possible outcomes:
      • MODE_STANDARDS  → §5.5 valid + B16.5 data available → fully
        standards-derived (Slice 1 fast path).
      • MODE_AI_ONLY    → §5.5 valid but no B16.5 data for the rating/
        group → AI generates everything; user takes responsibility.
      • MODE_UNSUPPORTED → §5.5 doesn't recognise the combination
        (e.g. material not in the digit table). Refused outright.

    Returns a dict with `mode`, `class_code` (when derivable), `reason`,
    and any additional context the frontend needs (e.g. the standards
    preview when MODE_STANDARDS)."""
    try:
        cls = derive_class_code(rating, material, corrosion_allowance)
    except DerivationError as e:
        # §5.5 itself can't accept this — definitely unsupported.
        return {
            "mode":       MODE_UNSUPPORTED,
            "class_code": None,
            "reason":     str(e),
        }

    # §5.5 OK. Try the standards-data lookup.
    try:
        pt = derive_p_t_table(rating, material)
        return {
            "mode":       MODE_STANDARDS,
            "class_code": cls,
            "reason":     f"Class {cls} derived from §5.5 + ASME B16.5",
            "pt_preview": pt,
        }
    except DerivationError as e:
        # No B16.5 data for this rating/group combination — fall back to
        # AI-only mode rather than refusing. The frontend shows a clearly
        # different panel for this case (warning colour + "AI-only" tag)
        # so the user knows the output is not standards-grounded.
        return {
            "mode":       MODE_AI_ONLY,
            "class_code": cls,
            "reason":     (
                f"Class {cls} derived from §5.5 naming, but no indexed "
                f"standards data for this rating/material combination. "
                f"AI will generate from its own knowledge of relevant "
                f"standards. Original gap: {e}"
            ),
        }


def is_combination_supported(
    rating: str,
    material: str,
    corrosion_allowance: str,
) -> tuple[bool, str]:
    """Quick "can we derive this?" check for the frontend opt-in flow.

    Returns (True, class_code) if the combination is derivable (in
    EITHER standards or AI-only mode), or (False, error_message) when
    even the §5.5 class code can't be assembled.

    Never raises — frontend uses the boolean and message directly.
    Use `classify_combination` instead if you need to distinguish
    standards-derived vs AI-only modes."""
    result = classify_combination(rating, material, corrosion_allowance)
    if result["mode"] == MODE_UNSUPPORTED:
        return False, result["reason"]
    return True, result["class_code"]

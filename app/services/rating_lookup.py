"""Single source of truth for the project's supported pressure ratings.

Reads `app/data/pressure_ratings.json` and exposes bidirectional lookup
between rating labels ('150#', 'Tubing') and §5.5 letters ('A', 'T').

Replaces three duplicated copies of the same mapping that previously
lived in:
  • app/services/class_derivation.py — `_RATING_LETTER_BY_STR` + inverse
  • app/services/validation_service.py — `_RATING_LETTER`
  • app/static/js/app.js — frontend dropdown and lookup table

When the project adds, removes, or renames a rating, this is the only
file that has to change. Each consumer reads through these functions.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "pressure_ratings.json"


@lru_cache(maxsize=1)
def _data() -> list[dict]:
    """Load the file once per process. Tests / dev reloads call
    `_data.cache_clear()` to pick up edits without restarting."""
    raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return raw.get("ratings") or []


def label_to_letter(label: str | None) -> str | None:
    """'150#' -> 'A'. Returns None for unknown labels.

    Whitespace-tolerant + case-insensitive — '  150#', '150#', '150 #',
    'tubing', 'TUBING' all resolve correctly. Callers can pass user
    input directly without pre-normalising.
    """
    if not label:
        return None
    norm = label.strip().upper().replace(" ", "")
    for r in _data():
        if r["label"].upper().replace(" ", "") == norm:
            return r["letter"]
    return None


def letter_to_label(letter: str | None) -> str | None:
    """'A' -> '150#'. Returns None for unknown letters. Case-insensitive.

    Used for reverse-deriving the rating string from a §5.5 class code,
    e.g. recovering '150#' from class code 'A1' so we can look up its
    P-T table.
    """
    if not letter:
        return None
    norm = letter.strip().upper()
    for r in _data():
        if r["letter"].upper() == norm:
            return r["label"]
    return None


def all_labels() -> list[str]:
    """Every supported rating label, in declaration order — '150#',
    '300#', ..., 'Tubing'. Used by UI dropdowns and error messages
    that list the valid set."""
    return [r["label"] for r in _data()]


def all_letters() -> list[str]:
    """Every §5.5 letter, in declaration order — 'A', 'B', 'D', ...,
    'T'. Used by class-code parsers that need to validate the first
    character against the supported set (e.g. 'C' is intentionally
    absent — it's a PART-3 suffix, not a rating letter)."""
    return [r["letter"] for r in _data()]


def all_pairs() -> list[tuple[str, str]]:
    """[(letter, label)] in declaration order — 'A=150#', 'B=300#', etc.
    Convenience for error messages that print the full mapping."""
    return [(r["letter"], r["label"]) for r in _data()]

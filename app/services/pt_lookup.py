"""Per-(material_group, rating) lookup for ASME B16.5 P-T tables.

Reads `app/data/standards/pt_by_class.json` — the clean B16.5 curve for
each (rating, group) pair, with no project caps applied. Caps (NACE
250°C, galv 150°C, etc.) live in the catalogue and are applied by
truncation, not by replacing values here.

The data file is built incrementally rating-by-rating. Until it covers
every (rating, group) the catalogue uses, lookups for unindexed
combinations return None — callers must treat None as "no standards
data, fall back to catalogue or AI-only".
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "standards" / "pt_by_class.json"


class PTCurve(TypedDict):
    """Shape returned by lookup_pt — same field names as the JSON file
    so callers don't need a translator. Lengths of all three arrays match."""
    temperatures_c: list[float]
    pressures_barg: list[float]
    temp_labels: list[str]


@lru_cache(maxsize=1)
def _data() -> dict:
    """Load the JSON once per process. Tests / dev reloads call
    `_data.cache_clear()` to pick up edits without restarting."""
    with _DATA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _rating_key(rating: str | None) -> str:
    """'600#' -> '600'.  Returns '' for non-numeric ratings (e.g.
    'EEMUA 20 bar') so they fall through to the None branch."""
    r = (rating or "").strip().rstrip("#").strip()
    return r if r.isdigit() else ""


def lookup_pt(material_group: str | None, rating: str | None) -> PTCurve | None:
    """Return the standard P-T curve for a (group, rating) pair, or None.

    None means the file doesn't index that combination yet — caller falls
    back to the catalogue's embedded P-T or to the AI-only path. Project
    caps are NOT applied here; the caller is responsible for layering
    those on top via the catalogue's truncation or a future pt_override.
    """
    if not material_group:
        return None
    group_key = material_group.strip()
    if not group_key:
        return None
    rk = _rating_key(rating)
    if not rk:
        return None
    rating_block = (_data().get("ratings") or {}).get(rk)
    if not rating_block:
        return None
    return (rating_block.get("groups") or {}).get(group_key)


def has_pt(material_group: str | None, rating: str | None) -> bool:
    """Cheap predicate. Same semantics as `lookup_pt(...) is not None`,
    but lets callers branch without binding the curve."""
    return lookup_pt(material_group, rating) is not None


def indexed_combinations() -> list[tuple[str, str]]:
    """Every (rating, group) currently indexed. Useful for tests and for
    a 'what's covered today' admin view as the file grows rating-by-rating."""
    out: list[tuple[str, str]] = []
    for rating, body in (_data().get("ratings") or {}).items():
        for group in (body.get("groups") or {}).keys():
            out.append((rating, group))
    return out

"""Project-conventional schedule floor per (class, NPS) — JSON-driven.

Reads `app/data/standards/project_schedule_floors.json` and exposes
one lookup function:

    floor_for(class_code, nps) -> str | None

Returns the project-mandated minimum schedule key (e.g. '160', 'STD',
'80S') for that (class, size). `None` means either:
  • the matching range explicitly has `"schedule": null` (project spec
    deliberately leaves the WT to Eq. 3a alone for that size), or
  • the class isn't listed in the JSON at all (custom-class derivations,
    AI-only, exotic materials), or
  • the NPS is outside every declared range for the class.

In all three None cases, the post-processor falls back to pure Eq. 3a
for that row — no project-conventional minimum applies.

Used by `pipe_data.correct_pipe_data` as the floor in:
    required_wt = MAX(eq3a_minimum, floor_wt)

So the project floor wins when Eq. 3a is small (typical low-pressure
small-bore lines) and Eq. 3a wins when the design pressure pushes
above the floor (high-pressure / large-bore).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "standards"
    / "project_schedule_floors.json"
)


@lru_cache(maxsize=1)
def _index() -> dict[str, list[dict]]:
    """class_code → [range_rule, …]. Built once per process from the JSON."""
    if not _DATA_PATH.exists():
        return {}
    with _DATA_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    out: dict[str, list[dict]] = {}
    for rule in raw.get("rules") or []:
        ranges = rule.get("ranges") or []
        for cls in rule.get("applies_to") or []:
            out.setdefault(cls.upper(), []).extend(ranges)
    return out


def _nps_to_float(nps) -> float | None:
    """'0.5'/'1.5'/'24'/'1/2'/'1-1/2' → float. None if unparseable."""
    if nps is None:
        return None
    if isinstance(nps, (int, float)):
        return float(nps)
    s = str(nps).strip().replace('"', '').replace("'", '')
    if s in ("1/2",):           return 0.5
    if s in ("3/4",):           return 0.75
    if s in ("1-1/2", "1 1/2"): return 1.5
    if s in ("2-1/2", "2 1/2"): return 2.5
    try:
        return float(s)
    except ValueError:
        return None


def floor_for(class_code: str | None, nps) -> str | None:
    """Project-mandated minimum schedule key for (class, NPS), or None.

    The returned string is a B36.10M / B36.19M schedule KEY as it appears
    in `pipe_dimensions.json` (e.g. '160', 'STD', '80S', 'XS', 'XXS').
    Caller passes it to `lookup_wall_thickness` to resolve to a numeric
    WT. None means "no project floor — use Eq. 3a alone".
    """
    if not class_code:
        return None
    nps_f = _nps_to_float(nps)
    if nps_f is None:
        return None
    rules = _index().get(class_code.upper())
    if not rules:
        return None
    for r in rules:
        if (r["from"] - 1e-6) <= nps_f <= (r["to"] + 1e-6):
            return r.get("schedule")
    return None


def floor_lookup_dict() -> dict[str, list[dict]]:
    """Raw class → ranges dict. Used by API endpoints that mirror the
    floor data to the frontend so the UI can compute the same floor
    the backend uses."""
    return dict(_index())

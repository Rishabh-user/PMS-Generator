"""Schedule + wall-thickness selector for ASME B36.10M pipe.

Given an NPS and a calculated minimum thickness `t_calc_mm`, find the
smallest standard schedule whose nominal wall thickness is ≥ t_calc_mm.
Returns (schedule_label, wall_thickness_mm).

The data lives in `app/data/standards/pipe_dimensions.json` —
specifically the `wall_thicknesses_mm` block, sourced verbatim from
ASME B36.10M-2018 Table 2-1.

Selection rules
---------------
1. Walk the candidate schedules sorted by wall thickness ascending.
2. When two schedules tie on thickness (e.g. SCH 40 = STD = 2.77 mm at
   NPS 0.5"), prefer the entry that appears earlier in the JSON's
   per-NPS object — the JSON is built with identifier aliases (STD /
   XS / XXS) listed BEFORE the matching numeric schedule, so STD wins
   over 40, XS wins over 80, etc. This matches project spec convention.
3. If no schedule has wall ≥ t_calc_mm (i.e. t_calc exceeds even XXS),
   return the thickest available schedule. Caller should treat that
   row as "substandard" — the calculated wall thickness exceeds every
   buyable B36.10M schedule for that NPS.
4. Returns None if NPS isn't in the table or t_calc_mm is unusable.

Usage
-----
    from app.services.schedule_selector import select_schedule_for_thickness

    result = select_schedule_for_thickness(nps='0.5', t_calc_mm=3.601)
    # → ('XS', 3.73)
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable

_DATA_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "standards"
    / "pipe_dimensions.json"
)


@lru_cache(maxsize=1)
def _wt_table() -> dict[str, dict[str, float]]:
    """Load the wall_thicknesses_mm block from pipe_dimensions.json once
    per process. Tests / dev reloads call `_wt_table.cache_clear()`."""
    with _DATA_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    return raw.get("wall_thicknesses_mm") or {}


def _normalise_nps(nps) -> str | None:
    """'0.5', '0.5"', 0.5, '1/2' → '0.5'. Returns None if unparseable."""
    if nps is None:
        return None
    if isinstance(nps, (int, float)):
        nps = str(nps)
    s = str(nps).strip().replace('"', "").replace("'", "")
    if s in ("1/2",):     return "0.5"
    if s in ("3/4",):     return "0.75"
    if s in ("1-1/2", "1 1/2"):     return "1.5"
    if s in ("2-1/2", "2 1/2"):     return "2.5"
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return None


def _format_schedule_label(key: str) -> str:
    """Format a JSON schedule key the way it appears on a PMS sheet:
    'STD'/'XS'/'XXS' as bare identifiers; numeric schedules as 'SCH 40' etc."""
    if key in ("STD", "XS", "XXS"):
        return key
    return f"SCH {key}"


def select_schedule_for_thickness(
    nps,
    t_calc_mm: float,
) -> tuple[str, float] | None:
    """Pick the smallest B36.10M standard schedule whose wall thickness
    meets `t_calc_mm`.

    Returns
    -------
    (schedule_label, wall_thickness_mm) — e.g. ('XS', 3.73), ('SCH 80', 7.62).
    None if NPS is not in the table or `t_calc_mm` is non-numeric / non-positive.
    If `t_calc_mm` exceeds every available schedule, returns the thickest
    one (XXS or SCH 160 typically) — caller should mark the row substandard.
    """
    nps_key = _normalise_nps(nps)
    if not nps_key:
        return None
    try:
        t = float(t_calc_mm)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None

    schedules = _wt_table().get(nps_key)
    if not schedules:
        return None

    # `schedules` preserves JSON insertion order. Stable sort by WT keeps
    # tied entries in their JSON order — STD before 40, XS before 80, etc.
    ordered: list[tuple[str, float]] = sorted(
        schedules.items(), key=lambda kv: kv[1],
    )

    for sched_key, wt in ordered:
        if wt + 1e-6 >= t:
            return _format_schedule_label(sched_key), wt

    # Nothing thick enough — return the thickest available
    sched_key, wt = ordered[-1]
    return _format_schedule_label(sched_key), wt


def lookup_wall_thickness(nps, schedule_key: str | None) -> float:
    """Numeric wall thickness (mm) for (NPS, schedule key), or 0.0 when no
    match. Public helper for callers that already have a schedule KEY (e.g.
    a project-floor lookup returning '160' / 'STD' / 'XS') and just need
    the numeric WT to compare against an Eq. 3a result."""
    if not schedule_key:
        return 0.0
    nps_key = _normalise_nps(nps)
    if not nps_key:
        return 0.0
    return float((_wt_table().get(nps_key) or {}).get(schedule_key, 0.0) or 0.0)

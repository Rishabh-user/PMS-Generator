"""Pipe-data builder — replaces AI-generated pipe_data with a deterministic
build from class_metadata.json.

Responsibilities:
  • For each NPS in the class's size list, emit a row with:
      size_inch, pipe_type, material_spec, ends, id_mm
  • For non-ASME classes (CuNi/Copper/GRE/Titanium): also fill in the
    explicit od_mm + wall_thickness_mm + schedule from the metadata's
    `explicit_dimensions` block.
  • For CPVC (A60): fill in od_mm via ASME B36.10M IPS lookup + WT from
    SCH 80 row of pipe_dimensions.json.
  • For ASME classes (B36.10M / B36.19M): leave od_mm/schedule/wall_thickness_mm
    blank — the caller is expected to run `pipe_data.correct_pipe_data` next,
    which fills them via dual-case Eq. 3a + project floor.

Tubing classes (T80*/T90*) are NOT in class_metadata.json — they have their
own deterministic builder in `app.services.tubing_service`. Calling
`build_pipe_data_rows("T80A")` will raise KeyError (intentional).
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from app.utils.engineering_constants import lookup_od

logger = logging.getLogger(__name__)

_METADATA_PATH = Path(__file__).resolve().parents[1] / "data" / "class_metadata.json"


@lru_cache(maxsize=1)
def _metadata() -> dict:
    """Load class_metadata.json once per process. Tests / dev reloads call
    `_metadata.cache_clear()` to pick up edits without restarting."""
    with _METADATA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def all_class_codes() -> list[str]:
    """Every class code defined in class_metadata.json. Excludes tubing
    (handled by tubing_service.py) — caller can union the two if they want
    the full project class list."""
    return sorted(_metadata().get("classes", {}).keys())


def get_class_meta(class_code: str) -> dict | None:
    """Return the per-class metadata dict, with `profile` references resolved.

    Two flavours of class entry in class_metadata.json:
      • Compact: `{ "profile": "<name>", "sizes": "<group>", ...overrides }`
        — looked up in `pipe_profiles[<name>]` and shallow-merged with the
        per-class entry; class fields win over profile fields.
      • Inline: full entry with all keys (pipe_code, transition_nps,
        small_bore, large_bore, ends, fittings, [explicit_dimensions, ...]).

    Returns None if the class code isn't in the file. The returned dict is
    a fresh shallow copy (safe to read; do not mutate the nested objects
    since they're shared with the cached metadata)."""
    code = class_code.upper()
    cls = _metadata().get("classes", {}).get(code)
    if not cls:
        return None
    profile_name = cls.get("profile")
    if not profile_name:
        return cls  # inline class — nothing to merge
    profile = _metadata().get("pipe_profiles", {}).get(profile_name)
    if not profile:
        raise KeyError(
            f"Class {code!r} references unknown pipe_profile {profile_name!r}"
        )
    # Shallow merge: profile defaults first, class overrides win. The
    # `profile` key itself is dropped — downstream code shouldn't see it.
    merged = {**profile, **{k: v for k, v in cls.items() if k != "profile"}}
    return merged


def get_class_pipe_code(class_code: str) -> str | None:
    """Convenience accessor — returns just the pipe_code field for a
    class (e.g., 'ASME B 36.10M', 'EEMUA 234 20 BAR'). Used to override
    whatever the AI emitted for the pipe_code field on the PMS."""
    cls = get_class_meta(class_code)
    return cls.get("pipe_code") if cls else None


def get_class_pt(class_code: str) -> dict | None:
    """Return the class-specific P-T curve from class_metadata.json's
    `explicit_pt` block, or None.

    Used by /api/pressure-temperature for non-ASME classes (A30 CuNi /
    A40 Copper / A50 + A52 GRE / A51 BONSTRAND) that aren't covered by
    ASME B16.5 P-T tables but have manufacturer / EEMUA / B16.24
    project-rated P-T envelopes baked into the metadata.

    Profile inheritance applies — a class's `explicit_pt` can come from
    its `pipe_profile` (e.g. A50 and A52 both inherit the GRE profile's
    P-T envelope) or be defined inline on the class itself. Inline wins
    if both are present.

    Shape (matches the /api/pressure-temperature response):
        {
          "temperatures": [38, 50, 100],
          "pressures":    [15.5, 15.5, 14.5],
          "temp_labels":  ["0 to 38", "50", "100"]
        }
    """
    cls = get_class_meta(class_code)
    if not cls:
        return None
    pt = cls.get("explicit_pt")
    if not pt:
        return None
    return {
        "temperatures": list(pt.get("temperatures") or []),
        "pressures":    list(pt.get("pressures") or []),
        "temp_labels":  list(pt.get("temp_labels") or []),
    }


def _resolve_sizes(sizes_field, size_groups: dict) -> list[str]:
    """A class's `sizes` is either:
      • a string naming an entry in size_groups (most classes), or
      • an inline array of size strings (rare — class-specific lists).
    Either way, returns a fresh list of NPS strings."""
    if isinstance(sizes_field, list):
        return list(sizes_field)
    if not isinstance(sizes_field, str):
        raise TypeError(f"sizes must be str or list, got {type(sizes_field).__name__}")
    if sizes_field not in size_groups:
        raise KeyError(f"size_group {sizes_field!r} not in class_metadata.json")
    return list(size_groups[sizes_field])


def _is_small_bore(nps: str, transition_nps: float) -> bool:
    """True when this NPS uses the class's `small_bore` MOC + pipe_type."""
    try:
        return float(nps) < float(transition_nps)
    except (TypeError, ValueError):
        return True  # parse failures default to small_bore — safer for fittings


def _round2(x: float | int | None) -> float:
    """Round a numeric value to 2 decimals, returning 0.0 for missing/bad."""
    if x is None:
        return 0.0
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return 0.0


def build_pipe_data_rows(class_code: str) -> list[dict]:
    """Build the pipe_data row list for a class.

    Returns a list of dicts in the same shape the AI used to emit, so
    downstream code (correct_pipe_data, Excel generator, response model)
    can consume it without modification.

    Per-row fields:
      • size_inch, pipe_type, material_spec, ends, id_mm — always populated
      • od_mm, schedule, wall_thickness_mm — populated if class has explicit
        dimensions (non-ASME) OR is CPVC. For ASME classes, left as 0/""
        and the caller must run correct_pipe_data next.

    Raises KeyError for class codes not in class_metadata.json (e.g.,
    tubing classes T80*/T90* which are handled by tubing_service.py).
    """
    cls = get_class_meta(class_code)
    if not cls:
        raise KeyError(
            f"Class {class_code!r} not in class_metadata.json — "
            f"either add it or use a different builder (e.g., "
            f"tubing_service.build_tubing_pms for T80*/T90*)."
        )

    meta = _metadata()
    sizes = _resolve_sizes(cls["sizes"], meta.get("size_groups", {}))
    transition_nps = cls.get("transition_nps", 999)

    explicit = cls.get("explicit_dimensions") or None
    is_cpvc = cls.get("cpvc_uses_asme_od", False)

    # Lazy import: avoids circular dependency if schedule_selector ever needs
    # this module. Only imported when CPVC's WT lookup is actually needed.
    if is_cpvc:
        from app.services.schedule_selector import lookup_wall_thickness as _lookup_wt
    else:
        _lookup_wt = None

    rows: list[dict] = []
    for size in sizes:
        spec = cls["small_bore"] if _is_small_bore(size, transition_nps) else cls["large_bore"]

        row = {
            "size_inch": size,
            "od_mm": 0,
            "schedule": "",
            "wall_thickness_mm": 0,
            "pipe_type": spec["pipe_type"],
            "material_spec": spec["material_spec"],
            "ends": cls["ends"],
            "id_mm": 0.0,
        }

        if explicit:
            # Non-ASME with hand-curated dimensions (CuNi/Copper/GRE).
            row["od_mm"] = _round2(explicit.get("od_mm", {}).get(size))
            row["wall_thickness_mm"] = _round2(explicit.get("wall_thickness_mm", {}).get(size))
            row["schedule"] = explicit.get("schedule", "-")
            id_table = explicit.get("id_mm")
            if id_table is not None:
                row["id_mm"] = _round2(id_table.get(size))
        elif is_cpvc:
            # CPVC: ASME B36.10M IPS ODs + SCH 80 wall thickness.
            row["od_mm"] = _round2(lookup_od(size, pipe_code="ASME B 36.10M"))
            sched_label = cls.get("cpvc_schedule", "SCH 80")
            row["schedule"] = sched_label
            sched_key = sched_label.replace("SCH ", "").strip()
            row["wall_thickness_mm"] = _round2(_lookup_wt(size, sched_key))
        # ASME classes: row['od_mm'], 'schedule', 'wall_thickness_mm' stay
        # at 0/'' — correct_pipe_data fills them in via dual-case Eq. 3a.

        rows.append(row)

    return rows

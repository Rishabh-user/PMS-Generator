"""Pre-generated PMS class catalog — replaces the per-request AI call.

Architecture
------------
The PMS generation pipeline used to call Anthropic for every cache miss.
That cost ~1.5 minutes per call and ~$0.50 per request. This catalog
replaces that with a one-time AI batch run whose output is committed to
disk as `app/data/classes/{class_code}.json`. Production lookups become
a millisecond-scale file read.

  spec file (this directory's `app/data/standards/class_catalog_spec.json`)
    │   91 entries → (rating, material, CA, service) per class code
    ▼
  scripts/generate_class_catalog.py
    │   one-time: iterates spec, calls AI for each, writes JSON to disk
    ▼
  app/data/classes/{A1,A1N,B1,…,T90C}.json
    │   permanent on-disk PMS bodies
    ▼
  class_catalog.lookup_pms(class_code)  ← THIS MODULE
    │
    ▼
  pms_service.generate_pms()  ← reads catalog instead of calling AI

When a class isn't materialised yet (catalog file missing), `lookup_pms`
returns `None`. The caller is expected to surface a clear error rather
than silently fall back to AI — the whole point of moving to the catalog
is to remove the AI dependency from the hot path.

Adding a new class
------------------
1. Append the entry to `class_catalog_spec.json`.
2. Run `python scripts/generate_class_catalog.py --only NEWCODE`.
3. The new file lands in `app/data/classes/NEWCODE.json`; from there,
   production reads it like any other class.

If the AI prompt rules change (new fittings standard, updated NACE rule),
delete the affected files and re-run the script for those codes only —
the script is idempotent and skips files that already exist.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

_CATALOG_DIR = Path(__file__).resolve().parents[1] / "data" / "classes"
_SPEC_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "standards"
    / "class_catalog_spec.json"
)


class ClassSpec(TypedDict):
    """Shape of one entry in `class_catalog_spec.json`. The four parameters
    are exactly what `pms_service.generate_pms()` already takes — so the
    spec doubles as a "what AI inputs would produce this class" record."""
    code: str
    rating: str
    material: str
    corrosion_allowance: str
    service: str


@lru_cache(maxsize=1)
def _load_spec() -> dict[str, ClassSpec]:
    """Load class_catalog_spec.json once and index by code."""
    if not _SPEC_PATH.exists():
        logger.warning("class_catalog_spec.json not found at %s", _SPEC_PATH)
        return {}
    with _SPEC_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    return {entry["code"].upper(): entry for entry in (raw.get("classes") or [])}


def all_class_codes() -> list[str]:
    """Every class code listed in the spec, sorted alphabetically. Used by
    the generation script to know what to materialise, and by the API
    `/pipe-classes/codes` endpoint to populate frontend dropdowns."""
    return sorted(_load_spec().keys())


def get_spec(class_code: str) -> ClassSpec | None:
    """Return the (rating, material, CA, service) tuple recorded for a
    class. Returns None when the class isn't in the spec at all — i.e.
    not just "not yet materialised", but "this project doesn't define
    that class". Used by the generation script and by validators."""
    return _load_spec().get(class_code.upper())


def lookup_pms(class_code: str) -> dict | None:
    """Read the materialised PMS body for a class from disk.

    Returns the parsed JSON dict (the same shape AI used to produce —
    `pipe_data`, `fittings`, `flange`, `valves`, `notes`, etc.) or None
    if the file doesn't exist yet.

    The post-processor (`pipe_data.correct_pipe_data`) still runs over
    the result before it's returned to the user — OD and schedule for
    ASME classes always come from `pipe_dimensions.json` + project
    floors, not from the catalog. So if the AI prompt's pipe-dimension
    rules drift, only the engineering values diverge — the catalog stays
    valid.
    """
    path = _CATALOG_DIR / f"{class_code.upper()}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read catalog file %s: %s", path, e)
        return None


def materialised_class_codes() -> list[str]:
    """Class codes that actually have a JSON file on disk (i.e. were
    pre-generated). Useful for /admin endpoints to surface "you haven't
    generated these yet" gaps."""
    if not _CATALOG_DIR.exists():
        return []
    return sorted(p.stem for p in _CATALOG_DIR.glob("*.json"))


def missing_class_codes() -> list[str]:
    """Spec entries that DON'T have a corresponding JSON file. The
    generation script defaults to running over this list; pass `--all`
    to force regeneration of everything."""
    have = set(materialised_class_codes())
    return [c for c in all_class_codes() if c not in have]


def has_pms(class_code: str) -> bool:
    """Cheap predicate. Same semantics as `lookup_pms(...) is not None`,
    but lets callers branch without binding the dict."""
    return (_CATALOG_DIR / f"{class_code.upper()}.json").exists()

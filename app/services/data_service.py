"""
Data service — reads P-T rating data from embedded JSON.
Only pressure-temperature data and class identifiers are stored locally.
All other PMS data (pipe sizes, fittings, flanges, etc.) comes from AI.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "pipe_classes.json"

_data: list[dict] | None = None


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

    def mat_family(m: str) -> str:
        m = m.upper().strip().replace(" NACE", "")
        if "GALV" in m or "EPOXY" in m:
            return "CS"
        return m

    target_family = mat_family(mat_upper)

    for e in data:
        if e.get("rating") == rating and e.get("material", "").upper().strip() == mat_upper:
            return e

    for e in data:
        if e.get("rating") == rating and mat_family(e.get("material", "")) == target_family:
            return e

    for e in data:
        if e.get("rating") == rating:
            return e

    return None

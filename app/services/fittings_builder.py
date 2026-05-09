"""Fittings builder — replaces AI-generated fittings_by_size with a
deterministic build from class_metadata.json + industry-standard rules.

Architecture matches pipe_data_builder.py:

  app/data/class_metadata.json
    │   • fitting_groups: per-family small/large-bore MOC + fitting_type
    │   • fitting_standards.default: elbow/tee/reducer/cap/plug/weldolet
    │     constants that apply across every ASME class
    │   • classes[code].fittings: which fitting_group the class uses
    │   • classes[code].fittings_transition_nps (optional override —
    │     defaults to the same transition_nps as pipe_data_builder)
    ▼
  build_fittings_by_size(class_code) → list[dict]
    │   one entry per NPS in the class's size list, with:
    │     size_inch, type ('Seamless' or 'Welded'),
    │     fitting_type, material_spec,
    │     elbow_standard, tee_standard, reducer_standard, cap_standard,
    │     plug_standard, weldolet_spec
    │
    ▼
  pms_service overrides ai_data['fittings_by_size'] with the result;
  Excel generator + frontend continue to read this field unchanged.

Industry-standards basis (the "why"):
  • Seamless typically maxes out at ~18" NPS due to billet-piercing
    manufacturing limits — larger diameters use welded pipe (LSAW/EFW),
    so fittings follow the same split. Project specs may put the
    transition at 14"/18"/20" depending on availability and cost.
  • The split material spec for B/D/E-series CS reflects A 234 Gr. WPB
    being unsuitable for welded fittings ≥ NPS 14 — A 420 Gr. WPL6 is
    the standard substitute. F/G-series 1500/2500 use API 5L X60 for
    pipe, which pairs with ASTM A 860 WPHY 60 for fittings.
  • DSS / SDSS use ASTM A 815 with two grades: WP-S (seamless) and
    WP-WX (welded). Same UNS, different forming process.
  • Standards (B 16.9 / B 16.11 / MSS SP 97) are global constants —
    they don't change by class.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from app.services.pipe_data_builder import (
    _metadata,
    _resolve_sizes,
    _is_small_bore,
    get_class_meta,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _fitting_groups() -> dict:
    """Lazy lookup so a metadata reload picks up new fitting_groups
    without restart (call _metadata.cache_clear() first)."""
    return _metadata().get("fitting_groups") or {}


@lru_cache(maxsize=1)
def _fitting_standards_default() -> dict:
    return (_metadata().get("fitting_standards") or {}).get("default") or {}


def _bore_label(is_small: bool) -> str:
    """Match the AI's previous output style — 'Seamless' or 'Welded'."""
    return "Seamless" if is_small else "Welded"


def _resolve_fitting_group(cls_meta: dict) -> dict | None:
    """Read the class's fitting_group reference and look it up. Returns
    None for classes with no fitting metadata (uncommon — usually means
    the class isn't in the metadata file or hasn't been wired yet)."""
    group_name = cls_meta.get("fittings")
    if not group_name:
        return None
    return _fitting_groups().get(group_name)


def build_fittings_by_size(class_code: str) -> list[dict]:
    """Build the fittings_by_size array for a class.

    One entry per NPS in the class's size list, populated with the
    small_bore or large_bore values from the fitting_group. Standards
    fields (elbow/tee/reducer/cap/plug/weldolet) come from the global
    fitting_standards.default — weldolet_spec gets the row's material_spec
    interpolated in.

    Returns [] if the class isn't in class_metadata.json (caller should
    fall back to AI-emitted fittings_by_size in that case).
    """
    cls = get_class_meta(class_code)
    if not cls:
        logger.warning("Class %s not in class_metadata.json — cannot build fittings", class_code)
        return []

    group = _resolve_fitting_group(cls)
    if not group:
        logger.warning(
            "Class %s has no fitting_group reference in class_metadata.json — "
            "fittings_by_size will be empty", class_code,
        )
        return []

    meta = _metadata()
    sizes = _resolve_sizes(cls["sizes"], meta.get("size_groups", {}))
    transition_nps = cls.get("fittings_transition_nps", cls.get("transition_nps", 999))
    standards = _fitting_standards_default()
    weldolet_template = standards.get("weldolet_template", "MSS SP 97, {material_spec}")

    rows: list[dict] = []
    for size in sizes:
        is_small = _is_small_bore(size, transition_nps)
        bore = group["small_bore"] if is_small else group["large_bore"]
        material_spec = bore.get("material_spec", "")

        rows.append({
            "size_inch": size,
            "type": _bore_label(is_small),
            "fitting_type": bore.get("fitting_type", ""),
            "material_spec": material_spec,
            "elbow_standard":   standards.get("elbow_standard", ""),
            "tee_standard":     standards.get("tee_standard", ""),
            "reducer_standard": standards.get("reducer_standard", ""),
            "cap_standard":     standards.get("cap_standard", ""),
            "plug_standard":    standards.get("plug_standard", ""),
            "weldolet_spec":    weldolet_template.format(material_spec=material_spec),
            # Extras default to empty — class_metadata can override later
            # if a class needs e.g. coupling_standard or sockolet_standard.
            # Currently no class populates these via the builder; the
            # AI used to fill them only for non-standard cases.
            "coupling_standard":  "",
            "union_standard":     "",
            "sockolet_standard":  "",
            "nipple_standard":    "",
            "swage_standard":     "",
            "mold_tee_standard":  "",
            "red_saddle_standard": "",
            "adaptor_standard":   "",
        })
    return rows


def has_fitting_metadata(class_code: str) -> bool:
    """Cheap predicate — does the class have a fitting_group wired?"""
    cls = get_class_meta(class_code)
    return bool(cls and cls.get("fittings") and _fitting_groups().get(cls["fittings"]))

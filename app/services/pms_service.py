"""
Core PMS service — orchestrates P-T lookup from JSON, DB cache, and AI generation.

Flow:
  1. Check in-memory cache (L1) and PostgreSQL cache (L2)
  2. If cached, return instantly
  3. If not cached, call Claude AI to generate all fields
  4. Correct wall thickness using ASME lookup tables
  5. Store result in both DB and memory cache
  6. Regenerate endpoint bypasses cache and forces fresh AI call
"""
import logging
import re
from pathlib import Path

from app.models.pms_models import (
    PMSRequest, PMSResponse, PressureTemperature,
    PipeSize, FittingsData, FittingBySize, ExtraFittings, FlangeData,
    SpectacleBlind, BoltsNutsGaskets, ValveData, ValveSizeEntry,
)
from app.services.ai_service import generate_pms_with_ai, generate_class_code_with_ai, AIGenerationError
from app.services.rag_service import retrieve_context
from app.services.branch_chart_service import get_charts_for_class
from app.services.excel_generator import generate_pms_excel_bytes
from app.services.tubing_service import build_tubing_pms, is_tubing_class
from app.services import data_service
from app.services import db_service
from app.services import valvesheet_sync_service
from app.utils.pipe_data import correct_pipe_data
from app.utils.engineering_constants import HYDROTEST_FACTOR, MILL_TOLERANCE_PERCENT

logger = logging.getLogger(__name__)

# L1 (in-memory) PMS cache — unbounded plain dict, NO TTL and NO size cap.
# Rationale per the project owner's directive: an entry should only ever
# change when the same (piping_class, material, CA, service) combination
# is re-generated — in which case `_store_in_caches` overwrites the
# existing key. It should NEVER silently disappear due to age or size
# pressure. The L2 PostgreSQL cache already has no expiry either, so
# both layers are permanent unless something explicitly deletes them
# (the Admin UI trash button, or POST /api/clear-cache).
#
# Earlier versions used cachetools.TTLCache with a 1-hour TTL + 256-entry
# cap, which is why users saw the in-memory cache appear to "empty
# itself" over time. The DB entries were never lost — but once the L1
# entry expired, the next request took the slower L2 path.
_pms_cache: dict[str, PMSResponse] = {}


def _norm(s: str) -> str:
    """Normalise a string for use in a composite cache key."""
    return re.sub(r'\s+', ' ', (s or '').strip()).upper()


def _clip(text: str, limit: int = 120) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _size_sort_key(size: str) -> float:
    try:
        return float(str(size).replace('"', '').strip())
    except (TypeError, ValueError):
        return 1e9


def _format_size_span(start: str, end: str) -> str:
    return f'{start}"' if start == end else f'{start}"-{end}"'


def _compress_pipe_runs(
    pipe_rows: list[dict],
    *,
    value_builder,
    max_groups: int = 5,
) -> str:
    rows = sorted(
        [row for row in pipe_rows if isinstance(row, dict) and row.get("size_inch")],
        key=lambda row: _size_sort_key(row.get("size_inch", "")),
    )
    if not rows:
        return ""

    groups: list[str] = []
    current_value = value_builder(rows[0])
    start = end = str(rows[0].get("size_inch", ""))

    for row in rows[1:]:
        row_value = value_builder(row)
        size = str(row.get("size_inch", ""))
        if row_value == current_value:
            end = size
            continue
        groups.append(f"{_format_size_span(start, end)} => {current_value}")
        current_value = row_value
        start = end = size

    groups.append(f"{_format_size_span(start, end)} => {current_value}")
    if len(groups) > max_groups:
        return " | ".join(groups[:max_groups]) + " | …"
    return " | ".join(groups)


async def _select_cached_pattern_examples(
    *,
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    rating: str,
    limit: int = 2,
) -> list[dict]:
    """Select the strongest cached PMS payloads to use as AI pattern anchors."""
    if not db_service.is_available():
        return []

    rows = await db_service.list_cached_pms_examples(limit=80)
    scored: list[dict] = []
    for row in rows:
        payload = row.get("response_json") or {}
        if not isinstance(payload, dict):
            continue
        candidate = {
            "piping_class": row.get("piping_class", ""),
            "rating": payload.get("rating", ""),
            "material": row.get("material", ""),
            "corrosion_allowance": row.get("corrosion_allowance", ""),
            "service": row.get("service", ""),
        }
        score = data_service.score_reference_entry(
            target_piping_class=piping_class,
            target_material=material,
            target_corrosion_allowance=corrosion_allowance,
            target_service=service,
            target_rating=rating,
            candidate=candidate,
        )
        if score == float("-inf"):
            continue
        enriched = dict(row)
        enriched["_similarity_score"] = round(score, 2)
        scored.append(enriched)

    scored.sort(
        key=lambda row: (
            row.get("_similarity_score", float("-inf")),
            row.get("updated_at") or "",
        ),
        reverse=True,
    )
    return scored[:limit]


def _summarize_cached_pattern(example: dict) -> str:
    payload = example.get("response_json") or {}
    pipe_rows = payload.get("pipe_data") or []
    sizes = [str(row.get("size_inch", "")) for row in pipe_rows if isinstance(row, dict) and row.get("size_inch")]
    size_span = (
        f'{sizes[0]}"-{sizes[-1]}" ({len(sizes)} sizes)'
        if len(sizes) >= 2
        else (f'{sizes[0]}" (1 size)' if sizes else "n/a")
    )
    schedule_pattern = _compress_pipe_runs(
        pipe_rows,
        value_builder=lambda row: (row.get("schedule") or "-").strip() or "-",
    ) or "n/a"
    transition_pattern = _compress_pipe_runs(
        pipe_rows,
        value_builder=lambda row: _clip(
            " / ".join(
                part for part in [
                    row.get("pipe_type", ""),
                    row.get("material_spec", ""),
                    row.get("ends", ""),
                ]
                if part
            ) or "n/a",
            72,
        ),
        max_groups=3,
    ) or "n/a"
    flange = payload.get("flange") or {}
    fittings = payload.get("fittings") or {}
    notes = payload.get("notes") or []

    return (
        f"- {example.get('piping_class', '?')} "
        f"(score {example.get('_similarity_score', 0):.1f}) | "
        f"{payload.get('rating', '?')} | {example.get('material', '?')} | "
        f"CA {example.get('corrosion_allowance', '?')} | "
        f"sizes {size_span}\n"
        f"  schedules: {schedule_pattern}\n"
        f"  pipe/transitions: {_clip(payload.get('pipe_code', ''), 54) or 'n/a'} | {transition_pattern}\n"
        f"  fittings/flange: {_clip(fittings.get('fitting_type', ''), 42) or 'n/a'} | "
        f"{_clip(flange.get('standard', ''), 34) or 'n/a'} | "
        f"{_clip(flange.get('face_type', ''), 34) or 'n/a'} | notes={len(notes)}"
    )


def _build_pattern_guidance(
    *,
    req: PMSRequest,
    piping_class: str,
    rating: str,
    reference_entries: list[dict],
    cached_examples: list[dict],
) -> str:
    """Build a compact inference guide for unmatched or custom requests."""
    normalized_rating = data_service.normalize_rating(rating)
    target_family = data_service.material_family(req.material)
    pressure_system = (
        "API 6A high-pressure"
        if normalized_rating in {"2500#", "5000#", "10000#"}
        else "ASME B16.5 / B31.3"
    )
    target_traits: list[str] = []
    if data_service.has_nace_trait(req.material, req.service, piping_class):
        target_traits.append("NACE")
    if data_service.has_low_temp_trait(req.material, req.service, piping_class):
        target_traits.append("Low Temperature")
    service_traits = sorted(data_service.service_traits(req.service))

    lines = [
        "Target inference fingerprint:",
        (
            f"family={data_service.material_family(req.material)} | rating={rating} | "
            f"pressure_system={pressure_system} | CA={req.corrosion_allowance or 'NIL'} | "
            f"suffix_traits={', '.join(target_traits) if target_traits else 'standard'} | "
            f"service_traits={', '.join(service_traits) if service_traits else 'general'}"
        ),
        "If an exact rule is missing, infer in this order:",
        "1. Closest same-family + same-suffix examples (NACE / LT / material family).",
        "2. Nearest-rating examples within the same pressure system.",
        "3. Retrieved RAG standards excerpts for the final material, flange, fitting, and valve wording.",
        "4. Produce the full PMS JSON anyway; only note assumptions when examples genuinely diverge.",
    ]

    if target_family == "NICKEL_CRA":
        lines.append(
            "Unsupported material-family handling: this request is a nickel-based CRA "
            "(e.g. Inconel/Hastelloy/Monel family). Reuse the closest corrosion-resistant "
            "project pattern for structure and schedules, preferring SDSS first, then DSS, "
            "then SS316L if needed; keep all emitted MOC/spec strings faithful to the "
            "requested alloy and use RAG to refine ASTM/ASME wording."
        )

    if reference_entries:
        lines.append("Closest catalogue anchors:")
        for entry in reference_entries[:4]:
            lines.append(
                f"- {entry.get('piping_class', '?')} | {entry.get('rating', '?')} | "
                f"{entry.get('material', '?')} | CA {entry.get('corrosion_allowance', '?')} | "
                f"service {_clip(entry.get('service', ''), 84) or 'n/a'} | "
                f"score {entry.get('_similarity_score', 0):.1f}"
            )

    if cached_examples:
        lines.append("Closest full PMS anchors from cache:")
        for example in cached_examples[:2]:
            lines.append(_summarize_cached_pattern(example))
    else:
        lines.append(
            "No cached full PMS anchors are available in pms_cache for this request; "
            "lean more heavily on the catalogue anchors plus the retrieved RAG context."
        )

    return "\n".join(lines)


def _apply_hydrotest(pms: PMSResponse) -> PMSResponse:
    """Enforce the mandatory hydrotest rule on any PMSResponse (fresh or cached).
    P_hydrotest = pressures[0] × 1.5  — the first (coldest) P-T column.
    Mutates pms.hydrotest_pressure in-place and returns pms."""
    pressures = pms.pressure_temperature.pressures if pms.pressure_temperature else []
    non_zero = [p for p in pressures if (p or 0) > 0]
    if non_zero:
        pms.hydrotest_pressure = str(round(non_zero[0] * HYDROTEST_FACTOR, 2))
    return pms


def _cache_key(req: PMSRequest) -> str:
    """Composite cache key: class || material || CA || service (all normalised).

    A change in ANY of the four parameters produces a different key, so the
    backend never serves a cached entry that doesn't match the exact request.
    The display class code (just the class part) is stored separately in
    display_class so the admin UI still shows a clean code, not the full key.
    """
    return "||".join([
        _norm(req.piping_class),
        _norm(req.material),
        _norm(req.corrosion_allowance),
        _norm(req.service),
    ])


# Classes whose pipe-size table extends past NPS 24 (B16.5's upper bound).
# For these classes the flange-Standard cell must cite both B16.5 (for sizes
# ≤24") AND B16.47A (for sizes ≥26") because a single B16.5 reference is
# wrong above 24". Source-of-truth list comes from the AI prompt's "PIPE
# SIZES — STANDARD NPS RANGES" block; cross-check before adding entries.
# A50/A52/A51 (GRE) handle their own dimensional reference string in the
# AI prompt and are deliberately excluded here.
_FLANGE_NEEDS_B1647_DUAL: frozenset[str] = frozenset({
    "A1", "A1N",     # CS 150# — sizes to 36"
    "A1L", "A1LN",   # LTCS 150# — sizes to 30"
    "A2N", "A2LN",   # CS NACE 150# — sizes to 30"
    "A20", "A20N",   # DSS 150# — sizes to 32"
    "B20",           # DSS 300# — sizes to 32"
    "B25",           # SDSS 300# — sizes to 32"
    "A30",           # CuNi EEMUA 234 — sizes to 28"
})


# Classes whose flange MOC must be A105N (B16.5 Group 1.1 forging), not the
# B16.47 pipeline-flange material A694 F60. Earlier prompt revisions told
# the AI to use A694 F60 for all 1500#/2500# CS forgings; that's wrong
# because none of those classes exceeds NPS 24, so they live entirely
# inside B16.5 territory where Table 1A specifies A105 / A350 LF2 / A182 F1.
# A694 F60 still belongs to the hub_connector row (which is a B16.47-style
# component) — that field is not touched by this enforcer.
_FLANGE_FORCE_A105N: frozenset[str] = frozenset({
    "F1", "G1",       # 1500# / 2500# CS — to 24"
    "F2N", "G2N",     # 1500# / 2500# CS NACE — to 24"
})


def _enforce_flange_material(piping_class: str, ai_value: str) -> str:
    """Override A694 F60 with A105N for 1500#/2500# CS classes ≤24".

    Same defensive pattern as `_enforce_flange_standard`: we only
    intervene when the AI emitted exactly the wrong material for one
    of the affected classes. Any other string (custom MOC, project
    override, NACE qualifier added by the AI) passes through.
    """
    cls = (piping_class or "").upper().strip()
    value = (ai_value or "").strip()
    if cls not in _FLANGE_FORCE_A105N:
        return value
    if "A 694" in value.upper() or "A694" in value.upper():
        upgraded = "ASTM A 105N"
        logger.info(
            "Flange.material_spec for %s upgraded from %r to %r "
            "(B16.5 Table 1A Group 1.1 — A694 is a B16.47 material)",
            cls, value, upgraded,
        )
        return upgraded
    return value


def _enforce_flange_standard(piping_class: str, ai_value: str) -> str:
    """Make sure the flange.standard string is correct for the class.

    The AI prompt now teaches the dual-citation rule for classes that
    span past NPS 24, but model output drifts and a regenerated cache
    entry from before the prompt update can still carry a bare
    "ASME B16.5". This is a last-line server-side enforcer: if the
    class is on the >24" list AND the AI's emitted value doesn't
    already mention B16.47, upgrade the string to the dual citation.

    We only intervene when the AI value is empty OR cites only B16.5
    without B16.47 — anything else (GRE classes, manufacturer-standard
    strings, edge cases) passes through untouched so the AI's intent
    isn't second-guessed.
    """
    cls = (piping_class or "").upper().strip()
    value = (ai_value or "").strip()
    if cls not in _FLANGE_NEEDS_B1647_DUAL:
        return value
    # Already cites B16.47 (in any form) → trust it.
    norm = value.upper().replace(" ", "")
    if "16.47" in norm or "B1647" in norm:
        return value
    # Empty / bare B16.5 / something we don't recognise → upgrade.
    upgraded = "ASME B16.5 / B16.47A"
    if value:
        logger.info(
            "Flange.standard for %s upgraded from %r to %r "
            "(class spans >24\"; B16.47A required per B16.5 §1.1)",
            cls, value, upgraded,
        )
    return upgraded


def _determine_class_type(piping_class: str) -> str:
    """Determine the class type from the piping class name."""
    cls = piping_class.upper()
    if cls.startswith("T"):
        return "tubing"
    if cls.startswith("A30"):
        return "cuni"
    if cls.startswith("A40"):
        return "copper"
    if any(cls.startswith(pfx) for pfx in ["A50", "A51", "A52"]):
        return "gre"
    if cls.startswith("A60"):
        return "cpvc"
    if cls.startswith("A70"):
        return "titanium"
    if any(cls == pfx or cls.startswith(pfx) and len(cls) == len(pfx)
           for pfx in ["A3", "A4", "A5", "A6", "B4", "D4"]):
        return "galv_screwed"
    return "standard"


def _normalize_pt_table(pt_data: dict) -> dict:
    """Fix common AI mistakes in P-T arrays so all three lists have equal length.

    Known mistakes this corrects:
    1. -29 (or -46) listed as a standalone temperatures entry instead of only
       appearing in the first temp_label string → it gets dropped and the next
       value (50) becomes the first entry.
    2. temp_labels shorter than temperatures/pressures (AI grouped -29+50 into
       one label but still emitted both as separate temperature entries).
    3. Duplicate consecutive temperature values (e.g. 121 appearing twice).
    4. pressures and temperatures counts differ.
    """
    temps = [float(t) for t in pt_data.get("temperatures", [])]
    presses = [float(p) for p in pt_data.get("pressures", [])]
    labels = list(pt_data.get("temp_labels", []))

    if not temps:
        return pt_data

    # Step 1: remove the -29 / -46 standalone entry when it causes a mismatch.
    # Heuristic: if first temp is ≤ -29 AND len(temps) == len(labels) + 1 AND
    # the first label already contains "TO" (meaning it spans the range), the
    # -29 is redundant.
    if (len(temps) == len(labels) + 1
            and temps[0] <= -29
            and labels
            and "TO" in labels[0].upper()):
        temps = temps[1:]
        presses = presses[1:] if len(presses) > len(labels) else presses

    # Step 2: deduplicate consecutive duplicate temperatures (e.g. [100, 121, 121]).
    deduped_temps: list[float] = []
    deduped_presses: list[float] = []
    for i, t in enumerate(temps):
        if deduped_temps and t == deduped_temps[-1]:
            continue
        deduped_temps.append(t)
        if i < len(presses):
            deduped_presses.append(presses[i])

    # Step 3: align pressures length to temps (truncate or pad with last value).
    n = len(deduped_temps)
    while len(deduped_presses) < n:
        deduped_presses.append(deduped_presses[-1] if deduped_presses else 0.0)
    deduped_presses = deduped_presses[:n]

    # Step 4: align temp_labels length to n.
    while len(labels) < n:
        t_val = deduped_temps[len(labels)]
        labels.append(str(int(t_val)) if t_val == int(t_val) else str(t_val))
    labels = labels[:n]

    return {
        "temperatures": deduped_temps,
        "pressures": deduped_presses,
        "temp_labels": labels,
    }


def _build_pms_response(entry: dict, ai_data: dict, req: PMSRequest, piping_class_override: str | None = None) -> PMSResponse:
    """Merge JSON P-T data with AI-generated fields into a PMSResponse."""
    pt_data = entry.get("pressure_temperature", {})

    # Only for 5000# / 10000# and any other custom-rated class (req.custom_rating
    # is set): the catalogue has no P-T entry, so the AI generates it.
    # Run the normalizer to fix common AI array-length mistakes before use.
    # Catalogue classes (A/B/D/E/F/G series with recorded P-T) are never touched.
    if req.custom_rating and not pt_data.get("temperatures") and ai_data.get("pressure_temperature"):
        pt_data = _normalize_pt_table(ai_data["pressure_temperature"])

    pt = PressureTemperature(
        temperatures=pt_data.get("temperatures", []),
        pressures=pt_data.get("pressures", []),
        temp_labels=pt_data.get("temp_labels", []),
    )

    # Hydrotest pressure — mandatory rule for all classes:
    #   P_hydrotest = first pressure in P-T table × 1.5
    # The first column is the cold/base rating (at -29°C or lowest listed
    # temperature), which is the MAWP at ambient test conditions.
    pressures = pt_data.get("pressures", [])
    if pressures:
        base_p = pressures[0]
        hydrotest_str = str(round(base_p * HYDROTEST_FACTOR, 2))
        logger.info(
            "Hydrotest for %s: %.2f barg (= %.4g × 1.5)",
            req.piping_class, float(hydrotest_str), base_p,
        )
    else:
        hydrotest_str = ai_data.get("hydrotest_pressure", "")

    pipe_data = []
    for p in ai_data.get("pipe_data", []):
        pipe_data.append(PipeSize(
            size_inch=str(p.get("size_inch", "")),
            od_mm=float(p.get("od_mm", 0)),
            schedule=str(p.get("schedule", "")),
            wall_thickness_mm=float(p.get("wall_thickness_mm", 0)),
            pipe_type=p.get("pipe_type", "Seamless"),
            material_spec=p.get("material_spec", ""),
            ends=p.get("ends", "BE"),
            id_mm=float(p.get("id_mm", 0) or 0),
        ))

    f = ai_data.get("fittings", {})
    fittings = FittingsData(
        fitting_type=f.get("fitting_type", ""),
        material_spec=f.get("material_spec", ""),
        elbow_standard=f.get("elbow_standard", ""),
        tee_standard=f.get("tee_standard", ""),
        reducer_standard=f.get("reducer_standard", ""),
        cap_standard=f.get("cap_standard", ""),
        plug_standard=f.get("plug_standard", ""),
        weldolet_spec=f.get("weldolet_spec", ""),
        rating=f.get("rating", ""),
    )

    fw = ai_data.get("fittings_welded")
    fittings_welded = None
    if fw and isinstance(fw, dict):
        fittings_welded = FittingsData(
            fitting_type=fw.get("fitting_type", ""),
            material_spec=fw.get("material_spec", ""),
            elbow_standard=fw.get("elbow_standard", ""),
            tee_standard=fw.get("tee_standard", ""),
            reducer_standard=fw.get("reducer_standard", ""),
            cap_standard=fw.get("cap_standard", ""),
            plug_standard=fw.get("plug_standard", ""),
            weldolet_spec=fw.get("weldolet_spec", ""),
        )

    fittings_by_size = []
    for fb in ai_data.get("fittings_by_size", []):
        fittings_by_size.append(FittingBySize(
            size_inch=str(fb.get("size_inch", "")),
            type=fb.get("type", ""),
            fitting_type=fb.get("fitting_type", ""),
            material_spec=fb.get("material_spec", ""),
            elbow_standard=fb.get("elbow_standard", ""),
            tee_standard=fb.get("tee_standard", ""),
            reducer_standard=fb.get("reducer_standard", ""),
            cap_standard=fb.get("cap_standard", ""),
            plug_standard=fb.get("plug_standard", ""),
            weldolet_spec=fb.get("weldolet_spec", ""),
            coupling_standard=fb.get("coupling_standard", ""),
            union_standard=fb.get("union_standard", ""),
            sockolet_standard=fb.get("sockolet_standard", ""),
            nipple_standard=fb.get("nipple_standard", ""),
            swage_standard=fb.get("swage_standard", ""),
            mold_tee_standard=fb.get("mold_tee_standard", ""),
            red_saddle_standard=fb.get("red_saddle_standard", ""),
            adaptor_standard=fb.get("adaptor_standard", ""),
        ))

    # Extra Fittings intentionally emitted as empty — the section was removed
    # from the Excel output and the AI prompt. The Pydantic field is kept so
    # previously-cached PMS entries still deserialize cleanly.
    extra_fittings = ExtraFittings()

    fl = ai_data.get("flange", {})
    flange = FlangeData(
        material_spec=_enforce_flange_material(req.piping_class, fl.get("material_spec", "")),
        face_type=fl.get("face_type", ""),
        flange_type=fl.get("flange_type", ""),
        standard=_enforce_flange_standard(req.piping_class, fl.get("standard", "")),
        compact_flange=fl.get("compact_flange", ""),
        hub_connector=fl.get("hub_connector", ""),
    )

    sb = ai_data.get("spectacle_blind", {})
    spectacle = SpectacleBlind(
        material_spec=sb.get("material_spec", ""),
        standard=sb.get("standard", ""),
        standard_large=sb.get("standard_large", ""),
    )

    bg = ai_data.get("bolts_nuts_gaskets", {})
    bng = BoltsNutsGaskets(
        stud_bolts=bg.get("stud_bolts", ""),
        hex_nuts=bg.get("hex_nuts", ""),
        gasket=bg.get("gasket", ""),
        washers=bg.get("washers", ""),
        gasket_2=bg.get("gasket_2", ""),
    )

    v = ai_data.get("valves", {})

    def _parse_valve_by_size(entries) -> list[ValveSizeEntry]:
        if not entries or not isinstance(entries, list):
            return []
        return [
            ValveSizeEntry(size_inch=str(e.get("size_inch", "")), code=e.get("code", ""))
            for e in entries if isinstance(e, dict)
        ]

    valves = ValveData(
        rating=v.get("rating", ""),
        ball=v.get("ball", ""),
        gate=v.get("gate", ""),
        globe=v.get("globe", ""),
        check=v.get("check", ""),
        butterfly=v.get("butterfly", ""),
        dbb=v.get("dbb", ""),
        dbb_inst=v.get("dbb_inst", ""),
        needle=v.get("needle", ""),
        ball_by_size=_parse_valve_by_size(v.get("ball_by_size")),
        gate_by_size=_parse_valve_by_size(v.get("gate_by_size")),
        globe_by_size=_parse_valve_by_size(v.get("globe_by_size")),
        check_by_size=_parse_valve_by_size(v.get("check_by_size")),
        butterfly_by_size=_parse_valve_by_size(v.get("butterfly_by_size")),
        dbb_by_size=_parse_valve_by_size(v.get("dbb_by_size")),
        dbb_inst_by_size=_parse_valve_by_size(v.get("dbb_inst_by_size")),
    )

    class_type = _determine_class_type(req.piping_class)

    return PMSResponse(
        piping_class=piping_class_override or req.piping_class,
        rating=req.custom_rating or entry.get("rating", ""),
        class_type=class_type,
        material=req.material,
        corrosion_allowance=req.corrosion_allowance,
        # Mill tolerance is a fixed ASME B36.10M standard (12.5% for seamless
        # pipe) — set deterministically from engineering_constants so it never
        # depends on whether the AI remembered to emit it, and so old cached
        # entries get the value filled in on the next regenerate.
        mill_tolerance=f"{MILL_TOLERANCE_PERCENT}%",
        design_code=ai_data.get("design_code", ""),
        service=req.service,
        branch_chart=ai_data.get("branch_chart", ""),
        hydrotest_pressure=hydrotest_str,
        pressure_temperature=pt,
        pipe_code=ai_data.get("pipe_code", ""),
        pipe_data=pipe_data,
        fittings=fittings,
        fittings_welded=fittings_welded,
        fittings_by_size=fittings_by_size,
        extra_fittings=extra_fittings,
        flange=flange,
        spectacle_blind=spectacle,
        bolts_nuts_gaskets=bng,
        valves=valves,
        branch_charts=get_charts_for_class(req.piping_class),
        notes=ai_data.get("notes", []),
    )


async def _generate_from_ai(req: PMSRequest) -> PMSResponse:
    """Core AI generation logic — shared by generate_pms and regenerate_pms."""
    # Find P-T data from JSON
    entry = data_service.find_entry(req.piping_class)

    # ── Custom class path ─────────────────────────────────────────────────
    # When piping_class is not in the catalogue AND custom_rating is provided,
    # this is a user-defined combination.  We:
    #   1. Build a synthetic catalogue entry (empty P-T — the AI generates
    #      all structural data from the custom params + naming rules).
    #   2. Call generate_class_code_with_ai() to replace the internal CUST-*
    #      placeholder with a proper project-standard code (e.g. "K10").
    #   3. Use that code as the canonical piping_class throughout so caching,
    #      the PMSResponse, and the UI all show a clean designation.
    if not entry:
        if not req.custom_rating:
            raise RuntimeError(
                f"Piping class '{req.piping_class}' not found in database. "
                "Only classes with P-T data in the system can be generated."
            )
        logger.info(
            "Custom class '%s' — resolving class code via AI "
            "(custom_rating='%s', material='%s', CA='%s')",
            req.piping_class, req.custom_rating, req.material, req.corrosion_allowance,
        )
        entry = {
            "rating": req.custom_rating,
            "piping_class": req.piping_class,
            "pressure_temperature": {"temperatures": [], "pressures": [], "temp_labels": []},
            "material": req.material,
        }

    rating = req.custom_rating or entry.get("rating", "")
    reference_entries = data_service.select_reference_entries(
        piping_class=req.piping_class,
        material=req.material,
        corrosion_allowance=req.corrosion_allowance,
        service=req.service,
        rating=rating,
        limit=6,
    )

    # ── Custom class: resolve proper piping class code ────────────────────
    # Resolve FIRST so the correct class code (e.g. "K10") goes into both
    # the RAG query and the LLM prompt — not the "CUST-*" placeholder.
    effective_piping_class = req.piping_class
    if req.custom_rating and req.piping_class.upper().startswith('CUST'):
        effective_piping_class = await generate_class_code_with_ai(
            rating=rating,
            material=req.material,
            corrosion_allowance=req.corrosion_allowance,
            service=req.service,
            reference_entries=reference_entries,
            rag_context=None,   # no RAG yet at this point
        )
        logger.info(
            "Custom class resolved (direct path): %s → %s",
            req.piping_class, effective_piping_class,
        )

    reference_entries = data_service.select_reference_entries(
        piping_class=effective_piping_class,
        material=req.material,
        corrosion_allowance=req.corrosion_allowance,
        service=req.service,
        rating=rating,
        limit=6,
    )

    # RAG: retrieve relevant ASME standard excerpts using the RESOLVED class code.
    # Returns (context_string, source_map) where source_map = {1: "ASME B16.5_2020", ...}
    # so we can resolve the LLM's integer rag_docs_used back to real document names.
    rag_context, rag_source_map = retrieve_context(
        piping_class=effective_piping_class,
        material=req.material,
        corrosion_allowance=req.corrosion_allowance,
        service=req.service,
        rating=rating,
    )

    # For custom classes the synthetic entry has no P-T data; instruct the
    # AI to generate pressure_temperature + hydrotest_pressure from ASME tables.
    is_custom_class = not bool(entry.get("pressure_temperature", {}).get("temperatures"))
    cached_pattern_examples = await _select_cached_pattern_examples(
        piping_class=effective_piping_class,
        material=req.material,
        corrosion_allowance=req.corrosion_allowance,
        service=req.service,
        rating=rating,
        limit=2,
    )
    pattern_guidance = _build_pattern_guidance(
        req=req,
        piping_class=effective_piping_class,
        rating=rating,
        reference_entries=reference_entries,
        cached_examples=cached_pattern_examples,
    )

    # Call AI to generate everything (P-T included for custom classes).
    # generate_pms_with_ai raises AIGenerationError with a specific reason on
    # failure — we re-raise as RuntimeError so the route handler turns it
    # into a 422 with the exact cause (credit balance, rate limit, etc.).
    try:
        ai_data = await generate_pms_with_ai(
            piping_class=effective_piping_class,
            material=req.material,
            corrosion_allowance=req.corrosion_allowance,
            service=req.service,
            rating=rating,
            reference_entries=reference_entries,
            rag_context=rag_context,
            generate_pt=is_custom_class,
            pattern_guidance=pattern_guidance,
        )
    except AIGenerationError as e:
        raise RuntimeError(
            f"Unable to generate PMS for class '{req.piping_class}': {e}"
        ) from e

    if not ai_data:
        raise RuntimeError(
            f"AI returned no data for class '{req.piping_class}'. "
            "Try regenerating, or contact support if the issue persists."
        )

    # Correct OD and wall thickness values.
    #   - ASME-coded classes with standard schedules: WT/OD replaced from
    #     ASME B36.10M / B36.19M lookup tables.
    #   - ASME-coded classes with Schedule "-": WT is COMPUTED per ASME
    #     B31.3 §304.1.2 Eq. 3a using the class's design P/T envelope (max
    #     pressure/temperature from pipe_classes.json) and the request's
    #     material / CA. OD is replaced from the OD table.
    #   - Non-ASME pipe codes (CuNi, Copper, GRE, CPVC, Tubing): untouched.
    if "pipe_data" in ai_data:
        pt_data = entry.get("pressure_temperature", {}) or {}
        # For custom classes the catalogue entry has no P-T; fall back to
        # the AI-generated block so wall-thickness correction has real values.
        if not (pt_data.get("pressures") or pt_data.get("temperatures")):
            pt_data = ai_data.get("pressure_temperature", {}) or {}
        pressures = pt_data.get("pressures") or []
        temperatures = pt_data.get("temperatures") or []
        design_pressure = max(pressures) if pressures else None
        design_temp = max(temperatures) if temperatures else None
        material_for_correction = req.material or entry.get("material", "")
        correct_pipe_data(
            ai_data["pipe_data"],
            pipe_code=ai_data.get("pipe_code", ""),
            material=material_for_correction,
            design_pressure_barg=design_pressure,
            design_temp_c=design_temp,
            corrosion_allowance=req.corrosion_allowance,
        )
        logger.info(
            "Corrected pipe_data for %s (pipe_code='%s', material='%s', "
            "P=%s barg, T=%s°C, CA=%s) — WT/OD from ASME tables where "
            "schedule is a standard code; WT computed per B31.3 Eq. 3a "
            "where schedule is '-'",
            req.piping_class, ai_data.get("pipe_code", ""),
            material_for_correction, design_pressure, design_temp,
            req.corrosion_allowance,
        )

    # Merge P-T from JSON + AI-generated data
    pms = _build_pms_response(entry, ai_data, req, piping_class_override=effective_piping_class)

    # Tag each section with its data origin for the optional Excel audit table.
    ai_src = "LLM (RAG)" if rag_context else "LLM (No RAG)"
    pt_src = ai_src if is_custom_class else "Rule Extraction"
    pms.data_sources = {
        "Pressure-Temperature Rating": pt_src,
        "Hydrotest Pressure": pt_src,
        "Pipe Data — Dimensions (OD / WT)": "Rule Extraction",
        "Pipe Data — Type, MOC, Schedule, Ends": ai_src,
        "Fittings": ai_src,
        "Flange": ai_src,
        "Spectacle Blind": ai_src,
        "Bolts / Nuts / Gaskets": ai_src,
        "Valves": ai_src,
        "Notes": ai_src,
    }

    # Build data_source_notes with full RAG chunk text for used documents.
    # Parse chunk bodies from rag_context; use LLM's integer indices to select
    # only the chunks it actually referenced.
    pms.data_source_notes = {}
    if rag_context and rag_source_map:
        # Parse full text of each numbered chunk.
        # First line is "[N] SourceName" — use [^\n]+ so it stops at the newline,
        # then (.*) with DOTALL captures the entire body.
        chunk_texts: dict[int, str] = {}
        for chunk in re.split(r'\n\n---\n\n', rag_context):
            chunk = chunk.strip()
            m = re.match(r'^\[(\d+)\]\s*[^\n]+\n(.*)', chunk, re.DOTALL)
            if m:
                chunk_texts[int(m.group(1))] = m.group(2).strip()

        def _build_note(indices: list[int]) -> str:
            valid_idx = [n for n in indices
                         if isinstance(n, int) and n in rag_source_map]
            if not valid_idx:
                valid_idx = list(rag_source_map.keys())
            parts = []
            for n in valid_idx:
                name = rag_source_map[n]
                text = chunk_texts.get(n, "")
                parts.append(f"[{n}] {name}\n{text}")
            return "\n\n---\n\n".join(parts)

        all_keys = list(rag_source_map.keys())
        used_raw = ai_data.get("rag_docs_used", {})

        if isinstance(used_raw, dict) and used_raw:
            # New format: per-section mapping {"Fittings": [1, 2], "Flange": [1], ...}
            for section, src in pms.data_sources.items():
                if src != "LLM (RAG)":
                    continue
                raw_idxs = used_raw.get(section, [])
                try:
                    idxs = [int(n) for n in (raw_idxs or [])]
                except (TypeError, ValueError):
                    idxs = []
                pms.data_source_notes[section] = _build_note(idxs if idxs else all_keys)
        else:
            # Old flat-list format or empty — assign all retrieved docs to every section
            if isinstance(used_raw, list) and used_raw:
                try:
                    flat_idxs = [int(n) for n in used_raw
                                 if str(n).lstrip("-").isdigit()]
                except (TypeError, ValueError):
                    flat_idxs = all_keys
            else:
                flat_idxs = all_keys
            rag_note = _build_note(flat_idxs)
            for section, src in pms.data_sources.items():
                if src == "LLM (RAG)":
                    pms.data_source_notes[section] = rag_note

    logger.info(
        "Generated PMS for %s (display: %s) via AI (P-T from %s, rest from %s)",
        req.piping_class, effective_piping_class,
        pt_src, ai_src,
    )
    return pms


async def _store_in_caches(key: str, req: PMSRequest, pms: PMSResponse):
    """Store PMS in both in-memory cache and PostgreSQL. The DB layer
    upserts by piping_class and bumps `version` on each write, so the
    L2 row for a class is overwritten in place rather than duplicated.
    The returned version string is written back onto the PMSResponse
    so the frontend + Excel header can show the current revision.

    After a successful DB write, mirror the payload to the external
    SPE Valvesheet backend — POST for a new row (version A0), PUT for
    a regeneration (A1+). The sync runs as a background task so the
    user's response isn't delayed by the downstream HTTP call, and all
    errors are swallowed+logged (a flaky mirror must not break local
    generation)."""
    synced_version: str | None = None
    if db_service.is_available():
        synced_version = await db_service.store_pms(
            piping_class=key,                            # composite cache key
            display_class=pms.piping_class.upper().strip(),  # human-readable code
            material=req.material,
            corrosion_allowance=req.corrosion_allowance,
            service=req.service,
            response=pms.model_dump(),
        )
        if synced_version:
            pms.version = synced_version
    _pms_cache[key] = pms

    # Mirror to the external valvesheet backend. Treat anything other
    # than exactly "A0" as an update — the UPSERT returns A0 only on
    # the very first insert, so this cleanly distinguishes POST vs PUT
    # even when the caller (regenerate_pms) forced a bump.
    if synced_version is not None:
        is_regenerate = synced_version != "A0"
        valvesheet_sync_service.sync_in_background(pms, is_regenerate=is_regenerate)


def _backfill_rag_notes(pms: PMSResponse) -> None:
    """Populate data_source_notes from RAG for cached entries that predate the feature."""
    try:
        rag_context, rag_source_map = retrieve_context(
            piping_class=pms.piping_class,
            material=pms.material,
            corrosion_allowance=pms.corrosion_allowance,
            service=pms.service,
            rating=pms.rating,
        )
        if not rag_context or not rag_source_map:
            return
        chunk_texts: dict[int, str] = {}
        for chunk in re.split(r'\n\n---\n\n', rag_context):
            chunk = chunk.strip()
            m = re.match(r'^\[(\d+)\]\s*[^\n]+\n(.*)', chunk, re.DOTALL)
            if m:
                chunk_texts[int(m.group(1))] = m.group(2).strip()
        valid = list(rag_source_map.keys())
        if not valid:
            return
        parts = []
        for n in valid:
            name = rag_source_map[n]
            text = chunk_texts.get(n, "")
            parts.append(f"[{n}] {name}\n{text}")
        rag_note = "\n\n---\n\n".join(parts)
        for section, src in (pms.data_sources or {}).items():
            if src == "LLM (RAG)":
                pms.data_source_notes[section] = rag_note
    except Exception as e:
        logger.warning("RAG notes backfill failed for %s: %s", pms.piping_class, e)


async def generate_pms(req: PMSRequest) -> PMSResponse:
    """
    Generate PMS with layered caching:
      L1: In-memory dict (fast, process-scoped, NO TTL or eviction)
      L2: PostgreSQL (persistent across restarts, NO TTL)
      L3: Claude AI (expensive, only if not cached)

    Both cache layers are write-through on regenerate — calling
    regenerate_pms() overwrites the existing L1 entry for this class and
    bumps the L2 `version` column (A0 → A1 → A2 …) via UPSERT, so the
    table never holds two rows for the same piping_class. The only way
    an entry "disappears" is:
      * The Admin UI delete button / /api/admin/db/pms-cache/{piping_class}
      * POST /api/clear-cache (nukes both L1 and L2)
      * Manual SQL DELETE on the pms_cache table
    """
    # ── Tubing classes (T80A/B/C, T90A/B/C) bypass the AI entirely ──
    # Tubing specs are fully deterministic per the project sheet workbook
    # and don't have any flexibility the AI could add value to. We build
    # the response from app/data/tubing_specs.json instead. Still cache
    # it so subsequent requests hit L1 directly.
    key = _cache_key(req)
    if is_tubing_class(req.piping_class):
        if key in _pms_cache:
            logger.info("L1 memory cache HIT for %s (tubing)", req.piping_class)
            return _pms_cache[key]
        logger.info("Building %s deterministically from tubing_specs.json", req.piping_class)
        pms = build_tubing_pms(req)
        _pms_cache[key] = pms
        # Note: tubing classes are NOT persisted to the L2 Postgres cache
        # because the JSON file IS the source of truth — re-deploys with
        # an updated spec sheet should pick up the new values immediately,
        # not be shadowed by stale DB rows.
        return pms

    # L1: In-memory cache
    if key in _pms_cache:
        logger.info("L1 memory cache HIT for %s (key=%s)", req.piping_class, key)
        pms = _pms_cache[key]
        if not pms.data_source_notes and any(
            src == "LLM (RAG)" for src in (pms.data_sources or {}).values()
        ):
            _backfill_rag_notes(pms)
        return _apply_hydrotest(pms)

    # L2: PostgreSQL cache
    if db_service.is_available():
        cached = await db_service.get_cached_pms(key)
        if cached:
            # A row exists for this class — honour the "don't regenerate
            # unless user explicitly asks" contract. If the stored payload
            # fails to deserialize against the current PMSResponse schema
            # (rare — e.g. a required field was renamed after the row was
            # written), log LOUDLY and fall through to AI rather than
            # silently return a corrupt object. Regenerate will overwrite
            # the row and self-heal.
            try:
                pms = PMSResponse(**cached)
            except Exception as e:
                logger.warning(
                    "L2 DB row for %s failed to deserialize (%s) — falling "
                    "back to AI. Row will be overwritten on next store.",
                    req.piping_class, e,
                )
            else:
                logger.info(
                    "L2 database cache HIT for %s (key=%s, version=%s)",
                    req.piping_class, key, cached.get("version", "?"),
                )
                _apply_hydrotest(pms)
                # Backfill data_source_notes for entries cached before this
                # feature was added (data_source_notes would be empty {}).
                if not pms.data_source_notes and any(
                    src == "LLM (RAG)" for src in (pms.data_sources or {}).values()
                ):
                    _backfill_rag_notes(pms)
                    if pms.data_source_notes:
                        await _store_in_caches(key, req, pms)
                        logger.info("Backfilled data_source_notes for %s", req.piping_class)
                _pms_cache[key] = pms  # Promote to L1
                return pms
        else:
            logger.info(
                "L2 database cache MISS for %s (key=%s) — no row found",
                req.piping_class, key,
            )
    else:
        logger.info("L2 database disabled — skipping cache check")

    # L3: AI generation (only reached when BOTH caches missed)
    logger.info("Generating %s via AI (cache miss)", req.piping_class)
    pms = await _generate_from_ai(req)
    _apply_hydrotest(pms)
    await _store_in_caches(key, req, pms)
    return pms


async def regenerate_pms(req: PMSRequest) -> PMSResponse:
    """Force fresh AI generation, bypassing all caches. Overwrites cache.

    Tubing classes (T80A/B/C, T90A/B/C) are still deterministic — Regenerate
    just refreshes the L1 entry from tubing_specs.json (in case the file
    was edited at runtime).
    """
    key = _cache_key(req)
    if is_tubing_class(req.piping_class):
        logger.info("Regenerating %s deterministically (tubing — refresh from JSON)", req.piping_class)
        pms = build_tubing_pms(req)
        _pms_cache[key] = pms
        return pms

    logger.info("Regenerating PMS for %s via AI (forced, bypassing cache)", req.piping_class)
    pms = await _generate_from_ai(req)
    _apply_hydrotest(pms)
    await _store_in_caches(key, req, pms)
    return pms


async def clear_cache():
    """Clear both in-memory and database caches."""
    _pms_cache.clear()
    count = 0
    if db_service.is_available():
        count = await db_service.clear_all_cache()
    logger.info("Cache cleared — memory + %d DB entries", count)


def generate_excel(pms: PMSResponse) -> bytes:
    return generate_pms_excel_bytes(pms)

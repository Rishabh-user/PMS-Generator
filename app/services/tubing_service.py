"""
Deterministic PMS builder for tubing classes T80A/B/C and T90A/B/C.

Tubing classes follow ASTM A 269 (SS316/316L for T80, UNS S31254 / 6 Mo
for T90) and use proprietary thickness ratings (A=125 barg, B=206 barg,
C=330 barg). Every value is fixed by the project spec sheet — no AI
flexibility helps here, so we build the PMSResponse directly and bypass
the AI path entirely.

Single source of truth:
  • Per-class data (material, P-T, service, CA) → pipe_classes.json
    (read at request time via data_service.find_entry).
  • Tubing-only constants (sizes, walls, fittings strings, valve code
    templates, notes) → constants in this module. They're identical
    across all 6 tubing classes and rarely change.

This module ONLY handles the 6 tubing classes; every other class still
goes through the AI path in pms_service.generate_pms.

Source spec: D:/targeticon/pms-files/Pipe Class Sheets-With Tubing-updated.xlsx
"""
from __future__ import annotations

import logging

from app.models.pms_models import (
    BoltsNutsGaskets,
    ExtraFittings,
    FittingBySize,
    FittingsData,
    FlangeData,
    PipeSize,
    PMSRequest,
    PMSResponse,
    PressureTemperature,
    SpectacleBlind,
    ValveData,
    ValveSizeEntry,
)
from app.services import data_service
from app.utils.engineering import hydrotest_pressure_corrected
from app.utils.engineering_constants import HYDROTEST_FACTOR

logger = logging.getLogger(__name__)

# ── Tubing-only constants (verbatim from the project spec workbook) ──
TUBING_CLASSES: frozenset[str] = frozenset({
    "T80A", "T80B", "T80C", "T90A", "T90B", "T90C",
})

# Identical across all 6 classes — proprietary tubing wall thicknesses.
_SIZES   = ["0.5", "0.75", "1", "1.5"]
_OD_MM   = [12.7, 19.05, 25.4, 38.1]
_WT_MM   = [1.245, 1.651, 2.413, 3.404]

_PIPE_CODE = "ASTM A 269"
_DESIGN_CODE = "ASME B 31.3"
_ENDS = "PE"

# Material spec strings differ between the T80 (SS 316/316L) and T90
# (6 Mo / UNS S31254) families but are identical within each family.
_T80_MATERIAL_SPEC = "ASTM A269 Type 316/316L SML, Annealed, Hardness <= 90 HRB SML"
_T90_MATERIAL_SPEC = "ASTM A269 (UNS S31254) SML, Annealed, Hardness <= 90 HRB SML"

# Fittings — manufacturer-standard compression fittings, same for all 6 classes.
_FITTING_TYPE          = "Compression Fitting"
_FITTING_MATERIAL_SPEC = (
    "Compression fitting with double ferrule, body AISI 316, "
    "ferrules and nuts in AISI 316"
)
_FITTING_ENDS          = "OD X THD, OD X OD, & OD X SW (Manufacturer Standard)"

# Valve codes — instrument-only with JT (RTJ + NPT female) suffix per spec.
# Format: "<Type><Bore><Seat>{class}JT"
_VALVE_RATING        = "10000# (69 MPa)"
_VALVE_DBB_TEMPLATE   = "DBFP{cls}JT"   # DBB:    DB · F bore · P (PEEK) seat
_VALVE_NEEDLE_TEMPLATE= "NEIP{cls}JT"   # Needle: NE · I (Inline) · P (PEEK)
_VALVE_BALL_TEMPLATE  = "BLFP{cls}JT"   # Ball:   BL · F bore · P (PEEK)
_VALVE_CHECK_TEMPLATE = "CHPM{cls}JT"   # Check:  CH · P (Piston) · M (Metal)

# Notes — 7 standard for all 6 classes; T90 family adds note 8 (Mo content).
_NOTES_COMMON = [
    "PMS to be read in conjunction with Project Piping Design Basis, "
    "and Valve Material Specification.",
    "Seal or fillet welds are not allowed in OD fittings and valves assembly.",
    "The maximum work pressure for fittings shall be above the maximum "
    "work pressure of the tubing for all operation conditions.",
    "Proper tools shall be used to assemble the OD fittings (feeler "
    "gauge, wrench etc) in accordance with the fitting manufacturer "
    "instructions. Special attention shall be given for assemblies with "
    "OD 1\" and bigger, where preswage must be done.",
    "When used in hazardous area, this piping class shall comply to "
    "ABS requirements for hazardous area.",
    "The Tubing shall be prepared (cut, burred, and bent) only with the "
    "tools recommended by the OD fittings manufacturer.",
    "Thickness mentioned in this spec is preliminary, Manufacturer "
    "recommended thickness will update during project execution.",
]
_T90_EXTRA_NOTE = (
    "The minimum Molybdenum content shall be 8.5% for tubing material."
)


def is_tubing_class(piping_class: str) -> bool:
    """True iff this class is one of the 6 tubing variants."""
    return (piping_class or "").upper().strip() in TUBING_CLASSES


def _material_spec_for(piping_class: str) -> str:
    """T80 family → SS 316/316L; T90 family → 6 Mo (UNS S31254)."""
    return _T90_MATERIAL_SPEC if piping_class.upper().startswith("T90") else _T80_MATERIAL_SPEC


def build_tubing_pms(req: PMSRequest) -> PMSResponse:
    """Construct a fully-populated PMSResponse for a tubing class.

    Per-class data (material name, P-T, default service, CA) comes from
    `pipe_classes.json` so there's a single source of truth. Tubing-only
    constants are in this module (above).

    Raises ValueError if the class isn't one of the 6 tubing variants.
    """
    cls = (req.piping_class or "").upper().strip()
    if cls not in TUBING_CLASSES:
        raise ValueError(f"build_tubing_pms called with non-tubing class: {cls!r}")

    entry = data_service.find_entry(cls)
    if not entry:
        raise ValueError(
            f"Tubing class {cls} declared in TUBING_CLASSES but missing "
            f"from pipe_classes.json — add the entry first."
        )

    # ── Per-class data (from pipe_classes.json) ──
    material   = entry.get("material") or ""
    ca         = entry.get("corrosion_allowance") or "NIL"
    catalogue_service = (entry.get("service") or "").strip()
    pt_data    = entry.get("pressure_temperature") or {}
    temps      = pt_data.get("temperatures") or []
    pressures  = pt_data.get("pressures") or []
    temp_lbls  = pt_data.get("temp_labels") or [str(t) for t in temps]

    pressure_temperature = PressureTemperature(
        temperatures=temps,
        pressures=pressures,
        temp_labels=temp_lbls,
    )
    # Hydrotest per ASME B31.3 §345.4.2(b). Tubing rarely sees high
    # temperatures (instrumentation lines), so the correction usually
    # collapses to flat 1.5·P; but the helper is used so the moment a
    # tubing class is rated for hot service, the correction kicks in
    # automatically without a tubing-specific code path.
    max_p = max(pressures) if pressures else 0.0
    rated_temps = [t for t, p in zip(temps, pressures) if (p or 0) > 0 and t is not None]
    max_t = max(rated_temps) if rated_temps else (max(temps) if temps else 0)
    ht = hydrotest_pressure_corrected(
        design_pressure=max_p,
        design_temp_c=max_t,
        material_spec=material,
    )
    hydrotest = ht["pressure_barg"]

    # User-supplied service in the request overrides the catalogue default,
    # same convention as the AI-generated classes.
    service = (req.service or catalogue_service).strip() or catalogue_service

    # ── Pipe data — 4 sizes, identical wall thicknesses ──
    material_spec = _material_spec_for(cls)
    pipe_data: list[PipeSize] = [
        PipeSize(
            size_inch=_SIZES[i],
            od_mm=_OD_MM[i],
            schedule="-",
            wall_thickness_mm=_WT_MM[i],
            pipe_type="Seamless",
            material_spec=material_spec,
            ends=_ENDS,
        )
        for i in range(len(_SIZES))
    ]

    # ── Fittings — class-level fallback strings + per-size rows ──
    fittings = FittingsData(
        fitting_type=_FITTING_TYPE,
        material_spec=_FITTING_MATERIAL_SPEC,
        elbow_standard="Manufacturer Std",
        tee_standard="Manufacturer Std",
        reducer_standard="Manufacturer Std",
        cap_standard="Manufacturer Std",
        plug_standard="Manufacturer Std",
        weldolet_spec="N/A — compression fittings only",
    )
    fittings_by_size: list[FittingBySize] = [
        FittingBySize(
            size_inch=s,
            type="Compression Fitting",
            fitting_type=_FITTING_TYPE,
            material_spec=_FITTING_MATERIAL_SPEC,
            elbow_standard="Manufacturer Std",
            tee_standard="Manufacturer Std",
            reducer_standard="Manufacturer Std",
            cap_standard="Manufacturer Std",
            plug_standard="Manufacturer Std",
            weldolet_spec="N/A",
        )
        for s in _SIZES
    ]
    extra_fittings = ExtraFittings(
        coupling="", hex_plug="", union="", union_large="",
        olet="", olet_large="", swage="",
    )

    # ── Tubing has no flanges, no spectacle blinds, no bolts/gaskets ──
    flange = FlangeData(
        material_spec="N/A — tubing class (compression fittings)",
        face_type="N/A", flange_type="N/A", standard="N/A",
        compact_flange="", hub_connector="",
    )
    spectacle_blind = SpectacleBlind(
        material_spec="N/A — tubing class",
        standard="N/A", standard_large="",
    )
    bolts = BoltsNutsGaskets(
        stud_bolts="N/A — tubing class", hex_nuts="N/A", gasket="N/A",
    )

    # ── Valves — instrument-only, all with JT suffix ──
    dbb_inst    = _VALVE_DBB_TEMPLATE.format(cls=cls)
    needle_inst = _VALVE_NEEDLE_TEMPLATE.format(cls=cls)
    ball_inst   = _VALVE_BALL_TEMPLATE.format(cls=cls)
    check_inst  = _VALVE_CHECK_TEMPLATE.format(cls=cls)
    valves = ValveData(
        rating=_VALVE_RATING,
        ball="", gate="", globe="", check="", butterfly="", dbb="",
        dbb_inst=dbb_inst,
        needle=needle_inst,
        ball_by_size=[ValveSizeEntry(size_inch=_SIZES[0], code=ball_inst)],
        gate_by_size=[],
        globe_by_size=[],
        check_by_size=[ValveSizeEntry(size_inch=_SIZES[0], code=check_inst)],
        butterfly_by_size=[],
        dbb_by_size=[],
        dbb_inst_by_size=[ValveSizeEntry(size_inch=_SIZES[0], code=dbb_inst)],
    )

    # ── Notes — 7 common; T90 adds the Mo content note ──
    notes = list(_NOTES_COMMON)
    if cls.startswith("T90"):
        notes.append(_T90_EXTRA_NOTE)

    return PMSResponse(
        piping_class=cls,
        rating=entry.get("rating") or "-",
        material=material,
        corrosion_allowance=ca,
        class_type="tubing",
        mill_tolerance="0.0%",
        design_code=_DESIGN_CODE,
        service=service,
        branch_chart="",
        hydrotest_pressure=str(hydrotest),
        pressure_temperature=pressure_temperature,
        pipe_code=_PIPE_CODE,
        pipe_data=pipe_data,
        fittings=fittings,
        fittings_by_size=fittings_by_size,
        extra_fittings=extra_fittings,
        flange=flange,
        spectacle_blind=spectacle_blind,
        bolts_nuts_gaskets=bolts,
        valves=valves,
        branch_charts=[],
        notes=notes,
    )

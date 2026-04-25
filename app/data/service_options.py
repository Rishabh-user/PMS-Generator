"""Canonical service-description list shown in the Service Description picker.

Single source of truth — exposed via GET /api/services so both the standalone
HTML UI and the Valvesheet frontend pull from the same list. Order matters:
the picker renders these top-to-bottom, with most-common services first.
Append-only — removing an entry will hide it from existing UIs but won't
affect cached PMS rows that already include it as free-form text.
"""

SERVICE_OPTIONS: list[str] = [
    "General",
    "Hydrocarbon Service",
    "Sour / H2S Service (NACE)",
    "Cooling Water / Seawater",
    "Cooling Media",
    "Heating Media",
    "Steam",
    "Fire Water",
    "Diesel",
    "Water Injection",
    "Hydraulic Oil",
    "Fuel Gas",
    "Glycol",
    "Nitrogen",
    "Hydrogen Service",
    "Utility / Instrument",
    "Low Temperature Service",
]

"""Canonical material families and corrosion-allowance increments per
§5.5 of the project Piping Material Specification (40801-SPE-80000-PP-SP-0001).

Single source of truth — both backend and frontend read these via the
/api/options/* endpoints, so the dropdowns and validators can never drift
out of sync. Adding a new material/CA here lights it up everywhere at once.

Order matters: dropdowns render top-to-bottom in this order. Carbon-steel
variants come first because they're the most common project selection;
exotic materials trail.
"""

# §5.5 Part-2 (material digit) families. Each entry corresponds to a
# distinct material the project supports — the §5.5 digit is determined
# at class-derivation time by combining material + CA.
SPEC_MATERIALS: list[str] = [
    "CS",                  # digit 1 / 2 (CS-3mm vs CS-6mm CA)
    "CS NACE",
    "LTCS",                # A350 LF2 — also Group 1.1 in B16.5
    "LTCS NACE",
    "CS GALV",             # digits 3 / 4 / 5 by CA
    "CS - Epoxy Lined",    # digit 6
    "SS316",               # digit 9
    "SS316L",              # digit 10
    "SS316L NACE",
    "DSS",                 # digit 20 (S31803)
    "DSS NACE",
    "SDSS",                # digit 25 (S32750)
    "SDSS NACE",
    "CuNi",                # digit 30 (90/10)
    "Copper",              # digit 40
    "GRE",                 # digit 50 (composite)
    "CPVC",                # digit 60 (plastic)
    "TITANIUM",            # digit 70
]

# Standard CA increments per project convention. The §5.5 material digit
# encodes BOTH material AND CA, so the (material, CA) pair determines the
# digit at derivation time. Showing the full list lets the user request
# any combination — uncatalogued ones route through the standards path.
SPEC_CORROSION_ALLOWANCES: list[str] = [
    "NIL",
    "1.5 mm",
    "3 mm",
    "6 mm",
]

"""
Rebuild app/data/pipe_classes.json from the authoritative INDEX sheet of
Pipe Class Sheets-With Tubing-updated.xlsx.

This script is the single source of truth for pipe class metadata (rating,
material, CA, service) AND for the P-T rating table. Run it whenever the
Excel spec master is updated.

Usage:
    python scripts/rebuild_pipe_classes_from_excel.py \\
        "D:/targeticon/pms-files/Pipe Class Sheets-With Tubing-updated.xlsx"
"""
import json
import sys
from pathlib import Path

import openpyxl

# INDEX sheet column layout (0-indexed) — matches the updated Excel.
COL_SPEC = 1
COL_RATING = 2
COL_MATERIAL = 4
COL_CA = 5
COL_SERVICE = 6
COL_MIN_TEMP = 19
# Temperature header columns. Index → header-temp (°C). Leave pressures for
# temperatures whose cell is blank as missing so we don't invent data.
TEMP_HEADERS = [
    (20, 38),
    (21, 50),
    (22, 100),
    (23, 150),
    (24, 200),
    (25, 250),
    (26, 300),
    (27, 350),
    (28, 400),
    (29, 450),
    (30, 500),
]


def _clean(v) -> str:
    return str(v).strip() if v is not None else ""


def _fnum(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_pt(min_temp, row) -> dict:
    temps: list[float] = []
    pressures: list[float] = []
    labels: list[str] = []
    for col_idx, header_temp in TEMP_HEADERS:
        cell = row[col_idx] if col_idx < len(row) else None
        p = _fnum(cell)
        if p is None:
            continue
        temps.append(float(header_temp))
        pressures.append(round(p, 2))
        if not labels:
            # First label combines MIN TEMP with the first header temp
            if min_temp is not None:
                labels.append(f"{int(min_temp)} to {int(header_temp)}")
            else:
                labels.append(str(int(header_temp)))
        else:
            labels.append(str(int(header_temp)))
    return {
        "temperatures": temps,
        "pressures": pressures,
        "temp_labels": labels,
    }


def rebuild(xlsx_path: Path, fallback_pt: dict[str, dict]) -> list[dict]:
    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
    ws = wb["INDEX"]
    rows = list(ws.iter_rows(values_only=True))

    entries: list[dict] = []
    for row in rows[2:]:  # skip title + header
        spec = _clean(row[COL_SPEC])
        if not spec:
            continue
        rating = _clean(row[COL_RATING])
        material = _clean(row[COL_MATERIAL])
        ca = _clean(row[COL_CA])
        min_temp = _fnum(row[COL_MIN_TEMP])
        pt = _build_pt(min_temp, row)

        # Excel INDEX has no P-T row for tubing classes — preserve the
        # previous JSON's P-T so downstream callers still get data.
        if not pt["temperatures"] and spec in fallback_pt:
            pt = fallback_pt[spec]

        entries.append(
            {
                "piping_class": spec,
                "rating": rating,
                "material": material,
                "corrosion_allowance": ca,
                "pressure_temperature": pt,
            }
        )
    return entries


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <path-to-xlsx>", file=sys.stderr)
        sys.exit(1)

    xlsx_path = Path(sys.argv[1])
    out_path = Path(__file__).resolve().parent.parent / "app" / "data" / "pipe_classes.json"

    # Preserve existing P-T data for classes whose P-T is missing in Excel
    # (notably the T8xA/T9xA tubing rows).
    fallback_pt: dict[str, dict] = {}
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        fallback_pt = {
            e["piping_class"]: e["pressure_temperature"]
            for e in existing
            if e.get("pressure_temperature")
        }

    entries = rebuild(xlsx_path, fallback_pt)
    out_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    print(f"Wrote {len(entries)} pipe classes -> {out_path}")


if __name__ == "__main__":
    main()

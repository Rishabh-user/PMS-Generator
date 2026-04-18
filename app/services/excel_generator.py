"""
Generates formatted Excel PMS sheets matching the standard industrial format.
Uses openpyxl for precise formatting control.
"""
import io
import logging
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.models.pms_models import PMSResponse

logger = logging.getLogger(__name__)

# Style constants
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
SECTION_FILL = PatternFill("solid", fgColor="D6E4F0")
DATA_FILL = PatternFill("solid", fgColor="FFFFFF")
ALT_FILL = PatternFill("solid", fgColor="F2F7FB")
NOTES_FILL = PatternFill("solid", fgColor="FFF2CC")

HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(name="Arial", bold=True, color="1F4E79", size=14)
SECTION_FONT = Font(name="Arial", bold=True, color="1F4E79", size=10)
LABEL_FONT = Font(name="Arial", bold=True, color="333333", size=9)
DATA_FONT = Font(name="Arial", color="333333", size=9)
NOTE_FONT = Font(name="Arial", italic=True, color="666666", size=8)

THIN_BORDER = Border(
    left=Side(style="thin", color="B0B0B0"),
    right=Side(style="thin", color="B0B0B0"),
    top=Side(style="thin", color="B0B0B0"),
    bottom=Side(style="thin", color="B0B0B0"),
)
BOTTOM_BORDER = Border(bottom=Side(style="medium", color="1F4E79"))

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)


def _apply_style(ws, row, col, font=DATA_FONT, fill=DATA_FILL, alignment=CENTER, border=THIN_BORDER):
    cell = ws.cell(row=row, column=col)
    cell.font = font
    cell.fill = fill
    cell.alignment = alignment
    cell.border = border
    return cell


def _write_section_header(ws, row: int, text: str, col_start: int = 1, col_end: int = 20):
    """Write a section header row with styling."""
    for c in range(col_start, col_end + 1):
        cell = _apply_style(ws, row, c, font=SECTION_FONT, fill=SECTION_FILL, alignment=LEFT)
    ws.cell(row=row, column=col_start).value = text
    ws.merge_cells(start_row=row, start_column=col_start, end_row=row, end_column=col_end)


def _write_merged_data_row(ws, row: int, label: str, values: list, col_start: int = 3,
                           total_cols: int = 20, font=DATA_FONT, fill=DATA_FILL):
    """Write a data row that auto-merges consecutive cells with identical values.

    E.g. if values = ["Seamless"]*14 + ["LSAW, 100% RT"]*8, it will merge
    the first 14 cells into one "Seamless" and the next 8 into one "LSAW, 100% RT".
    """
    _apply_style(ws, row, 1, font=LABEL_FONT, fill=fill, alignment=LEFT)
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=fill, alignment=LEFT).value = label
    # Style all data columns first
    for i in range(len(values)):
        col = col_start + i
        if col <= total_cols:
            _apply_style(ws, row, col, font=font, fill=fill, alignment=CENTER)
    # Fill remaining columns
    for c in range(col_start + len(values), total_cols + 1):
        _apply_style(ws, row, c, fill=fill)

    if not values:
        return

    # Find runs of identical values and merge them
    run_start = 0
    for i in range(1, len(values) + 1):
        if i == len(values) or str(values[i]) != str(values[run_start]):
            start_col = col_start + run_start
            end_col = col_start + i - 1
            if start_col <= total_cols:
                end_col = min(end_col, total_cols)
                ws.cell(row=row, column=start_col).value = values[run_start]
                if end_col > start_col:
                    ws.merge_cells(start_row=row, start_column=start_col, end_row=row, end_column=end_col)
            if i < len(values):
                run_start = i


def _write_size_header_row(ws, row: int, sizes: list, col_start: int = 3, total_cols: int = 20):
    """Write a size header row showing pipe sizes across columns."""
    _apply_style(ws, row, 1, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT)
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Size (in)"
    for i, size in enumerate(sizes):
        col = col_start + i
        if col <= total_cols:
            _apply_style(ws, row, col, font=LABEL_FONT, fill=ALT_FILL, alignment=CENTER).value = size
    for c in range(col_start + len(sizes), total_cols + 1):
        _apply_style(ws, row, c, fill=ALT_FILL)


def _write_label_value_row(ws, row: int, label: str, value: str, col_start: int = 1, val_col: int = 3, col_end: int = 20):
    """Write a label-value pair row.

    Layout (consistent with _write_merged_data_row and _write_size_header_row):
      Col 1 (A) = spacer / marker (empty)
      Col 2 (B) = label
      Cols 3..col_end (C onward, merged) = value
    """
    # Col 1 (A): empty spacer
    _apply_style(ws, row, 1, fill=DATA_FILL)
    # Col 2 (B): label
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=DATA_FILL, alignment=LEFT).value = label
    # Col 3..col_end (C onward, merged): value
    _apply_style(ws, row, val_col, font=DATA_FONT, fill=DATA_FILL, alignment=LEFT).value = value
    if col_end > val_col:
        ws.merge_cells(start_row=row, start_column=val_col, end_row=row, end_column=col_end)
    for c in range(val_col + 1, col_end + 1):
        _apply_style(ws, row, c, fill=DATA_FILL)


def generate_pms_excel(pms: PMSResponse, output_path: Path) -> Path:
    """Generate a formatted Excel PMS sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = pms.piping_class

    # Determine column count based on pipe sizes
    num_pipe_cols = max(len(pms.pipe_data), 1)
    total_cols = max(num_pipe_cols + 3, 20)  # At least 20 columns
    pipe_col_start = 3
    pipe_col_end = pipe_col_start + num_pipe_cols - 1

    # Column widths — standard PMS spec layout
    #   A (spacer)  width=4
    #   B (label)   width=22  — accommodates "Corrosion Allowance", "Mill Tolerance", etc.
    #   C..N (data) width=12  — accommodates size/OD/schedule/MOC strings
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 13
    for i in range(4, total_cols + 1):
        ws.column_dimensions[get_column_letter(i)].width = 12

    row = 1

    # === TITLE ===
    for c in range(1, total_cols + 1):
        _apply_style(ws, row, c, font=HEADER_FONT, fill=HEADER_FILL)
    ws.cell(row=row, column=1).value = "PIPING MATERIAL SPECIFICATION"
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
    ws.row_dimensions[row].height = 30
    _apply_style(ws, row, 1, font=Font(name="Arial", bold=True, color="FFFFFF", size=14), fill=HEADER_FILL, alignment=CENTER)
    row += 1

    # === HEADER INFO ===
    header_labels = [
        ("Piping Class", pms.piping_class),
        ("Rating", pms.rating),
        ("Material", pms.material),
        ("Corrosion Allowance", pms.corrosion_allowance),
        ("Mill Tolerance", pms.mill_tolerance),
        ("Design Code", pms.design_code),
        ("Service", pms.service),
        ("Branch Chart", pms.branch_chart),
    ]
    for label, value in header_labels:
        _write_label_value_row(ws, row, label, value, col_end=total_cols)
        row += 1

    row += 1

    # === PRESSURE-TEMPERATURE RATING ===
    _write_section_header(ws, row, "Pressure-Temperature Rating", col_end=total_cols)
    row += 1

    pt = pms.pressure_temperature

    # Temp labels row (e.g. "-29 TO 38")
    if pt.temp_labels:
        _apply_style(ws, row, 1, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT)
        _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Temp. Range (°C)"
        for i, lbl in enumerate(pt.temp_labels):
            col = pipe_col_start + i
            if col <= total_cols:
                _apply_style(ws, row, col, font=DATA_FONT, fill=ALT_FILL).value = lbl
        row += 1

    # Temperature row
    _apply_style(ws, row, 1, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT)
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Temp. (°C)"
    for i, temp in enumerate(pt.temperatures):
        col = pipe_col_start + i
        if col <= total_cols:
            _apply_style(ws, row, col, font=DATA_FONT, fill=ALT_FILL).value = temp
    # Hydrotest header
    if pms.hydrotest_pressure:
        ht_col = pipe_col_start + len(pt.temperatures) + 1
        if ht_col <= total_cols:
            _apply_style(ws, row, ht_col, font=LABEL_FONT, fill=ALT_FILL, alignment=CENTER).value = "Hydrotest Pr.\n(barg)"
    row += 1

    # Pressure row
    _apply_style(ws, row, 1, font=LABEL_FONT, fill=DATA_FILL, alignment=LEFT)
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=DATA_FILL, alignment=LEFT).value = "Press. (barg)"
    for i, press in enumerate(pt.pressures):
        col = pipe_col_start + i
        if col <= total_cols:
            _apply_style(ws, row, col, font=DATA_FONT, fill=DATA_FILL).value = press
    # Hydrotest value
    if pms.hydrotest_pressure:
        ht_col = pipe_col_start + len(pt.temperatures) + 1
        if ht_col <= total_cols:
            _apply_style(ws, row, ht_col, font=DATA_FONT, fill=DATA_FILL, alignment=CENTER).value = pms.hydrotest_pressure
    row += 2

    # === PIPE DATA ===
    _write_section_header(ws, row, "Pipe Data", col_end=total_cols)
    row += 1

    if pms.pipe_code:
        _write_label_value_row(ws, row, "Code", pms.pipe_code, col_end=total_cols)
        row += 1

    pipe_rows = [
        ("Size (in)", [p.size_inch for p in pms.pipe_data]),
        ("O.D. (mm)", [p.od_mm for p in pms.pipe_data]),
        ("Schedule", [p.schedule for p in pms.pipe_data]),
        ("W.T. (mm)", [p.wall_thickness_mm for p in pms.pipe_data]),
        ("Type", [p.pipe_type for p in pms.pipe_data]),
        ("MOC", [p.material_spec for p in pms.pipe_data]),
        ("Ends", [p.ends for p in pms.pipe_data]),
    ]

    # Rows that benefit from merging (repeated values across sizes)
    mergeable_pipe_rows = {"Type", "MOC", "Ends"}
    for label, values in pipe_rows:
        is_alt = label in ("Size (in)", "Schedule", "Type", "Ends")
        fill = ALT_FILL if is_alt else DATA_FILL
        if label in mergeable_pipe_rows:
            _write_merged_data_row(ws, row, label, values, col_start=pipe_col_start,
                                   total_cols=total_cols, fill=fill)
        else:
            _apply_style(ws, row, 1, font=LABEL_FONT, fill=fill, alignment=LEFT)
            _apply_style(ws, row, 2, font=LABEL_FONT, fill=fill, alignment=LEFT).value = label
            for i, val in enumerate(values):
                col = pipe_col_start + i
                if col <= total_cols:
                    _apply_style(ws, row, col, font=DATA_FONT, fill=fill).value = val
            # Fill remaining cols
            for c in range(pipe_col_start + len(values), total_cols + 1):
                _apply_style(ws, row, c, fill=fill)
        row += 1

    row += 1

    # === FITTINGS (SIZE-WISE DATA) ===
    _write_section_header(ws, row, "Fittings — Butt Weld (SCH to match pipe)", col_end=total_cols)
    row += 1

    # Size columns header
    _apply_style(ws, row, 1, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT)
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Size (in)"
    for i, fitting in enumerate(pms.fittings_by_size):
        col = 3 + i
        if col <= total_cols:
            _apply_style(ws, row, col, font=LABEL_FONT, fill=ALT_FILL, alignment=CENTER).value = fitting.size_inch
    for c in range(3 + len(pms.fittings_by_size), total_cols + 1):
        _apply_style(ws, row, c, fill=ALT_FILL)
    row += 1

    # Type row (Seamless/Welded) — with auto-merge
    type_values = ["Seam." if f.type == "Seamless" else "Weld." for f in pms.fittings_by_size]
    _write_merged_data_row(ws, row, "Type", type_values, col_start=3,
                           total_cols=total_cols, fill=DATA_FILL)
    row += 1

    # Fitting properties (MOC, Elbow, Tee, etc.) — with auto-merge
    fitting_props = [
        ("MOC", lambda f: f.material_spec),
        ("Elbow", lambda f: f.elbow_standard),
        ("Tee", lambda f: f.tee_standard),
        ("Red.", lambda f: f.reducer_standard),
        ("Cap", lambda f: f.cap_standard),
        ("Plug", lambda f: f.plug_standard),
        ("Weldolet", lambda f: f.weldolet_spec),
    ]

    for prop_idx, (label, getter) in enumerate(fitting_props):
        fill = ALT_FILL if prop_idx % 2 == 0 else DATA_FILL
        prop_values = [getter(f) or "" for f in pms.fittings_by_size]
        _write_merged_data_row(ws, row, label, prop_values, col_start=3,
                               total_cols=total_cols, fill=fill)
        row += 1

    row += 1

    # === EXTRA FITTINGS ===
    ef = pms.extra_fittings
    extra_items = [
        ("Coupling", ef.coupling), ("Hex Head Plug", ef.hex_plug),
        ("Union (Small Bore)", ef.union), ("Union (Large Bore)", ef.union_large),
        ("Olet (Small Bore)", ef.olet), ("Olet (Large Bore)", ef.olet_large),
        ("Swage", ef.swage),
    ]
    # Collect pipe sizes for size header rows in subsequent sections
    pipe_sizes = [p.size_inch for p in pms.pipe_data]

    has_extra = any(v for _, v in extra_items)
    if has_extra:
        _write_section_header(ws, row, "Extra Fittings", col_end=total_cols)
        row += 1
        _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
        row += 1
        for i, (label, value) in enumerate(extra_items):
            if value:
                fill = ALT_FILL if i % 2 == 0 else DATA_FILL
                _write_label_value_row(ws, row, label, value, col_end=total_cols)
                for c in range(1, total_cols + 1):
                    ws.cell(row=row, column=c).fill = fill
                row += 1
        row += 1

    # === FLANGE DATA ===
    _write_section_header(ws, row, "Flange", col_end=total_cols)
    row += 1
    _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
    row += 1

    flange_rows = [
        ("MOC", pms.flange.material_spec),
        ("Face", pms.flange.face_type),
        ("Type", pms.flange.flange_type),
        ("Standard", pms.flange.standard),
    ]
    for i, (label, value) in enumerate(flange_rows):
        fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        _write_label_value_row(ws, row, label, value, col_end=total_cols)
        for c in range(1, total_cols + 1):
            ws.cell(row=row, column=c).fill = fill
        row += 1

    row += 1

    # === SPECTACLE BLIND ===
    _write_section_header(ws, row, "Spectacle Blind / Spacer Blinds", col_end=total_cols)
    row += 1
    _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
    row += 1
    _write_label_value_row(ws, row, "MOC", pms.spectacle_blind.material_spec, col_end=total_cols)
    row += 1
    # Spectacle row — split across pipe size columns (like original spec)
    for c in range(1, total_cols + 1):
        _apply_style(ws, row, c, font=DATA_FONT, fill=ALT_FILL, alignment=CENTER)
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Spectacle"
    if pms.spectacle_blind.standard_large and len(pms.pipe_data) > 0:
        mid = len(pms.pipe_data) // 2
        left_end = pipe_col_start + mid - 1
        right_start = pipe_col_start + mid
        # Left half: standard
        ws.merge_cells(start_row=row, start_column=pipe_col_start, end_row=row, end_column=left_end)
        ws.cell(row=row, column=pipe_col_start).value = pms.spectacle_blind.standard
        # Right half: standard_large
        ws.merge_cells(start_row=row, start_column=right_start, end_row=row, end_column=total_cols)
        ws.cell(row=row, column=right_start).value = pms.spectacle_blind.standard_large
    else:
        ws.merge_cells(start_row=row, start_column=pipe_col_start, end_row=row, end_column=total_cols)
        ws.cell(row=row, column=pipe_col_start).value = pms.spectacle_blind.standard
    ws.row_dimensions[row].height = 25
    row += 2

    # === BOLTS / NUTS / GASKETS ===
    _write_section_header(ws, row, "Bolts / Nuts / Gaskets", col_end=total_cols)
    row += 1
    _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
    row += 1

    bng_rows = [
        ("Stud Bolts", pms.bolts_nuts_gaskets.stud_bolts),
        ("Hex Nuts", pms.bolts_nuts_gaskets.hex_nuts),
        ("Gasket", pms.bolts_nuts_gaskets.gasket),
    ]
    for i, (label, value) in enumerate(bng_rows):
        fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        _write_label_value_row(ws, row, label, value, col_end=total_cols)
        for c in range(1, total_cols + 1):
            ws.cell(row=row, column=c).fill = fill
        row += 1

    row += 1

    # === VALVES ===
    _write_section_header(ws, row, "Valves", col_end=total_cols)
    row += 1
    _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
    row += 1

    valve_types = [
        ("Rating", pms.valves.rating, []),
        ("Ball", pms.valves.ball, pms.valves.ball_by_size),
        ("Gate", pms.valves.gate, pms.valves.gate_by_size),
        ("Globe", pms.valves.globe, pms.valves.globe_by_size),
        ("Check", pms.valves.check, pms.valves.check_by_size),
        ("Butterfly", pms.valves.butterfly, pms.valves.butterfly_by_size),
        ("DBB (Inst)", pms.valves.dbb_inst, pms.valves.dbb_inst_by_size),
        ("DBB", pms.valves.dbb, pms.valves.dbb_by_size),
    ]
    for i, (label, fallback, by_size) in enumerate(valve_types):
        if not fallback and not by_size:
            continue
        fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        if by_size:
            # Size-specific rendering — map codes to pipe size columns
            size_code_map = {e.size_inch: e.code for e in by_size}
            values = [size_code_map.get(s, "") for s in pipe_sizes]
            _write_merged_data_row(ws, row, label, values, col_start=pipe_col_start,
                                   total_cols=total_cols, fill=fill)
        else:
            _write_label_value_row(ws, row, label, fallback, col_end=total_cols)
            for c in range(1, total_cols + 1):
                ws.cell(row=row, column=c).fill = fill
        row += 1

    row += 1

    # === NOTES ===
    if pms.notes:
        _write_section_header(ws, row, "Notes", col_end=total_cols)
        row += 1
        for note in pms.notes:
            _apply_style(ws, row, 1, font=NOTE_FONT, fill=NOTES_FILL, alignment=LEFT)
            ws.cell(row=row, column=1).value = note
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
            for c in range(1, total_cols + 1):
                ws.cell(row=row, column=c).fill = NOTES_FILL
                ws.cell(row=row, column=c).border = THIN_BORDER
            row += 1

    # Print settings
    ws.sheet_properties.pageSetUpPr = None
    ws.print_area = f"A1:{get_column_letter(total_cols)}{row}"

    # === BRANCH CHART SHEETS ===
    if pms.branch_charts:
        for chart in pms.branch_charts:
            _write_branch_chart_sheet(wb, chart)

    wb.save(output_path)
    logger.info("Excel PMS saved to %s", output_path)
    return output_path


def generate_pms_excel_bytes(pms: PMSResponse) -> bytes:
    """Generate Excel PMS and return as bytes (no disk write)."""
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = pms.piping_class

    # Re-use the same generation logic but save to buffer
    num_pipe_cols = max(len(pms.pipe_data), 1)
    total_cols = max(num_pipe_cols + 3, 20)
    pipe_col_start = 3

    from openpyxl.utils import get_column_letter as gcl
    # Column widths — standard PMS spec layout (consistent with generate_pms_excel)
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 13
    for i in range(4, total_cols + 1):
        ws.column_dimensions[gcl(i)].width = 12

    row = 1

    # Title
    for c in range(1, total_cols + 1):
        _apply_style(ws, row, c, font=HEADER_FONT, fill=HEADER_FILL)
    ws.cell(row=row, column=1).value = "PIPING MATERIAL SPECIFICATION"
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
    _apply_style(ws, row, 1, font=Font(name="Arial", bold=True, color="FFFFFF", size=14), fill=HEADER_FILL, alignment=CENTER)
    ws.row_dimensions[row].height = 30
    row += 1

    header_labels = [
        ("Piping Class", pms.piping_class), ("Rating", pms.rating),
        ("Material", pms.material), ("Corrosion Allowance", pms.corrosion_allowance),
        ("Mill Tolerance", pms.mill_tolerance), ("Design Code", pms.design_code),
        ("Service", pms.service), ("Branch Chart", pms.branch_chart),
    ]
    for label, value in header_labels:
        _write_label_value_row(ws, row, label, value, col_end=total_cols)
        row += 1
    row += 1

    # P-T Rating
    _write_section_header(ws, row, "Pressure-Temperature Rating", col_end=total_cols)
    row += 1

    # Temp labels row (e.g. "-29 TO 38")
    pt = pms.pressure_temperature
    if pt.temp_labels:
        _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Temp. Range (°C)"
        for i, lbl in enumerate(pt.temp_labels):
            col = pipe_col_start + i
            if col <= total_cols:
                _apply_style(ws, row, col, font=DATA_FONT, fill=ALT_FILL).value = lbl
        row += 1

    # Temperature values row
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Temp. (°C)"
    for i, temp in enumerate(pt.temperatures):
        col = pipe_col_start + i
        if col <= total_cols:
            _apply_style(ws, row, col, font=DATA_FONT, fill=ALT_FILL).value = temp
    # Hydrotest header in same row
    if pms.hydrotest_pressure:
        ht_col = pipe_col_start + len(pt.temperatures) + 1
        if ht_col <= total_cols:
            _apply_style(ws, row, ht_col, font=LABEL_FONT, fill=ALT_FILL, alignment=CENTER).value = "Hydrotest\n(barg)"
    row += 1

    # Pressure values row
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=DATA_FILL, alignment=LEFT).value = "Press. (barg)"
    for i, press in enumerate(pt.pressures):
        col = pipe_col_start + i
        if col <= total_cols:
            _apply_style(ws, row, col, font=DATA_FONT, fill=DATA_FILL).value = press
    # Hydrotest value
    if pms.hydrotest_pressure:
        ht_col = pipe_col_start + len(pt.temperatures) + 1
        if ht_col <= total_cols:
            _apply_style(ws, row, ht_col, font=DATA_FONT, fill=DATA_FILL, alignment=CENTER).value = pms.hydrotest_pressure
    row += 2

    # Pipe Data
    _write_section_header(ws, row, "Pipe Data", col_end=total_cols)
    row += 1
    if pms.pipe_code:
        _write_label_value_row(ws, row, "Code", pms.pipe_code, col_end=total_cols)
        row += 1

    pipe_rows = [
        ("Size (in)", [p.size_inch for p in pms.pipe_data]),
        ("O.D. (mm)", [p.od_mm for p in pms.pipe_data]),
        ("Schedule", [p.schedule for p in pms.pipe_data]),
        ("W.T. (mm)", [p.wall_thickness_mm for p in pms.pipe_data]),
        ("Type", [p.pipe_type for p in pms.pipe_data]),
        ("MOC", [p.material_spec for p in pms.pipe_data]),
        ("Ends", [p.ends for p in pms.pipe_data]),
    ]
    # Rows that benefit from merging (repeated values across sizes)
    mergeable_pipe_rows = {"Type", "MOC", "Ends"}
    for label, values in pipe_rows:
        is_alt = label in ("Size (in)", "Schedule", "Type", "Ends")
        fill = ALT_FILL if is_alt else DATA_FILL
        if label in mergeable_pipe_rows:
            _write_merged_data_row(ws, row, label, values, col_start=pipe_col_start,
                                   total_cols=total_cols, fill=fill)
        else:
            _apply_style(ws, row, 2, font=LABEL_FONT, fill=fill, alignment=LEFT).value = label
            for i, val in enumerate(values):
                col = pipe_col_start + i
                if col <= total_cols:
                    _apply_style(ws, row, col, font=DATA_FONT, fill=fill).value = val
        row += 1
    row += 1

    # Fittings (SIZE-WISE DATA)
    _write_section_header(ws, row, "Fittings — Butt Weld (SCH to match pipe)", col_end=total_cols)
    row += 1

    # Size columns header
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Size (in)"
    for i, fitting in enumerate(pms.fittings_by_size):
        col = 3 + i
        if col <= total_cols:
            _apply_style(ws, row, col, font=LABEL_FONT, fill=ALT_FILL, alignment=CENTER).value = fitting.size_inch
    for c in range(3 + len(pms.fittings_by_size), total_cols + 1):
        _apply_style(ws, row, c, fill=ALT_FILL)
    row += 1

    # Type row (Seamless/Welded) — with auto-merge
    type_values = ["Seam." if f.type == "Seamless" else "Weld." for f in pms.fittings_by_size]
    _write_merged_data_row(ws, row, "Type", type_values, col_start=3,
                           total_cols=total_cols, fill=DATA_FILL)
    row += 1

    # Fitting properties (MOC, Elbow, Tee, etc.) — with auto-merge
    fitting_props = [
        ("MOC", lambda f: f.material_spec),
        ("Elbow", lambda f: f.elbow_standard),
        ("Tee", lambda f: f.tee_standard),
        ("Red.", lambda f: f.reducer_standard),
        ("Cap", lambda f: f.cap_standard),
        ("Plug", lambda f: f.plug_standard),
        ("Weldolet", lambda f: f.weldolet_spec),
    ]

    for prop_idx, (label, getter) in enumerate(fitting_props):
        fill = ALT_FILL if prop_idx % 2 == 0 else DATA_FILL
        prop_values = [getter(f) or "" for f in pms.fittings_by_size]
        _write_merged_data_row(ws, row, label, prop_values, col_start=3,
                               total_cols=total_cols, fill=fill)
        row += 1
    row += 1

    # Extra Fittings
    ef = pms.extra_fittings
    extra_items = [
        ("Coupling", ef.coupling), ("Hex Head Plug", ef.hex_plug),
        ("Union (Small Bore)", ef.union), ("Union (Large Bore)", ef.union_large),
        ("Olet (Small Bore)", ef.olet), ("Olet (Large Bore)", ef.olet_large),
        ("Swage", ef.swage),
    ]
    # Collect pipe sizes for size header rows in subsequent sections
    pipe_sizes = [p.size_inch for p in pms.pipe_data]

    # Only add section if there's any data
    has_extra = any(v for _, v in extra_items)
    if has_extra:
        _write_section_header(ws, row, "Extra Fittings", col_end=total_cols)
        row += 1
        _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
        row += 1
        for i, (lbl, val) in enumerate(extra_items):
            if val:
                _write_label_value_row(ws, row, lbl, val, col_end=total_cols)
                for c in range(1, total_cols + 1):
                    ws.cell(row=row, column=c).fill = ALT_FILL if i % 2 == 0 else DATA_FILL
                row += 1
        row += 1

    # Flange
    _write_section_header(ws, row, "Flange", col_end=total_cols)
    row += 1
    _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
    row += 1
    flange_items = [
        ("MOC", pms.flange.material_spec), ("Face", pms.flange.face_type),
        ("Type", pms.flange.flange_type), ("Standard", pms.flange.standard),
    ]
    for i, (lbl, val) in enumerate(flange_items):
        _write_label_value_row(ws, row, lbl, val, col_end=total_cols)
        for c in range(1, total_cols + 1):
            ws.cell(row=row, column=c).fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        row += 1
    row += 1

    # Spectacle Blind
    _write_section_header(ws, row, "Spectacle Blind / Spacer Blinds", col_end=total_cols)
    row += 1
    _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
    row += 1
    _write_label_value_row(ws, row, "MOC", pms.spectacle_blind.material_spec, col_end=total_cols)
    row += 1
    # Spectacle row — split across pipe size columns (like original spec)
    for c in range(1, total_cols + 1):
        _apply_style(ws, row, c, font=DATA_FONT, fill=ALT_FILL, alignment=CENTER)
    _apply_style(ws, row, 2, font=LABEL_FONT, fill=ALT_FILL, alignment=LEFT).value = "Spectacle"
    if pms.spectacle_blind.standard_large and len(pms.pipe_data) > 0:
        mid = len(pms.pipe_data) // 2
        left_end = pipe_col_start + mid - 1
        right_start = pipe_col_start + mid
        # Left half: standard
        ws.merge_cells(start_row=row, start_column=pipe_col_start, end_row=row, end_column=left_end)
        ws.cell(row=row, column=pipe_col_start).value = pms.spectacle_blind.standard
        # Right half: standard_large
        ws.merge_cells(start_row=row, start_column=right_start, end_row=row, end_column=total_cols)
        ws.cell(row=row, column=right_start).value = pms.spectacle_blind.standard_large
    else:
        ws.merge_cells(start_row=row, start_column=pipe_col_start, end_row=row, end_column=total_cols)
        ws.cell(row=row, column=pipe_col_start).value = pms.spectacle_blind.standard
    ws.row_dimensions[row].height = 25
    row += 2

    # Bolts/Nuts/Gaskets
    _write_section_header(ws, row, "Bolts / Nuts / Gaskets", col_end=total_cols)
    row += 1
    _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
    row += 1
    for i, (lbl, val) in enumerate([
        ("Stud Bolts", pms.bolts_nuts_gaskets.stud_bolts),
        ("Hex Nuts", pms.bolts_nuts_gaskets.hex_nuts),
        ("Gasket", pms.bolts_nuts_gaskets.gasket),
    ]):
        _write_label_value_row(ws, row, lbl, val, col_end=total_cols)
        for c in range(1, total_cols + 1):
            ws.cell(row=row, column=c).fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        row += 1
    row += 1

    # Valves
    _write_section_header(ws, row, "Valves", col_end=total_cols)
    row += 1
    _write_size_header_row(ws, row, pipe_sizes, col_start=pipe_col_start, total_cols=total_cols)
    row += 1
    valve_types = [
        ("Rating", pms.valves.rating, []),
        ("Ball", pms.valves.ball, pms.valves.ball_by_size),
        ("Gate", pms.valves.gate, pms.valves.gate_by_size),
        ("Globe", pms.valves.globe, pms.valves.globe_by_size),
        ("Check", pms.valves.check, pms.valves.check_by_size),
        ("Butterfly", pms.valves.butterfly, pms.valves.butterfly_by_size),
        ("DBB (Inst)", pms.valves.dbb_inst, pms.valves.dbb_inst_by_size),
        ("DBB", pms.valves.dbb, pms.valves.dbb_by_size),
    ]
    for i, (lbl, fallback, by_size) in enumerate(valve_types):
        if not fallback and not by_size:
            continue
        fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        if by_size:
            size_code_map = {e.size_inch: e.code for e in by_size}
            values = [size_code_map.get(s, "") for s in pipe_sizes]
            _write_merged_data_row(ws, row, lbl, values, col_start=pipe_col_start,
                                   total_cols=total_cols, fill=fill)
        else:
            _write_label_value_row(ws, row, lbl, fallback, col_end=total_cols)
            for c in range(1, total_cols + 1):
                ws.cell(row=row, column=c).fill = fill
        row += 1

    # Notes
    if pms.notes:
        row += 1
        _write_section_header(ws, row, "Notes", col_end=total_cols)
        row += 1
        for note in pms.notes:
            ws.cell(row=row, column=1).value = note
            ws.cell(row=row, column=1).font = NOTE_FONT
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
            for c in range(1, total_cols + 1):
                ws.cell(row=row, column=c).fill = NOTES_FILL
                ws.cell(row=row, column=c).border = THIN_BORDER
            row += 1

    # === BRANCH CHART SHEETS ===
    if pms.branch_charts:
        for chart in pms.branch_charts:
            _write_branch_chart_sheet(wb, chart)

    wb.save(buf)
    return buf.getvalue()


def _write_branch_chart_sheet(wb, chart):
    """Write a branch connection chart as a separate Excel sheet."""
    ws = wb.create_sheet(title=f"Chart {chart.chart_id}")

    # Color map for connection types
    FILL_T = PatternFill("solid", fgColor="C6EFCE")    # Green — Tee
    FILL_W = PatternFill("solid", fgColor="FFC7CE")    # Red — Weldolet
    FILL_H = PatternFill("solid", fgColor="FFEB9C")    # Yellow — Threadolet
    FILL_S = PatternFill("solid", fgColor="B4C6E7")    # Blue — Sockolet
    FILL_RT = PatternFill("solid", fgColor="D9E2F3")   # Light blue — Reducing Tee
    FILL_DASH = PatternFill("solid", fgColor="D9D9D9")  # Grey — N/A
    FILL_EMPTY = PatternFill("solid", fgColor="F2F2F2")  # Light grey — empty
    FILL_MAP = {"T": FILL_T, "W": FILL_W, "H": FILL_H, "S": FILL_S, "RT": FILL_RT, "-": FILL_DASH}

    CHART_FONT = Font(name="Arial", bold=True, size=9)
    CELL_FONT = Font(name="Arial", bold=True, size=9)
    HEADER_CELL = Font(name="Arial", bold=True, color="FFFFFF", size=9)

    run_sizes = chart.run_sizes
    branch_sizes = chart.branch_sizes

    # Title row
    row = 1
    ws.cell(row=row, column=1).value = f"BRANCH CONNECTION CHART {chart.chart_id} — {chart.title}"
    ws.cell(row=row, column=1).font = Font(name="Arial", bold=True, color="1F4E79", size=12)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(branch_sizes) + 2)
    ws.row_dimensions[row].height = 28
    row += 1

    ws.cell(row=row, column=1).value = "Branch Table as per API RP 14E"
    ws.cell(row=row, column=1).font = Font(name="Arial", italic=True, size=9, color="666666")
    row += 1

    # Column header: "RUN \\ BRANCH" then branch sizes
    ws.cell(row=row, column=1).value = "RUN \\ BRANCH"
    ws.cell(row=row, column=1).font = HEADER_CELL
    ws.cell(row=row, column=1).fill = HEADER_FILL
    ws.cell(row=row, column=1).alignment = CENTER
    ws.cell(row=row, column=1).border = THIN_BORDER
    ws.column_dimensions["A"].width = 14

    for j, bs in enumerate(branch_sizes):
        col = j + 2
        cell = ws.cell(row=row, column=col)
        cell.value = bs
        cell.font = HEADER_CELL
        cell.fill = HEADER_FILL
        cell.alignment = CENTER
        cell.border = THIN_BORDER
        ws.column_dimensions[get_column_letter(col)].width = 7
    row += 1

    # Data rows
    for i, rs in enumerate(run_sizes):
        # Run size header
        cell = ws.cell(row=row, column=1)
        cell.value = rs
        cell.font = CHART_FONT
        cell.fill = SECTION_FILL
        cell.alignment = CENTER
        cell.border = THIN_BORDER

        # Grid cells
        grid_row = chart.grid[i] if i < len(chart.grid) else []
        for j in range(len(branch_sizes)):
            col = j + 2
            val = grid_row[j] if j < len(grid_row) else ""
            cell = ws.cell(row=row, column=col)
            cell.value = val
            cell.font = CELL_FONT
            cell.alignment = CENTER
            cell.border = THIN_BORDER
            cell.fill = FILL_MAP.get(val, FILL_EMPTY)
        row += 1

    row += 1

    # Legend
    ws.cell(row=row, column=1).value = "LEGEND"
    ws.cell(row=row, column=1).font = Font(name="Arial", bold=True, size=10, color="1F4E79")
    row += 1
    for code, desc in chart.legend.items():
        ws.cell(row=row, column=1).value = code
        ws.cell(row=row, column=1).font = CELL_FONT
        ws.cell(row=row, column=1).alignment = CENTER
        ws.cell(row=row, column=1).fill = FILL_MAP.get(code, DATA_FILL)
        ws.cell(row=row, column=1).border = THIN_BORDER
        ws.cell(row=row, column=2).value = desc
        ws.cell(row=row, column=2).font = Font(name="Arial", size=9)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        row += 1

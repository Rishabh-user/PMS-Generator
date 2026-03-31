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


def _write_label_value_row(ws, row: int, label: str, value: str, col_start: int = 1, val_col: int = 3, col_end: int = 20):
    """Write a label-value pair row."""
    _apply_style(ws, row, col_start, font=LABEL_FONT, fill=DATA_FILL, alignment=LEFT).value = label
    for c in range(col_start + 1, val_col):
        _apply_style(ws, row, c, fill=DATA_FILL)
    _apply_style(ws, row, val_col, font=DATA_FONT, fill=DATA_FILL, alignment=LEFT).value = value
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

    # Set column widths - improved for better alignment (matching screenshot layout)
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 11
    for i in range(4, total_cols + 1):
        ws.column_dimensions[get_column_letter(i)].width = 10

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

    for label, values in pipe_rows:
        is_alt = label in ("Size (in)", "Schedule", "Type", "Ends")
        fill = ALT_FILL if is_alt else DATA_FILL
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

    # === FITTINGS (PRIMARY) ===
    ft_label = pms.fittings.fitting_type or "Fittings"
    _write_section_header(ws, row, f"Fittings — {ft_label}", col_end=total_cols)
    row += 1

    fitting_rows = [
        ("Type", pms.fittings.fitting_type),
        ("MOC", pms.fittings.material_spec),
        ("Elbow", pms.fittings.elbow_standard),
        ("Tee", pms.fittings.tee_standard),
        ("Reducer", pms.fittings.reducer_standard),
        ("Cap", pms.fittings.cap_standard),
        ("Plug", pms.fittings.plug_standard),
        ("Weldolet", pms.fittings.weldolet_spec),
    ]
    for i, (label, value) in enumerate(fitting_rows):
        fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        _write_label_value_row(ws, row, label, value, col_end=total_cols)
        for c in range(1, total_cols + 1):
            ws.cell(row=row, column=c).fill = fill
        row += 1

    row += 1

    # === FITTINGS (SECONDARY) ===
    if pms.fittings_welded:
        fw_label = pms.fittings_welded.fitting_type or "Fittings (Welded)"
        _write_section_header(ws, row, f"Fittings — {fw_label}", col_end=total_cols)
        row += 1

        for i, (label, value) in enumerate([
            ("Type", pms.fittings_welded.fitting_type),
            ("MOC", pms.fittings_welded.material_spec),
            ("Elbow", pms.fittings_welded.elbow_standard),
            ("Tee", pms.fittings_welded.tee_standard),
            ("Reducer", pms.fittings_welded.reducer_standard),
            ("Cap", pms.fittings_welded.cap_standard),
            ("Plug", pms.fittings_welded.plug_standard),
            ("Weldolet", pms.fittings_welded.weldolet_spec),
        ]):
            fill = ALT_FILL if i % 2 == 0 else DATA_FILL
            _write_label_value_row(ws, row, label, value, col_end=total_cols)
            for c in range(1, total_cols + 1):
                ws.cell(row=row, column=c).fill = fill
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
    has_extra = any(v for _, v in extra_items)
    if has_extra:
        _write_section_header(ws, row, "Extra Fittings", col_end=total_cols)
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
    _write_label_value_row(ws, row, "MOC", pms.spectacle_blind.material_spec, col_end=total_cols)
    row += 1
    _write_label_value_row(ws, row, "Standard", pms.spectacle_blind.standard, col_end=total_cols)
    for c in range(1, total_cols + 1):
        ws.cell(row=row, column=c).fill = ALT_FILL
    row += 2

    # === BOLTS / NUTS / GASKETS ===
    _write_section_header(ws, row, "Bolts / Nuts / Gaskets", col_end=total_cols)
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

    valve_rows = [
        ("Rating", pms.valves.rating),
        ("Ball", pms.valves.ball),
        ("Gate", pms.valves.gate),
        ("Globe", pms.valves.globe),
        ("Check", pms.valves.check),
        ("Butterfly", pms.valves.butterfly),
    ]
    for i, (label, value) in enumerate(valve_rows):
        fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        _write_label_value_row(ws, row, label, value, col_end=total_cols)
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
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 11
    for i in range(4, total_cols + 1):
        ws.column_dimensions[gcl(i)].width = 10

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
    for label, values in pipe_rows:
        is_alt = label in ("Size (in)", "Schedule", "Type", "Ends")
        fill = ALT_FILL if is_alt else DATA_FILL
        _apply_style(ws, row, 2, font=LABEL_FONT, fill=fill, alignment=LEFT).value = label
        for i, val in enumerate(values):
            col = pipe_col_start + i
            if col <= total_cols:
                _apply_style(ws, row, col, font=DATA_FONT, fill=fill).value = val
        row += 1
    row += 1

    # Fittings (Primary)
    ft_label = pms.fittings.fitting_type or "Fittings"
    _write_section_header(ws, row, f"Fittings — {ft_label}", col_end=total_cols)
    row += 1

    for i, (lbl, val) in enumerate([
        ("Type", pms.fittings.fitting_type), ("MOC", pms.fittings.material_spec),
        ("Elbow", pms.fittings.elbow_standard), ("Tee", pms.fittings.tee_standard),
        ("Reducer", pms.fittings.reducer_standard), ("Cap", pms.fittings.cap_standard),
        ("Plug", pms.fittings.plug_standard), ("Weldolet", pms.fittings.weldolet_spec),
    ]):
        _write_label_value_row(ws, row, lbl, val, col_end=total_cols)
        for c in range(1, total_cols + 1):
            ws.cell(row=row, column=c).fill = ALT_FILL if i % 2 == 0 else DATA_FILL
        row += 1
    row += 1

    # Fittings (Secondary)
    if pms.fittings_welded:
        fw_label = pms.fittings_welded.fitting_type or "Fittings (Welded)"
        _write_section_header(ws, row, f"Fittings — {fw_label}", col_end=total_cols)
        row += 1

        for i, (lbl, val) in enumerate([
            ("Type", pms.fittings_welded.fitting_type), ("MOC", pms.fittings_welded.material_spec),
            ("Elbow", pms.fittings_welded.elbow_standard), ("Tee", pms.fittings_welded.tee_standard),
            ("Reducer", pms.fittings_welded.reducer_standard), ("Cap", pms.fittings_welded.cap_standard),
            ("Plug", pms.fittings_welded.plug_standard), ("Weldolet", pms.fittings_welded.weldolet_spec),
        ]):
            _write_label_value_row(ws, row, lbl, val, col_end=total_cols)
            for c in range(1, total_cols + 1):
                ws.cell(row=row, column=c).fill = ALT_FILL if i % 2 == 0 else DATA_FILL
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
    # Only add section if there's any data
    has_extra = any(v for _, v in extra_items)
    if has_extra:
        _write_section_header(ws, row, "Extra Fittings", col_end=total_cols)
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
    _write_label_value_row(ws, row, "MOC", pms.spectacle_blind.material_spec, col_end=total_cols)
    row += 1
    _write_label_value_row(ws, row, "Standard", pms.spectacle_blind.standard, col_end=total_cols)
    for c in range(1, total_cols + 1):
        ws.cell(row=row, column=c).fill = ALT_FILL
    row += 2

    # Bolts/Nuts/Gaskets
    _write_section_header(ws, row, "Bolts / Nuts / Gaskets", col_end=total_cols)
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
    for i, (lbl, val) in enumerate([
        ("Rating", pms.valves.rating), ("Ball", pms.valves.ball),
        ("Gate", pms.valves.gate), ("Globe", pms.valves.globe),
        ("Check", pms.valves.check), ("Butterfly", pms.valves.butterfly),
    ]):
        _write_label_value_row(ws, row, lbl, val, col_end=total_cols)
        for c in range(1, total_cols + 1):
            ws.cell(row=row, column=c).fill = ALT_FILL if i % 2 == 0 else DATA_FILL
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

    wb.save(buf)
    return buf.getvalue()

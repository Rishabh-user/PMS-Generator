"""
Branch Connection Charts from Appendix-1 of the PMS document.

These are project-level reference charts (not class-specific data).
They define which branch connection type (Tee, Weldolet, Threadolet, etc.)
to use based on run pipe size and branch pipe size.

Source: 50501-SPE-80000-PP-ET-0001, Appendix 1 — Branch Table (API RP 14E)
"""
from app.models.pms_models import BranchChart


def _build_chart_1() -> BranchChart:
    """Chart 1: CS, LTCS, SS, DSS, SDSS — Branch Table as per API RP 14E."""
    branch = ["<=1", "1.5", "2", "3", "4", "6", "8", "10", "12", "14", "16", "18", "20", "22", "24", "30", "32"]
    run =    ["<=1", "1.5", "2", "3", "4", "6", "8", "10", "12", "14", "16", "18", "20", "22", "24", "30", "32"]
    # grid[run_idx][branch_idx] — read row by row from PDF
    grid = [
        ["T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # <=1
        ["T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 1.5
        ["W", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 2
        ["W", "W", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 3
        ["W", "W", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 4
        ["W", "W", "W", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 6
        ["W", "W", "W", "W", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 8
        ["W", "W", "W", "W", "W", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 10
        ["W", "W", "W", "W", "W", "T", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  ""],   # 12
        ["W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "",  "",  "",  "",  "",  "",  ""],   # 14
        ["W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "",  "",  "",  "",  "",  ""],   # 16
        ["W", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "",  "",  "",  "",  ""],   # 18
        ["W", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", "",  "",  "",  ""],   # 20
        ["W", "W", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", "",  "",  ""],   # 22
        ["W", "W", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", "T", "",  ""],   # 24
        ["W", "W", "W", "W", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", ""],   # 30
        ["W", "W", "W", "W", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", "T"],  # 32
    ]
    return BranchChart(
        chart_id="1",
        title="CS, LTCS, SS, DSS, SDSS",
        run_sizes=run,
        branch_sizes=branch,
        grid=grid,
        legend={"W": "WELDOLET", "T": "TEE"},
    )


def _build_chart_2() -> BranchChart:
    """Chart 2: CS GALV."""
    branch = ["<=1", "1.5", "2", "3", "4", "6", "8", "10", "12", "14", "16", "18", "20", "24"]
    run =    ["<=1", "1.5", "2", "3", "4", "6", "8", "10", "12", "14", "16", "18", "20", "24"]
    grid = [
        ["T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # <=1
        ["T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 1.5
        ["H", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 2
        ["H", "H", "H", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 3
        ["H", "H", "H", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 4
        ["H", "H", "H", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  ""],   # 6
        ["H", "H", "H", "W", "T", "T", "T", "",  "",  "",  "",  "",  "",  ""],   # 8
        ["H", "H", "H", "W", "W", "T", "T", "T", "",  "",  "",  "",  "",  ""],   # 10
        ["H", "H", "H", "W", "W", "T", "T", "T", "T", "",  "",  "",  "",  ""],   # 12
        ["H", "H", "H", "W", "W", "W", "T", "T", "T", "T", "",  "",  "",  ""],   # 14
        ["H", "H", "H", "W", "W", "W", "T", "T", "T", "T", "T", "",  "",  ""],   # 16
        ["H", "H", "H", "W", "W", "W", "W", "T", "T", "T", "T", "T", "",  ""],   # 18
        ["H", "H", "H", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", ""],   # 20
        ["H", "H", "H", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T"],  # 24
    ]
    return BranchChart(
        chart_id="2",
        title="CS GALV",
        run_sizes=run,
        branch_sizes=branch,
        grid=grid,
        legend={"H": "THREADOLET", "W": "WELDOLET", "T": "TEE"},
    )


def _build_chart_3() -> BranchChart:
    """Chart 3: CuNi."""
    branch = ["<=1", "1.5", "2", "3", "4", "6", "8", "10", "12", "14", "16", "18", "20", "24", "28", "32", "36"]
    run =    ["<=1", "1.5", "2", "3", "4", "6", "8", "10", "12", "14", "16", "18", "20", "24", "28", "32", "36"]
    grid = [
        ["T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # <=1
        ["T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 1.5
        ["S", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 2
        ["S", "S", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 3
        ["S", "S", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 4
        ["S", "S", "W", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 6
        ["S", "S", "W", "W", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 8
        ["S", "S", "W", "W", "W", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  "",  ""],   # 10
        ["S", "S", "W", "W", "W", "T", "T", "T", "T", "",  "",  "",  "",  "",  "",  "",  ""],   # 12
        ["S", "S", "W", "W", "W", "W", "T", "T", "T", "T", "",  "",  "",  "",  "",  "",  ""],   # 14
        ["S", "S", "W", "W", "W", "W", "T", "T", "T", "T", "T", "",  "",  "",  "",  "",  ""],   # 16
        ["S", "S", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "",  "",  "",  "",  ""],   # 18
        ["S", "S", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", "",  "",  "",  ""],   # 20
        ["S", "S", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", "",  "",  ""],   # 24
        ["S", "S", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", "T", "",  ""],   # 28
        ["S", "S", "W", "W", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", ""],   # 32
        ["S", "S", "W", "W", "W", "W", "W", "W", "W", "W", "T", "T", "T", "T", "T", "T", "T"],  # 36
    ]
    return BranchChart(
        chart_id="3",
        title="CuNi",
        run_sizes=run,
        branch_sizes=branch,
        grid=grid,
        legend={"S": "SOCKOLET", "W": "WELDOLET", "T": "TEE BW"},
    )


def _build_chart_4() -> BranchChart:
    """Chart 4: GRE."""
    branch = ["<=1", "1.5", "2", "3", "4", "6", "8", "10", "12", "14", "16", "18", "20", "24"]
    run =    ["0.75", "1", "2", "3", "4", "6", "8", "10", "12", "14", "16", "18", "20", "24"]
    grid = [
        ["-",  "",   "",   "",   "",   "",   "",   "",   "",   "",   "",   "",   "",   ""],   # 0.75
        ["-",  "T",  "",   "",   "",   "",   "",   "",   "",   "",   "",   "",   "",   ""],   # 1
        ["-",  "RT", "T",  "",   "",   "",   "",   "",   "",   "",   "",   "",   "",   ""],   # 2
        ["-",  "RT", "RT", "T",  "",   "",   "",   "",   "",   "",   "",   "",   "",   ""],   # 3
        ["-",  "RT", "RT", "RT", "T",  "",   "",   "",   "",   "",   "",   "",   "",   ""],   # 4
        ["-",  "S",  "S",  "RT", "RT", "T",  "",   "",   "",   "",   "",   "",   "",   ""],   # 6
        ["-",  "S",  "S",  "RT", "RT", "RT", "T",  "",   "",   "",   "",   "",   "",   ""],   # 8
        ["-",  "S",  "S",  "RT", "RT", "RT", "RT", "T",  "",   "",   "",   "",   "",   ""],   # 10
        ["-",  "S",  "S",  "RT", "RT", "RT", "RT", "RT", "T",  "",   "",   "",   "",   ""],   # 12
        ["-",  "S",  "S",  "RT", "RT", "RT", "RT", "RT", "RT", "T",  "",   "",   "",   ""],   # 14
        ["-",  "S",  "S",  "RT", "RT", "RT", "RT", "RT", "RT", "RT", "T",  "",   "",   ""],   # 16
        ["-",  "S",  "S",  "RT", "RT", "RT", "RT", "RT", "RT", "RT", "RT", "T",  "",   ""],   # 18
        ["-",  "S",  "S",  "RT", "RT", "RT", "RT", "RT", "RT", "RT", "RT", "RT", "T",  ""],   # 20
        ["-",  "S",  "S",  "RT", "RT", "RT", "RT", "RT", "RT", "RT", "RT", "RT", "RT", "T"],  # 24
    ]
    return BranchChart(
        chart_id="4",
        title="GRE",
        run_sizes=run,
        branch_sizes=branch,
        grid=grid,
        legend={"T": "EQUAL TEE", "RT": "REDUCING TEE", "S": "REDUCING SADDLE", "-": "NOT APPLICABLE"},
    )


# Pre-built chart instances
ALL_CHARTS = {
    "1": _build_chart_1(),
    "2": _build_chart_2(),
    "3": _build_chart_3(),
    "4": _build_chart_4(),
}


def get_branch_chart(chart_id: str) -> BranchChart | None:
    """Get a branch chart by ID."""
    return ALL_CHARTS.get(chart_id)


def get_charts_for_class(piping_class: str) -> list[BranchChart]:
    """Determine which branch charts apply to a piping class based on its material family."""
    cls = piping_class.upper()

    # GALV classes → Chart 2
    if any(cls.startswith(pfx) for pfx in ["A3", "A4", "A5", "A6", "B4", "D4"]):
        return [ALL_CHARTS["2"]]

    # CuNi → Chart 3
    if cls.startswith("A30"):
        return [ALL_CHARTS["3"]]

    # GRE → Chart 4
    if any(cls.startswith(pfx) for pfx in ["A50", "A51", "A52"]):
        return [ALL_CHARTS["4"]]

    # All other mainstream classes (CS, LTCS, SS, DSS, SDSS) → Chart 1
    return [ALL_CHARTS["1"]]


def get_all_charts() -> list[BranchChart]:
    """Get all branch charts."""
    return list(ALL_CHARTS.values())

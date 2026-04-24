# PMS-Generator

A FastAPI backend that generates **Piping Material Specifications (PMS)** for oil & gas / energy projects. Combines an embedded ASME pressure-temperature database, ASME B31.3 / B36.10M / B36.19M engineering rules, Anthropic Claude for narrative-level field generation, and PostgreSQL for persistent caching with revision tracking.

Serves its own HTML UI at `/` and an Admin DB browser at `/admin`. The same API also powers the React frontend at `SPE-Valvesheet-Frontend-Staging`.

---

## Features

- **Per-class PMS generation** — 91 piping classes covering CS / LTCS / SS / DSS / SDSS / CuNi / Copper / GRE / CPVC / Titanium / Tubing
- **Two-layer cache** (L1 in-memory dict + L2 PostgreSQL) — once a class is generated, subsequent requests are instant
- **Revision control** — each `piping_class` is one row in `pms_cache`; regenerate bumps the version (`A0` → `A1` → `A2` …)
- **ASME-accurate pipe data** — WT / OD replaced from B36.10M / B36.19M lookup tables; schedules marked `-` get WT computed per B31.3 §304.1.2 Eq. 3a
- **Wall-thickness calculator** — per-size MAWP, margin, governs (Pressure / PMS-minimum / Calculated), Case-1 vs Case-2 selection
- **Excel export** — single-sheet datasheet matching the reference PMS layout, with revision stamp and branch charts (Appendix-1)
- **Bulk ZIP download** — generate up to 50 classes in parallel and stream back a single archive
- **PMS Agent chat** — Claude-backed Q&A over PMS data with saved sessions per user
- **Validation report** — rule-based QA on a generated PMS
- **Admin UI** — browse `pms_cache` + `pms_agent_sessions` tables, inspect JSON payloads, delete rows

---

## Requirements

- **Python** 3.11 or 3.12 (install from [python.org](https://www.python.org/downloads/windows/) — not the Windows Store build)
- **PostgreSQL** 14+ running on `localhost:5435` (or change `DATABASE_URL`)
- **Anthropic API key** (Claude Sonnet 4)

---

## Setup (Windows, cmd.exe)

### 1. Install Python and confirm it's on PATH

During install, tick **"Add python.exe to PATH"**.

```cmd
python --version
```

### 2. Clone and enter the project

```cmd
cd /d D:\targeticon\pms-generator
```

### 3. Create a virtualenv and install dependencies

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Create `.env` in the project root

```env
ANTHROPIC_API_KEY=sk-ant-api03-...
APP_HOST=0.0.0.0
APP_PORT=8003
LOG_LEVEL=INFO
DATA_DIR=data
DATABASE_URL=postgresql://postgres:postgres@localhost:5435/pms_generator
```

### 5. Start Postgres

If using Docker:

```cmd
docker run -d --name pms-postgres -p 5435:5432 ^
  -e POSTGRES_PASSWORD=postgres ^
  -e POSTGRES_DB=pms_generator ^
  postgres:16
```

The app creates the `pms_cache` and `pms_agent_sessions` tables on first boot. A built-in migration normalizes legacy rows (uppercases `piping_class`, adds the `version` column, dedupes).

### 6. Run the server

```cmd
python run.py
```

Server starts on `http://localhost:8003` (port controlled by `APP_PORT`).

---

## Next time

```cmd
cd /d D:\targeticon\pms-generator
.venv\Scripts\activate.bat
python run.py
```

---

## URLs

| Path | Purpose |
|------|---------|
| `http://localhost:8003/` | Main UI (Generate PMS, browse classes, download Excel) |
| `http://localhost:8003/admin` | Database browser — `pms_cache` + `pms_agent_sessions` |
| `http://localhost:8003/health` | Health + config check |
| `http://localhost:8003/docs` | FastAPI auto-generated Swagger docs |

---

## Key API endpoints

All under `/api`.

### PMS lifecycle
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/preview-pms` | Class metadata + P-T + form defaults (no AI) |
| `POST` | `/generate-pms` | Cache-aware — returns stored row if present, else AI-generates |
| `POST` | `/regenerate-pms` | Force fresh AI, bump version, overwrite cache |
| `POST` | `/download-excel` | XLSX for one class |
| `POST` | `/download-excel-zip` | ZIP of up to 50 XLSX files |
| `POST` | `/validate-pms` | Rule-based validation report |
| `POST` | `/clear-cache` | Wipe L1 + L2 |
| `POST` | `/compute-thickness` | Wall-thickness table with MAWP, margin, governs |

### Data
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/pipe-classes` | Full class list |
| `GET` | `/pipe-classes/codes` | Class codes only |
| `GET` | `/index-data` | Cascading-dropdown data |
| `GET` | `/cached-classes` | Classes present in `pms_cache` |
| `GET` | `/engineering-constants` | Constants used by the frontend |
| `GET` | `/branch-charts` | All branch-connection charts |
| `GET` | `/branch-charts/{chart_id}` | One branch chart |

### Admin DB browser (used by `/admin`)
| Method | Path |
|--------|------|
| `GET` | `/admin/db/stats` |
| `GET` | `/admin/db/pms-cache` |
| `GET` | `/admin/db/pms-cache/{piping_class}` |
| `DELETE` | `/admin/db/pms-cache/{piping_class}` |
| `GET` | `/admin/db/agent-sessions` |
| `GET` | `/admin/db/agent-sessions/{session_id}?user_id=...` |
| `DELETE` | `/admin/db/agent-sessions/{session_id}` |

### PMS Agent
| Method | Path |
|--------|------|
| `POST` | `/pms-agent/chat` |
| `GET` | `/pms-agent/sessions` |
| `GET` | `/pms-agent/sessions/{session_id}` |
| `PUT` | `/pms-agent/sessions/{session_id}` |
| `DELETE` | `/pms-agent/sessions/{session_id}` |

---

## Caching & versioning

- **L1 (in-memory)** — unbounded dict, process-scoped, no TTL. Survives for the lifetime of the uvicorn process.
- **L2 (PostgreSQL `pms_cache`)** — one row per `piping_class` (PK), never expires. Holds the full `PMSResponse` as `JSONB`.
- **Regenerate behaviour** — `POST /regenerate-pms` runs AI, then the DB upsert bumps `version` (`A0` → `A1` → `A2` …) and overwrites `response_json`. The Excel header pulls `version` into the `Rev:` cell.
- **Lookups** are case- and whitespace-insensitive (`UPPER(TRIM(piping_class))`), so `A1`, `a1`, and `A1 ` all resolve to the same row.

The only ways an entry disappears:
1. Admin UI → Delete button
2. `POST /api/clear-cache` (wipes L1 + L2)
3. Manual `DELETE FROM pms_cache` in SQL

---

## Project layout

```
pms-generator/
├── app/
│   ├── main.py                 # FastAPI app, lifespan hooks, UI routes
│   ├── config.py               # Pydantic settings loaded from .env
│   ├── data/
│   │   └── pipe_classes.json   # 91 classes + P-T tables (source of truth)
│   ├── models/                 # Pydantic request/response schemas
│   │   ├── pms_models.py
│   │   ├── thickness_models.py
│   │   ├── validation_models.py
│   │   └── pms_agent_models.py
│   ├── routes/
│   │   └── pms_routes.py       # All /api/* endpoints
│   ├── services/
│   │   ├── pms_service.py      # Cache-aware generate/regenerate
│   │   ├── ai_service.py       # Anthropic Claude wrapper
│   │   ├── db_service.py       # asyncpg pool + pms_cache schema + migration
│   │   ├── data_service.py     # pipe_classes.json loader
│   │   ├── excel_generator.py  # openpyxl datasheet builder
│   │   ├── thickness_service.py# B31.3 Eq. 3a + MAWP + governs
│   │   ├── validation_service.py
│   │   ├── branch_chart_service.py
│   │   └── pms_agent_service.py
│   ├── utils/
│   │   ├── pipe_data.py        # ASME B36.10M/19M lookup + WT correction
│   │   ├── engineering.py      # P-T interpolation helpers
│   │   └── engineering_constants.py
│   ├── static/
│   │   ├── css/styles.css
│   │   ├── js/app.js           # Built-in UI — cache-busted via ?v=N
│   │   └── images/
│   └── templates/
│       ├── index.html          # Main UI
│       └── admin.html          # DB browser
├── requirements.txt
├── run.py                      # `python run.py` entry point
├── .env                        # Not checked in
└── README.md
```

---

## Engineering conventions baked in

- **Mill tolerance** — fixed **12.5 %** (ASME B36.10M seamless), never AI-supplied
- **Hydrotest pressure** — `max(P) × 1.5` from the class P-T envelope
- **Y coefficient** — 0.4 (ferritic, T < 482 °C). Flagged at higher temperatures.
- **Joint efficiency E** — 1.0 for seamless / 100 % RT EFW, 0.85 for plain EFW or ERW
- **Corrosion allowance** — string like `"3 mm"` or `"6 mm"`, parsed to float by `_parse_corrosion_allowance_mm`
- **Schedules** — real code (e.g. `SCH XXS`, `160`, `80S`, `STD`, `XS`) → WT looked up from ASME tables; `-` / blank → WT computed per B31.3 Eq. 3a and Sel. Thk mirrors the rounded Calc. Thk T value

---

## Troubleshooting

**`python` not recognised** — Python wasn't added to PATH during install. Reinstall and tick *"Add python.exe to PATH"*, or use `py` instead of `python`.

**venv's `python.exe` disappears after Windows updates** — don't use the Windows Store Python. Install from python.org and recreate the venv.

**`PostgreSQL cache disabled — using AI-only mode`** at startup — `DATABASE_URL` is missing or Postgres isn't reachable. The server still works but every request hits the AI.

**Browser shows old JS after an `app.js` edit** — bump the cache-buster in `templates/index.html` (`app.js?v=12` → `app.js?v=13`) and hard-refresh (Ctrl + Shift + R).

**"Still AI-generating when row exists"** — confirm the migration ran (startup log will mention `pms_cache: added version column` the first time). The `get_cached_pms` lookup is case-insensitive so any row for that class will hit.

---

## Stopping / restarting

- **Stop:** `Ctrl + C` in the terminal running `python run.py`
- **Restart:** same command again — `--reload` is enabled in `run.py`, so most code changes auto-reload without a manual restart

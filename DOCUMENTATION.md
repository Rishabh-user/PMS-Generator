# PMS Generator — Complete Technical Documentation

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture & Tech Stack](#2-architecture--tech-stack)
3. [Directory Structure](#3-directory-structure)
4. [Data Sources — Where Does the Data Come From?](#4-data-sources--where-does-the-data-come-from)
5. [Application Flow — Step by Step](#5-application-flow--step-by-step)
6. [AI Integration — What Claude Generates](#6-ai-integration--what-claude-generates)
7. [Engineering Calculations — Where Math is Used](#7-engineering-calculations--where-math-is-used)
8. [Caching System — Three-Tier Architecture](#8-caching-system--three-tier-architecture)
9. [Frontend — User Interface & Client-Side Logic](#9-frontend--user-interface--client-side-logic)
10. [API Endpoints Reference](#10-api-endpoints-reference)
11. [Excel Export](#11-excel-export)
12. [Branch Connection Charts](#12-branch-connection-charts)
13. [Engineering Constants — Single Source of Truth](#13-engineering-constants--single-source-of-truth)
14. [Configuration & Environment](#14-configuration--environment)
15. [Key Design Decisions](#15-key-design-decisions)

---

## 1. Project Overview

The **PMS Generator** (Piping Material Specification Generator) is a web application that generates complete piping material specifications compliant with international codes and standards (ASME B31.3, B16.5, B36.10M, B36.19M, NACE MR0175, etc.).

### What Does It Do?

Given a user's selection of:
- **Pressure Rating** (e.g., 150#, 300#, 600#, 900#, 1500#, 2500#)
- **Material** (CS, LTCS, SS316L, SS304L, DSS, SDSS, CuNi, GRE, CPVC, Galvanised, Tubing)
- **Corrosion Allowance** (3 mm, 1.5 mm, 1 mm, NIL)
- **Service Type** (General, Sour/NACE, Low Temperature, etc.)

It produces a full PMS containing:
- Pressure–Temperature rating table
- Pipe schedule & wall thickness for every standard size (0.5" to 36")
- Fitting specifications (seamless & welded)
- Flange specifications
- Bolt/nut/gasket specifications
- Valve specifications with size-specific codes
- Branch connection chart references
- Engineering compliance flags (NACE, LTCS, sour service, steam, etc.)
- Wall thickness adequacy calculations per ASME B31.3

### Who Is It For?

Piping engineers who need to quickly generate, review, and download piping material specifications for oil & gas, petrochemical, and industrial projects.

---

## 2. Architecture & Tech Stack

```
┌────────────────────────────────────────────────────────────────┐
│                        BROWSER (Frontend)                      │
│  Single-Page App: HTML + CSS + JavaScript (app.js ~1300 lines) │
│  Client-side calculations: MAWP, t_req, interpolation          │
└──────────────────────────┬─────────────────────────────────────┘
                           │ REST API (JSON)
                           ▼
┌────────────────────────────────────────────────────────────────┐
│                     FastAPI Server (Backend)                    │
│  Routes → Services → AI/Data/DB → Models → Utils               │
├─────────────┬──────────────┬──────────────┬────────────────────┤
│  AI Service │  Data Service │  DB Service  │  Engineering Utils │
│  (Claude)   │  (JSON files) │ (PostgreSQL) │  (ASME formulas)   │
└─────────────┴──────────────┴──────────────┴────────────────────┘
```

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | HTML5, CSS3, Vanilla JS | User interface, client-side calculations |
| **Backend** | Python 3, FastAPI, Uvicorn | REST API, orchestration |
| **AI** | Anthropic Claude API (`claude-sonnet-4-20250514`) | Generate pipe specs, fittings, valves |
| **Database** | PostgreSQL 18 (port 5435) | Persistent cache (L2) |
| **Data** | JSON files (`pipe_classes.json`) | P-T ratings, class definitions |
| **Export** | openpyxl | Professional Excel PMS sheets |
| **Models** | Pydantic v2 | Request/response validation |

---

## 3. Directory Structure

```
pms-generator/
│
├── run.py                              # Server entry point
├── .env                                # Environment variables (API keys, DB URL, port)
├── requirements.txt                    # Python dependencies
├── DOCUMENTATION.md                    # This file
│
├── app/
│   ├── __init__.py
│   ├── main.py                         # FastAPI app initialization, startup/shutdown hooks
│   ├── config.py                       # Pydantic settings (reads .env)
│   │
│   ├── data/
│   │   └── pipe_classes.json           # 92 piping classes with P-T rating data
│   │
│   ├── models/
│   │   └── pms_models.py              # PMSRequest, PMSResponse, PipeEntry, etc.
│   │
│   ├── routes/
│   │   └── pms_routes.py             # All API endpoints
│   │
│   ├── services/
│   │   ├── ai_service.py             # Claude API calls + system prompt
│   │   ├── pms_service.py            # Orchestration: cache check → AI → post-process
│   │   ├── data_service.py           # JSON data loading & lookup
│   │   ├── db_service.py             # PostgreSQL connection pool & cache ops
│   │   ├── branch_chart_service.py   # Branch connection charts (4 charts)
│   │   └── excel_generator.py        # Excel export with openpyxl styling
│   │
│   ├── static/
│   │   ├── css/styles.css            # Responsive styling, light/dark themes
│   │   └── js/app.js                 # Entire frontend logic (~1300 lines)
│   │
│   ├── templates/
│   │   └── index.html                # Single-page HTML template (Jinja2)
│   │
│   └── utils/
│       ├── engineering.py             # ASME B31.3 wall thickness & P-T adequacy formulas
│       ├── engineering_constants.py   # Centralized constants (stress tables, factors)
│       └── pipe_data.py              # ASME B36.10M/19M wall thickness lookup tables
│
└── data/
    └── pipe_classes.json              # Symlink / alternate location
```

---

## 4. Data Sources — Where Does the Data Come From?

### 4.1 Local JSON Data (pipe_classes.json)

**File:** `app/data/pipe_classes.json`
**Contains:** 92 piping class definitions
**Loaded by:** `data_service.py` at server startup (in-memory)

Each entry provides:

```json
{
  "piping_class": "A1",
  "rating": "150#",
  "material": "CS",
  "corrosion_allowance": "3 mm",
  "pressure_temperature": {
    "temperatures": [38, 50, 100, 150, 200, 250, 300],
    "pressures": [19.6, 19.2, 17.7, 15.8, 13.8, 12.1, 10.2],
    "temp_labels": ["-29 to 38", "50", "100", "150", "200", "250", "300"]
  }
}
```

**This data is NOT AI-generated.** It comes from ASME B16.5 pressure–temperature rating tables and is maintained manually in the JSON file.

**What this data provides:**
- Piping class code (A1, B1, D1, F20N, G25N, etc.)
- ASME pressure rating (150#, 300#, 600#, 900#, 1500#, 2500#)
- Material family (CS, LTCS, SS316L, SS304L, DSS, SDSS, CuNi, Galvanised, GRE, CPVC)
- Corrosion allowance (3 mm, 1.5 mm, 1 mm, NIL)
- P-T rating table: allowable pressures at specific temperature breakpoints

### 4.2 ASME B36.10M / B36.19M Wall Thickness Tables

**File:** `app/utils/pipe_data.py`
**Contains:** Standard pipe wall thicknesses for all NPS sizes and schedules
**Used by:** Post-processing after AI generation

This is a Python dictionary mapping `(OD_mm, schedule_name)` → `wall_thickness_mm`. Example:

```python
_WT_TABLE = {
    (21.3, "SCH 160"): 4.78,     # 0.5" NPS, Sch 160
    (21.3, "XXS"):     7.47,     # 0.5" NPS, XXS
    (33.4, "SCH 80"):  4.55,     # 1" NPS, Sch 80
    # ... covers all standard sizes 0.5" to 36"
}
```

**Schedules covered:** 5S, 10, 10S, 20, 30, 40, 40S, 60, 80, 80S, 100, 120, 140, 160, STD, XS, XXS

**Purpose:** After Claude AI generates pipe data with schedule assignments, the `correct_pipe_data()` function replaces all wall thickness values with the exact standard ASME value. This ensures WT is always deterministic and standard-compliant, regardless of what AI returned.

### 4.3 ASME B31.3 Table A-1 — Allowable Stress Tables

**File:** `app/utils/engineering_constants.py`
**Contains:** Allowable stress values at temperature for 6 material families

```python
STRESS_CS = {38: 20000, 50: 20000, 100: 20000, 150: 18900, 200: 17700, 250: 17700, ...}  # psi
STRESS_DSS = {38: 25000, 50: 25000, 100: 23300, 150: 21600, 200: 20200, ...}             # psi
STRESS_SDSS = {38: 36700, 50: 36700, 100: 35000, 150: 33200, 200: 31500, ...}            # psi
STRESS_SS316L = {38: 16700, 50: 16700, 100: 16700, 150: 15500, 200: 14100, ...}          # psi
```

**Purpose:** Used in wall thickness calculations. The function `get_allowable_stress(material, temp_c)` performs linear interpolation between temperature breakpoints to find the exact stress value at any design temperature.

### 4.4 AI-Generated Data (Claude API)

**File:** `app/services/ai_service.py`
**Model:** `claude-sonnet-4-20250514`
**Max tokens:** 16,384

**Everything NOT in the JSON or lookup tables is generated by Claude AI.** See [Section 6](#6-ai-integration--what-claude-generates) for full details.

### 4.5 Branch Connection Charts

**File:** `app/services/branch_chart_service.py`
**Contains:** 4 hardcoded branch connection matrices (17×17 or 14×14 grids)
**Source:** API RP 14E / project engineering standards

Each chart maps (run size × branch size) to a connection type (Tee, Weldolet, Threadolet, Sockolet, Reducing Tee, etc.).

---

## 5. Application Flow — Step by Step

### 5.1 Server Startup

```
run.py → uvicorn.run("app.main:app")
         ↓
app/main.py:
  1. Create FastAPI app
  2. Mount static files (/static)
  3. Include API router (prefix="/api")
  4. @startup hook:
     a. Load pipe_classes.json into memory (92 classes)
     b. Initialize PostgreSQL connection pool (if DATABASE_URL set)
     c. Create pms_cache table if not exists
  5. @shutdown hook:
     a. Close PostgreSQL pool
```

### 5.2 User Opens the App

```
Browser → GET / → index.html (Jinja2 template)
  ↓
app.js initializes:
  1. loadEngineeringConstants() → GET /api/engineering-constants
     → Populates ENG global object (stress tables, factors, etc.)
  2. loadIndexData() → GET /api/index-data
     → Populates cascading dropdowns (Rating → Material → CA → Service)
  3. loadBrowseData() → GET /api/pipe-classes
     → Populates browse table (92 classes)
```

### 5.3 Step 1: Preview PMS (No AI)

```
User selects: Rating=150# → Material=CS → CA=3mm → Service=General
  ↓
User clicks "Preview PMS"
  ↓
Frontend resolves piping_class from indexData (e.g., "A1")
  ↓
POST /api/preview-pms
  Body: { piping_class: "A1", material: "CS", corrosion_allowance: "3 mm", service: "General" }
  ↓
Backend (pms_routes.py):
  1. data_service.find_entry("A1") → P-T data from JSON
  2. Compute hydrotest = max(pressures) × 1.5
  3. Return: { piping_class, rating, material, CA, P-T table, hydrotest }
  ↓
Frontend shows:
  - Preview banner: "Class A1 — 150# CS — Ready to generate"
  - P-T table (read-only)
  - "Generate Full PMS" button
  ↓
⏱ This step takes < 100ms (no AI involved)
```

### 5.4 Step 2: Generate Full PMS (AI + Calculations)

```
User clicks "Generate Full PMS"
  ↓
POST /api/generate-pms
  Body: { piping_class: "A1", material: "CS", corrosion_allowance: "3 mm", service: "General" }
  ↓
Backend (pms_service.py):
  1. Compute cache_key = MD5("A1|CS|3 mm|General")
  2. Check L1 (in-memory cache) → miss
  3. Check L2 (PostgreSQL cache) → miss
  4. Cache miss → call AI:
     a. data_service.find_entry("A1") → P-T data
     b. data_service.get_all_entries() → reference entries (3-5 similar classes)
     c. ai_service.generate_pms_with_ai("A1", "CS", "3 mm", "General", "150#", references)
        → Sends 478-line system prompt + user request to Claude
        → Claude returns full JSON with pipe_data, fittings, flanges, valves, etc.
     d. pipe_data.correct_pipe_data(ai_response.pipe_data)
        → Replaces all wall thicknesses with ASME standard values
     e. Build PMSResponse from merged (JSON P-T + AI data)
  5. Store in L1 and L2 caches
  6. Return PMSResponse
  ↓
Frontend renders 4 tabs:
  - Tab 1: P-T Rating (adequacy check, design conditions)
  - Tab 2: Schedule & Wall Thickness (engineering calculations)
  - Tab 3: Pipe & Fittings Material (component specifications)
  - Tab 4: Components & Notes (flanges, bolts, valves)
  ↓
⏱ First generation: 15-30 seconds (Claude API latency)
⏱ Cached: < 100ms
```

### 5.5 Optional: Regenerate (Force Fresh AI)

```
User clicks "Regenerate with AI"
  ↓
POST /api/regenerate-pms
  → Bypasses ALL caches
  → Fresh Claude API call
  → Overwrites cached result
  ↓
⏱ Always 15-30 seconds
```

### 5.6 Optional: Download Excel

```
User clicks "Download Excel"
  ↓
POST /api/download-excel
  → excel_generator.generate_pms_excel_bytes(pms_response)
  → Returns binary XLSX blob
  → Browser downloads file
```

---

## 6. AI Integration — What Claude Generates

### 6.1 When Is AI Used?

AI is used **only in Step 2** (Generate Full PMS) and **only on cache miss**. It is NOT used for:
- P-T ratings (from JSON)
- Wall thickness values (from ASME lookup tables)
- Engineering calculations (from formulas)
- Branch connection charts (from hardcoded matrices)

### 6.2 The System Prompt

**File:** `app/services/ai_service.py`
**Length:** ~478 lines of engineering specifications

The system prompt teaches Claude the complete engineering knowledge base:

1. **ASME Standards Referenced:**
   - ASME B31.3 — Process Piping (design code)
   - ASME B36.10M — Welded and Seamless Wrought Steel Pipe (CS/LTCS/GALV schedules)
   - ASME B36.19M — Stainless Steel Pipe (SS/DSS/SDSS schedules)
   - ASME B16.5 — Pipe Flanges and Flanged Fittings
   - ASME B16.9 — Factory-Made Wrought Butt-Welding Fittings
   - ASME B16.11 — Forged Fittings, Socket-Welding and Threaded
   - ASME B16.20 — Metallic Gaskets
   - ASME B16.48 — Line Blanks
   - NACE MR0175 / ISO 15156 — Sulfide Stress Cracking Resistant Metallic Materials

2. **Schedule Mapping by Class:**
   The prompt contains explicit schedule tables for every class. Example:
   ```
   A1 (150#, CS):
     0.5" → XXS | 0.75"→SCH 160 | 1"-1.5" → SCH 160 | 2"-6" → SCH 80
     8"-14" → SCH 40 | 16"-36" → STD

   F20N (DSS, NACE):
     0.5"-1" → SCH 40S | 1.5"-8" → SCH 40S | 10"-36" → SCH 10S/-
   ```

3. **Material Specifications:**
   - CS pipe: ASTM A 106 Gr. B (seamless), ASTM A 672 Gr. B60 (welded)
   - LTCS pipe: ASTM A 333 Gr. 6
   - SS316L pipe: ASTM A 312 TP 316L
   - DSS pipe: ASTM A 790 S31803
   - SDSS pipe: ASTM A 790 S32750
   - CuNi pipe: ASTM B 466 UNS C70600
   - Galvanised: ASTM A 53 Gr. B (hot-dip galvanised)

4. **Valve Codes:**
   Size-specific valve identification codes for each class (e.g., BLRTA1R = Ball valve, Reduced bore, Trunnion, Class A1, Rising stem).

5. **Fitting Standards:**
   - Butt-Weld: ASME B16.9
   - Socket-Weld: ASME B16.11
   - Screwed: ASME B16.11
   - Forged: ASTM A 105 (CS), A 182 F316L (SS), A 182 F51 (DSS), etc.

### 6.3 What Data Is Sent to Claude

```python
# User request message sent to Claude:
f"""Generate a complete PMS JSON for:
  Piping Class: {piping_class}
  Material: {material}
  Corrosion Allowance: {ca}
  Service: {service}
  Rating: {rating}

Reference entries for context:
{reference_entries_json}
"""
```

**Reference entries** are 3-5 similar piping classes (same material family) included as examples so Claude can maintain consistency.

### 6.4 What Claude Returns

Claude returns a single JSON object:

```json
{
  "design_code": "ASME B 31.3",
  "pipe_code": "ASME B 36.10M",
  "mill_tolerance": "12.5%",
  "branch_chart": "Ref. APPENDIX-1, Chart 1",
  "hydrotest_pressure": "",

  "pipe_data": [
    {
      "size_inch": "0.5",
      "od_mm": 21.3,
      "schedule": "XXS",
      "wall_thickness_mm": 7.47,
      "pipe_type": "Seamless",
      "material_spec": "ASTM A 106 Gr. B",
      "ends": "PE"
    },
    { "size_inch": "1", ... },
    { "size_inch": "2", ... },
    // ... for ALL standard sizes up to 36"
  ],

  "fittings": {
    "fitting_type": "Socket Weld / Screwed, ASME B16.11, 6000#",
    "material_spec": "ASTM A 105",
    "elbow_standard": "ASME B16.11",
    "tee_standard": "ASME B16.11",
    "reducer_standard": "Swaged Nipple per MSS-SP-95",
    "cap_standard": "Hex Head Plug, ASME B16.11, 6000#",
    "plug_standard": "Hex Head Plug, ASME B16.11, 6000#",
    "weldolet_standard": "N/A"
  },

  "fittings_welded": {
    "fitting_type": "Butt Weld, ASME B16.9",
    "material_spec": "ASTM A 234 WPB",
    "elbow_standard": "ASME B16.9",
    "tee_standard": "ASME B16.9",
    "reducer_standard": "ASME B16.9",
    "cap_standard": "ASME B16.9",
    "weldolet_standard": "MSS-SP-97"
  },

  "fittings_by_size": [
    {
      "size_inch": "0.5",
      "type": "Seamless",
      "fitting_type": "Socket Weld / Screwed, 6000#",
      "material_spec": "ASTM A 105",
      "elbow_standard": "ASME B16.11",
      "tee_standard": "ASME B16.11",
      "reducer_standard": "Swaged Nipple",
      "cap_standard": "Hex Head Plug",
      "plug_standard": "Hex Head Plug",
      "weldolet_standard": "N/A"
    },
    // ... one entry per pipe size
  ],

  "extra_fittings": {
    "coupling": "Full / Half, ASME B16.11, 6000#, ASTM A 105",
    "hex_plug": "ASME B16.11, 6000#, ASTM A 105",
    "union": "ASME B16.11, 6000#, ASTM A 105",
    "olet": "Sockolet / Thredolet, 6000#, ASTM A 105",
    "swage": "Conc / Ecc, MSS-SP-95, ASTM A 105"
  },

  "flange": {
    "material_spec": "ASTM A 105",
    "face_type": "Raised Face (RF)",
    "flange_type": "Weld Neck (WN)",
    "standard": "ASME B16.5"
  },

  "spectacle_blind": {
    "material_spec": "ASTM A 516 Gr. 70",
    "standard": "ASME B16.48 (≤ 24\")",
    "standard_large": "Engineered per ASME B31.3 (> 24\")"
  },

  "bolts_nuts_gaskets": {
    "stud_bolts": "ASTM A 193 B7",
    "hex_nuts": "ASTM A 194 2H",
    "gasket": "Spiral Wound, SS316 windings, Flexible Graphite Filler, CS Centering Ring, ASME B16.20"
  },

  "valves": {
    "rating": "150#",
    "ball": "Floating / Trunnion, Full / Reduced bore, NACE",
    "gate": "Gate, OS&Y, BB, NACE",
    "globe": "Globe, OS&Y, BB, NACE",
    "check": "Swing Check / Dual Plate Check, NACE",
    "butterfly": "Lug Type / Wafer",
    "dbb": "Double Block & Bleed",
    "dbb_inst": "DBB Instrument",
    "ball_by_size": [
      { "size_inch": "0.5", "code": "BLRTA1R" },
      { "size_inch": "2", "code": "BLRTA1R, BLFTA1R" },
      // ... per size
    ],
    "gate_by_size": [ ... ],
    "globe_by_size": [ ... ],
    "check_by_size": [ ... ]
  },

  "notes": [
    "All welding per ASME B31.3 and qualified to ASME IX.",
    "NDE per ASME B31.3 Table 341.3.2.",
    // ...
  ]
}
```

### 6.5 Post-Processing After AI

After Claude returns the JSON, the backend performs **deterministic corrections**:

1. **Wall Thickness Correction** (`pipe_data.py → correct_pipe_data()`):
   - For each pipe size, look up the OD from the NPS-to-OD table
   - For each (OD, schedule), look up the exact wall thickness from ASME B36.10M/B36.19M tables
   - Replace the AI-generated WT with the standard value
   - If schedule is "-" (calculated/non-standard), keep AI's value

2. **Hydrotest Pressure Calculation** (`pms_service.py`):
   - Compute: `hydrotest = max(P-T pressures) × 1.5`
   - This uses the JSON data, NOT AI output

3. **P-T Data Merge**:
   - The P-T rating table from JSON is merged into the response
   - AI never generates P-T data — it only generates specifications

---

## 7. Engineering Calculations — Where Math Is Used

### 7.1 Backend Calculations (Python)

**File:** `app/utils/engineering.py`

#### A. Hydrotest Pressure
```
Location: pms_service.py + engineering.py
Formula: P_hydrotest = P_max × HYDROTEST_FACTOR
Where:
  P_max = maximum pressure from P-T rating table (at any temperature)
  HYDROTEST_FACTOR = 1.5 (per ASME B31.3 §345.4.2)

Example: P-T max = 19.6 barg → Hydrotest = 19.6 × 1.5 = 29.4 barg
```

#### B. P-T Adequacy Check (Linear Interpolation)
```
Location: engineering.py → check_pt_adequacy()
Purpose: Check if selected rating is adequate for design conditions

Method:
  1. Given design_temp and the P-T table (temps[], pressures[])
  2. Find the two bracketing temperatures: T_low ≤ design_temp ≤ T_high
  3. Linear interpolation:
     P_allowed = P_low + (P_high - P_low) × (design_temp - T_low) / (T_high - T_low)
  4. Compare: P_allowed ≥ design_pressure → ADEQUATE / INADEQUATE

Example:
  Design: 120°C, 15 barg
  P-T table: 100°C → 17.7 barg, 150°C → 15.8 barg
  P_allowed = 17.7 + (15.8 - 17.7) × (120 - 100) / (150 - 100)
            = 17.7 + (-1.9) × 0.4
            = 17.7 - 0.76 = 16.94 barg
  16.94 ≥ 15 → ADEQUATE ✓
```

#### C. Wall Thickness (ASME B31.3 Eq. 3a)
```
Location: engineering.py → calculate_wall_thickness()
Formula:
  t_calc = (P × D) / (2 × (S × E × W + P × Y))

Where:
  P = design pressure (MPa)         [converted from barg]
  D = outside diameter (mm)          [from ASME B36.10M/19M]
  S = allowable stress (MPa)         [from Table A-1 at design temp]
  E = longitudinal joint efficiency   [1.0 for seamless, 0.85 for ERW]
  W = weld joint strength reduction   [1.0 for T < 510°C]
  Y = coefficient                     [0.4 for ferritic steel < 482°C]

Then:
  t_with_CA = t_calc + CA              [add corrosion allowance]
  t_minimum = t_with_CA / (1 - 0.125)  [add 12.5% mill tolerance]
```

### 7.2 Frontend Calculations (JavaScript)

**File:** `app/static/js/app.js`

The frontend performs the **same calculations** as the backend for display purposes in the Schedule & Wall Thickness tab. This allows real-time calculation without server round-trips.

#### A. Allowable Stress Lookup
```javascript
// Location: app.js → getAllowableStress(material, tempC)
// Uses ENG.stress_tables loaded from /api/engineering-constants

function getAllowableStress(material, tempC) {
    // 1. Determine material family (CS, DSS, SDSS, SS316L, etc.)
    // 2. Look up stress table from ENG.stress_tables
    // 3. Linear interpolation between temperature breakpoints
    // Returns: { S_psi: value, S_mpa: value, label: "S(100°C) = 20,000 psi" }
}
```

#### B. Required Wall Thickness (per size)
```javascript
// Location: app.js → renderEnhancedPipeTable()
// For each pipe size in the PMS:

t_req_mm = (P_mpa * OD_mm) / (2 * (S_mpa * E * W + P_mpa * Y))
         // ASME B31.3 Eq. 3a (pressure thickness only)
```

#### C. Minimum Wall Thickness (after mill tolerance)
```javascript
t_min_mm = WT_nominal * (1 - ENG.mill_tolerance_fraction)
         // e.g., 7.47 × (1 - 0.125) = 6.54 mm
```

#### D. Effective Thickness (after CA)
```javascript
t_eff_mm = t_min_mm - CA_mm
         // e.g., 6.54 - 3.0 = 3.54 mm (CS with 3mm CA)
         // e.g., 6.54 - 0.0 = 6.54 mm (DSS with NIL CA)
```

#### E. Maximum Allowable Working Pressure (MAWP)
```javascript
MAWP_mpa = (2 * S_mpa * E * W * t_eff_mm) / (OD_mm - 2 * Y * t_eff_mm)
MAWP_barg = MAWP_mpa * 10
```

#### F. Utilization & Margin
```javascript
utilization = (design_pressure / MAWP) * 100    // percentage
margin = MAWP - design_pressure                  // barg
```

#### G. P-T Interpolation (Frontend)
```javascript
// Location: app.js → interpolatePressure(temps, pressures, designTemp)
// Same linear interpolation as backend
```

#### H. Hydrotest Display
```javascript
// Location: app.js → renderPTRatingTab()
htBaseP = hydrotest_value / ENG.hydrotest_factor    // Back-calculate base pressure
// Display: "29.4 barg (= 1.5 × 19.6 barg max rated pressure)"
```

### 7.3 Calculation Summary Table

| Calculation | Where (Backend) | Where (Frontend) | Standard |
|------------|-----------------|-------------------|----------|
| Hydrotest pressure | `pms_service.py` | `app.js` display | ASME B31.3 §345.4.2 |
| P-T adequacy | `engineering.py` | `app.js` | ASME B16.5 |
| Wall thickness (t_req) | `engineering.py` | `app.js` | ASME B31.3 Eq. 3a |
| MAWP | — | `app.js` | ASME B31.3 (reverse) |
| Allowable stress S(T) | `engineering_constants.py` | `app.js` via ENG | ASME B31.3 Table A-1 |
| Mill tolerance | `engineering_constants.py` | `app.js` via ENG | Standard 12.5% |
| WT correction | `pipe_data.py` | — | ASME B36.10M/19M |

---

## 8. Caching System — Three-Tier Architecture

### Why Cache?

Each Claude API call takes 15-30 seconds and costs money. The same piping class with the same parameters always generates essentially the same specification. Caching eliminates redundant AI calls.

### Cache Tiers

```
Request → L1 (Memory) → L2 (PostgreSQL) → L3 (Claude AI)
           ~0ms            ~5ms              ~15-30 sec
```

| Tier | Storage | Lifetime | Scope |
|------|---------|----------|-------|
| **L1** | Python `TTLCache` (in-memory) | 3600 seconds (1 hour) | Current server session |
| **L2** | PostgreSQL `pms_cache` table | Permanent (until cleared) | Persistent across restarts |
| **L3** | Claude API | N/A (always fresh) | On-demand |

### Cache Key

```python
cache_key = MD5(f"{piping_class}|{material}|{corrosion_allowance}|{service}")
# Example: MD5("A1|CS|3 mm|General") → "a3b4c5d6e7f8..."
```

### Cache Flow

```
generate_pms(request):
    key = compute_cache_key(request)

    # L1 check
    if key in memory_cache:
        return memory_cache[key]           ← ~0ms

    # L2 check
    db_result = await db_service.get_cached_pms(key)
    if db_result:
        memory_cache[key] = db_result      ← Promote to L1
        return db_result                   ← ~5ms

    # L3: AI generation
    ai_result = await ai_service.generate_pms_with_ai(...)   ← ~15-30s
    corrected = pipe_data.correct_pipe_data(ai_result)
    response = build_response(corrected)

    # Store in both L1 and L2
    memory_cache[key] = response
    await db_service.store_pms(key, response)

    return response
```

### Cache Operations

| Action | Endpoint | Effect |
|--------|----------|--------|
| Normal generate | `POST /api/generate-pms` | Checks L1 → L2 → L3 |
| Regenerate | `POST /api/regenerate-pms` | Bypasses ALL caches, fresh AI call |
| Clear cache | `POST /api/clear-cache` | Wipes L1 and L2 |

---

## 9. Frontend — User Interface & Client-Side Logic

### 9.1 Page Layout

```
┌─────────────────────────────────────────────────────────┐
│ Header: PMS Generator                    [Theme Toggle] │
├─────────────┬───────────────────────────────────────────┤
│ Tab: Generate │ Tab: Browse                              │
├─────────────┴───────────────────────────────────────────┤
│                                                         │
│ GENERATE TAB:                                           │
│ ┌─────────────────────────────────────────────────┐     │
│ │ Rating:    [Dropdown ▼]                         │     │
│ │ Material:  [Dropdown ▼] (filtered by rating)    │     │
│ │ CA:        [Dropdown ▼] (filtered by material)  │     │
│ │ Service:   [Dropdown ▼] (filtered by CA)        │     │
│ │                                                 │     │
│ │ [Preview PMS]                                   │     │
│ └─────────────────────────────────────────────────┘     │
│                                                         │
│ RESULT AREA (after generation):                         │
│ ┌─────────┬──────────────┬───────────────┬────────────┐ │
│ │ P-T     │ Schedule &   │ Pipe &        │ Components │ │
│ │ Rating  │ Thickness    │ Fittings      │ & Notes    │ │
│ ├─────────┴──────────────┴───────────────┴────────────┤ │
│ │                                                     │ │
│ │ [Tab content rendered here]                         │ │
│ │                                                     │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ [Download Excel] [Regenerate with AI] [Copy JSON]       │
│                                                         │
│ BROWSE TAB:                                             │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ Search: [__________]                                │ │
│ │ ┌──────┬────────┬──────────┬────┐                   │ │
│ │ │Class │Rating  │Material  │ CA │                   │ │
│ │ ├──────┼────────┼──────────┼────┤                   │ │
│ │ │ A1   │ 150#   │ CS       │3mm │                   │ │
│ │ │ B1   │ 300#   │ CS       │3mm │                   │ │
│ │ │ ...  │ ...    │ ...      │... │                   │ │
│ │ └──────┴────────┴──────────┴────┘                   │ │
│ └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### 9.2 Cascading Dropdown Logic

The dropdowns filter based on the JSON data:

```
Rating selected (e.g., "150#")
  → Filter: show only materials that have a 150# class

Material selected (e.g., "CS")
  → Filter: show only CA values that exist for 150# CS

CA selected (e.g., "3 mm")
  → Filter: show only services that exist for 150# CS 3mm

Service selected (e.g., "General")
  → Resolve: find the exact piping_class code (e.g., "A1")
```

### 9.3 The Four Result Tabs

#### Tab 1: P-T Rating
- Class identification (code, rating, material, CA)
- Service assessment (NACE flag, Low Temp flag)
- P-T table with all temperature/pressure pairs
- Design conditions input (Design P and Design T)
- Adequacy check: interpolated allowable vs design pressure
- Standard reference bar (ASME B16.5 group/class)

#### Tab 2: Schedule & Wall Thickness
- Design parameters section:
  - Design pressure, design temperature
  - Material, allowable stress S(T) at design temp
  - Material-specific: CS=20,000 psi, DSS=23,300 psi, SDSS=35,000 psi, SS316L=16,700 psi
- Code factors section:
  - Pipe standard (B36.10M for CS/LTCS/GALV, B36.19M for SS/DSS/SDSS)
  - Joint type (Seamless, E=1.0)
  - Y coefficient, W factor, CA, mill tolerance
- Engineering flags section (material-specific):
  - CS NACE: "Max hardness 22 HRC / 250 HBW, Sch 160/XS minimum, PWHT required"
  - DSS NACE: "Max hardness 28 HRC, Ferrite 35-65%, PREN ≥ 34, No PWHT required"
  - SDSS NACE: "Max hardness 32 HRC, PREN ≥ 40, No PWHT required"
  - SS NACE: "Max hardness 22 HRC, solution annealed, no PWHT"
  - LTCS: "Impact testing per ASME B31.3 required"
  - Steam/Corrosive: Relevant warnings
- Enhanced pipe table (per size):
  - Size, OD, Schedule, WT (nominal), Pipe type, MOC
  - t_req (required thickness from Eq. 3a)
  - t_min (after mill tolerance)
  - t_eff (after CA deduction)
  - MAWP (back-calculated)
  - Margin (MAWP - design P)
  - Utilization % (design P / MAWP × 100)
  - Tags: NACE, LTCS, Pressure (governing factor)
- Summary statistics:
  - Min/Max MAWP across all sizes
  - Minimum margin
  - Hydrotest pressure

#### Tab 3: Pipe & Fittings Material
- Small bore section (≤ 2" NPS):
  - Seamless pipe, socket weld / screwed fittings
  - Component matrix: elbow, tee, reducer, cap, plug, coupling, union, olet
  - Material specs per component
- Large bore section (> 2" NPS):
  - Seamless/welded pipe, butt weld fittings
  - Component matrix: elbow, tee, reducer, cap, weldolet
  - Material specs per component
- Branch connection chart reference

#### Tab 4: Components & Notes
- Flanges: MOC, face type (RF/RTJ), flange type (WN/SW), standard
- Spectacle blind: MOC, standard for ≤24" and >24"
- Bolts/nuts/gaskets: Stud bolt spec, hex nut spec, gasket type/spec
- Valves: Rating, type (ball/gate/globe/check/butterfly/DBB)
  - Size-specific valve codes in expandable table
- Notes: Engineering compliance, welding, NDE, testing requirements

### 9.4 Key Frontend Functions

| Function | Purpose |
|----------|---------|
| `loadEngineeringConstants()` | Fetch ENG object from backend at startup |
| `loadIndexData()` | Populate cascading dropdowns |
| `generatePMS()` | Step 1: preview (no AI) |
| `generateFullPMS()` | Step 2: full generation (AI) |
| `regenerateFullPMS()` | Force fresh AI call |
| `renderFullResult()` | Master render — calls all tab renderers |
| `renderPTRatingTab()` | P-T table, adequacy check |
| `renderScheduleTab()` | Calculations, flags, pipe table |
| `renderEnhancedPipeTable()` | Per-size t_req, MAWP, margin, utilization |
| `renderEngineeringFlags()` | Material-specific compliance warnings |
| `renderPipeFittingsTab()` | Component specs, branch chart |
| `renderComponentsTab()` | Flanges, bolts, valves, notes |
| `getAllowableStress()` | ASME B31.3 Table A-1 lookup with interpolation |
| `interpolatePressure()` | P-T linear interpolation |
| `downloadExcel()` | Request and download XLSX |

---

## 10. API Endpoints Reference

### Core PMS Endpoints

| Method | Path | Purpose | AI Used? | Response Time |
|--------|------|---------|----------|---------------|
| `POST` | `/api/preview-pms` | Step 1: P-T preview only | No | < 100ms |
| `POST` | `/api/generate-pms` | Step 2: Full PMS generation | On cache miss | 100ms–30s |
| `POST` | `/api/regenerate-pms` | Force fresh AI generation | Always | 15–30s |
| `GET` | `/api/pms/{class}` | Direct class lookup | On cache miss | 100ms–30s |

### Data Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/pipe-classes` | All 92 classes (browse table) |
| `GET` | `/api/pipe-classes/codes` | List of class codes only |
| `GET` | `/api/index-data` | Cascading dropdown data |
| `GET` | `/api/engineering-constants` | All constants + stress tables |
| `GET` | `/api/branch-charts` | All 4 branch connection charts |
| `GET` | `/api/branch-charts/{id}` | Single branch chart |

### Utility Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/download-excel` | Generate & download XLSX |
| `POST` | `/api/clear-cache` | Clear L1 + L2 caches |
| `GET` | `/health` | Diagnostic info |
| `GET` | `/` | Serve HTML page |

### Request/Response Models

**PMSRequest:**
```json
{
  "piping_class": "A1",           // Required: class code
  "material": "CS",               // Optional: defaults from JSON
  "corrosion_allowance": "3 mm",  // Optional: defaults from JSON
  "service": "General"            // Optional: defaults to "General"
}
```

**PMSResponse:**
```json
{
  "piping_class": "A1",
  "rating": "150#",
  "material": "CS",
  "corrosion_allowance": "3 mm",
  "service": "General",
  "design_code": "ASME B 31.3",
  "pipe_code": "ASME B 36.10M",
  "mill_tolerance": "12.5%",
  "branch_chart": "Ref. APPENDIX-1, Chart 1",
  "hydrotest_pressure": "29.4 barg",
  "pressure_temperature": { ... },
  "pipe_data": [ ... ],
  "fittings": { ... },
  "fittings_welded": { ... },
  "fittings_by_size": [ ... ],
  "extra_fittings": { ... },
  "flange": { ... },
  "spectacle_blind": { ... },
  "bolts_nuts_gaskets": { ... },
  "valves": { ... },
  "notes": [ ... ]
}
```

---

## 11. Excel Export

### Generation Flow

```
POST /api/download-excel
  → pms_routes.py: generate PMS (cached or AI)
  → excel_generator.py: generate_pms_excel_bytes(pms_response)
  → Returns: StreamingResponse with XLSX binary
  → Browser: Downloads file
```

### Excel Structure

**File:** `app/services/excel_generator.py`

**Sheet 1: PMS (main spec)**

| Section | Contents |
|---------|----------|
| Title | "PIPING MATERIAL SPECIFICATION" |
| Header | Class, Rating, Material, CA, Mill Tolerance, Design Code, Service |
| P-T Rating | Temperature labels, Pressure values, Hydrotest |
| Pipe Data | Size, OD, Schedule, WT, Pipe Type, MOC, Ends |
| Fittings (by size) | Size, Type, Fitting Type, MOC, Standards per component |
| Extra Fittings | Coupling, Hex Plug, Union, Olet, Swage |
| Flanges | MOC, Face Type, Flange Type, Standard |
| Spectacle Blind | MOC, Standard, Standard (Large) |
| Bolts/Nuts/Gaskets | Stud Bolts, Hex Nuts, Gasket |
| Valves | Rating, Ball, Gate, Globe, Check, Butterfly, DBB |
| Notes | Engineering/compliance notes |

**Sheets 2-5: Branch Charts (if applicable)**

| Section | Contents |
|---------|----------|
| Title | "BRANCH CONNECTION CHART X — [Title]" |
| Matrix | Run sizes (rows) × Branch sizes (columns) |
| Cells | Connection type codes (T, W, H, S, RT) |
| Legend | Color-coded: Green=Tee, Red=Weldolet, etc. |

### Styling

| Element | Style |
|---------|-------|
| Header row | Dark blue (#1F4E79), white bold text |
| Section headers | Light blue (#D6E4F0), bold |
| Data rows | Alternating white / light grey (#F2F7FB) |
| Notes | Yellow (#FFF2CC), italic |
| Borders | Thin grey, medium blue for section breaks |

---

## 12. Branch Connection Charts

### Overview

**File:** `app/services/branch_chart_service.py`

Four standard branch connection charts define how branch connections are made based on run size and branch size:

### Chart 1: CS, LTCS, SS, DSS, SDSS
- **Grid:** 17 × 17 (Run sizes ≤1" to 32" × Branch sizes)
- **Connection types:**
  - **T** = Equal/Reducing Tee (ASME B16.9)
  - **W** = Weldolet (MSS-SP-97)
- **Used by:** Classes A1, B1, D1, E1, F20, G25, and NACE variants

### Chart 2: CS Galvanised
- **Grid:** 14 × 14
- **Connection types:**
  - **T** = Tee
  - **H** = Threadolet
  - **W** = Weldolet
- **Used by:** Galvanised classes (A1G, etc.)

### Chart 3: CuNi 90/10
- **Grid:** 17 × 17
- **Connection types:**
  - **T** = Tee (Butt Weld)
  - **S** = Sockolet
  - **W** = Weldolet
- **Used by:** CuNi classes

### Chart 4: GRE (Glass Reinforced Epoxy)
- **Grid:** 14 × 14
- **Connection types:**
  - **T** = Equal Tee
  - **RT** = Reducing Tee
  - **S** = Reducing Saddle
- **Used by:** GRE classes

### How Charts Are Assigned

```python
def get_charts_for_class(piping_class):
    material = lookup_material(piping_class)
    if material == "GALV":    return [chart_2]
    if material == "CUNI":    return [chart_3]
    if material == "GRE":     return [chart_4]
    else:                     return [chart_1]    # CS, LTCS, SS, DSS, SDSS
```

---

## 13. Engineering Constants — Single Source of Truth

### Why Centralized?

To prevent inconsistencies between backend calculations and frontend display, ALL engineering constants are defined in one file and served to the frontend via API.

### File: `app/utils/engineering_constants.py`

```python
# ── Hydrotest & Operating Factors ──────────────────────────
HYDROTEST_FACTOR          = 1.5      # ASME B31.3 §345.4.2
OPERATING_PRESSURE_FACTOR = 0.8      # Typical operating margin
OPERATING_TEMP_FACTOR     = 0.8      # Typical operating margin

# ── Wall Thickness Calculation ─────────────────────────────
MILL_TOLERANCE_PERCENT    = 12.5     # Standard pipe mill tolerance (%)
MILL_TOLERANCE_FRACTION   = 0.125    # Same as decimal
JOINT_EFFICIENCY_E        = 1.0      # Seamless pipe
WELD_STRENGTH_W           = 1.0      # T < 510°C
Y_COEFFICIENT             = 0.4      # Ferritic steel, T < 482°C

# ── Size Classification ────────────────────────────────────
SMALL_BORE_CUTOFF_NPS     = 2.0      # ≤ 2" = small bore

# ── AI Parameters ──────────────────────────────────────────
AI_MAX_TOKENS             = 16384    # Claude response limit

# ── Default Values ─────────────────────────────────────────
DEFAULT_CORROSION_ALLOWANCE = "3 mm"
DEFAULT_SERVICE             = "General"

# ── ASME B31.3 Table A-1 — Allowable Stress (psi) at Temperature (°C) ──
STRESS_CS    = {38: 20000, 50: 20000, 100: 20000, 150: 18900, ...}
STRESS_SS316L = {38: 16700, 50: 16700, 100: 16700, 150: 15500, ...}
STRESS_SS304L = {38: 16700, 50: 16700, 100: 16700, 150: 14400, ...}
STRESS_DSS   = {38: 25000, 50: 25000, 100: 23300, 150: 21600, ...}
STRESS_SDSS  = {38: 36700, 50: 36700, 100: 35000, 150: 33200, ...}
STRESS_CUNI  = {38: 10000, 50: 10000, 100: 9300, 150: 8900, ...}
```

### How Constants Flow to Frontend

```
engineering_constants.py  →  /api/engineering-constants  →  app.js ENG object
       (Python)                    (REST API)                 (JavaScript)
```

The frontend loads constants at startup:
```javascript
// app.js
let ENG = { /* fallback defaults */ };

async function loadEngineeringConstants() {
    const resp = await fetch('/api/engineering-constants');
    const data = await resp.json();
    Object.assign(ENG, data);   // Overwrite defaults with server values
}
```

### Where Constants Are Consumed

| Constant | Backend Files | Frontend |
|----------|--------------|----------|
| `HYDROTEST_FACTOR` | `pms_service.py`, `pms_routes.py`, `engineering.py` | `ENG.hydrotest_factor` |
| `MILL_TOLERANCE_PERCENT` | `ai_service.py` (in prompt), `engineering.py` | `ENG.mill_tolerance_percent` |
| `JOINT_EFFICIENCY_E` | `engineering.py` | `ENG.joint_efficiency_E` |
| `Y_COEFFICIENT` | `engineering.py` | `ENG.y_coefficient` |
| `WELD_STRENGTH_W` | `engineering.py` | `ENG.weld_strength_W` |
| `STRESS_CS`, etc. | `engineering_constants.py` (interpolation) | `ENG.stress_tables` |
| `SMALL_BORE_CUTOFF_NPS` | — | `ENG.small_bore_cutoff_nps` |
| `AI_MAX_TOKENS` | `ai_service.py` | — |

---

## 14. Configuration & Environment

### Environment Variables (.env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | (required) | Claude API authentication |
| `APP_HOST` | `0.0.0.0` | Server bind address |
| `APP_PORT` | `8001` | Server port |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `DATABASE_URL` | (optional) | PostgreSQL connection string |
| `CACHE_TTL` | `3600` | L1 cache lifetime (seconds) |
| `CACHE_MAX_SIZE` | `256` | L1 cache max entries |

### Settings (config.py)

```python
class Settings(BaseSettings):
    app_name: str = "PMS Generator API"
    app_version: str = "1.0.0"
    app_host: str = "0.0.0.0"
    app_port: int = 8001
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    database_url: str = ""
    cache_ttl: int = 3600
    cache_max_size: int = 256
```

### Running the Server

```bash
# Install dependencies
pip install -r requirements.txt

# Set up PostgreSQL (optional, for persistent caching)
# Database: pms_generator, Port: 5435

# Start server
python run.py
# → Server starts at http://localhost:8001 (or APP_PORT)
```

---

## 15. Key Design Decisions

### 1. Two-Step Generation (Preview → Generate)

**Why:** Users want to see the P-T rating quickly before committing to a 15-30 second AI generation. Step 1 (preview) uses only local JSON data and returns instantly. Step 2 (full generation) triggers the AI only when the user confirms.

### 2. AI for Specifications, Deterministic for Calculations

**Why:** AI excels at generating complex, interrelated specifications (pipe schedules, fitting types, valve codes, material specs) that follow engineering rules with many edge cases. But wall thicknesses and P-T ratings must be exact numbers from published tables — these are always deterministic lookups or formula calculations, never AI-generated.

### 3. Post-Processing Wall Thickness Correction

**Why:** Even though the AI prompt contains schedule tables, AI might occasionally return slightly wrong wall thickness values. The `correct_pipe_data()` function replaces ALL AI-generated WT values with exact ASME B36.10M/B36.19M standard values. This provides a safety net ensuring engineering accuracy.

### 4. Frontend Mirrors Backend Calculations

**Why:** The Schedule & Thickness tab shows live calculations (t_req, MAWP, margin) for every pipe size. Doing this server-side would require another API call every time the user changes the design pressure or temperature. By performing calculations in JavaScript using the same constants (loaded from the API), we get instant reactivity.

### 5. Three-Tier Caching

**Why:**
- **L1 (memory):** Fastest, handles repeated views in the same session
- **L2 (PostgreSQL):** Survives server restarts, shared across instances
- **L3 (AI):** Expensive fallback, only on true cache miss

### 6. Single Source of Truth for Constants

**Why:** Engineering constants (hydrotest factor, mill tolerance, stress tables, etc.) were previously scattered across multiple files in both Python and JavaScript. This caused bugs where the frontend used 20,000 psi for all materials while the backend had material-specific values. Centralizing into `engineering_constants.py` + serving via REST API ensures perfect consistency.

### 7. Material-Specific Engineering Logic

**Why:** Different material families have fundamentally different engineering requirements:
- **CS NACE:** Minimum schedule (Sch 160/XS), PWHT required, 22 HRC max
- **DSS NACE:** No minimum schedule override, no PWHT, 28 HRC max, ferrite 35-65%
- **SDSS NACE:** Same as DSS but 32 HRC max, PREN ≥ 40
- **SS NACE:** Solution annealed, no PWHT, 22 HRC max

Treating all materials the same (as the original code did) produces incorrect and potentially dangerous engineering specifications.

---

## Appendix A: Material Family Reference

| Code | Material | Pipe Spec | Pipe Standard | Key Properties |
|------|----------|-----------|---------------|----------------|
| CS | Carbon Steel | ASTM A 106 Gr. B | B36.10M | General service, most common |
| LTCS | Low Temperature CS | ASTM A 333 Gr. 6 | B36.10M | Impact tested, -46°C min |
| SS316L | Stainless Steel 316L | ASTM A 312 TP 316L | B36.19M | Corrosion resistant |
| SS304L | Stainless Steel 304L | ASTM A 312 TP 304L | B36.19M | Corrosion resistant |
| DSS | Duplex Stainless | ASTM A 790 S31803 | B36.19M | High strength, sour service |
| SDSS | Super Duplex | ASTM A 790 S32750 | B36.19M | Highest strength, aggressive sour |
| CuNi | Copper-Nickel 90/10 | ASTM B 466 C70600 | EEMUA 234 | Seawater service |
| GALV | Galvanised CS | ASTM A 53 Gr. B | B36.10M | Utility/firewater |
| GRE | Glass Reinforced Epoxy | — | Manufacturer | Non-metallic, corrosion immune |
| CPVC | Chlorinated PVC | — | Manufacturer | Non-metallic, chemical service |

## Appendix B: Piping Class Naming Convention

```
Format: [Letter][Number][Suffix]

Letter:  A=150#, B=300#, C=400#, D=600#, E=900#, F=1500#, G=2500#
Number:  1=CS, 2=LTCS, 3=SS316L, 4=SS304L, 5=CuNi, 20=DSS, 25=SDSS
Suffix:  N=NACE, L=Low Temp, G=Galvanised, T=Tubing, GRE/CPVC=Non-metallic

Examples:
  A1   = 150# CS General
  A1N  = 150# CS NACE (Sour Service)
  B1   = 300# CS General
  F20N = 1500# DSS NACE
  G25N = 2500# SDSS NACE
  A1G  = 150# Galvanised
  D5   = 600# CuNi
```

---

*Document generated for PMS Generator v1.0.0*
*Last updated: 2026-04-16*

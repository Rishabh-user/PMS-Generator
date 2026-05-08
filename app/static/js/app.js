// === State ===
let currentPMS = null;
let allClasses = [];
let indexData = [];

let pendingPMSRequest = null;  // Stores the request data between Step 1 and Step 2

// === Engineering Constants (loaded from backend on startup) ===
// Defaults here are fallbacks — overwritten by /api/engineering-constants
let ENG = {
    hydrotest_factor: 1.5,
    operating_pressure_factor: 0.8,
    operating_temp_factor: 0.8,
    mill_tolerance_percent: 12.5,
    mill_tolerance_fraction: 0.125,
    joint_efficiency_E: 1.0,
    weld_strength_W: 1.0,
    y_coefficient: 0.4,
    small_bore_cutoff_nps: 2.0,
    default_corrosion_allowance: "3 mm",
    default_service: "General",
    // ASME B36.10M / B36.19M outside diameters in mm. Overwritten on page
    // load with the authoritative values from app/data/standards/pipe_dimensions.json
    // via /api/engineering-constants. The defaults here just keep the UI
    // probe usable if the API call fails (offline, slow, etc).
    asme_pipe_od: {
        "0.5": 21.3, "0.75": 26.7, "1": 33.4, "1.5": 48.3, "2": 60.3,
        "3": 88.9, "4": 114.3, "6": 168.3, "8": 219.1, "10": 273.0,
        "12": 323.8, "14": 355.6, "16": 406.4, "18": 457.0, "20": 508.0,
        "22": 559.0, "24": 610.0, "26": 660.4, "28": 711.2, "30": 762.0,
        "32": 812.8, "36": 914.4,
    },
    stress_tables: {
        CS:     { 38: 20000, 50: 20000, 100: 20000, 150: 18900, 200: 17700, 250: 16500, 300: 15600, 350: 14800, 400: 12100 },
        SS316L: { 38: 16700, 50: 16700, 100: 16700, 150: 14500, 200: 13300, 250: 12500, 300: 11800, 350: 11300, 400: 10900 },
        SS304L: { 38: 16700, 50: 16700, 100: 16700, 150: 13800, 200: 12700, 250: 11800, 300: 11200, 350: 10700, 400: 10300 },
        DSS:    { 38: 25000, 50: 25000, 100: 23300, 150: 22000, 200: 21000, 250: 20400, 300: 20000 },
        SDSS:   { 38: 36700, 50: 36700, 100: 35000, 150: 33100, 200: 31900, 250: 31000, 300: 30500 },
        CUNI:   { 38: 10000, 50: 10000, 100: 10000, 150: 10000, 200: 9400, 250: 8600 },
    },
};

const API = {
    previewPMS: (data) => fetch('/api/preview-pms', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
    generatePMS: (data) => fetch('/api/generate-pms', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
    regeneratePMS: (data) => fetch('/api/regenerate-pms', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
    downloadExcel: (data) => fetch('/api/download-excel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
    listClasses: () => fetch('/api/pipe-classes'),
    listCodes: () => fetch('/api/pipe-classes/codes'),
    indexData: () => fetch('/api/index-data'),
    engineeringConstants: () => fetch('/api/engineering-constants'),
    services: () => fetch('/api/services'),
    // Spec-driven dropdown options — independent of the catalogue.
    // Lets all three dropdowns render the full standards list at page
    // load even when no class is catalogued for that combination.
    optionsRatings: () => fetch('/api/options/ratings'),
    optionsMaterials: () => fetch('/api/options/materials'),
    optionsCorrosionAllowances: () => fetch('/api/options/corrosion-allowances'),
    health: () => fetch('/health'),
};

// === Unit Conversions (physical constants — never change) ===
const barg2psig = (b) => (b * 14.5038).toFixed(1);
const c2f = (c) => (c * 9 / 5 + 32).toFixed(1);
const mm2inch = (mm) => mm / 25.4;
const inch2mm = (inch) => inch * 25.4;
const barg2mpa = (b) => b * 0.1;
const psi2mpa = (p) => p * 0.00689476;
const mpa2psi = (m) => m / 0.00689476;

// === Load Engineering Constants from Backend ===
async function loadEngineeringConstants() {
    try {
        const res = await API.engineeringConstants();
        if (res.ok) {
            const data = await res.json();
            ENG = { ...ENG, ...data };
            console.log('Engineering constants loaded from backend:', ENG);
        }
    } catch (e) {
        console.warn('Failed to load engineering constants — using defaults:', e);
    }
}

// === Init ===
document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initTabs();
    initResultTabs();
    initForm();
    initCascadingDropdowns();
    initServiceMultiSelect();
    initDesignInputs();
    initValvesheetSync();
    checkAPI();
    loadBrowseData();
    // Spec-driven dropdowns first — independent of the catalogue, so the
    // form is usable even before /api/index-data resolves (or if it 404s).
    loadSpecDropdowns();
    // Catalogue index still loaded for the resolution fast-path
    // (`resolvePipingClass`); it's no longer needed for dropdown population.
    loadIndexData();
    loadEngineeringConstants();
});

// === Push to Valvesheet ===
// Two-step flow that keeps the target URL as a single source of truth
// in .env (EXTERNAL_VALVESHEET_API_URL):
//
//   1. GET  /api/sync/valvesheet/payload   — our backend returns a
//      ready-to-POST array of all cached PMS rows.
//   2. POST <valvesheet-api-url>           — the frontend sends that
//      array directly to the external Valvesheet API. The URL comes
//      from the meta tag injected by Jinja, which in turn reads .env.
//
// No URL is hardcoded in this file — to change targets, edit .env and
// redeploy. The real outbound request is visible in the browser's
// Network tab so you can see exactly what was sent and the response.
function getValvesheetApiUrl() {
    const meta = document.querySelector('meta[name="valvesheet-api-url"]');
    return (meta && meta.content && meta.content.trim()) || '';
}

function initValvesheetSync() {
    const btn = document.getElementById('syncValvesheetBtn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        if (btn.disabled) return;
        const originalHTML = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="icon">&#8635;</span><span>Syncing…</span>';
        try {
            const targetUrl = getValvesheetApiUrl();
            if (!targetUrl) {
                showToast(
                    'EXTERNAL_VALVESHEET_API_URL is not set in .env — nothing to push to.',
                    'error',
                );
                return;
            }

            // ── Step 1: fetch the payload dict from our backend ──
            //   { "A1": {notes, service, version}, "A1N": {...}, ... }
            // We still fetch it as ONE dict but send it as N POSTs —
            // one per spec — because the valvesheet API processes
            // sheets individually and the per-request response gives
            // cleaner per-spec success/fail reporting.
            const payloadResp = await fetch('/api/sync/valvesheet/payload');
            if (!payloadResp.ok) {
                const body = await payloadResp.json().catch(() => ({}));
                const msg = body.detail || `HTTP ${payloadResp.status}`;
                showToast(`Could not build payload — ${msg}`, 'error');
                return;
            }
            const bodyJson = await payloadResp.json();
            const bulkPayload = bodyJson.payload || {};
            const codes = Object.keys(bulkPayload);
            const total = codes.length;
            if (total === 0) {
                showToast('Nothing to sync — pms_cache is empty.', 'info');
                return;
            }

            // ── Step 2: one POST per spec, shape {"A1": {...}} ──
            const CONCURRENCY = 4;
            const successes = [];
            const failures = [];

            async function sendOne(code) {
                const singlePayload = { [code]: bulkPayload[code] };
                try {
                    const r = await fetch(targetUrl, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'accept': 'application/json',
                        },
                        body: JSON.stringify(singlePayload),
                    });
                    const text = await r.text();
                    let parsed = null;
                    try { parsed = JSON.parse(text); } catch { /* non-JSON */ }
                    // HTTP 200 with db_failed for our spec is still a
                    // failure — the valvesheet side saves the JSON to
                    // disk (→ ok:true) but may still reject the DB
                    // insert per-sheet. Reconcile both signals.
                    let failedForCode = [];
                    if (parsed && Array.isArray(parsed.db_failed)) {
                        failedForCode = parsed.db_failed.filter(
                            (f) => f && f.spec_code === code,
                        );
                    }
                    if (r.ok && failedForCode.length === 0) {
                        successes.push(code);
                    } else {
                        const err = failedForCode.length
                            ? failedForCode[0].error
                            : `HTTP ${r.status}: ${(text || '').slice(0, 150)}`;
                        failures.push({ piping_class: code, error: String(err) });
                    }
                } catch (e) {
                    failures.push({
                        piping_class: code,
                        error: (e && e.message) || String(e),
                    });
                }
                const done = successes.length + failures.length;
                btn.innerHTML =
                    `<span class="icon">&#8635;</span><span>Syncing ${done}/${total}…</span>`;
            }

            const queue = [...codes];
            const workers = Array.from(
                { length: Math.min(CONCURRENCY, queue.length) },
                async () => {
                    while (queue.length) {
                        const next = queue.shift();
                        if (next) await sendOne(next);
                    }
                },
            );
            await Promise.all(workers);

            if (failures.length === 0) {
                showToast(
                    `✓ Pushed ${successes.length} spec${successes.length === 1 ? '' : 's'} to Valvesheet`,
                    'success',
                );
            } else if (successes.length === 0) {
                showToast(
                    `Valvesheet rejected all ${failures.length} — ${failures[0].piping_class}: ${failures[0].error}`,
                    'error',
                );
            } else {
                showToast(
                    `Pushed ${successes.length}, failed ${failures.length} — ${failures[0].piping_class}: ${failures[0].error}`,
                    'error',
                );
            }
            console.log('[valvesheet] target:', targetUrl,
                        'successes:', successes,
                        'failures:', failures);
        } catch (err) {
            // Network errors (CORS, DNS, offline) land here. The error
            // message tells you which — e.g. "Failed to fetch" is CORS
            // or network; "NetworkError" is DNS/offline.
            showToast(`Sync failed — ${err && err.message || err}`, 'error');
            console.error('[valvesheet] sync error:', err);
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHTML;
        }
    });
}

// === Theme Toggle ===
function initTheme() {
    const saved = localStorage.getItem('pms_theme') || 'dark';
    setTheme(saved);
    document.getElementById('themeToggle').addEventListener('click', () => {
        const current = document.documentElement.getAttribute('data-theme');
        setTheme(current === 'dark' ? 'light' : 'dark');
    });
}

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('pms_theme', theme);
    document.getElementById('themeIcon').textContent = theme === 'dark' ? '\u263E' : '\u2600';
    document.getElementById('themeLabel').textContent = theme === 'dark' ? 'Dark' : 'Light';
}

// === Top Nav Tabs ===
function initTabs() {
    document.querySelectorAll('.nav-tab').forEach(t => {
        t.addEventListener('click', e => {
            if (t.getAttribute('href') !== '#') return; // Let config link navigate
            e.preventDefault();
            document.querySelectorAll('.nav-tab').forEach(n => n.classList.remove('active'));
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            t.classList.add('active');
            document.getElementById(`tab-${t.dataset.tab}`).classList.add('active');
        });
    });
}

// === Result Tabs ===
function initResultTabs() {
    document.querySelectorAll('.result-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.result-tab').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.result-panel').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(`rtab-${btn.dataset.rtab}`).classList.add('active');
        });
    });
}

// === Form ===
function initForm() {
    document.getElementById('pmsForm').addEventListener('submit', async e => { e.preventDefault(); await generatePMS(); });
    document.getElementById('downloadBtn').addEventListener('click', downloadExcel);
    document.getElementById('copyJsonBtn').addEventListener('click', () => {
        if (currentPMS) { navigator.clipboard.writeText(JSON.stringify(currentPMS, null, 2)); showToast('JSON copied to clipboard', 'info'); }
    });
    document.getElementById('loadJsonBtn').addEventListener('click', loadJsonFromClipboard);
}

async function loadJsonFromClipboard() {
    try {
        const text = await navigator.clipboard.readText();
        if (!text.trim()) { showToast('Clipboard is empty', 'error'); return; }
        const pms = JSON.parse(text);
        if (!pms.piping_class) { showToast('Invalid PMS JSON — missing piping_class', 'error'); return; }
        currentPMS = pms;
        renderPMSCodeBanner(currentPMS);
        renderFullResult(currentPMS);
        document.getElementById('resultsContainer').style.display = '';
        document.querySelector('.result-tabs-nav').style.display = '';
        document.querySelectorAll('.result-panel').forEach(p => p.style.display = '');
        document.getElementById('actionBar').style.display = '';
        showToast(`Loaded PMS for ${pms.piping_class} from clipboard`, 'success');
    } catch (err) {
        if (err.name === 'NotAllowedError') {
            // Fallback: prompt user to paste
            const text = prompt('Paste PMS JSON here:');
            if (!text) return;
            try {
                const pms = JSON.parse(text);
                if (!pms.piping_class) { showToast('Invalid PMS JSON', 'error'); return; }
                currentPMS = pms;
                renderPMSCodeBanner(currentPMS);
                renderFullResult(currentPMS);
                document.getElementById('resultsContainer').style.display = '';
                document.querySelector('.result-tabs-nav').style.display = '';
                document.querySelectorAll('.result-panel').forEach(p => p.style.display = '');
                document.getElementById('actionBar').style.display = '';
                showToast(`Loaded PMS for ${pms.piping_class} from JSON`, 'success');
            } catch { showToast('Invalid JSON format', 'error'); }
        } else {
            showToast('Invalid JSON: ' + err.message, 'error');
        }
    }
}

// ============================================================
// === CASCADING DROPDOWNS: Class -> Material -> CA -> Service
// ============================================================

async function loadIndexData() {
    try {
        const res = await API.indexData();
        if (!res.ok) return;
        indexData = await res.json();
        populateClassDropdown();
    } catch {}
}

// Spec-driven dropdown options. Sourced at page load from:
//   • /api/options/ratings              -> pressure_ratings.json
//   • /api/options/materials            -> spec_options.SPEC_MATERIALS
//   • /api/options/corrosion-allowances -> spec_options.SPEC_CORROSION_ALLOWANCES
//
// Backend is the single source of truth — these fallback constants just
// avoid an empty UI if the API call fails (offline, slow, etc.). Keep them
// in lock-step with `app/data/spec_options.py` and `app/data/pressure_ratings.json`.
const SPEC_RATINGS_FALLBACK = [
    '150#', '300#', '600#', '900#', '1500#', '2500#', '5000#', '10000#', 'Tubing',
];
const SPEC_MATERIALS_FALLBACK = [
    'CS', 'CS NACE', 'LTCS', 'LTCS NACE', 'CS GALV', 'CS - Epoxy Lined',
    'SS316', 'SS316L', 'SS316L NACE', 'DSS', 'DSS NACE', 'SDSS', 'SDSS NACE',
    'CuNi', 'Copper', 'GRE', 'CPVC', 'TITANIUM',
];
const SPEC_CAS_FALLBACK = ['NIL', '1.5 mm', '3 mm', '6 mm'];

// Re-exposed for downstream code that still references the legacy names.
const SPEC_RATINGS_ORDER = SPEC_RATINGS_FALLBACK.filter(r => r !== 'Tubing');
const SPEC_MATERIALS = SPEC_MATERIALS_FALLBACK;
const SPEC_CAS = SPEC_CAS_FALLBACK;

// Populate one <select> with placeholder + the supplied options.
function _fillSelect(sel, placeholder, options) {
    sel.innerHTML = `<option value="">${placeholder}</option>`;
    options.forEach(value => {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = value;
        sel.appendChild(opt);
    });
}

// Fetch a list endpoint with graceful fallback to a hardcoded constant.
async function _fetchOptions(apiCall, fallback) {
    try {
        const res = await apiCall();
        if (res.ok) {
            const list = await res.json();
            if (Array.isArray(list) && list.length) return list;
        }
    } catch (e) {
        console.warn('Spec options fetch failed; using fallback:', e);
    }
    return fallback;
}

// Page-load: fetch all three option lists in parallel and populate the
// Rating / Material / CA dropdowns. Each dropdown is INDEPENDENT — no
// cascading off the catalogue. The Custom-Class panel handles every
// combination (catalogued => fast path, otherwise standards / AI).
async function loadSpecDropdowns() {
    const ratingSel   = document.getElementById('pipingClass');
    const materialSel = document.getElementById('material');
    const caSel       = document.getElementById('corrosionAllowance');

    const [ratings, materials, cas] = await Promise.all([
        _fetchOptions(API.optionsRatings,            SPEC_RATINGS_FALLBACK),
        _fetchOptions(API.optionsMaterials,          SPEC_MATERIALS_FALLBACK),
        _fetchOptions(API.optionsCorrosionAllowances, SPEC_CAS_FALLBACK),
    ]);

    _fillSelect(ratingSel,   '-- Select Rating --',   ratings);
    _fillSelect(materialSel, '-- Select Material --', materials);
    _fillSelect(caSel,       '-- Select CA --',       cas);

    // All three are pickable from page-load — no disabled-then-enabled dance.
    materialSel.disabled = false;
    caSel.disabled = false;
}

// Legacy shim: kept so existing call-sites (loadIndexData) compile. The
// rating dropdown is now populated by loadSpecDropdowns at page-load.
function populateClassDropdown() { /* no-op — handled by loadSpecDropdowns */ }

// SPEC_MATERIALS / SPEC_CAS were declared earlier in this file (next to
// the FALLBACK constants used by `loadSpecDropdowns`). The data values
// now live in `app/data/spec_options.py` on the backend; the frontend
// constants are just fallbacks for when the API call fails.

// Default cold-rated design pressure per ASME class — used by the
// AI-only panel to pre-fill the Design Pressure input so the user
// doesn't have to type a value. Two regimes:
//
//   • B16.5 territory (150#–2500#): values are the cold-rated barg of
//     B16.5 Group 1.1 (carbon steel) at 38 °C — a safe default for
//     any non-tubing material since most B16.5 groups have very
//     similar cold ratings within ~10%.
//
//   • API 6A territory (5000# / 10000#): the class number IS the cold-
//     rated pressure in psig, so we convert directly.
//
// User can override either input on the panel; this is just the
// no-input default so the form is one-click usable.
const RATING_DEFAULT_PRESSURE_BARG = {
    '150#':    19.6,     // B16.5 G1.1 cold rating
    '300#':    51.1,
    '600#':   102.1,
    '900#':   153.2,
    '1500#':  255.3,
    '2500#':  425.5,
    '5000#':  344.7,     // API 6A: 5000 psig × 0.06895
    '10000#': 689.5,     // API 6A: 10000 psig × 0.06895
};
const AI_ONLY_DEFAULT_DESIGN_TEMP_C = 38;   // B16.5 cold-endpoint convention

function initCascadingDropdowns() {
    const ratingSelect   = document.getElementById('pipingClass');
    const materialSelect = document.getElementById('material');
    const caSelect       = document.getElementById('corrosionAllowance');
    const serviceInput   = document.getElementById('service');

    // Dropdown population now happens once at page load via
    // `loadSpecDropdowns()`. Each dropdown is INDEPENDENT — picking a
    // rating no longer narrows the Material list. The user can pick any
    // of the three in any order; resolution fires when all three are set.
    //
    // Resolution flow (unchanged behaviour):
    //   1. (rating, material, CA) all set
    //   2. Try the catalogue (`resolvePipingClass`) — fast path.
    //   3. Miss? Hit /api/preview-custom-class for the standards-derived
    //      preview and render the Custom-Class opt-in panel.

    function tryResolve() {
        clearCustomClassPanel();
        const rating = ratingSelect.value;
        const mat    = materialSelect.value;
        const ca     = caSelect.value;
        if (!rating || !mat || !ca) return;

        const cataloguedMatch = resolvePipingClass(rating, mat, ca);
        if (cataloguedMatch) {
            // Fast path — this combination is in the catalogue. The
            // existing form-submit handler picks up the class from the
            // dropdown selections and proceeds normally.
            return;
        }
        // Catalogue miss — fetch derivation preview from the backend
        // and render the opt-in panel. Don't block the form: the user
        // chooses whether to opt in to standards-driven generation.
        renderCustomClassPanel({ rating, material: mat, ca });
    }

    // Resolution fires whenever any of the three dropdowns changes —
    // doesn't matter which one the user picks last. The guard inside
    // `tryResolve` exits cleanly while they're still mid-selection.
    ratingSelect.addEventListener('change', tryResolve);
    materialSelect.addEventListener('change', tryResolve);
    caSelect.addEventListener('change', tryResolve);
}

// Resolve piping class from rating + material + CA. Mirrors the
// `ratingMatches` rule above: a "Tubing" pick resolves to a T-prefixed
// class without checking the catalogue's "-" rating field.
function resolvePipingClass(rating, material, ca) {
    const match = indexData.find(d => {
        const ratingOk = (rating === 'Tubing')
            ? (d.piping_class || '').toUpperCase().startsWith('T')
            : (d.rating === rating);
        return ratingOk && d.material === material && d.corrosion_allowance === ca;
    });
    return match ? match.piping_class : null;
}


// ── Custom-class (standards-derived) opt-in panel ─────────────────
//
// When the user picks (rating, material, CA) that doesn't resolve to a
// catalogued class, we don't block the form. Instead we render an
// inline panel under the form that:
//
//   1. shows the §5.5-derived class code (e.g. "A2")
//   2. previews the B16.5-derived P-T table (read-only)
//   3. exposes optional Design Pressure / Design Temperature inputs
//      so the user can specify operating conditions explicitly
//      (used for the §345.4.2(b) hydrotest correction)
//   4. asks the user to opt-in: "Yes — generate from standards" or
//      they can change inputs to a catalogued combination instead.
//
// Backend hookup: the panel calls /api/preview-custom-class to fetch
// the derived class + P-T data. On opt-in, the form-submit handler
// (initFormHandler in app.js) picks up the derived class code from
// `customClassState.classCode` and the design P/T from the input
// fields, and POSTs to /api/generate-pms as a normal request.

let customClassState = null;  // {classCode, designP, designT, derivedPt} | null

function clearCustomClassPanel() {
    customClassState = null;
    const panel = document.getElementById('customClassPanel');
    if (panel) panel.innerHTML = '';
}

async function renderCustomClassPanel({ rating, material, ca }) {
    const panel = ensureCustomClassPanel();
    panel.innerHTML = `
        <div class="custom-class-loading">
            <span>Checking standards for ${escapeHtml(rating)} / ${escapeHtml(material)} / ${escapeHtml(ca)}…</span>
        </div>
    `;
    // Bring the panel into view so the user sees the standards-derived
    // result land. Nice-to-have but high-impact: without this, the panel
    // is below the form and users miss it entirely.
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    let resp;
    try {
        const r = await fetch('/api/preview-custom-class', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                rating, material, corrosion_allowance: ca,
            }),
        });
        resp = await r.json();
    } catch (e) {
        panel.innerHTML = `
            <div class="custom-class-error">
                <strong>Could not check the standards.</strong>
                Network error: ${escapeHtml(String(e))}
            </div>
        `;
        return;
    }

    // Backend returns `mode`: 'catalogued' | 'standards_derived' | 'ai_only' |
    // 'unsupported'. Render the appropriate panel for each case.
    const mode = resp.mode;

    if (mode === 'unsupported') {
        // §5.5 itself can't make sense of this combo — usually means the
        // material/CA pair isn't in the digit table. Show why so the user
        // can fix the inputs; no opt-in offered.
        panel.innerHTML = `
            <div class="custom-class-error">
                <strong>This combination doesn't fit the §5.5 naming convention.</strong>
                <p>${escapeHtml(resp.reason)}</p>
                <p class="hint">Pick a different material or CA, or stick with a catalogued combination (no <em>(via standards)</em> tag).</p>
            </div>
        `;
        customClassState = null;
        return;
    }

    if (mode === 'catalogued') {
        // Combination resolved to a catalogue class. The dropdowns must
        // have lost sync somehow — keep the panel quiet so the form
        // submits normally.
        clearCustomClassPanel();
        return;
    }

    if (mode === 'standards_derived') {
        renderStandardsPanel(panel, resp);
        return;
    }
    if (mode === 'ai_only') {
        renderAiOnlyPanel(panel, resp);
        return;
    }

    // Unknown mode — defensive fallback.
    panel.innerHTML = `
        <div class="custom-class-error">
            <strong>Unexpected response from the standards check.</strong>
            <p>Mode: <code>${escapeHtml(String(mode))}</code></p>
        </div>
    `;
    customClassState = null;
}


// ── Renderer: standards-derived custom class (Slice 1 fast path) ──
// Used when the §5.5 class code resolves AND we have B16.5 P-T data
// for that material group. Shows the full standards-grounded preview.

function renderStandardsPanel(panel, resp) {
    const preview = resp.preview;
    const pt = preview.pressure_temperature;
    // The "design point" we pre-fill is the highest rated temperature
    // and the rated pressure AT THAT TEMPERATURE (which is the lowest
    // pressure in a monotonically-decreasing P-T table). This is a
    // consistent design point — the line is genuinely rated for that
    // pressure at that temperature. Pre-filling with `(max P at max T)`
    // would be inconsistent (max P only applies at min T) and would
    // trip the backend's §345.4.2(b) over-rating guard.
    const idxMaxT = pt.temperatures.indexOf(Math.max(...pt.temperatures));
    const defaultT = pt.temperatures[idxMaxT];
    const defaultP = pt.pressures[idxMaxT];
    const ceilingP = Math.max(...pt.pressures);
    const ceilingT = Math.max(...pt.temperatures);

    customClassState = {
        classCode: resp.class_code,
        designP:   defaultP,
        designT:   defaultT,
        aiOnly:    false,
        derivedPt: pt,
    };

    panel.innerHTML = `
        <div class="custom-class-card">
            <div class="cc-header">
                <span class="cc-badge">CUSTOM CLASS — DERIVED FROM STANDARDS</span>
                <h3>${escapeHtml(resp.class_code)}</h3>
                <p class="cc-reason">${escapeHtml(resp.reason)}</p>
            </div>

            <div class="cc-section">
                <h4>Pressure-Temperature table (from ASME B16.5)</h4>
                <table class="cc-pt-table">
                    <thead>
                        <tr>
                            <th>Temperature (°C)</th>
                            ${pt.temperatures.map(t => `<th>${formatTemp(t, pt.temp_labels)}</th>`).join('')}
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td><strong>Pressure (barg)</strong></td>
                            ${pt.pressures.map(p => `<td>${p}</td>`).join('')}
                        </tr>
                    </tbody>
                </table>
                <p class="cc-hint">Cold-rated point: <strong>${ceilingP} barg @ ${pt.temperatures[pt.pressures.indexOf(ceilingP)]}°C</strong> · Hottest rated: <strong>${defaultP} barg @ ${defaultT}°C</strong></p>
            </div>

            <div class="cc-section">
                <h4>Design conditions (auto-filled from the P-T table — edit to override)</h4>
                <div class="cc-design-inputs">
                    <label>
                        Design Pressure (barg)
                        <input type="number" id="ccDesignP" step="0.1" min="0" value="${defaultP}">
                    </label>
                    <label>
                        Design Temperature (°C)
                        <input type="number" id="ccDesignT" step="1" value="${defaultT}">
                    </label>
                </div>
                <p class="cc-hint">Defaults to the hottest rated point (most conservative). Change Design Temperature and Design Pressure auto-syncs to the rated value at that temp. Drives the §345.4.2(b) hydrotest correction.</p>
            </div>

            <div class="cc-confirm">
                <label class="cc-confirm-line">
                    <input type="checkbox" id="ccOptIn">
                    <span>Generate <strong>${escapeHtml(resp.class_code)}</strong> using §5.5 + ASME B16.5 standards (this combination is not in the catalogue).</span>
                </label>
            </div>
        </div>
    `;
    wireCustomClassInputs(/* aiOnly */ false, /* ptForAutoSync */ pt);
}


// ── Renderer: AI-only custom class (no indexed standards data) ────
// Used when the §5.5 class code is valid but no B16.5 / API 6A / etc.
// data is indexed for this rating-material combination yet (e.g. 5000#
// J-series, SS316L pre-Slice-2). The AI generates everything from its
// own training knowledge.
//
// Differences from the standards panel:
//   • Yellow/warning colour scheme + clear "AI-only" tag.
//   • No P-T preview (AI builds it at generation time).
//   • Design Pressure + Design Temperature are REQUIRED inputs (no
//     rated ceiling to default to). The opt-in checkbox stays disabled
//     until both are filled.
//   • Stronger opt-in language: "I understand the AI-only output is
//     not standards-verified — I will review carefully."

function renderAiOnlyPanel(panel, resp) {
    // Read the rating directly from the form so we can look up the
    // appropriate cold-rated default pressure. The preview-custom-class
    // response doesn't echo the rating back, but we can re-derive it
    // from the §5.5 class code's first letter via the same lookup the
    // backend uses (rating_from_class_code).
    const ratingPicked = document.getElementById('pipingClass').value.trim();
    const defaultP = RATING_DEFAULT_PRESSURE_BARG[ratingPicked] ?? null;
    const defaultT = AI_ONLY_DEFAULT_DESIGN_TEMP_C;

    customClassState = {
        classCode: resp.class_code,
        designP:   defaultP,
        designT:   defaultT,
        aiOnly:    true,
        derivedPt: null,
    };

    // Hint text shown beneath the design inputs — varies by whether we
    // could pre-fill a sensible default for the chosen rating.
    const designHint = defaultP !== null
        ? `Auto-filled with the cold-rated default for ${escapeHtml(ratingPicked)} (${defaultP} barg at ${defaultT} °C). Edit either field to change the design point. Drives the §345.4.2(b) hydrotest correction.`
        : `No standard default exists for ${escapeHtml(ratingPicked)} — enter the line's actual design pressure and temperature. Drives the §345.4.2(b) hydrotest correction.`;

    panel.innerHTML = `
        <div class="custom-class-card cc-ai-only">
            <div class="cc-header">
                <span class="cc-badge cc-badge-warn">CUSTOM CLASS — AI-ONLY (NO STANDARDS GROUNDING)</span>
                <h3>${escapeHtml(resp.class_code)}</h3>
                <p class="cc-reason">${escapeHtml(resp.reason)}</p>
            </div>

            <div class="cc-section">
                <h4>What this means</h4>
                <p class="cc-warn-text">
                    The standards engine doesn't have indexed P-T / material data
                    for this combination. The AI will generate the full PMS from
                    its training knowledge of ASME / API / NACE conventions, but
                    the output is <strong>not verified against an indexed
                    standard</strong>. Review every value before use.
                </p>
            </div>

            <div class="cc-section">
                <h4>Design conditions (auto-filled — edit to override)</h4>
                <div class="cc-design-inputs">
                    <label>
                        Design Pressure (barg)
                        <input type="number" id="ccDesignP" step="0.1" min="0.1"
                               value="${defaultP !== null ? defaultP : ''}"
                               placeholder="${defaultP !== null ? defaultP : 'e.g. 345 for 5000# CS'}">
                    </label>
                    <label>
                        Design Temperature (°C)
                        <input type="number" id="ccDesignT" step="1"
                               value="${defaultT}"
                               placeholder="${defaultT}">
                    </label>
                </div>
                <p class="cc-hint">${designHint}</p>
            </div>

            <div class="cc-confirm">
                <label class="cc-confirm-line">
                    <input type="checkbox" id="ccOptIn"${defaultP !== null ? '' : ' disabled'}>
                    <span>I understand the output for <strong>${escapeHtml(resp.class_code)}</strong> is AI-only and not standards-verified. I will review every field before use.</span>
                </label>
            </div>
        </div>
    `;
    wireCustomClassInputs(/* aiOnly */ true);
}


// Linear-interpolate a P-T table at a target temperature. Mirrors the
// backend's `interpolate_pressure_at_temp` in app/utils/engineering.py
// so the auto-sync behaviour the user sees in the panel matches what
// the §345.4.2(b) hydrotest correction will compute server-side.
function interpolatePressureAtTemp(temperatures, pressures, targetT) {
    if (!temperatures.length || !pressures.length) return 0;
    const pairs = temperatures
        .map((t, i) => [Number(t), Number(pressures[i])])
        .sort((a, b) => a[0] - b[0]);
    if (targetT <= pairs[0][0]) return pairs[0][1];
    if (targetT >= pairs[pairs.length - 1][0]) return pairs[pairs.length - 1][1];
    for (let i = 0; i < pairs.length - 1; i++) {
        const [t1, p1] = pairs[i];
        const [t2, p2] = pairs[i + 1];
        if (t1 <= targetT && targetT <= t2) {
            const f = (targetT - t1) / (t2 - t1);
            return p1 + f * (p2 - p1);
        }
    }
    return pairs[pairs.length - 1][1];
}


// Wire opt-in checkbox + design inputs to customClassState.
//
//   • aiOnly=true:  Opt-in box stays disabled until both design fields
//                   have valid values (no rated ceiling to fall back on).
//
//   • aiOnly=false + ptForAutoSync supplied: Standards-derived mode.
//                   Pre-filled values land on first render via the
//                   `value=...` HTML attributes; in addition we wire
//                   the Design Temperature input so any change auto-
//                   recomputes Design Pressure as the interpolated
//                   rated pressure at the new temperature. The user
//                   can override Design Pressure afterwards if they
//                   want to design below the rated value.
function wireCustomClassInputs(aiOnly, ptForAutoSync) {
    const optInBox = document.getElementById('ccOptIn');
    const dpInput = document.getElementById('ccDesignP');
    const dtInput = document.getElementById('ccDesignT');

    // Tracks whether the user has manually edited Design Pressure since
    // the panel rendered (or since the last temp-driven auto-fill). When
    // false, changing Design Temperature also overwrites Design Pressure
    // with the new rated value. Once the user types in the Design Pressure
    // field, we stop overwriting so their value survives subsequent T
    // edits — that's the "I want to design below rating" workflow.
    let dpUserEdited = false;

    function syncState() {
        const dp = parseFloat(dpInput.value);
        const dt = parseFloat(dtInput.value);
        customClassState.designP = isNaN(dp) ? null : dp;
        customClassState.designT = isNaN(dt) ? null : dt;
        customClassState.optedIn = optInBox.checked;

        if (aiOnly) {
            // Opt-in only allowed once both design fields have valid values.
            const ready = customClassState.designP !== null && customClassState.designP > 0
                       && customClassState.designT !== null;
            optInBox.disabled = !ready;
            if (!ready && optInBox.checked) {
                optInBox.checked = false;
                customClassState.optedIn = false;
            }
        }
    }

    optInBox.addEventListener('change', syncState);
    dpInput.addEventListener('input', () => {
        dpUserEdited = true;     // user took manual control of Design P
        syncState();
    });
    dtInput.addEventListener('input', () => {
        // Standards-derived mode: when the user changes Design T and
        // hasn't manually edited Design P, recompute Design P as the
        // rated value at the new temperature. The auto-fill happens in
        // the input field directly so the user sees what's about to
        // be submitted; they can still override afterwards.
        if (!aiOnly && ptForAutoSync && !dpUserEdited) {
            const newT = parseFloat(dtInput.value);
            if (!isNaN(newT)) {
                const ratedP = interpolatePressureAtTemp(
                    ptForAutoSync.temperatures, ptForAutoSync.pressures, newT,
                );
                // Round to one decimal so the field looks tidy
                dpInput.value = (Math.round(ratedP * 10) / 10).toFixed(1);
            }
        }
        syncState();
    });
    syncState();
}

function ensureCustomClassPanel() {
    let panel = document.getElementById('customClassPanel');
    if (panel) return panel;
    panel = document.createElement('div');
    panel.id = 'customClassPanel';
    // `custom-class-panel full-width` keeps the panel as a single grid
    // cell that spans every column of the form's CSS grid (the same
    // class the multi-select Service field uses for the same reason).
    panel.className = 'custom-class-panel full-width';

    // Insert the panel as the LAST child of the form's grid that
    // precedes `.form-actions`. End result: dropdowns → panel → Generate
    // button, all in one continuous form. This keeps the warning panel
    // visually tied to the form and ensures the user sees it BEFORE
    // they see the submit button — the previous layout placed the
    // panel below the button, which led to users clicking Generate
    // without realising the warning existed.
    const form = document.getElementById('pmsForm');
    const actions = form ? form.querySelector('.form-actions') : null;
    if (form && actions) {
        form.insertBefore(panel, actions);
    } else if (form) {
        form.appendChild(panel);
    } else {
        document.body.appendChild(panel);
    }
    return panel;
}

function formatTemp(t, labels) {
    // P-T tables sometimes store a label like "-29 to 38" for the cold
    // endpoint. Use it when present, otherwise fall back to the numeric
    // temp.
    if (labels && labels.length) {
        const i = labels.indexOf(String(t));
        if (i >= 0) return labels[i];
    }
    return String(t);
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

// === Service Description Multi-Select ===
// Options fetched from GET /api/services so the standalone UI and the
// Valvesheet frontend stay on the same canonical list. "Other" reveals a
// free-text input for one-off services that aren't in the list.
const SERVICE_OPTIONS_FALLBACK = [
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
];

async function fetchServiceOptions() {
    try {
        const res = await API.services();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const list = await res.json();
        return Array.isArray(list) && list.length ? list : SERVICE_OPTIONS_FALLBACK;
    } catch (e) {
        console.warn('[services] falling back to local list:', e);
        return SERVICE_OPTIONS_FALLBACK;
    }
}

async function initServiceMultiSelect() {
    const root    = document.getElementById('serviceMultiSelect');
    const panel   = document.getElementById('servicePanel');
    const trigger = document.getElementById('serviceTrigger');
    const label   = document.getElementById('serviceLabel');
    const hidden  = document.getElementById('service');
    const otherIn = document.getElementById('serviceOtherInput');
    if (!root || !panel || !trigger || !hidden) return;

    const options = await fetchServiceOptions();
    panel.innerHTML = '';
    options.forEach(opt => {
        const row = document.createElement('label');
        row.className = 'multi-select-option';
        row.dataset.value = opt;
        row.innerHTML = `<input type="checkbox"><span></span>`;
        row.querySelector('input').value = opt;
        row.querySelector('span').textContent = opt;
        panel.appendChild(row);
    });
    const div = document.createElement('div');
    div.className = 'multi-select-divider';
    panel.appendChild(div);
    const other = document.createElement('label');
    other.className = 'multi-select-option';
    other.dataset.value = '__OTHER__';
    other.innerHTML = `<input type="checkbox" value="__OTHER__"><span>Other (custom)</span>`;
    panel.appendChild(other);

    function syncValue() {
        const checks = Array.from(panel.querySelectorAll('input[type="checkbox"]'));
        const picks = checks.filter(c => c.checked && c.value !== '__OTHER__').map(c => c.value);
        const otherChecked = checks.some(c => c.value === '__OTHER__' && c.checked);
        otherIn.style.display = otherChecked ? 'block' : 'none';
        if (otherChecked) {
            const txt = otherIn.value.trim();
            if (txt) picks.push(txt);
        }
        const joined = picks.join(', ');
        hidden.value = joined;
        if (joined) {
            label.textContent = joined.length > 70 ? joined.slice(0, 67) + '…' : joined;
            label.classList.remove('placeholder');
        } else {
            label.textContent = 'Select one or more services…';
            label.classList.add('placeholder');
        }
        panel.querySelectorAll('.multi-select-option').forEach(r => {
            r.classList.toggle('selected', r.querySelector('input').checked);
        });
    }

    panel.addEventListener('change', e => {
        if (e.target.matches('input[type="checkbox"]')) syncValue();
    });
    otherIn.addEventListener('input', syncValue);

    trigger.addEventListener('click', e => {
        e.stopPropagation();
        const open = root.classList.toggle('open');
        trigger.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', e => {
        if (!root.contains(e.target)) {
            root.classList.remove('open');
            trigger.setAttribute('aria-expanded', 'false');
        }
    });
    panel.addEventListener('click', e => e.stopPropagation());
    otherIn.addEventListener('click', e => e.stopPropagation());
}

// === Design Condition Inputs ===
function initDesignInputs() {
    const dp = document.getElementById('designPressure');
    const dpPsig = document.getElementById('designPressurePsig');
    const dt = document.getElementById('designTemperature');
    const mdmt = document.getElementById('mdmt');
    const jt = document.getElementById('jointType');

    // Two-way sync flag to prevent infinite loops
    let syncing = false;

    const syncBargToPsig = () => {
        if (syncing) return;
        syncing = true;
        const barg = parseFloat(dp.value) || 0;
        dpPsig.value = (barg * 14.5038).toFixed(1);
        syncing = false;
    };

    const syncPsigToBarg = () => {
        if (syncing) return;
        syncing = true;
        const psig = parseFloat(dpPsig.value) || 0;
        dp.value = (psig / 14.5038).toFixed(2);
        syncing = false;
    };

    // When Design Temperature changes, interpolate the rated pressure from the P-T table
    // and update both the barg and psig pressure fields.
    const syncPressureFromTemp = () => {
        if (syncing) return;
        if (!currentPMS || !currentPMS.pressure_temperature) return;
        const temps = currentPMS.pressure_temperature.temperatures || [];
        const press = currentPMS.pressure_temperature.pressures || [];
        if (!temps.length || !press.length) return;
        const targetT = parseFloat(dt.value);
        if (isNaN(targetT)) return;
        const interpBarg = interpolatePressure(temps, press, targetT);
        if (interpBarg <= 0) return;
        syncing = true;
        dp.value = interpBarg.toFixed(2);
        dpPsig.value = (interpBarg * 14.5038).toFixed(1);
        syncing = false;
    };

    const update = () => {
        const dtv = parseFloat(dt.value) || 0;
        const mv = parseFloat(mdmt.value) || 0;
        document.getElementById('tempFahrenheit').textContent = `= ${c2f(dtv)} \u00b0F`;
        document.getElementById('mdmtFahrenheit').textContent = `= ${c2f(mv)} \u00b0F`;
        document.getElementById('jointRef').textContent = `ASME B31.3 Table A-1B`;
        if (currentPMS) updateCalculations();
    };

    // Sync events
    dp.addEventListener('input', () => { syncBargToPsig(); update(); });
    dpPsig.addEventListener('input', () => { syncPsigToBarg(); update(); });
    dt.addEventListener('input', () => {
        // 1) Interpolate P from P-T table using the new T, update both P fields
        syncPressureFromTemp();
        // 2) Run full update (recompute tables/MAWP/etc.)
        update();
    });
    [mdmt, jt].forEach(el => el.addEventListener('input', update));

    // Wire up Case 1 + Stress Override fields to also trigger re-render
    ['case1PressurePsig', 'case1StressPsi', 'case2StressPsi'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', update);
    });

    syncBargToPsig();
    update();
}

// === Step 1: Preview PMS (no AI call) ===
async function generatePMS() {
    const selectedRating = document.getElementById('pipingClass').value.trim();
    const selectedMaterial = document.getElementById('material').value;
    const selectedCA = document.getElementById('corrosionAllowance').value;
    const selectedService = document.getElementById('service').value.trim();

    if (!selectedRating || !selectedMaterial) { showToast('Please select Rating and Material', 'error'); return; }

    // Try to resolve via the catalogue first.
    let resolvedClass = resolvePipingClass(selectedRating, selectedMaterial, selectedCA);

    // Catalogue miss → route through the standards-derivation path.
    // The Custom-Class panel below the form shows the user what's about
    // to be derived (or why it can't be) and asks for explicit opt-in.
    // The submit handler reads that panel's state to decide whether to
    // proceed, refuse with context, or guide the user back to the panel.
    if (!resolvedClass) {
        const panel = document.getElementById('customClassPanel');
        const isLoading = !!(panel && panel.querySelector('.custom-class-loading'));
        const hasError  = !!(panel && panel.querySelector('.custom-class-error'));
        const hasOptIn  = !!(panel && panel.querySelector('#ccOptIn'));

        if (!panel || !panel.children.length) {
            // No panel at all — most likely the preview-custom-class
            // call hasn't been triggered yet. Re-trigger and ask the
            // user to wait.
            renderCustomClassPanel({
                rating: selectedRating, material: selectedMaterial, ca: selectedCA,
            });
            showToast(
                'Checking the standards for this combination — please retry in a moment.',
                'warning',
            );
            return;
        }
        if (isLoading) {
            showToast(
                'Still checking the standards — give it a second and try again.',
                'info',
            );
            return;
        }
        if (hasError) {
            // Combination isn't supported. Scroll the panel into view so
            // the user sees the actual reason rather than a generic toast.
            panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            showToast(
                'This combination isn\'t supported by the standards engine yet. '
                + 'See the panel below the form for the reason.',
                'error',
            );
            return;
        }
        if (hasOptIn && (!customClassState || !customClassState.optedIn)) {
            // Panel is rendered with an opt-in box but the user hasn't
            // ticked it. Two sub-cases:
            //   (a) The checkbox is enabled — user just needs to tick.
            //   (b) The checkbox is DISABLED (AI-only mode, design P/T
            //       not filled in) — telling the user to "tick the box"
            //       is misleading because they can't. Send them to the
            //       design inputs instead.
            panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            const box = document.getElementById('ccOptIn');
            const dpInput = document.getElementById('ccDesignP');
            const dtInput = document.getElementById('ccDesignT');

            if (box && box.disabled) {
                // AI-only mode — the box is locked until both design
                // fields are filled. Highlight the design inputs and
                // focus the first empty one so the user can start
                // typing immediately.
                const target = (!dpInput.value || parseFloat(dpInput.value) <= 0)
                    ? dpInput
                    : (!dtInput.value ? dtInput : dpInput);
                if (target) {
                    target.focus();
                    target.style.transition = 'background 0.3s, border-color 0.3s';
                    target.style.background = '#fff3cd';
                    target.style.borderColor = '#d68d00';
                    setTimeout(() => {
                        target.style.background = '';
                        target.style.borderColor = '';
                    }, 2500);
                }
                showToast(
                    'Fill in the Design Pressure and Design Temperature in the '
                    + 'panel below — then the opt-in checkbox unlocks and you '
                    + 'can confirm.',
                    'warning',
                );
                return;
            }

            // Standards-derived mode — checkbox is enabled, user just
            // hasn't ticked it.
            if (box) {
                box.focus();
                const line = box.closest('.cc-confirm-line');
                if (line) {
                    line.style.transition = 'background 0.3s';
                    line.style.background = '#fff3cd';
                    line.style.padding = '8px';
                    line.style.borderRadius = '4px';
                    setTimeout(() => {
                        line.style.background = '';
                        line.style.padding = '';
                    }, 2000);
                }
            }
            showToast(
                'Almost there — tick the "Generate from standards" checkbox in the '
                + 'highlighted panel below to confirm, then click Generate again.',
                'warning',
            );
            return;
        }
        // Opt-in confirmed; carry on with the derived class code.
        resolvedClass = customClassState.classCode;
    }

    const data = {
        piping_class: resolvedClass,
        material: selectedMaterial,
        corrosion_allowance: selectedCA,
        service: selectedService || 'General',
    };
    // Carry the Custom-Class panel's design P/T into the request so the
    // §345.4.2(b) hydrotest correction uses the user's operating point
    // rather than the rated P-T ceiling. Catalogued generations don't
    // need these — pms_service falls back to the ceiling automatically.
    if (customClassState && customClassState.optedIn) {
        if (customClassState.designP !== null && customClassState.designP !== undefined) {
            data.design_pressure_barg = customClassState.designP;
        }
        if (customClassState.designT !== null && customClassState.designT !== undefined) {
            data.design_temp_c = customClassState.designT;
        }
    }

    // Save request for Step 2
    pendingPMSRequest = data;

    showLoading('Resolving piping class...');
    try {
        const res = await API.previewPMS(data);
        if (!res.ok) { const err = await res.json(); throw new Error(err.detail || 'Preview failed'); }
        const preview = await res.json();

        // Show banner card with "Generate Full PMS" button — no tabs yet
        renderPreviewBanner(preview);
        document.getElementById('resultsContainer').style.display = '';
        // Hide tabs, panels, and action bar until full generation
        document.querySelector('.result-tabs-nav').style.display = 'none';
        document.querySelectorAll('.result-panel').forEach(p => p.style.display = 'none');
        document.getElementById('actionBar').style.display = 'none';
        document.getElementById('resultsContainer').scrollIntoView({ behavior: 'smooth', block: 'start' });
        showToast(`Class ${preview.piping_class} resolved — click "Generate Full PMS" to load all data`, 'info');
    } catch (err) { showToast(err.message, 'error'); }
    finally { hideLoading(); }
}

// === Step 2: Full AI Generation (triggered from card button) ===
async function generateFullPMS() {
    if (!pendingPMSRequest) { showToast('No class selected. Please generate preview first.', 'error'); return; }

    // Disable the generate button and show loading state on it
    const btn = document.getElementById('bannerGenerateBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<svg class="spinner-icon" viewBox="0 0 24 24" width="18" height="18"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" fill="none" stroke-dasharray="31.4 31.4" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/></circle></svg> Generating with AI...`;
    }

    showLoading('Generating full PMS with AI — this may take 15-30 seconds...');
    try {
        const res = await API.generatePMS(pendingPMSRequest);
        if (!res.ok) { const err = await res.json(); throw new Error(err.detail || 'Generation failed'); }
        currentPMS = await res.json();

        // Replace preview banner with final banner
        renderPMSCodeBanner(currentPMS);

        // Show tabs, action bar, and render full result
        renderFullResult(currentPMS);
        document.querySelector('.result-tabs-nav').style.display = '';
        document.querySelectorAll('.result-panel').forEach(p => p.style.display = '');
        document.getElementById('actionBar').style.display = '';
        // Activate first result tab
        document.querySelectorAll('.result-tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.result-panel').forEach(p => p.classList.remove('active'));
        document.querySelector('.result-tab').classList.add('active');
        document.querySelector('.result-panel').classList.add('active');
        showToast('Full PMS generated successfully!', 'success');
    } catch (err) {
        showToast(err.message, 'error');
        // Re-enable button on error
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> Generate Full PMS`;
        }
    }
    finally { hideLoading(); }
}

// === Preview Banner (Step 1 — with Generate button) ===
function renderPreviewBanner(preview) {
    const banner = document.getElementById('pmsCodeBanner');
    banner.innerHTML = `
        <div class="pms-banner-header">Generated PMS Code</div>
        <div class="pms-banner-code">${preview.piping_class}</div>
        <div class="pms-banner-details">
            <span class="pms-banner-tag rating">${preview.rating}</span>
            <span class="pms-banner-tag material">${preview.material}</span>
            <span class="pms-banner-tag ca">${preview.corrosion_allowance} CA</span>
            <span class="pms-banner-tag service">${preview.service}</span>
        </div>
        <div class="pms-banner-id">PMS-${preview.piping_class}</div>
        <div class="pms-banner-action">
            <button class="btn btn-generate-full" id="bannerGenerateBtn" onclick="generateFullPMS()">
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                Generate Full PMS
            </button>
        </div>
    `;
}

// === Final PMS Code Banner (Step 2 — after full generation, with Regenerate button) ===
function renderPMSCodeBanner(pms) {
    const banner = document.getElementById('pmsCodeBanner');
    banner.innerHTML = `
        <div class="pms-banner-header">Generated PMS Code</div>
        <div class="pms-banner-code">${pms.piping_class}</div>
        <div class="pms-banner-details">
            <span class="pms-banner-tag rating">${pms.rating}</span>
            <span class="pms-banner-tag material">${pms.material}</span>
            <span class="pms-banner-tag ca">${pms.corrosion_allowance} CA</span>
            <span class="pms-banner-tag service">${pms.service}</span>
        </div>
        <div class="pms-banner-id">PMS-${pms.piping_class}</div>
        <div class="pms-banner-action">
            <button class="btn btn-regenerate" id="regenerateBtn" onclick="regenerateFullPMS()">
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>
                Regenerate with AI
            </button>
        </div>
    `;
}

// === Regenerate PMS (force fresh AI call, bypass DB cache) ===
async function regenerateFullPMS() {
    if (!pendingPMSRequest) { showToast('No class selected. Please generate first.', 'error'); return; }

    const btn = document.getElementById('regenerateBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<svg class="spinner-icon" viewBox="0 0 24 24" width="18" height="18"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" fill="none" stroke-dasharray="31.4 31.4" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/></circle></svg> Regenerating...`;
    }

    showLoading('Regenerating PMS with fresh AI data — this may take 15-30 seconds...');
    try {
        const res = await API.regeneratePMS(pendingPMSRequest);
        if (!res.ok) { const err = await res.json(); throw new Error(err.detail || 'Regeneration failed'); }
        currentPMS = await res.json();

        renderPMSCodeBanner(currentPMS);
        renderFullResult(currentPMS);
        showToast('PMS regenerated with fresh AI data!', 'success');
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        hideLoading();
        // Re-enable button (it's re-rendered by renderPMSCodeBanner, but just in case)
        const newBtn = document.getElementById('regenerateBtn');
        if (newBtn) { newBtn.disabled = false; }
    }
}

// === Render Full Result ===
function renderFullResult(pms) {
    const pt = pms.pressure_temperature;
    const temps = pt.temperatures;
    const press = pt.pressures;
    const n = Math.min(temps.length, press.length);

    // Auto-set Design Pressure & Temperature from P-T data
    const dp = document.getElementById('designPressure');
    const dt = document.getElementById('designTemperature');
    const mdmtInput = document.getElementById('mdmt');

    if (n > 0) {
        // Default Design Temperature = P-T table MAX temp (worst-case envelope, where P is min).
        // This aligns with ASME B31.3 design practice — design for worst envelope case.
        // User can change this; Case 2 calculations will use whatever they enter.
        let maxIdx = 0;
        for (let i = 1; i < n; i++) {
            if (parseFloat(temps[i]) > parseFloat(temps[maxIdx])) maxIdx = i;
        }
        dt.value = temps[maxIdx];
        dp.value = press[maxIdx];
        const minTemp = Math.min(...temps.filter(t => !isNaN(t)));
        if (isFinite(minTemp)) mdmtInput.value = minTemp;
    }

    dp.dispatchEvent(new Event('input'));

    // Tab 1: P-T Rating
    renderPTRatingTab(pms);

    // Tab 2: Schedule & Wall Thickness
    renderScheduleTab(pms);

    // Tab 3: Pipe & Fittings Material Assignment
    renderPipeFittingsTab(pms);

    // Tab 4: Components
    renderComponentsTab(pms);
}

// ============================================================
// === TAB 1: P-T Rating
// ============================================================
function renderPTRatingTab(pms) {
    const pt = pms.pressure_temperature;
    const temps = pt.temperatures;
    const n = Math.min(temps.length, pt.pressures.length);

    setKVList('pmsInputsList', [
        { l: 'PMS Code', v: pms.piping_class, bold: true },
        { l: 'Pressure Rating', v: `${pms.rating} (Class ${pms.rating.replace('#','')})` },
        { l: 'Material Type', v: pms.material, bold: true },
        { l: 'Material Grade', v: pms.pipe_data.length ? pms.pipe_data[0].material_spec : '\u2014' },
    ]);

    const isNACE = pms.material.toUpperCase().includes('NACE') || pms.design_code.toUpperCase().includes('NACE');
    const isLowTemp = pms.material.toUpperCase().includes('LT') || (n > 0 && Math.min(...temps) < -29);
    setKVList('serviceMaterialList', [
        { l: 'Service', v: pms.service },
        { l: 'Corrosion Allowance', v: pms.corrosion_allowance, bold: true },
        { l: 'Mill Tolerance', v: pms.mill_tolerance || '\u2014' },
        { l: 'Low Temperature', v: isLowTemp ? 'Yes' : 'No', tag: isLowTemp ? 'yes' : 'no' },
        { l: 'NACE MR0175', v: isNACE ? 'Yes' : 'No', tag: isNACE ? 'yes' : 'no' },
    ]);

    updateCalculations();
}

function updateCalculations() {
    if (!currentPMS) return;
    const pms = currentPMS;
    const dpVal = parseFloat(document.getElementById('designPressure').value) || 0;
    const dtVal = parseFloat(document.getElementById('designTemperature').value) || 0;
    const mdmtVal = parseFloat(document.getElementById('mdmt').value) || 0;

    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * ENG.hydrotest_factor);
    const htStr = typeof ht === 'number' ? ht.toFixed(1) : String(ht);
    const op = (dpVal * ENG.operating_pressure_factor).toFixed(1);
    const opT = (dtVal * ENG.operating_temp_factor).toFixed(1);

    setKVList('pressureCalcList', [
        { l: 'Design Pressure', v: `<strong>${dpVal} barg</strong> <span class="unit">(${barg2psig(dpVal)} psig)</span>` },
        { l: `Hydrotest${pms.hydrotest_pressure ? ' (from ref data)' : ' (1.5\u00d7DP)'}`, v: `<strong>${htStr} barg</strong> <span class="unit">(${barg2psig(parseFloat(htStr))} psig)</span>` },
        { l: 'Operating (est. 80% DP)', v: `${op} barg <span class="unit">(${barg2psig(parseFloat(op))} psig)</span>`, cls: 'warning' },
    ]);

    setKVList('tempCalcList', [
        { l: 'Design Temperature', v: `<strong>${dtVal}\u00b0C</strong> <span class="unit">(${c2f(dtVal)}\u00b0F)</span>` },
        { l: 'Operating (est. 80% DT)', v: `${opT}\u00b0C <span class="unit">(${c2f(parseFloat(opT))}\u00b0F)</span>`, cls: 'warning' },
        { l: 'MDMT', v: `<strong>${mdmtVal}\u00b0C</strong> <span class="unit">(${c2f(mdmtVal)}\u00b0F)</span>` },
    ]);

    // Material group
    const matGroups = {
        'CS': { g: '1.1', t: 'A105/A216 WCB' }, 'CS NACE': { g: '1.1', t: 'A105/A216 WCB' },
        'LTCS': { g: '1.1', t: 'A350 LF2/A352 LCB' }, 'LTCS NACE': { g: '1.1', t: 'A350 LF2/A352 LCB' },
        'SS316L': { g: '2.3', t: 'A182 F316L/A351 CF3M' }, 'SS316L NACE': { g: '2.3', t: 'A182 F316L/A351 CF3M' },
        'DSS': { g: '2.4', t: 'A182 F51' }, 'DSS NACE': { g: '2.4', t: 'A182 F51' },
        'SDSS': { g: '2.6', t: 'A182 F55' }, 'SDSS NACE': { g: '2.6', t: 'A182 F55' },
    };
    const mg = matGroups[pms.material] || { g: '1.1', t: 'Unknown' };
    document.getElementById('standardBar').innerHTML =
        `<strong>Standard:</strong> ASME B16.5-2020 &nbsp;|&nbsp; <strong>Table:</strong> Group ${mg.g} (${mg.t}) &nbsp;|&nbsp; <strong>Class:</strong> ${pms.rating} &nbsp;|&nbsp; <strong>Material:</strong> ${pms.material}`;

    renderPTTable(pms, dtVal);

    // Adequacy check
    const temps = pms.pressure_temperature.temperatures;
    const press = pms.pressure_temperature.pressures;
    const allowable = interpolatePressure(temps, press, dtVal);

    const box = document.getElementById('adequacyBox');
    if (allowable >= dpVal) {
        box.className = 'adequacy-box pass';
        box.innerHTML = `\u2713 &nbsp; Class ${pms.rating} is ADEQUATE: ${allowable} barg \u2265 Design ${dpVal} barg at ${dtVal}\u00b0C`;
    } else {
        box.className = 'adequacy-box fail';
        box.innerHTML = `\u2717 &nbsp; Class ${pms.rating} is NOT ADEQUATE: ${allowable} barg < Design ${dpVal} barg at ${dtVal}\u00b0C \u2014 Consider higher rating class`;
    }

    // Also update Schedule tab calculations
    renderScheduleTab(pms);
}

function interpolatePressure(temps, pressures, targetTemp) {
    if (!temps.length || !pressures.length) return 0;
    const n = Math.min(temps.length, pressures.length);
    const pairs = [];
    for (let i = 0; i < n; i++) pairs.push({ t: temps[i], p: pressures[i] });
    pairs.sort((a, b) => a.t - b.t);

    if (targetTemp <= pairs[0].t) return pairs[0].p;
    if (targetTemp >= pairs[pairs.length - 1].t) return pairs[pairs.length - 1].p;

    for (let i = 0; i < pairs.length - 1; i++) {
        if (pairs[i].t <= targetTemp && targetTemp <= pairs[i + 1].t) {
            const frac = (targetTemp - pairs[i].t) / (pairs[i + 1].t - pairs[i].t);
            const p = pairs[i].p + frac * (pairs[i + 1].p - pairs[i].p);
            return Math.floor(p * 10) / 10;
        }
    }
    return pairs[pairs.length - 1].p;
}

function renderPTTable(pms, designTemp) {
    const temps = pms.pressure_temperature.temperatures;
    const press = pms.pressure_temperature.pressures;
    const labels = pms.pressure_temperature.temp_labels || temps.map(String);
    const n = Math.min(temps.length, press.length);

    let highlightIdx = -1;
    let minDiff = Infinity;
    for (let i = 0; i < n; i++) {
        const d = Math.abs(temps[i] - designTemp);
        if (d < minDiff) { minDiff = d; highlightIdx = i; }
    }

    const htVal = pms.hydrotest_pressure || '';

    let html = '<thead><tr><th></th>';
    for (let i = 0; i < n; i++) {
        const cls = i === highlightIdx ? ' class="col-highlight-header"' : '';
        html += `<th${cls}>${labels[i] || temps[i]}</th>`;
    }
    if (htVal) html += '<th>Hydrotest Pr. (barg)</th>';
    html += '</tr></thead><tbody>';

    html += '<tr><td><strong>Press., barg</strong></td>';
    for (let i = 0; i < n; i++) {
        const cls = i === highlightIdx ? ' class="col-highlight"' : '';
        html += `<td${cls}>${press[i]}</td>`;
    }
    if (htVal) html += `<td rowspan="2" style="vertical-align:middle;font-weight:700;font-size:1.1em">${htVal}</td>`;
    html += '</tr>';

    html += '<tr><td><strong>Temp., \u00b0C</strong></td>';
    for (let i = 0; i < n; i++) {
        const cls = i === highlightIdx ? ' class="col-highlight"' : '';
        html += `<td${cls}>${labels[i] || temps[i]}</td>`;
    }
    html += '</tr></tbody>';

    document.getElementById('ptTable').innerHTML = html;
}

// ============================================================
// === TAB 2: Schedule & Wall Thickness
// ============================================================
// === ASME B31.3 Table A-1: Allowable Stress S(T) by material family (psi) ===
// Tables loaded from backend via ENG.stress_tables — single source of truth.
//
// `materialSpec` (optional, e.g. "API 5L Gr, X60 PSL-2", "ASTM A 312 TP 316L")
// — the actual pipe MOC assigned by the AI at the row level. When supplied,
// it's checked FIRST because it carries the real stress identity. The class-
// level `material` ("CS NACE") only labels the §5.5 family and would yield
// the wrong S for high-rating classes (F1/G1 1500-2500#) where regular CS
// can't withstand the cold-end allowable pressure and the AI substitutes
// API 5L X60 PSL-2 (S=25 ksi vs A106-B's 20 ksi at 38°C).
function getAllowableStress(material, tempC, materialSpec) {
    const mat = (material || '').toUpperCase();
    const spec = (materialSpec || '').toUpperCase();
    const tables = ENG.stress_tables;
    let table = tables.CS;  // default

    // ── 1. Spec-driven detection (highest priority — overrides class material) ──
    // Mirrors backend `_detect_stress_table` logic in engineering_constants.py.
    if (/API\s*5L.*X\s*?60|X60\s*PSL|\bX60\b/.test(spec)) {
        table = tables.API5LX60;
    } else if (/SDSS|S32750|SUPER\s*DUPLEX/.test(spec)) {
        table = tables.SDSS;
    } else if (/S31803|S32205|\bDUPLEX\b/.test(spec)) {
        table = tables.DSS;
    } else if (/TP\s*316L|\b316L\b/.test(spec)) {
        table = tables.SS316L;
    } else if (/TP\s*304L|\b304L\b/.test(spec)) {
        table = tables.SS304L;
    } else if (/TP\s*316\b/.test(spec)) {
        table = tables.SS316;
    }
    // ── 2. Class-material fallback (when spec didn't match anything specific) ──
    else if (mat.includes('SDSS') || mat.includes('S32750') || mat.includes('SUPER DUPLEX')) {
        table = tables.SDSS;
    } else if (mat.includes('DSS') || mat.includes('S31803') || mat.includes('DUPLEX')) {
        table = tables.DSS;
    } else if (mat.includes('316L')) {
        table = tables.SS316L;
    } else if (mat.includes('304L')) {
        table = tables.SS304L;
    } else if (mat.includes('SS') || mat.includes('STAINLESS')) {
        table = tables.SS316L;  // default SS
    } else if (mat.includes('CUNI') || mat.includes('CU-NI') || mat.includes('COPPER') || mat.includes('C70600')) {
        table = tables.CUNI;
    } else if (mat.includes('GALV')) {
        table = tables.CS;
    }
    // CS, LTCS, NACE variants all use CS table (NACE is just a service condition, same material)

    // Interpolate S(T) at the given temperature
    const temps = Object.keys(table).map(Number).sort((a, b) => a - b);
    const T = tempC;
    if (T <= temps[0]) return { S_psi: table[temps[0]], S_mpa: +(table[temps[0]] * 0.00689476).toFixed(1) };
    if (T >= temps[temps.length - 1]) return { S_psi: table[temps[temps.length - 1]], S_mpa: +(table[temps[temps.length - 1]] * 0.00689476).toFixed(1) };
    for (let i = 0; i < temps.length - 1; i++) {
        if (T >= temps[i] && T <= temps[i + 1]) {
            const frac = (T - temps[i]) / (temps[i + 1] - temps[i]);
            const S = table[temps[i]] + frac * (table[temps[i + 1]] - table[temps[i]]);
            const S_rounded = Math.round(S / 100) * 100;  // round to nearest 100 psi
            return { S_psi: S_rounded, S_mpa: +(S_rounded * 0.00689476).toFixed(1) };
        }
    }
    return { S_psi: 20000, S_mpa: 137.9 };  // fallback
}

// === Schedule & Wall Thickness tab ===
// Renders the Wall Thickness Calculation Table for the current PMS.
// Computes per-NPS:
//   • t        — pressure thickness from ASME B31.3 §304.1.2 Eq. 3a
//   • D/6      — applicability cap for the thin-wall equation
//   • t < D/6  — pass/fail check
//   • t_m      — t + corrosion allowance
//   • Calc Thk T = t_m / (1 − mill_tolerance)
//   • SCH / Sel Thk — smallest standard B36.10M schedule whose nominal
//     wall ≥ Calc Thk T. Schedule data comes from
//     `ENG.asme_wall_thicknesses_mm` (loaded from pipe_dimensions.json
//     via /api/engineering-constants).
//
// Picks the schedule per ASME B36.10M-2018 Table 2-1. When two schedules
// tie on wall thickness (e.g. STD = 40 = 2.77 mm at NPS 0.5″), prefer the
// alias listed earlier in the JSON's per-NPS object (STD over 40, XS over
// 80, XXS where defined) — matches typical project spec convention.
function _selectScheduleForThickness(nps, t_calc_mm) {
    const table = ENG.asme_wall_thicknesses_mm || {};
    const row = table[String(nps)];
    if (!row || !(t_calc_mm > 0)) return null;

    const entries = Object.entries(row).sort((a, b) => a[1] - b[1]);
    for (const [schedKey, wt] of entries) {
        if (wt + 1e-6 >= t_calc_mm) {
            return { schedule: _formatScheduleLabel(schedKey), wt };
        }
    }
    const [k, w] = entries[entries.length - 1];
    return { schedule: _formatScheduleLabel(k), wt: w };
}

// Project-conventional schedule floor for (class, NPS), sourced from
// `ENG.project_schedule_floors` (mirrors app/data/standards/project_schedule_floors.json).
// Returns the schedule key (e.g. '160', 'STD', '80S'), or null when no rule
// applies — meaning the row falls back to Eq. 3a alone with no floor.
function _projectFloorScheduleKey(classCode, nps) {
    if (!classCode) return null;
    const rules = (ENG.project_schedule_floors || {})[classCode.toUpperCase()];
    if (!rules) return null;
    const npsF = parseFloat(nps);
    if (!isFinite(npsF)) return null;
    for (const r of rules) {
        if ((r.from - 1e-6) <= npsF && npsF <= (r.to + 1e-6)) {
            return r.schedule;  // may be null — explicit "calc-only" entry
        }
    }
    return null;
}

// Resolve a floor schedule key to its numeric WT in the B36.10M / B36.19M
// wall-thickness table. Returns 0 if the key isn't found (e.g. 80S is
// B36.19M but the WT data here is B36.10M).
function _projectFloorWt(nps, schedKey) {
    if (!schedKey) return 0;
    const row = (ENG.asme_wall_thicknesses_mm || {})[String(nps)];
    if (!row) return 0;
    return Number(row[schedKey] || 0);
}

function _formatScheduleLabel(schedKey) {
    if (schedKey === 'STD' || schedKey === 'XS' || schedKey === 'XXS') return schedKey;
    return `SCH ${schedKey}`;
}

function renderScheduleTab(pms) {
    const target = document.getElementById('enhancedPipeTable');
    if (!target) return;

    const dpVal = parseFloat(document.getElementById('designPressure').value) || 0;
    const dtVal = parseFloat(document.getElementById('designTemperature').value) || 0;
    const E = ENG.joint_efficiency_E ?? 1.0;
    const W = ENG.weld_strength_W ?? 1.0;
    const Y = ENG.y_coefficient ?? 0.4;
    const millTolPct = parseFloat(pms.mill_tolerance) || ENG.mill_tolerance_percent || 12.5;
    const millFrac = millTolPct / 100;

    const caStr = pms.corrosion_allowance || '0';
    const caMM = caStr.toUpperCase().includes('NIL') ? 0 : (parseFloat(caStr) || 0);

    // ─────────────────────────────────────────────────────────────────────
    // Dual-case ASME B31.3 §304.1.2 wall thickness check
    // ─────────────────────────────────────────────────────────────────────
    // Pipe wall must contain BOTH:
    //   • Case 1 (Min T / Max P) — the rating's allowable pressure at the
    //     coldest temperature in the B16.5 P-T curve. This is the worst-case
    //     overpressure during cold-startup or pressure-spike scenarios. S is
    //     evaluated at the same low temperature (where it's largest).
    //   • Case 2 (Design Point) — the user's design pressure at design temp.
    //     S is evaluated at design temp (smaller than at cold end).
    //
    // Whichever case demands more wall, governs. Same governing case applies
    // to every NPS because t_press scales linearly with D — the case picked
    // here propagates to every row in the WT table.
    // Per-class actual pipe MOC — sourced from the AI-assigned material_spec
    // on the first pipe row. Per the AI prompt's "single unified MOC" rule
    // (ai_service.py §PIPE TYPE TRANSITION), every row in a given class
    // shares the same material_spec, so pipe_data[0] is representative. This
    // is the spec we feed into getAllowableStress so high-rating CS classes
    // (F1/G1 1500-2500#) correctly resolve to the API 5L X60 stress curve
    // rather than vanilla CS A106-B.
    const projectMaterialSpec = pms.pipe_data?.[0]?.material_spec || '';

    const ptTemps = pms.pressure_temperature?.temperatures || [];
    const ptPress = pms.pressure_temperature?.pressures || [];
    let case1 = null;  // null if no P-T curve attached (e.g., AI-only path)
    if (ptTemps.length && ptPress.length && ptTemps.length === ptPress.length) {
        // Find the lowest-temp index — that's the rating-defining cold end.
        let idx = 0;
        for (let i = 1; i < ptTemps.length; i++) {
            if (ptTemps[i] < ptTemps[idx]) idx = i;
        }
        const t1 = ptTemps[idx];
        const p1 = ptPress[idx];
        const s1 = getAllowableStress(pms.material, t1, projectMaterialSpec);
        const labels = pms.pressure_temperature.temp_labels || [];
        case1 = {
            label: labels[idx] || `${t1}°C`,
            temp_c: t1,
            P_barg: p1,
            P_psig: parseFloat(barg2psig(p1)),
            P_mpa: p1 * 0.1,
            S_psi: s1.S_psi,
            S_mpa: s1.S_mpa,
        };
    }

    const s2 = getAllowableStress(pms.material, dtVal, projectMaterialSpec);
    const case2 = {
        label: `${dtVal}°C`,
        temp_c: dtVal,
        P_barg: dpVal,
        P_psig: parseFloat(barg2psig(dpVal)),
        P_mpa: dpVal * 0.1,
        S_psi: s2.S_psi,
        S_mpa: s2.S_mpa,
    };

    // Per-NPS coefficient k = P / (2(SEW + PY)) → t = k × D.
    // Compare k values to decide which case governs (D cancels out, so the
    // governing case is identical across all NPS).
    const k = (P_mpa, S_mpa) => P_mpa / (2 * (S_mpa * E * W + P_mpa * Y));
    const k1 = case1 ? k(case1.P_mpa, case1.S_mpa) : 0;
    const k2 = k(case2.P_mpa, case2.S_mpa);
    const case1Governs = case1 != null && k1 >= k2;
    const governing = case1Governs ? case1 : case2;
    const governingLabel = case1Governs ? 'Case 1 (Min T / Max P)' : 'Case 2 (Design Point)';

    // Stress used for per-row Eq. 3a — sourced from the governing case.
    const S_mpa = governing.S_mpa;
    const S_psi = governing.S_psi;

    const rows = (pms.pipe_data || []).map(p => {
        const nps = p.size_inch;
        const D = p.od_mm;
        if (!D) return null;

        // Compute t_press for both cases at this NPS, take the larger.
        const t_press_1 = case1 ? k1 * D : 0;
        const t_press_2 = k2 * D;
        const t = Math.max(t_press_1, t_press_2);
        const d_over_6 = D / 6;
        const t_applicable = t < d_over_6 ? 'OK' : 'ALERT';
        const t_m = t + caMM;
        const t_req = t_m / (1 - millFrac);

        // Apply project-conventional schedule floor for (class, NPS).
        // floor_wt is 0 when no rule applies — Eq. 3a alone wins.
        // When a rule applies, required_wt = MAX(eq3a, floor_wt) so the
        // class-conventional minimum schedule is honoured at low pressures
        // and Eq. 3a takes over once design pressure pushes above the floor.
        const floorKey = _projectFloorScheduleKey(pms.piping_class, nps);
        const floorWt = _projectFloorWt(nps, floorKey);
        const required_wt = Math.max(t_req, floorWt);

        const picked = _selectScheduleForThickness(nps, required_wt);
        const sel_sch = picked ? picked.schedule : (p.schedule || '-');
        const sel_thk = picked ? picked.wt : (p.wall_thickness_mm || 0);
        const sel_status = (picked && picked.wt + 0.001 >= required_wt) ? 'OK' : 'SUBSTD';
        const applColor = t_applicable === 'OK' ? '#16a34a' : '#b91c1c';
        const selColor = sel_status === 'OK' ? '#16a34a' : '#b91c1c';
        const floorDriven = floorWt > t_req + 1e-6;

        return { nps, D, t, t_press_1, t_press_2, d_over_6, t_applicable, t_m, t_req,
                 floorKey, floorWt, required_wt, floorDriven,
                 sel_sch, sel_thk, sel_status, applColor, selColor };
    }).filter(r => r !== null);

    if (rows.length === 0) {
        target.innerHTML = '<p style="color:var(--text-muted);padding:12px">No pipe data available.</p>';
        return;
    }

    let html = `<table><thead><tr>
        <th>NPS</th>
        <th>D<br>(mm)</th>
        <th>t<br>(mm)</th>
        <th>D/6<br>(mm)</th>
        <th>If<br>t&lt;D/6</th>
        <th>t<sub>m</sub><br>(mm)</th>
        <th>Mill<br>Tol.</th>
        <th>Calc. Thk<br>T (mm)</th>
        <th>SCH</th>
        <th>Sel. Thk<br>(mm)</th>
        <th>Sel. Thk<br>Status</th>
    </tr></thead><tbody>`;

    rows.forEach(r => {
        // SUBSTD rows: every standard B36.10M schedule for this NPS is too
        // thin to meet Eq. 3a / project-floor requirement. Report the row
        // honestly — blank SCH (no buyable schedule satisfies it) and
        // Sel. Thk = the calculated minimum (rounded), so the engineer
        // sees the wall they actually need to procure (custom-machined or
        // via material-spec upgrade).
        const isSubstd = r.sel_status === 'SUBSTD';
        const schCell  = isSubstd ? '—' : r.sel_sch;
        const thkCell  = isSubstd ? r.t_req.toFixed(2) : r.sel_thk;
        html += `<tr>
            <td><strong>${r.nps}"</strong></td>
            <td>${r.D}</td>
            <td>${r.t.toFixed(3)}</td>
            <td>${r.d_over_6.toFixed(2)}</td>
            <td style="color:${r.applColor};font-weight:600">${r.t_applicable}</td>
            <td>${r.t_m.toFixed(3)}</td>
            <td>${millTolPct}%</td>
            <td><strong>${r.t_req.toFixed(3)}</strong></td>
            <td><strong>${schCell}</strong></td>
            <td>${thkCell}</td>
            <td style="color:${r.selColor};font-weight:600">${r.sel_status}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    target.innerHTML = html;

    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * (ENG.hydrotest_factor ?? 1.5));
    const summaryEl = document.getElementById('summaryStats');
    if (summaryEl) {
        setKVList('summaryStats', [
            { l: 'Hydrotest Pressure (1.5×P)', v: `<strong>${ht.toFixed(1)} barg</strong>`, bold: true },
            { l: 'Total NPS Sizes', v: `${rows.length}` },
        ]);
    }

    // ── Design Parameters panel — dual rows with GOVERNS flag ──
    // Each of {Pressure, Temperature, Allowable Stress S(T)} shows both
    // Case 1 (Min T / Max P from B16.5 cold-end) and Case 2 (Design Point
    // from form). The case that drives more wall thickness is tagged
    // [GOVERNS] (green); the other is tagged [active] (grey). When no P-T
    // curve is attached (AI-only path), Case 1 is omitted and only the
    // Design Point row renders.
    const materialSpec = pms.pipe_data?.[0]?.material_spec || pms.material || '—';

    const govTag = `<span style="color:#16a34a;font-weight:700">[GOVERNS]</span>`;
    const actTag = `<span style="color:#6b7280;font-style:italic">[active]</span>`;
    const dualLine = (label, val, unit, tag) =>
        `<div><strong>${label}: ${val}</strong> <span class="unit">${unit}</span> ${tag}</div>`;

    const designParams = [
        { l: 'PMS Class', v: `<strong>${pms.piping_class}</strong> (${pms.rating})` },
    ];

    if (case1) {
        // Round to clean spec values: psig → integer, barg → 1 decimal.
        // Standard piping-spec convention; avoids the false-precision look
        // of "6171.4 psig (425.50 barg)" — engineers read the integer.
        const fmtPress = (psig, barg) =>
            `${Math.round(psig).toLocaleString()} psig (${barg.toFixed(1)} barg)`;
        const pressureBlock = [
            dualLine('Min T / Max P', fmtPress(case1.P_psig, case1.P_barg),
                     `@ ${case1.label}`, case1Governs ? govTag : actTag),
            dualLine('Design Point', fmtPress(case2.P_psig, case2.P_barg),
                     `@ ${case2.label}`, case1Governs ? actTag : govTag),
            `<div class="unit" style="font-size:0.85em;margin-top:4px">t<sub>REQ</sub> uses MAX(Case 1, Case 2) per size</div>`,
        ].join('');
        designParams.push({ l: 'Design Pressure (P)', v: pressureBlock });

        const tempBlock = [
            dualLine('Min', `${case1.label} (${c2f(case1.temp_c)}°F)`,
                     '[P-T min]', case1Governs ? govTag : actTag),
            dualLine('Max (Design)', `${case2.temp_c}°C (${c2f(case2.temp_c)}°F)`,
                     '[design]', case1Governs ? actTag : govTag),
        ].join('');
        designParams.push({ l: 'Design Temperature', v: tempBlock });
    } else {
        designParams.push({
            l: 'Design Pressure (P)',
            v: `<strong>${dpVal} barg</strong> <span class="unit">(${barg2psig(dpVal)} psig)</span>`,
        });
        designParams.push({
            l: 'Design Temperature',
            v: `<strong>${dtVal}°C</strong> <span class="unit">(${c2f(dtVal)}°F)</span>`,
        });
    }

    designParams.push({ l: 'Material', v: pms.material || '—' });
    designParams.push({ l: 'Material Spec', v: materialSpec });

    if (case1) {
        const matFamily = (pms.material || '').toUpperCase().includes('SS') ? 'SS'
                        : (pms.material || '').toUpperCase().includes('DSS') ? 'DSS' : 'CS';
        const stressBlock = [
            dualLine(`S @ ${case1.label}`, `${case1.S_psi.toLocaleString()} psi`,
                     `(${case1.S_mpa} MPa)`, case1Governs ? govTag : actTag),
            dualLine(`S @ ${case2.temp_c}°C`, `${case2.S_psi.toLocaleString()} psi`,
                     `(${case2.S_mpa} MPa)`, case1Governs ? actTag : govTag),
            `<div class="unit" style="font-size:0.85em;margin-top:4px">per ASME B31.3 Table A-1 [${matFamily}]</div>`,
        ].join('');
        designParams.push({ l: 'Allowable Stress S(T)', v: stressBlock });
    } else {
        designParams.push({
            l: 'Allowable Stress S(T)',
            v: `<strong>${S_psi.toLocaleString()} psi</strong> <span class="unit">(${S_mpa} MPa) — ASME B31.3 Table A-1</span>`,
        });
    }

    setKVList('designParamsList', designParams);

    // ── Formula example: pick a representative NPS and show the dual-case calc ──
    // Prefer NPS 6" if present (canonical engineering example), otherwise the
    // largest NPS in the spec (worst case scales with D so big NPS shows the
    // full effect).
    const exampleEl = document.getElementById('formulaExample');
    if (exampleEl && rows.length > 0) {
        let exRow = rows.find(r => parseFloat(r.nps) === 6);
        if (!exRow) {
            exRow = rows.reduce((a, b) => parseFloat(b.nps) > parseFloat(a.nps) ? b : a, rows[0]);
        }
        const odIn = (exRow.D / 25.4).toFixed(3);
        const caIn = (caMM / 25.4).toFixed(4);
        const yNote = ['CS', 'LTCS'].some(s => (pms.material || '').toUpperCase().includes(s))
                    ? 'ferritic/alloy steel' : 'austenitic / non-ferrous';

        const t1_mm = exRow.t_press_1;
        const t2_mm = exRow.t_press_2;
        const t1_in = (t1_mm / 25.4).toFixed(4);
        const t2_in = (t2_mm / 25.4).toFixed(4);
        const t_in  = (exRow.t / 25.4).toFixed(4);
        const tm_in = (exRow.t_m / 25.4).toFixed(4);
        const treq_in = (exRow.t_req / 25.4).toFixed(4);
        const treq_mm = exRow.t_req.toFixed(2);

        let html = `<strong>NPS ${exRow.nps}" example:</strong> OD = ${odIn}" `
                 + ` | E = ${E} | W = ${W} | Y = ${Y} <span class="unit">[${yNote}]</span> `
                 + ` | c = ${caIn}" (${caMM} mm) | mill tol = ${millTolPct}%<br>`;
        // Round psig to integer for clean spec-style display, matching the
        // Design Parameters panel above.
        const p1Int = case1 ? Math.round(case1.P_psig).toLocaleString() : '';
        const p2Int = Math.round(case2.P_psig).toLocaleString();
        if (case1) {
            html += `<strong>Case 1 (Min T / Max P @ ${case1.label}):</strong> `
                  + `P = ${p1Int} psig, S = ${case1.S_psi.toLocaleString()} psi → `
                  + `t<sub>press</sub> = ${t1_in}"`
                  + (case1Governs ? ` <span style="color:#16a34a;font-weight:700">— GOVERNS</span>` : ``)
                  + `<br>`;
            html += `<strong>Case 2 (Design Point @ ${case2.temp_c}°C):</strong> `
                  + `P = ${p2Int} psig, S = ${case2.S_psi.toLocaleString()} psi → `
                  + `t<sub>press</sub> = ${t2_in}"`
                  + (!case1Governs ? ` <span style="color:#16a34a;font-weight:700">— GOVERNS</span>` : ``)
                  + `<br>`;
            html += `<span style="color:#b91c1c;font-weight:600">`
                  + `Using ${case1Governs ? 'Case 1 (Min T / Max P)' : 'Case 2 (Design Point)'}: `
                  + `t = ${t_in}" → t<sub>m</sub> = t+c = ${tm_in}" → `
                  + `T<sub>REQ</sub> = t<sub>m</sub>/(1−${(millFrac).toFixed(3)}) = ${treq_in}" `
                  + `(${treq_mm} mm)</span>`;
        } else {
            html += `<strong>Single-case (Design Point @ ${case2.temp_c}°C):</strong> `
                  + `P = ${p2Int} psig, S = ${case2.S_psi.toLocaleString()} psi → `
                  + `t = ${t_in}" → t<sub>m</sub> = ${tm_in}" → T<sub>REQ</sub> = ${treq_in}" (${treq_mm} mm)`;
        }
        exampleEl.innerHTML = html;
    }

    // ── Service tag (above the two-col panels) ──
    // Inline styles instead of a CSS class — the project CSS doesn't define
    // .service-tag, and falling through to default styling rendered the
    // text invisible against the page background. Use the same dark navy
    // pill as the table headers so it reads at a glance.
    const serviceTagsEl = document.getElementById('serviceTags');
    if (serviceTagsEl) {
        const svc = pms.service || 'General';
        serviceTagsEl.innerHTML =
            `<span style="color:var(--text-muted);font-weight:500;margin-right:10px">Service:</span>`
          + `<span style="display:inline-block;padding:4px 14px;background:#0f2a52;`
          +   `color:#fff;font-weight:600;border-radius:4px;font-size:0.9em">${svc}</span>`;
    }

    // ── Fabrication & Code Factors panel ──
    // Y coefficient and W factor are temperature-dependent per ASME B31.3
    // Tables 304.1.1 / 302.3.5. Here we annotate which temperature was used —
    // that's the design temp for both factors at the project's operating
    // conditions (the cold-end Case 1 only affects S and P, not Y or W).
    const matUpper = (pms.material || '').toUpperCase();
    const isSS = matUpper.includes('SS') || matUpper.includes('STAINLESS')
              || matUpper.includes('DSS') || matUpper.includes('SDSS');
    const pipeStandard = isSS ? 'ASME B36.19M' : 'ASME B36.10M';
    const jointType = document.getElementById('jointType')?.value || 'Seamless';
    const yFamily = isSS ? 'austenitic / non-ferrous' : 'ferritic/alloy steel';
    const dt_F = c2f(dtVal);
    setKVList('codeFactorsList', [
        { l: 'Pipe Standard', v: pipeStandard, bold: true },
        { l: 'Joint Type', v: jointType, bold: true },
        { l: 'Joint Efficiency (E)', v: E.toString() },
        { l: 'Y Coefficient', v: `${Y} <span class="unit">(ASME B31.3 Table 304.1.1 @ ${dt_F}°F (${yFamily}))</span>` },
        { l: 'W-factor (Weld Str.)', v: `${W} <span class="unit">(ASME B31.3 Table 302.3.5 @ ${dt_F}°F (W=${W}))</span>` },
        { l: 'Corrosion Allow. (c)', v: caMM > 0 ? `${caMM} mm` : '<strong>NIL</strong> (no corrosion allowance)', bold: true },
        { l: 'Mill Undertolerance', v: `${millTolPct}%` },
    ]);

    // ── Engineering Requirements & Flags ──
    // Derived client-side from the class code + material — no backend call.
    const cls = pms.piping_class || '';
    const isNACE = cls.includes('N') || matUpper.includes('NACE');
    const isLTCS = cls.includes('L') || matUpper.includes('LTCS');
    const isHighRating = ['1500#', '2500#', '5000#', '10000#'].includes(pms.rating);
    const isGalv = matUpper.includes('GALV');
    const isCoated = matUpper.includes('EPOXY') || matUpper.includes('COATED');

    const flags = [];
    if (isNACE) flags.push({
        level: 'mandatory', badge: 'NACE',
        title: 'Sour-Service Requirements (NACE MR-01-75 / ISO 15156)',
        body: 'Material hardness limits, heat-treatment certification, and HIC/SSC qualification required. Maximum service temperature 250°C per project policy.',
    });
    if (isLTCS) flags.push({
        level: 'mandatory', badge: 'LTCS',
        title: 'Low-Temperature Service',
        body: 'Charpy V-notch impact testing per ASTM A350 LF2 / A333 Gr.6. Verify minimum design metal temperature (MDMT) is within material allowable range.',
    });
    if (isHighRating) flags.push({
        level: 'warning', badge: 'HP',
        title: 'High-Pressure Rating (' + pms.rating + ')',
        body: 'Hydrotest pressure exceeds 250 barg in many configurations. Verify test fixture and gauge ranges. Welder qualification per ASME IX required.',
    });
    if (isGalv) flags.push({
        level: 'warning', badge: 'GALV',
        title: 'Galvanized Coating Temperature Limit',
        body: 'Hot-dip galvanizing degrades above ~150°C. Confirm operating temperature does not exceed coating limit. Consider field-applied repair where galvanizing is damaged at fittings/welds.',
    });
    if (isCoated) flags.push({
        level: 'note', badge: 'COATED',
        title: 'Internal Coating Notice',
        body: 'Internal epoxy / lining requires holiday testing per project spec. Damaged coating at field welds must be repaired per coating manufacturer instructions.',
    });

    const flagsEl = document.getElementById('engineeringFlags');
    if (flagsEl) {
        if (flags.length === 0) {
            flagsEl.innerHTML = '<p style="color:var(--text-muted);padding:12px">No special engineering flags for this specification.</p>';
        } else {
            flagsEl.innerHTML = flags.map(f => `
                <div class="flag-card ${f.level}">
                    <div class="flag-header">
                        <span class="flag-badge ${f.level}">${f.badge}</span>
                        <span class="flag-title">${f.title}</span>
                    </div>
                    <p class="flag-body">${f.body}</p>
                </div>`).join('');
        }
    }

    // ── Tag Legend (pipe-row colour codes used in the WT table) ──
    const legendItems = [];
    if (rows.some(r => r.sel_status === 'SUBSTD')) legendItems.push({
        tag: 'pressure', label: 'SUBSTD', desc: 'Selected schedule does not meet calculated minimum — upgrade required',
    });
    if (isNACE) legendItems.push({ tag: 'nace', label: 'NACE', desc: 'NACE MR-01-75 / ISO 15156 sour-service requirements' });
    if (isLTCS) legendItems.push({ tag: 'ltcs', label: 'LTCS', desc: 'Low-temperature service per ASTM A350 LF2 / A333 Gr.6' });

    const legendEl = document.getElementById('tagLegend');
    if (legendEl) {
        if (legendItems.length === 0) {
            legendEl.innerHTML = '<p style="color:var(--text-muted);padding:8px;font-size:0.9em">No row-level tags for this class.</p>';
        } else {
            legendEl.innerHTML = legendItems.map(item =>
                `<div class="legend-row"><span class="pipe-tag ${item.tag}">${item.label}</span> <span class="legend-desc">${item.desc}</span></div>`
            ).join('');
        }
    }
}

// ============================================================
// === TAB 3: Pipe & Fittings Material Assignment
// ============================================================
function renderPipeFittingsTab(pms) {
    const pipes = pms.pipe_data;
    const fittings = pms.fittings;
    const fittingsW = pms.fittings_welded;

    // Split into small bore (≤ 2") and large bore (> 2")
    const smallBore = pipes.filter(p => parseFloat(p.size_inch) <= ENG.small_bore_cutoff_nps);
    const largeBore = pipes.filter(p => parseFloat(p.size_inch) > ENG.small_bore_cutoff_nps);

    const smallSchedule = smallBore.length > 0 ? smallBore[0].schedule : (pipes.length > 0 ? pipes[0].schedule : 'STD');
    const largeSchedule = largeBore.length > 0 ? largeBore[0].schedule : smallSchedule;

    const connType = fittings.fitting_type || 'Butt Weld (BW)';

    // Build component rows
    function buildComponentRows(isSmallBore, schedule) {
        const pipeSubset = isSmallBore ? smallBore : largeBore;
        const pipeSpec = pipeSubset.length > 0 ? pipeSubset[0].material_spec : (pipes.length > 0 ? pipes[0].material_spec : '\u2014');

        // Determine which fittings set to use based on bore size
        // Small bore: use fittings (screwed), Large bore: use fittings_welded (butt weld) if available
        const fit = (!isSmallBore && fittingsW) ? fittingsW : fittings;
        const fitMat = fit.material_spec || fittings.material_spec || '';

        const rows = [
            { component: 'Pipe', material: pipeSpec, schedClass: schedule, standard: 'ASTM' },
        ];

        if (fit.elbow_standard || fittings.elbow_standard) {
            rows.push({ component: '90\u00b0 LR Elbow', material: fitMat, schedClass: `Sch ${schedule} / XS`, standard: fit.elbow_standard || fittings.elbow_standard });
            rows.push({ component: '45\u00b0 Elbow', material: fitMat, schedClass: `Sch ${schedule} / XS`, standard: fit.elbow_standard || fittings.elbow_standard });
        }
        if (fit.tee_standard || fittings.tee_standard) {
            rows.push({ component: 'Equal Tee', material: fitMat, schedClass: `Sch ${schedule} / XS`, standard: fit.tee_standard || fittings.tee_standard });
            rows.push({ component: 'Reducing Tee', material: fitMat, schedClass: `Sch ${schedule} / XS`, standard: fit.tee_standard || fittings.tee_standard });
        }
        if (fit.reducer_standard || fittings.reducer_standard) {
            rows.push({ component: 'Concentric Reducer', material: fitMat, schedClass: `Sch ${schedule} / XS`, standard: fit.reducer_standard || fittings.reducer_standard });
            rows.push({ component: 'Eccentric Reducer', material: fitMat, schedClass: `Sch ${schedule} / XS`, standard: fit.reducer_standard || fittings.reducer_standard });
        }
        if (fit.cap_standard || fittings.cap_standard) {
            rows.push({ component: 'Pipe Cap', material: fitMat, schedClass: fit.cap_standard || fittings.cap_standard, standard: fit.cap_standard || fittings.cap_standard });
        }
        if (fittings.plug_standard) rows.push({ component: 'Plug', material: fitMat || 'N/A', schedClass: '', standard: fittings.plug_standard });
        if (fittings.weldolet_spec) rows.push({ component: 'Weldolet', material: fittings.weldolet_spec, schedClass: '', standard: 'MSS SP-97' });

        // Extra fittings: coupling, hex plug, union, olet, swage
        const ef = pms.extra_fittings || {};
        if (ef.coupling) rows.push({ component: 'Coupling', material: fitMat || 'N/A', schedClass: '', standard: ef.coupling });
        if (ef.hex_plug) rows.push({ component: 'Hex Head Plug', material: fitMat || 'N/A', schedClass: '', standard: ef.hex_plug });
        if (ef.union || ef.union_large) {
            const unionStd = isSmallBore ? (ef.union || ef.union_large) : (ef.union_large || ef.union);
            rows.push({ component: 'Union', material: fitMat, schedClass: '', standard: unionStd });
        }
        if (ef.olet || ef.olet_large) {
            const oletSpec = isSmallBore ? (ef.olet || ef.olet_large) : (ef.olet_large || ef.olet);
            rows.push({ component: 'Olet', material: oletSpec, schedClass: '', standard: ef.olet ? 'MSS SP-97' : '' });
        }
        if (ef.swage) rows.push({ component: 'Swage', material: ef.swage, schedClass: '', standard: 'MSS SP-95' });

        return rows;
    }

    function renderBoreSection(title, subtitle, schedule, isSmallBore) {
        const rows = buildComponentRows(isSmallBore, schedule);
        const fit = (!isSmallBore && fittingsW) ? fittingsW : fittings;
        let html = `
            <div class="bore-section">
                <h3 class="bore-header">${title} <span class="bore-range">${subtitle}</span></h3>
                <div class="bore-info-bar">
                    <strong>Connection:</strong> ${fit.fitting_type || connType} &nbsp;|&nbsp; <strong>Schedule:</strong> ${schedule}
                </div>
                <div class="card">
                    <table class="component-table">
                        <thead><tr>
                            <th>Component</th>
                            <th>Material</th>
                            <th>Schedule/Class</th>
                            <th>Standard</th>
                        </tr></thead>
                        <tbody>`;
        rows.forEach(r => {
            html += `<tr>
                <td><strong>${r.component}</strong></td>
                <td>${r.material || '\u2014'}</td>
                <td>${r.schedClass || '\u2014'}</td>
                <td>${r.standard || '\u2014'}</td>
            </tr>`;
        });
        html += '</tbody></table></div></div>';
        return html;
    }

    document.getElementById('smallBoreSection').innerHTML =
        renderBoreSection('Small Bore', '(NPS \u00bd" \u2013 2")', smallSchedule, true);
    document.getElementById('largeBoreSection').innerHTML =
        renderBoreSection('Large Bore', '(NPS 2\u00bd" \u2013 36")', largeSchedule, false);

    // Branch Chart
    const bc = pms.branch_chart;
    if (bc) {
        document.getElementById('branchChartSection').innerHTML = `
            <div class="card" style="margin-top:16px">
                <div class="card-title-underline">Branch Connection Chart</div>
                <p style="font-size:0.85rem;color:var(--text-secondary)">${bc}</p>
            </div>`;
    } else {
        document.getElementById('branchChartSection').innerHTML = '';
    }
}

// ============================================================
// === TAB 4: Components & Notes
// ============================================================
function renderComponentsTab(pms) {
    setKVList('flangeList', [
        { l: 'MOC', v: pms.flange.material_spec },
        { l: 'Face', v: pms.flange.face_type },
        { l: 'Type', v: pms.flange.flange_type },
    ]);

    setKVList('bngList', [
        { l: 'Stud Bolts', v: pms.bolts_nuts_gaskets.stud_bolts },
        { l: 'Hex Nuts', v: pms.bolts_nuts_gaskets.hex_nuts },
        { l: 'Gasket', v: pms.bolts_nuts_gaskets.gasket },
    ]);

    const valveItems = [
        { l: 'Rating', v: pms.valves.rating },
    ];
    // Tubing classes have different valve types (DBB, Needle, Ball Inst, Check Inst)
    if (pms.class_type === 'tubing') {
        if (pms.valves.dbb) valveItems.push({ l: 'DBB (Inst)', v: pms.valves.dbb });
        if (pms.valves.needle) valveItems.push({ l: 'Needle (Inst)', v: pms.valves.needle });
        if (pms.valves.ball) valveItems.push({ l: 'Ball (Inst)', v: pms.valves.ball });
        if (pms.valves.check) valveItems.push({ l: 'Check (Inst)', v: pms.valves.check });
    } else {
        valveItems.push({ l: 'Ball', v: pms.valves.ball });
        valveItems.push({ l: 'Gate', v: pms.valves.gate });
        valveItems.push({ l: 'Globe', v: pms.valves.globe });
        valveItems.push({ l: 'Check', v: pms.valves.check });
        if (pms.valves.butterfly) {
            valveItems.push({ l: 'Butterfly', v: pms.valves.butterfly });
        }
        if (pms.valves.dbb_inst) {
            valveItems.push({ l: 'DBB (Inst)', v: pms.valves.dbb_inst });
        }
        if (pms.valves.dbb) {
            valveItems.push({ l: 'DBB', v: pms.valves.dbb });
        }
    }
    setKVList('valvesList', valveItems);

    const spectacleItems = [
        { l: 'MOC', v: pms.spectacle_blind.material_spec },
        { l: 'Standard', v: pms.spectacle_blind.standard },
    ];
    if (pms.spectacle_blind.standard_large) {
        spectacleItems.push({ l: 'Standard (Large)', v: pms.spectacle_blind.standard_large });
    }
    setKVList('spectacleList', spectacleItems);

    if (pms.notes && pms.notes.length) {
        document.getElementById('notesCard').style.display = '';
        document.getElementById('notesList').innerHTML = pms.notes.map(n => `<p>${n}</p>`).join('');
    } else {
        document.getElementById('notesCard').style.display = 'none';
    }
}

// === KV List Helper ===
function setKVList(id, items) {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = items.map(item => {
        let valHtml;
        if (item.tag) valHtml = `<span class="kv-tag ${item.tag}">${item.v}</span>`;
        else if (item.bold) valHtml = `<span class="kv-value bold">${item.v || '\u2014'}</span>`;
        else valHtml = `<span class="kv-value">${item.v || '\u2014'}</span>`;
        return `<div class="kv-row${item.cls ? ' ' + item.cls : ''}"><span class="kv-label">${item.l}</span>${valHtml}</div>`;
    }).join('');
}

// === Download Excel ===
async function downloadExcel() {
    if (!currentPMS) { showToast('Generate PMS first', 'error'); return; }
    const data = {
        piping_class: currentPMS.piping_class,
        material: currentPMS.material,
        corrosion_allowance: currentPMS.corrosion_allowance,
        service: currentPMS.service || 'General',
    };
    showLoading('Generating Excel (AI processing, please wait)...');
    try {
        const res = await API.downloadExcel(data);
        if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || 'Download failed'); }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url;
        a.download = `PMS_${currentPMS.piping_class}_${currentPMS.rating.replace('#','').replace(' ','_')}.xlsx`;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
        showToast('Excel downloaded!', 'success');
    } catch (err) { showToast(err.message, 'error'); }
    finally { hideLoading(); }
}

// === Browse ===
async function loadBrowseData() {
    try {
        const res = await API.listClasses();
        if (!res.ok) return;
        const data = await res.json();
        renderBrowseTable(data);
        document.getElementById('browseSearch').addEventListener('input', e => {
            const q = e.target.value.toLowerCase();
            renderBrowseTable(data.filter(c =>
                c.piping_class.toLowerCase().includes(q) ||
                c.material.toLowerCase().includes(q) ||
                c.rating.toLowerCase().includes(q)
            ));
        });
    } catch {}
}

function renderBrowseTable(data) {
    document.getElementById('classTableBody').innerHTML = data.map(c => `
        <tr onclick="loadFromBrowse('${c.piping_class}','${c.material}','${c.corrosion_allowance}')">
            <td><strong>${c.piping_class}</strong></td>
            <td>${c.rating}</td>
            <td>${c.material}</td>
            <td>${c.corrosion_allowance}</td>
            <td><button class="btn btn-primary btn-sm">Load</button></td>
        </tr>`).join('');
}

function loadFromBrowse(cls, mat, ca) {
    document.querySelectorAll('.nav-tab').forEach(n => n.classList.remove('active'));
    document.querySelector('[data-tab="generate"]').classList.add('active');
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-generate').classList.add('active');

    // Find the rating for this class from indexData
    const entry = indexData.find(d => d.piping_class === cls);
    const rating = entry ? entry.rating : '';

    // Set rating dropdown (which is the pipingClass select)
    const ratingSelect = document.getElementById('pipingClass');
    ratingSelect.value = rating;
    ratingSelect.dispatchEvent(new Event('change'));

    setTimeout(() => {
        const materialSelect = document.getElementById('material');
        materialSelect.value = mat;
        materialSelect.dispatchEvent(new Event('change'));

        setTimeout(() => {
            const caSelect = document.getElementById('corrosionAllowance');
            caSelect.value = ca;
            caSelect.dispatchEvent(new Event('change'));
        }, 50);
    }, 50);

    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// === API Check ===
async function checkAPI() {
    const badge = document.getElementById('apiBadge');
    try { const res = await API.health(); if (res.ok) badge.classList.add('online'); } catch {}
}

// === UI Helpers ===
function showLoading(t) { document.getElementById('loadingText').textContent = t; document.getElementById('loadingOverlay').classList.add('active'); }
function hideLoading() { document.getElementById('loadingOverlay').classList.remove('active'); }
function showToast(msg, type = 'info') {
    const c = document.getElementById('toastContainer');
    const t = document.createElement('div'); t.className = `toast ${type}`; t.textContent = msg;
    c.appendChild(t); setTimeout(() => t.remove(), 4000);
}

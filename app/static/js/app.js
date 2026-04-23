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
    initDesignInputs();
    checkAPI();
    loadBrowseData();
    loadIndexData();
    loadEngineeringConstants();
});

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

function populateClassDropdown() {
    const sel = document.getElementById('pipingClass');
    sel.innerHTML = '<option value="">-- Select Rating --</option>';

    if (!indexData.length) {
        sel.innerHTML = '<option value="">-- No Data Available --</option>';
        return;
    }

    // Show unique ratings only (150#, 300#, 600#, etc.)
    const seen = new Set();
    indexData.forEach(d => {
        const rating = d.rating || '';
        if (!rating || seen.has(rating)) return;
        seen.add(rating);
        const opt = document.createElement('option');
        opt.value = rating;
        opt.textContent = rating;
        sel.appendChild(opt);
    });
}

function initCascadingDropdowns() {
    const ratingSelect = document.getElementById('pipingClass');  // Now shows ratings
    const materialSelect = document.getElementById('material');
    const caSelect = document.getElementById('corrosionAllowance');
    const serviceInput = document.getElementById('service');

    // Rating changed -> populate Material dropdown (filtered by rating)
    ratingSelect.addEventListener('change', () => {
        const rating = ratingSelect.value;
        materialSelect.innerHTML = '<option value="">-- Select Material --</option>';
        caSelect.innerHTML = '<option value="">-- Select CA --</option>';
        materialSelect.disabled = true;
        caSelect.disabled = true;
        if (!rating) return;

        // Find all materials for this rating
        const matches = indexData.filter(d => d.rating === rating);
        const materials = [...new Set(matches.map(d => d.material))];
        if (materials.length === 0) return;

        materialSelect.disabled = false;
        materials.forEach(mat => {
            const opt = document.createElement('option');
            opt.value = mat;
            opt.textContent = mat;
            materialSelect.appendChild(opt);
        });
    });

    // Material changed -> populate CA dropdown (filtered by rating + material)
    materialSelect.addEventListener('change', () => {
        const rating = ratingSelect.value;
        const mat = materialSelect.value;
        caSelect.innerHTML = '<option value="">-- Select CA --</option>';
        caSelect.disabled = true;
        if (!rating || !mat) return;

        const matches = indexData.filter(d => d.rating === rating && d.material === mat);
        const cas = [...new Set(matches.map(d => d.corrosion_allowance))];
        if (cas.length === 0) return;

        caSelect.disabled = false;
        cas.forEach(ca => {
            const opt = document.createElement('option');
            opt.value = ca;
            opt.textContent = ca;
            caSelect.appendChild(opt);
        });
    });
}

// Resolve piping class from rating + material + CA
function resolvePipingClass(rating, material, ca) {
    const match = indexData.find(d => d.rating === rating && d.material === material && d.corrosion_allowance === ca);
    return match ? match.piping_class : null;
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

    const resolvedClass = resolvePipingClass(selectedRating, selectedMaterial, selectedCA);
    if (!resolvedClass) { showToast('No matching piping class found for this combination', 'error'); return; }

    const data = {
        piping_class: resolvedClass,
        material: selectedMaterial,
        corrosion_allowance: selectedCA,
        service: selectedService || 'General',
    };

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
// Tables loaded from backend via ENG.stress_tables — single source of truth
function getAllowableStress(material, tempC) {
    const mat = material.toUpperCase();
    const tables = ENG.stress_tables;

    // Determine which table to use
    let table = tables.CS;  // default
    if (mat.includes('SDSS') || mat.includes('S32750') || mat.includes('SUPER DUPLEX')) {
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

function renderScheduleTab(pms) {
    const dpVal = parseFloat(document.getElementById('designPressure').value) || 0;
    const dtVal = parseFloat(document.getElementById('designTemperature').value) || 0;
    const E = ENG.joint_efficiency_E;
    const W = ENG.weld_strength_W;
    const Y = ENG.y_coefficient;
    // Material-specific allowable stress from ASME B31.3 Table A-1
    const stressData = getAllowableStress(pms.material, dtVal);
    const S_psi = stressData.S_psi;
    const S_mpa = stressData.S_mpa;
    const P_psig = parseFloat(barg2psig(dpVal));
    const P_mpa = barg2mpa(dpVal);
    const dtF = parseFloat(c2f(dtVal));
    const isNACE = pms.material.toUpperCase().includes('NACE') || pms.design_code.toUpperCase().includes('NACE');
    const isLTCS = pms.material.toUpperCase().includes('LT');
    const isDSS = pms.material.toUpperCase().includes('DSS') || pms.material.toUpperCase().includes('DUPLEX');
    const isSDSS = pms.material.toUpperCase().includes('SDSS') || pms.material.toUpperCase().includes('SUPER DUPLEX') || pms.material.toUpperCase().includes('S32750');
    const isSS = pms.material.toUpperCase().includes('SS') || pms.material.toUpperCase().includes('STAINLESS');
    const millTol = parseFloat(pms.mill_tolerance) || ENG.mill_tolerance_percent;
    const millFrac = millTol / 100;

    // Parse CA in mm — respect NIL / 0 corrosion allowance
    const caStr = pms.corrosion_allowance || '0';
    const caMM = caStr.toUpperCase().includes('NIL') ? 0 : (parseFloat(caStr) || 0);
    const caInch = mm2inch(caMM);

    // Determine pipe standard and Y coefficient description based on material
    const pipeStandard = (isSS || isDSS || isSDSS) ? 'ASME B36.19M' : 'ASME B36.10M';
    const yMatDesc = (isDSS || isSDSS) ? 'duplex/austenitic-ferritic steel' : (isSS ? 'austenitic stainless steel' : 'ferritic/alloy steel');

    // Formula example with NPS 6" if available — aligned with reference A1 Excel:
    //   t  = MAX(Case1, Case2) × OD    (pressure thickness only)
    //   tm = t + CA
    //   T  = tm / (1 - mill_tol)       ← displayed tREQ
    const ref6 = pms.pipe_data.find(p => p.size_inch === '6' || p.size_inch === '6"');
    if (ref6) {
        const od6 = mm2inch(ref6.od_mm).toFixed(3);
        const _t = pms.pressure_temperature.temperatures || [];
        const _p = pms.pressure_temperature.pressures || [];
        let _lo = 0;
        for (let i = 1; i < _t.length; i++) {
            if (parseFloat(_t[i]) < parseFloat(_t[_lo])) _lo = i;
        }
        const Tlow = parseFloat(_t[_lo]);
        const Pmax = parseFloat(_p[_lo]);

        // Case 1: use override if present, else convert P-T max from barg
        const case1Ov = document.getElementById('case1PressurePsig');
        const case1OvVal = case1Ov ? parseFloat(case1Ov.value) : NaN;
        const P1_psig = (!isNaN(case1OvVal) && case1OvVal > 0) ? case1OvVal : parseFloat(barg2psig(Pmax));

        // Case 2: user's design conditions (P from psig field, T from design-temp field)
        const Tuser = (dtVal > 0) ? dtVal : Math.max(..._t.map(parseFloat));
        const dpPsigField2 = document.getElementById('designPressurePsig');
        const P2_psig = dpPsigField2 ? (parseFloat(dpPsigField2.value) || parseFloat(barg2psig(interpolatePressure(_t, _p, Tuser))))
                                      : parseFloat(barg2psig(interpolatePressure(_t, _p, Tuser)));
        // Stress overrides (leave blank for auto)
        const s1F = document.getElementById('case1StressPsi');
        const s2F = document.getElementById('case2StressPsi');
        const s1OvVal = s1F ? parseFloat(s1F.value) : NaN;
        const s2OvVal = s2F ? parseFloat(s2F.value) : NaN;
        const S1_psi = (!isNaN(s1OvVal) && s1OvVal > 0) ? s1OvVal : getAllowableStress(pms.material, Tlow).S_psi;
        const S2_psi = (!isNaN(s2OvVal) && s2OvVal > 0) ? s2OvVal : getAllowableStress(pms.material, Tuser).S_psi;
        // Pressure thickness per case (no CA yet)
        const t1_p = (P1_psig * parseFloat(od6)) / (2 * (S1_psi * E * W + P1_psig * Y));
        const t2_p = (P2_psig * parseFloat(od6)) / (2 * (S2_psi * E * W + P2_psig * Y));
        const t_press = Math.max(t1_p, t2_p);
        const tm_inch = t_press + caInch;
        const T_req_inch = tm_inch / (1 - millFrac);
        const gov = (t1_p >= t2_p) ? 'Case 1 (Min T / Max P)' : 'Case 2 (Design Point)';
        document.getElementById('formulaExample').innerHTML =
            `<strong>NPS 6" example:</strong> OD = ${od6}" | E = ${E} | W = ${W} | Y = ${Y} ` +
            `<span style="color:var(--text-muted)">[${yMatDesc}]</span> | c = ${caMM > 0 ? caInch.toFixed(4) + '" (' + caMM + ' mm)' : 'NIL'} | mill tol = ${(millFrac*100).toFixed(1)}%<br>` +
            `<strong>Case 1 (Min T / Max P @ ${Tlow}\u00b0C):</strong> P = ${P1_psig.toFixed(1)} psig, S = ${S1_psi.toLocaleString()} psi ` +
            `\u2192 t<sub>press</sub> = <strong>${t1_p.toFixed(4)}"</strong>${t1_p >= t2_p ? ' \u2190 GOVERNS' : ''}<br>` +
            `<strong>Case 2 (Design Point @ ${Tuser}\u00b0C):</strong> P = ${P2_psig.toFixed(1)} psig, S = ${S2_psi.toLocaleString()} psi ` +
            `\u2192 t<sub>press</sub> = <strong>${t2_p.toFixed(4)}"</strong>${t2_p > t1_p ? ' \u2190 GOVERNS' : ''}<br>` +
            `<span style="color:#b91c1c"><strong>Using ${gov}: t = ${t_press.toFixed(4)}" \u2192 tm = t+c = ${tm_inch.toFixed(4)}" \u2192 ` +
            `T<sub>REQ</sub> = tm/(1\u2212${millFrac}) = ${T_req_inch.toFixed(4)}" (${inch2mm(T_req_inch).toFixed(2)} mm)</strong></span>`;
    } else {
        document.getElementById('formulaExample').innerHTML = '';
    }

    // Service Tags
    const tags = [];
    const svc = pms.service.toLowerCase();
    if (isNACE || svc.includes('sour') || svc.includes('h2s')) tags.push({ label: 'Sour / H\u2082S', color: '#b91c1c' });
    if (svc.includes('steam')) tags.push({ label: 'Steam', color: '#1e3a5f' });
    if (isLTCS || svc.includes('low temp')) tags.push({ label: 'Low Temperature', color: '#0369a1' });
    if (svc.includes('corrosive') || svc.includes('acid')) tags.push({ label: 'Corrosive', color: '#92400e' });
    if (svc.includes('hydrogen') || svc.includes('h2')) tags.push({ label: 'Hydrogen', color: '#6d28d9' });
    if (tags.length === 0) tags.push({ label: pms.service, color: '#1e3a5f' });

    document.getElementById('serviceTags').innerHTML =
        '<span style="margin-right:8px;color:var(--text-muted);font-size:0.85rem">Service:</span>' +
        tags.map(t => `<span class="service-tag" style="background:${t.color}">${t.label}</span>`).join('');

    // Design Parameters — TWO-CASE envelope analysis:
    //   Case 1 (Min T / Max P): P-T table's LOWEST temperature (burst/high-stress case)
    //   Case 2 (Max T / Min P): P-T table's HIGHEST temperature (reduced-stress case)
    // User can override pressures via Case1/Case2 psig override fields.
    const materialSpec = pms.pipe_data.length ? pms.pipe_data[0].material_spec : '\u2014';

    const ptTemps = pms.pressure_temperature.temperatures || [];
    const ptPress = pms.pressure_temperature.pressures || [];
    const ptLabels = pms.pressure_temperature.temp_labels || [];

    // Find min-temp and max-temp indices from P-T table
    let tMinI = 0, tMaxI = 0;
    for (let i = 1; i < ptTemps.length; i++) {
        if (parseFloat(ptTemps[i]) < parseFloat(ptTemps[tMinI])) tMinI = i;
        if (parseFloat(ptTemps[i]) > parseFloat(ptTemps[tMaxI])) tMaxI = i;
    }

    // Case 1 — min temp, max pressure (from P-T table)
    const T_low       = parseFloat(ptTemps[tMinI]);
    const P_max       = parseFloat(ptPress[tMinI]);
    const T_low_label = ptLabels[tMinI] || `${T_low}`;

    // Case 2 — max temp, pressure at that temp (from P-T table)
    // If user provided Design Pressure (psig) override, use it; else auto from P-T table.
    const T_high       = parseFloat(ptTemps[tMaxI]);
    const P_at_Tmax    = parseFloat(ptPress[tMaxI]);
    const T_high_label = ptLabels[tMaxI] || `${T_high}`;

    // Use P-T envelope max as Case 2 by default; user's design pressure psig can override
    const dpPsigField = document.getElementById('designPressurePsig');
    const userPsig = dpPsigField ? parseFloat(dpPsigField.value) : NaN;
    const userBarg = (!isNaN(userPsig) && userPsig > 0) ? userPsig / 14.5038 : NaN;
    // Only treat as override if user's barg differs significantly from envelope P_at_Tmax
    const case2Overridden = !isNaN(userBarg) && Math.abs(userBarg - P_at_Tmax) > 0.5;
    const P_case2_barg = case2Overridden ? userBarg : P_at_Tmax;
    const P_case2_psig = P_case2_barg * 14.5038;
    // Case 2 temperature: use P-T max by default; user's Design Temperature can override
    const T_case2 = (dtVal > 0 && Math.abs(dtVal - T_high) > 0.5) ? dtVal : T_high;
    const T_case2_label = case2Overridden || (dtVal > 0 && Math.abs(dtVal - T_high) > 0.5)
                          ? `${T_case2} (user input)`
                          : T_high_label;

    // Allowable stress at each case temp
    const S_atTlow  = getAllowableStress(pms.material, T_low);
    const S_atCase2 = getAllowableStress(pms.material, T_case2);

    // Expose to other render helpers (e.g. enhanced pipe table)
    pms._designEnvelope = {
        T_low,              T_high: T_case2,
        P_max,              P_min:  P_case2_barg,
        S_low:  S_atTlow,   S_high: S_atCase2
    };

    // Determine the governing case for a sample NPS (e.g., 6") so we can label it in the display
    const case1OvInput = document.getElementById('case1PressurePsig');
    const case1OvVal = case1OvInput ? parseFloat(case1OvInput.value) : NaN;
    const case1OvValid = !isNaN(case1OvVal) && case1OvVal > 0;
    const case1_psig = case1OvValid ? case1OvVal : parseFloat(barg2psig(P_max));
    const case1_barg = case1_psig / 14.5038;

    // Quick governing-case probe using NPS 6" (representative)
    const probeOdIn = 168.3 / 25.4;
    const t1_probe = (case1_psig * probeOdIn) / (2 * (S_atTlow.S_psi * E * W + case1_psig * Y));
    const t2_probe = (P_case2_psig * probeOdIn) / (2 * (S_atCase2.S_psi * E * W + P_case2_psig * Y));
    const case1Gov = t1_probe >= t2_probe;

    const gov1 = case1Gov ? ' <span style="color:#16a34a;font-weight:700">[GOVERNS]</span>'
                          : ' <span style="color:var(--text-muted)">[active]</span>';
    const gov2 = case1Gov ? ' <span style="color:var(--text-muted)">[active]</span>'
                          : ' <span style="color:#16a34a;font-weight:700">[GOVERNS]</span>';

    const designPressureDisplay =
        `<strong>Min T / Max P:</strong> ${case1_psig.toFixed(1)} psig (${case1_barg.toFixed(2)} barg) <span class="unit">@ ${T_low_label}\u00b0C</span>${gov1}<br>` +
        `<strong>Design Point:</strong> ${P_case2_psig.toFixed(1)} psig (${P_case2_barg.toFixed(2)} barg) <span class="unit">@ ${T_case2_label}\u00b0C</span>${gov2}<br>` +
        `<span class="unit" style="font-size:0.85em;color:var(--text-muted)">t<sub>REQ</sub> uses MAX(Case 1, Case 2) per size</span>`;

    const designTempDisplay =
        `<strong>Min:</strong> ${T_low_label}\u00b0C (${c2f(T_low)}\u00b0F) <span class="unit">[P-T min]</span>${gov1}<br>` +
        `<strong>Max (Design):</strong> ${T_case2_label}\u00b0C (${c2f(T_case2)}\u00b0F) <span class="unit">[design]</span>${gov2}`;

    const stressDisplay =
        `<strong>S @ ${T_low}\u00b0C:</strong> ${S_atTlow.S_psi.toLocaleString()} psi (${S_atTlow.S_mpa} MPa)${gov1}<br>` +
        `<strong>S @ ${T_case2}\u00b0C:</strong> ${S_atCase2.S_psi.toLocaleString()} psi (${S_atCase2.S_mpa} MPa)${gov2}<br>` +
        `<span class="unit">per ASME B31.3 Table A-1 [${pms.material}]</span>`;

    setKVList('designParamsList', [
        { l: 'PMS Class', v: `<strong>${pms.piping_class}</strong> (${pms.rating})` },
        { l: 'Design Pressure (P)', v: designPressureDisplay },
        { l: 'Design Temperature', v: designTempDisplay },
        { l: 'Material Spec', v: materialSpec },
        { l: 'Allowable Stress S(T)', v: stressDisplay },
    ]);

    // Code Factors
    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * ENG.hydrotest_factor);
    setKVList('codeFactorsList', [
        { l: 'Pipe Standard', v: pipeStandard, bold: true },
        { l: 'Joint Type', v: document.getElementById('jointType').value, bold: true },
        { l: 'Joint Efficiency (E)', v: E.toString() },
        { l: 'Y Coefficient', v: `${Y} <span class="unit">(ASME B31.3 Table 304.1.1 @ ${dtF}\u00b0F (${yMatDesc}))</span>` },
        { l: 'W-factor (Weld Str.)', v: `${W} <span class="unit">(ASME B31.3 Table 302.3.5 @ ${dtF}\u00b0F (W=1.0))</span>` },
        { l: 'Corrosion Allow. (c)', v: caMM > 0 ? `${caMM} mm` : '<strong>NIL</strong> (no corrosion allowance)', bold: true },
        { l: 'Mill Undertolerance', v: `${millTol}%` },
    ]);

    // Engineering Flags
    renderEngineeringFlags(pms, dpVal, isNACE, isLTCS);

    // Enhanced Pipe Table with calculations
    renderEnhancedPipeTable(pms, dpVal, S_psi, E, W, Y, caInch, caMM, millFrac, isNACE, isLTCS);
}

// === Engineering Flags ===
function renderEngineeringFlags(pms, dpVal, isNACE, isLTCS) {
    const flags = [];
    const svc = pms.service.toLowerCase();
    const mat = pms.material.toUpperCase();
    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * ENG.hydrotest_factor);
    // Base pressure for hydrotest: max P-T rated pressure if available, otherwise design pressure
    const ptPressures = pms.pressure_temperature && pms.pressure_temperature.pressures ? pms.pressure_temperature.pressures : [];
    const maxRatedP = ptPressures.length > 0 ? Math.max(...ptPressures) : dpVal;
    const htBaseP = pms.hydrotest_pressure ? (ht / ENG.hydrotest_factor) : dpVal;  // back-calculate the base used

    // Determine material family for flag specificity
    const isCS = mat.includes('CS') && !mat.includes('DSS') && !mat.includes('SS') && !mat.includes('SDSS');
    const isDSS = mat.includes('DSS') && !mat.includes('SDSS');
    const isSDSS = mat.includes('SDSS') || mat.includes('SUPER DUPLEX') || mat.includes('S32750');
    const isSS = mat.includes('SS') || mat.includes('STAINLESS');
    const isDuplexFamily = isDSS || isSDSS;

    if (isNACE) {
        // NACE compliance — material-specific hardness requirements
        if (isDuplexFamily) {
            flags.push({
                level: 'critical', badge: 'CRITICAL',
                title: 'NACE MR0175 / ISO 15156-3 \u2014 Duplex Sour Service Compliance',
                body: `All ${isSDSS ? 'Super Duplex (S32750)' : 'Duplex (S31803)'} pipe, fittings, flanges, and welds must comply with NACE MR0175 / ISO 15156-3 Annex A. Max hardness: ${isSDSS ? '32 HRC (SDSS)' : '28 HRC (DSS)'}. Solution annealing required. Ferrite content: 35\u201365%. PREN \u2265 ${isSDSS ? '40 (SDSS)' : '34 (DSS)'}. No PWHT required for DSS/SDSS (solution-annealed condition).`
            });
        } else if (isSS) {
            flags.push({
                level: 'critical', badge: 'CRITICAL',
                title: 'NACE MR0175 / ISO 15156-3 \u2014 Austenitic SS Sour Service Compliance',
                body: 'All SS316L pipe, fittings, flanges, and welds must comply with NACE MR0175 / ISO 15156-3. Max hardness: 22 HRC (solution annealed). Cold work limit applies. No PWHT typically required for austenitic SS.'
            });
        } else {
            flags.push({
                level: 'critical', badge: 'CRITICAL',
                title: 'NACE MR0175 / ISO 15156 \u2014 Sour Service Compliance',
                body: 'All pipe, fittings, flanges, and welds must comply with NACE MR0175 / ISO 15156. Max hardness: CS \u2264 22 HRC / 250 HBW (base metal, weld metal, HAZ). HIC testing per NACE TM0284 if H\u2082S partial pressure > 0.0003 MPa (0.05 psia). SSC testing per NACE TM0177 Method A may also be required.'
            });
        }

        // Minimum schedule — this is a PROJECT / COMPANY spec, NOT a NACE requirement
        if (isCS) {
            flags.push({
                level: 'warning', badge: 'PROJECT SPEC',
                title: 'Minimum Schedule Recommended \u2014 Sch 160 (\u2264 NPS 1\u00bd") / XS (\u2265 NPS 2")',
                body: 'Common oil & gas project specs (Shell DEP 31.38.01, Aramco SAES-L, Total GS EP PVV) require minimum Sch 160 (NPS \u2264 1\u00bd") / Extra Strong (NPS \u2265 2") for CS sour service \u2014 for mechanical robustness and lifecycle margin. NOTE: NACE MR0175 itself does NOT mandate any minimum schedule; this is a project / company standard. Verify against your project\u2019s Piping Design Basis (PDS).'
            });
        } else if (isDuplexFamily || isSS) {
            flags.push({
                level: 'note', badge: 'NOTE',
                title: `Schedule per Design Calculation \u2014 ${isDuplexFamily ? 'Duplex' : 'SS'} NACE`,
                body: `For ${isDuplexFamily ? 'Duplex/Super Duplex' : 'Stainless Steel'} NACE service, schedule is governed by pressure/mechanical design calculation \u2014 no project-standard minimum schedule override (unlike CS sour). Corrosion allowance is typically NIL for ${isDuplexFamily ? 'DSS/SDSS' : 'SS'} in sour service.`
            });
        }

        // Bolting — split into NACE requirements (grades) and project spec (coating)
        flags.push({
            level: 'mandatory', badge: 'NACE REQ',
            title: `NACE Bolting Grades \u2014 ${pms.bolts_nuts_gaskets.stud_bolts || 'A320 L7M Studs'} + ${pms.bolts_nuts_gaskets.hex_nuts || 'A194 7ML Nuts'}`,
            body: `Per NACE MR0175 Table 7: max hardness 22 HRC (studs) / 22 HRC (nuts) for sour service exposure. Studs: ${pms.bolts_nuts_gaskets.stud_bolts || 'ASTM A320 Gr. L7M'}. Nuts: ${pms.bolts_nuts_gaskets.hex_nuts || 'ASTM A194 Gr. 7ML'}. Alternative grades (B7M + 2HM) also NACE-compliant.`
        });
        if (isCS) {
            flags.push({
                level: 'warning', badge: 'PROJECT SPEC',
                title: 'Bolting Coating \u2014 XYLAR 2 + XYLAN 1070 (Project Optional)',
                body: 'XYLAR 2 + XYLAN 1070 coating (min 50 \u00b5m combined) is a common offshore / splash-zone project spec for corrosion and galling protection. NOTE: NACE MR0175 does NOT mandate coatings. Uncoated B7M / 2HM bolts are fully NACE-compliant for onshore applications. Verify against your project\u2019s bolting spec.'
            });
        }

        // PWHT — CONDITIONAL per ASME B31.3 + NACE MR0175 (not unconditional)
        if (isCS) {
            flags.push({
                level: 'warning', badge: 'CONDITIONAL',
                title: 'PWHT \u2014 Required Based on Thickness / Hardness',
                body: 'Per ASME B31.3 Table 331.1.1 (P-Number 1 / CS): PWHT required when nominal wall thickness > 19 mm (\u00be"). For thinner sections, PWHT may be waived if HAZ hardness \u2264 250 HBW is demonstrated in PQR. Per NACE MR0175 \u00a77.2.1.3, PWHT is NOT mandatory if hardness limits are met via: low-hydrogen electrodes + proper preheat + PQR hardness testing. WPS/PQR must include hardness survey regardless.'
            });
        } else if (isDuplexFamily) {
            flags.push({
                level: 'note', badge: 'NOTE',
                title: 'No PWHT Required \u2014 Duplex / Super Duplex',
                body: `PWHT is NOT required for ${isSDSS ? 'Super Duplex (S32750)' : 'Duplex (S31803)'}. Material is supplied in solution-annealed condition. Ferrite/austenite balance must be maintained in HAZ (35\u201365% ferrite).`
            });
        }
    }

    if (svc.includes('steam') || svc.includes('condensate')) {
        flags.push({
            level: 'note', badge: 'NOTE',
            title: 'Steam / Condensate \u2014 Thermal Fatigue & Drainage',
            body: 'Provide adequate drain points and thermal insulation. Check for water hammer and thermal cycling fatigue. For steam > 250\u00b0C apply ASME B31.1 Power Piping if applicable. ERW pipe not recommended; specify seamless.'
        });
    }

    if (svc.includes('corrosive') || svc.includes('acid') || svc.includes('chemical')) {
        // Material-specific corrosive-service guidance
        if (isSDSS) {
            flags.push({
                level: 'note', badge: 'NOTE',
                title: 'Corrosive / Acid Service \u2014 Super Duplex (PREN \u2265 40)',
                body: 'SDSS (S32750) has PREN \u2265 40 \u2014 one of the highest corrosion-resistant CRAs. CA typically NIL. Suitable for chloride, sour, and dilute acid exposure. Limits: avoid sustained service above 300\u00b0C (475\u00b0C embrittlement risk). Monitor crevice corrosion at gaskets/flange faces. NDE: 100% RT or UT for butt welds; maintain ferrite 35\u201355% in HAZ.'
            });
        } else if (isDSS) {
            flags.push({
                level: 'note', badge: 'NOTE',
                title: 'Corrosive / Acid Service \u2014 Duplex (PREN \u2265 34)',
                body: 'DSS (S31803) has PREN \u2265 34 \u2014 superior to SS 316L in chloride/sour environments. CA typically NIL. Avoid prolonged service above 300\u00b0C (475\u00b0C embrittlement). For highly aggressive acids (pH < 2) or high-chloride + high-temp combinations, consider SDSS or nickel alloys. NDE: 100% RT or UT for butt welds; maintain ferrite balance 35\u201365% in HAZ.'
            });
        } else if (isSS) {
            flags.push({
                level: 'warning', badge: 'WARNING',
                title: 'Corrosive / Acid Service \u2014 SS (Chloride SCC Risk)',
                body: 'SS 316L is susceptible to chloride stress corrosion cracking (SCC) above ~60\u00b0C or when Cl\u207b > 50 ppm. Consider upgrading to DSS/SDSS if: pH < 4, chloride > 50 ppm, or T > 60\u00b0C. Typical CA: 1\u20131.5 mm. 100% RT or UT for all butt welds. Monitor crevice corrosion at flanges and dead-legs.'
            });
        } else if (isCS || isLTCS) {
            if (isNACE) {
                // Already NACE-qualified CS — don't suggest "upgrade"; just verify chemistry bounds
                flags.push({
                    level: 'note', badge: 'NOTE',
                    title: 'Corrosive Service \u2014 Verify CS NACE Application Limits',
                    body: `CS NACE class (${pms.piping_class}) is already qualified for sour service. Verify process chemistry is within CS operating envelope: H\u2082S partial pressure, pH (typically > 4 for CS), chloride, temperature. For very aggressive sour (pH < 4, high H\u2082S, high Cl\u207b, T > 60\u00b0C), consider switching to a CRA class (DSS/SDSS) at material selection stage. Monitor corrosion rate at turnarounds.`
                });
            } else {
                // Non-NACE CS in corrosive service — legitimate upgrade suggestion
                flags.push({
                    level: 'warning', badge: 'WARNING',
                    title: 'Corrosive / Acid Service \u2014 CS May Be Insufficient',
                    body: 'For aggressive corrosive service, consider upgrading to SS 316L, DSS, or nickel alloy (especially if pH < 4, T > 60\u00b0C, or chloride-bearing). Minimum CA: 3.0 mm if CS is retained. 100% RT or UT typically specified by project. Monitor corrosion rate; review CA at major turnarounds. For sour + corrosive combined, NACE MR0175-compliant class required.'
                });
            }
        } else {
            // CuNi, GRE, CPVC, GALV or other non-metallics
            flags.push({
                level: 'note', badge: 'NOTE',
                title: 'Corrosive / Acid Service \u2014 Verify Material Compatibility',
                body: `Verify that ${pms.material} is compatible with the specific process fluid, concentration, and temperature. Consult material datasheet and corrosion tables. 100% RT or UT for butt welds where applicable.`
            });
        }
    }

    if (isNACE || svc.includes('sour') || svc.includes('h2s')) {
        flags.push({
            level: 'warning', badge: 'PROJECT SPEC',
            title: 'NDE: 100% RT or UT \u2014 Commonly Specified for Sour Service',
            body: '100% Radiographic (RT) or Ultrasonic (UT) examination of butt welds is typically required by client specifications for sour service (e.g., ExxonMobil GP 03-02-01, Shell DEP 31.38.01, Aramco SAES-L). NOTE: ASME B31.3 does NOT mandate 100% RT for sour service \u2014 default per \u00a7341.4.1 is 5% random RT for Normal Fluid Service. 100% RT is codified only for: Category M (high-toxicity fluids, \u00a7M341.4), Severe Cyclic (\u00a7341.4.3), or when specified by the owner. Verify against your project\u2019s inspection test plan (ITP).'
        });
    }

    if (isLTCS) {
        flags.push({
            level: 'mandatory', badge: 'MANDATORY',
            title: 'Low Temperature Service \u2014 Impact Testing Required',
            body: 'Impact testing per ASME B31.3 \u00a7323.2 required for LTCS materials at MDMT. Charpy V-notch test: minimum 27J (20 ft-lbs) at MDMT. Materials must be A333 Gr.6 / A350 LF2 / A352 LCB or equivalent.'
        });
    }

    // Hydrotest — formula is simplified; strict ASME B31.3 §345.4.2 includes stress correction
    flags.push({
        level: 'mandatory', badge: 'MANDATORY',
        title: `Hydrostatic Test Pressure: ${ht.toFixed(1)} barg (\u2248 1.5 \u00d7 ${htBaseP.toFixed(1)} barg max rated pressure)`,
        body: `Shop test: ${ht.toFixed(1)} barg per ASME B31.3 \u00a7345.4.2. Base: max P-T rated pressure = ${htBaseP.toFixed(1)} barg (at ambient). Medium: potable water (deionised for SS; chloride \u2264 50 ppm). Duration: minimum 10 minutes. Verify all flanges rated \u2265 ${ht.toFixed(1)} barg at test temperature. NOTE: Strict \u00a7345.4.2(b) formula is P<sub>test</sub> = 1.5 \u00d7 P<sub>design</sub> \u00d7 (S<sub>T_ambient</sub> / S<sub>T_design</sub>) \u2014 may yield higher pressure when design temperature significantly reduces allowable stress. This display uses the simplified 1.5 \u00d7 rated pressure form as a conservative default.`
    });

    const container = document.getElementById('engineeringFlags');
    if (flags.length === 0) {
        container.innerHTML = '<p style="color:var(--text-muted);padding:12px">No special engineering flags for this specification.</p>';
        return;
    }

    container.innerHTML = flags.map(f => `
        <div class="flag-card ${f.level}">
            <div class="flag-header">
                <span class="flag-badge ${f.level}">${f.badge}</span>
                <span class="flag-title">${f.title}</span>
            </div>
            <p class="flag-body">${f.body}</p>
        </div>
    `).join('');
}

// === Enhanced Pipe Table ===
function renderEnhancedPipeTable(pms, dpVal, S_psi, E, W, Y, caInch, caMM, millFrac, isNACE, isLTCS) {
    const pipes = pms.pipe_data;
    if (!pipes.length) {
        document.getElementById('enhancedPipeTable').innerHTML = '<p style="color:var(--text-muted)">No pipe data available</p>';
        document.getElementById('summaryStats').innerHTML = '';
        return;
    }

    const P_psig = parseFloat(barg2psig(dpVal));
    const results = [];

    pipes.forEach(p => {
        const od_inch = mm2inch(p.od_mm);
        const wt_mm = p.wall_thickness_mm;
        const wt_inch = mm2inch(wt_mm);
        const sizeNum = parseFloat(p.size_inch) || 0;

        // t_req: matches reference A1 Excel (20171-SPOG-80000-PP-CL-0001):
        //   t  (pressure)  = MAX(t1, t2) × OD     (t1, t2 are t/D ratios per case)
        //   tm             = t + CA
        //   T  (displayed) = tm / (1 - mill_tolerance)   ← this is what column "tREQ (mm)" shows
        // Adequacy check becomes:  nominal WT ≥ T
        // Case 1: Max P @ Min T (higher P, higher S)
        // Case 2: User design-temp P @ Design T (interpolated)
        const env = pms._designEnvelope;
        let t_pressure_inch;
        if (env) {
            // BOTH CASES always calculated; tREQ uses the GOVERNING (larger) one.
            //
            // Case 1 (Min T / Max P):
            //   Pressure = Case 1 Override if user provided it, else P-T envelope max pressure
            //   Stress   = Case 1 Stress Override if provided, else S(T_low) from material table
            //   Temperature = P-T envelope min temp
            const case1OverrideField = document.getElementById('case1PressurePsig');
            const case1Override = case1OverrideField ? parseFloat(case1OverrideField.value) : NaN;
            const P1_psig = (!isNaN(case1Override) && case1Override > 0)
                           ? case1Override
                           : parseFloat(barg2psig(env.P_max));
            const s1OvField = document.getElementById('case1StressPsi');
            const s1Ov = s1OvField ? parseFloat(s1OvField.value) : NaN;
            const S1 = (!isNaN(s1Ov) && s1Ov > 0) ? s1Ov : env.S_low.S_psi;
            const t1_p = (P1_psig * od_inch) / (2 * (S1 * E * W + P1_psig * Y));

            // Case 2 (Max T / Design T):
            //   Pressure = user's Design Pressure (psig input, auto-interpolated from P-T at Design T)
            //   Stress   = Case 2 Stress Override if provided, else S(T_case2) from material table
            //   Temperature = user's Design Temperature
            const dpPsigField = document.getElementById('designPressurePsig');
            const P2_psig = dpPsigField ? (parseFloat(dpPsigField.value) || parseFloat(barg2psig(env.P_min)))
                                         : parseFloat(barg2psig(env.P_min));
            const s2OvField = document.getElementById('case2StressPsi');
            const s2Ov = s2OvField ? parseFloat(s2OvField.value) : NaN;
            const S2 = (!isNaN(s2Ov) && s2Ov > 0) ? s2Ov : env.S_high.S_psi;
            const t2_p = (P2_psig * od_inch) / (2 * (S2 * E * W + P2_psig * Y));

            // tREQ uses whichever case produces THICKER required wall
            t_pressure_inch = Math.max(t1_p, t2_p);
        } else {
            t_pressure_inch = (P_psig * od_inch) / (2 * (S_psi * E * W + P_psig * Y));
        }
        const t_pressure_mm = inch2mm(t_pressure_inch);        // "t" column — pressure thickness
        const tm_inch = t_pressure_inch + caInch;              // "tm" column — t + CA
        const tm_mm = inch2mm(tm_inch);
        const t_req_inch = tm_inch / (1 - millFrac);           // "T" column — tm / (1 - mill_tol)
        const t_req_mm = inch2mm(t_req_inch);

        // D/6 and t<D/6 applicability check (thin-wall equation validity)
        const d_over_6_mm = p.od_mm / 6;
        const t_applicable = (t_pressure_mm < d_over_6_mm) ? 'OK' : 'ALERT';

        // t_min = WT_nom * (1 - mill%) — minimum thickness after mill tolerance (for MAWP)
        const t_min_mm = wt_mm * (1 - millFrac);
        const t_min_inch = mm2inch(t_min_mm);

        // t_eff = t_min - CA — effective thickness for MAWP calculation
        const t_eff_mm = t_min_mm - caMM;
        const t_eff_inch = mm2inch(t_eff_mm);

        // MAWP = [2 * S * E * W * t_eff] / [OD - 2 * Y * t_eff]   (in psi, then convert to barg)
        let mawp_psi = 0;
        let mawp_barg = 0;
        if (t_eff_inch > 0 && (od_inch - 2 * Y * t_eff_inch) > 0) {
            mawp_psi = (2 * S_psi * E * W * t_eff_inch) / (od_inch - 2 * Y * t_eff_inch);
            mawp_barg = mawp_psi / 14.5038;
        }

        const margin = dpVal > 0 ? ((mawp_barg - dpVal) / dpVal * 100) : 0;
        const utilization = mawp_barg > 0 ? (dpVal / mawp_barg * 100) : 100;

        // Determine schedule tags and governs
        let tags = [];
        let governs = '';
        const matUpper = pms.material.toUpperCase();
        const isCSNACE = isNACE && matUpper.includes('CS') && !matUpper.includes('DSS') && !matUpper.includes('SS') && !matUpper.includes('SDSS');
        if (isNACE && isCSNACE) {
            tags.push('NACE');
            if (sizeNum <= 1.5) {
                governs = `PMS minimum \u2014 Sch 160 (NPS \u2264 1\u00bd")`;
            } else if (sizeNum <= 6) {
                governs = `PMS minimum \u2014 Sch 80 (NPS 2"\u20136")`;
            } else {
                governs = `PMS minimum \u2014 XS (NPS \u2265 ${sizeNum}")`;
            }
        } else if (isNACE) {
            tags.push('NACE');
            governs = 'Design calculation governs';
        } else if (isLTCS) {
            tags.push('LTCS');
            governs = 'Low-temperature service minimum governs';
        }

        if (!governs) {
            // t_req now includes (1 / (1 - mill_tol)) factor — compare directly to nominal WT
            if (t_req_mm > wt_mm) {
                governs = 'Pressure governs';
                tags.push('Pressure');
            } else {
                governs = 'PMS minimum schedule governs';
            }
        }

        results.push({
            size: p.size_inch,
            od: p.od_mm,
            schedule: p.schedule,
            wt_nom: wt_mm,
            t_pressure: t_pressure_mm,      // pressure thickness only ("t" in Excel)
            d_over_6: d_over_6_mm,           // D/6
            t_applicable: t_applicable,      // OK/ALERT for t<D/6
            tm: tm_mm,                       // t + CA ("tm" in Excel)
            t_req: t_req_mm,                 // tm / (1 - mill_tol) ("T" in Excel — displayed tREQ)
            t_min: t_min_mm,
            t_eff: t_eff_mm,
            mawp: mawp_barg,
            margin: margin,
            utilization: utilization,
            tags: tags,
            governs: governs,
        });
    });

    // Build table — column layout matches reference Excel A1 sheet
    // (20171-SPOG-80000-PP-CL-0001_Rev03.xlsx → A1 sheet)
    const millPct = (millFrac * 100).toFixed(1);
    let html = `<table><thead><tr>
        <th>NPS</th>
        <th>D<br>(mm)</th>
        <th>t<br>(mm)</th>
        <th>D/6<br>(mm)</th>
        <th>If<br>t&lt;D/6</th>
        <th>t<sub>m</sub><br>(mm)</th>
        <th>Mill<br>Tol.</th>
        <th>Calc. Thk<br>T (mm)</th>
        <th>Sel. Thk<br>(mm)</th>
        <th>SCH</th>
        <th>Sel. Thk<br>Status</th>
        <th>MAWP<br>(barg)</th>
        <th>Margin</th>
        <th>Governs</th>
    </tr></thead><tbody>`;

    results.forEach(r => {
        const tagHtml = r.tags.map(t => {
            const cls = t === 'NACE' ? 'nace' : t === 'LTCS' ? 'ltcs' : t === 'Pressure' ? 'pressure' : 'default';
            return `<span class="pipe-tag ${cls}">${t}</span>`;
        }).join(' ');

        // Status: OK if nominal WT >= tREQ
        const selOK = r.wt_nom >= r.t_req ? 'OK' : 'NOT OK';
        const selColor = selOK === 'OK' ? '#16a34a' : '#b91c1c';
        const applColor = r.t_applicable === 'OK' ? '#16a34a' : '#b91c1c';

        // Sel. Thk display rule:
        //   • Schedule is a real code (XXS, 160, 80S, STD, …) → show the PMS
        //     nominal WT (looked up from ASME B36.10M / B36.19M tables).
        //   • Schedule is "-" or blank → there is no table selection; mirror
        //     the Calc. Thk T column rounded to 2 decimals. Per the project
        //     owner: no extra math, just round(Calc. Thk T).
        const schRaw = String(r.schedule || '').trim();
        const schIsCalc = schRaw === '' || schRaw === '-' || schRaw === '--' || schRaw === '\u2014';
        const selThkDisplay = schIsCalc ? r.t_req.toFixed(2) : r.wt_nom;

        html += `<tr>
            <td><strong>${r.size}"</strong></td>
            <td>${r.od}</td>
            <td>${r.t_pressure.toFixed(3)}</td>
            <td>${r.d_over_6.toFixed(2)}</td>
            <td style="color:${applColor};font-weight:600">${r.t_applicable}</td>
            <td>${r.tm.toFixed(3)}</td>
            <td>${millPct}%</td>
            <td><strong>${r.t_req.toFixed(3)}</strong></td>
            <td>${selThkDisplay}</td>
            <td><strong>${r.schedule}</strong> ${tagHtml}</td>
            <td style="color:${selColor};font-weight:600">${selOK}</td>
            <td>${r.mawp.toFixed(1)}</td>
            <td>${r.margin.toFixed(1)}%</td>
            <td class="governs-cell">${r.governs}</td>
        </tr>`;
    });
    html += '</tbody></table>';

    document.getElementById('enhancedPipeTable').innerHTML = html;

    // Summary Stats
    const mawps = results.map(r => r.mawp).filter(m => m > 0);
    const margins = results.map(r => r.margin).filter(m => m > 0);
    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * ENG.hydrotest_factor);

    setKVList('summaryStats', [
        { l: 'Min MAWP', v: `${Math.min(...mawps).toFixed(1)} barg` },
        { l: 'Max MAWP', v: `${Math.max(...mawps).toFixed(1)} barg` },
        { l: 'Min Pressure Margin', v: `${Math.min(...margins).toFixed(1)}%` },
        { l: 'Hydrotest Pressure (1.5\u00d7P)', v: `<strong>${ht.toFixed(1)} barg</strong>`, bold: true },
        { l: 'Total NPS Sizes', v: `${results.length}` },
    ]);

    // Tag Legend
    const legendItems = [];
    if (isNACE) legendItems.push({ tag: 'nace', label: 'NACE', desc: 'NACE MR0175 / ISO 15156 minimum schedule governs' });
    if (isLTCS) legendItems.push({ tag: 'ltcs', label: 'LTCS', desc: 'Low-temperature service minimum governs' });
    legendItems.push({ tag: 'pressure', label: 'Pressure', desc: 'ASME B31.3 Eq. 3a governs' });

    document.getElementById('tagLegend').innerHTML = legendItems.map(item =>
        `<div class="legend-row"><span class="pipe-tag ${item.tag}">${item.label}</span> <span class="legend-desc">${item.desc}</span></div>`
    ).join('');
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

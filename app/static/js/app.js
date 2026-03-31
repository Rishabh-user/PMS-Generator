// === State ===
let currentPMS = null;
let allClasses = [];
let indexData = [];

const API = {
    generatePMS: (data) => fetch('/api/generate-pms', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
    downloadExcel: (data) => fetch('/api/download-excel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
    listClasses: () => fetch('/api/pipe-classes'),
    listCodes: () => fetch('/api/pipe-classes/codes'),
    indexData: () => fetch('/api/index-data'),
    health: () => fetch('/health'),
};

// === Engineering Constants ===
const barg2psig = (b) => (b * 14.5038).toFixed(1);
const c2f = (c) => (c * 9 / 5 + 32).toFixed(1);
const mm2inch = (mm) => mm / 25.4;
const inch2mm = (inch) => inch * 25.4;
const barg2mpa = (b) => b * 0.1;
const psi2mpa = (p) => p * 0.00689476;
const mpa2psi = (m) => m / 0.00689476;

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
    const dt = document.getElementById('designTemperature');
    const mdmt = document.getElementById('mdmt');
    const jt = document.getElementById('jointType');

    const update = () => {
        const dpv = parseFloat(dp.value) || 0;
        const dtv = parseFloat(dt.value) || 0;
        const mv = parseFloat(mdmt.value) || 0;
        document.getElementById('pressurePsig').textContent = `\u2248 ${barg2psig(dpv)} psig`;
        document.getElementById('tempFahrenheit').textContent = `= ${c2f(dtv)} \u00b0F`;
        document.getElementById('mdmtFahrenheit').textContent = `= ${c2f(mv)} \u00b0F`;

        document.getElementById('jointRef').textContent = `ASME B31.3 Table A-1B`;

        if (currentPMS) updateCalculations();
    };

    [dp, dt, mdmt, jt].forEach(el => el.addEventListener('input', update));
    update();
}

// === Generate PMS ===
async function generatePMS() {
    const selectedRating = document.getElementById('pipingClass').value.trim();
    const selectedMaterial = document.getElementById('material').value;
    const selectedCA = document.getElementById('corrosionAllowance').value;
    const selectedService = document.getElementById('service').value.trim();

    if (!selectedRating || !selectedMaterial) { showToast('Please select Rating and Material', 'error'); return; }

    // Resolve piping class from rating + material + CA
    const resolvedClass = resolvePipingClass(selectedRating, selectedMaterial, selectedCA);
    if (!resolvedClass) { showToast('No matching piping class found for this combination', 'error'); return; }

    const data = {
        piping_class: resolvedClass,
        material: selectedMaterial,
        corrosion_allowance: selectedCA,
        service: selectedService || 'General',
    };

    showLoading('Loading PMS from reference data...');
    try {
        const res = await API.generatePMS(data);
        if (!res.ok) { const err = await res.json(); throw new Error(err.detail || 'Generation failed'); }
        currentPMS = await res.json();

        // Render PMS Code Banner
        renderPMSCodeBanner(currentPMS);

        renderFullResult(currentPMS);
        document.getElementById('resultsContainer').style.display = '';
        // Activate first result tab
        document.querySelectorAll('.result-tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.result-panel').forEach(p => p.classList.remove('active'));
        document.querySelector('.result-tab').classList.add('active');
        document.querySelector('.result-panel').classList.add('active');
        document.getElementById('resultsContainer').scrollIntoView({ behavior: 'smooth', block: 'start' });
        showToast('PMS loaded from reference data!', 'success');
    } catch (err) { showToast(err.message, 'error'); }
    finally { hideLoading(); }
}

// === PMS Code Banner ===
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
    `;
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
        let bestIdx = 0;
        for (let i = 0; i < n; i++) {
            if (temps[i] >= 100) { bestIdx = i; break; }
            bestIdx = i;
        }
        dp.value = press[bestIdx];
        dt.value = temps[bestIdx];
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

    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * 1.5);
    const htStr = typeof ht === 'number' ? ht.toFixed(1) : String(ht);
    const op = (dpVal * 0.8).toFixed(1);
    const opT = (dtVal * 0.8).toFixed(1);

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
function renderScheduleTab(pms) {
    const dpVal = parseFloat(document.getElementById('designPressure').value) || 0;
    const dtVal = parseFloat(document.getElementById('designTemperature').value) || 0;
    const E = 1.0;  // Seamless butt weld joint efficiency
    const W = 1.0;
    const Y = 0.4;
    const S_psi = 20000;  // Reference allowable stress (per ASME B31.3)
    const S_mpa = 137.9;
    const P_psig = parseFloat(barg2psig(dpVal));
    const P_mpa = barg2mpa(dpVal);
    const dtF = parseFloat(c2f(dtVal));
    const isNACE = pms.material.toUpperCase().includes('NACE') || pms.design_code.toUpperCase().includes('NACE');
    const isLTCS = pms.material.toUpperCase().includes('LT');
    const millTol = parseFloat(pms.mill_tolerance) || 12.5;
    const millFrac = millTol / 100;

    // Parse CA in mm
    const caStr = pms.corrosion_allowance || '3 mm';
    const caMM = parseFloat(caStr) || 3;
    const caInch = mm2inch(caMM);

    // Formula example with NPS 6" if available
    const ref6 = pms.pipe_data.find(p => p.size_inch === '6' || p.size_inch === '6"');
    if (ref6) {
        const od6 = mm2inch(ref6.od_mm).toFixed(3);
        const t_calc_inch = (P_psig * parseFloat(od6)) / (2 * (S_psi * E * W + P_psig * Y));
        document.getElementById('formulaExample').innerHTML =
            `<strong>NPS 6" example:</strong>&nbsp; P = ${P_psig} psig | OD = ${od6}" | S(T) = ${S_psi.toLocaleString()} psi | E = ${E} | W = ${W} ` +
            `<span style="color:var(--text-muted)">(ASME B31.3 Table 302.3.5 @ ${dtF}\u00b0F (W=1.0))</span> | Y = ${Y} ` +
            `<span style="color:var(--text-muted)">(ASME B31.3 Table 304.1.1 @ ${dtF}\u00b0F (ferritic/alloy steel))</span> | ` +
            `c = ${caInch.toFixed(4)}" &nbsp;\u2192&nbsp; t<sub>calc</sub> = <strong>${t_calc_inch.toFixed(4)}"</strong>`;
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

    // Design Parameters
    const materialSpec = pms.pipe_data.length ? pms.pipe_data[0].material_spec : '\u2014';
    setKVList('designParamsList', [
        { l: 'PMS Class', v: `<strong>${pms.piping_class}</strong> (${pms.rating})` },
        { l: 'Design Pressure (P)', v: `${P_psig} psig (${dpVal} barg)` },
        { l: 'Design Temperature', v: `${dtF}\u00b0F (${dtVal}\u00b0C)` },
        { l: 'Material Spec', v: materialSpec },
        { l: 'Reference Allowable Stress S(T)', v: `${S_psi.toLocaleString()} psi (${S_mpa} MPa) @ Design Temp` },
    ]);

    // Code Factors
    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * 1.5);
    setKVList('codeFactorsList', [
        { l: 'Pipe Standard', v: 'ASME B36.10M', bold: true },
        { l: 'Joint Type', v: document.getElementById('jointType').value, bold: true },
        { l: 'Joint Efficiency (E)', v: E.toString() },
        { l: 'Y Coefficient', v: `${Y} <span class="unit">(ASME B31.3 Table 304.1.1 @ ${dtF}\u00b0F (ferritic/alloy steel))</span>` },
        { l: 'W-factor (Weld Str.)', v: `${W} <span class="unit">(ASME B31.3 Table 302.3.5 @ ${dtF}\u00b0F (W=1.0))</span>` },
        { l: 'Corrosion Allow. (c)', v: `${caMM} mm`, bold: true },
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
    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * 1.5);

    if (isNACE) {
        flags.push({
            level: 'critical', badge: 'CRITICAL',
            title: 'NACE MR0175 / ISO 15156 \u2014 Sour Service Compliance',
            body: 'All pipe, fittings, flanges, and welds must comply with NACE MR0175 / ISO 15156. Max hardness: CS \u2264 22 HRC / 250 HBW (base metal, weld metal, HAZ). HIC testing per NACE TM0284 if H\u2082S partial pressure > 0.0003 MPa (0.05 psia). SSC testing per NACE TM0177 Method A may also be required.'
        });
        flags.push({
            level: 'critical', badge: 'CRITICAL',
            title: 'Minimum Schedule Enforced \u2014 Sch 160 (\u2264 NPS 1\u00bd") / XS (\u2265 NPS 2")',
            body: 'NACE MR0175 mandates minimum wall thickness regardless of pressure calculation. NPS \u2264 1\u00bd": Schedule 160. NPS \u2265 2": Extra Strong (XS / Sch 80). Do NOT downgrade based on pressure margin alone.'
        });
        flags.push({
            level: 'mandatory', badge: 'MANDATORY',
            title: `NACE Bolting \u2014 ${pms.bolts_nuts_gaskets.stud_bolts || 'A320 L7M Studs'} + ${pms.bolts_nuts_gaskets.hex_nuts || 'A194 7ML Nuts'} (XYLAN Coated)`,
            body: `Studs: ${pms.bolts_nuts_gaskets.stud_bolts || 'ASTM A320 Gr. L7M'}. Nuts: ${pms.bolts_nuts_gaskets.hex_nuts || 'ASTM A194 Gr. 7ML'}. Coating: XYLAR 2 + XYLAN 1070, minimum combined thickness 50 \u00b5m.`
        });
        flags.push({
            level: 'mandatory', badge: 'MANDATORY',
            title: 'PWHT \u2014 Post Weld Heat Treatment Required',
            body: 'PWHT mandatory for all carbon steel welds in NACE/sour service to ensure HAZ hardness \u2264 250 HBW. WPS/PQR must include hardness survey.'
        });
    }

    if (svc.includes('steam') || svc.includes('condensate')) {
        flags.push({
            level: 'note', badge: 'NOTE',
            title: 'Steam / Condensate \u2014 Thermal Fatigue & Drainage',
            body: 'Provide adequate drain points and thermal insulation. Check for water hammer and thermal cycling fatigue. For steam > 250\u00b0C apply ASME B31.1 Power Piping if applicable. ERW pipe not recommended; specify seamless.'
        });
    }

    if (svc.includes('corrosive') || svc.includes('acid') || svc.includes('chemical')) {
        flags.push({
            level: 'warning', badge: 'WARNING',
            title: 'Corrosive / Acid Service \u2014 Enhanced CA & NDE',
            body: 'Minimum recommended CA: 3.0 mm. Consider upgrading to SS 316L or Alloy if pH < 4 or T > 60\u00b0C. 100% RT or UT required for all butt welds. Monitor corrosion rate and review CA at major turnarounds.'
        });
    }

    if (isNACE || svc.includes('sour') || svc.includes('h2s')) {
        flags.push({
            level: 'mandatory', badge: 'MANDATORY',
            title: 'NDE: 100% RT or UT \u2014 NACE / Sour Service (B31.3 \u00a7341.4.2)',
            body: 'Weld examination: 100% RT or UT \u2014 NACE / Sour Service (B31.3 \u00a7341.4.2). PWHT: Required \u2014 NACE MR0175 hardness control (HAZ \u2264 250 HBW). Pressure test: hydrostatic at ' + ht.toFixed(1) + ' barg (1.5 \u00d7 DP) per B31.3 \u00a7345.4.2.'
        });
    }

    if (isLTCS) {
        flags.push({
            level: 'mandatory', badge: 'MANDATORY',
            title: 'Low Temperature Service \u2014 Impact Testing Required',
            body: 'Impact testing per ASME B31.3 \u00a7323.2 required for LTCS materials at MDMT. Charpy V-notch test: minimum 27J (20 ft-lbs) at MDMT. Materials must be A333 Gr.6 / A350 LF2 / A352 LCB or equivalent.'
        });
    }

    // Always add hydrotest flag
    flags.push({
        level: 'mandatory', badge: 'MANDATORY',
        title: `Hydrostatic Test Pressure: ${ht.toFixed(1)} barg (= 1.5 \u00d7 ${dpVal} barg DP)`,
        body: `Shop test: ${ht.toFixed(1)} barg per ASME B31.3 \u00a7345.4.2. Medium: potable water (deionised for SS). Duration: minimum 10 minutes. Verify all flanges rated \u2265 ${ht.toFixed(1)} barg at test temperature.`
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

        // t_req in inches (pressure calculation only, no CA)
        const t_req_inch = (P_psig * od_inch) / (2 * (S_psi * E * W + P_psig * Y));
        const t_req_mm = inch2mm(t_req_inch);

        // t_min = WT_nom * (1 - mill%) — minimum thickness after mill tolerance
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
        if (isNACE) {
            tags.push('NACE');
            if (sizeNum <= 1.5) {
                governs = `PMS minimum \u2014 Sch 160 (NPS \u2264 1\u00bd")`;
            } else if (sizeNum <= 6) {
                governs = `PMS minimum \u2014 Sch 80 (NPS 2"\u20136")`;
            } else {
                governs = `PMS minimum \u2014 XS (NPS \u2265 ${sizeNum}")`;
            }
        } else if (isLTCS) {
            tags.push('LTCS');
            governs = 'Low-temperature service minimum governs';
        }

        if (!governs) {
            if (t_req_mm > t_min_mm) {
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
            t_req: t_req_mm,
            t_min: t_min_mm,
            t_eff: t_eff_mm,
            mawp: mawp_barg,
            margin: margin,
            utilization: utilization,
            tags: tags,
            governs: governs,
        });
    });

    // Build table
    let html = `<table><thead><tr>
        <th>NPS (in)</th><th>OD (mm)</th><th>Schedule / Tags</th><th>WT Nom (mm)</th>
        <th>t<sub>REQ</sub> (mm)</th><th>t<sub>MIN</sub> (mm)</th><th>t<sub>EFF</sub> (mm)</th>
        <th>MAWP (barg)</th><th>Margin</th><th>Util.</th><th>Governs</th>
    </tr></thead><tbody>`;

    results.forEach(r => {
        const tagHtml = r.tags.map(t => {
            const cls = t === 'NACE' ? 'nace' : t === 'LTCS' ? 'ltcs' : t === 'Pressure' ? 'pressure' : 'default';
            return `<span class="pipe-tag ${cls}">${t}</span>`;
        }).join(' ');

        html += `<tr>
            <td><strong>${r.size}"</strong></td>
            <td>${r.od}</td>
            <td><strong>${r.schedule}</strong> ${tagHtml}</td>
            <td>${r.wt_nom}</td>
            <td>${r.t_req.toFixed(3)}</td>
            <td>${r.t_min.toFixed(2)}</td>
            <td>${r.t_eff.toFixed(2)}</td>
            <td><strong>${r.mawp.toFixed(1)}</strong></td>
            <td>${r.margin.toFixed(1)}%</td>
            <td>${r.utilization.toFixed(1)}%</td>
            <td class="governs-cell">${r.governs}</td>
        </tr>`;
    });
    html += '</tbody></table>';

    document.getElementById('enhancedPipeTable').innerHTML = html;

    // Summary Stats
    const mawps = results.map(r => r.mawp).filter(m => m > 0);
    const margins = results.map(r => r.margin).filter(m => m > 0);
    const ht = pms.hydrotest_pressure ? parseFloat(pms.hydrotest_pressure) : (dpVal * 1.5);

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
    const smallBore = pipes.filter(p => parseFloat(p.size_inch) <= 2);
    const largeBore = pipes.filter(p => parseFloat(p.size_inch) > 2);

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
        { l: 'Ball', v: pms.valves.ball },
        { l: 'Gate', v: pms.valves.gate },
        { l: 'Globe', v: pms.valves.globe },
        { l: 'Check', v: pms.valves.check },
    ];
    if (pms.valves.butterfly) {
        valveItems.push({ l: 'Butterfly', v: pms.valves.butterfly });
    }
    setKVList('valvesList', valveItems);

    setKVList('spectacleList', [
        { l: 'MOC', v: pms.spectacle_blind.material_spec },
        { l: 'Standard', v: pms.spectacle_blind.standard },
    ]);

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

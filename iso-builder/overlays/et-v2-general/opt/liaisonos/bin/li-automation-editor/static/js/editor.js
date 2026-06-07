/*
 * Author  : Sylvain Deguire (VA2OPS)
 * Purpose : LiaisonOS Automation Editor — client-side logic.
 *           Renders the presence + mission lists from window.SCHEDULE,
 *           edits in-place, builds connect_url from form fields, queries
 *           Pat for the RMS picker, and round-trips through /api/save.
 */

// ── State ────────────────────────────────────────────────────────────────────
// We work on a deep clone so unsaved edits stay isolated and can be discarded
// by the user reloading the page.
let state = JSON.parse(JSON.stringify(SCHEDULE));
ensureSkeleton(state);

// Mission-index currently bound to the open RMS picker. Set by openRmsPicker(),
// consumed by selectRms(). null when the picker isn't pointing at any mission.
let rmsPickerTarget = null;

const T = LANG === "fr" ? {
    remove: "Supprimer",
    edit_alts: "Alternatives",
    pick_rms: "Choisir un RMS",
    confirm_del_presence: "Supprimer ce mode de présence ?",
    confirm_del_mission: "Supprimer cette mission ?",
    saved: "Sauvegardé.",
    validate_ok: "Validation OK.",
    validate_failed: "Validation échouée — voir détails plus bas.",
    save_failed: "Échec de la sauvegarde",
    restarted: "Démon redémarré.",
    no_rms: "Aucun RMS retourné par Pat.",
} : {
    remove: "Remove",
    edit_alts: "Alt RMS",
    pick_rms: "Pick RMS",
    confirm_del_presence: "Remove this presence window?",
    confirm_del_mission: "Remove this mission?",
    saved: "Saved.",
    validate_ok: "Validation OK.",
    validate_failed: "Validation failed — see details below.",
    save_failed: "Save failed",
    restarted: "Daemon restarted.",
    no_rms: "Pat returned no RMS stations.",
};


// ── Skeleton ensure ──────────────────────────────────────────────────────────
function ensureSkeleton(d) {
    if (!d.timezone) d.timezone = "UTC";
    if (!d.station) d.station = {};
    if (typeof d.station.callsign_must_be_set !== "boolean")
        d.station.callsign_must_be_set = true;
    if (!Array.isArray(d.presence)) d.presence = [];
    if (!Array.isArray(d.missions)) d.missions = [];
    if (!d.boot) d.boot = {start_current_presence: true, delay_seconds: 30};
    if (!d.override_flag) d.override_flag = "~/.cache/liaisonos/automation-paused";
}


// ── Top-level form ←→ state binding ──────────────────────────────────────────
function loadTopForm() {
    document.getElementById("f-timezone").value = state.timezone || "UTC";
    document.getElementById("f-role").value  = state.station.role  || "";
    document.getElementById("f-radio").value = state.station.radio || "";
    document.getElementById("f-callsign-must").checked = !!state.station.callsign_must_be_set;
    document.getElementById("f-boot-start").checked   = !!(state.boot && state.boot.start_current_presence);
    document.getElementById("f-boot-delay").value     = (state.boot && state.boot.delay_seconds) ?? 30;
}
function syncTopForm() {
    state.timezone = document.getElementById("f-timezone").value;
    state.station.role  = document.getElementById("f-role").value.trim();
    state.station.radio = document.getElementById("f-radio").value.trim();
    state.station.callsign_must_be_set = document.getElementById("f-callsign-must").checked;
    state.boot.start_current_presence = document.getElementById("f-boot-start").checked;
    state.boot.delay_seconds = parseInt(document.getElementById("f-boot-delay").value, 10) || 0;
}


// ── Presence rendering ───────────────────────────────────────────────────────
function renderPresenceList() {
    const root = document.getElementById("presence-list");
    root.innerHTML = "";
    state.presence.forEach((p, i) => root.appendChild(renderPresence(i, p)));
}

function renderPresence(idx, p) {
    const div = document.createElement("div");
    div.className = "row-card";
    const modeInfo = PRESENCE_MODES.find(m => m.id === p.mode) || PRESENCE_MODES[0];
    const showModem = modeInfo.modems.length > 0;
    const modemOpts = modeInfo.modems.map(m =>
        `<option value="${m.value}" ${p.modem === m.value ? "selected" : ""}>${m.label}</option>`
    ).join("");
    const modeOpts = PRESENCE_MODES.map(m =>
        `<option value="${m.id}" ${p.mode === m.id ? "selected" : ""}>${m.label}</option>`
    ).join("");

    div.innerHTML = `
      <div class="row-grid">
        <label><span>Mode</span>
          <select onchange="updPresence(${idx},'mode',this.value)">${modeOpts}</select>
        </label>
        <label class="${showModem ? '' : 'hidden'}"><span>Modem</span>
          <select onchange="updPresence(${idx},'modem',this.value)">
            <option value="">— select —</option>${modemOpts}
          </select>
        </label>
        <label><span>Window (HH:MM-HH:MM)</span>
          <input type="text" value="${p.window || ''}" placeholder="00:00-23:59"
                 onchange="updPresence(${idx},'window',this.value)">
        </label>
        <label><span>Band</span>
          <input type="text" value="${p.band || ''}" placeholder="40m"
                 onchange="updPresence(${idx},'band',this.value)">
        </label>
        <label><span>QSY kHz</span>
          <input type="number" step="0.1" value="${p.qsy_khz ?? ''}" placeholder="7078.0"
                 onchange="updPresence(${idx},'qsy_khz',parseFloat(this.value)||0)">
        </label>
        <label><span>Rig mode</span>
          <input type="text" value="${p.rig_mode || ''}" placeholder="PKTUSB"
                 onchange="updPresence(${idx},'rig_mode',this.value)">
        </label>
        <label><span>Rig BW (Hz, 0=default)</span>
          <input type="number" min="0" value="${p.rig_bw ?? 0}"
                 onchange="updPresence(${idx},'rig_bw',parseInt(this.value)||0)">
        </label>
        <label><span>Rig memory (CH#, blank if N/A)</span>
          <input type="number" min="1" value="${p.rig_memory ?? ''}"
                 onchange="updPresence(${idx},'rig_memory', this.value ? parseInt(this.value) : undefined)">
        </label>
      </div>
      ${renderQsyScheduleBlock(idx, p)}
      <div class="row-actions">
        <button class="btn btn-small btn-danger" onclick="delPresence(${idx})">× ${T.remove}</button>
      </div>
    `;
    return div;
}


// ── qsy_schedule sub-block (one per presence card) ──────────────────────────
function renderQsyScheduleBlock(presenceIdx, presence) {
    const schedule = presence.qsy_schedule || [];
    const rows = schedule.map((q, j) => `
      <div class="qsy-row">
        <input type="text" placeholder="HH:MM" value="${q.at || ''}"
               onchange="updQsy(${presenceIdx},${j},'at',this.value)">
        <input type="number" step="0.1" placeholder="Freq kHz" value="${q.qsy_khz ?? ''}"
               onchange="updQsy(${presenceIdx},${j},'qsy_khz',parseFloat(this.value)||0)">
        <input type="text" placeholder="band (40m)" value="${q.band || ''}"
               onchange="updQsy(${presenceIdx},${j},'band',this.value)">
        <input type="text" placeholder="rig_mode (PKTUSB)" value="${q.rig_mode || ''}"
               onchange="updQsy(${presenceIdx},${j},'rig_mode',this.value)">
        <input type="number" min="0" placeholder="BW" value="${q.rig_bw ?? ''}"
               onchange="updQsy(${presenceIdx},${j},'rig_bw',parseInt(this.value))">
        <button class="btn btn-small btn-danger" onclick="delQsy(${presenceIdx},${j})">×</button>
      </div>
    `).join("");
    const label = (LANG === "fr")
        ? "QSY planifié (réglages de la station courante uniquement)"
        : "QSY Schedule (re-tune events while this presence is active)";
    return `
      <div class="qsy-block">
        <div class="qsy-header">
          <strong>${label}</strong>
          <button class="btn btn-small" onclick="addQsy(${presenceIdx})">+ QSY</button>
        </div>
        ${rows || `<p class="helptext">${
          (LANG === "fr")
            ? "Aucun QSY planifié. Ajoutez une entrée pour changer de fréquence à une heure précise pendant cette présence."
            : "No scheduled QSY yet. Add one to re-tune the rig at a specific time while this presence runs."
        }</p>`}
      </div>
    `;
}

function addQsy(presenceIdx) {
    const p = state.presence[presenceIdx];
    if (!p.qsy_schedule) p.qsy_schedule = [];
    p.qsy_schedule.push({ at: "12:00", qsy_khz: 14078.0, band: "20m" });
    renderPresenceList();
}
function delQsy(presenceIdx, qsyIdx) {
    state.presence[presenceIdx].qsy_schedule.splice(qsyIdx, 1);
    if (state.presence[presenceIdx].qsy_schedule.length === 0)
        delete state.presence[presenceIdx].qsy_schedule;
    renderPresenceList();
}
function updQsy(presenceIdx, qsyIdx, key, value) {
    const ev = state.presence[presenceIdx].qsy_schedule[qsyIdx];
    if (value === undefined || value === "" || (typeof value === "number" && isNaN(value))) {
        delete ev[key];
    } else {
        ev[key] = value;
    }
}


function addPresence() {
    state.presence.push({
        window: "00:00-23:59", mode: "js8call",
        band: "40m", qsy_khz: 7078.0,
        rig_mode: "PKTUSB", rig_bw: 0,
    });
    renderPresenceList();
}
function delPresence(idx) {
    if (!confirm(T.confirm_del_presence)) return;
    state.presence.splice(idx, 1);
    renderPresenceList();
}
function updPresence(idx, key, value) {
    if (value === undefined || value === "" || (typeof value === "number" && isNaN(value))) {
        delete state.presence[idx][key];
    } else {
        state.presence[idx][key] = value;
    }
    if (key === "mode") renderPresenceList();   // mode change can show/hide modem
}


// ── Mission rendering ────────────────────────────────────────────────────────
function renderMissionList() {
    const root = document.getElementById("mission-list");
    root.innerHTML = "";
    state.missions.forEach((m, i) => root.appendChild(renderMission(i, m)));
}

function renderMission(idx, m) {
    const div = document.createElement("div");
    div.className = "row-card";
    const modeOpts = MISSION_MODES.map(mm =>
        `<option value="${mm.id}" ${m.mode === mm.id ? "selected" : ""}>${mm.label}</option>`
    ).join("");
    const dayCBs = WEEKDAYS.map(d => {
        const checked = (m.days || WEEKDAYS).includes(d) ? "checked" : "";
        return `<label class="cb-inline">
          <input type="checkbox" value="${d}" ${checked}
                 onchange="updMissionDays(${idx})"> ${d.toUpperCase()}
        </label>`;
    }).join("");
    const onFailOpts = ["skip", "retry_same", "retry_alt"].map(v =>
        `<option value="${v}" ${m.on_fail === v ? "selected" : ""}>${v}</option>`
    ).join("");
    const altsBlock = (m.on_fail === "retry_alt")
        ? renderAltRmsBlock(idx, m.alt_rms || [])
        : "";

    div.innerHTML = `
      <div class="row-grid">
        <label><span>Name</span>
          <input type="text" value="${m.name || ''}" placeholder="Morning Winlink (40m)"
                 onchange="updMission(${idx},'name',this.value)">
        </label>
        <label><span>At (HH:MM, in schedule TZ)</span>
          <input type="text" value="${m.at || ''}" placeholder="14:30"
                 onchange="updMission(${idx},'at',this.value)">
        </label>
        <label class="span-2"><span>Days</span>
          <div class="cb-row" data-mission-days="${idx}">${dayCBs}</div>
        </label>
        <label><span>Mode</span>
          <select onchange="updMission(${idx},'mode',this.value); renderMissionList();">${modeOpts}</select>
        </label>
        <label><span>RMS callsign</span>
          <input type="text" value="${m.rms || ''}" placeholder="N0CALL"
                 onchange="updMission(${idx},'rms',this.value); rebuildConnectUrl(${idx});">
        </label>
        <label class="span-2"><span>Connect URL (auto-built; edit only if you know what you're doing)</span>
          <input type="text" value="${m.connect_url || ''}"
                 onchange="updMission(${idx},'connect_url',this.value)">
        </label>
        <label><span>Freq (kHz)</span>
          <input type="number" step="0.1" value="${freqFromConnectUrl(m.connect_url) ?? ''}"
                 onchange="updMissionFreq(${idx}, this.value)">
        </label>
        <label><span>BW (Hz)</span>
          <input type="number" value="${bwFromConnectUrl(m.connect_url) ?? 2300}"
                 onchange="updMissionBw(${idx}, this.value)">
        </label>
        <label><span>Rig mode</span>
          <input type="text" value="${m.rig_mode || ''}" placeholder="PKTUSB"
                 onchange="updMission(${idx},'rig_mode',this.value)">
        </label>
        <label><span>Rig BW (Hz)</span>
          <input type="number" min="0" value="${m.rig_bw ?? 0}"
                 onchange="updMission(${idx},'rig_bw',parseInt(this.value)||0)">
        </label>
        <label><span>Rig memory (CH#)</span>
          <input type="number" min="1" value="${m.rig_memory ?? ''}"
                 onchange="updMission(${idx},'rig_memory', this.value ? parseInt(this.value) : undefined)">
        </label>
        <label><span>Timeout (min)</span>
          <input type="number" min="1" value="${m.timeout_min ?? 10}"
                 onchange="updMission(${idx},'timeout_min',parseInt(this.value)||10)">
        </label>
        <label><span>On fail</span>
          <select onchange="updMission(${idx},'on_fail',this.value); renderMissionList();">${onFailOpts}</select>
        </label>
      </div>
      ${altsBlock}
      <div class="row-actions">
        <button class="btn btn-small" onclick="openRmsPicker(${idx})">🔍 ${T.pick_rms}</button>
        <button class="btn btn-small btn-danger" onclick="delMission(${idx})">× ${T.remove}</button>
      </div>
    `;
    return div;
}

function renderAltRmsBlock(missionIdx, alts) {
    const rows = alts.map((a, j) => `
      <div class="alt-row">
        <input type="text" placeholder="RMS callsign" value="${a.rms || ''}"
               onchange="updAlt(${missionIdx},${j},'rms',this.value); rebuildAltUrl(${missionIdx},${j});">
        <input type="number" step="0.1" placeholder="Freq kHz"
               value="${freqFromConnectUrl(a.connect_url) ?? ''}"
               onchange="updAltFreq(${missionIdx},${j},this.value)">
        <input type="number" placeholder="BW"
               value="${bwFromConnectUrl(a.connect_url) ?? 2300}"
               onchange="updAltBw(${missionIdx},${j},this.value)">
        <input type="text" placeholder="connect_url" style="flex:2;"
               value="${a.connect_url || ''}"
               onchange="updAlt(${missionIdx},${j},'connect_url',this.value)">
        <button class="btn btn-small btn-danger" onclick="delAlt(${missionIdx},${j})">×</button>
      </div>
    `).join("");
    return `
      <div class="alt-block">
        <div class="alt-header">
          <strong>${T.edit_alts}</strong>
          <button class="btn btn-small" onclick="addAlt(${missionIdx})">+ alt RMS</button>
        </div>
        ${rows || `<p class="helptext">No alt RMS yet — add one for fallback.</p>`}
      </div>
    `;
}

function addMission() {
    state.missions.push({
        name: "New mission", at: "12:00",
        days: [...WEEKDAYS],
        mode: "winlink-vara-hf",
        rms: "N0CALL",
        connect_url: "varahf:///N0CALL?freq=7102&bw=2300",
        rig_mode: "PKTUSB", rig_bw: 0,
        timeout_min: 10, on_fail: "skip",
    });
    renderMissionList();
}
function delMission(idx) {
    if (!confirm(T.confirm_del_mission)) return;
    state.missions.splice(idx, 1);
    renderMissionList();
}
function updMission(idx, key, value) {
    if (value === undefined || value === "") delete state.missions[idx][key];
    else state.missions[idx][key] = value;
}
function updMissionDays(idx) {
    const cbs = document.querySelectorAll(`[data-mission-days="${idx}"] input`);
    state.missions[idx].days = Array.from(cbs).filter(c => c.checked).map(c => c.value);
}
function addAlt(idx) {
    if (!state.missions[idx].alt_rms) state.missions[idx].alt_rms = [];
    state.missions[idx].alt_rms.push({rms: "N0CALL", connect_url: "varahf:///N0CALL?freq=7102&bw=2300"});
    renderMissionList();
}
function delAlt(idx, altIdx) {
    state.missions[idx].alt_rms.splice(altIdx, 1);
    renderMissionList();
}
function updAlt(idx, altIdx, key, value) {
    state.missions[idx].alt_rms[altIdx][key] = value;
}
function rebuildAltUrl(idx, altIdx) {
    const a = state.missions[idx].alt_rms[altIdx];
    const m = state.missions[idx];
    const scheme = schemeForMode(m.mode);
    const freq = freqFromConnectUrl(a.connect_url) ?? freqFromConnectUrl(m.connect_url) ?? 7102;
    const bw   = bwFromConnectUrl(a.connect_url) ?? bwFromConnectUrl(m.connect_url) ?? 2300;
    a.connect_url = buildUrl(scheme, a.rms, freq, bw);
    renderMissionList();
}
function updAltFreq(idx, altIdx, value) {
    const a = state.missions[idx].alt_rms[altIdx];
    const m = state.missions[idx];
    const scheme = schemeForMode(m.mode);
    const bw = bwFromConnectUrl(a.connect_url) ?? 2300;
    a.connect_url = buildUrl(scheme, a.rms || m.rms, parseFloat(value) || 0, bw);
    renderMissionList();
}
function updAltBw(idx, altIdx, value) {
    const a = state.missions[idx].alt_rms[altIdx];
    const m = state.missions[idx];
    const scheme = schemeForMode(m.mode);
    const freq = freqFromConnectUrl(a.connect_url) ?? 7102;
    a.connect_url = buildUrl(scheme, a.rms || m.rms, freq, parseInt(value) || 0);
    renderMissionList();
}


// ── connect_url helpers ──────────────────────────────────────────────────────
function schemeForMode(modeId) {
    const m = MISSION_MODES.find(x => x.id === modeId);
    return m ? m.scheme : "varahf";
}
function buildUrl(scheme, callsign, freqKhz, bw) {
    const cs = (callsign || "N0CALL").toUpperCase();
    const qs = [];
    if (freqKhz) qs.push(`freq=${freqKhz}`);
    if (bw)      qs.push(`bw=${bw}`);
    return `${scheme}:///${cs}` + (qs.length ? `?${qs.join("&")}` : "");
}
function freqFromConnectUrl(url) {
    if (!url) return null;
    const m = url.match(/[?&]freq=([\d.]+)/);
    return m ? parseFloat(m[1]) : null;
}
function bwFromConnectUrl(url) {
    if (!url) return null;
    const m = url.match(/[?&]bw=(\d+)/);
    return m ? parseInt(m[1]) : null;
}
function rebuildConnectUrl(idx) {
    const m = state.missions[idx];
    const scheme = schemeForMode(m.mode);
    const freq = freqFromConnectUrl(m.connect_url) ?? 7102;
    const bw   = bwFromConnectUrl(m.connect_url) ?? 2300;
    m.connect_url = buildUrl(scheme, m.rms, freq, bw);
    renderMissionList();
}
function updMissionFreq(idx, value) {
    const m = state.missions[idx];
    const scheme = schemeForMode(m.mode);
    const bw = bwFromConnectUrl(m.connect_url) ?? 2300;
    m.connect_url = buildUrl(scheme, m.rms, parseFloat(value) || 0, bw);
    renderMissionList();
}
function updMissionBw(idx, value) {
    const m = state.missions[idx];
    const scheme = schemeForMode(m.mode);
    const freq = freqFromConnectUrl(m.connect_url) ?? 7102;
    m.connect_url = buildUrl(scheme, m.rms, freq, parseInt(value) || 0);
    renderMissionList();
}


// ── RMS Picker ───────────────────────────────────────────────────────────────
// Cache of the most recent /api/rms-list result so the callsign filter can
// filter client-side without re-hitting pat (rmslist is a ~5-30s round trip).
let rmsLastList = [];

function openRmsPicker(missionIdx) {
    rmsPickerTarget = missionIdx;
    const m = state.missions[missionIdx];
    document.getElementById("rms-modem-filter").value = schemeForMode(m.mode);
    document.getElementById("rms-band-filter").value = "";
    const cf = document.getElementById("rms-call-filter");
    if (cf) cf.value = "";
    rmsLastList = [];
    document.getElementById("rms-tbody").innerHTML =
        `<tr><td colspan="7" class="rms-empty">${LANG === "fr" ? "Cliquez « Rafraîchir »." : "Click \"Refresh\"."}</td></tr>`;
    document.getElementById("rms-modal").style.display = "flex";
}
function closeRmsPicker() {
    rmsPickerTarget = null;
    document.getElementById("rms-modal").style.display = "none";
}
async function refreshRms() {
    const modem = document.getElementById("rms-modem-filter").value;
    const band  = document.getElementById("rms-band-filter").value;
    const tbody = document.getElementById("rms-tbody");
    tbody.innerHTML = `<tr><td colspan="7" class="rms-empty">…querying Pat…</td></tr>`;
    try {
        const r = await fetch(`/api/rms-list?modem=${encodeURIComponent(modem)}&band=${encodeURIComponent(band)}`);
        const data = await r.json();
        // Backend failure: show actual pat error inline so the operator
        // doesn't have to guess (no internet? pat not configured? wrong
        // --mode? old pat that doesn't speak --json?)
        if (!data.ok) {
            rmsLastList = [];
            tbody.innerHTML = `
              <tr><td colspan="7" class="rms-err">
                <strong>pat rmslist failed.</strong>
                <pre>${(data.error || "(no error message)").replace(/</g,"&lt;")}</pre>
                <small>command: <code>${(data.command || "").replace(/</g,"&lt;")}</code></small>
              </td></tr>`;
            return;
        }
        rmsLastList = data.rms || [];
        renderRmsRows();
    } catch (e) {
        rmsLastList = [];
        tbody.innerHTML = `<tr><td colspan="7" class="rms-empty">Error: ${e}</td></tr>`;
    }
}
// Re-render the table from rmsLastList, applying the callsign substring
// filter. Called by the callsign input's oninput handler and after refresh.
function renderRmsRows() {
    const tbody = document.getElementById("rms-tbody");
    const callFilterEl = document.getElementById("rms-call-filter");
    const needle = (callFilterEl ? callFilterEl.value : "").trim().toUpperCase();
    const rows = rmsLastList.filter(rms =>
        !needle || (rms.callsign || "").toUpperCase().includes(needle)
    );
    // Sort by distance ascending (closest first). Rows with no/unparseable
    // distance sink to the bottom — Infinity sentinel preserves their
    // pat-order among themselves.
    rows.sort((a, b) => rmsDistanceKm(a) - rmsDistanceKm(b));
    if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="rms-empty">${
            needle
                ? (LANG === "fr"
                    ? `Aucun RMS ne correspond à « ${needle} ».`
                    : `No RMS matches "${needle}".`)
                : T.no_rms
        }</td></tr>`;
        return;
    }
    tbody.innerHTML = rows.map(rms => `
      <tr>
        <td>${rms.callsign || "?"}</td>
        <td>${rms.band || rms.Band || ""}</td>
        <td>${freqDisplay(rms)}</td>
        <td>${rms.mode || rms.Mode || ""}</td>
        <td>${rms.bw || rms.Bandwidth || ""}</td>
        <td>${rms.distance || rms.Distance || ""}</td>
        <td><button class="btn btn-small" onclick='selectRms(${JSON.stringify(rms)})'>→</button></td>
      </tr>`).join("");
}
function filterRmsTable() {
    // Just re-render — keeps pat off the wire when the operator types.
    renderRmsRows();
}
function freqDisplay(rms) {
    const f = rms.freq || rms.Frequency || rms.freq_hz;
    if (!f) return "";
    // Pat returns Hz typically; show kHz with 1 decimal
    return (parseFloat(f) / 1000).toFixed(1);
}
function rmsDistanceKm(rms) {
    // Pull the leading number out of "1234 km" / "1234km" / "1234 mi".
    // Returns Infinity for missing/unparseable so those rows sort last.
    const s = (rms.distance || rms.Distance || "").trim();
    if (!s) return Infinity;
    const m = s.match(/(\d+(?:\.\d+)?)\s*(km|mi|miles)?/i);
    if (!m) return Infinity;
    const n = parseFloat(m[1]);
    if (!isFinite(n)) return Infinity;
    // Normalize miles → km so units mix cleanly.
    const unit = (m[2] || "km").toLowerCase();
    return (unit === "mi" || unit === "miles") ? n * 1.60934 : n;
}
function selectRms(rms) {
    if (rmsPickerTarget === null) return;
    const m = state.missions[rmsPickerTarget];
    const scheme = schemeForMode(m.mode);
    const callsign = rms.callsign || "N0CALL";
    const freqKhz  = (parseFloat(rms.freq || rms.Frequency || rms.freq_hz || 0) / 1000) || 7102;
    const bw       = parseInt(rms.bw || rms.Bandwidth || 2300);
    m.rms = callsign;
    m.connect_url = buildUrl(scheme, callsign, freqKhz, bw);
    closeRmsPicker();
    renderMissionList();
}


// ── Save / validate ──────────────────────────────────────────────────────────
function showToast(msg, kind) {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.className = `toast toast-${kind || "info"}`;
    t.style.display = "block";
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => { t.style.display = "none"; }, 4000);
}

async function validateOnly() {
    syncTopForm();
    try {
        const r = await fetch("/api/validate", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({schedule: state}),
        });
        const data = await r.json();
        document.getElementById("validator-output").textContent = data.output || "(no output)";
        showToast(data.ok ? T.validate_ok : T.validate_failed, data.ok ? "ok" : "err");
    } catch (e) {
        showToast(`Error: ${e}`, "err");
    }
}

async function saveSchedule(doRestart) {
    syncTopForm();
    try {
        const r = await fetch("/api/save", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({schedule: state, restart: !!doRestart}),
        });
        const data = await r.json();
        document.getElementById("validator-output").textContent =
            data.validator_output || data.error || "(no output)";
        if (!data.ok) {
            showToast(`${T.save_failed}: ${data.error || ""}`, "err");
            return;
        }
        let msg = T.saved;
        if (doRestart && data.restarted) msg += "  " + T.restarted;
        showToast(msg, "ok");
    } catch (e) {
        showToast(`Error: ${e}`, "err");
    }
}


// ── Bootstrap ────────────────────────────────────────────────────────────────
loadTopForm();
renderPresenceList();
renderMissionList();

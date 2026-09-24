const API = "/api";
let charts = {};
let assetsCache = [];
let currentStatusFilter = "";

// -------------------------------------------------------------- helpers --
function friendlyFetchError(err, path) {
  const msg = (err && err.message) ? String(err.message) : String(err);
  if (msg === "Failed to fetch" || msg.includes("NetworkError") || msg.includes("Network request failed")) {
    return `Cannot reach server (${path || "API"}). Is the app still running? Try http://127.0.0.1:5050`;
  }
  return msg;
}

async function apiGet(path) {
  try {
    const res = await fetch(API + path);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || res.statusText || `HTTP ${res.status}`);
    }
    return res.json();
  } catch (e) {
    throw new Error(friendlyFetchError(e, path));
  }
}

async function apiPost(path, body) {
  try {
    const res = await fetch(API + path, {
      method: "POST",
      headers: body instanceof FormData ? undefined : { "Content-Type": "application/json" },
      body: body instanceof FormData ? body : JSON.stringify(body || {}),
    });
    if (!res.ok) {
      const bodyJson = await res.json().catch(() => ({}));
      throw new Error(bodyJson.error || res.statusText || `HTTP ${res.status}`);
    }
    return res.json();
  } catch (e) {
    throw new Error(friendlyFetchError(e, path));
  }
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.style.borderColor = isError ? "var(--accent-danger)" : "var(--panel-border-hi)";
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 4000);
}

function fmtDate(d) {
  return d ? new Date(d).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) : "—";
}

function priorityClass(score) {
  if (score == null) return "p-mid";
  if (score >= 65) return "p-high";
  if (score >= 40) return "p-mid";
  return "p-low";
}

const CHART_COLORS = ["#6c5ce7", "#74b9ff", "#fdcb6e", "#ff7675", "#00b894", "#a29bfe", "#e17055"];

function destroyChart(key) {
  if (charts[key]) { charts[key].destroy(); delete charts[key]; }
}

function ensureChart() {
  if (typeof Chart === "undefined") {
    console.error("Chart.js is not loaded");
    toast("Chart library missing — hard-refresh the page (Ctrl+Shift+R)", true);
    return false;
  }
  return true;
}

function makeChart(key, canvasId, config) {
  if (!ensureChart()) return null;
  const el = document.getElementById(canvasId);
  if (!el) return null;
  destroyChart(key);
  charts[key] = new Chart(el, config);
  return charts[key];
}

function baseChartOptions(extra = {}) {
  return Object.assign({
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: "#475569", font: { family: "Inter", size: 11 } } },
      tooltip: { backgroundColor: "#0f172a", borderColor: "rgba(15,23,42,0.1)", borderWidth: 1, titleColor: "#fff", bodyColor: "#e2e8f0" },
    },
    scales: {
      x: { ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { color: "rgba(15,23,42,0.05)" } },
      y: { ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { color: "rgba(15,23,42,0.05)" } },
    },
  }, extra);
}

// -------------------------------------------------------------- dashboard --
async function loadDashboard() {
  const d = await apiGet("/dashboard");
  document.getElementById("kpiAssets").textContent = d.total_assets;
  document.getElementById("kpiAssetsSub").textContent = `${d.degraded_assets} degraded / out of service`;
  document.getElementById("kpiPending").textContent = d.pending_requests;
  document.getElementById("kpiOptimized").textContent = d.optimized_requests;
  document.getElementById("kpiApproved").textContent = d.approved_requests;
  document.getElementById("kpiConflicts").textContent = d.conflicts;
  document.getElementById("kpiCondition").textContent = d.avg_condition_score;

  makeChart("status", "chartStatus", {
    type: "doughnut",
    data: {
      labels: d.by_status.map(r => r.status),
      datasets: [{ data: d.by_status.map(r => r.count), backgroundColor: CHART_COLORS, borderWidth: 0 }],
    },
    options: baseChartOptions({ scales: undefined, cutout: "62%" }),
  });

  makeChart("trend", "chartTrend", {
    type: "bar",
    data: {
      labels: d.severity_trend.map(r => r.reported_date),
      datasets: [
        { type: "bar", label: "Requests", data: d.severity_trend.map(r => r.count), backgroundColor: "rgba(108,92,231,0.45)", borderRadius: 6, yAxisID: "y" },
        { type: "line", label: "Avg severity", data: d.severity_trend.map(r => r.avg_severity), borderColor: "#e17055", backgroundColor: "#e17055", tension: 0.35, yAxisID: "y1" },
      ],
    },
    options: baseChartOptions({
      scales: {
        x: { ticks: { color: "#94a3b8", font: { size: 9 } }, grid: { display: false } },
        y: { position: "left", ticks: { color: "#94a3b8" }, grid: { color: "rgba(15,23,42,0.05)" } },
        y1: { position: "right", min: 0, max: 5, ticks: { color: "#94a3b8" }, grid: { display: false } },
      },
    }),
  });

  makeChart("lineChart", "chartLine", {
    type: "bar",
    data: { labels: d.by_line.map(r => r.line), datasets: [{ data: d.by_line.map(r => r.count), backgroundColor: CHART_COLORS, borderRadius: 6 }] },
    options: baseChartOptions({ plugins: { legend: { display: false } } }),
  });

  makeChart("assetType", "chartAssetType", {
    type: "bar",
    data: { labels: d.by_asset_type.map(r => r.asset_type), datasets: [{ data: d.by_asset_type.map(r => r.count), backgroundColor: CHART_COLORS, borderRadius: 6 }] },
    options: baseChartOptions({ plugins: { legend: { display: false } }, indexAxis: "y" }),
  });

  makeChart("condition", "chartCondition", {
    type: "bar",
    data: { labels: d.condition_by_line.map(r => r.line), datasets: [{ data: d.condition_by_line.map(r => Math.round(r.avg_condition)), backgroundColor: "#00b894", borderRadius: 6 }] },
    options: baseChartOptions({ plugins: { legend: { display: false } }, scales: { y: { min: 0, max: 100, ticks: { color: "#94a3b8" }, grid: { color: "rgba(15,23,42,0.05)" } }, x: { ticks: { color: "#94a3b8" }, grid: { display: false } } } }),
  });
}

// -------------------------------------------------------------- requests --
function renderExplainRow(req) {
  const b = req.priority_breakdown;
  let breakdownHtml = "";
  if (b && typeof b === "object") {
    breakdownHtml = Object.entries(b).filter(([k]) => k !== "total").map(([k, v]) =>
      `<span class="m" style="display:inline-block;margin:2px 6px 2px 0;">${k.replace(/_/g, " ")}: <b style="font-size:.8rem">${v.points}pt</b></span>`
    ).join("");
  }
  return `
    <div><strong>Explanation:</strong> ${req.explanation || "Not yet optimized."}</div>
    ${breakdownHtml ? `<div style="margin-top:8px;">${breakdownHtml}</div>` : ""}
    ${req.conflict ? `<div class="conflict-tag">⚠ Conflict: ${req.conflict}</div>` : ""}
  `;
}

async function loadRequests() {
  const path = currentStatusFilter ? `/requests?status=${encodeURIComponent(currentStatusFilter)}` : "/requests";
  const rows = await apiGet(path);
  const tbody = document.getElementById("requestsBody");
  tbody.innerHTML = "";

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-state">No maintenance requests in this view.</td></tr>`;
    return;
  }

  rows.forEach(req => {
    const tr = document.createElement("tr");
    const window = req.recommended_date
      ? `${fmtDate(req.recommended_date)}<br><span class="asset-meta">${req.recommended_start}–${req.recommended_end}</span>`
      : `<span class="asset-meta">not scheduled</span>`;

    tr.innerHTML = `
      <td><button class="expand-btn" data-id="${req.id}">▸</button></td>
      <td><div class="asset-name">${req.asset_name}</div><div class="asset-meta">${req.asset_type} · ${req.line}</div></td>
      <td>${req.title}<div class="asset-meta">severity ${req.defect_severity}/5 · ${req.duration_minutes}min</div></td>
      <td>${req.priority_score != null ? `<span class="priority-pill ${priorityClass(req.priority_score)}">${req.priority_score}</span>` : "—"}</td>
      <td>${window}</td>
      <td>${req.resource_name || "—"}</td>
      <td>${req.disruption_score != null ? req.disruption_score : "—"}</td>
      <td><span class="status-pill status-${req.status}">${req.status}</span>${req.conflict ? '<div class="conflict-tag">⚠ conflict</div>' : ""}</td>
      <td class="row-actions">
        ${req.status === "Optimized" ? `<button class="btn btn-primary" data-approve="${req.id}">Approve</button>` : ""}
        ${req.status === "Pending" ? `<button class="btn btn-primary" data-approve="${req.id}" title="Will optimize then approve if needed">Approve</button>` : ""}
        ${["Pending", "Optimized"].includes(req.status) ? `<button class="btn btn-ghost" data-reject="${req.id}">Reject</button>` : ""}
        ${req.status === "Approved" ? `<button class="btn btn-outline" data-complete="${req.id}">Mark done</button>` : ""}
      </td>
    `;
    tbody.appendChild(tr);

    const explainTr = document.createElement("tr");
    explainTr.className = "explain-row";
    explainTr.id = `explain-${req.id}`;
    const td = document.createElement("td");
    td.colSpan = 9;
    td.innerHTML = renderExplainRow(req);
    explainTr.appendChild(td);
    tbody.appendChild(explainTr);
  });

  tbody.querySelectorAll("[data-id]").forEach(btn => {
    btn.addEventListener("click", () => {
      const row = document.getElementById(`explain-${btn.dataset.id}`);
      row.classList.toggle("show");
      btn.textContent = row.classList.contains("show") ? "▾" : "▸";
    });
  });
  tbody.querySelectorAll("[data-approve]").forEach(btn =>
    btn.addEventListener("click", () => actOnRequest(btn.dataset.approve, "approve")));
  tbody.querySelectorAll("[data-reject]").forEach(btn =>
    btn.addEventListener("click", () => actOnRequest(btn.dataset.reject, "reject")));
  tbody.querySelectorAll("[data-complete]").forEach(btn =>
    btn.addEventListener("click", () => actOnRequest(btn.dataset.complete, "complete")));
}

async function actOnRequest(id, action) {
  try {
    await apiPost(`/requests/${id}/${action}`);
    toast(`Request #${id} ${action === "complete" ? "marked complete" : action + "d"}.`);
    await Promise.all([loadRequests(), loadDashboard()]);
  } catch (e) {
    toast(e.message, true);
  }
}

// ------------------------------------------------------------- schedules --
async function loadScheduleLines() {
  const assets = await apiGet("/assets");
  assetsCache = assets;
  const lines = [...new Set(assets.map(a => a.line))];
  const sel = document.getElementById("scheduleLine");
  sel.innerHTML = lines.map(l => `<option value="${l}">${l}</option>`).join("");
  sel.addEventListener("change", () => loadSchedule(sel.value));
  await loadSchedule(lines[0]);

  const formAsset = document.getElementById("formAsset");
  formAsset.innerHTML = assets.map(a => `<option value="${a.id}">${a.name} (${a.line})</option>`).join("");
  const inspectAsset = document.getElementById("inspectAsset");
  inspectAsset.innerHTML = `<option value="">No asset (demo sample)</option>` +
    assets.map(a => `<option value="${a.id}">${a.name}</option>`).join("");
}

async function loadSchedule(line) {
  const tbody = document.getElementById("scheduleBody");
  try {
    const rows = await apiGet(`/schedules?line=${encodeURIComponent(line)}`);
    tbody.innerHTML = rows.map(r => `
      <tr><td>${r.train_no}</td><td>${r.departure}</td><td>${r.arrival}</td><td>${r.priority}</td></tr>
    `).join("") || `<tr><td colspan="4" class="empty-state">No scheduled trains.</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-state">${friendlyFetchError(e, "schedules")}</td></tr>`;
  }
}

// -------------------------------------------------------------- activity --
async function loadActivity() {
  const rows = await apiGet("/activity");
  const list = document.getElementById("activityList");
  list.innerHTML = rows.map(r => `
    <li><strong>${r.actor}</strong> — ${r.action.replace(/_/g, " ")}<br>${r.details || ""}
      <span class="a-time">${new Date(r.ts).toLocaleString()}</span></li>
  `).join("") || `<li class="empty-state">No activity yet.</li>`;
}

// ---------------------------------------------------------------- model --
async function loadModelStatus() {
  const s = await apiGet("/model/status");
  const badge = document.getElementById("modelBadge");
  const metrics = document.getElementById("modelMetrics");
  if (!s.trained) {
    badge.textContent = "not trained";
    metrics.innerHTML = "";
    return;
  }
  badge.textContent = s.source === "kaggle" ? "trained · kaggle" : "trained · fallback data";
  badge.className = "badge" + (s.source === "kaggle" ? "" : " warn");
  metrics.innerHTML = `
    <span class="m">Accuracy<br><b>${(s.accuracy * 100).toFixed(1)}%</b></span>
    <span class="m">F1<br><b>${(s.f1 * 100).toFixed(1)}%</b></span>
    <span class="m">Samples<br><b>${s.n_samples}</b></span>
    <span class="m">Trained<br><b>${new Date(s.trained_at).toLocaleTimeString()}</b></span>
  `;
}

// ------------------------------------------------------------- rail data --
async function loadIRStatus() {
  const s = await apiGet("/rail-data/status");
  const badge = document.getElementById("irBadge");
  const metrics = document.getElementById("irMetrics");
  if (!s.loaded) {
    badge.textContent = "not loaded";
    metrics.innerHTML = "";
    renderStationCharts([], []);
    return;
  }
  badge.textContent = s.source === "kaggle" ? "live · kaggle" : "fallback subset";
  badge.className = "badge" + (s.source === "kaggle" ? "" : " warn");
  metrics.innerHTML = `
    <span class="m">Stations<br><b>${s.n_stations}</b></span>
    <span class="m">Schedule stops<br><b>${s.n_schedule_stops}</b></span>
    ${s.traffic_classifier && s.traffic_classifier.trained
      ? `<span class="m">Classifier acc.<br><b>${(s.traffic_classifier.accuracy * 100).toFixed(1)}%</b></span>` : ""}
  `;
  renderStationCharts(s.top_stations || [], s.zone_summary || []);
  await loadStationsTable();
}

function renderStationCharts(topStations, zoneSummary) {
  makeChart("stations", "chartStations", {
    type: "bar",
    data: {
      labels: topStations.map(s => s.code),
      datasets: [{ label: "Train stops", data: topStations.map(s => s.stop_count), backgroundColor: "#6c5ce7", borderRadius: 6 }],
    },
    options: baseChartOptions({ plugins: { legend: { display: false } } }),
  });

  makeChart("zones", "chartZones", {
    type: "doughnut",
    data: { labels: zoneSummary.map(z => z.zone || "Unknown"), datasets: [{ data: zoneSummary.map(z => z.stop_count), backgroundColor: CHART_COLORS, borderWidth: 0 }] },
    options: baseChartOptions({ scales: undefined, cutout: "58%" }),
  });
}

async function loadStationsTable(search = "") {
  const rows = await apiGet(`/rail-data/stations${search ? `?search=${encodeURIComponent(search)}` : ""}`);
  const tbody = document.getElementById("stationsBody");
  tbody.innerHTML = rows.map(r => `
    <tr><td>${r.code}</td><td>${r.name}</td><td>${r.state || "—"}</td><td>${r.zone || "—"}</td><td>${r.stop_count}</td></tr>
  `).join("") || `<tr><td colspan="5" class="empty-state">No stations loaded yet — click "Load real network data".</td></tr>`;
}

// ------------------------------------------------------------------ init --
function wireEvents() {
  document.getElementById("btnOptimize").addEventListener("click", async () => {
    try {
      const r = await apiPost("/optimize");
      toast(`Optimized ${r.optimized} request(s).`);
      await Promise.all([loadRequests(), loadDashboard()]);
      await loadActivity();
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById("btnReoptimize").addEventListener("click", async () => {
    try {
      const r = await apiPost("/reoptimize");
      toast(`Re-optimized ${r.optimized} request(s).`);
      await Promise.all([loadRequests(), loadDashboard()]);
      await loadActivity();
    } catch (e) { toast(e.message, true); }
  });

  document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      currentStatusFilter = tab.dataset.status;
      loadRequests();
    });
  });

  document.getElementById("btnNewRequest").addEventListener("click", () => document.getElementById("modalBackdrop").classList.add("show"));
  document.getElementById("modalClose").addEventListener("click", closeModal);
  document.getElementById("formCancel").addEventListener("click", closeModal);
  document.getElementById("modalBackdrop").addEventListener("click", e => { if (e.target.id === "modalBackdrop") closeModal(); });

  document.getElementById("requestForm").addEventListener("submit", async e => {
    e.preventDefault();
    try {
      await apiPost("/requests", {
        asset_id: parseInt(document.getElementById("formAsset").value),
        title: document.getElementById("formTitle").value,
        description: document.getElementById("formDescription").value,
        defect_severity: parseInt(document.getElementById("formSeverity").value),
        duration_minutes: parseInt(document.getElementById("formDuration").value),
      });
      toast("Request submitted.");
      closeModal();
      document.getElementById("requestForm").reset();
      await Promise.all([loadRequests(), loadDashboard()]);
      await loadActivity();
    } catch (e2) { toast(e2.message, true); }
  });

  document.getElementById("btnTrainModel").addEventListener("click", async () => {
    const btn = document.getElementById("btnTrainModel");
    btn.disabled = true; btn.textContent = "Training…";
    try {
      const m = await apiPost("/model/train");
      toast(`Model trained (${m.source}) — accuracy ${(m.accuracy * 100).toFixed(1)}%`);
      await loadModelStatus();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = "Train / retrain model"; }
  });

  document.getElementById("btnInspect").addEventListener("click", async () => {
    const fileInput = document.getElementById("inspectFile");
    const assetId = document.getElementById("inspectAsset").value;
    const fd = new FormData();
    if (fileInput.files[0]) fd.append("image", fileInput.files[0]);
    if (assetId) fd.append("asset_id", assetId);
    try {
      const r = await apiPost("/inspect", fd);
      const box = document.getElementById("inspectResult");
      box.classList.add("show");
      box.innerHTML = `
        <div>Predicted: <strong>${r.predicted_label}</strong></div>
        <div class="prob ${r.defect_probability >= 0.5 ? "high" : ""}">${(r.defect_probability * 100).toFixed(1)}%</div>
        <div class="asset-meta">defect probability</div>
      `;
      toast("Inspection complete.");
      if (assetId) { await loadRequests(); await loadDashboard(); }
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById("btnLoadIR").addEventListener("click", async () => {
    const btn = document.getElementById("btnLoadIR");
    btn.disabled = true; btn.textContent = "Loading…";
    try {
      const s = await apiPost("/rail-data/load");
      toast(`Loaded ${s.n_stations} stations (${s.source}).`);
      await loadIRStatus();
      await loadActivity();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = "Load real network data"; }
  });

  let searchTimer;
  document.getElementById("stationSearch").addEventListener("input", e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadStationsTable(e.target.value), 250);
  });
}

function closeModal() { document.getElementById("modalBackdrop").classList.remove("show"); }

async function safe(label, fn) {
  try {
    await fn();
  } catch (e) {
    console.error(`[${label}]`, e);
    toast(friendlyFetchError(e, label), true);
  }
}

async function init() {
  const todayEl = document.getElementById("todayLabel");
  if (todayEl) {
    todayEl.textContent = new Date().toLocaleDateString(undefined, {
      weekday: "long", year: "numeric", month: "long", day: "numeric",
    });
  }
  wireEvents();
  // Load each section independently so one failure does not blank the whole page
  await Promise.all([
    safe("dashboard", loadDashboard),
    safe("requests", loadRequests),
    safe("schedules", loadScheduleLines),
    safe("activity", loadActivity),
    safe("model", loadModelStatus),
    safe("rail-data", loadIRStatus),
  ]);
}

document.addEventListener("DOMContentLoaded", init);

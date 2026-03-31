// ── DOM refs ──────────────────────────────────────────────────────────────────
const envBadge           = document.getElementById("envBadge");
const envStatusValue     = document.getElementById("envStatusValue");
const accountEmail       = document.getElementById("accountEmail");
const logoutBtn          = document.getElementById("logoutBtn");
const fileInput          = document.getElementById("fileInput");
const includeDetails     = document.getElementById("includeDetails");
const fetchBtn           = document.getElementById("fetchBtn");
const createBtn          = document.getElementById("createBtn");
const planBtn            = document.getElementById("planBtn");
const applyBtn           = document.getElementById("applyBtn");
const copyLogsBtn        = document.getElementById("copyLogsBtn");
const clearLogsBtn       = document.getElementById("clearLogsBtn");
const zoneSearchInput    = document.getElementById("zoneSearchInput");
const zoneSortSelect     = document.getElementById("zoneSortSelect");
const updatesSearchInput = document.getElementById("updatesSearchInput");
const lastActionValue    = document.getElementById("lastActionValue");
const sessionStatusValue = document.getElementById("sessionStatusValue");
const zonesCountValue    = document.getElementById("zonesCountValue");
const updatesCountValue  = document.getElementById("updatesCountValue");
const zoneResultsMeta    = document.getElementById("zoneResultsMeta");
const updatesResultsMeta = document.getElementById("updatesResultsMeta");
const toastContainer     = document.getElementById("toastContainer");
const logsEl             = document.getElementById("logs");
const summaryEl          = document.getElementById("summary");
const zonesTableBody     = document.querySelector("#zonesTable tbody");
const updatesTableBody   = document.querySelector("#updatesTable tbody");
const confirmModal       = document.getElementById("confirmModal");
const confirmTitle       = document.getElementById("confirmTitle");
const confirmMessage     = document.getElementById("confirmMessage");
const confirmOkBtn       = document.getElementById("confirmOkBtn");
const confirmCancelBtn   = document.getElementById("confirmCancelBtn");

// ── Storage keys ──────────────────────────────────────────────────────────────
const COOKIE_STORAGE_KEY = "dzm.cookie";
const ENV_STORAGE_KEY    = "dzm.env";
const EMAIL_STORAGE_KEY  = "dzm.email";
const MAX_TOASTS         = 4;


// ── App state ─────────────────────────────────────────────────────────────────
let currentEnv = "sit";
const appState = { zones: [], updates: [] };

// ── Local storage ─────────────────────────────────────────────────────────────
function storageGet(key)        { try { return localStorage.getItem(key);    } catch (_) { return null; } }
function storageSet(key, value) { try { localStorage.setItem(key, value);    } catch (_) {} }
function storageRemove(key)     { try { localStorage.removeItem(key);        } catch (_) {} }

// ── Cookie — read from localStorage, never from UI ───────────────────────────
function getStoredCookie() {
  return String(storageGet(COOKIE_STORAGE_KEY) || "").trim();
}

function requireCookie() {
  const cookie = getStoredCookie();
  if (!cookie) {
    showToast("Session expired. Please log in again.", "error", 4000);
    setTimeout(() => { window.location.href = "/login"; }, 1500);
    throw new Error("No session found. Redirecting to login…");
  }
  return cookie;
}

function requireFile() {
  const file = fileInput.files && fileInput.files[0];
  if (!file) throw new Error("Please select a CSV/XLS/XLSX file.");
  return file;
}

// ── Account display ───────────────────────────────────────────────────────────
function initAccountDisplay() {
  const cookie = getStoredCookie();
  const email  = storageGet(EMAIL_STORAGE_KEY) || "";

  if (!cookie) {
    // No session — redirect to login
    window.location.href = "/login";
    return;
  }

  accountEmail.textContent = email || "Logged in";
  setStatusValue(sessionStatusValue, "Active", "status-ok");

  // Logout: clear session and go to login
  logoutBtn.addEventListener("click", (e) => {
    e.preventDefault();
    storageRemove(COOKIE_STORAGE_KEY);
    storageRemove(EMAIL_STORAGE_KEY);
    storageRemove("dzm.cookie.remember");
    window.location.href = "/login";
  });
}

// ── Logging ───────────────────────────────────────────────────────────────────
function log(message) {
  const timestamp = new Date().toLocaleTimeString("en-IN", { hour12: false });
  if (!logsEl.textContent.trim() || logsEl.textContent === "Ready.") {
    logsEl.textContent = `[${timestamp}] ${message}`;
  } else {
    logsEl.textContent += `\n[${timestamp}] ${message}`;
  }
  logsEl.scrollTop = logsEl.scrollHeight;
}

function clearLogs(text = "Running...") {
  logsEl.textContent = text;
}

// ── Summary ───────────────────────────────────────────────────────────────────
function setSummaryText(text) {
  summaryEl.className = "summary muted";
  summaryEl.textContent = text || "No summary.";
}

function setSummaryRows(rows) {
  summaryEl.className = "summary";
  const html = rows
    .map(({ label, value, type }) => {
      const cls = type ? ` summary-value-${type}` : "";
      return `<div class="summary-row">
        <span class="summary-row-label">${escapeHtml(label)}</span>
        <span class="summary-row-value${cls}">${escapeHtml(String(value))}</span>
      </div>`;
    })
    .join("");
  summaryEl.innerHTML = `<div class="summary-grid">${html}</div>`;
}

// ── Status values ─────────────────────────────────────────────────────────────
function setStatusValue(node, value, stateClass = "") {
  node.textContent = value;
  node.classList.remove("status-ok", "status-warn", "status-error");
  if (stateClass) node.classList.add(stateClass);
}

function setLastAction(value, stateClass = "")    { setStatusValue(lastActionValue,    value, stateClass); }
function setSessionStatus(value, stateClass = "") { setStatusValue(sessionStatusValue, value, stateClass); }
function setZonesCount(value)                     { setStatusValue(zonesCountValue,    String(value)); }
function setUpdatesCount(value)                   { setStatusValue(updatesCountValue,  String(value)); }

function setMetaText(node, text) {
  if (node) node.textContent = text;
}

// ── Environment ───────────────────────────────────────────────────────────────
const envMeta = {}; // populated by loadEnvironments()

function applyEnv(env) {
  currentEnv = env;
  storageSet(ENV_STORAGE_KEY, env);

  const label = env.toUpperCase();
  envStatusValue.textContent = label;
  envBadge.textContent       = label;
  envBadge.className         = `pill pill-env pill-${env}`;

  // Update env type pill in hero
  const typePill = document.getElementById("envTypePill");
  if (typePill) {
    const type = envMeta[env] && envMeta[env].type;
    if (type) {
      typePill.textContent = type;
      typePill.hidden = false;
    } else {
      typePill.hidden = true;
    }
  }

  // Update env api hint
  const hint = document.getElementById("envApiHint");
  if (hint && envMeta[env]) {
    hint.textContent = envMeta[env].type ? `(${envMeta[env].type})` : "";
  }

  // Sync app-level env tabs
  document.querySelectorAll("#appEnvTabs .env-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.env === env);
  });
}

function loadEnvironments() {
  fetch("/api/environments")
    .then((r) => r.json())
    .then(({ environments }) => {
      if (!Array.isArray(environments)) return;
      for (const e of environments) envMeta[e.key] = e;
      // Re-apply current env to populate type pill now that meta is loaded
      applyEnv(currentEnv);
    })
    .catch(() => {});
}

// Wire up app-level env tab clicks
document.querySelectorAll("#appEnvTabs .env-tab").forEach((tab) => {
  tab.addEventListener("click", () => applyEnv(tab.dataset.env));
});

// ── Toast ─────────────────────────────────────────────────────────────────────
function showToast(message, type = "info", timeoutMs = 3200) {
  const active = toastContainer.querySelectorAll(".toast");
  if (active.length >= MAX_TOASTS) active[0].remove();
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => toast.remove(), timeoutMs);
}

// ── Confirm modal ─────────────────────────────────────────────────────────────
function showConfirm(title, message) {
  return new Promise((resolve) => {
    confirmTitle.textContent   = title;
    confirmMessage.textContent = message;
    confirmModal.hidden        = false;

    function cleanup(result) {
      confirmModal.hidden = true;
      confirmOkBtn.removeEventListener("click", onOk);
      confirmCancelBtn.removeEventListener("click", onCancel);
      resolve(result);
    }
    function onOk()     { cleanup(true);  }
    function onCancel() { cleanup(false); }

    confirmOkBtn.addEventListener("click", onOk);
    confirmCancelBtn.addEventListener("click", onCancel);
  });
}

// ── Busy state ────────────────────────────────────────────────────────────────
function setBusy(isBusy) {
  [fetchBtn, createBtn, planBtn, applyBtn].forEach((btn) => {
    btn.disabled = isBusy;
  });
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function normalizeSearchText(value) {
  return String(value || "").trim().toLowerCase();
}

function formatShownMeta(shown, total, noun) {
  return `Showing ${shown} of ${total} ${total === 1 ? noun : noun + "s"}`;
}

function toSafeNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

// ── Zone filtering / sorting ──────────────────────────────────────────────────
function getZoneSearchBlob(zone) {
  return normalizeSearchText(
    [zone.name, zone.slug, zone.zoneId, zone.regionType,
     zone.countriesPreview, zone.storesPreview, zone.regionsPreview].join(" ")
  );
}

function getUpdateSearchBlob(u) {
  return normalizeSearchText(
    [u.slug, u.zoneId, (u.storeIds || []).join(","),
     u.oldRegionsCount, u.newRegionsCount].join(" ")
  );
}

function getFilteredSortedZones() {
  const query   = normalizeSearchText(zoneSearchInput.value);
  const sortKey = zoneSortSelect.value || "name_asc";
  let zones = [...appState.zones];

  if (query) zones = zones.filter((z) => getZoneSearchBlob(z).includes(query));

  zones.sort((a, b) => {
    if (sortKey === "stores_desc") {
      const diff = toSafeNumber(b.storesCount) - toSafeNumber(a.storesCount);
      return diff !== 0 ? diff : normalizeSearchText(a.name).localeCompare(normalizeSearchText(b.name));
    }
    if (sortKey === "regions_desc") {
      const diff = toSafeNumber(b.regionsCount) - toSafeNumber(a.regionsCount);
      return diff !== 0 ? diff : normalizeSearchText(a.name).localeCompare(normalizeSearchText(b.name));
    }
    const an = normalizeSearchText(a.name);
    const bn = normalizeSearchText(b.name);
    return sortKey === "name_desc" ? bn.localeCompare(an) : an.localeCompare(bn);
  });
  return zones;
}

function getFilteredUpdates() {
  const query = normalizeSearchText(updatesSearchInput.value);
  let updates = [...appState.updates];
  if (query) updates = updates.filter((u) => getUpdateSearchBlob(u).includes(query));
  return updates;
}

// ── Table rendering ───────────────────────────────────────────────────────────
function renderZonesTable(zones = [], emptyMessage = "No zones to display.") {
  if (!zones.length) {
    zonesTableBody.innerHTML = `<tr><td colspan="8" class="muted">${escapeHtml(emptyMessage)}</td></tr>`;
    return;
  }
  zonesTableBody.innerHTML = zones.map((z, i) => `
    <tr>
      <td>${i + 1}</td>
      <td>${escapeHtml(z.name || "-")}</td>
      <td>${escapeHtml(z.slug || "-")}</td>
      <td>${escapeHtml(z.zoneId || "-")}</td>
      <td>${escapeHtml(z.regionType || "N/A")}</td>
      <td title="${escapeHtml(z.storesPreview || "-")}">${z.storesCount}</td>
      <td title="${escapeHtml(z.regionsPreview || "-")}">${z.regionsCount}</td>
      <td title="${escapeHtml(z.countriesPreview || "-")}">${escapeHtml(z.countriesPreview || "-")}</td>
    </tr>`).join("");
}

function renderUpdatesTable(updates = [], emptyMessage = "No planned updates.") {
  if (!updates.length) {
    updatesTableBody.innerHTML = `<tr><td colspan="6" class="muted">${escapeHtml(emptyMessage)}</td></tr>`;
    return;
  }
  updatesTableBody.innerHTML = updates.map((u, i) => `
    <tr>
      <td>${i + 1}</td>
      <td>${escapeHtml(u.slug || "-")}</td>
      <td>${escapeHtml(u.zoneId || "-")}</td>
      <td>${escapeHtml((u.storeIds || []).join(", "))}</td>
      <td>${u.oldRegionsCount}</td>
      <td>${u.newRegionsCount}</td>
    </tr>`).join("");
}

function refreshZonesView() {
  const filtered = getFilteredSortedZones();
  const total    = appState.zones.length;
  renderZonesTable(filtered, total ? "No matching zones." : "No zones to display.");
  setMetaText(zoneResultsMeta, formatShownMeta(filtered.length, total, "zone"));
}

function refreshUpdatesView() {
  const filtered = getFilteredUpdates();
  const total    = appState.updates.length;
  renderUpdatesTable(filtered, total ? "No matching planned updates." : "No planned updates.");
  setMetaText(updatesResultsMeta, formatShownMeta(filtered.length, total, "planned update"));
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────
async function postJson(url, payload) {
  const res  = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function postForm(url, formData) {
  const res  = await fetch(url, { method: "POST", body: formData });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// ── Action runner ─────────────────────────────────────────────────────────────
async function runAction(action, options = {}) {
  const { actionLabel = "Action", clearLogFirst = true } = options;
  if (clearLogFirst) clearLogs(`Running: ${actionLabel}…`);
  setLastAction(`${actionLabel} in progress`, "status-warn");
  setBusy(true);
  try {
    const result = await action();
    if (result === false) {
      setLastAction(`${actionLabel} cancelled`, "status-warn");
      return;
    }
    setLastAction(`${actionLabel} completed`, "status-ok");
    showToast(`${actionLabel} completed`, "success");
  } catch (error) {
    log(`ERROR: ${error.message}`);
    setLastAction(`${actionLabel} failed`, "status-error");
    showToast(error.message || `${actionLabel} failed`, "error", 4200);
  } finally {
    setBusy(false);
  }
}

// ── Clipboard ─────────────────────────────────────────────────────────────────
async function copyLogsToClipboard() {
  const text = logsEl.textContent || "";
  if (!text.trim()) { showToast("Logs are empty.", "info"); return; }
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const el = document.createElement("textarea");
  el.value = text;
  el.style.cssText = "position:fixed;opacity:0";
  document.body.appendChild(el);
  el.select();
  document.execCommand("copy");
  document.body.removeChild(el);
}

// ── Event listeners ───────────────────────────────────────────────────────────
zoneSearchInput.addEventListener("input", refreshZonesView);
zoneSortSelect.addEventListener("change", refreshZonesView);
updatesSearchInput.addEventListener("input", refreshUpdatesView);

copyLogsBtn.addEventListener("click", () =>
  runAction(
    async () => { await copyLogsToClipboard(); log("Logs copied to clipboard."); },
    { actionLabel: "Copy Logs", clearLogFirst: false }
  )
);

clearLogsBtn.addEventListener("click", () => {
  clearLogs("Ready.");
  setLastAction("Logs cleared");
  showToast("Logs cleared.", "info");
});

fetchBtn.addEventListener("click", () =>
  runAction(async () => {
    const cookieString = requireCookie();
    log(`Fetching zones [${currentEnv.toUpperCase()}]…`);
    const data = await postJson("/api/fetch-zones", {
      cookieString,
      includeDetails: includeDetails.checked,
      env: currentEnv,
    });
    for (const line of data.logs || []) log(line);
    appState.zones = Array.isArray(data.zones) ? data.zones : [];
    setZonesCount(appState.zones.length);
    refreshZonesView();
    setSummaryRows([
      { label: "Environment",   value: currentEnv.toUpperCase() },
      { label: "Zones Fetched", value: data.total, type: data.total > 0 ? "ok" : "warn" },
    ]);
  }, { actionLabel: "Fetch Zones" })
);

createBtn.addEventListener("click", () =>
  runAction(async () => {
    const cookieString = requireCookie();
    const file         = requireFile();
    const formData     = new FormData();
    formData.append("cookieString", cookieString);
    formData.append("file", file);
    formData.append("env", currentEnv);

    log(`Creating zones from '${file.name}' [${currentEnv.toUpperCase()}]…`);
    const data = await postForm("/api/create-zones", formData);
    for (const line of data.logs || []) log(line);
    const s = data.summary || {};
    setSummaryRows([
      { label: "Environment", value: currentEnv.toUpperCase() },
      { label: "Total Rows",  value: s.totalRows ?? 0 },
      { label: "Created",     value: s.created   ?? 0, type: (s.created  ?? 0) > 0 ? "ok"     : "" },
      { label: "Skipped",     value: s.skipped   ?? 0, type: (s.skipped  ?? 0) > 0 ? "warn"   : "" },
      { label: "Failed",      value: s.failed    ?? 0, type: (s.failed   ?? 0) > 0 ? "danger" : "" },
    ]);
  }, { actionLabel: "Create Zones" })
);

planBtn.addEventListener("click", () =>
  runAction(async () => {
    const cookieString = requireCookie();
    const file         = requireFile();
    const formData     = new FormData();
    formData.append("cookieString", cookieString);
    formData.append("file", file);
    formData.append("env", currentEnv);

    log(`Planning updates from '${file.name}' [${currentEnv.toUpperCase()}]…`);
    const data = await postForm("/api/plan-updates", formData);
    for (const line of data.logs || []) log(line);
    const s = data.summary || {};
    appState.updates = Array.isArray(data.updates) ? data.updates : [];
    setUpdatesCount(appState.updates.length);
    refreshUpdatesView();
    setSummaryRows([
      { label: "Environment",     value: currentEnv.toUpperCase() },
      { label: "Total Rows",      value: s.totalRows      ?? 0 },
      { label: "Planned Updates", value: s.plannedUpdates ?? 0, type: (s.plannedUpdates ?? 0) > 0 ? "warn" : "ok" },
    ]);
  }, { actionLabel: "Plan Updates" })
);

applyBtn.addEventListener("click", () =>
  runAction(async () => {
    const cookieString = requireCookie();
    const file         = requireFile();

    const proceed = await showConfirm(
      "Apply Updates",
      `This will update zones on the ${currentEnv.toUpperCase()} server using '${file.name}'. This cannot be undone. Continue?`
    );
    if (!proceed) {
      log("Update cancelled by user.");
      showToast("Update cancelled.", "info");
      return false;
    }

    const formData = new FormData();
    formData.append("cookieString", cookieString);
    formData.append("file", file);
    formData.append("env", currentEnv);

    log(`Applying updates from '${file.name}' [${currentEnv.toUpperCase()}]…`);
    const data = await postForm("/api/apply-updates", formData);
    for (const line of data.logs || []) log(line);
    const s = data.summary || {};
    setSummaryRows([
      { label: "Environment",     value: currentEnv.toUpperCase() },
      { label: "Total Rows",      value: s.totalRows      ?? 0 },
      { label: "Planned Updates", value: s.plannedUpdates ?? 0 },
      { label: "Updated",         value: s.updated        ?? 0, type: (s.updated ?? 0) > 0 ? "ok"     : "" },
      { label: "Failed",          value: s.failed         ?? 0, type: (s.failed  ?? 0) > 0 ? "danger" : "" },
    ]);
  }, { actionLabel: "Apply Updates" })
);

// ── Version display (browser mode — Electron uses updater.js) ────────────────
if (!window.electronAPI) {
  fetch("/api/version")
    .then((r) => r.json())
    .then(({ version, buildNumber }) => {
      const pill = document.getElementById("appVersionPill");
      if (pill && version) {
        pill.textContent = buildNumber ? `v${version} (build ${buildNumber})` : `v${version}`;
      }
    })
    .catch(() => {});
}

// ── Init ──────────────────────────────────────────────────────────────────────
initAccountDisplay();
loadEnvironments();
applyEnv(storageGet(ENV_STORAGE_KEY) || "sit");
setLastAction("Idle");
setZonesCount(0);
setUpdatesCount(0);
refreshZonesView();
refreshUpdatesView();

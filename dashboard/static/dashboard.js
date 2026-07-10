/* GramAddict dashboard */

let ws = null;
let devices = [];
let selectedSerials = new Set();
let activeSerial = loadStoredActiveSerial();
let autoscroll = true;
let debugLogAutoscroll = true;
let debugLogSince = 0;
let debugLogPollTimer = null;
let weditorConnecting = false;
let deviceFilterSuffix = "";

let currentMainTab = "farm";
let currentAccountTab = localStorage.getItem("accountTab") || "basics";

let gaAccounts = [];
let gaCurrentAccountId = localStorage.getItem("gaAccountId") || "";
let gaSchema = null;
let gaFiltersSchema = null;
let gaTelegramSchema = null;
let gaPostReelSchema = null;
let gaFollowVisionSchema = null;
let gaFilesMeta = null;
let gaAccountRunning = false;
let gaFormLoading = false;
let gaFormLoadDepth = 0;
let advFileSnapshot = "";
const autosaveTimers = {};
const autosavePending = {};

function beginGaFormLoad() {
  gaFormLoadDepth += 1;
  gaFormLoading = true;
}

function endGaFormLoad() {
  gaFormLoadDepth = Math.max(0, gaFormLoadDepth - 1);
  gaFormLoading = gaFormLoadDepth > 0;
}

function scheduleAutosave(kind, fn, delay = 900) {
  autosavePending[kind] = fn;
  clearTimeout(autosaveTimers[kind]);
  autosaveTimers[kind] = setTimeout(async () => {
    delete autosaveTimers[kind];
    const run = autosavePending[kind];
    delete autosavePending[kind];
    if (run) await run();
  }, delay);
}

async function flushAutosave() {
  for (const kind of Object.keys(autosaveTimers)) {
    clearTimeout(autosaveTimers[kind]);
    delete autosaveTimers[kind];
  }
  const pending = { ...autosavePending };
  for (const kind of Object.keys(autosavePending)) delete autosavePending[kind];
  for (const fn of Object.values(pending)) {
    await fn();
  }
}
const ACCOUNT_TAB_LABELS = {
  basics: "Basics",
  jobs: "Jobs",
  limits: "Limits",
  filters: "Filters",
  lists: "Usernames",
  comments: "Comments & PM",
  schedule: "Schedule",
  reports: "Reports",
  raw: "Raw config",
};

function $(id) {
  return document.getElementById(id);
}

function log(message, level = "info") {
  const el = $("unified-log");
  if (!el) return;
  const line = document.createElement("div");
  const ts = new Date().toLocaleTimeString();
  line.className = `log-line log-${level}`;
  line.textContent = `[${ts}] ${message}`;
  el.appendChild(line);
  if (autoscroll) el.scrollTop = el.scrollHeight;
}

function clearLog() {
  const el = $("unified-log");
  if (el) el.innerHTML = "";
}

function clearDebugTerminal() {
  const el = $("debug-live-log");
  if (el) el.textContent = "";
  debugLogSince = 0;
}

function toggleDebugLogAutoscroll() {
  debugLogAutoscroll = !debugLogAutoscroll;
  const btn = $("btn-debug-log-autoscroll");
  if (btn) btn.classList.toggle("active", debugLogAutoscroll);
}

function appendDebugTerminalLine(message) {
  const el = $("debug-live-log");
  if (!el) return;
  if (!el.dataset.started) {
    el.textContent = "";
    el.dataset.started = "1";
  }
  const line = document.createElement("div");
  let cls = "debug-log-line";
  if (message.startsWith("▶")) cls += " debug-log-step";
  else if (/\sW\b/.test(message) || /WARNING/i.test(message)) cls += " debug-log-warn";
  else if (/\sE\b/.test(message) || /ERROR/i.test(message)) cls += " debug-log-error";
  line.className = cls;
  line.textContent = message;
  el.appendChild(line);
  if (debugLogAutoscroll) el.scrollTop = el.scrollHeight;
}

function startDebugLogPolling(serial) {
  stopDebugLogPolling();
  debugLogSince = 0;
  clearDebugTerminal();
  const el = $("debug-live-log");
  if (el) delete el.dataset.started;
  if (!serial) return;
  debugLogPollTimer = setInterval(async () => {
    if (!debugTestRunning || !serial) return;
    try {
      const res = await fetch(
        `/api/devices/${encodeURIComponent(serial)}/debug/logs?since=${debugLogSince}`
      );
      if (!res.ok) return;
      const data = await res.json();
      for (const line of data.lines || []) {
        appendDebugTerminalLine(line);
      }
      if (typeof data.next === "number") debugLogSince = data.next;
    } catch (_) {}
  }, 350);
}

function stopDebugLogPolling() {
  if (debugLogPollTimer) {
    clearInterval(debugLogPollTimer);
    debugLogPollTimer = null;
  }
}

function toggleAutoscroll() {
  autoscroll = !autoscroll;
  const btn = $("btn-autoscroll");
  if (btn) btn.classList.toggle("active", autoscroll);
}

function setConnectionStatus(connected) {
  const pill = $("live-indicator");
  const label = $("connection-status");
  if (!pill || !label) return;
  pill.classList.toggle("off", !connected);
  label.textContent = connected ? "Live" : "Disconnected";
}

function shortSerial(serial) {
  if (!serial) return "—";
  return serial.length <= 4 ? serial : serial.slice(-4);
}

function loadStoredActiveSerial() {
  return localStorage.getItem("activeSerial") || sessionStorage.getItem("activeSerial") || null;
}

function loadStoredSelectedSerials() {
  try {
    const raw = localStorage.getItem("selectedSerials");
    if (!raw) return;
    const list = JSON.parse(raw);
    if (Array.isArray(list)) list.forEach((s) => selectedSerials.add(s));
  } catch (_) {
    /* ignore */
  }
}

function persistDeviceSelection() {
  if (activeSerial) {
    localStorage.setItem("activeSerial", activeSerial);
    sessionStorage.setItem("activeSerial", activeSerial);
  } else {
    localStorage.removeItem("activeSerial");
    sessionStorage.removeItem("activeSerial");
  }
  localStorage.setItem("selectedSerials", JSON.stringify([...selectedSerials]));
}

loadStoredSelectedSerials();
if (activeSerial) selectedSerials.add(activeSerial);

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function accountForDevice(serial) {
  if (!serial) return null;
  return gaAccounts.find((a) => a.device === serial) || null;
}

function accountForActivePhone() {
  return activeSerial ? accountForDevice(activeSerial) : null;
}

function normalizeInstagramHandle(value) {
  return String(value || "").trim().replace(/^@+/, "");
}

const deviceAccountDrafts = {};
let deviceAccountEditingSerial = null;

function deviceAccountInputValue(serial, acct) {
  if (Object.prototype.hasOwnProperty.call(deviceAccountDrafts, serial)) {
    return deviceAccountDrafts[serial];
  }
  if (!acct) return "";
  return acct.username || acct.id || "";
}

function deviceAccountLabel(acct) {
  if (!acct) return "";
  return acct.username || acct.id || "";
}

function startDeviceAccountEdit(serial) {
  const acct = accountForDevice(serial);
  deviceAccountEditingSerial = serial;
  deviceAccountDrafts[serial] = deviceAccountInputValue(serial, acct);
  renderDevices();
  requestAnimationFrame(() => {
    const row = findDeviceRow(serial);
    const input = row?.querySelector(".phones-account-input");
    if (!input) return;
    input.focus();
    input.select();
  });
}

function findDeviceRow(serial) {
  return [...($("farm-grid")?.querySelectorAll("tr.phones-table-row") || [])].find(
    (row) => row.dataset.serial === serial
  );
}

function deviceAccountCellHtml(serial, acct, acctRunning) {
  const runningMark = acctRunning ? '<span class="phones-account-running" title="Bot running">●</span>' : "";
  if (deviceAccountEditingSerial === serial) {
    const handleValue = escapeHtml(deviceAccountInputValue(serial, acct));
    return `
      <div class="phones-account-wrap phones-account-editing">
        <span class="phones-account-at">@</span>
        <input type="text" class="phones-account-input account-handle" data-serial="${escapeHtml(serial)}"
               value="${handleValue}" placeholder="username" autocomplete="off" spellcheck="false"
               aria-label="Instagram account for ${escapeHtml(serial)}">
        ${runningMark}
      </div>`;
  }
  const handle = deviceAccountLabel(acct);
  if (!handle) {
    return `
      <div class="phones-account-wrap">
        <button type="button" class="phones-account-set">Set account</button>
        ${runningMark}
      </div>`;
  }
  return `
    <div class="phones-account-wrap">
      <button type="button" class="phones-account-display" title="@${escapeHtml(handle)}">@${escapeHtml(handle)}</button>
      ${runningMark}
    </div>`;
}

function bindDeviceAccountCell(row, serial) {
  const setBtn = row.querySelector(".phones-account-set");
  const displayBtn = row.querySelector(".phones-account-display");
  setBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    startDeviceAccountEdit(serial);
  });
  displayBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    startDeviceAccountEdit(serial);
  });
  const accountInput = row.querySelector(".phones-account-input");
  if (accountInput) {
    accountInput.addEventListener("input", () => onDeviceAccountInput(serial, accountInput));
    accountInput.addEventListener("blur", () => onDeviceAccountBlur(serial, accountInput));
    accountInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        accountInput.blur();
      }
      if (e.key === "Escape") {
        e.preventDefault();
        delete deviceAccountDrafts[serial];
        deviceAccountEditingSerial = null;
        renderDevices();
      }
    });
  }
}

function syncAccountSelectionFromPhone(serial) {
  const acct = accountForDevice(serial);
  if (!acct || gaCurrentAccountId === acct.id) return;
  gaCurrentAccountId = acct.id;
  localStorage.setItem("gaAccountId", acct.id);
  const select = $("ga-account-select");
  if (select && [...select.options].some((o) => o.value === acct.id)) {
    select.value = acct.id;
  }
}

function onDeviceAccountInput(serial, input) {
  deviceAccountDrafts[serial] = input.value;
}

async function onDeviceAccountBlur(serial, input) {
  const value = normalizeInstagramHandle(input.value);
  deviceAccountEditingSerial = null;
  delete deviceAccountDrafts[serial];
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(serial)}/account`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: value }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to save account");
    await loadGaAccounts();
    if (data.account_id) {
      gaCurrentAccountId = data.account_id;
      localStorage.setItem("gaAccountId", data.account_id);
      const select = $("ga-account-select");
      if (select) select.value = data.account_id;
    }
    if (activeSerial === serial) syncAccountSelectionFromPhone(serial);
    log(value ? `Linked @${value} to ${shortSerial(serial)}` : `Cleared account on ${shortSerial(serial)}`);
    updateContextStrip();
    renderDevices();
  } catch (err) {
    log(err.message, "error");
    renderDevices();
  }
}

function currentAccount() {
  return accountForActivePhone() || gaAccounts.find((a) => a.id === gaCurrentAccountId) || null;
}

/* ── Tab routing ── */

function setMainTab(tab, opts = {}) {
  const prev = currentMainTab;
  currentMainTab = tab;
  localStorage.setItem("mainTab", tab);
  document.querySelectorAll(".main-tab").forEach((el) => {
    const on = el.dataset.tab === tab;
    el.classList.toggle("active", on);
    el.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".app-view").forEach((el) => {
    el.classList.toggle("hidden", el.id !== `view-${tab}`);
  });
  if (prev !== tab && (prev === "account" || prev === "tools")) flushAutosave();
  updateContextStrip();
  renderSessionEstimate(null);
  if (tab === "account" && gaCurrentAccountId) scheduleSessionEstimateRefresh();
  if (tab === "tools") {
    updateToolsView();
    loadAdvFiles();
    if (!opts.skipWeditorConnect && activeSerial) {
      connectWeditor();
    }
  }
  location.hash = tab;
}

function bindMainTabs() {
  document.querySelectorAll(".main-tab").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      setMainTab(el.dataset.tab);
    });
  });
  const hash = (location.hash || "#farm").replace(/^#/, "");
  const valid = ["farm", "account", "tools"];
  const tab = hash === "advanced" ? "tools" : hash;
  setMainTab(valid.includes(tab) ? tab : localStorage.getItem("mainTab") || "farm", {
    skipWeditorConnect: true,
  });
  window.addEventListener("hashchange", () => {
    const h = (location.hash || "#farm").replace(/^#/, "");
    const next = h === "advanced" ? "tools" : h;
    if (["farm", "account", "tools"].includes(next)) setMainTab(next);
  });
}

function setAccountTab(tab) {
  const prev = currentAccountTab;
  currentAccountTab = tab;
  localStorage.setItem("accountTab", tab);
  document.querySelectorAll(".account-subtab").forEach((el) => {
    const on = el.dataset.accountTab === tab;
    el.classList.toggle("active", on);
    el.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".account-panel").forEach((el) => {
    el.classList.toggle("hidden", el.id !== `account-panel-${tab}`);
  });
  if (prev !== tab) flushAutosave();
  setActiveSaveField(null);
  if (tab === "posting") loadPostReelMedia();
}

function bindAccountTabs() {
  document.querySelectorAll(".account-subtab").forEach((el) => {
    el.addEventListener("click", () => setAccountTab(el.dataset.accountTab));
    applyTabHelp(el);
  });
  setAccountTab(currentAccountTab);
}

function applyTabHelp(button) {
  const tab = button?.dataset?.accountTab;
  const help = gaSchema?.tab_help?.[tab] || gaFilesMeta?.tab_help?.[tab];
  if (help) button.title = help;
}

function applyAllTabHelp() {
  document.querySelectorAll(".account-subtab").forEach(applyTabHelp);
}

let settingsSearchIndex = [];
let settingSearchActiveIndex = -1;

function rebuildSettingsSearchIndex() {
  const items = [];
  const tabMap = gaSchema?.tabs || {};
  const sectionToTab = {};
  for (const [tab, sections] of Object.entries(tabMap)) {
    for (const sectionId of sections) sectionToTab[sectionId] = tab;
  }
  for (const [sectionId, fields] of Object.entries(gaSchema?.sections || {})) {
    const tab = sectionToTab[sectionId] || "basics";
    const tabLabel = ACCOUNT_TAB_LABELS[tab] || tab;
    for (const field of fields) {
      const sub = inlineFieldSubKeys(field);
      const selector = sub
        ? `[data-ga-key="${sub.listKey}"]${sub.limitKey ? `, [data-ga-key="${sub.limitKey}"]` : ""}`
        : `[data-ga-key="${field.key}"]`;
      items.push({
        key: field.key,
        label: field.label,
        tab,
        tabLabel,
        selector,
        help: field.help || "",
      });
    }
  }
  for (const fields of Object.values(gaFiltersSchema?.sections || {})) {
    for (const field of fields) {
      items.push({
        key: field.key,
        label: field.label,
        tab: "filters",
        tabLabel: ACCOUNT_TAB_LABELS.filters,
        selector: `[data-filter-key="${field.key}"]`,
        help: field.help || "",
      });
    }
  }
  for (const field of gaTelegramSchema?.fields || []) {
    items.push({
      key: field.key,
      label: field.label,
      tab: "reports",
      tabLabel: ACCOUNT_TAB_LABELS.reports,
      selector: `[data-tg-key="${field.key}"]`,
      help: field.help || "",
    });
  }
  for (const [name, label] of Object.entries(gaFilesMeta?.lists || {})) {
    items.push({
      key: name,
      label,
      tab: "lists",
      tabLabel: ACCOUNT_TAB_LABELS.lists,
      selector: `[data-file-key="${name}"]`,
      help: gaFilesMeta?.file_help?.[name] || "",
    });
  }
  for (const [name, label] of Object.entries(gaFilesMeta?.text || {})) {
    items.push({
      key: name,
      label,
      tab: "comments",
      tabLabel: ACCOUNT_TAB_LABELS.comments,
      selector: `[data-file-key="${name}"]`,
      help: gaFilesMeta?.file_help?.[name] || "",
    });
  }
  settingsSearchIndex = items;
}

function normalizeSearchText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[_-]/g, " ");
}

function matchSettings(query) {
  const q = normalizeSearchText(query.trim());
  if (!q) return [];
  return settingsSearchIndex
    .filter((item) => {
      const hay = normalizeSearchText([item.label, item.key, item.help, item.tabLabel].join(" "));
      return hay.includes(q);
    })
    .slice(0, 12);
}

function renderSettingSearchResults(matches) {
  const box = $("account-setting-results");
  if (!box) return;
  if (!matches.length) {
    box.innerHTML = '<div class="account-setting-result-meta" style="padding:0.65rem">No matching settings</div>';
    box.classList.remove("hidden");
    return;
  }
  box.innerHTML = matches
    .map(
      (item, i) => `<button type="button" class="account-setting-result${i === settingSearchActiveIndex ? " active" : ""}" role="option"
        onclick="goToSetting(${i}, true)">
        <div class="account-setting-result-label">${escapeHtml(item.label)}</div>
        <div class="account-setting-result-meta">${escapeHtml(item.tabLabel)}</div>
      </button>`
    )
    .join("");
  box.classList.remove("hidden");
  window._settingSearchMatches = matches;
}

function hideSettingSearchResults() {
  $("account-setting-results")?.classList.add("hidden");
  settingSearchActiveIndex = -1;
  window._settingSearchMatches = [];
}

function highlightSettingElement(el) {
  if (!el) return;
  let details = el.closest("details");
  while (details) {
    details.open = true;
    details = details.parentElement?.closest("details");
  }
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  const target = el.closest(".field") || el.closest(".ga-check") || el;
  target.classList.add("setting-search-highlight");
  let focusEl = el;
  if (el.type === "hidden" && el.closest(".working-hours-widget")) {
    focusEl = el.closest(".working-hours-widget").querySelector(".wh-start") || el;
  }
  if (el.type === "hidden" && el.closest(".comments-list-widget")) {
    focusEl = el.closest(".comments-list-widget").querySelector(".comments-section-input") || el;
  }
  if (typeof focusEl.focus === "function") {
    try {
      focusEl.focus({ preventScroll: true });
    } catch (_) {
      focusEl.focus();
    }
  }
  setTimeout(() => target.classList.remove("setting-search-highlight"), 2000);
}

function goToRawYamlMatch(query) {
  const ta = $("ga-raw-yaml");
  if (!ta) return false;
  const q = query.trim().toLowerCase();
  const slug = q.replace(/\s+/g, "-");
  const patterns = [slug + ":", q + ":", slug, q];
  const lower = ta.value.toLowerCase();
  let index = -1;
  for (const pattern of patterns) {
    index = lower.indexOf(pattern);
    if (index >= 0) break;
  }
  if (index < 0) return false;
  setAccountTab("raw");
  requestAnimationFrame(() => {
    ta.focus();
    const lineStart = ta.value.lastIndexOf("\n", index) + 1;
    const lineEnd = ta.value.indexOf("\n", index);
    const end = lineEnd >= 0 ? lineEnd : ta.value.length;
    ta.setSelectionRange(lineStart, end);
    highlightSettingElement(ta);
  });
  return true;
}

function goToSetting(index, fromClick = false) {
  const matches = window._settingSearchMatches || [];
  const item = typeof index === "number" ? matches[index] : index;
  if (!item) return;
  const input = $("account-setting-search");
  if (input && fromClick) input.value = item.label;
  hideSettingSearchResults();
  setMainTab("account");
  setAccountTab(item.tab);
  requestAnimationFrame(() => {
    const el = document.querySelector(item.selector);
    if (el) {
      highlightSettingElement(el);
      return;
    }
    goToRawYamlMatch(item.key || item.label);
  });
}

function onSettingSearchInput() {
  const input = $("account-setting-search");
  if (!input) return;
  const matches = matchSettings(input.value);
  settingSearchActiveIndex = matches.length ? 0 : -1;
  if (!input.value.trim()) {
    hideSettingSearchResults();
    return;
  }
  renderSettingSearchResults(matches);
}

function onSettingSearchKeydown(event) {
  const matches = window._settingSearchMatches || [];
  const input = $("account-setting-search");
  if (!input) return;

  if (event.key === "Escape") {
    hideSettingSearchResults();
    return;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    if (!matches.length) {
      onSettingSearchInput();
      return;
    }
    settingSearchActiveIndex = Math.min(settingSearchActiveIndex + 1, matches.length - 1);
    renderSettingSearchResults(matches);
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    if (!matches.length) return;
    settingSearchActiveIndex = Math.max(settingSearchActiveIndex - 1, 0);
    renderSettingSearchResults(matches);
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    if (matches.length && settingSearchActiveIndex >= 0) {
      goToSetting(settingSearchActiveIndex, true);
      return;
    }
    const direct = matchSettings(input.value);
    if (direct.length === 1) {
      window._settingSearchMatches = direct;
      goToSetting(0, true);
      return;
    }
    if (goToRawYamlMatch(input.value)) {
      hideSettingSearchResults();
    }
  }
}

function bindSettingSearch() {
  document.addEventListener("click", (event) => {
    const wrap = document.querySelector(".account-setting-search-wrap");
    if (wrap && !wrap.contains(event.target)) hideSettingSearchResults();
  });
}

/* ── Context strip ── */

function deviceOptionLabel(device) {
  const model = [device.manufacturer, device.model].filter(Boolean).join(" ") || "Android";
  return `${shortSerial(device.serial)} · ${model}`;
}

function getContextPhoneCandidates() {
  if (selectedSerials.size > 0) {
    return devices.filter((d) => selectedSerials.has(d.serial));
  }
  if (activeSerial) {
    const device = devices.find((d) => d.serial === activeSerial);
    return device ? [device] : [];
  }
  return [];
}

function ensureActivePhone() {
  const candidates = getContextPhoneCandidates();
  if (!candidates.length) return;
  if (!activeSerial || !candidates.some((d) => d.serial === activeSerial)) {
    activeSerial = candidates[0].serial;
    persistDeviceSelection();
  }
}

function populateCtxPhoneSelect() {
  const select = $("ctx-phone-select");
  const text = $("ctx-phone");
  if (!select || !text) return;

  const candidates = getContextPhoneCandidates();
  if (candidates.length <= 1) {
    select.classList.add("hidden");
    text.classList.remove("hidden");
    const device = candidates[0];
    text.textContent = device ? deviceOptionLabel(device) : "None selected";
    return;
  }

  select.classList.remove("hidden");
  text.classList.add("hidden");
  select.innerHTML = candidates
    .map(
      (d) =>
        `<option value="${escapeHtml(d.serial)}">${escapeHtml(deviceOptionLabel(d))}</option>`
    )
    .join("");
  const value =
    activeSerial && candidates.some((d) => d.serial === activeSerial)
      ? activeSerial
      : candidates[0].serial;
  select.value = value;
  if (value !== activeSerial) {
    selectActiveDevice(value, { quiet: true });
  }
}

function onCtxPhoneChange() {
  const select = $("ctx-phone-select");
  if (select?.value) selectActiveDevice(select.value, { quiet: true });
}

function updateContextStrip() {
  ensureActivePhone();
  populateCtxPhoneSelect();
  const accountEl = $("ctx-account");
  const statusEl = $("ctx-status");
  const acct = currentAccount();
  const running = !!(acct?.running || gaAccountRunning);

  if (accountEl) {
    accountEl.textContent = acct ? `@${acct.username || acct.id}` : "None";
  }
  if (statusEl) {
    statusEl.textContent = running ? "Running" : "Idle";
    statusEl.className = "context-status " + (running ? "running" : "idle");
  }

  const canRun = !!acct && (!!activeSerial || acct.device || gaCurrentAccountId);
  const runBtns = ["btn-farm-run"];
  const stopBtns = ["btn-farm-stop"];
  runBtns.forEach((id) => {
    const el = $(id);
    if (el) el.disabled = !canRun || running;
  });
  stopBtns.forEach((id) => {
    const el = $(id);
    if (el) el.disabled = !running;
  });
  $("btn-ctx-mirror") && ($("btn-ctx-mirror").disabled = !activeSerial && selectedSerials.size === 0);
  const quickDebugBtn = $("btn-ctx-quick-debug");
  if (quickDebugBtn) {
    const stepId = localStorage.getItem("debugTestId") || "open-instagram";
    quickDebugBtn.disabled = !activeSerial || debugTestRunning;
    quickDebugBtn.title = `Quick debug: ${stepId} (pick a step on Tools tab to change)`;
  }
}

function syncRunButtons(running) {
  gaAccountRunning = !!running;
  updateContextStrip();
}

/* ── Devices ── */

function updateInspectorHeader() {
  if (!activeSerial) return;
  const device = devices.find((d) => d.serial === activeSerial);
  const subtitle = $("inspector-subtitle");
  if (subtitle && device) {
    subtitle.textContent = `Weditor · ${shortSerial(device.serial)} · ${device.status || "CONNECTED"}`;
  }
}

function updateWeditorButtons() {
  const enabled = !!activeSerial && !weditorConnecting;
  $("btn-weditor-reconnect") && ($("btn-weditor-reconnect").disabled = !enabled);
  $("btn-weditor-tab") && ($("btn-weditor-tab").disabled = !activeSerial);
  $("btn-home") && ($("btn-home").disabled = !activeSerial);
  const dumpBtn = $("btn-export-dump");
  if (dumpBtn && !dumpBtn.textContent?.includes("…")) dumpBtn.disabled = !activeSerial;
}

function updateToolsView() {
  const empty = $("tools-empty");
  const weditor = $("tools-weditor");
  const subtitle = $("tools-subtitle");
  if (!activeSerial) {
    empty?.classList.remove("hidden");
    weditor?.classList.add("hidden");
    if (subtitle) subtitle.textContent = "Select a phone on Farm to open Weditor.";
    updateWeditorButtons();
    return;
  }
  empty?.classList.add("hidden");
  const device = devices.find((d) => d.serial === activeSerial);
  if (subtitle && device) {
    subtitle.textContent = `Weditor · ${shortSerial(device.serial)} · ${device.model || "Android"}`;
  }
  updateInspectorHeader();
  updateWeditorButtons();
}

function restoreActiveSelection() {
  const serials = new Set(devices.map((d) => d.serial));
  const preferred = activeSerial || loadStoredActiveSerial();
  if (!preferred || !serials.has(preferred)) return false;
  activeSerial = preferred;
  selectedSerials.add(preferred);
  persistDeviceSelection();
  $("btn-weditor-reconnect") && ($("btn-weditor-reconnect").disabled = false);
  $("btn-weditor-tab") && ($("btn-weditor-tab").disabled = false);
  $("btn-home") && ($("btn-home").disabled = false);
  const dumpBtn = $("btn-export-dump");
  if (dumpBtn) dumpBtn.disabled = false;
  updateInspectorHeader();
  updateToolsView();
  return true;
}

function pruneDisconnectedSelections() {
  const serials = new Set(devices.map((d) => d.serial));
  for (const s of [...selectedSerials]) {
    if (!serials.has(s)) selectedSerials.delete(s);
  }
  if (activeSerial && !serials.has(activeSerial)) {
    const gone = activeSerial;
    activeSerial = null;
    selectedSerials.delete(gone);
    persistDeviceSelection();
    clearInspector();
    return;
  }
  if (restoreActiveSelection()) {
    persistDeviceSelection();
  }
}

function updateCounts() {
  const online = devices.length;
  const countEl = $("farm-online-count");
  const batchEl = $("batch-slot-count");
  if (countEl) countEl.textContent = `${online} device${online === 1 ? "" : "s"}`;
  if (batchEl) {
    const n = selectedSerials.size;
    batchEl.textContent = n === 0 ? "0 selected" : `${n} selected`;
  }
  const dumpBtn = $("btn-export-dump");
  if (dumpBtn) dumpBtn.disabled = !activeSerial;
  updateContextStrip();
}

function renderDevices() {
  const tbody = $("farm-grid");
  if (!tbody) return;
  tbody.innerHTML = "";

  if (devices.length === 0) {
    const row = document.createElement("tr");
    row.className = "phones-table-row empty";
    row.innerHTML = `<td colspan="5" class="phones-td phones-muted" style="text-align:center;padding:1.5rem">No devices connected. Plug in a phone with USB debugging enabled.</td>`;
    tbody.appendChild(row);
    updateCounts();
    return;
  }

  devices.forEach((device, index) => {
    const serial = device.serial;
    const selected = selectedSerials.has(serial);
    const acct = accountForDevice(serial);
    const acctRunning = acct?.running;
    const row = document.createElement("tr");
    row.className = "phones-table-row" + (activeSerial === serial ? " running" : "");
    row.dataset.serial = serial;
    row.dataset.selected = selected ? "true" : "false";
    row.onclick = (e) => {
      if (e.target.closest("input, button, .phones-account-wrap")) return;
      selectActiveDevice(serial);
    };

    row.innerHTML = `
      <td class="phones-td phones-td-check" onclick="event.stopPropagation()">
        <input type="checkbox" class="ui-checkbox phones-check" ${selected ? "checked" : ""}
               aria-label="Select ${serial}"
               onchange="onDeviceCheckChange('${serial}', this.checked)">
      </td>
      <td class="phones-td phones-td-slot">${index + 1}</td>
      <td class="phones-td phones-td-handle" title="${escapeHtml(serial)}">
        <span class="phones-handle-link">${shortSerial(serial)}</span>
      </td>
      <td class="phones-td phones-td-account${acctRunning ? " running-acct" : ""}" onclick="event.stopPropagation()">
        ${deviceAccountCellHtml(serial, acct, acctRunning)}
      </td>
      <td class="phones-td">
        <span class="phones-status-cell">
          <span class="status-dot online"></span>
          <span class="badge CONNECTED">${device.status || "CONNECTED"}</span>
        </span>
      </td>
    `;
    tbody.appendChild(row);
    bindDeviceAccountCell(row, serial);
  });

  const selectAll = $("batch-select-all");
  if (selectAll) {
    selectAll.checked = devices.length > 0 && selectedSerials.size === devices.length;
    selectAll.indeterminate = selectedSerials.size > 0 && selectedSerials.size < devices.length;
  }
  updateCounts();
}

function onDeviceCheckChange(serial, checked) {
  if (checked) selectedSerials.add(serial);
  else selectedSerials.delete(serial);
  persistDeviceSelection();
  ensureActivePhone();
  renderDevices();
}

function onBatchSelectAllChange(checked) {
  if (checked) devices.forEach((d) => selectedSerials.add(d.serial));
  else {
    selectedSerials.clear();
    activeSerial = null;
    persistDeviceSelection();
  }
  ensureActivePhone();
  renderDevices();
}

function selectActiveDevice(serial, opts = {}) {
  if (!serial) return;
  const quiet = opts.quiet === true;
  const prev = activeSerial;
  const changed = prev !== serial;
  activeSerial = serial;
  if (!selectedSerials.has(serial)) selectedSerials.add(serial);
  persistDeviceSelection();
  syncAccountSelectionFromPhone(serial);
  renderDevices();
  $("btn-weditor-reconnect") && ($("btn-weditor-reconnect").disabled = false);
  $("btn-weditor-tab") && ($("btn-weditor-tab").disabled = false);
  $("btn-home") && ($("btn-home").disabled = false);
  const dumpBtn = $("btn-export-dump");
  if (dumpBtn) dumpBtn.disabled = false;
  updateInspectorHeader();
  updateToolsView();
  populateCtxPhoneSelect();
  if (!quiet) log(`Selected ${shortSerial(serial)}`);
  if (changed && currentMainTab === "tools" && !opts.skipWeditor) {
    connectWeditor(serial);
  }
}

async function refreshDevices() {
  try {
    const res = await fetch("/api/devices?fast=true");
    if (!res.ok) throw new Error(await res.text());
    devices = await res.json();
    pruneDisconnectedSelections();
    if (devices.length === 1 && (!activeSerial || !devices.some((d) => d.serial === activeSerial))) {
      selectActiveDevice(devices[0].serial, { quiet: true, skipWeditor: true });
    }
    renderDevices();
    populateGaDeviceSelects();
    const suffix = deviceFilterSuffix ? ` ending …${deviceFilterSuffix}` : "";
    log(`Found ${devices.length} device(s)${suffix}`);
  } catch (err) {
    log(`Refresh failed: ${err.message}`, "error");
  }
}

async function loadDeviceFilterMeta() {
  try {
    const res = await fetch("/api/devices/meta");
    if (!res.ok) return;
    const meta = await res.json();
    deviceFilterSuffix = meta.device_filter || "";
    const subtitle = $("tools-subtitle");
    if (subtitle && deviceFilterSuffix) {
      subtitle.textContent = `Dev mode: only phone serial ending …${deviceFilterSuffix}. Inspect, debug, and edit files.`;
    }
  } catch (_) {
    /* ignore */
  }
}

/* ── Weditor inspector ── */

function setWeditorLoading(loading, message) {
  weditorConnecting = loading;
  updateWeditorButtons();
  const placeholder = $("weditor-placeholder");
  const frame = $("weditor-frame");
  if (!placeholder) return;
  if (loading) {
    placeholder.textContent = message || "Connecting Weditor…";
    placeholder.classList.remove("hidden");
    frame?.classList.add("hidden");
    $("tools-empty")?.classList.add("hidden");
    $("tools-weditor")?.classList.remove("hidden");
    return;
  }
  weditorConnecting = false;
  updateWeditorButtons();
  if (message) {
    placeholder.textContent = message;
    placeholder.classList.remove("hidden");
    frame?.classList.add("hidden");
  }
}

function clearInspector() {
  $("tools-empty")?.classList.remove("hidden");
  $("tools-weditor")?.classList.add("hidden");
  $("weditor-placeholder")?.classList.add("hidden");
  const frame = $("weditor-frame");
  if (frame) {
    frame.src = "about:blank";
    frame.classList.add("hidden");
  }
  const subtitle = $("inspector-subtitle");
  if (subtitle) subtitle.textContent = "Weditor — dump hierarchy and pick elements";
  weditorConnecting = false;
  updateWeditorButtons();
  updateToolsView();
}

async function connectWeditor(serial = activeSerial) {
  if (!serial) {
    clearInspector();
    return;
  }
  if (await isDebugBusy(serial)) {
    await waitForDebugIdle(serial, 6000);
  }
  if (weditorConnecting) return;
  setWeditorLoading(true, "Starting Weditor and connecting to device…");
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(serial)}/weditor/connect`, {
      method: "POST",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Weditor connect failed");
    const frame = $("weditor-frame");
    if (frame) {
      frame.src = data.url;
      frame.classList.remove("hidden");
    }
    $("weditor-placeholder")?.classList.add("hidden");
    $("tools-empty")?.classList.add("hidden");
    $("tools-weditor")?.classList.remove("hidden");
    updateInspectorHeader();
    if (data.connected === false) {
      log("Weditor opened — use Dump Hierarchy in the inspector if needed", "error");
    } else {
      log(`Weditor connected to ${shortSerial(serial)}`);
    }
  } catch (err) {
    log(`Weditor error: ${err.message}`, "error");
    setWeditorLoading(false, err.message || "Could not connect — click Reconnect");
    return;
  }
  weditorConnecting = false;
  updateWeditorButtons();
}

async function openWeditorInTab(serial = activeSerial) {
  if (!serial) return;
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(serial)}/weditor/url`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to get Weditor URL");
    window.open(data.url, "_blank", "noopener,noreferrer");
  } catch (err) {
    log(`Weditor open failed: ${err.message}`, "error");
  }
}

function extractInspectorFilterFromTarget(target) {
  if (!target) return "";
  const text = String(target);
  const backtick = text.match(/`([^`]+)`/);
  if (backtick) {
    const id = backtick[1];
    return id.includes(":id/") ? id.split(":id/").pop() : id;
  }
  const token = text.match(/\b([A-Z][A-Z0-9_]{4,})\b/);
  return token ? token[1] : "";
}

async function showToolsInspector({ target, zipPath } = {}) {
  if (!activeSerial) return;
  setMainTab("tools", { skipWeditorConnect: true });
  updateToolsView();
  await connectWeditor();
  const filter = extractInspectorFilterFromTarget(target);
  if (filter) log(`Search in Weditor for: ${filter}`);
  if (zipPath) log(`Dump saved: ${zipPath}`);
}

async function pressHome() {
  if (!activeSerial) return;
  try {
    const res = await fetch(`/api/devices/${activeSerial}/home`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    log("Pressed home — dump hierarchy again in Weditor to refresh");
  } catch (err) {
    log(`Home failed: ${err.message}`, "error");
  }
}

async function mirrorSelected() {
  const serial = activeSerial || [...selectedSerials][0];
  if (!serial) return;
  try {
    const res = await fetch(`/api/devices/${serial}/mirror`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Mirror failed");
    log(data.status === "already_running" ? "Mirror already open" : "Mirror window opened (scrcpy)");
  } catch (err) {
    log(`Mirror failed: ${err.message}`, "error");
  }
}

async function dumpSelected() {
  const serial = activeSerial || [...selectedSerials][0];
  if (!serial) {
    log("Select a phone on Farm first", "error");
    return;
  }
  if (!activeSerial) {
    activeSerial = serial;
    persistDeviceSelection();
    restoreActiveSelection();
  }
  const dumpBtn = $("btn-export-dump");
  const prevLabel = dumpBtn?.textContent || "Export dump";
  if (dumpBtn) {
    dumpBtn.disabled = true;
    dumpBtn.textContent = "Exporting…";
  }
  log(`Exporting UI dump for ${shortSerial(serial)}…`);
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(serial)}/dump`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Dump failed");
    if (data.zip_name) {
      const link = document.createElement("a");
      link.href = `/api/dumps/${encodeURIComponent(data.zip_name)}`;
      link.download = data.zip_name;
      document.body.appendChild(link);
      link.click();
      link.remove();
    }
    await showToolsInspector({ zipPath: data.zip });
    log(`Dump saved${data.zip_name ? `: ${data.zip_name}` : ""} — use Weditor to inspect`);
  } catch (err) {
    log(`Dump failed: ${err.message}`, "error");
    setMainTab("tools");
    if (activeSerial) await connectWeditor();
  } finally {
    if (dumpBtn) {
      dumpBtn.disabled = !activeSerial;
      dumpBtn.textContent = prevLabel;
    }
  }
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setConnectionStatus(true);
  ws.onclose = () => {
    setConnectionStatus(false);
    setTimeout(connectWebSocket, 2000);
  };
  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "devices") {
        devices = msg.devices || [];
        pruneDisconnectedSelections();
        renderDevices();
        populateGaDeviceSelects();
      } else if (msg.type === "error") {
        log(msg.message, "error");
      } else if (msg.type === "bot_log") {
        log(`[bot] ${msg.message}`, "info");
      } else if (msg.type === "debug_log") {
        if (!activeSerial || !msg.serial || msg.serial === activeSerial) {
          appendDebugTerminalLine(msg.message);
        }
      }
    } catch (_) {}
  };
}

/* ── Debug ── */

let debugFlowStepIds = [];
let debugFlowSteps = [];
let debugTestRunning = false;
let debugTestAbort = null;
let currentDebugFlow = localStorage.getItem("debugFlow") || "flow";

function setDebugStatus(message, level = "") {
  const el = $("debug-status");
  if (!el) return;
  el.textContent = message || "";
  el.className = "adv-status-line" + (level ? ` ${level}` : "");
}

function resetDebugRunButtons() {
  ["btn-debug-run", "btn-debug-run-all"].forEach((id) => {
    const el = $(id);
    if (el) {
      el.disabled = false;
      el.textContent = id === "btn-debug-run-all" ? "Run A→Z" : "Run test";
    }
  });
  const killBtn = $("btn-debug-kill");
  if (killBtn) killBtn.disabled = true;
}

function formatDetects(text) {
  if (!text) return "";
  return escapeHtml(text).replace(/\n/g, "<br>");
}

function selectedDebugStep() {
  const select = $("debug-test-select");
  const id = select?.value;
  if (!id) return null;
  return debugFlowSteps.find((s) => s.id === id) || null;
}

function updateDebugStepDetail(opts = {}) {
  const panel = $("debug-detail-panel");
  const titleEl = $("debug-detail-title");
  const stateEl = $("debug-detail-state");
  const detectsEl = $("debug-detail-detects");
  const resultEl = $("debug-detail-result");
  if (!titleEl || !stateEl || !detectsEl || !resultEl) return;

  const step = opts.step ?? selectedDebugStep();
  const state = opts.state || "";
  const resultText = opts.resultText ?? "";
  const resultOk = opts.resultOk;

  if (step) {
    titleEl.textContent = step.label.replace(/^\d+\.\s*/, "");
  } else if (opts.title) {
    titleEl.textContent = opts.title;
  } else {
    titleEl.textContent = "Select a pipeline step above";
  }

  stateEl.textContent = opts.stateMessage || "";
  stateEl.className = "debug-detail-state text-xs mt-1" + (state ? ` ${state}` : "");

  if (panel) {
    panel.classList.toggle("running", state === "running");
  }

  const detects = step?.detects || opts.detects || "";
  if (detects) {
    detectsEl.textContent = detects;
    detectsEl.classList.remove("hidden");
  } else {
    detectsEl.textContent = "";
    detectsEl.classList.add("hidden");
  }

  if (resultText) {
    resultEl.textContent = resultText;
    resultEl.classList.remove("hidden");
    resultEl.classList.toggle("ok", resultOk === true);
    resultEl.classList.toggle("failed", resultOk === false);
  } else {
    resultEl.textContent = "";
    resultEl.classList.add("hidden");
    resultEl.classList.remove("ok", "failed");
  }
}

function debugStepListScrollKey() {
  return `debugStepListScroll:${currentDebugFlow || "flow"}`;
}

function saveDebugStepListScroll() {
  const list = $("debug-step-list");
  if (!list) return;
  localStorage.setItem(debugStepListScrollKey(), String(list.scrollTop));
}

function restoreDebugStepListView(activeId) {
  const list = $("debug-step-list");
  if (!list) return;

  const apply = () => {
    let restored = false;
    if (activeId) {
      const el = list.querySelector(`.debug-step-item[data-step-id="${activeId}"]`);
      if (el) {
        el.scrollIntoView({ block: "nearest", behavior: "auto" });
        restored = true;
      }
    }
    if (!restored) {
      const raw = localStorage.getItem(debugStepListScrollKey());
      if (raw != null) {
        const top = parseInt(raw, 10);
        if (!Number.isNaN(top)) list.scrollTop = top;
      }
    }
    saveDebugStepListScroll();
  };

  bindDebugStepListScroll();
  requestAnimationFrame(() => requestAnimationFrame(apply));
}

function restoreDebugStepListScroll() {
  restoreDebugStepListView(localStorage.getItem("debugTestId") || "");
}

let debugStepListScrollBound = false;

function bindDebugStepListScroll() {
  const list = $("debug-step-list");
  if (!list || debugStepListScrollBound) return;
  debugStepListScrollBound = true;
  let timer = null;
  list.addEventListener(
    "scroll",
    () => {
      clearTimeout(timer);
      timer = setTimeout(saveDebugStepListScroll, 100);
    },
    { passive: true }
  );
}

function renderDebugSteps() {
  const list = $("debug-step-list");
  const select = $("debug-test-select");
  if (!list || !select) return;
  saveDebugStepListScroll();
  list.innerHTML = "";
  select.innerHTML = "";
  const savedTest = localStorage.getItem("debugTestId");
  let activeId = debugFlowStepIds[0] || "";
  if (savedTest && debugFlowStepIds.includes(savedTest)) activeId = savedTest;

  debugFlowSteps.forEach((step, index) => {
    const option = document.createElement("option");
    option.value = step.id;
    option.textContent = step.label;
    select.appendChild(option);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "debug-step-item" + (activeId === step.id ? " selected" : "");
    btn.dataset.stepId = step.id;
    const shortLabel = step.label.replace(/^\d+\.\s*/, "");
    btn.innerHTML = `<span class="debug-step-num">${index + 1}</span><span class="debug-step-body"><span class="debug-step-label">${escapeHtml(shortLabel)}</span></span>`;
    btn.onclick = () => {
      select.value = step.id;
      localStorage.setItem("debugTestId", step.id);
      renderDebugSteps();
    };
    list.appendChild(btn);
  });
  if (activeId) select.value = activeId;
  const labelEl = $("debug-step-list-label");
  const hintEl = $("debug-step-list-hint");
  const n = debugFlowSteps.length;
  if (labelEl) {
    labelEl.textContent = n
      ? `Pipeline steps (${n} — scroll if needed)`
      : "Pipeline steps (in order)";
  }
  if (hintEl) {
    hintEl.classList.toggle("hidden", n <= 8);
  }
  updateDebugStepDetail({ stateMessage: "Idle — run the step to search the device for these elements." });
  restoreDebugStepListView(activeId);
}

function setDebugStepState(stepId, state) {
  const el = document.querySelector(`.debug-step-item[data-step-id="${stepId}"]`);
  if (!el) return;
  el.classList.remove("running", "done", "failed");
  if (state) el.classList.add(state);
}

function resetDebugStepStates() {
  document.querySelectorAll(".debug-step-item").forEach((el) => {
    el.classList.remove("running", "done", "failed");
  });
}

const EXPECTED_FEED_DEBUG_STEPS = 14;

function debugGroupCountsFromPage() {
  const counts = window.__DEBUG_GROUP_COUNTS__;
  return counts && typeof counts === "object" ? counts : null;
}

function updateDebugServerStaleHint(flowGroup) {
  const staleEl = $("debug-server-stale-hint");
  if (!staleEl) return;
  const counts = debugGroupCountsFromPage();
  const feedCount = counts?.feed ?? 0;
  const hasFeedGroup = counts && Object.prototype.hasOwnProperty.call(counts, "feed");
  if (flowGroup === "feed" && (!hasFeedGroup || feedCount < EXPECTED_FEED_DEBUG_STEPS)) {
    staleEl.textContent =
      `Dashboard server is outdated (feed=${feedCount}, need ${EXPECTED_FEED_DEBUG_STEPS}). Quit Device Lab completely and reopen Device Lab.command.`;
    staleEl.classList.remove("hidden");
    return;
  }
  staleEl.classList.add("hidden");
  staleEl.textContent = "";
}

async function loadDebugTests(group) {
  const flowGroup = group || currentDebugFlow || "flow";
  updateDebugServerStaleHint(flowGroup);
  try {
    const res = await fetch(`/api/debug/tests?group=${encodeURIComponent(flowGroup)}`);
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(
        detail.includes("outdated") || res.status === 404
          ? "Dashboard server outdated — quit Device Lab and reopen Device Lab.command"
          : detail
      );
    }
    const tests = await res.json();
    debugFlowSteps = tests;
    debugFlowStepIds = tests.map((t) => t.id);
    renderDebugSteps();
    try {
      const metaRes = await fetch("/api/debug/meta");
      if (metaRes.ok) {
        const meta = await metaRes.json();
        const feedCount = meta.counts?.feed ?? 0;
        const staleEl = $("debug-server-stale-hint");
        if (staleEl && flowGroup === "feed" && feedCount < EXPECTED_FEED_DEBUG_STEPS) {
          staleEl.textContent =
            `Dashboard server is outdated (feed has ${feedCount} steps). Quit Device Lab and reopen it.`;
          staleEl.classList.remove("hidden");
        }
      }
    } catch {
      /* meta endpoint optional */
    }
  } catch (err) {
    debugFlowStepIds = [];
    debugFlowSteps = [];
    renderDebugSteps();
    setDebugStatus(err.message, "error");
  }
}

function switchDebugFlow(value) {
  currentDebugFlow = value;
  localStorage.setItem("debugFlow", value);
  updateDebugTargetFieldVisibility();
  loadDebugTests(value);
}

function bindDebugPanel() {
  const select = $("debug-test-select");
  if (select) {
    select.addEventListener("change", () => {
      localStorage.setItem("debugTestId", select.value);
      document.querySelectorAll(".debug-step-item").forEach((el) => {
        el.classList.toggle("selected", el.dataset.stepId === select.value);
      });
      restoreDebugStepListView(select.value);
      updateDebugStepDetail({ stateMessage: "Idle — run the step to search the device for these elements." });
    });
  }
  window.addEventListener("beforeunload", saveDebugStepListScroll);
  const savedFlow = localStorage.getItem("debugFlow") || "flow";
  currentDebugFlow = savedFlow;
  document.querySelectorAll('input[name="debug-flow-select"]').forEach((r) => {
    r.checked = r.value === savedFlow;
  });
  updateDebugTargetFieldVisibility();
  initVpnAppName();
  initDebugTargetUsername();
  loadDebugTests(savedFlow);
}

function getVpnAppName() {
  return localStorage.getItem("vpnAppName") || "Shadowrocket";
}

function getDebugTargetUsername() {
  const el = $("debug-target-username");
  const raw = el?.value || localStorage.getItem("debugTargetUsername") || "";
  return raw.trim().replace(/^@/, "");
}

function onDebugTargetChange() {
  const el = $("debug-target-username");
  if (el) localStorage.setItem("debugTargetUsername", el.value.trim());
}

function updateDebugTargetFieldVisibility() {
  const isIg = currentDebugFlow === "instagram";
  const isFeed = currentDebugFlow === "feed";
  const isBpl = currentDebugFlow === "blogger-post-likers";
  const isReelComment = currentDebugFlow === "reel-comment";
  const isProfilePostVideo = currentDebugFlow === "profile-post-video";
  const showIgFields = isIg || isBpl;
  const showExtras = isIg || isBpl || isReelComment || isProfilePostVideo;
  const userField = $("debug-target-field");
  const searchField = $("debug-search-field");
  const postUrlField = $("debug-post-url-field");
  const messageField = $("debug-message-field");
  const likersField = $("debug-likers-offset-field");
  const fullscreenField = $("debug-fullscreen-likers-field");
  const reelCommentField = $("debug-reel-comment-field");
  const inlinePostVideoField = $("debug-inline-post-video-field");
  const postReelField = $("debug-post-reel-field");
  const unfollowField = $("debug-unfollow-list-field");
  const removeField = $("debug-remove-list-field");
  const telegramField = $("debug-telegram-field");
  if (userField) userField.hidden = !showIgFields;
  if (searchField) searchField.hidden = !showIgFields;
  if (postUrlField) postUrlField.hidden = !showIgFields;
  if (messageField) messageField.hidden = !(showIgFields || isReelComment || isProfilePostVideo);
  if (likersField) likersField.hidden = !showExtras;
  if (fullscreenField) fullscreenField.hidden = !showExtras;
  if (reelCommentField) reelCommentField.hidden = !showExtras;
  if (inlinePostVideoField) inlinePostVideoField.hidden = !showExtras;
  if (postReelField) postReelField.hidden = !showExtras;
  if (unfollowField) unfollowField.hidden = !showExtras;
  if (removeField) removeField.hidden = !showExtras;
  if (telegramField) telegramField.hidden = !showExtras;
  const subtitle = $("tools-subtitle");
  if (subtitle && isBpl) {
    subtitle.textContent =
      "Blogger post likers (production) — real bot timing & code paths. Set target @username (e.g. croy615), then run step 7 for full preflight.";
  } else if (subtitle && isFeed) {
    subtitle.textContent = "Feed job debug — run step 4 (inspect current post) on the home feed with a post visible.";
  } else if (subtitle && isReelComment) {
    subtitle.textContent =
      "Reel comment debug — open a profile video in fullscreen, then run steps 1→4 (step 4 needs test message).";
  } else if (subtitle && isProfilePostVideo) {
    subtitle.textContent =
      "Profile Posts inline video — open a grid video (Posts header, heart below media), then run 1→4.";
  } else if (subtitle) {
    subtitle.textContent = "Inspect the screen, run debug steps, and test pipeline stages.";
  }
}

function getDebugTargetSearch() {
  const el = $("debug-target-search");
  const raw = el?.value || localStorage.getItem("debugTargetSearch") || "";
  return raw.trim().replace(/^#/, "");
}

function onDebugSearchChange() {
  const el = $("debug-target-search");
  if (el) localStorage.setItem("debugTargetSearch", el.value.trim());
}

function getDebugTargetPostUrl() {
  const el = $("debug-target-post-url");
  return (el?.value || localStorage.getItem("debugTargetPostUrl") || "").trim();
}

function onDebugPostUrlChange() {
  const el = $("debug-target-post-url");
  if (el) localStorage.setItem("debugTargetPostUrl", el.value.trim());
}

function getDebugTestMessage() {
  const el = $("debug-test-message");
  return (el?.value || localStorage.getItem("debugTestMessage") || "").trim();
}

function onDebugTestMessageChange() {
  const el = $("debug-test-message");
  if (el) localStorage.setItem("debugTestMessage", el.value.trim());
}

function getDebugLikersTapOffset() {
  const el = $("debug-likers-tap-offset");
  const raw = el?.value ?? localStorage.getItem("debugLikersTapOffset") ?? "15";
  const n = parseInt(String(raw), 10);
  if (Number.isNaN(n)) return 15;
  return Math.max(0, Math.min(n, 500));
}

function onDebugLikersOffsetChange() {
  const el = $("debug-likers-tap-offset");
  if (el) localStorage.setItem("debugLikersTapOffset", el.value);
}

function getDebugLikersTapOffsetY() {
  const el = $("debug-likers-tap-offset-y");
  const raw = el?.value ?? localStorage.getItem("debugLikersTapOffsetY") ?? "15";
  const n = parseInt(String(raw), 10);
  if (Number.isNaN(n)) return 15;
  return Math.max(0, Math.min(n, 500));
}

function onDebugLikersOffsetYChange() {
  const el = $("debug-likers-tap-offset-y");
  if (el) localStorage.setItem("debugLikersTapOffsetY", el.value);
}

async function runDebugStepButton(stepId, statusMsg, errMsg) {
  if (!activeSerial) {
    setDebugStatus("Select a device on the Farm tab first", "error");
    return;
  }
  setDebugStatus(statusMsg, "info");
  try {
    const result = await executeDebugStep(stepId);
    applyDebugStepResult(stepId, result);
  } catch (err) {
    setDebugStatus(err.message || errMsg, "error");
  }
}

async function runLikersTapTest() {
  await runDebugStepButton("ig-post-tap-likers", "Testing likers tap…", "Likers tap test failed");
}

async function runFullscreenDetectTest() {
  await runDebugStepButton("ig-post-fullscreen-detect", "Detecting fullscreen video…", "Fullscreen detect failed");
}

async function runFullscreenRevealLikesTest() {
  await runDebugStepButton("ig-post-fullscreen-reveal-likes", "Revealing hidden likes…", "Reveal hidden likes failed");
}

async function runFullscreenLikersTapTest() {
  await runDebugStepButton("ig-post-fullscreen-likers", "Testing fullscreen likers tap…", "Fullscreen likers tap failed");
}

async function runFullscreenLikeStayTest() {
  await runDebugStepButton(
    "ig-post-fullscreen-like-stay",
    "Liking video and checking reel view stays open…",
    "Like & stay in reel failed"
  );
}

async function runFullscreenDetectCommentTest() {
  await runDebugStepButton(
    "ig-post-fullscreen-detect-comment",
    "Detecting fullscreen comment button…",
    "Fullscreen comment detect failed"
  );
}

async function runFullscreenOpenCommentsTest() {
  await runDebugStepButton(
    "ig-post-fullscreen-open-comments",
    "Opening fullscreen comments…",
    "Fullscreen open comments failed"
  );
}

async function runFullscreenSendCommentTest() {
  const msg = getDebugTestMessage();
  if (!msg) {
    setDebugStatus("Set a test comment message first (Test comment / PM field)", "error");
    return;
  }
  await runDebugStepButton(
    "ig-post-fullscreen-send-comment",
    "Sending fullscreen reel comment…",
    "Fullscreen send comment failed"
  );
}

async function runInlinePostDetectTest() {
  await runDebugStepButton(
    "ig-post-inline-detect",
    "Detecting inline profile Posts video…",
    "Inline video detect failed"
  );
}

async function runInlinePostLikeTest() {
  await runDebugStepButton(
    "ig-post-inline-like",
    "Liking inline profile Posts video…",
    "Inline like failed"
  );
}

async function runInlinePostOpenCommentsTest() {
  await runDebugStepButton(
    "ig-post-inline-open-comments",
    "Opening inline post comments…",
    "Inline open comments failed"
  );
}

async function runInlinePostSendCommentTest() {
  const msg = getDebugTestMessage();
  if (!msg) {
    setDebugStatus("Set a test comment message first (Test comment / PM field)", "error");
    return;
  }
  await runDebugStepButton(
    "ig-post-inline-send-comment",
    "Sending inline profile post comment…",
    "Inline send comment failed"
  );
}

function getDebugPostReelCount() {
  const el = $("debug-post-reel-count");
  const raw = el?.value ?? localStorage.getItem("debugPostReelCount") ?? "1";
  const n = parseInt(String(raw), 10);
  if (Number.isNaN(n)) return 1;
  return Math.max(1, Math.min(n, 20));
}

function onDebugPostReelCountChange() {
  const el = $("debug-post-reel-count");
  if (el) localStorage.setItem("debugPostReelCount", el.value);
}

async function runTelegramTest() {
  await runDebugStepButton("telegram-test-send", "Sending Telegram test message…", "Telegram test failed");
}

function initDebugTargetUsername() {
  const userEl = $("debug-target-username");
  if (userEl) {
    const savedUser = localStorage.getItem("debugTargetUsername");
    if (savedUser) userEl.value = savedUser;
  }
  const searchEl = $("debug-target-search");
  if (searchEl) {
    const savedSearch = localStorage.getItem("debugTargetSearch");
    if (savedSearch) searchEl.value = savedSearch;
  }
  const postUrlEl = $("debug-target-post-url");
  if (postUrlEl) {
    const savedUrl = localStorage.getItem("debugTargetPostUrl");
    if (savedUrl) postUrlEl.value = savedUrl;
  }
  const messageEl = $("debug-test-message");
  if (messageEl) {
    const savedMsg = localStorage.getItem("debugTestMessage");
    if (savedMsg) messageEl.value = savedMsg;
  }
  const offsetEl = $("debug-likers-tap-offset");
  if (offsetEl) {
    const savedOffset = localStorage.getItem("debugLikersTapOffset");
    if (savedOffset) offsetEl.value = savedOffset;
  }
  const offsetYEl = $("debug-likers-tap-offset-y");
  if (offsetYEl) {
    const savedOffsetY = localStorage.getItem("debugLikersTapOffsetY");
    if (savedOffsetY) offsetYEl.value = savedOffsetY;
  }
  const postReelEl = $("debug-post-reel-count");
  if (postReelEl) {
    const savedPostReel = localStorage.getItem("debugPostReelCount");
    if (savedPostReel) postReelEl.value = savedPostReel;
  }
  updateDebugTargetFieldVisibility();
}

function initVpnAppName() {
  if (!localStorage.getItem("vpnAppName")) {
    localStorage.setItem("vpnAppName", "Shadowrocket");
  }
}

async function executeDebugStep(testId, signal) {
  const res = await fetch(`/api/devices/${encodeURIComponent(activeSerial)}/debug/${encodeURIComponent(testId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      vpn_app_name: getVpnAppName(),
      target_username: getDebugTargetUsername(),
      target_search: getDebugTargetSearch(),
      target_post_url: getDebugTargetPostUrl(),
      test_message: getDebugTestMessage(),
      likers_tap_offset_x: getDebugLikersTapOffset(),
      likers_tap_offset_y: getDebugLikersTapOffsetY(),
      post_reel_posts_count: getDebugPostReelCount(),
    }),
    signal,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

async function executeDebugBatch(stepIds, signal) {
  const res = await fetch(`/api/devices/${encodeURIComponent(activeSerial)}/debug/run-batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      test_ids: stepIds,
      vpn_app_name: getVpnAppName(),
      target_username: getDebugTargetUsername(),
      target_search: getDebugTargetSearch(),
      target_post_url: getDebugTargetPostUrl(),
      test_message: getDebugTestMessage(),
      likers_tap_offset_x: getDebugLikersTapOffset(),
      likers_tap_offset_y: getDebugLikersTapOffsetY(),
      post_reel_posts_count: getDebugPostReelCount(),
    }),
    signal,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function applyDebugStepResult(stepId, result, { runAll = false } = {}) {
  const stepMeta = debugFlowSteps.find((s) => s.id === stepId);
  const label = stepMeta?.label || stepId;
  const ok = result.success !== false;
  log(`Debug ${label}: ${result.message || (ok ? "OK" : "failed")}`, ok ? "info" : "error");
  if (!ok) {
    setDebugStepState(stepId, "failed");
    setDebugStatus(result.message || "Test failed", "error");
    const failDetail = [result.message, result.target].filter(Boolean).join("\n\n");
    updateDebugStepDetail({
      step: stepMeta,
      state: "failed",
      stateMessage: "Step failed — could not find or tap the expected element.",
      resultText: failDetail,
      resultOk: false,
    });
    if (result.target) log(result.target, "error");
    showToolsInspector({ target: result.target || result.message });
    return false;
  }
  setDebugStepState(stepId, "done");
  updateDebugStepDetail({
    step: stepMeta,
    state: "done",
    stateMessage: "Step passed.",
    resultText: result.message || "OK",
    resultOk: true,
  });
  return true;
}

function requestDebugCancel(serial) {
  if (!serial) return Promise.resolve();
  return fetch(`/api/devices/${encodeURIComponent(serial)}/debug/cancel`, { method: "POST" });
}

async function isDebugBusy(serial) {
  if (!serial) return false;
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(serial)}/debug/status`);
    if (!res.ok) return false;
    const data = await res.json();
    return !!data.busy;
  } catch (_) {
    return false;
  }
}

async function waitForDebugIdle(serial, maxMs = 5000) {
  if (!serial) return true;
  const start = Date.now();
  while (Date.now() - start < maxMs) {
    if (!(await isDebugBusy(serial))) return true;
    await new Promise((r) => setTimeout(r, 150));
  }
  return false;
}

async function killDebugTest() {
  const serial = activeSerial;
  if (!serial) return;
  if (debugTestAbort) debugTestAbort.abort();
  setDebugStatus("Stopping…", "error");
  updateDebugStepDetail({
    state: "failed",
    stateMessage: "Stopping test and releasing device…",
    resultText: "Cancelling on server, then freeing the device lock.",
    resultOk: false,
  });
  log("Debug test stop requested", "error");
  document.querySelectorAll(".debug-step-item.running").forEach((el) => {
    el.classList.remove("running");
    el.classList.add("failed");
  });
  try {
    await requestDebugCancel(serial);
    const idle = await waitForDebugIdle(serial, 5000);
    if (!idle) {
      log("Device still busy — wait a moment before running again", "error");
    }
  } catch (err) {
    log(`Stop failed: ${err.message}`, "error");
  } finally {
    debugTestRunning = false;
    debugTestAbort = null;
    resetDebugRunButtons();
    const quickBtn = $("btn-ctx-quick-debug");
    if (quickBtn) quickBtn.textContent = "Debug";
    setDebugStatus("Test stopped", "error");
    updateContextStrip();
  }
}

async function runQuickDebug() {
  if (debugTestRunning) return;
  if (!activeSerial) {
    log("Select a device on Farm first", "error");
    return;
  }
  if (await isDebugBusy(activeSerial)) {
    const idle = await waitForDebugIdle(activeSerial, 6000);
    if (!idle) {
      log("Device busy — wait a moment or stop the current test", "error");
      return;
    }
  }
  const testId = localStorage.getItem("debugTestId") || "open-instagram";
  const select = $("debug-test-select");
  if (select) select.value = testId;

  let stepMeta = debugFlowSteps.find((s) => s.id === testId);
  if (!stepMeta) {
    await loadDebugTests(currentDebugFlow || "flow");
    stepMeta = debugFlowSteps.find((s) => s.id === testId);
  }
  if (!stepMeta) {
    for (const group of ["flow", "instagram", "vpn"]) {
      if (group === currentDebugFlow) continue;
      await loadDebugTests(group);
      stepMeta = debugFlowSteps.find((s) => s.id === testId);
      if (stepMeta) break;
    }
  }
  if (stepMeta?.needs_username && !getDebugTargetUsername()) {
    log("Quick debug needs a target @username — set it on the Tools tab", "error");
    return;
  }
  if (stepMeta?.needs_search && !getDebugTargetSearch()) {
    log("Quick debug needs a hashtag — set it on the Tools tab", "error");
    return;
  }
  if (stepMeta?.needs_post_url && !getDebugTargetPostUrl()) {
    log("Quick debug needs a post URL — set it on the Tools tab", "error");
    return;
  }
  if (stepMeta?.needs_test_message && !getDebugTestMessage()) {
    log("Quick debug needs a test message — set it on the Tools tab", "error");
    return;
  }

  const quickBtn = $("btn-ctx-quick-debug");
  const killBtn = $("btn-debug-kill");
  const controller = new AbortController();
  debugTestAbort = controller;
  debugTestRunning = true;
  updateContextStrip();
  if (quickBtn) quickBtn.textContent = "Running…";
  if (killBtn) killBtn.disabled = false;
  setDebugStepState(testId, "running");
  setDebugStatus(`Running ${stepMeta?.label || testId}…`);
  log(`Quick debug: ${stepMeta?.label || testId}`);
  startDebugLogPolling(activeSerial);
  try {
    const result = await executeDebugStep(testId, controller.signal);
    applyDebugStepResult(testId, result);
    setDebugStatus(result.message || "Done", result.success !== false ? "ok" : "error");
  } catch (err) {
    if (err.name === "AbortError") {
      log("Quick debug cancelled", "error");
    } else {
      log(`Quick debug failed: ${err.message}`, "error");
      setDebugStatus(err.message, "error");
      setDebugStepState(testId, "failed");
    }
  } finally {
    debugTestRunning = false;
    debugTestAbort = null;
    stopDebugLogPolling();
    if (quickBtn) quickBtn.textContent = "Debug";
    resetDebugRunButtons();
    updateContextStrip();
  }
}

async function runDebugTest(runAll) {
  if (debugTestRunning) return;
  if (!activeSerial) {
    setDebugStatus("Select a device on Farm first", "error");
    log("Select a device on Farm before running debug tests", "error");
    return;
  }
  if (await isDebugBusy(activeSerial)) {
    setDebugStatus("Waiting for device to finish stopping…", "error");
    const idle = await waitForDebugIdle(activeSerial, 6000);
    if (!idle) {
      setDebugStatus("Device still busy — try again in a moment", "error");
      return;
    }
  }
  const select = $("debug-test-select");
  const testId = select?.value;
  if (!testId) {
    setDebugStatus("Choose a debug test", "error");
    return;
  }
  const steps = runAll ? [...debugFlowStepIds] : [testId];
  const needsUsername = steps.some((id) => debugFlowSteps.find((s) => s.id === id)?.needs_username);
  const needsSearch = steps.some((id) => debugFlowSteps.find((s) => s.id === id)?.needs_search);
  if (needsUsername && !getDebugTargetUsername()) {
    setDebugStatus("Enter a target @username for this step", "error");
    return;
  }
  if (needsSearch && !getDebugTargetSearch()) {
    setDebugStatus("Enter a hashtag for this step", "error");
    return;
  }
  const needsPostUrl = steps.some((id) => debugFlowSteps.find((s) => s.id === id)?.needs_post_url);
  if (needsPostUrl && !getDebugTargetPostUrl()) {
    setDebugStatus("Enter an Instagram post URL for this step", "error");
    return;
  }
  const needsTestMessage = steps.some((id) => debugFlowSteps.find((s) => s.id === id)?.needs_test_message);
  if (needsTestMessage && !getDebugTestMessage()) {
    setDebugStatus("Enter a test comment / PM message for this step", "error");
    return;
  }
  const runBtn = $("btn-debug-run");
  const allBtn = $("btn-debug-run-all");
  const killBtn = $("btn-debug-kill");
  const controller = new AbortController();
  debugTestAbort = controller;
  debugTestRunning = true;
  updateContextStrip();
  if (runBtn) runBtn.disabled = true;
  if (allBtn) allBtn.disabled = true;
  if (killBtn) killBtn.disabled = false;
  resetDebugStepStates();
  let failed = false;
  startDebugLogPolling(activeSerial);
  try {
    if (runAll && steps.length > 1) {
      steps.forEach((stepId) => setDebugStepState(stepId, "running"));
      if (allBtn) allBtn.textContent = `Running ${steps.length} steps…`;
      setDebugStatus(`Running ${steps.length} steps on device…`);
      updateDebugStepDetail({
        state: "running",
        stateMessage: "Running all steps in one session — no reconnect delay.",
        resultText: "",
      });
      const batch = await executeDebugBatch(steps, controller.signal);
      const results = batch.results || [];
      for (let i = 0; i < results.length; i++) {
        const result = results[i];
        const stepId = result.test_id || steps[i];
        if (allBtn) allBtn.textContent = `Step ${i + 1}/${steps.length}…`;
        if (!applyDebugStepResult(stepId, result)) {
          failed = true;
          for (let j = i + 1; j < steps.length; j++) setDebugStepState(steps[j], "");
          break;
        }
      }
    } else {
      for (let i = 0; i < steps.length; i++) {
        const stepId = steps[i];
        const stepMeta = debugFlowSteps.find((s) => s.id === stepId);
        const label = stepMeta?.label || stepId;
        setDebugStepState(stepId, "running");
        setDebugStatus(`Running ${label}…`);
        updateDebugStepDetail({
          step: stepMeta,
          state: "running",
          stateMessage: "Searching device now — looking for:",
          resultText: "",
        });
        log(`Debug: ${label}`, "info");
        const result = await executeDebugStep(stepId, controller.signal);
        if (!applyDebugStepResult(stepId, result)) {
          failed = true;
          break;
        }
      }
    }
    if (!failed) {
      setDebugStatus(runAll ? `Completed ${steps.length} step(s)` : "Test passed", "success");
      if (currentMainTab === "tools") connectWeditor();
    }
  } catch (err) {
    const msg = err.name === "AbortError" ? "Test aborted" : err.message || String(err);
    setDebugStatus(msg, "error");
    log(`Debug failed: ${msg}`, "error");
    updateDebugStepDetail({
      state: "failed",
      stateMessage: "Test stopped or errored.",
      resultText: msg,
      resultOk: false,
    });
  } finally {
    debugTestRunning = false;
    debugTestAbort = null;
    stopDebugLogPolling();
    resetDebugRunButtons();
    const quickBtn = $("btn-ctx-quick-debug");
    if (quickBtn) quickBtn.textContent = "Debug";
    updateContextStrip();
  }
}

/* ── GramAddict forms ── */

let activeSaveField = null;
let inlineSaveHideTimer = null;

function inlineBadgeHost(container) {
  if (!container) return null;
  return (
    container.querySelector(":scope > label") ||
    container.querySelector(".ga-check-label") ||
    container.querySelector(".ga-form-section-title") ||
    container
  );
}

function clearInlineSaveStatus() {
  if (inlineSaveHideTimer) {
    clearTimeout(inlineSaveHideTimer);
    inlineSaveHideTimer = null;
  }
  document.querySelectorAll(".field-save-badge").forEach((el) => el.remove());
}

function setActiveSaveField(container) {
  if (container === activeSaveField) return;
  clearInlineSaveStatus();
  activeSaveField = container || null;
}

function setInlineSaveStatus(message, level) {
  if (!activeSaveField || !activeSaveField.isConnected) return;
  const host = inlineBadgeHost(activeSaveField);
  if (!host) return;
  let badge = host.querySelector(".field-save-badge");
  if (!message) {
    if (badge) badge.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement("span");
    badge.className = "field-save-badge";
    host.appendChild(badge);
  }
  badge.textContent = message;
  badge.className = "field-save-badge" + (level ? ` ${level}` : "");
  if (inlineSaveHideTimer) {
    clearTimeout(inlineSaveHideTimer);
    inlineSaveHideTimer = null;
  }
  if (level === "success") {
    const target = badge;
    inlineSaveHideTimer = setTimeout(() => {
      if (target && target.isConnected) target.remove();
    }, 1800);
  }
}

function setGaStatus(message, level = "") {
  setInlineSaveStatus(message, level);
  const el = $("ga-config-status");
  if (!el) return;
  el.textContent = message || "";
  el.className = "adv-status-line" + (level ? ` ${level}` : "");
}

function setAdvFileStatus(message, level = "") {
  const el = $("adv-file-status");
  if (!el) return;
  el.textContent = message || "";
  el.className = "adv-status-line" + (level ? ` ${level}` : "");
}

function setBundleStatus(message, level = "") {
  const el = $("account-bundle-status");
  if (!el) return;
  el.textContent = message || "";
  el.className = "adv-status-line mt-2" + (level ? ` ${level}` : "");
}

function openBundleTab(tab) {
  if (!tab || tab === "files") return;
  setAccountTab(tab);
}

function renderAccountBundle(bundle) {
  const tbody = $("account-bundle-body");
  if (!tbody || !bundle?.files) return;
  tbody.innerHTML = bundle.files
    .map((file) => {
      const tabLabel = ACCOUNT_TAB_LABELS[file.tab] || file.tab;
      const status = file.present
        ? '<span class="bundle-status ok">Present</span>'
        : '<span class="bundle-status missing">Missing</span>';
      const openBtn =
        file.tab && file.tab !== "files"
          ? `<button type="button" class="btn-ghost btn-sm bundle-open-btn" onclick="openBundleTab('${file.tab}')">Open tab</button>`
          : "";
      return `<tr>
        <td><code title="${escapeHtml(file.description || "")}">${escapeHtml(file.name)}</code><div class="text-xs text-muted-foreground mt-0.5">${fieldLabelHtml(file.label || "", file.description)}</div></td>
        <td class="text-muted-foreground">${escapeHtml(file.description || "")}</td>
        <td>${escapeHtml(tabLabel)}</td>
        <td>${status}</td>
        <td>${openBtn}</td>
      </tr>`;
    })
    .join("");
  if (bundle.missing?.length) {
    setBundleStatus(`${bundle.missing.length} file(s) missing — click Add missing files to copy from config-examples.`);
  } else {
    setBundleStatus("All standard account files are present.", "success");
  }
}

async function loadAccountBundle() {
  if (!gaCurrentAccountId) return;
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/bundle`);
    if (!res.ok) throw new Error(await res.text());
    const bundle = await res.json();
    renderAccountBundle(bundle);
  } catch (err) {
    setBundleStatus(err.message, "error");
  }
}

async function ensureAccountFiles() {
  if (!gaCurrentAccountId) return;
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/ensure-files`, {
      method: "POST",
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to add files");
    renderAccountBundle(data);
    if (data.added?.length) {
      setBundleStatus(`Added: ${data.added.join(", ")}`, "success");
      log(`Added missing files for ${gaCurrentAccountId}: ${data.added.join(", ")}`);
      await loadAccountFiles();
      if (currentMainTab === "tools") await loadAdvFiles();
    } else {
      setBundleStatus("No missing files — account folder is complete.", "success");
    }
  } catch (err) {
    setBundleStatus(err.message, "error");
  }
}

function fieldLabelHtml(label, help) {
  const safeLabel = escapeHtml(label);
  if (!help) return safeLabel;
  return `<span class="field-label-with-help" tabindex="0">${safeLabel}<span class="field-help-tip" role="tooltip">${escapeHtml(help)}</span></span>`;
}

function tooltipInnerHtml(title, body) {
  const parts = [];
  if (title) parts.push(`<span class="field-help-title">${escapeHtml(title)}</span>`);
  if (body) parts.push(`<span class="field-help-body">${escapeHtml(body)}</span>`);
  return parts.join("");
}

function configKeyLabelHtml(key, help) {
  const keyEl = `<code class="ga-field-key">${escapeHtml(key)}</code>`;
  if (!help) return keyEl;
  return `<span class="field-label-with-help" tabindex="0">${keyEl}<span class="field-help-tip" role="tooltip">${escapeHtml(help)}</span></span>`;
}

function gaFieldLabelHtml(field) {
  const keyEl = `<code class="ga-field-key">${escapeHtml(field.key || "")}</code>`;
  const tip = tooltipInnerHtml(field.label, field.help);
  if (!tip) return keyEl;
  return `<span class="field-label-with-help" tabindex="0">${keyEl}<span class="field-help-tip" role="tooltip">${tip}</span></span>`;
}

function gaTimeToInput(gaTime) {
  if (!gaTime) return "";
  const [hour, minute = "0"] = String(gaTime).trim().split(".");
  return `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
}

function inputTimeToGa(inputTime) {
  if (!inputTime) return "";
  const [hour, minute] = inputTime.split(":");
  return `${parseInt(hour, 10)}.${minute}`;
}

function parseWorkingHoursValue(value) {
  const text = String(value || "").trim();
  if (!text) return [];
  return text
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [start = "", end = ""] = part.split("-").map((s) => s.trim());
      return { start, end };
    })
    .filter((range) => range.start && range.end);
}

function formatWorkingHoursValue(ranges) {
  return ranges
    .map(({ start, end }) => `${start}-${end}`)
    .filter(Boolean)
    .join(", ");
}

function formatTimeReadable(inputTime) {
  if (!inputTime) return "";
  const [hour, minute] = inputTime.split(":").map((v) => parseInt(v, 10));
  if (Number.isNaN(hour) || Number.isNaN(minute)) return "";
  const date = new Date();
  date.setHours(hour, minute, 0, 0);
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function workingHoursRangeSummary(startInput, endInput) {
  if (!startInput || !endInput) return "";
  return `${formatTimeReadable(startInput)} – ${formatTimeReadable(endInput)}`;
}

function renderWorkingHoursRangeRow(range = { start: "", end: "" }) {
  const start = gaTimeToInput(range.start);
  const end = gaTimeToInput(range.end);
  const summary = workingHoursRangeSummary(start, end);
  return `
    <div class="working-hours-range">
      <div class="working-hours-range-inputs">
        <label class="working-hours-time-label">
          <span class="working-hours-time-caption">Start</span>
          <input type="time" class="ga-input wh-start" value="${start}">
        </label>
        <span class="working-hours-sep" aria-hidden="true">to</span>
        <label class="working-hours-time-label">
          <span class="working-hours-time-caption">End</span>
          <input type="time" class="ga-input wh-end" value="${end}">
        </label>
      </div>
      <p class="working-hours-summary">${summary ? escapeHtml(summary) : "Pick start and end times"}</p>
      <button type="button" class="btn-ghost btn-sm wh-remove" title="Remove this time window">Remove</button>
    </div>`;
}

function populateWorkingHoursWidget(widget, value) {
  if (!widget) return;
  const hidden = widget.querySelector('input[type="hidden"][data-ga-key="working-hours"]');
  const rangesEl = widget.querySelector(".working-hours-ranges");
  if (!hidden || !rangesEl) return;
  const ranges = parseWorkingHoursValue(value);
  hidden.value = formatWorkingHoursValue(ranges);
  rangesEl.innerHTML = ranges.length
    ? ranges.map((range) => renderWorkingHoursRangeRow(range)).join("")
    : renderWorkingHoursRangeRow();
}

function syncWorkingHoursWidget(widget) {
  if (!widget) return;
  const hidden = widget.querySelector('input[type="hidden"][data-ga-key="working-hours"]');
  if (!hidden) return;
  const ranges = [];
  widget.querySelectorAll(".working-hours-range").forEach((row) => {
    const start = row.querySelector(".wh-start")?.value;
    const end = row.querySelector(".wh-end")?.value;
    const summary = row.querySelector(".working-hours-summary");
    if (summary) {
      summary.textContent = start && end ? workingHoursRangeSummary(start, end) : "Pick start and end times";
    }
    if (start && end) ranges.push(`${inputTimeToGa(start)}-${inputTimeToGa(end)}`);
  });
  hidden.value = formatWorkingHoursValue(
    ranges.map((part) => {
      const [start, end] = part.split("-");
      return { start, end };
    })
  );
  hidden.dispatchEvent(new Event("input", { bubbles: true }));
}

function refreshAllWorkingHoursWidgets() {
  document.querySelectorAll(".working-hours-widget").forEach((widget) => {
    const hidden = widget.querySelector('input[type="hidden"][data-ga-key="working-hours"]');
    populateWorkingHoursWidget(widget, hidden?.value || "");
  });
}

function bindWorkingHoursWidgets() {
  const root = $("ga-config-fields");
  if (!root || root.dataset.workingHoursBound === "1") return;
  root.dataset.workingHoursBound = "1";
  root.addEventListener("input", (event) => {
    const row = event.target.closest(".working-hours-range");
    if (!row) return;
    if (!event.target.matches(".wh-start, .wh-end")) return;
    syncWorkingHoursWidget(row.closest(".working-hours-widget"));
  });
  root.addEventListener("click", (event) => {
    const widget = event.target.closest(".working-hours-widget");
    if (!widget) return;
    if (event.target.closest(".wh-add")) {
      event.preventDefault();
      const rangesEl = widget.querySelector(".working-hours-ranges");
      rangesEl?.insertAdjacentHTML("beforeend", renderWorkingHoursRangeRow());
      syncWorkingHoursWidget(widget);
      return;
    }
    if (event.target.closest(".wh-remove")) {
      event.preventDefault();
      const row = event.target.closest(".working-hours-range");
      const rangesEl = widget.querySelector(".working-hours-ranges");
      const rows = rangesEl?.querySelectorAll(".working-hours-range") || [];
      if (rows.length <= 1) {
        row.querySelector(".wh-start").value = "";
        row.querySelector(".wh-end").value = "";
      } else {
        row.remove();
      }
      syncWorkingHoursWidget(widget);
    }
  });
}

function inlineFieldSubKeys(field) {
  const key = field.key;
  if (field.type === "inline-file-job") {
    return { listKey: `${key}-list`, limitKey: `${key}-limit` };
  }
  if (field.type === "inline-lines-file") {
    return { listKey: `${key}-list` };
  }
  return null;
}

const POST_REEL_ACCEPT =
  "video/mp4,video/quicktime,video/webm,video/x-m4v,.mp4,.mov,.m4v,.webm,.mkv";

function renderFormField(field, attr = "data-ga-key") {
  const key = field.key;
  const label = gaFieldLabelHtml(field);
  const placeholder = field.placeholder ? escapeHtml(field.placeholder) : "";
  if (field.key === "post-reels") {
    const countPh = placeholder || "3";
    return `
      <div class="field post-reels-field field-span-2">
        <label>${label}</label>
        <div class="post-reels-row">
          <input type="text" ${attr}="${key}" class="ga-input post-reels-count" placeholder="${countPh}" autocomplete="off">
          <button type="button" class="btn-ghost btn-sm post-reels-choose hidden" data-post-reels-choose>Choose file</button>
          <input type="file" class="sr-only" data-post-reels-input accept="${POST_REEL_ACCEPT}" multiple>
        </div>
        <ul class="post-reel-media-list post-reels-inline-list" aria-live="polite"></ul>
        <p class="adv-status-line post-reels-inline-status"></p>
      </div>`;
  }
  if (field.type === "inline-file-job") {
    const sub = inlineFieldSubKeys(field);
    const linesPlaceholder = placeholder || "One username per line";
    const limitPh = escapeHtml(field.limit_placeholder || "10-15");
    const fileHint = field.file ? escapeHtml(field.file) : "targets.txt";
    const limitLabel = configKeyLabelHtml(sub.limitKey, field.limit_help || undefined);
    return `
      <div class="field inline-file-job-field field-span-2">
        <label>${label}</label>
        <p class="inline-file-hint">Edit here — saved as <code>${fileHint}</code> automatically.</p>
        <textarea ${attr}="${sub.listKey}" class="ga-input" rows="4" placeholder="${linesPlaceholder}"></textarea>
        <div class="inline-file-limit-row">
          <label class="inline-file-sublabel">${limitLabel}</label>
          <input type="text" ${attr}="${sub.limitKey}" class="ga-input inline-file-limit" placeholder="${limitPh}" autocomplete="off">
        </div>
      </div>`;
  }
  if (field.type === "inline-lines-file") {
    const sub = inlineFieldSubKeys(field);
    const linesPlaceholder = placeholder || "One URL per line";
    const fileHint = field.file ? escapeHtml(field.file) : "post_urls.txt";
    return `
      <div class="field inline-lines-file-field field-span-2">
        <label>${label}</label>
        <p class="inline-file-hint">Edit here — saved as <code>${fileHint}</code> automatically.</p>
        <textarea ${attr}="${sub.listKey}" class="ga-input" rows="4" placeholder="${linesPlaceholder}"></textarea>
      </div>`;
  }
  if (field.type === "bool") {
    return `<label class="ga-check"><input type="checkbox" class="ui-checkbox" ${attr}="${key}"><span class="ga-check-label">${label}</span></label>`;
  }
  if (field.type === "lines") {
    const linesPlaceholder = placeholder || "One per line";
    return `<div class="field"><label>${label}</label><textarea ${attr}="${key}" class="ga-input" rows="2" placeholder="${linesPlaceholder}"></textarea></div>`;
  }
  if (field.type === "textarea") {
    return `<div class="field field-span-2"><label>${label}</label><textarea ${attr}="${key}" class="ga-input" rows="4" spellcheck="true" placeholder="${placeholder}"></textarea></div>`;
  }
  if (field.type === "device") {
    return `<div class="field"><label>${label}</label><select ${attr}="${key}" class="ga-input"></select></div>`;
  }
  if (field.type === "select" && field.options?.length) {
    const brandLabels = {
      "": "None (this account only)",
      "615films": "615Films",
      ylf: "YLF",
    };
    const opts = field.options
      .map((o) => {
        const label = field.key === "brand-pool" ? brandLabels[o] || o : o;
        return `<option value="${escapeHtml(o)}">${escapeHtml(label)}</option>`;
      })
      .join("");
    return `<div class="field"><label>${label}</label><select ${attr}="${key}" class="ga-input">${opts}</select></div>`;
  }
  if (field.type === "password") {
    return `<div class="field"><label>${label}</label><input type="password" ${attr}="${key}" class="ga-input" placeholder="${placeholder}" autocomplete="off"></div>`;
  }
  if (field.type === "working-hours") {
    return `
      <div class="field working-hours-field">
        <label>${label}</label>
        <div class="working-hours-widget">
          <input type="hidden" ${attr}="${key}" value="">
          <div class="working-hours-ranges"></div>
          <button type="button" class="btn-ghost btn-sm wh-add">Add time window</button>
        </div>
      </div>`;
  }
  return `<div class="field"><label>${label}</label><input type="text" ${attr}="${key}" class="ga-input" placeholder="${placeholder}" autocomplete="off"></div>`;
}

function renderSectionsInto(containerId, sectionIds, schema, attr = "data-ga-key") {
  const container = $(containerId);
  if (!container || !schema?.sections) return;
  const labels = schema.labels || {};
  const sectionHelp = schema.section_help || {};
  const collapsed = new Set(schema.collapsed || []);
  const parts = [];
  for (const sectionId of sectionIds) {
    const fields = schema.sections[sectionId];
    if (!fields?.length) continue;
    const title = labels[sectionId] || sectionId;
    const titleHtml = fieldLabelHtml(title, sectionHelp[sectionId]);
    const boolFields = fields.filter((f) => f.type === "bool");
    const otherFields = fields.filter((f) => f.type !== "bool");
    const isCollapsed = collapsed.has(sectionId);

    if (isCollapsed) {
      parts.push('<details class="ga-form-section ga-form-section-collapsible">');
      parts.push(`<summary class="ga-form-section-title ga-form-section-summary">${titleHtml}</summary>`);
    } else {
      parts.push(`<div class="ga-form-section"><div class="ga-form-section-title">${titleHtml}</div>`);
    }
    if (otherFields.length) {
      parts.push('<div class="adv-grid-2">');
      otherFields.forEach((f) => parts.push(renderFormField(f, attr)));
      parts.push("</div>");
    }
    if (boolFields.length) {
      parts.push('<div class="ga-check-row">');
      boolFields.forEach((f) => parts.push(renderFormField(f, attr)));
      parts.push("</div>");
    }
    parts.push(isCollapsed ? "</details>" : "</div>");
  }
  container.innerHTML = parts.join("");
}

function renderTerminologyKey() {
  const container = $("ga-terminology-key");
  const terms = gaSchema?.terminology;
  if (!container) return;
  if (!terms?.length) {
    container.innerHTML = "";
    container.classList.add("hidden");
    return;
  }
  container.classList.remove("hidden");
  const items = terms
    .map(
      (t) =>
        `<div class="ga-terminology-item"><dt>${escapeHtml(t.term)}</dt><dd>${escapeHtml(t.definition)}</dd></div>`
    )
    .join("");
  container.innerHTML = `
    <details class="ga-terminology-details" open>
      <summary class="ga-terminology-summary">Key terms</summary>
      <p class="ga-terminology-lead">How GramAddict uses these words in your config and session estimates.</p>
      <dl class="ga-terminology-list">${items}</dl>
    </details>`;
}

function renderAccountForms() {
  if (!gaSchema) return;
  const tabs = gaSchema.tabs || {};
  renderTerminologyKey();
  renderSectionsInto("ga-form-basics", tabs.basics || ["general"], gaSchema);
  renderSectionsInto("ga-form-jobs", tabs.jobs || [], gaSchema);
  renderSectionsInto("ga-form-limits", tabs.limits || [], gaSchema);
  renderSectionsInto("ga-form-schedule", tabs.schedule || [], gaSchema);
  renderSectionsInto("ga-form-reports", tabs.reports || [], gaSchema);
  populateGaDeviceSelects();
}

function renderFiltersForm() {
  if (!gaFiltersSchema) return;
  const sectionIds = Object.keys(gaFiltersSchema.sections || {});
  renderSectionsInto("ga-form-filters", sectionIds, gaFiltersSchema, "data-filter-key");
}

function renderTelegramForm() {
  const container = $("ga-form-telegram");
  if (!container || !gaTelegramSchema?.fields) return;
  container.innerHTML = gaTelegramSchema.fields.map((f) => renderFormField(f, "data-tg-key")).join("");
}

function renderPostReelForm() {
  const container = $("ga-form-post-reel");
  if (!container || !gaPostReelSchema?.fields) return;
  container.innerHTML = gaPostReelSchema.fields.map((f) => renderFormField(f, "data-pr-key")).join("");
}

function renderFollowVisionForm() {
  const container = $("ga-form-follow-vision");
  if (!container || !gaFollowVisionSchema?.fields) return;
  container.innerHTML = gaFollowVisionSchema.fields
    .map((f) => renderFormField(f, "data-fv-key"))
    .join("");
}

function fillPostReelPrompts(prompts) {
  const p615 = $("post-reel-prompt-615");
  const pylf = $("post-reel-prompt-ylf");
  if (p615 && prompts?.["615FILMS"] != null) p615.value = prompts["615FILMS"];
  if (pylf && prompts?.YourLoveFilms != null) pylf.value = prompts.YourLoveFilms;
}

function collectPostReelPrompts() {
  return {
    "615FILMS": ($("post-reel-prompt-615")?.value || "").trim(),
    YourLoveFilms: ($("post-reel-prompt-ylf")?.value || "").trim(),
  };
}

function fillFollowVisionPrompts(prompts) {
  const p615 = $("follow-vision-prompt-615");
  const pylf = $("follow-vision-prompt-ylf");
  if (p615 && prompts?.["615FILMS"] != null) p615.value = prompts["615FILMS"];
  if (pylf && prompts?.YourLoveFilms != null) pylf.value = prompts.YourLoveFilms;
}

function collectFollowVisionPrompts() {
  return {
    "615FILMS": ($("follow-vision-prompt-615")?.value || "").trim(),
    YourLoveFilms: ($("follow-vision-prompt-ylf")?.value || "").trim(),
  };
}

function setPostReelMediaHint(text) {
  const el = $("post-reel-media-hint");
  if (el) el.textContent = text || "";
}

const POST_REEL_VIDEO_RE = /\.(mp4|mov|m4v|webm|mkv)$/i;

// Map MIME types to a fallback extension. Files dragged from macOS Photos,
// Quick Look, or screen recordings sometimes arrive without a filename
// extension, so we fall back to the browser-reported MIME type.
const POST_REEL_MIME_EXT = {
  "video/mp4": ".mp4",
  "video/quicktime": ".mov",
  "video/x-m4v": ".m4v",
  "video/webm": ".webm",
  "video/x-matroska": ".mkv",
};

function hasFileExtension(name) {
  return /\.[^./\\]+$/.test(name || "");
}

function isPostReelVideo(file) {
  const name = file.name || "";
  if (POST_REEL_VIDEO_RE.test(name)) return true;
  const type = (file.type || "").toLowerCase();
  if (POST_REEL_MIME_EXT[type]) return true;
  if (type.startsWith("video/")) return true;
  // Extensionless downloads (e.g. Instagram/Facebook CDN blobs like "AQM...")
  // often report an empty or generic MIME type. In a video-only dropzone,
  // accept them and default to .mp4 on upload.
  if (!hasFileExtension(name) && (type === "" || type === "application/octet-stream")) {
    return true;
  }
  return false;
}

function postReelUploadName(file) {
  const name = file.name || "video";
  if (POST_REEL_VIDEO_RE.test(name)) return name;
  const type = (file.type || "").toLowerCase();
  const ext = POST_REEL_MIME_EXT[type] || (type.startsWith("video/") ? ".mp4" : "");
  const base = name.replace(/\.[^./\\]+$/, "") || `video-${Date.now()}`;
  return `${base}${ext || ".mp4"}`;
}

function setPostReelUploadStatus(message, kind = "") {
  const targets = [
    $("post-reel-upload-status"),
    ...document.querySelectorAll(".post-reels-inline-status"),
  ].filter(Boolean);
  targets.forEach((el) => {
    el.textContent = message || "";
    el.className = `adv-status-line mt-2${kind ? ` ${kind}` : ""}`;
  });
}

function renderPostReelMediaList(files) {
  const lists = document.querySelectorAll(".post-reel-media-list");
  if (!lists.length) return;
  lists.forEach((list) => {
    const inline = list.classList.contains("post-reels-inline-list");
    if (!files?.length) {
      list.innerHTML = inline
        ? '<li class="post-reel-media-empty">No videos yet — choose a file above.</li>'
        : '<li class="post-reel-media-empty">No videos yet — drag files above.</li>';
      return;
    }
    list.innerHTML = files
      .map(
        (f, i) => `
    <li class="post-reel-media-item">
      <span class="post-reel-media-item-name">${i + 1}. ${escapeHtml(f.name)}</span>
      <span class="post-reel-media-item-meta">${escapeHtml(f.size_label || "")}</span>
      <button type="button" class="btn-ghost btn-sm" data-post-reel-delete="${escapeHtml(f.name)}" title="Remove">×</button>
    </li>`
      )
      .join("");
    list.querySelectorAll("[data-post-reel-delete]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        deletePostReelMedia(btn.getAttribute("data-post-reel-delete"));
      });
    });
  });
}

function postReelsCountValue() {
  const input = document.querySelector('.post-reels-count[data-ga-key="post-reels"]');
  if (!input) return 0;
  const match = String(input.value || "").match(/\d+/);
  return match ? parseInt(match[0], 10) : 0;
}

function updatePostReelsInline() {
  const field = document.querySelector(".post-reels-field");
  if (!field) return;
  const show = postReelsCountValue() > 0;
  field.classList.toggle("post-reels-active", show);
  const btn = field.querySelector("[data-post-reels-choose]");
  if (btn) btn.classList.toggle("hidden", !show);
  const list = field.querySelector(".post-reels-inline-list");
  if (list) list.classList.toggle("hidden", !show);
  const status = field.querySelector(".post-reels-inline-status");
  if (status) status.classList.toggle("hidden", !show);
}

async function loadPostReelMedia() {
  if (!gaCurrentAccountId) {
    renderPostReelMediaList([]);
    return;
  }
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/post-reel/media`
    );
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    renderPostReelMediaList(data.files || []);
  } catch (err) {
    renderPostReelMediaList([]);
    setPostReelUploadStatus(err.message || "Could not load videos", "error");
  }
}

async function uploadPostReelFiles(fileList) {
  if (!gaCurrentAccountId) {
    setPostReelUploadStatus("Select an account first", "error");
    return;
  }
  const files = [...fileList].filter(isPostReelVideo);
  if (!files.length) {
    setPostReelUploadStatus("No supported video files (.mp4, .mov, .m4v, .webm, .mkv)", "error");
    return;
  }
  const dropzone = $("post-reel-dropzone");
  dropzone?.classList.add("is-uploading");
  setPostReelUploadStatus(`Uploading ${files.length} file(s)…`, "");
  let uploaded = 0;
  try {
    for (const file of files) {
      const form = new FormData();
      form.append("file", file, postReelUploadName(file));
      const res = await fetch(
        `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/post-reel/media`,
        { method: "POST", body: form }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Upload failed: ${file.name}`);
      uploaded += 1;
      renderPostReelMediaList(data.files || []);
    }
    setPostReelUploadStatus(`Uploaded ${uploaded} video(s)`, "success");
    log(`Uploaded ${uploaded} reel video(s) for ${gaCurrentAccountId}`);
  } catch (err) {
    setPostReelUploadStatus(err.message || "Upload failed", "error");
  } finally {
    dropzone?.classList.remove("is-uploading");
  }
}

async function deletePostReelMedia(filename) {
  if (!gaCurrentAccountId || !filename) return;
  if (!window.confirm(`Remove ${filename} from post_media?`)) return;
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/post-reel/media/${encodeURIComponent(filename)}`,
      { method: "DELETE" }
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Delete failed");
    renderPostReelMediaList(data.files || []);
    setPostReelUploadStatus(`Removed ${filename}`, "success");
  } catch (err) {
    setPostReelUploadStatus(err.message || "Delete failed", "error");
  }
}

function bindPostReelDropzone() {
  const dropzone = $("post-reel-dropzone");
  const input = $("post-reel-file-input");
  if (!dropzone || !input || dropzone.dataset.bound === "1") return;
  dropzone.dataset.bound = "1";

  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      input.click();
    }
  });
  input.addEventListener("change", () => {
    if (input.files?.length) uploadPostReelFiles(input.files);
    input.value = "";
  });

  ["dragenter", "dragover"].forEach((type) => {
    dropzone.addEventListener(type, (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      dropzone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    dropzone.addEventListener(type, (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      dropzone.classList.remove("is-dragover");
    });
  });
  dropzone.addEventListener("drop", (ev) => {
    if (ev.dataTransfer?.files?.length) uploadPostReelFiles(ev.dataTransfer.files);
  });
}

const COMMENT_SECTIONS = [
  {
    key: "PHOTO",
    label: "Photo posts",
    hint: "Used when the bot comments on a single photo.",
    placeholder: "Love this shot!\nSo good 🔥",
  },
  {
    key: "VIDEO",
    label: "Videos & Reels",
    hint: "Used for videos, Reels, and IGTV.",
    placeholder: "Great video!\nThis is awesome",
  },
  {
    key: "CAROUSEL",
    label: "Carousels",
    hint: "Used when the post has multiple slides.",
    placeholder: "Love this carousel!\nAmazing content",
  },
];

function isPlaceholderCommentLine(line) {
  const trimmed = line.trim();
  return !trimmed || trimmed === "..." || /^comment \d+ for /i.test(trimmed) || /^private message \d+$/i.test(trimmed);
}

function parseCommentsList(content) {
  const sections = { PHOTO: [], VIDEO: [], CAROUSEL: [] };
  let current = null;
  for (const line of String(content || "").split("\n")) {
    const trimmed = line.trim();
    if (trimmed === "%PHOTO") {
      current = "PHOTO";
      continue;
    }
    if (trimmed === "%VIDEO") {
      current = "VIDEO";
      continue;
    }
    if (trimmed === "%CAROUSEL") {
      current = "CAROUSEL";
      continue;
    }
    if (!current || isPlaceholderCommentLine(line)) continue;
    sections[current].push(line);
  }
  return sections;
}

function serializeCommentsList(sections) {
  const parts = [];
  for (const { key } of COMMENT_SECTIONS) {
    parts.push(`%${key}`);
    const lines = (sections[key] || []).map((line) => line.trim()).filter((line) => line && !isPlaceholderCommentLine(line));
    if (lines.length) parts.push(...lines);
  }
  return `${parts.join("\n")}\n`;
}

function renderCommentsListEditor(labelText, help) {
  const label = fieldLabelHtml(labelText, help);
  const sections = COMMENT_SECTIONS.map(
    (section) => `
      <div class="comments-section-card" data-comment-section="${section.key}">
        <div class="comments-section-head">
          <div class="comments-section-title">${escapeHtml(section.label)}</div>
          <p class="comments-section-hint">${escapeHtml(section.hint)}</p>
        </div>
        <textarea class="ga-input comments-section-input" rows="5" spellcheck="true"
                  placeholder="${escapeHtml(section.placeholder)}" aria-label="${escapeHtml(section.label)}"></textarea>
      </div>`
  ).join("");
  return `
    <div class="account-file-editor field comments-list-field">
      <label>${label}</label>
      <div class="comments-list-widget">
        <input type="hidden" data-file-key="comments_list.txt" value="">
        <div class="comments-sections">${sections}</div>
      </div>
    </div>`;
}

function renderPmListEditor(labelText, help) {
  const label = fieldLabelHtml(labelText, help);
  return `
    <div class="account-file-editor field">
      <label for="file-pm_list.txt">${label}</label>
      <p class="comments-section-hint mb-2">One message per line. The bot randomly picks one when sending a DM.</p>
      <textarea id="file-pm_list.txt" data-file-key="pm_list.txt" class="ga-input" rows="8" spellcheck="true"
                placeholder="Hey! Love your content\nThanks for the inspiration!"></textarea>
    </div>`;
}

function populateCommentsListWidget(widget, content) {
  if (!widget) return;
  const hidden = widget.querySelector('input[type="hidden"][data-file-key="comments_list.txt"]');
  if (!hidden) return;
  const sections = parseCommentsList(content);
  hidden.value = serializeCommentsList(sections);
  COMMENT_SECTIONS.forEach((section) => {
    const card = widget.querySelector(`[data-comment-section="${section.key}"]`);
    const input = card?.querySelector(".comments-section-input");
    if (input) input.value = (sections[section.key] || []).join("\n");
  });
}

function syncCommentsListWidget(widget) {
  if (!widget) return;
  const hidden = widget.querySelector('input[type="hidden"][data-file-key="comments_list.txt"]');
  if (!hidden) return;
  const sections = {};
  COMMENT_SECTIONS.forEach((section) => {
    const card = widget.querySelector(`[data-comment-section="${section.key}"]`);
    const input = card?.querySelector(".comments-section-input");
    sections[section.key] = String(input?.value || "")
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !isPlaceholderCommentLine(line));
  });
  hidden.value = serializeCommentsList(sections);
  hidden.dispatchEvent(new Event("input", { bubbles: true }));
}

function refreshCommentsListWidgets() {
  document.querySelectorAll(".comments-list-widget").forEach((widget) => {
    const hidden = widget.querySelector('input[type="hidden"][data-file-key="comments_list.txt"]');
    populateCommentsListWidget(widget, hidden?.value || "");
  });
}

function bindCommentsListEditors() {
  const root = $("ga-config-fields");
  if (!root || root.dataset.commentsListBound === "1") return;
  root.dataset.commentsListBound = "1";
  root.addEventListener("input", (event) => {
    const widget = event.target.closest(".comments-list-widget");
    if (!widget || !event.target.matches(".comments-section-input")) return;
    syncCommentsListWidget(widget);
  });
}

function renderFileEditors() {
  const listsEl = $("ga-form-lists");
  const commentsEl = $("ga-form-comments");
  if (!gaFilesMeta) return;
  const fileHelp = gaFilesMeta.file_help || {};
  const renderListEditor = ([name, labelText]) => {
    const help = [labelText, fileHelp[name] || ""].filter(Boolean).join(" ");
    const label = configKeyLabelHtml(name, help || undefined);
    return `
        <div class="account-file-editor field">
          <label for="file-${name}">${label}</label>
          <textarea id="file-${name}" data-file-key="${name}" class="ga-input" rows="8" spellcheck="false" placeholder="One username per line"></textarea>
        </div>`;
  };
  const renderEditor = ([name, labelText]) => {
    if (name === "comments_list.txt") return renderCommentsListEditor(labelText, fileHelp[name] || "");
    if (name === "pm_list.txt") return renderPmListEditor(labelText, fileHelp[name] || "");
    const help = [labelText, fileHelp[name] || ""].filter(Boolean).join(" ");
    const label = configKeyLabelHtml(name, help || undefined);
    return `
        <div class="account-file-editor field">
          <label for="file-${name}">${label}</label>
          <textarea id="file-${name}" data-file-key="${name}" class="ga-input" rows="8" spellcheck="false"></textarea>
        </div>`;
  };
  if (listsEl) {
    listsEl.innerHTML = Object.entries(gaFilesMeta.lists || {}).map(renderListEditor).join("");
  }
  if (commentsEl) {
    commentsEl.innerHTML = Object.entries(gaFilesMeta.text || {}).map(renderEditor).join("");
  }
  bindCommentsListEditors();
}

function populateGaDeviceSelects(selected) {
  document.querySelectorAll('select[data-ga-key="device"]').forEach((select) => {
    const options = ['<option value="">Use selected phone</option>'];
    devices.forEach((d) => {
      const label = `${shortSerial(d.serial)} — ${d.model || d.serial}`;
      options.push(`<option value="${d.serial}">${label}</option>`);
    });
    select.innerHTML = options.join("");
    const value = selected ?? select.value;
    if (value && [...select.options].some((o) => o.value === value)) select.value = value;
  });
}

function fillFields(attr, form) {
  if (!form) return;
  beginGaFormLoad();
  try {
    document.querySelectorAll(`[${attr}]`).forEach((el) => {
      const key = el.getAttribute(attr);
      const value = form[key];
      if (el.type === "checkbox") el.checked = !!value;
      else if (value !== undefined && value !== null) el.value = value;
      else el.value = "";
    });
  } finally {
    endGaFormLoad();
  }
}

function collectFields(attr) {
  const config = {};
  document.querySelectorAll(`[${attr}]`).forEach((el) => {
    const key = el.getAttribute(attr);
    config[key] = el.type === "checkbox" ? el.checked : el.value;
  });
  return config;
}

function fillGaForm(form) {
  fillFields("data-ga-key", form);
  refreshAllWorkingHoursWidgets();
  populateGaDeviceSelects(form?.device || "");
}

function collectGaForm() {
  return collectFields("data-ga-key");
}

let sessionEstimateTimer = null;
let lastSessionEstimate = null;

function fillEstimatePanel(root, data) {
  if (!root || !data) return;
  const dur = root.querySelector('[data-est="duration"]');
  const binding = root.querySelector('[data-est="binding"]');
  const profiles = root.querySelector('[data-est="profiles"]');
  const schedule = root.querySelector('[data-est="schedule"]');
  const warnBadge = root.querySelector('[data-est="warn-badge"]');
  if (dur) dur.textContent = data.session_minutes?.label || "—";
  if (binding) binding.textContent = data.binding_limit || "—";
  if (profiles) {
    const p = data.expected_profiles;
    profiles.textContent = p ? `${p.low}–${p.high}` : "—";
  }
  if (schedule) {
    schedule.textContent = data.schedule?.label || "";
    schedule.classList.toggle("hidden", !data.schedule?.label);
  }
  if (warnBadge) {
    const warnings = (data.warnings || []).filter((w) => w.level === "warn");
    if (warnings.length) {
      warnBadge.classList.remove("hidden");
      warnBadge.textContent = String(warnings.length);
      warnBadge.title = warnings.map((w) => w.message).join("\n\n");
    } else {
      warnBadge.classList.add("hidden");
      warnBadge.title = "";
    }
  }
}

function renderSessionEstimate(estimate) {
  if (estimate) lastSessionEstimate = estimate;
  const data = estimate || lastSessionEstimate;
  const panel = $("account-session-estimate");
  if (!panel) return;
  const show = !!(data && gaCurrentAccountId);
  panel.classList.toggle("hidden", !show);
  if (!show) return;
  fillEstimatePanel(panel, data);
}

function collectPostReelSettings() {
  return collectFields("data-pr-key");
}

async function refreshSessionEstimate() {
  if (!gaCurrentAccountId || gaFormLoading) return;
  try {
    const res = await fetch("/api/gramaddict/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        config: collectGaForm(),
        post_reel: collectPostReelSettings(),
      }),
    });
    if (!res.ok) return;
    const estimate = await res.json();
    renderSessionEstimate(estimate);
  } catch {
    /* ignore transient estimate errors */
  }
}

function scheduleSessionEstimateRefresh() {
  if (sessionEstimateTimer) clearTimeout(sessionEstimateTimer);
  sessionEstimateTimer = setTimeout(() => refreshSessionEstimate(), 450);
}

async function loadGaSchema() {
  const [schemaRes, filtersRes, tgRes, prRes, fvRes, filesRes] = await Promise.all([
    fetch("/api/gramaddict/schema"),
    fetch("/api/gramaddict/schema/filters"),
    fetch("/api/gramaddict/schema/telegram"),
    fetch("/api/gramaddict/schema/post-reel"),
    fetch("/api/gramaddict/schema/follow-vision"),
    fetch("/api/gramaddict/schema/files"),
  ]);
  if (!schemaRes.ok) throw new Error(await schemaRes.text());
  gaSchema = await schemaRes.json();
  gaFiltersSchema = filtersRes.ok ? await filtersRes.json() : null;
  gaTelegramSchema = tgRes.ok ? await tgRes.json() : null;
  gaPostReelSchema = prRes.ok ? await prRes.json() : null;
  gaFollowVisionSchema = fvRes.ok ? await fvRes.json() : null;
  gaFilesMeta = filesRes.ok ? await filesRes.json() : null;
  renderAccountForms();
  renderFiltersForm();
  renderTelegramForm();
  renderPostReelForm();
  renderFollowVisionForm();
  renderFileEditors();
  bindWorkingHoursWidgets();
  applyAllTabHelp();
  rebuildSettingsSearchIndex();
}

async function loadAccountFiles() {
  if (!gaCurrentAccountId) return;
  const meta = gaFilesMeta || { lists: {}, text: {} };
  const names = [...Object.keys(meta.lists || {}), ...Object.keys(meta.text || {})];
  await Promise.all(
    names.map(async (name) => {
      try {
        const res = await fetch(
          `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/files/${encodeURIComponent(name)}`
        );
        if (!res.ok) return;
        const data = await res.json();
        const el = document.querySelector(`[data-file-key="${name}"]`);
        if (!el) return;
        let content = data.content || "";
        if (name === "pm_list.txt") {
          content = content
            .split("\n")
            .filter((line) => !isPlaceholderCommentLine(line))
            .join("\n");
        }
        el.value = content;
      } catch (_) {}
    })
  );
  refreshCommentsListWidgets();
}

let gaSettingTemplates = [];

async function loadSettingsTemplateSources() {
  const select = $("template-load-source");
  if (!select) return;
  try {
    const res = await fetch("/api/gramaddict/templates");
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    gaSettingTemplates = data.templates || [];
  } catch (_) {
    gaSettingTemplates = [];
  }
  const parts = ['<option value="">Load from…</option>'];
  const others = gaAccounts.filter((a) => a.id !== gaCurrentAccountId);
  if (others.length) {
    parts.push('<optgroup label="Other accounts">');
    others.forEach((a) => {
      parts.push(
        `<option value="account:${escapeHtml(a.id)}">${escapeHtml(a.username || a.id)}</option>`
      );
    });
    parts.push("</optgroup>");
  }
  if (gaSettingTemplates.length) {
    parts.push('<optgroup label="Saved templates">');
    gaSettingTemplates.forEach((t) => {
      parts.push(
        `<option value="template:${escapeHtml(t.id)}">${escapeHtml(t.name || t.id)}</option>`
      );
    });
    parts.push("</optgroup>");
  }
  select.innerHTML = parts.join("");
}

async function saveCurrentAsTemplate() {
  if (!gaCurrentAccountId) return;
  const name = ($("template-save-name")?.value || "").trim();
  if (!name) {
    setGaStatus("Enter a template name", "error");
    return;
  }
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/save-template`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Save template failed");
    $("template-save-name").value = "";
    setGaStatus(`Saved template “${data.name || data.id}”`, "success");
    log(`Saved settings template: ${data.id}`);
    await loadSettingsTemplateSources();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function applySettingsFromSource() {
  if (!gaCurrentAccountId) return;
  const raw = $("template-load-source")?.value || "";
  const includeLists = !!$("template-include-lists")?.checked;
  if (!raw || !raw.includes(":")) {
    setGaStatus("Choose an account or template to load from", "error");
    return;
  }
  const [sourceType, ...rest] = raw.split(":");
  const sourceId = rest.join(":");
  const label =
    $("template-load-source")?.selectedOptions?.[0]?.textContent?.trim() || sourceId;
  if (
    !confirm(
      `Replace settings on this account with “${label}”?\n\nUsername and phone link stay the same.${
        includeLists ? "\nUsername lists will be copied too." : ""
      }`
    )
  ) {
    return;
  }
  try {
    await flushAutosave();
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/apply-settings`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_type: sourceType,
          source_id: sourceId,
          include_lists: includeLists,
        }),
      }
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Apply settings failed");
    setGaStatus(`Loaded settings from ${label}`, "success");
    log(`Applied settings from ${sourceType}:${sourceId} → ${gaCurrentAccountId}`);
    await onGaAccountChange();
    await loadSettingsTemplateSources();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

function bindSettingsTemplates() {
  $("btn-save-template")?.addEventListener("click", saveCurrentAsTemplate);
  $("btn-apply-settings")?.addEventListener("click", applySettingsFromSource);
}

async function loadGaAccounts() {
  try {
    const res = await fetch("/api/gramaddict/accounts");
    if (!res.ok) throw new Error(await res.text());
    gaAccounts = await res.json();
    const select = $("ga-account-select");
    const fields = $("ga-config-fields");
    const empty = $("ga-no-accounts");
    if (!select) return;
    if (!gaAccounts.length) {
      select.innerHTML = "";
      fields?.classList.add("hidden");
      empty?.classList.remove("hidden");
      $("account-session-estimate")?.classList.add("hidden");
      gaCurrentAccountId = "";
      syncRunButtons(false);
      $("btn-delete-account")?.setAttribute("disabled", "disabled");
      renderDevices();
      return;
    }
    empty?.classList.add("hidden");
    fields?.classList.remove("hidden");
    $("btn-delete-account")?.removeAttribute("disabled");
    select.innerHTML = gaAccounts
      .map((a) => `<option value="${a.id}">${a.username || a.id}${a.running ? " (running)" : ""}</option>`)
      .join("");
    if (gaCurrentAccountId && gaAccounts.some((a) => a.id === gaCurrentAccountId)) {
      select.value = gaCurrentAccountId;
    } else {
      select.value = gaAccounts[0].id;
      gaCurrentAccountId = gaAccounts[0].id;
    }
    await onGaAccountChange();
    await loadSettingsTemplateSources();
    renderDevices();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function onGaAccountChange() {
  const select = $("ga-account-select");
  if (!select?.value) return;
  await flushAutosave();
  setActiveSaveField(null);
  gaCurrentAccountId = select.value;
  localStorage.setItem("gaAccountId", gaCurrentAccountId);
  beginGaFormLoad();
  try {
    const [acctRes, filtersRes, tgRes, prRes, fvRes] = await Promise.all([
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}`),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/filters`),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/telegram`),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/post-reel`),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/follow-vision`),
    ]);
    if (!acctRes.ok) throw new Error(await acctRes.text());
    const data = await acctRes.json();
    fillGaForm(data.form || {});
    updatePostReelsInline();
    const raw = $("ga-raw-yaml");
    if (raw) raw.value = data.raw_yaml || "";
    syncRunButtons(data.running);
    if (filtersRes.ok) {
      const filters = await filtersRes.json();
      fillFields("data-filter-key", filters.form || {});
    }
    if (tgRes.ok) {
      const tg = await tgRes.json();
      fillFields("data-tg-key", tg.form || {});
    }
    if (prRes.ok) {
      const pr = await prRes.json();
      fillFields("data-pr-key", pr.settings || {});
      fillPostReelPrompts(pr.prompts || {});
      const counter = pr.state?.media_selection_counter ?? 1;
      setPostReelMediaHint(
        pr.media_dir
          ? `Videos folder: ${pr.media_dir} · next gallery select counter: ${counter}`
          : ""
      );
      await loadPostReelMedia();
    }
    if (fvRes.ok) {
      const fv = await fvRes.json();
      fillFields("data-fv-key", fv.settings || {});
      fillFollowVisionPrompts(fv.prompts || {});
    }
    await loadAccountFiles();
    renderSessionEstimate(data.estimate);
    setGaStatus("");
    updateContextStrip();
    if (currentMainTab === "tools") await loadAdvFiles();
  } catch (err) {
    setGaStatus(err.message, "error");
  } finally {
    endGaFormLoad();
  }
}

async function createGaAccount() {
  const name = window.prompt("Instagram username for new account:");
  if (!name?.trim()) return;
  setActiveSaveField(null);
  try {
    const res = await fetch("/api/gramaddict/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to create account");
    gaCurrentAccountId = data.id;
    localStorage.setItem("gaAccountId", gaCurrentAccountId);
    log(`Created account ${data.id}`);
    await loadGaAccounts();
    setGaStatus("Account created", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function deleteGaAccount() {
  if (!gaCurrentAccountId) return;
  const account = gaAccounts.find((a) => a.id === gaCurrentAccountId);
  const label = account?.username || gaCurrentAccountId;
  const confirmed = window.confirm(
    `Delete account “${label}”?\n\nThis permanently removes its config, lists, and all other files. This cannot be undone.`
  );
  if (!confirmed) return;
  await flushAutosave();
  setActiveSaveField(null);
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}`, {
      method: "DELETE",
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to delete account");
    const deletedId = gaCurrentAccountId;
    gaCurrentAccountId = "";
    localStorage.removeItem("gaAccountId");
    log(`Deleted account ${deletedId}`);
    await loadGaAccounts();
    setGaStatus("Account deleted", "success");
    updateContextStrip();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaConfig(opts = {}) {
  const quiet = opts.quiet === true;
  if (!gaCurrentAccountId) {
    if (!quiet) setGaStatus("Create or select an account first", "error");
    return;
  }
  setGaStatus("Saving…", "");
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: collectGaForm() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Save failed");
    if (!quiet) {
      fillGaForm(data.form || {});
      const rawEl = $("ga-raw-yaml");
      if (rawEl) rawEl.value = data.raw_yaml || "";
      log(`Saved config for ${gaCurrentAccountId}`);
      await loadGaAccounts();
    }
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaRawYaml(opts = {}) {
  const quiet = opts.quiet === true;
  if (!gaCurrentAccountId) return;
  const raw = $("ga-raw-yaml")?.value;
  if (raw === undefined) return;
  setGaStatus("Saving…", "");
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: {}, raw_yaml: raw }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Save failed");
    const rawEl = $("ga-raw-yaml");
    if (rawEl) rawEl.value = data.raw_yaml || "";
    if (!quiet) {
      fillGaForm(data.form || {});
      log(`Saved raw YAML for ${gaCurrentAccountId}`);
    }
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaFilters(opts = {}) {
  const quiet = opts.quiet === true;
  if (!gaCurrentAccountId) return;
  setGaStatus("Saving…", "");
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/filters`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filters: collectFields("data-filter-key") }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Save failed");
    if (!quiet) {
      fillFields("data-filter-key", data.form || {});
      log(`Saved filters for ${gaCurrentAccountId}`);
    }
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaPosting(opts = {}) {
  const quiet = opts.quiet === true;
  if (!gaCurrentAccountId) return;
  setGaStatus("Saving…", "");
  try {
    const [settingsRes, promptsRes, fvSettingsRes, fvPromptsRes] = await Promise.all([
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/post-reel`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ post_reel: collectFields("data-pr-key") }),
      }),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/post-reel/prompts`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompts: collectPostReelPrompts() }),
      }),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/follow-vision`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ follow_vision: collectFields("data-fv-key") }),
      }),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/follow-vision/prompts`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompts: collectFollowVisionPrompts() }),
      }),
    ]);
    const settingsData = await settingsRes.json();
    const promptsData = await promptsRes.json();
    const fvSettingsData = await fvSettingsRes.json();
    const fvPromptsData = await fvPromptsRes.json();
    if (!settingsRes.ok) throw new Error(settingsData.detail || "Post reel save failed");
    if (!promptsRes.ok) throw new Error(promptsData.detail || "Prompts save failed");
    if (!fvSettingsRes.ok) throw new Error(fvSettingsData.detail || "Follow vision save failed");
    if (!fvPromptsRes.ok) throw new Error(fvPromptsData.detail || "Follow vision prompts save failed");
    if (!quiet) log(`Saved posting settings for ${gaCurrentAccountId}`);
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaReports(opts = {}) {
  const quiet = opts.quiet === true;
  if (!gaCurrentAccountId) return;
  setGaStatus("Saving…", "");
  try {
    const [cfgRes, tgRes] = await Promise.all([
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: collectGaForm() }),
      }),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/telegram`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ telegram: collectFields("data-tg-key") }),
      }),
    ]);
    const cfgData = await cfgRes.json();
    const tgData = await tgRes.json();
    if (!cfgRes.ok) throw new Error(cfgData.detail || "Config save failed");
    if (!tgRes.ok) throw new Error(tgData.detail || "Telegram save failed");
    if (!quiet) {
      fillGaForm(cfgData.form || {});
      fillFields("data-tg-key", tgData.form || {});
      log(`Saved reports for ${gaCurrentAccountId}`);
    }
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveAccountTextFiles(fileKeys) {
  if (!gaCurrentAccountId) return;
  for (const name of fileKeys) {
    const el = document.querySelector(`[data-file-key="${name}"]`);
    if (!el) continue;
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/files/${encodeURIComponent(name)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: el.value }),
      }
    );
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Failed to save ${name}`);
    }
  }
}

async function saveGaLists(opts = {}) {
  const quiet = opts.quiet === true;
  if (!gaFilesMeta?.lists) return;
  setGaStatus("Saving…", "");
  try {
    await saveAccountTextFiles(Object.keys(gaFilesMeta.lists));
    if (!quiet) log(`Saved lists for ${gaCurrentAccountId}`);
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaComments(opts = {}) {
  const quiet = opts.quiet === true;
  if (!gaFilesMeta?.text) return;
  setGaStatus("Saving…", "");
  try {
    await saveAccountTextFiles(Object.keys(gaFilesMeta.text));
    if (!quiet) log(`Saved comments/PM for ${gaCurrentAccountId}`);
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

function onAccountFieldInput(event) {
  if (gaFormLoading || !gaCurrentAccountId) return;
  const target = event.target;
  if (!target) return;

  const fieldContainer =
    target.closest(".field") ||
    target.closest(".ga-check") ||
    target.closest(".ga-form-section");
  setActiveSaveField(fieldContainer);

  if (target.id === "ga-raw-yaml") {
    scheduleAutosave("raw-yaml", () => saveGaRawYaml({ quiet: true }), 1500);
    return;
  }
  if (target.matches("[data-ga-key]")) {
    if (target.getAttribute("data-ga-key") === "post-reels") {
      updatePostReelsInline();
      if (postReelsCountValue() > 0) loadPostReelMedia();
    }
    scheduleSessionEstimateRefresh();
    if (currentAccountTab === "reports") {
      scheduleAutosave("reports", () => saveGaReports({ quiet: true }));
    } else {
      scheduleAutosave("config", () => saveGaConfig({ quiet: true }));
    }
    return;
  }
  if (target.matches("[data-filter-key]")) {
    scheduleAutosave("filters", () => saveGaFilters({ quiet: true }));
    return;
  }
  if (target.matches("[data-tg-key]")) {
    scheduleAutosave("reports", () => saveGaReports({ quiet: true }));
    return;
  }
  if (target.matches("[data-pr-key]") || target.matches("[data-pr-prompt]")) {
    scheduleSessionEstimateRefresh();
    scheduleAutosave("posting", () => saveGaPosting({ quiet: true }));
    return;
  }
  if (target.matches("[data-fv-key]") || target.matches("[data-fv-prompt]")) {
    scheduleAutosave("posting", () => saveGaPosting({ quiet: true }));
    return;
  }
  if (target.matches("[data-file-key]")) {
    const name = target.dataset.fileKey;
    if (gaFilesMeta?.lists?.[name]) {
      scheduleAutosave("lists", () => saveGaLists({ quiet: true }));
    } else if (gaFilesMeta?.text?.[name]) {
      scheduleAutosave("comments", () => saveGaComments({ quiet: true }));
    }
  }
}

function bindAccountAutosave() {
  const root = $("ga-config-fields");
  if (root) {
    root.addEventListener("input", onAccountFieldInput);
    root.addEventListener("change", onAccountFieldInput);
    root.addEventListener("click", (event) => {
      const chooseBtn = event.target.closest("[data-post-reels-choose]");
      if (!chooseBtn) return;
      const input = chooseBtn.parentElement?.querySelector("[data-post-reels-input]");
      input?.click();
    });
    root.addEventListener("change", (event) => {
      const input = event.target.closest("[data-post-reels-input]");
      if (!input) return;
      if (input.files?.length) uploadPostReelFiles(input.files);
      input.value = "";
    });
  }
  const adv = $("adv-file-content");
  if (adv) {
    adv.addEventListener("input", () => {
      if (gaFormLoading || !gaCurrentAccountId) return;
      scheduleAutosave("adv-file", () => saveAdvFile({ quiet: true }), 1200);
    });
  }
}

function resolveRunDevice() {
  const deviceSelect = document.querySelector('select[data-ga-key="device"]');
  const acct = currentAccount();
  return deviceSelect?.value || activeSerial || acct?.device || "";
}

async function runGramAddict() {
  setActiveSaveField(null);
  const acct = currentAccount();
  const accountId = acct?.id || gaCurrentAccountId;
  if (!accountId) {
    setGaStatus("Set an @ account on the phone or select one under Account", "error");
    return;
  }
  if (accountId !== gaCurrentAccountId) {
    gaCurrentAccountId = accountId;
    localStorage.setItem("gaAccountId", accountId);
  }
  const deviceSerial = resolveRunDevice();
  if (!deviceSerial) {
    setGaStatus("Select a phone on Farm or set device in Account → Basics", "error");
    return;
  }
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_serial: deviceSerial, vpn_app_name: getVpnAppName() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to start bot");
    syncRunButtons(true);
    setGaStatus(`Bot started on ${shortSerial(deviceSerial)}`, "success");
    log(`GramAddict started for ${accountId}`);
    await loadGaAccounts();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function stopGramAddict() {
  setActiveSaveField(null);
  const accountId = currentAccount()?.id || gaCurrentAccountId;
  if (!accountId) return;
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/stop`, {
      method: "POST",
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Stop failed");
    syncRunButtons(false);
    setGaStatus(data.message || "Bot stopped", "success");
    log(`GramAddict stopped for ${accountId}`);
    await loadGaAccounts();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

/* ── Advanced file browser ── */

async function loadAdvFiles() {
  const select = $("adv-file-select");
  if (!select || !gaCurrentAccountId) {
    if (select) select.innerHTML = '<option value="">Select an account first</option>';
    return;
  }
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/files`);
    if (!res.ok) throw new Error(await res.text());
    const files = await res.json();
    select.innerHTML = files.map((f) => `<option value="${f.name}">${f.name} (${f.size} B)</option>`).join("");
    if (files.length) await onAdvFileChange();
  } catch (err) {
    setAdvFileStatus(err.message, "error");
  }
}

async function onAdvFileChange() {
  await flushAutosave();
  const select = $("adv-file-select");
  const textarea = $("adv-file-content");
  if (!select?.value || !gaCurrentAccountId || !textarea) return;
  beginGaFormLoad();
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/files/${encodeURIComponent(select.value)}`
    );
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    textarea.value = data.content || "";
    advFileSnapshot = textarea.value;
    setAdvFileStatus("");
  } catch (err) {
    setAdvFileStatus(err.message, "error");
  } finally {
    endGaFormLoad();
  }
}

async function saveAdvFile(opts = {}) {
  const quiet = opts.quiet === true;
  const select = $("adv-file-select");
  const textarea = $("adv-file-content");
  if (!select?.value || !gaCurrentAccountId || !textarea) return;
  if (quiet && textarea.value === advFileSnapshot) return;
  setAdvFileStatus("Saving…", "");
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/files/${encodeURIComponent(select.value)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: textarea.value }),
      }
    );
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "Save failed");
    }
    advFileSnapshot = textarea.value;
    setAdvFileStatus("Saved", "success");
    if (!quiet) {
      log(`Saved ${select.value} for ${gaCurrentAccountId}`);
      await loadAdvFiles();
    }
  } catch (err) {
    setAdvFileStatus(err.message, "error");
  }
}

/* ── Brand pools ── */

let gaBrandPools = { pools: [], unassigned: [] };

async function loadBrandPools() {
  const res = await fetch("/api/brand-pools");
  if (!res.ok) throw new Error("Failed to load brand pools");
  gaBrandPools = await res.json();
  renderBrandPools();
}

function renderBrandPools() {
  const grid = document.getElementById("brand-pools-grid");
  if (!grid) return;
  const pools = gaBrandPools.pools || [];
  if (!pools.length) {
    grid.innerHTML = `<p class="brand-pools-empty">No brand pools configured.</p>`;
    return;
  }
  grid.innerHTML = pools
    .map((pool) => {
      const members = (pool.accounts || [])
        .map(
          (member) => `
          <span class="brand-pool-chip">
            <span>@${escapeHtml(member.username || member.account_id)}</span>
            <button type="button" class="brand-pool-chip-remove" title="Remove from pool"
              onclick="removeFromBrandPool('${escapeHtml(pool.id)}', '${escapeHtml(member.account_id)}')">×</button>
          </span>`
        )
        .join("");
      const options = (gaBrandPools.unassigned || [])
        .map(
          (acct) =>
            `<option value="${escapeHtml(acct.account_id)}">@${escapeHtml(acct.username || acct.account_id)}</option>`
        )
        .join("");
      return `
        <div class="brand-pool-card" data-pool-id="${escapeHtml(pool.id)}">
          <div class="brand-pool-card-head">
            <h3>${escapeHtml(pool.name)}</h3>
            <span class="brand-pool-meta">${pool.interacted_count || 0} people in shared history</span>
          </div>
          <div class="brand-pool-members">${members || '<span class="brand-pool-empty-members">No accounts assigned</span>'}</div>
          <div class="brand-pool-add-row">
            <select class="ga-input brand-pool-add-select" data-pool-add="${escapeHtml(pool.id)}">
              <option value="">Add account…</option>
              ${options}
            </select>
            <button type="button" class="btn-ghost btn-sm" onclick="addToBrandPool('${escapeHtml(pool.id)}')">Add</button>
          </div>
        </div>`;
    })
    .join("");
}

async function saveBrandPoolAccounts(poolId, accountIds) {
  const res = await fetch(`/api/brand-pools/${encodeURIComponent(poolId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ accounts: accountIds }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to update brand pool");
  }
  return res.json();
}

async function addToBrandPool(poolId) {
  const select = document.querySelector(`select[data-pool-add="${poolId}"]`);
  if (!select || !select.value) return;
  const pool = (gaBrandPools.pools || []).find((p) => p.id === poolId);
  const current = (pool?.accounts || []).map((a) => a.account_id);
  if (current.includes(select.value)) return;
  try {
    await saveBrandPoolAccounts(poolId, [...current, select.value]);
    await loadBrandPools();
    await loadGaAccounts();
    log(`Added @${select.value} to ${pool?.name || poolId} pool`);
  } catch (err) {
    log(err.message, "error");
  }
}

async function removeFromBrandPool(poolId, accountId) {
  const pool = (gaBrandPools.pools || []).find((p) => p.id === poolId);
  const current = (pool?.accounts || []).map((a) => a.account_id).filter((id) => id !== accountId);
  try {
    await saveBrandPoolAccounts(poolId, current);
    await loadBrandPools();
    await loadGaAccounts();
    log(`Removed @${accountId} from ${pool?.name || poolId} pool`);
  } catch (err) {
    log(err.message, "error");
  }
}

function bindBrandPools() {
  const btn = document.getElementById("btn-refresh-brand-pools");
  if (btn) btn.addEventListener("click", () => loadBrandPools().catch((err) => log(err.message, "error")));
}

/* ── Init ── */

document.addEventListener("DOMContentLoaded", async () => {
  bindMainTabs();
  bindAccountTabs();
  bindAccountAutosave();
  bindSettingsTemplates();
  bindPostReelDropzone();
  bindSettingSearch();
  bindDebugPanel();
  bindBrandPools();
  connectWebSocket();
  await loadDeviceFilterMeta();
  await refreshDevices();
  restoreActiveSelection();
  renderDevices();
  loadDebugTests();
  try {
    await loadBrandPools();
  } catch (err) {
    log(err.message, "error");
  }
  try {
    await loadGaSchema();
    await loadGaAccounts();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
  updateContextStrip();
  if (currentMainTab === "tools" && activeSerial) {
    connectWeditor();
  }
  log("GramAddict dashboard ready");
});

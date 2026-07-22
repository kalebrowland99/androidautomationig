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
/** Account whose settings are currently loaded into the Account form fields.
 *  Autosaves must target this — never a different account selected on Farm. */
let gaFormAccountId = "";
let gaSchema = null;
let gaFiltersSchema = null;
let gaTelegramSchema = null;
let gaPostReelSchema = null;
let gaFollowVisionSchema = null;
let gaFilesMeta = null;
let gaAccountRunning = false;
let farmBatchRunning = false;
let farmBatchCancel = false;
let gaAccountAutopostLocked = false;
let gaFormLoading = false;
let gaFormLoadDepth = 0;
let advFileSnapshot = "";
const autosaveTimers = {};
const autosavePending = {};

let gaLoadSpinnerTimer = null;

function beginGaFormLoad() {
  gaFormLoadDepth += 1;
  gaFormLoading = true;
  if (gaFormLoadDepth === 1) {
    clearTimeout(gaLoadSpinnerTimer);
    // Only reveal the spinner if the load actually drags (avoids flicker on
    // fast account switches).
    gaLoadSpinnerTimer = setTimeout(() => {
      $("account-loading-overlay")?.classList.remove("hidden");
    }, 250);
  }
}

function endGaFormLoad() {
  gaFormLoadDepth = Math.max(0, gaFormLoadDepth - 1);
  gaFormLoading = gaFormLoadDepth > 0;
  if (gaFormLoadDepth === 0) {
    clearTimeout(gaLoadSpinnerTimer);
    $("account-loading-overlay")?.classList.add("hidden");
  }
}

function formAccountId() {
  return gaFormAccountId || gaCurrentAccountId || "";
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

async function saveAllBeforeRun() {
  const accountId = formAccountId();
  if (!accountId) return;
  syncAllCommentsListWidgets();
  await flushAutosave();
  await saveGaConfig({ quiet: true, accountId });
  if (document.querySelector("[data-filter-key]")) {
    await saveGaFilters({ quiet: true, accountId });
  }
  if (document.querySelector("[data-tg-key]") || document.querySelector('[data-ga-key="telegram-reports"]')) {
    await saveGaReports({ quiet: true, accountId });
  }
  if (document.querySelector("[data-pr-key]") || document.querySelector("[data-fv-key]")) {
    await saveGaPosting({ quiet: true, accountId });
  }
  await saveGaLists({ quiet: true, accountId });
  await saveGaComments({ quiet: true, accountId });
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

function formatLogTime(date = new Date()) {
  return date.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
}

function log(message, level = "info") {
  const el = $("unified-log");
  if (!el) return;
  const line = document.createElement("div");
  const ts = formatLogTime();
  line.className = `log-line log-${level}`;
  line.textContent = `[${ts}] ${message}`;
  el.insertBefore(line, el.firstChild);
  if (autoscroll) el.scrollTop = 0;
}

// Append a line verbatim (no wall-clock prefix) — used for on-disk log history,
// whose lines already carry their own "[MM/DD HH:MM:SS] LEVEL | …" timestamp.
function logRaw(message, level = "info") {
  const el = $("unified-log");
  if (!el) return;
  const line = document.createElement("div");
  line.className = `log-line log-${level}`;
  line.textContent = message;
  el.appendChild(line);
}

// Infer a severity level from a GramAddict log line for coloring.
function logLevelForLine(line) {
  if (/\|\s*(ERROR|CRITICAL)\b/.test(line) || /\bERROR\b/.test(line)) return "error";
  if (/\|\s*WARNING\b/.test(line) || /\bWARNING\b/.test(line)) return "warn";
  return "info";
}

function clearLog() {
  const el = $("unified-log");
  if (el) el.innerHTML = "";
}

const STORY_LIKES_LOG_MARKERS = [
  "daily story likes",
  "story likes |",
  "story segment",
  "has no new story",
  "has no story",
  "last story check",
  "already checked today",
  "already checked",
  "story watch limit",
  "accounts checked for stories",
  "no new story to like",
  "checked — no new story",
  "checked — no story",
  "session start",
  "removed from list",
  "added to story list",
  "job complete",
  "could not open profile",
];

function isStoryLikesLogLine(message) {
  const lower = String(message || "").toLowerCase();
  if (lower.includes("plugin_loader")) return false;
  if (lower.includes("like new stories for a fixed list")) return false;
  return STORY_LIKES_LOG_MARKERS.some((marker) => lower.includes(marker));
}

function storyLikesLogLevel(line) {
  const lower = String(line || "").toLowerCase();
  if (lower.includes("error") || lower.includes("failed")) return "error";
  if (lower.includes("skip") || lower.includes("no new story") || lower.includes("limit")) {
    return "warn";
  }
  if (lower.includes("liked")) return "success";
  return "info";
}

function storyLikesLogRaw(message, level = "info") {
  const el = $("story-likes-log");
  if (!el) return;
  const line = document.createElement("div");
  line.className = `log-line log-${level}`;
  line.textContent = message;
  el.appendChild(line);
}

function storyLikesLogLive(message, level = "info") {
  const el = $("story-likes-log");
  if (!el) return;
  const line = document.createElement("div");
  line.className = `log-line log-${level}`;
  line.textContent = message;
  el.insertBefore(line, el.firstChild);
  if (autoscroll) el.scrollTop = 0;
}

function clearStoryLikesLog() {
  const el = $("story-likes-log");
  if (el) el.innerHTML = "";
}

async function showSuccessfulStoryLikes() {
  const modal = $("story-likes-success-modal");
  const content = $("story-likes-success-content");
  if (!modal || !content) return;
  
  const accountId = activeAccountId();
  if (!accountId) {
    content.innerHTML = '<div class="text-center text-muted-foreground py-4">No account selected</div>';
    modal.classList.remove("hidden");
    return;
  }
  
  modal.classList.remove("hidden");
  content.innerHTML = '<div class="text-center text-muted-foreground py-4">Loading...</div>';
  
  try {
    const response = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/story-likes-success`);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    
    if (!data.liked_accounts || data.liked_accounts.length === 0) {
      content.innerHTML = '<div class="text-center text-muted-foreground py-4">No successfully liked stories yet</div>';
      return;
    }
    
    let html = `<div style="margin-bottom: 0.75rem; padding: 0.5rem; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 0.5rem;">
      <strong>Total accounts with liked stories:</strong> ${data.total_count}
    </div>`;
    
    html += '<div style="display: grid; gap: 0.5rem;">';
    for (const account of data.liked_accounts) {
      html += `
        <div style="padding: 0.75rem; border: 1px solid #e5e7eb; border-radius: 0.5rem; background: #fafafa;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.25rem;">
            <a href="https://instagram.com/${account.username}" target="_blank" style="font-weight: 600; color: #6366f1; text-decoration: none;">
              @${account.username}
            </a>
            <span style="font-size: 0.75rem; color: #6b7280;">${account.timestamp}</span>
          </div>
          <div style="font-size: 0.875rem; color: #6b7280;">
            Last liked: ${account.segments_liked} segment(s) | Total: ${account.total_likes} segment(s)
          </div>
        </div>
      `;
    }
    html += '</div>';
    
    content.innerHTML = html;
  } catch (err) {
    content.innerHTML = `<div class="text-center text-muted-foreground py-4">Error loading data: ${err.message}</div>`;
  }
}

function hideSuccessfulStoryLikes() {
  const modal = $("story-likes-success-modal");
  if (modal) modal.classList.add("hidden");
}

function accountHasStoryLikesEnabled(acct) {
  return Boolean(acct?.story_likes_enabled);
}

function updateStoryLikesLogPanelVisibility(acct) {
  const panel = $("story-likes-log-panel");
  if (!panel) return;
  const show = accountHasStoryLikesEnabled(acct);
  panel.classList.toggle("hidden", !show);
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

let farmSelectionSyncTimer = null;

function persistDeviceSelection() {
  if (activeSerial) {
    localStorage.setItem("activeSerial", activeSerial);
    sessionStorage.setItem("activeSerial", activeSerial);
  } else {
    localStorage.removeItem("activeSerial");
    sessionStorage.removeItem("activeSerial");
  }
  const serials = [...selectedSerials];
  localStorage.setItem("selectedSerials", JSON.stringify(serials));
  // Persist Farm checkboxes server-side so the 5 AM starter uses the same set.
  clearTimeout(farmSelectionSyncTimer);
  farmSelectionSyncTimer = setTimeout(() => {
    fetch("/api/gramaddict/farm-selection", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ serials }),
    }).catch(() => {
      /* ignore transient sync errors */
    });
  }, 250);
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
  const bySerial = gaAccounts.find((a) => a.device === serial);
  if (bySerial) return bySerial;
  // Fall back to the phone's stable hardware id so the @name sticks even when the
  // ADB serial changes (e.g. wireless reconnect on a new IP:port).
  const device = devices.find((d) => d.serial === serial);
  const hardwareId = device?.hardware_id;
  if (hardwareId) {
    return gaAccounts.find((a) => a.device_id && a.device_id === hardwareId) || null;
  }
  return null;
}

function accountForActivePhone() {
  return activeSerial ? accountForDevice(activeSerial) : null;
}

function normalizeInstagramHandle(value) {
  return String(value || "").trim().replace(/^@+/, "");
}

const deviceAccountDrafts = {};
let deviceAccountEditingSerial = null;

// Per-account note editing state (Farm rows). Drafts survive the frequent
// device-poll re-renders of the table so an in-progress note isn't wiped.
const farmNoteDrafts = {};
const farmNoteTimers = {};

function deviceAccountNoteHtml(acct) {
  if (!acct) return "";
  const val = Object.prototype.hasOwnProperty.call(farmNoteDrafts, acct.id)
    ? farmNoteDrafts[acct.id]
    : acct.note || "";
  const hasNote = !!(val && val.trim());
  return `<input type="text" class="phones-account-note${hasNote ? " has-note" : ""}"
      data-account-id="${escapeHtml(acct.id)}" value="${escapeHtml(val)}"
      placeholder="Add note…" autocomplete="off" spellcheck="false"
      aria-label="Note for ${escapeHtml(acct.username || acct.id)}">`;
}

function onFarmNoteInput(accountId, input) {
  farmNoteDrafts[accountId] = input.value;
  if (farmNoteTimers[accountId]) clearTimeout(farmNoteTimers[accountId]);
  // Debounced autosave so notes persist even without leaving the field.
  farmNoteTimers[accountId] = setTimeout(() => saveFarmNote(accountId, input), 1200);
}

async function saveFarmNote(accountId, input) {
  if (farmNoteTimers[accountId]) {
    clearTimeout(farmNoteTimers[accountId]);
    delete farmNoteTimers[accountId];
  }
  const note = input ? input.value : farmNoteDrafts[accountId];
  if (note == null) return;
  const acct = gaAccounts.find((a) => a.id === accountId);
  if (note === (acct ? acct.note || "" : "")) {
    delete farmNoteDrafts[accountId];
    return;
  }
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(accountId)}/note`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      }
    );
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    if (acct) acct.note = data.note || "";
    delete farmNoteDrafts[accountId];
  } catch (err) {
    log(`Could not save note: ${err.message}`, "error");
  }
}

// Preserve which note input is focused (and caret position) across a full
// table re-render so device polling doesn't interrupt note editing.
function captureFarmNoteFocus() {
  const el = document.activeElement;
  if (el && el.classList && el.classList.contains("phones-account-note")) {
    return {
      accountId: el.dataset.accountId,
      start: el.selectionStart,
      end: el.selectionEnd,
    };
  }
  return null;
}

function restoreFarmNoteFocus(info) {
  if (!info || !info.accountId) return;
  let el;
  try {
    el = document.querySelector(
      `.phones-account-note[data-account-id="${CSS.escape(info.accountId)}"]`
    );
  } catch (e) {
    el = null;
  }
  if (!el) return;
  el.focus();
  try {
    el.setSelectionRange(info.start, info.end);
  } catch (e) {
    /* non-text input edge case */
  }
}

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

function formatBackOnline(nextAt) {
  if (!nextAt) return "";
  const d = new Date(nextAt);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const time = d.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
  if (sameDay) return time;
  const day = d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
  return `${day}, ${time}`;
}

function botStateFromProgress(acct, acctRunning) {
  const p = acct?.progress;
  if (p?.state) return p.state;
  if (p?.rate_limited) return "action_limit";
  if (acctRunning && p?.sleeping) return "waiting";
  if (acctRunning) return "running";
  return "stopped";
}

/** Clear Farm status line: Running · Waiting · Action limit · Stopped */
function botStatusRowHtml(acct, acctRunning) {
  const p = acct?.progress || {};
  const state = botStateFromProgress(acct, acctRunning);
  const when = formatBackOnline(p.next_session_at);
  const job = (p.current_job || "").replace(/-/g, " ");

  if (state === "action_limit") {
    const title = when
      ? `Instagram action limit — starts again ${when}`
      : "Instagram action limit";
    return `<div class="phones-account-status" title="${escapeHtml(title)}">
      <span class="phones-status-tag action-limit">Action limit</span>
      ${when ? `<span class="phones-status-when">starts again ${escapeHtml(when)}</span>` : ""}
    </div>`;
  }
  if (state === "waiting") {
    const title = when
      ? `Waiting between sessions — starts again ${when}`
      : "Waiting between sessions";
    return `<div class="phones-account-status" title="${escapeHtml(title)}">
      <span class="phones-status-tag waiting">Waiting</span>
      ${when ? `<span class="phones-status-when">starts again ${escapeHtml(when)}</span>` : ""}
    </div>`;
  }
  if (state === "running") {
    const title = job ? `Running — ${job}` : "Bot running";
    return `<div class="phones-account-status" title="${escapeHtml(title)}">
      <span class="phones-status-tag running">Running</span>
      ${job ? `<span class="phones-status-when">${escapeHtml(job)}</span>` : ""}
    </div>`;
  }
  return `<div class="phones-account-status" title="Bot process is not running">
    <span class="phones-status-tag stopped">Stopped</span>
  </div>`;
}

function progressLimitsHtml(acct, acctRunning) {
  const p = acct?.progress;
  if (!p) return "";
  const state = botStateFromProgress(acct, acctRunning);
  const part = (label, val, lim) => {
    if ((val == null || val === 0) && !lim) return null;
    return `${label} ${val ?? 0}${lim ? `/${lim}` : ""}`;
  };
  // Always show — even at 0 — so Farm makes accounts-liked visible.
  const countPart = (label, val) => `${label} ${val ?? 0}`;

  // Session line (while active / waiting / action-limit).
  let sessionLine = "";
  if (state !== "stopped") {
    const parts = [
      part("Liked Posts", p.likes, p.likes_limit),
      part(
        "Liked Stories",
        p.story_likes || p.watched,
        p.story_likes_limit ?? p.watches_limit
      ),
      countPart("Story Accounts", p.story_accounts_liked),
      part("Followed", p.follows, p.follows_limit),
    ];
    if (
      (p.daily_story_likes != null && p.daily_story_likes > 0) ||
      p.current_job === "daily-story-likes"
    ) {
      parts.push(
        part("Daily list", p.daily_story_likes ?? 0, p.daily_story_likes_limit)
      );
    }
    const text = parts.filter(Boolean).join(" · ");
    if (text) {
      sessionLine = `<div class="phones-account-progress" title="This session">${escapeHtml(
        "Session · " + text
      )}</div>`;
    }
  }

  // Today line — display-only daily goals (never stop the bot).
  const t = p.today;
  let todayLine = "";
  if (t && typeof t === "object") {
    const todayParts = [
      part("Liked Posts", t.likes, t.likes_goal),
      part("Liked Stories", t.story_likes, t.story_likes_goal),
      countPart("Story Accounts", t.story_accounts_liked),
      part("Followed", t.follows, t.follows_goal),
    ].filter(Boolean);
    if (todayParts.length) {
      todayLine = `<div class="phones-account-progress phones-account-today" title="Today (goal is display-only)">${escapeHtml(
        "Today · " + todayParts.join(" · ")
      )}</div>`;
    }
  }
  if (!sessionLine && !todayLine) return "";
  return `${sessionLine}${todayLine}`;
}

function deviceAccountCellHtml(serial, acct, acctRunning) {
  const runningMark = acctRunning ? '<span class="phones-account-running" title="Bot active">●</span>' : "";
  const errorMark = hasRecentError(acct?.last_error)
    ? `<button type="button" class="phones-account-error" data-error="${escapeHtml(JSON.stringify(acct.last_error))}"
         onclick='showErrorPopover(event)' title="Recent error — click for details" aria-label="Recent error">&#9432;</button>`
    : "";
  const disabledMark = acct?.disabled
    ? `<span class="phones-account-disabled" title="${escapeHtml(
        acct.disabled_reason ? `Disabled — ${acct.disabled_reason}` : "Disabled"
      )}">Disabled</span>`
    : "";
  const marks = `${runningMark}${errorMark}${disabledMark}`;
  const statusRow = botStatusRowHtml(acct, acctRunning);
  const progress = progressLimitsHtml(acct, acctRunning);
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
        ${marks}
      </div>`;
  }
  return `
    <div class="phones-account-cell">
      <div class="phones-account-wrap">
        <button type="button" class="phones-account-display" title="Click to edit · double-click to open @${escapeHtml(handle)} on Instagram">@${escapeHtml(handle)}</button>
        ${marks}
      </div>
      ${statusRow}
      ${progress}
      ${deviceAccountNoteHtml(acct)}
    </div>`;
}

function deviceLinkedAccountIds() {
  // Accounts that have an @ assigned to a phone (device serial or stable hardware
  // id), whether or not that phone is currently connected.
  const ids = new Set();
  for (const acct of gaAccounts) {
    if (acct && (acct.device || acct.device_id)) ids.add(acct.id);
  }
  return ids;
}

function openInstagramProfile(handle) {
  const clean = normalizeInstagramHandle(handle);
  if (!clean) return;
  window.open(`https://www.instagram.com/${encodeURIComponent(clean)}/`, "_blank", "noopener");
}

function bindDeviceAccountCell(row, serial) {
  const setBtn = row.querySelector(".phones-account-set");
  const displayBtn = row.querySelector(".phones-account-display");
  setBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    startDeviceAccountEdit(serial);
  });
  if (displayBtn) {
    // Single click edits the handle; double click opens the IG profile.
    // Delay the single-click edit so a double click can cancel it.
    let clickTimer = null;
    displayBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (clickTimer) return;
      clickTimer = setTimeout(() => {
        clickTimer = null;
        startDeviceAccountEdit(serial);
      }, 250);
    });
    displayBtn.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (clickTimer) {
        clearTimeout(clickTimer);
        clickTimer = null;
      }
      const acct = accountForDevice(serial);
      openInstagramProfile(acct?.username || acct?.id || "");
    });
  }
  const noteInput = row.querySelector(".phones-account-note");
  if (noteInput) {
    const accountId = noteInput.dataset.accountId;
    // Stop clicks from bubbling to the row (which would select the device).
    noteInput.addEventListener("click", (e) => e.stopPropagation());
    noteInput.addEventListener("input", () => onFarmNoteInput(accountId, noteInput));
    noteInput.addEventListener("blur", () => saveFarmNote(accountId, noteInput));
    noteInput.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") {
        e.preventDefault();
        noteInput.blur();
      }
    });
  }
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
  // Update selection for Farm/context only. Do NOT touch the Account form or
  // flush form fields into this account — that was overwriting vision settings
  // across accounts when switching phones after an edit.
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
  if (tab === "account") {
    const select = $("ga-account-select");
    const wanted = select?.value || gaCurrentAccountId;
    // Farm phone clicks can change the selected account without reloading the
    // form — reload here so we never edit/save the wrong account's settings.
    if (wanted && wanted !== gaFormAccountId) {
      if (select && select.value !== wanted) select.value = wanted;
      onGaAccountChange();
    } else if (gaCurrentAccountId) {
      scheduleSessionEstimateRefresh();
    }
  }
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
  if (tab === "limits") loadRateLimitHistory();
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
        ? `[data-ga-key="${sub.listKey}"]${sub.limitKey ? `, [data-ga-key="${sub.limitKey}"]` : ""}${sub.enabledKey ? `, [data-ga-key="${sub.enabledKey}"]` : ""}`
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
  const text = $("ctx-phone");
  if (!text) return;

  // Phone is always shown as static text — the active phone is chosen by
  // clicking a row in the devices table, so no dropdown is needed here.
  text.classList.remove("hidden");
  const candidates = getContextPhoneCandidates();
  const device =
    candidates.find((d) => d.serial === activeSerial) || candidates[0];
  text.textContent = device ? deviceOptionLabel(device) : "None selected";
}

function populateCtxAccountSelect() {
  const select = $("ctx-account-select");
  const text = $("ctx-account");
  if (!select || !text) return;
  const acct = currentAccount();
  if (gaAccounts.length <= 1) {
    select.classList.add("hidden");
    text.classList.remove("hidden");
    text.textContent = acct ? `@${acct.username || acct.id}` : "None";
    return;
  }
  select.classList.remove("hidden");
  text.classList.add("hidden");
  select.innerHTML = gaAccounts
    .map(
      (a) =>
        `<option value="${escapeHtml(a.id)}">@${escapeHtml(a.username || a.id)}${
          a.running ? " (running)" : ""
        }</option>`
    )
    .join("");
  if (acct) select.value = acct.id;
}

function onCtxAccountChange() {
  const select = $("ctx-account-select");
  if (select?.value) selectActiveAccount(select.value);
}

function selectActiveAccount(accountId) {
  const acct = gaAccounts.find((a) => a.id === accountId);
  if (!acct) return;
  // Keep the active phone in sync with this account's linked device when it's
  // connected, so the context strip and currentAccount() agree.
  const dev = devices.find(
    (d) =>
      d.serial === acct.device ||
      (acct.device_id && d.hardware_id && d.hardware_id === acct.device_id)
  );
  if (dev) {
    activeSerial = dev.serial;
    if (!selectedSerials.has(dev.serial)) selectedSerials.add(dev.serial);
  } else {
    activeSerial = "";
  }
  persistDeviceSelection();
  const pageSelect = $("ga-account-select");
  if (pageSelect) pageSelect.value = accountId;
  // Do not change gaCurrentAccountId here — setMainTab("account") / onGaAccountChange
  // flush the form to the previous account first, then load this one.
  if (currentMainTab === "account") {
    onGaAccountChange();
  } else {
    setMainTab("account");
  }
  renderDevices();
}

/* ── Status tags (active/inactive + recent error) ── */

function hasRecentError(lastError) {
  return !!(lastError && lastError.recent);
}

// Context-strip status: Running / Waiting / Action limit / Stopped (+ error/disabled).
function statusTagsHtml(running, lastError, disabled, disabledReason, progress) {
  const state =
    progress?.state ||
    (progress?.rate_limited
      ? "action_limit"
      : running && progress?.sleeping
        ? "waiting"
        : running
          ? "running"
          : "stopped");
  const when = formatBackOnline(progress?.next_session_at);
  let stateTag;
  if (state === "action_limit") {
    stateTag = `<span class="status-tag action-limit" title="${escapeHtml(
      when ? `Action limit — starts again ${when}` : "Action limit"
    )}">Action limit${when ? ` · ${escapeHtml(when)}` : ""}</span>`;
  } else if (state === "waiting") {
    stateTag = `<span class="status-tag waiting" title="${escapeHtml(
      when ? `Waiting — starts again ${when}` : "Waiting between sessions"
    )}">Waiting${when ? ` · ${escapeHtml(when)}` : ""}</span>`;
  } else if (state === "running") {
    const job = (progress?.current_job || "").replace(/-/g, " ");
    stateTag = `<span class="status-tag active" title="${escapeHtml(
      job ? `Running — ${job}` : "Running"
    )}">Running${job ? ` · ${escapeHtml(job)}` : ""}</span>`;
  } else {
    stateTag = '<span class="status-tag inactive">Stopped</span>';
  }
  const disabledTag = disabled
    ? `<span class="status-tag disabled" title="${escapeHtml(
        disabledReason ? `Disabled — ${disabledReason}` : "Disabled"
      )}">Disabled${disabledReason ? ` — ${escapeHtml(disabledReason)}` : ""}</span>`
    : "";
  if (!hasRecentError(lastError)) return stateTag + disabledTag;
  const payload = escapeHtml(JSON.stringify(lastError));
  return (
    stateTag +
    disabledTag +
    `<button type="button" class="status-tag error" data-error="${payload}"
       onclick='showErrorPopover(event)' title="Recent error — click for details">
       Recent error
       <span class="status-info" aria-hidden="true">&#9432;</span>
     </button>`
  );
}

let errorPopoverEl = null;

function closeErrorPopover() {
  if (errorPopoverEl) {
    errorPopoverEl.remove();
    errorPopoverEl = null;
    document.removeEventListener("click", onErrorPopoverOutside, true);
    document.removeEventListener("keydown", onErrorPopoverKey, true);
  }
}

function onErrorPopoverOutside(e) {
  if (errorPopoverEl && !errorPopoverEl.contains(e.target) && !e.target.closest(".status-tag.error, .phones-account-error")) {
    closeErrorPopover();
  }
}

function onErrorPopoverKey(e) {
  if (e.key === "Escape") closeErrorPopover();
}

function showErrorPopover(event) {
  event.stopPropagation();
  const btn = event.currentTarget;
  let err;
  try {
    err = JSON.parse(btn.dataset.error || "{}");
  } catch (_) {
    err = {};
  }
  const wasOpenForThis = errorPopoverEl && errorPopoverEl.dataset.anchor === (btn.dataset.error || "");
  closeErrorPopover();
  if (wasOpenForThis) return; // toggle off if clicking the same trigger

  errorPopoverEl = document.createElement("div");
  errorPopoverEl.className = "error-popover";
  errorPopoverEl.dataset.anchor = btn.dataset.error || "";
  errorPopoverEl.innerHTML = `
    <div class="error-popover-head">
      <span class="error-popover-level">${escapeHtml(err.level || "ERROR")}</span>
      ${err.at ? `<span class="error-popover-time">${escapeHtml(err.at)}</span>` : ""}
      <button type="button" class="error-popover-close" onclick="closeErrorPopover()" aria-label="Close">&times;</button>
    </div>
    <div class="error-popover-body">${escapeHtml(err.message || "No details available.")}</div>`;
  document.body.appendChild(errorPopoverEl);

  const rect = btn.getBoundingClientRect();
  const pop = errorPopoverEl.getBoundingClientRect();
  let left = rect.left;
  if (left + pop.width > window.innerWidth - 12) {
    left = Math.max(12, window.innerWidth - pop.width - 12);
  }
  errorPopoverEl.style.top = `${rect.bottom + 6 + window.scrollY}px`;
  errorPopoverEl.style.left = `${left + window.scrollX}px`;

  setTimeout(() => {
    document.addEventListener("click", onErrorPopoverOutside, true);
    document.addEventListener("keydown", onErrorPopoverKey, true);
  }, 0);
}

function updateContextStrip() {
  ensureActivePhone();
  populateCtxPhoneSelect();
  populateCtxAccountSelect();
  const statusEl = $("ctx-status");
  const acct = currentAccount();
  const running = !!(acct?.running || gaAccountRunning);

  if (statusEl) {
    statusEl.className = "context-status-group";
    statusEl.innerHTML = statusTagsHtml(
      running,
      acct?.last_error,
      acct?.disabled,
      acct?.disabled_reason,
      acct?.progress
    );
  }

  const disableBtn = $("btn-ctx-disable");
  if (disableBtn) {
    disableBtn.disabled = !acct;
    disableBtn.textContent = acct?.disabled ? "Enable" : "Disable";
    disableBtn.classList.toggle("btn-kill", !acct?.disabled);
    disableBtn.title = acct?.disabled
      ? "Re-enable this account so it can run again"
      : "Pause this account (stops it and blocks runs until re-enabled)";
  }

  const canRun = !!acct && (!!activeSerial || acct.device || gaCurrentAccountId);
  const runBtns = ["btn-farm-run"];
  const stopBtns = ["btn-farm-stop"];
  runBtns.forEach((id) => {
    const el = $(id);
    if (el) el.disabled = !canRun || running || farmBatchRunning;
  });
  stopBtns.forEach((id) => {
    const el = $(id);
    if (el) el.disabled = !running || farmBatchRunning;
  });
  updateFarmBatchButtons();
  $("btn-ctx-mirror") && ($("btn-ctx-mirror").disabled = !activeSerial && selectedSerials.size === 0);
  const typeInput = $("ctx-type-input");
  const typeBtn = $("btn-ctx-type");
  if (typeInput) typeInput.disabled = !activeSerial;
  if (typeBtn) typeBtn.disabled = !activeSerial;
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
  const noteFocus = captureFarmNoteFocus();
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
  updateFarmBatchButtons();
  restoreFarmNoteFocus(noteFocus);
  // Device links can change (e.g. serial healed on reconnect), which affects
  // which @names are offered in the pool "Add account" list.
  if ((gaBrandPools.pools || []).length) renderBrandPools();
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
  if (!quiet) {
    // Clicking a phone shows/refreshes that device's account bot log.
    renderActiveBotLog();
  }
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

async function sendPhoneText(event) {
  if (event) event.preventDefault();
  const serial = activeSerial || [...selectedSerials][0];
  const input = $("ctx-type-input");
  if (!serial) {
    log("Select a phone first", "error");
    return false;
  }
  const text = (input?.value || "").toString();
  if (!text.trim()) {
    log("Type something first", "error");
    return false;
  }
  const btn = $("btn-ctx-type");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(serial)}/type`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, enter: false }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Send failed");
    log(`Sent ${data.chars ?? text.length} chars → ${shortSerial(serial)}`);
    if (input) {
      input.value = "";
      input.focus();
    }
  } catch (err) {
    log(`Type-to-phone failed: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = !activeSerial && selectedSerials.size === 0;
  }
  return false;
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

let accountStatusPollTimer = null;

// Per-account bot log buffers so parallel runs don't interleave in one console.
// The console shows only the active device's account log.
const botLogsByAccount = {};
const storyLikesLogsByAccount = {};
const BOT_LOG_BUFFER_MAX = 600;

function activeAccountId() {
  if (!activeSerial) return null;
  return accountForDevice(activeSerial)?.id || null;
}

/* ── Per-account notes ── */
let accountNoteSaveTimer = null;
let accountNoteLastSaved = "";

function setAccountNoteStatus(text) {
  const el = $("account-notes-status");
  if (el) el.textContent = text || "";
}

function renderAccountNote(note) {
  const wrap = $("account-notes");
  const input = $("account-notes-input");
  if (!wrap || !input) return;
  wrap.classList.remove("hidden");
  input.value = note || "";
  accountNoteLastSaved = note || "";
  setAccountNoteStatus("");
}

function onAccountNoteInput() {
  setAccountNoteStatus("Editing…");
  if (accountNoteSaveTimer) clearTimeout(accountNoteSaveTimer);
  // Debounced autosave so notes persist even without leaving the field.
  accountNoteSaveTimer = setTimeout(saveAccountNote, 1200);
}

async function saveAccountNote() {
  const input = $("account-notes-input");
  if (!input || !gaCurrentAccountId) return;
  if (accountNoteSaveTimer) {
    clearTimeout(accountNoteSaveTimer);
    accountNoteSaveTimer = null;
  }
  const note = input.value;
  if (note === accountNoteLastSaved) return;
  const accountId = gaCurrentAccountId;
  setAccountNoteStatus("Saving…");
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(accountId)}/note`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      }
    );
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    accountNoteLastSaved = data.note || "";
    const acct = gaAccounts.find((a) => a.id === accountId);
    if (acct) acct.note = accountNoteLastSaved;
    setAccountNoteStatus("Saved");
    setTimeout(() => {
      if ($("account-notes-status")?.textContent === "Saved") {
        setAccountNoteStatus("");
      }
    }, 1500);
  } catch (err) {
    setAccountNoteStatus("Save failed");
    log(`Could not save note: ${err.message}`, "error");
  }
}

function pushBotLog(accountId, message) {
  if (!accountId) return;
  const buf = botLogsByAccount[accountId] || (botLogsByAccount[accountId] = []);
  buf.push(message);
  if (buf.length > BOT_LOG_BUFFER_MAX) buf.splice(0, buf.length - BOT_LOG_BUFFER_MAX);
  if (isStoryLikesLogLine(message)) {
    pushStoryLikesLog(accountId, message);
  }
}

function pushStoryLikesLog(accountId, message) {
  if (!accountId || !message) return;
  const clean = String(message).replace(/^Story likes \| /i, "").trim();
  const buf =
    storyLikesLogsByAccount[accountId] ||
    (storyLikesLogsByAccount[accountId] = []);
  buf.push(clean);
  if (buf.length > BOT_LOG_BUFFER_MAX) buf.splice(0, buf.length - BOT_LOG_BUFFER_MAX);
}

// Re-render the console with the active account's bot log. Called on device
// switch so clicking a phone shows that account's log — loading the persisted
// on-disk history (current + previous runs) so you can see why a run ended,
// then live lines continue to append via the websocket.
let botLogRenderToken = 0;
async function renderActiveBotLog() {
  const el = $("unified-log");
  if (!el) return;
  el.innerHTML = "";
  const id = activeAccountId();
  const acct = id ? gaAccounts.find((a) => a.id === id) : null;
  const label = acct ? `@${acct.username || acct.id}` : null;
  if (!id) {
    log("Select a phone to view its bot log", "info");
    return;
  }
  const token = ++botLogRenderToken;
  log(`— ${label} log — loading most recent activity…`, "info");
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(id)}/log?lines=1000`
    );
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    // A newer selection happened while we were fetching — abandon this render.
    if (token !== botLogRenderToken || activeAccountId() !== id) return;
    el.innerHTML = "";
    const lines = data.lines || [];
    if (!lines.length) {
      logRaw("(no log history found - bot has not run yet or logs were cleared)", "info");
    } else {
      // Show the most recent logs (from today or previous days)
      for (const line of lines.slice().reverse()) {
        logRaw(line, logLevelForLine(line));
      }
    }
    const statusMsg = data.running ? "(running)" : data.exists && lines.length ? "(stopped - showing history)" : "(stopped)";
    logRaw(`— ${label} log ${statusMsg} —`, "info");
    logRaw("— live updates above —", "info");
    for (const line of botLogsByAccount[id] || []) {
      log(`[bot] ${line}`, "info");
    }
  } catch (err) {
    if (token !== botLogRenderToken) return;
    el.innerHTML = "";
    log(`— ${label} log —`, "info");
    log(`Could not load log history: ${err.message}`, "error");
    for (const line of botLogsByAccount[id] || []) {
      log(`[bot] ${line}`, "info");
    }
  }
  await renderActiveStoryLikesLog({ accountId: id, label, token });
}

let storyLikesLogRenderToken = 0;
async function renderActiveStoryLikesLog(opts = {}) {
  const panel = $("story-likes-log-panel");
  const el = $("story-likes-log");
  if (!panel || !el) return;
  const id = opts.accountId || activeAccountId();
  const acct = id ? gaAccounts.find((a) => a.id === id) : null;
  updateStoryLikesLogPanelVisibility(acct);
  if (!id || !accountHasStoryLikesEnabled(acct)) {
    el.innerHTML = "";
    return;
  }
  const label = opts.label || `@${acct?.username || acct?.id || id}`;
  const token = opts.token ?? ++storyLikesLogRenderToken;
  if (!opts.token) storyLikesLogRenderToken = token;
  el.innerHTML = "";
  storyLikesLogRaw(`— ${label} story likes — loading…`, "info");
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(id)}/story-likes-log?lines=400`
    );
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    if (token !== storyLikesLogRenderToken || activeAccountId() !== id) return;
    el.innerHTML = "";
    const lines = data.lines || [];
    if (!lines.length) {
      storyLikesLogRaw("(no story likes history yet)", "info");
    } else {
      for (const line of lines.slice().reverse()) {
        storyLikesLogRaw(line, storyLikesLogLevel(line));
      }
    }
    storyLikesLogRaw(
      `— ${label} story likes ${data.running ? "(running)" : "(stopped)"} —`,
      "info"
    );
    storyLikesLogRaw("— live updates above —", "info");
    for (const line of storyLikesLogsByAccount[id] || []) {
      storyLikesLogLive(line, storyLikesLogLevel(line));
    }
  } catch (err) {
    if (token !== storyLikesLogRenderToken) return;
    el.innerHTML = "";
    storyLikesLogRaw(`Could not load story likes log: ${err.message}`, "error");
    for (const line of storyLikesLogsByAccount[id] || []) {
      storyLikesLogLive(line, storyLikesLogLevel(line));
    }
  }
}

// Poll lightweight running + recent-error + live-progress status and merge it
// into the loaded accounts, refreshing the status tags/counters WITHOUT
// reloading the config form (which would interrupt editing). Runs every 5s so
// live like/follow/comment counters feel responsive.
async function pollAccountStatus() {
  if (!gaAccounts.length) return;
  try {
    const res = await fetch("/api/gramaddict/accounts-status");
    if (!res.ok) return;
    const statuses = await res.json();
    const byId = new Map(statuses.map((s) => [s.id, s]));
    let changed = false;
    for (const acct of gaAccounts) {
      const s = byId.get(acct.id);
      if (!s) continue;
      const prev = JSON.stringify([
        acct.running,
        acct.last_error || null,
        acct.progress || null,
        acct.disabled || false,
        acct.disabled_reason || "",
        acct.story_likes_enabled || false,
      ]);
      acct.running = s.running;
      acct.last_error = s.last_error || null;
      acct.progress = s.progress || null;
      acct.disabled = s.disabled || false;
      acct.disabled_reason = s.disabled_reason || "";
      acct.story_likes_enabled = Boolean(s.story_likes_enabled);
      if (
        JSON.stringify([
          acct.running,
          acct.last_error || null,
          acct.progress || null,
          acct.disabled || false,
          acct.disabled_reason || "",
          acct.story_likes_enabled || false,
        ]) !== prev
      ) {
        changed = true;
      }
    }
    if (changed) {
      updateContextStrip();
      renderDevices();
    }
  } catch (_) {
    /* transient network error — try again next tick */
  }
}

function startAccountStatusPolling() {
  if (accountStatusPollTimer) clearInterval(accountStatusPollTimer);
  accountStatusPollTimer = setInterval(pollAccountStatus, 5000);
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
      } else if (msg.type === "accounts_changed") {
        loadGaAccounts()
          .then(() => renderDevices())
          .catch(() => {});
      } else if (msg.type === "bot_log") {
        // Buffer per account; only show the active device's account log so
        // parallel runs don't combine into one stream.
        pushBotLog(msg.account, msg.message);
        if (msg.account && msg.account === activeAccountId()) {
          log(`[bot] ${msg.message}`, "info");
          if (isStoryLikesLogLine(msg.message)) {
            const storyLine = msg.message.replace(/^Story likes \| /i, "").trim();
            storyLikesLogLive(storyLine, storyLikesLogLevel(storyLine));
          }
        }
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
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", hour12: true });
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
  const hidden = widget.querySelector('input[type="hidden"][data-ga-key]');
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
  const hidden = widget.querySelector('input[type="hidden"][data-ga-key]');
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
    const hidden = widget.querySelector('input[type="hidden"][data-ga-key]');
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
    const sub = { listKey: `${key}-list`, limitKey: `${key}-limit` };
    if (field.enable_checkbox) sub.enabledKey = `${key}-enabled`;
    return sub;
  }
  if (field.type === "inline-lines-file") {
    return { listKey: `${key}-list` };
  }
  return null;
}

function applyInlineFileJobEnableState(fieldKey = "daily-story-likes") {
  const field = document.querySelector(`.inline-file-job-field[data-inline-file-job="${fieldKey}"]`);
  if (!field) return;
  const enabledInput = field.querySelector(`[data-ga-key="${fieldKey}-enabled"]`);
  const enabled = enabledInput ? enabledInput.checked : true;
  field.classList.toggle("inline-file-job-disabled", !enabled);
  field.querySelectorAll("textarea, input.inline-file-limit").forEach((el) => {
    el.disabled = !enabled;
    el.setAttribute("aria-disabled", enabled ? "false" : "true");
  });
}

function applyAllInlineFileJobEnableStates() {
  document.querySelectorAll(".inline-file-job-field[data-inline-file-job]").forEach((field) => {
    applyInlineFileJobEnableState(field.getAttribute("data-inline-file-job"));
  });
}

const POST_REEL_ACCEPT =
  "video/mp4,video/quicktime,video/webm,video/x-m4v,.mp4,.mov,.m4v,.webm,.mkv";

function autopostLockedHandles() {
  const locked = gaSchema?.autopost_locked_accounts || ["615films", "yourlovefilms"];
  return new Set(locked.map((name) => String(name).replace(/^@/, "").toLowerCase()));
}

function isAutopostLockedAccount(accountId, username) {
  const handles = autopostLockedHandles();
  return [accountId, username]
    .filter(Boolean)
    .map((value) => String(value).replace(/^@/, "").toLowerCase())
    .some((value) => handles.has(value));
}

function applyPostReelsLock() {
  const acct = currentAccount();
  gaAccountAutopostLocked = isAutopostLockedAccount(gaCurrentAccountId, acct?.username);
  const field = document.querySelector(".post-reels-field");
  const input = document.querySelector('.post-reels-count[data-ga-key="post-reels"]');
  if (!field || !input) return;

  let note = field.querySelector(".post-reels-lock-note");
  if (gaAccountAutopostLocked) {
    input.value = "0";
    input.disabled = true;
    input.setAttribute("aria-disabled", "true");
    field.classList.add("post-reels-locked");
    if (!note) {
      note = document.createElement("p");
      note.className = "post-reels-lock-note";
      field.appendChild(note);
    }
    const handle = (acct?.username || gaCurrentAccountId || "this account").replace(/^@/, "");
    note.textContent = `Autopost locked for @${handle} — reel posting is disabled to prevent accidents.`;
  } else {
    input.disabled = false;
    input.removeAttribute("aria-disabled");
    field.classList.remove("post-reels-locked");
    note?.remove();
  }
  updatePostReelsInline();
}

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
    const header = field.enable_checkbox
      ? `<label class="ga-check inline-file-enable">
          <input type="checkbox" class="ui-checkbox" ${attr}="${sub.enabledKey}" data-inline-file-enable="${key}">
          <span class="ga-check-label">${label}</span>
        </label>`
      : `<label>${label}</label>`;
    return `
      <div class="field inline-file-job-field field-span-2" data-inline-file-job="${key}">
        <div class="inline-file-job-header">${header}</div>
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
    return `<div class="field">
      <div class="lines-field-header">
        <label>${label}</label>
        <div class="lines-import-wrap">
          <button type="button" class="btn-ghost btn-sm lines-import-btn" data-lines-import="${key}" aria-haspopup="true" aria-expanded="false" title="Load names from a .txt list saved on this account">Import .txt</button>
          <div class="lines-import-menu" data-lines-menu="${key}" role="menu" hidden></div>
        </div>
      </div>
      <textarea ${attr}="${key}" class="ga-input" rows="2" placeholder="${linesPlaceholder}"></textarea>
    </div>`;
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
  applyAllInlineFileJobEnableStates();
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
      <button type="button" class="btn-ghost btn-sm post-reel-media-distribute" data-post-reel-distribute="${escapeHtml(f.name)}" title="Copy this video to accounts on the same template">
        <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
        Copy to connected
      </button>
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
    list.querySelectorAll("[data-post-reel-distribute]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        distributePostReelMedia(btn.getAttribute("data-post-reel-distribute"), btn);
      });
    });
  });
}

async function distributePostReelMedia(filename, btn) {
  if (!gaCurrentAccountId || !filename) return;
  if (
    !window.confirm(
      `Copy “${filename}” to every account connected to this account's template?\n\n` +
        `It's added alongside their existing videos. Autopost-locked accounts are skipped.`
    )
  ) {
    return;
  }
  const origHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="ga-spinner ga-spinner-inline"></span> Copying…`;
  }
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/post-reel/media/distribute`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename }),
      }
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Copy failed");
    const okCount = (data.copied || []).length;
    const existing = (data.skipped_existing || []).length;
    const locked = (data.skipped_locked || []).length;
    const errCount = (data.errors || []).length;
    const parts = [`Copied to ${okCount} account${okCount === 1 ? "" : "s"}`];
    if (existing) parts.push(`${existing} already had it`);
    if (locked) parts.push(`${locked} locked skipped`);
    if (errCount) parts.push(`${errCount} failed`);
    const msg = parts.join(" · ");
    setPostReelUploadStatus(msg, errCount ? "error" : "success");
    log(`Distribute “${filename}”: ${msg}`);
    (data.errors || []).forEach((e) => log(`  ${e.account_id}: ${e.error}`, "error"));
  } catch (err) {
    setPostReelUploadStatus(err.message || "Copy failed", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origHtml || "Copy to connected";
    }
  }
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
  if (gaAccountAutopostLocked) {
    setPostReelUploadStatus("Reel uploads are locked for this account", "error");
    return;
  }
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
  if (
    !window.confirm(
      `Remove “${filename}” from this account AND every account connected to its template?\n\n` +
        `Autopost-locked accounts are skipped.`
    )
  ) {
    return;
  }
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/post-reel/media/delete-connected`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename }),
      }
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Delete failed");
    renderPostReelMediaList(data.files || []);
    const connectedDeleted = (data.deleted || []).length;
    const locked = (data.skipped_locked || []).length;
    const errCount = (data.errors || []).length;
    const parts = [`Removed ${filename}`];
    if (connectedDeleted)
      parts.push(`${connectedDeleted} connected account${connectedDeleted === 1 ? "" : "s"}`);
    if (locked) parts.push(`${locked} locked skipped`);
    if (errCount) parts.push(`${errCount} failed`);
    const msg = parts.join(" · ");
    setPostReelUploadStatus(msg, errCount ? "error" : "success");
    log(`Delete “${filename}”: ${msg}`);
    (data.errors || []).forEach((e) => log(`  ${e.account_id}: ${e.error}`, "error"));
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

function syncAllCommentsListWidgets() {
  document.querySelectorAll(".comments-list-widget").forEach((widget) => syncCommentsListWidget(widget));
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
  applyAllInlineFileJobEnableStates();
}

function collectGaForm() {
  const form = collectFields("data-ga-key");
  if (gaAccountAutopostLocked) form["post-reels"] = "0";
  return form;
}

let sessionEstimateTimer = null;
let lastSessionEstimate = null;

function formatActionRange(actions, key) {
  const a = actions?.[key];
  if (!a || a.high == null) return "";
  const label = (a.label || key).toLowerCase();
  return a.low === a.high ? `${a.low} ${label}` : `${a.low}–${a.high} ${label}`;
}

function formatProfileEstimate(data) {
  const p = data?.expected_profiles;
  const s = data?.successful_interactions;
  const vision = data?.ai_vision || {};
  const visionOn = !!vision.screens_profiles;
  const passPct = Math.round((vision.pass_ratio || 0.4) * 100);
  const actions = Object.fromEntries(
    (data?.action_estimates || []).map((a) => [a.action, a])
  );
  const outcomeParts = [
    formatActionRange(actions, "likes"),
    formatActionRange(actions, "follows"),
    formatActionRange(actions, "comments"),
    formatActionRange(actions, "story_likes"),
  ].filter(Boolean);
  const outcomeLine = outcomeParts.length
    ? `Then up to ${outcomeParts.join(", ")} (each has its own cap)`
    : "";

  if (!p) {
    return {
      label: "Profile visits",
      value: "—",
      detail: "",
      title: "",
      summaryPart: "",
    };
  }

  if (visionOn && s) {
    const detailParts = [
      `~${s.low}–${s.high} pass AI vision (~${passPct}%) — only these are interacted with`,
      outcomeLine,
    ].filter(Boolean);
    return {
      label: "Profile visits",
      value: `${p.low}–${p.high} opened`,
      detail: detailParts.join(". "),
      title:
        `The bot opens ${p.low}–${p.high} Instagram profiles. AI vision screenshots each ` +
        `one — only ~${passPct}% pass (~${s.low}–${s.high}). Those passing profiles ` +
        `enter your interaction pool (likes, follows, comments). Each action has its ` +
        `own limit — e.g. comments are capped separately and only a few profiles get ` +
        `commented on, not every pass.`,
      summaryPart: `${p.low}–${p.high} opened · ~${s.low}–${s.high} pass vision`,
    };
  }

  const detailParts = [
    "Instagram accounts opened to like, follow, or comment",
    outcomeLine,
  ].filter(Boolean);
  return {
    label: "Profile visits",
    value: `${p.low}–${p.high}`,
    detail: detailParts.join(". "),
    title:
      `Estimated ${p.low}–${p.high} Instagram profile visits before session limits ` +
      `stop the run. Each visit may like, follow, or comment depending on your ` +
      `percentages and per-action caps.`,
    summaryPart: `${p.low}–${p.high} profile visits`,
  };
}

function fillEstimatePanel(root, data) {
  if (!root || !data) return;
  const dur = root.querySelector('[data-est="duration"]');
  const binding = root.querySelector('[data-est="binding"]');
  const profilesLabel = root.querySelector('[data-est="profiles-label"]');
  const profiles = root.querySelector('[data-est="profiles"]');
  const profilesDetail = root.querySelector('[data-est="profiles-detail"]');
  const profilesStat = root.querySelector('[data-est="profiles-stat"]');
  const schedule = root.querySelector('[data-est="schedule"]');
  const warnBadge = root.querySelector('[data-est="warn-badge"]');
  const summary = root.querySelector('[data-est="summary"]');
  const profileEst = formatProfileEstimate(data);
  if (summary) {
    const parts = [];
    if (data.session_minutes?.label) parts.push(data.session_minutes.label);
    if (profileEst.summaryPart) parts.push(profileEst.summaryPart);
    summary.textContent = parts.join(" · ");
  }
  if (dur) dur.textContent = data.session_minutes?.label || "—";
  if (binding) binding.textContent = data.binding_limit || "—";
  if (profilesLabel) profilesLabel.textContent = profileEst.label;
  if (profiles) profiles.textContent = profileEst.value;
  if (profilesDetail) {
    profilesDetail.textContent = profileEst.detail || "";
    profilesDetail.classList.toggle("hidden", !profileEst.detail);
  }
  if (profilesStat) profilesStat.title = profileEst.title || "";
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

function applySessionEstimateCollapse() {
  const panel = $("account-session-estimate");
  const toggle = $("btn-estimate-toggle");
  if (!panel) return;
  // Collapsed by default; only expanded if the user chose to expand it.
  const collapsed = localStorage.getItem("sessionEstimateOpen") !== "1";
  panel.classList.toggle("is-collapsed", collapsed);
  toggle?.setAttribute("aria-expanded", collapsed ? "false" : "true");
}

function toggleSessionEstimate() {
  const panel = $("account-session-estimate");
  if (!panel) return;
  const nowCollapsed = !panel.classList.contains("is-collapsed");
  localStorage.setItem("sessionEstimateOpen", nowCollapsed ? "0" : "1");
  applySessionEstimateCollapse();
}

function formatRateLimitWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatRateLimitJob(job) {
  if (!job) return "—";
  return String(job).replace(/-/g, " ");
}

function renderRateLimitCell(val) {
  if (val == null || val === "") return "—";
  return escapeHtml(String(val));
}

function renderRateLimitHistory(data) {
  const panel = $("account-rate-limits");
  const summaryEl = $("account-rate-limits-summary");
  const tableWrap = $("account-rate-limits-table-wrap");
  if (!panel || !summaryEl || !tableWrap) return;

  const events = data?.events || [];
  panel.classList.toggle("hidden", !gaCurrentAccountId);

  if (!events.length) {
    summaryEl.textContent =
      "No “Try Again Later” events recorded yet. The next time Instagram rate-limits this account, counts are saved here automatically.";
    tableWrap.innerHTML = "";
    return;
  }

  const latest = data.summary || {};
  const daily = latest.daily_story_accounts;
  const parts = [];
  if (daily != null) parts.push(`${daily} daily story accounts`);
  if (latest.story_likes != null) parts.push(`${latest.story_likes} story likes`);
  if (latest.follows != null) parts.push(`${latest.follows} follows`);
  if (latest.likes != null) parts.push(`${latest.likes} likes`);
  if (latest.comments != null) parts.push(`${latest.comments} comments`);
  summaryEl.textContent = `Latest limit (${formatRateLimitWhen(latest.at)}${
    latest.job ? ` during ${formatRateLimitJob(latest.job)}` : ""
  }): ${parts.join(" · ") || "counts recorded"}.`;

  const rows = events
    .map((ev) => {
      const c = ev.counts || {};
      const dailyCount =
        c.daily_story_accounts_today ??
        c.daily_story_accounts_live ??
        c.daily_story_accounts_session;
      return `<tr>
        <td>${escapeHtml(formatRateLimitWhen(ev.at))}</td>
        <td>${escapeHtml(formatRateLimitJob(ev.job))}</td>
        <td>${renderRateLimitCell(dailyCount)}</td>
        <td>${renderRateLimitCell(c.story_likes)}</td>
        <td>${renderRateLimitCell(c.follows)}</td>
        <td>${renderRateLimitCell(c.likes)}</td>
        <td>${renderRateLimitCell(c.comments)}</td>
        <td>${renderRateLimitCell(c.interactions)}</td>
        <td>${renderRateLimitCell(ev.break_minutes)}m</td>
      </tr>`;
    })
    .join("");

  tableWrap.innerHTML = `<table class="account-rate-limits-table">
    <thead>
      <tr>
        <th>When</th>
        <th>Job</th>
        <th>Daily story accts</th>
        <th>Story likes</th>
        <th>Follows</th>
        <th>Likes</th>
        <th>Comments</th>
        <th>Interactions</th>
        <th>Pause</th>
      </tr>
    </thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function loadRateLimitHistory() {
  const panel = $("account-rate-limits");
  if (!panel || !gaCurrentAccountId) {
    panel?.classList.add("hidden");
    return;
  }
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/rate-limits`
    );
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    renderRateLimitHistory(data);
  } catch (err) {
    const summaryEl = $("account-rate-limits-summary");
    if (summaryEl) summaryEl.textContent = `Could not load rate limit history: ${err.message}`;
    panel.classList.remove("hidden");
  }
}

function renderSessionOutcomes(estimate) {
  const el = document.querySelector('[data-est="outcomes"]');
  if (!el) return;
  const actions = estimate?.action_estimates || [];
  const total = estimate?.total_actions;
  if (!actions.length && !total?.high) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  const parts = actions.map(
    (a) =>
      `<span class="account-estimate-outcome-item"><strong>${a.low}–${a.high} ${escapeHtml(a.label)}</strong></span>`
  );
  const totalHtml = total?.high
    ? `<span class="account-estimate-outcome-total"><strong>Total: ~${total.low}–${total.high} actions per session</strong></span>`
    : "";
  el.innerHTML = `<div class="account-estimate-outcomes-line"><strong>Expected outcomes per session:</strong> ${parts.join(", ")}.${totalHtml ? ` ${totalHtml}.` : ""}</div>`;
  el.classList.remove("hidden");
}

function markExplanationStale() {
  // AI explanation removed; nothing to invalidate.
}

function syncSessionExplanationForAccount() {
  // AI explanation removed; deterministic outcomes line is rendered separately.
}

function renderSessionEstimate(estimate) {
  if (estimate) lastSessionEstimate = estimate;
  const data = estimate || lastSessionEstimate;
  const panel = $("account-session-estimate");
  if (!panel) return;
  const show = !!(data && gaCurrentAccountId);
  panel.classList.toggle("hidden", !show);
  if (!show) return;
  applySessionEstimateCollapse();
  fillEstimatePanel(panel, data);
  renderSessionOutcomes(data);
  syncSessionExplanationForAccount();
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
        follow_vision: collectFields("data-fv-key"),
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
  markExplanationStale();
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
let tmplApplyId = null; // template id currently in the apply step

async function loadSettingsTemplateSources() {
  try {
    const res = await fetch("/api/gramaddict/templates");
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    gaSettingTemplates = data.templates || [];
  } catch (_) {
    gaSettingTemplates = [];
  }
  renderAppliedTemplateStatus();
  if (!$("template-manager-overlay")?.classList.contains("hidden")) {
    // Refresh whichever view is open.
    if (tmplApplyId && gaSettingTemplates.some((t) => t.id === tmplApplyId)) {
      renderTemplateApplyView();
    } else {
      showTemplateView("library");
      renderTemplateLibrary();
    }
  }
}

function appliedTemplateForAccount(accountId) {
  for (const t of gaSettingTemplates) {
    const member = (t.applied_to || []).find((m) => m.account_id === accountId);
    if (member) return { template: t, member };
  }
  return null;
}

/* Compact status line on the account page. */
function renderAppliedTemplateStatus() {
  const el = $("account-applied-template");
  if (!el) return;
  const applied = gaCurrentAccountId
    ? appliedTemplateForAccount(gaCurrentAccountId)
    : null;
  if (!applied) {
    el.className = "account-template-status-value is-none";
    el.textContent = "No template applied";
    return;
  }
  const { template, member } = applied;
  const modified = !!member.modified;
  el.className = "account-template-status-value";
  el.innerHTML = `${escapeHtml(template.name || template.id)} <span class="tmpl-pill ${
    modified ? "tmpl-pill-warn" : "tmpl-pill-ok"
  }">${modified ? "Modified" : "In sync"}</span>`;
}

function fmtTemplateDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/* ── Modal shell ── */

function openTemplateManager() {
  const overlay = $("template-manager-overlay");
  if (!overlay) return;
  overlay.classList.remove("hidden");
  document.body.classList.add("modal-open");
  hideNewTemplateForm();
  showTemplateView("library");
  loadSettingsTemplateSources();
  renderTemplateLibrary();
}

function closeTemplateManager() {
  const overlay = $("template-manager-overlay");
  if (!overlay) return;
  overlay.classList.add("hidden");
  document.body.classList.remove("modal-open");
  tmplApplyId = null;
}

function showTemplateView(view) {
  const lib = $("tmpl-view-library");
  const apply = $("tmpl-view-apply");
  const back = $("btn-tmpl-back");
  const title = $("template-manager-title");
  const subtitle = $("template-manager-subtitle");
  const isApply = view === "apply";
  lib?.classList.toggle("hidden", isApply);
  apply?.classList.toggle("hidden", !isApply);
  back?.classList.toggle("hidden", !isApply);
  if (!isApply) {
    tmplApplyId = null;
    if (title) title.textContent = "Templates";
    if (subtitle)
      subtitle.textContent =
        "Save an account’s settings once, then apply them to any account. Each account always keeps its own username and phone link.";
  }
}

/* ── Save-as-template inline form ── */

function showNewTemplateForm() {
  if (!gaCurrentAccountId) {
    setGaStatus("Open an account first", "error");
    return;
  }
  $("tmpl-new-form")?.classList.remove("hidden");
  $("btn-tmpl-new")?.classList.add("hidden");
  const input = $("template-save-name");
  if (input) {
    const acct = gaAccounts.find((a) => a.id === gaCurrentAccountId);
    input.value = "";
    input.placeholder = `Template from @${acct?.username || gaCurrentAccountId}…`;
    input.focus();
  }
}

function hideNewTemplateForm() {
  $("tmpl-new-form")?.classList.add("hidden");
  $("btn-tmpl-new")?.classList.remove("hidden");
}

async function saveCurrentAsTemplate() {
  if (!gaCurrentAccountId) return;
  const name = ($("template-save-name")?.value || "").trim();
  if (!name) {
    setGaStatus("Enter a template name", "error");
    $("template-save-name")?.focus();
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
    hideNewTemplateForm();
    setGaStatus(`Saved template “${data.name || data.id}”`, "success");
    log(`Saved settings template: ${data.id}`);
    await loadSettingsTemplateSources();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

/* ── Library view (browse) ── */

function renderTemplateLibrary() {
  const list = $("template-manager-list");
  if (!list) return;
  if (!gaSettingTemplates.length) {
    list.innerHTML = `
      <div class="tmpl-empty">
        <div class="tmpl-empty-icon" aria-hidden="true">
          <svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/></svg>
        </div>
        <h3 class="tmpl-empty-title">No templates yet</h3>
        <p class="tmpl-empty-text">Set up one account exactly how you like it, then use <strong>Save current account as template</strong> above. You can apply it to any other account in one click.</p>
      </div>`;
    return;
  }
  list.innerHTML = gaSettingTemplates
    .map((t) => {
      const applied = t.applied_to || [];
      const usedBy = applied.length
        ? `Used by ${applied.length} account${applied.length === 1 ? "" : "s"}`
        : "Not applied yet";
      const meta = [
        t.source_account ? `From @${escapeHtml(t.source_account)}` : "",
        fmtTemplateDate(t.created_at) ? `Created ${fmtTemplateDate(t.created_at)}` : "",
        `${t.file_count || 0} files`,
      ]
        .filter(Boolean)
        .join(" · ");
      const chips = applied.length
        ? `<div class="tmpl-card-chips">${applied
            .map(
              (m) =>
                `<span class="tmpl-chip${m.modified ? " is-modified" : ""}" title="${
                  m.modified ? "Edited since applied" : "In sync with template"
                }">@${escapeHtml(m.username)}</span>`
            )
            .join("")}</div>`
        : "";
      return `
      <div class="tmpl-card" data-template-id="${escapeHtml(t.id)}">
        <div class="tmpl-card-info">
          <h3 class="tmpl-card-name">${escapeHtml(t.name || t.id)}</h3>
          <span class="tmpl-card-meta">${escapeHtml(meta)} · ${usedBy}</span>
          ${chips}
        </div>
        <div class="tmpl-card-actions">
          ${
            applied.length
              ? `<button type="button" class="btn-ghost btn-sm btn-primary-outline tmpl-sync-btn" title="Overwrite every connected account with this template" onclick="syncTemplateToConnected('${escapeHtml(t.id)}', this)">
                  <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>
                  Apply to ${applied.length} connected
                </button>`
              : ""
          }
          <button type="button" class="btn-ghost btn-sm" onclick="startTemplateApply('${escapeHtml(t.id)}')">Apply…</button>
          <button type="button" class="tmpl-icon-btn" title="Rename" aria-label="Rename template" onclick="renameTemplate('${escapeHtml(t.id)}')">
            <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
          </button>
          <button type="button" class="tmpl-icon-btn tmpl-icon-btn-danger" title="Delete" aria-label="Delete template" onclick="deleteTemplate('${escapeHtml(t.id)}')">
            <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      </div>`;
    })
    .join("");
}

/* ── Apply view (choose accounts) ── */

function startTemplateApply(templateId) {
  const tmpl = gaSettingTemplates.find((t) => t.id === templateId);
  if (!tmpl) return;
  tmplApplyId = templateId;
  showTemplateView("apply");
  renderTemplateApplyView();
}

function renderTemplateApplyView() {
  const container = $("tmpl-view-apply");
  const tmpl = gaSettingTemplates.find((t) => t.id === tmplApplyId);
  if (!container || !tmpl) return;
  const title = $("template-manager-title");
  const subtitle = $("template-manager-subtitle");
  if (title) title.textContent = `Apply “${tmpl.name || tmpl.id}”`;
  if (subtitle)
    subtitle.textContent =
      "Choose which accounts get this template. It replaces their settings — usernames and phone links stay.";
  const appliedIds = new Set((tmpl.applied_to || []).map((m) => m.account_id));
  const rows = gaAccounts.length
    ? gaAccounts
        .map((a) => {
          const isCurrent = a.id === gaCurrentAccountId;
          const already = appliedIds.has(a.id);
          return `
        <label class="tmpl-target">
          <input type="checkbox" class="ui-checkbox tmpl-target-cb" value="${escapeHtml(a.id)}"${
            isCurrent ? " checked" : ""
          } onchange="updateTemplateApplyCount()">
          <span class="tmpl-target-name">@${escapeHtml(a.username || a.id)}</span>
          ${isCurrent ? '<span class="tmpl-target-tag">current</span>' : ""}
          ${already ? '<span class="tmpl-target-tag tmpl-target-tag-muted">using this</span>' : ""}
        </label>`;
        })
        .join("")
    : '<p class="tmpl-empty-text">No accounts available.</p>';
  container.innerHTML = `
    <div class="tmpl-apply-controls">
      <span class="tmpl-apply-controls-label">Apply to accounts</span>
      <div class="tmpl-apply-controls-actions">
        <button type="button" class="link-btn" onclick="toggleAllTemplateTargets(true)">Select all</button>
        <button type="button" class="link-btn" onclick="toggleAllTemplateTargets(false)">Clear</button>
      </div>
    </div>
    <div class="tmpl-target-list">${rows}</div>
    <label class="ga-check tmpl-apply-lists">
      <input type="checkbox" class="ui-checkbox" id="tmpl-apply-include-lists" checked>
      <span class="ga-check-label">Also copy username lists (whitelist, blacklist, story-like list, etc.)</span>
    </label>
    <div class="tmpl-apply-foot">
      <button type="button" class="btn-ghost btn-sm" onclick="showTemplateView('library')">Cancel</button>
      <button type="button" class="btn-ghost btn-sm btn-primary-outline" id="tmpl-apply-confirm" onclick="confirmTemplateApply()">Apply to 1 account</button>
    </div>`;
  updateTemplateApplyCount();
}

function toggleAllTemplateTargets(checked) {
  document
    .querySelectorAll("#tmpl-view-apply .tmpl-target-cb")
    .forEach((el) => (el.checked = checked));
  updateTemplateApplyCount();
}

function selectedApplyTargets() {
  return [...document.querySelectorAll("#tmpl-view-apply .tmpl-target-cb:checked")].map(
    (el) => el.value
  );
}

function updateTemplateApplyCount() {
  const btn = $("tmpl-apply-confirm");
  if (!btn) return;
  const n = selectedApplyTargets().length;
  btn.disabled = n === 0;
  btn.textContent = n === 1 ? "Apply to 1 account" : `Apply to ${n} accounts`;
}

async function confirmTemplateApply() {
  const templateId = tmplApplyId;
  const tmpl = gaSettingTemplates.find((t) => t.id === templateId);
  if (!tmpl) return;
  const accountIds = selectedApplyTargets();
  if (!accountIds.length) return;
  const includeLists = !!$("tmpl-apply-include-lists")?.checked;
  const btn = $("tmpl-apply-confirm");
  const origLabel = btn?.textContent;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="ga-spinner ga-spinner-inline"></span> Applying…`;
  }
  try {
    if (accountIds.includes(gaCurrentAccountId)) await flushAutosave();
    const res = await fetch(
      `/api/gramaddict/templates/${encodeURIComponent(templateId)}/apply`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_ids: accountIds, include_lists: includeLists }),
      }
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Apply failed");
    const okCount = (data.applied || []).length;
    const errCount = (data.errors || []).length;
    let msg = `Applied “${tmpl.name || templateId}” to ${okCount} account${okCount === 1 ? "" : "s"}`;
    if (errCount) msg += ` (${errCount} failed)`;
    setGaStatus(msg, errCount ? "error" : "success");
    log(msg);
    (data.errors || []).forEach((e) => log(`  ${e.account_id}: ${e.error}`, "error"));
    if (accountIds.includes(gaCurrentAccountId)) await onGaAccountChange();
    await loadSettingsTemplateSources();
    showTemplateView("library");
    renderTemplateLibrary();
  } catch (err) {
    setGaStatus(err.message, "error");
    if (btn) {
      btn.disabled = false;
      btn.textContent = origLabel || "Apply";
    }
  }
}

/* One-click: push a template to every account already connected to it. */
async function syncTemplateToConnected(templateId, btn) {
  const tmpl = gaSettingTemplates.find((t) => t.id === templateId);
  if (!tmpl) return;
  const members = tmpl.applied_to || [];
  const accountIds = members.map((m) => m.account_id).filter(Boolean);
  if (!accountIds.length) {
    setGaStatus("No connected accounts to apply to", "error");
    return;
  }
  const names = members.map((m) => `@${m.username || m.account_id}`).join(", ");
  const plural = accountIds.length === 1 ? "" : "s";
  if (
    !confirm(
      `Apply “${tmpl.name || templateId}” to ${accountIds.length} connected account${plural}?\n\n` +
        `${names}\n\n` +
        `This overwrites their settings AND username lists (whitelist, blacklist, story-like list, etc.). ` +
        `Each account keeps its own username and phone link.`
    )
  ) {
    return;
  }
  const origLabel = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="ga-spinner ga-spinner-inline"></span> Applying…`;
  }
  try {
    if (accountIds.includes(gaCurrentAccountId)) await flushAutosave();
    const res = await fetch(
      `/api/gramaddict/templates/${encodeURIComponent(templateId)}/apply`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_ids: accountIds, include_lists: true }),
      }
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Apply failed");
    const okCount = (data.applied || []).length;
    const errCount = (data.errors || []).length;
    let msg = `Applied “${tmpl.name || templateId}” to ${okCount} connected account${
      okCount === 1 ? "" : "s"
    }`;
    if (errCount) msg += ` (${errCount} failed)`;
    setGaStatus(msg, errCount ? "error" : "success");
    log(msg);
    (data.errors || []).forEach((e) => log(`  ${e.account_id}: ${e.error}`, "error"));
    if (accountIds.includes(gaCurrentAccountId)) await onGaAccountChange();
    await loadSettingsTemplateSources();
    renderTemplateLibrary();
  } catch (err) {
    setGaStatus(err.message, "error");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origLabel || "Apply to connected";
    }
  }
}

async function renameTemplate(templateId) {
  const tmpl = gaSettingTemplates.find((t) => t.id === templateId);
  const next = prompt("Rename template", tmpl?.name || templateId);
  if (next === null) return;
  const name = next.trim();
  if (!name || name === tmpl?.name) return;
  try {
    const res = await fetch(`/api/gramaddict/templates/${encodeURIComponent(templateId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Rename failed");
    setGaStatus(`Renamed template to “${data.name}”`, "success");
    await loadSettingsTemplateSources();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function deleteTemplate(templateId) {
  const tmpl = gaSettingTemplates.find((t) => t.id === templateId);
  if (!confirm(`Delete template “${tmpl?.name || templateId}”?\n\nAccounts already using it keep their settings — they just stop tracking this template.`)) {
    return;
  }
  try {
    const res = await fetch(`/api/gramaddict/templates/${encodeURIComponent(templateId)}`, {
      method: "DELETE",
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Delete failed");
    setGaStatus("Template deleted", "success");
    await loadSettingsTemplateSources();
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

function bindSettingsTemplates() {
  $("btn-manage-templates")?.addEventListener("click", openTemplateManager);
  $("btn-close-template-manager")?.addEventListener("click", closeTemplateManager);
  $("btn-tmpl-back")?.addEventListener("click", () => {
    showTemplateView("library");
    renderTemplateLibrary();
  });
  $("btn-tmpl-new")?.addEventListener("click", showNewTemplateForm);
  $("btn-tmpl-new-cancel")?.addEventListener("click", hideNewTemplateForm);
  $("btn-save-template")?.addEventListener("click", saveCurrentAsTemplate);
  $("template-save-name")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      saveCurrentAsTemplate();
    }
  });
  $("template-manager-overlay")?.addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeTemplateManager();
  });
  document.addEventListener("keydown", (e) => {
    if (
      e.key === "Escape" &&
      !$("template-manager-overlay")?.classList.contains("hidden")
    ) {
      closeTemplateManager();
    }
  });
}

// Accounts whose username should always be pinned to the top, in this order.
const PINNED_ACCOUNT_USERNAMES = ["yourlovefilms", "615films"];

function sortAccountsPinned(accounts) {
  const rank = (a) => {
    const name = String(a.username || a.id || "").trim().toLowerCase();
    const idx = PINNED_ACCOUNT_USERNAMES.indexOf(name);
    return idx === -1 ? PINNED_ACCOUNT_USERNAMES.length : idx;
  };
  // Stable sort: pinned accounts move to the top in the configured order;
  // everything else keeps the order the API returned.
  return [...accounts].sort((a, b) => rank(a) - rank(b));
}

async function loadGaAccounts() {
  try {
    const res = await fetch("/api/gramaddict/accounts");
    if (!res.ok) throw new Error(await res.text());
    gaAccounts = sortAccountsPinned(await res.json());
    const select = $("ga-account-select");
    const fields = $("ga-config-fields");
    const empty = $("ga-no-accounts");
    if (!select) return;
    if (!gaAccounts.length) {
      select.innerHTML = "";
      fields?.classList.add("hidden");
      empty?.classList.remove("hidden");
      $("account-session-estimate")?.classList.add("hidden");
      $("account-templates-bar")?.classList.add("hidden");
      $("account-notes")?.classList.add("hidden");
      gaCurrentAccountId = "";
      syncRunButtons(false);
      $("btn-delete-account")?.setAttribute("disabled", "disabled");
      renderDevices();
      return;
    }
    empty?.classList.add("hidden");
    fields?.classList.remove("hidden");
    $("account-templates-bar")?.classList.remove("hidden");
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
  // Flush pending edits to the account currently in the form BEFORE switching.
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
    renderAccountNote(data.note || "");
    fillGaForm(data.form || {});
    gaAccountAutopostLocked = !!data.autopost_locked;
    applyPostReelsLock();
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
    loadRateLimitHistory();
    setGaStatus("");
    updateContextStrip();
    await loadSettingsTemplateSources();
    if (currentMainTab === "tools") await loadAdvFiles();
    // Form now represents this account — all autosaves must target it.
    gaFormAccountId = gaCurrentAccountId;
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
  const accountId = opts.accountId || formAccountId();
  if (!accountId) {
    if (!quiet) setGaStatus("Create or select an account first", "error");
    return;
  }
  setGaStatus("Saving…", "");
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}`, {
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
      log(`Saved config for ${accountId}`);
      await loadGaAccounts();
    }
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaRawYaml(opts = {}) {
  const quiet = opts.quiet === true;
  const accountId = opts.accountId || formAccountId();
  if (!accountId) return;
  const raw = $("ga-raw-yaml")?.value;
  if (raw === undefined) return;
  setGaStatus("Saving…", "");
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}`, {
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
      log(`Saved raw YAML for ${accountId}`);
    }
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaFilters(opts = {}) {
  const quiet = opts.quiet === true;
  const accountId = opts.accountId || formAccountId();
  if (!accountId) return;
  setGaStatus("Saving…", "");
  try {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/filters`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filters: collectFields("data-filter-key") }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Save failed");
    if (!quiet) {
      fillFields("data-filter-key", data.form || {});
      log(`Saved filters for ${accountId}`);
    }
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaPosting(opts = {}) {
  const quiet = opts.quiet === true;
  const accountId = opts.accountId || formAccountId();
  if (!accountId) return;
  setGaStatus("Saving…", "");
  try {
    const [settingsRes, promptsRes, fvSettingsRes, fvPromptsRes] = await Promise.all([
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/post-reel`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ post_reel: collectFields("data-pr-key") }),
      }),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/post-reel/prompts`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompts: collectPostReelPrompts() }),
      }),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/follow-vision`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ follow_vision: collectFields("data-fv-key") }),
      }),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/follow-vision/prompts`, {
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
    if (!quiet) log(`Saved posting settings for ${accountId}`);
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaReports(opts = {}) {
  const quiet = opts.quiet === true;
  const accountId = opts.accountId || formAccountId();
  if (!accountId) return;
  setGaStatus("Saving…", "");
  try {
    const [cfgRes, tgRes] = await Promise.all([
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: collectGaForm() }),
      }),
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/telegram`, {
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
      log(`Saved reports for ${accountId}`);
    }
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveAccountTextFiles(fileKeys, accountId = formAccountId()) {
  if (!accountId) return;
  for (const name of fileKeys) {
    const el = document.querySelector(`[data-file-key="${name}"]`);
    if (!el) continue;
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(accountId)}/files/${encodeURIComponent(name)}`,
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
  const accountId = opts.accountId || formAccountId();
  if (!gaFilesMeta?.lists || !accountId) return;
  setGaStatus("Saving…", "");
  try {
    await saveAccountTextFiles(Object.keys(gaFilesMeta.lists), accountId);
    if (!quiet) log(`Saved lists for ${accountId}`);
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

async function saveGaComments(opts = {}) {
  const quiet = opts.quiet === true;
  const accountId = opts.accountId || formAccountId();
  if (!gaFilesMeta?.text || !accountId) return;
  syncAllCommentsListWidgets();
  setGaStatus("Saving…", "");
  try {
    await saveAccountTextFiles(Object.keys(gaFilesMeta.text), accountId);
    if (!quiet) log(`Saved comments/PM for ${accountId}`);
    setGaStatus("Saved", "success");
  } catch (err) {
    setGaStatus(err.message, "error");
  }
}

function onAccountFieldInput(event) {
  if (gaFormLoading || !formAccountId()) return;
  const target = event.target;
  if (!target) return;

  const fieldContainer =
    target.closest(".field") ||
    target.closest(".ga-check") ||
    target.closest(".ga-form-section");
  setActiveSaveField(fieldContainer);
  const accountId = formAccountId();

  if (target.id === "ga-raw-yaml") {
    scheduleAutosave("raw-yaml", () => saveGaRawYaml({ quiet: true, accountId }), 1500);
    return;
  }
  if (target.matches("[data-ga-key]")) {
    if (target.getAttribute("data-ga-key") === "post-reels" && gaAccountAutopostLocked) {
      target.value = "0";
      applyPostReelsLock();
      return;
    }
    if (target.hasAttribute("data-inline-file-enable")) {
      applyInlineFileJobEnableState(target.getAttribute("data-inline-file-enable"));
    }
    if (target.getAttribute("data-ga-key") === "post-reels") {
      updatePostReelsInline();
      if (postReelsCountValue() > 0) loadPostReelMedia();
    }
    scheduleSessionEstimateRefresh();
    if (currentAccountTab === "reports") {
      scheduleAutosave("reports", () => saveGaReports({ quiet: true, accountId }));
    } else {
      scheduleAutosave("config", () => saveGaConfig({ quiet: true, accountId }));
    }
    return;
  }
  if (target.matches("[data-filter-key]")) {
    scheduleAutosave("filters", () => saveGaFilters({ quiet: true, accountId }));
    return;
  }
  if (target.matches("[data-tg-key]")) {
    scheduleAutosave("reports", () => saveGaReports({ quiet: true, accountId }));
    return;
  }
  if (target.matches("[data-pr-key]") || target.matches("[data-pr-prompt]")) {
    scheduleSessionEstimateRefresh();
    scheduleAutosave("posting", () => saveGaPosting({ quiet: true, accountId }));
    return;
  }
  if (target.matches("[data-fv-key]") || target.matches("[data-fv-prompt]")) {
    scheduleAutosave("posting", () => saveGaPosting({ quiet: true, accountId }));
    return;
  }
  if (target.matches("[data-file-key]")) {
    const name = target.dataset.fileKey;
    if (gaFilesMeta?.lists?.[name]) {
      scheduleAutosave("lists", () => saveGaLists({ quiet: true, accountId }));
    } else if (gaFilesMeta?.text?.[name]) {
      scheduleAutosave("comments", () => saveGaComments({ quiet: true, accountId }));
    }
  }
}

function applyImportedLines(key, text, sourceLabel) {
  const textarea = document.querySelector(`textarea[data-ga-key="${key}"]`);
  if (!textarea) return;
  const imported = text
    .split(/[\r\n,]+/)
    .map((s) => s.trim().replace(/^@/, ""))
    .filter(Boolean);
  if (!imported.length) {
    log(`${sourceLabel} had no usernames/tags to import`, "error");
    return;
  }
  const existing = textarea.value
    .split(/[\r\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const merged = [];
  const seen = new Set();
  for (const item of [...existing, ...imported]) {
    const dedupeKey = item.toLowerCase();
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    merged.push(item);
  }
  const added = merged.length - existing.length;
  textarea.value = merged.join("\n");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  log(
    `Imported ${added} new entr${added === 1 ? "y" : "ies"} from ${sourceLabel}` +
      (added < imported.length ? ` (${imported.length - added} already present)` : "")
  );
}

function closeAllLinesImportMenus(exceptKey) {
  document.querySelectorAll("[data-lines-menu]").forEach((menu) => {
    if (exceptKey && menu.getAttribute("data-lines-menu") === exceptKey) return;
    menu.hidden = true;
    const btn = document.querySelector(`[data-lines-import="${menu.getAttribute("data-lines-menu")}"]`);
    btn?.setAttribute("aria-expanded", "false");
  });
}

async function toggleLinesImportMenu(key, btn) {
  const menu = document.querySelector(`[data-lines-menu="${key}"]`);
  if (!menu) return;
  if (!menu.hidden) {
    closeAllLinesImportMenus();
    return;
  }
  closeAllLinesImportMenus(key);
  if (!gaCurrentAccountId) {
    log("Select an account before importing a list", "error");
    return;
  }
  menu.innerHTML = `<div class="lines-import-empty">Loading…</div>`;
  menu.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  let files = [];
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/files`
    );
    if (!res.ok) throw new Error("Failed to load files");
    files = (await res.json()).filter((f) => f.name.toLowerCase().endsWith(".txt"));
  } catch (err) {
    menu.innerHTML = `<div class="lines-import-empty">${escapeHtml(err.message)}</div>`;
    return;
  }
  if (!files.length) {
    menu.innerHTML = `<div class="lines-import-empty">No .txt lists on this account</div>`;
    return;
  }
  menu.innerHTML = files
    .map(
      (f) =>
        `<button type="button" class="lines-import-item" role="menuitem" data-lines-menu-key="${escapeHtml(
          key
        )}" data-lines-menu-file="${escapeHtml(f.name)}">${escapeHtml(f.name)}</button>`
    )
    .join("");
}

async function applyLinesFromAccountFile(key, filename) {
  if (!gaCurrentAccountId) return;
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(gaCurrentAccountId)}/files/${encodeURIComponent(filename)}`
    );
    if (!res.ok) throw new Error(`Could not read ${filename}`);
    const data = await res.json();
    applyImportedLines(key, data.content || "", filename);
  } catch (err) {
    log(err.message, "error");
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
    root.addEventListener("click", (event) => {
      const importBtn = event.target.closest("[data-lines-import]");
      if (importBtn) {
        toggleLinesImportMenu(importBtn.getAttribute("data-lines-import"), importBtn);
        return;
      }
      const item = event.target.closest("[data-lines-menu-file]");
      if (item) {
        applyLinesFromAccountFile(
          item.getAttribute("data-lines-menu-key"),
          item.getAttribute("data-lines-menu-file")
        );
        closeAllLinesImportMenus();
        return;
      }
      if (!event.target.closest("[data-lines-menu]")) closeAllLinesImportMenus();
    });
  }
  document.addEventListener("click", (event) => {
    if (event.target.closest(".lines-import-wrap")) return;
    closeAllLinesImportMenus();
  });
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

function getFarmRunMode() {
  const select = $("farm-run-mode");
  return select?.value || localStorage.getItem("farmRunMode") || "consecutive";
}

function onFarmRunModeChange() {
  const mode = getFarmRunMode();
  localStorage.setItem("farmRunMode", mode);
}

function initFarmRunMode() {
  const select = $("farm-run-mode");
  if (!select) return;
  // Default parallel — morning start always runs parallel anyway.
  const saved = localStorage.getItem("farmRunMode") || "parallel";
  if ([...select.options].some((o) => o.value === saved)) select.value = saved;
}

function farmBatchSerials() {
  if (selectedSerials.size > 0) return [...selectedSerials];
  return activeSerial ? [activeSerial] : [];
}

function resolveFarmBatchTargets() {
  const serials = farmBatchSerials();
  const targets = [];
  const skipped = [];
  for (const serial of serials) {
    const account = accountForDevice(serial);
    if (!account) {
      skipped.push({ serial, reason: "no linked account" });
      continue;
    }
    if (account.disabled) {
      skipped.push({ serial, reason: "disabled" });
      continue;
    }
    if (account.running) {
      skipped.push({ serial, reason: "already running" });
      continue;
    }
    targets.push({ serial, account });
  }
  return { targets, skipped, serials };
}

function updateFarmBatchButtons() {
  const { targets, skipped, serials } = resolveFarmBatchTargets();
  const anyRunningSelected = serials.some((serial) => accountForDevice(serial)?.running);
  const runSelected = $("btn-farm-run-selected");
  const stopSelected = $("btn-farm-stop-selected");
  if (runSelected) {
    runSelected.disabled = farmBatchRunning || targets.length === 0;
    runSelected.title =
      targets.length === 0
        ? skipped.length
          ? "Selected phones need linked accounts (and must not already be running)"
          : "Select one or more phones with the checkboxes"
        : `${targets.length} phone(s) ready — ${getFarmRunMode() === "parallel" ? "parallel" : "consecutive"}`;
  }
  if (stopSelected) {
    stopSelected.disabled = !farmBatchRunning && !anyRunningSelected;
  }
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForBotDone(accountId) {
  while (!farmBatchCancel) {
    const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/status`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Status check failed");
    if (!data.running) return true;
    await sleepMs(3000);
  }
  return false;
}

function clearStoryLikesLogBuffer(accountId) {
  if (!accountId) return;
  delete storyLikesLogsByAccount[accountId];
  if (accountId === activeAccountId()) {
    renderActiveStoryLikesLog();
  }
}

function clearBotLog(accountId) {
  if (!accountId) return;
  delete botLogsByAccount[accountId];
  if (accountId === activeAccountId()) renderActiveBotLog();
}

async function toggleAccountDisabled() {
  const acct = currentAccount();
  if (!acct) return;
  const label = `@${acct.username || acct.id}`;
  let reason = acct.disabled_reason || "";
  if (!acct.disabled) {
    reason = prompt(`Disable ${label}? Optional reason (e.g. selfie verification):`, "");
    if (reason === null) return; // cancelled
  }
  try {
    const res = await fetch(
      `/api/gramaddict/accounts/${encodeURIComponent(acct.id)}/disable`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ disabled: !acct.disabled, reason }),
      }
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to update account");
    acct.disabled = data.disabled;
    acct.disabled_reason = data.disabled_reason || "";
    if (data.disabled) acct.running = false;
    log(
      data.disabled
        ? `Disabled ${label}${reason ? ` — ${reason}` : ""}`
        : `Enabled ${label}`,
      "success"
    );
    await loadGaAccounts();
    updateContextStrip();
    renderDevices();
  } catch (err) {
    log(err.message, "error");
    setGaStatus(err.message, "error");
  }
}

async function startBotForFarmTarget(serial, accountId) {
  // Fresh run → drop the previous run's buffered log for this account.
  clearBotLog(accountId);
  clearStoryLikesLogBuffer(accountId);
  const res = await fetch(`/api/gramaddict/accounts/${encodeURIComponent(accountId)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_serial: serial, vpn_app_name: getVpnAppName() }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Failed to start bot");
  return data;
}

async function runFarmBatch() {
  if (farmBatchRunning) return;
  const { targets, skipped } = resolveFarmBatchTargets();
  if (targets.length === 0) {
    const detail =
      skipped.length > 0
        ? skipped.map((s) => `${shortSerial(s.serial)}: ${s.reason}`).join("; ")
        : "Select phones with linked accounts";
    setGaStatus(detail, "error");
    log(detail, "error");
    return;
  }

  const mode = getFarmRunMode();
  localStorage.setItem("farmRunMode", mode);
  farmBatchRunning = true;
  farmBatchCancel = false;
  updateFarmBatchButtons();
  updateContextStrip();

  for (const item of skipped) {
    log(`Skip ${shortSerial(item.serial)} — ${item.reason}`, "error");
  }
  log(
    `Farm batch (${mode === "parallel" ? "parallel" : "one after another"}): ${targets.length} phone(s)`,
    "info"
  );

  try {
    if (mode === "parallel") {
      for (let i = 0; i < targets.length; i += 1) {
        if (farmBatchCancel) break;
        const { serial, account } = targets[i];
        log(`Starting @${account.username || account.id} on ${shortSerial(serial)}…`);
        await startBotForFarmTarget(serial, account.id);
        if (i < targets.length - 1) await sleepMs(1500);
      }
      setGaStatus(`Started ${targets.length} bot(s) in parallel`, "success");
    } else {
      for (const { serial, account } of targets) {
        if (farmBatchCancel) break;
        const label = account.username || account.id;
        log(`Queue: starting @${label} on ${shortSerial(serial)}…`);
        await startBotForFarmTarget(serial, account.id);
        await loadGaAccounts();
        renderDevices();
        const finished = await waitForBotDone(account.id);
        if (!finished) {
          log(`Queue cancelled while @${label} was running`, "error");
          break;
        }
        log(`Finished @${label} on ${shortSerial(serial)}`, "success");
        await loadGaAccounts();
        renderDevices();
      }
      if (!farmBatchCancel) {
        setGaStatus(`Farm queue finished (${targets.length} phone(s))`, "success");
      }
    }
  } catch (err) {
    setGaStatus(err.message, "error");
    log(err.message, "error");
  } finally {
    farmBatchRunning = false;
    farmBatchCancel = false;
    await loadGaAccounts();
    renderDevices();
    updateContextStrip();
  }
}

async function stopFarmBatch() {
  // Hard kill: cancel any in-flight batch and fire every stop at once with
  // force=true so all selected bots die instantly (parallel, no grace period).
  farmBatchCancel = true;
  farmBatchRunning = false;

  const targets = [];
  const seen = new Set();
  for (const serial of farmBatchSerials()) {
    const account = accountForDevice(serial);
    if (!account?.running || seen.has(account.id)) continue;
    seen.add(account.id);
    targets.push(account);
    account.running = false; // optimistic: reflect the kill immediately
  }

  if (!targets.length) {
    updateFarmBatchButtons();
    updateContextStrip();
    renderDevices();
    return;
  }

  // Instant visual feedback before the network round-trips resolve.
  renderDevices();
  updateContextStrip();
  log(`Killing ${targets.length} bot(s)…`, "info");

  const results = await Promise.allSettled(
    targets.map((account) =>
      fetch(`/api/gramaddict/accounts/${encodeURIComponent(account.id)}/stop?force=true`, {
        method: "POST",
      }).then(async (res) => {
        const data = await res.json();
        if (!res.ok || !data.stopped) throw new Error(data.detail || data.message || "Stop failed");
        return account.username || account.id;
      })
    )
  );

  const stopped = [];
  const errors = [];
  results.forEach((r, i) => {
    if (r.status === "fulfilled") stopped.push(r.value);
    else errors.push(`${targets[i].username || targets[i].id}: ${r.reason?.message || r.reason}`);
  });

  if (stopped.length) log(`Stopped: ${stopped.map((n) => `@${n}`).join(", ")}`, "success");
  if (errors.length) log(errors.join("; "), "error");
  if (stopped.length || errors.length) {
    setGaStatus(stopped.length ? `Stopped ${stopped.length} bot(s)` : errors[0], stopped.length ? "success" : "error");
  }
  await loadGaAccounts();
  renderDevices();
  updateContextStrip();
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
    await saveAllBeforeRun();
    clearBotLog(accountId);
    clearStoryLikesLogBuffer(accountId);
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
    if (!data.stopped) throw new Error(data.message || "Bot is not running");
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
let gaPoolMedia = {};

async function loadBrandPools() {
  const res = await fetch("/api/brand-pools");
  if (!res.ok) throw new Error("Failed to load brand pools");
  gaBrandPools = await res.json();
  await Promise.allSettled(
    (gaBrandPools.pools || []).map((pool) => refreshPoolMedia(pool.id, false))
  );
  renderBrandPools();
}

async function refreshPoolMedia(poolId, rerender = true) {
  try {
    const res = await fetch(`/api/brand-pools/${encodeURIComponent(poolId)}/media`);
    if (!res.ok) return;
    gaPoolMedia[poolId] = await res.json();
  } catch (_) {
    /* transient — keep whatever we had */
  }
  if (rerender) renderBrandPools();
}

function poolMediaSectionHtml(pool) {
  const media = gaPoolMedia[pool.id] || { files: [], member_total: (pool.accounts || []).length };
  const total = media.member_total ?? (pool.accounts || []).length;
  const files = media.files || [];
  const rows = files
    .map((f) => {
      const reach =
        f.member_count >= total
          ? `<span class="pool-video-reach all">on all ${total}</span>`
          : `<span class="pool-video-reach partial" title="Only ${f.member_count} of ${total} accounts have this video">on ${f.member_count}/${total}</span>`;
      return `
        <div class="pool-video-row">
          <span class="pool-video-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
          <span class="pool-video-size">${escapeHtml(f.size_label || "")}</span>
          ${reach}
          <button type="button" class="brand-pool-chip-remove" title="Delete from all accounts in this pool"
            onclick="deletePoolMedia('${escapeHtml(pool.id)}', '${escapeHtml(f.name)}')">×</button>
        </div>`;
    })
    .join("");
  const disabled = total ? "" : "disabled";
  return `
    <div class="brand-pool-media">
      <div class="brand-pool-media-head">
        <span class="brand-pool-media-title">Videos</span>
        <button type="button" class="btn-ghost btn-sm" ${disabled}
          onclick="document.getElementById('pool-video-input-${escapeHtml(pool.id)}').click()"
          title="Upload video(s) to every account in this pool">Upload video</button>
        <input type="file" id="pool-video-input-${escapeHtml(pool.id)}" accept="video/*" multiple
          class="pool-video-input" onchange="uploadPoolMedia('${escapeHtml(pool.id)}', this)">
      </div>
      <div class="pool-video-list">${
        rows || '<p class="brand-pool-empty-members">No shared videos yet. Upload one to send it to all accounts in this pool.</p>'
      }</div>
    </div>`;
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
      const onDevice = deviceLinkedAccountIds();
      const available = (gaBrandPools.unassigned || []).filter((acct) =>
        onDevice.has(acct.account_id)
      );
      const options = available
        .map(
          (acct) => `
          <label class="brand-pool-add-option">
            <input type="checkbox" class="ui-checkbox" value="${escapeHtml(acct.account_id)}">
            <span>@${escapeHtml(acct.username || acct.account_id)}</span>
          </label>`
        )
        .join("");
      const addList = available.length
        ? `<div class="brand-pool-add-list" data-pool-add-list="${escapeHtml(pool.id)}">${options}</div>`
        : `<p class="brand-pool-empty-members">No available accounts to add.</p>`;
      const postingDisabled = pool.posting_enabled === false;
      return `
        <div class="brand-pool-card" data-pool-id="${escapeHtml(pool.id)}">
          <div class="brand-pool-card-head">
            <h3>${escapeHtml(pool.name)}</h3>
            <span class="brand-pool-meta">${pool.interacted_count || 0} people in shared history</span>
          </div>
          <label class="brand-pool-posting-toggle ${postingDisabled ? "is-off" : ""}"
            title="When on, the post-reels job is skipped for every account in this pool (no 'upload reels' errors).">
            <input type="checkbox" class="ui-checkbox" ${postingDisabled ? "checked" : ""}
              onchange="togglePoolPosting('${escapeHtml(pool.id)}', this.checked)">
            <span>Disable reel posting for this pool</span>
          </label>
          <div class="brand-pool-members">${members || '<span class="brand-pool-empty-members">No accounts assigned</span>'}</div>
          ${addList}
          ${
            available.length
              ? `<div class="brand-pool-add-row">
            <button type="button" class="btn-ghost btn-sm" onclick="addToBrandPool('${escapeHtml(pool.id)}')">Add selected</button>
          </div>`
              : ""
          }
          ${poolMediaSectionHtml(pool)}
        </div>`;
    })
    .join("");
}

async function uploadPoolMedia(poolId, input) {
  const files = [...(input?.files || [])];
  if (!files.length) return;
  const pool = (gaBrandPools.pools || []).find((p) => p.id === poolId);
  log(
    `Uploading ${files.length} video(s) to ${pool?.name || poolId} pool…`,
    "info"
  );
  let ok = 0;
  let failed = 0;
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await fetch(`/api/brand-pools/${encodeURIComponent(poolId)}/media`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Upload failed");
      const parts = [];
      if (data.copied?.length) parts.push(`added to ${data.copied.length}`);
      if (data.skipped_existing?.length) parts.push(`${data.skipped_existing.length} already had it`);
      if (data.skipped_locked?.length) parts.push(`${data.skipped_locked.length} locked`);
      if (data.errors?.length) parts.push(`${data.errors.length} failed`);
      log(
        `“${data.filename}” → ${pool?.name || poolId}: ${parts.join(", ") || "no accounts"}`,
        data.errors?.length ? "error" : "success"
      );
      if (gaPoolMedia[poolId]) gaPoolMedia[poolId].files = data.files || gaPoolMedia[poolId].files;
      ok += 1;
    } catch (err) {
      failed += 1;
      log(`“${file.name}”: ${err.message}`, "error");
    }
  }
  if (files.length > 1) {
    log(
      `Pool upload done: ${ok} succeeded, ${failed} failed`,
      failed ? "error" : "success"
    );
  }
  renderBrandPools();
  input.value = "";
}

async function deletePoolMedia(poolId, filename) {
  const pool = (gaBrandPools.pools || []).find((p) => p.id === poolId);
  if (!confirm(`Delete “${filename}” from every account in ${pool?.name || poolId}?`)) return;
  try {
    const res = await fetch(`/api/brand-pools/${encodeURIComponent(poolId)}/media/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Delete failed");
    log(
      `Deleted “${data.filename}” from ${data.deleted?.length || 0} account(s) in ${pool?.name || poolId}`,
      data.errors?.length ? "error" : "success"
    );
    if (gaPoolMedia[poolId]) gaPoolMedia[poolId].files = data.files || [];
    renderBrandPools();
  } catch (err) {
    log(err.message, "error");
  }
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

async function togglePoolPosting(poolId, disable) {
  const pool = (gaBrandPools.pools || []).find((p) => p.id === poolId);
  const enabled = !disable;
  try {
    const res = await fetch(
      `/api/brand-pools/${encodeURIComponent(poolId)}/posting`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to update posting setting");
    }
    if (pool) pool.posting_enabled = enabled;
    renderBrandPools();
    log(
      `Reel posting ${enabled ? "enabled" : "disabled"} for ${pool?.name || poolId} pool`
    );
  } catch (err) {
    log(err.message, "error");
    await loadBrandPools();
  }
}

async function addToBrandPool(poolId) {
  const list = document.querySelector(`[data-pool-add-list="${poolId}"]`);
  if (!list) return;
  const selected = [...list.querySelectorAll('input[type="checkbox"]:checked')].map(
    (el) => el.value
  );
  if (!selected.length) return;
  const pool = (gaBrandPools.pools || []).find((p) => p.id === poolId);
  const current = (pool?.accounts || []).map((a) => a.account_id);
  const toAdd = selected.filter((id) => !current.includes(id));
  if (!toAdd.length) return;
  try {
    await saveBrandPoolAccounts(poolId, [...current, ...toAdd]);
    await loadBrandPools();
    await loadGaAccounts();
    log(
      `Added ${toAdd.map((id) => `@${id}`).join(", ")} to ${pool?.name || poolId} pool`
    );
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
  initFarmRunMode();
  connectWebSocket();
  await loadDeviceFilterMeta();
  await refreshDevices();
  restoreActiveSelection();
  renderDevices();
  // Sync current Farm checkboxes to the server for the 5 AM auto-start.
  persistDeviceSelection();
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
  startAccountStatusPolling();
  if (currentMainTab === "tools" && activeSerial) {
    connectWeditor();
  }
  log("GramAddict dashboard ready");
});

// A tab left open for hours can show stale template/account state (e.g. an
// account still listed under an old template after it was re-applied elsewhere).
// Re-fetch accounts + templates whenever the tab regains focus so the view
// self-heals against the backend, which is always the source of truth.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  loadGaAccounts().catch(() => {});
});

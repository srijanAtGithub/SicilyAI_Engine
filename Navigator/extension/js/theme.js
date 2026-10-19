/**
 * theme.js
 * ---------
 * Side panel half of the Dark Theme quick action. One action at a time:
 *
 *   - Site is already dark            → says so, nothing to do.
 *   - Light site, no Sicily theme     → "Make it dark": measure the page,
 *                                       have the backend design a dark
 *                                       palette, save it, apply it.
 *   - Sicily theme active             → "Remove Sicily theme": delete it
 *                                       for this site (e.g. the site turns
 *                                       out to have its own dark mode).
 *
 * "Manage themed sites" switches the card to a list of every themed
 * site, each removable from there.
 *
 * Themes live in SQLite (backend) and are mirrored into
 * chrome.storage.local under "sicily-theme:<hostname>". theme_engine.js
 * listens to that key, so every open tab on the site updates at once.
 */

import { NotificationService } from "./notifications.js";
import { appWrap } from "./ui.js";
import { BACKEND_HOST } from "./api.js";
import { isScriptableTab } from "./mentions.js";

const THEME_KEY_PREFIX = "sicily-theme:";
const keyFor = (host) => THEME_KEY_PREFIX + host;

const overlay = document.getElementById("theme-overlay");
const hostEl = document.getElementById("theme-host");
const statusEl = document.getElementById("theme-status");
const actionBtn = document.getElementById("theme-action-btn");
const mainScene = document.getElementById("theme-main");
const sitesScene = document.getElementById("theme-sites");
const sitesList = document.getElementById("theme-sites-list");
const sitesBtn = document.getElementById("theme-sites-btn");
const sitesBack = document.getElementById("theme-sites-back");

let state = freshState();

function freshState() {
  return { tab: null, host: null, record: null, detected: null, busy: false, ready: false };
}

// ── Plumbing ─────────────────────────────────────────────────────────

function sendToTab(tabId, message) {
  return chrome.tabs.sendMessage(tabId, message, { frameId: 0 });
}

// Tabs opened before an extension reload have no live engine — inject one.
async function ensureEngine(tabId) {
  try {
    const r = await sendToTab(tabId, { type: "sicily-theme-ping" });
    if (r?.ok) return true;
  } catch (_) { /* no receiver yet */ }
  try {
    await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: ["theme_engine.js"] });
    const r = await sendToTab(tabId, { type: "sicily-theme-ping" });
    return !!r?.ok;
  } catch (err) {
    console.error("Couldn't start the theme engine in this tab:", err);
    return false;
  }
}

// SQLite (via the bridge) is the source of truth; the local mirror is
// only used when the bridge can't be reached.
async function loadRecord(host) {
  try {
    const res = await fetch(`http://${BACKEND_HOST}/theme/${encodeURIComponent(host)}`);
    if (res.status === 404) {
      await chrome.storage.local.remove(keyFor(host));
      return null;
    }
    if (!res.ok) throw new Error(`status ${res.status}`);
    const record = await res.json();
    await chrome.storage.local.set({ [keyFor(host)]: record });
    return record;
  } catch (_) {
    const r = await chrome.storage.local.get(keyFor(host));
    return r[keyFor(host)] || null;
  }
}

// ── Open ─────────────────────────────────────────────────────────────

export async function openThemeOverlay() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let host = "";
  try { host = tab && isScriptableTab(tab) ? new URL(tab.url).hostname : ""; } catch (_) { /* ignore */ }
  if (!host) {
    NotificationService.show("A dark theme can't be applied to this page.");
    return;
  }

  state = freshState();
  state.tab = tab;
  state.host = host;

  showScene("main");
  hostEl.textContent = host;
  statusEl.textContent = "";
  actionBtn.classList.add("hidden");
  overlay.classList.add("active");

  state.record = await loadRecord(host);

  if (await ensureEngine(tab.id)) {
    try {
      const s = await sendToTab(tab.id, { type: "sicily-theme-status" });
      if (s?.ok) state.detected = s.detected_mode;
    } catch (err) {
      console.warn("Theme status failed:", err);
    }
    state.ready = true;
  }

  render();
}

// ── Actions ──────────────────────────────────────────────────────────

overlay?.addEventListener("click", (e) => {
  if (!state.busy && !e.target.closest(".organise-card")) overlay.classList.remove("active");
});

actionBtn?.addEventListener("click", () => {
  if (!state.ready || state.busy) return;
  if (state.record) removeTheme();
  else makeDark();
});

// The overlay stays in front, unchanged, until the work is done, while
// the panel shows the same neural glow as the other quick actions
// (drawn above the overlay via .theme-busy, see overlays.css).
function setBusy(on, label) {
  state.busy = on;
  overlay.classList.toggle("is-busy", on);
  appWrap.classList.toggle("theme-busy", on);
  actionBtn.disabled = on;
  if (label) actionBtn.textContent = label;
}

async function makeDark() {
  setBusy(true, "Making it dark…");
  try {
    const m = await sendToTab(state.tab.id, { type: "sicily-theme-measure" });
    if (!m?.ok || !m.summary?.palette?.length) throw new Error(m?.error || "Nothing to measure");

    const res = await fetch(`http://${BACKEND_HOST}/theme/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(m.summary),
    });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const record = await res.json();

    await chrome.storage.local.set({ [keyFor(record.host)]: record });
    state.record = record;
  } catch (err) {
    console.error("Dark theme failed:", err);
    NotificationService.show("Couldn't make this site dark. Is the backend running?");
  } finally {
    setBusy(false);
    render();
  }
}

// Deletes a site's theme in SQLite and the local mirror; open tabs on
// that site switch back to their own look straight away.
async function deleteTheme(host) {
  const res = await fetch(`http://${BACKEND_HOST}/theme/${encodeURIComponent(host)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`status ${res.status}`);
  await chrome.storage.local.remove(keyFor(host));
  if (host === state.host) state.record = null;
}

async function removeTheme() {
  setBusy(true, "Removing…");
  try {
    await deleteTheme(state.host);
  } catch (err) {
    console.error("Removing theme failed:", err);
    NotificationService.show("Couldn't remove the theme. Is the backend running?");
  } finally {
    setBusy(false);
    render();
  }
}

// ── Themed sites list ──────────────────────────────────────────────────

sitesBtn?.addEventListener("click", () => {
  if (!state.busy) openSitesList();
});

sitesBack?.addEventListener("click", () => {
  showScene("main");
  render();
});

function showScene(name) {
  mainScene.classList.toggle("hidden", name !== "main");
  sitesScene.classList.toggle("hidden", name !== "sites");
}

async function listThemedHosts() {
  try {
    const res = await fetch(`http://${BACKEND_HOST}/themes`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    return (data.themes || []).map((t) => t.host);
  } catch (_) {
    const all = await chrome.storage.local.get(null);
    return Object.keys(all).filter((k) => k.startsWith(THEME_KEY_PREFIX)).map((k) => k.slice(THEME_KEY_PREFIX.length));
  }
}

async function openSitesList() {
  showScene("sites");
  sitesList.innerHTML = `<div class="collections-picker-empty">Loading…</div>`;
  renderSites(await listThemedHosts());
}

function renderSites(hosts) {
  sitesList.innerHTML = "";
  if (!hosts.length) {
    sitesList.innerHTML = `<div class="collections-picker-empty">No sites have a Sicily dark theme yet.</div>`;
    return;
  }

  for (const host of hosts) {
    const row = document.createElement("div");
    row.className = "collection-view-item theme-site-row";

    const name = document.createElement("div");
    name.className = "theme-site-name";
    name.textContent = host;

    const del = document.createElement("div");
    del.className = "item-delete-btn";
    del.title = "Remove dark theme";
    del.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
      <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
    </svg>`;
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await deleteTheme(host);
        row.remove();
        if (!sitesList.querySelector(".theme-site-row")) renderSites([]);
      } catch (err) {
        console.error("Removing theme failed:", err);
        NotificationService.show("Couldn't remove the theme. Is the backend running?");
      }
    });

    row.append(name, del);
    sitesList.appendChild(row);
  }
}

// ── Rendering ────────────────────────────────────────────────────────

function render() {
  statusEl.textContent = "";
  actionBtn.classList.remove("hidden", "is-remove");

  if (state.record) {
    actionBtn.textContent = "Remove Sicily theme";
    actionBtn.classList.add("is-remove");
  } else if (!state.ready) {
    statusEl.textContent = "Reload the tab and try again.";
    actionBtn.classList.add("hidden");
  } else if (state.detected === "dark") {
    statusEl.textContent = "This site is already dark.";
    actionBtn.classList.add("hidden");
  } else {
    actionBtn.textContent = "Make it dark";
  }
}
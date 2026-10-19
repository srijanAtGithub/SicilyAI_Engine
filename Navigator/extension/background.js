const BACKEND_HOST = "localhost:8765";
const EDIT_MENU_ID = "sicily-navigator-edit-selection";

function setupContextMenu() {
  // removeAll then create avoids "duplicate id" errors when the service
  // worker re-registers this during development reloads.
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: EDIT_MENU_ID,
      title: "Use Sicily",
      // Only show when text is actually selected. content_script.js does
      // the real classification when clicked (form field vs. plain
      // contenteditable vs. framework-managed rich text vs. plain page
      // text) — background.js has no DOM access to check any of that
      // itself, so this is just the coarse "is there a selection at all" gate.
      contexts: ["selection"],
    });
  });
}

// Enable the side panel to open on extension icon click
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error(error));

chrome.runtime.onInstalled.addListener(() => {
  console.log("[Sicily Navigator] extension installed.");
  setupContextMenu();
  syncThemesFromBackend();
});

// ── Site Themes: SQLite → chrome.storage.local mirror ─────────────────
// The backend's ThemeStore (SQLite) is the source of truth. theme_engine.js
// reads a mirror of it from chrome.storage.local so a theme can be applied
// at document_start, before the backend could ever answer. We re-sync the
// mirror whenever the worker starts — install, browser start, and every
// DevTools "reload extension" — so nothing is lost across reloads. Only
// keys that actually changed are written, so open tabs aren't needlessly
// re-themed. If the backend is down, the existing mirror is kept as is.
const THEME_KEY_PREFIX = "sicily-theme:";

async function syncThemesFromBackend() {
  try {
    const res = await fetch(`http://${BACKEND_HOST}/themes`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const { themes = [] } = await res.json();

    const current = await chrome.storage.local.get(null);
    const next = {};
    for (const t of themes) next[THEME_KEY_PREFIX + t.host] = t;

    const toRemove = Object.keys(current).filter(
      (k) => k.startsWith(THEME_KEY_PREFIX) && !(k in next)
    );
    const toSet = {};
    for (const [k, v] of Object.entries(next)) {
      if (JSON.stringify(current[k]) !== JSON.stringify(v)) toSet[k] = v;
    }

    if (toRemove.length) await chrome.storage.local.remove(toRemove);
    if (Object.keys(toSet).length) await chrome.storage.local.set(toSet);
  } catch (err) {
    console.log("[Sicily Navigator] theme sync skipped (backend unreachable?)", err?.message || err);
  }
}

chrome.runtime.onStartup.addListener(syncThemesFromBackend);
syncThemesFromBackend(); // also on every worker start (covers DevTools reloads)

// Cross-origin stylesheets can't be read from the page (cssRules throws),
// so theme_engine.js asks us to fetch their text. host_permissions
// <all_urls> lets the worker do this without CORS getting in the way.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "sicily-theme-fetch-css") return false;
  fetch(message.url, { credentials: "include" })
    .then((res) => {
      if (!res.ok) throw new Error(`status ${res.status}`);
      return res.text();
    })
    .then((text) => sendResponse({ ok: true, text }))
    .catch((err) => sendResponse({ ok: false, error: String(err?.message || err) }));
  return true;
});

// ── Tab-Specific Side Panel Management ────────────────────────────────
// REMOVE or comment out the old chrome.sidePanel.setPanelBehavior block entirely.

chrome.action.onClicked.addListener((tab) => {
  if (!tab || !tab.id) return;

  // 1. Configure the side panel path strictly for this tab.
  // We do NOT use 'await' here so we don't break the synchronous execution turn.
  chrome.sidePanel.setOptions({
    tabId: tab.id,
    path: "side_panel.html",
    enabled: true
  });

  // 2. Immediately invoke open() in the exact same code block.
  // This preserves the active user gesture token!
  chrome.sidePanel.open({ tabId: tab.id }).catch((error) => {
    console.error("Failed to open tab-specific side panel:", error);
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === EDIT_MENU_ID && tab && tab.id != null) {
    chrome.tabs.sendMessage(tab.id, { type: "navigator-open-edit-box" });
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type !== "navigator-edit-selection-request") return false;

  fetch(`http://${BACKEND_HOST}/edit-selection`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      selected_text: message.selected_text,
      instruction: message.instruction,
      action_type: message.action_type || "edit",
      surrounding_context: message.surrounding_context || ""
    }),
  })
    .then((res) => {
      if (!res.ok) throw new Error(`status ${res.status}`);
      return res.json();
    })
    .then((data) => {
      sendResponse({ ok: true, edited_text: data.edited_text });
    })
    .catch((err) => {
      console.log("[Sicily Navigator] edit-selection request failed", err);
      sendResponse({ ok: false, error: "Backend not reachable — please start using sicily navigator --start?" });
    });

  return true; // Important: keeps the channel open for async response
});

// NOTE: we deliberately do NOT delete the backend ChatStore session
// when a tab closes. Sessions are looked up by URL (see
// api.js:resolveSessionKey), scoped to the current browser session —
// not by Chrome's tabId, which is reassigned on every browsing session
// anyway. Closing a tab isn't the user asking to delete anything: the
// URL's mapping in chrome.storage.session is untouched by a tab
// closing, so reopening the same page (same tab or a new one,
// Ctrl+Shift+T or otherwise) resumes the same conversation for as long
// as the browser stays open. Deleting ChatStore rows only ever happens
// via the panel's explicit per-conversation delete in the Chats list,
// or Collections/Reading List's own delete actions.

// However, for Temporary/Incognito chats, the memory IS strictly bound 
// to the tab. We catch the tab closing here to wipe the backend memory.
chrome.tabs.onRemoved.addListener((tabId) => {
  fetch(`http://${BACKEND_HOST}/temp_session/temp_${tabId}`, { method: "DELETE" })
    .catch(() => { }); // silently ignore if backend is unreachable
});
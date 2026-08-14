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
      sendResponse({ ok: false, error: "Backend not reachable — is navigator_bridge.py running?" });
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
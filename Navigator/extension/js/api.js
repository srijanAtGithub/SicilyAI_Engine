import { setStatus, showOnline, showOffline, addMessage, setSending } from "./ui.js";

export const BACKEND_HOST = "localhost:8765";
export let socket = null;

export async function getActiveTabInfo() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return { id: null, url: "(no active tab)", title: "(no active tab)" };
  return { id: tab.id, url: tab.url || "", title: tab.title || "" };
}

// ── Stable session identity ──────────────────────────────────────────
// Chrome's tabId is NOT a persistent identity — it's an in-memory integer
// that Chrome reassigns per browsing session. Closing a tab and reopening
// it with Ctrl+Shift+T restores "the same tab" from the user's point of
// view, but Chrome hands it a brand new tabId, so keying sessions by
// tabId makes that history unreachable even though nothing was ever
// meant to be cleared.
//
// The URL is the only thing that's actually stable across that close/
// reopen round-trip, so session identity (history load/clear, and the
// websocket) is keyed off a hash of the URL instead. This also matches
// the already-accepted behavior that navigating to a genuinely different
// URL starts a fresh conversation (same as e.g. Gemini's side panel) —
// it's the same key, just no longer thrown away by an incidental tab
// close.
//
// Not cryptographic — just enough spread to use as a URL-safe path
// segment key. Strips the fragment (#...) so in-page anchor jumps on the
// same document don't fragment the session.
export function getSessionKey(url) {
  if (!url) return "no-url";
  const withoutFragment = url.split("#")[0];
  let hash = 0;
  for (let i = 0; i < withoutFragment.length; i++) {
    hash = (Math.imul(31, hash) + withoutFragment.charCodeAt(i)) | 0;
  }
  // Base36, unsigned — keeps it short and safe to drop straight into a
  // REST path / ws URL segment.
  return (hash >>> 0).toString(36);
}

export async function loadHistory(sessionKey) {
  try {
    const res = await fetch(`http://${BACKEND_HOST}/session/${sessionKey}`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    return data.messages || [];
  } catch (err) {
    addMessage("Couldn't load this tab's history (backend not running?).", "system");
    return [];
  }
}

export async function clearHistoryOnBackend(sessionKey) {
  try {
    await fetch(`http://${BACKEND_HOST}/session/${sessionKey}`, { method: "DELETE" });
    return true;
  } catch (err) {
    addMessage("Couldn't clear history — is navigator_bridge.py running?", "system");
    return false;
  }
}

// Same request as clearHistoryOnBackend — the backend only has one way
// to wipe a session's rows out of ChatStore. Kept as a distinct export
// so call sites read according to intent (permanently deleting a
// conversation from the Chats list vs. clearing the active one) even
// though today they hit the same DELETE route.
export async function deleteSessionOnBackend(sessionKey) {
  try {
    const res = await fetch(`http://${BACKEND_HOST}/session/${sessionKey}`, { method: "DELETE" });
    return res.ok;
  } catch (err) {
    console.error("Couldn't delete session:", err);
    return false;
  }
}

// ── Per-tab "which session is this tab currently on" override ────────
// Session identity is normally a deterministic hash of the page URL
// (getSessionKey), so the same page always resumes the same history.
// "New Chat" breaks that determinism on purpose — it mints a fresh key
// so old history stays untouched and browsable from the Chats panel.
// That override needs to be remembered somewhere keyed by the browser
// tab itself (chrome.tabs id), NOT the URL, so that reopening the panel
// on *that tab* picks up the new empty conversation, while a plain
// hash(url) lookup (e.g. a different tab on the same URL, or this tab
// after truly closing/reopening it) still falls back to the original,
// deterministic per-page history.
//
// chrome.storage.session is used (not local) since this is scoped to
// the current browser session by design — it's a "where was this tab
// left" breadcrumb, not data worth persisting past a browser restart.
function tabOverrideKey(tabId) {
  return `sicily-session-override-${tabId}`;
}

export async function getSessionKeyForTab(tabId, url) {
  const fallback = getSessionKey(url);
  if (tabId == null) return fallback;
  try {
    const stored = await chrome.storage.session.get(tabOverrideKey(tabId));
    return stored[tabOverrideKey(tabId)] || fallback;
  } catch (err) {
    return fallback;
  }
}

export async function setSessionKeyForTab(tabId, sessionKey) {
  if (tabId == null) return;
  try {
    await chrome.storage.session.set({ [tabOverrideKey(tabId)]: sessionKey });
  } catch (err) {
    console.error("Couldn't persist session override for tab:", err);
  }
}

// A "New Chat" key: still URL-derived (so it's recognizable/stable in
// shape) but suffixed with randomness so it's guaranteed distinct from
// the page's default hash(url) key and from any prior New Chat key for
// this same page.
export function makeFreshSessionKey(url) {
  const base = getSessionKey(url);
  const suffix = Math.random().toString(36).slice(2, 8);
  return `${base}-${suffix}`;
}

export function closeSocket() {
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    socket.close();
  }
  socket = null;
}

export function connectSocket(sessionKey) {
  setStatus("connecting");
  socket = new WebSocket(`ws://${BACKEND_HOST}/ws/${sessionKey}`);

  socket.onopen = () => {
    setStatus("connected");
  };

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      addMessage(data.reply ?? "(empty response)", "ai");
    } catch (err) {
      addMessage("Couldn't parse server response.", "system");
    }
    setSending(false);
  };

  socket.onclose = () => {
    setStatus("disconnected");
    setSending(false);
  };

  socket.onerror = () => {
    setStatus("disconnected");
  };
}

export async function fetchAllSessions() {
  try {
    const res = await fetch(`http://${BACKEND_HOST}/sessions`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    return data.sessions || [];
  } catch (err) {
    console.error("Couldn't fetch sessions list:", err);
    return [];
  }
}
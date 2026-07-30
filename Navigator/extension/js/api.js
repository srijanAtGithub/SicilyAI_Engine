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

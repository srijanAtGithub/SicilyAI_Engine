import { setStatus, showOnline, showOffline, addMessage, setSending } from "./ui.js";

export const BACKEND_HOST = "localhost:8765";
export let socket = null;

export async function getActiveTabInfo() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return { id: null, url: "(no active tab)", title: "(no active tab)" };
  return { id: tab.id, url: tab.url || "", title: tab.title || "" };
}

// ── Session identity: per-URL, scoped to the browser session ─────────
//
// A conversation is looked up by URL, same as before — but the mapping
// itself now lives in chrome.storage.session instead of being a pure
// hash, which gives us two things a pure hash couldn't:
//
//   1. It expires when the browser restarts (Chrome wipes
//      chrome.storage.session automatically), so a page you chatted
//      with last week doesn't silently resume — you get a fresh
//      conversation, with the old one still reachable via Chats.
//   2. A tab's conversation can follow it across navigation: when a tab
//      moves from page1 to page2, page2 is pointed at page1's existing
//      session key instead of getting a brand new one. A tab is a
//      workflow, not a single page, so the conversation should carry
//      forward with it.
//
// Concretely, this means:
//   - Reload the page                     → same chat
//   - Close the tab, Ctrl+Shift+T it back  → same chat (URL's mapping
//                                            is untouched by closing)
//   - Two tabs open on the same URL        → SAME chat (shared, like
//                                            the original design)
//   - Navigate to a different URL,
//     same tab                             → SAME chat continues;
//                                            the new URL is pointed at
//                                            the tab's existing key
//   - Restart the browser entirely         → NEW chat (storage wiped)
//
// Note the resulting quirk, which is a direct consequence of "URL is
// just a lookup key, not an owner": if tab A talks about page1, then
// navigates to page2 (conversation follows), and tab B *later* opens
// page1 fresh, tab B resumes tab A's conversation too — now including
// the page2 portion. The mapping isn't torn down when a tab leaves a
// URL, so any tab that lands on that URL later (even after the
// original tab closed) picks up wherever that key left off, until the
// browser restarts.

function urlStorageKey(url) {
  const withoutFragment = (url || "").split("#")[0];
  return `sicily-url-session:${withoutFragment}`;
}

// Not cryptographic — just a short, readable seed for a freshly minted
// key. No longer the identity itself (the stored mapping is).
function hashUrl(url) {
  if (!url) return "no-url";
  const withoutFragment = url.split("#")[0];
  let hash = 0;
  for (let i = 0; i < withoutFragment.length; i++) {
    hash = (Math.imul(31, hash) + withoutFragment.charCodeAt(i)) | 0;
  }
  return (hash >>> 0).toString(36);
}

function mintSessionKey(url) {
  const seed = hashUrl(url);
  const suffix = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  return `${seed}-${suffix}`;
}

async function getUrlMapping(url) {
  const key = urlStorageKey(url);
  try {
    const stored = await chrome.storage.session.get(key);
    return stored[key] || null;
  } catch (err) {
    console.error("Couldn't read session mapping for URL:", err);
    return null;
  }
}

async function setUrlMapping(url, sessionKey) {
  try {
    await chrome.storage.session.set({ [urlStorageKey(url)]: sessionKey });
  } catch (err) {
    console.error("Couldn't persist session mapping for URL:", err);
  }
}

// Resolves the session key for a URL: reuse it if this browser session
// already has a mapping for it, otherwise mint and store a fresh one.
export async function resolveSessionKey(url) {
  const existing = await getUrlMapping(url);
  if (existing) return existing;

  const fresh = mintSessionKey(url);
  await setUrlMapping(url, fresh);
  return fresh;
}

// Called on same-tab navigation: points the new URL at the tab's
// current (pre-navigation) session key, so the conversation continues
// instead of restarting. If the new URL already has its own mapping
// from some earlier, unrelated visit, that mapping is intentionally
// overwritten — the active, continuing conversation takes precedence
// over a stale one that happened to touch this URL before.
export async function carrySessionToUrl(sessionKey, newUrl) {
  await setUrlMapping(newUrl, sessionKey);
}

// Forces a brand-new session and points this URL at it — the "New
// Chat" primitive. Only this URL's mapping is touched; any other URL
// that was previously pointing at the old session key (e.g. via
// carrySessionToUrl) is left alone and keeps resolving to the old one.
export async function startNewSessionForUrl(url) {
  const fresh = mintSessionKey(url);
  await setUrlMapping(url, fresh);
  return fresh;
}

// Points this URL's mapping at an existing session key — used when the
// user manually switches conversations from the Chats panel, so this
// page resumes that conversation from now on (until browser restart).
export async function pinSessionKeyToUrl(url, sessionKey) {
  await setUrlMapping(url, sessionKey);
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

// Deletes a conversation's rows from ChatStore permanently. Used by the
// Chats panel's per-row delete, and nowhere else — "New Chat" no longer
// clears anything server-side, it just stops using the old session key.
export async function deleteSessionOnBackend(sessionKey) {
  try {
    const res = await fetch(`http://${BACKEND_HOST}/session/${sessionKey}`, { method: "DELETE" });
    return res.ok;
  } catch (err) {
    console.error("Couldn't delete session:", err);
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
      
      // Dispatch a custom event so main.js knows a turn finished
      window.dispatchEvent(new Event("chat-turn-complete"));
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
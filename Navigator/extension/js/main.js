import { NotificationService } from "./notifications.js";
import { addMessage, clearMessagesUI, addContextTrail, setSending, sendBtn, appWrap, showEmptyState } from "./ui.js";
import { socket, getActiveTabInfo, loadHistory, deleteSessionOnBackend, connectSocket, closeSocket, resolveSessionKey, carrySessionToUrl, startNewSessionForUrl, pinSessionKeyToUrl, fetchAllSessions, BACKEND_HOST } from "./api.js";
import { attachedContexts, clearAttachedContexts } from "./features.js";
import {
  getMentionedTabSnippets, hasMentionedTab, clearMentionedTab, isMentionDropdownOpen,
  hasMentionedCollection, getMentionedCollectionIds, clearMentionedCollection,
  autoMentionActiveTab, removeMentionedTab
} from "./mentions.js";

const inputEl = document.getElementById("input-box");
const clearBtn = document.getElementById("new-chat-btn");
let currentTab = { id: null, url: "", title: "" };
let currentSessionKey = null;

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text) return;

  if (!socket || socket.readyState !== WebSocket.OPEN) {
    addMessage("Not connected to backend yet.", "system");
    return;
  }

  const payloadSnippets = [...attachedContexts, ...getMentionedTabSnippets()];

  // Fetch each mentioned collection's full text (AI needs this full
  // content). Each collection becomes its own labeled block, same as
  // each tab does, so the model can tell sources apart when the user
  // asks something like "where is XYZ mentioned".
  if (hasMentionedCollection()) {
    const ids = getMentionedCollectionIds();
    const results = await Promise.allSettled(
      ids.map(id => fetch(`http://${BACKEND_HOST}/collections/${id}`))
    );
    for (const result of results) {
      if (result.status !== "fulfilled" || !result.value.ok) {
        console.error("Failed to load collection text:", result.reason || result.value?.status);
        continue;
      }
      try {
        const data = await result.value.json();
        if (data.snippets && data.snippets.length > 0) {
          const coll_text = data.snippets.map(s => `- ${s.text}`).join("\n\n");
          payloadSnippets.push(`Collection: ${data.name}\n\n${coll_text}`);
        } else {
          payloadSnippets.push(`Collection: ${data.name}\n\n(Empty)`);
        }
      } catch (err) {
        console.error("Failed to parse collection response:", err);
      }
    }
  }

  // --- NEW: Create a clean display array for the UI ---
  const displaySnippets = payloadSnippets.map(snippet => {
    // If it starts with our prefixes, take only the header line (before the \n\n)
    if (snippet.startsWith('Tab: ') || snippet.startsWith('Collection: ')) {
      return snippet.split('\n\n')[0];
    }
    // For manual drag/drop, just show the first 30 chars
    return snippet.length > 30 ? snippet.substring(0, 30) + "..." : snippet;
  });

  // Render the UI with only the short labels
  addContextTrail(displaySnippets);

  addMessage(text, "user");
  inputEl.value = "";

  inputEl.style.height = 'auto';
  inputEl.style.overflowY = 'hidden';

  setSending(true);

  // Send the FULL content to the backend
  const fresh = await getActiveTabInfo();
  const payload = {
    text: text,
    page_url: fresh.url,
    page_title: fresh.title,
    context_snippets: payloadSnippets
  };

  socket.send(JSON.stringify(payload));
  clearAttachedContexts();
  if (hasMentionedTab()) clearMentionedTab();
  if (hasMentionedCollection()) clearMentionedCollection();
}

// "New Chat" starts a distinct backend session rather than wiping the
// current one: it mints a fresh session key and points this URL at it
// (so this page resumes the new, empty conversation from now on,
// instead of the old one), clears the visible UI, and reconnects the
// socket. The old conversation is untouched and stays reachable from
// the Chats panel — including from any other URL that had been carried
// along with it via navigation.
async function handleClear() {
  closeSocket();
  clearMessagesUI();
  showEmptyState();

  currentSessionKey = await startNewSessionForUrl(currentTab.url);

  autoMentionActiveTab(currentTab);

  connectSocket(currentSessionKey);
  inputEl.focus();

  NotificationService.show("New conversation started.");
}

// ── Chats Panel ───────────────────────────────────────────────────────
const chatsBtn = document.getElementById("chats-btn");
const chatsOverlay = document.getElementById("chats-overlay");
const chatsPanelList = document.getElementById("chats-panel-list");
const chatsPanelEmpty = document.getElementById("chats-panel-empty");
const chatsPanelClose = document.getElementById("chats-panel-close");

function openChatsPanel() {
  chatsOverlay.classList.add("active");
  populateChatsPanel();
}

function closeChatsPanel() {
  chatsOverlay.classList.remove("active");
}

function formatTimeAgo(isoStr) {
  if (!isoStr) return "";
  const then = new Date(isoStr);
  const now = new Date();
  const diffMs = now - then;
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d ago`;
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

async function populateChatsPanel() {
  chatsPanelList.innerHTML = "";
  chatsPanelEmpty.classList.remove("visible");

  const sessions = await fetchAllSessions();

  if (sessions.length === 0) {
    chatsPanelEmpty.classList.add("visible");
    return;
  }

  sessions.forEach((s, i) => {
    const item = document.createElement("div");
    item.className = "chats-item";
    if (s.session_key === currentSessionKey) {
      item.classList.add("current");
    }
    item.style.animationDelay = `${i * 0.04}s`;

    const preview = document.createElement("div");
    preview.className = "chats-item-preview";
    preview.textContent = s.preview || "(empty conversation)";

    const meta = document.createElement("div");
    meta.className = "chats-item-meta";

    const timeSpan = document.createElement("span");
    timeSpan.textContent = formatTimeAgo(s.last_active);

    meta.appendChild(timeSpan);

    item.appendChild(preview);
    item.appendChild(meta);

    // Hover-reveal delete icon — deletes this conversation from the
    // backend (SQLite-backed ChatStore) permanently, not just from this
    // list. Mirrors the .item-delete-btn pattern already used for
    // Collections/Reading List rows elsewhere in this UI.
    const deleteBtn = document.createElement("button");
    deleteBtn.className = "item-delete-btn chats-item-delete-btn";
    deleteBtn.title = "Delete conversation";
    deleteBtn.innerHTML = `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" width="14" height="14">
      <path d="M4 6h12M8 6V4.5A1.5 1.5 0 0 1 9.5 3h1A1.5 1.5 0 0 1 12 4.5V6M6 6v9a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;

    deleteBtn.addEventListener("click", async (e) => {
      // Stop this from bubbling up to the row's own click handler
      // (which would otherwise switch to the session we're deleting).
      e.stopPropagation();
      deleteBtn.disabled = true;

      // If deleting the active session, close our socket BEFORE the
      // DELETE request hits the server. The server drops the WS
      // connection on delete, so if we don't close first, socket.onclose
      // fires mid-await and triggers showOffline() / the disconnect screen.
      const isDeletingActive = s.session_key === currentSessionKey;
      if (isDeletingActive) {
        closeSocket();
        clearMessagesUI();
        showEmptyState();
      }

      const ok = await deleteSessionOnBackend(s.session_key);
      if (!ok) {
        deleteBtn.disabled = false;
        // If we pre-emptively closed, reconnect to the old session.
        if (isDeletingActive) {
          connectSocket(currentSessionKey);
        }
        return;
      }

      if (isDeletingActive) {
        // Mint a fresh session so the user lands on a clean slate,
        // fully connected — not an offline/disconnected state.
        currentSessionKey = await startNewSessionForUrl(currentTab.url);
        autoMentionActiveTab(currentTab);
        connectSocket(currentSessionKey);
      }

      item.remove();
      NotificationService.show("Conversation deleted.");

      // Refresh so the empty-state message shows if that was the last one.
      if (chatsPanelList.children.length === 0) {
        chatsPanelEmpty.classList.add("visible");
      }
    });

    item.appendChild(deleteBtn);

    item.addEventListener("click", () => {
      switchToSession(s.session_key);
    });

    chatsPanelList.appendChild(item);
  });
}

async function switchToSession(sessionKey) {
  closeChatsPanel();

  if (sessionKey === currentSessionKey) return;

  // Tear down old session
  closeSocket();
  clearMessagesUI();

  // Set new session
  currentSessionKey = sessionKey;

  // Remember that this URL now resolves to this session, so it (and any
  // tab that visits it) resumes here from now on, until browser restart.
  await pinSessionKeyToUrl(currentTab.url, sessionKey);

  // Load history for the new session
  const history = await loadHistory(sessionKey);

  if (history.length === 0) {
    showEmptyState();
  }

  for (const m of history) {
    if (m.role === "user" && Array.isArray(m.context_snippets) && m.context_snippets.length) {
      addContextTrail(m.context_snippets);
    }
    addMessage(m.text, m.role === "user" ? "user" : "ai");
  }

  // Reconnect socket to the new session
  connectSocket(sessionKey);
  inputEl.focus();

  NotificationService.show("Switched conversation.");
}

chatsBtn.addEventListener("click", openChatsPanel);
chatsPanelClose.addEventListener("click", closeChatsPanel);

// Close on clicking the backdrop (not the panel itself)
chatsOverlay.addEventListener("click", (e) => {
  if (e.target === chatsOverlay) closeChatsPanel();
});

sendBtn.addEventListener("click", sendMessage);
clearBtn.addEventListener("click", handleClear);

// Empty-state suggestion chips: these are generic conversation starters
// (deliberately distinct from the Quick Actions menu items), so just
// drop their text into the input and send like a normal typed message.
document.querySelectorAll(".es-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const prompt = chip.dataset.prompt;
    if (!prompt) return;
    inputEl.value = prompt;
    sendMessage();
  });
});

// Auto-resize the textarea as the user types
inputEl.addEventListener("input", function () {
  this.style.height = 'auto';

  // Add 2px to account for the 1px top and 1px bottom borders
  this.style.height = (this.scrollHeight + 2) + 'px';

  // Only show the scrollbar if it hits your max-height (150px)
  if (this.scrollHeight >= 150) {
    this.style.overflowY = 'auto';
  } else {
    this.style.overflowY = 'hidden';
  }
});

// Handle standard chat Enter vs Shift+Enter behavior
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !isMentionDropdownOpen()) {
    if (!e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }
});

// Loads whatever conversation this URL currently resolves to — used on
// panel open, and when manually switching tabs/sessions. Does NOT carry
// any prior conversation forward; it's a fresh lookup.
async function loadSessionForUrl(url) {
  currentSessionKey = await resolveSessionKey(url);

  closeSocket();
  clearMessagesUI();

  const history = await loadHistory(currentSessionKey);

  if (history.length === 0) {
    // Fresh conversation for this URL — default to "I want to ask about
    // this page" instead of making the user @-mention it themselves.
    // Skipped when history exists so reopening the panel on an ongoing
    // conversation doesn't silently re-attach the full page text as a
    // brand new mention on top of it.
    showEmptyState();
    autoMentionActiveTab(currentTab);
  }

  for (const m of history) {
    if (m.role === "user" && Array.isArray(m.context_snippets) && m.context_snippets.length) {
      addContextTrail(m.context_snippets);
    }
    addMessage(m.text, m.role === "user" ? "user" : "ai");
  }

  connectSocket(currentSessionKey);
}

// Same-tab navigation is treated as a continuation of one workflow, not
// a new one: the new URL is pointed at the tab's existing session key
// (carrySessionToUrl) rather than looking up whatever that URL already
// resolves to. The visible conversation and socket are untouched —
// nothing to reload, since it's the same session either way.
//
// The user just followed a link — the natural next question is almost
// always about the page they landed on, so auto-mention it the same way
// the very first page in a tab gets auto-mentioned on panel open. The
// old page's mention (if any) is dropped first: it's still keyed to
// this same tabId, so autoMentionActiveTab's "already mentioned"
// dedupe would otherwise skip attaching the new page entirely.
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (tabId !== currentTab.id) return;
  // Chrome fires onUpdated repeatedly through a navigation (url change,
  // then loading, then complete). Wait for "complete" so the new page
  // actually exists in the DOM before extracting its text — otherwise
  // this races a blank/loading document.
  if (changeInfo.status !== "complete") return;
  const newUrl = tab.url || "";
  if (!newUrl || newUrl === currentTab.url) return;

  await carrySessionToUrl(currentSessionKey, newUrl);
  currentTab = { id: tab.id, url: newUrl, title: tab.title || currentTab.title };

  removeMentionedTab(tabId);
  autoMentionActiveTab(currentTab);
});

(async () => {
  requestAnimationFrame(() => {
    if (appWrap) appWrap.classList.add("ready");
  });

  currentTab = await getActiveTabInfo();
  if (currentTab.id == null) {
    addMessage("Couldn't identify the active tab.", "system");
    return;
  }

  // Resolves to whatever conversation this URL currently maps to in this
  // browser session (shared across any tab that's visited it, including
  // one carried forward from navigation) — or mints a fresh one if
  // there's no live mapping (first visit this browser session, or the
  // browser has restarted since). See api.js for the full rules.
  await loadSessionForUrl(currentTab.url);
  inputEl.focus();
})();
import { NotificationService } from "./notifications.js";
import { addMessage, clearMessagesUI, addContextTrail, setSending, sendBtn, appWrap, showEmptyState } from "./ui.js";
import { socket, getActiveTabInfo, loadHistory, clearHistoryOnBackend, connectSocket, closeSocket, getSessionKey, fetchAllSessions, BACKEND_HOST } from "./api.js";
import { attachedContexts, clearAttachedContexts } from "./features.js";
import {
  getMentionedTabSnippets, hasMentionedTab, clearMentionedTab, isMentionDropdownOpen,
  hasMentionedCollection, getMentionedCollectionIds, clearMentionedCollection,
  autoMentionActiveTab
} from "./mentions.js";

const inputEl = document.getElementById("input-box");
const clearBtn = document.getElementById("clear-btn");
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

async function handleClear() {
  if (currentSessionKey == null) return;
  const ok = await clearHistoryOnBackend(currentSessionKey);
  if (ok) {
    clearMessagesUI();
    NotificationService.show("Conversation cleared.");
  }
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

    const dot = document.createElement("span");
    dot.className = "chats-item-meta-dot";

    const countSpan = document.createElement("span");
    const msgCount = Math.floor(s.message_count / 2);
    countSpan.textContent = `${msgCount} ${msgCount === 1 ? "turn" : "turns"}`;

    meta.appendChild(timeSpan);
    meta.appendChild(dot);
    meta.appendChild(countSpan);

    item.appendChild(preview);
    item.appendChild(meta);

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

(async () => {
  requestAnimationFrame(() => {
    if (appWrap) appWrap.classList.add("ready");
  });

  currentTab = await getActiveTabInfo();
  if (currentTab.id == null) {
    addMessage("Couldn't identify the active tab.", "system");
    return;
  }
  currentSessionKey = getSessionKey(currentTab.url);

  const history = await loadHistory(currentSessionKey);

  if (history.length === 0) {
    // Fresh conversation for this page — default to "I want to ask about
    // this page" instead of making the user @-mention it themselves.
    // Skipped when history exists (session persists across tab close/
    // reopen and backend restarts now) so reopening the panel on an
    // ongoing conversation doesn't silently re-attach the full page text
    // as a brand new mention on top of it.
    autoMentionActiveTab(currentTab);
  }

  for (const m of history) {
    if (m.role === "user" && Array.isArray(m.context_snippets) && m.context_snippets.length) {
      addContextTrail(m.context_snippets);
    }
    addMessage(m.text, m.role === "user" ? "user" : "ai");
  }

  connectSocket(currentSessionKey);
  inputEl.focus();
})();

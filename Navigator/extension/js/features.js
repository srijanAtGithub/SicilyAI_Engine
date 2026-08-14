import { NotificationService } from "./notifications.js";
import { appWrap } from "./ui.js";
import { BACKEND_HOST } from "./api.js";
import { isScriptableTab } from "./mentions.js";

const quickActionsWrap = document.getElementById("quick-actions");
const quickActionsBtn = document.getElementById("quick-actions-btn");
const quickActionsMenu = document.getElementById("quick-actions-menu");
const qaItems = document.querySelectorAll(".qa-item");

const dragOverlay = document.getElementById("drag-overlay");
const zoneContext = document.getElementById("zone-context");
const zoneCollections = document.getElementById("zone-collections");
const contextShelf = document.getElementById("context-shelf");

const organiseTabsOverlay = document.getElementById("organise-tabs-overlay");
const btnIncludeGrouped = document.getElementById("btn-include-grouped");
const btnOnlyUngrouped = document.getElementById("btn-only-ungrouped");

const summaryOverlay = document.getElementById("summary-overlay");
const summaryContent = document.getElementById("summary-content");
const btnOkSummary = document.getElementById("btn-ok-summary");
const btnAddChatSummary = document.getElementById("btn-add-chat-summary");

const findMoreOverlay = document.getElementById("find-more-overlay");
const findMoreList = document.getElementById("find-more-list");
const btnFindMore = document.getElementById("btn-find-more");

const collectionsPickerOverlay = document.getElementById("collections-picker-overlay");
const collectionsPickerPreview = document.getElementById("collections-picker-preview");
const collectionsPickerInput = document.getElementById("collections-picker-input");
const collectionsPickerList = document.getElementById("collections-picker-list");
const collectionsCreateBtn = document.getElementById("collections-create-btn");
const collectionsCreateBtnLabel = document.getElementById("collections-create-btn-label");

const readingListPickerOverlay = document.getElementById("reading-list-picker-overlay");
const readingListPickerPreviewTitle = document.getElementById("reading-list-picker-preview-title");
const readingListPickerInput = document.getElementById("reading-list-picker-input");
const readingListPickerList = document.getElementById("reading-list-picker-list");
const readingListCreateBtn = document.getElementById("reading-list-create-btn");
const readingListCreateBtnLabel = document.getElementById("reading-list-create-btn-label");

// Wire up the OK button to just close the overlay
btnOkSummary?.addEventListener("click", () => {
  summaryOverlay.classList.remove("active");
});

// Wire up the "Add to Chat" button
btnAddChatSummary?.addEventListener("click", () => {
  const summaryText = summaryContent.textContent;

  if (summaryText && summaryText.trim() !== "") {
    // 1. Push it into the existing context array
    attachedContexts.push(summaryText.trim());

    // 2. Re-render the visual UI shelf at the bottom dock
    renderContextShelf();

    // 3. Notify the user it was successful
    NotificationService.show("Summary added to context.");
  }

  // Close the window
  summaryOverlay.classList.remove("active");
});

// "Find More" fetches another batch and appends it below the current
// results — it deliberately does NOT close the overlay, so the existing
// results stay visible the whole time. Dismissing the overlay is still
// just a click on the dimmed background (handler below), same as every
// other overlay in this UI.
btnFindMore?.addEventListener("click", () => {
  startFindMoreLikeThis({ append: true });
});

// Clicking the dimmed background (not the card) closes it too, same as
// the collections view overlay's behaviour.
findMoreOverlay?.addEventListener("click", (e) => {
  if (!e.target.closest(".organise-card")) {
    findMoreOverlay.classList.remove("active");
  }
});

// ── Saved Collections Viewer ──────────────────────────────────────────
const collectionsViewOverlay = document.getElementById("collections-view-overlay");
const collectionsViewList = document.getElementById("collections-view-list");
const collectionsViewTitle = document.getElementById("collections-view-title");
const collectionsViewBackBtn = document.getElementById("collections-view-back-btn");
const collectionsViewActions = document.getElementById("collections-view-actions");

// Clicking the background closes the overlay completely
collectionsViewOverlay?.addEventListener("click", (e) => {
  if (!e.target.closest(".organise-card")) {
    collectionsViewOverlay.classList.remove("active");
  }
});

// The "Back" button returns to the main collections list (Scene A)
collectionsViewBackBtn?.addEventListener("click", openSavedCollectionsView);

// ── Reading List Groups Viewer ────────────────────────────────────────
const readingListGroupsViewOverlay = document.getElementById("reading-list-groups-view-overlay");
const readingListGroupsViewList = document.getElementById("reading-list-groups-view-list");
const readingListGroupsViewTitle = document.getElementById("reading-list-groups-view-title");
const readingListGroupsViewBackBtn = document.getElementById("reading-list-groups-view-back-btn");
const readingListGroupsViewActions = document.getElementById("reading-list-groups-view-actions");

readingListGroupsViewOverlay?.addEventListener("click", (e) => {
  if (!e.target.closest(".organise-card")) {
    readingListGroupsViewOverlay.classList.remove("active");
  }
});

readingListGroupsViewBackBtn?.addEventListener("click", openReadingListGroupsView);

export let attachedContexts = [];
let expandedContextIndices = new Set();
let qaCloseTimer = null;

function openQuickActions() {
  clearTimeout(qaCloseTimer);
  quickActionsWrap.classList.add("open");
}

function closeQuickActions(delay = 0) {
  clearTimeout(qaCloseTimer);
  qaCloseTimer = setTimeout(() => {
    quickActionsWrap.classList.remove("open");
  }, delay);
}

quickActionsWrap.addEventListener("mouseenter", openQuickActions);
quickActionsMenu.addEventListener("mouseenter", openQuickActions);
quickActionsWrap.addEventListener("mouseleave", () => closeQuickActions(220));

quickActionsBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (quickActionsWrap.classList.contains("open")) {
    closeQuickActions();
  } else {
    openQuickActions();
  }
});

document.addEventListener("click", (e) => {
  if (!quickActionsWrap.contains(e.target)) {
    closeQuickActions();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeQuickActions();
});

function handleQuickAction(action) {
  closeQuickActions();
  if (action === "organise-tabs") {
    showOrganiseTabsOverlay();
    return;
  }
  if (action === "summarise-page") {
    startSummarisePage();
    return;
  }
  if (action === "saved-collections") {
    openSavedCollectionsView();
    return;
  }
  if (action === "find-more-like-this") {
    startFindMoreLikeThis();
    return;
  }
  if (action === "reading-list-groups") {
    openReadingListGroupsView();
    return;
  }

  const labels = {
    "summarise-page": "Summarise Page",
    "find-more-like-this": "Find More Like This",
    "reading-list-groups": "Reading List Groups",
    "saved-collections": "Saved Collections",
  };
  const actionName = labels[action] || action;
  NotificationService.show(`"${actionName}" is coming soon.`);
}

qaItems.forEach((item) => {
  // The font-size row and capability row aren't real "actions" — they host
  // interactive controls with their own click handlers and must never fall
  // through to handleQuickAction (that always closes the menu).
  if (item.classList.contains("qa-font-size")) return;
  if (item.classList.contains("qa-capability-row")) return;
  item.addEventListener("click", () => handleQuickAction(item.dataset.action));
});

// ── Chat Font Size Controller ───────────────────────────────────────
// Scoped to chat bubbles ONLY: it just nudges the --chat-font-size CSS
// variable on #messages, which .msg's font-size (in chat.css) reads
// from. Nothing else in the UI references that variable.
const CHAT_FONT_MIN = 11;
const CHAT_FONT_MAX = 20;
const CHAT_FONT_DEFAULT = 14;
const CHAT_FONT_STEP = 1;
const CHAT_FONT_STORAGE_KEY = "chatFontSize";

const messagesElForFont = document.getElementById("messages");
const qaFontMinusBtn = document.getElementById("qa-font-minus");
const qaFontPlusBtn = document.getElementById("qa-font-plus");
const qaFontValueEl = document.getElementById("qa-font-value");

let chatFontSize = CHAT_FONT_DEFAULT;

function applyChatFontSize(size) {
  chatFontSize = Math.min(CHAT_FONT_MAX, Math.max(CHAT_FONT_MIN, size));
  if (messagesElForFont) {
    messagesElForFont.style.setProperty("--chat-font-size", `${chatFontSize}px`);
  }
  if (qaFontValueEl) {
    qaFontValueEl.textContent = `${chatFontSize}px`;
  }
  if (qaFontMinusBtn) qaFontMinusBtn.disabled = chatFontSize <= CHAT_FONT_MIN;
  if (qaFontPlusBtn) qaFontPlusBtn.disabled = chatFontSize >= CHAT_FONT_MAX;
}

function persistChatFontSize(size) {
  try {
    chrome.storage?.local?.set({ [CHAT_FONT_STORAGE_KEY]: size });
  } catch (err) {
    console.error("Failed to persist chat font size:", err);
  }
}

// Restore the saved preference on load (falls back to default silently
// if chrome.storage isn't available or nothing's been saved yet).
try {
  chrome.storage?.local?.get([CHAT_FONT_STORAGE_KEY], (result) => {
    const saved = result?.[CHAT_FONT_STORAGE_KEY];
    applyChatFontSize(typeof saved === "number" ? saved : CHAT_FONT_DEFAULT);
  });
} catch (err) {
  applyChatFontSize(CHAT_FONT_DEFAULT);
}

// Critical: stopPropagation on these clicks. The document-level click
// listener above closes Quick Actions on any click outside the menu,
// and even inside the menu a bare click would otherwise be free to
// bubble into logic that closes it. These buttons are meant to be
// clicked repeatedly while the menu stays open, so every interaction
// here is fully contained.
qaFontMinusBtn?.addEventListener("click", (e) => {
  e.stopPropagation();
  applyChatFontSize(chatFontSize - CHAT_FONT_STEP);
  persistChatFontSize(chatFontSize);
});

qaFontPlusBtn?.addEventListener("click", (e) => {
  e.stopPropagation();
  applyChatFontSize(chatFontSize + CHAT_FONT_STEP);
  persistChatFontSize(chatFontSize);
});

// ── Chat Capability Selector ─────────────────────────────────────────
// Two pills (Basic / Smart) in the quick-actions menu that
// let the user choose which LLM backs the chat. Stored here and read
// by main.js via the exported getter so it can be included in every
// WebSocket payload.
export let currentCapability = "basic";

const CAPABILITY_STORAGE_KEY = "chatCapability";

export function setCapability(cap) {
  currentCapability = cap;
  // Update active pill
  document.querySelectorAll(".qa-cap-pill").forEach(pill => {
    pill.classList.toggle("active", pill.dataset.cap === cap);
  });
  // Persist choice
  try {
    chrome.storage?.local?.set({ [CAPABILITY_STORAGE_KEY]: cap });
  } catch (err) {
    console.error("Failed to persist capability:", err);
  }
}

// Restore saved capability on load
try {
  chrome.storage?.local?.get([CAPABILITY_STORAGE_KEY], (result) => {
    const saved = result?.[CAPABILITY_STORAGE_KEY];
    if (saved && ["basic", "smart"].includes(saved)) {
      setCapability(saved);
    }
  });
} catch (err) {
  // Silently fall back to "basic" default
}

// Wire up the pill buttons — stopPropagation keeps the menu open
document.querySelectorAll(".qa-cap-pill").forEach(pill => {
  pill.addEventListener("click", (e) => {
    e.stopPropagation();
    setCapability(pill.dataset.cap);
  });
});

function setDragHoverState(targetZone) {
  zoneContext.classList.toggle("drag-hover", targetZone === "context");
  zoneCollections.classList.toggle("drag-hover", targetZone === "collections");
}

function clearDragHoverState() {
  zoneContext.classList.remove("drag-hover");
  zoneCollections.classList.remove("drag-hover");
}

window.addEventListener("dragenter", (e) => {
  e.preventDefault();
  dragOverlay.classList.add("active");
});

dragOverlay.addEventListener("dragover", (e) => {
  e.preventDefault();
  const overContext = !!e.target.closest("#zone-context");
  const overCollections = !!e.target.closest("#zone-collections");
  if (overContext) {
    setDragHoverState("context");
  } else if (overCollections) {
    setDragHoverState("collections");
  } else {
    clearDragHoverState();
  }
});

dragOverlay.addEventListener("dragleave", (e) => {
  if (e.relatedTarget === null || !dragOverlay.contains(e.relatedTarget)) {
    dragOverlay.classList.remove("active");
    clearDragHoverState();
  }
});

dragOverlay.addEventListener("drop", (e) => {
  e.preventDefault();
  dragOverlay.classList.remove("active");
  const droppedText = e.dataTransfer.getData("text/plain");
  if (!droppedText || droppedText.trim() === "") {
    clearDragHoverState();
    return;
  }
  if (e.target.closest("#zone-context")) {
    attachedContexts.push(droppedText.trim());
    renderContextShelf();
  } else if (e.target.closest("#zone-collections")) {
    openCollectionsPicker(droppedText.trim());
  }
  clearDragHoverState();
});

export function makeContextLabel(text) {
  const cleaned = text.replace(/\s+/g, " ").trim();
  const maxLen = 42;
  return cleaned.length > maxLen ? cleaned.slice(0, maxLen).trimEnd() + "…" : cleaned;
}

export function renderContextShelf() {
  contextShelf.innerHTML = "";
  if (attachedContexts.length === 0) {
    contextShelf.classList.add("hidden");
    expandedContextIndices.clear();
    return;
  }
  contextShelf.classList.remove("hidden");
  attachedContexts.forEach((text, index) => {
    const isExpanded = expandedContextIndices.has(index);
    const chip = document.createElement("div");
    chip.className = "context-chip" + (isExpanded ? " expanded" : "");

    const head = document.createElement("div");
    head.className = "context-chip-head";

    const icon = document.createElement("div");
    icon.className = "context-chip-icon";
    icon.innerHTML = `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M4 3.5h9l3 3v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M13 3.5v3h3" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M6.5 10.5h7M6.5 13h7M6.5 8h3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
    </svg>`;

    const label = document.createElement("span");
    label.className = "context-chip-label";
    label.textContent = makeContextLabel(text);

    const caret = document.createElement("div");
    caret.className = "context-chip-caret";
    caret.innerHTML = `<svg viewBox="0 0 12 8" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M1 1.5L6 6.5L11 1.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;

    const closeBtn = document.createElement("div");
    closeBtn.className = "context-close";
    closeBtn.innerHTML = "&times;";
    closeBtn.title = "Remove";
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      attachedContexts.splice(index, 1);
      expandedContextIndices.delete(index);
      renderContextShelf();
    });

    head.appendChild(icon);
    head.appendChild(label);
    head.appendChild(caret);
    head.appendChild(closeBtn);

    const body = document.createElement("div");
    body.className = "context-chip-body";

    const bodyInner = document.createElement("div");
    bodyInner.className = "context-chip-body-inner";

    const fullText = document.createElement("div");
    fullText.className = "context-chip-text";
    fullText.textContent = text;

    bodyInner.appendChild(fullText);
    body.appendChild(bodyInner);

    chip.appendChild(head);
    chip.appendChild(body);

    chip.addEventListener("click", () => {
      if (expandedContextIndices.has(index)) {
        expandedContextIndices.delete(index);
      } else {
        expandedContextIndices.add(index);
      }
      renderContextShelf();
    });

    contextShelf.appendChild(chip);
  });
}

function showOrganiseTabsOverlay() {
  organiseTabsOverlay.classList.add("active");
}

function hideOrganiseTabsOverlay() {
  organiseTabsOverlay.classList.remove("active");
}

organiseTabsOverlay.addEventListener("click", (e) => {
  if (!e.target.closest(".organise-card")) hideOrganiseTabsOverlay();
});

async function startOrganiseTabs(includeGrouped) {
  hideOrganiseTabsOverlay();
  NotificationService.show("Organising your tabs...");
  appWrap.classList.add("busy");

  try {
    const tabs = await chrome.tabs.query({ currentWindow: true });
    let filteredTabs = tabs;
    if (!includeGrouped) {
      filteredTabs = tabs.filter(tab => tab.groupId === -1 || tab.groupId === (chrome.tabGroups ? chrome.tabGroups.TAB_GROUP_ID_NONE : -1));
    }

    if (filteredTabs.length === 0) {
      appWrap.classList.remove("busy");
      NotificationService.show("No tabs found that need organising.");
      return;
    }

    const tabData = filteredTabs.map(tab => ({
      id: tab.id,
      title: tab.title || "",
      url: tab.url || "",
      groupId: tab.groupId
    }));

    const response = await fetch(`http://${BACKEND_HOST}/organise-tabs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tabs: tabData })
    });

    if (!response.ok) throw new Error(`HTTP Error Status: ${response.status}`);

    const data = await response.json();

    if (data.groups && data.groups.length > 0) {
      for (const groupPlan of data.groups) {
        const { title, color, tab_ids } = groupPlan;
        if (tab_ids && tab_ids.length > 0) {
          try {
            const groupId = await chrome.tabs.group({ tabIds: tab_ids });
            await chrome.tabGroups.update(groupId, {
              title: title,
              color: color
            });
          } catch (groupError) {
            console.warn("Failed to complete an individual tab group structure", groupError);
          }
        }
      }
      appWrap.classList.remove("busy");
      NotificationService.show(`Successfully organised tabs into ${data.groups.length} groups!`);
    } else {
      appWrap.classList.remove("busy");
      NotificationService.show("FastAPI LLM suggested no groupings.");
    }
  } catch (err) {
    appWrap.classList.remove("busy");
    console.error("Error organizing tabs:", err);
    NotificationService.show("Could not contact the Sicily Bridge. Is uvicorn active?");
  }
}

btnIncludeGrouped.addEventListener("click", () => startOrganiseTabs(true));
btnOnlyUngrouped.addEventListener("click", () => startOrganiseTabs(false));

export function clearAttachedContexts() {
  attachedContexts.length = 0;
  renderContextShelf();
}

async function startSummarisePage() {
  // 1. Show notification and trigger neural glow
  NotificationService.show("Summarising page...");
  appWrap.classList.add("busy");

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) throw new Error("No active tab found.");

    // 1b. Bail early with a clear message for pages Chrome will never let
    // us script into (chrome://, chrome-extension://, the Web Store, etc.)
    // — same check @-mention uses, so behaviour stays consistent across
    // the extension rather than falling through to a generic network-style
    // error that wrongly implies the backend is down.
    if (!isScriptableTab(tab)) {
      NotificationService.show("This page can't be summarised — browser security blocks reading it.");
      appWrap.classList.remove("busy");
      return;
    }

    // 2. Pre-LLM Extraction Layer (Domestic Chores)
    const injectionResult = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        // Strip out useless structural tags before grabbing text
        const clone = document.cloneNode(true);
        const tagsToRemove = ['nav', 'footer', 'aside', 'script', 'style', 'noscript', 'header'];
        tagsToRemove.forEach(tag => {
          const elements = clone.querySelectorAll(tag);
          elements.forEach(el => el.parentNode?.removeChild(el));
        });

        // Truncate to ~5000 characters to cap tokens (Smart Cap)
        return clone.body ? clone.body.innerText.substring(0, 5000) : "";
      }
    });

    const cleanText = injectionResult[0]?.result || "";

    if (!cleanText) {
      throw new Error("Could not extract text from this page.");
    }

    // 3. API Call to the dedicated nano-model route
    const response = await fetch(`http://${BACKEND_HOST}/summarise-page`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: tab.url,
        title: tab.title,
        content: cleanText
      })
    });

    if (!response.ok) throw new Error(`HTTP Error Status: ${response.status}`);

    const data = await response.json();

    // 4. Show the plain text result in the glassmorphism overlay
    summaryContent.textContent = data.summary;
    summaryOverlay.classList.add("active");

  } catch (err) {
    console.error("Summarise error:", err);
    NotificationService.show("Failed to summarise page. Is the backend running?");
  } finally {
    // Stop the neural glow
    appWrap.classList.remove("busy");
  }
}

// Tracks every URL already shown in the current find-more session, so
// repeated "Find More" clicks never resurface a link the user has already
// seen. Reset whenever the quick action is triggered fresh (a new overlay
// open, possibly for a different page) — see startFindMoreLikeThis's
// `append` flag.
let findMoreShownUrls = [];

function renderFindMoreResults(results, { append = false } = {}) {
  if (!append) {
    findMoreList.innerHTML = "";
    findMoreShownUrls = [];
  }

  if (!results || results.length === 0) {
    if (!append) {
      findMoreList.innerHTML = `<div class="find-more-empty">No related pages found for this one.</div>`;
    }
    // In append mode with zero new results, leave the existing list intact
    // and let the caller surface a "nothing new" notification instead of
    // wiping out what's already on screen.
    return;
  }

  let firstNewItem = null;

  results.forEach((r) => {
    findMoreShownUrls.push(r.url);

    const item = document.createElement("a");
    // fluidPopIn is defined once in overlays.css and keyed off this class,
    // so every batch — first load or a later "Find More" — gets the same
    // pop-in entrance animation.
    item.className = "link-result-item";
    item.href = r.url;
    item.target = "_blank";
    item.rel = "noopener noreferrer";

    const title = document.createElement("div");
    title.className = "link-result-title";
    title.textContent = r.title || r.url;

    const url = document.createElement("div");
    url.className = "link-result-url";
    url.textContent = r.url;

    const reason = document.createElement("div");
    reason.className = "link-result-reason";
    reason.textContent = r.reason || "";

    // Hover '+' — opens the "Add to Reading List" group picker for this
    // result. Stops propagation so it doesn't also trigger the card's
    // own href navigation.
    const addBtn = document.createElement("div");
    addBtn.className = "link-result-add-btn";
    addBtn.title = "Add to Reading List";
    addBtn.innerHTML = `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" width="14" height="14">
      <path d="M10 4v12M4 10h12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    </svg>`;
    addBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openReadingListPicker({
        title: r.title || r.url,
        url: r.url,
        reason: r.reason || "",
      });
    });

    item.appendChild(title);
    item.appendChild(url);
    if (r.reason) item.appendChild(reason);
    item.appendChild(addBtn);

    findMoreList.appendChild(item);
    if (!firstNewItem) firstNewItem = item;
  });

  // Auto-scroll to the first newly-added card so the user immediately
  // sees the new batch instead of having to scroll down manually to find
  // where the old results ended and the new ones begin.
  if (append && firstNewItem) {
    firstNewItem.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

async function startFindMoreLikeThis({ append = false } = {}) {
  NotificationService.show(append ? "Finding more..." : "Finding related pages...");
  appWrap.classList.add("busy");

  // Fresh open (not a "Find More" click) starts a clean session — clear
  // whatever was tracked from a previous page/overlay open.
  if (!append) {
    findMoreShownUrls = [];
  }

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) throw new Error("No active tab found.");

    // Same "can we even read this page" guard as Summarise Page — bail
    // early with a clear message instead of letting executeScript throw
    // and having that read as a backend/network failure.
    if (!isScriptableTab(tab)) {
      NotificationService.show("This page can't be analysed — browser security blocks reading it.");
      appWrap.classList.remove("busy");
      return;
    }

    // Same extraction step as Summarise Page — strip boilerplate tags,
    // cap length so the fingerprinting call stays cheap.
    const injectionResult = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const clone = document.cloneNode(true);
        const tagsToRemove = ['nav', 'footer', 'aside', 'script', 'style', 'noscript', 'header'];
        tagsToRemove.forEach(tag => {
          const elements = clone.querySelectorAll(tag);
          elements.forEach(el => el.parentNode?.removeChild(el));
        });
        return clone.body ? clone.body.innerText.substring(0, 5000) : "";
      }
    });

    const cleanText = injectionResult[0]?.result || "";

    if (!cleanText) {
      throw new Error("Could not extract text from this page.");
    }

    const response = await fetch(`http://${BACKEND_HOST}/find-more-like-this`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: tab.url,
        title: tab.title,
        content: cleanText,
        // Backend excludes these from both the search-candidate pool and
        // the ranking step, so "Find More" never re-surfaces a link
        // that's already on screen.
        exclude_urls: findMoreShownUrls,
      })
    });

    if (!response.ok) throw new Error(`HTTP Error Status: ${response.status}`);

    const data = await response.json();

    if (data.error && (!data.results || data.results.length === 0)) {
      if (append) {
        // Don't wipe the existing list on a failed "Find More" — just
        // tell the user nothing new turned up this time.
        NotificationService.show(data.error);
      } else {
        findMoreList.innerHTML = `<div class="find-more-empty">${data.error}</div>`;
      }
    } else {
      renderFindMoreResults(data.results, { append });
    }

    // Opening (or re-opening) the overlay is idempotent — safe to call on
    // every run, and required on the very first run.
    findMoreOverlay.classList.add("active");

  } catch (err) {
    console.error("Find more like this error:", err);
    NotificationService.show("Failed to find related pages. Is the backend running?");
  } finally {
    appWrap.classList.remove("busy");
  }
}

// ── Add to Collections Picker ───────────────────────────────────────
// Drag-drop entry point is the "collections" branch in the drop
// handler above. This owns the floating picker: it fetches existing
// collections, filters them live as the user types, and posts to
// /collections/add-snippet whether the user picks an existing
// collection or types a brand new name ("Create & Add").

let pendingSnippet = null; // { text, tab_title, url } captured at drop time
let allCollections = [];   // last fetch from GET /collections

async function openCollectionsPicker(text) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  pendingSnippet = {
    text,
    tab_title: tab?.title || "",
    url: tab?.url || "",
  };

  collectionsPickerPreview.textContent = text;
  collectionsPickerInput.value = "";
  collectionsPickerOverlay.classList.add("active");

  collectionsPickerList.innerHTML = `<div class="collections-picker-empty">Loading collections…</div>`;
  updateCreateButton("");

  try {
    const res = await fetch(`http://${BACKEND_HOST}/collections`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    allCollections = data.collections || [];
  } catch (err) {
    allCollections = [];
    collectionsPickerList.innerHTML = `<div class="collections-picker-empty">Couldn't reach the backend — is navigator_bridge.py running?</div>`;
    return;
  }

  renderCollectionsList("");
  collectionsPickerInput.focus();
}

function closeCollectionsPicker() {
  collectionsPickerOverlay.classList.remove("active");
  pendingSnippet = null;
}

collectionsPickerOverlay?.addEventListener("click", (e) => {
  if (!e.target.closest(".organise-card")) {
    closeCollectionsPicker();
  }
});

collectionsPickerInput?.addEventListener("input", () => {
  const query = collectionsPickerInput.value.trim();
  renderCollectionsList(query);
  updateCreateButton(query);
});

collectionsPickerInput?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const query = collectionsPickerInput.value.trim();
    const filtered = filterCollections(query);
    // Exact existing match on Enter -> add to it directly; otherwise
    // fall through to create-and-add, same as clicking the button.
    const exact = filtered.find((c) => c.name.toLowerCase() === query.toLowerCase());
    if (exact) {
      submitSnippetToCollection(exact.name);
    } else if (query) {
      submitSnippetToCollection(query);
    }
  } else if (e.key === "Escape") {
    closeCollectionsPicker();
  }
});

collectionsCreateBtn?.addEventListener("click", () => {
  const query = collectionsPickerInput.value.trim();
  if (query) submitSnippetToCollection(query);
});

function filterCollections(query) {
  if (!query) return allCollections;
  const q = query.toLowerCase();
  return allCollections.filter((c) => c.name.toLowerCase().includes(q));
}

function renderCollectionsList(query) {
  const filtered = filterCollections(query);
  collectionsPickerList.innerHTML = "";

  if (filtered.length === 0) {
    collectionsPickerList.innerHTML = `<div class="collections-picker-empty">${allCollections.length === 0 ? "No collections yet." : "No matching collections."
      }</div>`;
    return;
  }

  filtered.forEach((c) => {
    const item = document.createElement("button");
    item.className = "collection-picker-item";

    const icon = document.createElement("span");
    icon.className = "qa-icon";
    icon.innerHTML = `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M6 3.5h5.5L15 7v9a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M11 3.5V7h3.5" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M7.5 10.5h5M7.5 13h3.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
    </svg>`;

    const name = document.createElement("span");
    name.className = "collection-picker-name";
    name.textContent = c.name;

    const count = document.createElement("span");
    count.className = "collection-picker-count";
    count.textContent = c.snippet_count;

    item.appendChild(icon);
    item.appendChild(name);
    item.appendChild(count);

    item.addEventListener("click", () => submitSnippetToCollection(c.name));
    collectionsPickerList.appendChild(item);
  });
}

function updateCreateButton(query) {
  if (!query) {
    collectionsCreateBtn.classList.add("hidden");
    return;
  }
  const exists = allCollections.some((c) => c.name.toLowerCase() === query.toLowerCase());
  if (exists) {
    collectionsCreateBtn.classList.add("hidden");
  } else {
    collectionsCreateBtnLabel.textContent = `Create "${query}" & Add`;
    collectionsCreateBtn.classList.remove("hidden");
  }
}

async function submitSnippetToCollection(collectionName) {
  if (!pendingSnippet) return;
  const snippet = pendingSnippet;
  closeCollectionsPicker();

  try {
    const res = await fetch(`http://${BACKEND_HOST}/collections/add-snippet`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        collection_name: collectionName,
        text: snippet.text,
        tab_title: snippet.tab_title,
        url: snippet.url,
      }),
    });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    NotificationService.show(
      data.created_new_collection
        ? `Created "${data.collection_name}" and added snippet.`
        : `Added to "${data.collection_name}".`
    );
  } catch (err) {
    console.error("Add to collection failed:", err);
    NotificationService.show("Couldn't save to collection — is the backend running?");
  }
}

async function openSavedCollectionsView() {
  collectionsViewOverlay.classList.add("active");
  collectionsViewTitle.textContent = "Saved Collections";

  // SCENE A: Hide the Back button completely
  collectionsViewActions.style.display = "none";

  collectionsViewList.innerHTML = `<div class="collections-picker-empty">Loading collections...</div>`;

  try {
    const res = await fetch(`http://${BACKEND_HOST}/collections`);
    if (!res.ok) throw new Error("Failed to load");
    const data = await res.json();
    renderCollectionsView(data.collections || []);
  } catch (err) {
    collectionsViewList.innerHTML = `<div class="collections-picker-empty">Backend unreachable.</div>`;
  }
}

function renderCollectionsView(collections) {
  collectionsViewList.innerHTML = "";
  if (collections.length === 0) {
    collectionsViewList.innerHTML = `<div class="collections-picker-empty">No saved collections yet.</div>`;
    return;
  }

  collections.forEach(c => {
    const item = document.createElement("button");
    item.className = "collection-view-item";
    item.innerHTML = `
      <div style="padding-right: 32px; text-align: left;">
        <div style="font-weight: 600;">${c.name}</div>
        <div style="font-size: 11px; color: #8e8e93; margin-top: 3px;">${c.snippet_count} saved snippet${c.snippet_count !== 1 ? 's' : ''}</div>
      </div>
      <div class="item-delete-btn" title="Delete Collection">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
        </svg>
      </div>
    `;

    item.addEventListener("click", (e) => {
      e.stopPropagation();
      openCollectionSnippets(c.id, c.name);
    });

    const deleteBtn = item.querySelector('.item-delete-btn');
    deleteBtn.addEventListener('click', async (e) => {
      e.stopPropagation(); // Stop the card click from firing
      if (!confirm(`Delete collection "${c.name}"?`)) return;

      try {
        const res = await fetch(`http://${BACKEND_HOST}/collections/${c.id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error("Failed to delete");

        item.remove();
        NotificationService.show(`Deleted "${c.name}"`);
      } catch (err) {
        NotificationService.show("Error deleting collection.");
      }
    });

    collectionsViewList.appendChild(item);
  });
}

async function openCollectionSnippets(collectionId, collectionName) {
  collectionsViewTitle.textContent = collectionName;

  // SCENE B: Show the Back button
  collectionsViewActions.style.display = "flex";

  collectionsViewList.innerHTML = `<div class="collections-picker-empty">Loading snippets...</div>`;

  try {
    const res = await fetch(`http://${BACKEND_HOST}/collections/${collectionId}`);
    if (!res.ok) throw new Error("Failed to load");
    const data = await res.json();
    renderSnippetsView(data.snippets || []);
  } catch (err) {
    collectionsViewList.innerHTML = `<div class="collections-picker-empty">Error loading snippets.</div>`;
  }
}

function renderSnippetsView(snippets) {
  collectionsViewList.innerHTML = "";
  if (snippets.length === 0) {
    collectionsViewList.innerHTML = `<div class="collections-picker-empty">This collection is empty.</div>`;
    return;
  }

  snippets.forEach(s => {
    const item = document.createElement("div");
    item.className = "snippet-view-item";
    const dateStr = new Date(s.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

    item.innerHTML = `
      <div style="padding-right: 32px;">
        <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px;">
          <div style="font-weight: 600; font-size: 11.5px; color: #e5e5ea; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 80%;">${s.tab_title || 'Unknown Source'}</div>
          <div style="font-size: 10px; color: #636366;">${dateStr}</div>
        </div>
        <div class="snippet-text">${s.text}</div>
      </div>
      <div class="item-delete-btn" title="Remove Snippet">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
        </svg>
      </div>
    `;

    const deleteBtn = item.querySelector('.item-delete-btn');
    deleteBtn.addEventListener('click', async (e) => {
      e.stopPropagation();

      try {
        const res = await fetch(`http://${BACKEND_HOST}/collections/snippets/${s.id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error("Failed to delete");

        item.remove();
        NotificationService.show("Snippet removed from collection.");
      } catch (err) {
        NotificationService.show("Error deleting snippet.");
      }
    });

    collectionsViewList.appendChild(item);
  });
}

// ── Add to Reading List Group Picker ────────────────────────────────
// Entry point is the hover '+' on a link-result-item (see
// renderFindMoreResults above). This owns the floating picker: it
// fetches existing reading list groups, filters them live as the user
// types, and posts to /reading-list-groups/add-item whether the user
// picks an existing group or types a brand new name ("Create & Add").
// Structurally identical to the Collections picker just above.

let pendingReadingListLink = null; // { title, url, reason } captured at '+' click time
let allReadingListGroups = [];     // last fetch from GET /reading-list-groups

async function openReadingListPicker(link) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  pendingReadingListLink = {
    title: link.title,
    url: link.url,
    reason: link.reason || "",
    source_title: tab?.title || "",
    source_url: tab?.url || "",
  };

  readingListPickerPreviewTitle.textContent = link.title;
  readingListPickerInput.value = "";
  readingListPickerOverlay.classList.add("active");

  readingListPickerList.innerHTML = `<div class="collections-picker-empty">Loading reading lists…</div>`;
  updateReadingListCreateButton("");

  try {
    const res = await fetch(`http://${BACKEND_HOST}/reading-list-groups`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    allReadingListGroups = data.groups || [];
  } catch (err) {
    allReadingListGroups = [];
    readingListPickerList.innerHTML = `<div class="collections-picker-empty">Couldn't reach the backend — is navigator_bridge.py running?</div>`;
    return;
  }

  renderReadingListGroupsPickerList("");
  readingListPickerInput.focus();
}

function closeReadingListPicker() {
  readingListPickerOverlay.classList.remove("active");
  pendingReadingListLink = null;
}

readingListPickerOverlay?.addEventListener("click", (e) => {
  if (!e.target.closest(".organise-card")) {
    closeReadingListPicker();
  }
});

readingListPickerInput?.addEventListener("input", () => {
  const query = readingListPickerInput.value.trim();
  renderReadingListGroupsPickerList(query);
  updateReadingListCreateButton(query);
});

readingListPickerInput?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const query = readingListPickerInput.value.trim();
    const filtered = filterReadingListGroups(query);
    const exact = filtered.find((g) => g.name.toLowerCase() === query.toLowerCase());
    if (exact) {
      submitLinkToReadingListGroup(exact.name);
    } else if (query) {
      submitLinkToReadingListGroup(query);
    }
  } else if (e.key === "Escape") {
    closeReadingListPicker();
  }
});

readingListCreateBtn?.addEventListener("click", () => {
  const query = readingListPickerInput.value.trim();
  if (query) submitLinkToReadingListGroup(query);
});

function filterReadingListGroups(query) {
  if (!query) return allReadingListGroups;
  const q = query.toLowerCase();
  return allReadingListGroups.filter((g) => g.name.toLowerCase().includes(q));
}

function renderReadingListGroupsPickerList(query) {
  const filtered = filterReadingListGroups(query);
  readingListPickerList.innerHTML = "";

  if (filtered.length === 0) {
    readingListPickerList.innerHTML = `<div class="collections-picker-empty">${allReadingListGroups.length === 0 ? "No reading lists yet." : "No matching reading lists."
      }</div>`;
    return;
  }

  filtered.forEach((g) => {
    const item = document.createElement("button");
    item.className = "collection-picker-item";

    const icon = document.createElement("span");
    icon.className = "qa-icon";
    icon.innerHTML = `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M5 3.5h10a.5.5 0 0 1 .5.5v12.3a.3.3 0 0 1-.46.25L10 13.8l-5.04 2.75a.3.3 0 0 1-.46-.25V4a.5.5 0 0 1 .5-.5z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
    </svg>`;

    const name = document.createElement("span");
    name.className = "collection-picker-name";
    name.textContent = g.name;

    const count = document.createElement("span");
    count.className = "collection-picker-count";
    count.textContent = g.item_count;

    item.appendChild(icon);
    item.appendChild(name);
    item.appendChild(count);

    item.addEventListener("click", () => submitLinkToReadingListGroup(g.name));
    readingListPickerList.appendChild(item);
  });
}

function updateReadingListCreateButton(query) {
  if (!query) {
    readingListCreateBtn.classList.add("hidden");
    return;
  }
  const exists = allReadingListGroups.some((g) => g.name.toLowerCase() === query.toLowerCase());
  if (exists) {
    readingListCreateBtn.classList.add("hidden");
  } else {
    readingListCreateBtnLabel.textContent = `Create "${query}" & Add`;
    readingListCreateBtn.classList.remove("hidden");
  }
}

async function submitLinkToReadingListGroup(groupName) {
  if (!pendingReadingListLink) return;
  const link = pendingReadingListLink;
  closeReadingListPicker();

  try {
    const res = await fetch(`http://${BACKEND_HOST}/reading-list-groups/add-item`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        group_name: groupName,
        title: link.title,
        url: link.url,
        reason: link.reason,
        source_title: link.source_title,
        source_url: link.source_url,
      }),
    });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    if (data.already_existed) {
      NotificationService.show(`Already in "${data.group_name}".`);
    } else {
      NotificationService.show(
        data.created_new_group
          ? `Created "${data.group_name}" and added link.`
          : `Added to "${data.group_name}".`
      );
    }
  } catch (err) {
    console.error("Add to reading list failed:", err);
    NotificationService.show("Couldn't save to reading list — is the backend running?");
  }
}

// ── Reading List Groups Viewer ──────────────────────────────────────
// Opened from the "Reading List Groups" quick action. Scene A lists
// every group; clicking one drills into Scene B showing its saved
// links, each with a read/unread toggle and a delete button — same
// two-scene shape as the Saved Collections viewer above.

async function openReadingListGroupsView() {
  readingListGroupsViewOverlay.classList.add("active");
  readingListGroupsViewTitle.textContent = "Reading List Groups";
  readingListGroupsViewActions.style.display = "none";

  readingListGroupsViewList.innerHTML = `<div class="collections-picker-empty">Loading reading lists...</div>`;

  try {
    const res = await fetch(`http://${BACKEND_HOST}/reading-list-groups`);
    if (!res.ok) throw new Error("Failed to load");
    const data = await res.json();
    renderReadingListGroupsView(data.groups || []);
  } catch (err) {
    readingListGroupsViewList.innerHTML = `<div class="collections-picker-empty">Backend unreachable.</div>`;
  }
}

function renderReadingListGroupsView(groups) {
  readingListGroupsViewList.innerHTML = "";
  if (groups.length === 0) {
    readingListGroupsViewList.innerHTML = `<div class="collections-picker-empty">No reading list groups yet.</div>`;
    return;
  }

  groups.forEach(g => {
    const item = document.createElement("button");
    item.className = "collection-view-item";
    item.innerHTML = `
      <div style="padding-right: 32px; text-align: left;">
        <div style="font-weight: 600;">${g.name}</div>
        <div style="font-size: 11px; color: #8e8e93; margin-top: 3px;">${g.item_count} saved link${g.item_count !== 1 ? 's' : ''}</div>
      </div>
      <div class="item-delete-btn" title="Delete Reading List">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
        </svg>
      </div>
    `;

    item.addEventListener("click", (e) => {
      e.stopPropagation();
      openReadingListGroupItems(g.id, g.name);
    });

    const deleteBtn = item.querySelector('.item-delete-btn');
    deleteBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete reading list "${g.name}"?`)) return;

      try {
        const res = await fetch(`http://${BACKEND_HOST}/reading-list-groups/${g.id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error("Failed to delete");

        item.remove();
        NotificationService.show(`Deleted "${g.name}"`);
      } catch (err) {
        NotificationService.show("Error deleting reading list.");
      }
    });

    readingListGroupsViewList.appendChild(item);
  });
}

async function openReadingListGroupItems(groupId, groupName) {
  readingListGroupsViewTitle.textContent = groupName;
  readingListGroupsViewActions.style.display = "flex";

  readingListGroupsViewList.innerHTML = `<div class="collections-picker-empty">Loading links...</div>`;

  try {
    const res = await fetch(`http://${BACKEND_HOST}/reading-list-groups/${groupId}`);
    if (!res.ok) throw new Error("Failed to load");
    const data = await res.json();
    renderReadingListItemsView(data.items || []);
  } catch (err) {
    readingListGroupsViewList.innerHTML = `<div class="collections-picker-empty">Error loading links.</div>`;
  }
}

function renderReadingListItemsView(items) {
  readingListGroupsViewList.innerHTML = "";
  if (items.length === 0) {
    readingListGroupsViewList.innerHTML = `<div class="collections-picker-empty">This reading list is empty.</div>`;
    return;
  }

  items.forEach(it => {
    const item = document.createElement("a");
    item.className = "link-result-item reading-list-item-view" + (it.is_read ? " is-read" : "");
    item.href = it.url;
    item.target = "_blank";
    item.rel = "noopener noreferrer";

    const title = document.createElement("div");
    title.className = "link-result-title";
    title.textContent = it.title;

    const url = document.createElement("div");
    url.className = "link-result-url";
    url.textContent = it.url;

    item.appendChild(title);
    item.appendChild(url);

    if (it.reason) {
      const reason = document.createElement("div");
      reason.className = "link-result-reason";
      reason.textContent = it.reason;
      item.appendChild(reason);
    }

    // Tick mark in both states — dim/outline when unread, filled green
    // (via the .is-read CSS rule) when read. Same glyph throughout, only
    // the styling communicates the state, so it always reads as "mark as
    // read" rather than switching to a different icon after the toggle.
    const TICK_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M20 6 9 17l-5-5"/></svg>`;

    const readBtn = document.createElement("div");
    readBtn.className = "item-read-toggle-btn" + (it.is_read ? " is-read" : "");
    readBtn.title = it.is_read ? "Mark as unread" : "Mark as read";
    readBtn.innerHTML = TICK_SVG;
    readBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const newIsRead = !it.is_read;
      try {
        const res = await fetch(`http://${BACKEND_HOST}/reading-list-groups/items/${it.id}/read`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ is_read: newIsRead }),
        });
        if (!res.ok) throw new Error("Failed to update");
        it.is_read = newIsRead;
        item.classList.toggle("is-read", newIsRead);
        readBtn.classList.toggle("is-read", newIsRead);
        readBtn.title = newIsRead ? "Mark as unread" : "Mark as read";
      } catch (err) {
        NotificationService.show("Error updating read status.");
      }
    });

    const deleteBtn = document.createElement("div");
    deleteBtn.className = "item-delete-btn";
    deleteBtn.title = "Remove Link";
    deleteBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
      <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
    </svg>`;
    deleteBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();

      try {
        const res = await fetch(`http://${BACKEND_HOST}/reading-list-groups/items/${it.id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error("Failed to delete");
        item.remove();
        NotificationService.show("Link removed from reading list.");
      } catch (err) {
        NotificationService.show("Error deleting link.");
      }
    });

    item.appendChild(readBtn);
    item.appendChild(deleteBtn);
    readingListGroupsViewList.appendChild(item);
  });
}
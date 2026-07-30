/**
 * @-mention tab picker.
 *
 * Typing "@" in the input box opens a dynamic dropdown of every open tab
 * (across all windows). Picking one:
 *   1. Removes the "@" (and whatever partial filter text followed it)
 *      from the input.
 *   2. Extracts that tab's full page text (same DOM-cleaning approach as
 *      Summarise Page, but WITHOUT the summarisation call and WITHOUT the
 *      5000-char cap — this is the raw page content, sent straight to chat
 *      as-is on the next message).
 *   3. Shows a single "Using: <tab title>" bar just above the input row.
 *
 * Re-mentioning a tab REPLACES the current mention (there is only ever one
 * mentioned tab at a time) rather than stacking, per spec.
 */

import { NotificationService } from "./notifications.js";
import { BACKEND_HOST } from "./api.js";

const inputEl = document.getElementById("input-box");
const bottomDock = document.getElementById("bottom-dock");
const inputRow = document.getElementById("input-row");

// ── State ────────────────────────────────────────────────────────────
// All currently mentioned tabs / collections. Each @ or # pick now ADDS
// to these lists (de-duped by id) instead of replacing a single value —
// the bar's display collapses to "Using Multiple Tabs/Collections" once
// there's more than one, but every item picked is kept and sent.
let mentionedTabs = []; // [{ tabId, title, url, favIconUrl, content }]
let mentionedCollections = []; // [{ id, name }]

// Mention-typing state: are we mid "@filter" or "#filter" in the input right now?
let mentionActive = false;
let mentionType = null; // '@' or '#'
let mentionStartIndex = -1; // index of the trigger character in inputEl.value
let blurCloseTimer = null; // pending delayed close-on-blur, cancellable

let dropdownEl = null;
let allTabsCache = []; // refreshed each time the dropdown opens
let allCollectionsCache = []; // refreshed each time the # dropdown opens
let highlightedIndex = 0;

// ── Public API ───────────────────────────────────────────────────────

// Every mentioned tab, framed the same way attachedContexts snippets read
// on the wire (backend just folds context_snippets into the turn, so no
// backend changes needed here). Each tab gets its own clearly-labeled
// block so the model can tell them apart when asked "where is X mentioned".
export function getMentionedTabSnippets() {
    if (!mentionedTabs.length) return [];
    return mentionedTabs.map(t => `Tab: "${t.title}"\n\n${t.content}`);
}

export function hasMentionedTab() {
    return mentionedTabs.length > 0;
}

export function clearMentionedTab({ animate = true } = {}) {
    if (!mentionedTabs.length) return;
    mentionedTabs = [];
    hideUsingBar(animate);
}

// Collection IDs only — main.js fetches each collection's full text from
// the backend right before sending, same as it always did for the single
// case, just now for every id in the list.
export function getMentionedCollectionIds() {
    return mentionedCollections.map(c => c.id);
}

export function hasMentionedCollection() {
    return mentionedCollections.length > 0;
}

export function clearMentionedCollection({ animate = true } = {}) {
    if (!mentionedCollections.length) return;
    mentionedCollections = [];
    hideUsingCollectionBar(animate);
}

export function isMentionDropdownOpen() {
    return mentionActive && !!dropdownEl && dropdownEl.classList.contains("mention-dropdown--visible");
}

// ── "Using: tab" bar ─────────────────────────────────────────────────
// Single tab  -> favicon + "Using Tab: <title>"
// 2+ tabs     -> up to 3 stacked favicons + "Using Multiple Tabs"
// Clicking the bar (anywhere except the close button) reopens the same
// @ dropdown UI used to pick tabs in the first place, with everything
// already picked pre-checked, so adding/removing more stays one flow.
let usingBarEl = null;

function ensureUsingBarEl() {
    if (usingBarEl) return usingBarEl;

    usingBarEl = document.createElement("div");
    usingBarEl.id = "using-tab-bar";
    usingBarEl.className = "using-tab-bar";

    const iconWrap = document.createElement("div");
    iconWrap.className = "using-tab-icon";

    const label = document.createElement("div");
    label.className = "using-tab-label";

    const prefix = document.createElement("span");
    prefix.className = "using-tab-prefix";
    prefix.textContent = "Using Tab: ";

    const name = document.createElement("span");
    name.className = "using-tab-name";

    label.appendChild(prefix);
    label.appendChild(name);

    const closeBtn = document.createElement("div");
    closeBtn.className = "using-tab-close";
    closeBtn.innerHTML = "&times;";
    closeBtn.title = "Stop using tabs";
    closeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        clearMentionedTab();
    });

    usingBarEl.appendChild(iconWrap);
    usingBarEl.appendChild(label);
    usingBarEl.appendChild(closeBtn);

    usingBarEl.addEventListener("click", () => {
        if (mentionedTabs.length <= 1) return; // nothing to manage in a list of one
        openManageDropdown("@");
    });

    // Insert directly above #input-row, inside the same floating dock, so
    // it shares the glass cluster and pushes the input row down naturally.
    bottomDock.insertBefore(usingBarEl, inputRow);

    return usingBarEl;
}

function showUsingBar({ animate = true } = {}) {
    const el = ensureUsingBarEl();
    const iconWrap = el.querySelector(".using-tab-icon");
    const prefixEl = el.querySelector(".using-tab-prefix");
    const nameEl = el.querySelector(".using-tab-name");

    if (mentionedTabs.length === 1) {
        const tab = mentionedTabs[0];
        prefixEl.textContent = "Using Tab: ";
        nameEl.textContent = tab.title || tab.url || "Untitled tab";
        el.title = tab.url || "";
        iconWrap.classList.remove("using-tab-icon--stack");
        iconWrap.innerHTML = "";
        setTabIcon(iconWrap, tab.favIconUrl);
    } else {
        prefixEl.textContent = "";
        nameEl.textContent = "Using Multiple Tabs";
        el.title = mentionedTabs.map(t => t.title || t.url).join(", ");
        renderStackedFavicons(iconWrap, mentionedTabs);
    }

    // Restart the "born from the input box" entrance animation every time
    // a mention is (re)made, so the bar always visibly reasserts itself.
    el.classList.remove("using-tab-bar--born");
    el.classList.add("using-tab-bar--visible");
    if (animate) {
        // Force reflow so the removed class actually resets before we re-add it.
        void el.offsetWidth;
        el.classList.add("using-tab-bar--born");
    }
}

function hideUsingBar(animate = true) {
    if (!usingBarEl) return;
    if (!animate) {
        usingBarEl.classList.remove("using-tab-bar--visible", "using-tab-bar--born");
        return;
    }
    usingBarEl.classList.add("using-tab-bar--leaving");
    usingBarEl.classList.remove("using-tab-bar--visible");
    setTimeout(() => {
        usingBarEl?.classList.remove("using-tab-bar--leaving", "using-tab-bar--born");
    }, 320);
}

// Renders up to 3 overlapping favicons (extra items just add to the
// count implied by "Multiple", we don't need a "+N" badge here — the
// label already says "Multiple Tabs").
function renderStackedFavicons(iconWrap, tabs) {
    iconWrap.classList.add("using-tab-icon--stack");
    iconWrap.innerHTML = "";
    tabs.slice(0, 3).forEach(tab => {
        const slot = document.createElement("div");
        slot.className = "using-tab-icon-slot";
        setTabIcon(slot, tab.favIconUrl);
        iconWrap.appendChild(slot);
    });
}

function fallbackTabIconSvg() {
    return `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
    <rect x="2.5" y="4" width="15" height="12" rx="2" stroke="currentColor" stroke-width="1.4"/>
    <path d="M2.5 7.5h15" stroke="currentColor" stroke-width="1.4"/>
  </svg>`;
}

// Renders a favicon <img> that swaps itself out for the fallback SVG on
// load failure, instead of leaving a broken image / console error. Some
// favicons (cross-origin, NotSameOrigin-blocked, 404s) simply won't load
// in the extension popup context — that's expected and cosmetic, not a
// bug, so we just degrade quietly to the generic tab icon.
function setTabIcon(iconEl, favIconUrl) {
    if (!favIconUrl) {
        iconEl.innerHTML = fallbackTabIconSvg();
        return;
    }
    iconEl.innerHTML = "";
    const img = document.createElement("img");
    img.alt = "";
    img.referrerPolicy = "no-referrer";
    img.onerror = () => {
        iconEl.innerHTML = fallbackTabIconSvg();
    };
    img.src = favIconUrl;
    iconEl.appendChild(img);
}

// Tabs whose URL scheme Chrome will never allow scripting into,
// regardless of what host_permissions the extension holds. Filtering
// these out up front means the user never picks a dead option in the
// first place, instead of hitting a runtime permission error after
// the fact.
const UNSCRIPTABLE_URL_PREFIXES = [
    "chrome://", "chrome-extension://", "edge://", "about:",
    "chrome.google.com/webstore", "chromewebstore.google.com",
    "https://chrome.google.com/webstore",
];

function isScriptableTab(tab) {
    const url = tab.url || "";
    if (!url) return false;
    return !UNSCRIPTABLE_URL_PREFIXES.some(prefix => url.startsWith(prefix) || url.includes(prefix));
}
async function extractFullPageText(tabId) {
    const injectionResult = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
            // 1. Only remove non-content execution/style tags. 
            // DO NOT remove <header> or <aside>—Jira & Bitbucket store key sidebars there!
            const IGNORED_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG']);

            function extractNodeText(node) {
                if (!node) return '';

                // Skip non-content tags
                if (node.nodeType === Node.ELEMENT_NODE && IGNORED_TAGS.has(node.tagName)) {
                    return '';
                }

                // Plain Text Nodes
                if (node.nodeType === Node.TEXT_NODE) {
                    return node.textContent || '';
                }

                // Element Nodes
                if (node.nodeType === Node.ELEMENT_NODE) {
                    const el = node;

                    // Skip hidden elements
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') {
                        return '';
                    }

                    let pieces = [];

                    // Extract missing attributes often used in Jira/Bitbucket for Assignees/Reviewers/Avatars
                    const ariaLabel = el.getAttribute('aria-label');
                    const alt = el.getAttribute('alt');
                    const title = el.getAttribute('title');

                    // Capture Avatar images (e.g., <img alt="John Doe" />)
                    if (el.tagName === 'IMG' && alt) {
                        pieces.push(` [Image: ${alt}] `);
                    }

                    // Capture dynamic badges / button labels
                    if (ariaLabel && !el.innerText?.includes(ariaLabel)) {
                        pieces.push(` [${ariaLabel}] `);
                    } else if (title && !el.innerText?.includes(title)) {
                        pieces.push(` [${title}] `);
                    }

                    // Walk Light DOM children
                    for (const child of el.childNodes) {
                        pieces.push(extractNodeText(child));
                    }

                    // Walk Shadow DOM children (Critical for Atlassian/Atlaskit components!)
                    if (el.shadowRoot) {
                        for (const shadowChild of el.shadowRoot.childNodes) {
                            pieces.push(extractNodeText(shadowChild));
                        }
                    }

                    // Add formatting spacing for block-level elements
                    const isBlock = style.display === 'block' ||
                        style.display === 'flex' ||
                        style.display === 'grid' ||
                        ['P', 'DIV', 'TR', 'SECTION', 'MAIN', 'ASIDE', 'HEADER'].includes(el.tagName);

                    let result = pieces.join('');
                    if (isBlock && result.trim()) {
                        result = '\n' + result + '\n';
                    }
                    return result;
                }

                return '';
            }

            const rawText = extractNodeText(document.body);

            // Clean up multi-line white spaces without losing structure
            return rawText
                .split('\n')
                .map(line => line.trim())
                .filter(line => line.length > 0)
                .join('\n');
        }
    });
    return injectionResult[0]?.result || "";
}

function isMissingHostPermissionError(err) {
    const message = String(err?.message || err || "");
    return message.includes("Cannot access contents") || message.includes("Extension manifest must request permission");
}

/**
 * Chrome only injects host_permissions grants into a tab's renderer at
 * the moment that tab navigates. A tab that was already open BEFORE this
 * extension was installed, reloaded (common during dev via "Reload" in
 * chrome://extensions), or granted <all_urls> will still throw "Cannot
 * access contents..." from executeScript even though the manifest is
 * completely correct — until that tab itself reloads/navigates once.
 *
 * So on that specific error (not other failures), we reload the target
 * tab once and retry the injection — this silently self-heals the
 * overwhelmingly common case without the user needing to know why.
 */
async function extractFullPageTextWithRetry(tab) {
    // Tabs in a collapsed group (or just backgrounded long enough) get
    // discarded by Chrome — their renderer doesn't exist, so injection
    // either throws or runs against an empty document. Reload to force
    // Chrome to re-hydrate it, same self-heal as the permissions case.
    if (tab.discarded) {
        NotificationService.show(`Waking up "${tab.title || tab.url}"…`);
        await chrome.tabs.reload(tab.id);
        await waitForTabComplete(tab.id);
    }

    try {
        return await extractFullPageText(tab.id);
    } catch (err) {
        if (!isMissingHostPermissionError(err)) throw err;

        NotificationService.show(`Refreshing "${tab.title || tab.url}" to enable access…`);
        await chrome.tabs.reload(tab.id);
        await waitForTabComplete(tab.id);

        return await extractFullPageText(tab.id);
    }
}

function waitForTabComplete(tabId, timeoutMs = 8000) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = () => {
            if (settled) return;
            settled = true;
            chrome.tabs.onUpdated.removeListener(listener);
            clearTimeout(timer);
            resolve();
        };
        const listener = (updatedTabId, changeInfo) => {
            if (updatedTabId === tabId && changeInfo.status === "complete") finish();
        };
        chrome.tabs.onUpdated.addListener(listener);
        const timer = setTimeout(finish, timeoutMs);
    });
}

async function selectTabForMention(tab) {
    // IMPORTANT: remove the "@filter" text from the input BEFORE closing
    // the dropdown — closeDropdown() resets mentionStartIndex, and once
    // that's gone removeMentionTextFromInput() has nothing to go on and
    // silently no-ops, leaving the stray "@" sitting in the box.
    removeMentionTextFromInput();
    closeDropdown();

    // Already mentioned — nothing to do here. Removing a mention only
    // happens via the × in the manage view (opened by clicking the bar).
    const alreadyMentioned = mentionedTabs.some(t => t.tabId === tab.id);
    if (alreadyMentioned) {
        inputEl.focus();
        return;
    }

    try {
        const content = await extractFullPageTextWithRetry(tab);
        if (!content) {
            NotificationService.show("Couldn't extract text from that tab.");
            return;
        }
        mentionedTabs.push({
            tabId: tab.id,
            title: tab.title || tab.url || "Untitled tab",
            url: tab.url || "",
            favIconUrl: tab.favIconUrl || "",
            content,
        });
        showUsingBar({ animate: true });
    } catch (err) {
        console.error("Mention extraction error:", err);
        if (isMissingHostPermissionError(err)) {
            // We already tried a reload-and-retry inside extractFullPageTextWithRetry
            // and it still failed — genuinely out of automatic options here.
            NotificationService.show("Still can't access that tab after refreshing it. Try switching to the tab manually once, then mention it again.");
        } else {
            NotificationService.show("Couldn't read that tab.");
        }
    } finally {
        inputEl.focus();
    }
}

/**
 * Auto-attach the current tab as context on side-panel open, so the user
 * doesn't have to manually @-mention the page they're already looking at
 * — that's the overwhelmingly common intent ("ask questions about this
 * page"). Takes the same plain {id, url, title, favIconUrl} shape that
 * getActiveTabInfo() in api.js already returns (main.js passes it
 * straight through), so no chrome.tabs.query call is needed here.
 *
 * Reuses the exact same extraction path as a manual @ pick, just without
 * touching the input box / dropdown (there's no mention text to remove
 * since the user didn't type anything).
 */
export async function autoMentionActiveTab(tab) {
    if (!tab || tab.id == null) return;
    if (!isScriptableTab(tab)) return; // e.g. chrome:// pages — nothing to extract
    if (mentionedTabs.some(t => t.tabId === tab.id)) return; // already mentioned somehow

    try {
        const content = await extractFullPageTextWithRetry(tab);
        if (!content) return; // silent — this is a convenience default, not a user action to error on

        mentionedTabs.push({
            tabId: tab.id,
            title: tab.title || tab.url || "Untitled tab",
            url: tab.url || "",
            favIconUrl: tab.favIconUrl || "",
            content,
        });
        showUsingBar({ animate: false }); // no entrance animation — it should just already be there
    } catch (err) {
        // Best-effort only. A silent failure here just means the user falls
        // back to manually @-mentioning the tab, same as before this feature.
        console.log("[Sicily Navigator] auto-mention of active tab failed", err);
    }
}

// ── "Using: collection" bar ──────────────────────────────────────────
// Single collection -> list-glyph icon + "Using Collection: <name>"
// 2+ collections     -> a single "stacked lists" glyph + "Using Multiple
//                        Collections" (collections have no favicon to
//                        stack, so instead of 3 duplicate icons we swap
//                        to one glyph that reads as "several lists").
const COLLECTION_ICON_SINGLE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h16"></path></svg>`;
const COLLECTION_ICON_MULTI = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="14" height="4" rx="1"></rect><rect x="6" y="10" width="14" height="4" rx="1"></rect><rect x="3" y="16" width="14" height="4" rx="1"></rect></svg>`;

let usingCollectionBarEl = null;

function ensureUsingCollectionBarEl() {
    if (usingCollectionBarEl) return usingCollectionBarEl;

    usingCollectionBarEl = document.createElement("div");
    usingCollectionBarEl.className = "using-tab-bar";

    const iconWrap = document.createElement("div");
    iconWrap.className = "using-tab-icon";
    iconWrap.innerHTML = COLLECTION_ICON_SINGLE;

    const label = document.createElement("div");
    label.className = "using-tab-label";

    const prefix = document.createElement("span");
    prefix.className = "using-tab-prefix";
    prefix.textContent = "Using Collection: ";

    const name = document.createElement("span");
    name.className = "using-tab-name";

    label.appendChild(prefix);
    label.appendChild(name);

    const closeBtn = document.createElement("div");
    closeBtn.className = "using-tab-close";
    closeBtn.innerHTML = "&times;";
    closeBtn.title = "Stop using collections";
    closeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        clearMentionedCollection();
    });

    usingCollectionBarEl.appendChild(iconWrap);
    usingCollectionBarEl.appendChild(label);
    usingCollectionBarEl.appendChild(closeBtn);

    usingCollectionBarEl.addEventListener("click", () => {
        if (mentionedCollections.length <= 1) return; // nothing to manage in a list of one
        openManageDropdown("#");
    });

    bottomDock.insertBefore(usingCollectionBarEl, inputRow);
    return usingCollectionBarEl;
}

function showUsingCollectionBar({ animate = true } = {}) {
    const el = ensureUsingCollectionBarEl();
    const iconWrap = el.querySelector(".using-tab-icon");
    const prefixEl = el.querySelector(".using-tab-prefix");
    const nameEl = el.querySelector(".using-tab-name");

    if (mentionedCollections.length === 1) {
        prefixEl.textContent = "Using Collection: ";
        nameEl.textContent = mentionedCollections[0].name || "Untitled Collection";
        iconWrap.innerHTML = COLLECTION_ICON_SINGLE;
    } else {
        prefixEl.textContent = "";
        nameEl.textContent = "Using Multiple Collections";
        el.title = mentionedCollections.map(c => c.name).join(", ");
        iconWrap.innerHTML = COLLECTION_ICON_MULTI;
    }

    el.classList.remove("using-tab-bar--born");
    el.classList.add("using-tab-bar--visible");
    if (animate) {
        void el.offsetWidth;
        el.classList.add("using-tab-bar--born");
    }
}

function hideUsingCollectionBar(animate = true) {
    if (!usingCollectionBarEl) return;
    if (!animate) {
        usingCollectionBarEl.classList.remove("using-tab-bar--visible", "using-tab-bar--born");
        return;
    }
    usingCollectionBarEl.classList.add("using-tab-bar--leaving");
    usingCollectionBarEl.classList.remove("using-tab-bar--visible");
    setTimeout(() => {
        usingCollectionBarEl?.classList.remove("using-tab-bar--leaving", "using-tab-bar--born");
    }, 320);
}

function selectCollectionForMention(collection) {
    removeMentionTextFromInput();
    closeDropdown();

    // Already mentioned — nothing to do here. Removing a mention only
    // happens via the × in the manage view (opened by clicking the bar).
    const alreadyMentioned = mentionedCollections.some(c => c.id === collection.id);
    if (!alreadyMentioned) {
        mentionedCollections.push({ id: collection.id, name: collection.name });
        showUsingCollectionBar({ animate: true });
    }
    inputEl.focus();
}

// ── Dropdown ─────────────────────────────────────────────────────────
function ensureDropdownEl() {
    if (dropdownEl) return dropdownEl;
    dropdownEl = document.createElement("div");
    dropdownEl.id = "mention-dropdown";
    dropdownEl.className = "mention-dropdown";
    bottomDock.appendChild(dropdownEl);
    return dropdownEl;
}

function renderDropdown(filter) {
    const el = ensureDropdownEl();
    const q = filter.trim().toLowerCase();

    let filtered = [];
    if (mentionType === "@") {
        filtered = q
            ? allTabsCache.filter(t => (t.title || "").toLowerCase().includes(q) || (t.url || "").toLowerCase().includes(q))
            : allTabsCache;
    } else {
        filtered = q
            ? allCollectionsCache.filter(c => (c.name || "").toLowerCase().includes(q))
            : allCollectionsCache;
    }

    el.innerHTML = "";

    if (filtered.length === 0) {
        const empty = document.createElement("div");
        empty.className = "mention-dropdown-empty";
        empty.textContent = mentionType === "@" ? "No matching tabs" : "No matching collections";
        el.appendChild(empty);
        el.classList.add("mention-dropdown--visible");
        return;
    }

    highlightedIndex = Math.min(highlightedIndex, filtered.length - 1);

    filtered.forEach((item, idx) => {
        const row = document.createElement("div");
        row.className = "mention-dropdown-item" + (idx === highlightedIndex ? " highlighted" : "");

        const icon = document.createElement("div");
        icon.className = "mention-dropdown-icon";

        if (mentionType === "@") {
            setTabIcon(icon, item.favIconUrl);
        } else {
            icon.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h16"></path></svg>`;
        }

        const textWrap = document.createElement("div");
        textWrap.className = "mention-dropdown-text";

        const titleEl = document.createElement("div");
        titleEl.className = "mention-dropdown-title";

        if (mentionType === "@") {
            titleEl.textContent = item.title || "(untitled)";
            titleEl.title = item.url || "";
        } else {
            titleEl.textContent = item.name;
        }

        textWrap.appendChild(titleEl);

        row.appendChild(icon);
        row.appendChild(textWrap);

        row.addEventListener("mousedown", (e) => {
            e.preventDefault();
            if (mentionType === "@") selectTabForMention(item);
            else selectCollectionForMention(item);
        });

        el.appendChild(row);
    });

    el._filteredTabs = filtered;
    el.classList.add("mention-dropdown--visible");

    const highlightedEl = el.querySelector(".mention-dropdown-item.highlighted");
    if (highlightedEl) {
        highlightedEl.scrollIntoView({ block: "nearest" });
    }
}

function closeDropdown() {
    mentionActive = false;
    mentionType = null;
    mentionStartIndex = -1;
    manageMode = false;
    if (dropdownEl) {
        dropdownEl.classList.remove("mention-dropdown--visible");
    }
}

async function openDropdown() {
    // Cancel any pending blur-triggered close — see the blur listener
    // below. Without this, a stale close scheduled from a moment ago
    // (e.g. focus briefly left the input) can fire ~120ms after this
    // dropdown opens and slam it shut right away.
    if (blurCloseTimer) {
        clearTimeout(blurCloseTimer);
        blurCloseTimer = null;
    }
    manageMode = false;
    highlightedIndex = 0;
    if (mentionType === "@") {
        try {
            const tabs = await chrome.tabs.query({});
            allTabsCache = tabs.filter(isScriptableTab);
        } catch (err) {
            console.error("Failed to query tabs:", err);
            allTabsCache = [];
        }
    } else if (mentionType === "#") {
        try {
            const res = await fetch(`http://${BACKEND_HOST}/collections`);
            if (res.ok) {
                const data = await res.json();
                allCollectionsCache = data.collections || [];
            }
        } catch (err) {
            console.error("Failed to fetch collections:", err);
            allCollectionsCache = [];
        }
    }
    renderDropdown("");
}

// ── "Manage" dropdown ────────────────────────────────────────────────
// Opened by clicking the "Using..." bar. Unlike the add-picker above,
// this shows ONLY the items already mentioned, each with a remove (×)
// control — no search, no adding, nothing else clickable. Tapping
// anywhere outside it just closes it (handled by the existing
// document-level outside-click listener).
let manageMode = false; // true while the manage (remove-only) list is open

function openManageDropdown(type) {
    if (blurCloseTimer) {
        clearTimeout(blurCloseTimer);
        blurCloseTimer = null;
    }
    manageMode = true;
    mentionActive = true;
    mentionType = type;
    mentionStartIndex = -1;
    highlightedIndex = -1; // nothing is keyboard-highlighted in manage mode
    renderManageDropdown();
    inputEl.focus();
}

function renderManageDropdown() {
    const el = ensureDropdownEl();
    const items = mentionType === "@" ? mentionedTabs : mentionedCollections;

    el.innerHTML = "";

    items.forEach((item) => {
        const row = document.createElement("div");
        // Deliberately NOT "mention-dropdown-item" — that class carries
        // hover/highlight/click affordances for the add-flow. This is a
        // plain, non-interactive row except for its remove button.
        row.className = "mention-manage-item";

        const icon = document.createElement("div");
        icon.className = "mention-dropdown-icon";
        if (mentionType === "@") {
            setTabIcon(icon, item.favIconUrl);
        } else {
            icon.innerHTML = COLLECTION_ICON_SINGLE;
        }

        const textWrap = document.createElement("div");
        textWrap.className = "mention-dropdown-text";
        const titleEl = document.createElement("div");
        titleEl.className = "mention-dropdown-title";
        if (mentionType === "@") {
            titleEl.textContent = item.title || "(untitled)";
            titleEl.title = item.url || "";
        } else {
            titleEl.textContent = item.name;
        }
        textWrap.appendChild(titleEl);

        const removeBtn = document.createElement("div");
        removeBtn.className = "mention-manage-remove";
        removeBtn.innerHTML = "&times;";
        removeBtn.title = mentionType === "@" ? "Remove this tab" : "Remove this collection";
        removeBtn.addEventListener("mousedown", (e) => {
            // Only this control is interactive in manage mode — the row
            // itself does nothing on click.
            e.preventDefault();
            e.stopPropagation();
            if (mentionType === "@") {
                removeMentionedTab(item.tabId);
            } else {
                removeMentionedCollection(item.id);
            }
        });

        row.appendChild(icon);
        row.appendChild(textWrap);
        row.appendChild(removeBtn);
        el.appendChild(row);
    });

    el._filteredTabs = []; // manage mode has no keyboard nav / Enter-to-pick
    el.classList.add("mention-dropdown--visible");
}

// Exported so main.js can drop a tab's mention when that same tab
// navigates elsewhere (its old content is now stale) before
// auto-mentioning it again on the new page. Internal callers (the ×
// button in the manage dropdown) keep using it exactly as before.
export function removeMentionedTab(tabId) {
    const idx = mentionedTabs.findIndex(t => t.tabId === tabId);
    if (idx === -1) return;
    mentionedTabs.splice(idx, 1);
    if (mentionedTabs.length) {
        showUsingBar({ animate: true });
        renderManageDropdown(); // refresh the open list in place
    } else {
        hideUsingBar(true);
        closeDropdown();
    }
}

function removeMentionedCollection(collectionId) {
    const idx = mentionedCollections.findIndex(c => c.id === collectionId);
    if (idx === -1) return;
    mentionedCollections.splice(idx, 1);
    if (mentionedCollections.length) {
        showUsingCollectionBar({ animate: true });
        renderManageDropdown();
    } else {
        hideUsingCollectionBar(true);
        closeDropdown();
    }
}

function removeMentionTextFromInput() {
    if (mentionStartIndex === -1) return;
    const value = inputEl.value;
    const caret = inputEl.selectionStart ?? value.length;
    // Remove from the "@" up to the current caret position (covers
    // whatever filter text the user typed after the "@").
    const before = value.slice(0, mentionStartIndex);
    const after = value.slice(caret);
    inputEl.value = before + after;
    const newCaret = before.length;
    inputEl.setSelectionRange(newCaret, newCaret);
    mentionActive = false;
    mentionType = null;
    mentionStartIndex = -1;
}

// ── Input wiring ─────────────────────────────────────────────────────
// ── Input wiring ─────────────────────────────────────────────────────
inputEl.addEventListener("input", () => {
    const value = inputEl.value;
    const caret = inputEl.selectionStart ?? value.length;

    if (!mentionActive) {
        const charBeforeCaret = value[caret - 1];
        if (charBeforeCaret === "@" || charBeforeCaret === "#") {
            const precedingChar = value[caret - 2];
            const startsToken = caret === 1 || precedingChar === undefined || /\s/.test(precedingChar);
            if (startsToken) {
                mentionActive = true;
                mentionType = charBeforeCaret;
                mentionStartIndex = caret - 1;
                openDropdown();
                return;
            }
        }
        return;
    }

    if (caret <= mentionStartIndex || value[mentionStartIndex] !== mentionType) {
        closeDropdown();
        return;
    }
    const filterText = value.slice(mentionStartIndex + 1, caret);
    if (/\s/.test(filterText)) {
        closeDropdown();
        return;
    }
    renderDropdown(filterText);
});

inputEl.addEventListener("keydown", (e) => {
    if (!mentionActive || !dropdownEl || !dropdownEl.classList.contains("mention-dropdown--visible")) {
        return;
    }
    const filtered = dropdownEl._filteredTabs || [];
    if (e.key === "ArrowDown") {
        e.preventDefault();
        if (filtered.length) {
            highlightedIndex = (highlightedIndex + 1) % filtered.length;
            renderDropdown(getCurrentFilterText());
        }
    } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (filtered.length) {
            highlightedIndex = (highlightedIndex - 1 + filtered.length) % filtered.length;
            renderDropdown(getCurrentFilterText());
        }
    } else if (e.key === "Enter") {
        if (filtered.length) {
            e.preventDefault();
            e.stopImmediatePropagation();
            const item = filtered[highlightedIndex];
            if (mentionType === "@") selectTabForMention(item);
            else selectCollectionForMention(item);
        }
    } else if (e.key === "Escape") {
        e.preventDefault();
        closeDropdown();
    }
});

function getCurrentFilterText() {
    const value = inputEl.value;
    const caret = inputEl.selectionStart ?? value.length;
    if (mentionStartIndex === -1) return "";
    return value.slice(mentionStartIndex + 1, caret);
}

inputEl.addEventListener("blur", () => {
    // Slight delay so a mousedown-selected dropdown item still registers.
    blurCloseTimer = setTimeout(() => {
        blurCloseTimer = null;
        closeDropdown();
    }, 120);
});

document.addEventListener("click", (e) => {
    if (!dropdownEl) return;
    const clickedInsideDropdown = dropdownEl.contains(e.target);
    const clickedInput = e.target === inputEl;
    // Clicking either "Using..." bar to REOPEN the picker also bubbles to
    // this document listener on the same tick. Without this check, the
    // picker would open and then immediately close again from this same
    // click. Exclude both bars from the "click outside" close.
    const clickedUsingBar = (usingBarEl && usingBarEl.contains(e.target))
        || (usingCollectionBarEl && usingCollectionBarEl.contains(e.target));
    if (!clickedInsideDropdown && !clickedInput && !clickedUsingBar) {
        closeDropdown();
    }
});
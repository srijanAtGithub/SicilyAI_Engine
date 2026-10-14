import { makeContextLabel } from "./features.js";
import { markdownToHtml } from "./markdown.js";

export const appWrap = document.getElementById("app-wrap");
export const messagesEl = document.getElementById("messages");
export const sendBtn = document.getElementById("send-btn");
export const disconnectedScreen = document.getElementById("disconnected-screen");

export function hideEmptyState() {
  const empty = document.getElementById("empty-state");
  if (empty) empty.classList.add("hidden");
}

export function showEmptyState() {
  const empty = document.getElementById("empty-state");
  if (empty) empty.classList.remove("hidden");
}

// Shared icon markup for any "copy to clipboard" button in the panel
// (both the per-message copy button and each code block's copy button).
const COPY_ICON = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M8 4V16C8 17.1046 8.89543 18 10 18H20C21.1046 18 22 17.1046 22 16V4C22 2.89543 21.1046 2 20 2H10C8.89543 2 8 2.89543 8 4Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
  <path d="M16 18V20C16 21.1046 15.1046 22 14 22H4C2.89543 22 2 21.1046 2 20V8C2 6.89543 2.89543 6 4 6H6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

const EDIT_ICON = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M12 20h9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

const CHECK_ICON = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M20 6L9 17L4 12" stroke="#34c759" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

// Injects a small "copy" button into the top-right corner of every fenced
// code block (<pre><code>...) inside a rendered AI message, so long code
// can be grabbed without selecting text by hand. Copies the code's exact
// text content (i.e. what's inside <code>, not any HTML around it), so
// indentation/newlines come out exactly as the model wrote them.
function addCodeBlockCopyButtons(container) {
  const blocks = container.querySelectorAll("pre");
  blocks.forEach((pre) => {
    pre.classList.add("code-block-wrap");

    const codeEl = pre.querySelector("code") || pre;

    const btn = document.createElement("button");
    btn.className = "code-copy-btn";
    btn.type = "button";
    btn.title = "Copy code";
    btn.innerHTML = COPY_ICON;

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const codeText = codeEl.textContent;
      navigator.clipboard.writeText(codeText).then(() => {
        btn.innerHTML = CHECK_ICON;
        setTimeout(() => {
          btn.innerHTML = COPY_ICON;
        }, 1500);
      }).catch((err) => console.error("Failed to copy code:", err));
    });

    pre.appendChild(btn);
  });
}

// ── Edit & resend (user messages) ─────────────────────────────────────
// main.js registers the handler that actually truncates history and
// re-sends; ui.js only owns the inline editor. The handler returns
// false to keep the editor open (e.g. socket not connected), anything
// else means "done, the bubble has been replaced".
let editHandler = null;
let activeEdit = null;

export function setEditHandler(fn) {
  editHandler = fn;
}

export function cancelActiveEdit() {
  if (activeEdit) activeEdit.cancel();
}

function startEditing(el, originalText) {
  if (activeEdit) activeEdit.cancel();

  // Keep the original nodes (text / collapsible head+body / buttons)
  // so Cancel can put the bubble back exactly as it was.
  const saved = Array.from(el.childNodes);
  el.classList.add("editing");

  const editor = document.createElement("div");
  editor.className = "msg-editor";

  const ta = document.createElement("textarea");
  ta.className = "msg-edit-input";
  ta.rows = 1;
  ta.value = originalText;

  const actions = document.createElement("div");
  actions.className = "msg-edit-actions";

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "msg-edit-btn";
  cancelBtn.textContent = "Cancel";

  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.className = "msg-edit-btn primary";
  submitBtn.textContent = "Send";

  actions.appendChild(cancelBtn);
  actions.appendChild(submitBtn);
  editor.appendChild(ta);
  editor.appendChild(actions);

  const autosize = () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight + 2, 200) + "px";
  };

  const close = () => {
    el.classList.remove("editing");
    el.replaceChildren(...saved);
    activeEdit = null;
  };

  const submit = async () => {
    const newText = ta.value.trim();
    if (!newText || !editHandler) return;
    submitBtn.disabled = true;
    const ok = await editHandler(el, newText);
    if (ok === false) {
      submitBtn.disabled = false;
    } else {
      activeEdit = null; // the handler has replaced this bubble
    }
  };

  ta.addEventListener("input", autosize);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  });
  cancelBtn.addEventListener("click", close);
  submitBtn.addEventListener("click", submit);

  activeEdit = { el, cancel: close };

  el.replaceChildren(editor);
  autosize();
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

// `meta` (optional, user messages only) lets main.js remember what was
// attached to this turn so an edit can re-send the same context:
//   { contextSnippets: string[]  // full text that goes to the backend
//     trailSnippets:   string[]  // what addContextTrail displayed }
// Returns the created element.
export function addMessage(text, role, meta = {}) {
  // Once there's a real message (user, ai, or even a system notice),
  // the "nothing here yet" placeholder no longer applies.[cite: 2]
  hideEmptyState();

  const el = document.createElement("div");
  el.className = `msg ${role}`;

  // Check if it is a long user message (e.g., > 150 chars or multiple line breaks)
  const isLong = text.length > 150 || text.split('\n').length > 3;

  if (role === "user" && isLong) {
    el.classList.add("collapsible");

    // 1. Header (Preview + Caret)
    const header = document.createElement("div");
    header.className = "msg-collapse-head";

    const preview = document.createElement("div");
    preview.className = "msg-collapse-preview";
    preview.textContent = text.substring(0, 60).replace(/\n/g, ' ') + "...";

    const caret = document.createElement("div");
    caret.className = "msg-collapse-caret";
    caret.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="6 9 12 15 18 9"></polyline>
    </svg>`;

    header.appendChild(preview);
    header.appendChild(caret);

    // 2. Expandable Body (Full Text)
    const body = document.createElement("div");
    body.className = "msg-collapse-body";

    const bodyInner = document.createElement("div");
    bodyInner.className = "msg-collapse-body-inner";

    const textEl = document.createElement("div");
    textEl.className = "msg-collapse-text";
    // User messages are never markdown-rendered (see note below) — this
    // branch only ever fires for long *user* text since AI bubbles skip
    // the collapsible treatment entirely (see isLong condition above).
    textEl.textContent = text;

    bodyInner.appendChild(textEl);
    body.appendChild(bodyInner);

    el.appendChild(header);
    el.appendChild(body);

    // Toggle expansion on click
    header.addEventListener("click", () => {
      el.classList.toggle("expanded");
    });
  } else if (role === "ai") {
    // AI replies are rendered as Markdown -> sanitized HTML (headings,
    // lists, code blocks, tables, etc). markdownToHtml() escapes all
    // literal text itself before adding any tag, so this is safe even
    // though the model's raw text could contain '<', '&', etc.
    el.classList.add("md-body");
    el.innerHTML = markdownToHtml(text);
    addCodeBlockCopyButtons(el);
  } else {
    // User/system messages are shown as plain text, not markdown-rendered:
    // markdown syntax someone actually typed (e.g. "what does * do in
    // regex?") shouldn't be reinterpreted as formatting instructions.
    el.textContent = text;
  }

  // Create the copy button[cite: 2]
  const copyBtn = document.createElement("button");
  copyBtn.className = "copy-btn";
  copyBtn.title = "Copy text";
  copyBtn.innerHTML = COPY_ICON;

  // Handle clipboard functionality[cite: 2]
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(text).then(() => {
      copyBtn.innerHTML = CHECK_ICON;
      setTimeout(() => {
        copyBtn.innerHTML = COPY_ICON;
      }, 1500);
    }).catch(err => console.error("Failed to copy text:", err));
  });

  el.appendChild(copyBtn);

  if (role === "user") {
    el._meta = meta;
    const editBtn = document.createElement("button");
    editBtn.className = "copy-btn edit-btn";
    editBtn.type = "button";
    editBtn.title = "Edit and resend";
    editBtn.innerHTML = EDIT_ICON;
    editBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      startEditing(el, text);
    });
    el.appendChild(editBtn);
  }

  messagesEl.appendChild(el);

  requestAnimationFrame(() => {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });

  return el;
}

export function addContextTrail(snippets) {
  if (!snippets || snippets.length === 0) return null;
  const trail = document.createElement("div");
  trail.className = "msg-context-trail";
  snippets.forEach((text) => {
    const chip = document.createElement("div");
    chip.className = "context-trail-chip";
    chip.title = text;
    const icon = document.createElement("span");
    icon.className = "context-trail-icon";
    icon.innerHTML = `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M4 3.5h9l3 3v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M13 3.5v3h3" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M6.5 10.5h7M6.5 13h7M6.5 8h3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
    </svg>`;
    const label = document.createElement("span");
    label.className = "context-trail-label";
    label.textContent = makeContextLabel(text);
    chip.appendChild(icon);
    chip.appendChild(label);
    trail.appendChild(chip);
  });
  messagesEl.appendChild(trail);
  requestAnimationFrame(() => {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });
  return trail;
}

export function clearMessagesUI() {
  // Wiping #messages would also nuke the #empty-state node living inside
  // it (it's markup in the DOM, not generated by JS), so pull it out
  // first and put it back afterwards instead of hardcoding its HTML here.
  const empty = document.getElementById("empty-state");
  messagesEl.innerHTML = "";
  if (empty) {
    empty.classList.remove("hidden");
    messagesEl.appendChild(empty);
  }
}

export function setStatus(state) {
  if (state === "connected") {
    showOnline();
  } else if (state === "disconnected") {
    showOffline();
  }
}

export function showOffline() {
  appWrap.classList.add("offline");
  disconnectedScreen.classList.add("visible");
}

export function showOnline() {
  appWrap.classList.remove("offline");
  disconnectedScreen.classList.remove("visible");
  disconnectedScreen.classList.remove("retrying");
}

let isSendingNow = false;

export function setSending(isSending) {
  isSendingNow = isSending;
  sendBtn.disabled = isSending;
  const icon = document.getElementById("send-icon");
  const spinner = document.getElementById("send-spinner");
  if (icon) icon.style.display = isSending ? "none" : "";
  if (spinner) spinner.style.display = isSending ? "inline-block" : "none";
  appWrap.classList.toggle("busy", isSending);
  syncSendBtn();
}

export const inputRow = document.getElementById("input-row");
export const inputBox = document.getElementById("input-box");

// Show the send button only when there's something to send
// (or while a send is in flight, so the spinner stays visible).
export function syncSendBtn() {
  const hasText = !!inputBox && inputBox.value.trim().length > 0;
  sendBtn.classList.toggle("revealed", hasText || isSendingNow);
}

if (inputBox) {
  inputBox.addEventListener("input", syncSendBtn);
}

// Initial state on load
syncSendBtn();
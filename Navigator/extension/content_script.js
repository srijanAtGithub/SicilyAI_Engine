/**
 * content_script.js
 * ------------------
 * Powers the "Edit with Navigator" right-click flow. This is a separate,
 * stateless feature from the tab-chat popup — no tab_id, no session
 * memory, no relationship to navigator_bridge.py's WebSocket/SessionStore
 * at all. One request, one response, done.
 *
 * Flow:
 *   1. User selects text and right-clicks (menu only shows when there IS
 *      a selection — see background.js's contexts: ["selection"]).
 *   2. background.js relays a "navigator-open-edit-box" message here.
 *      This script re-reads the live selection itself — background.js
 *      has no DOM access to inspect it.
 *   3. This script classifies what's selected into one of three tiers
 *      (see detectEditContext below) and renders a floating box near it,
 *      inside a Shadow DOM so the page's own CSS can't clash with it.
 *   4. On submit, it asks background.js to call the backend's
 *      POST /edit-selection (background.js does the actual fetch — see
 *      that file for why) and either splices the result back in, or —
 *      for text this script can't safely rewrite in place — shows it as
 *      a copyable suggestion instead.
 *
 * Three detection tiers (this is the "how aware can we realistically be"
 * question):
 *
 *   1. FORM FIELDS (<input>, <textarea>) — text lives in .value, a plain
 *      string, with .selectionStart/.selectionEnd. Fully readable and
 *      fully writable. Auto-applies.
 *
 *   2. PLAIN CONTENTEDITABLE — real DOM text nodes under an editable
 *      root (isContentEditable, which covers contenteditable="true",
 *      "", "plaintext-only", and inherited/designMode cases — using the
 *      browser's own resolved flag instead of hand-checking the
 *      attribute string). Fully readable via Range, fully writable via
 *      Range.deleteContents()/insertNode(). Auto-applies.
 *
 *   3. FRAMEWORK-MANAGED RICH TEXT (ProseMirror, Slate, Lexical,
 *      Draft.js, Quill, CKEditor, and similar — likely what claude.ai's
 *      own message box is) — technically contenteditable at the DOM
 *      level, but the framework owns an internal model and re-renders
 *      the DOM out from under any direct edits. Detected via common
 *      fingerprint classes/attributes each library leaves in the DOM.
 *      Reading the selected text still works fine (window.getSelection
 *      doesn't care who manages the DOM). Writing it back safely would
 *      need a framework-specific approach (synthetic input events the
 *      framework's own handler expects) — out of scope for this pass.
 *      So: read-only mode. We still call the backend and show the
 *      result, just as a copyable suggestion instead of an auto-apply.
 *
 *   Anything selected outside all of the above (plain read-only page
 *   text) gets the same copyable-suggestion treatment as tier 3, for
 *   the same reason — there's nothing to write back into.
 */

(() => {
  const OPEN_BOX_MESSAGE = "navigator-open-edit-box";
  const EDIT_REQUEST_MESSAGE = "navigator-edit-selection-request";

  if (!document.getElementById("navigator-highlight-style")) {
    const style = document.createElement("style");
    style.id = "navigator-highlight-style";
    // Using a nice transparent version of your #3a6df0 blue
    style.textContent = `::highlight(navigator-selection) { background-color: #6879a3ff !important; color: #ffffff !important; }`;
    document.head.appendChild(style);
  }

  let activeBox = null; // only one edit box open at a time

  // ── Tier 3 fingerprints ─────────────────────────────────────────────
  // Heuristic, not exhaustive — common conventions each library leaves
  // in the DOM. False negatives just fall through to tier-2 handling
  // (which will fail loudly if the framework rejects the direct edit,
  // rather than silently corrupting state, since text is only mutated
  // in the DOM subtree under the detected root).

  const FRAMEWORK_FINGERPRINTS = [
    { name: "ProseMirror", test: (root) => root.classList?.contains("ProseMirror") || !!root.closest?.(".ProseMirror") },
    { name: "Slate", test: (root) => root.hasAttribute?.("data-slate-editor") || !!root.querySelector?.("[data-slate-string]") },
    { name: "Lexical", test: (root) => root.hasAttribute?.("data-lexical-editor") || !!root.querySelector?.("[data-lexical-text]") },
    { name: "Draft.js", test: (root) => root.classList?.contains("DraftEditor-root") || !!root.closest?.(".DraftEditor-root") || !!root.querySelector?.('[data-contents="true"]') },
    { name: "Quill", test: (root) => root.classList?.contains("ql-editor") || !!root.closest?.(".ql-editor") },
    { name: "CKEditor", test: (root) => root.classList?.contains("ck-editor__editable") || !!root.closest?.(".ck-editor__editable") },
  ];

  function detectFramework(editableRoot) {
    for (const fp of FRAMEWORK_FINGERPRINTS) {
      try {
        if (fp.test(editableRoot)) return fp.name;
      } catch (e) {
        // A fingerprint test throwing shouldn't break detection of the
        // others — just skip it.
      }
    }
    return null;
  }

  // ── Figure out what's selected and how (or whether) to write back ──

  function isTextLikeInput(el) {
    const textTypes = ["text", "search", "url", "tel", "email", "password", ""];
    return textTypes.includes((el.getAttribute("type") || "").toLowerCase());
  }

  function findEditableAncestor(el) {
    // .isContentEditable is the browser's own resolved answer — it
    // already accounts for contenteditable="true"/""/"plaintext-only",
    // inheritance from a parent, and is false where explicitly
    // overridden — more robust than matching the attribute string
    // ourselves.
    let node = el;
    while (node && node !== document.body && node !== document.documentElement) {
      if (node.nodeType === Node.ELEMENT_NODE && node.isContentEditable) return node;
      node = node.parentElement;
    }
    return null;
  }

  // ── Word-boundary snapping ──────────────────────────────────────────
  // Users routinely drag-select from mid-word to mid-word. If we edit
  // exactly that ragged span, the leftover fragments ("e" + "ional" from
  // a selection that only grabbed "xcept" out of "exceptional") get left
  // behind on either side of the replacement. Snap both ends outward to
  // the nearest whole-word boundary before doing anything else with the
  // range, so what we read, highlight, and eventually replace is always
  // whole words — never fragments.
  const wordSegmenter = (typeof Intl !== "undefined" && Intl.Segmenter)
    ? new Intl.Segmenter(undefined, { granularity: "word" })
    : null;

  function isWordLike(segment) {
    // Intl.Segmenter marks each segment isWordLike: true for actual words,
    // false for whitespace/punctuation between them — exactly the split we
    // want, and it's Unicode/locale-aware unlike a \w regex.
    return segment.isWordLike;
  }

  function snapOffsetToWordBoundary(textNode, offset, direction) {
    const full = textNode.data;
    if (!wordSegmenter || !full) return offset;

    // direction: "start" pulls the offset back to the start of whatever
    // word it's inside of; "end" pushes it forward to the end of whatever
    // word it's inside of. If the offset already sits exactly on a
    // boundary (start or end of a word, or inside whitespace/punctuation),
    // it's left alone — only genuine mid-word offsets get moved.
    for (const seg of wordSegmenter.segment(full)) {
      const segStart = seg.index;
      const segEnd = seg.index + seg.segment.length;
      if (offset > segStart && offset < segEnd && isWordLike(seg)) {
        return direction === "start" ? segStart : segEnd;
      }
    }
    return offset;
  }

  function expandRangeToWordBoundaries(range) {
    try {
      const newRange = range.cloneRange();

      const startNode = newRange.startContainer;
      if (startNode.nodeType === Node.TEXT_NODE) {
        const newStart = snapOffsetToWordBoundary(startNode, newRange.startOffset, "start");
        newRange.setStart(startNode, newStart);
      }

      const endNode = newRange.endContainer;
      if (endNode.nodeType === Node.TEXT_NODE) {
        const newEnd = snapOffsetToWordBoundary(endNode, newRange.endOffset, "end");
        newRange.setEnd(endNode, newEnd);
      }

      return newRange;
    } catch (e) {
      // Any DOM range weirdness (e.g. non-text boundary nodes) — fall back
      // to the original, unexpanded range rather than breaking selection
      // entirely.
      return range;
    }
  }

  // ── Surrounding context extraction ──────────────────────────────────
  // Grabs a bounded amount of text around the selection so the backend
  // can match tone/register instead of rewriting the fragment in a
  // vacuum. Capped in characters (not words) since that's what actually
  // bounds prompt/token cost regardless of language. ~600 chars is
  // roughly a paragraph-and-a-bit either side — enough to establish
  // voice without ballooning the request.
  const CONTEXT_MAX_CHARS = 600;

  function extractSurroundingContext(selectionText, containerEl) {
    if (!containerEl) return "";

    // Walk up to a reasonably-sized block ancestor rather than reading
    // the whole page — a paragraph/list-item/cell/message-bubble is the
    // right unit of "surrounding" text; document.body would pull in nav
    // bars, sidebars, and unrelated content that pollutes tone-matching
    // more than it helps.
    const BLOCK_TAGS = new Set([
      "P", "DIV", "LI", "TD", "TH", "BLOCKQUOTE", "ARTICLE", "SECTION",
      "TEXTAREA", "PRE", "FIGCAPTION",
    ]);

    let block = containerEl;
    let hops = 0;
    while (
      block &&
      block !== document.body &&
      !BLOCK_TAGS.has(block.tagName) &&
      hops < 6
    ) {
      block = block.parentElement;
      hops++;
    }
    if (!block || block === document.body) block = containerEl;

    const blockText = (block.innerText || block.textContent || "").trim();
    if (!blockText) return "";

    // If the block itself is already short, just use the whole thing.
    if (blockText.length <= CONTEXT_MAX_CHARS) return blockText;

    // Otherwise, center a window of CONTEXT_MAX_CHARS on wherever the
    // selection sits inside the block text, so long blocks still surface
    // the locally relevant surrounding sentences rather than always the
    // start of the block.
    const idx = blockText.indexOf(selectionText);
    if (idx === -1) return blockText.slice(0, CONTEXT_MAX_CHARS);

    const half = Math.floor(CONTEXT_MAX_CHARS / 2);
    const start = Math.max(0, idx - half);
    const end = Math.min(blockText.length, idx + selectionText.length + half);
    return blockText.slice(start, end);
  }

  function detectEditContext() {
    // Tier 1: an <input>/<textarea> with an active selection. Note that
    // window.getSelection() does NOT see into form controls in Chrome —
    // their selection only shows up via selectionStart/selectionEnd on
    // the focused element — so this has to be checked separately from
    // everything below.
    const active = document.activeElement;
    if (
      active &&
      (active.tagName === "TEXTAREA" || (active.tagName === "INPUT" && isTextLikeInput(active)))
    ) {
      if (typeof active.selectionStart === "number" && active.selectionStart !== active.selectionEnd) {
        const { start, end } = expandFieldSelectionToWordBoundaries(
          active.value, active.selectionStart, active.selectionEnd
        );
        return {
          tier: "form",
          element: active,
          start,
          end,
          text: active.value.slice(start, end),
          rect: active.getBoundingClientRect(),
          pageContext: extractSurroundingContext(active.value.slice(start, end), active),
        };
      }
    }

    function expandFieldSelectionToWordBoundaries(value, start, end) {
      if (!wordSegmenter || !value) return { start, end };

      let newStart = start;
      let newEnd = end;

      for (const seg of wordSegmenter.segment(value)) {
        const segStart = seg.index;
        const segEnd = seg.index + seg.segment.length;
        if (isWordLike(seg)) {
          if (start > segStart && start < segEnd) newStart = segStart;
          if (end > segStart && end < segEnd) newEnd = segEnd;
        }
      }

      return { start: newStart, end: newEnd };
    }

    // Tiers 2/3/readonly all start from a real window selection.
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
      return null;
    }

    const rawRange = selection.getRangeAt(0);
    const range = expandRangeToWordBoundaries(rawRange);
    const text = range.toString();
    const rect = range.getBoundingClientRect();
    const container = range.commonAncestorContainer;
    const el = container.nodeType === Node.TEXT_NODE ? container.parentElement : container;

    const editableRoot = (el && findEditableAncestor(el))
      || (document.designMode === "on" ? document.body : null);

    if (!editableRoot) {
      // Selected, but nothing editable underneath it — ordinary page text.
      return { tier: "readonly", text, rect, range: range.cloneRange(), pageContext: extractSurroundingContext(text, el) };
    }

    const framework = detectFramework(editableRoot);
    if (framework) {
      return { tier: "rich-text", frameworkName: framework, text, rect, range: range.cloneRange(), pageContext: extractSurroundingContext(text, editableRoot) };
    }

    return {
      tier: "contenteditable",
      range: range.cloneRange(),
      root: editableRoot,
      text,
      rect,
      pageContext: extractSurroundingContext(text, editableRoot),
    };
  }

  // Helper: Briefly flash a subtle highlight on an element to signal a change
  function pulseElement(el) {
    if (!el) return;
    const originalTransition = el.style.transition;
    const originalBg = el.style.backgroundColor;

    el.style.transition = "background-color 0.15s ease-in-out";
    el.style.backgroundColor = "rgba(58, 109, 240, 0.15)"; // Soft blue pulse

    setTimeout(() => {
      el.style.transition = "background-color 0.8s ease-out";
      el.style.backgroundColor = originalBg;

      setTimeout(() => {
        el.style.transition = originalTransition;
        if (!originalBg) el.style.removeProperty("background-color");
      }, 800);
    }, 200);
  }

  // Smoothly apply edits with an adaptive typewriter animation
  function applyEdit(context, newText) {
    if (!newText) return;

    // ── TIER 1: Standard Form Fields (<input>, <textarea>) ─────────────────────
    if (context.tier === "form" && context.element) {
      const el = context.element;
      const start = el.selectionStart || 0;
      const end = el.selectionEnd || 0;
      const oldVal = el.value;

      const prefix = oldVal.substring(0, start);
      const suffix = oldVal.substring(end);

      let charIndex = 0;
      const speed = Math.max(8, Math.floor(350 / newText.length));

      const timer = setInterval(() => {
        charIndex += Math.max(1, Math.floor(newText.length / 25));
        if (charIndex >= newText.length) {
          charIndex = newText.length;
          clearInterval(timer);
          pulseElement(el);
        }

        const currentSlice = newText.substring(0, charIndex);
        el.value = prefix + currentSlice + suffix;

        const newCursorPos = prefix.length + currentSlice.length;
        el.setSelectionRange(newCursorPos, newCursorPos);

        el.dispatchEvent(new Event("input", { bubbles: true }));
      }, speed);

      el.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }

    // ── TIER 2 & 3: ContentEditable & Rich-Text Frameworks (Notion, Slack, etc.) ──
    if ((context.tier === "contenteditable" || context.tier === "rich-text") && context.range) {
      const selection = window.getSelection();
      if (!selection) return;

      // 1. Force focus back to the target element if it slipped away
      if (context.root && typeof context.root.focus === "function") {
        context.root.focus();
      }

      // 2. Re-select the exact text block the user highlighted
      selection.removeAllRanges();
      selection.addRange(context.range);

      let charIndex = 0;
      const speed = Math.max(8, Math.floor(350 / newText.length));
      const chunkSize = Math.max(1, Math.floor(newText.length / 25));

      // 3. Clear out the highlighted text using the browser's native command
      // This alerts frameworks (like ProseMirror/Lexical) that an active deletion happened
      document.execCommand("delete", false);

      const timer = setInterval(() => {
        if (charIndex >= newText.length) {
          clearInterval(timer);
          // Highlight target: use root container or fallback to where the cursor is resting
          pulseElement(context.root || selection.anchorNode?.parentElement);
          return;
        }

        const nextChunk = newText.substring(charIndex, charIndex + chunkSize);
        charIndex += chunkSize;

        // 4. Stream chunks directly at the cursor position
        // The editor's virtual DOM engine intercepts this and updates safely!
        document.execCommand("insertText", false, nextChunk);

        // Throw an explicit input event at the container to cover reactive bindings
        if (context.root) {
          context.root.dispatchEvent(new Event("input", { bubbles: true }));
        }
      }, speed);

      return;
    }
  }

  // ── Floating box (Shadow DOM keeps page CSS from leaking in/out) ──────

  function closeActiveBox() {
    if (!activeBox) return;

    if (activeBox.onScroll) {
      window.removeEventListener("scroll", activeBox.onScroll, true);
    }

    activeBox.host.remove();
    document.removeEventListener("keydown", activeBox.onKeydown, true);
    document.removeEventListener("mousedown", activeBox.onOutsideClick, true);

    if (CSS.highlights) {
      CSS.highlights.delete("navigator-selection");
    }

    activeBox = null;
  }

  function tierNotice(context) {
    if (context.tier === "readonly") {
      return "Static text detected — edits will show as a copyable result.";
    }
    // We removed the rich-text warning entirely because our new 
    // execCommand trick natively handles it now!
    return null;
  }

  function openEditBox(context) {
    closeActiveBox();

    // Apply the highlight if a DOM range exists
    if (CSS.highlights && context.range) {
      const highlight = new Highlight(context.range);
      CSS.highlights.set("navigator-selection", highlight);
    }

    const host = document.createElement("div");
    host.style.position = "fixed";
    host.style.zIndex = "2147483647"; // Stay above everything
    host.style.transition = "opacity 0.3s ease"; // Initial fade-in
    document.body.appendChild(host);

    const shadow = host.attachShadow({ mode: "open" });
    const notice = tierNotice(context);

    shadow.innerHTML = `
      <style>
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Helvetica, sans-serif; }

        /* --- Main Premium Container (Apple Style) --- */
        .wrap {
          width: 380px;
          border-radius: 18px; /* Classic smooth Apple corner */
          border: 1px solid rgba(255, 255, 255, 0.18); /* Subtle edge definition */
          
          /* Glassmorphism Effect */
          backdrop-filter: blur(20px); 
          -webkit-backdrop-filter: blur(20px); /* Safari support */
          background: rgba(25, 25, 26, 0.65); /* Premium Dark Mode Gray */
          
          /* The "Shader" / Glowing Border Effect */
          box-shadow: 
            0 12px 40px rgba(0, 0, 0, 0.45),      /* Depth Shadow */
            0 0 20px rgba(94, 92, 230, 0.2),      /* Subtle Inner Blue Glow */
            inset 0 0 1px rgba(255, 255, 255, 0.1); /* Crisp Inner Edge */

          overflow: hidden; /* Contains the height animation */
          position: relative; /* For the neural glow overlays */
          opacity: 0;
          transform: translateY(10px) scale(0.98); /* Start state for entry animation */
          transition: 
            height 0.45s cubic-bezier(0.25, 1, 0.5, 1), /* Smooth Height Animation */
            opacity 0.3s ease-out, 
            transform 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }

        /* Animate in the window */
        .wrap.ready {
          opacity: 1;
          transform: translateY(0) scale(1);
        }

        /* Content Container (holds views) */
        .content {
          position: relative;
        }

        /* Common Styling for Input and Result Views (Cinematic slide-and-fade) */
        .view {
          position: absolute;
          width: 100%;
          top: 0;
          opacity: 0;
          transform: translateY(12px) scale(0.97);
          filter: blur(5px);
          transition: 
            opacity 0.45s cubic-bezier(0.16, 1, 0.3, 1), 
            transform 0.45s cubic-bezier(0.16, 1, 0.3, 1),
            filter 0.45s cubic-bezier(0.16, 1, 0.3, 1);
          pointer-events: none; /* Block interactions when hidden */
          display: block; /* Use block, control visibility via opacity */
        }

        .view.active {
          position: relative; /* Take up space for height calculation */
          opacity: 1;
          transform: translateY(0) scale(1);
          filter: blur(0px);
          pointer-events: auto; /* Enable interaction */
        }

        /* --- Full Window Scanning Scanline --- */
        .wrap::before {
          content: '';
          position: absolute;
          inset: 0;
          background: linear-gradient(to bottom, transparent, rgba(130, 240, 255, 0.15), transparent);
          transform: translateY(-100%);
          pointer-events: none;
          z-index: 5;
          opacity: 0;
          transition: opacity 0.3s ease;
        }
        .wrap.busy::before {
          opacity: 1;
          animation: scanLine 2s cubic-bezier(0.4, 0, 0.2, 1) infinite;
        }

        @keyframes scanLine {
          0% { transform: translateY(-100%); }
          100% { transform: translateY(100%); }
        }

        /* --- Neural Glow Edge (Rotating Inner Border) & full container color drift --- */
        .neural-glow {
          position: absolute;
          inset: 0;
          border-radius: 18px;
          pointer-events: none;
          opacity: 0;
          /* Animated color drift when busy */
          background: radial-gradient(circle at 20% 30%, rgba(94, 92, 230, 0.25), transparent 50%),
                      radial-gradient(circle at 80% 70%, rgba(249, 81, 165, 0.25), transparent 50%);
          mix-blend-mode: screen;
          transition: opacity 0.5s ease;
          z-index: 1;
        }
        .wrap.busy .neural-glow { 
          opacity: 1; 
          animation: neuralPulse 3s ease-in-out infinite alternate;
        }

        @keyframes neuralPulse {
          0% { filter: hue-rotate(0deg); }
          100% { filter: hue-rotate(90deg); }
        }

        /* --- Outer Radiant Glow Backdrop (Escapes overflow: hidden) --- */
        .glow-backdrop {
          position: absolute;
          top: 0; left: 0; right: 0; bottom: 0;
          border-radius: 18px;
          pointer-events: none; /* Let clicks pass straight through */
          z-index: -1; /* Render behind the main panel */
          opacity: 0;
          transition: opacity 0.4s ease;
        }

        /* When the panel is busy, ignite the backdrop with synced rotation and breathing */
        /*UNCOMMENT THE FOLLOWING TO HAVE THE BORDER GLOW AS WELL*/
        /*
        .wrap.busy ~ .glow-backdrop {
          opacity: 1;
          animation: 
            glowRotate 2s linear infinite, 
            radiantPulse 1.5s ease-in-out infinite alternate;
        }
        */

        @keyframes glowRotate { 
          100% { filter: hue-rotate(360deg); } 
        }

        /* Subtle, elegant, close-to-the-border glow */
        @keyframes radiantPulse {
          0% {
            box-shadow: 
              0 0 15px rgba(94, 92, 230, 0.4),      /* Tight inner purple */
              0 0 30px rgba(130, 240, 255, 0.2),     /* Soft middle cyan */
              0 0 50px rgba(94, 92, 230, 0.08);     /* Barely visible outer ambient */
          }
          100% {
            box-shadow: 
              0 0 25px rgba(94, 92, 230, 0.6),      /* Tight hot purple */
              0 0 45px rgba(130, 240, 255, 0.35),    /* Medium cyan bloom */
              0 0 75px rgba(94, 92, 230, 0.15);     /* Gentle outer glow */
          }
        }

        /* Tier Notice (Blurred Golden Style) */
        .notice {
          font-size: 11px;
          color: rgba(230, 185, 90, 0.9);
          background: rgba(230, 185, 90, 0.15); /* Translucent gold background */
          backdrop-filter: blur(10px);
          padding: 8px 12px;
          display: ${notice ? "block" : "none"};
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
          line-height: 1.4;
          font-weight: 500;
        }

        /* --- STATE 1: Input View --- */
        .input-view {
          display: flex;
          flex-direction: column; /* Stacks the text area and buttons vertically */
          padding: 10px;
          gap: 8px;
        }
        textarea {
          width: 100%; /* Reclaims all horizontal space */
          height: 36px;
          min-height: 36px;
          max-height: 160px;
          padding: 8px 12px;
          border-radius: 10px;
          
          /* Removed standard border, replaced with a near-invisible 4% opacity lining */
          border: 1px solid rgba(255, 255, 255, 0.04); 
          background: rgba(10, 10, 12, 0.45); /* Seamlessly integrates with the glass base */
          
          color: #f2f2f7;
          font-size: 13.5px;
          outline: none;
          resize: none;
          font-family: inherit;
          box-sizing: border-box;
          line-height: 1.5;
          overflow-y: auto;
          scrollbar-width: none; /* Firefox */
          -ms-overflow-style: none; /* old Edge/IE */
          
          /* Added smooth ease-out for a gradual glow transition */
          transition: border-color 0.25s ease-out, box-shadow 0.25s ease-out, opacity 0.3s ease;
        }
        textarea::-webkit-scrollbar { display: none; } /* Chrome/Safari */

        textarea:focus { 
          /* Ultra-fine pink boundary line */
          border-color: rgba(249, 81, 165, 0.3); 
          
          /* Fully covers the bounding box with a soft pink ambient glow */
          box-shadow: 0 0 14px rgba(255, 105, 180, 0.25); 
        }

        /* --- Action Button Container (Grid Stacking) --- */
        .action-btns {
          display: grid; /* Grid allows children to stack perfectly on top of each other */
          align-self: flex-end; 
          transition: opacity 0.3s cubic-bezier(0.16, 1, 0.3, 1), transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .btn-group {
          grid-area: 1 / 1; /* Forces both groups into the exact same cell */
          display: flex;
          gap: 6px;
          justify-content: flex-end;
          transition: opacity 0.4s cubic-bezier(0.16, 1, 0.3, 1), 
                      transform 0.4s cubic-bezier(0.16, 1, 0.3, 1),
                      filter 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }

        /* --- Default State: Summarize & Rewrite --- */
        .group-default {
          opacity: 1;
          transform: translateY(0) scale(1);
          pointer-events: auto;
          filter: blur(0px);
        }
        
        /* The incoming state waits slightly below and blurred */
        .group-typing {
          opacity: 0;
          transform: translateY(8px) scale(0.95);
          pointer-events: none;
          filter: blur(3px);
        }

        /* --- Typing State: Ask & Edit (Triggered by .has-text) --- */
        /* Default buttons float up and blur out */
        .action-btns.has-text .group-default {
          opacity: 0;
          transform: translateY(-8px) scale(0.95);
          pointer-events: none;
          filter: blur(3px);
        }
        
        /* Typing buttons snap into focus */
        .action-btns.has-text .group-typing {
          opacity: 1;
          transform: translateY(0) scale(1);
          pointer-events: auto;
          filter: blur(0px);
        }

        /* --- Hide smoothly when backend is busy --- */
        .wrap.busy .action-btns {
          opacity: 0;
          transform: translateY(12px);
          pointer-events: none;
        }
        .wrap.busy textarea {
          opacity: 0.4;
          pointer-events: none;
        }

        /* --- Action & Submission Buttons (Uniform Glassmorphism) --- */
        .submit-btn, .btn {
          padding: 9px 14px;
          border-radius: 10px;
          border: 1px solid rgba(255, 255, 255, 0.08); /* Crisp visible border */
          font-size: 13.5px;
          font-weight: 600;
          cursor: pointer;
          backdrop-filter: blur(20px);
          -webkit-backdrop-filter: blur(20px);
          transition: transform 0.1s, background-color 0.15s;
          box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
        }

        /* All Action Buttons - Ultra Translucent Gray */
        .btn-ask, .btn-edit, .btn-replace, .btn-summarise, .btn-rewrite {
          background: rgba(255, 255, 255, 0.06);
          color: #f2f2f7;
        }

        /* Shared Hover Effect */
        .btn-ask:hover:not(:disabled),
        .btn-edit:hover:not(:disabled),
        .btn-replace:hover,
        .btn-summarise:hover:not(:disabled),
        .btn-rewrite:hover:not(:disabled) { 
          background: rgba(255, 255, 255, 0.12);
        }

        .status {
          font-size: 11px;
          color: #ff453a; /* Apple Red */
          padding: 0 12px 10px;
          display: none;
          font-weight: 500;
        }

        /* --- STATE 2: Result Preview View --- */
        .result-view {
          padding: 16px;
        }
        .result-text {
          font-size: 14.5px;
          color: #f2f2f7;
          line-height: 1.6;
          max-height: 280px; 
          overflow-y: auto;
          white-space: pre-wrap;
          word-wrap: break-word;
          margin-bottom: 16px;
          padding-right: 8px;
          scrollbar-width: none; /* Firefox */
          -ms-overflow-style: none; /* old Edge/IE */
        }
        .result-text::-webkit-scrollbar { display: none; } /* Chrome/Safari */

        .actions {
          display: flex;
          justify-content: flex-end;
          gap: 10px;
        }

        /* The Copy & Cancel Buttons - Standard Translucent Gray */
        .btn-copy, .btn-cancel {
          background: rgba(120, 120, 128, 0.2);
          color: #f2f2f7;
        }
        .btn-copy:hover, .btn-cancel:hover { 
          background: rgba(120, 120, 128, 0.3); 
        }

        /* Global Button Interactions */
        .submit-btn:active:not(:disabled), .btn:active { transform: scale(0.96); }
        .submit-btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn-success { background: #34c759 !important; border-color: transparent !important; }

        /* --- Follow-up row (result view) --- */
        .followup-row {
          display: flex;
          gap: 8px;
          align-items: flex-end;
          margin-top: 12px;
          padding-top: 12px;
          border-top: 1px solid rgba(255, 255, 255, 0.08);
        }
        .followup-input {
          flex: 1;
          height: 36px;
          min-height: 36px;
          max-height: 120px;
          padding: 8px 12px;
          border-radius: 10px;
          border: 1px solid rgba(255, 255, 255, 0.04);
          background: rgba(10, 10, 12, 0.45);
          color: #f2f2f7;
          font-size: 13.5px;
          outline: none;
          resize: none;
          font-family: inherit;
          box-sizing: border-box;
          line-height: 1.5;
          overflow-y: auto;
          scrollbar-width: none; /* Firefox */
          -ms-overflow-style: none; /* old Edge/IE */
          transition: border-color 0.25s ease-out, box-shadow 0.25s ease-out;
        }
        .followup-input::-webkit-scrollbar { display: none; } /* Chrome/Safari */
        .followup-input:focus {
          border-color: rgba(249, 81, 165, 0.3);
          box-shadow: 0 0 14px rgba(255, 105, 180, 0.25);
        }
        .btn-followup {
          background: rgba(255, 255, 255, 0.06);
          color: #f2f2f7;
          flex-shrink: 0;
        }
        .btn-followup:hover:not(:disabled) { background: rgba(255, 255, 255, 0.12); }
        .wrap.busy .followup-row { opacity: 0.4; pointer-events: none; }
      </style>

      <div class="wrap">
        <div class="neural-glow"></div>
        <div class="notice" style="display: none;"></div>
        <div class="content">
          <div class="view input-view active">
            <textarea placeholder="Ask or describe edit..." rows="1"></textarea>
            <div class="action-btns">
              <!-- Default state buttons -->
              <div class="btn-group group-default">
                <button class="submit-btn btn-summarise">Summarize</button>
                <button class="submit-btn btn-rewrite">Rewrite</button>
              </div>
              <!-- Typing state buttons -->
              <div class="btn-group group-typing">
                <button class="submit-btn btn-ask">Ask</button>
                <button class="submit-btn btn-edit">Edit</button>
              </div>
            </div>
          </div>
          
          <div class="status"></div>
          
          <div class="view result-view">
            <div class="result-text"></div>
            <div class="actions">
              <button class="btn btn-copy">Copy</button>
              <button class="btn btn-replace">Replace</button>
            </div>
            <div class="followup-row">
              <textarea class="followup-input" placeholder="Ask a follow-up..." rows="1"></textarea>
              <button class="submit-btn btn-followup">Send</button>
            </div>
          </div>
        </div>
      </div>

      <div class="glow-backdrop"></div>
    `;

    // Elements
    const hostEl = host;
    const wrapEl = shadow.querySelector(".wrap");
    const inputView = shadow.querySelector(".input-view");
    const resultView = shadow.querySelector(".result-view");
    const input = shadow.querySelector(".input-view textarea");
    const actionBtns = shadow.querySelector(".action-btns");
    const askBtn = shadow.querySelector(".btn-ask");
    const editBtn = shadow.querySelector(".btn-edit");
    const summariseBtn = shadow.querySelector(".btn-summarise");
    const rewriteBtn = shadow.querySelector(".btn-rewrite");
    const status = shadow.querySelector(".status");
    const noticeEl = shadow.querySelector(".notice");
    const resultText = shadow.querySelector(".result-text");
    const replaceBtn = shadow.querySelector(".btn-replace");
    const copyBtn = shadow.querySelector(".btn-copy");
    const followupInput = shadow.querySelector(".followup-input");
    const followupBtn = shadow.querySelector(".btn-followup");

    let pendingAiText = "";

    // Turn history for THIS open box only. Lives entirely in this
    // closure — created fresh every time openEditBox() runs, and gone
    // the instant the box closes (host.remove() in closeActiveBox()
    // releases this whole scope). Never written anywhere, never reused
    // across selections or reloads. This is what lets the user ask a
    // follow-up ("shorter", "more formal", "no actually undo that")
    // without the model losing context, while keeping the feature
    // exactly as stateless as before at the box-lifetime boundary.
    const conversationHistory = [];

    // The action_type of the very first submission in this box. Follow-
    // ups keep using this so the system persona (ask/edit/rewrite/
    // summarise) stays consistent for the life of the box rather than
    // flip-flopping turn to turn.
    let initialActionType = null;

    // Helper for live bounds of target element
    function getFreshRect() {
      if (context.tier === "form" && context.element) {
        return context.element.getBoundingClientRect();
      } else if (context.range) {
        return context.range.getBoundingClientRect();
      }
      return context.rect;
    }

    // --- Smart Position Calculation ---
    function reposition() {
      const rect = getFreshRect();
      if (!rect) return;

      const boxWidth = 380;
      let left = rect.left;
      if (left + boxWidth > window.innerWidth) {
        left = window.innerWidth - boxWidth - 16;
      }
      hostEl.style.left = `${Math.max(8, left)}px`;

      const boxHeight = wrapEl.offsetHeight;
      const spaceBelow = window.innerHeight - rect.bottom;

      if (spaceBelow < boxHeight + 12 && rect.top > boxHeight + 12) {
        hostEl.style.top = `${rect.top - boxHeight - 8}px`;
      } else {
        hostEl.style.top = `${rect.bottom + 6}px`;
      }
    }

    // Perform initial repositioning
    reposition();

    // Trigger entry animation
    requestAnimationFrame(() => wrapEl.classList.add("ready"));

    // --- Smooth Height Lerping Mechanism ---
    function animateHeightChange(targetStateFn) {
      // 1. Measure current exact height (starting point)
      const startHeight = wrapEl.offsetHeight;

      // 2. Add an explicit inline height to force a transition starting point
      wrapEl.style.height = `${startHeight}px`;

      // 3. Immediately apply the DOM changes (this happens invisibly behind the fixed height)
      targetStateFn();

      // 4. Force a browser layout recalculation (critical for next step)
      // Reading offsetHeight triggers a synchronous reflow
      const _forcedReflow = wrapEl.offsetHeight;

      // 5. Measure the final desired scroll height of the new content
      const endHeight = wrapEl.scrollHeight;

      // 6. Set the height to the end point to trigger the CSS transition
      wrapEl.style.height = `${endHeight}px`;

      // 7. Cleanup after the animation finishes
      function cleanupTransition() {
        wrapEl.style.height = ""; // Restore to 'auto' so it can resize with text
        wrapEl.removeEventListener("transitionend", cleanupTransition);
        reposition(); // Perform collision check after new layout is stable
      }
      wrapEl.addEventListener("transitionend", cleanupTransition);
    }

    function showError(msg) {
      status.textContent = msg;
      status.style.display = "block";
      animateHeightChange(() => { }); // Re-calc height with error text visible
    }

    function setBusy(isBusy) {
      askBtn.disabled = isBusy;
      editBtn.disabled = isBusy;
      input.disabled = isBusy;
      followupBtn.disabled = isBusy;
      followupInput.disabled = isBusy;

      // Toggle neural glow/border shaders
      if (isBusy) {
        wrapEl.classList.add("busy");
      } else {
        wrapEl.classList.remove("busy");
      }
    }

    // Morph the window from Input to Preview mode with animation
    function showPreviewMode(text, actionType) {
      pendingAiText = text;

      // Use the smooth lerp engine
      animateHeightChange(() => {
        // Toggle active views
        inputView.classList.remove("active");
        status.style.display = "none";

        resultText.textContent = text;
        resultView.classList.add("active");

        // Show the warning ONLY if the user tried to execute an 'edit' on static text
        if (actionType === "edit" && context.tier === "readonly") {
          noticeEl.textContent = "Static text detected — edits will show as a copyable result.";
          noticeEl.style.display = "block";
        } else {
          noticeEl.style.display = "none";
        }

        // Hide the replace button if the field is read-only OR if the action is an 'ask'
        if (context.tier === "readonly" || actionType === "ask") {
          replaceBtn.style.display = "none";
        } else {
          replaceBtn.style.display = ""; // Restores the default button layout for regular edits
        }
      });

      // Let the user immediately keep the conversation going without an
      // extra click — this is the whole point of not dead-ending here.
      followupInput.focus();
    }

    function submit(actionType, instructionOverride) {
      let instruction = instructionOverride !== undefined
        ? instructionOverride.trim()
        : input.value.trim();

      // Allow 'summarise' and 'rewrite' to proceed even if the text box is empty
      if (!instruction) {
        if (actionType === "summarise") {
          instruction = "Summarise this text.";
        } else if (actionType === "rewrite") {
          instruction = "Rewrite this professionally.";
        } else {
          // If it's 'ask'/'edit'/a follow-up, we still strictly require user input
          return;
        }
      }

      setBusy(true);
      status.style.display = "none";

      chrome.runtime.sendMessage(
        {
          type: "navigator-edit-selection-request",
          selected_text: context.text,
          instruction,
          action_type: actionType,
          surrounding_context: context.pageContext || "",
          history: conversationHistory
        },
        (response) => {
          setBusy(false);

          if (chrome.runtime.lastError) {
            showError("Couldn't reach the extension backend.");
            return;
          }
          if (!response || !response.ok) {
            showError((response && response.error) || "Action failed.");
            return;
          }

          // Record this turn so a follow-up (still within this same
          // open box) has the context. Nothing here survives the box
          // closing — see conversationHistory's declaration above.
          if (initialActionType === null) initialActionType = actionType;

          conversationHistory.push({ role: "user", text: instruction });
          conversationHistory.push({ role: "ai", text: response.edited_text });

          if (instructionOverride !== undefined) {
            followupInput.value = "";
            followupInput.style.height = "auto";
          } else {
            input.value = "";
            input.style.height = "auto";
            actionBtns.classList.remove("has-text");
          }

          showPreviewMode(response.edited_text, actionType);
        }
      );
    }

    askBtn.addEventListener("click", () => submit("ask"));
    editBtn.addEventListener("click", () => submit("edit"));
    summariseBtn.addEventListener("click", () => submit("summarise"));
    rewriteBtn.addEventListener("click", () => submit("rewrite"));

    // Follow-up flow: reuses whatever action_type started this box (the
    // system persona doesn't change mid-conversation), just supplies a
    // new instruction each time. History (built up above) is what gives
    // the model the thread to follow.
    function submitFollowup() {
      followupBtn.disabled || submit(initialActionType || "ask", followupInput.value);
    }

    followupBtn.addEventListener("click", submitFollowup);

    followupInput.addEventListener("input", () => {
      followupInput.style.height = "auto";
      followupInput.style.height = `${Math.min(followupInput.scrollHeight, 120)}px`;
    });

    followupInput.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submitFollowup();
      }
      if (e.key === "Escape") closeActiveBox();
    });
    followupInput.addEventListener("keyup", (e) => e.stopPropagation());
    followupInput.addEventListener("keypress", (e) => e.stopPropagation());

    // Smooth auto-grow mechanic and state toggle for the buttons
    input.addEventListener("input", () => {
      // Auto-grow
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 160)}px`;

      if (input.value.trim().length > 0) {
        actionBtns.classList.add("has-text");
      } else {
        actionBtns.classList.remove("has-text");
      }
    });

    input.addEventListener("keydown", (e) => {
      e.stopPropagation();

      if (e.key === "Enter") {
        if (e.shiftKey) {
          // Allow Shift+Enter to naturally drop to the next line.
          // Force a slight timeout recalculation to expand the UI frame dynamically.
          setTimeout(() => {
            input.style.height = "auto";
            input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
          }, 0);
        } else if (e.ctrlKey || e.metaKey) {
          // Ctrl+Enter or Cmd+Enter triggers the "Edit" flow
          e.preventDefault();
          submit("edit");
        } else {
          // Regular Enter triggers the primary "Ask" flow
          e.preventDefault();
          submit("ask");
        }
      }
      if (e.key === "Escape") closeActiveBox();
    });

    input.addEventListener("keyup", (e) => e.stopPropagation());
    input.addEventListener("keypress", (e) => e.stopPropagation());

    replaceBtn.addEventListener("click", () => {
      if (pendingAiText) {
        applyEdit(context, pendingAiText);
      }
      closeActiveBox();
    });

    copyBtn.addEventListener("click", () => {
      navigator.clipboard?.writeText(pendingAiText).catch(() => { });
      copyBtn.textContent = "Copied";
      copyBtn.classList.add("btn-success"); // Apply Green Success Gradient
      setTimeout(() => {
        copyBtn.textContent = "Copy";
        copyBtn.classList.remove("btn-success");
      }, 1200);
    });

    const onKeydown = (e) => { if (e.key === "Escape") closeActiveBox(); };
    const onOutsideClick = (e) => { if (!hostEl.contains(e.target)) closeActiveBox(); };

    const handleScroll = () => reposition();
    window.addEventListener("scroll", handleScroll, true);

    document.addEventListener("keydown", onKeydown, true);
    setTimeout(() => document.addEventListener("mousedown", onOutsideClick, true), 0);

    activeBox = { host: hostEl, onKeydown, onOutsideClick, onScroll: handleScroll };
    input.focus();
  }

  // ── Entry point: background.js relays the context-menu click here ─────
  chrome.runtime.onMessage.addListener((message) => {
    if (message.type !== OPEN_BOX_MESSAGE) return;

    const context = detectEditContext();
    if (!context) {
      // Menu is gated on contexts: ["selection"], so this shouldn't
      // normally happen — but selection can vanish between the
      // right-click and this message arriving. Log for visibility while
      // testing rather than failing completely silently.
      console.log("[Sicily Navigator] no selection found when edit box was requested.");
      return;
    }
    openEditBox(context);
  });
})();
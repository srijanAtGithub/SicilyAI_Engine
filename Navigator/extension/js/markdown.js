/**
 * markdown.js
 * -----------
 * Best-effort Markdown -> sanitized HTML for the side panel's AI message
 * bubbles. Renders real elements (headings, lists, tables, code blocks,
 * blockquotes) rather than approximating them as plain text.
 *
 * Why this exists as its own thing instead of reusing markdown_helper.py:
 * that module targets Telegram's Bot API HTML subset (b/i/u/s/code/pre/a/
 * blockquote only — no headings, no real lists, no tables) and runs
 * server-side in Python. This panel renders straight into the DOM in the
 * browser, so it can support the actual HTML elements CSS can style
 * (h1-h3, ul/ol/li, table, pre/code, blockquote) instead of flattening
 * everything into Telegram-safe approximations. Same spirit — no external
 * deps, handles what LLMs actually produce, escapes first — different
 * output target.
 *
 * Security: all literal text runs through escapeHtml() before any tag is
 * added around it. The only place a URL becomes an href/src is
 * safeUrl(), which rejects javascript:/data:/vbscript: schemes. Treat
 * this the same as the Python version: safe to call unconditionally on
 * any AI message text, never on content you plan to trust further.
 *
 * Supported: headings (#..######), bold/italic/strikethrough, inline
 * code, fenced code blocks (```lang), links, images, bullet/numbered/
 * checkbox lists (including nesting by indentation), blockquotes,
 * horizontal rules, tables, and paragraph breaks.
 */

function escapeHtml(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// Only allow http(s)/mailto links and same-origin-ish relative paths.
// Blocks javascript:, data:, vbscript:, etc. from becoming a clickable href.
function safeUrl(url) {
    const trimmed = (url || "").trim();
    if (/^(https?:|mailto:)/i.test(trimmed)) return trimmed;
    if (/^[./#]/.test(trimmed)) return trimmed;
    return "#";
}

// ── Inline-level formatting (applied within a single block of text) ──
// Runs AFTER escapeHtml, so literal `<`/`>`/`&` in the source are already
// safe text by the time these regexes add real tags around spans of it.
function renderInline(text) {
    // Inline code first, and stash it, so markup characters inside
    // backticks (e.g. `**not bold**`) aren't touched by the rules below.
    const stashed = [];
    text = text.replace(/`([^`\n]+)`/g, (_, code) => {
        stashed.push(`<code>${code}</code>`);
        return `\x00INLINECODE${stashed.length - 1}\x00`;
    });

    // Images: ![alt](url)
    text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, alt, url) => {
        return `<img src="${safeUrl(url)}" alt="${alt}" loading="lazy">`;
    });

    // Links: [text](url)
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
        return `<a href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });

    // Bold, then italic, then strikethrough
    text = text.replace(/\*\*(.+?)\*\*|__(.+?)__/g, (_, a, b) => `<b>${a || b}</b>`);
    text = text.replace(
        /(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.+?)(?<!_)_(?!_)/g,
        (_, a, b) => `<i>${a || b}</i>`
    );
    text = text.replace(/~~(.+?)~~/g, (_, s) => `<s>${s}</s>`);

    // Restore stashed inline code
    text = text.replace(/\x00INLINECODE(\d+)\x00/g, (_, i) => stashed[Number(i)]);

    return text;
}

function isTableSeparatorLine(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
}

function splitTableRow(line) {
    let row = line.trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|")) row = row.slice(0, -1);
    return row.split("|").map((cell) => cell.trim());
}

/**
 * Converts a Markdown string into sanitized HTML for insertion via
 * innerHTML. Safe to call unconditionally, including on plain text with
 * no markdown in it (it just becomes an escaped paragraph).
 */
export function markdownToHtml(text) {
    if (!text) return "";

    const lines = text.replace(/\r\n/g, "\n").split("\n");
    const html = [];

    let i = 0;
    let paragraphBuf = [];

    function flushParagraph() {
        if (paragraphBuf.length === 0) return;
        const joined = paragraphBuf.join("\n");
        html.push(`<p>${renderInline(escapeHtml(joined))}</p>`);
        paragraphBuf = [];
    }

    while (i < lines.length) {
        const rawLine = lines[i];
        const line = rawLine;

        // Fenced code block: ```lang ... ```
        const fenceMatch = /^```(\w*)\s*$/.exec(line.trim());
        if (fenceMatch) {
            flushParagraph();
            const lang = fenceMatch[1];
            const codeLines = [];
            i++;
            while (i < lines.length && lines[i].trim() !== "```") {
                codeLines.push(lines[i]);
                i++;
            }
            i++; // skip closing fence
            const langAttr = lang ? ` class="language-${escapeHtml(lang)}"` : "";
            html.push(`<pre><code${langAttr}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
            continue;
        }

        // Horizontal rule
        if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
            flushParagraph();
            html.push("<hr>");
            i++;
            continue;
        }

        // Heading
        const headingMatch = /^(#{1,6})\s+(.*)$/.exec(line);
        if (headingMatch) {
            flushParagraph();
            const level = Math.min(headingMatch[1].length, 6);
            html.push(`<h${level}>${renderInline(escapeHtml(headingMatch[2]))}</h${level}>`);
            i++;
            continue;
        }

        // Table: header row + separator row + >=0 data rows
        if (line.includes("|") && i + 1 < lines.length && isTableSeparatorLine(lines[i + 1]) && lines[i + 1].includes("-")) {
            flushParagraph();
            const header = splitTableRow(line);
            i += 2;
            const rows = [];
            while (i < lines.length && lines[i].trim().includes("|") && lines[i].trim() !== "") {
                rows.push(splitTableRow(lines[i]));
                i++;
            }
            let table = "<table><thead><tr>";
            table += header.map((h) => `<th>${renderInline(escapeHtml(h))}</th>`).join("");
            table += "</tr></thead><tbody>";
            for (const row of rows) {
                table += "<tr>" + row.map((c) => `<td>${renderInline(escapeHtml(c))}</td>`).join("") + "</tr>";
            }
            table += "</tbody></table>";
            html.push(table);
            continue;
        }

        // Blockquote (consume consecutive `>` lines as one block)
        if (/^\s*>\s?/.test(line)) {
            flushParagraph();
            const quoteLines = [];
            while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
                quoteLines.push(lines[i].replace(/^\s*>\s?/, ""));
                i++;
            }
            html.push(`<blockquote>${renderInline(escapeHtml(quoteLines.join("\n")))}</blockquote>`);
            continue;
        }

        // Checkbox / bullet / numbered list (consume a contiguous run as one list)
        const checkboxMatch = /^(\s*)[-*+]\s+\[( |x|X)\]\s+(.*)$/.exec(line);
        const bulletMatch = /^(\s*)[-*+]\s+(.*)$/.exec(line);
        const numberedMatch = /^(\s*)\d+\.\s+(.*)$/.exec(line);

        if (checkboxMatch || bulletMatch || numberedMatch) {
            flushParagraph();
            const ordered = !!numberedMatch && !checkboxMatch && !bulletMatch;
            const items = [];
            while (i < lines.length) {
                const cbm = /^(\s*)[-*+]\s+\[( |x|X)\]\s+(.*)$/.exec(lines[i]);
                const blm = /^(\s*)[-*+]\s+(.*)$/.exec(lines[i]);
                const nbm = /^(\s*)\d+\.\s+(.*)$/.exec(lines[i]);
                if (cbm) {
                    const checked = cbm[2].toLowerCase() === "x";
                    items.push(
                        `<li class="md-checkbox-item"><input type="checkbox" disabled${checked ? " checked" : ""}> ${renderInline(
                            escapeHtml(cbm[3])
                        )}</li>`
                    );
                } else if (ordered && nbm) {
                    items.push(`<li>${renderInline(escapeHtml(nbm[2]))}</li>`);
                } else if (!ordered && blm) {
                    items.push(`<li>${renderInline(escapeHtml(blm[2]))}</li>`);
                } else {
                    break;
                }
                i++;
            }
            const tag = ordered ? "ol" : "ul";
            html.push(`<${tag}>${items.join("")}</${tag}>`);
            continue;
        }

        // Blank line -> paragraph break
        if (line.trim() === "") {
            flushParagraph();
            i++;
            continue;
        }

        // Plain text line -> accumulate into current paragraph
        paragraphBuf.push(line);
        i++;
    }

    flushParagraph();
    return html.join("\n");
}
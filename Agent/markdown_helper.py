"""
Converts common LLM-generated Markdown into the small HTML subset
Telegram's Bot API supports (parse_mode="HTML").

Why HTML and not Telegram's "MarkdownV2":
MarkdownV2 requires escaping ~15 special characters (_ * [ ] ( ) ~ ` > # + - = | { } . !)
anywhere they appear outside of intended formatting. LLM output routinely contains
these characters (bullets, decimals, parens) in ways that don't match the required
escaping, so real messages break constantly. Telegram's HTML mode only cares about
< > & and a small fixed set of tags, which is much safer to target with a formatter
that isn't a full markdown implementation.

Supported Telegram HTML tags: b, i, u, s, code, pre, a, blockquote
(https://core.telegram.org/bots/api#html-style)

No external dependencies — pure stdlib regex. Kept intentionally simple: it does not
try to be a spec-complete Markdown parser, just handles what LLMs actually produce
(bold, italic, inline code, fenced code blocks, links, images, headers, bullet/
numbered/checkbox lists, blockquotes, horizontal rules, and tables).

Two elements have no Telegram equivalent, so they're approximated:
  - Horizontal rules (---)   -> a plain "──────────" separator line
  - Tables (| a | b |)       -> a monospace <pre> block (via <pre>) so columns
                                 still line up, since Telegram has no <table>
Images (![alt](url)) become "🖼 alt: url" since Telegram messages can't embed
a remote image inline via HTML — only actual photo uploads can do that.
"""

import re
import html as _html

_CODE_BLOCK_RE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.+?)(?<!_)_(?!_)")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_HEADER_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_HR_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_CHECKBOX_DONE_RE = re.compile(r"^([ \t]*)[-*+][ \t]+\[[xX]\][ \t]+(.*)$", re.MULTILINE)
_CHECKBOX_TODO_RE = re.compile(r"^([ \t]*)[-*+][ \t]+\[[ ]\][ \t]+(.*)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.*)$", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^[ \t]*\d+\.[ \t]+(.*)$", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^(?:[ \t]*&gt;[ \t]?.*(?:\n|$))+", re.MULTILINE)
# A markdown table: a header row, a separator row (---|---), then >=1 data rows.
_TABLE_RE = re.compile(
    r"^[ \t]*\|.*\|[ \t]*\n[ \t]*\|[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*\n(?:[ \t]*\|.*\|[ \t]*\n?)+",
    re.MULTILINE,
)


def markdown_to_html(text: str) -> str:
    """Best-effort Markdown -> Telegram-HTML conversion.

    Safe to call on plain text (no markdown) too — it will just pass through
    with HTML-escaping applied, so this can be used unconditionally on every
    outgoing message.
    """
    if not text:
        return text

    table_blocks: list[str] = []

    def _stash_table(m):
        raw = m.group(0).rstrip("\n")
        rows = [r.strip() for r in raw.split("\n") if r.strip()]

        def _split_row(row: str) -> list[str]:
            row = row.strip()
            if row.startswith("|"):
                row = row[1:]
            if row.endswith("|"):
                row = row[:-1]
            return [cell.strip() for cell in row.split("|")]

        header = _split_row(rows[0])
        data_rows = [_split_row(r) for r in rows[2:]]  # skip header + separator row

        widths = [len(h) for h in header]
        for row in data_rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(cell))

        def _format_row(cells: list[str]) -> str:
            padded = [cells[i].ljust(widths[i]) if i < len(cells) else "" for i in range(len(widths))]
            return "  ".join(padded).rstrip()

        rendered = "\n".join(
            [_format_row(header), "  ".join("-" * w for w in widths)]
            + [_format_row(row) for row in data_rows]
        )

        placeholder = f"\x00TABLEBLOCK{len(table_blocks)}\x00"
        table_blocks.append(f"<pre>{_html.escape(rendered)}</pre>")
        return placeholder

    text = _TABLE_RE.sub(_stash_table, text)

    # 1. Pull out fenced code blocks so nothing inside them gets mangled.
    code_blocks = []

    def _stash_code_block(m):
        lang, code = m.group(1), m.group(2)
        code = code.strip("\n")
        placeholder = f"\x00CODEBLOCK{len(code_blocks)}\x00"
        lang_attr = f' class="language-{lang}"' if lang else ""
        code_blocks.append(f"<pre><code{lang_attr}>{_html.escape(code)}</code></pre>")
        return placeholder

    text = _CODE_BLOCK_RE.sub(_stash_code_block, text)

    # 2. Escape everything else for HTML now, before adding our own tags.
    text = _html.escape(text)

    # 3. Inline code (after escaping, so `<` inside backticks stays literal text)
    inline_code = []

    def _stash_inline_code(m):
        placeholder = f"\x00INLINECODE{len(inline_code)}\x00"
        inline_code.append(f"<code>{m.group(1)}</code>")
        return placeholder

    text = _INLINE_CODE_RE.sub(_stash_inline_code, text)

    # 4. Images -> readable text (Telegram HTML can't embed a remote image)
    text = _IMAGE_RE.sub(lambda m: f"🖼 {m.group(1)}: {m.group(2)}" if m.group(1) else f"🖼 {m.group(2)}", text)

    # 5. Links  [text](url)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)

    # 6. Bold / italic / strikethrough
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)

    # 7. Headers -> bold line (Telegram has no header tags)
    text = _HEADER_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)

    # 8. Horizontal rules -> visual separator (Telegram has no <hr>)
    text = _HR_RE.sub("──────────", text)

    # 9. Checkbox lists -> "✅ item" / "☐ item", before the generic bullet regex
    text = _CHECKBOX_DONE_RE.sub(lambda m: f"{m.group(1)}✅ {m.group(2)}", text)
    text = _CHECKBOX_TODO_RE.sub(lambda m: f"{m.group(1)}☐ {m.group(2)}", text)

    # 10. Remaining list items -> "• item" (Telegram has no list tags)
    text = _BULLET_RE.sub(lambda m: f"• {m.group(1)}", text)
    text = _NUMBERED_RE.sub(lambda m: m.group(0).lstrip(), text)  # keep "1. item" as-is

    # 11. Blockquotes -> <blockquote> (run after HTML-escape, so '>' is '&gt;')
    def _stash_blockquote(m):
        block = m.group(0)
        inner_lines = [re.sub(r"^[ \t]*&gt;[ \t]?", "", line) for line in block.rstrip("\n").split("\n")]
        return f"<blockquote>{chr(10).join(inner_lines)}</blockquote>\n"

    text = _BLOCKQUOTE_RE.sub(_stash_blockquote, text)

    # 12. Restore stashed inline code / code blocks / tables
    for i, code_html in enumerate(inline_code):
        text = text.replace(f"\x00INLINECODE{i}\x00", code_html)
    for i, block_html in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block_html)
    for i, table_html in enumerate(table_blocks):
        text = text.replace(f"\x00TABLEBLOCK{i}\x00", table_html)

    return text
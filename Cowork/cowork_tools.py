"""
cowork_tools.py
--------------
Sandboxed filesystem tools for `sicily start`.

Mirrors the @modelcontextprotocol/server-filesystem interface,
re-implemented in pure Python with zero extra dependencies.

ALL tools are locked to a single root directory (the cwd where
`sicily start` was invoked). No path can escape that root.

Tool tiers
----------
Read-only tools  — safe:      read_file (full or line-range), list_directory,
                              file_tree_shallow, get_file_info, list_allowed_directories
Write tools      — safe-ish: write_file (mode="create" for new files,
                  mode="edit" for line-range replacement in existing ones)
                  Guarantee: never delete existing content.
                  write_file's edit mode requires dry_run=False to apply changes.
Path pins        — memory:   pin_path, recall_path, recall_all_pins
                  Survive context summarisation — stored in process memory,
                  not in the message list.
"""

import datetime
import stat
from pathlib import Path
from typing import Optional
import importlib

from langchain_core.tools import tool

# Noise directories — skipped in trees and searches
SKIP_DIRS = {
    ".venv", "venv", "env", ".env",
    "node_modules",
    "__pycache__",
    ".git",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".eggs",
    ".tox", ".nox",
    ".idea", ".vscode",
    ".sicily-trash",
}


# Allowed extensions for text-based file CONTENT reads/writes only
# (write_file, in THIS file).
# Binary formats (.docx, .xlsx, .pdf, …) are intentionally excluded from
# these content write operations — they require structured serialisation,
# not raw text I/O.
#
# NOTE: this restriction is scoped to reading/writing file CONTENT. It does
# NOT apply to filesystem operations like move/copy/rename/delete, which
# never touch content — see cowork_tool_fileops.py's READABLE_EXTENSIONS,
# which deliberately includes .pdf/.docx/.xlsx/.xls/.doc for exactly that
# reason. Don't infer from this set alone that binary files are unsupported
# sandbox-wide.
ALLOWED_WRITE_EXTENSIONS: frozenset[str] = frozenset({
    # Documents & notes
    ".txt", ".md", ".markdown", ".rst", ".org", ".tex",
    # Config & data interchange
    ".json", ".jsonl", ".ndjson",
    ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env",
    # Web & markup
    ".html", ".htm", ".css", ".scss", ".sass", ".xml", ".svg",
    # Source code — common languages
    ".py", ".pyi",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".sh", ".bash", ".zsh", ".fish",
    ".rb", ".go", ".rs",
    ".java", ".kt", ".scala",
    ".c", ".cpp", ".cc", ".h", ".hpp",
    ".cs", ".fs",
    ".php", ".lua", ".r", ".sql",
    # Data & logs
    ".csv", ".tsv", ".log",
    # Misc text
    ".diff", ".patch", ".gitignore", ".editorconfig",
})


# Sandbox root
_SANDBOX_ROOT: Optional[Path] = None


def set_sandbox_root(path: Path) -> None:
    global _SANDBOX_ROOT
    _SANDBOX_ROOT = path.resolve()


def get_sandbox_root() -> Path:
    if _SANDBOX_ROOT is None:
        raise RuntimeError("Sandbox root has not been set. Call set_sandbox_root() first.")
    return _SANDBOX_ROOT


# Path pin store — survives context summarisation
# Stored in process memory, not in the message list, so the summariser
# cannot compress it away.
_PATH_PINS: dict[str, str] = {}


# Internal helpers
def _safe_path(relative: str) -> Path:
    """
    Resolve a user/AI-supplied path against the sandbox root.
    Raises PermissionError if the resolved path would escape the root.
    """
    root = get_sandbox_root()
    candidate = root / relative
    try:
        resolved = candidate.resolve()
    except OSError:
        # On Windows, resolve() can raise FileNotFoundError for paths
        # that don't exist yet. Fall back to normpath-based resolution,
        # which works for non-existent paths.
        import os
        resolved = Path(os.path.normpath(candidate))

    if not resolved.is_relative_to(root):
        raise PermissionError(
            f"Access denied: '{relative}' resolves outside the allowed directory."
        )
    return resolved


def _is_skipped(path: Path) -> bool:
    """True if this is a noise directory that should be excluded."""
    return path.is_dir() and path.name in SKIP_DIRS


def _fmt_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_permissions(mode: int) -> str:
    """Convert a stat st_mode integer to a human-readable 'rwxrwxrwx' string."""
    result = []
    for who in ("USR", "GRP", "OTH"):
        for perm, letter in (("R", "r"), ("W", "w"), ("X", "x")):
            flag = getattr(stat, f"S_I{perm}{who}")
            result.append(letter if mode & flag else "-")
    return "".join(result)


# Extensions that require binary parsing rather than UTF-8 text reads
_BINARY_EXTENSIONS = frozenset({".pdf", ".xlsx", ".xls", ".docx", ".doc"})


def _read_binary(path: Path) -> str:
    """
    Extract human-readable text from binary file formats.
    Dispatches to the appropriate parser based on file extension.
    Raises ImportError with an install hint if the required library is missing.
    Raises ValueError for unsupported binary extensions.
    """
    ext = path.suffix.lower()

    if ext == ".pdf":
        if importlib.util.find_spec("pypdf") is None:
            raise ImportError("pip install pypdf")

        from pypdf import PdfReader

        reader = PdfReader(path)
        text_output = ""

        # 1. Extract text page by page
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_output += f"[Page {i+1}]\n{page_text.strip()}\n\n"

        # 2. Extract form fields (AcroForm)
        try:
            fields = reader.get_fields()
            if fields:
                field_lines = []
                for name, field in fields.items():
                    value = field.value
                    if value is not None:
                        field_lines.append(f"{name}: {value}")
                    else:
                        # Optional: show field name even if empty
                        field_lines.append(f"{name}: [empty]")
                if field_lines:
                    text_output += "[Form Field Values]\n" + "\n".join(field_lines)
        except Exception as e:
            text_output += f"\n[Form Field Extraction Failed: {e}]"

        return text_output.strip()

    if ext in {".xlsx", ".xls"}:
        if importlib.util.find_spec("openpyxl") is None:
            raise ImportError("pip install openpyxl")
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheets = []
        for name in wb.sheetnames:
            ws = wb[name]
            rows = [
                "\t".join("" if cell.value is None else str(cell.value) for cell in row)
                for row in ws.iter_rows()
            ]
            sheets.append(f"[Sheet: {name}]\n" + "\n".join(rows))
        wb.close()
        return "\n\n".join(sheets)

    if ext in {".docx", ".doc"}:
        if importlib.util.find_spec("docx") is None:
            raise ImportError("pip install python-docx")
        from docx import Document
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    raise ValueError(
        f"No binary reader available for '{ext}'. "
        "For plain text files this tool reads UTF-8 directly. "
        "For other binary formats, a dedicated tool may be needed."
    )


# READ-ONLY TOOLS
@tool
def search_index(query: str) -> str:
    """
    Search the local RAG index for content relevant to a query.
 
    This is the FIRST tool to call for any question that involves finding
    information inside files — before reading any file directly.
 
    The index covers all text-based files in the sandbox:
    .txt, .md, .pdf, .docx, .xlsx, .py, .json, .csv, and more.
 
    Returns the top matching snippets with their file path and position.
    If a snippet looks relevant, use read_file with start_line/end_line to
    read more context around it in the original file.
 
    Args:
        query: Plain-language description of what you are looking for.
               e.g. "quarterly budget figures" or "meeting notes from January"
    """
    from Cowork.cowork_rag import get_rag   # adjust import path to match your project
    rag = get_rag()
    if rag is None:
        return "RAG index is not initialised. This is a bug — please report it."
    results = rag.search(query)
    return rag.format_results(results)


@tool
def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """
    Read a file's contents as plain text — either in full or a specific
    line range.

    NOT THE DEFAULT FIRST MOVE. Before reaching for this tool, ask whether
    search_file_contents or search_index would get you a line number instead.
    Blindly reading a file you haven't searched yet is a common source of
    wasted calls (you're guessing where the answer lives instead of letting
    a search tell you).

    Two modes, chosen by whether you pass start_line/end_line:

    FULL READ (start_line=0, end_line=0 — the default)
      Returns the entire file. Only use this for small-to-medium files or
      when the complete content is genuinely required (summarizing a short
      file, reviewing a small script). Avoid on large files (logs, big
      CSVs, long source files) — use the ranged mode instead.

    RANGED READ (start_line=N, end_line=M, both 1-indexed, inclusive)
      Returns only that line range, each line numbered. Max 500 lines per
      call. Use this after a search gives you a real line number to anchor
      on — do not guess a range speculatively ("probably around line 400");
      search for the line first, then read exactly around it. This is also
      the required first step before write_file's edit mode, to confirm
      you're targeting the correct lines.

    Handles two file categories transparently:
      Text-based (.txt .md .py .json .csv .yaml .html etc.) — raw UTF-8.
      Binary documents — .pdf (text per page, labelled [Page N]),
      .docx (paragraph text in order), .xlsx/.xls (each sheet as a
      tab-separated table, labelled [Sheet: name]). Ranged reads are
      text-file only; binary documents always return in full.

    Typical workflow
    ----------------
    1. search_file_contents(pattern, ...)     — get a real line number
    2. read_file(path, N, M)                  — confirm exact target lines
    3. write_file(path, mode="edit", ...)     — make the surgical change

    Args:
        path:       Relative path to the file.
        start_line: First line to read (1-indexed). Leave at 0 for a full read.
        end_line:   Last line to read (inclusive). Leave at 0 for a full read.
    """
    ranged = start_line > 0 or end_line > 0
    if ranged:
        if start_line < 1:
            return "Error: start_line must be >= 1."
        if end_line < start_line:
            return "Error: end_line must be >= start_line."
        if end_line - start_line > 500:
            return "Error: Cannot read more than 500 lines at once. Narrow your range."

    try:
        file_path = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not file_path.exists():
        return f"File '{path}' does not exist."
    if not file_path.is_file():
        return f"'{path}' is a directory, not a file."

    is_binary = file_path.suffix.lower() in _BINARY_EXTENSIONS

    if ranged:
        if is_binary:
            return (
                f"'{path}' is a binary document ({file_path.suffix}). "
                "Ranged reads only work on text files — call read_file(path) "
                "without start_line/end_line to get its full extracted text."
            )
        try:
            all_lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except Exception as e:
            return f"Could not read '{path}': {e}"

        total = len(all_lines)
        if start_line > total:
            return f"File only has {total} lines. start_line={start_line} is out of range."

        actual_end = min(end_line, total)
        selected = all_lines[start_line - 1 : actual_end]

        numbered = "".join(f"{start_line + i:>6}  {line}" for i, line in enumerate(selected))
        header = f"[{path} | lines {start_line}–{actual_end} of {total}]\n"
        return header + numbered

    # Full read
    if is_binary:
        try:
            content = _read_binary(file_path)
        except ImportError as e:
            return f"Cannot read '{path}': missing required package — {e}"
        except Exception as e:
            return f"Could not extract text from '{path}': {e}"
    else:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Could not read '{path}': {e}"

    total_lines = len(content.splitlines())
    header = f"[{path} | full file, {total_lines} lines]\n"
    return header + content


@tool
def list_directory(path: str = ".") -> str:
    """
    List the immediate contents of a directory.
    Each entry is prefixed with [FILE] or [DIR].
    Does NOT recurse into subdirectories.

    Args:
        path: Relative path to the directory. Defaults to "." (sandbox root).
    """
    try:
        target = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not target.exists():
        return f"Directory '{path}' does not exist."
    if not target.is_dir():
        return f"'{path}' is a file, not a directory."

    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return f"Permission denied: cannot list '{path}'."

    if not entries:
        return "Directory is empty."

    lines = []
    for entry in entries:
        tag = "[DIR] " if entry.is_dir() else "[FILE]"
        note = "  [skipped — noise dir]" if _is_skipped(entry) else ""
        lines.append(f"{tag} {entry.name}{note}")

    return "\n".join(lines)


@tool
def get_file_info(path: str) -> str:
    """
    Get detailed metadata about a file or directory.
    Returns: name, type, size, permissions, created, modified, and accessed times.

    Args:
        path: Relative path to the file or directory.
    """
    try:
        target = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not target.exists():
        return f"'{path}' does not exist."

    try:
        s = target.stat()
    except PermissionError:
        return f"Permission denied: cannot stat '{path}'."

    kind = "Directory" if target.is_dir() else "File"
    size = f"{s.st_size:,} bytes" if target.is_file() else "—"
    permissions = _fmt_permissions(s.st_mode)

    # Creation time:
    #   macOS  → st_birthtime (real creation time)
    #   Windows→ st_ctime     (real creation time)
    #   Linux  → st_ctime     (last metadata change; true birthtime not exposed by Python)
    created = _fmt_ts(getattr(s, "st_birthtime", s.st_ctime))

    return "\n".join([
        f"Name:        {target.name}",
        f"Type:        {kind}",
        f"Size:        {size}",
        f"Permissions: {permissions}",
        f"Created:     {created}",
        f"Modified:    {_fmt_ts(s.st_mtime)}",
        f"Accessed:    {_fmt_ts(s.st_atime)}",
        f"Path:        {path}",
    ])


# WRITE TOOLS
@tool
def write_file(
    path: str,
    content: str = "",
    mode: str = "create",
    start_line: int = 0,
    end_line: int = 0,
    create_parents: bool = True,
    dry_run: bool = True,
) -> str:
    """
    Create a new text file, or edit a line range in an existing one. This is
    the only tool for writing file content — choose the mode with `mode`.

    mode="create" (default) — make a NEW file
      Writes `content` to `path`. Refuses if anything already exists there
      (use mode="edit" to modify an existing file instead). Parent
      directories are created automatically when create_parents=True
      (default). Ignores dry_run/start_line/end_line.

    mode="edit" — replace a line range in an EXISTING file
      Replaces lines [start_line, end_line] inclusive (1-indexed) with
      `content`; surrounding lines are untouched. Pass content="" to delete
      the range without inserting anything. The file must already exist.
      dry_run=True (default) previews the change as a diff without writing;
      call again with dry_run=False once the preview looks right.

    Both modes only accept text-based extensions:
      Documents/notes : .txt .md .markdown .rst .org .tex
      Config/data     : .json .jsonl .ndjson .yaml .yml .toml .ini .cfg .conf .env
      Web/markup      : .html .htm .css .scss .sass .xml .svg
      Source code     : .py .pyi .js .mjs .cjs .ts .tsx .jsx .sh .bash .zsh .fish
                        .rb .go .rs .java .kt .scala .c .cpp .cc .h .hpp .cs .fs
                        .php .lua .r .sql
      Data/logs       : .csv .tsv .log
      Misc text       : .diff .patch .gitignore .editorconfig

    Typical edit workflow
    ---------------------
    1. read_file(path, N, M)                                   — confirm target lines
    2. write_file(path, new, mode="edit", start_line=N,
                   end_line=M, dry_run=True)                   — preview
    3. write_file(..., dry_run=False)                          — apply once confirmed

    Args:
        path:           Relative path to the file.
        content:        For mode="create": the full file content. For
                        mode="edit": the replacement text for the line
                        range (pass "" to delete the range).
        mode:           "create" for a new file, "edit" to replace lines in
                        an existing file.
        start_line:     mode="edit" only — first line to replace (1-indexed).
        end_line:       mode="edit" only — last line to replace (inclusive).
        create_parents: mode="create" only — auto-create missing parent
                        directories (default True).
        dry_run:        mode="edit" only — if True (default), preview
                        without writing.
    """
    if mode not in ("create", "edit"):
        return f"Error: mode must be 'create' or 'edit', got '{mode}'."

    try:
        target = _safe_path(path)
    except PermissionError as e:
        return str(e)

    ext = target.suffix.lower()

    if mode == "create":
        if target.exists():
            kind = "directory" if target.is_dir() else "file"
            return (
                f"Refused: '{path}' already exists as a {kind}. "
                "Use mode=\"edit\" to modify an existing file."
            )
        if not ext:
            return (
                f"Refused: '{path}' has no file extension. "
                "Please include one (e.g. report.md, config.yaml)."
            )
        if ext not in ALLOWED_WRITE_EXTENSIONS:
            allowed_str = "  " + "\n  ".join(sorted(ALLOWED_WRITE_EXTENSIONS))
            return (
                f"Refused: extension '{ext}' is not in the allowed list.\n"
                f"Supported extensions:\n{allowed_str}"
            )

        parent = target.parent
        if not parent.exists():
            if not create_parents:
                rel_parent = parent.relative_to(get_sandbox_root())
                return (
                    f"Error: parent directory '{rel_parent}' does not exist. "
                    "Pass create_parents=True to create it automatically, "
                    "or use run_file_command('mkdir ...') first."
                )
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return f"Could not create parent directories for '{path}': {e}"

        try:
            target.write_text(content, encoding="utf-8")
        except Exception as e:
            return f"Could not write '{path}': {e}"

        size = target.stat().st_size
        return f"Created '{path}'.\nSize: {size:,} bytes | Encoding: utf-8"

    # mode == "edit"
    if start_line < 1:
        return "Error: start_line must be >= 1."
    if end_line < start_line:
        return "Error: end_line must be >= start_line."

    if not target.exists():
        return f"File '{path}' does not exist. Use mode=\"create\" to make new files."
    if not target.is_file():
        return f"'{path}' is a directory, not a file."
    if ext not in ALLOWED_WRITE_EXTENSIONS:
        return (
            f"Refused: extension '{ext}' is not in the allowed list for editing. "
            "Only text-based files can be edited."
        )

    try:
        all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except Exception as e:
        return f"Could not read '{path}': {e}"

    total = len(all_lines)
    if start_line > total + 1:
        return f"File only has {total} lines. start_line={start_line} is out of range."

    actual_end = min(end_line, total)
    removed = all_lines[start_line - 1 : actual_end]

    # Ensure new content ends with a newline so the file stays well-formed
    if content and not content.endswith("\n"):
        replacement_block = content + "\n"
    else:
        replacement_block = content

    new_file_lines = all_lines[: start_line - 1] + ([replacement_block] if replacement_block else []) + all_lines[actual_end:]
    new_content_full = "".join(new_file_lines)

    if dry_run:
        removed_preview = (
            "".join(f"  - {l.rstrip()}\n" for l in removed)
            or "  (nothing — pure insertion before this line)\n"
        )
        added_lines = replacement_block.splitlines() if replacement_block else []
        added_preview = (
            "".join(f"  + {l}\n" for l in added_lines)
            if added_lines
            else "  (deleted — no replacement)\n"
        )
        return (
            f"[DRY RUN — no changes written]\n\n"
            f"File:   {path}\n"
            f"Range:  lines {start_line}–{actual_end} of {total}\n\n"
            f"REMOVE:\n{removed_preview}\n"
            f"INSERT:\n{added_preview}\n"
            f"Call again with dry_run=False to apply."
        )

    try:
        target.write_text(new_content_full, encoding="utf-8")
    except Exception as e:
        return f"Could not write '{path}': {e}"

    new_total = len(new_file_lines)
    delta = new_total - total
    delta_str = f"+{delta}" if delta >= 0 else str(delta)
    added_count = len(replacement_block.splitlines()) if replacement_block else 0
    return (
        f"✅ Edit applied to '{path}'.\n"
        f"Replaced lines {start_line}–{actual_end} "
        f"({len(removed)} line(s) removed → {added_count} line(s) inserted).\n"
        f"File now has {new_total} lines ({delta_str})."
    )


# EXPORTED TOOL LIST
LOCAL_TOOLS = [
    # Read-only (safe)
    search_index,
    read_file,
    list_directory,
    get_file_info,

    # Write (safe-ish)
    write_file,
]


# SPINNER STATUS MESSAGES
TOOL_STATUS_MAP = {
    "search_index": lambda args: (
        f"Searching index for [white]'{args.get('query')}'[/white]"
    ),
    "read_file": lambda args: (
        f"Reading lines {args.get('start_line')}–{args.get('end_line')} of "
        f"[white]'{args.get('path')}'[/white]"
        if args.get("start_line") or args.get("end_line")
        else f"Reading file [white]'{args.get('path')}'[/white]"
    ),
    "list_directory": lambda args: (
        f"Listing contents of [white]'{args.get('path', '.')}'[/white]"
    ),
    "get_file_info": lambda args: (
        f"Inspecting metadata for [white]'{args.get('path')}'[/white]"
    ),
    "write_file": lambda args: (
        f"Creating [white]'{args.get('path')}'[/white]"
        if args.get("mode", "create") == "create"
        else (
            f"Previewing edit to [white]'{args.get('path')}'[/white] "
            f"(lines {args.get('start_line')}–{args.get('end_line')})"
            if args.get("dry_run", True)
            else f"Applying edit to [white]'{args.get('path')}'[/white] "
                 f"(lines {args.get('start_line')}–{args.get('end_line')})"
        )
    ),
}

import Cowork.cowork_tool_fileops as fileops
LOCAL_TOOLS.extend(fileops.FILEOPS_TOOLS)   # Merge fileops tools
TOOL_STATUS_MAP.update(fileops.FILEOPS_TOOL_STATUS_MAP) # Merge status messages


def get_friendly_tool_message(tool_call: dict) -> str:
    """Extracts the tool name and args to build a readable status update."""
    name = tool_call.get("name")
    args = tool_call.get("args", {})

    if name in TOOL_STATUS_MAP:
        return TOOL_STATUS_MAP[name](args)

    # Fallback for any future tools not yet in the map
    return name or "working..."
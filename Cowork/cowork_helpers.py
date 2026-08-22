import datetime
import stat
from pathlib import Path
from typing import Optional
import importlib
import time
import shlex
import shutil


# Noise directories — skipped in trees and searches
_SKIP_DIRS = {
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
_ALLOWED_WRITE_EXTENSIONS: frozenset[str] = frozenset({
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


def _set_sandbox_root(path: Path) -> None:
    global _SANDBOX_ROOT
    _SANDBOX_ROOT = path.resolve()


def _get_sandbox_root() -> Path:
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
    root = _get_sandbox_root()
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
    return path.is_dir() and path.name in _SKIP_DIRS


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


def _list_directory_entries(target: Path, path_label: str) -> str:
    """
    List the immediate contents of a directory. Each entry is prefixed
    with [FILE] or [DIR]. Does NOT recurse into subdirectories.
    `target` must already be a validated, existing directory Path;
    `path_label` is the original relative path string, used for messages.
    """
    if not target.exists():
        return f"Directory '{path_label}' does not exist."
    if not target.is_dir():
        return f"'{path_label}' is a file, not a directory."

    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return f"Permission denied: cannot list '{path_label}'."

    if not entries:
        return "Directory is empty."

    lines = []
    for entry in entries:
        tag = "[DIR] " if entry.is_dir() else "[FILE]"
        note = "  [skipped — noise dir]" if _is_skipped(entry) else ""
        lines.append(f"{tag} {entry.name}{note}")

    return "\n".join(lines)


def _describe_path(target: Path, path_label: str) -> str:
    """
    Get detailed metadata about a file or directory: name, type, size,
    permissions, created, modified, and accessed times.
    `target` must already be a validated Path; `path_label` is the
    original relative path string, used for messages.
    """
    if not target.exists():
        return f"'{path_label}' does not exist."

    try:
        s = target.stat()
    except PermissionError:
        return f"Permission denied: cannot stat '{path_label}'."

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
        f"Path:        {path_label}",
    ])


# Combined "readable" universe for this module: anything we can write/edit
# as text, plus anything we can extract text from (PDF/docx/xlsx). This set
# is consumed by the CONTENT tools in this module — search_file_contents and
# preview_files_for_review — which genuinely cannot do anything with an
# extension outside it, since there's no parser for it.
READABLE_EXTENSIONS: frozenset[str] = _ALLOWED_WRITE_EXTENSIONS | _BINARY_EXTENSIONS

# Extensions with no content parser, but that pure filesystem ops (copy/move/
# rename/delete) can still handle safely — those ops never open or interpret
# the bytes, so parseability is irrelevant to them. Kept as a DELIBERATELY
# SEPARATE set from READABLE_EXTENSIONS: merging it in would make
# search_file_contents / preview_files_for_review think they can extract text
# from a .zip or .mp4, which they can't. Extend this list, not
# READABLE_EXTENSIONS, if a new binary type should become movable/copyable
# without becoming "readable".
MANAGEABLE_ONLY_EXTENSIONS: frozenset[str] = frozenset({
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic", ".ico",
    # Video
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
    # Audio
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac",
    # Archives
    ".zip", ".tar", ".gz", ".tgz", ".rar", ".7z", ".bz2",
    # Packages / installers / misc binaries
    ".apk", ".ipa", ".exe", ".dmg", ".msi", ".bin", ".iso",
})

# What copy_file / move_file / rename_file / delete_file are allowed to
# touch. Union of the two sets above — everything content-readable, plus
# everything that's only filesystem-manageable.
MANAGEABLE_EXTENSIONS: frozenset[str] = READABLE_EXTENSIONS | MANAGEABLE_ONLY_EXTENSIONS

TRASH_DIR_NAME = ".sicily-trash"


# ---------------------------------------------------------------------------
# Trash helpers
# ---------------------------------------------------------------------------

def _trash_root() -> Path:
    """
    Return (and create) the sandbox-local trash directory.
    Lives INSIDE the sandbox root so it passes _safe_path() like everything
    else, and so trashed files survive a session restart for manual recovery.
    """
    root = _get_sandbox_root()
    trash = root / TRASH_DIR_NAME
    trash.mkdir(exist_ok=True)
    return trash


def _move_to_trash(target: Path) -> Path:
    """
    Move `target` into the trash dir, preserving its relative path so a
    human can find and restore it by hand. Timestamps the leaf name on
    collision instead of overwriting a previously trashed item.
    """
    root = _get_sandbox_root()
    rel = target.relative_to(root)
    dest = _trash_root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = dest.with_name(f"{stamp}__{dest.name}")

    shutil.move(str(target), str(dest))
    return dest


# ---------------------------------------------------------------------------
# COPY / MOVE / RENAME — via a single validated CLI-command tool
# ---------------------------------------------------------------------------

# Executables this tool will ever run, and the flags each may take.
# Anything not listed here — unknown executables, unknown flags, or
# anything requiring shell features — is rejected before a subprocess
# ever starts.
_ALLOWED_FLAGS = {
    "cp": {"-n", "-r"},
    "mv": {"-n"},
    "mkdir": {"-p"},
    "ls": set(),
    "info": set(),
}

_ALLOWED_EXECUTABLES = frozenset(_ALLOWED_FLAGS.keys())

# Sub-commands that take zero or more path positionals (0 => defaults to
# the sandbox root for `ls`, or is simply invalid for `info`, handled in
# run_file_command itself). Kept separate from mkdir's "1+" rule because
# `ls` alone (no args) is a legitimate, common call.
_ZERO_OR_MORE_POSITIONAL_COMMANDS = frozenset({"ls", "info"})

# Executables/keywords that must never reach this tool, checked explicitly
# so a rejection names the real reason ("that's a shell/programming/VCS/
# package/privilege command") instead of a generic "not allowed". Not
# exhaustive by design — _ALLOWED_EXECUTABLES is the actual allowlist and
# is what's enforced; this set only makes common bad attempts easier to
# explain. Covers both Unix and Windows spellings.
_EXPLICITLY_BLOCKED = frozenset({
    "rm", "rmdir", "del", "erase", "rd",
    "python", "python3", "py", "node", "ruby", "perl", "php",
    "git", "svn", "hg",
    "npm", "npx", "pip", "pip3", "yarn", "pnpm", "cargo", "gem",
    "sudo", "su", "chmod", "chown", "runas",
    "sh", "bash", "zsh", "powershell", "pwsh", "cmd", "cmd.exe",
    "curl", "wget", "ssh", "scp", "nc", "netcat",
    "eval", "exec",
})


class CommandValidationError(ValueError):
    """Raised by _validate_fileops_command when a command fails the
    docstring-declared contract. Message is written to be shown directly
    to the model/user — no internals leak, just the rule that was broken.
    """


def _validate_fileops_command(command: str) -> tuple[str, list[Path], list[str]]:
    """
    Parse and validate a `cp`/`mv`/`mkdir`/`ls`/`info` command string
    against the contract in run_file_command's docstring. Returns
    (executable, resolved_paths, flags) on success:
      - cp/mv: resolved_paths is [src1, src2, ..., dst] (2+ items —
        one or more sources, last item is the destination).
      - mkdir: resolved_paths is [path1, path2, ...] (1+ items).
      - ls/info: resolved_paths is [path1, path2, ...] (0+ items — an
        empty `ls` defaults to the sandbox root).
    Raises _CommandValidationError otherwise.

    Uses shlex.split (no shell=True, no /bin/sh) — no pipes, redirects,
    globs, or chaining possible.
    """
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise CommandValidationError(f"Could not parse command: {e}")

    if not tokens:
        raise CommandValidationError("Empty command.")

    executable = tokens[0]
    # Normalize away path prefixes / .exe so "/bin/rm" or "python3.exe"
    # still match the blocklist.
    executable_name = Path(executable).stem.lower()

    if executable_name in _EXPLICITLY_BLOCKED:
        raise CommandValidationError(
            f"Refused: '{executable}' is a shell, scripting, VCS, package-"
            "manager, or privilege-escalation command. run_file_command "
            "only runs cp, mv, mkdir, ls, or info."
        )

    if executable not in _ALLOWED_EXECUTABLES:
        raise CommandValidationError(
            f"Refused: '{executable}' is not an allowed command. "
            f"Only {sorted(_ALLOWED_EXECUTABLES)} are supported by "
            "run_file_command."
        )

    allowed_flags = _ALLOWED_FLAGS[executable]
    flags: list[str] = []
    positionals: list[str] = []
    for tok in tokens[1:]:
        if tok.startswith("-") and tok != "-":
            flags.append(tok)
        else:
            positionals.append(tok)

    unknown_flags = [f for f in flags if f not in allowed_flags]
    if unknown_flags:
        raise CommandValidationError(
            f"Refused: flag(s) {unknown_flags} are not allowed for "
            f"'{executable}'. Only {sorted(allowed_flags)} are permitted."
        )

    if executable in _ZERO_OR_MORE_POSITIONAL_COMMANDS:
        min_positionals = 0
    elif executable == "mkdir":
        min_positionals = 1
    else:
        min_positionals = 2

    if len(positionals) < min_positionals:
        noun = "path argument" if executable == "mkdir" else "path arguments (at least one source plus a destination)"
        raise CommandValidationError(
            f"Refused: expected at least {min_positionals} {noun} for "
            f"'{executable}', got {len(positionals)}: {positionals}."
        )

    # `ls` with zero arguments means "list the sandbox root" — give it an
    # explicit "." so downstream resolution has something to work with.
    if executable == "ls" and not positionals:
        positionals = ["."]

    for raw in positionals:
        if any(ch in raw for ch in "*?[]"):
            raise CommandValidationError(
                f"Refused: path '{raw}' contains a glob character "
                "(*, ?, [, ]). There is no shell here to expand globs — "
                "they would be treated as a literal, nonexistent filename. "
                "List each exact path as its own argument instead."
            )

    try:
        resolved = [_safe_path(p) for p in positionals]
    except PermissionError as e:
        raise CommandValidationError(str(e))

    return executable, resolved, flags
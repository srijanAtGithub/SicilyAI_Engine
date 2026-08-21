"""
cowork_tools_fileops.py
------------------------
File-management and content-search tools for Sicily Cowork. Extends
cowork_tools.py with: run_file_command (validated cp/mv/mkdir), delete_file
/ delete_directory (soft-delete to trash), search_file_contents (grep over
readable extensions), find_files_by_name (glob match), and
preview_files_for_review (batched multi-file preview).

Scope: content tools (search_file_contents, preview_files_for_review) only
work on READABLE_EXTENSIONS (text + PDF/docx/xlsx via existing parsers).
Management tools (run_file_command's cp/mv, delete_file, delete_directory)
work on the broader MANAGEABLE_EXTENSIONS (READABLE_EXTENSIONS plus images/
video/audio/archives/APKs/etc.) since these are pure filesystem ops that
never open or interpret content — file type is not a blocker for them.
delete_directory has no extension gate at all.

run_file_command is a single validated command tool (not a shell escape
hatch) replacing separate copy/move/rename/mkdir tools — see its docstring
for the exact contract.

Safety: every path goes through _safe_path(). Destructive ops move to
.sicily-trash/ rather than unlinking. Destructive/overwriting ops default
to dry_run=True.
"""

import re
import shlex
import shutil
import time
from pathlib import Path
import json
import fnmatch
from typing import List, Optional

from langchain_core.tools import tool

from Cowork.cowork_rag import SKIP_DIRS
from Cowork.cowork_tools import (
    _safe_path,
    _is_skipped,
    _read_binary,
    _BINARY_EXTENSIONS,
    ALLOWED_WRITE_EXTENSIONS,
    get_sandbox_root,
)


# Combined "readable" universe for this module: anything we can write/edit
# as text, plus anything we can extract text from (PDF/docx/xlsx). This set
# is consumed by the CONTENT tools in this module — search_file_contents and
# preview_files_for_review — which genuinely cannot do anything with an
# extension outside it, since there's no parser for it.
READABLE_EXTENSIONS: frozenset[str] = ALLOWED_WRITE_EXTENSIONS | _BINARY_EXTENSIONS

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
    root = get_sandbox_root()
    trash = root / TRASH_DIR_NAME
    trash.mkdir(exist_ok=True)
    return trash


def _move_to_trash(target: Path) -> Path:
    """
    Move `target` into the trash dir, preserving its relative path so a
    human can find and restore it by hand. Timestamps the leaf name on
    collision instead of overwriting a previously trashed item.
    """
    root = get_sandbox_root()
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

# Flags each executable is allowed to use. Anything not listed here is
# rejected before the command ever runs — this is the whole point of the
# validator: the model cannot "invent" a plausible-looking flag (e.g. `-r`
# on cp, `--force`) and have it silently reach a real subprocess.
_ALLOWED_FLAGS = {
    "cp": {"-n"},      # -n: no-clobber (never overwrite silently) — the
                       #     only flag exposed; there is no -f, no -r
                       #     (directories go through delete_directory,
                       #     not this tool).
    "mv": {"-n"},      # -n: no-clobber. Same rationale as cp.
    "mkdir": {"-p"},   # -p: create intermediate parents. This is also the
                       #     ALWAYS-ON behavior (see below) — accepted so a
                       #     model-written `mkdir -p ...` isn't rejected,
                       #     but a bare `mkdir dir` behaves identically.
}

# Number of required positional path arguments per executable.
_POSITIONAL_COUNTS = {"cp": 2, "mv": 2, "mkdir": 1}

_ALLOWED_EXECUTABLES = frozenset(_ALLOWED_FLAGS.keys())


class _CommandValidationError(ValueError):
    """Raised by _validate_fileops_command when a command fails the
    docstring-declared contract. Message is written to be shown directly
    to the model/user — no internals leak, just the rule that was broken.
    """


def _validate_fileops_command(command: str) -> tuple[str, list[Path], list[str]]:
    """
    Parse and validate a `cp`/`mv`/`mkdir` command string against the
    contract in run_file_command's docstring. Returns (executable,
    resolved_paths, flags) on success — resolved_paths is [src, dst] for
    cp/mv or [target] for mkdir. Raises _CommandValidationError otherwise.

    Uses shlex.split (no shell=True, no /bin/sh) — no pipes, redirects,
    globs, or chaining possible.
    """
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise _CommandValidationError(f"Could not parse command: {e}")

    if not tokens:
        raise _CommandValidationError("Empty command.")

    executable = tokens[0]
    if executable not in _ALLOWED_EXECUTABLES:
        raise _CommandValidationError(
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
        raise _CommandValidationError(
            f"Refused: flag(s) {unknown_flags} are not allowed for "
            f"'{executable}'. Only {sorted(allowed_flags)} are permitted."
        )

    expected = _POSITIONAL_COUNTS[executable]
    if len(positionals) != expected:
        noun = "path argument" if expected == 1 else "path arguments (source, destination)"
        raise _CommandValidationError(
            f"Refused: expected exactly {expected} {noun} for "
            f"'{executable}', got {len(positionals)}: {positionals}. "
            "No globs, no multi-destination forms."
        )

    for label, raw in zip(("source", "destination") if expected == 2 else ("path",), positionals):
        if any(ch in raw for ch in "*?[]"):
            raise _CommandValidationError(
                f"Refused: {label} '{raw}' contains a glob character "
                "(*, ?, [, ]). There is no shell here to expand globs — "
                "they would be treated as a literal, nonexistent filename. "
                "Pass one exact path instead."
            )

    try:
        resolved = [_safe_path(p) for p in positionals]
    except PermissionError as e:
        raise _CommandValidationError(str(e))

    return executable, resolved, flags


@tool
def run_file_command(command: str) -> str:
    """
    Copy, move, rename, or create a directory by running a validated `cp`,
    `mv`, or `mkdir` command. This is the ONLY way to do these things in
    this sandbox — it replaces the old copy_file/move_file/rename_file/
    make_directory tools. Renaming is just `mv <old-path> <new-path-same-
    folder>`; there is no separate verb for it.

    This is NOT a general shell tool. Only these exact forms are accepted:

        cp [-n] <source> <destination>
        mv [-n] <source> <destination>
        mkdir [-p] <path>

    Hard contract (violating ANY of these gets the command rejected before
    anything runs — no partial execution, no fallback interpretation):
      - The executable must be exactly "cp", "mv", or "mkdir". Nothing
        else — not "cp -r", not "rsync", not "rm", not any pipe/redirect/
        chain (`|`, `>`, `;`, `&&`, backticks, etc.).
      - cp/mv accept only `-n` (no-clobber). mkdir accepts only `-p`
        (create missing parents — already the default behavior, see
        below). Any other flag (`-r`, `-f`, `-v`, `--force`) is refused.
        cp/mv have no recursive/directory form — directories other than
        via mkdir are out of scope for this tool.
      - cp/mv take exactly two path arguments (source, destination); mkdir
        takes exactly one. No globs (`*.txt`), no multiple sources.
      - Every path must resolve inside the sandbox (same _safe_path()
        check every other tool in this module uses); for cp/mv the source
        file's extension must be in MANAGEABLE_EXTENSIONS.
      - cp/mv without `-n`: an existing destination file is refused rather
        than silently overwritten. mkdir is always idempotent — an
        already-existing directory at that path is a no-op success either
        way, and missing intermediate parents are always created, whether
        or not `-p` is passed.

    If you're unsure whether a command you're about to write is valid,
    write the simplest possible form — `cp source.txt dest.txt`,
    `mv old/path.pdf new/path.pdf`, or `mkdir reports/q3` — rather than
    guessing at flags. An invalid command is rejected with a clear reason
    and nothing happens; it never partially runs.

    cp/mv work on ANY file this sandbox can manage — plain text,
    PDF/DOCX/XLSX, and also images, video, audio, archives (.zip/.tar), and
    APKs. They move raw bytes and never parse or interpret content, so file
    type is never a blocker. (The agent still cannot read or extract text
    from an image/zip/video via this tool — only relocate/duplicate it.
    Don't infer readability from copyability.)

    Args:
        command: A single `cp`, `mv`, or `mkdir` invocation as a plain
                 string, e.g. "cp reports/draft.md reports/draft-backup.md",
                 "mv photo.jpg archive/2026/photo.jpg", or
                 "mkdir reports/q3". `-n`/`-p` are accepted but rarely
                 needed — their behavior is already the default.
    """
    try:
        executable, paths, flags = _validate_fileops_command(command)
    except _CommandValidationError as e:
        return str(e)

    root = get_sandbox_root()

    if executable == "mkdir":
        target = paths[0]
        if target.is_file():
            return (
                f"Refused: '{target.relative_to(root)}' already exists as "
                "a file. Cannot create a directory at that path."
            )
        if target.is_dir():
            return f"Directory '{target.relative_to(root)}' already exists — nothing to do."
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"Could not create directory '{target.relative_to(root)}': {e}"
        return f"Directory '{target.relative_to(root)}' created."

    # cp / mv
    src, dst = paths

    if not src.exists():
        return f"Source '{src.name}' does not exist."
    if not src.is_file():
        return (
            f"Source resolves to a directory. run_file_command only "
            "handles single files for cp/mv, not directories."
        )

    ext = src.suffix.lower()
    if ext not in MANAGEABLE_EXTENSIONS:
        return (
            f"Refused: '{ext}' is not currently a manageable file type in "
            "this sandbox."
        )

    if dst.is_dir():
        return "Refused: destination is an existing directory, not a file path."
    if dst.exists():
        return (
            f"Refused: destination already exists. Pass -n explicitly if "
            "you intend to no-clobber-refuse (same result), or choose a "
            "different destination path — this tool never overwrites."
        )

    src_rel = src.relative_to(root)
    dst_rel = dst.relative_to(root)

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if executable == "cp":
            shutil.copy2(str(src), str(dst))
            return f"Copied '{src_rel}' -> '{dst_rel}' ({dst.stat().st_size:,} bytes)."
        else:  # mv
            shutil.move(str(src), str(dst))
            return f"Moved '{src_rel}' -> '{dst_rel}'."
    except Exception as e:
        return f"Could not run '{command}': {e}"


# ---------------------------------------------------------------------------
# DELETE (soft — trash, never unlink)
# ---------------------------------------------------------------------------

@tool
def delete_file(path: str, dry_run: bool = True) -> str:
    """
    Delete a file — soft delete, moved to .sicily-trash/, never unlinked.
    Works on any manageable file type (not just text/PDF/docx/xlsx).
    dry_run=True (default) previews only; dry_run=False applies.

    Args:
        path:    Relative path to the file to delete.
        dry_run: If True (default), preview only.
    """
    try:
        target = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not target.exists():
        return f"'{path}' does not exist."
    if not target.is_file():
        return f"'{path}' is a directory. Use delete_directory instead."

    ext = target.suffix.lower()
    if ext not in MANAGEABLE_EXTENSIONS:
        return (
            f"Refused: '{ext}' is not currently a manageable file type in "
            "this sandbox."
        )

    if dry_run:
        return (
            f"[DRY RUN — nothing deleted]\n"
            f"Would move '{path}' to {TRASH_DIR_NAME}/.\n"
            "Call again with dry_run=False to apply."
        )

    try:
        trashed = _move_to_trash(target)
    except Exception as e:
        return f"Could not delete '{path}': {e}"

    rel_trashed = trashed.relative_to(get_sandbox_root())
    return f"Deleted '{path}' (moved to '{rel_trashed}')."


@tool
def delete_directory(path: str, recursive: bool = False, dry_run: bool = True) -> str:
    """
    Delete a directory — soft delete, moved to .sicily-trash/, not unlinked.
    Refuses on a non-empty directory unless recursive=True. dry_run=True
    (default) previews contents and effect; dry_run=False applies.

    Args:
        path:      Relative path to the directory to delete.
        recursive: Must be True to delete a non-empty directory.
        dry_run:   If True (default), preview only.
    """
    try:
        target = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not target.exists():
        return f"'{path}' does not exist."
    if not target.is_dir():
        return f"'{path}' is a file. Use delete_file instead."
    if target == get_sandbox_root():
        return "Refused: cannot delete the sandbox root itself."

    contents = list(target.rglob("*"))
    file_count = sum(1 for p in contents if p.is_file())
    dir_count = sum(1 for p in contents if p.is_dir())

    if contents and not recursive:
        return (
            f"Refused: '{path}' is not empty "
            f"({file_count} file(s), {dir_count} subfolder(s)). "
            "Pass recursive=True to confirm you want to delete it all."
        )

    if dry_run:
        root = get_sandbox_root()
        preview = "\n".join(f"  - {p.relative_to(root)}" for p in contents[:30])
        more = f"\n  ... and {len(contents) - 30} more" if len(contents) > 30 else ""
        return (
            f"[DRY RUN — nothing deleted]\n"
            f"Would move '{path}' and its contents "
            f"({file_count} file(s), {dir_count} subfolder(s)) to {TRASH_DIR_NAME}/.\n\n"
            f"{preview}{more}\n\n"
            "Call again with dry_run=False to apply."
        )

    try:
        trashed = _move_to_trash(target)
    except Exception as e:
        return f"Could not delete '{path}': {e}"

    rel_trashed = trashed.relative_to(get_sandbox_root())
    return (
        f"Deleted '{path}' and its contents "
        f"({file_count} file(s), {dir_count} subfolder(s)) — moved to '{rel_trashed}'."
    )


# ---------------------------------------------------------------------------
# SEARCH — three tiers, escalating cost, with the strategy baked into the
# docstrings themselves so the model follows it without being separately
# prompted each time.
# ---------------------------------------------------------------------------

@tool
def find_files_by_name(path: str, pattern: str, exclude_patterns: list[str] = []) -> str:
    """
    Recursively find files by NAME/GLOB (e.g. "*.py", "invoice_*"), not
    content. Returns relative paths only — reads no file content.

    Args:
        path:             Starting directory (relative path).
        pattern:          Glob pattern matched against each entry's name.
        exclude_patterns: Optional glob patterns to exclude, matched
                          against both the entry name and relative path.
    """
    import fnmatch

    try:
        start = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not start.exists():
        return f"Directory '{path}' does not exist."
    if not start.is_dir():
        return f"'{path}' is not a directory."

    root = get_sandbox_root()
    matches: list[str] = []

    def _walk(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return

        for child in children:
            if _is_skipped(child):
                continue

            rel = str(child.relative_to(root))

            if any(
                fnmatch.fnmatch(child.name, xp) or fnmatch.fnmatch(rel, xp)
                for xp in exclude_patterns
            ):
                continue

            if fnmatch.fnmatch(child.name, pattern):
                matches.append(rel)

            if child.is_dir():
                _walk(child)

    _walk(start)

    if not matches:
        return f"No files matching '{pattern}' found under '{path}'."

    return f"Found {len(matches)} match(es):\n" + "\n".join(matches)


@tool
def search_file_contents(
    pattern: str,
    path: str = ".",
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 0,
    match_per_line: bool = True,
    includes: Optional[List[str]] = None,
    max_results: int = 50,
    return_json: bool = False,
) -> str:
    """
    Grep-equivalent literal/regex search inside file content, under `path`
    — plain text/code directly, plus PDF/docx/xlsx via text extraction.

    Args:
        pattern:        Text or regex pattern to search for.
        path:           Directory to search under (relative or absolute). Defaults to ".".
        regex:          If True, `pattern` is treated as a regular expression.
        case_sensitive: If False (default), performs case-insensitive matching.
        context_lines:  Lines of context above/below each match (default 0).
        match_per_line: If True (default), returns matching lines and line numbers.
                        If False, returns only matching file paths (like git grep -l).
        includes:       Optional list of glob patterns to filter files (e.g. ["*.py", "!**/node_modules/*"]).
        max_results:    Stop after this many matches (default 50).
        return_json:    If True, outputs raw JSON objects like grep_search API.
    """
    try:
        start = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not start.exists():
        return f"'{path}' does not exist."
    if not start.is_dir():
        return f"'{path}' is a file, not a directory. Pass a directory to search."

    # Compile regex pattern
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        search_regex = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    root = get_sandbox_root()
    matches = []
    matching_files = set()
    files_scanned = 0
    files_skipped = []

    # Common directory excludes to keep search fast
    DEFAULT_IGNORE_DIRS = SKIP_DIRS

    def _should_include(file_rel_path: str) -> bool:
        if not includes:
            return True
        included = False
        for inc in includes:
            if inc.startswith("!"):
                if fnmatch.fnmatch(file_rel_path, inc[1:]):
                    return False
            else:
                if fnmatch.fnmatch(file_rel_path, inc):
                    included = True
        return included if any(not inc.startswith("!") for inc in includes) else True

    def _iter_files(directory: Path):
        try:
            children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for child in children:
            if child.is_dir():
                if child.name in DEFAULT_IGNORE_DIRS or _is_skipped(child):
                    continue
                yield from _iter_files(child)
            elif child.is_file():
                if _is_skipped(child):
                    continue
                yield child

    for file_path in _iter_files(start):
        if len(matches) >= max_results:
            break

        ext = file_path.suffix.lower()
        if ext not in READABLE_EXTENSIONS:
            continue

        try:
            rel_str = str(file_path.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel_str = str(file_path).replace("\\", "/")

        if not _should_include(rel_str):
            continue

        try:
            if ext in _BINARY_EXTENSIONS:
                text = _read_binary(file_path)
            else:
                text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            files_skipped.append(rel_str)
            continue

        files_scanned += 1
        lines = text.splitlines()

        for i, line in enumerate(lines):
            if len(matches) >= max_results:
                break

            if search_regex.search(line):
                matching_files.add(rel_str)

                # If only file listing requested (git grep -l behavior)
                if not match_per_line:
                    if rel_str not in matches:
                        matches.append(rel_str)
                    break

                line_num = i + 1

                if return_json:
                    matches.append({
                        "Filename": rel_str,
                        "LineNumber": line_num,
                        "LineContent": line.strip()
                    })
                else:
                    if context_lines > 0:
                        lo = max(0, i - context_lines)
                        hi = min(len(lines), i + context_lines + 1)
                        snippet_lines = lines[lo:hi]
                        snippet = "\n".join(
                            f"{'>' if lo + j == i else ' '} {lo + j + 1:>5}  {l}"
                            for j, l in enumerate(snippet_lines)
                        )
                        matches.append(f"[{rel_str}]\n{snippet}")
                    else:
                        matches.append(f"[{rel_str}:{line_num}]  {line.strip()}")

    search_desc = (
        f"Searched for {'regex' if regex else 'literal'} pattern '{pattern}' "
        f"({'case-sensitive' if case_sensitive else 'case-insensitive'}) under '{path}'"
    )

    if not matches:
        note = f" ({len(files_skipped)} file(s) could not be read)" if files_skipped else ""
        return (
            f"{search_desc}\n"
            f"No matches across {files_scanned} readable file(s){note}."
        )

    if return_json:
        return json.dumps(matches, indent=2)

    header = f"{search_desc}\nFound {len(matches)} match(es) across {len(matching_files)} file(s) ({files_scanned} scanned)"
    if len(matches) >= max_results:
        header += f" (capped at max_results={max_results})"
    
    return header + ":\n\n" + "\n\n".join(matches)


FILEOPS_TOOLS = [
    # Search tier (escalating cost — see docstrings for the strategy)
    find_files_by_name,
    search_file_contents,

    # Copy / move / rename — one validated cp/mv command tool (no-clobber
    # by default, replaces copy_file/move_file/rename_file)
    run_file_command,

    # Delete (soft — trash, dry_run by default)
    delete_file,
    delete_directory,
]


FILEOPS_TOOL_STATUS_MAP = {
    "find_files_by_name": lambda args: (
        f"Searching filenames for [white]'{args.get('pattern')}'[/white] "
        f"under [white]'{args.get('path')}'[/white]"
    ),
    "search_file_contents": lambda args: (
        f"Searching for [white]'{args.get('pattern')}'[/white] "
        f"under [white]'{args.get('path', '.')}'[/white]"
    ),
    "run_file_command": lambda args: (
        f"Running [white]'{args.get('command')}'[/white]"
    ),
    "delete_file": lambda args: (
        f"Previewing delete of [white]'{args.get('path')}'[/white]"
        if args.get("dry_run", True)
        else f"Deleting [white]'{args.get('path')}'[/white] (-> trash)"
    ),
    "delete_directory": lambda args: (
        f"Previewing delete of [white]'{args.get('path')}'[/white]"
        if args.get("dry_run", True)
        else f"Deleting [white]'{args.get('path')}'[/white] (-> trash)"
    ),
}
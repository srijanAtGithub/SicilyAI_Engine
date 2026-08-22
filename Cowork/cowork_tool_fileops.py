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
hatch) covering copy/move/rename/mkdir — see its docstring for scope.

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

# Executables this tool will ever run, and the flags each may take.
# Anything not listed here — unknown executables, unknown flags, or
# anything requiring shell features — is rejected before a subprocess
# ever starts.
_ALLOWED_FLAGS = {
    "cp": {"-n", "-r"},
    "mv": {"-n"},
    "mkdir": {"-p"},
}

_ALLOWED_EXECUTABLES = frozenset(_ALLOWED_FLAGS.keys())

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


class _CommandValidationError(ValueError):
    """Raised by _validate_fileops_command when a command fails the
    docstring-declared contract. Message is written to be shown directly
    to the model/user — no internals leak, just the rule that was broken.
    """


def _validate_fileops_command(command: str) -> tuple[str, list[Path], list[str]]:
    """
    Parse and validate a `cp`/`mv`/`mkdir` command string against the
    contract in run_file_command's docstring. Returns (executable,
    resolved_paths, flags) on success:
      - cp/mv: resolved_paths is [src1, src2, ..., dst] (2+ items —
        one or more sources, last item is the destination).
      - mkdir: resolved_paths is [path1, path2, ...] (1+ items).
    Raises _CommandValidationError otherwise.

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
    # Normalize away path prefixes / .exe so "/bin/rm" or "python3.exe"
    # still match the blocklist.
    executable_name = Path(executable).stem.lower()

    if executable_name in _EXPLICITLY_BLOCKED:
        raise _CommandValidationError(
            f"Refused: '{executable}' is a shell, scripting, VCS, package-"
            "manager, or privilege-escalation command. run_file_command "
            "only runs cp, mv, or mkdir."
        )

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

    min_positionals = 1 if executable == "mkdir" else 2
    if len(positionals) < min_positionals:
        noun = "path argument" if executable == "mkdir" else "path arguments (at least one source plus a destination)"
        raise _CommandValidationError(
            f"Refused: expected at least {min_positionals} {noun} for "
            f"'{executable}', got {len(positionals)}: {positionals}."
        )

    for raw in positionals:
        if any(ch in raw for ch in "*?[]"):
            raise _CommandValidationError(
                f"Refused: path '{raw}' contains a glob character "
                "(*, ?, [, ]). There is no shell here to expand globs — "
                "they would be treated as a literal, nonexistent filename. "
                "List each exact path as its own argument instead."
            )

    try:
        resolved = [_safe_path(p) for p in positionals]
    except PermissionError as e:
        raise _CommandValidationError(str(e))

    return executable, resolved, flags


@tool
def run_file_command(command: str) -> str:
    """
    Run a copy, move/rename, or create-directory command inside the
    sandbox: `cp`, `mv`, or `mkdir`. Use this whenever the task is to
    duplicate, relocate, rename, or organize files and folders.

    Handles single or multiple files, single or multiple folders (copied/
    moved recursively), and multiple destinations for mkdir — same as the
    real commands: `cp a.txt b.txt dest/` (2 sources -> a directory),
    `cp -r folder1 folder2 archive/`, `mkdir a b c`. mkdir also accepts a
    single path. Renaming is `mv <old> <new>` in the same folder.

    This is not a general shell — only `cp`, `mv`, `mkdir` are supported,
    no flags beyond `-n`/`-r`/`-p`, no globs (list exact paths instead of
    `*.txt`), no piping or chaining. Every path must resolve inside the
    sandbox.

    Args:
        command: A single cp/mv/mkdir invocation, e.g.
                 "cp reports/draft.md reports/draft-backup.md",
                 "cp report.pdf photo.jpg archive/2026/",
                 "mv -r old_project new_project",
                 "mkdir reports/q3 reports/q4".
    """
    try:
        executable, paths, flags = _validate_fileops_command(command)
    except _CommandValidationError as e:
        return str(e)

    root = get_sandbox_root()
    no_clobber = "-n" in flags

    if executable == "mkdir":
        results = []
        for target in paths:
            if target.is_file():
                results.append(f"'{target.relative_to(root)}': refused — already exists as a file.")
                continue
            if target.is_dir():
                results.append(f"'{target.relative_to(root)}': already exists — nothing to do.")
                continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                results.append(f"'{target.relative_to(root)}': created.")
            except Exception as e:
                results.append(f"'{target.relative_to(root)}': could not create — {e}.")
        return "\n".join(results)

    # cp / mv — one or more sources, last positional is the destination.
    *sources, dst = paths
    multi_source = len(sources) > 1

    # With 2+ sources, the destination must be a directory (create it if
    # it doesn't exist yet, same as real cp/mv). With exactly 1 source,
    # dst may be either a directory (item goes inside it) or a new path
    # (item is placed/renamed at that exact path).
    if multi_source:
        if dst.exists() and not dst.is_dir():
            return (
                f"Refused: with multiple sources the destination "
                f"'{dst.relative_to(root)}' must be a directory."
            )
        try:
            dst.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"Could not create destination directory '{dst.relative_to(root)}': {e}"

    results = []
    for src in sources:
        if not src.exists():
            results.append(f"'{src.name}': source does not exist.")
            continue

        is_dir = src.is_dir()
        if is_dir and executable == "cp" and "-r" not in flags:
            results.append(
                f"'{src.relative_to(root)}': is a directory — pass -r to copy it "
                "(e.g. \"cp -r folder dest\")."
            )
            continue
        if not is_dir:
            ext = src.suffix.lower()
            if ext not in MANAGEABLE_EXTENSIONS:
                results.append(f"'{src.relative_to(root)}': refused — '{ext}' is not a manageable file type.")
                continue

        # Resolve final destination path for this item.
        item_dst = (dst / src.name) if (multi_source or dst.is_dir()) else dst

        if item_dst.exists():
            if no_clobber:
                results.append(f"'{src.relative_to(root)}': skipped (destination exists, -n set).")
                continue
            results.append(
                f"'{src.relative_to(root)}': refused — destination "
                f"'{item_dst.relative_to(root)}' already exists (this tool never overwrites)."
            )
            continue

        src_rel = src.relative_to(root)
        try:
            item_dst.parent.mkdir(parents=True, exist_ok=True)
            if executable == "cp":
                if is_dir:
                    shutil.copytree(str(src), str(item_dst))
                    results.append(f"Copied '{src_rel}' -> '{item_dst.relative_to(root)}' (directory).")
                else:
                    shutil.copy2(str(src), str(item_dst))
                    results.append(f"Copied '{src_rel}' -> '{item_dst.relative_to(root)}' ({item_dst.stat().st_size:,} bytes).")
            else:  # mv
                shutil.move(str(src), str(item_dst))
                results.append(f"Moved '{src_rel}' -> '{item_dst.relative_to(root)}'.")
        except Exception as e:
            results.append(f"'{src_rel}': could not {executable} — {e}")

    return "\n".join(results)


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


# Hard server-side ceilings — independent of whatever the caller passes.
# These exist because an unscoped search_file_contents call (broad path,
# no includes, high max_results) can otherwise walk and return a large
# fraction of a monorepo in one call. Args are clamped, not rejected, so
# a call never fails — it just can't blow the budget.
_MAX_RESULTS_CEILING = 40
_MAX_FILES_SCANNED = 400          # stop walking after this many readable files, matches or not
_MAX_CONTEXT_LINES = 4
_MAX_OUTPUT_CHARS = 6_000         # hard cap on the returned string; truncated with a note past this


@tool
def search_file_contents(
    pattern: str,
    path: str = ".",
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 0,
    match_per_line: bool = True,
    includes: Optional[List[str]] = None,
    max_results: int = 20,
    return_json: bool = False,
) -> str:
    """
    Grep-equivalent literal/regex search inside file content, under `path`
    — plain text/code directly, plus PDF/docx/xlsx via text extraction.

    Scope this tightly: pass the narrowest `path` and `includes` the
    evidence supports, rather than searching the whole tree with a wide
    alternation pattern. Results, files scanned, and context are all
    capped server-side (max_results<=40, ~400 files walked, context<=4
    lines, output truncated past ~6000 chars) — an unscoped call will be
    clamped and truncated rather than returning everything, so a narrow
    query is the only way to get complete results back. If a first
    targeted search comes up empty, widen path/pattern deliberately on
    the next call rather than starting broad.

    Args:
        pattern:        Text or regex pattern to search for.
        path:           Directory to search under (relative or absolute). Defaults to ".".
        regex:          If True, `pattern` is treated as a regular expression.
        case_sensitive: If False (default), performs case-insensitive matching.
        context_lines:  Lines of context above/below each match (default 0, max 4).
        match_per_line: If True (default), returns matching lines and line numbers.
                        If False, returns only matching file paths (like git grep -l) — cheaper,
                        prefer this to locate candidate files before requesting line content.
        includes:       Optional list of glob patterns to filter files (e.g. ["*.py", "!**/node_modules/*"]).
                        Strongly recommended whenever you have any hint about file type or area.
        max_results:    Stop after this many matches (default 20, hard cap 40).
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

    # Clamp caller-supplied limits to server-side ceilings rather than
    # trusting them — this is what actually bounds worst-case cost.
    max_results = max(1, min(max_results, _MAX_RESULTS_CEILING))
    context_lines = max(0, min(context_lines, _MAX_CONTEXT_LINES))

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
    scan_capped = False

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
        if files_scanned >= _MAX_FILES_SCANNED:
            scan_capped = True
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
    cap_note = (
        f" (stopped after scanning {_MAX_FILES_SCANNED} files — narrow `path`/`includes` "
        "to search the rest)" if scan_capped else ""
    )

    if not matches:
        note = f" ({len(files_skipped)} file(s) could not be read)" if files_skipped else ""
        return (
            f"{search_desc}\n"
            f"No matches across {files_scanned} readable file(s){note}{cap_note}."
        )

    if return_json:
        out = json.dumps(matches, indent=2)
        if len(out) > _MAX_OUTPUT_CHARS:
            out = out[:_MAX_OUTPUT_CHARS] + f"\n... [truncated at {_MAX_OUTPUT_CHARS} chars — narrow the query for full results]"
        return out

    header = f"{search_desc}\nFound {len(matches)} match(es) across {len(matching_files)} file(s) ({files_scanned} scanned{cap_note})"
    if len(matches) >= max_results:
        header += f" (capped at max_results={max_results})"

    body = "\n\n".join(matches)
    if len(body) > _MAX_OUTPUT_CHARS:
        body = body[:_MAX_OUTPUT_CHARS] + f"\n... [truncated at {_MAX_OUTPUT_CHARS} chars — narrow `path`/`includes`/`pattern` for full results]"

    return header + ":\n\n" + body


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
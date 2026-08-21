"""
cowork_tools_fileops.py
------------------------
File-management and content-search tools for Sicily Cowork.

Extends cowork_tools.py with operations beyond read/write:
  - run_file_command                    : copy/move/rename files by running
                                           a validated `cp` or `mv` command —
                                           replaces the old separate
                                           copy_file/move_file/rename_file
                                           tools with one CLI-shaped tool
                                           (same pattern used by Antigravity
                                           and similar agentic IDEs: one
                                           narrow, whitelisted command
                                           executor instead of N bespoke
                                           tools for related operations).
  - delete_file, delete_directory       : soft-delete (trash, not unlink) —
                                           deliberately NOT folded into
                                           run_file_command; see note below.
  - search_file_contents                : grep-equivalent, scoped to ALL
                                           readable extensions (plain text +
                                           PDF/docx/xlsx via the existing
                                           binary parsers) — not just the
                                           narrower RAG-indexed subset.
  - find_files_by_name                  : renamed `search_files` from
                                           cowork_tools.py (filename/glob
                                           match only, no content reading).
  - preview_files_for_review            : batched multi-file preview — the
                                           deliberate fallback step for
                                           vague/fuzzy queries that neither
                                           search_index nor
                                           search_file_contents can resolve
                                           on their own.

Scope (intentional)
--------------------
Two different scopes apply within this module, gated by two different sets:

  CONTENT tools (search_file_contents, preview_files_for_review) only work
  on READABLE_EXTENSIONS — the extensions in ALLOWED_WRITE_EXTENSIONS plus
  the binary-but-parseable formats in _BINARY_EXTENSIONS (.pdf, .docx, .doc,
  .xlsx, .xls). There is no parser for images/video/audio/archives/APKs, so
  these tools genuinely cannot do anything with them and correctly refuse.

  MANAGEMENT tools (run_file_command's cp/mv, delete_file,
  delete_directory) work on the broader MANAGEABLE_EXTENSIONS — READABLE_EXTENSIONS
  plus MANAGEABLE_ONLY_EXTENSIONS (images, video, audio, archives/.zip/.tar,
  APKs, and other common binaries). These ops are pure shutil/Path filesystem
  calls that never open or interpret file content, so there's no technical
  reason to block them on file type. delete_directory never had an extension
  gate at all (it moves whole trees, mixed contents and all).

In short: the agent can organize (copy/move/rename/delete) any file in the
sandbox, but can only read/search the content of text + PDF/docx/xlsx.
Operations refuse on genuinely out-of-scope extensions (e.g. an unrecognized
proprietary format) with a clear message rather than silently mishandling
them — extend MANAGEABLE_ONLY_EXTENSIONS if a new type should become
manageable.

IMPORTANT — this is a DIFFERENT (broader) scope than write_file/edit_file_lines
in cowork_tools.py. Those tools exclude non-text formats entirely because
overwriting content requires structured serialisation, not raw text I/O.
That restriction does NOT apply here. run_file_command's cp/mv and
delete_file are pure filesystem operations (shutil.copy2/shutil.move under
the hood) — they never open, parse, or rewrite the file's content, so file
format is irrelevant to them, whether that's .pdf/.docx/.xlsx or
.png/.zip/.mp4/.apk. If you're about to tell the user a file "can't be
moved/copied/renamed/deleted because it's binary" or "because it's an
image/video/archive" — that's wrong for the tools in this module. Just call
the tool and trust its actual return value instead of pre-deciding it will
fail. The one thing these tools still cannot do is show you what's INSIDE an
image/video/audio/archive — that requires a parser this module doesn't have
(see READABLE_EXTENSIONS above).

Why cp/mv are a single CLI-shaped tool instead of three bespoke ones
----------------------------------------------------------------------
copy_file, move_file, and rename_file used to be three separate @tool
functions that each re-implemented the same source/destination validation
around a one-line shutil call. They're collapsed into a single
run_file_command tool that accepts a `cp <source> <destination>` or
`mv <source> <destination>` command string, because:

  - Fewer near-duplicate tool schemas for the model to choose between
    (rename is just `mv` with the destination in the same folder — it was
    never a functionally distinct operation).
  - This mirrors the pattern used by Antigravity-style agentic IDEs: one
    narrow, whitelisted "run this exact class of command" executor, rather
    than a bespoke tool per verb.

This is NOT a general shell escape hatch. run_file_command does not use
shell=True, does not go through /bin/sh, and does not support pipes,
redirects, globs, chaining, or any command other than `cp`/`mv`. The
command string is parsed with shlex (no shell semantics), the executable
must be exactly "cp" or "mv", every flag must be on an explicit allow-list,
and every path argument is re-resolved through _safe_path() before
subprocess.run() ever sees it. A hallucinated flag or an out-of-sandbox
path is rejected before execution — never silently passed through to a
real shell where it could do something unintended.

Safety model (matches cowork_tools.py conventions)
----------------------------------------------------
  - Every path goes through the same _safe_path() sandbox check used
    everywhere else — nothing here can escape the sandbox root.
  - run_file_command validates the parsed command against its own
    docstring-declared contract (allowed executables, allowed flags,
    exactly two path arguments) before running anything — see the
    function's docstring and _validate_fileops_command() below.
  - Destructive ops (delete_*) never hard-unlink. They move the target into
    a hidden sandbox-local trash folder (.sicily-trash/), preserving
    relative structure, so a wrong call is always recoverable by hand.
  - Destructive and relocating-into-existing-path ops follow the same
    dry_run=True-by-default pattern as edit_file_lines: preview first,
    apply only once the caller explicitly passes dry_run=False.
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
    "cp": {"-n"},   # -n: no-clobber (never overwrite silently) — the only
                    #     flag exposed; there is no -f, no -r (directories
                    #     go through delete_directory/dedicated handling,
                    #     not this tool).
    "mv": {"-n"},   # -n: no-clobber. Same rationale as cp.
}

_ALLOWED_EXECUTABLES = frozenset(_ALLOWED_FLAGS.keys())


class _CommandValidationError(ValueError):
    """Raised by _validate_fileops_command when a command fails the
    docstring-declared contract. Message is written to be shown directly
    to the model/user — no internals leak, just the rule that was broken.
    """


def _validate_fileops_command(command: str) -> tuple[str, Path, Path, bool]:
    """
    Parse and validate a `cp`/`mv` command string against the exact
    contract documented in run_file_command's docstring. This is the
    enforcement point that keeps the tool from becoming a general shell
    escape hatch: nothing here trusts the model's command string beyond
    what is explicitly re-checked.

    Returns (executable, src_path, dst_path, no_clobber) on success.
    Raises _CommandValidationError with a human-readable reason on any
    violation — unknown executable, disallowed flag, wrong argument count,
    or a path that resolves outside the sandbox.

    Deliberately does NOT use shell=True / a real shell anywhere in this
    module. shlex.split() gives POSIX-ish tokenization (handles quoting)
    without ever invoking /bin/sh, so there is no pipe, redirect, glob,
    `;`, `&&`, backtick, or env-var expansion for a hallucinated or
    adversarial command to exploit.
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
            f"'{executable}'. Only {sorted(allowed_flags)} are permitted — "
            "this tool only does single-file copy/move/rename, nothing "
            "recursive or forced."
        )

    if len(positionals) != 2:
        raise _CommandValidationError(
            f"Refused: expected exactly 2 path arguments (source, "
            f"destination) for '{executable}', got {len(positionals)}: "
            f"{positionals}. No globs, no multi-destination forms."
        )

    source, destination = positionals

    for label, raw in (("source", source), ("destination", destination)):
        if any(ch in raw for ch in "*?[]"):
            raise _CommandValidationError(
                f"Refused: {label} '{raw}' contains a glob character "
                "(*, ?, [, ]). There is no shell here to expand globs — "
                "they would be treated as a literal, nonexistent filename. "
                "Pass one exact path instead."
            )

    try:
        src = _safe_path(source)
        dst = _safe_path(destination)
    except PermissionError as e:
        raise _CommandValidationError(str(e))

    no_clobber = "-n" in flags
    return executable, src, dst, no_clobber


@tool
def run_file_command(command: str) -> str:
    """
    Copy, move, or rename a file by running a validated `cp` or `mv`
    command. This is the ONLY way to copy/move/rename in this sandbox —
    it replaces the old copy_file/move_file/rename_file tools. Renaming is
    just `mv <old-path> <new-path-same-folder>`; there is no separate verb
    for it.

    This is NOT a general shell tool. Only these exact forms are accepted:

        cp [-n] <source> <destination>
        mv [-n] <source> <destination>

    Hard contract (violating ANY of these gets the command rejected before
    anything runs — no partial execution, no fallback interpretation):
      - The executable must be exactly "cp" or "mv". Nothing else — not
        "cp -r", not "rsync", not "cp file1 file2 dir/", not any
        pipe/redirect/chain (`|`, `>`, `;`, `&&`, backticks, etc.).
      - The only flag either command accepts is `-n` (no-clobber — refuse
        to overwrite an existing destination). Any other flag, including
        ones that look reasonable (`-r`, `-f`, `-v`, `--force`), is
        refused. There is no recursive/directory form in this tool —
        directories are out of scope here.
      - Exactly two path arguments: source, then destination. No globs
        (`*.txt`), no multiple sources, no trailing-slash directory-target
        shorthand.
      - Both paths must resolve inside the sandbox (same _safe_path()
        check every other tool in this module uses) and the source file's
        extension must be in MANAGEABLE_EXTENSIONS.
      - Without `-n`, an existing destination file is refused rather than
        silently overwritten — same no-clobber-by-default behavior the
        old copy_file/move_file had with overwrite=False.

    If you're unsure whether a command you're about to write is valid,
    write the simplest possible form — `cp source.txt dest.txt` or
    `mv old/path.pdf new/path.pdf` — rather than guessing at flags. An
    invalid command is rejected with a clear reason and nothing happens;
    it never partially runs.

    Works on ANY file this sandbox can manage — plain text, PDF/DOCX/XLSX,
    and also images, video, audio, archives (.zip/.tar), and APKs. cp/mv
    move raw bytes; they never parse or interpret content, so file type is
    never a blocker for this tool. (The agent still cannot read or extract
    text from an image/zip/video via this tool — only relocate/duplicate
    it. Don't infer readability from copyability.)

    Args:
        command: A single `cp` or `mv` invocation as a plain string, e.g.
                 "cp reports/draft.md reports/draft-backup.md" or
                 "mv photo.jpg archive/2026/photo.jpg". `-n` is accepted
                 but not required — an existing destination is always
                 refused regardless, so you normally don't need to pass it.
    """
    try:
        executable, src, dst, no_clobber = _validate_fileops_command(command)
    except _CommandValidationError as e:
        return str(e)

    if not src.exists():
        return f"Source '{src.name}' does not exist."
    if not src.is_file():
        return (
            f"Source resolves to a directory. run_file_command only "
            "handles single files, not directories."
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

    root = get_sandbox_root()
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
    Delete a file. This NEVER permanently destroys data — the file is moved
    into a hidden sandbox-local trash folder (.sicily-trash/), not unlinked.
    It can always be recovered by hand afterward.

    Works on ANY manageable file type, including images, video, audio,
    archives, and APKs — not just text/PDF/docx/xlsx. This is a filesystem
    move (shutil.move into trash), never a content operation.

    Safety design (matches edit_file_lines)
    ----------------------------------------
    - dry_run=True (default): reports what WOULD happen, writes nothing.
    - dry_run=False: actually moves the file to trash. Only use after the
      user has confirmed the dry-run preview is what they want.

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
    Delete a directory. Like delete_file, this is non-destructive — the
    whole directory is moved into .sicily-trash/, not unlinked.

    Safety design
    --------------
    - Refuses on a non-empty directory unless `recursive=True` is passed —
      a separate, louder guard from dry_run, so an accidental "delete this
      folder" can't silently wipe out more than the caller expected.
    - dry_run=True (default): lists what's inside and what would happen,
      writes nothing.
    - dry_run=False: actually moves the directory to trash.

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
    STEP 1 of file discovery — find files by NAME/GLOB, not content.
    Recursively matches filenames against a glob pattern (e.g. "*.py",
    "invoice_*", "*.pdf"). Returns relative paths only — never opens or
    reads file content.

    Use this first whenever there's any hint about filename, folder naming
    convention, or extension (e.g. "find anything that looks like an
    invoice" -> pattern="*invoice*" or "*receipt*"). It's the cheapest
    possible search: cost is proportional to match count, not tree size.

    How this fits with the other search tools
    --------------------------------------------
      search_index             -> meaning/concepts, INDEXED types only
                                   (.txt .md .pdf .docx .xlsx .csv ...)
      find_files_by_name (this)-> filename/glob match, ALL file types,
                                   reads no content
      search_file_contents     -> exact/regex match INSIDE file content,
                                   ALL readable types incl. code
      preview_files_for_review -> last resort: open a shortlist of files
                                   and reason over their content directly

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
    STEP 2 of file discovery — grep-equivalent EXACT/PATTERN search INSIDE
    file content (powered by literal/regex matching).

    Searches readable files under `path` — plain text/code files directly,
    plus PDF/docx/xlsx via standard text extraction.

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
    DEFAULT_IGNORE_DIRS = {".git", ".vs", ".vscode", "node_modules", "__pycache__", "dist", "build", ".venv"}

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
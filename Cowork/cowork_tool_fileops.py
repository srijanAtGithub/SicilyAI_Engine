"""
cowork_tools_fileops.py
------------------------
File-management and content-search tools for Sicily Cowork.

Extends cowork_tools.py with operations beyond read/write:
  - copy_file, move_file, rename_file   : relocate/duplicate files
  - delete_file, delete_directory       : soft-delete (trash, not unlink)
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

  MANAGEMENT tools (copy_file, move_file, rename_file, delete_file,
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
That restriction does NOT apply here. copy_file, move_file, rename_file, and
delete_file are pure filesystem operations (shutil.copy2/shutil.move) — they
never open, parse, or rewrite the file's content, so file format is
irrelevant to them, whether that's .pdf/.docx/.xlsx or .png/.zip/.mp4/.apk.
If you're about to tell the user a file "can't be moved/copied/renamed/deleted
because it's binary" or "because it's an image/video/archive" — that's wrong
for the tools in this module. Just call the tool and trust its actual return
value instead of pre-deciding it will fail. The one thing these tools still
cannot do is show you what's INSIDE an image/video/audio/archive — that
requires a parser this module doesn't have (see READABLE_EXTENSIONS above).

Safety model (matches cowork_tools.py conventions)
----------------------------------------------------
  - Every path goes through the same _safe_path() sandbox check used
    everywhere else — nothing here can escape the sandbox root.
  - Destructive ops (delete_*) never hard-unlink. They move the target into
    a hidden sandbox-local trash folder (.sicily-trash/), preserving
    relative structure, so a wrong call is always recoverable by hand.
  - Destructive and relocating-into-existing-path ops follow the same
    dry_run=True-by-default pattern as edit_file_lines: preview first,
    apply only once the caller explicitly passes dry_run=False.
"""

import re
import shutil
import time
from pathlib import Path

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
# COPY / MOVE / RENAME
# ---------------------------------------------------------------------------

@tool
def copy_file(source: str, destination: str, overwrite: bool = False) -> str:
    """
    Copy a file to a new location within the sandbox. The source is left
    untouched — this only duplicates it.

    Works on ANY file this sandbox can manage — plain text, PDF/DOCX/XLSX,
    and also images, video, audio, archives (.zip/.tar), and APKs. This
    copies raw bytes (shutil.copy2); it never parses or interprets content,
    so file type is never a blocker for this tool. (The agent cannot read
    or extract text from an image/zip/video — only move it around. Don't
    infer readability from copyability.)

    Safety guarantees
    ------------------
    - Both `source` and `destination` must resolve inside the sandbox.
    - Refuses to overwrite an existing file at `destination` unless
      `overwrite=True` is explicitly passed.
    - Parent directories of `destination` are created automatically.

    Args:
        source:      Relative path to the existing file.
        destination: Relative path to copy it to, including filename.
        overwrite:   If True, replaces an existing file at destination.
                     Default False (refuses instead).
    """
    try:
        src = _safe_path(source)
        dst = _safe_path(destination)
    except PermissionError as e:
        return str(e)

    if not src.exists():
        return f"Source '{source}' does not exist."
    if not src.is_file():
        return f"Source '{source}' is a directory. copy_file only handles files."

    ext = src.suffix.lower()
    if ext not in MANAGEABLE_EXTENSIONS:
        return (
            f"Refused: '{ext}' is not currently a manageable file type in "
            "this sandbox."
        )

    if dst.exists() and not overwrite:
        return (
            f"Refused: '{destination}' already exists. "
            "Pass overwrite=True to replace it."
        )
    if dst.is_dir():
        return f"Refused: '{destination}' is an existing directory, not a file path."

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
    except Exception as e:
        return f"Could not copy '{source}' -> '{destination}': {e}"

    return f"Copied '{source}' -> '{destination}' ({dst.stat().st_size:,} bytes)."


@tool
def move_file(source: str, destination: str, overwrite: bool = False) -> str:
    """
    Move (relocate into a different folder) an existing file within the
    sandbox. For renaming a file in place, prefer `rename_file` — same
    underlying operation, but the name better matches that intent.

    Works on ANY manageable file type — plain text, PDF/DOCX/XLSX, and also
    images, video, audio, archives, and APKs. This is a filesystem
    relocation, not a content rewrite, so file type is never a blocker.
    (Don't confuse this with write_file/edit_file_lines, which are
    text-only for a different reason — they rewrite content. Call this
    tool on binary or media files directly rather than assuming it will
    fail.)

    Safety guarantees
    ------------------
    - Both `source` and `destination` must resolve inside the sandbox.
    - Refuses to overwrite an existing file at `destination` unless
      `overwrite=True` is explicitly passed.
    - Parent directories of `destination` are created automatically.

    Args:
        source:      Relative path to the existing file.
        destination: Relative path to move it to, including filename.
        overwrite:   If True, replaces an existing file at destination.
                     Default False (refuses instead).
    """
    try:
        src = _safe_path(source)
        dst = _safe_path(destination)
    except PermissionError as e:
        return str(e)

    if not src.exists():
        return f"Source '{source}' does not exist."
    if not src.is_file():
        return f"Source '{source}' is a directory. Use delete_directory/copy logic for folders."

    ext = src.suffix.lower()
    if ext not in MANAGEABLE_EXTENSIONS:
        return (
            f"Refused: '{ext}' is not currently a manageable file type in "
            "this sandbox."
        )

    if dst.exists() and not overwrite:
        return (
            f"Refused: '{destination}' already exists. "
            "Pass overwrite=True to replace it."
        )
    if dst.is_dir():
        return f"Refused: '{destination}' is an existing directory, not a file path."

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except Exception as e:
        return f"Could not move '{source}' -> '{destination}': {e}"

    return f"Moved '{source}' -> '{destination}'."


@tool
def rename_file(path: str, new_name: str) -> str:
    """
    Rename a file in place, keeping it in the same folder. A thin,
    clearer-intent wrapper over move_file for the common "just rename it"
    case — use move_file instead if you also need to relocate it to a
    different folder.

    Args:
        path:     Relative path to the existing file.
        new_name: New filename ONLY (no slashes) — e.g. "final_report.md"
                  or "vacation_photo.jpg". Extension is optional — if
                  omitted, the source file's current extension is kept
                  (e.g. "test2" on "test.py" becomes "test2.py"). If
                  provided, it must be a currently manageable extension
                  (this includes images, video, audio, archives, and APKs
                  — not just text/PDF/docx/xlsx).
    """
    if "/" in new_name or "\\" in new_name:
        return (
            "Refused: new_name must be a filename only, not a path. "
            "Use move_file if you need to change the folder as well."
        )

    try:
        src = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not src.exists():
        return f"'{path}' does not exist."
    if not src.is_file():
        return f"'{path}' is a directory. rename_file only handles files."

    new_name_path = Path(new_name)
    if new_name_path.suffix:
        new_ext = new_name_path.suffix.lower()
        if new_ext not in MANAGEABLE_EXTENSIONS:
            return f"Refused: '{new_ext}' is not currently a manageable file type."
        final_name = new_name
    else:
        # No extension given — most natural reading of "rename X to Y" is
        # "keep it the same kind of file, just change the name." Inherit
        # the source file's existing (already-valid) extension rather than
        # rejecting; this only kicks in when new_name has no suffix at all —
        # an explicitly wrong extension is still refused above.
        final_name = new_name + src.suffix

    dest_rel = str(Path(path).parent / final_name)

    try:
        dst = _safe_path(dest_rel)
    except PermissionError as e:
        return str(e)

    if dst.exists():
        return f"Refused: '{dest_rel}' already exists."

    try:
        shutil.move(str(src), str(dst))
    except Exception as e:
        return f"Could not rename '{path}' -> '{final_name}': {e}"

    return f"Renamed '{path}' -> '{dest_rel}'."


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
    max_results: int = 50,
) -> str:
    """
    STEP 2 of file discovery — grep-equivalent EXACT/PATTERN search INSIDE
    file content. This is a literal text/regex matcher, NOT semantic search.

    Searches every readable file under `path` — plain text/code files
    directly, plus PDF/docx/xlsx via the same extraction read_file uses.
    This is the key difference from search_index: search_index only covers
    the narrower set of file types embedded into the RAG index at startup
    (general documents), and answers conceptual/meaning-based questions.
    This tool answers "does this exact string/pattern appear anywhere",
    across EVERY readable file type — including source code, .json, .log,
    and other extensions search_index intentionally skips.

    WHEN TO USE WHICH SEARCH TOOL
    --------------------------------
      "What does the contract say about termination?"   -> search_index
      "Find the function that calculates shipping cost" -> search_file_contents
                                                              (try "shipping" plain,
                                                              or regex="def.*shipping")
      "Is there a file mentioning invoice INV-2291?"     -> search_file_contents
                                                              (exact ID -> literal match)
      "Is there a bill for around ₹15,000?"              -> NEITHER tool reliably
                                                              finds this alone —
                                                              see escalation path below.

    ESCALATION PATH for vague / fuzzy / numeric queries
    --------------------------------------------------------
    A literal pattern has zero recall on phrasing that can't be predicted
    exactly (e.g. "₹15,000" vs "Rs. 14,850" vs "fifteen thousand", or a
    scanned PDF where the number isn't extractable text at all). When a
    query is inherently fuzzy, don't just retry the same literal pattern —
    work through this in order:
      1. Try a handful of LIKELY literal variants in one or two calls
         (e.g. "15000", "15,000", "15k" — or with regex=True a range like
         "1[45][0-9]{3}" for "around 15000").
      2. If that turns up nothing, narrow the candidate set structurally
         first — use find_files_by_name on filenames/folders that
         plausibly relate (e.g. "*invoice*", "*bill*", a "Receipts"
         folder) rather than scanning the whole sandbox blindly.
      3. Pass that shortlist to preview_files_for_review and reason over
         the actual content yourself — this is the only way to catch
         paraphrased amounts, rounded figures, or non-numeral phrasing.
      4. If the shortlist is still too large to read (dozens+ of
         candidates) and you're not converging, it is more honest and
         cheaper to ask the user a clarifying question (rough date,
         vendor, folder) than to brute-force read everything.

    Args:
        pattern:        Text to search for. Treated as a literal substring
                         unless regex=True.
        path:           Directory to search under (relative). Defaults to
                         the sandbox root.
        regex:          If True, `pattern` is compiled as a regular
                         expression instead of matched literally.
        case_sensitive: Default False (matches grep -i, usually what's
                         wanted for natural-language-ish queries).
        context_lines:  Lines of surrounding context above/below each
                         match (like grep -C). Default 0.
        max_results:    Stop after this many matches, to avoid flooding
                         context on a common pattern. Default 50.
    """
    try:
        start = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not start.exists():
        return f"'{path}' does not exist."
    if not start.is_dir():
        return f"'{path}' is a file, not a directory. Pass a directory to search."

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    root = get_sandbox_root()
    matches: list[str] = []
    files_scanned = 0
    files_skipped: list[str] = []

    def _iter_files(directory: Path):
        try:
            children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for child in children:
            if _is_skipped(child):
                continue
            if child.is_dir():
                yield from _iter_files(child)
            elif child.is_file():
                yield child

    for file_path in _iter_files(start):
        if len(matches) >= max_results:
            break

        ext = file_path.suffix.lower()
        if ext not in READABLE_EXTENSIONS:
            continue

        try:
            if ext in _BINARY_EXTENSIONS:
                text = _read_binary(file_path)
            else:
                text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            files_skipped.append(str(file_path.relative_to(root)))
            continue

        files_scanned += 1
        lines = text.splitlines()

        for i, line in enumerate(lines):
            if len(matches) >= max_results:
                break
            if compiled.search(line):
                rel = file_path.relative_to(root)
                lo = max(0, i - context_lines)
                hi = min(len(lines), i + context_lines + 1)
                snippet_lines = lines[lo:hi]
                snippet = "\n".join(
                    f"{'>' if lo + j == i else ' '} {lo + j + 1:>5}  {l}"
                    for j, l in enumerate(snippet_lines)
                )
                matches.append(f"[{rel}]\n{snippet}")

    if not matches:
        note = f" ({len(files_skipped)} file(s) could not be read)" if files_skipped else ""
        return (
            f"No matches for '{pattern}' across {files_scanned} readable file(s) "
            f"under '{path}'{note}.\n"
            "If this query is vague, numeric, or paraphrased, see the "
            "ESCALATION PATH in this tool's description — try "
            "find_files_by_name to narrow candidates, then "
            "preview_files_for_review."
        )

    header = f"Found {len(matches)} match(es) across {files_scanned} file(s) scanned"
    if len(matches) >= max_results:
        header += f" (capped at max_results={max_results}, there may be more)"
    return header + ":\n\n" + "\n\n".join(matches)


@tool
def preview_files_for_review(paths: list[str], max_lines_each: int = 40) -> str:
    """
    STEP 3 (last resort) of file discovery — open several candidate files
    at once and return their content so YOU can reason over it directly.

    Use this only after both search_index and search_file_contents have
    failed to find what the user wants — typically because the query is
    vague, numeric, paraphrased, or about a file type outside the RAG
    index. This is the fallback that catches things literal/semantic
    search both miss: "a bill for around ₹15,000" might actually say
    "Rs. 14,850" or "fourteen thousand eight hundred fifty" — no pattern
    match or embedding reliably surfaces that, but reading the actual text
    will.

    This batches up to ~15 files into ONE call (vs. N separate read_file
    calls), each truncated to max_lines_each, so multiple candidates can
    be compared in a single reasoning pass instead of paying a full round
    trip per file.

    Keep the candidate list as SMALL and well-justified as possible — this
    is the most expensive search tool in token terms. Narrow with
    find_files_by_name first (filename hints, folder, extension) rather
    than passing an unfiltered directory listing. If the candidates can't
    reasonably be narrowed below a manageable shortlist (dozens+), it is
    cheaper and more reliable to ask the user a clarifying question
    (rough date, vendor, folder) than to brute-force preview everything.

    Args:
        paths:          Relative paths to preview, recommended 3-15 files.
        max_lines_each: Max lines read per file (default 40). Raise this
                         only for the one or two files most suspected.
    """
    if not paths:
        return "No paths provided."
    if len(paths) > 20:
        return (
            f"Refused: {len(paths)} files requested in one call — too many "
            "to reason over reliably. Narrow the candidate list first (e.g. "
            "with find_files_by_name) and pass 15 or fewer."
        )

    sections = []

    for p in paths:
        try:
            target = _safe_path(p)
        except PermissionError as e:
            sections.append(f"[{p}]\n  {e}")
            continue

        if not target.exists():
            sections.append(f"[{p}]\n  (does not exist)")
            continue
        if not target.is_file():
            sections.append(f"[{p}]\n  (is a directory, skipped)")
            continue

        ext = target.suffix.lower()
        if ext not in READABLE_EXTENSIONS:
            sections.append(f"[{p}]\n  (extension '{ext}' not yet supported, skipped)")
            continue

        try:
            if ext in _BINARY_EXTENSIONS:
                text = _read_binary(target)
            else:
                text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            sections.append(f"[{p}]\n  (could not read: {e})")
            continue

        lines = text.splitlines()
        preview = "\n".join(lines[:max_lines_each])
        truncated_note = (
            f"\n  ... ({len(lines) - max_lines_each} more line(s) — use "
            "read_file for the full content if this looks like the match)"
            if len(lines) > max_lines_each else ""
        )
        sections.append(f"[{p}] ({len(lines)} lines)\n{preview}{truncated_note}")

    return "\n\n---\n\n".join(sections)


FILEOPS_TOOLS = [
    # Search tier (escalating cost — see docstrings for the strategy)
    find_files_by_name,
    search_file_contents,
    preview_files_for_review,

    # Copy / move / rename (safe-ish — no-clobber by default)
    copy_file,
    move_file,
    rename_file,

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
    "preview_files_for_review": lambda args: (
        f"Opening {len(args.get('paths', []))} candidate file(s) for review"
    ),
    "copy_file": lambda args: (
        f"Copying [white]'{args.get('source')}'[/white] -> "
        f"[white]'{args.get('destination')}'[/white]"
    ),
    "move_file": lambda args: (
        f"Moving [white]'{args.get('source')}'[/white] -> "
        f"[white]'{args.get('destination')}'[/white]"
    ),
    "rename_file": lambda args: (
        f"Renaming [white]'{args.get('path')}'[/white] -> "
        f"[white]'{args.get('new_name')}'[/white]"
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
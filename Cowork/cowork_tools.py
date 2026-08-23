"""
cowork_tools.py
--------------
Sandboxed filesystem tools for `sicily start`.

All tools are locked to a single root directory (the cwd `sicily start` was invoked from); no path can escape it.
"""

import re
import shutil
from pathlib import Path
import json
import fnmatch
from typing import List, Optional, Union

from langchain_core.tools import tool
from Cowork.cowork_helpers import (
    _safe_path,
    _read_binary,
    _read_binary_units,
    _search_binary_units,
    BINARY_UNIT_LABELS,
    _get_sandbox_root,
    _is_skipped,
    _list_directory_entries,
    _describe_path,
    _validate_fileops_command,
    _move_to_trash,
    _SKIP_DIRS,
    _BINARY_EXTENSIONS,
    _ALLOWED_WRITE_EXTENSIONS,
    MANAGEABLE_EXTENSIONS,
    TRASH_DIR_NAME,
    READABLE_EXTENSIONS,
    CommandValidationError,
    build_binary_file,
    _format_script_library_status,
    _fmt_delete_path_arg,
)


# Caps on read_file's UNRANGED ("full read") path — the one a model falls
# into by omitting start_line/end_line/start_unit/end_unit entirely. This
# is the actual risk surface for cost/latency: a targeted range is already
# capped (500 lines / 50 units, enforced above), but nothing previously
# stopped an unranged call from returning an entire 300-page PDF or
# 500-slide deck in one shot. These constants are that backstop — past
# them, a full read auto-downgrades to a preview + explicit paging
# instruction instead of dumping everything.
_FULL_READ_MAX_LINES = 500     # text files
_FULL_READ_MAX_UNITS = 20      # binary documents (pages/slides/sheets/paragraphs)
_FULL_READ_PREVIEW_UNITS = 3   # units shown in the preview when capped


# READ-ONLY TOOLS
@tool
def search_index(query: str) -> str:
    """
    Semantic search over the sandbox's RAG index (.txt, .md, .pdf, .docx,
    .xlsx, .py, .json, .csv, and more). Returns top matching snippets with
    file path and position.

    Prefer this over read_file for "find X in this document" on any file
    you haven't already confirmed is short — it returns only the relevant
    snippets instead of a full read, at a fraction of the token cost, and
    works even when you don't know which page/slide/sheet the answer is
    on. Use read_file instead when you already know the exact unit/line
    range you need, or need the full unmodified text of a short file.

    Args:
        query: Plain-language description of what you're looking for,
               e.g. "quarterly budget figures".

    Returns:
        Formatted top-matching snippets with file path and position, or
        a message that the index has no relevant matches.
    """
    from Cowork.cowork_rag import get_rag   # adjust import path to match your project
    rag = get_rag()
    if rag is None:
        return "RAG index is not initialised. This is a bug — please report it."
    results = rag.search(query)
    return rag.format_results(results)


@tool
def read_file(
    path: str,
    start_line: int = 0,
    end_line: int = 0,
    start_unit: int = 0,
    end_unit: int = 0,
) -> str:
    """
    Read a file as plain text, in full or by a targeted range.

    Text files: use start_line/end_line, 1-indexed inclusive, max 500
    lines/call.

    Binary documents (.pdf/.docx/.xlsx/.xls/.pptx): use start_unit/
    end_unit instead — line numbers don't apply. A unit is the format's
    own structural division: .pdf=page, .pptx=slide, .xlsx/.xls=sheet
    (by order, not name), .docx=paragraph (no page concept in the file
    format). 1-indexed inclusive, max 50 units/call. Extraction per
    format: .pdf per-page (plus trailing form fields on a full read),
    .docx per-paragraph, .xlsx/.xls per-sheet tab-separated, .pptx
    per-slide with text/tables/notes.

    Use one mechanism matching the file's type, not both.

    0/0 on both (default) = full file/document — but only up to
    500 lines / 20 units. Past that, you get a preview (first 3 units, or
    the text-file equivalent) plus the true total, not the whole thing —
    large files must be paged with start_unit/end_unit (or start_line/
    end_line) instead of read in one call. Check the header on any full
    read: it states the true total, so if it's larger than what a preview
    would show, switch to a ranged call. For "find X in this document"
    without knowing which page/slide it's on, prefer search_index — it's
    cheaper and works regardless of document length.

    Args:
        path:       Relative path to the file.
        start_line: Text files only — first line (1-indexed). 0 for full/preview.
        end_line:   Text files only — last line (inclusive). 0 for full/preview.
        start_unit: Binary documents only — first page/slide/sheet/paragraph (1-indexed). 0 for full/preview.
        end_unit:   Binary documents only — last page/slide/sheet/paragraph (inclusive). 0 for full/preview.

    Returns:
        A header line stating what was read and the true total ("lines
        1–500 of 3200" or "pages 1–3 of 340 (PREVIEW — capped; use
        start_unit/end_unit to page through the rest)"), followed by the
        content. On error: a plain-text explanation (bad range, wrong
        mechanism for this file type, file not found, unsupported/legacy
        format, missing package) — never raises.
    """
    line_ranged = start_line > 0 or end_line > 0
    unit_ranged = start_unit > 0 or end_unit > 0

    if line_ranged and unit_ranged:
        return (
            "Error: pass either start_line/end_line or start_unit/end_unit, "
            "not both — they address different file types."
        )

    if line_ranged:
        if start_line < 1:
            return "Error: start_line must be >= 1."
        if end_line < start_line:
            return "Error: end_line must be >= start_line."
        if end_line - start_line > 500:
            return "Error: Cannot read more than 500 lines at once. Narrow your range."

    if unit_ranged:
        if start_unit < 1:
            return "Error: start_unit must be >= 1."
        if end_unit < start_unit:
            return "Error: end_unit must be >= start_unit."
        if end_unit - start_unit > 50:
            return "Error: Cannot read more than 50 units at once. Narrow your range."

    try:
        file_path = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not file_path.exists():
        return f"File '{path}' does not exist."
    if not file_path.is_file():
        return f"'{path}' is a directory, not a file."

    suffix = file_path.suffix.lower()
    if suffix in {".doc", ".ppt"}:
        modern_ext = ".docx" if suffix == ".doc" else ".pptx"
        return (
            f"'{path}' is the legacy pre-2007 Office format ('{suffix}') and "
            f"isn't supported — only the modern '{modern_ext}' format can be "
            f"read. Re-save the file as {modern_ext} (e.g. via 'Save As' in "
            "Word/PowerPoint) and try again."
        )

    is_binary = suffix in _BINARY_EXTENSIONS

    if line_ranged and is_binary:
        unit_label = BINARY_UNIT_LABELS.get(suffix, "unit")
        return (
            f"'{path}' is a binary document ({file_path.suffix}). "
            f"start_line/end_line only work on text files — use "
            f"start_unit/end_unit instead, which for this format means "
            f"{unit_label} number."
        )

    if unit_ranged and not is_binary:
        return (
            f"'{path}' is a text file. start_unit/end_unit only work on "
            "binary documents (.pdf/.docx/.xlsx/.pptx) — use start_line/"
            "end_line instead."
        )

    if line_ranged:
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

    if unit_ranged:
        unit_label = BINARY_UNIT_LABELS.get(suffix, "unit")
        try:
            units = _read_binary_units(file_path)
        except ImportError as e:
            return f"Cannot read '{path}': missing required package — {e}"
        except Exception as e:
            return f"Could not extract text from '{path}': {e}"

        total = len(units)
        if total == 0:
            return f"'{path}' has no extractable {unit_label}s (it may be empty or unparseable)."
        if start_unit > total:
            return f"'{path}' only has {total} {unit_label}(s). start_unit={start_unit} is out of range."

        actual_end = min(end_unit, total)
        selected = units[start_unit - 1 : actual_end]

        header = f"[{path} | {unit_label}s {start_unit}–{actual_end} of {total}]\n"
        return header + "\n\n".join(selected)

    # ── Unranged ("full read") path — the one a model falls into by
    # omitting every range argument. This is the actual cost/latency risk
    # for large documents, so it is capped the same way a ranged call
    # would be, rather than ever returning an entire large document in one
    # response. Small files (the common case) are completely unaffected —
    # this only changes behavior once a file exceeds the cap.
    if is_binary:
        unit_label = BINARY_UNIT_LABELS.get(suffix, "unit")
        try:
            units = _read_binary_units(file_path)
        except ImportError as e:
            return f"Cannot read '{path}': missing required package — {e}"
        except Exception as e:
            return f"Could not extract text from '{path}': {e}"

        total = len(units)
        if total == 0:
            return f"'{path}' has no extractable {unit_label}s (it may be empty or unparseable)."

        if total > _FULL_READ_MAX_UNITS:
            preview = units[:_FULL_READ_PREVIEW_UNITS]
            header = (
                f"[{path} | {unit_label}s 1–{_FULL_READ_PREVIEW_UNITS} of {total} "
                f"— PREVIEW, full document too large for one read. Use "
                f"start_unit/end_unit (max 50 {unit_label}s/call) to page "
                f"through the rest, or search_index/search_file_contents "
                "to jump straight to relevant content.]\n"
            )
            return header + "\n\n".join(preview)

        # Under the cap — safe to return the full document. Goes through
        # _read_binary (not just "\n\n".join(units)) so PDFs still get
        # their trailing form-field block, matching pre-cap behavior
        # exactly for every file this cap doesn't affect.
        try:
            content = _read_binary(file_path)
        except ImportError as e:
            return f"Cannot read '{path}': missing required package — {e}"
        except Exception as e:
            return f"Could not extract text from '{path}': {e}"

        header = f"[{path} | full document, {total} {unit_label}s]\n"
        return header + content

    else:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Could not read '{path}': {e}"

        all_lines = content.splitlines(keepends=True)
        total = len(all_lines)

        if total > _FULL_READ_MAX_LINES:
            preview_lines = all_lines[:_FULL_READ_MAX_LINES]
            numbered = "".join(f"{i + 1:>6}  {line}" for i, line in enumerate(preview_lines))
            header = (
                f"[{path} | lines 1–{_FULL_READ_MAX_LINES} of {total} — "
                f"PREVIEW, full file too large for one read. Use "
                f"start_line/end_line (max 500 lines/call) to page through "
                "the rest, or search_index/search_file_contents to jump "
                "straight to relevant content.]\n"
            )
            return header + numbered

        header = f"[{path} | full file, {total} lines]\n"
        return header + content


# WRITE TOOLS
@tool
def check_binary_write_libraries() -> str:
    """
    Report which Python libraries (and pandoc) are actually importable in
    THIS sandbox right now, for building .docx/.pptx/.xlsx/.xls/.pdf files
    via write_file's binary-script mode.

    Call this ONCE, before writing your first binary-build script this
    session — it tells you which library to reach for without guessing or
    discovering a missing import from a failed run. The result is valid
    for the rest of the session (nothing installs/uninstalls itself
    mid-session), so there's no need to call this again per file.

    Takes no arguments and does not touch the filesystem.
    """
    lines = ["Library availability for binary file creation in this sandbox:\n"]
    for ext in (".docx", ".pptx", ".xlsx", ".xls", ".pdf"):
        lines.append(f"{ext}:")
        lines.append(_format_script_library_status(ext))
        lines.append("")
    lines.append(
        "Any library shown as NOT available can still be installed from "
        "inside your build script (e.g. `subprocess.check_call([sys.executable, "
        "'-m', 'pip', 'install', 'PACKAGE', '--break-system-packages'])` "
        "before importing it) — but preferring an already-available library "
        "avoids that extra install step and its runtime cost."
    )
    return "\n".join(lines)


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
    Create a new file, or replace a line range in an existing text file.

    mode="create" (default): writes `content` to `path`; refuses if the
    path already exists. Parent dirs auto-created when create_parents=True.

    mode="edit": replaces lines [start_line, end_line] (1-indexed,
    inclusive) with `content` ("" to delete the range); file must already
    exist. dry_run=True (default) previews as a diff; dry_run=False
    applies. Text extensions only — not supported for .docx/.xlsx/.pptx/
    .pdf.

    --- Binary formats (.docx, .xlsx, .xls, .pptx, .pdf) ---

    mode="create" only. `content` is not written verbatim — it must be a
    full standalone Python 3 script that builds the file, using whatever
    library and structure you choose. Two hard requirements, everything
    else is free:
      1. The script's working directory contains nothing else — don't
         read/reference any other path.
      2. Save the result to exactly this filename (relative, no folders):
           .docx -> "__cowork_output__.docx"   .xlsx -> "__cowork_output__.xlsx"
           .pptx -> "__cowork_output__.pptx"   .xls  -> "__cowork_output__.xls"
           .pdf  -> "__cowork_output__.pdf"
    That file is validated by opening it with the matching reader library
    (python-docx/python-pptx/openpyxl/pypdf), then copied to `path`. If
    the script errors, times out, produces no file, or produces one that
    fails to open, nothing is written to the sandbox — the error explains
    which, with the script's own stderr or the reader's parse error; fix
    and call write_file again.

    Call check_binary_write_libraries() once per session, before your
    first binary build, to see which libraries are actually importable
    here.

    Args:
        path:           Relative path to the file.
        content:        mode="create" + text extension: full file content, written verbatim.
                        mode="create" + binary extension: a Python build script (see above).
                        mode="edit": replacement text for the line range ("" to delete it).
        mode:           "create" or "edit".
        start_line:     mode="edit" only — first line to replace (1-indexed).
        end_line:       mode="edit" only — last line to replace (inclusive).
        create_parents: mode="create" only — auto-create missing parent dirs (default True).
        dry_run:        mode="edit" only — True (default) previews without writing.

    Returns:
        Success: "Created '{path}'.\\nSize: N bytes" (text writes also
        append "| Encoding: utf-8"). mode="edit" success: a diff preview
        (dry_run=True) or confirmation of applied lines (dry_run=False).
        Failure: a plain-text explanation of exactly what went wrong —
        already-exists, missing extension, disallowed extension, missing
        parent dir, or (binary only) the build script's stderr or the
        reader library's validation error. Never raises; always returns
        a string either way.
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

        if ext in _BINARY_EXTENSIONS:
            parent = target.parent
            if not parent.exists():
                if not create_parents:
                    rel_parent = parent.relative_to(_get_sandbox_root())
                    return (
                        f"Error: parent directory '{rel_parent}' does not exist. "
                        "Pass create_parents=True to create it automatically, "
                        "or use run_file_command('mkdir ...') first."
                    )
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    return f"Could not create parent directories for '{path}': {e}"

            if not content.strip():
                status = _format_script_library_status(ext)
                return (
                    f"Refused: '{path}' is a '{ext}' file, which requires "
                    f"`content` to be a Python script that builds it (see "
                    f"write_file's docstring). Library availability in this "
                    f"sandbox:\n{status}"
                )

            try:
                size = build_binary_file(target, ext, content)
            except ValueError as e:
                return str(e)
            except Exception as e:
                return f"Could not build '{path}': {e}"

            return f"Created '{path}'.\nSize: {size:,} bytes"

        if ext not in _ALLOWED_WRITE_EXTENSIONS:
            allowed_str = "  " + "\n  ".join(sorted(_ALLOWED_WRITE_EXTENSIONS))
            return (
                f"Refused: extension '{ext}' is not in the allowed list.\n"
                f"Supported extensions:\n{allowed_str}"
            )

        parent = target.parent
        if not parent.exists():
            if not create_parents:
                rel_parent = parent.relative_to(_get_sandbox_root())
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
    if ext not in _ALLOWED_WRITE_EXTENSIONS:
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


@tool
def run_file_command(command: str) -> str:
    """
    Run a filesystem command inside the sandbox: `cp`, `mv`, `mkdir`, `ls`,
    or `info` — for copying, moving/renaming, creating directories, listing
    directory contents, and reading file/folder metadata. Supports multiple
    paths per call (e.g. `ls reports/q3 reports/q4`, `info a.pdf b.md`) —
    batch paths together rather than calling this once per path.

    Not a general shell: only these five commands, limited flags (`-n`/`-r`/
    `-p`), no globs, no piping/chaining. Since it's still a command line,
    feel free to write the exact invocation for what you need rather than
    defaulting to a generic one — precise flags/paths get you a more
    targeted result and less to filter through.

    Args:
        command: A single cp/mv/mkdir/ls/info invocation, e.g.
                "cp reports/draft.md reports/draft-backup.md",
                "mv -r old_project new_project",
                "mkdir reports/q3 reports/q4",
                "ls reports/q3 reports/q4",
                "info report.pdf notes.md archive/2026".
    """
    try:
        executable, paths, flags = _validate_fileops_command(command)
    except CommandValidationError as e:
        return str(e)

    root = _get_sandbox_root()
    no_clobber = "-n" in flags

    if executable == "ls":
        sections = []
        for target in paths:
            label = str(target.relative_to(root)) if target.is_relative_to(root) else str(target)
            body = _list_directory_entries(target, label)
            sections.append(f"[{label}]\n{body}" if len(paths) > 1 else body)
        return "\n\n".join(sections)

    if executable == "info":
        if not paths:
            return "Refused: 'info' needs at least one path argument."
        sections = []
        for target in paths:
            label = str(target.relative_to(root)) if target.is_relative_to(root) else str(target)
            sections.append(_describe_path(target, label))
        return "\n\n".join(sections)

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

    # With 2+ sources, the destination must be a directory (create it if it doesn't exist yet, same as real cp/mv). With exactly 1 source,
    # dst may be either a directory (item goes inside it) or a new path (item is placed/renamed at that exact path).
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
def delete_path(
    path: Union[str, List[str]],
    recursive: bool = False,
    dry_run: bool = True,
) -> str:
    """
    Delete one or more files/directories — soft delete, moved to
    .sicily-trash/, never unlinked. `path` may be a single string or a
    list; files and directories can be freely mixed in one call.

    Every path is validated independently: must exist, files must be a
    manageable type, non-empty directories need recursive=True, sandbox
    root is always refused. Any single failure refuses the ENTIRE
    batch — never a partial delete — with the specific reason per failing
    path.

    dry_run=True (default) previews every path's effect without deleting
    anything; dry_run=False applies the whole validated batch at once. No
    cap on batch size.

    Args:
        path:      A single path, or a list of paths, to delete.
        recursive: Must be True if ANY directory in the batch is non-empty.
                   Ignored for files.
        dry_run:   If True (default), preview only — nothing is deleted.

    Returns:
        dry_run=True: "[DRY RUN — nothing deleted]" + per-path preview
        and counts. dry_run=False success: per-path confirmation with
        each new trash location. Any validation failure: a report naming
        each failing path and its reason — nothing deleted in this case.
    """
    paths = [path] if isinstance(path, str) else list(path)

    if not paths:
        return "Error: no path(s) provided."

    root = _get_sandbox_root()

    # ── Pass 1: validate every path independently, collect outcomes.
    # Nothing is deleted in this pass — this is pure inspection, so a
    # failing item never prevents us from also reporting on the others.
    resolved = []       # list of dicts describing each valid, ready-to-delete item
    problems = []        # list of "path: reason" strings for anything that failed

    for p in paths:
        try:
            target = _safe_path(p)
        except PermissionError as e:
            problems.append(f"'{p}': {e}")
            continue

        if not target.exists():
            problems.append(f"'{p}': does not exist.")
            continue

        if target.is_file():
            ext = target.suffix.lower()
            if ext not in MANAGEABLE_EXTENSIONS:
                problems.append(
                    f"'{p}': '{ext}' is not currently a manageable file type."
                )
                continue
            resolved.append({"path": p, "target": target, "kind": "file"})
            continue

        # target.is_dir()
        if target == root:
            problems.append(f"'{p}': cannot delete the sandbox root itself.")
            continue

        contents = list(target.rglob("*"))
        file_count = sum(1 for c in contents if c.is_file())
        dir_count = sum(1 for c in contents if c.is_dir())

        if contents and not recursive:
            problems.append(
                f"'{p}': not empty ({file_count} file(s), {dir_count} "
                "subfolder(s)) — pass recursive=True to confirm deleting it all."
            )
            continue

        resolved.append({
            "path": p, "target": target, "kind": "dir",
            "contents": contents, "file_count": file_count, "dir_count": dir_count,
        })

    # ── All-or-nothing gate: any failure refuses the ENTIRE batch, so a
    # destructive call never partially applies. Every failure is reported
    # together, not just the first, so one retry can fix them all.
    if problems:
        header = (
            f"Refused: {len(problems)} of {len(paths)} path(s) failed "
            "validation — nothing was deleted.\n\n"
        )
        body = "\n".join(f"  - {p}" for p in problems)
        footer = (
            "\n\nFix the issue(s) above (or pass recursive=True if any are "
            "non-empty directories you intend to delete entirely) and call "
            "again with the full corrected list."
        )
        return header + body + footer

    # ── All paths valid. dry_run: preview the whole batch, delete nothing.
    if dry_run:
        lines = [f"[DRY RUN — nothing deleted, {len(resolved)} path(s) validated]\n"]
        for item in resolved:
            if item["kind"] == "file":
                lines.append(f"  FILE  '{item['path']}' -> {TRASH_DIR_NAME}/")
            else:
                preview = "\n".join(
                    f"      - {c.relative_to(root)}" for c in item["contents"][:10]
                )
                more = (
                    f"\n      ... and {len(item['contents']) - 10} more"
                    if len(item["contents"]) > 10 else ""
                )
                lines.append(
                    f"  DIR   '{item['path']}' -> {TRASH_DIR_NAME}/ "
                    f"({item['file_count']} file(s), {item['dir_count']} subfolder(s))"
                    + (f"\n{preview}{more}" if item["contents"] else "")
                )
        lines.append("\nCall again with dry_run=False to apply.")
        return "\n".join(lines)

    # ── Apply the entire validated batch.
    results = []
    for item in resolved:
        try:
            trashed = _move_to_trash(item["target"])
        except Exception as e:
            # A failure here is a filesystem-level surprise happening
            # AFTER validation passed (e.g. permissions changed, disk
            # error) — surface it per-item rather than silently stopping,
            # since prior items in this loop may already be trashed.
            results.append(f"  FAILED '{item['path']}': {e}")
            continue

        rel_trashed = trashed.relative_to(root)
        if item["kind"] == "file":
            results.append(f"  Deleted '{item['path']}' -> '{rel_trashed}'")
        else:
            results.append(
                f"  Deleted '{item['path']}' and its contents "
                f"({item['file_count']} file(s), {item['dir_count']} "
                f"subfolder(s)) -> '{rel_trashed}'"
            )

    header = f"Deleted {len(resolved)} path(s):\n\n"
    return header + "\n".join(results)


# ---------------------------------------------------------------------------
# SEARCH — three tiers, escalating cost, with the strategy baked into the
# docstrings themselves so the model follows it without being separately prompted each time.
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

    root = _get_sandbox_root()
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
# These exist because an unscoped search_file_contents call (broad path, no includes, high max_results) can otherwise walk and return a large
# fraction of a monorepo in one call. Args are clamped, not rejected, so a call never fails — it just can't blow the budget.
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
    Grep-equivalent literal/regex search inside file content under `path`
    — plain text/code by line; PDF/docx/xlsx/pptx by real structure
    (page/slide/sheet/paragraph) instead of a flattened blob. `path` may
    be a single file OR a directory — a single file searches only that
    file (nothing else in its folder is touched); a directory recurses.

    Binary matches report a unit number (page/slide/sheet/paragraph) plus
    a finer locator where the format supports one (in-page line, slide
    part, or cell reference). That unit number is what read_file's
    start_unit/end_unit takes — workflow is: search here to find which
    unit has the evidence, then read_file(path, start_unit=N, end_unit=N)
    for that unit's full content, not the whole document. context_lines
    is text-files-only; for binary matches, get context via read_file.

    If you already know or suspect the specific file, pass that file
    directly as `path` — don't pass its parent directory, which would
    also search every other file there. Pass a directory only when
    searching across multiple files or you don't yet know which one has
    it. Server-side caps apply regardless of arguments passed: max_results
    <=40, ~400 files walked (directory mode only), context_lines<=4,
    output truncated ~6000 chars — an unscoped call gets clamped/
    truncated, not a full result, so narrowing the query is the only way
    to get everything back.

    Args:
        pattern:        Text or regex pattern to search for.
        path:           A single file to search, or a directory to search recursively. Defaults to ".".
        regex:          If True, `pattern` is a regular expression.
        case_sensitive: Default False.
        context_lines:  Lines of context per match (default 0, max 4). Text files only.
        match_per_line: True (default): matching lines + line numbers.
                        False: matching file paths only (git grep -l) — cheaper, use to
                        locate candidates before requesting line content.
        includes:       Glob filters (e.g. ["*.py", "!**/node_modules/*"]). Directory mode
                        only — ignored when `path` is a single file. Use whenever file
                        type/area is known.
        max_results:    Stop after this many matches (default 20, hard cap 40).
        return_json:    Return raw JSON match objects instead of formatted text.

    Returns:
        Plain text (default): a header line ("Found N match(es) across
        M file(s) (K scanned)") followed by one block per match —
        "[path:line]  text" for text files, "[path | location]  text"
        for binary units — or "No matches across N readable file(s)."
        if none. return_json=True: a JSON array of match objects
        (Filename, LineNumber/Unit/UnitLabel/Location, LineContent)
        instead of formatted text.
    """
    try:
        start = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not start.exists():
        return f"'{path}' does not exist."

    single_file_mode = start.is_file()

    if single_file_mode:
        ext = start.suffix.lower()
        if ext not in READABLE_EXTENSIONS:
            return (
                f"'{path}' has extension '{ext}', which isn't a searchable "
                "text or document format."
            )
    elif not start.is_dir():
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

    root = _get_sandbox_root()
    matches = []
    matching_files = set()
    files_scanned = 0
    files_skipped = []
    scan_capped = False

    # Common directory excludes to keep search fast
    DEFAULT_IGNORE_DIRS = _SKIP_DIRS

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

    def _iter_files(target: Path):
        # Single-file mode: yield just that file, no directory walk.
        if target.is_file():
            if not _is_skipped(target):
                yield target
            return
        try:
            children = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
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

        if not single_file_mode and not _should_include(rel_str):
            continue

        # ── Binary documents: search real structure (page/slide/sheet/
        #    paragraph), not a flattened line number — this is what keeps
        #    results in sync with read_file's start_unit/end_unit, which
        #    addresses the same page/slide/sheet/paragraph numbering.
        if ext in _BINARY_EXTENSIONS:
            try:
                unit_matches = _search_binary_units(file_path, search_regex)
            except Exception:
                files_skipped.append(rel_str)
                continue

            files_scanned += 1
            for m in unit_matches:
                if len(matches) >= max_results:
                    break

                matching_files.add(rel_str)

                if not match_per_line:
                    if rel_str not in matches:
                        matches.append(rel_str)
                    break

                if return_json:
                    matches.append({
                        "Filename": rel_str,
                        "Unit": m["unit"],
                        "UnitLabel": m["unit_label"],
                        "Location": m["location"],
                        "LineContent": m["line_text"],
                    })
                else:
                    matches.append(f"[{rel_str} | {m['location']}]  {m['line_text']}")

            continue

        # ── Text files: unchanged line-based search.
        try:
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


# EXPORTED TOOL LIST
# list_directory and get_file_info are no longer standalone tools — they're
# available as the `ls` / `info` sub-commands of run_file_command (see
# cowork_tool_fileops.py), which lets the model target several paths per
# call instead of one per round-trip.
LOCAL_TOOLS = [
    # Read-only (safe)
    search_index,
    read_file,

    # Write (safe-ish)
    write_file,

    # Search tier (escalating cost — see docstrings for the strategy)
    find_files_by_name,
    search_file_contents,

    # Copy / move / rename / mkdir / list / inspect — one validated command tool (no-clobber by default).
    run_file_command,

    # Delete (soft — trash, dry_run by default)
    delete_path,
]


TOOL_STATUS_MAP = {
    "search_index": lambda args: (
        f"Searching index for [white]'{args.get('query')}'[/white]"
    ),
    "read_file": lambda args: (
        f"Reading lines {args.get('start_line')}–{args.get('end_line')} of "
        f"[white]'{args.get('path')}'[/white]"
        if args.get("start_line") or args.get("end_line")
        else (
            f"Reading units {args.get('start_unit')}–{args.get('end_unit')} of "
            f"[white]'{args.get('path')}'[/white]"
            if args.get("start_unit") or args.get("end_unit")
            else f"Reading file [white]'{args.get('path')}'[/white]"
        )
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
    "delete_path": lambda args: (
        f"Previewing delete of {_fmt_delete_path_arg(args.get('path'))}"
        if args.get("dry_run", True)
        else f"Deleting {_fmt_delete_path_arg(args.get('path'))} (-> trash)"
    ),
}


def get_friendly_tool_message(tool_call: dict) -> str:
    """Extracts the tool name and args to build a readable status update."""
    name = tool_call.get("name")
    args = tool_call.get("args", {})

    if name in TOOL_STATUS_MAP:
        return TOOL_STATUS_MAP[name](args)

    # Fallback for any future tools not yet in the map
    return name or "working..."
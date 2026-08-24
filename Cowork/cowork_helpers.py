import datetime
import importlib
import importlib.util
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional, Callable


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
# which deliberately includes .pdf/.docx/.xlsx/.xls/.pptx for exactly that
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
    global _SANDBOX_ROOT, _SCRIPT_LIBRARY_AVAILABILITY
    _SANDBOX_ROOT = path.resolve()
    # Probe binary-build library availability exactly once, here, not per
    # write_file call — the result cannot change mid-session and importing
    # five modules on every tool invocation would be pure waste.
    _SCRIPT_LIBRARY_AVAILABILITY = _probe_script_libraries()


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
_BINARY_EXTENSIONS = frozenset({".pdf", ".xlsx", ".xls", ".docx", ".pptx"})

# Legacy pre-2007 Office formats (OLE Compound File Binary, not ZIP/XML).
# python-docx/python-pptx cannot open these at all — deliberately NOT in
# _BINARY_EXTENSIONS (so they're excluded from READABLE_EXTENSIONS and
# search_file_contents/read_file won't attempt extraction), but still
# routed to a clear, explicit error in _read_binary rather than an
# undocumented library crash if something reaches this function anyway.
_UNSUPPORTED_LEGACY_BINARY_EXTENSIONS = frozenset({".doc", ".ppt"})


# Which structural unit each binary format is addressed by in read_file's
# start_unit/end_unit — surfaced so callers can build accurate messages
# ("pages 1-40", "slides 1-12") without hardcoding the mapping themselves.
BINARY_UNIT_LABELS: dict[str, str] = {
    ".pdf": "page",
    ".pptx": "slide",
    ".xlsx": "sheet",
    ".xls": "sheet",
    ".docx": "paragraph",
}


# DOCX: python-docx exposes doc.paragraphs and doc.tables as separate flat
# lists that do NOT preserve their relative order in the document (a table
# in the middle of a doc would otherwise get shoved after every paragraph
# when reconstructing unit order). This walks the real body XML in true
# document order so paragraph/table units are numbered the way a reader
# would actually encounter them, and — critically — so table content (e.g.
# a name list laid out as a table, which doc.paragraphs never sees at all)
# is actually searchable/readable.
def _iter_docx_body_units(doc):
    """
    Yield (kind, unit_obj) for each top-level body paragraph/table, in true
    document order. kind is "paragraph" or "table"; unit_obj is a
    docx.text.paragraph.Paragraph or docx.table.Table.
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield "paragraph", Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield "table", Table(child, doc)
        # Other body-level elements (sectPr, etc.) carry no readable text
        # and are intentionally skipped.


def _docx_table_text(table) -> str:
    """Render a docx Table as tab-separated rows, matching the .xlsx/.pptx table convention used elsewhere."""
    rows = [
        "\t".join(cell.text.strip() for cell in row.cells)
        for row in table.rows
    ]
    return "[Table]\n" + "\n".join(rows)


def _search_binary_units(path: Path, search_regex: re.Pattern) -> list[dict]:
    """
    Search a binary document's real internal structure for `search_regex`,
    returning one dict per match:
        {
          "unit": int,          # 1-indexed page/slide/sheet/paragraph —
                                 # the SAME number read_file's start_unit/
                                 # end_unit expects, so a search hit here
                                 # can be handed straight to read_file to
                                 # pull that exact unit back.
          "unit_label": str,    # "page" / "slide" / "sheet" / "paragraph"
          "location": str,      # extra format-specific locator (see below)
          "line_text": str,     # the matching line/cell/paragraph text
        }

    This is intentionally separate from _read_binary_units (which returns
    flattened per-unit text for reading) because search benefits from
    staying close to each format's real structure instead of flattening
    first — most usefully for .xlsx, where flattening to tab-separated
    rows loses the cell reference (e.g. "B7") a match came from.

    Per-format `location` detail (only what's genuinely available/cheap —
    nothing inferred or approximated):
      .pdf  -> "page N, line L" (L = line number within that page's own
               extracted text — meaningful within the page, unlike a
               whole-document line number)
      .pptx -> "slide N, <part>" where <part> is "text", "table", or
               "notes" — which part of the slide the match is actually in
      .xlsx/.xls -> "sheet 'name', cell REF" (e.g. "sheet 'Q3', cell B7")
      .docx -> "paragraph N" (no finer locator exists within one
               paragraph's flat text)

    Raises the same ImportError/ValueError as _read_binary_units for
    missing packages / unsupported extensions.
    """
    ext = path.suffix.lower()

    if ext in _UNSUPPORTED_LEGACY_BINARY_EXTENSIONS:
        modern_ext = ".docx" if ext == ".doc" else ".pptx"
        raise ValueError(
            f"'{ext}' is the legacy pre-2007 Office format and isn't "
            f"supported — only the modern '{modern_ext}' format can be "
            f"read. Re-save the file as {modern_ext} (e.g. via 'Save As' "
            "in Word/PowerPoint) and try again."
        )

    matches: list[dict] = []

    if ext == ".pdf":
        if importlib.util.find_spec("pypdf") is None:
            raise ImportError("pip install pypdf")
        from pypdf import PdfReader

        reader = PdfReader(path)
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if not page_text.strip():
                continue
            for j, line in enumerate(page_text.splitlines()):
                if search_regex.search(line):
                    matches.append({
                        "unit": i + 1,
                        "unit_label": "page",
                        "location": f"page {i+1}, line {j+1}",
                        "line_text": line.strip(),
                    })
        return matches

    if ext in {".xlsx", ".xls"}:
        if importlib.util.find_spec("openpyxl") is None:
            raise ImportError("pip install openpyxl")
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for sheet_idx, name in enumerate(wb.sheetnames):
            ws = wb[name]
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    value_str = str(cell.value)
                    if search_regex.search(value_str):
                        matches.append({
                            "unit": sheet_idx + 1,
                            "unit_label": "sheet",
                            "location": f"sheet '{name}', cell {cell.coordinate}",
                            "line_text": value_str.strip(),
                        })
        wb.close()
        return matches

    if ext == ".docx":
        if importlib.util.find_spec("docx") is None:
            raise ImportError("pip install python-docx")
        from docx import Document

        doc = Document(path)
        for i, (kind, unit_obj) in enumerate(_iter_docx_body_units(doc)):
            unit_num = i + 1
            if kind == "paragraph":
                if unit_obj.text.strip() and search_regex.search(unit_obj.text):
                    matches.append({
                        "unit": unit_num,
                        "unit_label": "paragraph",
                        "location": f"paragraph {unit_num}",
                        "line_text": unit_obj.text.strip(),
                    })
            else:  # kind == "table"
                for r_idx, row in enumerate(unit_obj.rows):
                    for c_idx, cell in enumerate(row.cells):
                        if cell.text.strip() and search_regex.search(cell.text):
                            matches.append({
                                "unit": unit_num,
                                "unit_label": "paragraph",
                                "location": f"table at unit {unit_num}, row {r_idx+1}, col {c_idx+1}",
                                "line_text": cell.text.strip(),
                            })
        return matches

    if ext == ".pptx":
        if importlib.util.find_spec("pptx") is None:
            raise ImportError("pip install python-pptx")
        from pptx import Presentation

        prs = Presentation(path)
        for i, slide in enumerate(prs.slides):
            # Text frames — titles, body placeholders, free text boxes.
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for p in shape.text_frame.paragraphs:
                        if p.text.strip() and search_regex.search(p.text):
                            matches.append({
                                "unit": i + 1,
                                "unit_label": "slide",
                                "location": f"slide {i+1}, text",
                                "line_text": p.text.strip(),
                            })

                # Tables — cell by cell.
                if shape.has_table:
                    for row in shape.table.rows:
                        for cell in row.cells:
                            if cell.text.strip() and search_regex.search(cell.text):
                                matches.append({
                                    "unit": i + 1,
                                    "unit_label": "slide",
                                    "location": f"slide {i+1}, table",
                                    "line_text": cell.text.strip(),
                                })

            # Speaker notes.
            if slide.has_notes_slide:
                notes_text = (slide.notes_slide.notes_text_frame.text or "")
                if notes_text.strip() and search_regex.search(notes_text):
                    for line in notes_text.splitlines():
                        if line.strip() and search_regex.search(line):
                            matches.append({
                                "unit": i + 1,
                                "unit_label": "slide",
                                "location": f"slide {i+1}, notes",
                                "line_text": line.strip(),
                            })
        return matches

    raise ValueError(
        f"No binary reader available for '{ext}'. "
        "For plain text files this tool reads UTF-8 directly. "
        "For other binary formats, a dedicated tool may be needed."
    )


def _read_binary_units(path: Path) -> list[str]:
    """
    Extract each binary document's content as a list of independently
    addressable units — one string per unit, in document order, each
    already carrying its own header (e.g. "[Page 3]\\n...", "[Slide 12]\\n...").

    This is the shared extraction core behind both _read_binary (full read
    — joins every unit) and ranged binary reads (slices a start/end window
    of units) in read_file, so both code paths always agree on where a
    unit begins and ends — a targeted read can never split a slide, page,
    sheet, or paragraph-block in half.

    Unit meaning per format (dictated by what each format's own document
    model actually exposes — not arbitrary):
      .pdf  -> one unit per page (PDF's native structural unit)
      .pptx -> one unit per slide (PPTX's native structural unit)
      .xlsx/.xls -> one unit per sheet (workbook's native structural unit)
      .docx -> one unit per paragraph. DOCX has no page concept in the
               XML (pagination is computed at render time by Word, not
               stored), and python-docx's `sections` don't expose which
               paragraphs belong to which section — so paragraph index is
               the finest stable, addressable unit actually available.

    Only pages/slides/sheets/paragraphs with non-empty text produce a
    unit — this matches _read_binary's existing full-read behavior of
    skipping blank pages/slides, so unit numbering (e.g. "[Page 3]") is
    unaffected by this refactor.

    Empty pdf form-field data is intentionally NOT its own unit (it's
    sandbox metadata, not page content) — _read_binary appends it once,
    after all page units, to preserve the exact prior full-read output.

    Raises ImportError with an install hint if the required library is
    missing. Raises ValueError for unsupported binary extensions.
    """
    ext = path.suffix.lower()

    if ext in _UNSUPPORTED_LEGACY_BINARY_EXTENSIONS:
        modern_ext = ".docx" if ext == ".doc" else ".pptx"
        raise ValueError(
            f"'{ext}' is the legacy pre-2007 Office format and isn't "
            f"supported — only the modern '{modern_ext}' format can be "
            f"read. Re-save the file as {modern_ext} (e.g. via 'Save As' "
            "in Word/PowerPoint) and try again."
        )

    if ext == ".pdf":
        if importlib.util.find_spec("pypdf") is None:
            raise ImportError("pip install pypdf")

        from pypdf import PdfReader

        reader = PdfReader(path)
        units = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                units.append(f"[Page {i+1}]\n{page_text.strip()}")
        return units

    if ext in {".xlsx", ".xls"}:
        if importlib.util.find_spec("openpyxl") is None:
            raise ImportError("pip install openpyxl")
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        units = []
        for name in wb.sheetnames:
            ws = wb[name]
            rows = [
                "\t".join("" if cell.value is None else str(cell.value) for cell in row)
                for row in ws.iter_rows()
            ]
            units.append(f"[Sheet: {name}]\n" + "\n".join(rows))
        wb.close()
        return units

    if ext == ".docx":
        if importlib.util.find_spec("docx") is None:
            raise ImportError("pip install python-docx")
        from docx import Document
        doc = Document(path)
        units = []
        for i, (kind, unit_obj) in enumerate(_iter_docx_body_units(doc)):
            unit_num = i + 1
            if kind == "paragraph":
                if unit_obj.text.strip():
                    units.append(f"[Paragraph {unit_num}]\n{unit_obj.text.strip()}")
            else:  # kind == "table"
                rows = [
                    "\t".join(cell.text.strip() for cell in row.cells)
                    for row in unit_obj.rows
                ]
                rows_text = "\n".join(rows).strip()
                if rows_text:
                    units.append(f"[Unit {unit_num} — Table]\n{rows_text}")
        return units

    if ext == ".pptx":
        if importlib.util.find_spec("pptx") is None:
            raise ImportError("pip install python-pptx")
        from pptx import Presentation

        prs = Presentation(path)
        units = []

        for i, slide in enumerate(prs.slides):
            parts = []

            # 1. Text frames (titles + body placeholders + free text boxes),
            #    in shape order — cheap: no OCR, no embedded-chart parsing.
            for shape in slide.shapes:
                if shape.has_text_frame:
                    text = "\n".join(
                        p.text for p in shape.text_frame.paragraphs if p.text.strip()
                    )
                    if text.strip():
                        parts.append(text.strip())

                # 2. Tables — cell text only, row by row, tab-separated to
                #    match the .xlsx sheet convention used above.
                if shape.has_table:
                    rows = [
                        "\t".join(cell.text.strip() for cell in row.cells)
                        for row in shape.table.rows
                    ]
                    parts.append("[Table]\n" + "\n".join(rows))

            # 3. Speaker notes, if present.
            if slide.has_notes_slide:
                notes_text = (slide.notes_slide.notes_text_frame.text or "").strip()
                if notes_text:
                    parts.append(f"[Notes]\n{notes_text}")

            if parts:
                units.append(f"[Slide {i+1}]\n" + "\n\n".join(parts))

        return units

    raise ValueError(
        f"No binary reader available for '{ext}'. "
        "For plain text files this tool reads UTF-8 directly. "
        "For other binary formats, a dedicated tool may be needed."
    )


def _read_binary(path: Path) -> str:
    """
    Extract human-readable text from binary file formats, as one string
    (all units joined in document order). For PDFs, also appends any
    AcroForm field values as a trailing block, matching this function's
    long-standing full-read output.

    Dispatches to the appropriate parser based on file extension.
    Raises ImportError with an install hint if the required library is missing.
    Raises ValueError for unsupported binary extensions.
    """
    ext = path.suffix.lower()
    units = _read_binary_units(path)
    text_output = "\n\n".join(units)

    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        try:
            fields = reader.get_fields()
            if fields:
                field_lines = []
                for name, field in fields.items():
                    value = field.value
                    if value is not None:
                        field_lines.append(f"{name}: {value}")
                    else:
                        field_lines.append(f"{name}: [empty]")
                if field_lines:
                    sep = "\n\n" if text_output else ""
                    text_output += sep + "[Form Field Values]\n" + "\n".join(field_lines)
        except Exception as e:
            text_output += f"\n[Form Field Extraction Failed: {e}]"

    return text_output.strip()


# ---------------------------------------------------------------------------
# BINARY FILE CREATION — scripted builders for .docx/.xlsx/.pptx/.pdf
# ---------------------------------------------------------------------------
#
# Design intent (read before changing):
#
# write_file's `content` param is normally inert text — written verbatim,
# never interpreted. For binary extensions there is no "verbatim bytes as a
# string" equivalent, so `content` instead carries a Python script that
# BUILDS the file. This mirrors the officially-documented approach in this
# environment's own docx/pptx/xlsx/pdf skills, which all create new binary
# files by writing a script against a real library (docx-js, pptxgenjs,
# openpyxl, reportlab) rather than filling in a fixed template/schema — a
# schema would cap the model's expressiveness below what these formats
# actually support (styled runs, native charts, formulas, positional
# layout, etc).
#
# This is deliberately NOT a generic "run arbitrary shell" escape hatch —
# it is scoped the same way run_file_command's allowlist is scoped, just
# with a different mechanism:
#   - The script executes in its OWN throwaway temp directory, never the
#     sandbox root. It cannot see, read, or write any other user file —
#     there is nothing else present in its working directory.
#   - The script's only sanctioned output is ONE file, at a fixed filename
#     ("__cowork_output__<ext>") that WE choose, not the model. The model
#     never controls a real filesystem path — only what bytes end up at
#     that one throwaway name.
#   - This tool (not the script) is what copies that single output file
#     into the sandbox, through the same _safe_path() root-escape check
#     every other write in this module goes through. A script that never
#     produces that exact file simply produces no write, no matter what
#     else it did in its own temp dir.
#   - Non-zero exit, timeout, or a missing/empty output file all fail
#     closed with the model's own stdout/stderr surfaced, so it can see
#     and fix its own bug — the same self-correcting loop the skills rely
#     on ("run validate.py, fix what it names").
#
# The model decides imports, structure, layout, and library calls freely.
# It does not decide where output lands on disk.

# Binary formats writable via a generated script, and the fixed, sandboxed
# output filename each script must produce (extension baked in so the
# library being called — which usually infers format from filename — does
# the right thing without the model needing to know the trick).
_SCRIPTED_BINARY_OUTPUT_NAME: dict[str, str] = {
    ".docx": "__cowork_output__.docx",
    ".pptx": "__cowork_output__.pptx",
    ".xlsx": "__cowork_output__.xlsx",
    ".xls":  "__cowork_output__.xls",
    ".pdf":  "__cowork_output__.pdf",
}

# Candidate libraries per extension, in the order we'd recommend trying
# them. `module` is what actually gets import-checked (may differ from the
# pip/uv package name — e.g. "docx" vs "python-docx"). `install` is the
# real install instruction shown when a library is missing, so a model
# never has to guess the pip name or discover --break-system-packages the
# hard way via a failed subprocess call.
_SCRIPT_LIBRARY_CANDIDATES: dict[str, list[dict[str, str]]] = {
    ".docx": [
        {"module": "docx", "package": "python-docx",
         "install": "pip install python-docx --break-system-packages"},
        {"module": None, "package": "pandoc",
         "install": "(system binary, not pip) apt-get install pandoc / brew install pandoc / winget install JohnMacFarlane.Pandoc",
         "note": "Markdown -> docx via subprocess, no Python import needed"},
    ],
    ".pptx": [
        {"module": "pptx", "package": "python-pptx",
         "install": "pip install python-pptx --break-system-packages"},
    ],
    ".xlsx": [
        {"module": "openpyxl", "package": "openpyxl",
         "install": "pip install openpyxl --break-system-packages"},
        {"module": "pandas", "package": "pandas",
         "install": "pip install pandas --break-system-packages"},
    ],
    ".xls": [
        {"module": "openpyxl", "package": "openpyxl",
         "install": "pip install openpyxl --break-system-packages"},
        {"module": "pandas", "package": "pandas",
         "install": "pip install pandas --break-system-packages"},
    ],
    ".pdf": [
        {"module": "reportlab", "package": "reportlab",
         "install": "pip install reportlab --break-system-packages"},
        {"module": "pypdf", "package": "pypdf",
         "install": "pip install pypdf --break-system-packages"},
    ],
}

# Populated once by _probe_script_libraries() at sandbox startup (called
# from _set_sandbox_root — see below). None means "not probed yet"; if a
# caller sees None it means _set_sandbox_root() hasn't run, which is
# already a hard error everywhere else in this module.
_SCRIPT_LIBRARY_AVAILABILITY: Optional[dict[str, bool]] = None


def _probe_script_libraries() -> dict[str, bool]:
    """
    Actually import-check (not just importlib.util.find_spec — a real
    import catches broken installs find_spec would miss) every candidate
    module across all binary-writable extensions, ONCE. Returns a flat
    {module_name: bool} map, e.g. {"docx": True, "pptx": False, ...}.

    Deliberately does not check "pandoc" the binary here (module is None
    for it) — that's a shutil.which() check done separately in the
    formatter below, since it's not a Python import.

    This runs a handful of import statements one time at sandbox startup.
    It is NOT re-run per write_file call — that would waste a real import
    (disk stat + bytecode load) on every single tool invocation for
    information that cannot change mid-session.
    """
    modules = {
        cand["module"]
        for candidates in _SCRIPT_LIBRARY_CANDIDATES.values()
        for cand in candidates
        if cand["module"] is not None
    }
    result: dict[str, bool] = {}
    for mod in modules:
        try:
            importlib.import_module(mod)
            result[mod] = True
        except Exception:
            # Broad except is intentional: a library can be "installed"
            # but fail to import for any number of environment reasons
            # (missing shared lib, corrupt wheel, version mismatch) — all
            # of those should read as "not available", not crash startup.
            result[mod] = False
    return result


def _format_script_library_status(ext: str) -> str:
    """
    Human/model-readable line(s) for write_file's docstring, built from
    the REAL probed availability for this extension — never a claim we
    haven't actually verified in this sandbox. One line per candidate:

        python-docx: available
        pandoc: NOT available - (system binary, not pip) apt-get install pandoc / ...

    If _SCRIPT_LIBRARY_AVAILABILITY is still None (probe never ran —
    _set_sandbox_root wasn't called), says so plainly rather than
    guessing, so the gap is visible instead of silently wrong.
    """
    if _SCRIPT_LIBRARY_AVAILABILITY is None:
        return "  (library availability not yet probed for this sandbox)"

    lines = []
    for cand in _SCRIPT_LIBRARY_CANDIDATES.get(ext, []):
        package = cand["package"]
        if cand["module"] is None:
            # System-binary candidate (pandoc) — checked via PATH, not import.
            available = shutil.which(package) is not None
        else:
            available = _SCRIPT_LIBRARY_AVAILABILITY.get(cand["module"], False)

        if available:
            lines.append(f"  {package}: available")
        else:
            note = f" — {cand['note']}" if "note" in cand else ""
            lines.append(f"  {package}: NOT available — install with: {cand['install']}{note}")
    return "\n".join(lines) if lines else "  (no known library candidates for this extension)"


_SCRIPT_TIMEOUT_SECONDS = 120


def _run_binary_build_script(ext: str, script: str) -> Path:
    """
    Execute `script` (a Python source string, already written by the model)
    in an isolated temp directory, and return the Path to the single
    sanctioned output file it produced.

    Raises ValueError (message is safe to show the model/user directly —
    it's meant to drive a self-correcting retry, same as a failed
    recalc.py/validate.py run in the skills) if:
      - the script exits non-zero,
      - the script exceeds _SCRIPT_TIMEOUT_SECONDS,
      - the script exits 0 but never produced the expected output file,
        or produced it empty.

    Caller is responsible for copying the returned path to its real
    sandbox destination and for cleaning up the temp directory afterward.
    """
    if ext not in _SCRIPTED_BINARY_OUTPUT_NAME:
        raise ValueError(f"No scripted binary builder registered for '{ext}'.")

    output_name = _SCRIPTED_BINARY_OUTPUT_NAME[ext]
    workdir = Path(tempfile.mkdtemp(prefix="cowork_build_"))
    script_path = workdir / f"build_{uuid.uuid4().hex}.py"
    expected_output = workdir / output_name

    try:
        script_path.write_text(script, encoding="utf-8")

        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=_SCRIPT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                f"Build script for '{ext}' timed out after "
                f"{_SCRIPT_TIMEOUT_SECONDS}s. Simplify the script or split "
                "the work (e.g. fewer/lighter images, less data)."
            )

        if proc.returncode != 0:
            raise ValueError(
                f"Build script for '{ext}' failed (exit {proc.returncode}).\n\n"
                f"--- stderr ---\n{proc.stderr.strip()[-4000:]}\n\n"
                f"--- stdout ---\n{proc.stdout.strip()[-2000:]}\n\n"
                f"Fix the script and call write_file again. The script must "
                f"save its result to exactly '{output_name}' in its working "
                "directory (relative path, no directories) — that is the "
                "only file this tool will pick up."
            )

        if not expected_output.exists():
            raise ValueError(
                f"Build script for '{ext}' exited successfully but did not "
                f"create '{output_name}' in its working directory. The "
                "script must save/write its output to exactly that "
                "filename (relative, not an absolute path) for the file to "
                "be created.\n\n"
                f"--- stdout ---\n{proc.stdout.strip()[-2000:]}"
            )

        if expected_output.stat().st_size == 0:
            raise ValueError(
                f"Build script for '{ext}' produced an empty '{output_name}'. "
                "Nothing was written to disk — check the script actually "
                "calls the library's save/write method."
            )

        return expected_output

    except ValueError:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise ValueError(f"Could not run build script for '{ext}': {e}")


def _validate_docx(path: Path) -> None:
    """Raise ValueError with the real parser error if this isn't a valid docx."""
    if importlib.util.find_spec("docx") is None:
        return  # can't validate without the reader; not the model's fault
    from docx import Document
    Document(path)  # raises on malformed package/XML


def _validate_pptx(path: Path) -> None:
    if importlib.util.find_spec("pptx") is None:
        return
    from pptx import Presentation
    prs = Presentation(path)
    if len(prs.slides) == 0:
        raise ValueError(
            "The .pptx opened but contains zero slides. A presentation "
            "needs at least one slide."
        )


def _validate_xlsx(path: Path) -> None:
    if importlib.util.find_spec("openpyxl") is None:
        return
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    if not wb.sheetnames:
        raise ValueError(
            "The workbook opened but contains zero sheets. A workbook "
            "needs at least one sheet."
        )
    wb.close()


def _validate_pdf(path: Path) -> None:
    if importlib.util.find_spec("pypdf") is None:
        return
    from pypdf import PdfReader
    reader = PdfReader(path)
    if len(reader.pages) == 0:
        raise ValueError(
            "The .pdf opened but contains zero pages. A PDF needs at "
            "least one page."
        )


# One validator per binary-writable extension. Each opens the file with
# the SAME reader library this module already trusts for the read side
# (_read_binary_units etc) — so "valid" here means "the exact library this
# codebase uses to read this format back can open it," not just "a zip/
# file with the right extension exists." Deliberately format-agnostic
# about how the file was BUILT: a script using python-pptx, pptxgenjs-
# equivalent code, or raw zipfile/XML construction is validated the same
# way, by whether the result actually opens — because a script constructing
# OOXML by hand (an equally legitimate approach the model is free to take)
# is exactly the case most likely to produce a file that "exists and is
# non-empty" while still being structurally broken (wrong namespace,
# missing relationship part, malformed content-types, etc) in a way no
# amount of exit-code or file-size checking would ever catch.
_BINARY_VALIDATORS: dict[str, Callable[[Path], None]] = {
    ".docx": _validate_docx,
    ".pptx": _validate_pptx,
    ".xlsx": _validate_xlsx,
    ".xls":  _validate_xlsx,
    ".pdf":  _validate_pdf,
}


def _validate_binary_output(ext: str, path: Path) -> None:
    """
    Open the freshly-built file with the real reader library for `ext`
    and confirm it's structurally valid — not just present and non-empty.
    Raises ValueError with the underlying parser's own error message
    (safe to hand back to the model verbatim — same self-correcting-retry
    pattern as every other failure this module surfaces) if the file is
    corrupt, malformed, or empty of content.

    If the reader library itself isn't installed in this sandbox, this
    silently skips validation rather than failing the build — an
    unrelated missing dependency on the READ side shouldn't block a WRITE
    that otherwise looks fine; check_binary_write_libraries() covers read-
    side availability separately if that ever needs surfacing.
    """
    validator = _BINARY_VALIDATORS.get(ext)
    if validator is None:
        return
    try:
        validator(path)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(
            f"Build script for '{ext}' produced a file, but it failed "
            f"validation — opening it with the same library this tool "
            f"uses to read '{ext}' files raised:\n\n"
            f"  {type(e).__name__}: {e}\n\n"
            "This means the file is structurally invalid and would not "
            "open correctly in the real application (Word/PowerPoint/"
            "Excel/a PDF viewer), even though the build script exited "
            "successfully. This is common when a script constructs the "
            "file's internal XML/zip structure by hand rather than "
            "through a library like python-docx/python-pptx/openpyxl/"
            "reportlab, which handle that structure correctly for you — "
            "switching to one of those libraries is the most reliable "
            "fix. Nothing was written to the sandbox; fix the script and "
            "call write_file again."
        )


def build_binary_file(target: Path, ext: str, script: str) -> int:
    """
    Run `script` to build a binary file of type `ext`, validate the
    result actually opens with this format's real reader library, then
    copy it to `target` (already validated/resolved by the caller via
    _safe_path — this function does not re-check sandbox containment).

    Returns the final file size in bytes. Raises ValueError on any build
    OR validation failure — the message is safe to surface directly to
    the model so it can retry. Nothing is copied into the sandbox unless
    validation passes, so a structurally broken file is never delivered
    as if it were a success.

    Cleans up the build's temp directory in all cases, success or failure.
    """
    produced = _run_binary_build_script(ext, script)
    try:
        _validate_binary_output(ext, produced)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(produced, target)
        return target.stat().st_size
    finally:
        shutil.rmtree(produced.parent, ignore_errors=True)


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
    # Legacy pre-2007 Office formats — movable/copyable/deletable, but no
    # content parser exists (see _UNSUPPORTED_LEGACY_BINARY_EXTENSIONS).
    ".doc", ".ppt",
})

# What copy_file / move_file / rename_file / delete_file are allowed to
# touch. Union of the two sets above — everything content-readable, plus
# everything that's only filesystem-manageable.
MANAGEABLE_EXTENSIONS: frozenset[str] = READABLE_EXTENSIONS | MANAGEABLE_ONLY_EXTENSIONS


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


# SPINNER STATUS MESSAGES
def _fmt_delete_path_arg(path) -> str:
    """
    Render delete_path's `path` arg (a single string OR a list, since
    batch delete support was added) as a clean status-line fragment.
    A bare f-string interpolation of a list renders its raw Python repr
    (e.g. "'['a.txt', 'b.md']'") — this keeps the display readable and
    truncates long batches instead of dumping every path inline.
    """
    if isinstance(path, list):
        shown = ", ".join(f"[white]'{p}'[/white]" for p in path[:3])
        if len(path) > 3:
            shown += f", and {len(path) - 3} more"
        return shown
    return f"[white]'{path}'[/white]"
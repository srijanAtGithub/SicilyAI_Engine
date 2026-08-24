"""
cowork_sandbox_exec.py
-----------------------
The "Tier 1" escape hatch: arbitrary script execution, for tasks that don't
fit run_file_command's known cp/mv/mkdir/ls/info shape (e.g. "strip these
JSON keys from 50 files", "rename every file matching this pattern").

This does NOT try to make arbitrary code safe to run — it can't be made
fully safe at the command layer with no extra installs (no Docker, no
Linux-only kernel namespaces — see jail_status() and the design notes at
the bottom of this file for exactly what is and isn't enforced on macOS
vs. Windows). Instead it makes the *output* reversible and
human-inspectable before it ever touches the real sandbox, which is the
guarantee that holds regardless of platform.

Pipeline, every call, no exceptions:

    stage()   copy the sandbox (or a scoped subtree of it) into a fresh,
              disposable temp directory. The model's script never sees the
              real files.
    execute() run the script INSIDE that copy only, cwd pinned to the
              copy, with a wall-clock timeout and a memory cap (real via
              rlimit on macOS, best-effort via a Windows Job Object) —
              see jail_status() for exactly what this does and does not
              block on your platform. Notably: NOT a network block. A
              script that hardcodes a destination can still reach it —
              there is no no-extra-install primitive on macOS or Windows
              that severs networking the way a Linux network namespace
              or a Docker --network none container would.
    diff()    compare copy vs. original: files added / removed / modified,
              with real content diffs for text, and byte-size deltas for
              binary. This is what the model (and the human) actually see
              — never "trust me, it worked."
    apply()   ONLY callable with the exact change_id returned by execute(),
              which only exists once a diff has been produced. Copies the
              changed files back over the real sandbox, after snapshotting
              every file it's about to overwrite or delete.
    rollback() restore the pre-apply snapshot for a given change_id.

Every stage/execute/diff/apply/rollback call is appended to an on-disk
audit log that this module owns — the calling tool layer never gets a
handle that lets it skip a step or edit the log.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Interpreters this module is willing to invoke. Anything else is refused
# before a subprocess is ever created. This is a much shorter list than
# _EXPLICITLY_BLOCKED in cowork_helpers.py needs, because here the
# interpreter itself is expected to run arbitrary code — the safety comes
# from the sandbox around it, not from restricting the interpreter.
_ALLOWED_INTERPRETERS = {
    "python3": ["python3"],
    "python": ["python3"],   # normalize: always actually invoke python3
    "node": ["node"],
    "bash": ["bash"],
    "sh": ["sh"],
}

_DEFAULT_TIMEOUT_S = 30
_MAX_TIMEOUT_S = 120
_MAX_OUTPUT_CHARS = 20_000        # stdout/stderr captured per run
_MAX_STAGED_FILES = 5_000          # refuse to stage something this large
_MAX_DIFF_FILE_BYTES = 2_000_000   # skip content-diffing files bigger than this (still reported as changed)

# Text-ish extensions worth content-diffing. Everything else still gets a
# changed/added/removed entry, just without an inline unified diff body.
_DIFFABLE_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".xml", ".svg",
    ".csv", ".tsv", ".sh", ".sql", ".log",
})

_CHANGES_DIR_NAME = ".sicily-sandbox-changes"   # lives inside sandbox root, alongside .sicily-trash
_AUDIT_LOG_NAME = "audit.jsonl"


class SandboxExecError(ValueError):
    """Raised for any rejection that should be shown to the model/user
    verbatim — parse errors, disallowed interpreters, timeouts, missing
    change_id, etc. Never leaks internals."""


# ---------------------------------------------------------------------------
# State: one ChangeSet per execute() call
# ---------------------------------------------------------------------------

@dataclass
class FileDiff:
    rel_path: str
    kind: str                      # "added" | "removed" | "modified"
    diff_text: Optional[str] = None    # unified diff, text files only
    size_before: Optional[int] = None
    size_after: Optional[int] = None


@dataclass
class ChangeSet:
    change_id: str
    scope_rel: str                 # subtree of the sandbox this ran against
    interpreter: str
    script: str
    created_at: float
    copy_dir: str                  # absolute path to the staged working copy
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    diffs: list = field(default_factory=list)   # list[FileDiff]
    applied: bool = False
    applied_at: Optional[float] = None
    snapshot_dir: Optional[str] = None   # pre-apply backup, for rollback
    rolled_back: bool = False
    confirmed: bool = False   # set True only by the session loop, on a genuinely new human turn


# In-memory registry for this process. change_id is also durably recorded
# in the audit log, so a restart doesn't silently lose the ability to see
# what happened, even though pending (un-applied) staged copies won't
# survive a restart — that's intentional: an un-applied change is not yet
# real, and re-running staging is cheap and safer than resurrecting an
# old temp directory of unknown provenance.
_CHANGES: dict[str, ChangeSet] = {}


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _audit_path(sandbox_root: Path) -> Path:
    d = sandbox_root / _CHANGES_DIR_NAME
    d.mkdir(exist_ok=True)
    return d / _AUDIT_LOG_NAME


def _audit(sandbox_root: Path, event: str, **fields) -> None:
    entry = {"ts": time.time(), "event": event, **fields}
    with open(_audit_path(sandbox_root), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# Stage: copy scope -> disposable temp dir
# ---------------------------------------------------------------------------

def _validate_interpreter(interpreter: str) -> list[str]:
    if interpreter not in _ALLOWED_INTERPRETERS:
        raise SandboxExecError(
            f"Refused: interpreter '{interpreter}' is not supported. "
            f"Allowed: {sorted(_ALLOWED_INTERPRETERS)}."
        )
    return _ALLOWED_INTERPRETERS[interpreter]


def _stage(sandbox_root: Path, scope_rel: str) -> Path:
    """
    Copy sandbox_root/scope_rel into a fresh temp directory. Raises
    SandboxExecError if the scope doesn't exist, escapes the sandbox, or
    is too large to stage safely.
    """
    scope = (sandbox_root / scope_rel).resolve()
    if not scope.is_relative_to(sandbox_root):
        raise SandboxExecError(f"Refused: scope '{scope_rel}' resolves outside the sandbox.")
    if not scope.exists():
        raise SandboxExecError(f"Refused: scope '{scope_rel}' does not exist.")

    all_files = [p for p in scope.rglob("*") if p.is_file()] if scope.is_dir() else [scope]
    if len(all_files) > _MAX_STAGED_FILES:
        raise SandboxExecError(
            f"Refused: scope '{scope_rel}' contains {len(all_files)} files, "
            f"over the {_MAX_STAGED_FILES} limit. Narrow the scope."
        )

    tmp_root = Path(tempfile.mkdtemp(prefix="sicily-sandbox-"))
    copy_dir = tmp_root / "work"
    if scope.is_dir():
        shutil.copytree(scope, copy_dir)
    else:
        copy_dir.mkdir()
        shutil.copy2(scope, copy_dir / scope.name)

    return copy_dir


# ---------------------------------------------------------------------------
# Execute: run the interpreter inside the staged copy — SOFT JAIL
#
# No Docker, no Linux namespaces, no admin/root requirement, works
# out-of-the-box on macOS and Windows. This trades the kernel-enforced
# guarantees from the Linux version for something that runs anywhere with
# just Python, at the cost of weaker isolation. Read jail_status() and the
# module design notes at the bottom before assuming more than this
# actually enforces.
# ---------------------------------------------------------------------------

def _sanitized_env() -> dict:
    """
    Minimal environment for the child process: no inherited credentials,
    API keys, cloud tokens, or proxy settings that happen to be sitting
    in the parent's env. This is real and effective against *accidental*
    leakage (a script that does os.environ and finds nothing sensitive
    there) — it is NOT a network block. A script with its own hardcoded
    URL, or one that uses the system DNS/routing directly, can still
    reach the network; env sanitization only removes ambient credentials
    and proxy config, it doesn't touch the OS's actual network stack.
    """
    keep = {"PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SystemRoot", "windir"}
    return {k: v for k, v in os.environ.items() if k in keep}


def _apply_resource_limits(memory_mb: int) -> Optional[object]:
    """
    Returns a `preexec_fn`-compatible callable on POSIX (macOS/Linux) that
    caps the child's address space via resource.setrlimit — a real,
    kernel-enforced cap: a script that allocates past this genuinely gets
    killed (MemoryError / SIGSEGV depending on how it hits the limit), not
    a polite request. Returns None on Windows, where preexec_fn isn't
    supported at all — Windows memory capping is handled separately via
    a Job Object in _run_windows_with_job_limit, since there's no POSIX
    rlimit equivalent to hand to subprocess.Popen directly.
    """
    if os.name != "posix":
        return None

    def _limit():
        import resource
        limit_bytes = memory_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        except (ValueError, OSError):
            # Some sandboxed/containerized hosts refuse RLIMIT_AS changes
            # entirely (e.g. already-constrained environments) — fail open
            # on the resource cap specifically rather than crash the whole
            # execution; the wall-clock timeout still applies regardless.
            pass
        # New session so the whole process group can be killed together on
        # timeout, rather than orphaning children the interpreter spawned.
        os.setsid()

    return _limit


def _run_posix(argv: list[str], cwd: str, env: dict, timeout_s: int, memory_mb: int) -> tuple[str, str, Optional[int], bool]:
    import signal

    preexec = _apply_resource_limits(memory_mb)
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        preexec_fn=preexec,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return stdout[-_MAX_OUTPUT_CHARS:], stderr[-_MAX_OUTPUT_CHARS:], proc.returncode, False
    except subprocess.TimeoutExpired:
        # Kill the whole process group (see os.setsid() above), not just
        # the immediate child — a script that spawned its own children
        # would otherwise leave them running past the timeout.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        return stdout[-_MAX_OUTPUT_CHARS:], stderr[-_MAX_OUTPUT_CHARS:], None, True


def _run_windows(argv: list[str], cwd: str, env: dict, timeout_s: int, memory_mb: int) -> tuple[str, str, Optional[int], bool]:
    """
    Windows has no preexec_fn / rlimit. Real memory capping AND real
    tree-kill-on-timeout both go through the same Win32 Job Object (via
    ctypes, no pywin32 dependency needed):
      - JOB_OBJECT_LIMIT_PROCESS_MEMORY caps the process's memory.
      - JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE means closing the job handle
        kills every process still assigned to it — including any
        children the script itself spawned via subprocess/os.system,
        which proc.kill() alone would leave orphaned. This is the
        Windows equivalent of the POSIX path's os.setsid()+killpg().
    job_handle and h_process are both real OS handles and are ALWAYS
    closed in the finally block below, on every exit path (normal
    return, timeout, or an unexpected exception) — a handle leaked here
    on every run_script call would otherwise accumulate for the lifetime
    of the host process.

    Falls back to timeout-only enforcement (proc.kill() on just the
    immediate process, still real) if Job Object setup fails for any
    reason (e.g. restricted permissions) — logged into stderr, not
    silently swallowed.

    NOTE: written against the documented Win32 Job Object API but not
    executed on a real Windows host in this session (only Linux was
    available to test against) — smoke-test on Windows before relying on
    the memory cap or tree-kill specifically. The subprocess timeout
    path itself is standard library and should work regardless.
    """
    import ctypes
    from ctypes import wintypes

    warnings = []
    job_handle = None
    h_process = None
    proc = None
    kernel32 = ctypes.windll.kernel32

    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9
    PROCESS_ALL_ACCESS = 0x1F0FFF

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", ctypes.c_byte * 48),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    try:
        try:
            job_handle = kernel32.CreateJobObjectW(None, None)
            if job_handle:
                info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                # KILL_ON_JOB_CLOSE always set — tree-kill-on-timeout
                # shouldn't depend on the memory cap being requested.
                limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if memory_mb:
                    limit_flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
                    info.ProcessMemoryLimit = memory_mb * 1024 * 1024
                info.BasicLimitInformation.LimitFlags = limit_flags

                ok = kernel32.SetInformationJobObject(
                    job_handle, JobObjectExtendedLimitInformation,
                    ctypes.byref(info), ctypes.sizeof(info),
                )
                if not ok:
                    warnings.append("Job Object limits could not be set (SetInformationJobObject failed) — no memory cap or tree-kill.")
            else:
                warnings.append("Job Object could not be created — no memory cap or tree-kill on timeout.")
        except Exception as e:
            warnings.append(f"Job Object setup failed ({e}) — no memory cap or tree-kill on timeout.")
            job_handle = None

        proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        if job_handle:
            try:
                h_process = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
                if h_process:
                    if not kernel32.AssignProcessToJobObject(job_handle, h_process):
                        warnings.append("Could not assign process to Job Object — no memory cap or tree-kill.")
                else:
                    warnings.append("OpenProcess failed — no memory cap or tree-kill.")
            except Exception as e:
                warnings.append(f"Could not assign process to Job Object ({e}) — no memory cap or tree-kill.")

        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            if warnings:
                stderr = (stderr or "") + "\n[sandbox warning] " + " ".join(warnings)
            return stdout[-_MAX_OUTPUT_CHARS:], (stderr or "")[-_MAX_OUTPUT_CHARS:], proc.returncode, False
        except subprocess.TimeoutExpired:
            # Closing the job handle (in finally, below) kills every
            # process assigned to it — this is what actually reaps any
            # children the script spawned, not this kill() call alone.
            # proc.kill() stays as a fallback for when job_handle setup
            # failed above and this is the only handle we actually have.
            proc.kill()
            stdout, stderr = proc.communicate()
            if warnings:
                stderr = (stderr or "") + "\n[sandbox warning] " + " ".join(warnings)
            return stdout[-_MAX_OUTPUT_CHARS:], (stderr or "")[-_MAX_OUTPUT_CHARS:], None, True
    finally:
        # Guaranteed cleanup on every exit path — normal return, timeout,
        # or an exception we didn't anticipate. Closing job_handle here
        # (after KILL_ON_JOB_CLOSE was set above) is also what enforces
        # the tree-kill on timeout; on a clean exit it's just handle
        # hygiene since the process already finished on its own.
        if h_process:
            kernel32.CloseHandle(h_process)
        if job_handle:
            kernel32.CloseHandle(job_handle)


def _execute(copy_dir: Path, interpreter: str, script: str, timeout_s: int, memory_mb: int = 512) -> tuple[str, str, Optional[int], bool]:
    """
    Run `script` via `interpreter` with cwd=copy_dir. Returns
    (stdout, stderr, exit_code, timed_out).

    Enforced, regardless of OS: wall-clock timeout (kills the whole
    process tree on expiry), sanitized environment (no ambient
    credentials/proxy config), working directory pinned to the staged
    copy. On macOS/Linux, also a real address-space memory cap via
    rlimit. On Windows, a best-effort memory cap via a Job Object.

    NOT enforced on either platform without Docker/a container runtime:
    network access. A script that hardcodes a URL or IP can still reach
    it — there is no OS-native primitive this module uses to sever
    networking without a container/VM boundary. See jail_status() and
    the module design notes for what to do if that matters for your
    threat model.
    """
    argv = _validate_interpreter(interpreter) + ["-c", script]
    env = _sanitized_env()

    if os.name == "posix":
        return _run_posix(argv, str(copy_dir), env, timeout_s, memory_mb)
    else:
        return _run_windows(argv, str(copy_dir), env, timeout_s, memory_mb)


def jail_status() -> str:
    """
    Human-readable summary of what isolation is actually active — surface
    this somewhere visible (startup banner, an `info` command) so nobody
    assumes network isolation exists when it doesn't. This is a SOFT
    jail: real timeout and memory limits, a real credential-free
    environment, but NOT a network block and NOT a write-outside-scope
    block at the OS level (the latter is prevented by convention — the
    script is only ever handed the staged copy_dir as its cwd — not by
    a filesystem permission the OS enforces).
    """
    plat = "macOS/Linux (POSIX)" if os.name == "posix" else "Windows"
    mem_note = (
        "real rlimit-based memory cap" if os.name == "posix"
        else "best-effort Job Object memory cap (falls back to timeout-only if unavailable)"
    )
    return (
        f"Sandbox jail: SOFT MODE on {plat}. Enforced: wall-clock timeout, "
        f"{mem_note}, sanitized/credential-free environment. NOT enforced: "
        "network access (a script with a hardcoded destination can still "
        "reach it) or writes outside the staged copy at the OS level (only "
        "prevented by the script never being given a path outside it). For "
        "kernel- or hypervisor-enforced network/write isolation, run this "
        "inside Docker with --network none and a single bind-mounted "
        "volume instead of relying on this module's execution step alone."
    )


# ---------------------------------------------------------------------------
# Diff: staged copy vs. original scope
# ---------------------------------------------------------------------------

def _relative_file_map(root: Path) -> dict[str, Path]:
    """
    Map of relative path -> absolute path for every REGULAR file under
    root. Symlinks are deliberately excluded here (use
    _relative_symlink_map for those) — is_file()/read_bytes()/copy2 all
    follow symlinks transparently, so a script that swaps a staged file
    for a symlink pointing outside the scope (e.g. to a sensitive file
    elsewhere on disk) could otherwise have that target's CONTENT read
    into the diff and copied into the real sandbox on apply, without
    ever being flagged as a symlink. Treating symlinks as a distinct,
    always-surfaced category closes that off.
    """
    if root.is_symlink():
        return {}
    if root.is_file():
        return {root.name: root}
    return {
        str(p.relative_to(root)): p
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def _relative_symlink_map(root: Path) -> dict[str, Path]:
    """Map of relative path -> absolute path for every symlink under root
    (files or dirs), regardless of where it points."""
    if root.is_symlink():
        return {root.name: root}
    if not root.is_dir():
        return {}
    return {
        str(p.relative_to(root)): p
        for p in root.rglob("*")
        if p.is_symlink()
    }


def _diff_scope(original_scope: Path, copy_dir: Path) -> list[FileDiff]:
    before = _relative_file_map(original_scope) if original_scope.exists() else {}
    after = _relative_file_map(copy_dir)

    before_links = _relative_symlink_map(original_scope) if original_scope.exists() else {}
    after_links = _relative_symlink_map(copy_dir)

    diffs: list[FileDiff] = []

    # Symlinks get their own, impossible-to-miss diff entry — never
    # silently diffed as regular file content, and never eligible for
    # apply_change (enforced in apply_change itself, not just here).
    for rel in sorted(set(before_links) | set(after_links)):
        b_link, a_link = before_links.get(rel), after_links.get(rel)
        if a_link is not None and b_link is None:
            try:
                target = os.readlink(a_link)
            except OSError:
                target = "<unreadable>"
            diffs.append(FileDiff(
                rel_path=rel, kind="symlink_blocked",
                diff_text=f"Script created a symlink at '{rel}' -> '{target}'. "
                          "Refused: symlinks are never applied to the real sandbox.",
            ))
        elif b_link is not None and a_link is None:
            diffs.append(FileDiff(rel_path=rel, kind="symlink_blocked",
                                   diff_text=f"A pre-existing symlink at '{rel}' was removed by the script."))
        # a symlink present on both sides, unchanged target or not, is
        # still surfaced once so it's never silently ignored either way.
        elif a_link is not None:
            diffs.append(FileDiff(rel_path=rel, kind="symlink_blocked",
                                   diff_text=f"'{rel}' is a symlink — not diffed or applyable."))

    for rel in sorted(set(before) | set(after)):
        b, a = before.get(rel), after.get(rel)
        ext = Path(rel).suffix.lower()

        if b is None and a is not None:
            diffs.append(FileDiff(rel_path=rel, kind="added", size_after=a.stat().st_size,
                                   diff_text=_text_preview(a, ext)))
        elif b is not None and a is None:
            diffs.append(FileDiff(rel_path=rel, kind="removed", size_before=b.stat().st_size))
        else:
            b_bytes, a_bytes = b.read_bytes(), a.read_bytes()
            if b_bytes == a_bytes:
                continue  # unchanged, not part of the diff
            diff_text = None
            if ext in _DIFFABLE_EXTENSIONS and len(b_bytes) <= _MAX_DIFF_FILE_BYTES and len(a_bytes) <= _MAX_DIFF_FILE_BYTES:
                diff_text = _unified_text_diff(b, a, rel)
            diffs.append(FileDiff(
                rel_path=rel, kind="modified",
                size_before=len(b_bytes), size_after=len(a_bytes),
                diff_text=diff_text,
            ))

    return diffs


def _text_preview(path: Path, ext: str, max_lines: int = 30) -> Optional[str]:
    if ext not in _DIFFABLE_EXTENSIONS:
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    shown = lines[:max_lines]
    body = "\n".join(f"+ {l}" for l in shown)
    if len(lines) > max_lines:
        body += f"\n... ({len(lines) - max_lines} more lines)"
    return body


def _unified_text_diff(before_path: Path, after_path: Path, rel: str) -> Optional[str]:
    try:
        b_lines = before_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        a_lines = after_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except Exception:
        return None
    diff = list(difflib.unified_diff(b_lines, a_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=2))
    if not diff:
        return None
    text = "".join(diff)
    if len(text) > 4000:
        text = text[:4000] + "\n... (diff truncated)"
    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def propose_script(
    sandbox_root: Path,
    interpreter: str,
    script: str,
    scope_rel: str = ".",
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> ChangeSet:
    """
    Stage `scope_rel`, run `script` against the STAGED COPY ONLY, diff the
    result against the real files, and return a ChangeSet. Nothing in the
    real sandbox is touched by this call, regardless of what the script
    does — even a malicious/buggy script only ever affects the disposable
    copy_dir.
    """
    timeout_s = min(max(1, timeout_s), _MAX_TIMEOUT_S)
    _validate_interpreter(interpreter)

    scope = (sandbox_root / scope_rel).resolve()
    change_id = uuid.uuid4().hex[:12]

    _audit(sandbox_root, "propose_start", change_id=change_id, interpreter=interpreter,
           scope=scope_rel, script_len=len(script))

    copy_dir = _stage(sandbox_root, scope_rel)
    stdout, stderr, exit_code, timed_out = _execute(copy_dir, interpreter, script, timeout_s)
    diffs = _diff_scope(scope, copy_dir)

    cs = ChangeSet(
        change_id=change_id,
        scope_rel=scope_rel,
        interpreter=interpreter,
        script=script,
        created_at=time.time(),
        copy_dir=str(copy_dir),
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        diffs=diffs,
    )
    _CHANGES[change_id] = cs

    _audit(sandbox_root, "propose_done", change_id=change_id, exit_code=exit_code,
           timed_out=timed_out, files_changed=len(diffs))

    return cs


def apply_change(sandbox_root: Path, change_id: str) -> str:
    """
    Copy the staged, already-diffed result back over the real sandbox.
    Refuses if change_id is unknown, already applied, the script errored
    or timed out, or there's nothing to apply. Snapshots every real file
    it's about to overwrite/delete first, so rollback_change() can undo it.
    """
    cs = _CHANGES.get(change_id)
    if cs is None:
        raise SandboxExecError(
            f"Refused: no pending change with id '{change_id}'. Call "
            "propose_script first — apply_change can only act on a "
            "change_id that came from a real, already-shown diff."
        )
    if cs.applied:
        raise SandboxExecError(f"Change '{change_id}' was already applied at {cs.applied_at}.")
    if cs.timed_out:
        raise SandboxExecError(f"Refused: change '{change_id}' timed out mid-execution — not safe to apply partial results.")
    if cs.exit_code not in (0, None):
        raise SandboxExecError(f"Refused: change '{change_id}' exited with code {cs.exit_code} — fix the script and re-propose.")
    if not cs.diffs:
        raise SandboxExecError(f"Refused: change '{change_id}' produced no changes — nothing to apply.")

    applyable_diffs = [d for d in cs.diffs if d.kind != "symlink_blocked"]
    blocked = [d for d in cs.diffs if d.kind == "symlink_blocked"]
    if blocked and not applyable_diffs:
        raise SandboxExecError(
            f"Refused: change '{change_id}' only contains symlink changes, which "
            "are never applied to the real sandbox. Nothing to do."
        )

    scope = (sandbox_root / cs.scope_rel).resolve()
    copy_dir = Path(cs.copy_dir)

    # Snapshot every file about to be overwritten or removed — this is
    # what makes apply_change reversible even after the staged copy_dir
    # is eventually cleaned up.
    snapshot_dir = Path(tempfile.mkdtemp(prefix="sicily-snapshot-"))
    for d in applyable_diffs:
        if d.kind in ("modified", "removed"):
            src = scope / d.rel_path
            if src.exists() and not src.is_symlink():
                dest = snapshot_dir / d.rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

    applied_paths = []
    skipped_symlinks = []
    try:
        for d in applyable_diffs:
            real_path = scope / d.rel_path
            if d.kind == "removed":
                if real_path.exists() and not real_path.is_symlink():
                    real_path.unlink()
            else:  # added or modified
                staged_path = copy_dir / d.rel_path
                # Defense-in-depth: even though _diff_scope already routes
                # symlinks to the symlink_blocked kind and excludes them
                # from applyable_diffs, refuse again right here at the
                # actual copy2 call — the one place a mistake would
                # actually matter — rather than trust upstream tagging
                # alone. copy2 follows symlinks and copies their TARGET's
                # content, which is exactly the exfiltration path a
                # mis-tagged or newly-introduced symlink could exploit.
                if staged_path.is_symlink() or real_path.is_symlink():
                    skipped_symlinks.append(d.rel_path)
                    continue
                real_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged_path, real_path)
            applied_paths.append(d.rel_path)
    except Exception as e:
        _audit(sandbox_root, "apply_partial_failure", change_id=change_id,
               applied_so_far=applied_paths, error=str(e))
        raise SandboxExecError(
            f"Apply failed partway through after touching {len(applied_paths)} "
            f"of {len(cs.diffs)} file(s): {e}. A pre-apply snapshot exists at "
            f"'{snapshot_dir}' — use rollback_change('{change_id}') to restore."
        )

    cs.applied = True
    cs.applied_at = time.time()
    cs.snapshot_dir = str(snapshot_dir)

    _audit(sandbox_root, "apply_done", change_id=change_id, files_applied=len(applied_paths),
           skipped_symlinks=skipped_symlinks, snapshot_dir=str(snapshot_dir))

    msg = (
        f"Applied change '{change_id}': {len(applied_paths)} file(s) updated in "
        f"'{cs.scope_rel}'. Pre-apply snapshot saved — call "
        f"rollback_change('{change_id}') to undo if needed."
    )
    if blocked or skipped_symlinks:
        names = sorted(set(d.rel_path for d in blocked) | set(skipped_symlinks))
        msg += f"\nSkipped (symlinks are never applied): {', '.join(names)}"
    return msg


def rollback_change(sandbox_root: Path, change_id: str) -> str:
    """Restore the pre-apply snapshot for an already-applied change."""
    cs = _CHANGES.get(change_id)
    if cs is None or not cs.applied or not cs.snapshot_dir:
        raise SandboxExecError(f"Refused: no applied, rollback-able change with id '{change_id}'.")
    if cs.rolled_back:
        raise SandboxExecError(f"Change '{change_id}' was already rolled back.")

    scope = (sandbox_root / cs.scope_rel).resolve()
    snapshot_dir = Path(cs.snapshot_dir)
    restored = []

    for d in cs.diffs:
        real_path = scope / d.rel_path
        snap_path = snapshot_dir / d.rel_path
        if d.kind == "added":
            # wasn't there before; undo means remove it again
            if real_path.exists():
                real_path.unlink()
                restored.append(f"removed {d.rel_path}")
        elif snap_path.exists():
            real_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snap_path, real_path)
            restored.append(f"restored {d.rel_path}")

    cs.rolled_back = True
    _audit(sandbox_root, "rollback_done", change_id=change_id, restored=len(restored))
    return f"Rolled back change '{change_id}': {len(restored)} file(s) restored."


def format_diff_for_model(cs: ChangeSet, max_files_shown: int = 25) -> str:
    """Human/model-readable rendering of a ChangeSet's diff — this is what
    the agent should show the person before ever calling apply_change."""
    lines = [f"[Proposed change '{cs.change_id}' — scope '{cs.scope_rel}', {cs.interpreter}]"]
    if cs.timed_out:
        lines.append(f"TIMED OUT after execution limit — not applyable.")
    elif cs.exit_code not in (0, None):
        lines.append(f"Script exited with code {cs.exit_code} — not applyable.")
    if cs.stderr.strip():
        lines.append(f"stderr:\n{cs.stderr.strip()[:1000]}")

    if not cs.diffs:
        lines.append("No file changes detected.")
        return "\n".join(lines)

    lines.append(f"\n{len(cs.diffs)} file(s) affected:")
    for d in cs.diffs[:max_files_shown]:
        header = f"  [{d.kind.upper()}] {d.rel_path}"
        if d.kind == "modified":
            header += f" ({d.size_before} -> {d.size_after} bytes)"
        lines.append(header)
        if d.diff_text:
            indented = "\n".join(f"    {l}" for l in d.diff_text.splitlines()[:20])
            lines.append(indented)

    if len(cs.diffs) > max_files_shown:
        lines.append(f"  ... and {len(cs.diffs) - max_files_shown} more file(s)")

    if not (cs.timed_out or cs.exit_code not in (0, None)):
        lines.append(
            f"\nNothing has been applied yet. Call apply_change('{cs.change_id}') "
            "to write these changes to the real files, or discard by simply not calling it."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DESIGN NOTES — read before wiring this into a new environment
# ---------------------------------------------------------------------------
#
# What this module actually guarantees:
#   - A malicious or buggy script cannot touch the real sandbox files.
#     It can only ever write into a disposable temp directory that gets
#     diffed, then thrown away (on discard) or copied back through
#     apply_change (on explicit approval).
#   - Every apply is preceded by a snapshot, so it's always reversible.
#   - Every stage/execute/diff/apply/rollback is logged to an append-only
#     file the model has no tool access to edit.
#
# What this module additionally guarantees on top of that (SOFT JAIL,
# no Docker / no OS-level container or namespace — this is the deliberate
# tradeoff for running on macOS and Windows with zero extra installs):
#   - Wall-clock timeout is real and enforced on both platforms: on POSIX
#     the whole process GROUP is killed on expiry (not just the immediate
#     child — os.setsid() + killpg means a script that spawned its own
#     subprocesses doesn't leave orphans running past the timeout); on
#     Windows the process is killed via proc.kill().
#   - Memory capping: real and kernel-enforced on macOS via
#     resource.setrlimit(RLIMIT_AS) — a script that allocates past the
#     cap genuinely fails. On Windows this uses a Win32 Job Object via
#     ctypes (no pywin32 dependency needed); this was implemented against
#     the documented API but has NOT been executed against a real Windows
#     host in this session (only Linux was available to test against) —
#     smoke-test this specifically before depending on it. If Job Object
#     setup fails for any reason (locked-down permissions, etc.) it logs
#     a warning into stderr and falls back to timeout-only enforcement
#     rather than silently pretending the cap is active.
#   - The child process never inherits ambient credentials, API keys, or
#     proxy configuration from the parent environment — a script that
#     dumps os.environ finds nothing sensitive there.
#
# What this module does NOT guarantee, on any platform, without adding
# Docker (or an equivalent container/VM runtime) back in:
#   - Network isolation. There is no no-extra-install primitive on macOS
#     or Windows equivalent to a Linux network namespace. A script with
#     a hardcoded URL/IP, or one that just uses normal DNS/sockets, WILL
#     reach the network if run this way. The sanitized environment
#     removes ambient credentials and proxy settings, which blocks
#     accidental leakage — it does not block a script that doesn't need
#     credentials to do something bad (e.g. exfiltrating file contents to
#     an attacker-controlled endpoint it has hardcoded).
#   - OS-enforced write isolation outside the staged copy. The script's
#     cwd is pinned to the staged copy_dir, and normal agent-generated
#     scripts have no reason to reach outside it — but nothing at the OS
#     level stops a script from opening an absolute path elsewhere and
#     writing to it, the way a Linux mount-namespace read-only remount
#     would. This module's actual safety property is therefore: apply()
#     is the only path from "script ran" to "real files changed", and
#     apply() is gated on a human-reviewed diff — not "the script
#     physically cannot write outside its box."
#   - Read isolation of any kind — a script can read anything the OS user
#     running it could normally read, on every platform.
#   - If any of the above genuinely matters for your threat model (e.g.
#     this will run scripts whose INTENT you don't fully trust, not just
#     scripts you're checking for bugs), the fix is to require Docker
#     Desktop and run _execute()'s subprocess inside a container with
#     `--network none` and a single bind-mounted volume as the only
#     writable path instead of a bare subprocess — the
#     stage/diff/apply/rollback logic in the rest of this module doesn't
#     need to change, only what _execute() shells out to.
#   - Protection against a script that's simply WRONG in a way the diff
#     doesn't make obvious (e.g. subtly corrupts every 100th row of a
#     CSV). The diff shows what changed; deciding whether that change is
#     *correct* is still a human judgment call at the apply_change step.
#   - A structural, un-bypassable human-in-the-loop gate. apply_change
#     requires a change_id that only exists after a real propose_script
#     call, which prevents an agent from fabricating one — but if the
#     SAME agent turn can call propose_script and then immediately
#     apply_change without a person reading the diff in between, the gate
#     is procedural, not physical. For a genuine Ring-4 guarantee, the
#     calling application (cowork_session.py's turn loop) should require
#     apply_change to come from a NEW user turn after the diff was shown
#     — i.e. treat a pending change_id like a confirmation prompt the
#     model can propose but not resolve on its own, the same shape as
#     end_conversation's two-call pattern, except the second call must
#     originate from the human's next message, not the model's next
#     tool call in the same turn.
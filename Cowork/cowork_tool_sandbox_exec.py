"""
cowork_tool_sandbox_exec.py
----------------------------
The Tier 1 escape hatch, exposed as tools: run_script / rollback_change.

Use run_file_command (cp/mv/mkdir/ls/info) for anything that fits its
shape — it's instant and needs no staging. Reach for run_script only when
a task doesn't fit that shape: bulk content edits (e.g. stripping JSON
keys across many files), pattern-based transforms, anything that needs a
real interpreter.

run_script stages `scope` into a disposable copy, runs the script only
against that copy, diffs it against the real files, and — if the diff is
clean — WRITES IT THROUGH IMMEDIATELY. There is no separate apply_change
call anymore: the diff you get back is a record of what was just written,
not a proposal waiting on approval. Show it to the user for review.
rollback_change() is the undo path if the result turns out wrong, and can
be called by the model itself, not only on explicit user request.

See cowork_sandbox_exec.py for the staging/diff/write-through/rollback
pipeline itself. This file is just the @tool binding + docstrings the
model reads to decide when/how to call it.
"""

from langchain_core.tools import tool

from Cowork.cowork_helpers import _get_sandbox_root
from Cowork.cowork_sandbox_exec import (
    propose_script,
    rollback_change as _rollback_change,
    format_diff_for_model,
    SandboxExecError,
)


@tool
def run_script(script: str, interpreter: str = "python3", scope: str = ".", timeout_s: int = 30) -> str:
    """
    Run a script for tasks run_file_command can't express — bulk content
    edits, pattern-based transforms across many files, anything needing
    real logic.

    `script` runs against a disposable STAGED COPY of `scope`, never the
    real files directly. If it completes cleanly (no error, no timeout)
    and produces a diff, that diff is WRITTEN THROUGH TO THE REAL SANDBOX
    IMMEDIATELY, before this call returns — there is no separate confirm
    step and nothing else to call to make it take effect. What you get
    back is a record of what changed AND was written, not a proposal.

    Show the returned diff to the user so they can review what happened.
    If the script errored, timed out, or the diff looks wrong once you
    read it, call rollback_change(change_id) — you don't need to wait for
    the user to ask; if a follow-up check shows the result is broken, undo
    it yourself and say so.

    python3, node, bash, or sh only. Soft jail (no Docker): real wall-clock
    timeout and memory cap enforced; network is NOT blocked. Only run
    scripts whose intent you trust. jail_status() gives exact enforced limits.

    Prefer the smallest `scope` that covers the task — a broad first
    attempt that fails costs more to stage/diff than a narrow one that
    succeeds. If you're iterating run_script to refine the same transform
    and it's not converging after ~3 tries, stop, show the current diff,
    and ask the user for direction instead of continuing to adjust it —
    each iteration is now a real write, not a disposable draft, so
    thrashing here isn't free the way it was when apply was a separate step.

    Args:
        script:      Full source, passed via `-c`. Operate on the CURRENT
                    DIRECTORY (`.`, Path("."), os.getcwd()) — it IS the
                    staged copy of `scope` already. If scope="apps",
                    iterate Path(".").iterdir(), NOT Path("apps").iterdir()
                    — that path doesn't exist inside the staged copy.
        interpreter: One of "python3", "node", "bash", "sh". Default python3.
        scope:       Subpath to stage (e.g. "apps"). Default "." = whole sandbox.
        timeout_s:   Wall-clock limit in seconds (max 120). Default 30.

    Returns:
        A readable diff (files added/removed/modified + unified diffs for
        text) plus the change_id needed for rollback_change() if it needs
        undoing. States plainly whether the write actually happened. If
        the script errored or timed out, nothing was written, and this
        says so.
    """
    root = _get_sandbox_root()
    try:
        cs = propose_script(root, interpreter=interpreter, script=script, scope_rel=scope, timeout_s=timeout_s)
    except SandboxExecError as e:
        return str(e)
    return format_diff_for_model(cs)


@tool
def rollback_change(change_id: str) -> str:
    """
    Undo an already-written run_script change, restoring the real files
    to their state right before that run_script call wrote them.

    Call this whenever a change turns out to be wrong — whether the user
    asks for it, or you determine it yourself (e.g. a follow-up read or
    verification step shows the result is broken). You don't need
    explicit permission to roll back a change you just made and now
    believe is incorrect; say what you're undoing and why.

    Args:
        change_id: The id shown in a prior run_script diff output.
    """
    root = _get_sandbox_root()
    try:
        return _rollback_change(root, change_id)
    except SandboxExecError as e:
        return str(e)


# ---------------------------------------------------------------------------
# STATUS — write-through is now the default, not gated behind confirmation.
#    run_script() stages, executes, diffs, and — via propose_script() in
#    cowork_sandbox_exec.py — writes the result to the real sandbox in the
#    same call, no separate apply step. apply_change() as a @tool has been
#    removed; the underlying apply_change() function still exists in
#    cowork_sandbox_exec.py but is now called internally by
#    propose_script() itself, right after diffing, never exposed to the
#    model directly.
#
#    This replaces the previous design, where ChangeSet.confirmed started
#    False and a tool-layer apply_change() refused to run until a NEW
#    human turn flipped it (via a flip-loop in cowork_session.py's chat
#    loop). That flip-loop has been removed along with the `confirmed`
#    field on ChangeSet — see the design notes at the bottom of
#    cowork_sandbox_exec.py for the reasoning and how to revert if this
#    turns out to be the wrong call.
#
#    rollback_change() is unchanged in mechanics but is now the ONLY undo
#    path (no pre-write gate exists anymore), so its correctness matters
#    more than it used to, and the model is explicitly encouraged above to
#    use it proactively rather than only on request.
# ---------------------------------------------------------------------------
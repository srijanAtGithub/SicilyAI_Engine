"""
cowork_tool_sandbox_exec.py
----------------------------
The Tier 1 escape hatch, exposed as tools: run_script / apply_change /
rollback_change.

Use run_file_command (cp/mv/mkdir/ls/info) for anything that fits its
shape — it's instant and needs no staging. Reach for run_script only when
a task doesn't fit that shape: bulk content edits (e.g. stripping JSON
keys across many files), pattern-based transforms, anything that needs a
real interpreter.

run_script NEVER touches real files directly — see cowork_sandbox_exec.py
for the staging/diff/apply pipeline. This file is just the @tool binding
+ docstrings the model reads to decide when/how to call it.
"""

from langchain_core.tools import tool

from Cowork.cowork_helpers import _get_sandbox_root
from Cowork.cowork_sandbox_exec import (
    propose_script,
    apply_change as _apply_change,
    rollback_change as _rollback_change,
    format_diff_for_model,
    SandboxExecError,
    _CHANGES,
)


@tool
def run_script(script: str, interpreter: str = "python3", scope: str = ".", timeout_s: int = 30) -> str:
    """
    Run a script for tasks run_file_command can't express — bulk content
    edits, pattern-based transforms across many files, anything needing
    real logic.

    Never touches real sandbox files: `script` runs against a disposable
    COPY of `scope`. Returns a diff only. Nothing is written until you
    separately call apply_change() with the returned change_id, after
    actually reading the diff — a broken script still produces a diff, so
    don't apply without reading it.

    python3, node, bash, or sh only. Soft jail (no Docker): real wall-clock
    timeout and memory cap enforced; network is NOT blocked. Only run
    scripts whose intent you trust. jail_status() gives exact enforced limits.

    Prefer the smallest `scope` that covers the task — a broad first
    attempt that fails costs more to stage/diff than a narrow one that
    succeeds. If you're iterating run_script to refine the same transform
    and it's not converging after ~3 tries, stop, show the current diff,
    and ask the user for direction instead of continuing to adjust it.

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
        text) plus the change_id needed to apply it. If the script errored
        or timed out, says so and marks the change not-applyable.
    """
    root = _get_sandbox_root()
    try:
        cs = propose_script(root, interpreter=interpreter, script=script, scope_rel=scope, timeout_s=timeout_s)
    except SandboxExecError as e:
        return str(e)
    return format_diff_for_model(cs)


@tool
def apply_change(change_id: str) -> str:
    """
    Write a previously proposed run_script change to the REAL sandbox
    files. Requires a change_id from an actual run_script diff — cannot
    be invented.

    Only call after the diff has actually been read and confirmed correct
    (same care as delete_path(dry_run=False)) — irreversible until
    rollback_change(). A pre-apply snapshot is taken automatically so
    rollback_change() can undo it.

    Refused if not yet confirmed by the user: show the diff and wait for
    their go-ahead in their NEXT message — don't call this again same-turn.

    Args:
        change_id: The id shown in a prior run_script diff output.
    """
    cs = _CHANGES.get(change_id)
    if cs is None:
        return f"Refused: no such change_id '{change_id}'."
    if not cs.confirmed:
        return (
            f"Change '{change_id}' hasn't been confirmed by the user yet. "
            "Show them the diff and wait for their explicit go-ahead in "
            "their NEXT message — do not call apply_change again in this "
            "same turn."
        )

    root = _get_sandbox_root()
    try:
        return _apply_change(root, change_id)
    except SandboxExecError as e:
        return str(e)


@tool
def rollback_change(change_id: str) -> str:
    """
    Undo an already-applied run_script change, restoring the real files
    to their state right before apply_change() ran.

    Args:
        change_id: The id of a change that was previously applied.
    """
    root = _get_sandbox_root()
    try:
        return _rollback_change(root, change_id)
    except SandboxExecError as e:
        return str(e)


# ---------------------------------------------------------------------------
# STATUS — structural gate (b) is now implemented, not just proposed:
#    ChangeSet.confirmed starts False. apply_change() above refuses to run
#    until it's True. The only place that flips it is cowork_session.py's
#    chat loop, at the top of each NEW iteration (i.e. right after a fresh
#    console.input() from the human) — see the comment there. This means
#    a same-turn run_script -> apply_change chain, like the one in the
#    original test transcript, now gets refused: the model must show the
#    diff and wait for the person's actual next message before apply_change
#    can succeed. Same shape as end_conversation's two-call confirm
#    pattern — first call is a proposal, the resolving call must come from
#    a genuinely separate turn, not the model's own next tool call.
#
#    This is a policy choice, not a hard safety requirement — the
#    stage/diff/snapshot/rollback pipeline in cowork_sandbox_exec.py was
#    already safe (reversible, never bypasses the sandbox) without it.
#    Revert by deleting the `if not cs.confirmed` check above, the
#    `confirmed` field on ChangeSet, and the flip-loop in cowork_session.py
#    if this turns out to add more friction than it's worth.
# ---------------------------------------------------------------------------
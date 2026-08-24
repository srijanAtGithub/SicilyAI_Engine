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
    real logic rather than a single cp/mv/mkdir call.

    SAFETY MODEL: this NEVER touches the real sandbox. `script` runs only
    against a disposable COPY of `scope`. You get a diff of what the
    script would change. Nothing is written until you separately call
    apply_change() with the exact change_id this returns — and only after
    you have actually read the diff. A broken or half-working script still
    produces a diff; applying it blindly defeats the point of this tool.

    Only python3, node, bash, and sh are supported. Runs in SOFT JAIL
    (no Docker): real wall-clock timeout (default 30s, max 120s) and a
    real memory cap are enforced, but network is NOT blocked. Never use
    this on a script whose intent you don't already trust; call
    jail_status() if you need the exact wording of what's enforced.

    Prefer the smallest `scope` that covers the task — smaller scope means
    a faster stage/diff and a readable diff.

    Args:
        script:      Full source, passed via `-c`. Operate on the CURRENT
                    DIRECTORY (`.`, Path("."), os.getcwd()) — the working
                    directory IS ALREADY the staged copy of `scope`. If
                    scope="apps", iterate Path(".").iterdir() for the
                    module folders; do NOT do Path("apps").iterdir() —
                    that path does not exist inside the staged copy.
        interpreter: One of "python3", "node", "bash", "sh". Default python3.
        scope:       Subpath within the sandbox to stage (e.g. "apps",
                    "reports/q3"). Default "." = whole sandbox.
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
    files. Only works with a change_id that came from an actual
    run_script call's diff output — you cannot invent one.

    Only call this after you (and ideally the person you're working for)
    have actually looked at the diff and confirmed it is correct. This is
    the irreversible-until-rollback step; treat it with the same care as
    delete_path(dry_run=False).

    A pre-apply snapshot is taken automatically, so rollback_change() can
    undo this if the applied change turns out to be wrong.

    If the change has not been confirmed by the user yet, this call is
    refused. Show the diff and wait for their explicit go-ahead in their
    NEXT message — do not call apply_change again in the same turn.

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
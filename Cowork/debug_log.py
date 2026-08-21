"""
debug_log.py
------------
One switch for minimal Sicily debug logging: which tool ran, in what
order, and how many tokens each step used. Nothing else.

Usage
-----
    DEBUG = True   # see logs
    DEBUG = False  # production — completely silent, zero overhead
"""

# ── The switch ───────────────────────────────────────────────────────────────
# Flip this by hand. True  = print tool/token debug lines.
#                    False = production behaviour, nothing extra printed.
DEBUG = False

_step = 0  # running counter so you can see call order across a whole turn


def reset_step_counter() -> None:
    """Call at the start of each user turn so numbering restarts at 1."""
    global _step
    _step = 0


def log_tool_call(name: str, args: dict | None = None) -> None:
    if not DEBUG:
        return
    global _step
    _step += 1
    safe_args = {
        k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v)
        for k, v in (args or {}).items()
    }
    print(f"[DEBUG] #{_step} tool_call  -> {name} args={safe_args}")


def log_tool_tokens(name: str, tokens: int) -> None:
    """
    Log tokens consumed by a tool's result once it's back in context
    (i.e. how many tokens the tool's output will cost on the next LLM call).
    """
    if not DEBUG:
        return
    print(f"[DEBUG] #{_step} tool_result {name}: ~{tokens} tokens")


def log_llm_tokens(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> None:
    """Log tokens used by a single LLM request/response."""
    if not DEBUG:
        return
    total = input_tokens + output_tokens
    print(
        f"[DEBUG] llm_call    {model}: "
        f"in={input_tokens} out={output_tokens} cached={cached_tokens} total={total}"
    )


def log_turn_summary(tool_calls: int, llm_calls: int, input_tokens: int, output_tokens: int) -> None:
    """One-line total for the whole turn."""
    if not DEBUG:
        return
    total = input_tokens + output_tokens
    print(
        f"[DEBUG] === turn summary: {tool_calls} tool call(s), {llm_calls} llm call(s), "
        f"{input_tokens} input + {output_tokens} output = {total} tokens total ==="
    )


def log_error(message: str, exc: Exception | str | None = None) -> None:
    """Log error details only if DEBUG mode is active."""
    if not DEBUG:
        return
    if exc:
        print(f"[DEBUG] ERROR: {message} -> {exc}")
    else:
        print(f"[DEBUG] ERROR: {message}")
"""
shared_utils.py
---------------
Lightweight utilities shared across dimensions (Agent, Navigator, etc.).
No heavy imports — safe to import before configuration.load_config() is called.
"""


def content_to_text(content) -> str:
    """
    Normalize AIMessage.content (str | list of blocks) to plain text.

    Message content from LangChain/LLM providers can show up as:
      - a plain string
      - a list of blocks, where each block is either:
          - a raw string
          - a dict with a "type" of "text" or "output_text" and a "text" key
          - an object with a `.text` attribute (some LangChain content types)

    Anything else (e.g. pure tool-call or reasoning-only blocks) is skipped.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # Prefer final text blocks; skip pure reasoning/tool blocks.
                if block.get("type") in ("text", "output_text") and block.get("text"):
                    parts.append(block["text"])
            elif hasattr(block, "text"):
                t = getattr(block, "text", None)
                if t:
                    parts.append(t)
        return "\n".join(p for p in parts if p).strip()
    return str(content)

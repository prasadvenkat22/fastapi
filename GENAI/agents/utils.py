from typing import Any


def extract_text(content: Any) -> str:
    """Extract the plain text answer from a langchain_anthropic AIMessage.content.

    Claude Opus 5 has adaptive thinking on by default — when it decides to think,
    .content becomes a list of blocks (e.g. thinking + text) instead of a bare
    string, so callers must not assume content is always `str`.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)

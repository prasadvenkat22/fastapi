from typing import Any


def extract_text(content: Any) -> str:
    """Extract the plain text answer from a chat model's AIMessage.content.

    GeminiChat returns a plain string; the list-of-blocks branch is kept because
    some LangChain models return content as blocks, and a caller must not assume
    `str`.
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

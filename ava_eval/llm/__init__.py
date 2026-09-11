"""Gemini access and the prompt library."""

from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str, **fields: str) -> str:
    """Load a prompt template and fill its placeholders.

    Templates use ``{{UPPERCASE}}`` tokens rather than Python's ``str.format``
    braces, because the prompts contain JSON examples full of ``{`` and ``}``
    that ``format`` would try to interpret as fields.

    Raises:
        FileNotFoundError: if the template does not exist.
        KeyError: if a supplied field has no matching token, or a token in the
            template was left unfilled. Both are silent-failure bugs otherwise --
            a prompt shipped to the model with a literal ``{{TRANSCRIPT}}`` in it
            still gets an answer, just a worthless one.
    """
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"No prompt template at {path}")

    text = path.read_text(encoding="utf-8")
    for key, value in fields.items():
        token = "{{" + key.upper() + "}}"
        if token not in text:
            raise KeyError(f"Prompt {name!r} has no placeholder {token}")
        text = text.replace(token, value)

    leftover = [line for line in text.splitlines() if "{{" in line and "}}" in line]
    if leftover:
        raise KeyError(f"Prompt {name!r} still has unfilled placeholders: {leftover[0].strip()}")

    return text

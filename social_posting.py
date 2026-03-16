"""Helpers for publish-time social post formatting."""

from __future__ import annotations


AI_DISCLOSURE_LINE = "(🤖 Disclosure: This content is AI generated #AIDisclosure)"


def append_ai_disclosure(text: str) -> str:
    """Append AI disclosure on a new line if not already present."""
    base = (text or "").rstrip()
    if base.endswith(AI_DISCLOSURE_LINE):
        return base
    if not base:
        return AI_DISCLOSURE_LINE
    return f"{base}\n\n{AI_DISCLOSURE_LINE}"


def disclosure_overhead_chars() -> int:
    """Return worst-case characters added when disclosure is appended."""
    return 2 + len(AI_DISCLOSURE_LINE)

"""Short title generation utilities."""

import re

from openai import OpenAI


def _clean_title(text: str) -> str:
    """Normalize title output to a single clean line."""
    first_line = (text or "").strip().splitlines()[0] if text else ""
    cleaned = first_line.strip().strip("\"'`")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _word_count(text: str) -> int:
    """Count words in a title candidate."""
    return len(re.findall(r"\b[\w'-]+\b", text))


def _fallback_title(headline: str) -> str:
    """Build a safe fallback title in the required length range."""
    words = re.findall(r"\b[\w'-]+\b", headline)
    if not words:
        return "Dream Logic Inside The Copy Room"

    keep = [w for w in words if len(w) > 3][:7]
    if len(keep) < 5:
        keep = (words[:5] + ["copyroom", "dreams"])[:7]
    fallback = " ".join(keep[:7]).title()
    if _word_count(fallback) < 5:
        fallback = "Static Dreams In The Copy Room"
    return fallback


def generate_title(client: OpenAI, model: str, headline: str) -> str:
    """Generate a cryptic headline-related title (5-10 words)."""
    instruction = (
        "Create one short title for a surreal satirical artwork.\n"
        "The title must be cryptic but related to the topic.\n"
        "Length must be between 5 and 10 words total.\n"
        "Return only the title text, no quotes, no punctuation-only lines.\n\n"
        f"Headline: {headline}"
    )

    try:
        response = client.responses.create(model=model, input=instruction, temperature=1.0)
    except Exception as exc:
        raise RuntimeError(f"Failed to generate title: {exc}") from exc

    candidate = _clean_title(response.output_text or "")
    if not candidate:
        return _fallback_title(headline)

    count = _word_count(candidate)
    if count < 5:
        return _fallback_title(headline)
    if count > 10:
        trimmed = " ".join(candidate.split()[:10])
        if _word_count(trimmed) >= 5:
            return trimmed
        return _fallback_title(headline)

    return candidate

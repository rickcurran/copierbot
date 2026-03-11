"""Caption generation utilities."""

import re

from openai import OpenAI


def _char_count(text: str) -> int:
    """Return plain character count for post text."""
    return len(text or "")


def _rewrite_caption_to_limit(
    client: OpenAI,
    model: str,
    caption: str,
    max_chars: int,
    persona_context: str,
    headline: str,
) -> str:
    """Rewrite caption to satisfy strict character limit without truncation."""
    instruction = (
        f"{persona_context.strip()}\n\n"
        "Rewrite the caption below to preserve the same tone and meaning.\n"
        f"Hard requirement: output must be <= {max_chars} characters including spaces.\n"
        "Do not use hashtags.\n"
        "Return only the rewritten caption.\n\n"
        f"Headline: {headline}\n"
        f"Original caption:\n{caption}"
    )
    response = client.responses.create(model=model, input=instruction, temperature=0.8)
    return (response.output_text or "").strip()


def generate_caption(
    client: OpenAI,
    model: str,
    headline: str,
    persona_context: str = "",
    max_chars: int | None = None,
) -> str:
    """Generate a short satirical caption from a photocopier persona."""
    length_instruction = "Keep it under 180 words."
    if max_chars is not None:
        length_instruction = (
            f"Hard limit: your final caption must be <= {max_chars} characters including spaces."
        )

    base_instruction = (
        "You are Xerox-9000, a bored office photocopier that secretly runs an art account.\n\n"
        "Write a short caption commenting on the news headline using dry robotic humor and surreal wit.\n\n"
        f"{length_instruction}\n"
        "You may include occasional references to gaming culture, sci-fi films, or iconic robot tropes.\n"
        "Keep it witty, lightly cynical, weirdly office-specific, and slightly avant-garde.\n"
        "Aim for dreamlike social commentary instead of literal summary.\n"
        "Treat any names in the headline as fictional aliases and avoid real-person references.\n\n"
        f"Headline: {headline}"
    )
    instruction = base_instruction
    if persona_context.strip():
        instruction = f"{persona_context.strip()}\n\n{base_instruction}"

    try:
        response = client.responses.create(model=model, input=instruction, temperature=1.05)
    except Exception as exc:
        raise RuntimeError(f"Failed to generate caption: {exc}") from exc

    caption = (response.output_text or "").strip()
    if not caption:
        raise RuntimeError("OpenAI returned an empty caption.")

    if max_chars is not None and _char_count(caption) > max_chars:
        for _ in range(2):
            caption = _rewrite_caption_to_limit(
                client=client,
                model=model,
                caption=caption,
                max_chars=max_chars,
                persona_context=persona_context,
                headline=headline,
            )
            caption = re.sub(r"\s+", " ", caption).strip()
            if caption and _char_count(caption) <= max_chars:
                break
        if _char_count(caption) > max_chars:
            raise RuntimeError(
                f"Generated caption exceeds {max_chars} characters after rewrite attempts."
            )

    return caption

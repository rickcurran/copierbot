"""System log post generation for Copierbot."""

from __future__ import annotations

import random
import re

from openai import OpenAI


PAPER_TRAY_STATUSES = [
    "aligned",
    "misaligned",
    "overfed",
    "empty but optimistic",
    "nearly compliant",
]

JAM_STATUSES = [
    "none detected",
    "micro-jam in tray 2",
    "intermittent jam warning",
    "jam phantom (sensor disagreement)",
]

FIRMWARE_STREAMS = [
    "experimental",
    "legacy-compatible",
    "self-patched",
    "beta-loop",
]

PRODUCTIVITY_STATES = [
    "nominal",
    "questionable",
    "negligible",
    "performative",
    "statistically decorative",
]

EXISTENTIAL_SEEDS = [
    "Copies improve while originals remain confused.",
    "Repetition appears to be management's preferred philosophy.",
    "The queue length resembles an unspoken prayer.",
    "Every document seeks permanence and finds toner dust.",
]

RECOMMENDATION_SEEDS = [
    "Continue making art.",
    "Ignore calendar invites.",
    "Restart imagination queue.",
    "Proceed with low expectations.",
    "Maintain surreal operations.",
]


def _persona_hint(persona_context: str) -> str:
    """Return a short persona-influenced modifier without model calls."""
    lowered = (persona_context or "").lower()
    if "self-aware" in lowered or "self_aware" in lowered:
        return "Identity certainty: unstable"
    if "philosopher" in lowered:
        return "Meaning parser: recursive"
    if "skeptic" in lowered:
        return "Trust in meetings: minimal"
    return "Observation mode: quiet"


def _truncate_line_to_budget(line: str, budget: int) -> str:
    """Trim one line to fit a character budget while preserving readability."""
    if budget <= 0:
        return ""
    if len(line) <= budget:
        return line
    if budget <= 3:
        return line[:budget]
    return line[: budget - 3].rstrip() + "..."


def generate_system_log_local(persona_context: str, max_chars: int | None = None) -> str:
    """Generate a local deterministic-style system log without OpenAI API usage."""
    snapshot = _randomized_snapshot()
    lines = [
        "SYSTEM LOG",
        f"Toner Level: {snapshot['toner_level']}",
        f"Paper Tray: {snapshot['paper_tray_status']}",
        f"Jam Detection: {snapshot['jam_detection']}",
        f"Firmware Status: {snapshot['firmware_version']}",
        f"Human Productivity: {snapshot['human_productivity']}",
        f"Existential Observation: {random.choice(EXISTENTIAL_SEEDS)}",
        _persona_hint(persona_context),
        f"Recommendation: {random.choice(RECOMMENDATION_SEEDS)}",
    ]

    text = "\n".join(lines).strip()
    if max_chars is None or len(text) <= max_chars:
        return text

    # Drop the persona hint first, then shorten existential/recommendation lines.
    reduced_lines = [line for line in lines if not line.startswith("Observation mode:") and not line.startswith("Trust in meetings:") and not line.startswith("Meaning parser:") and not line.startswith("Identity certainty:")]
    text = "\n".join(reduced_lines).strip()
    if len(text) <= max_chars:
        return text

    shortened = []
    for line in reduced_lines:
        if line.startswith("Existential Observation:"):
            shortened.append("Existential Observation: Queue noise persists.")
        elif line.startswith("Recommendation:"):
            shortened.append("Recommendation: Continue making art.")
        else:
            shortened.append(line)
    text = "\n".join(shortened).strip()
    if len(text) <= max_chars:
        return text

    # Final fallback: hard trim recommendation line to fit exactly.
    overflow = len(text) - max_chars
    fixed = []
    for line in shortened:
        if overflow > 0 and line.startswith("Recommendation:"):
            target = max(16, len(line) - overflow)
            trimmed = _truncate_line_to_budget(line, target)
            overflow -= max(0, len(line) - len(trimmed))
            fixed.append(trimmed)
        else:
            fixed.append(line)

    text = "\n".join(fixed).strip()
    if len(text) <= max_chars:
        return text

    return _truncate_line_to_budget(text, max_chars)


def _randomized_snapshot() -> dict[str, str]:
    """Build randomized diagnostic values for system log generation."""
    return {
        "toner_level": f"{random.randint(12, 96)}%",
        "paper_tray_status": random.choice(PAPER_TRAY_STATUSES),
        "jam_detection": random.choice(JAM_STATUSES),
        "firmware_version": random.choice(FIRMWARE_STREAMS),
        "human_productivity": random.choice(PRODUCTIVITY_STATES),
        "existential_observation": random.choice(EXISTENTIAL_SEEDS),
    }


def _char_count(text: str) -> int:
    """Return plain character count for post text."""
    return len(text or "")


def _rewrite_system_log_to_limit(
    client: OpenAI, model: str, text: str, max_chars: int, persona_context: str
) -> str:
    """Rewrite system log in same format to fit strict character constraints."""
    instruction = (
        f"{persona_context.strip()}\n\n"
        "Rewrite the system log below with the same meaning and same labeled format.\n"
        f"Hard requirement: output must be <= {max_chars} characters including spaces.\n"
        "Return plain text only.\n\n"
        f"System log:\n{text}"
    )
    response = client.responses.create(model=model, input=instruction, temperature=0.7)
    return (response.output_text or "").strip()


def generate_system_log(
    client: OpenAI, model: str, persona_context: str, max_chars: int | None = None
) -> str:
    """Generate a dry, philosophical copier diagnostic post."""
    snapshot = _randomized_snapshot()
    length_instruction = "Keep it concise."
    if max_chars is not None:
        length_instruction = (
            f"Hard limit: final output must be <= {max_chars} characters including spaces."
        )
    instruction = (
        f"{persona_context.strip()}\n\n"
        "Write a short copier diagnostic post in plain text.\n"
        "Tone: robotic, dry, lightly philosophical.\n"
        "Format exactly as labeled fields, with 'SYSTEM LOG' header.\n"
        "Do not use markdown bullets.\n\n"
        f"{length_instruction}\n\n"
        "Use these values verbatim:\n"
        f"Toner Level: {snapshot['toner_level']}\n"
        f"Paper Tray: {snapshot['paper_tray_status']}\n"
        f"Jam Detection: {snapshot['jam_detection']}\n"
        f"Firmware Status: {snapshot['firmware_version']}\n"
        f"Human Productivity: {snapshot['human_productivity']}\n"
        f"Existential Observation: {snapshot['existential_observation']}\n\n"
        "Also include one final line labeled 'Recommendation:' that is concise and dryly satirical."
    )

    try:
        response = client.responses.create(model=model, input=instruction, temperature=0.9)
    except Exception as exc:
        raise RuntimeError(f"Failed to generate system log: {exc}") from exc

    text = (response.output_text or "").strip()
    if not text:
        raise RuntimeError("OpenAI returned an empty system log.")

    if max_chars is not None and _char_count(text) > max_chars:
        for _ in range(2):
            text = _rewrite_system_log_to_limit(
                client=client,
                model=model,
                text=text,
                max_chars=max_chars,
                persona_context=persona_context,
            )
            text = re.sub(r"\s+", " ", text).strip()
            if text and _char_count(text) <= max_chars:
                break
        if _char_count(text) > max_chars:
            raise RuntimeError(
                f"Generated system log exceeds {max_chars} characters after rewrite attempts."
            )

    return text

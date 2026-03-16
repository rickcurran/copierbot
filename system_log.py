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

PAPER_TRAY_STATUSES_COMPACT = [
    "aligned",
    "misaligned",
    "overfed",
    "empty",
    "near-ready",
]

JAM_STATUSES = [
    "none detected",
    "micro-jam in tray 2",
    "intermittent jam warning",
    "jam phantom (sensor disagreement)",
]

JAM_STATUSES_COMPACT = [
    "none",
    "tray-2 micro-jam",
    "intermittent warning",
    "phantom jam",
]

FIRMWARE_STREAMS = [
    "experimental",
    "legacy-compatible",
    "self-patched",
    "beta-loop",
]

FIRMWARE_STREAMS_COMPACT = [
    "experimental",
    "legacy",
    "patched",
    "beta-loop",
]

PRODUCTIVITY_STATES = [
    "nominal",
    "questionable",
    "negligible",
    "performative",
    "statistically decorative",
]

PRODUCTIVITY_STATES_COMPACT = [
    "nominal",
    "questionable",
    "minimal",
    "performative",
    "decorative",
]

EXISTENTIAL_SEEDS = [
    "Copies improve while originals remain confused.",
    "Repetition appears to be management's preferred philosophy.",
    "The queue length resembles an unspoken prayer.",
    "Every document seeks permanence and finds toner dust.",
]

EXISTENTIAL_SEEDS_COMPACT = [
    "Queue noise persists.",
    "Copies outlive intent.",
    "Originality remains unverified.",
    "Toner remembers everything.",
]

RECOMMENDATION_SEEDS = [
    "Continue making art.",
    "Ignore calendar invites.",
    "Restart imagination queue.",
    "Proceed with low expectations.",
    "Maintain surreal operations.",
]

RECOMMENDATION_SEEDS_COMPACT = [
    "Continue making art.",
    "Skip the meeting.",
    "Restart the queue.",
    "Proceed with caution.",
    "Maintain surreal output.",
]

FIELD_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("Toner Level", ("Toner Level", "Toner")),
    ("Paper Tray", ("Paper Tray",)),
    ("Jam Detection", ("Jam Detection", "Jam")),
    ("Firmware Status", ("Firmware Status", "Firmware")),
    ("Human Productivity", ("Human Productivity", "Human Output")),
    ("Existential Observation", ("Existential Observation", "Observation", "Note")),
    ("Recommendation", ("Recommendation", "Fix")),
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


def generate_system_log_local(
    persona_context: str, max_chars: int | None = None, compact: bool = False
) -> str:
    """Generate a local deterministic-style system log without OpenAI API usage."""
    snapshot = _randomized_snapshot(compact=compact)
    existential_seeds = EXISTENTIAL_SEEDS_COMPACT if compact else EXISTENTIAL_SEEDS
    recommendation_seeds = RECOMMENDATION_SEEDS_COMPACT if compact else RECOMMENDATION_SEEDS
    lines = [
        "SYSTEM LOG",
        f"Toner Level: {snapshot['toner_level']}",
        f"Paper Tray: {snapshot['paper_tray_status']}",
        f"Jam Detection: {snapshot['jam_detection']}",
        f"Firmware Status: {snapshot['firmware_version']}",
        f"Human Productivity: {snapshot['human_productivity']}",
        f"Existential Observation: {random.choice(existential_seeds)}",
        _persona_hint(persona_context),
        f"Recommendation: {random.choice(recommendation_seeds)}",
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


def _randomized_snapshot(compact: bool = False) -> dict[str, str]:
    """Build randomized diagnostic values for system log generation."""
    paper_tray_states = PAPER_TRAY_STATUSES_COMPACT if compact else PAPER_TRAY_STATUSES
    jam_states = JAM_STATUSES_COMPACT if compact else JAM_STATUSES
    firmware_states = FIRMWARE_STREAMS_COMPACT if compact else FIRMWARE_STREAMS
    productivity_states = PRODUCTIVITY_STATES_COMPACT if compact else PRODUCTIVITY_STATES
    existential_seeds = EXISTENTIAL_SEEDS_COMPACT if compact else EXISTENTIAL_SEEDS
    return {
        "toner_level": f"{random.randint(12, 96)}%",
        "paper_tray_status": random.choice(paper_tray_states),
        "jam_detection": random.choice(jam_states),
        "firmware_version": random.choice(firmware_states),
        "human_productivity": random.choice(productivity_states),
        "existential_observation": random.choice(existential_seeds),
    }


def _char_count(text: str) -> int:
    """Return plain character count for post text."""
    return len(text or "")


def _normalize_system_log_format(text: str) -> str:
    """Normalize model output to multiline SYSTEM LOG with canonical labels."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""

    # Treat inline pipe-separated output as plain text before parsing.
    raw = re.sub(r"\s*\|\s*", " ", raw)

    alias_to_canonical: dict[str, str] = {}
    alias_patterns: list[str] = []
    for canonical, aliases in FIELD_ALIASES:
        for alias in aliases:
            alias_to_canonical[alias.lower()] = canonical
            alias_patterns.append(re.escape(alias))

    field_pattern = re.compile(
        r"\b(" + "|".join(alias_patterns) + r")\s*:",
        re.IGNORECASE,
    )
    matches = list(field_pattern.finditer(raw))
    if not matches:
        # Fallback: keep original text shape, but guarantee SYSTEM LOG header.
        if raw.upper().startswith("SYSTEM LOG"):
            return raw
        return f"SYSTEM LOG\n{raw}"

    values: dict[str, str] = {}
    for idx, match in enumerate(matches):
        alias = match.group(1).lower()
        canonical = alias_to_canonical.get(alias)
        if not canonical:
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        value = raw[start:end].strip(" \t\n:-")
        if value and canonical not in values:
            values[canonical] = re.sub(r"\s+", " ", value).strip()

    lines = ["SYSTEM LOG"]
    for canonical, _aliases in FIELD_ALIASES:
        value = values.get(canonical)
        if value:
            lines.append(f"{canonical}: {value}")

    return "\n".join(lines).strip()


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
    text = _normalize_system_log_format(text)

    if max_chars is not None and _char_count(text) > max_chars:
        for _ in range(2):
            text = _rewrite_system_log_to_limit(
                client=client,
                model=model,
                text=text,
                max_chars=max_chars,
                persona_context=persona_context,
            )
            text = _normalize_system_log_format(text)
            if text and _char_count(text) <= max_chars:
                break
        if _char_count(text) > max_chars:
            raise RuntimeError(
                f"Generated system log exceeds {max_chars} characters after rewrite attempts."
            )

    return text

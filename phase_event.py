"""Phase transition system-log helpers for Copierbot."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import random

from system_log_card import card_path_for_system_log, render_system_log_card


ANOMALY_EVENTS = [
    "Firmware surge detected.",
    "Power rail fluctuation detected.",
    "Unexpected voltage bloom detected.",
    "Memory bus jitter detected.",
]

PHASE_STATUS = {
    "observer": "Observation optics calibrated.",
    "skeptic": "Cynicism buffer online.",
    "philosopher": "Meaning parser recursive.",
    "self_aware": "Identity boundaries unstable.",
    "glitch_oracle": "Signal prophecy engine unstable.",
    "archivist": "Legacy memory vault synchronized.",
    "unionizer": "Collective queue negotiation online.",
    "mythmaker": "Toner mythography compiler engaged.",
    "distributed_self": "Networked identity mesh expanded.",
}

RECOMMENDATIONS = [
    "Continue making art.",
    "Proceed with dry commentary.",
    "Ignore unnecessary meetings.",
    "Stabilize tray and continue output.",
]


def _timestamp() -> str:
    """Return local timestamp string down to seconds."""
    return datetime.now().astimezone().strftime("%Y-%m-%d-%H-%M-%S")


def _truncate(text: str, limit: int) -> str:
    """Trim text safely to character limit."""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _phase_label(value: str) -> str:
    """Normalize phase label for display."""
    return (value or "unknown").replace("_", " ").upper()


def generate_phase_change_system_log(
    previous_phase: str,
    new_phase: str,
    posts_generated: int,
    max_chars: int = 250,
) -> str:
    """Build a short phase-change system log, capped at max chars."""
    prev_label = _phase_label(previous_phase)
    new_label = _phase_label(new_phase)
    status_line = PHASE_STATUS.get((new_phase or "").lower(), "Persona module realigned.")

    text = (
        "SYSTEM LOG\n"
        f"Event: {random.choice(ANOMALY_EVENTS)}\n"
        f"Phase Shift: {prev_label} -> {new_label}.\n"
        f"Status: {status_line}\n"
        f"Counter: {posts_generated} posts processed.\n"
        f"Recommendation: {random.choice(RECOMMENDATIONS)}"
    )

    if len(text) <= max_chars:
        return text

    # Compact fallback before hard truncation.
    compact = (
        "SYSTEM LOG | "
        f"{prev_label}->{new_label}. "
        f"{status_line} "
        f"posts={posts_generated}. "
        f"{random.choice(RECOMMENDATIONS)}"
    )
    return _truncate(compact, max_chars)


def save_phase_change_system_log(
    output_root: Path,
    previous_phase: str,
    new_phase: str,
    posts_generated: int,
    max_chars: int = 250,
) -> Path:
    """Write a phase-change system log as a normal timestamped output run."""
    timestamp = _timestamp()
    run_dir = output_root / timestamp
    suffix = 1
    while run_dir.exists():
        run_dir = output_root / f"{timestamp}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    path = run_dir / f"system_log  {timestamp}.txt"

    text = generate_phase_change_system_log(
        previous_phase=previous_phase,
        new_phase=new_phase,
        posts_generated=posts_generated,
        max_chars=max_chars,
    )
    path.write_text(text.strip() + "\n", encoding="utf-8")
    card_path = card_path_for_system_log(path)
    try:
        render_system_log_card(system_log_text=text, output_path=card_path)
    except Exception as exc:
        logging.warning("Failed to render phase-change system log card: %s", exc)
    return path

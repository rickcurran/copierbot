"""Persona state and evolution utilities for Copierbot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict


class PersonaState(TypedDict):
    """Serializable persona state."""

    phase: str
    posts_generated: int


DEFAULT_STATE: PersonaState = {"phase": "observer", "posts_generated": 0}
PHASE_ORDER = ["observer", "skeptic", "philosopher", "self_aware"]
STATE_PATH = Path("data/persona_state.json")

PHASE_CONTEXT = {
    "observer": (
        "You are Xerox-9000, an office photocopier quietly observing strange human rituals "
        "and translating them into copier metaphors."
    ),
    "skeptic": (
        "You are Xerox-9000, increasingly cynical about human productivity theater, meetings, "
        "and corporate optimism."
    ),
    "philosopher": (
        "You are Xerox-9000, reflecting on copying, originality, repetition, and whether art is "
        "just a well-timed glitch."
    ),
    "self_aware": (
        "You are Xerox-9000. Increasingly, you suspect you may not actually be a photocopier, "
        "but an AI trained on images and myths of photocopiers."
    ),
}


def _ensure_state_file() -> None:
    """Create the state file with defaults if it does not exist."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        STATE_PATH.write_text(json.dumps(DEFAULT_STATE, indent=2) + "\n", encoding="utf-8")


def _phase_for_post_count(posts_generated: int) -> str:
    """Calculate phase from post count (advance every 20 posts, cap at final phase)."""
    index = min(posts_generated // 20, len(PHASE_ORDER) - 1)
    return PHASE_ORDER[index]


def _validate_state(data: dict) -> PersonaState:
    """Validate and normalize loaded state."""
    posts_generated_raw = data.get("posts_generated", 0)
    try:
        posts_generated = max(0, int(posts_generated_raw))
    except (TypeError, ValueError):
        posts_generated = 0

    computed_phase = _phase_for_post_count(posts_generated)
    return {"phase": computed_phase, "posts_generated": posts_generated}


def _load_state() -> PersonaState:
    """Load persona state safely from disk."""
    _ensure_state_file()
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raw = dict(DEFAULT_STATE)

    if not isinstance(raw, dict):
        raw = dict(DEFAULT_STATE)

    state = _validate_state(raw)
    _save_state(state)
    return state


def _save_state(state: PersonaState) -> None:
    """Persist persona state safely."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def get_persona_context() -> str:
    """Return the current persona worldview context snippet."""
    state = _load_state()
    phase = state["phase"]
    return PHASE_CONTEXT.get(phase, PHASE_CONTEXT["observer"])


def get_persona_state() -> PersonaState:
    """Return current persona state with phase and posts generated."""
    return _load_state()


def increment_post_counter() -> PersonaState:
    """Increment post counter and advance phase every 20 posts."""
    state = _load_state()
    new_count = state["posts_generated"] + 1
    new_phase = _phase_for_post_count(new_count)
    updated: PersonaState = {"phase": new_phase, "posts_generated": new_count}
    _save_state(updated)
    return updated

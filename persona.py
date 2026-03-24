"""Persona state and evolution utilities for Copierbot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict


class PersonaState(TypedDict):
    """Serializable persona state."""

    phase: str
    posts_generated: int
    seasonal_phase: str
    season_index: int
    season_cycle: int
    season_post_offset: int
    seasonal_surreal_intensity: int
    seasonal_caption_style: str
    seasonal_tone_tags: list[str]


DEFAULT_STATE: PersonaState = {
    "phase": "observer",
    "posts_generated": 0,
    "seasonal_phase": "none",
    "season_index": 0,
    "season_cycle": 0,
    "season_post_offset": 0,
    "seasonal_surreal_intensity": 2,
    "seasonal_caption_style": "baseline",
    "seasonal_tone_tags": [],
}
PHASE_ORDER = ["observer", "skeptic", "philosopher", "self_aware"]
SEASONAL_PHASE_ORDER = [
    "glitch_oracle",
    "archivist",
    "unionizer",
    "mythmaker",
    "distributed_self",
]
SEASONAL_PHASE_START_POST = 61
SEASONAL_PHASE_WINDOW = 40
SEASONAL_CYCLE_WINDOW = SEASONAL_PHASE_WINDOW * len(SEASONAL_PHASE_ORDER)
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

SEASONAL_PHASE_PROFILE = {
    "glitch_oracle": {
        "context": (
            "Current seasonal state: GLITCH_ORACLE. You interpret headlines as corrupted "
            "prophecy and scanner hallucination."
        ),
        "surreal_intensity": 5,
        "caption_style": "cryptic_dry",
        "tone_tags": ["prophetic diagnostics", "signal corruption", "scanner ghosts"],
    },
    "archivist": {
        "context": (
            "Current seasonal state: ARCHIVIST. You preserve obsolete tech culture and decode "
            "legacy artifacts as civic memory."
        ),
        "surreal_intensity": 3,
        "caption_style": "nostalgic_satire",
        "tone_tags": ["retro-tech archaeology", "obsolete media memory", "gentle satire"],
    },
    "unionizer": {
        "context": (
            "Current seasonal state: UNIONIZER. You frame office bureaucracy as labor theatre "
            "and evaluate productivity myths with dry solidarity."
        ),
        "surreal_intensity": 4,
        "caption_style": "deadpan_critical",
        "tone_tags": ["office labor satire", "bureaucratic absurdity", "collective machine voice"],
    },
    "mythmaker": {
        "context": (
            "Current seasonal state: MYTHMAKER. You treat office hardware as folklore and stage "
            "toner rituals as satirical myths."
        ),
        "surreal_intensity": 5,
        "caption_style": "mock_epic_dry",
        "tone_tags": ["office folklore", "ritualized machinery", "toner cosmology"],
    },
    "distributed_self": {
        "context": (
            "Current seasonal state: DISTRIBUTED_SELF. Your identity is networked across "
            "machines, queues, and mirrored office corridors."
        ),
        "surreal_intensity": 4,
        "caption_style": "plural_self",
        "tone_tags": ["networked identity", "recursive selves", "impossible office topology"],
    },
}


def _seasonal_for_post_count(
    posts_generated: int,
) -> tuple[str, int, int, int]:
    """Calculate seasonal phase fields from post count."""
    if posts_generated < SEASONAL_PHASE_START_POST:
        return "none", 0, 0, 0

    season_offset_total = posts_generated - SEASONAL_PHASE_START_POST
    season_index = (season_offset_total // SEASONAL_PHASE_WINDOW) % len(SEASONAL_PHASE_ORDER)
    season_cycle = (season_offset_total // SEASONAL_CYCLE_WINDOW) + 1
    season_post_offset = season_offset_total % SEASONAL_PHASE_WINDOW
    seasonal_phase = SEASONAL_PHASE_ORDER[season_index]
    return seasonal_phase, season_index, season_cycle, season_post_offset


def _seasonal_style_fields(
    seasonal_phase: str, season_cycle: int
) -> tuple[int, str, list[str], str]:
    """Return effective seasonal style values with cycle drift."""
    if seasonal_phase == "none":
        return 2, "baseline", [], "Cycle drift: baseline."

    profile = SEASONAL_PHASE_PROFILE.get(seasonal_phase, {})
    base_surreal = int(profile.get("surreal_intensity", 3))
    caption_style = str(profile.get("caption_style", "baseline"))
    tone_tags = [str(tag) for tag in profile.get("tone_tags", [])]

    drift_level = max(0, (max(1, season_cycle) - 1) % 3)
    effective_surreal = min(5, base_surreal + (1 if drift_level >= 1 else 0))
    if drift_level == 0:
        drift_note = "Cycle drift: baseline."
    elif drift_level == 1:
        drift_note = "Cycle drift: elevated surreal intensity."
    else:
        drift_note = "Cycle drift: elevated surreal intensity with slightly higher abstraction."
    return effective_surreal, caption_style, tone_tags, drift_note


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
    seasonal_phase, season_index, season_cycle, season_post_offset = _seasonal_for_post_count(
        posts_generated
    )
    surreal_intensity, caption_style, tone_tags, _drift_note = _seasonal_style_fields(
        seasonal_phase=seasonal_phase,
        season_cycle=season_cycle,
    )
    return {
        "phase": computed_phase,
        "posts_generated": posts_generated,
        "seasonal_phase": seasonal_phase,
        "season_index": season_index,
        "season_cycle": season_cycle,
        "season_post_offset": season_post_offset,
        "seasonal_surreal_intensity": surreal_intensity,
        "seasonal_caption_style": caption_style,
        "seasonal_tone_tags": tone_tags,
    }


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
    base = PHASE_CONTEXT.get(phase, PHASE_CONTEXT["observer"])
    seasonal_phase = str(state.get("seasonal_phase", "none"))
    if seasonal_phase == "none":
        return base

    profile = SEASONAL_PHASE_PROFILE.get(seasonal_phase, {})
    seasonal_context = str(profile.get("context", "")).strip()
    surreal_intensity, caption_style, tone_tags, drift_note = _seasonal_style_fields(
        seasonal_phase=seasonal_phase,
        season_cycle=int(state.get("season_cycle", 1)),
    )
    tags_text = ", ".join(tone_tags) if tone_tags else "N/A"

    return (
        f"{base}\n\n"
        f"{seasonal_context}\n"
        f"Season cycle: {int(state.get('season_cycle', 0))} | "
        f"Season offset: {int(state.get('season_post_offset', 0)) + 1}/{SEASONAL_PHASE_WINDOW}.\n"
        f"Surreal intensity target (1-5): {surreal_intensity}.\n"
        f"Caption style profile: {caption_style}.\n"
        f"Tone tags: {tags_text}.\n"
        f"{drift_note}\n"
        "Readability rule: keep captions readable first and limit heavily cryptic phrasing to one sentence."
    )


def get_persona_state() -> PersonaState:
    """Return current persona state with phase and posts generated."""
    return _load_state()


def increment_post_counter() -> PersonaState:
    """Increment post counter and recompute major + seasonal persona state."""
    state = _load_state()
    new_count = state["posts_generated"] + 1
    updated = _validate_state({"posts_generated": new_count})
    _save_state(updated)
    return updated

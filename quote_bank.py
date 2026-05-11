"""Curated quote-bank loading helpers for mention replies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
QUOTE_BANK_PATH = BASE_DIR / "data" / "quote_bank.json"


class QuoteBankError(RuntimeError):
    """Raised when the local quote bank cannot be loaded safely."""


def load_quote_bank(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the quote bank and return normalized entries."""
    target = path or QUOTE_BANK_PATH
    if not target.exists():
        return []

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuoteBankError(f"Failed to read quote bank at {target}: {exc}") from exc

    if not isinstance(payload, dict):
        raise QuoteBankError("Quote bank root payload must be a JSON object.")
    raw_entries = payload.get("entries", [])
    if not isinstance(raw_entries, list):
        raise QuoteBankError("Quote bank 'entries' must be a JSON array.")

    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        entry["id"] = str(entry.get("id", "")).strip()
        if not entry["id"]:
            continue
        entry["source_type"] = str(entry.get("source_type", "")).strip()
        entry["source_title"] = str(entry.get("source_title", "")).strip()
        entry["character"] = str(entry.get("character", "")).strip()
        entry["category"] = str(entry.get("category", "")).strip()
        entry["reply_intent"] = str(entry.get("reply_intent", "")).strip()
        entry["direct_quote"] = str(entry.get("direct_quote", "")).strip()
        entry["quote_slot"] = str(entry.get("quote_slot", "")).strip()
        entry["review_status"] = str(entry.get("review_status", "")).strip()
        entry["trigger_patterns"] = [
            str(item).strip().lower()
            for item in entry.get("trigger_patterns", [])
            if str(item).strip()
        ]
        entry["themes"] = [
            str(item).strip().lower() for item in entry.get("themes", []) if str(item).strip()
        ]
        entry["phase_fit"] = [
            str(item).strip().lower() for item in entry.get("phase_fit", []) if str(item).strip()
        ]
        entry["season_fit"] = [
            str(item).strip().lower() for item in entry.get("season_fit", []) if str(item).strip()
        ]
        entry["tone"] = [
            str(item).strip().lower() for item in entry.get("tone", []) if str(item).strip()
        ]
        entry["content_flags"] = [
            str(item).strip().lower()
            for item in entry.get("content_flags", [])
            if str(item).strip()
        ]
        entry["selection_notes"] = str(entry.get("selection_notes", "")).strip()
        entry["enabled"] = bool(entry.get("enabled", False))
        try:
            entry["priority"] = int(entry.get("priority", 0))
        except (TypeError, ValueError):
            entry["priority"] = 0
        try:
            entry["max_chars"] = int(entry.get("max_chars", 280))
        except (TypeError, ValueError):
            entry["max_chars"] = 280
        try:
            entry["cooldown_hours"] = int(entry.get("cooldown_hours", 24))
        except (TypeError, ValueError):
            entry["cooldown_hours"] = 24
        try:
            entry["max_uses_per_30_days"] = int(entry.get("max_uses_per_30_days", 10))
        except (TypeError, ValueError):
            entry["max_uses_per_30_days"] = 10

        variants = entry.get("approved_variants", {})
        normalized_variants: dict[str, str] = {}
        if isinstance(variants, dict):
            for key, value in variants.items():
                variant_key = str(key).strip().lower()
                variant_text = str(value).strip()
                if variant_key and variant_text:
                    normalized_variants[variant_key] = variant_text
        entry["approved_variants"] = normalized_variants
        entries.append(entry)

    return entries

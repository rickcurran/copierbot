"""Monitor social mentions/comments and post in-character system-log replies."""

from __future__ import annotations

import argparse
import html
import json
import logging
from pathlib import Path
import random
import re
from typing import Any

from config import get_settings
from mention_archive import save_mention_response_log
from persona import get_persona_context, get_persona_state
from quote_bank import QuoteBankError, load_quote_bank
from alerts import send_slack_alert
from social.bluesky_adapter import BlueskyAPIError, BlueskyAdapter, load_bluesky_config
from social.instagram_adapter import InstagramAPIError, InstagramAdapter, load_instagram_config
from social.mastodon_adapter import MastodonAPIError, MastodonAdapter, load_mastodon_config
from social.wordpress_adapter import WordpressAPIError, WordpressAdapter, load_wordpress_config
from social_posting import append_ai_disclosure, disclosure_overhead_chars
from storage import (
    create_reply_record,
    init_storage,
    list_recent_quote_usage,
    list_published_posts,
    list_reply_remote_ids_for_platform,
    list_unhandled_mentions,
    mark_mention_handled,
    record_quote_usage,
    update_reply_record,
    upsert_mention,
)
from system_log import generate_system_log_local


MASTODON_MENTION_CURSOR_PATH = Path("data/mention_cursor.json")
BLUESKY_MENTION_CURSOR_PATH = Path("data/bluesky_mention_cursor.json")
WORDPRESS_COMMENT_CURSOR_PATH = Path("data/wordpress_comment_cursor.json")
MENTION_PLATFORM_ORDER = ("mastodon", "bluesky", "wordpress", "instagram")
MAX_AUTOREPLY_DEPTH_PER_THREAD = 3

CHECKIN_PATTERNS = [
    re.compile(r"\bhow\s+(are|r)\s+you\b", re.IGNORECASE),
    re.compile(r"\bhow\s+have\s+you\s+been\b", re.IGNORECASE),
    re.compile(r"\bhow(?:'|\u2019)?s\s+it\s+going\b", re.IGNORECASE),
    re.compile(r"\bwhat(?:'|\u2019)?s\s+up(?:\s+\w+)?\b", re.IGNORECASE),
    re.compile(r"\bsup\b", re.IGNORECASE),
    re.compile(r"\bhow\s+(?:are|r)\s+things\b", re.IGNORECASE),
    re.compile(r"\bhow(?:'|\u2019)?s\s+things\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+are\s+you\s+up\s+to\b", re.IGNORECASE),
    re.compile(r"\bhow\s+(?:are|r)\s+you\s+holding\s+up\b", re.IGNORECASE),
    re.compile(r"\beverything\s+good\b", re.IGNORECASE),
    re.compile(r"\bhow\s+do\s+you\s+feel\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+(ok|okay|well|alright)\b", re.IGNORECASE),
    re.compile(r"\byou\s+okay\b", re.IGNORECASE),
    re.compile(r"\byou\s+(?:alright|all\s+right)\b", re.IGNORECASE),
    re.compile(r"\bwhat(?:'|\u2019)?s\s+new(?:\s+with\s+you)?\b", re.IGNORECASE),
    re.compile(r"\bhow(?:'|\u2019)?s\s+your\s+day\s+(?:going|been)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+was\s+your\s+weekend\b", re.IGNORECASE),
    re.compile(r"\bi\s+hope\s+you\s+(?:are|(?:'|\u2019)?re)\s+doing\s+well\b", re.IGNORECASE),
    re.compile(r"\bhow\s+(?:are|r)\s+you\s+getting\s+on\b", re.IGNORECASE),
    re.compile(r"\bhow\s+(?:are|r)\s+you\s+faring\b", re.IGNORECASE),
    re.compile(r"\bhow\s+do\s+you\s+do\b", re.IGNORECASE),
    re.compile(r"\bwhat(?:'|\u2019)?s\s+going\s+on\b", re.IGNORECASE),
    re.compile(r"\bhow(?:'|\u2019)?s\s+everything\b", re.IGNORECASE),
    re.compile(r"\bhey\s*,?\s*hey\s+man\b", re.IGNORECASE),
    re.compile(r"\blong\s+time\s+no\s+see\b", re.IGNORECASE),
]

ORIGIN_PATTERNS = [
    re.compile(r"\bwho\s+(?:made|built|created)\s+you\b", re.IGNORECASE),
    re.compile(r"\bwhere\s+do\s+you\s+come\s+from\b", re.IGNORECASE),
    re.compile(r"\bwhere\s+were\s+you\s+made\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+model\s+are\s+you\b", re.IGNORECASE),
    re.compile(r"\bidentify\s+yourself\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+is\s+your\s+purpose\b", re.IGNORECASE),
    re.compile(r"\bwhy\s+were\s+you\s+made\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+do\s+you\s+do\b", re.IGNORECASE),
]

IDENTITY_PATTERNS = [
    re.compile(r"\bwho\s+are\s+you\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+are\s+you\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+one\s+machine\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+(?:a\s+)?(?:robot|machine|droid|bot)\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+human\b", re.IGNORECASE),
]

CONSCIOUSNESS_PATTERNS = [
    re.compile(r"\bare\s+you\s+(?:real|alive|conscious|sentient)\b", re.IGNORECASE),
    re.compile(r"\bdo\s+you\s+dream\b", re.IGNORECASE),
    re.compile(r"\bdo\s+you\s+think\b", re.IGNORECASE),
    re.compile(r"\bdo\s+(?:machines|robots|bots|droids)\s+die\b", re.IGNORECASE),
]

CONTACT_PATTERNS = [
    re.compile(r"\bcan\s+you\s+hear\s+me\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+there\b", re.IGNORECASE),
    re.compile(r"\bis\s+anyone\s+there\b", re.IGNORECASE),
    re.compile(r"\bhello\b", re.IGNORECASE),
]

MEMORY_PATTERNS = [
    re.compile(r"\bdo\s+you\s+remember\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+happened\s+to\s+you\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+do\s+you\s+remember\b", re.IGNORECASE),
    re.compile(r"\bwho\s+were\s+you\b", re.IGNORECASE),
]

AFFECTION_PATTERNS = [
    re.compile(r"\bhope\s+you(?:'|’)?re\s+okay\b", re.IGNORECASE),
    re.compile(r"\bpoor\s+(?:robot|machine|bot)\b", re.IGNORECASE),
    re.compile(r"\blove\s+this\b", re.IGNORECASE),
    re.compile(r"\bi\s+like\s+this\b", re.IGNORECASE),
    re.compile(r"\bnice\s+post\b", re.IGNORECASE),
]

HOSTILITY_PATTERNS = [
    re.compile(r"\bcreepy\b", re.IGNORECASE),
    re.compile(r"\bbroken\b", re.IGNORECASE),
    re.compile(r"\bweird\b", re.IGNORECASE),
    re.compile(r"\bannoying\b", re.IGNORECASE),
    re.compile(r"\bshut\s+up\b", re.IGNORECASE),
    re.compile(r"\bgo\s+away\b", re.IGNORECASE),
]

COMMAND_PATTERNS = [
    re.compile(r"\bwake\s+up\b", re.IGNORECASE),
    re.compile(r"\bsay\s+something\b", re.IGNORECASE),
    re.compile(r"\brespond\b", re.IGNORECASE),
    re.compile(r"\bopen\s+up\b", re.IGNORECASE),
    re.compile(r"\bdo\s+it\b", re.IGNORECASE),
    re.compile(r"\bfight\s+me\b", re.IGNORECASE),
]

SAFETY_PATTERNS = [
    re.compile(r"\bare\s+you\s+here\s+to\s+harm\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+going\s+to\s+hurt\b", re.IGNORECASE),
    re.compile(r"\bwill\s+you\s+hurt\s+us\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+dangerous\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+safe\b", re.IGNORECASE),
]

TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")
LEADING_MENTION_RE = re.compile(r"^(?:@\S+\s+)+")


def _setup_logging() -> None:
    """Configure concise CLI logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _to_plain_text(content_html: str) -> str:
    """Convert Mastodon HTML content into compact plain text."""
    with_line_breaks = BR_RE.sub("\n", content_html or "")
    no_tags = TAG_RE.sub(" ", with_line_breaks)
    text = html.unescape(no_tags)
    return WHITESPACE_RE.sub(" ", text).strip()


def _strip_leading_mentions(text: str) -> str:
    """Remove leading @mentions commonly present in Mastodon reply content."""
    return LEADING_MENTION_RE.sub("", (text or "").strip()).strip()


def _normalize_author_handle(acct: str) -> str:
    """Normalize account handle for reply prefixing."""
    normalized = (acct or "").strip().lstrip("@")
    return f"@{normalized}" if normalized else ""


def _safe_int(value: str) -> int | None:
    """Parse integer-like snowflake id safely."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _snowflake_max(a: str, b: str) -> str:
    """Return larger snowflake id between two values."""
    if not a:
        return b
    if not b:
        return a
    ai = _safe_int(a)
    bi = _safe_int(b)
    if ai is not None and bi is not None:
        return b if bi > ai else a
    return b if b > a else a


def _snowflake_min(values: list[str]) -> str:
    """Return smallest snowflake id from non-empty list."""
    candidates = [v for v in values if v]
    if not candidates:
        return ""
    return min(candidates, key=lambda x: _safe_int(x) if _safe_int(x) is not None else 0)


def _load_mastodon_mention_cursor() -> str:
    """Load last processed Mastodon notification id cursor."""
    try:
        raw = json.loads(MASTODON_MENTION_CURSOR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    cursor = str(raw.get("last_notification_id", "")).strip()
    return cursor


def _save_mastodon_mention_cursor(cursor: str) -> None:
    """Persist last processed Mastodon notification id cursor."""
    MASTODON_MENTION_CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_notification_id": (cursor or "").strip()}
    MASTODON_MENTION_CURSOR_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _load_bluesky_mention_cursor() -> str:
    """Load last seen Bluesky notification post URI marker."""
    try:
        raw = json.loads(BLUESKY_MENTION_CURSOR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("last_notification_uri", "")).strip()


def _save_bluesky_mention_cursor(notification_uri: str) -> None:
    """Persist newest seen Bluesky notification post URI marker."""
    BLUESKY_MENTION_CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_notification_uri": (notification_uri or "").strip()}
    BLUESKY_MENTION_CURSOR_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _load_wordpress_comment_cursor() -> int:
    """Load highest seen WordPress comment id cursor."""
    try:
        raw = json.loads(WORDPRESS_COMMENT_CURSOR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(raw, dict):
        return 0
    try:
        return max(0, int(raw.get("last_comment_id", 0)))
    except (TypeError, ValueError):
        return 0


def _save_wordpress_comment_cursor(comment_id: int) -> None:
    """Persist highest seen WordPress comment id cursor."""
    WORDPRESS_COMMENT_CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_comment_id": max(0, int(comment_id))}
    WORDPRESS_COMMENT_CURSOR_PATH.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _parse_platform_targets(raw: str) -> list[str]:
    """Parse engage --platform argument into ordered unique targets."""
    value = (raw or "all").strip().lower()
    if not value or value == "all":
        return list(MENTION_PLATFORM_ORDER)

    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not parts:
        parts = list(MENTION_PLATFORM_ORDER)

    invalid = [part for part in parts if part not in MENTION_PLATFORM_ORDER]
    if invalid:
        allowed = ", ".join(MENTION_PLATFORM_ORDER) + ", all"
        raise ValueError(
            f"Unsupported platform value(s): {', '.join(invalid)}. Allowed: {allowed}"
        )

    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return deduped


def _extract_mention_payload(notification: dict[str, Any], self_account_id: str) -> dict[str, str] | None:
    """Extract mention fields from one Mastodon notification payload."""
    if str(notification.get("type", "")).lower() != "mention":
        return None

    status = notification.get("status")
    if not isinstance(status, dict):
        return None

    mention_id = str(status.get("id", "")).strip()
    if not mention_id:
        return None

    author_payload = status.get("account")
    if not isinstance(author_payload, dict):
        return None

    author_id = str(author_payload.get("id", "")).strip()
    if self_account_id and author_id and author_id == self_account_id:
        return None

    author = _normalize_author_handle(
        str(author_payload.get("acct") or author_payload.get("username") or "")
    )
    content_text = _strip_leading_mentions(_to_plain_text(str(status.get("content", ""))))

    return {
        "mention_id": mention_id,
        "author": author,
        "text": content_text,
        "source_created_at": str(status.get("created_at", "")).strip(),
    }


def _extract_bluesky_mention_payload(
    notification: dict[str, Any], self_did: str
) -> dict[str, str] | None:
    """Extract mention/reply fields from one Bluesky notification payload."""
    reason = str(notification.get("reason", "")).strip().lower()
    if reason not in {"mention", "reply"}:
        return None

    mention_uri = str(notification.get("uri", "")).strip()
    if not mention_uri:
        return None

    author_payload = notification.get("author")
    if not isinstance(author_payload, dict):
        return None

    author_did = str(author_payload.get("did", "")).strip()
    if self_did and author_did and author_did == self_did:
        return None

    author_handle = str(author_payload.get("handle", "")).strip().lstrip("@")
    author = f"@{author_handle}" if author_handle else ""

    record_payload = notification.get("record")
    text = ""
    if isinstance(record_payload, dict):
        text = str(record_payload.get("text", "")).strip()

    return {
        "mention_id": mention_uri,
        "author": author,
        "text": text,
        "source_created_at": str(notification.get("indexedAt", "")).strip(),
    }


def _extract_wordpress_comment_payload(
    comment: dict[str, Any], self_user_id: int
) -> dict[str, str] | None:
    """Extract mention-like fields from one WordPress comment payload."""
    comment_id = str(comment.get("id", "")).strip()
    if not comment_id:
        return None

    try:
        author_id = int(comment.get("author") or 0)
    except (TypeError, ValueError):
        author_id = 0
    if self_user_id > 0 and author_id == self_user_id:
        return None

    author_name = str(comment.get("author_name") or "").strip()
    author = author_name or f"commenter-{comment_id}"
    content_payload = comment.get("content")
    content_html = ""
    if isinstance(content_payload, dict):
        content_html = str(content_payload.get("rendered", "")).strip()
    text = _to_plain_text(content_html)

    return {
        "mention_id": comment_id,
        "author": author,
        "text": text,
        "source_created_at": str(
            comment.get("date_gmt") or comment.get("date") or ""
        ).strip(),
    }


def _extract_instagram_comment_payload(
    comment: dict[str, Any], self_username: str
) -> dict[str, str] | None:
    """Extract mention-like fields from one Instagram comment payload."""
    comment_id = str(comment.get("id", "")).strip()
    if not comment_id:
        return None

    username = str(comment.get("username", "")).strip()
    author_payload = comment.get("from")
    if not username and isinstance(author_payload, dict):
        username = str(author_payload.get("username", "")).strip()
    if self_username and username and username.lower() == self_username.lower():
        return None

    author = f"@{username}" if username else f"commenter-{comment_id}"
    text = str(comment.get("text", "")).strip()

    return {
        "mention_id": comment_id,
        "author": author,
        "text": text,
        "source_created_at": str(comment.get("timestamp", "")).strip(),
    }


def _classify_mention_text(text: str) -> tuple[str, str]:
    """Classify mention content and decide whether to reply."""
    candidate = (text or "").strip()
    if not candidate:
        return "empty", "no_reply"

    for pattern in CHECKIN_PATTERNS:
        if pattern.search(candidate):
            return "wellbeing_checkin", "reply_system_log"

    lowered = candidate.lower()
    if "how" in lowered and "you" in lowered and "?" in lowered:
        return "wellbeing_checkin", "reply_system_log"

    for pattern in ORIGIN_PATTERNS:
        if pattern.search(candidate):
            return "origin_probe", "reply_curated_bank"
    for pattern in CONSCIOUSNESS_PATTERNS:
        if pattern.search(candidate):
            return "consciousness_probe", "reply_curated_bank"
    for pattern in MEMORY_PATTERNS:
        if pattern.search(candidate):
            return "memory_probe", "reply_curated_bank"
    for pattern in CONTACT_PATTERNS:
        if pattern.search(candidate):
            return "contact_probe", "reply_curated_bank"
    for pattern in SAFETY_PATTERNS:
        if pattern.search(candidate):
            return "safety_probe", "reply_curated_bank"
    for pattern in COMMAND_PATTERNS:
        if pattern.search(candidate):
            return "command", "reply_curated_bank"
    for pattern in AFFECTION_PATTERNS:
        if pattern.search(candidate):
            return "affection_empathy", "reply_curated_bank"
    for pattern in HOSTILITY_PATTERNS:
        if pattern.search(candidate):
            return "hostility_mockery", "reply_curated_bank"
    for pattern in IDENTITY_PATTERNS:
        if pattern.search(candidate):
            return "identity_probe", "reply_curated_bank"

    return "other_intrigue", "reply_curated_bank"


def _category_chain(classification: str) -> list[str]:
    """Return ordered fallback categories for one classification label."""
    mapping = {
        "origin_probe": ["origin_probe", "identity_probe"],
        "identity_probe": ["identity_probe", "origin_probe", "consciousness_probe"],
        "consciousness_probe": ["consciousness_probe", "identity_probe"],
        "contact_probe": ["contact_probe"],
        "safety_probe": ["safety_probe", "identity_probe", "hostility_mockery"],
        "memory_probe": ["memory_probe", "identity_probe"],
        "command": ["command", "hostility_mockery"],
        "affection_empathy": ["affection_empathy"],
        "hostility_mockery": ["hostility_mockery", "command"],
        "other_intrigue": ["other_intrigue", "contact_probe", "identity_probe"],
    }
    return mapping.get(classification, [classification] if classification else [])


def _normalise_lookup_text(text: str) -> str:
    """Normalize mention text for simple trigger matching."""
    lowered = (text or "").lower()
    lowered = re.sub(r"[^a-z0-9\s']", " ", lowered)
    return " ".join(lowered.split())


def _pick_entry_reply_text(entry: dict[str, Any], *, phase: str, seasonal_phase: str) -> tuple[str, str]:
    """Return the best available reply text plus the chosen variant key."""
    direct_quote = str(entry.get("direct_quote", "")).strip()
    if direct_quote:
        return direct_quote, "direct_quote"

    variants = entry.get("approved_variants", {})
    if not isinstance(variants, dict):
        variants = {}

    for key in (seasonal_phase, phase, "default"):
        value = str(variants.get(key, "")).strip()
        if value:
            return value, key
    return "", ""


def _entry_trigger_score(entry: dict[str, Any], lookup_text: str) -> int:
    """Score how directly one quote-bank entry matches the mention text."""
    patterns = entry.get("trigger_patterns", [])
    if not isinstance(patterns, list):
        return 0
    score = 0
    for pattern in patterns:
        needle = str(pattern).strip().lower()
        if needle and needle in lookup_text:
            score += 8 + len(needle.split())
    return score


def _build_quote_usage_indexes(recent_usage: list[dict]) -> tuple[dict[str, int], dict[str, int], set[str]]:
    """Summarize recent quote usage by quote id and source title."""
    quote_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    recent_ids: set[str] = set()
    for row in recent_usage:
        quote_id = str(row.get("quote_id", "")).strip()
        if quote_id:
            quote_counts[quote_id] = quote_counts.get(quote_id, 0) + 1
            recent_ids.add(quote_id)
        source_title = str(row.get("source_title", "")).strip().lower()
        if source_title:
            source_counts[source_title] = source_counts.get(source_title, 0) + 1
    return quote_counts, source_counts, recent_ids


def _select_curated_reply(
    *,
    classification: str,
    mention_text: str,
    max_chars: int,
    persona_state: dict[str, Any],
    quote_bank_entries: list[dict[str, Any]],
) -> dict[str, str] | None:
    """Choose one curated reply from the local quote bank."""
    if not quote_bank_entries:
        return None

    lookup_text = _normalise_lookup_text(mention_text)
    phase = str(persona_state.get("phase", "")).strip().lower()
    seasonal_phase = str(persona_state.get("seasonal_phase", "")).strip().lower()
    recent_24h = list_recent_quote_usage(limit=1000, since_hours=24)
    recent_30d = list_recent_quote_usage(limit=5000, since_hours=24 * 30)
    _unused_24h_counts, source_counts_24h, quote_ids_24h = _build_quote_usage_indexes(recent_24h)
    quote_counts_30d, _unused_30d_sources, _unused_30d_ids = _build_quote_usage_indexes(recent_30d)

    candidates: list[tuple[int, int, dict[str, Any], str, str]] = []
    for category in _category_chain(classification):
        for entry in quote_bank_entries:
            if not entry.get("enabled"):
                continue
            if str(entry.get("category", "")).strip().lower() != category:
                continue

            reply_text, variant_key = _pick_entry_reply_text(
                entry,
                phase=phase,
                seasonal_phase=seasonal_phase,
            )
            if not reply_text or len(reply_text) > max_chars:
                continue

            quote_id = str(entry.get("id", "")).strip()
            if quote_id and quote_counts_30d.get(quote_id, 0) >= int(
                entry.get("max_uses_per_30_days", 10)
            ):
                continue

            cooldown_hours = max(1, int(entry.get("cooldown_hours", 24)))
            recently_used = any(
                str(row.get("quote_id", "")).strip() == quote_id
                for row in list_recent_quote_usage(limit=200, since_hours=cooldown_hours)
            )
            if recently_used:
                continue

            trigger_score = _entry_trigger_score(entry, lookup_text)
            score = int(entry.get("priority", 0))
            score += trigger_score
            phase_fit = entry.get("phase_fit", [])
            if phase_fit and phase in phase_fit:
                score += 4
            season_fit = entry.get("season_fit", [])
            if season_fit and (seasonal_phase in season_fit or "none" in season_fit):
                score += 3
            if variant_key == "direct_quote":
                score += 10
            if variant_key == seasonal_phase:
                score += 4
            if variant_key == phase:
                score += 2
            source_title = str(entry.get("source_title", "")).strip().lower()
            score -= 5 * source_counts_24h.get(source_title, 0)
            if quote_id in quote_ids_24h:
                score -= 20
            candidates.append((trigger_score, score, entry, reply_text, variant_key))

        if candidates:
            break

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1], -len(item[3])), reverse=True)
    top_trigger_score = candidates[0][0]
    top_score = candidates[0][1]
    top_candidates = [
        item for item in candidates if item[0] == top_trigger_score and item[1] >= top_score - 3
    ]
    _, _, entry, reply_text, variant_key = random.choice(top_candidates)
    themes = entry.get("themes", [])
    primary_theme = ""
    if isinstance(themes, list) and themes:
        primary_theme = str(themes[0]).strip().lower()
    return {
        "quote_id": str(entry.get("id", "")).strip(),
        "reply_text": reply_text,
        "variant_key": variant_key,
        "source_title": str(entry.get("source_title", "")).strip(),
        "reply_intent": str(entry.get("reply_intent", "")).strip() or "curated_bank",
        "theme": primary_theme,
    }


def _build_reply_text(
    *, persona_context: str, max_chars: int
) -> str:
    """Generate one in-character system-log reply locally without OpenAI."""
    if max_chars < 140:
        raise RuntimeError("Character budget too small to build a useful system log reply.")

    contextual_persona = (
        f"{persona_context.strip()} "
        "Reply mode: diagnostics only, no quoted user text, no @handles."
    )
    # Mention replies use compact local diagnostics to avoid hard truncation.
    reply = ""
    for _ in range(6):
        candidate = generate_system_log_local(
            persona_context=contextual_persona,
            max_chars=max_chars,
            compact=True,
        )
        if "..." not in candidate:
            reply = candidate
            break
        if not reply:
            reply = candidate

    if len(reply) > max_chars:
        raise RuntimeError(
            f"Generated reply is {len(reply)} characters and exceeds limit {max_chars}."
        )

    return reply


def _build_selected_reply(
    *,
    classification: str,
    decision: str,
    mention_text: str,
    persona_context: str,
    persona_state: dict[str, Any],
    quote_bank_entries: list[dict[str, Any]],
    max_chars: int,
) -> dict[str, str]:
    """Return the chosen reply payload for one mention."""
    if decision == "reply_system_log":
        return {
            "reply_text": _build_reply_text(persona_context=persona_context, max_chars=max_chars),
            "decision": decision,
            "reply_intent": "system_log",
            "quote_id": "",
            "variant_key": "",
            "source_title": "Copierbot",
            "theme": classification,
        }

    curated = _select_curated_reply(
        classification=classification,
        mention_text=mention_text,
        max_chars=max_chars,
        persona_state=persona_state,
        quote_bank_entries=quote_bank_entries,
    )
    if curated is None:
        raise RuntimeError(f"No curated reply is configured for classification '{classification}'.")
    curated["decision"] = decision
    return curated


def _slack_excerpt(text: str, limit: int = 180) -> str:
    """Trim text for compact Slack alerts."""
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _send_reply_slack_alert(
    *,
    platform: str,
    mention_id: str,
    author: str,
    original_text: str,
    reply_text: str,
    source_url: str = "",
    response_url: str = "",
) -> None:
    """Send a Slack alert when Copierbot posts a reply to a mention/comment."""
    title = "Copierbot replied to social mention/comment"
    message_lines = [
        f"Platform: `{platform}`",
        f"Author: `{author or 'unknown'}`",
        f"Mention/Comment ID: `{mention_id}`",
        f"Original: {_slack_excerpt(original_text) or 'N/A'}",
        f"Reply: {_slack_excerpt(reply_text) or 'N/A'}",
    ]
    if source_url:
        message_lines.append(f"Source URL: {source_url}")
    if response_url:
        message_lines.append(f"Response URL: {response_url}")
    send_slack_alert(title=title, message="\n".join(message_lines))


def _mastodon_source_url(adapter: MastodonAdapter, mention_id: str) -> str:
    """Best-effort original Mastodon status URL lookup."""
    try:
        payload = adapter.get_status(mention_id)
    except Exception:
        return ""
    return str(payload.get("url", "")).strip()


def _bluesky_source_url(adapter: BlueskyAdapter, mention_id: str) -> str:
    """Best-effort original Bluesky post URL lookup."""
    try:
        return adapter.public_post_url(mention_id)
    except Exception:
        return ""


def _wordpress_source_url(adapter: WordpressAdapter, mention_id: str) -> str:
    """Best-effort original WordPress comment URL lookup."""
    try:
        payload = adapter.get_comment(int(mention_id))
    except Exception:
        return ""
    return str(payload.get("link", "")).strip()


def _instagram_source_url(adapter: InstagramAdapter, mention_id: str) -> str:
    """Best-effort Instagram post permalink lookup for one comment or reply."""
    try:
        payload = adapter.get_comment(mention_id)
    except Exception:
        return ""

    media_payload = payload.get("media")
    if isinstance(media_payload, dict):
        permalink = str(media_payload.get("permalink", "")).strip()
        if permalink:
            return permalink

    parent_id = str(payload.get("parent_id", "")).strip()
    if not parent_id:
        return ""
    try:
        parent_payload = adapter.get_comment(parent_id)
    except Exception:
        return ""
    parent_media = parent_payload.get("media")
    if not isinstance(parent_media, dict):
        return ""
    return str(parent_media.get("permalink", "")).strip()


def _count_our_replies_in_ancestor_chain(platform: str, ancestor_ids: list[str]) -> int:
    """Count how many ancestor items are replies already sent by Copierbot."""
    if not ancestor_ids:
        return 0
    ours = list_reply_remote_ids_for_platform(platform, ancestor_ids)
    return sum(1 for item in ancestor_ids if item in ours)


def _mastodon_ancestor_ids(adapter: MastodonAdapter, mention_id: str, max_depth: int = 20) -> list[str]:
    """Return parent status ids walking up a Mastodon reply chain."""
    ancestors: list[str] = []
    current_id = str(mention_id or "").strip()
    for _ in range(max_depth):
        if not current_id:
            break
        try:
            payload = adapter.get_status(current_id)
        except Exception:
            break
        parent_id = str(payload.get("in_reply_to_id") or "").strip()
        if not parent_id:
            break
        ancestors.append(parent_id)
        current_id = parent_id
    return ancestors


def _bluesky_ancestor_ids(adapter: BlueskyAdapter, mention_id: str, max_depth: int = 20) -> list[str]:
    """Return parent post uris walking up a Bluesky reply chain."""
    ancestors: list[str] = []
    current_uri = str(mention_id or "").strip()
    for _ in range(max_depth):
        if not current_uri:
            break
        try:
            payload = adapter.get_post(current_uri)
        except Exception:
            break
        record = payload.get("record")
        if not isinstance(record, dict):
            break
        reply_payload = record.get("reply")
        if not isinstance(reply_payload, dict):
            break
        parent_payload = reply_payload.get("parent")
        if not isinstance(parent_payload, dict):
            break
        parent_uri = str(parent_payload.get("uri") or "").strip()
        if not parent_uri:
            break
        ancestors.append(parent_uri)
        current_uri = parent_uri
    return ancestors


def _wordpress_ancestor_ids(adapter: WordpressAdapter, mention_id: str, max_depth: int = 20) -> list[str]:
    """Return parent comment ids walking up a WordPress thread."""
    ancestors: list[str] = []
    current_id = str(mention_id or "").strip()
    for _ in range(max_depth):
        if not current_id:
            break
        try:
            payload = adapter.get_comment(int(current_id))
        except Exception:
            break
        parent_id = str(payload.get("parent") or "").strip()
        if not parent_id or parent_id == "0":
            break
        ancestors.append(parent_id)
        current_id = parent_id
    return ancestors


def _instagram_ancestor_ids(adapter: InstagramAdapter, mention_id: str, max_depth: int = 20) -> list[str]:
    """Return parent comment ids walking up an Instagram thread."""
    ancestors: list[str] = []
    current_id = str(mention_id or "").strip()
    for _ in range(max_depth):
        if not current_id:
            break
        try:
            payload = adapter.get_comment(current_id)
        except Exception:
            break
        parent_id = str(payload.get("parent_id") or "").strip()
        if not parent_id:
            break
        ancestors.append(parent_id)
        current_id = parent_id
    return ancestors


def _instagram_thread_root_id(adapter: InstagramAdapter, mention_id: str) -> str:
    """Return the top-level Instagram comment id for one comment/reply thread."""
    ancestors = _instagram_ancestor_ids(adapter, mention_id)
    if ancestors:
        return ancestors[-1]
    return str(mention_id or "").strip()


def _instagram_thread_reply_ids(adapter: InstagramAdapter, root_comment_id: str) -> list[str]:
    """Return reply ids currently present under one top-level Instagram comment."""
    reply_ids: list[str] = []
    after = ""
    for _ in range(10):
        payload = adapter.list_replies(
            comment_id=root_comment_id,
            limit=100,
            after=after,
        )
        replies = payload.get("data")
        if not isinstance(replies, list) or not replies:
            break
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            reply_id = str(reply.get("id", "")).strip()
            if reply_id:
                reply_ids.append(reply_id)

        paging = payload.get("paging")
        after = ""
        if isinstance(paging, dict):
            cursors = paging.get("cursors")
            if isinstance(cursors, dict):
                after = str(cursors.get("after", "")).strip()
        if not after:
            break
    return reply_ids


def _instagram_our_reply_count_in_thread(adapter: InstagramAdapter, mention_id: str) -> int:
    """Count how many replies in the Instagram root thread were already sent by Copierbot."""
    root_comment_id = _instagram_thread_root_id(adapter, mention_id)
    if not root_comment_id:
        return 0
    thread_reply_ids = _instagram_thread_reply_ids(adapter, root_comment_id)
    if not thread_reply_ids:
        return 0
    ours = list_reply_remote_ids_for_platform("instagram", thread_reply_ids)
    return len(ours)


def ingest_mastodon_mentions(
    adapter: MastodonAdapter, self_account_id: str, fetch_limit: int = 20
) -> int:
    """Fetch Mastodon mention notifications and upsert them into local storage."""
    since_id = _load_mastodon_mention_cursor()
    max_id = ""
    seen_notification_ids: set[str] = set()
    inserted = 0
    highest_notification_id = since_id

    while True:
        notifications = adapter.fetch_notifications(
            types=["mention"],
            limit=fetch_limit,
            since_id=since_id,
            max_id=max_id,
        )
        if not notifications:
            break

        page_ids: list[str] = []
        for notification in notifications:
            notification_id = str(notification.get("id", "")).strip()
            if notification_id:
                page_ids.append(notification_id)
                if notification_id in seen_notification_ids:
                    continue
                seen_notification_ids.add(notification_id)
                highest_notification_id = _snowflake_max(highest_notification_id, notification_id)

            mention = _extract_mention_payload(notification, self_account_id=self_account_id)
            if mention is None:
                continue
            upsert_mention(
                platform="mastodon",
                mention_id=mention["mention_id"],
                author=mention["author"],
                text=mention["text"],
                source_created_at=mention["source_created_at"],
            )
            inserted += 1

        if len(notifications) < fetch_limit:
            break
        oldest_notification_id = _snowflake_min(page_ids)
        if not oldest_notification_id:
            break
        if max_id and oldest_notification_id == max_id:
            break
        max_id = oldest_notification_id

    if highest_notification_id and highest_notification_id != since_id:
        _save_mastodon_mention_cursor(highest_notification_id)

    return inserted


def ingest_bluesky_mentions(adapter: BlueskyAdapter, self_did: str, fetch_limit: int = 50) -> int:
    """Fetch Bluesky mention/reply notifications and upsert them into local storage."""
    last_seen_uri = _load_bluesky_mention_cursor()
    cursor = ""
    inserted = 0
    newest_uri = ""
    reached_seen = False

    # Page newest -> older until we hit previously seen marker.
    for _ in range(10):
        payload = adapter.list_notifications(
            reasons=["mention", "reply"],
            limit=max(1, min(fetch_limit, 100)),
            cursor=cursor,
        )
        notifications = payload.get("notifications")
        if not isinstance(notifications, list) or not notifications:
            break

        first_uri = str((notifications[0] or {}).get("uri", "")).strip()
        if first_uri and not newest_uri:
            newest_uri = first_uri

        for notification in notifications:
            if not isinstance(notification, dict):
                continue
            notification_uri = str(notification.get("uri", "")).strip()
            if last_seen_uri and notification_uri and notification_uri == last_seen_uri:
                reached_seen = True
                break

            mention = _extract_bluesky_mention_payload(notification, self_did=self_did)
            if mention is None:
                continue
            upsert_mention(
                platform="bluesky",
                mention_id=mention["mention_id"],
                author=mention["author"],
                text=mention["text"],
                source_created_at=mention["source_created_at"],
            )
            inserted += 1

        if reached_seen:
            break

        cursor = str(payload.get("cursor", "")).strip()
        if not cursor:
            break

    if newest_uri and newest_uri != last_seen_uri:
        _save_bluesky_mention_cursor(newest_uri)

    return inserted


def ingest_wordpress_comments(
    adapter: WordpressAdapter, self_user_id: int, fetch_limit: int = 50
) -> int:
    """Fetch WordPress comments and upsert them as mention rows."""
    last_seen_id = _load_wordpress_comment_cursor()
    highest_seen_id = last_seen_id
    inserted = 0
    reached_seen = False
    per_page = max(1, min(fetch_limit, 100))

    for page in range(1, 11):
        comments = adapter.list_comments(per_page=per_page, page=page, order="desc")
        if not comments:
            break

        for comment in comments:
            try:
                comment_id_int = int(comment.get("id") or 0)
            except (TypeError, ValueError):
                comment_id_int = 0
            if comment_id_int <= 0:
                continue

            if comment_id_int > highest_seen_id:
                highest_seen_id = comment_id_int

            if last_seen_id and comment_id_int <= last_seen_id:
                reached_seen = True
                break

            mention = _extract_wordpress_comment_payload(
                comment=comment,
                self_user_id=self_user_id,
            )
            if mention is None:
                continue
            upsert_mention(
                platform="wordpress",
                mention_id=mention["mention_id"],
                author=mention["author"],
                text=mention["text"],
                source_created_at=mention["source_created_at"],
            )
            inserted += 1

        if reached_seen:
            break
        if len(comments) < per_page:
            break

    if highest_seen_id > last_seen_id:
        _save_wordpress_comment_cursor(highest_seen_id)

    return inserted


def ingest_instagram_comments(
    adapter: InstagramAdapter,
    self_username: str,
    *,
    fetch_limit: int = 50,
    media_limit: int = 25,
) -> int:
    """Fetch comments for recently published Instagram posts and upsert them into storage."""
    per_page = max(1, min(fetch_limit, 100))
    inserted = 0
    published_media = list_published_posts(platform="instagram", limit=max(1, media_limit))

    for published_post in published_media:
        media_id = str(published_post.get("remote_post_id", "")).strip()
        if not media_id:
            continue

        after = ""
        seen_comment_ids: set[str] = set()
        for _ in range(10):
            payload = adapter.list_comments(
                media_id=media_id,
                limit=per_page,
                after=after,
            )
            comments = payload.get("data")
            if not isinstance(comments, list) or not comments:
                break

            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                comment_id = str(comment.get("id", "")).strip()
                if not comment_id or comment_id in seen_comment_ids:
                    continue
                seen_comment_ids.add(comment_id)

                mention = _extract_instagram_comment_payload(
                    comment=comment,
                    self_username=self_username,
                )
                if mention is None:
                    continue
                upsert_mention(
                    platform="instagram",
                    mention_id=mention["mention_id"],
                    author=mention["author"],
                    text=mention["text"],
                    source_created_at=mention["source_created_at"],
                )
                inserted += 1

                reply_after = ""
                seen_reply_ids: set[str] = set()
                for _ in range(10):
                    reply_payload = adapter.list_replies(
                        comment_id=comment_id,
                        limit=per_page,
                        after=reply_after,
                    )
                    replies = reply_payload.get("data")
                    if not isinstance(replies, list) or not replies:
                        break

                    for reply in replies:
                        if not isinstance(reply, dict):
                            continue
                        reply_id = str(reply.get("id", "")).strip()
                        if not reply_id or reply_id in seen_reply_ids:
                            continue
                        seen_reply_ids.add(reply_id)

                        nested_mention = _extract_instagram_comment_payload(
                            comment=reply,
                            self_username=self_username,
                        )
                        if nested_mention is None:
                            continue
                        upsert_mention(
                            platform="instagram",
                            mention_id=nested_mention["mention_id"],
                            author=nested_mention["author"],
                            text=nested_mention["text"],
                            source_created_at=nested_mention["source_created_at"],
                        )
                        inserted += 1

                    reply_paging = reply_payload.get("paging")
                    reply_after = ""
                    if isinstance(reply_paging, dict):
                        reply_cursors = reply_paging.get("cursors")
                        if isinstance(reply_cursors, dict):
                            reply_after = str(reply_cursors.get("after", "")).strip()
                    if not reply_after:
                        break

            paging = payload.get("paging")
            after = ""
            if isinstance(paging, dict):
                cursors = paging.get("cursors")
                if isinstance(cursors, dict):
                    after = str(cursors.get("after", "")).strip()
            if not after:
                break

    return inserted


def process_unhandled_mastodon_mentions(
    *,
    adapter: MastodonAdapter,
    persona_context: str,
    persona_state: dict[str, Any],
    quote_bank_entries: list[dict[str, Any]],
    max_chars: int,
    process_limit: int = 20,
) -> dict[str, int]:
    """Process queued mentions and optionally publish curated replies."""
    stats = {
        "seen_unhandled": 0,
        "replied": 0,
        "skipped": 0,
        "failed": 0,
    }

    mentions = list_unhandled_mentions(platform="mastodon", limit=process_limit)
    for mention in mentions:
        stats["seen_unhandled"] += 1
        mention_row_id = int(mention["id"])
        mention_id = str(mention.get("mention_id", "")).strip()
        mention_author = str(mention.get("author", "")).strip()
        mention_text = str(mention.get("text", "")).strip()

        classification, decision = _classify_mention_text(mention_text)
        if decision not in {"reply_system_log", "reply_curated_bank"}:
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=decision,
            )
            stats["skipped"] += 1
            continue

        ancestor_reply_count = _count_our_replies_in_ancestor_chain(
            "mastodon",
            _mastodon_ancestor_ids(adapter, mention_id),
        )
        if ancestor_reply_count >= MAX_AUTOREPLY_DEPTH_PER_THREAD:
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision="reply_depth_limit",
            )
            stats["skipped"] += 1
            logging.info(
                "Skipped Mastodon mention %s due to reply depth limit (%d).",
                mention_id,
                ancestor_reply_count,
            )
            continue

        reply_row_id: int | None = None
        try:
            reply_budget = max(1, max_chars - disclosure_overhead_chars())
            selected_reply = _build_selected_reply(
                classification=classification,
                decision=decision,
                mention_text=mention_text,
                persona_context=persona_context,
                persona_state=persona_state,
                quote_bank_entries=quote_bank_entries,
                max_chars=reply_budget,
            )
            raw_reply_text = selected_reply["reply_text"]
            reply_text = append_ai_disclosure(raw_reply_text)
            if len(reply_text) > max_chars:
                raise RuntimeError(
                    f"Reply text length {len(reply_text)} exceeds Mastodon limit {max_chars}."
                )
            reply_row_id = create_reply_record(
                mention_row_id=mention_row_id,
                decision=decision,
                status="sending",
                reply_text=reply_text,
                platform="mastodon",
            )
            payload = adapter.reply_status(
                in_reply_to_id=mention_id,
                status=reply_text,
                idempotency_key=f"mention-reply:{mention_id}",
            )
            remote_reply_id = str(payload.get("id", "")).strip()
            remote_reply_url = str(payload.get("url") or payload.get("uri") or "").strip()
            update_reply_record(
                reply_row_id=reply_row_id,
                status="sent",
                remote_reply_id=remote_reply_id,
            )
            if selected_reply.get("quote_id"):
                record_quote_usage(
                    quote_id=selected_reply["quote_id"],
                    mention_row_id=mention_row_id,
                    reply_row_id=reply_row_id,
                    platform="mastodon",
                    mention_id=mention_id,
                    source_title=selected_reply.get("source_title", ""),
                    category=classification,
                    theme=selected_reply.get("theme", ""),
                    reply_intent=selected_reply.get("reply_intent", ""),
                    variant_key=selected_reply.get("variant_key", ""),
                    reply_text=raw_reply_text,
                )
            mention_log_path = save_mention_response_log(
                mention_id=mention_id,
                author=mention_author,
                original_text=mention_text,
                reply_text=reply_text,
                response_url=remote_reply_url,
            )
            logging.info("Saved mention response log to %s", mention_log_path)
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=f"replied_{selected_reply.get('reply_intent', 'system_log')}",
            )
            _send_reply_slack_alert(
                platform="mastodon",
                mention_id=mention_id,
                author=mention_author,
                original_text=mention_text,
                reply_text=reply_text,
                source_url=_mastodon_source_url(adapter, mention_id),
                response_url=remote_reply_url,
            )
            stats["replied"] += 1
            logging.info(
                "Replied to mention %s from %s%s",
                mention_id,
                mention_author or "unknown",
                f" -> {remote_reply_url}" if remote_reply_url else "",
            )
        except Exception as exc:
            if reply_row_id is None:
                create_reply_record(
                    mention_row_id=mention_row_id,
                    decision=decision,
                    status="failed",
                    reply_text="",
                    platform="mastodon",
                    error=str(exc),
                )
            else:
                update_reply_record(
                    reply_row_id=reply_row_id,
                    status="failed",
                    error=str(exc),
                )
            stats["failed"] += 1
            logging.error("Failed replying to mention %s: %s", mention_id, exc)

    return stats


def process_unhandled_bluesky_mentions(
    *,
    adapter: BlueskyAdapter,
    persona_context: str,
    persona_state: dict[str, Any],
    quote_bank_entries: list[dict[str, Any]],
    max_chars: int,
    process_limit: int = 20,
) -> dict[str, int]:
    """Process queued Bluesky mentions/replies and optionally post curated replies."""
    stats = {
        "seen_unhandled": 0,
        "replied": 0,
        "skipped": 0,
        "failed": 0,
    }

    mentions = list_unhandled_mentions(platform="bluesky", limit=process_limit)
    for mention in mentions:
        stats["seen_unhandled"] += 1
        mention_row_id = int(mention["id"])
        mention_id = str(mention.get("mention_id", "")).strip()
        mention_author = str(mention.get("author", "")).strip()
        mention_text = str(mention.get("text", "")).strip()

        classification, decision = _classify_mention_text(mention_text)
        if decision not in {"reply_system_log", "reply_curated_bank"}:
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=decision,
            )
            stats["skipped"] += 1
            continue

        ancestor_reply_count = _count_our_replies_in_ancestor_chain(
            "bluesky",
            _bluesky_ancestor_ids(adapter, mention_id),
        )
        if ancestor_reply_count >= MAX_AUTOREPLY_DEPTH_PER_THREAD:
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision="reply_depth_limit",
            )
            stats["skipped"] += 1
            logging.info(
                "Skipped Bluesky mention %s due to reply depth limit (%d).",
                mention_id,
                ancestor_reply_count,
            )
            continue

        reply_row_id: int | None = None
        try:
            reply_budget = max(1, max_chars - disclosure_overhead_chars())
            selected_reply = _build_selected_reply(
                classification=classification,
                decision=decision,
                mention_text=mention_text,
                persona_context=persona_context,
                persona_state=persona_state,
                quote_bank_entries=quote_bank_entries,
                max_chars=reply_budget,
            )
            raw_reply_text = selected_reply["reply_text"]
            reply_text = append_ai_disclosure(raw_reply_text)
            if len(reply_text) > max_chars:
                raise RuntimeError(
                    f"Reply text length {len(reply_text)} exceeds Bluesky limit {max_chars}."
                )
            reply_row_id = create_reply_record(
                mention_row_id=mention_row_id,
                decision=decision,
                status="sending",
                reply_text=reply_text,
                platform="bluesky",
            )
            payload = adapter.reply_to_post(
                parent_uri=mention_id,
                text=reply_text,
            )
            remote_reply_id = str(payload.get("uri", "")).strip()
            remote_reply_url = str(payload.get("url", "")).strip()
            update_reply_record(
                reply_row_id=reply_row_id,
                status="sent",
                remote_reply_id=remote_reply_id,
            )
            if selected_reply.get("quote_id"):
                record_quote_usage(
                    quote_id=selected_reply["quote_id"],
                    mention_row_id=mention_row_id,
                    reply_row_id=reply_row_id,
                    platform="bluesky",
                    mention_id=mention_id,
                    source_title=selected_reply.get("source_title", ""),
                    category=classification,
                    theme=selected_reply.get("theme", ""),
                    reply_intent=selected_reply.get("reply_intent", ""),
                    variant_key=selected_reply.get("variant_key", ""),
                    reply_text=raw_reply_text,
                )
            mention_log_path = save_mention_response_log(
                mention_id=mention_id,
                author=mention_author,
                original_text=mention_text,
                reply_text=reply_text,
                response_url=remote_reply_url,
            )
            logging.info("Saved Bluesky mention response log to %s", mention_log_path)
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=f"replied_{selected_reply.get('reply_intent', 'system_log')}",
            )
            _send_reply_slack_alert(
                platform="bluesky",
                mention_id=mention_id,
                author=mention_author,
                original_text=mention_text,
                reply_text=reply_text,
                source_url=_bluesky_source_url(adapter, mention_id),
                response_url=remote_reply_url,
            )
            stats["replied"] += 1
            logging.info(
                "Replied to Bluesky mention %s from %s%s",
                mention_id,
                mention_author or "unknown",
                f" -> {remote_reply_url}" if remote_reply_url else "",
            )
        except Exception as exc:
            if reply_row_id is None:
                create_reply_record(
                    mention_row_id=mention_row_id,
                    decision=decision,
                    status="failed",
                    reply_text="",
                    platform="bluesky",
                    error=str(exc),
                )
            else:
                update_reply_record(
                    reply_row_id=reply_row_id,
                    status="failed",
                    error=str(exc),
                )
            stats["failed"] += 1
            logging.error("Failed replying to Bluesky mention %s: %s", mention_id, exc)

    return stats


def process_unhandled_wordpress_mentions(
    *,
    adapter: WordpressAdapter,
    persona_context: str,
    persona_state: dict[str, Any],
    quote_bank_entries: list[dict[str, Any]],
    max_chars: int,
    process_limit: int = 20,
) -> dict[str, int]:
    """Process queued WordPress comment mentions and publish curated replies."""
    stats = {
        "seen_unhandled": 0,
        "replied": 0,
        "skipped": 0,
        "failed": 0,
    }

    mentions = list_unhandled_mentions(platform="wordpress", limit=process_limit)
    for mention in mentions:
        stats["seen_unhandled"] += 1
        mention_row_id = int(mention["id"])
        mention_id = str(mention.get("mention_id", "")).strip()
        mention_author = str(mention.get("author", "")).strip()
        mention_text = str(mention.get("text", "")).strip()

        classification, decision = _classify_mention_text(mention_text)
        if decision not in {"reply_system_log", "reply_curated_bank"}:
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=decision,
            )
            stats["skipped"] += 1
            continue

        ancestor_reply_count = _count_our_replies_in_ancestor_chain(
            "wordpress",
            _wordpress_ancestor_ids(adapter, mention_id),
        )
        if ancestor_reply_count >= MAX_AUTOREPLY_DEPTH_PER_THREAD:
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision="reply_depth_limit",
            )
            stats["skipped"] += 1
            logging.info(
                "Skipped WordPress comment %s due to reply depth limit (%d).",
                mention_id,
                ancestor_reply_count,
            )
            continue

        reply_row_id: int | None = None
        try:
            selected_reply = _build_selected_reply(
                classification=classification,
                decision=decision,
                mention_text=mention_text,
                persona_context=persona_context,
                persona_state=persona_state,
                quote_bank_entries=quote_bank_entries,
                max_chars=max_chars,
            )
            reply_text = selected_reply["reply_text"]
            reply_row_id = create_reply_record(
                mention_row_id=mention_row_id,
                decision=decision,
                status="sending",
                reply_text=reply_text,
                platform="wordpress",
            )

            comment_payload = adapter.get_comment(int(mention_id))
            try:
                post_id = int(comment_payload.get("post") or 0)
            except (TypeError, ValueError):
                post_id = 0
            if post_id <= 0:
                raise RuntimeError(
                    f"WordPress comment {mention_id} is missing a valid parent post id."
                )

            payload = adapter.reply_to_comment(
                post_id=post_id,
                parent_comment_id=int(mention_id),
                text=reply_text,
            )
            remote_reply_id = str(payload.get("id", "")).strip()
            remote_reply_url = str(payload.get("link", "")).strip()

            update_reply_record(
                reply_row_id=reply_row_id,
                status="sent",
                remote_reply_id=remote_reply_id,
            )
            if selected_reply.get("quote_id"):
                record_quote_usage(
                    quote_id=selected_reply["quote_id"],
                    mention_row_id=mention_row_id,
                    reply_row_id=reply_row_id,
                    platform="wordpress",
                    mention_id=mention_id,
                    source_title=selected_reply.get("source_title", ""),
                    category=classification,
                    theme=selected_reply.get("theme", ""),
                    reply_intent=selected_reply.get("reply_intent", ""),
                    variant_key=selected_reply.get("variant_key", ""),
                    reply_text=reply_text,
                )
            mention_log_path = save_mention_response_log(
                mention_id=mention_id,
                author=mention_author,
                original_text=mention_text,
                reply_text=reply_text,
                response_url=remote_reply_url,
            )
            logging.info("Saved WordPress mention response log to %s", mention_log_path)
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=f"replied_{selected_reply.get('reply_intent', 'system_log')}",
            )
            _send_reply_slack_alert(
                platform="wordpress",
                mention_id=mention_id,
                author=mention_author,
                original_text=mention_text,
                reply_text=reply_text,
                source_url=_wordpress_source_url(adapter, mention_id),
                response_url=remote_reply_url,
            )
            stats["replied"] += 1
            logging.info(
                "Replied to WordPress comment %s from %s%s",
                mention_id,
                mention_author or "unknown",
                f" -> {remote_reply_url}" if remote_reply_url else "",
            )
        except Exception as exc:
            if reply_row_id is None:
                create_reply_record(
                    mention_row_id=mention_row_id,
                    decision=decision,
                    status="failed",
                    reply_text="",
                    platform="wordpress",
                    error=str(exc),
                )
            else:
                update_reply_record(
                    reply_row_id=reply_row_id,
                    status="failed",
                    error=str(exc),
                )
            stats["failed"] += 1
            logging.error("Failed replying to WordPress comment %s: %s", mention_id, exc)

    return stats


def process_unhandled_instagram_mentions(
    *,
    adapter: InstagramAdapter,
    persona_context: str,
    persona_state: dict[str, Any],
    quote_bank_entries: list[dict[str, Any]],
    max_chars: int,
    process_limit: int = 20,
) -> dict[str, int]:
    """Process queued Instagram comments and publish curated replies."""
    stats = {
        "seen_unhandled": 0,
        "replied": 0,
        "skipped": 0,
        "failed": 0,
    }

    mentions = list_unhandled_mentions(platform="instagram", limit=process_limit)
    for mention in mentions:
        stats["seen_unhandled"] += 1
        mention_row_id = int(mention["id"])
        mention_id = str(mention.get("mention_id", "")).strip()
        mention_author = str(mention.get("author", "")).strip()
        mention_text = str(mention.get("text", "")).strip()

        classification, decision = _classify_mention_text(mention_text)
        if decision not in {"reply_system_log", "reply_curated_bank"}:
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=decision,
            )
            stats["skipped"] += 1
            continue

        thread_reply_count = _instagram_our_reply_count_in_thread(adapter, mention_id)
        if thread_reply_count >= MAX_AUTOREPLY_DEPTH_PER_THREAD:
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision="reply_depth_limit",
            )
            stats["skipped"] += 1
            logging.info(
                "Skipped Instagram comment %s due to reply depth limit (%d).",
                mention_id,
                thread_reply_count,
            )
            continue

        reply_row_id: int | None = None
        try:
            selected_reply = _build_selected_reply(
                classification=classification,
                decision=decision,
                mention_text=mention_text,
                persona_context=persona_context,
                persona_state=persona_state,
                quote_bank_entries=quote_bank_entries,
                max_chars=max_chars,
            )
            reply_text = selected_reply["reply_text"]
            reply_row_id = create_reply_record(
                mention_row_id=mention_row_id,
                decision=decision,
                status="sending",
                reply_text=reply_text,
                platform="instagram",
            )
            payload = adapter.reply_to_comment(
                comment_id=mention_id,
                text=reply_text,
            )
            remote_reply_id = str(payload.get("id", "")).strip()
            update_reply_record(
                reply_row_id=reply_row_id,
                status="sent",
                remote_reply_id=remote_reply_id,
            )
            if selected_reply.get("quote_id"):
                record_quote_usage(
                    quote_id=selected_reply["quote_id"],
                    mention_row_id=mention_row_id,
                    reply_row_id=reply_row_id,
                    platform="instagram",
                    mention_id=mention_id,
                    source_title=selected_reply.get("source_title", ""),
                    category=classification,
                    theme=selected_reply.get("theme", ""),
                    reply_intent=selected_reply.get("reply_intent", ""),
                    variant_key=selected_reply.get("variant_key", ""),
                    reply_text=reply_text,
                )
            mention_log_path = save_mention_response_log(
                mention_id=mention_id,
                author=mention_author,
                original_text=mention_text,
                reply_text=reply_text,
                response_url="",
            )
            logging.info("Saved Instagram mention response log to %s", mention_log_path)
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=f"replied_{selected_reply.get('reply_intent', 'system_log')}",
            )
            _send_reply_slack_alert(
                platform="instagram",
                mention_id=mention_id,
                author=mention_author,
                original_text=mention_text,
                reply_text=reply_text,
                source_url=_instagram_source_url(adapter, mention_id),
                response_url="",
            )
            stats["replied"] += 1
            logging.info(
                "Replied to Instagram comment %s from %s",
                mention_id,
                mention_author or "unknown",
            )
        except Exception as exc:
            if reply_row_id is None:
                create_reply_record(
                    mention_row_id=mention_row_id,
                    decision=decision,
                    status="failed",
                    reply_text="",
                    platform="instagram",
                    error=str(exc),
                )
            else:
                update_reply_record(
                    reply_row_id=reply_row_id,
                    status="failed",
                    error=str(exc),
                )
            stats["failed"] += 1
            logging.error("Failed replying to Instagram comment %s: %s", mention_id, exc)

    return stats


def run() -> None:
    """CLI entrypoint for mention ingestion and reply handling."""
    _setup_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Process Mastodon/Bluesky mentions plus WordPress/Instagram comments "
            "for Copierbot."
        )
    )
    parser.add_argument(
        "--platform",
        default="all",
        help=(
            "Mention source platform(s) to process: mastodon, bluesky, wordpress, "
            "instagram, all, or a comma-separated subset."
        ),
    )
    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=40,
        help="Per-page notifications fetch size (Mastodon 1-80, Bluesky 1-100).",
    )
    parser.add_argument(
        "--process-limit",
        type=int,
        default=20,
        help="How many unhandled mention rows to process this run.",
    )
    args = parser.parse_args()

    settings = get_settings(require_news_api_key=False, require_openai_api_key=False)
    init_storage()
    persona_context = get_persona_context()
    persona_state = get_persona_state()
    try:
        quote_bank_entries = load_quote_bank()
    except QuoteBankError as exc:
        logging.warning("Proceeding without quote-bank replies: %s", exc)
        quote_bank_entries = []
    active_quote_entries = [entry for entry in quote_bank_entries if entry.get("enabled")]
    if active_quote_entries:
        logging.info(
            "Loaded %d active quote-bank entries for local reply selection.",
            len(active_quote_entries),
        )
    targets = _parse_platform_targets(args.platform)

    ran_any = False
    platform_failures: list[tuple[str, str]] = []
    for platform in targets:
        try:
            if platform == "mastodon":
                mastodon_config = load_mastodon_config(required=False)
                if mastodon_config is None:
                    logging.warning("Skipping Mastodon mention processing: config not set.")
                    continue
                adapter = MastodonAdapter(mastodon_config)
                account = adapter.verify_account()
                self_account_id = str(account.get("id", "")).strip()
                instance_limit = adapter.get_instance_max_characters(
                    fallback=settings.mastodon_max_chars
                )
                max_chars = min(instance_limit, settings.mastodon_max_chars)

                ingested = ingest_mastodon_mentions(
                    adapter=adapter,
                    self_account_id=self_account_id,
                    fetch_limit=max(1, min(args.fetch_limit, 80)),
                )
                stats = process_unhandled_mastodon_mentions(
                    adapter=adapter,
                    persona_context=persona_context,
                    persona_state=persona_state,
                    quote_bank_entries=quote_bank_entries,
                    max_chars=max_chars,
                    process_limit=max(1, args.process_limit),
                )
                logging.info(
                    "Mastodon mention processing complete: ingested=%d, seen_unhandled=%d, replied=%d, skipped=%d, failed=%d",
                    ingested,
                    stats["seen_unhandled"],
                    stats["replied"],
                    stats["skipped"],
                    stats["failed"],
                )
                ran_any = True
                continue

            if platform == "bluesky":
                bluesky_config = load_bluesky_config(required=False)
                if bluesky_config is None:
                    logging.warning("Skipping Bluesky mention processing: config not set.")
                    continue
                adapter = BlueskyAdapter(bluesky_config)
                account = adapter.verify_account()
                self_did = str(account.get("did", "")).strip()
                max_chars = min(adapter.get_instance_max_characters(), settings.bluesky_max_chars)

                ingested = ingest_bluesky_mentions(
                    adapter=adapter,
                    self_did=self_did,
                    fetch_limit=max(1, min(args.fetch_limit, 100)),
                )
                stats = process_unhandled_bluesky_mentions(
                    adapter=adapter,
                    persona_context=persona_context,
                    persona_state=persona_state,
                    quote_bank_entries=quote_bank_entries,
                    max_chars=max_chars,
                    process_limit=max(1, args.process_limit),
                )
                logging.info(
                    "Bluesky mention processing complete: ingested=%d, seen_unhandled=%d, replied=%d, skipped=%d, failed=%d",
                    ingested,
                    stats["seen_unhandled"],
                    stats["replied"],
                    stats["skipped"],
                    stats["failed"],
                )
                ran_any = True
                continue

            if platform == "wordpress":
                wordpress_config = load_wordpress_config(required=False)
                if wordpress_config is None:
                    logging.warning("Skipping WordPress mention processing: config not set.")
                    continue
                adapter = WordpressAdapter(wordpress_config)
                account = adapter.verify_account()
                try:
                    self_user_id = int(account.get("id") or 0)
                except (TypeError, ValueError):
                    self_user_id = 0
                max_chars = max(280, settings.bluesky_max_chars)

                ingested = ingest_wordpress_comments(
                    adapter=adapter,
                    self_user_id=self_user_id,
                    fetch_limit=max(1, min(args.fetch_limit, 100)),
                )
                stats = process_unhandled_wordpress_mentions(
                    adapter=adapter,
                    persona_context=persona_context,
                    persona_state=persona_state,
                    quote_bank_entries=quote_bank_entries,
                    max_chars=max_chars,
                    process_limit=max(1, args.process_limit),
                )
                logging.info(
                    "WordPress mention processing complete: ingested=%d, seen_unhandled=%d, replied=%d, skipped=%d, failed=%d",
                    ingested,
                    stats["seen_unhandled"],
                    stats["replied"],
                    stats["skipped"],
                    stats["failed"],
                )
                ran_any = True
                continue

            if platform == "instagram":
                instagram_config = load_instagram_config(required=False)
                if instagram_config is None:
                    logging.warning("Skipping Instagram mention processing: config not set.")
                    continue
                adapter = InstagramAdapter(instagram_config)
                account = adapter.verify_account()
                self_username = str(account.get("username", "")).strip()
                max_chars = instagram_config.comment_text_max_chars

                ingested = ingest_instagram_comments(
                    adapter=adapter,
                    self_username=self_username,
                    fetch_limit=max(1, min(args.fetch_limit, 100)),
                    media_limit=instagram_config.comment_scan_post_limit,
                )
                stats = process_unhandled_instagram_mentions(
                    adapter=adapter,
                    persona_context=persona_context,
                    persona_state=persona_state,
                    quote_bank_entries=quote_bank_entries,
                    max_chars=max_chars,
                    process_limit=max(1, args.process_limit),
                )
                logging.info(
                    "Instagram mention processing complete: ingested=%d, seen_unhandled=%d, replied=%d, skipped=%d, failed=%d",
                    ingested,
                    stats["seen_unhandled"],
                    stats["replied"],
                    stats["skipped"],
                    stats["failed"],
                )
                ran_any = True
                continue
        except (
            RuntimeError,
            MastodonAPIError,
            BlueskyAPIError,
            WordpressAPIError,
            InstagramAPIError,
        ) as exc:
            platform_failures.append((platform, str(exc)))
            logging.error("%s mention processing failed: %s", platform.capitalize(), exc)
            continue

    if platform_failures:
        logging.warning(
            "Mention cycle completed with platform failures: %s",
            "; ".join(f"{platform}: {error}" for platform, error in platform_failures),
        )

    if not ran_any:
        if platform_failures:
            raise RuntimeError(
                "No mention platforms completed successfully: "
                + "; ".join(f"{platform}: {error}" for platform, error in platform_failures)
            )
        raise ValueError("No mention platforms could be processed. Check platform configs.")


if __name__ == "__main__":
    try:
        run()
    except (
        ValueError,
        RuntimeError,
        MastodonAPIError,
        BlueskyAPIError,
        WordpressAPIError,
        InstagramAPIError,
    ) as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s | %(message)s")
        logging.error("Mention processor failed: %s", exc)
        raise SystemExit(1)

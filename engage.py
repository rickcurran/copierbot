"""Monitor social mentions/comments and post in-character system-log replies."""

from __future__ import annotations

import argparse
import html
import json
import logging
from pathlib import Path
import re
from typing import Any

from config import get_settings
from mention_archive import save_mention_response_log
from persona import get_persona_context
from social.bluesky_adapter import BlueskyAPIError, BlueskyAdapter, load_bluesky_config
from social.mastodon_adapter import MastodonAPIError, MastodonAdapter, load_mastodon_config
from social.wordpress_adapter import WordpressAPIError, WordpressAdapter, load_wordpress_config
from social_posting import append_ai_disclosure, disclosure_overhead_chars
from storage import (
    create_reply_record,
    init_storage,
    list_unhandled_mentions,
    mark_mention_handled,
    update_reply_record,
    upsert_mention,
)
from system_log import generate_system_log_local


MASTODON_MENTION_CURSOR_PATH = Path("data/mention_cursor.json")
BLUESKY_MENTION_CURSOR_PATH = Path("data/bluesky_mention_cursor.json")
WORDPRESS_COMMENT_CURSOR_PATH = Path("data/wordpress_comment_cursor.json")

CHECKIN_PATTERNS = [
    re.compile(r"\bhow\s+(are|r)\s+you\b", re.IGNORECASE),
    re.compile(r"\bhow\s+have\s+you\s+been\b", re.IGNORECASE),
    re.compile(r"\bhow(?:'|\u2019)?s\s+it\s+going\b", re.IGNORECASE),
    re.compile(r"\bhow\s+do\s+you\s+feel\b", re.IGNORECASE),
    re.compile(r"\bare\s+you\s+(ok|okay|well|alright)\b", re.IGNORECASE),
    re.compile(r"\byou\s+okay\b", re.IGNORECASE),
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

    return "other", "no_reply"


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


def process_unhandled_mastodon_mentions(
    *,
    adapter: MastodonAdapter,
    persona_context: str,
    max_chars: int,
    process_limit: int = 20,
) -> dict[str, int]:
    """Process queued mentions and optionally publish system-log replies."""
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
        if decision != "reply_system_log":
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=decision,
            )
            stats["skipped"] += 1
            continue

        reply_row_id: int | None = None
        try:
            reply_budget = max(1, max_chars - disclosure_overhead_chars())
            raw_reply_text = _build_reply_text(
                persona_context=persona_context,
                max_chars=reply_budget,
            )
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
                decision="replied_system_log",
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
    max_chars: int,
    process_limit: int = 20,
) -> dict[str, int]:
    """Process queued Bluesky mentions/replies and optionally post system-log replies."""
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
        if decision != "reply_system_log":
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=decision,
            )
            stats["skipped"] += 1
            continue

        reply_row_id: int | None = None
        try:
            reply_budget = max(1, max_chars - disclosure_overhead_chars())
            raw_reply_text = _build_reply_text(
                persona_context=persona_context,
                max_chars=reply_budget,
            )
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
                decision="replied_system_log",
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
    max_chars: int,
    process_limit: int = 20,
) -> dict[str, int]:
    """Process queued WordPress comment mentions and publish system-log replies."""
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
        if decision != "reply_system_log":
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision=decision,
            )
            stats["skipped"] += 1
            continue

        reply_row_id: int | None = None
        try:
            reply_text = _build_reply_text(
                persona_context=persona_context,
                max_chars=max_chars,
            )
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
                decision="replied_system_log",
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


def run() -> None:
    """CLI entrypoint for mention ingestion and reply handling."""
    _setup_logging()
    parser = argparse.ArgumentParser(
        description="Process Mastodon/Bluesky mentions and WordPress comments for Copierbot."
    )
    parser.add_argument(
        "--platform",
        default="all",
        choices=["mastodon", "bluesky", "wordpress", "all"],
        help="Mention source platform(s) to process.",
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
    targets = ["mastodon", "bluesky", "wordpress"] if args.platform == "all" else [args.platform]

    ran_any = False
    for platform in targets:
        if platform == "mastodon":
            mastodon_config = load_mastodon_config(required=False)
            if mastodon_config is None:
                logging.warning("Skipping Mastodon mention processing: config not set.")
                continue
            adapter = MastodonAdapter(mastodon_config)
            account = adapter.verify_account()
            self_account_id = str(account.get("id", "")).strip()
            instance_limit = adapter.get_instance_max_characters(fallback=settings.mastodon_max_chars)
            max_chars = min(instance_limit, settings.mastodon_max_chars)

            ingested = ingest_mastodon_mentions(
                adapter=adapter,
                self_account_id=self_account_id,
                fetch_limit=max(1, min(args.fetch_limit, 80)),
            )
            stats = process_unhandled_mastodon_mentions(
                adapter=adapter,
                persona_context=persona_context,
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

    if not ran_any:
        raise ValueError("No mention platforms could be processed. Check platform configs.")


if __name__ == "__main__":
    try:
        run()
    except (ValueError, RuntimeError, MastodonAPIError, BlueskyAPIError, WordpressAPIError) as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s | %(message)s")
        logging.error("Mention processor failed: %s", exc)
        raise SystemExit(1)

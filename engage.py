"""Monitor Mastodon mentions and post in-character system-log replies."""

from __future__ import annotations

import argparse
import html
import logging
import re
from typing import Any

from config import get_settings
from persona import get_persona_context, increment_post_counter
from social.mastodon_adapter import MastodonAPIError, MastodonAdapter, load_mastodon_config
from storage import (
    create_reply_record,
    init_storage,
    list_unhandled_mentions,
    mark_mention_handled,
    update_reply_record,
    upsert_mention,
)
from system_log import generate_system_log_local


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
    reply = generate_system_log_local(
        persona_context=contextual_persona,
        max_chars=max_chars,
    )

    if len(reply) > max_chars:
        raise RuntimeError(
            f"Generated reply is {len(reply)} characters and exceeds limit {max_chars}."
        )

    return reply


def ingest_mentions(adapter: MastodonAdapter, self_account_id: str, fetch_limit: int = 20) -> int:
    """Fetch mention notifications and upsert them into local storage."""
    notifications = adapter.fetch_notifications(types=["mention"], limit=fetch_limit)
    inserted = 0
    for notification in notifications:
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
    return inserted


def process_unhandled_mentions(
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
            reply_text = _build_reply_text(
                persona_context=persona_context,
                max_chars=max_chars,
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
            update_reply_record(
                reply_row_id=reply_row_id,
                status="sent",
                remote_reply_id=remote_reply_id,
            )
            mark_mention_handled(
                mention_row_id=mention_row_id,
                classification=classification,
                decision="replied_system_log",
            )
            increment_post_counter()
            stats["replied"] += 1
            logging.info("Replied to mention %s from %s", mention_id, mention_author or "unknown")
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


def run() -> None:
    """CLI entrypoint for mention ingestion and reply handling."""
    _setup_logging()
    parser = argparse.ArgumentParser(description="Process Mastodon mentions for Copierbot.")
    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=20,
        help="How many notifications to fetch from Mastodon (1-40).",
    )
    parser.add_argument(
        "--process-limit",
        type=int,
        default=20,
        help="How many unhandled mention rows to process this run.",
    )
    args = parser.parse_args()

    settings = get_settings(require_news_api_key=False, require_openai_api_key=False)
    mastodon_config = load_mastodon_config(required=True)
    assert mastodon_config is not None

    init_storage()
    adapter = MastodonAdapter(mastodon_config)
    account = adapter.verify_account()
    self_account_id = str(account.get("id", "")).strip()

    persona_context = get_persona_context()

    instance_limit = adapter.get_instance_max_characters(fallback=settings.mastodon_max_chars)
    if settings.post_mode == "mastodon":
        max_chars = min(instance_limit, settings.mastodon_max_chars)
    else:
        max_chars = instance_limit

    ingested = ingest_mentions(
        adapter=adapter,
        self_account_id=self_account_id,
        fetch_limit=max(1, min(args.fetch_limit, 40)),
    )
    stats = process_unhandled_mentions(
        adapter=adapter,
        persona_context=persona_context,
        max_chars=max_chars,
        process_limit=max(1, args.process_limit),
    )

    logging.info(
        "Mention processing complete: ingested=%d, seen_unhandled=%d, replied=%d, skipped=%d, failed=%d",
        ingested,
        stats["seen_unhandled"],
        stats["replied"],
        stats["skipped"],
        stats["failed"],
    )


if __name__ == "__main__":
    try:
        run()
    except (ValueError, RuntimeError, MastodonAPIError) as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s | %(message)s")
        logging.error("Mention processor failed: %s", exc)
        raise SystemExit(1)

"""Instagram Graph API adapter for publishing Copierbot posts and replying to comments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
import time

import requests
from dotenv import load_dotenv


@dataclass(frozen=True)
class InstagramConfig:
    """Configuration for Instagram Graph API access."""

    base_url: str
    ig_user_id: str
    access_token: str
    access_token_expires_at: datetime | None = None
    api_version: str = "v23.0"
    timeout_seconds: int = 30
    caption_max_chars: int = 2200
    comment_text_max_chars: int = 1000
    comment_scan_post_limit: int = 25


class InstagramAPIError(RuntimeError):
    """Raised for HTTP or protocol-level Instagram API failures."""


INSTAGRAM_TOKEN_ALERT_STATE_PATH = Path("data/instagram_token_alert_state.json")
INSTAGRAM_TOKEN_EXPIRY_ALERT_STATE_PATH = Path("data/instagram_token_expiry_alert_state.json")


def _parse_instagram_token_expiry(raw_value: str) -> datetime | None:
    """Parse optional Instagram token expiry timestamp from environment."""
    value = (raw_value or "").strip()
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _redact_access_token(text: str) -> str:
    """Redact access_token query values from error strings."""
    return re.sub(r"(access_token=)[^&\s]+", r"\1[REDACTED]", text or "", flags=re.IGNORECASE)


def instagram_token_expiry_warning(
    config: InstagramConfig,
    *,
    warning_days: int = 7,
    now: datetime | None = None,
) -> str:
    """Return a warning string when the configured token is near expiry."""
    expires_at = config.access_token_expires_at
    if expires_at is None:
        return ""

    current = now or datetime.now(timezone.utc)
    remaining = expires_at - current
    remaining_seconds = remaining.total_seconds()
    if remaining_seconds <= 0:
        return (
            "Configured INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT is already in the past. "
            "Replace INSTAGRAM_ACCESS_TOKEN with a fresh long-lived token."
        )

    if remaining_seconds > warning_days * 86400:
        return ""

    remaining_days = remaining_seconds / 86400
    if remaining_days >= 1:
        window = f"{remaining_days:.1f} days"
    else:
        remaining_hours = max(1.0, remaining_seconds / 3600)
        window = f"{remaining_hours:.1f} hours"

    return (
        "Instagram access token expires soon "
        f"({window} remaining, expires at {expires_at.isoformat()})."
    )


def instagram_token_time_remaining(
    config: InstagramConfig,
    *,
    now: datetime | None = None,
) -> timedelta | None:
    """Return remaining time until the configured token expires."""
    expires_at = config.access_token_expires_at
    if expires_at is None:
        return None
    current = now or datetime.now(timezone.utc)
    return expires_at - current


def is_instagram_token_error_message(message: str) -> bool:
    """Return True when an Instagram API error looks token-related."""
    normalized = " ".join((message or "").lower().split())
    if not normalized:
        return False
    indicators = (
        "access token",
        "validating access token",
        "session has expired",
        "invalid oauth access token",
    )
    return any(indicator in normalized for indicator in indicators)


def instagram_token_error_guidance(message: str) -> str:
    """Return concise remediation guidance for Instagram token failures."""
    if is_instagram_token_error_message(message):
        return (
            "Replace INSTAGRAM_ACCESS_TOKEN in .env with a long-lived Instagram/Meta token, "
            "update INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT if known, then restart the dashboard."
        )
    return "Review the Instagram configuration and API permissions, then retry."


def clear_instagram_token_alert_state() -> None:
    """Remove persisted Instagram token alert suppression state."""
    try:
        INSTAGRAM_TOKEN_ALERT_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def should_send_instagram_token_expiry_alert(
    config: InstagramConfig,
    *,
    threshold_days: int,
    now: datetime | None = None,
) -> bool:
    """Return True when a proactive Instagram token-expiry alert should be sent."""
    expires_at = config.access_token_expires_at
    if expires_at is None:
        return False

    current = now or datetime.now(timezone.utc)
    remaining = expires_at - current
    if remaining.total_seconds() <= 0:
        return False
    if remaining.total_seconds() > threshold_days * 86400:
        return False

    try:
        raw = json.loads(INSTAGRAM_TOKEN_EXPIRY_ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}

    expires_key = expires_at.astimezone(timezone.utc).isoformat()
    sent_thresholds_raw = raw.get(expires_key, [])
    if isinstance(sent_thresholds_raw, list):
        sent_thresholds = {int(item) for item in sent_thresholds_raw if str(item).strip()}
    else:
        sent_thresholds = set()
    if threshold_days in sent_thresholds:
        return False

    sent_thresholds.add(threshold_days)
    raw[expires_key] = sorted(sent_thresholds, reverse=True)
    INSTAGRAM_TOKEN_EXPIRY_ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTAGRAM_TOKEN_EXPIRY_ALERT_STATE_PATH.write_text(
        json.dumps(raw, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def should_send_instagram_token_alert(
    *,
    context: str,
    error_text: str,
    cooldown_hours: int = 12,
    now: datetime | None = None,
) -> bool:
    """Return True when an Instagram token alert should be sent."""
    if not is_instagram_token_error_message(error_text):
        return False

    current = now or datetime.now(timezone.utc)
    fingerprint = "instagram_token_error"

    try:
        raw = json.loads(INSTAGRAM_TOKEN_ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}

    last_fingerprint = str(raw.get("fingerprint", "")).strip()
    last_sent_raw = str(raw.get("sent_at", "")).strip()
    last_sent: datetime | None = None
    if last_sent_raw:
        try:
            last_sent = datetime.fromisoformat(last_sent_raw.replace("Z", "+00:00"))
        except ValueError:
            last_sent = None
        if last_sent is not None and last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)

    if (
        fingerprint == last_fingerprint
        and last_sent is not None
        and (current - last_sent).total_seconds() < cooldown_hours * 3600
    ):
        return False

    INSTAGRAM_TOKEN_ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTAGRAM_TOKEN_ALERT_STATE_PATH.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "context": context,
                "sent_at": current.astimezone(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return True


def load_instagram_config(required: bool = False) -> InstagramConfig | None:
    """Load Instagram Graph configuration from environment variables."""
    load_dotenv()
    base_url = os.getenv("INSTAGRAM_BASE_URL", "https://graph.facebook.com").strip().rstrip("/")
    ig_user_id = os.getenv("INSTAGRAM_IG_USER_ID", "").strip()
    access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    access_token_expires_at = _parse_instagram_token_expiry(
        os.getenv("INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT", "")
    )
    api_version = os.getenv("INSTAGRAM_API_VERSION", "v23.0").strip().lstrip("/") or "v23.0"

    timeout_raw = os.getenv("INSTAGRAM_TIMEOUT_SECONDS", "30").strip()
    caption_max_chars_raw = os.getenv("INSTAGRAM_CAPTION_MAX_CHARS", "2200").strip()
    comment_text_max_chars_raw = os.getenv("INSTAGRAM_COMMENT_TEXT_MAX_CHARS", "1000").strip()
    comment_scan_post_limit_raw = os.getenv("INSTAGRAM_COMMENT_SCAN_POST_LIMIT", "25").strip()

    try:
        timeout_seconds = int(timeout_raw)
    except ValueError:
        timeout_seconds = 30
    timeout_seconds = max(5, min(timeout_seconds, 180))

    try:
        caption_max_chars = int(caption_max_chars_raw)
    except ValueError:
        caption_max_chars = 2200
    caption_max_chars = max(100, min(caption_max_chars, 2200))

    try:
        comment_text_max_chars = int(comment_text_max_chars_raw)
    except ValueError:
        comment_text_max_chars = 1000
    comment_text_max_chars = max(100, min(comment_text_max_chars, 1000))

    try:
        comment_scan_post_limit = int(comment_scan_post_limit_raw)
    except ValueError:
        comment_scan_post_limit = 25
    comment_scan_post_limit = max(1, min(comment_scan_post_limit, 100))

    if not ig_user_id or not access_token:
        if required:
            raise ValueError(
                "Missing Instagram configuration. Set INSTAGRAM_IG_USER_ID and "
                "INSTAGRAM_ACCESS_TOKEN."
            )
        return None

    return InstagramConfig(
        base_url=base_url or "https://graph.facebook.com",
        ig_user_id=ig_user_id,
        access_token=access_token,
        access_token_expires_at=access_token_expires_at,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
        caption_max_chars=caption_max_chars,
        comment_text_max_chars=comment_text_max_chars,
        comment_scan_post_limit=comment_scan_post_limit,
    )


class InstagramAdapter:
    """Thin wrapper around key Instagram Graph API endpoints."""

    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    MAX_REQUEST_RETRIES = 2
    CONTAINER_READY_STATES = {"finished", "published"}
    CONTAINER_PENDING_STATES = {"in_progress"}
    CONTAINER_ERROR_STATES = {"error", "expired", "failed"}

    def __init__(self, config: InstagramConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "copierbot/1.0 (+local-cli)",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Perform an Instagram Graph API request and return JSON payload."""
        url = f"{self.config.base_url}/{self.config.api_version}/{path.lstrip('/')}"
        query = dict(params or {})
        query["access_token"] = self.config.access_token
        max_attempts = 1 + self.MAX_REQUEST_RETRIES
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=query or None,
                    data=data or None,
                    timeout=self.config.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    time.sleep(0.7 * (2**attempt))
                    continue
                raise InstagramAPIError(
                    f"Instagram request failed ({method} {path}): {_redact_access_token(str(exc))}"
                ) from exc

            if (
                response.status_code in self.RETRYABLE_STATUS_CODES
                and attempt < max_attempts - 1
            ):
                retry_after = response.headers.get("Retry-After", "").strip()
                try:
                    wait_seconds = float(retry_after)
                except ValueError:
                    wait_seconds = 0.7 * (2**attempt)
                time.sleep(max(0.2, min(wait_seconds, 8.0)))
                continue

            if response.status_code >= 400:
                message = response.text[:360]
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        error_payload = payload.get("error")
                        if isinstance(error_payload, dict):
                            message = str(error_payload.get("message") or message)
                        else:
                            message = str(payload.get("message") or message)
                except ValueError:
                    pass
                raise InstagramAPIError(
                    f"Instagram API error {response.status_code} ({method} {path}): {message}"
                )

            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise InstagramAPIError(
                    f"Instagram API returned non-JSON response ({method} {path})."
                ) from exc

        if last_error is not None:
            raise InstagramAPIError(
                f"Instagram request failed ({method} {path}): {_redact_access_token(str(last_error))}"
            )
        raise InstagramAPIError(f"Instagram request failed ({method} {path}): unknown error")

    def verify_account(self) -> dict:
        """Validate credentials and return basic account identity fields."""
        payload = self._request(
            "GET",
            self.config.ig_user_id,
            params={"fields": "id,username"},
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected Instagram account payload type.")
        return payload

    def create_image_container(self, *, image_url: str, caption: str = "") -> dict:
        """Create one Instagram media container for a public image URL."""
        public_url = (image_url or "").strip()
        if not public_url:
            raise InstagramAPIError("Instagram image publishing requires a public image URL.")
        if len(caption) > self.config.caption_max_chars:
            raise InstagramAPIError(
                f"Instagram caption length {len(caption)} exceeds limit "
                f"{self.config.caption_max_chars}."
            )

        payload = self._request(
            "POST",
            f"{self.config.ig_user_id}/media",
            data={
                "image_url": public_url,
                "caption": caption,
            },
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected media container payload type.")
        return payload

    def get_media_container(self, creation_id: str) -> dict:
        """Fetch one media container payload."""
        payload = self._request(
            "GET",
            creation_id,
            params={"fields": "id,status,status_code"},
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected media container payload type.")
        return payload

    def wait_for_container_ready(
        self,
        creation_id: str,
        *,
        max_attempts: int = 10,
        poll_seconds: float = 2.0,
    ) -> None:
        """Poll one media container until it can be published."""
        if not creation_id:
            raise InstagramAPIError("Missing Instagram creation id while waiting for readiness.")

        for attempt in range(max_attempts):
            payload = self.get_media_container(creation_id)
            status_code = str(payload.get("status_code") or payload.get("status") or "").strip()
            normalized = status_code.lower()
            if not normalized:
                return
            if normalized in self.CONTAINER_READY_STATES:
                return
            if normalized in self.CONTAINER_ERROR_STATES:
                raise InstagramAPIError(
                    f"Instagram media container {creation_id} failed with status {status_code}."
                )
            if normalized not in self.CONTAINER_PENDING_STATES:
                return
            if attempt < max_attempts - 1:
                time.sleep(max(0.2, poll_seconds))

    def publish_container(self, *, creation_id: str) -> dict:
        """Publish a previously created Instagram media container."""
        payload = self._request(
            "POST",
            f"{self.config.ig_user_id}/media_publish",
            data={"creation_id": creation_id},
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected media publish payload type.")
        return payload

    def get_media(self, media_id: str) -> dict:
        """Fetch one published media payload."""
        payload = self._request(
            "GET",
            media_id,
            params={"fields": "id,caption,permalink,media_type,shortcode,timestamp"},
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected media payload type.")
        return payload

    def publish_image(self, *, image_url: str, caption: str = "") -> dict:
        """Publish one image post to Instagram and return media metadata."""
        container = self.create_image_container(image_url=image_url, caption=caption)
        creation_id = str(container.get("id", "")).strip()
        if not creation_id:
            raise InstagramAPIError("Instagram media container response missing id.")
        self.wait_for_container_ready(creation_id)

        publish_payload = self.publish_container(creation_id=creation_id)
        media_id = str(publish_payload.get("id", "")).strip()
        if not media_id:
            raise InstagramAPIError("Instagram publish succeeded but returned no media id.")

        media_payload = self.get_media(media_id)
        result = dict(media_payload)
        result["creation_id"] = creation_id
        return result

    def list_comments(
        self,
        *,
        media_id: str,
        limit: int = 50,
        after: str = "",
    ) -> dict:
        """List comments for one Instagram media object."""
        params: dict[str, Any] = {
            "fields": "id,text,timestamp,username,from{id,username},parent_id,media{id,permalink}",
            "limit": max(1, min(limit, 100)),
        }
        if after:
            params["after"] = after
        payload = self._request(
            "GET",
            f"{media_id}/comments",
            params=params,
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected Instagram comments payload type.")
        data = payload.get("data")
        if data is None:
            payload["data"] = []
        elif not isinstance(data, list):
            raise InstagramAPIError("Unexpected Instagram comments data payload type.")
        return payload

    def list_replies(
        self,
        *,
        comment_id: str,
        limit: int = 50,
        after: str = "",
    ) -> dict:
        """List replies for one Instagram comment."""
        params: dict[str, Any] = {
            "fields": "id,text,timestamp,username,from{id,username},parent_id,media{id,permalink}",
            "limit": max(1, min(limit, 100)),
        }
        if after:
            params["after"] = after
        payload = self._request(
            "GET",
            f"{comment_id}/replies",
            params=params,
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected Instagram replies payload type.")
        data = payload.get("data")
        if data is None:
            payload["data"] = []
        elif not isinstance(data, list):
            raise InstagramAPIError("Unexpected Instagram replies data payload type.")
        return payload

    def get_comment(self, comment_id: str) -> dict:
        """Fetch one Instagram comment or reply."""
        payload = self._request(
            "GET",
            comment_id,
            params={"fields": "id,text,timestamp,username,from{id,username},parent_id,media{id,permalink}"},
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected Instagram comment payload type.")
        return payload

    def reply_to_comment(self, *, comment_id: str, text: str) -> dict:
        """Reply to one Instagram comment."""
        message = (text or "").strip()
        if not message:
            raise InstagramAPIError("Cannot publish empty Instagram comment reply.")
        if len(message) > self.config.comment_text_max_chars:
            raise InstagramAPIError(
                f"Instagram comment reply length {len(message)} exceeds limit "
                f"{self.config.comment_text_max_chars}."
            )

        payload = self._request(
            "POST",
            f"{comment_id}/replies",
            data={"message": message},
        )
        if not isinstance(payload, dict):
            raise InstagramAPIError("Unexpected Instagram reply payload type.")
        return payload

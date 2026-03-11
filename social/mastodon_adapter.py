"""Mastodon API adapter for publishing and engagement workflows."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv


@dataclass(frozen=True)
class MastodonConfig:
    """Configuration for Mastodon API access."""

    base_url: str
    access_token: str
    default_visibility: str = "unlisted"
    timeout_seconds: int = 30


class MastodonAPIError(RuntimeError):
    """Raised for HTTP or protocol-level Mastodon API failures."""


def load_mastodon_config(required: bool = False) -> MastodonConfig | None:
    """Load Mastodon configuration from environment variables."""
    load_dotenv()
    base_url = os.getenv("MASTODON_BASE_URL", "").strip().rstrip("/")
    access_token = os.getenv("MASTODON_ACCESS_TOKEN", "").strip()
    default_visibility = os.getenv("MASTODON_VISIBILITY", "unlisted").strip().lower() or "unlisted"

    if not base_url or not access_token:
        if required:
            raise ValueError(
                "Missing Mastodon configuration. Set MASTODON_BASE_URL and MASTODON_ACCESS_TOKEN."
            )
        return None

    return MastodonConfig(
        base_url=base_url,
        access_token=access_token,
        default_visibility=default_visibility,
    )


class MastodonAdapter:
    """Thin wrapper around Mastodon REST endpoints."""

    def __init__(self, config: MastodonConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.access_token}",
                "Accept": "application/json",
                "User-Agent": "copierbot/1.0 (+local-cli)",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform a Mastodon API request and return JSON body."""
        url = f"{self.config.base_url}{path}"
        headers = dict(extra_headers or {})
        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                data=data,
                files=files,
                headers=headers or None,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise MastodonAPIError(f"Mastodon request failed ({method} {path}): {exc}") from exc

        if response.status_code >= 400:
            message = response.text[:240]
            try:
                payload = response.json()
                message = str(payload.get("error") or payload.get("error_description") or message)
            except ValueError:
                pass
            raise MastodonAPIError(
                f"Mastodon API error {response.status_code} ({method} {path}): {message}"
            )

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise MastodonAPIError(
                f"Mastodon API returned non-JSON response ({method} {path})."
            ) from exc

    def verify_account(self) -> dict:
        """Validate token and return account metadata."""
        payload = self._request("GET", "/api/v1/accounts/verify_credentials")
        if not isinstance(payload, dict):
            raise MastodonAPIError("Unexpected verify_credentials payload type.")
        return payload

    def upload_media(
        self, file_path: Path, description: str = "", focus: str = "", mime_type: str = "image/png"
    ) -> dict:
        """Upload media and return Mastodon media object."""
        if not file_path.exists():
            raise FileNotFoundError(f"Media file not found: {file_path}")

        data: dict[str, str] = {}
        if description.strip():
            data["description"] = description.strip()
        if focus.strip():
            data["focus"] = focus.strip()

        with file_path.open("rb") as handle:
            files = {"file": (file_path.name, handle, mime_type)}
            payload = self._request("POST", "/api/v2/media", data=data or None, files=files)

        if not isinstance(payload, dict):
            raise MastodonAPIError("Unexpected media upload payload type.")
        return payload

    def publish_status(
        self,
        status: str,
        *,
        media_ids: list[str] | None = None,
        in_reply_to_id: str = "",
        visibility: str = "",
        spoiler_text: str = "",
        sensitive: bool = False,
        language: str = "",
        idempotency_key: str = "",
    ) -> dict:
        """Publish a status and return Mastodon status object."""
        payload: dict[str, Any] = {
            "status": status,
            "visibility": visibility or self.config.default_visibility,
            "sensitive": bool(sensitive),
        }
        if media_ids:
            payload["media_ids"] = media_ids
        if in_reply_to_id:
            payload["in_reply_to_id"] = in_reply_to_id
        if spoiler_text:
            payload["spoiler_text"] = spoiler_text
        if language:
            payload["language"] = language

        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        response = self._request(
            "POST",
            "/api/v1/statuses",
            json_body=payload,
            extra_headers=headers,
        )
        if not isinstance(response, dict):
            raise MastodonAPIError("Unexpected status publish payload type.")
        return response

    def reply_status(
        self,
        in_reply_to_id: str,
        status: str,
        *,
        visibility: str = "",
        idempotency_key: str = "",
    ) -> dict:
        """Reply to an existing Mastodon status."""
        return self.publish_status(
            status=status,
            in_reply_to_id=in_reply_to_id,
            visibility=visibility,
            idempotency_key=idempotency_key,
        )

    def fetch_notifications(
        self,
        *,
        types: Iterable[str] | None = None,
        limit: int = 20,
        since_id: str = "",
        min_id: str = "",
    ) -> list[dict]:
        """Fetch notifications (mentions, follows, etc.)."""
        limit = max(1, min(limit, 40))
        params: list[tuple[str, Any]] = [("limit", limit)]
        for notification_type in (types or []):
            params.append(("types[]", notification_type))
        if since_id:
            params.append(("since_id", since_id))
        if min_id:
            params.append(("min_id", min_id))

        payload = self._request("GET", "/api/v1/notifications", params=params)
        if not isinstance(payload, list):
            raise MastodonAPIError("Unexpected notifications payload type.")
        return [item for item in payload if isinstance(item, dict)]

    def dismiss_notification(self, notification_id: str) -> None:
        """Dismiss one notification."""
        self._request("POST", f"/api/v1/notifications/{notification_id}/dismiss")

    def get_instance_max_characters(self, fallback: int = 500) -> int:
        """Return configured status character limit with safe fallback."""
        try:
            v2 = self._request("GET", "/api/v2/instance")
            limit = (
                ((v2 or {}).get("configuration") or {})
                .get("statuses", {})
                .get("max_characters")
            )
            if isinstance(limit, int) and limit > 0:
                return limit
        except Exception:
            pass

        try:
            v1 = self._request("GET", "/api/v1/instance")
            limit = (v1 or {}).get("max_toot_chars")
            if isinstance(limit, int) and limit > 0:
                return limit
        except Exception:
            pass

        return fallback

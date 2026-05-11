"""Bluesky API adapter for publishing Copierbot posts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import time
from typing import Any

from PIL import Image
import requests
from dotenv import load_dotenv


@dataclass(frozen=True)
class BlueskyConfig:
    """Configuration for Bluesky/ATProto API access."""

    pds_url: str
    handle: str
    app_password: str
    timeout_seconds: int = 30
    max_chars: int = 300


class BlueskyAPIError(RuntimeError):
    """Raised for HTTP or protocol-level Bluesky API failures."""


def load_bluesky_config(required: bool = False) -> BlueskyConfig | None:
    """Load Bluesky configuration from environment variables."""
    load_dotenv()
    pds_url = os.getenv("BLUESKY_PDS_URL", "https://bsky.social").strip().rstrip("/")
    handle = os.getenv("BLUESKY_HANDLE", "").strip()
    app_password = os.getenv("BLUESKY_APP_PASSWORD", "").strip()

    max_chars_raw = os.getenv("BLUESKY_MAX_CHARS", "300").strip()
    try:
        max_chars = int(max_chars_raw)
    except ValueError:
        max_chars = 300
    max_chars = max(100, min(max_chars, 500))

    if not handle or not app_password:
        if required:
            raise ValueError(
                "Missing Bluesky configuration. Set BLUESKY_HANDLE and BLUESKY_APP_PASSWORD."
            )
        return None

    return BlueskyConfig(
        pds_url=pds_url or "https://bsky.social",
        handle=handle,
        app_password=app_password,
        max_chars=max_chars,
    )


class BlueskyAdapter:
    """Thin wrapper around key Bluesky XRPC endpoints."""

    MAX_IMAGE_BYTES = 950 * 1024
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    MAX_REQUEST_RETRIES = 2
    SESSION_CACHE_PATH = Path("data/bluesky_session.json")

    def __init__(self, config: BlueskyConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "copierbot/1.0 (+local-cli)",
            }
        )
        self._access_jwt = ""
        self._refresh_jwt = ""
        self._did = ""
        self._resolved_handle = ""

    def _apply_session_payload(self, payload: dict[str, Any]) -> None:
        """Apply session payload returned by Bluesky auth endpoints."""
        access_jwt = str(payload.get("accessJwt", "")).strip()
        refresh_jwt = str(payload.get("refreshJwt", "")).strip()
        did = str(payload.get("did", "")).strip()
        handle = str(payload.get("handle", "")).strip()

        if not access_jwt or not did:
            raise BlueskyAPIError("Session response missing accessJwt or did.")

        self._access_jwt = access_jwt
        self._refresh_jwt = refresh_jwt
        self._did = did
        self._resolved_handle = handle or self.config.handle

    def _reset_session(self) -> None:
        """Clear in-memory auth context."""
        self._access_jwt = ""
        self._refresh_jwt = ""
        self._did = ""
        self._resolved_handle = ""

    def _load_cached_session(self) -> bool:
        """Load cached Bluesky session tokens from disk when available."""
        try:
            raw = json.loads(self.SESSION_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        if not isinstance(raw, dict):
            return False

        try:
            self._apply_session_payload(raw)
        except BlueskyAPIError:
            self._clear_cached_session()
            return False
        return True

    def _save_cached_session(self) -> None:
        """Persist Bluesky session tokens for reuse across short-lived CLI runs."""
        payload = {
            "accessJwt": self._access_jwt,
            "refreshJwt": self._refresh_jwt,
            "did": self._did,
            "handle": self._resolved_handle or self.config.handle,
            "savedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        try:
            self.SESSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.SESSION_CACHE_PATH.write_text(
                json.dumps(payload, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _clear_cached_session(self) -> None:
        """Remove cached session tokens after auth failure."""
        self._reset_session()
        try:
            self.SESSION_CACHE_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    def _create_session(self) -> None:
        """Create a brand new Bluesky session from handle + app password."""
        payload = self._request(
            "POST",
            "/xrpc/com.atproto.server.createSession",
            json_body={"identifier": self.config.handle, "password": self.config.app_password},
            auth=False,
        )
        if not isinstance(payload, dict):
            raise BlueskyAPIError("Unexpected createSession payload type.")
        self._apply_session_payload(payload)
        self._save_cached_session()

    def _refresh_session(self) -> None:
        """Refresh a cached Bluesky session using its refresh token."""
        if not self._refresh_jwt:
            raise BlueskyAPIError("No cached Bluesky refresh token is available.")
        payload = self._request(
            "POST",
            "/xrpc/com.atproto.server.refreshSession",
            extra_headers={"Authorization": f"Bearer {self._refresh_jwt}"},
            auth=False,
        )
        if not isinstance(payload, dict):
            raise BlueskyAPIError("Unexpected refreshSession payload type.")
        self._apply_session_payload(payload)
        self._save_cached_session()

    def _recover_session(self) -> None:
        """Recover auth after a 401 by refreshing, then falling back to login."""
        refresh_error: Exception | None = None
        if self._refresh_jwt:
            try:
                self._refresh_session()
                return
            except Exception as exc:  # pragma: no cover - defensive fallback path
                refresh_error = exc
                self._clear_cached_session()
        try:
            self._create_session()
        except Exception as exc:
            if refresh_error is not None:
                raise BlueskyAPIError(
                    f"Failed to refresh Bluesky session ({refresh_error}) and login again ({exc})."
                ) from exc
            raise

    def _is_expired_auth_response(self, response: requests.Response) -> bool:
        """Return True when Bluesky says the bearer token has expired."""
        if response.status_code == 401:
            return True
        if response.status_code != 400:
            return False

        message = response.text[:320]
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = str(payload.get("message") or payload.get("error") or message)
        except ValueError:
            pass

        normalized = message.strip().lower()
        return "token" in normalized and "expired" in normalized

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        extra_headers: dict[str, str] | None = None,
        auth: bool = True,
        timeout_seconds: int | None = None,
    ) -> Any:
        """Perform a Bluesky XRPC request and return JSON payload."""
        url = f"{self.config.pds_url}{path}"
        headers = dict(extra_headers or {})
        if auth and self._access_jwt:
            headers["Authorization"] = f"Bearer {self._access_jwt}"
        request_timeout = int(timeout_seconds or self.config.timeout_seconds)
        max_attempts = 1 + self.MAX_REQUEST_RETRIES
        last_error: Exception | None = None
        auth_recovered = False

        for attempt in range(max_attempts):
            request_headers = dict(headers)
            if auth and self._access_jwt and "Authorization" not in request_headers:
                request_headers["Authorization"] = f"Bearer {self._access_jwt}"
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=request_headers or None,
                    timeout=request_timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    time.sleep(0.7 * (2**attempt))
                    continue
                raise BlueskyAPIError(f"Bluesky request failed ({method} {path}): {exc}") from exc

            if auth and not auth_recovered and self._is_expired_auth_response(response):
                try:
                    self._recover_session()
                except Exception:
                    pass
                else:
                    auth_recovered = True
                    continue

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
                message = response.text[:320]
                try:
                    payload = response.json()
                    message = str(payload.get("message") or payload.get("error") or message)
                except ValueError:
                    pass
                raise BlueskyAPIError(
                    f"Bluesky API error {response.status_code} ({method} {path}): {message}"
                )

            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise BlueskyAPIError(
                    f"Bluesky API returned non-JSON response ({method} {path})."
                ) from exc

        if last_error is not None:
            raise BlueskyAPIError(f"Bluesky request failed ({method} {path}): {last_error}")
        raise BlueskyAPIError(f"Bluesky request failed ({method} {path}): unknown error")

    def _ensure_session(self) -> None:
        """Create a session if needed and cache auth context."""
        if self._access_jwt and self._did:
            return

        if self._load_cached_session():
            return

        self._create_session()

    def verify_account(self) -> dict:
        """Validate credentials and return account identity fields."""
        self._ensure_session()
        return {
            "did": self._did,
            "handle": self._resolved_handle or self.config.handle,
        }

    def _public_post_url(self, uri: str) -> str:
        """Build a bsky.app URL from at:// post URI when possible."""
        uri_clean = (uri or "").strip()
        rkey = uri_clean.rsplit("/", 1)[-1] if "/" in uri_clean else ""
        handle = self._resolved_handle or self.config.handle
        if handle and rkey:
            return f"https://bsky.app/profile/{handle}/post/{rkey}"
        return ""

    def public_post_url(self, uri: str) -> str:
        """Return a public Bluesky URL for one at:// post URI."""
        return self._public_post_url(uri)

    def _mime_for_path(self, file_path: Path) -> str:
        """Return mime type hint for upload blob endpoint."""
        suffix = file_path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix == ".webp":
            return "image/webp"
        return "image/png"

    def _prepare_image_blob_bytes(self, file_path: Path) -> tuple[bytes, str]:
        """Compress and resize image to stay under Bluesky blob size constraints."""
        raw = file_path.read_bytes()
        original_mime = self._mime_for_path(file_path)
        if len(raw) <= self.MAX_IMAGE_BYTES and original_mime in {
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            return raw, original_mime

        try:
            source = Image.open(file_path)
        except Exception as exc:
            raise BlueskyAPIError(f"Failed to open image for Bluesky upload: {exc}") from exc

        # Bluesky accepts JPEG well; convert aggressively when needed.
        source = source.convert("RGBA")

        def _flatten_rgba_to_rgb(image: Image.Image) -> Image.Image:
            """Flatten alpha channel onto white background for JPEG encoding."""
            if image.mode != "RGBA":
                return image.convert("RGB")
            bg = Image.new("RGBA", image.size, (255, 255, 255, 255))
            bg.alpha_composite(image)
            return bg.convert("RGB")

        max_sides = [2048, 1700, 1500, 1300, 1150, 1024, 900, 820, 760]
        qualities = [88, 82, 76, 70, 64, 58, 52, 46, 40]

        for max_side in max_sides:
            working = source.copy()
            working.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            rgb = _flatten_rgba_to_rgb(working)
            for quality in qualities:
                buffer = BytesIO()
                rgb.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
                payload = buffer.getvalue()
                if len(payload) <= self.MAX_IMAGE_BYTES:
                    return payload, "image/jpeg"

        raise BlueskyAPIError(
            "Unable to compress image under Bluesky size limit. "
            f"Current limit: {self.MAX_IMAGE_BYTES} bytes."
        )

    def upload_image_blob(self, file_path: Path) -> dict:
        """Upload local image file as ATProto blob and return blob ref."""
        if not file_path.exists():
            raise FileNotFoundError(f"Image file not found: {file_path}")

        self._ensure_session()
        payload_bytes, mime_type = self._prepare_image_blob_bytes(file_path)
        payload = self._request(
            "POST",
            "/xrpc/com.atproto.repo.uploadBlob",
            data=payload_bytes,
            extra_headers={"Content-Type": mime_type},
            timeout_seconds=max(self.config.timeout_seconds, 60),
        )
        if not isinstance(payload, dict):
            raise BlueskyAPIError("Unexpected uploadBlob payload type.")
        blob = payload.get("blob")
        if not isinstance(blob, dict):
            raise BlueskyAPIError("uploadBlob response missing blob object.")
        return blob

    def publish_post(
        self,
        text: str,
        *,
        image_path: Path | None = None,
        image_alt: str = "",
    ) -> dict:
        """Publish a Bluesky post with optional single image."""
        self._ensure_session()
        status_text = (text or "").strip()
        if not status_text:
            raise BlueskyAPIError("Cannot publish empty Bluesky post.")
        if len(status_text) > self.config.max_chars:
            raise BlueskyAPIError(
                f"Status text length {len(status_text)} exceeds Bluesky limit {self.config.max_chars}."
            )

        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": status_text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        if image_path is not None and image_path.exists():
            blob = self.upload_image_blob(image_path)
            record["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": [
                    {
                        "alt": (image_alt or "Copierbot surreal collage artwork").strip(),
                        "image": blob,
                    }
                ],
            }

        payload = self._request(
            "POST",
            "/xrpc/com.atproto.repo.createRecord",
            json_body={
                "repo": self._did,
                "collection": "app.bsky.feed.post",
                "record": record,
            },
        )
        if not isinstance(payload, dict):
            raise BlueskyAPIError("Unexpected createRecord payload type.")

        uri = str(payload.get("uri", "")).strip()
        url = self._public_post_url(uri)

        return {
            "uri": uri,
            "cid": str(payload.get("cid", "")).strip(),
            "url": url,
        }

    def list_notifications(
        self,
        *,
        reasons: list[str] | None = None,
        limit: int = 50,
        cursor: str = "",
    ) -> dict:
        """Fetch Bluesky notifications page."""
        self._ensure_session()
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if reasons:
            params["reasons"] = reasons
        if cursor:
            params["cursor"] = cursor

        payload = self._request(
            "GET",
            "/xrpc/app.bsky.notification.listNotifications",
            params=params,
        )
        if not isinstance(payload, dict):
            raise BlueskyAPIError("Unexpected listNotifications payload type.")
        return payload

    def get_post(self, uri: str) -> dict:
        """Resolve one post URI to post view payload with uri/cid/record."""
        self._ensure_session()
        payload = self._request(
            "GET",
            "/xrpc/app.bsky.feed.getPosts",
            params={"uris": [uri]},
        )
        if not isinstance(payload, dict):
            raise BlueskyAPIError("Unexpected getPosts payload type.")
        posts = payload.get("posts")
        if not isinstance(posts, list) or not posts:
            raise BlueskyAPIError("getPosts returned no posts.")
        post = posts[0]
        if not isinstance(post, dict):
            raise BlueskyAPIError("Unexpected post payload item type.")
        return post

    def reply_to_post(self, *, parent_uri: str, text: str) -> dict:
        """Reply to an existing Bluesky post URI."""
        self._ensure_session()
        target = self.get_post(parent_uri)
        parent = {
            "uri": str(target.get("uri", "")).strip(),
            "cid": str(target.get("cid", "")).strip(),
        }
        if not parent["uri"] or not parent["cid"]:
            raise BlueskyAPIError("Cannot reply: target post missing uri/cid.")

        record_payload = target.get("record")
        root = parent
        if isinstance(record_payload, dict):
            reply_obj = record_payload.get("reply")
            if isinstance(reply_obj, dict):
                root_obj = reply_obj.get("root")
                root_uri = str((root_obj or {}).get("uri", "")).strip() if isinstance(root_obj, dict) else ""
                root_cid = str((root_obj or {}).get("cid", "")).strip() if isinstance(root_obj, dict) else ""
                if root_uri and root_cid:
                    root = {"uri": root_uri, "cid": root_cid}

        status_text = (text or "").strip()
        if not status_text:
            raise BlueskyAPIError("Cannot publish empty Bluesky reply.")
        if len(status_text) > self.config.max_chars:
            raise BlueskyAPIError(
                f"Reply text length {len(status_text)} exceeds Bluesky limit {self.config.max_chars}."
            )

        payload = self._request(
            "POST",
            "/xrpc/com.atproto.repo.createRecord",
            json_body={
                "repo": self._did,
                "collection": "app.bsky.feed.post",
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": status_text,
                    "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "reply": {
                        "root": root,
                        "parent": parent,
                    },
                },
            },
        )
        if not isinstance(payload, dict):
            raise BlueskyAPIError("Unexpected createRecord payload type for reply.")

        uri = str(payload.get("uri", "")).strip()
        return {
            "uri": uri,
            "cid": str(payload.get("cid", "")).strip(),
            "url": self._public_post_url(uri),
        }

    def get_instance_max_characters(self) -> int:
        """Return configured Bluesky text limit."""
        return int(self.config.max_chars)

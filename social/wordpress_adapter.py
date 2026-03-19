"""WordPress REST API adapter for publishing Copierbot posts."""

from __future__ import annotations

from dataclasses import dataclass
import html
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


@dataclass(frozen=True)
class WordpressConfig:
    """Configuration for WordPress REST API access."""

    base_url: str
    username: str
    app_password: str
    timeout_seconds: int = 30
    default_post_status: str = "publish"


class WordpressAPIError(RuntimeError):
    """Raised for HTTP or protocol-level WordPress API failures."""


def load_wordpress_config(required: bool = False) -> WordpressConfig | None:
    """Load WordPress REST configuration from environment variables."""
    load_dotenv()
    base_url = os.getenv("WORDPRESS_BASE_URL", "").strip().rstrip("/")
    username = os.getenv("WORDPRESS_USERNAME", "").strip()
    app_password = os.getenv("WORDPRESS_APP_PASSWORD", "").strip()
    status = os.getenv("WORDPRESS_POST_STATUS", "publish").strip().lower() or "publish"
    timeout_raw = os.getenv("WORDPRESS_TIMEOUT_SECONDS", "30").strip()
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError:
        timeout_seconds = 30
    timeout_seconds = max(5, min(timeout_seconds, 180))

    if not base_url or not username or not app_password:
        if required:
            raise ValueError(
                "Missing WordPress configuration. Set WORDPRESS_BASE_URL, "
                "WORDPRESS_USERNAME, and WORDPRESS_APP_PASSWORD."
            )
        return None

    if status not in {"publish", "draft", "private", "pending"}:
        status = "publish"

    return WordpressConfig(
        base_url=base_url,
        username=username,
        app_password=app_password,
        timeout_seconds=timeout_seconds,
        default_post_status=status,
    )


def _mime_for_path(path: Path) -> str:
    """Infer MIME type for image upload."""
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _text_to_html_paragraphs(text: str) -> str:
    """Convert plain text to basic HTML paragraphs preserving blank lines."""
    blocks = [block.strip() for block in (text or "").strip().split("\n\n") if block.strip()]
    if not blocks:
        return ""
    html_blocks = []
    for block in blocks:
        escaped = html.escape(block).replace("\n", "<br />\n")
        html_blocks.append(f"<p>{escaped}</p>")
    return "\n".join(html_blocks)


class WordpressAdapter:
    """Thin wrapper around WordPress REST endpoints."""

    def __init__(self, config: WordpressConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "copierbot/1.0 (+local-cli)",
            }
        )
        self.session.auth = (config.username, config.app_password)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform a WordPress API request and return JSON payload."""
        url = f"{self.config.base_url}{path}"
        headers = dict(extra_headers or {})

        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                data=data,
                headers=headers or None,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise WordpressAPIError(f"WordPress request failed ({method} {path}): {exc}") from exc

        if response.status_code >= 400:
            message = response.text[:360]
            try:
                payload = response.json()
                message = str(payload.get("message") or payload.get("code") or message)
            except ValueError:
                pass
            raise WordpressAPIError(
                f"WordPress API error {response.status_code} ({method} {path}): {message}"
            )

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise WordpressAPIError(
                f"WordPress API returned non-JSON response ({method} {path})."
            ) from exc

    def verify_account(self) -> dict:
        """Validate credentials and return basic user fields."""
        payload = self._request("GET", "/wp-json/wp/v2/users/me")
        if not isinstance(payload, dict):
            raise WordpressAPIError("Unexpected users/me payload type.")
        return payload

    def upload_media(self, file_path: Path) -> dict:
        """Upload media and return WordPress media payload."""
        path = file_path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"WordPress media file not found: {path}")

        mime = _mime_for_path(path)
        data = path.read_bytes()
        payload = self._request(
            "POST",
            "/wp-json/wp/v2/media",
            data=data,
            extra_headers={
                "Content-Type": mime,
                "Content-Disposition": f'attachment; filename="{path.name}"',
            },
        )
        if not isinstance(payload, dict):
            raise WordpressAPIError("Unexpected media upload payload type.")
        return payload

    def create_post(
        self,
        *,
        title: str,
        content_html: str,
        publish_date: str = "",
        publish_date_gmt: str = "",
    ) -> dict:
        """Create one WordPress post and return response payload."""
        payload: dict[str, Any] = {
            "title": title,
            "content": content_html,
            "status": self.config.default_post_status,
        }
        if publish_date:
            payload["date"] = publish_date
        if publish_date_gmt:
            payload["date_gmt"] = publish_date_gmt

        result = self._request(
            "POST",
            "/wp-json/wp/v2/posts",
            json_body=payload,
        )
        if not isinstance(result, dict):
            raise WordpressAPIError("Unexpected posts payload type.")
        return result

    def publish_post(
        self,
        *,
        title: str,
        body_text: str,
        image_path: Path | None = None,
        publish_date: str = "",
        publish_date_gmt: str = "",
    ) -> dict:
        """Publish an image-first post and return id/url."""
        media_id = 0
        image_html = ""
        if image_path is not None and image_path.exists():
            media = self.upload_media(image_path)
            media_id = int(media.get("id") or 0)
            source_url = str(media.get("source_url") or "").strip()
            if source_url:
                image_html = (
                    f'<p><img src="{html.escape(source_url, quote=True)}" alt="" /></p>'
                )

        body_html = _text_to_html_paragraphs(body_text)
        content_html = "\n".join(part for part in [image_html, body_html] if part)
        if not content_html:
            content_html = "<p></p>"

        created = self.create_post(
            title=title,
            content_html=content_html,
            publish_date=publish_date,
            publish_date_gmt=publish_date_gmt,
        )

        post_id = int(created.get("id") or 0)
        post_url = str(created.get("link") or "").strip()
        if post_id <= 0:
            raise WordpressAPIError("WordPress post created but response missing id.")

        return {
            "id": post_id,
            "url": post_url,
        }

    def list_comments(
        self,
        *,
        per_page: int = 50,
        page: int = 1,
        order: str = "desc",
    ) -> list[dict]:
        """List WordPress comments in requested order."""
        params = {
            "per_page": max(1, min(per_page, 100)),
            "page": max(1, int(page)),
            "order": "asc" if str(order).lower() == "asc" else "desc",
            "orderby": "date_gmt",
            "status": "approve",
            "type": "comment",
        }
        payload = self._request(
            "GET",
            "/wp-json/wp/v2/comments",
            params=params,
        )
        if not isinstance(payload, list):
            raise WordpressAPIError("Unexpected comments payload type.")
        return [item for item in payload if isinstance(item, dict)]

    def get_comment(self, comment_id: int) -> dict:
        """Fetch one WordPress comment by id."""
        payload = self._request("GET", f"/wp-json/wp/v2/comments/{int(comment_id)}")
        if not isinstance(payload, dict):
            raise WordpressAPIError("Unexpected comment payload type.")
        return payload

    def reply_to_comment(self, *, post_id: int, parent_comment_id: int, text: str) -> dict:
        """Create a threaded reply to a WordPress comment."""
        content_html = _text_to_html_paragraphs(text)
        if not content_html:
            raise WordpressAPIError("Cannot publish empty WordPress comment reply.")

        payload = self._request(
            "POST",
            "/wp-json/wp/v2/comments",
            json_body={
                "post": int(post_id),
                "parent": int(parent_comment_id),
                "content": content_html,
            },
        )
        if not isinstance(payload, dict):
            raise WordpressAPIError("Unexpected comment create payload type.")
        return payload

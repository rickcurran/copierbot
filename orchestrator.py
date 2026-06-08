"""Publish orchestration for Copierbot social posting."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alerts import send_slack_alert
from social.bluesky_adapter import BlueskyAdapter, BlueskyAPIError, load_bluesky_config
from social.instagram_adapter import (
    clear_instagram_token_alert_state,
    InstagramAdapter,
    InstagramAPIError,
    instagram_token_error_guidance,
    instagram_token_expiry_warning,
    is_instagram_token_error_message,
    load_instagram_config,
    should_send_instagram_token_alert,
)
from social_image import build_social_composite_image
from social.mastodon_adapter import MastodonAdapter, MastodonAPIError, load_mastodon_config
from social_posting import append_ai_disclosure
from social.wordpress_adapter import WordpressAdapter, WordpressAPIError, load_wordpress_config
from storage import (
    create_post_job,
    find_post_job_by_idempotency_key,
    init_storage,
    record_published_post,
    update_post_job_status,
    upsert_post_artifacts,
)
from system_log_card import card_path_for_system_log, render_system_log_card


OUTPUT_DIR = Path("output")
RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?$")
RUN_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})(?:-\d+)?$")
PUBLISH_PLATFORM_ORDER = ("mastodon", "bluesky", "wordpress", "instagram")


def _setup_logging() -> None:
    """Configure concise CLI logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _send_publish_failure_alert(
    *,
    run_dir: Path | None,
    requested_platforms: list[str],
    failed_platform: str = "",
    post_type: str = "",
    error: str,
) -> None:
    """Send a Slack alert for a publish orchestration failure."""
    if run_dir is None:
        run_dir_label = "N/A"
    else:
        run_dir_label = str(run_dir)

    message_lines = [
        f"Requested platforms: `{','.join(requested_platforms) or 'unknown'}`",
        f"Run folder: `{run_dir_label}`",
    ]
    if failed_platform:
        message_lines.append(f"Failed platform: `{failed_platform}`")
    if post_type:
        message_lines.append(f"Post type: `{post_type}`")
    message_lines.append(f"Error: {error.strip() or 'unknown error'}")
    if failed_platform == "instagram" and is_instagram_token_error_message(error):
        if not should_send_instagram_token_alert(context="publishing", error_text=error):
            return
        message_lines.append(f"Action: {instagram_token_error_guidance(error)}")
    send_slack_alert(title="Copierbot publish failed", message="\n".join(message_lines))


def _latest_run_dir(output_dir: Path = OUTPUT_DIR) -> Path:
    """Return the most recent timestamped run folder."""
    candidates = [
        path
        for path in output_dir.iterdir()
        if path.is_dir() and bool(RUN_DIR_RE.match(path.name))
    ]
    if not candidates:
        raise RuntimeError(f"No output run folders found in {output_dir}.")
    return sorted(candidates, key=lambda p: p.name)[-1]


def _pick_single_file(run_dir: Path, pattern: str) -> Path | None:
    """Return the newest file matching a glob pattern from run folder."""
    matches = sorted(run_dir.glob(pattern))
    return matches[-1] if matches else None


def _pick_image_file(run_dir: Path) -> Path | None:
    """Return image artifact from run folder, preferring jpg/jpeg over png."""
    for pattern in ("image  *.jpg", "image  *.jpeg", "image  *.png"):
        path = _pick_single_file(run_dir, pattern)
        if path is not None:
            return path
    return None


def _pick_system_log_card_file(run_dir: Path) -> Path | None:
    """Return rendered system-log card from run folder when present."""
    for pattern in ("system_log_card  *.png", "system_log_card  *.jpg", "system_log_card  *.jpeg"):
        path = _pick_single_file(run_dir, pattern)
        if path is not None:
            return path
    return None


def _image_mime_type(path: Path) -> str:
    """Infer image MIME type from file extension."""
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _social_upload_image_or_original(image_path: Path) -> Path:
    """Return composited social image when possible, else original image."""
    try:
        return build_social_composite_image(image_path)
    except Exception as exc:
        logging.warning("Failed to build social composite image, using original: %s", exc)
        return image_path


def _read_text_file(path: Path) -> str:
    """Read text file contents with utf-8 and normalized whitespace."""
    return path.read_text(encoding="utf-8").strip()


def _extract_title_from_prompt(prompt_text: str) -> str:
    """Extract generated title from prompt metadata text."""
    for line in prompt_text.splitlines():
        if line.lower().startswith("title:"):
            return line.split(":", 1)[1].strip()
    return ""


def _strip_title_from_caption(caption_text: str, title: str) -> str:
    """Strip leading title block from caption text when present."""
    if not title:
        return caption_text.strip()

    normalized = caption_text.strip()
    title_block = f"{title}\n\n"
    if normalized.startswith(title_block):
        return normalized[len(title_block) :].strip()

    # Fallback: if first non-empty line exactly matches title, drop it.
    lines = normalized.splitlines()
    if lines and lines[0].strip() == title:
        remainder = "\n".join(lines[1:]).lstrip()
        return remainder.strip()

    return normalized


def _first_nonempty_line(text: str) -> str:
    """Return first non-empty line from text."""
    for line in (text or "").splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def _pick_instagram_media_file(run_dir: Path, post_type: str) -> Path:
    """Return the media file to host for Instagram publishing."""
    if post_type == "system_log":
        existing_card = _pick_system_log_card_file(run_dir)
        if existing_card is not None:
            return existing_card

        system_log_path = _pick_single_file(run_dir, "system_log  *.txt")
        if system_log_path is None:
            raise RuntimeError("System log post selected but no system_log file found.")

        output_path = card_path_for_system_log(system_log_path)
        render_system_log_card(
            system_log_text=_read_text_file(system_log_path),
            output_path=output_path,
        )
        return output_path

    image_path = _pick_image_file(run_dir)
    if image_path is None:
        raise RuntimeError("Instagram publish requires an image file for news posts.")
    return image_path


def _safe_title_for_wordpress(title: str) -> str:
    """Normalize title for WordPress posts."""
    value = " ".join((title or "").replace("\r", " ").replace("\n", " ").split())
    value = value.replace("[", "(").replace("]", ")")
    return value[:180].strip() or "Copierbot Post"


def _wordpress_date_fields_from_run_dir(run_dir: Path) -> tuple[str, str] | None:
    """Build WordPress date/date_gmt fields from timestamped run folder name."""
    match = RUN_TS_RE.match(run_dir.name)
    if not match:
        return None
    timestamp = match.group(1)
    try:
        naive_local = datetime.strptime(timestamp, "%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return None
    local_tz = _resolve_wordpress_publish_timezone()
    if local_tz is None:
        return None
    local_dt = naive_local.replace(tzinfo=local_tz)
    gmt_dt = local_dt.astimezone(timezone.utc)
    return (
        local_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        gmt_dt.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _resolve_wordpress_publish_timezone():
    """Return the timezone used to interpret run folder timestamps for WordPress."""
    configured_name = os.getenv("WORDPRESS_SITE_TIMEZONE", "").strip()
    timezone_name = configured_name or _detect_system_timezone_name()
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logging.warning(
                "Ignoring invalid WORDPRESS_SITE_TIMEZONE=%r; falling back to system offset.",
                timezone_name,
            )

    return datetime.now().astimezone().tzinfo


def _detect_system_timezone_name() -> str:
    """Best-effort lookup of the host IANA timezone name."""
    try:
        target = Path("/etc/localtime").resolve()
    except OSError:
        return ""

    marker = "zoneinfo/"
    resolved = str(target)
    index = resolved.find(marker)
    if index == -1:
        return ""
    return resolved[index + len(marker) :].strip()


def _detect_post_type(run_dir: Path) -> str:
    """Infer post type from available artifacts in run folder."""
    if _pick_single_file(run_dir, "system_log  *.txt"):
        return "system_log"
    return "news"


def _parse_platform_targets(raw: str) -> list[str]:
    """Parse platform argument into ordered unique publish targets."""
    value = (raw or "mastodon").strip().lower()
    if not value:
        value = "mastodon"
    if value == "all":
        return list(PUBLISH_PLATFORM_ORDER)

    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not parts:
        parts = ["mastodon"]

    invalid = [part for part in parts if part not in PUBLISH_PLATFORM_ORDER]
    if invalid:
        allowed = ", ".join(PUBLISH_PLATFORM_ORDER) + ", all"
        raise ValueError(
            f"Unsupported platform value(s): {', '.join(invalid)}. Allowed: {allowed}"
        )

    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return deduped


def _publish_run_directory_mastodon(
    run_dir: Path,
    adapter: MastodonAdapter,
    visibility: str = "",
) -> dict:
    """Publish one run directory to Mastodon with idempotent job tracking."""
    init_storage()
    run_dir = run_dir.resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

    post_type = _detect_post_type(run_dir)
    idempotency_key = f"publish:{run_dir.name}"
    existing_job = find_post_job_by_idempotency_key(idempotency_key)
    if existing_job and existing_job.get("status") == "published":
        return {
            "job_id": int(existing_job["id"]),
            "status": "already_published",
            "post_type": post_type,
            "run_dir": str(run_dir),
        }

    if existing_job:
        job_id = int(existing_job["id"])
    else:
        job_id = create_post_job(
            post_type=post_type,
            status="generated",
            idempotency_key=idempotency_key,
        )

    update_post_job_status(job_id, status="publishing", error="")
    max_chars = adapter.get_instance_max_characters()

    try:
        if post_type == "system_log":
            system_log_path = _pick_single_file(run_dir, "system_log  *.txt")
            if system_log_path is None:
                raise RuntimeError("System log post selected but no system_log file found.")
            status_text = _read_text_file(system_log_path)
            publish_text = append_ai_disclosure(status_text)
            if len(publish_text) > max_chars:
                raise RuntimeError(
                    f"Status text length {len(publish_text)} exceeds Mastodon limit {max_chars}."
                )
            status_payload = adapter.publish_status(
                status=publish_text,
                visibility=visibility,
                idempotency_key=idempotency_key,
            )
            upsert_post_artifacts(
                job_id=job_id,
                system_log_path=str(system_log_path),
                caption=status_text,
            )
        else:
            caption_path = _pick_single_file(run_dir, "caption  *.txt")
            image_path = _pick_image_file(run_dir)
            prompt_path = _pick_single_file(run_dir, "prompt  *.txt")

            if caption_path is None:
                raise RuntimeError("News post selected but no caption file found.")

            caption_text = _read_text_file(caption_path)
            prompt_text = _read_text_file(prompt_path) if prompt_path else ""
            generated_title = _extract_title_from_prompt(prompt_text)
            status_text = _strip_title_from_caption(caption_text, generated_title)
            publish_text = append_ai_disclosure(status_text)
            if len(publish_text) > max_chars:
                raise RuntimeError(
                    f"Status text length {len(publish_text)} exceeds Mastodon limit {max_chars}."
                )
            media_ids: list[str] = []
            if image_path is not None:
                upload_image_path = _social_upload_image_or_original(image_path)
                media = adapter.upload_media(
                    file_path=upload_image_path,
                    description="Copierbot surreal collage artwork",
                    mime_type=_image_mime_type(upload_image_path),
                )
                media_id = str(media.get("id", "")).strip()
                if media_id:
                    media_ids.append(media_id)

            status_payload = adapter.publish_status(
                status=publish_text,
                media_ids=media_ids or None,
                visibility=visibility,
                idempotency_key=idempotency_key,
            )
            upsert_post_artifacts(
                job_id=job_id,
                caption=caption_text,
                prompt=prompt_text,
                image_path=str(image_path) if image_path else "",
            )

        remote_post_id = str(status_payload.get("id", "")).strip()
        remote_url = str(status_payload.get("url", "")).strip()
        if not remote_post_id:
            raise RuntimeError("Mastodon publish succeeded but returned no post id.")

        record_published_post(
            job_id=job_id,
            platform="mastodon",
            remote_post_id=remote_post_id,
            remote_url=remote_url,
        )
        update_post_job_status(job_id, status="published", error="")

        return {
            "job_id": job_id,
            "status": "published",
            "post_type": post_type,
            "run_dir": str(run_dir),
            "remote_post_id": remote_post_id,
            "remote_url": remote_url,
        }
    except (RuntimeError, MastodonAPIError, FileNotFoundError) as exc:
        update_post_job_status(job_id, status="failed", error=str(exc))
        raise


def _publish_run_directory_bluesky(run_dir: Path, adapter: BlueskyAdapter) -> dict:
    """Publish one run directory to Bluesky with idempotent job tracking."""
    init_storage()
    run_dir = run_dir.resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

    post_type = _detect_post_type(run_dir)
    idempotency_key = f"publish:bluesky:{run_dir.name}"
    existing_job = find_post_job_by_idempotency_key(idempotency_key)
    if existing_job and existing_job.get("status") == "published":
        return {
            "job_id": int(existing_job["id"]),
            "status": "already_published",
            "post_type": post_type,
            "run_dir": str(run_dir),
            "platform": "bluesky",
        }

    if existing_job:
        job_id = int(existing_job["id"])
    else:
        job_id = create_post_job(
            post_type=post_type,
            status="generated",
            idempotency_key=idempotency_key,
        )

    update_post_job_status(job_id, status="publishing", error="")
    max_chars = adapter.get_instance_max_characters()

    try:
        if post_type == "system_log":
            system_log_path = _pick_single_file(run_dir, "system_log  *.txt")
            if system_log_path is None:
                raise RuntimeError("System log post selected but no system_log file found.")
            status_text = _read_text_file(system_log_path)
            publish_text = append_ai_disclosure(status_text)
            if len(publish_text) > max_chars:
                raise RuntimeError(
                    f"Status text length {len(publish_text)} exceeds Bluesky limit {max_chars}."
                )
            status_payload = adapter.publish_post(text=publish_text)
            upsert_post_artifacts(
                job_id=job_id,
                system_log_path=str(system_log_path),
                caption=status_text,
            )
        else:
            caption_path = _pick_single_file(run_dir, "caption  *.txt")
            image_path = _pick_image_file(run_dir)
            prompt_path = _pick_single_file(run_dir, "prompt  *.txt")

            if caption_path is None:
                raise RuntimeError("News post selected but no caption file found.")

            caption_text = _read_text_file(caption_path)
            prompt_text = _read_text_file(prompt_path) if prompt_path else ""
            generated_title = _extract_title_from_prompt(prompt_text)
            status_text = _strip_title_from_caption(caption_text, generated_title)
            publish_text = append_ai_disclosure(status_text)
            if len(publish_text) > max_chars:
                raise RuntimeError(
                    f"Status text length {len(publish_text)} exceeds Bluesky limit {max_chars}."
                )

            status_payload = adapter.publish_post(
                text=publish_text,
                image_path=_social_upload_image_or_original(image_path) if image_path is not None else None,
                image_alt="Copierbot surreal collage artwork",
            )
            upsert_post_artifacts(
                job_id=job_id,
                caption=caption_text,
                prompt=prompt_text,
                image_path=str(image_path) if image_path else "",
            )

        remote_post_id = str(status_payload.get("uri", "")).strip()
        remote_url = str(status_payload.get("url", "")).strip()
        if not remote_post_id:
            raise RuntimeError("Bluesky publish succeeded but returned no post uri.")

        record_published_post(
            job_id=job_id,
            platform="bluesky",
            remote_post_id=remote_post_id,
            remote_url=remote_url,
        )
        update_post_job_status(job_id, status="published", error="")

        return {
            "job_id": job_id,
            "status": "published",
            "post_type": post_type,
            "run_dir": str(run_dir),
            "remote_post_id": remote_post_id,
            "remote_url": remote_url,
            "platform": "bluesky",
        }
    except (RuntimeError, BlueskyAPIError, FileNotFoundError) as exc:
        update_post_job_status(job_id, status="failed", error=str(exc))
        raise


def _publish_run_directory_wordpress(run_dir: Path, adapter: WordpressAdapter) -> dict:
    """Publish one run directory to WordPress REST API."""
    init_storage()
    run_dir = run_dir.resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

    post_type = _detect_post_type(run_dir)
    idempotency_key = f"publish:wordpress:{run_dir.name}"
    existing_job = find_post_job_by_idempotency_key(idempotency_key)
    if existing_job and existing_job.get("status") == "published":
        return {
            "job_id": int(existing_job["id"]),
            "status": "already_published",
            "post_type": post_type,
            "run_dir": str(run_dir),
            "platform": "wordpress",
        }

    if existing_job:
        job_id = int(existing_job["id"])
    else:
        job_id = create_post_job(
            post_type=post_type,
            status="generated",
            idempotency_key=idempotency_key,
        )

    update_post_job_status(job_id, status="publishing", error="")
    date_fields = _wordpress_date_fields_from_run_dir(run_dir)
    publish_date = date_fields[0] if date_fields else ""
    publish_date_gmt = date_fields[1] if date_fields else ""

    try:
        if post_type == "system_log":
            system_log_path = _pick_single_file(run_dir, "system_log  *.txt")
            if system_log_path is None:
                raise RuntimeError("System log post selected but no system_log file found.")
            system_log_text = _read_text_file(system_log_path)
            title = "SYSTEM LOG"
            post_result = adapter.publish_post(
                title=title,
                body_text=system_log_text,
                image_path=None,
                publish_date=publish_date,
                publish_date_gmt=publish_date_gmt,
            )
            upsert_post_artifacts(
                job_id=job_id,
                title=title,
                caption=system_log_text,
                system_log_path=str(system_log_path),
            )
        else:
            caption_path = _pick_single_file(run_dir, "caption  *.txt")
            image_path = _pick_image_file(run_dir)
            prompt_path = _pick_single_file(run_dir, "prompt  *.txt")

            if caption_path is None:
                raise RuntimeError("News post selected but no caption file found.")

            caption_text = _read_text_file(caption_path)
            prompt_text = _read_text_file(prompt_path) if prompt_path else ""
            generated_title = _extract_title_from_prompt(prompt_text)
            title = _safe_title_for_wordpress(
                generated_title or _first_nonempty_line(caption_text) or "Copierbot Dispatch"
            )
            caption_body = _strip_title_from_caption(caption_text, generated_title)
            if not caption_body:
                caption_body = caption_text.strip()

            # WordPress receives the original non-composited image and caption below it.
            post_result = adapter.publish_post(
                title=title,
                body_text=caption_body,
                image_path=image_path,
                publish_date=publish_date,
                publish_date_gmt=publish_date_gmt,
            )
            upsert_post_artifacts(
                job_id=job_id,
                title=title,
                caption=caption_text,
                prompt=prompt_text,
                image_path=str(image_path) if image_path else "",
            )

        remote_post_id = str(post_result.get("id", "")).strip()
        remote_url = str(post_result.get("url", "")).strip()
        if not remote_post_id:
            raise RuntimeError("WordPress publish succeeded but returned no post id.")
        record_published_post(
            job_id=job_id,
            platform="wordpress",
            remote_post_id=remote_post_id,
            remote_url=remote_url,
        )
        update_post_job_status(job_id, status="published", error="")
        return {
            "job_id": job_id,
            "status": "published",
            "post_type": post_type,
            "run_dir": str(run_dir),
            "remote_post_id": remote_post_id,
            "remote_url": remote_url,
            "platform": "wordpress",
        }
    except (RuntimeError, WordpressAPIError, FileNotFoundError) as exc:
        update_post_job_status(job_id, status="failed", error=str(exc))
        raise


def _publish_run_directory_instagram(
    run_dir: Path,
    adapter: InstagramAdapter,
    wordpress_host_adapter: WordpressAdapter,
    *,
    delete_hosted_media_after_publish: bool = False,
) -> dict:
    """Publish one run directory to Instagram using WordPress media as public hosting."""
    init_storage()
    run_dir = run_dir.resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

    post_type = _detect_post_type(run_dir)
    idempotency_key = f"publish:instagram:{run_dir.name}"
    existing_job = find_post_job_by_idempotency_key(idempotency_key)
    if existing_job and existing_job.get("status") == "published":
        return {
            "job_id": int(existing_job["id"]),
            "status": "already_published",
            "post_type": post_type,
            "run_dir": str(run_dir),
            "platform": "instagram",
        }

    if existing_job:
        job_id = int(existing_job["id"])
    else:
        job_id = create_post_job(
            post_type=post_type,
            status="generated",
            idempotency_key=idempotency_key,
        )

    update_post_job_status(job_id, status="publishing", error="")
    hosted_media_id = 0

    try:
        if post_type == "system_log":
            system_log_path = _pick_single_file(run_dir, "system_log  *.txt")
            if system_log_path is None:
                raise RuntimeError("System log post selected but no system_log file found.")
            caption_text = _read_text_file(system_log_path)
            publish_text = append_ai_disclosure(caption_text)
            media_path = _pick_instagram_media_file(run_dir, post_type)
            hosted_media_id, hosted_image_url = wordpress_host_adapter.upload_media_public_url(media_path)
            post_result = adapter.publish_image(
                image_url=hosted_image_url,
                caption=publish_text,
            )
            upsert_post_artifacts(
                job_id=job_id,
                caption=caption_text,
                system_log_path=str(system_log_path),
                image_path=str(media_path),
            )
        else:
            caption_path = _pick_single_file(run_dir, "caption  *.txt")
            prompt_path = _pick_single_file(run_dir, "prompt  *.txt")
            if caption_path is None:
                raise RuntimeError("News post selected but no caption file found.")

            caption_text = _read_text_file(caption_path)
            prompt_text = _read_text_file(prompt_path) if prompt_path else ""
            generated_title = _extract_title_from_prompt(prompt_text)
            caption_body = _strip_title_from_caption(caption_text, generated_title)
            if not caption_body:
                caption_body = caption_text.strip()
            publish_text = append_ai_disclosure(caption_body)
            media_path = _pick_instagram_media_file(run_dir, post_type)
            hosted_media_id, hosted_image_url = wordpress_host_adapter.upload_media_public_url(media_path)
            post_result = adapter.publish_image(
                image_url=hosted_image_url,
                caption=publish_text,
            )
            upsert_post_artifacts(
                job_id=job_id,
                caption=caption_text,
                prompt=prompt_text,
                image_path=str(media_path),
            )

        remote_post_id = str(post_result.get("id", "")).strip()
        remote_url = str(post_result.get("permalink", "")).strip()
        if not remote_post_id:
            raise RuntimeError("Instagram publish succeeded but returned no media id.")
        record_published_post(
            job_id=job_id,
            platform="instagram",
            remote_post_id=remote_post_id,
            remote_url=remote_url,
        )
        update_post_job_status(job_id, status="published", error="")
        return {
            "job_id": job_id,
            "status": "published",
            "post_type": post_type,
            "run_dir": str(run_dir),
            "remote_post_id": remote_post_id,
            "remote_url": remote_url,
            "platform": "instagram",
        }
    except (RuntimeError, InstagramAPIError, WordpressAPIError, FileNotFoundError) as exc:
        update_post_job_status(job_id, status="failed", error=str(exc))
        raise
    finally:
        if delete_hosted_media_after_publish and hosted_media_id > 0:
            try:
                wordpress_host_adapter.delete_media(hosted_media_id)
            except Exception as cleanup_exc:
                logging.warning(
                    "Failed to delete temporary WordPress media %s after Instagram publish: %s",
                    hosted_media_id,
                    cleanup_exc,
                )


def publish_run_directory(
    run_dir: Path,
    *,
    platform: str,
    mastodon_adapter: MastodonAdapter | None = None,
    bluesky_adapter: BlueskyAdapter | None = None,
    wordpress_adapter: WordpressAdapter | None = None,
    instagram_adapter: InstagramAdapter | None = None,
    delete_instagram_hosted_media_after_publish: bool = False,
    visibility: str = "",
) -> dict:
    """Publish one run directory to selected platform."""
    selected = (platform or "").strip().lower()
    if selected == "mastodon":
        if mastodon_adapter is None:
            raise ValueError("mastodon_adapter is required for platform='mastodon'.")
        return _publish_run_directory_mastodon(
            run_dir=run_dir,
            adapter=mastodon_adapter,
            visibility=visibility,
        )
    if selected == "bluesky":
        if bluesky_adapter is None:
            raise ValueError("bluesky_adapter is required for platform='bluesky'.")
        return _publish_run_directory_bluesky(run_dir=run_dir, adapter=bluesky_adapter)
    if selected == "wordpress":
        if wordpress_adapter is None:
            raise ValueError("wordpress_adapter is required for platform='wordpress'.")
        return _publish_run_directory_wordpress(run_dir=run_dir, adapter=wordpress_adapter)
    if selected == "instagram":
        if instagram_adapter is None:
            raise ValueError("instagram_adapter is required for platform='instagram'.")
        if wordpress_adapter is None:
            raise ValueError("wordpress_adapter is required for platform='instagram'.")
        return _publish_run_directory_instagram(
            run_dir=run_dir,
            adapter=instagram_adapter,
            wordpress_host_adapter=wordpress_adapter,
            delete_hosted_media_after_publish=delete_instagram_hosted_media_after_publish,
        )
    raise ValueError("Unsupported platform.")


def main() -> int:
    """CLI entrypoint for publish orchestration across social platforms."""
    _setup_logging()
    parser = argparse.ArgumentParser(description="Publish Copierbot output to social platforms.")
    parser.add_argument(
        "--run-dir",
        default="",
        help="Optional explicit run directory (default: latest under output/).",
    )
    parser.add_argument(
        "--platform",
        default="mastodon",
        help=(
            "Publishing destination platform(s): mastodon, bluesky, wordpress, instagram, "
            "all, or comma-separated subset (e.g. bluesky,wordpress)."
        ),
    )
    parser.add_argument(
        "--visibility",
        default="",
        help="Mastodon-only visibility override (public, unlisted, private, direct).",
    )
    args = parser.parse_args()

    run_dir: Path | None = None
    targets: list[str] = []
    current_target = ""

    try:
        run_dir = Path(args.run_dir).resolve() if args.run_dir else _latest_run_dir()
        targets = _parse_platform_targets(args.platform)

        mastodon_adapter: MastodonAdapter | None = None
        bluesky_adapter: BlueskyAdapter | None = None
        wordpress_adapter: WordpressAdapter | None = None
        instagram_adapter: InstagramAdapter | None = None

        platform_failures: list[tuple[str, str]] = []

        for target in targets:
            current_target = target
            try:
                if target == "mastodon" and mastodon_adapter is None:
                    mastodon_config = load_mastodon_config(required=True)
                    assert mastodon_config is not None
                    mastodon_adapter = MastodonAdapter(mastodon_config)
                    mastodon_account = mastodon_adapter.verify_account()
                    logging.info(
                        "Mastodon account verified: @%s",
                        mastodon_account.get("acct", "unknown"),
                    )

                if target == "bluesky" and bluesky_adapter is None:
                    bluesky_config = load_bluesky_config(required=True)
                    assert bluesky_config is not None
                    bluesky_adapter = BlueskyAdapter(bluesky_config)
                    bluesky_account = bluesky_adapter.verify_account()
                    logging.info(
                        "Bluesky account verified: @%s",
                        bluesky_account.get("handle", "unknown"),
                    )

                if target == "wordpress" and wordpress_adapter is None:
                    wordpress_config = load_wordpress_config(required=True)
                    assert wordpress_config is not None
                    wordpress_adapter = WordpressAdapter(wordpress_config)
                    wordpress_target = wordpress_adapter.verify_account()
                    logging.info(
                        "WordPress account verified: user=%s site=%s",
                        wordpress_target.get("slug", "unknown"),
                        wordpress_config.base_url,
                    )

                if target == "instagram":
                    if instagram_adapter is None:
                        instagram_config = load_instagram_config(required=True)
                        assert instagram_config is not None
                        instagram_warning = instagram_token_expiry_warning(instagram_config)
                        if instagram_warning:
                            logging.warning("Instagram token warning: %s", instagram_warning)
                        instagram_adapter = InstagramAdapter(instagram_config)
                        instagram_account = instagram_adapter.verify_account()
                        clear_instagram_token_alert_state()
                        logging.info(
                            "Instagram account verified: @%s",
                            instagram_account.get("username", "unknown"),
                        )
                    if wordpress_adapter is None:
                        wordpress_config = load_wordpress_config(required=True)
                        assert wordpress_config is not None
                        wordpress_adapter = WordpressAdapter(wordpress_config)
                        wordpress_target = wordpress_adapter.verify_account()
                        logging.info(
                            "WordPress media host verified for Instagram: user=%s site=%s",
                            wordpress_target.get("slug", "unknown"),
                            wordpress_config.base_url,
                        )

                result = publish_run_directory(
                    run_dir=run_dir,
                    platform=target,
                    mastodon_adapter=mastodon_adapter,
                    bluesky_adapter=bluesky_adapter,
                    wordpress_adapter=wordpress_adapter,
                    instagram_adapter=instagram_adapter,
                    delete_instagram_hosted_media_after_publish=(
                        target == "instagram" and "wordpress" not in targets
                    ),
                    visibility=args.visibility,
                )
                logging.info(
                    "Publish result: platform=%s status=%s post_type=%s run_dir=%s remote_url=%s",
                    target,
                    result.get("status"),
                    result.get("post_type"),
                    result.get("run_dir"),
                    result.get("remote_url", ""),
                )
            except (
                ValueError,
                RuntimeError,
                FileNotFoundError,
                MastodonAPIError,
                BlueskyAPIError,
                WordpressAPIError,
                InstagramAPIError,
            ) as exc:
                platform_failures.append((target, str(exc)))
                logging.error(
                    "Publish failed for platform=%s run_dir=%s: %s",
                    target,
                    run_dir,
                    exc,
                )
                continue

        if platform_failures:
            current_target = ",".join(platform for platform, _ in platform_failures)
            raise RuntimeError(
                "; ".join(
                    f"{platform}: {error}" for platform, error in platform_failures
                )
            )
        return 0
    except Exception as exc:
        post_type = ""
        if run_dir is not None and run_dir.exists():
            try:
                post_type = _detect_post_type(run_dir)
            except Exception:
                post_type = ""
        _send_publish_failure_alert(
            run_dir=run_dir,
            requested_platforms=targets,
            failed_platform=current_target,
            post_type=post_type,
            error=str(exc),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())

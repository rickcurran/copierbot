"""Publish orchestration for Copierbot social posting."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import re

from social.mastodon_adapter import MastodonAdapter, MastodonAPIError, load_mastodon_config
from storage import (
    create_post_job,
    find_post_job_by_idempotency_key,
    init_storage,
    record_published_post,
    update_post_job_status,
    upsert_post_artifacts,
)


OUTPUT_DIR = Path("output")
RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?$")


def _setup_logging() -> None:
    """Configure concise CLI logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


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


def _detect_post_type(run_dir: Path) -> str:
    """Infer post type from available artifacts in run folder."""
    if _pick_single_file(run_dir, "system_log  *.txt"):
        return "system_log"
    return "news"


def publish_run_directory(
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
            if len(status_text) > max_chars:
                raise RuntimeError(
                    f"Status text length {len(status_text)} exceeds Mastodon limit {max_chars}."
                )
            status_payload = adapter.publish_status(
                status=status_text,
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
            image_path = _pick_single_file(run_dir, "image  *.png")
            prompt_path = _pick_single_file(run_dir, "prompt  *.txt")

            if caption_path is None:
                raise RuntimeError("News post selected but no caption file found.")

            caption_text = _read_text_file(caption_path)
            prompt_text = _read_text_file(prompt_path) if prompt_path else ""
            generated_title = _extract_title_from_prompt(prompt_text)
            status_text = _strip_title_from_caption(caption_text, generated_title)
            if len(status_text) > max_chars:
                raise RuntimeError(
                    f"Status text length {len(status_text)} exceeds Mastodon limit {max_chars}."
                )
            media_ids: list[str] = []
            if image_path is not None:
                media = adapter.upload_media(
                    file_path=image_path,
                    description="Copierbot surreal collage artwork",
                )
                media_id = str(media.get("id", "")).strip()
                if media_id:
                    media_ids.append(media_id)

            status_payload = adapter.publish_status(
                status=status_text,
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


def main() -> int:
    """CLI entrypoint for publish orchestration."""
    _setup_logging()
    parser = argparse.ArgumentParser(description="Publish Copierbot output to Mastodon.")
    parser.add_argument(
        "--run-dir",
        default="",
        help="Optional explicit run directory (default: latest under output/).",
    )
    parser.add_argument(
        "--visibility",
        default="",
        help="Override status visibility (public, unlisted, private, direct).",
    )
    args = parser.parse_args()

    config = load_mastodon_config(required=True)
    assert config is not None
    adapter = MastodonAdapter(config)
    account = adapter.verify_account()
    logging.info("Mastodon account verified: @%s", account.get("acct", "unknown"))

    run_dir = Path(args.run_dir).resolve() if args.run_dir else _latest_run_dir()
    result = publish_run_directory(run_dir=run_dir, adapter=adapter, visibility=args.visibility)
    logging.info(
        "Publish result: status=%s, post_type=%s, run_dir=%s, remote_url=%s",
        result.get("status"),
        result.get("post_type"),
        result.get("run_dir"),
        result.get("remote_url", ""),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Manual Higgsfield video generation for an existing Copierbot run folder."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from alerts import send_slack_alert

load_dotenv()


VIDEO_PROMPT_PREFIX = """Animate from the provided reference image. Preserve the exact composition, color palette, collage materials, and character/object identities from the still image.

Video direction:
- Style: surreal torn-paper photoshop collage, mixed-media photomontage, cut-out magazine aesthetic, halftone print texture, photocopy grain.
- Motion: subtle parallax between foreground/midground/background layers; gentle flutter on torn paper edges; drifting toner dust; slight halftone shimmer.
- Camera: very slow push-in with minimal left-to-right drift.
- Tempo: dreamlike, uncanny, editorially satirical; avoid chaotic movement.
- Duration: exactly 5 seconds.
- Transition: no hard cuts; smooth continuous motion.
- End state: settle into a near-loopable final frame.

Constraints:
- Keep scene readable and coherent.
- Do not add new characters, logos, brand marks, or any readable text.
- Do not generate pseudo-text, lettering, numbers, captions, labels, subtitles, signage, UI fragments, watermark-like marks, or typographic shapes.
- If the source image contains text-like collage fragments, keep them abstract, minimized, and fully illegible rather than inventing or sharpening them.
- Do not introduce photoreal real people.
- Keep content non-violent and surreal-symbolic.

Source concept and style anchor:
"""

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?$")
API_BASE_URL = "https://platform.higgsfield.ai"
DEFAULT_VIDEO_MODEL = "higgsfield-ai/dop/lite"
DEFAULT_DURATION_SECONDS = 6
DEFAULT_POLL_SECONDS = 10
DEFAULT_TIMEOUT_SECONDS = 20 * 60


def setup_logging() -> None:
    """Configure readable console logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _require_higgsfield_client():
    """Import the official Higgsfield SDK only when this feature is used."""
    try:
        import higgsfield_client  # type: ignore
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "Missing optional dependency 'higgsfield-client'. "
            "Run `pip install -r requirements.txt` to enable video generation."
        ) from exc
    return higgsfield_client


def _resolve_run_dir(raw_value: str) -> Path:
    """Resolve and validate a timestamped run directory."""
    value = (raw_value or "").strip()
    if not value:
        raise ValueError("Missing --run-dir. Pass a timestamped folder under output/.")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = (BASE_DIR / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if candidate.parent != OUTPUT_DIR.resolve():
        raise ValueError("Run directory must be a direct child of output/.")
    if not RUN_DIR_RE.match(candidate.name):
        raise ValueError("Run directory must be a timestamped output folder.")
    if not candidate.is_dir():
        raise ValueError(f"Run directory not found: {candidate}")
    return candidate


def _find_single_run_file(run_dir: Path, prefix: str, extensions: tuple[str, ...]) -> Path:
    """Return the first file in a run directory matching prefix and extension."""
    matches = sorted(
        path
        for path in run_dir.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.suffix.lower() in extensions
    )
    if not matches:
        joined = ", ".join(extensions)
        raise FileNotFoundError(
            f"No file found in {run_dir.name} matching prefix '{prefix}' and extensions {joined}."
        )
    return matches[0]


def _extract_final_image_prompt(prompt_text: str) -> str:
    """Extract the final image prompt section from a Copierbot prompt text file."""
    marker = "Final image prompt:"
    if marker not in prompt_text:
        raise ValueError("Prompt file does not contain a 'Final image prompt:' section.")
    extracted = prompt_text.split(marker, 1)[1]
    if "\n\nASCII fallback content:" in extracted:
        extracted = extracted.split("\n\nASCII fallback content:", 1)[0]
    cleaned = extracted.strip()
    if not cleaned:
        raise ValueError("Final image prompt section is empty.")
    return cleaned


def _build_video_prompt(final_image_prompt: str) -> str:
    """Build the Higgsfield motion prompt from the saved final image prompt."""
    return f"{VIDEO_PROMPT_PREFIX}{final_image_prompt.strip()}"


def _load_higgsfield_auth_token() -> str:
    """Load Higgsfield API credentials in documented formats."""
    load_dotenv()
    combined = os.getenv("HF_KEY", "").strip()
    if combined:
        return combined
    api_key = os.getenv("HF_API_KEY", "").strip()
    api_secret = os.getenv("HF_API_SECRET", "").strip()
    if api_key and api_secret:
        return f"{api_key}:{api_secret}"
    raise ValueError(
        "Missing Higgsfield credentials. Set HF_KEY or both HF_API_KEY and HF_API_SECRET."
    )


def _video_model_id() -> str:
    """Return configured Higgsfield model identifier."""
    load_dotenv()
    return os.getenv("HIGGSFIELD_VIDEO_MODEL", DEFAULT_VIDEO_MODEL).strip() or DEFAULT_VIDEO_MODEL


def _video_duration_seconds() -> int:
    """Return configured generation duration within a safe supported range."""
    load_dotenv()
    raw = os.getenv("HIGGSFIELD_VIDEO_DURATION_SECONDS", str(DEFAULT_DURATION_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_DURATION_SECONDS
    return max(1, min(value, 12))


def _poll_interval_seconds() -> int:
    """Return queue polling interval."""
    load_dotenv()
    raw = os.getenv("HIGGSFIELD_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_POLL_SECONDS
    return max(2, min(value, 60))


def _timeout_seconds() -> int:
    """Return max wait time for the video job."""
    load_dotenv()
    raw = os.getenv("HIGGSFIELD_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(30, min(value, 60 * 60))


def _upload_reference_image(image_path: Path) -> str:
    """Upload the local reference image to Higgsfield and return a temporary URL."""
    higgsfield_client = _require_higgsfield_client()
    logging.info("Uploading reference image: %s", image_path.name)
    return str(higgsfield_client.upload_file(str(image_path)))


def _submit_video_request(image_url: str, prompt: str, duration_seconds: int) -> dict[str, Any]:
    """Submit Higgsfield video request and return queue metadata."""
    model_id = _video_model_id()
    auth_token = _load_higgsfield_auth_token()
    url = f"{API_BASE_URL}/{model_id}"
    headers = {
        "Authorization": f"Key {auth_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "image_url": image_url,
        "prompt": prompt,
        "duration": duration_seconds,
    }
    logging.info("Submitting video request to model: %s", model_id)
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = response.text.strip()
        raise RuntimeError(
            f"Higgsfield request failed ({response.status_code}): {body or exc}"
        ) from exc
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Higgsfield response: expected JSON object.")
    return data


def _poll_for_completion(status_url: str) -> dict[str, Any]:
    """Poll the Higgsfield queue until completion, failure, or timeout."""
    auth_token = _load_higgsfield_auth_token()
    headers = {
        "Authorization": f"Key {auth_token}",
        "Accept": "application/json",
    }
    poll_every = _poll_interval_seconds()
    timeout_seconds = _timeout_seconds()
    start = time.monotonic()

    while True:
        response = requests.get(status_url, headers=headers, timeout=60)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = response.text.strip()
            raise RuntimeError(
                f"Higgsfield status poll failed ({response.status_code}): {body or exc}"
            ) from exc

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected Higgsfield status response: expected JSON object.")

        status = str(data.get("status", "")).strip().lower()
        logging.info("Higgsfield status: %s", status or "unknown")
        if status == "completed":
            return data
        if status in {"failed", "nsfw", "cancelled"}:
            raise RuntimeError(f"Higgsfield video generation ended with status '{status}'.")

        elapsed = time.monotonic() - start
        if elapsed >= timeout_seconds:
            raise TimeoutError(
                f"Higgsfield video generation timed out after {timeout_seconds} seconds."
            )
        time.sleep(poll_every)


def _safe_fetch_status_snapshot(status_url: str) -> dict[str, Any] | None:
    """Best-effort status fetch for failure reporting."""
    try:
        auth_token = _load_higgsfield_auth_token()
        response = requests.get(
            status_url,
            headers={
                "Authorization": f"Key {auth_token}",
                "Accept": "application/json",
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:  # pragma: no cover - best-effort reporting only
        return None


def _download_video(video_url: str, destination: Path) -> None:
    """Download the completed Higgsfield video to the run folder."""
    logging.info("Downloading video to %s", destination.name)
    response = requests.get(video_url, stream=True, timeout=120)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = response.text.strip()
        raise RuntimeError(
            f"Failed to download Higgsfield video ({response.status_code}): {body or exc}"
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 128):
            if chunk:
                handle.write(chunk)


def _write_text(path: Path, content: str) -> None:
    """Write text to disk with trailing newline."""
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON to disk."""
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _build_versioned_output_paths(run_dir: Path, run_stamp: str) -> tuple[Path, Path, Path]:
    """Return non-overwriting output paths for prompt, result, and video files."""
    suffix = 0
    while True:
        label = "" if suffix == 0 else f"-{suffix + 1}"
        video_prompt_path = run_dir / f"video_prompt  {run_stamp}{label}.txt"
        result_json_path = run_dir / f"video_result  {run_stamp}{label}.json"
        video_path = run_dir / f"video  {run_stamp}{label}.mp4"
        if not any(path.exists() for path in (video_prompt_path, result_json_path, video_path)):
            return video_prompt_path, result_json_path, video_path
        suffix += 1


def run(run_dir_value: str) -> Path:
    """Generate a Higgsfield video for one existing Copierbot run folder."""
    run_dir = _resolve_run_dir(run_dir_value)
    prompt_file = _find_single_run_file(run_dir, "prompt  ", (".txt",))
    image_file = _find_single_run_file(run_dir, "image  ", (".jpg", ".jpeg", ".png", ".webp"))
    run_stamp = run_dir.name

    logging.info("Preparing Higgsfield video from run: %s", run_dir.name)
    prompt_text = prompt_file.read_text(encoding="utf-8")
    final_image_prompt = _extract_final_image_prompt(prompt_text)
    video_prompt = _build_video_prompt(final_image_prompt)

    video_prompt_path, result_json_path, video_path = _build_versioned_output_paths(
        run_dir, run_stamp
    )

    _write_text(video_prompt_path, video_prompt)
    logging.info("Saved video prompt to %s", video_prompt_path)

    image_url = _upload_reference_image(image_file)
    queued = _submit_video_request(
        image_url=image_url,
        prompt=video_prompt,
        duration_seconds=_video_duration_seconds(),
    )
    _write_json(result_json_path, {"queued": queued})
    logging.info("Queued request id: %s", queued.get("request_id", "unknown"))

    status_url = str(queued.get("status_url", "")).strip()
    if not status_url:
        raise RuntimeError("Higgsfield did not return a status_url for the queued request.")

    current_step = "polling"
    try:
        completed = _poll_for_completion(status_url)
        _write_json(result_json_path, {"queued": queued, "completed": completed})

        current_step = "validating_completed_response"
        video_info = completed.get("video")
        if not isinstance(video_info, dict):
            raise RuntimeError("Completed Higgsfield response did not include a video object.")
        video_url = str(video_info.get("url", "")).strip()
        if not video_url:
            raise RuntimeError("Completed Higgsfield response did not include a video URL.")

        current_step = "downloading_video"
        _download_video(video_url, video_path)
        logging.info("Saved video to %s", video_path)
        return video_path
    except Exception as exc:
        failure_payload: dict[str, Any] = {
            "error": str(exc),
            "step": current_step,
        }
        status_snapshot = _safe_fetch_status_snapshot(status_url)
        if status_snapshot:
            failure_payload["status"] = status_snapshot
        _write_json(result_json_path, {"queued": queued, "failed": failure_payload})
        status_error = ""
        if isinstance(status_snapshot, dict):
            status_error = str(status_snapshot.get("error", "")).strip()
        send_slack_alert(
            title="Copierbot video generation failed",
            message=(
                f"Run: `{run_dir.name}`\n"
                f"Model: `{_video_model_id()}`\n"
                f"Step: `{current_step}`\n"
                f"Error: `{str(exc).strip()}`"
                + (f"\nProvider detail: `{status_error}`" if status_error else "")
            ),
        )
        raise


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Generate a Higgsfield video from an existing Copierbot run folder."
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Path to an existing timestamped run folder under output/.",
    )
    return parser


def main() -> None:
    """CLI entrypoint."""
    setup_logging()
    args = build_arg_parser().parse_args()
    video_path = run(args.run_dir)
    print(f"Higgsfield video saved to {video_path}")


if __name__ == "__main__":
    main()

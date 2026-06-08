"""Local-only web dashboard for triggering Copierbot CLI commands and schedulers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlparse

from alerts import (
    classify_openai_error_text,
    is_fatal_openai_category,
    openai_category_action_text,
    send_slack_alert,
)
from persona import get_persona_state
from social.instagram_adapter import (
    instagram_token_time_remaining,
    load_instagram_config,
    should_send_instagram_token_expiry_alert,
)
from slack_control import PID_PATH as SLACK_CONTROL_PID_PATH
from storage import init_storage, list_published_posts


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
SCHED_PREFS_PATH = BASE_DIR / "data" / "dashboard_scheduler_state.json"
MAX_JOBS = 80
JOB_RENDER_LIMIT = 24
MAX_OUTPUT_CHARS = 12000
HOST = "127.0.0.1"
PORT = 8787
RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?$")
URL_RE = re.compile(r"https?://[^\s<>'\"]+")
TIME_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
JOB_REPLIED_RE = re.compile(r"\breplied=(?:[1-9]\d*)\b", re.IGNORECASE)

GENERATE_INTERVAL_HOURS = list(range(1, 25))
MENTION_INTERVAL_MINUTES = [1, 5, 10, 15, 20, 30, 60]
PUBLISH_PLATFORM_OPTIONS = ("mastodon", "bluesky", "wordpress", "instagram")
MENTION_PLATFORM_OPTIONS = ("mastodon", "bluesky", "wordpress", "instagram")
DEFAULT_ACTIVE_PUBLISH_PLATFORMS = ["mastodon"]
DEFAULT_ACTIVE_MENTION_PLATFORMS = ["mastodon", "bluesky"]
GENERATE_MISS_GRACE_SECONDS = 20 * 60
PLATFORM_DISPLAY_NAMES = {
    "mastodon": "Mastodon",
    "bluesky": "Bluesky",
    "wordpress": "WordPress",
    "instagram": "Instagram",
}
INSTAGRAM_TOKEN_WARNING_DAYS = (7, 3, 1)
ICON_PATH = BASE_DIR / "image.png"


@dataclass
class Job:
    """In-memory job state for one executed command."""

    job_id: int
    label: str
    command: list[str]
    status: str
    started_at: str
    finished_at: str = ""
    return_code: int | None = None
    output: str = ""


@dataclass
class SchedulerState:
    """State for one recurring scheduler."""

    key: str
    label: str
    interval_seconds: int
    running: bool = False
    last_run_at: str = ""
    next_run_at: str = ""
    last_result: str = ""
    thread: threading.Thread | None = None
    stop_event: threading.Event | None = None


JOBS: list[Job] = []
JOB_LOCK = threading.Lock()
NEXT_JOB_ID = 1

SCHED_LOCK = threading.Lock()
SCHEDULERS: dict[str, SchedulerState] = {
    "generate_publish": SchedulerState(
        key="generate_publish",
        label="Generate + Publish",
        interval_seconds=60 * 60,
    ),
    "mentions": SchedulerState(
        key="mentions",
        label="Mention Monitor",
        interval_seconds=5 * 60,
    ),
}
ACTIVE_PUBLISH_PLATFORMS: list[str] = list(DEFAULT_ACTIVE_PUBLISH_PLATFORMS)
ACTIVE_MENTION_PLATFORMS: list[str] = list(DEFAULT_ACTIVE_MENTION_PLATFORMS)
GENERATE_START_TIME = ""
SCHED_STOP_PREFIX = "__SCHED_STOP__:"
SLACK_CONTROL_LAST_RESULT = ""
LAST_GENERATE_MISS_ALERT_KEY = ""
GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS = False


def _validate_generate_interval_seconds(value: int) -> int:
    """Clamp generate scheduler interval to allowed hourly options."""
    hours = max(1, min(24, int(value) // 3600 if int(value) >= 3600 else int(value)))
    if hours not in GENERATE_INTERVAL_HOURS:
        hours = 1
    return hours * 3600


def _validate_mentions_interval_seconds(value: int) -> int:
    """Clamp mention scheduler interval to allowed minute options."""
    minutes = max(1, int(value) // 60 if int(value) >= 60 else int(value))
    if minutes not in MENTION_INTERVAL_MINUTES:
        minutes = 5
    return minutes * 60


def _normalize_active_publish_platforms(raw_value: object) -> list[str]:
    """Normalize persisted platform preference value."""
    values: list[str] = []
    if isinstance(raw_value, str):
        values = [part.strip().lower() for part in raw_value.split(",")]
    elif isinstance(raw_value, list):
        values = [str(part).strip().lower() for part in raw_value]

    deduped: list[str] = []
    for value in values:
        if value in PUBLISH_PLATFORM_OPTIONS and value not in deduped:
            deduped.append(value)
    return deduped or list(DEFAULT_ACTIVE_PUBLISH_PLATFORMS)


def _normalize_active_mention_platforms(raw_value: object) -> list[str]:
    """Normalize persisted mention platform preference value."""
    values: list[str] = []
    if isinstance(raw_value, str):
        values = [part.strip().lower() for part in raw_value.split(",")]
    elif isinstance(raw_value, list):
        values = [str(part).strip().lower() for part in raw_value]

    deduped: list[str] = []
    for value in values:
        if value in MENTION_PLATFORM_OPTIONS and value not in deduped:
            deduped.append(value)
    return deduped or list(DEFAULT_ACTIVE_MENTION_PLATFORMS)


def _normalize_generate_start_time(raw_value: object) -> str:
    """Normalize generate scheduler start time in HH:MM local format."""
    value = str(raw_value or "").strip()
    return value if TIME_HHMM_RE.match(value) else ""


def _platform_display_name(name: str) -> str:
    """Return user-facing platform label with canonical capitalization."""
    key = (name or "").strip().lower()
    return PLATFORM_DISPLAY_NAMES.get(key, key.title())


def _platforms_display_label(platforms: list[str]) -> str:
    """Return comma-separated user-facing platform labels."""
    return ", ".join(_platform_display_name(name) for name in platforms)


def _active_publish_platform_arg(platforms: list[str]) -> str:
    """Convert active publish platforms list to orchestrator --platform argument."""
    normalized = _normalize_active_publish_platforms(platforms)
    if len(normalized) == len(PUBLISH_PLATFORM_OPTIONS):
        return "all"
    if len(normalized) == 1:
        return normalized[0]
    return ",".join(normalized)


def _active_mention_platform_arg(platforms: list[str]) -> str:
    """Convert active mention platforms list to engage --platform argument."""
    normalized = _normalize_active_mention_platforms(platforms)
    if len(normalized) == len(MENTION_PLATFORM_OPTIONS):
        return "all"
    if len(normalized) == 1:
        return normalized[0]
    return ",".join(normalized)


def _get_active_publish_platforms() -> list[str]:
    """Return current active publish platforms (copy)."""
    with SCHED_LOCK:
        return list(ACTIVE_PUBLISH_PLATFORMS)


def _set_active_publish_platforms(platforms: list[str]) -> None:
    """Update active publish platforms and persist preferences."""
    normalized = _normalize_active_publish_platforms(platforms)
    with SCHED_LOCK:
        ACTIVE_PUBLISH_PLATFORMS.clear()
        ACTIVE_PUBLISH_PLATFORMS.extend(normalized)
        _save_scheduler_preferences()


def _get_active_mention_platforms() -> list[str]:
    """Return current active mention platforms (copy)."""
    with SCHED_LOCK:
        return list(ACTIVE_MENTION_PLATFORMS)


def _set_active_mention_platforms(platforms: list[str]) -> None:
    """Update active mention platforms and persist preferences."""
    normalized = _normalize_active_mention_platforms(platforms)
    with SCHED_LOCK:
        ACTIVE_MENTION_PLATFORMS.clear()
        ACTIVE_MENTION_PLATFORMS.extend(normalized)
        _save_scheduler_preferences()


def _get_generate_start_time() -> str:
    """Return configured generate scheduler start time in HH:MM local format."""
    with SCHED_LOCK:
        return GENERATE_START_TIME


def _set_generate_start_time(start_time: str) -> None:
    """Update generate scheduler start time and persist preferences."""
    normalized = _normalize_generate_start_time(start_time)
    with SCHED_LOCK:
        global GENERATE_START_TIME
        GENERATE_START_TIME = normalized
        _save_scheduler_preferences()


def _load_scheduler_preferences() -> dict[str, object]:
    """Load persisted scheduler interval preferences."""
    defaults = {
        "generate_publish_interval_seconds": 3600,
        "generate_start_time": "",
        "mentions_interval_seconds": 300,
        "active_publish_platforms": list(DEFAULT_ACTIVE_PUBLISH_PLATFORMS),
        "active_mention_platforms": list(DEFAULT_ACTIVE_MENTION_PLATFORMS),
        "last_generate_miss_alert_key": "",
        "generate_miss_alert_blocked_until_success": False,
    }
    try:
        raw = json.loads(SCHED_PREFS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return defaults
    except (OSError, json.JSONDecodeError):
        return defaults

    def _safe_int(value: object, fallback: int) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback

    return {
        "generate_publish_interval_seconds": _validate_generate_interval_seconds(
            _safe_int(
                raw.get(
                    "generate_publish_interval_seconds",
                    defaults["generate_publish_interval_seconds"],
                ),
                defaults["generate_publish_interval_seconds"],
            )
        ),
        "generate_start_time": _normalize_generate_start_time(
            raw.get("generate_start_time", defaults["generate_start_time"])
        ),
        "mentions_interval_seconds": _validate_mentions_interval_seconds(
            _safe_int(
                raw.get("mentions_interval_seconds", defaults["mentions_interval_seconds"]),
                defaults["mentions_interval_seconds"],
            )
        ),
        "active_publish_platforms": _normalize_active_publish_platforms(
            raw.get("active_publish_platforms", defaults["active_publish_platforms"])
        ),
        # Backward-compatible migration: old prefs used publish platforms for mentions.
        "active_mention_platforms": _normalize_active_mention_platforms(
            raw.get(
                "active_mention_platforms",
                raw.get("active_publish_platforms", defaults["active_mention_platforms"]),
            )
        ),
        "last_generate_miss_alert_key": str(
            raw.get("last_generate_miss_alert_key", defaults["last_generate_miss_alert_key"])
        ).strip(),
        "generate_miss_alert_blocked_until_success": bool(
            raw.get(
                "generate_miss_alert_blocked_until_success",
                defaults["generate_miss_alert_blocked_until_success"],
            )
        ),
    }


def _save_scheduler_preferences() -> None:
    """Persist scheduler interval preferences to disk."""
    payload = {
        "generate_publish_interval_seconds": int(SCHEDULERS["generate_publish"].interval_seconds),
        "generate_start_time": str(GENERATE_START_TIME),
        "mentions_interval_seconds": int(SCHEDULERS["mentions"].interval_seconds),
        "active_publish_platforms": list(ACTIVE_PUBLISH_PLATFORMS),
        "active_mention_platforms": list(ACTIVE_MENTION_PLATFORMS),
        "last_generate_miss_alert_key": str(LAST_GENERATE_MISS_ALERT_KEY),
        "generate_miss_alert_blocked_until_success": bool(
            GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS
        ),
    }
    SCHED_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHED_PREFS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _apply_scheduler_preferences() -> None:
    """Apply persisted preferences to in-memory scheduler defaults."""
    prefs = _load_scheduler_preferences()
    global GENERATE_START_TIME, LAST_GENERATE_MISS_ALERT_KEY, GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS
    SCHEDULERS["generate_publish"].interval_seconds = int(prefs["generate_publish_interval_seconds"])
    GENERATE_START_TIME = _normalize_generate_start_time(prefs.get("generate_start_time"))
    SCHEDULERS["mentions"].interval_seconds = int(prefs["mentions_interval_seconds"])
    LAST_GENERATE_MISS_ALERT_KEY = str(prefs.get("last_generate_miss_alert_key", "")).strip()
    GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS = bool(
        prefs.get("generate_miss_alert_blocked_until_success", False)
    )
    ACTIVE_PUBLISH_PLATFORMS.clear()
    ACTIVE_PUBLISH_PLATFORMS.extend(
        _normalize_active_publish_platforms(prefs.get("active_publish_platforms"))
    )
    ACTIVE_MENTION_PLATFORMS.clear()
    ACTIVE_MENTION_PLATFORMS.extend(
        _normalize_active_mention_platforms(prefs.get("active_mention_platforms"))
    )


_apply_scheduler_preferences()


def _now_dt() -> datetime:
    """Return current local datetime."""
    return datetime.now().astimezone()


def _now_stamp() -> str:
    """Return local timestamp for UI display."""
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


def _stamp_from_dt(value: datetime) -> str:
    """Return local timestamp string for a datetime."""
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _parse_local_stamp(value: str) -> datetime | None:
    """Parse dashboard local timestamp string."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.astimezone()


def _future_stamp(seconds: int) -> str:
    """Return local timestamp offset by given seconds."""
    return (_now_dt() + timedelta(seconds=max(0, int(seconds)))).strftime("%Y-%m-%d %H:%M:%S")


def _next_occurrence_for_time(start_time_hhmm: str, now: datetime | None = None) -> datetime:
    """Return next local datetime occurrence for HH:MM, rolling to tomorrow if passed."""
    current = now.astimezone() if now is not None else _now_dt()
    hour, minute = [int(part) for part in start_time_hhmm.split(":", 1)]
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # If selected clock time has already passed today, schedule first run tomorrow.
    if candidate < current and not (current.hour == hour and current.minute == minute):
        candidate += timedelta(days=1)
    return candidate


def _is_publishable_run_dir(path: Path) -> bool:
    """Return True for timestamped output run directories only."""
    return path.is_dir() and bool(RUN_DIR_RE.match(path.name))


def _list_publishable_run_paths() -> list[Path]:
    """List timestamped output run folders."""
    if not OUTPUT_DIR.exists():
        return []
    return [p for p in OUTPUT_DIR.iterdir() if _is_publishable_run_dir(p)]


def _list_run_dirs() -> list[str]:
    """Return publishable run directory names, newest first."""
    runs = [p.name for p in _list_publishable_run_paths()]
    return sorted(runs, reverse=True)


def _parse_run_dir_timestamp(name: str) -> datetime | None:
    """Parse local timestamp from a run directory name."""
    raw = (name or "").strip()
    if not raw:
        return None
    base = raw.split("-", 6)[:6]
    if len(base) != 6:
        return None
    stamp = "-".join(base)
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return None
    return parsed.astimezone()


def _latest_generated_run_time() -> datetime | None:
    """Return newest generated run timestamp from output folders."""
    latest: datetime | None = None
    for path in _list_publishable_run_paths():
        parsed = _parse_run_dir_timestamp(path.name)
        if parsed is None:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _parse_run_manifest(manifest_path: Path) -> list[Path]:
    """Read run manifest entries and return publishable run paths in order."""
    if not manifest_path.exists():
        return []

    result: list[Path] = []
    seen: set[str] = set()
    raw = manifest_path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        value = line.strip()
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        else:
            path = path.resolve()
        if not _is_publishable_run_dir(path):
            continue
        if path.parent.resolve() != OUTPUT_DIR.resolve():
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _latest_published_post_time(active_platforms: list[str]) -> datetime | None:
    """Return latest publish time for selected platforms, localized."""
    try:
        init_storage()
        rows = list_published_posts(platform=None, limit=80)
    except Exception:
        return None
    if not rows:
        return None

    active = set(_normalize_active_publish_platforms(active_platforms))
    latest: datetime | None = None
    for row in rows:
        platform = str(row.get("platform", "")).strip().lower()
        if platform not in active:
            continue
        raw = str(row.get("published_at", "")).strip()
        if not raw:
            continue
        try:
            parsed_local = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            ).astimezone()
        except ValueError:
            continue
        if latest is None or parsed_local > latest:
            latest = parsed_local
    return latest


def _compute_generate_initial_delay(interval_seconds: int, active_platforms: list[str]) -> int:
    """Delay first generate cycle if a publish already happened within interval."""
    latest = _latest_published_post_time(active_platforms=active_platforms)
    if latest is None:
        return 0
    elapsed = (_now_dt() - latest).total_seconds()
    if elapsed < 0:
        elapsed = 0
    if elapsed >= interval_seconds:
        return 0
    return int(math.ceil(interval_seconds - elapsed))


def _format_wait_duration(seconds: int) -> str:
    """Format wait duration for UI status text."""
    total_minutes = max(1, int(math.ceil(seconds / 60)))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _trim_output(text: str) -> str:
    """Limit stored output size while keeping newest lines."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "[...output trimmed...]\n" + text[-MAX_OUTPUT_CHARS:]


def _slack_control_pid() -> int | None:
    """Return running Slack control pid from pid file when valid."""
    try:
        raw = SLACK_CONTROL_PID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _slack_control_snapshot() -> dict[str, str | bool | int]:
    """Return immutable Slack control service state for UI rendering."""
    pid = _slack_control_pid()
    return {
        "running": bool(pid),
        "pid": pid or 0,
        "last_result": SLACK_CONTROL_LAST_RESULT,
    }


def _start_slack_control_listener() -> None:
    """Start Slack control listener unless already running."""
    global SLACK_CONTROL_LAST_RESULT
    pid = _slack_control_pid()
    if pid:
        SLACK_CONTROL_LAST_RESULT = f"already running (pid={pid})"
        return
    _enqueue_job(
        label="Start Slack Control Listener",
        command=[sys.executable, "slack_control.py"],
        background=True,
    )
    SLACK_CONTROL_LAST_RESULT = "launch requested"


def _stop_slack_control_listener() -> None:
    """Stop Slack control listener if it is running."""
    global SLACK_CONTROL_LAST_RESULT
    pid = _slack_control_pid()
    if not pid:
        SLACK_CONTROL_LAST_RESULT = "already stopped"
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        SLACK_CONTROL_LAST_RESULT = f"stop failed: {exc}"
        return
    SLACK_CONTROL_LAST_RESULT = f"stop requested (pid={pid})"


def _create_job(label: str, command: list[str]) -> Job:
    """Create and register job row in memory."""
    global NEXT_JOB_ID
    with JOB_LOCK:
        job = Job(
            job_id=NEXT_JOB_ID,
            label=label,
            command=command,
            status="queued",
            started_at=_now_stamp(),
        )
        NEXT_JOB_ID += 1
        JOBS.append(job)
        if len(JOBS) > MAX_JOBS:
            del JOBS[:-MAX_JOBS]
    return job


def _run_job(job: Job) -> None:
    """Execute job command and stream combined output into in-memory job state."""
    with JOB_LOCK:
        job.status = "running"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(
            job.command,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        output_parts: list[str] = []
        if proc.stdout is not None:
            for line in proc.stdout:
                output_parts.append(line)
                with JOB_LOCK:
                    job.output = _trim_output("".join(output_parts))
        return_code = proc.wait()
        output = "".join(output_parts)
        status = "succeeded" if return_code == 0 else "failed"
    except Exception as exc:
        output = f"Failed to execute command: {exc}"
        status = "failed"
        return_code = None

    with JOB_LOCK:
        job.output = _trim_output(output)
        job.status = status
        job.return_code = return_code
        job.finished_at = _now_stamp()


def _enqueue_job(label: str, command: list[str], background: bool = True) -> Job:
    """Queue command as tracked job; optionally run synchronously."""
    job = _create_job(label=label, command=command)
    if background:
        thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
        thread.start()
    else:
        _run_job(job)
    return job


def _build_actions() -> dict[str, tuple[str, Callable[[dict[str, str]], list[str] | None]]]:
    """Map form action keys to manual command builders."""

    def _cmd_generate(_: dict[str, str]) -> list[str]:
        return [sys.executable, "main.py"]

    def _cmd_generate_from_url(payload: dict[str, str]) -> list[str] | None:
        article_url = (payload.get("article_url") or "").strip()
        if not article_url:
            return None
        if not URL_RE.fullmatch(article_url):
            return None
        return [sys.executable, "main.py", "--article-url", article_url]

    def _cmd_publish_latest(_: dict[str, str]) -> list[str]:
        platform_arg = _active_publish_platform_arg(_get_active_publish_platforms())
        return [sys.executable, "orchestrator.py", "--platform", platform_arg]

    def _cmd_publish_selected(payload: dict[str, str]) -> list[str] | None:
        run_dir = (payload.get("run_dir") or "").strip()
        if not run_dir:
            return None
        if not RUN_DIR_RE.match(run_dir):
            return None
        platform_arg = _active_publish_platform_arg(_get_active_publish_platforms())
        return [
            sys.executable,
            "orchestrator.py",
            "--run-dir",
            f"output/{run_dir}",
            "--platform",
            platform_arg,
        ]

    def _cmd_generate_video_selected(payload: dict[str, str]) -> list[str] | None:
        run_dir = (payload.get("run_dir") or "").strip()
        if not run_dir:
            return None
        if not RUN_DIR_RE.match(run_dir):
            return None
        return [
            sys.executable,
            "generate_video.py",
            "--run-dir",
            f"output/{run_dir}",
        ]

    def _cmd_mentions(_: dict[str, str]) -> list[str]:
        platform_arg = _active_mention_platform_arg(_get_active_mention_platforms())
        return [sys.executable, "engage.py", "--platform", platform_arg]

    def _cmd_mentions_mastodon(_: dict[str, str]) -> list[str]:
        return [sys.executable, "engage.py", "--platform", "mastodon"]

    def _cmd_mentions_bluesky(_: dict[str, str]) -> list[str]:
        return [sys.executable, "engage.py", "--platform", "bluesky"]

    def _cmd_mentions_wordpress(_: dict[str, str]) -> list[str]:
        return [sys.executable, "engage.py", "--platform", "wordpress"]

    def _cmd_mentions_instagram(_: dict[str, str]) -> list[str]:
        return [sys.executable, "engage.py", "--platform", "instagram"]

    return {
        "generate": ("Generate Post", _cmd_generate),
        "generate_from_url": ("Generate Post From URL", _cmd_generate_from_url),
        "publish_latest": ("Publish Latest Run", _cmd_publish_latest),
        "publish_selected": ("Publish Selected Run", _cmd_publish_selected),
        "generate_video_selected": ("Generate Video", _cmd_generate_video_selected),
        "check_mentions": ("Check Mentions (All)", _cmd_mentions),
        "check_mentions_mastodon": ("Check Mentions (Mastodon)", _cmd_mentions_mastodon),
        "check_mentions_bluesky": ("Check Mentions (Bluesky)", _cmd_mentions_bluesky),
        "check_mentions_wordpress": ("Check Mentions (WordPress)", _cmd_mentions_wordpress),
        "check_mentions_instagram": ("Check Mentions (Instagram)", _cmd_mentions_instagram),
    }


def _run_generate_publish_cycle() -> str:
    """Run one scheduled cycle: generate post(s) then publish new runs in order."""
    global GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS
    manifest_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="copierbot-run-manifest-",
        delete=False,
    )
    manifest_path = Path(manifest_file.name)
    manifest_file.close()

    gen_job = _enqueue_job(
        label="Scheduled Generate Post",
        command=[sys.executable, "main.py", "--run-manifest", str(manifest_path)],
        background=False,
    )
    try:
        if gen_job.status != "succeeded":
            category = classify_openai_error_text(gen_job.output or "")
            action_text = openai_category_action_text(category)
            output_excerpt = " ".join((gen_job.output or "").strip().split())[-600:] or "N/A"
            base_message = (
                f"main.py failed (rc={gen_job.return_code}, "
                f"openai_category={category})."
            )
            send_slack_alert(
                title="Copierbot scheduled generation failed",
                message=(
                    f"Job: `Scheduled Generate Post`\n"
                    f"Category: `{category}`\n"
                    f"Action: {action_text}\n"
                    f"Return code: `{gen_job.return_code}`\n"
                    f"Output excerpt: `{output_excerpt}`"
                ),
            )
            if is_fatal_openai_category(category):
                send_slack_alert(
                    title="Copierbot scheduler auto-stopped",
                    message=(
                        "Generate + Publish halted due to fatal OpenAI error category.\n"
                        f"Category: `{category}`\n"
                        f"Job: `Scheduled Generate Post`\n"
                        f"Action: {action_text}\n"
                        "Scheduler state: stopped until manually restarted."
                    ),
                )
                return f"{SCHED_STOP_PREFIX}{base_message} Scheduler auto-stopped."
            return base_message

        created = _parse_run_manifest(manifest_path)
        if not created:
            send_slack_alert(
                title="Copierbot scheduled generation produced no run",
                message=(
                    "main.py reported success, but the generate scheduler did not receive any "
                    "run-manifest entries to publish.\n"
                    "Action: check generation output and scheduler state."
                ),
            )
            return "main.py succeeded; no run-manifest entries found."

        active_platforms = _get_active_publish_platforms()
        platform_arg = _active_publish_platform_arg(active_platforms)
        published = 0
        publish_failed = 0
        for run_path in created:
            pub_job = _enqueue_job(
                label=f"Scheduled Publish {run_path.name} [{platform_arg}]",
                command=[
                    sys.executable,
                    "orchestrator.py",
                    "--run-dir",
                    f"output/{run_path.name}",
                    "--platform",
                    platform_arg,
                ],
                background=False,
            )
            if pub_job.status == "succeeded":
                published += 1
            else:
                publish_failed += 1

        with SCHED_LOCK:
            GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS = False
            _save_scheduler_preferences()

        return (
            f"generated_runs={len(created)} platform={platform_arg} "
            f"published={published} publish_failed={publish_failed}."
        )
    finally:
        try:
            manifest_path.unlink(missing_ok=True)
        except Exception:
            pass


def _run_mention_cycle() -> str:
    """Run one scheduled mention-monitor cycle."""
    platform_arg = _active_mention_platform_arg(_get_active_mention_platforms())
    job = _enqueue_job(
        label="Scheduled Mention Check",
        command=[sys.executable, "engage.py", "--platform", platform_arg],
        background=False,
    )
    if job.status == "succeeded":
        return f"engage.py completed successfully (platform={platform_arg})."
    return f"engage.py failed (platform={platform_arg}, rc={job.return_code})."


def _start_generate_scheduler_from_settings(
    hours: int | None = None, start_time_raw: str | None = None
) -> None:
    """Start generate scheduler using explicit values or persisted settings."""
    if hours is None:
        hours = max(1, int(SCHEDULERS["generate_publish"].interval_seconds) // 3600)
    if hours not in GENERATE_INTERVAL_HOURS:
        hours = 1
    interval_seconds = hours * 60 * 60

    start_time = _normalize_generate_start_time(start_time_raw or "")
    if not start_time:
        persisted = _normalize_generate_start_time(_get_generate_start_time())
        start_time = persisted or _now_dt().strftime("%H:%M")
    _set_generate_start_time(start_time)

    now_local = _now_dt()
    first_run_local = _next_occurrence_for_time(start_time, now=now_local)
    initial_delay_seconds = int(
        max(0, math.ceil((first_run_local - now_local).total_seconds()))
    )
    queued_message = (
        f"queued for {_stamp_from_dt(first_run_local)} "
        f"(local); then every {hours}h."
    )
    _start_scheduler(
        key="generate_publish",
        interval_seconds=interval_seconds,
        runner=_run_generate_publish_cycle,
        initial_delay_seconds=initial_delay_seconds,
        queued_message=queued_message,
        initial_next_run_at=_stamp_from_dt(first_run_local),
    )


def _start_mentions_scheduler_from_settings(minutes: int | None = None) -> None:
    """Start mention scheduler using explicit values or persisted settings."""
    if minutes is None:
        minutes = max(1, int(SCHEDULERS["mentions"].interval_seconds) // 60)
    if minutes not in MENTION_INTERVAL_MINUTES:
        minutes = 5
    _start_scheduler(
        key="mentions",
        interval_seconds=minutes * 60,
        runner=_run_mention_cycle,
    )


def _scheduler_loop(
    state: SchedulerState,
    runner: Callable[[], str],
    stop_event: threading.Event,
    initial_delay_seconds: int = 0,
) -> None:
    """Background loop for recurring scheduler."""
    initial_delay = max(0, int(initial_delay_seconds))
    if initial_delay > 0 and stop_event.wait(initial_delay):
        with SCHED_LOCK:
            state.running = False
            state.next_run_at = ""
            state.thread = None
            state.stop_event = None
        return

    while not stop_event.is_set():
        with SCHED_LOCK:
            scheduled_run_at = _parse_local_stamp(state.next_run_at) or _now_dt()

        with SCHED_LOCK:
            state.last_run_at = _now_stamp()
            state.last_result = "running..."
            state.next_run_at = ""

        try:
            result = runner()
        except Exception as exc:  # defensive, runner already handles subprocess statuses
            result = f"runner error: {exc}"

        stop_requested = False
        if isinstance(result, str) and result.startswith(SCHED_STOP_PREFIX):
            stop_requested = True
            result = result[len(SCHED_STOP_PREFIX) :].strip()

        with SCHED_LOCK:
            if not state.running:
                break
            state.last_result = result
            if stop_requested:
                state.running = False
                state.next_run_at = ""
            else:
                next_run_dt = scheduled_run_at + timedelta(seconds=state.interval_seconds)
                now_local = _now_dt()
                while next_run_dt <= now_local:
                    next_run_dt += timedelta(seconds=state.interval_seconds)
                state.next_run_at = _stamp_from_dt(next_run_dt)

        if stop_requested:
            break

        next_run_dt = _parse_local_stamp(state.next_run_at)
        if next_run_dt is None:
            wait_seconds = state.interval_seconds
        else:
            wait_seconds = max(
                0,
                int(math.ceil((next_run_dt - _now_dt()).total_seconds())),
            )

        if stop_event.wait(wait_seconds):
            break

    with SCHED_LOCK:
        state.running = False
        state.next_run_at = ""
        state.thread = None
        state.stop_event = None


def _stop_scheduler(key: str) -> None:
    """Stop a running scheduler by key."""
    with SCHED_LOCK:
        state = SCHEDULERS[key]
        event = state.stop_event
        thread = state.thread
        state.running = False
        state.next_run_at = ""

    if event is not None:
        event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)

    with SCHED_LOCK:
        state = SCHEDULERS[key]
        state.thread = None
        state.stop_event = None


def _start_scheduler(
    key: str,
    interval_seconds: int,
    runner: Callable[[], str],
    initial_delay_seconds: int = 0,
    queued_message: str = "queued",
    initial_next_run_at: str = "",
) -> None:
    """Start scheduler with given interval, restarting if already running."""
    _stop_scheduler(key)

    state = SCHEDULERS[key]
    stop_event = threading.Event()
    delay = max(0, int(initial_delay_seconds))
    thread = threading.Thread(
        target=_scheduler_loop,
        args=(state, runner, stop_event, delay),
        daemon=True,
        name=f"copierbot-scheduler-{key}",
    )

    with SCHED_LOCK:
        state.interval_seconds = max(1, int(interval_seconds))
        _save_scheduler_preferences()
        state.running = True
        state.last_result = queued_message
        if initial_next_run_at:
            state.next_run_at = initial_next_run_at
        else:
            state.next_run_at = _future_stamp(delay) if delay > 0 else _now_stamp()
        state.stop_event = stop_event
        state.thread = thread

    thread.start()


def _scheduler_snapshot() -> dict[str, dict[str, str | int | bool]]:
    """Return immutable scheduler state for UI rendering."""
    with SCHED_LOCK:
        snap: dict[str, dict[str, str | int | bool]] = {}
        for key, state in SCHEDULERS.items():
            snap[key] = {
                "label": state.label,
                "interval_seconds": state.interval_seconds,
                "running": state.running,
                "last_run_at": state.last_run_at,
                "next_run_at": state.next_run_at,
                "last_result": state.last_result,
            }
    return snap


def _send_generate_miss_alert(expected_run_dt: datetime, last_seen: str, context: str) -> None:
    """Send one Slack alert for a missed scheduled generate run."""
    global LAST_GENERATE_MISS_ALERT_KEY, GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS
    alert_key = expected_run_dt.strftime("%Y-%m-%d %H:%M:%S")
    if GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS:
        return
    if LAST_GENERATE_MISS_ALERT_KEY == alert_key:
        return
    active_platforms = _platforms_display_label(_get_active_publish_platforms())
    alert_sent = send_slack_alert(
        title="Copierbot scheduled generation missed",
        message=(
            f"Expected run: `{alert_key}` local\n"
            f"Grace period: `{GENERATE_MISS_GRACE_SECONDS // 60} minutes`\n"
            f"Last seen generation/run: `{last_seen or 'N/A'}`\n"
            f"Active publish platforms: `{active_platforms}`\n"
            f"Context: {context}"
        ),
    )
    if not alert_sent:
        return
    with SCHED_LOCK:
        LAST_GENERATE_MISS_ALERT_KEY = alert_key
        GENERATE_MISS_ALERT_BLOCKED_UNTIL_SUCCESS = True
        _save_scheduler_preferences()


def _maybe_alert_if_generate_already_missed_before_startup() -> None:
    """Alert if dashboard starts after today's expected run was already missed."""
    start_time = _normalize_generate_start_time(_get_generate_start_time())
    if not start_time:
        return
    expected_today = _next_occurrence_for_time(start_time, now=_now_dt() - timedelta(days=1))
    now_local = _now_dt()
    if now_local <= expected_today + timedelta(seconds=GENERATE_MISS_GRACE_SECONDS):
        return
    latest_run = _latest_generated_run_time()
    if latest_run is not None and latest_run >= expected_today:
        return
    _send_generate_miss_alert(
        expected_run_dt=expected_today,
        last_seen=_stamp_from_dt(latest_run) if latest_run is not None else "",
        context="dashboard startup detected that the scheduled generation time had already passed without a new run folder.",
    )


def _maybe_alert_on_missed_generate_run() -> None:
    """Send one Slack alert if the next scheduled generate run is overdue."""
    global LAST_GENERATE_MISS_ALERT_KEY
    with SCHED_LOCK:
        state = SCHEDULERS["generate_publish"]
        if not state.running:
            return
        next_run_at = state.next_run_at
        last_run_at = state.last_run_at
    next_run_dt = _parse_local_stamp(next_run_at)
    if next_run_dt is None:
        return
    now_local = _now_dt()
    if now_local <= next_run_dt + timedelta(seconds=GENERATE_MISS_GRACE_SECONDS):
        return
    last_run_dt = _parse_local_stamp(last_run_at)
    if last_run_dt is not None and last_run_dt >= next_run_dt:
        return
    _send_generate_miss_alert(
        expected_run_dt=next_run_dt,
        last_seen=last_run_at,
        context="the generate scheduler remained overdue beyond the grace period while the dashboard was running.",
    )
    with SCHED_LOCK:
        state = SCHEDULERS["generate_publish"]
        if state.running and state.next_run_at == next_run_at:
            state.last_result = f"alerted: missed scheduled generate run due at {_stamp_from_dt(next_run_dt)} local."


def _maybe_alert_on_instagram_token_expiry() -> None:
    """Send one Slack alert at 7/3/1-day thresholds before Instagram token expiry."""
    instagram_config = load_instagram_config(required=False)
    if instagram_config is None or instagram_config.access_token_expires_at is None:
        return

    remaining = instagram_token_time_remaining(instagram_config)
    if remaining is None or remaining.total_seconds() <= 0:
        return

    remaining_days = remaining.total_seconds() / 86400
    for threshold_days in INSTAGRAM_TOKEN_WARNING_DAYS:
        if remaining_days > threshold_days:
            continue
        if not should_send_instagram_token_expiry_alert(
            instagram_config,
            threshold_days=threshold_days,
        ):
            continue

        expires_at = instagram_config.access_token_expires_at.astimezone(timezone.utc).isoformat()
        if remaining_days >= 1:
            remaining_label = f"{remaining_days:.1f} days"
        else:
            remaining_label = f"{max(1.0, remaining.total_seconds() / 3600):.1f} hours"
        threshold_label = f"{threshold_days} day" if threshold_days == 1 else f"{threshold_days} days"
        send_slack_alert(
            title="Copierbot Instagram token expiring soon",
            message=(
                f"Threshold: `{threshold_label}`\n"
                f"Remaining: `{remaining_label}`\n"
                f"Expires at: `{expires_at}`\n"
                "Action: Generate a fresh long-lived Instagram token, update "
                "`INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT`, then restart the dashboard."
            ),
        )
        break


def _scheduler_watchdog_loop() -> None:
    """Background watchdog for alerting on missed scheduled generate runs."""
    while True:
        try:
            _maybe_alert_on_missed_generate_run()
        except Exception:
            pass
        try:
            _maybe_alert_on_instagram_token_expiry()
        except Exception:
            pass
        time.sleep(60)


def _render_job_card(job: Job) -> str:
    """Render one job card with metadata and captured output."""
    command_text = " ".join(html.escape(part) for part in job.command)
    output = _render_output_with_links(job.output or "(no output)")
    return_code = "" if job.return_code is None else f" | rc={job.return_code}"
    finished = f" | finished={html.escape(job.finished_at)}" if job.finished_at else ""
    status_label = job.status
    extra_meta = ""
    if (
        job.status == "succeeded"
        and any(part.endswith("engage.py") or part == "engage.py" for part in job.command)
        and JOB_REPLIED_RE.search(job.output or "")
    ):
        status_label = "succeeded - replied to comment"
    if job.status == "failed":
        video_failure = _video_failure_summary(job)
        if video_failure:
            status_label = f"failed - {video_failure.lower()}"
            extra_meta = f"<div class='meta error-meta'>video error: {html.escape(video_failure)}</div>"

    return (
        "<article class='job'>"
        f"<div><strong>#{job.job_id}</strong> {html.escape(job.label)}</div>"
        f"<div class='meta'>started={html.escape(job.started_at)}{finished}{return_code}</div>"
        f"<div class='status {html.escape(job.status)}'>{html.escape(status_label)}</div>"
        f"<div class='meta'>cmd: <code>{command_text}</code></div>"
        f"{extra_meta}"
        "<details><summary>Output</summary>"
        f"<pre>{output}</pre>"
        "</details>"
        "</article>"
    )


def _job_uses_script(job: Job, script_name: str) -> bool:
    """Return True when the tracked command runs the given script."""
    return any(part.endswith(script_name) or part == script_name for part in job.command)


def _extract_job_run_dir_name(job: Job) -> str:
    """Return run dir name from --run-dir arg when present."""
    for index, part in enumerate(job.command):
        if part == "--run-dir" and index + 1 < len(job.command):
            raw = str(job.command[index + 1]).strip()
            return Path(raw).name
    return ""


def _video_failure_summary(job: Job) -> str:
    """Return concise video failure detail from saved result JSON when available."""
    if not _job_uses_script(job, "generate_video.py"):
        return ""
    run_name = _extract_job_run_dir_name(job)
    if not run_name or not RUN_DIR_RE.match(run_name):
        return ""
    run_dir = OUTPUT_DIR / run_name
    if not run_dir.is_dir():
        return ""
    candidates = sorted(
        (
            path
            for path in run_dir.iterdir()
            if path.is_file()
            and path.name.startswith("video_result  ")
            and path.suffix.lower() == ".json"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        failed = payload.get("failed")
        if not isinstance(failed, dict):
            continue
        status = failed.get("status")
        if isinstance(status, dict):
            provider_error = str(status.get("error", "")).strip()
            if provider_error:
                return provider_error
        error_text = str(failed.get("error", "")).strip()
        if error_text:
            return error_text
    for line in reversed((job.output or "").splitlines()):
        cleaned = line.strip()
        if cleaned.startswith("RuntimeError:") or cleaned.startswith("TimeoutError:"):
            return cleaned.split(":", 1)[1].strip()
    return ""


def _split_url_punctuation(token: str) -> tuple[str, str]:
    """Split trailing punctuation from URL-like token."""
    trailing = ""
    while token and token[-1] in ".,);]":
        trailing = token[-1] + trailing
        token = token[:-1]
    return token, trailing


def _render_output_with_links(text: str) -> str:
    """Escape output text and convert URLs into clickable links."""
    parts: list[str] = []
    last = 0
    for match in URL_RE.finditer(text):
        start, end = match.span()
        parts.append(html.escape(text[last:start]))
        raw_token = text[start:end]
        core, trailing = _split_url_punctuation(raw_token)
        href = html.escape(core, quote=True)
        label = html.escape(core)
        parts.append(
            f"<a href=\"{href}\" target=\"_blank\" rel=\"noopener noreferrer\">{label}</a>"
        )
        if trailing:
            parts.append(html.escape(trailing))
        last = end
    parts.append(html.escape(text[last:]))
    return "".join(parts)


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Copierbot dashboard."""

    server_version = "CopierbotDashboard/2.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/image.png":
            self._serve_static_file(ICON_PATH, "image/png")
            return
        if parsed.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._render_index()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/run":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        wants_async = self.headers.get("X-Requested-With", "").lower() == "fetch"

        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(body)
        payload = {k: (v[0] if v else "") for k, v in form.items()}
        action = (payload.get("action") or "").strip()

        if action == "update_publish_platforms":
            selected = form.get("publish_platform", [])
            _set_active_publish_platforms([str(item) for item in selected])
            self._finish_post_response(wants_async)
            return
        if action == "update_mention_platforms":
            selected = form.get("mention_platform", [])
            _set_active_mention_platforms([str(item) for item in selected])
            self._finish_post_response(wants_async)
            return

        actions = _build_actions()
        action_info = actions.get(action)
        if action_info is not None:
            label, cmd_builder = action_info
            command = cmd_builder(payload)
            if command:
                _enqueue_job(label=label, command=command, background=True)
        elif action == "start_generate_scheduler":
            hours_raw = (payload.get("generate_interval_hours") or "1").strip()
            start_time_raw = (payload.get("generate_start_time") or "").strip()
            try:
                hours = int(hours_raw)
            except ValueError:
                hours = 1
            _start_generate_scheduler_from_settings(hours=hours, start_time_raw=start_time_raw)
        elif action == "stop_generate_scheduler":
            _stop_scheduler("generate_publish")
        elif action == "start_mentions_scheduler":
            mins_raw = (payload.get("mentions_interval_minutes") or "5").strip()
            try:
                minutes = int(mins_raw)
            except ValueError:
                minutes = 5
            _start_mentions_scheduler_from_settings(minutes=minutes)
        elif action == "stop_mentions_scheduler":
            _stop_scheduler("mentions")
        elif action == "start_slack_control":
            _start_slack_control_listener()
        elif action == "stop_slack_control":
            _stop_slack_control_listener()

        self._finish_post_response(wants_async)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        """Silence default HTTP request logging for cleaner terminal output."""
        return

    def _finish_post_response(self, wants_async: bool) -> None:
        """Return either no-content for fetch callers or a redirect for normal forms."""
        if wants_async:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def _serve_static_file(self, path: Path, content_type: str) -> None:
        """Serve one known local static file."""
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(payload)

    def _render_index(self) -> None:
        runs = _list_run_dirs()
        persona_state = get_persona_state()
        seasonal_phase_raw = str(persona_state.get("seasonal_phase", "none"))
        seasonal_phase_label = (
            "N/A"
            if seasonal_phase_raw == "none"
            else seasonal_phase_raw.replace("_", " ").upper()
        )
        seasonal_offset = max(0, int(persona_state.get("season_post_offset", 0)))
        next_seasonal_in = (
            0 if seasonal_phase_raw == "none" else max(1, 40 - seasonal_offset)
        )
        sched = _scheduler_snapshot()
        with JOB_LOCK:
            all_jobs = list(reversed(JOBS))
        jobs = all_jobs[:JOB_RENDER_LIMIT]
        total_jobs = len(all_jobs)

        last_job = all_jobs[0] if all_jobs else None
        alert_html = ""
        if last_job:
            last_suffix = ""
            alert_class = "alert-neutral"
            if last_job.status == "failed":
                failure_summary = _video_failure_summary(last_job)
                if failure_summary:
                    last_suffix = f" - {html.escape(failure_summary)}"
                alert_class = "alert-failed"
            elif last_job.status == "succeeded":
                alert_class = "alert-succeeded"
            elif last_job.status == "running":
                alert_class = "alert-running"
            alert_html = (
                f"<div class='alert {alert_class}'>Last job: #{last_job.job_id} {html.escape(last_job.label)} "
                f"[{html.escape(last_job.status)}]{last_suffix}</div>"
            )

        run_options = "\n".join(
            f"<option value='{html.escape(run)}'>{html.escape(run)}</option>" for run in runs
        )
        if not run_options:
            run_options = "<option value=''>No run folders yet</option>"

        job_cards = "\n".join(_render_job_card(job) for job in jobs) or "<p>No jobs yet.</p>"
        job_count_label = (
            f"Showing latest {len(jobs)} of {total_jobs}"
            if total_jobs > len(jobs)
            else f"{total_jobs} total"
        )

        gp = sched["generate_publish"]
        mn = sched["mentions"]
        slack_control = _slack_control_snapshot()
        active_publish_platforms = _get_active_publish_platforms()
        active_publish_platform_arg = _active_publish_platform_arg(active_publish_platforms)
        active_publish_platforms_label = _platforms_display_label(active_publish_platforms)
        configured_generate_start_time = (
            _normalize_generate_start_time(_get_generate_start_time())
            or _now_dt().strftime("%H:%M")
        )
        active_mention_platforms = _get_active_mention_platforms()
        active_mention_platform_arg = _active_mention_platform_arg(active_mention_platforms)
        active_mention_platforms_label = _platforms_display_label(active_mention_platforms)
        gp_hours_selected = max(1, int(gp["interval_seconds"]) // 3600)
        mn_mins_selected = max(1, int(mn["interval_seconds"]) // 60)

        gp_options = "".join(
            (
                f"<option value='{h}'{' selected' if h == gp_hours_selected else ''}>"
                f"Every {h} hour{'s' if h != 1 else ''}</option>"
            )
            for h in GENERATE_INTERVAL_HOURS
        )
        mn_options = "".join(
            (
                f"<option value='{m}'{' selected' if m == mn_mins_selected else ''}>"
                f"Every {m} minute{'s' if m != 1 else ''}</option>"
            )
            for m in MENTION_INTERVAL_MINUTES
        )
        publish_platform_options = "".join(
            (
                "<label style='display:flex;align-items:center;gap:0.5rem;margin:0.35rem 0;'>"
                f"<input type='checkbox' name='publish_platform' value='{name}'"
                f"{' checked' if name in active_publish_platforms else ''} />"
                f"<span>{_platform_display_name(name)}</span>"
                "</label>"
            )
            for name in PUBLISH_PLATFORM_OPTIONS
        )
        mention_platform_options = "".join(
            (
                "<label style='display:flex;align-items:center;gap:0.5rem;margin:0.35rem 0;'>"
                f"<input type='checkbox' name='mention_platform' value='{name}'"
                f"{' checked' if name in active_mention_platforms else ''} />"
                f"<span>{_platform_display_name(name)}</span>"
                "</label>"
            )
            for name in MENTION_PLATFORM_OPTIONS
        )

        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Copierbot Dashboard</title>
  <style>
    :root {{
      --bg: #f4efe8;
      --ink: #2d2928;
      --card: #fffaf2;
      --card-strong: #fffdf8;
      --border: #d7cec2;
      --muted: #6d6761;
      --accent: #5fa8b4;
      --accent-deep: #3d8895;
      --accent2: #c36b97;
      --accent3: #d8b74d;
      --accent4: #342b2e;
      --success: #3c7f62;
      --warning: #a96836;
      --danger: #b14949;
      --shadow: 0 12px 34px rgba(52, 43, 46, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Gill Sans", "Trebuchet MS", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 8% 12%, rgba(95, 168, 180, 0.15) 0%, transparent 30%),
        radial-gradient(circle at 90% 10%, rgba(195, 107, 151, 0.10) 0%, transparent 28%),
        linear-gradient(180deg, #faf5ec 0%, var(--bg) 36%, #f2ece3 100%);
      min-height: 100vh;
    }}
    .wrap {{
      width: min(96vw, 1920px);
      max-width: none;
      margin: 0 auto;
      padding: 1.15rem 1.4rem 1.8rem;
    }}
    .dashboard-shell {{
      display: grid;
      gap: 1rem;
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 1rem;
      align-items: center;
      background:
        linear-gradient(90deg,
          rgba(95, 168, 180, 0.14) 0 23%,
          rgba(195, 107, 151, 0.11) 23% 49%,
          rgba(216, 183, 77, 0.14) 49% 72%,
          rgba(52, 43, 46, 0.10) 72% 100%
        ),
        var(--card-strong);
      border: 1px solid rgba(215, 206, 194, 0.9);
      border-radius: 18px;
      padding: 1rem 1.1rem;
      box-shadow: var(--shadow);
    }}
    .hero-copy {{
      min-width: 0;
    }}
    h1 {{
      margin: 0 0 0.45rem;
      font-size: clamp(1.65rem, 2vw, 2.15rem);
      letter-spacing: 0.01em;
    }}
    .sub {{
      margin: 0;
      color: var(--muted);
      font-size: 0.97rem;
      line-height: 1.5;
      max-width: 70ch;
    }}
    .hero-art {{
      display: flex;
      align-items: center;
      justify-content: center;
      padding-left: 0.75rem;
    }}
    .hero-art img {{
      width: 92px;
      height: 92px;
      object-fit: contain;
      border-radius: 18px;
      background: rgba(255, 250, 242, 0.86);
      box-shadow: 0 12px 24px rgba(52, 43, 46, 0.12);
    }}
    .toolbar {{
      display: flex;
      gap: 0.7rem;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 0.9rem;
    }}
    .toolbar button {{
      width: auto;
      min-width: 124px;
    }}
    .toolbar label {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.92rem;
      color: var(--muted);
    }}
    .toolbar-meta {{
      display: flex;
      gap: 0.8rem;
      align-items: center;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .auto-indicator {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.32rem 0.58rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.55);
    }}
    .auto-indicator::before {{
      content: "";
      width: 0.56rem;
      height: 0.56rem;
      border-radius: 999px;
      background: var(--warning);
      box-shadow: 0 0 0 3px rgba(169, 104, 54, 0.12);
    }}
    .auto-indicator.on::before {{
      background: var(--success);
      box-shadow: 0 0 0 3px rgba(60, 127, 98, 0.12);
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 0.6rem;
    }}
    .stat {{
      background: rgba(255, 250, 242, 0.92);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 0.72rem 0.82rem;
      box-shadow: 0 5px 14px rgba(52, 43, 46, 0.05);
    }}
    .stat .k {{
      color: var(--muted);
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .stat .v {{
      font-size: 1rem;
      font-weight: 700;
      margin-top: 0.14rem;
    }}
    .alert {{
      border: 1px solid rgba(95, 168, 180, 0.18);
      background: rgba(252, 250, 246, 0.95);
      border-left: 4px solid rgba(95, 168, 180, 0.72);
      padding: 0.72rem 0.86rem;
      border-radius: 12px;
      font-size: 0.94rem;
      box-shadow: 0 8px 20px rgba(52, 43, 46, 0.05);
    }}
    .alert-succeeded {{
      border-color: rgba(60, 127, 98, 0.22);
      border-left-color: var(--success);
      background: rgba(244, 251, 246, 0.96);
    }}
    .alert-failed {{
      border-color: rgba(177, 73, 73, 0.22);
      border-left-color: var(--danger);
      background: rgba(255, 245, 245, 0.96);
    }}
    .alert-running {{
      border-color: rgba(61, 136, 149, 0.22);
      border-left-color: var(--accent-deep);
      background: rgba(244, 250, 251, 0.96);
    }}
    .alert-neutral {{
      border-color: rgba(109, 103, 97, 0.18);
      border-left-color: rgba(109, 103, 97, 0.55);
      background: rgba(252, 250, 246, 0.95);
    }}
    .grid {{
      display: grid;
      gap: 0.95rem;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      align-items: stretch;
    }}
    .compact-grid {{
      display: flex;
      gap: 0.95rem;
      flex-wrap: wrap;
      align-items: stretch;
    }}
    .compact-grid > .card {{
      flex: 0 1 calc(25% - 0.72rem);
      max-width: calc(25% - 0.72rem);
      min-width: 220px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 0.95rem;
      box-shadow: var(--shadow);
      height: 100%;
    }}
    .hint {{
      margin: 0 0 0.65rem;
      color: var(--muted);
      font-size: 0.86rem;
      line-height: 1.35;
    }}
    .sched-meta {{
      margin: 0.26rem 0;
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.35;
    }}
    .sched-status {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      font-weight: 700;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      padding: 0.28rem 0.55rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.7);
      border: 1px solid var(--border);
    }}
    .sched-status::before {{
      content: "";
      width: 0.52rem;
      height: 0.52rem;
      border-radius: 999px;
      background: var(--warning);
    }}
    .sched-status.running {{ color: var(--success); }}
    .sched-status.running::before {{ background: var(--success); }}
    .sched-status.stopped {{ color: var(--warning); }}
    form {{ margin: 0; }}
    button {{
      width: 100%;
      border: none;
      border-radius: 10px;
      background: linear-gradient(145deg, var(--accent), var(--accent-deep));
      color: #fff;
      padding: 0.66rem 0.8rem;
      font-weight: 600;
      cursor: pointer;
      transition: transform 120ms ease, filter 120ms ease, opacity 120ms ease;
    }}
    button.stop {{
      background: linear-gradient(145deg, #b46d47, #95563a);
    }}
    button:hover {{
      filter: brightness(1.04);
      transform: translateY(-1px);
    }}
    button:disabled {{
      opacity: 0.7;
      cursor: progress;
      transform: none;
    }}
    select,
    input[type="time"],
    input[type="url"] {{
      width: 100%;
      margin-bottom: 0.5rem;
      padding: 0.56rem 0.6rem;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #fffdf9;
      color: var(--ink);
    }}
    .status-board {{
      display: grid;
      gap: 0.95rem;
      grid-template-columns: minmax(0, 1fr);
    }}
    .status-head {{
      display: flex;
      justify-content: space-between;
      gap: 0.75rem;
      align-items: baseline;
      flex-wrap: wrap;
      margin-bottom: 0.7rem;
    }}
    .status-count {{
      color: var(--muted);
      font-size: 0.84rem;
    }}
    .jobs-panel {{
      max-height: 450px;
      overflow-y: auto;
      padding-right: 0.2rem;
    }}
    .jobs-panel::-webkit-scrollbar {{
      width: 0.7rem;
    }}
    .jobs-panel::-webkit-scrollbar-thumb {{
      background: rgba(95, 168, 180, 0.32);
      border-radius: 999px;
      border: 2px solid transparent;
      background-clip: padding-box;
    }}
    .job {{
      background: rgba(255, 253, 248, 0.88);
      border: 1px solid var(--border);
      border-radius: 12px;
      margin-bottom: 0.75rem;
      padding: 0.8rem;
    }}
    .meta {{
      color: var(--muted);
      font-size: 0.85rem;
    }}
    .error-meta {{
      color: var(--danger);
      font-weight: 600;
    }}
    .status {{
      font-weight: 700;
      text-transform: uppercase;
      font-size: 0.8rem;
    }}
    .status.running {{ color: var(--accent-deep); }}
    .status.failed {{ color: var(--danger); }}
    .status.succeeded {{ color: var(--success); }}
    .status.queued {{ color: var(--warning); }}
    pre {{
      margin: 0.5rem 0 0;
      background: #211d1f;
      color: #eee7dd;
      padding: 0.72rem;
      border-radius: 9px;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 0.8rem;
      max-height: 280px;
    }}
    @media (max-width: 900px) {{
      .hero {{
        grid-template-columns: 1fr;
      }}
      .hero-art {{
        justify-content: flex-start;
        padding-left: 0;
      }}
    }}
    @media (max-width: 720px) {{
      .wrap {{
        width: 100%;
        padding: 0.95rem;
      }}
      .grid {{
        grid-template-columns: 1fr;
      }}
      .compact-grid {{
        display: grid;
        grid-template-columns: 1fr;
      }}
      .compact-grid > .card {{
        max-width: none;
        min-width: 0;
      }}
      .jobs-panel {{
        max-height: none;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div id="dashboard-shell" class="dashboard-shell">
      <section class="hero">
        <div class="hero-copy">
          <h1>Copierbot Local Dashboard</h1>
          <p class="sub">Local-only controls for generation, publishing, mention replies, Slack control, and video creation on <code>127.0.0.1</code>.</p>
          <div class="toolbar">
            <button id="refresh-now" type="button">Refresh Status</button>
            <label>
              <input id="auto-refresh" type="checkbox" />
              Auto refresh
            </label>
            <div class="toolbar-meta">
              <span id="auto-indicator" class="auto-indicator">
                <strong id="auto-status">Off</strong>
              </span>
              <span>Polling interval: 15s</span>
            </div>
          </div>
        </div>
        <div class="hero-art">
          <img src="/image.png" alt="Copierbot icon" />
        </div>
      </section>

      <div class="stats">
        <div class="stat">
          <div class="k">Posts Generated</div>
          <div class="v">{int(persona_state["posts_generated"])}</div>
        </div>
        <div class="stat">
          <div class="k">Persona Phase</div>
          <div class="v">{html.escape(str(persona_state["phase"]).upper())}</div>
        </div>
        <div class="stat">
          <div class="k">Next Major Phase In</div>
          <div class="v">{20 - (int(persona_state["posts_generated"]) % 20) if str(persona_state["phase"]) != "self_aware" else 0}</div>
        </div>
        <div class="stat">
          <div class="k">Seasonal Phase</div>
          <div class="v">{html.escape(seasonal_phase_label)}</div>
        </div>
        <div class="stat">
          <div class="k">Next Seasonal Shift In</div>
          <div class="v">{int(next_seasonal_in)}</div>
        </div>
      </div>

      {alert_html}

      <div class="grid">
      <div class="card">
        <h2>Publish Destinations</h2>
        <p class="hint">Choose active social platforms. Generate + Publish scheduler and manual publish actions will post to these destinations using one generated run.</p>
        <p class="sched-meta">Active: {html.escape(active_publish_platforms_label)} (orchestrator <code>--platform {html.escape(active_publish_platform_arg)}</code>)</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="update_publish_platforms" />
          {publish_platform_options}
          <button type="submit">Save Active Platforms</button>
        </form>
      </div>

      <div class="card">
        <h2>Mention Sources</h2>
        <p class="hint">Choose which platforms are checked by <code>engage.py</code> for manual "Check Mentions (All Platforms)" and the Mentions scheduler.</p>
        <p class="sched-meta">Active: {html.escape(active_mention_platforms_label)} (engage <code>--platform {html.escape(active_mention_platform_arg)}</code>)</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="update_mention_platforms" />
          {mention_platform_options}
          <button type="submit">Save Mention Platforms</button>
        </form>
      </div>

      <div class="card">
        <h2>Scheduler: Generate + Publish</h2>
        <p class="hint">Runs <code>main.py</code>, then publishes all new run folders from that cycle in creation order (normal post first, phase-change post second when present) to active destinations. Choose an interval and local start time; if the chosen time has passed today, first run starts tomorrow at that time.</p>
        <div class="sched-status {'running' if bool(gp['running']) else 'stopped'}">{'running' if bool(gp['running']) else 'stopped'}</div>
        <p class="sched-meta">Active platforms: {html.escape(active_publish_platforms_label)}</p>
        <p class="sched-meta">Start time (local): {html.escape(configured_generate_start_time)}</p>
        <p class="sched-meta">Last run: {html.escape(str(gp['last_run_at']) or 'N/A')}</p>
        <p class="sched-meta">Next run: {html.escape(str(gp['next_run_at']) or 'N/A')}</p>
        <p class="sched-meta">Last result: {html.escape(str(gp['last_result']) or 'N/A')}</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="start_generate_scheduler" />
          <select name="generate_interval_hours">{gp_options}</select>
          <input type="time" name="generate_start_time" value="{html.escape(configured_generate_start_time)}" step="60" />
          <button type="submit">Start / Update Generate Scheduler</button>
        </form>
        <form method="post" action="/run" style="margin-top:0.5rem;">
          <input type="hidden" name="action" value="stop_generate_scheduler" />
          <button class="stop" type="submit">Stop Generate Scheduler</button>
        </form>
      </div>

      <div class="card">
        <h2>Scheduler: Mentions</h2>
        <p class="hint">Runs <code>engage.py</code> on the selected minute interval to monitor and reply to qualifying mentions.</p>
        <div class="sched-status {'running' if bool(mn['running']) else 'stopped'}">{'running' if bool(mn['running']) else 'stopped'}</div>
        <p class="sched-meta">Active platforms: {html.escape(active_mention_platforms_label)}</p>
        <p class="sched-meta">Last run: {html.escape(str(mn['last_run_at']) or 'N/A')}</p>
        <p class="sched-meta">Next run: {html.escape(str(mn['next_run_at']) or 'N/A')}</p>
        <p class="sched-meta">Last result: {html.escape(str(mn['last_result']) or 'N/A')}</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="start_mentions_scheduler" />
          <select name="mentions_interval_minutes">{mn_options}</select>
          <button type="submit">Start / Update Mention Scheduler</button>
        </form>
        <form method="post" action="/run" style="margin-top:0.5rem;">
          <input type="hidden" name="action" value="stop_mentions_scheduler" />
          <button class="stop" type="submit">Stop Mention Scheduler</button>
        </form>
      </div>

      <div class="card">
        <h2>Slack Control</h2>
        <p class="hint">Runs <code>slack_control.py</code> to listen for DM commands from the separate Slack control app. Requires <code>SLACK_CONTROL_BOT_TOKEN</code>, <code>SLACK_CONTROL_APP_TOKEN</code>, and <code>SLACK_CONTROL_ALLOWED_USER_IDS</code> in <code>.env</code>.</p>
        <div class="sched-status {'running' if bool(slack_control['running']) else 'stopped'}">{'running' if bool(slack_control['running']) else 'stopped'}</div>
        <p class="sched-meta">PID: {html.escape(str(slack_control['pid']) if int(slack_control['pid']) else 'N/A')}</p>
        <p class="sched-meta">Last result: {html.escape(str(slack_control['last_result']) or 'N/A')}</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="start_slack_control" />
          <button type="submit">Start Slack Control Listener</button>
        </form>
        <form method="post" action="/run" style="margin-top:0.5rem;">
          <input type="hidden" name="action" value="stop_slack_control" />
          <button class="stop" type="submit">Stop Slack Control Listener</button>
        </form>
      </div>
    </div>

    <div class="compact-grid" style="margin-top:0.9rem;">
      <div class="card">
        <h2>Generate</h2>
        <p class="hint">Runs <code>main.py</code> to create a new Copierbot post in a fresh timestamped output folder.</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="generate" />
          <button type="submit">Run main.py</button>
        </form>
      </div>

      <div class="card">
        <h2>Generate From URL</h2>
        <p class="hint">Runs <code>main.py --article-url ...</code> to create a normal timestamped news-style run from one webpage you supply directly, instead of choosing from the news feed.</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="generate_from_url" />
          <input
            type="url"
            name="article_url"
            placeholder="https://example.com/article"
            required
          />
          <button type="submit">Generate From Webpage URL</button>
        </form>
      </div>

      <div class="card">
        <h2>Publish Latest</h2>
        <p class="hint">Runs <code>orchestrator.py</code> to publish the newest generated run to active destinations.</p>
        <p class="sched-meta">Active platforms: {html.escape(active_publish_platforms_label)}</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="publish_latest" />
          <button type="submit">Run orchestrator.py</button>
        </form>
      </div>

      <div class="card">
        <h2>Publish Specific Run</h2>
        <p class="hint">Select a run folder, then publish only that specific output instead of the latest one, to active destinations.</p>
        <p class="sched-meta">Active platforms: {html.escape(active_publish_platforms_label)}</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="publish_selected" />
          <select id="publish-run-dir" name="run_dir">{run_options}</select>
          <button type="submit">Publish Selected Folder</button>
        </form>
      </div>
    </div>

    <div class="compact-grid" style="margin-top:0.9rem;">
      <div class="card">
        <h2>Check Mentions</h2>
        <p class="hint">Runs <code>engage.py</code> to fetch mentions and auto-reply to qualifying wellbeing, identity, contact, memory, and similar prompts. "All Platforms" follows active mention sources.</p>
        <p class="sched-meta">Active platforms: {html.escape(active_mention_platforms_label)}</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="check_mentions" />
          <button type="submit">Check Mentions (All Platforms)</button>
        </form>
        <form method="post" action="/run" style="margin-top:0.5rem;">
          <input type="hidden" name="action" value="check_mentions_mastodon" />
          <button type="submit">Check Mentions (Mastodon)</button>
        </form>
        <form method="post" action="/run" style="margin-top:0.5rem;">
          <input type="hidden" name="action" value="check_mentions_bluesky" />
          <button type="submit">Check Mentions (Bluesky)</button>
        </form>
        <form method="post" action="/run" style="margin-top:0.5rem;">
          <input type="hidden" name="action" value="check_mentions_wordpress" />
          <button type="submit">Check Mentions (WordPress)</button>
        </form>
        <form method="post" action="/run" style="margin-top:0.5rem;">
          <input type="hidden" name="action" value="check_mentions_instagram" />
          <button type="submit">Check Mentions (Instagram)</button>
        </form>
      </div>

      <div class="card">
        <h2>Generate Video</h2>
        <p class="hint">Select a news run folder and send its saved image plus a motion prompt derived from <code>prompt  *.txt</code> to Higgsfield DoP Lite. This is manual-only and never runs automatically on dashboard startup.</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="generate_video_selected" />
          <select id="video-run-dir" name="run_dir">{run_options}</select>
          <button type="submit">Generate Higgsfield Video</button>
        </form>
      </div>
    </div>

      <section class="status-board">
        <div class="card">
          <div class="status-head">
            <h2>Recent Jobs</h2>
            <div class="status-count">{html.escape(job_count_label)}</div>
          </div>
          <p class="hint">Live status updates refresh in place without a full page reload. Older jobs are kept in memory, while this panel shows the latest activity in a scrollable feed.</p>
          <div id="jobs-panel" class="jobs-panel">{job_cards}</div>
        </div>
      </section>
    </div>
  </div>
  <script>
    (() => {{
      const autoRefreshKey = "copierbot_dashboard_auto_refresh";
      const publishRunKey = "copierbot_dashboard_publish_run_dir";
      const videoRunKey = "copierbot_dashboard_video_run_dir";
      const articleUrlKey = "copierbot_dashboard_article_url";
      const intervalMs = 15000;
      let timer = null;
      let refreshInFlight = false;

      const shell = () => document.getElementById("dashboard-shell");
      const statusLabel = () => document.getElementById("auto-status");
      const indicator = () => document.getElementById("auto-indicator");
      const checkbox = () => document.getElementById("auto-refresh");

      const selectBindings = [
        ["publish-run-dir", publishRunKey],
        ["video-run-dir", videoRunKey],
      ];

      const restoreControlState = () => {{
        for (const [id, storageKey] of selectBindings) {{
          const selectEl = document.getElementById(id);
          if (!selectEl) continue;
          const saved = window.localStorage.getItem(storageKey);
          if (saved && [...selectEl.options].some((option) => option.value === saved)) {{
            selectEl.value = saved;
          }}
          selectEl.addEventListener("change", () => {{
            window.localStorage.setItem(storageKey, selectEl.value || "");
          }});
        }}
        const urlInput = document.querySelector('input[name="article_url"]');
        if (urlInput) {{
          const savedUrl = window.localStorage.getItem(articleUrlKey);
          if (savedUrl && !urlInput.value) {{
            urlInput.value = savedUrl;
          }}
          urlInput.addEventListener("input", () => {{
            window.localStorage.setItem(articleUrlKey, urlInput.value || "");
          }});
        }}
      }};

      const apply = (enabled) => {{
        if (timer) {{
          clearInterval(timer);
          timer = null;
        }}
        if (enabled) {{
          timer = window.setInterval(() => {{
            if (shouldDeferRefresh()) return;
            refreshDashboard();
          }}, intervalMs);
          if (statusLabel()) statusLabel().textContent = "On";
          if (indicator()) indicator().classList.add("on");
        }} else {{
          if (statusLabel()) statusLabel().textContent = "Off";
          if (indicator()) indicator().classList.remove("on");
        }}
        if (checkbox()) checkbox().checked = !!enabled;
        window.localStorage.setItem(autoRefreshKey, enabled ? "1" : "0");
      }};

      const shouldDeferRefresh = () => {{
        const active = document.activeElement;
        return !!active && active.matches("input, textarea, select");
      }};

      const snapshotScrollPositions = () => {{
        const positions = {{}};
        document.querySelectorAll("[id='jobs-panel']").forEach((node) => {{
          positions[node.id] = node.scrollTop;
        }});
        return positions;
      }};

      const restoreScrollPositions = (positions) => {{
        Object.entries(positions).forEach(([id, value]) => {{
          const node = document.getElementById(id);
          if (node) node.scrollTop = value;
        }});
      }};

      const refreshDashboard = async () => {{
        if (refreshInFlight) return;
        refreshInFlight = true;
        const scrollPositions = snapshotScrollPositions();
        try {{
          const response = await fetch("/", {{
            headers: {{
              "X-Requested-With": "fetch",
              "Cache-Control": "no-cache",
            }},
          }});
          if (!response.ok) {{
            throw new Error(`refresh failed with status ${{response.status}}`);
          }}
          const text = await response.text();
          const doc = new DOMParser().parseFromString(text, "text/html");
          const nextShell = doc.getElementById("dashboard-shell");
          if (!nextShell || !shell()) {{
            throw new Error("dashboard shell missing in refresh response");
          }}
          shell().innerHTML = nextShell.innerHTML;
          restoreControlState();
          bindForms();
          bindRefreshControls();
          restoreScrollPositions(scrollPositions);
          apply(window.localStorage.getItem(autoRefreshKey) === "1");
        }} catch (error) {{
          console.error(error);
        }} finally {{
          refreshInFlight = false;
        }}
      }};

      const bindForms = () => {{
        document.querySelectorAll('form[action="/run"]').forEach((form) => {{
          form.addEventListener("submit", async (event) => {{
            event.preventDefault();
            const submitter = event.submitter;
            if (submitter) submitter.disabled = true;
            try {{
              const body = new URLSearchParams(new FormData(form));
              const response = await fetch(form.action, {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                  "X-Requested-With": "fetch",
                }},
                body: body.toString(),
              }});
              if (!response.ok && response.status !== 204) {{
                throw new Error(`action failed with status ${{response.status}}`);
              }}
              await refreshDashboard();
            }} catch (error) {{
              console.error(error);
            }} finally {{
              if (submitter) submitter.disabled = false;
            }}
          }}, {{ once: true }});
        }});
      }};

      const bindRefreshControls = () => {{
        const refreshBtn = document.getElementById("refresh-now");
        const refreshToggle = checkbox();
        if (refreshBtn) {{
          refreshBtn.addEventListener("click", () => {{
            refreshDashboard();
          }}, {{ once: true }});
        }}
        if (refreshToggle) {{
          refreshToggle.addEventListener("change", () => apply(refreshToggle.checked), {{ once: true }});
        }}
      }};

      restoreControlState();
      bindForms();
      bindRefreshControls();
      apply(window.localStorage.getItem(autoRefreshKey) === "1");
    }})();
  </script>
</body>
</html>
"""
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that can rebind immediately after restarts."""

    allow_reuse_address = True


def run_server() -> None:
    """Run local-only dashboard server."""
    _maybe_alert_if_generate_already_missed_before_startup()
    _maybe_alert_on_instagram_token_expiry()
    _start_generate_scheduler_from_settings()
    _start_mentions_scheduler_from_settings()
    _start_slack_control_listener()
    watchdog = threading.Thread(
        target=_scheduler_watchdog_loop,
        daemon=True,
        name="copierbot-scheduler-watchdog",
    )
    watchdog.start()
    server = ReusableThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Copierbot dashboard listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for key in list(SCHEDULERS.keys()):
            _stop_scheduler(key)
        server.server_close()


if __name__ == "__main__":
    run_server()

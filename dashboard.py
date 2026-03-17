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
import subprocess
import sys
import tempfile
import threading
from typing import Callable
from urllib.parse import parse_qs, urlparse

from persona import get_persona_state
from storage import init_storage, list_published_posts


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
SCHED_PREFS_PATH = BASE_DIR / "data" / "dashboard_scheduler_state.json"
MAX_JOBS = 80
MAX_OUTPUT_CHARS = 12000
HOST = "127.0.0.1"
PORT = 8787
RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?$")
URL_RE = re.compile(r"https?://[^\s<>'\"]+")

GENERATE_INTERVAL_HOURS = list(range(1, 25))
MENTION_INTERVAL_MINUTES = [1, 5, 10, 15, 20, 30, 60]
PUBLISH_PLATFORM_OPTIONS = ("mastodon", "bluesky")
DEFAULT_ACTIVE_PUBLISH_PLATFORMS = ["mastodon"]
DEFAULT_ACTIVE_MENTION_PLATFORMS = ["mastodon", "bluesky"]


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
        if value in PUBLISH_PLATFORM_OPTIONS and value not in deduped:
            deduped.append(value)
    return deduped or list(DEFAULT_ACTIVE_MENTION_PLATFORMS)


def _active_platform_arg(platforms: list[str]) -> str:
    """Convert active platforms list to orchestrator --platform argument."""
    normalized = _normalize_active_publish_platforms(platforms)
    if len(normalized) == 2:
        return "all"
    return normalized[0]


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


def _load_scheduler_preferences() -> dict[str, object]:
    """Load persisted scheduler interval preferences."""
    defaults = {
        "generate_publish_interval_seconds": 3600,
        "mentions_interval_seconds": 300,
        "active_publish_platforms": list(DEFAULT_ACTIVE_PUBLISH_PLATFORMS),
        "active_mention_platforms": list(DEFAULT_ACTIVE_MENTION_PLATFORMS),
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
    }


def _save_scheduler_preferences() -> None:
    """Persist scheduler interval preferences to disk."""
    payload = {
        "generate_publish_interval_seconds": int(SCHEDULERS["generate_publish"].interval_seconds),
        "mentions_interval_seconds": int(SCHEDULERS["mentions"].interval_seconds),
        "active_publish_platforms": list(ACTIVE_PUBLISH_PLATFORMS),
        "active_mention_platforms": list(ACTIVE_MENTION_PLATFORMS),
    }
    SCHED_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHED_PREFS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _apply_scheduler_preferences() -> None:
    """Apply persisted preferences to in-memory scheduler defaults."""
    prefs = _load_scheduler_preferences()
    SCHEDULERS["generate_publish"].interval_seconds = int(prefs["generate_publish_interval_seconds"])
    SCHEDULERS["mentions"].interval_seconds = int(prefs["mentions_interval_seconds"])
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


def _future_stamp(seconds: int) -> str:
    """Return local timestamp offset by given seconds."""
    return (_now_dt() + timedelta(seconds=max(0, int(seconds)))).strftime("%Y-%m-%d %H:%M:%S")


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
    """Execute job command and capture combined output."""
    with JOB_LOCK:
        job.status = "running"

    env = os.environ.copy()
    try:
        proc = subprocess.Popen(
            job.command,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        stdout, _ = proc.communicate()
        output = stdout or ""
        status = "succeeded" if proc.returncode == 0 else "failed"
        return_code = proc.returncode
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

    def _cmd_publish_latest(_: dict[str, str]) -> list[str]:
        platform_arg = _active_platform_arg(_get_active_publish_platforms())
        return [sys.executable, "orchestrator.py", "--platform", platform_arg]

    def _cmd_publish_selected(payload: dict[str, str]) -> list[str] | None:
        run_dir = (payload.get("run_dir") or "").strip()
        if not run_dir:
            return None
        if not RUN_DIR_RE.match(run_dir):
            return None
        platform_arg = _active_platform_arg(_get_active_publish_platforms())
        return [
            sys.executable,
            "orchestrator.py",
            "--run-dir",
            f"output/{run_dir}",
            "--platform",
            platform_arg,
        ]

    def _cmd_mentions(_: dict[str, str]) -> list[str]:
        platform_arg = _active_platform_arg(_get_active_mention_platforms())
        return [sys.executable, "engage.py", "--platform", platform_arg]

    def _cmd_mentions_mastodon(_: dict[str, str]) -> list[str]:
        return [sys.executable, "engage.py", "--platform", "mastodon"]

    def _cmd_mentions_bluesky(_: dict[str, str]) -> list[str]:
        return [sys.executable, "engage.py", "--platform", "bluesky"]

    return {
        "generate": ("Generate Post", _cmd_generate),
        "publish_latest": ("Publish Latest Run", _cmd_publish_latest),
        "publish_selected": ("Publish Selected Run", _cmd_publish_selected),
        "check_mentions": ("Check Mentions (All)", _cmd_mentions),
        "check_mentions_mastodon": ("Check Mentions (Mastodon)", _cmd_mentions_mastodon),
        "check_mentions_bluesky": ("Check Mentions (Bluesky)", _cmd_mentions_bluesky),
    }


def _run_generate_publish_cycle() -> str:
    """Run one scheduled cycle: generate post(s) then publish new runs in order."""
    manifest_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="copierbot-run-manifest-",
        dir=str(BASE_DIR),
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
            return f"main.py failed (rc={gen_job.return_code})."

        created = _parse_run_manifest(manifest_path)
        if not created:
            return "main.py succeeded; no run-manifest entries found."

        active_platforms = _get_active_publish_platforms()
        platform_arg = _active_platform_arg(active_platforms)
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
    platform_arg = _active_platform_arg(_get_active_mention_platforms())
    job = _enqueue_job(
        label="Scheduled Mention Check",
        command=[sys.executable, "engage.py", "--platform", platform_arg],
        background=False,
    )
    if job.status == "succeeded":
        return f"engage.py completed successfully (platform={platform_arg})."
    return f"engage.py failed (platform={platform_arg}, rc={job.return_code})."


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
            state.last_run_at = _now_stamp()
            state.last_result = "running..."
            state.next_run_at = ""

        try:
            result = runner()
        except Exception as exc:  # defensive, runner already handles subprocess statuses
            result = f"runner error: {exc}"

        with SCHED_LOCK:
            if not state.running:
                break
            state.last_result = result
            state.next_run_at = _future_stamp(state.interval_seconds)

        if stop_event.wait(state.interval_seconds):
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


def _render_job_card(job: Job) -> str:
    """Render one job card with metadata and captured output."""
    command_text = " ".join(html.escape(part) for part in job.command)
    output = _render_output_with_links(job.output or "(no output)")
    return_code = "" if job.return_code is None else f" | rc={job.return_code}"
    finished = f" | finished={html.escape(job.finished_at)}" if job.finished_at else ""

    return (
        "<article class='job'>"
        f"<div><strong>#{job.job_id}</strong> {html.escape(job.label)}</div>"
        f"<div class='meta'>started={html.escape(job.started_at)}{finished}{return_code}</div>"
        f"<div class='status {html.escape(job.status)}'>{html.escape(job.status)}</div>"
        f"<div class='meta'>cmd: <code>{command_text}</code></div>"
        "<details><summary>Output</summary>"
        f"<pre>{output}</pre>"
        "</details>"
        "</article>"
    )


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
        if parsed.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._render_index()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/run":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(body)
        payload = {k: (v[0] if v else "") for k, v in form.items()}
        action = (payload.get("action") or "").strip()

        if action == "update_publish_platforms":
            selected = form.get("publish_platform", [])
            _set_active_publish_platforms([str(item) for item in selected])
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if action == "update_mention_platforms":
            selected = form.get("mention_platform", [])
            _set_active_mention_platforms([str(item) for item in selected])
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()
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
            try:
                hours = int(hours_raw)
            except ValueError:
                hours = 1
            if hours not in GENERATE_INTERVAL_HOURS:
                hours = 1
            interval_seconds = hours * 60 * 60
            active_platforms = _get_active_publish_platforms()
            initial_delay_seconds = _compute_generate_initial_delay(
                interval_seconds, active_platforms
            )
            queued_message = "queued"
            if initial_delay_seconds > 0:
                queued_message = (
                    "waiting "
                    f"{_format_wait_duration(initial_delay_seconds)}; "
                    f"recent publish detected for {_active_platform_arg(active_platforms)}."
                )
            _start_scheduler(
                key="generate_publish",
                interval_seconds=interval_seconds,
                runner=_run_generate_publish_cycle,
                initial_delay_seconds=initial_delay_seconds,
                queued_message=queued_message,
            )
        elif action == "stop_generate_scheduler":
            _stop_scheduler("generate_publish")
        elif action == "start_mentions_scheduler":
            mins_raw = (payload.get("mentions_interval_minutes") or "5").strip()
            try:
                minutes = int(mins_raw)
            except ValueError:
                minutes = 5
            if minutes not in MENTION_INTERVAL_MINUTES:
                minutes = 5
            _start_scheduler(
                key="mentions",
                interval_seconds=minutes * 60,
                runner=_run_mention_cycle,
            )
        elif action == "stop_mentions_scheduler":
            _stop_scheduler("mentions")

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        """Silence default HTTP request logging for cleaner terminal output."""
        return

    def _render_index(self) -> None:
        runs = _list_run_dirs()
        persona_state = get_persona_state()
        sched = _scheduler_snapshot()
        with JOB_LOCK:
            jobs = list(reversed(JOBS))

        last_job = jobs[0] if jobs else None
        alert_html = ""
        if last_job:
            alert_html = (
                f"<div class='alert'>Last job: #{last_job.job_id} {html.escape(last_job.label)} "
                f"[{html.escape(last_job.status)}]</div>"
            )

        run_options = "\n".join(
            f"<option value='{html.escape(run)}'>{html.escape(run)}</option>" for run in runs
        )
        if not run_options:
            run_options = "<option value=''>No run folders yet</option>"

        job_cards = "\n".join(_render_job_card(job) for job in jobs) or "<p>No jobs yet.</p>"

        gp = sched["generate_publish"]
        mn = sched["mentions"]
        active_publish_platforms = _get_active_publish_platforms()
        active_publish_platform_arg = _active_platform_arg(active_publish_platforms)
        active_publish_platforms_label = ", ".join(active_publish_platforms)
        active_mention_platforms = _get_active_mention_platforms()
        active_mention_platform_arg = _active_platform_arg(active_mention_platforms)
        active_mention_platforms_label = ", ".join(active_mention_platforms)
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
                f"<span>{name.title()}</span>"
                "</label>"
            )
            for name in PUBLISH_PLATFORM_OPTIONS
        )
        mention_platform_options = "".join(
            (
                "<label style='display:flex;align-items:center;gap:0.5rem;margin:0.35rem 0;'>"
                f"<input type='checkbox' name='mention_platform' value='{name}'"
                f"{' checked' if name in active_mention_platforms else ''} />"
                f"<span>{name.title()}</span>"
                "</label>"
            )
            for name in PUBLISH_PLATFORM_OPTIONS
        )

        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Copierbot Dashboard</title>
  <style>
    :root {{
      --bg: #f4f0e9;
      --ink: #1e1f22;
      --accent: #0f766e;
      --accent2: #9a3412;
      --card: #ffffff;
      --border: #d8d2c5;
      --muted: #6b7280;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Gill Sans", "Trebuchet MS", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 20%, #efe6d4 0%, transparent 35%),
        radial-gradient(circle at 90% 10%, #d9efe6 0%, transparent 40%),
        var(--bg);
      min-height: 100vh;
    }}
    .wrap {{ max-width: 1080px; margin: 0 auto; padding: 1.1rem; }}
    h1 {{ margin: 0 0 0.7rem; font-size: 1.6rem; letter-spacing: 0.01em; }}
    h2 {{ margin: 0 0 0.55rem; font-size: 1rem; }}
    .sub {{ margin: 0 0 1rem; color: var(--muted); font-size: 0.95rem; }}
    .toolbar {{
      display: flex;
      gap: 0.5rem;
      align-items: center;
      margin: 0 0 0.9rem;
      flex-wrap: wrap;
    }}
    .toolbar button {{
      width: auto;
      min-width: 110px;
    }}
    .toolbar label {{
      font-size: 0.9rem;
      color: var(--muted);
    }}
    .alert {{
      border: 1px solid var(--border);
      background: #fffbe8;
      border-left: 4px solid var(--accent2);
      padding: 0.65rem 0.8rem;
      margin-bottom: 1rem;
      border-radius: 8px;
      font-size: 0.95rem;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 0.55rem;
      margin-bottom: 1rem;
    }}
    .stat {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.6rem 0.75rem;
    }}
    .stat .k {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }}
    .stat .v {{ font-size: 1rem; font-weight: 700; margin-top: 0.1rem; }}
    .grid {{ display: grid; gap: 0.9rem; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 0.9rem;
      box-shadow: 0 3px 10px rgba(0,0,0,0.04);
    }}
    .hint {{ margin: 0 0 0.6rem; color: var(--muted); font-size: 0.86rem; line-height: 1.3; }}
    .sched-meta {{ margin: 0.25rem 0; color: var(--muted); font-size: 0.84rem; line-height: 1.35; }}
    .sched-status {{ font-weight: 700; font-size: 0.8rem; text-transform: uppercase; }}
    .sched-status.running {{ color: #166534; }}
    .sched-status.stopped {{ color: #7c2d12; }}
    form {{ margin: 0; }}
    .inline {{ display: flex; gap: 0.45rem; align-items: center; margin-bottom: 0.5rem; }}
    button {{
      width: 100%;
      border: none;
      border-radius: 10px;
      background: linear-gradient(145deg, var(--accent), #115e59);
      color: #fff;
      padding: 0.62rem 0.78rem;
      font-weight: 600;
      cursor: pointer;
    }}
    button.stop {{ background: linear-gradient(145deg, #9a3412, #7c2d12); }}
    button:hover {{ filter: brightness(1.05); }}
    select {{
      width: 100%;
      margin-bottom: 0.5rem;
      padding: 0.5rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: #fff;
    }}
    .jobs {{ margin-top: 1rem; }}
    .job {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-bottom: 0.75rem;
      padding: 0.75rem;
    }}
    .meta {{ color: var(--muted); font-size: 0.85rem; }}
    .status {{ font-weight: 700; text-transform: uppercase; font-size: 0.8rem; }}
    .status.running {{ color: #0f766e; }}
    .status.failed {{ color: #b91c1c; }}
    .status.succeeded {{ color: #166534; }}
    .status.queued {{ color: #7c2d12; }}
    pre {{
      margin: 0.5rem 0 0;
      background: #111827;
      color: #e5e7eb;
      padding: 0.7rem;
      border-radius: 8px;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 0.8rem;
      max-height: 280px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Copierbot Local Dashboard</h1>
    <p class="sub">Local-only controls bound to 127.0.0.1.</p>
    <div class="toolbar">
      <button id="refresh-now" type="button">Refresh Now</button>
      <label>
        <input id="auto-refresh" type="checkbox" />
        Auto refresh (15s)
      </label>
      <span class="meta">Status: <strong id="auto-status">Off</strong></span>
    </div>

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
        <div class="k">Next Phase In</div>
        <div class="v">{20 - (int(persona_state["posts_generated"]) % 20) if str(persona_state["phase"]) != "self_aware" else 0}</div>
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
        <p class="hint">Runs <code>main.py</code>, then publishes all new run folders from that cycle in creation order (normal post first, phase-change post second when present) to active destinations.</p>
        <div class="sched-status {'running' if bool(gp['running']) else 'stopped'}">{'running' if bool(gp['running']) else 'stopped'}</div>
        <p class="sched-meta">Active platforms: {html.escape(active_publish_platforms_label)}</p>
        <p class="sched-meta">Last run: {html.escape(str(gp['last_run_at']) or 'N/A')}</p>
        <p class="sched-meta">Next run: {html.escape(str(gp['next_run_at']) or 'N/A')}</p>
        <p class="sched-meta">Last result: {html.escape(str(gp['last_result']) or 'N/A')}</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="start_generate_scheduler" />
          <select name="generate_interval_hours">{gp_options}</select>
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
    </div>

    <div class="grid" style="margin-top:0.9rem;">
      <div class="card">
        <h2>Generate</h2>
        <p class="hint">Runs <code>main.py</code> to create a new Copierbot post in a fresh timestamped output folder.</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="generate" />
          <button type="submit">Run main.py</button>
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
        <h2>Check Mentions</h2>
        <p class="hint">Runs <code>engage.py</code> to fetch mentions and auto-reply to qualifying wellbeing check-ins. "All Platforms" follows active mention sources.</p>
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
      </div>

      <div class="card">
        <h2>Publish Specific Run</h2>
        <p class="hint">Select a run folder, then publish only that specific output instead of the latest one, to active destinations.</p>
        <p class="sched-meta">Active platforms: {html.escape(active_publish_platforms_label)}</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="publish_selected" />
          <select name="run_dir">{run_options}</select>
          <button type="submit">Publish Selected Folder</button>
        </form>
      </div>
    </div>

    <div class="jobs">
      <h2>Recent Jobs</h2>
      {job_cards}
    </div>
  </div>
  <script>
    (() => {{
      const key = "copierbot_dashboard_auto_refresh";
      const intervalMs = 15000;
      const refreshBtn = document.getElementById("refresh-now");
      const checkbox = document.getElementById("auto-refresh");
      const status = document.getElementById("auto-status");
      let timer = null;

      const apply = (enabled) => {{
        if (timer) {{
          clearInterval(timer);
          timer = null;
        }}
        if (enabled) {{
          timer = window.setInterval(() => window.location.reload(), intervalMs);
          status.textContent = "On (15s)";
        }} else {{
          status.textContent = "Off";
        }}
        checkbox.checked = !!enabled;
        window.localStorage.setItem(key, enabled ? "1" : "0");
      }};

      refreshBtn.addEventListener("click", () => window.location.reload());
      checkbox.addEventListener("change", () => apply(checkbox.checked));
      apply(window.localStorage.getItem(key) === "1");
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


def run_server() -> None:
    """Run local-only dashboard server."""
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
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

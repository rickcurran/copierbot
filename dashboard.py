"""Local-only web dashboard for triggering Copierbot CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
MAX_JOBS = 60
MAX_OUTPUT_CHARS = 12000
HOST = "127.0.0.1"
PORT = 8787


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


JOBS: list[Job] = []
JOB_LOCK = threading.Lock()
NEXT_JOB_ID = 1


def _now_stamp() -> str:
    """Return local timestamp for UI display."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _list_run_dirs() -> list[str]:
    """Return timestamped output run directories, newest first."""
    if not OUTPUT_DIR.exists():
        return []
    runs = [
        p.name
        for p in OUTPUT_DIR.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    ]
    return sorted(runs, reverse=True)


def _trim_output(text: str) -> str:
    """Limit stored output size while keeping newest lines."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "[...output trimmed...]\n" + text[-MAX_OUTPUT_CHARS:]


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


def _enqueue_job(label: str, command: list[str]) -> Job:
    """Create and enqueue a command job for background execution."""
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

    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()
    return job


def _build_actions() -> dict[str, tuple[str, Callable[[dict[str, str]], list[str] | None]]]:
    """Map form action keys to command builders."""

    def _cmd_generate(_: dict[str, str]) -> list[str]:
        return [sys.executable, "main.py"]

    def _cmd_publish_latest(_: dict[str, str]) -> list[str]:
        return [sys.executable, "orchestrator.py"]

    def _cmd_publish_selected(payload: dict[str, str]) -> list[str] | None:
        run_dir = (payload.get("run_dir") or "").strip()
        if not run_dir:
            return None
        return [sys.executable, "orchestrator.py", "--run-dir", f"output/{run_dir}"]

    def _cmd_mentions(_: dict[str, str]) -> list[str]:
        return [sys.executable, "engage.py"]

    return {
        "generate": ("Generate Post", _cmd_generate),
        "publish_latest": ("Publish Latest Run", _cmd_publish_latest),
        "publish_selected": ("Publish Selected Run", _cmd_publish_selected),
        "check_mentions": ("Check Mentions", _cmd_mentions),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Copierbot dashboard."""

    server_version = "CopierbotDashboard/1.0"

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

        actions = _build_actions()
        action_info = actions.get(action)
        if action_info is not None:
            label, cmd_builder = action_info
            command = cmd_builder(payload)
            if command:
                _enqueue_job(label=label, command=command)

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        """Silence default HTTP request logging for cleaner terminal output."""
        return

    def _render_index(self) -> None:
        runs = _list_run_dirs()
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

        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Copierbot Dashboard</title>
  <meta http-equiv="refresh" content="4" />
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
    .wrap {{ max-width: 1024px; margin: 0 auto; padding: 1.1rem; }}
    h1 {{ margin: 0 0 0.7rem; font-size: 1.6rem; letter-spacing: 0.01em; }}
    .sub {{ margin: 0 0 1rem; color: var(--muted); font-size: 0.95rem; }}
    .alert {{
      border: 1px solid var(--border);
      background: #fffbe8;
      border-left: 4px solid var(--accent2);
      padding: 0.65rem 0.8rem;
      margin-bottom: 1rem;
      border-radius: 8px;
      font-size: 0.95rem;
    }}
    .grid {{ display: grid; gap: 0.9rem; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 0.9rem;
      box-shadow: 0 3px 10px rgba(0,0,0,0.04);
    }}
    .card h2 {{ margin: 0 0 0.55rem; font-size: 1rem; }}
    .hint {{ margin: 0 0 0.6rem; color: var(--muted); font-size: 0.86rem; line-height: 1.3; }}
    form {{ margin: 0; }}
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
    <p class="sub">Local-only controls bound to 127.0.0.1. Auto-refreshes every 4 seconds.</p>
    {alert_html}

    <div class="grid">
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
        <p class="hint">Runs <code>orchestrator.py</code> to publish the newest generated run to Mastodon.</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="publish_latest" />
          <button type="submit">Run orchestrator.py</button>
        </form>
      </div>

      <div class="card">
        <h2>Check Mentions</h2>
        <p class="hint">Runs <code>engage.py</code> to fetch mentions and auto-reply to qualifying wellbeing check-ins.</p>
        <form method="post" action="/run">
          <input type="hidden" name="action" value="check_mentions" />
          <button type="submit">Run engage.py</button>
        </form>
      </div>

      <div class="card">
        <h2>Publish Specific Run</h2>
        <p class="hint">Select a run folder, then publish only that specific output instead of the latest one.</p>
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
</body>
</html>
"""
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _render_job_card(job: Job) -> str:
    """Render one job card with metadata and captured output."""
    command_text = " ".join(html.escape(part) for part in job.command)
    output = html.escape(job.output or "(no output)")
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


def run_server() -> None:
    """Run local-only dashboard server."""
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Copierbot dashboard listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()

"""Slack DM control listener for Copierbot using a separate Socket Mode app."""

from __future__ import annotations

import argparse
import atexit
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from dotenv import load_dotenv

from persona import get_persona_state

try:
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web import WebClient
except ImportError:  # pragma: no cover - runtime dependency check only
    SocketModeClient = None  # type: ignore[assignment]
    SocketModeRequest = Any  # type: ignore[assignment]
    SocketModeResponse = None  # type: ignore[assignment]
    WebClient = Any  # type: ignore[assignment]


BASE_DIR = Path(__file__).resolve().parent
PID_PATH = BASE_DIR / "data" / "slack_control.pid"
URL_RE = re.compile(r"https?://[^\s<>'\"]+")
PLATFORM_OPTIONS = ("mastodon", "bluesky", "wordpress", "instagram", "all")


@dataclass(frozen=True)
class SlackControlConfig:
    """Runtime configuration for the Slack control listener."""

    bot_token: str
    app_token: str
    allowed_user_ids: set[str]


@dataclass(frozen=True)
class ParsedCommand:
    """Parsed control command plus subprocess invocation details."""

    label: str
    command: list[str]
    help_text: str = ""


def _setup_logging() -> None:
    """Configure concise CLI logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _require_sdk() -> None:
    """Fail with a helpful message when slack_sdk is not installed."""
    if SocketModeClient is None or SocketModeResponse is None:
        raise RuntimeError(
            "Missing dependency 'slack_sdk'. Run `pip install -r requirements.txt` "
            "to enable Slack control."
        )


def _load_config() -> SlackControlConfig:
    """Load Slack control credentials from environment."""
    load_dotenv()
    bot_token = os.getenv("SLACK_CONTROL_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_CONTROL_APP_TOKEN", "").strip()
    allowed_raw = os.getenv("SLACK_CONTROL_ALLOWED_USER_IDS", "").strip()
    allowed = {
        item.strip()
        for item in allowed_raw.split(",")
        if item.strip()
    }

    missing: list[str] = []
    if not bot_token:
        missing.append("SLACK_CONTROL_BOT_TOKEN")
    if not app_token:
        missing.append("SLACK_CONTROL_APP_TOKEN")
    if not allowed:
        missing.append("SLACK_CONTROL_ALLOWED_USER_IDS")
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Missing required Slack control environment variables: {joined}."
        )

    return SlackControlConfig(
        bot_token=bot_token,
        app_token=app_token,
        allowed_user_ids=allowed,
    )


def _pid_is_running(pid: int) -> bool:
    """Return True when a process id is alive."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _write_pid_lock() -> None:
    """Prevent duplicate Slack control listeners from starting."""
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PID_PATH.exists():
        raw = PID_PATH.read_text(encoding="utf-8").strip()
        try:
            existing_pid = int(raw)
        except ValueError:
            existing_pid = 0
        if existing_pid and _pid_is_running(existing_pid):
            raise RuntimeError(
                f"Slack control listener already running with pid {existing_pid}."
            )
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")


def _clear_pid_lock() -> None:
    """Remove pid lock when current process exits."""
    try:
        if PID_PATH.exists():
            raw = PID_PATH.read_text(encoding="utf-8").strip()
            if raw == str(os.getpid()):
                PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _help_text() -> str:
    """Return user-facing Slack command help."""
    return (
        "Available commands:\n"
        "- help\n"
        "- ping\n"
        "- status\n"
        "- generate\n"
        "- generate https://example.com/article\n"
        "- publish latest to mastodon|bluesky|wordpress|instagram|all\n"
        "- check mentions\n"
        "- check mentions instagram\n"
    )


def _extract_url(text: str) -> str:
    """Return the first URL found in the message."""
    match = URL_RE.search(text or "")
    return match.group(0).strip() if match else ""


def _extract_platform_arg(text: str) -> str:
    """Extract one or more requested platforms from free text."""
    lowered = (text or "").strip().lower()
    found: list[str] = []
    for platform in PLATFORM_OPTIONS:
        pattern = rf"(?<![a-z]){re.escape(platform)}(?![a-z])"
        if re.search(pattern, lowered) and platform not in found:
            found.append(platform)

    if not found:
        return ""
    if "all" in found:
        return "all"
    return ",".join(found)


def _parse_command(text: str) -> ParsedCommand | None:
    """Map one Slack DM body to a safe Copierbot subprocess command."""
    value = (text or "").strip()
    lowered = value.lower()
    python = sys.executable

    if not value:
        return None
    if lowered in {"help", "commands", "?"}:
        return ParsedCommand(label="Help", command=[], help_text=_help_text())
    if lowered == "ping":
        return ParsedCommand(label="Ping", command=[], help_text="pong")
    if lowered == "status":
        return ParsedCommand(label="Status", command=[], help_text="")

    if lowered.startswith("generate"):
        article_url = _extract_url(value)
        if article_url:
            return ParsedCommand(
                label="Generate From URL",
                command=[python, "main.py", "--article-url", article_url],
            )
        return ParsedCommand(label="Generate", command=[python, "main.py"])

    if lowered.startswith("publish"):
        platform_arg = _extract_platform_arg(value)
        if not platform_arg:
            return ParsedCommand(
                label="Publish Help",
                command=[],
                help_text=(
                    "Use `publish latest to mastodon`, `publish latest to instagram`, "
                    "or `publish latest to all`."
                ),
            )
        return ParsedCommand(
            label=f"Publish Latest [{platform_arg}]",
            command=[python, "orchestrator.py", "--platform", platform_arg],
        )

    if lowered.startswith("check mentions") or lowered.startswith("mentions"):
        platform_arg = _extract_platform_arg(value) or "all"
        return ParsedCommand(
            label=f"Check Mentions [{platform_arg}]",
            command=[python, "engage.py", "--platform", platform_arg],
        )

    return ParsedCommand(label="Help", command=[], help_text=_help_text())


def _format_status_text() -> str:
    """Return compact Copierbot status for Slack DM responses."""
    persona_state = get_persona_state()
    return (
        "Copierbot status:\n"
        f"- phase: {persona_state.get('phase', 'unknown')}\n"
        f"- seasonal_phase: {persona_state.get('seasonal_phase', 'none')}\n"
        f"- posts_generated: {int(persona_state.get('posts_generated', 0))}"
    )


def _truncate_output(text: str, limit: int = 1400) -> str:
    """Trim subprocess output for compact Slack delivery."""
    normalized = (text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _post_message(client: WebClient, channel: str, text: str, thread_ts: str = "") -> None:
    """Send one DM back to Slack."""
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    client.chat_postMessage(**payload)


def _run_command_and_reply(
    *,
    client: WebClient,
    channel: str,
    thread_ts: str,
    parsed: ParsedCommand,
) -> None:
    """Execute one Copierbot command and report the result back to Slack."""
    if parsed.help_text and not parsed.command:
        _post_message(client, channel, parsed.help_text, thread_ts=thread_ts)
        return
    if parsed.label == "Status":
        _post_message(client, channel, _format_status_text(), thread_ts=thread_ts)
        return

    _post_message(
        client,
        channel,
        f"Running `{parsed.label}`...",
        thread_ts=thread_ts,
    )

    completed = subprocess.run(
        parsed.command,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
    )
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
    output = _truncate_output(output)
    if completed.returncode == 0:
        message = f"`{parsed.label}` succeeded."
    else:
        message = f"`{parsed.label}` failed with exit code {completed.returncode}."
    if output:
        message += f"\n```text\n{output}\n```"
    _post_message(client, channel, message, thread_ts=thread_ts)


def _handle_message_event(
    *,
    client: SocketModeClient,
    web_client: WebClient,
    payload: dict[str, Any],
    allowed_user_ids: set[str],
    bot_user_id: str,
) -> None:
    """Process one Slack message event when it is an allowed DM."""
    event = payload.get("event", {}) or {}
    if event.get("type") != "message":
        return
    if event.get("channel_type") != "im":
        return
    if event.get("subtype"):
        return

    user_id = str(event.get("user", "")).strip()
    if not user_id or user_id == bot_user_id:
        return
    if user_id not in allowed_user_ids:
        logging.info("Ignoring DM from unauthorized Slack user %s.", user_id)
        return

    text = str(event.get("text", "")).strip()
    channel = str(event.get("channel", "")).strip()
    thread_ts = str(event.get("ts", "")).strip()
    parsed = _parse_command(text)
    if parsed is None or not channel:
        return

    logging.info("Accepted Slack control command from %s: %s", user_id, text)
    worker = threading.Thread(
        target=_run_command_and_reply,
        kwargs={
            "client": web_client,
            "channel": channel,
            "thread_ts": thread_ts,
            "parsed": parsed,
        },
        daemon=True,
    )
    worker.start()


def run() -> None:
    """Start the Slack DM control listener."""
    _setup_logging()
    _require_sdk()
    config = _load_config()
    _write_pid_lock()
    atexit.register(_clear_pid_lock)

    web_client = WebClient(token=config.bot_token)
    auth = web_client.auth_test()
    bot_user_id = str(auth.get("user_id", "")).strip()
    if not bot_user_id:
        raise RuntimeError("Failed to resolve Slack bot user id via auth.test.")

    client = SocketModeClient(app_token=config.app_token, web_client=web_client)

    def _process_request(socket_client: SocketModeClient, req: SocketModeRequest) -> None:
        if req.type != "events_api":
            return
        response = SocketModeResponse(envelope_id=req.envelope_id)
        socket_client.send_socket_mode_response(response)
        _handle_message_event(
            client=socket_client,
            web_client=web_client,
            payload=req.payload,
            allowed_user_ids=config.allowed_user_ids,
            bot_user_id=bot_user_id,
        )

    client.socket_mode_request_listeners.append(_process_request)
    client.connect()
    logging.info(
        "Slack control listener connected. Allowed Slack user ids: %s",
        ", ".join(sorted(config.allowed_user_ids)),
    )

    stop_event = threading.Event()

    def _signal_handler(signum, _frame) -> None:  # type: ignore[no-untyped-def]
        logging.info("Received signal %s, shutting down Slack control listener.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    while not stop_event.is_set():
        time.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Slack DM control listener.")
    args = parser.parse_args()
    try:
        run()
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s | %(message)s")
        logging.error("Slack control listener failed: %s", exc)
        raise SystemExit(1)

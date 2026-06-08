"""Alerting and error classification helpers for Copierbot."""

from __future__ import annotations

from functools import lru_cache
import os
import re
import socket

from dotenv import load_dotenv
import requests


OPENAI_FATAL_CATEGORIES = {"quota_exhausted", "auth_failed"}


def classify_openai_error_text(text: str) -> str:
    """Classify OpenAI-like error text into actionable categories."""
    value = (text or "").lower()
    if not value:
        return "unknown"

    if re.search(
        r"insufficient_quota|exceeded your current quota|billing|credit|payment|required|quota",
        value,
    ):
        return "quota_exhausted"

    if re.search(
        r"invalid[_\s-]?api[_\s-]?key|incorrect api key|authentication|unauthorized|401",
        value,
    ):
        return "auth_failed"

    if re.search(r"rate limit|too many requests|429", value):
        return "rate_limited"

    if re.search(r"safety|rejected by the safety system|policy", value):
        return "safety_rejected"

    if re.search(r"connection error|timed out|timeout|temporary|dns|network", value):
        return "network_error"

    return "unknown"


def is_fatal_openai_category(category: str) -> bool:
    """Return True when scheduler should stop and require human intervention."""
    return (category or "").strip().lower() in OPENAI_FATAL_CATEGORIES


def openai_category_action_text(category: str) -> str:
    """Return one operator-facing action line for an OpenAI failure category."""
    normalized = (category or "").strip().lower()
    if normalized == "quota_exhausted":
        return "OpenAI API quota/billing appears exhausted. Top up API credits or fix billing, then retry."
    if normalized == "auth_failed":
        return "OpenAI authentication failed. Check the API key or related credentials, then retry."
    if normalized == "rate_limited":
        return "OpenAI rate limit hit. Wait and retry once the limit window clears."
    if normalized == "safety_rejected":
        return "OpenAI rejected the request on safety/policy grounds. Review the prompt and input content."
    if normalized == "network_error":
        return "OpenAI request looks like a network issue. Check connectivity and retry."
    return "Review the error details and retry after fixing the underlying issue."


@lru_cache(maxsize=1)
def _slack_webhook_url() -> str:
    """Load Slack webhook URL from environment or .env."""
    load_dotenv()
    return os.getenv("SLACK_WEBHOOK_URL", "").strip()


def _hostname() -> str:
    """Return local host label for alert context."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def send_slack_alert(title: str, message: str) -> bool:
    """Send a simple Slack webhook alert; returns False if not configured/failed."""
    webhook_url = _slack_webhook_url()
    if not webhook_url:
        return False

    text = f"*{title.strip()}*\n{message.strip()}\nHost: `{_hostname()}`"
    payload = {"text": text}
    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        return False
    return True

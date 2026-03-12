"""Persist timestamped mention reply logs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


OUTPUT_DIR = Path("output")
MENTION_DIR = OUTPUT_DIR / "mention_responses"


def _timestamp() -> str:
    """Return local timestamp down to seconds."""
    return datetime.now().astimezone().strftime("%Y-%m-%d-%H-%M-%S")


def save_mention_response_log(
    mention_id: str,
    author: str,
    original_text: str,
    reply_text: str,
    response_url: str = "",
    output_root: Path = OUTPUT_DIR,
) -> Path:
    """Save one mention response as timestamped text file."""
    folder = output_root / "mention_responses"
    folder.mkdir(parents=True, exist_ok=True)

    timestamp = _timestamp()
    path = folder / f"mention_response  {timestamp}.txt"
    suffix = 1
    while path.exists():
        path = folder / f"mention_response  {timestamp}-{suffix}.txt"
        suffix += 1

    content = (
        "SYSTEM LOG\n"
        "Type: mention_response\n"
        f"Timestamp: {timestamp}\n"
        f"Mention ID: {mention_id}\n"
        f"Author: {author or 'unknown'}\n"
        f"Mention Text: {original_text or 'N/A'}\n"
        f"Response URL: {response_url or 'N/A'}\n\n"
        "Reply:\n"
        f"{reply_text.strip()}"
    )
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path

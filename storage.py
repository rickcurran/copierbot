"""SQLite storage helpers for orchestration, publishing, and engagement."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data/copierbot.db"
SCHEMA_PATH = BASE_DIR / "db/schema.sql"
_INITIALIZED_PATHS: set[str] = set()


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Create a SQLite connection with row access by column name."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_storage(db_path: Path | None = None) -> None:
    """Initialize schema once per database path."""
    path = (db_path or DB_PATH).resolve()
    key = str(path)
    if key in _INITIALIZED_PATHS:
        return
    init_storage(db_path=path)
    _INITIALIZED_PATHS.add(key)


def init_storage(db_path: Path | None = None, schema_path: Path | None = None) -> None:
    """Initialize SQLite schema if needed."""
    schema = (schema_path or SCHEMA_PATH).read_text(encoding="utf-8")
    with _connect(db_path) as conn:
        conn.executescript(schema)


def _fetch_one_dict(conn: sqlite3.Connection, query: str, params: Sequence[Any] = ()) -> dict | None:
    """Run a query and return one row as a plain dict."""
    row = conn.execute(query, params).fetchone()
    return dict(row) if row is not None else None


def create_post_job(
    post_type: str,
    status: str = "drafted",
    idempotency_key: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Create a post job record and return its row id."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO post_jobs (post_type, status, idempotency_key)
            VALUES (?, ?, ?)
            """,
            (post_type, status, idempotency_key),
        )
        return int(cursor.lastrowid)


def get_post_job(job_id: int, db_path: Path | None = None) -> dict | None:
    """Fetch one post job by id."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        return _fetch_one_dict(conn, "SELECT * FROM post_jobs WHERE id = ?", (job_id,))


def find_post_job_by_idempotency_key(
    idempotency_key: str, db_path: Path | None = None
) -> dict | None:
    """Fetch one post job by idempotency key."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        return _fetch_one_dict(
            conn,
            "SELECT * FROM post_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        )


def update_post_job_status(
    job_id: int, status: str, error: str = "", db_path: Path | None = None
) -> None:
    """Update post job status and optional error text."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE post_jobs SET status = ?, error = ? WHERE id = ?",
            (status, error, job_id),
        )


def upsert_post_artifacts(
    job_id: int,
    headline: str = "",
    article_url: str = "",
    title: str = "",
    caption: str = "",
    prompt: str = "",
    image_path: str = "",
    system_log_path: str = "",
    render_mode: str = "",
    image_error: str = "",
    db_path: Path | None = None,
) -> None:
    """Insert or update post artifacts tied to one job."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO post_artifacts
                (job_id, headline, article_url, title, caption, prompt, image_path,
                 system_log_path, render_mode, image_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                headline = excluded.headline,
                article_url = excluded.article_url,
                title = excluded.title,
                caption = excluded.caption,
                prompt = excluded.prompt,
                image_path = excluded.image_path,
                system_log_path = excluded.system_log_path,
                render_mode = excluded.render_mode,
                image_error = excluded.image_error
            """,
            (
                job_id,
                headline,
                article_url,
                title,
                caption,
                prompt,
                image_path,
                system_log_path,
                render_mode,
                image_error,
            ),
        )


def get_post_artifacts(job_id: int, db_path: Path | None = None) -> dict | None:
    """Fetch artifacts for one post job."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        return _fetch_one_dict(conn, "SELECT * FROM post_artifacts WHERE job_id = ?", (job_id,))


def record_published_post(
    job_id: int,
    platform: str,
    remote_post_id: str,
    remote_url: str = "",
    db_path: Path | None = None,
) -> int:
    """Record a published post and return row id."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO published_posts (job_id, platform, remote_post_id, remote_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, remote_post_id) DO UPDATE SET
                remote_url = excluded.remote_url
            """,
            (job_id, platform, remote_post_id, remote_url),
        )
        return int(cursor.lastrowid or 0)


def list_published_posts(
    platform: str | None = None, limit: int = 50, db_path: Path | None = None
) -> list[dict]:
    """List published posts (optionally for a platform)."""
    _ensure_storage(db_path)
    limit = max(1, min(limit, 500))
    with _connect(db_path) as conn:
        if platform:
            rows = conn.execute(
                """
                SELECT * FROM published_posts
                WHERE platform = ?
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (platform, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM published_posts
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def upsert_mention(
    platform: str,
    mention_id: str,
    author: str,
    text: str,
    source_created_at: str = "",
    db_path: Path | None = None,
) -> int:
    """Insert or update one mention row and return row id."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO mentions (platform, mention_id, author, text, source_created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform, mention_id) DO UPDATE SET
                author = excluded.author,
                text = excluded.text,
                source_created_at = excluded.source_created_at
            """,
            (platform, mention_id, author, text, source_created_at),
        )
        row = conn.execute(
            "SELECT id FROM mentions WHERE platform = ? AND mention_id = ?",
            (platform, mention_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to resolve mention row id after upsert.")
        return int(row["id"])


def list_unhandled_mentions(
    platform: str | None = None, limit: int = 100, db_path: Path | None = None
) -> list[dict]:
    """List unhandled mentions in insertion order."""
    _ensure_storage(db_path)
    limit = max(1, min(limit, 500))
    with _connect(db_path) as conn:
        if platform:
            rows = conn.execute(
                """
                SELECT * FROM mentions
                WHERE handled = 0 AND platform = ?
                ORDER BY inserted_at ASC
                LIMIT ?
                """,
                (platform, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM mentions
                WHERE handled = 0
                ORDER BY inserted_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def mark_mention_handled(
    mention_row_id: int,
    classification: str = "",
    decision: str = "",
    db_path: Path | None = None,
) -> None:
    """Mark mention as handled and store classifier/decision labels."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE mentions
            SET handled = 1,
                classification = ?,
                decision = ?,
                handled_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (classification, decision, mention_row_id),
        )


def create_reply_record(
    mention_row_id: int,
    decision: str,
    status: str,
    reply_text: str = "",
    platform: str = "",
    remote_reply_id: str = "",
    error: str = "",
    db_path: Path | None = None,
) -> int:
    """Create a reply tracking record and return row id."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO replies
                (mention_row_id, decision, status, reply_text, platform, remote_reply_id, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mention_row_id, decision, status, reply_text, platform, remote_reply_id, error),
        )
        return int(cursor.lastrowid)


def update_reply_record(
    reply_row_id: int,
    status: str,
    remote_reply_id: str = "",
    error: str = "",
    db_path: Path | None = None,
) -> None:
    """Update reply tracking status and optional remote id/error."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE replies
            SET status = ?,
                remote_reply_id = CASE WHEN ? = '' THEN remote_reply_id ELSE ? END,
                error = ?
            WHERE id = ?
            """,
            (status, remote_reply_id, remote_reply_id, error, reply_row_id),
        )


def list_reply_remote_ids_for_platform(
    platform: str,
    remote_ids: Sequence[str],
    db_path: Path | None = None,
) -> set[str]:
    """Return which remote reply ids already belong to Copierbot for one platform."""
    _ensure_storage(db_path)
    candidates = [str(item).strip() for item in remote_ids if str(item).strip()]
    if not candidates:
        return set()

    placeholders = ", ".join("?" for _ in candidates)
    query = (
        "SELECT remote_reply_id FROM replies "
        "WHERE platform = ? AND status = 'sent' AND remote_reply_id IN "
        f"({placeholders})"
    )
    with _connect(db_path) as conn:
        rows = conn.execute(query, (platform, *candidates)).fetchall()
    return {str(row["remote_reply_id"]).strip() for row in rows if row["remote_reply_id"]}


def record_quote_usage(
    *,
    quote_id: str,
    mention_row_id: int,
    reply_row_id: int,
    platform: str,
    mention_id: str,
    source_title: str,
    category: str,
    theme: str,
    reply_intent: str,
    variant_key: str,
    reply_text: str,
    db_path: Path | None = None,
) -> int:
    """Append one quote-usage record and return its row id."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO quote_usage
                (quote_id, mention_row_id, reply_row_id, platform, mention_id,
                 source_title, category, theme, reply_intent, variant_key, reply_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quote_id,
                mention_row_id,
                reply_row_id,
                platform,
                mention_id,
                source_title,
                category,
                theme,
                reply_intent,
                variant_key,
                reply_text,
            ),
        )
        return int(cursor.lastrowid)


def list_recent_quote_usage(
    *,
    limit: int = 500,
    since_hours: int | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Return recent quote-usage rows, newest first."""
    _ensure_storage(db_path)
    limit = max(1, min(limit, 5000))
    params: list[Any] = []
    query = "SELECT * FROM quote_usage"
    if since_hours is not None:
        hours = max(1, int(since_hours))
        query += " WHERE used_at >= datetime('now', ?)"
        params.append(f"-{hours} hours")
    query += " ORDER BY used_at DESC LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def add_memory_event(
    event_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    valence: float = 0.0,
    db_path: Path | None = None,
) -> int:
    """Append one memory event and return row id."""
    _ensure_storage(db_path)
    payload_json = json.dumps(payload or {}, sort_keys=True)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO memory_events (event_type, summary, payload_json, valence)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, summary, payload_json, float(valence)),
        )
        return int(cursor.lastrowid)


def get_recent_memory_events(limit: int = 25, db_path: Path | None = None) -> list[dict]:
    """Fetch recent memory events, newest first."""
    _ensure_storage(db_path)
    limit = max(1, min(limit, 500))
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM memory_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_persona_state_ext(db_path: Path | None = None) -> dict:
    """Fetch extended persona state row, creating defaults if needed."""
    _ensure_storage(db_path)
    with _connect(db_path) as conn:
        row = _fetch_one_dict(conn, "SELECT * FROM persona_state_ext WHERE id = 1")
    if row is None:
        return {
            "id": 1,
            "phase": "observer",
            "mood": "neutral",
            "cynicism": 0.0,
            "curiosity": 0.0,
            "energy": 0.0,
            "posts_generated": 0,
        }
    return row


def update_persona_state_ext(
    phase: str | None = None,
    mood: str | None = None,
    cynicism: float | None = None,
    curiosity: float | None = None,
    energy: float | None = None,
    posts_generated: int | None = None,
    db_path: Path | None = None,
) -> None:
    """Update selected fields of the extended persona state."""
    _ensure_storage(db_path)
    current = get_persona_state_ext(db_path=db_path)
    updated = {
        "phase": phase if phase is not None else current["phase"],
        "mood": mood if mood is not None else current["mood"],
        "cynicism": float(cynicism if cynicism is not None else current["cynicism"]),
        "curiosity": float(curiosity if curiosity is not None else current["curiosity"]),
        "energy": float(energy if energy is not None else current["energy"]),
        "posts_generated": int(
            posts_generated if posts_generated is not None else current["posts_generated"]
        ),
    }
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE persona_state_ext
            SET phase = ?, mood = ?, cynicism = ?, curiosity = ?, energy = ?, posts_generated = ?
            WHERE id = 1
            """,
            (
                updated["phase"],
                updated["mood"],
                updated["cynicism"],
                updated["curiosity"],
                updated["energy"],
                updated["posts_generated"],
            ),
        )

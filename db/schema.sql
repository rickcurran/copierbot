PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS post_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_type TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS post_artifacts (
    job_id INTEGER PRIMARY KEY,
    headline TEXT NOT NULL DEFAULT '',
    article_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    system_log_path TEXT NOT NULL DEFAULT '',
    render_mode TEXT NOT NULL DEFAULT '',
    image_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES post_jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS published_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    remote_post_id TEXT NOT NULL,
    remote_url TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES post_jobs(id) ON DELETE CASCADE,
    UNIQUE(platform, remote_post_id)
);

CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    mention_id TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    handled INTEGER NOT NULL DEFAULT 0,
    source_created_at TEXT NOT NULL DEFAULT '',
    inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    handled_at TEXT NOT NULL DEFAULT '',
    UNIQUE(platform, mention_id)
);

CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mention_row_id INTEGER NOT NULL,
    decision TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    remote_reply_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (mention_row_id) REFERENCES mentions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    valence REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS persona_state_ext (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    phase TEXT NOT NULL DEFAULT 'observer',
    mood TEXT NOT NULL DEFAULT 'neutral',
    cynicism REAL NOT NULL DEFAULT 0.0,
    curiosity REAL NOT NULL DEFAULT 0.0,
    energy REAL NOT NULL DEFAULT 0.0,
    posts_generated INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO persona_state_ext (id, phase, mood, cynicism, curiosity, energy, posts_generated)
VALUES (1, 'observer', 'neutral', 0.0, 0.0, 0.0, 0)
ON CONFLICT(id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_post_jobs_status_created_at
ON post_jobs(status, created_at);

CREATE INDEX IF NOT EXISTS idx_mentions_handled_inserted_at
ON mentions(handled, inserted_at);

CREATE INDEX IF NOT EXISTS idx_replies_mention_row_id
ON replies(mention_row_id);

CREATE TRIGGER IF NOT EXISTS trg_post_jobs_updated_at
AFTER UPDATE ON post_jobs
FOR EACH ROW
BEGIN
    UPDATE post_jobs
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_post_artifacts_updated_at
AFTER UPDATE ON post_artifacts
FOR EACH ROW
BEGIN
    UPDATE post_artifacts
    SET updated_at = CURRENT_TIMESTAMP
    WHERE job_id = NEW.job_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_replies_updated_at
AFTER UPDATE ON replies
FOR EACH ROW
BEGIN
    UPDATE replies
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_persona_state_ext_updated_at
AFTER UPDATE ON persona_state_ext
FOR EACH ROW
BEGIN
    UPDATE persona_state_ext
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.id;
END;

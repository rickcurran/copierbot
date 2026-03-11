# Copierbot

Copierbot is a local Python CLI project that simulates an AI office photocopier generating surreal robot-themed art posts.

## What it does

Running `python main.py` now chooses a post type:

1. `news` post (80% probability)
2. `system_log` post (20% probability)

### News post flow

1. Fetch and filter headlines from NewsAPI.org
2. Select one headline
3. Apply neutral pseudonymization to likely person names
4. Generate a short title (5-10 words)
5. Generate collage concept + image prompt
6. Generate image
7. Generate caption
8. Save outputs

### System log flow

1. Generate randomized copier diagnostics
2. Generate a dry, philosophical system log text
3. Save output

## Persona evolution

Copierbot has 4 phases:

1. `observer`
2. `skeptic`
3. `philosopher`
4. `self_aware`

State file:

- `data/persona_state.json`

Format:

```json
{
  "phase": "observer",
  "posts_generated": 0
}
```

Evolution rule:

- After every 20 generated posts, Copierbot advances to the next phase.
- Phase stops advancing after `self_aware`.

Persona context is injected into:

- `creative.generate_collage_concept_and_prompt(...)`
- `caption.generate_caption(...)`
- `system_log.generate_system_log(...)`

## Output files

Each run creates a timestamped folder under `output/`:

- `output/<timestamp>/...`

Example:

- `output/2026-03-10-11-03-27/`

### News post

`prompt.txt` includes:

- generated title
- original headline
- source article URL
- anonymized headline used for generation
- story context and extracted source visual cues
- image render mode (`openai_image` or `ascii_fallback`)
- image error context when fallback is used
- final image prompt

Saved files:

- `output/<timestamp>/image  <timestamp>.png`
- `output/<timestamp>/caption  <timestamp>.txt`
- `output/<timestamp>/prompt  <timestamp>.txt`

If OpenAI image generation fails, Copierbot creates a local fallback PNG at the same image path using ASCII-art diagnostics.  
The fallback mood is influenced by the API error category (for example: safety rejection, rate limit, auth, network, or inspiration drought).

### System log post

- `output/<timestamp>/system_log  <timestamp>.txt`

## Headline filtering behavior

- Tech/quirky/geek themes are prioritized
- Mainstream sports are filtered unless strong tech context is present
- NFL/american-football and soccer topics are explicitly blocked, except Super Bowl cultural-event coverage
- Trump-related and Iran-war-related topics are explicitly blocked
- Heavily political and immigration-focused topics are filtered
- Tabloid and selected hard-blocked sources are filtered
- If `RSS Feeds.opml` exists, matching source names/domains get a ranking boost
- Source article metadata and image-alt cues are extracted (when available) to ground surreal prompts in story-specific details

## Requirements

- Python 3.11+
- NewsAPI key from [NewsAPI.org](https://newsapi.org/)
- OpenAI API key

## Installation

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Configure environment variables:

```bash
cp .env.example .env
```

Then edit `.env`:

```env
OPENAI_API_KEY=your_openai_api_key_here
NEWS_API_KEY=your_newsapi_key_here
NEWS_COUNTRY=us
NEWS_PAGE_SIZE=40
POST_MODE=default
MASTODON_MAX_CHARS=500
MASTODON_BASE_URL=https://mastodon.social
MASTODON_ACCESS_TOKEN=your_mastodon_access_token_here
MASTODON_VISIBILITY=unlisted
```

Post length modes:

- `POST_MODE=default`: allows longer generated captions/system logs.
- `POST_MODE=mastodon`: generation uses `MASTODON_MAX_CHARS` as a hard character constraint (no truncation; it rewrites to fit).

## Run

```bash
python main.py
```

## Local Web Dashboard

Run a local-only web interface with buttons for generate/publish/mentions:

```bash
python dashboard.py
```

Then open:

- `http://127.0.0.1:8787`

Available actions:

- Run `main.py` (generate post)
- Run `orchestrator.py` (publish latest run)
- Run `engage.py` (check mentions/reply)
- Publish a selected timestamped run folder

Notes:

- Dashboard binds only to `127.0.0.1` (local machine only).
- Commands run in background and show live status/output in the page.
- Mention replies remain text-only (`engage.py` never calls image generation).

## Publish To Mastodon

Publish the latest run folder:

```bash
python orchestrator.py
```

Publish a specific run folder:

```bash
python orchestrator.py --run-dir output/2026-03-11-10-30-00
```

Override visibility for one publish:

```bash
python orchestrator.py --visibility public
```

Publish behavior:

- Uses idempotent job tracking in SQLite (`data/copierbot.db`) to avoid duplicate posts.
- News run: uploads image + posts caption text.
- System log run: posts system-log text only.

## Monitor Mentions And Reply

Process Mastodon mentions and auto-reply to wellbeing check-ins:

```bash
python engage.py
```

Optional flags:

```bash
python engage.py --fetch-limit 30 --process-limit 30
```

Behavior:

- Fetches `mention` notifications from Mastodon.
- Stores mentions in SQLite and processes unhandled rows.
- If mention text matches patterns like "How are you?", Copierbot generates a local `system_log` style reply and posts it as a reply.
- Other mentions are marked handled with `no_reply` so they are not repeatedly reprocessed.
- Replies are tracked in `replies` table and successful replies increment persona post count.
- `engage.py` does not call OpenAI APIs (text or image).

## Persistence Layer

Copierbot now includes a SQLite storage layer for upcoming orchestration and social integrations:

- `storage.py` provides helper functions for jobs, artifacts, publish records, mentions, replies, memory events, and extended persona state.
- `db/schema.sql` defines the schema.
- Default database path: `data/copierbot.db`.

## Project structure

```text
copierbot/
    main.py
    dashboard.py
    engage.py
    orchestrator.py
    article_context.py
    ascii_fallback.py
    persona.py
    system_log.py
    news.py
    creative.py
    image_gen.py
    caption.py
    title_gen.py
    anonymize.py
    storage.py
    social/
        mastodon_adapter.py
    config.py
    db/
        schema.sql
    data/
        persona_state.json
        copierbot.db
    output/
    requirements.txt
    .env.example
```

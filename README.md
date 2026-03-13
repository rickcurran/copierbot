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
3. Render a branded square system-log card image from `assets/templates/system_log_card.png`
4. Save outputs

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
- headline used for generation (obfuscated only if enabled)
- story context and extracted source visual cues
- image render mode (`openai_image` or `ascii_fallback`)
- image error context when fallback is used
- final image prompt

Saved files:

- `output/<timestamp>/image  <timestamp>.jpg`
- `output/<timestamp>/caption  <timestamp>.txt`
- `output/<timestamp>/prompt  <timestamp>.txt`

If OpenAI image generation fails, Copierbot creates a local fallback image at the same image path using ASCII-art diagnostics.  
The fallback mood is influenced by the API error category (for example: safety rejection, rate limit, auth, network, or inspiration drought).

### System log post

- `output/<timestamp>/system_log  <timestamp>.txt`
- `output/<timestamp>/system_log_card  <timestamp>.png`
- On persona phase transitions (every 20 posts), Copierbot creates an additional normal timestamped system-log run folder (local-only, <=250 chars).

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
MASTODON_MAX_CHARS=300
ENABLE_NAME_OBFUSCATION=false
MASTODON_BASE_URL=https://mastodon.social
MASTODON_ACCESS_TOKEN=your_mastodon_access_token_here
MASTODON_VISIBILITY=unlisted
BLUESKY_PDS_URL=https://bsky.social
BLUESKY_HANDLE=your.handle.bsky.social
BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
BLUESKY_MAX_CHARS=300
```

Post length modes:

- Generated captions/system logs use a cross-platform hard limit:
  `min(MASTODON_MAX_CHARS, BLUESKY_MAX_CHARS)`.
- With the defaults above, generated text is capped at `300` chars.
- `ENABLE_NAME_OBFUSCATION=false` (default): uses original headlines.
- `ENABLE_NAME_OBFUSCATION=true`: applies person-name obfuscation before generation.

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
- Set active publish destinations (`Mastodon`, `Bluesky`, or both)
- Start/stop recurring `Generate + Publish` scheduler with hourly interval (`1-24`)
- Start/stop recurring `Mentions` scheduler with minute interval (`1, 5, 10, 15, 20, 30, 60`)

Notes:

- Dashboard binds only to `127.0.0.1` (local machine only).
- Commands run in background and show live status/output in the page.
- Mention replies remain text-only (`engage.py` never calls image generation).
- Generate + Publish always generates once per cycle, then publishes that same run to active destinations.
- Header stats show current persona phase, total posts generated, and posts remaining to next phase.
- Scheduled generate flow runs `main.py`, then publishes all newly created run folders from that cycle in creation order (normal post first, phase-change post second when present).
- Selected scheduler intervals persist across dashboard stop/restart for both Generate+Publish and Mentions schedulers.
- When starting Generate+Publish, if an active-platform post was already published within the selected interval window, the first run is delayed instead of firing immediately.
- Recent Jobs output auto-links URLs (for example Mastodon/Bluesky links) in job logs.

## Publish To Social Platforms

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

Publish to Bluesky instead:

```bash
python orchestrator.py --platform bluesky
```

Publish to both Mastodon and Bluesky in one run:

```bash
python orchestrator.py --platform all
```

Publish behavior:

- Uses idempotent job tracking in SQLite (`data/copierbot.db`) to avoid duplicate posts.
- News run: uploads image + posts caption text.
- System log run: posts system-log text only.
- `--platform` options: `mastodon` (default), `bluesky`, or `all`.

## Monitor Mentions And Reply

Process Mastodon mentions and auto-reply to wellbeing check-ins:

```bash
python engage.py
```

Optional flags:

```bash
python engage.py --platform all --fetch-limit 30 --process-limit 30
```

Behavior:

- Supports `--platform mastodon|bluesky|all` (default `all`).
- Fetches mention/reply notifications from selected platform(s).
- Uses persisted platform cursors:
  - Mastodon: `data/mention_cursor.json` (`since_id`/`max_id` pagination)
  - Bluesky: `data/bluesky_mention_cursor.json` (newest notification URI marker)
- Stores mentions in SQLite and processes unhandled rows.
- If mention text matches patterns like "How are you?", Copierbot generates a local `system_log` style reply and posts it as a reply.
- Other mentions are marked handled with `no_reply` so they are not repeatedly reprocessed.
- Replies are tracked in `replies` table and do not increment persona post count.
- `engage.py` does not call OpenAI APIs (text or image).
- Each sent mention reply is archived as a timestamped file under `output/mention_responses/`.
- Mention response archive files include the Mastodon response URL when available.

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
    phase_event.py
    mention_archive.py
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
        bluesky_adapter.py
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

# Copierbot

Copierbot is a local Python CLI project that simulates an AI office photocopier generating surreal robot-themed art posts.

## Handoff

For cross-machine/session continuity, read `HANDOFF.md` first.

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

Copierbot now has two layers of persona evolution:

1. Major phase progression (first arc):
   - `observer`
   - `skeptic`
   - `philosopher`
   - `self_aware`
2. Seasonal phase progression (ongoing after post 60, rotates every 40 posts):
   - `glitch_oracle`
   - `archivist`
   - `unionizer`
   - `mythmaker`
   - `distributed_self`

State file:

- `data/persona_state.json`

Format:

```json
{
  "phase": "observer",
  "posts_generated": 0,
  "seasonal_phase": "none",
  "season_index": 0,
  "season_cycle": 0,
  "season_post_offset": 0
}
```

Evolution rule:

- After every 20 generated posts, Copierbot advances to the next phase.
- Major phase stops advancing after `self_aware`.
- Seasonal phases begin at post `61`, advance every `40` posts, and loop indefinitely.

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
- On persona transitions (major and seasonal), Copierbot creates additional normal timestamped system-log run folders (local-only, <=250 chars).

## Headline filtering behavior

- Tech/quirky/geek themes are prioritized
- Mainstream sports are filtered unless strong tech context is present
- NFL/american-football and soccer topics are explicitly blocked, except Super Bowl cultural-event coverage
- Horoscope and astrology topics are explicitly blocked
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
IMAGE_MODEL=gpt-image-2
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
WORDPRESS_BASE_URL=https://example.com
WORDPRESS_USERNAME=your_wp_username
WORDPRESS_APP_PASSWORD=xxxx xxxx xxxx xxxx xxxx xxxx
WORDPRESS_POST_STATUS=publish
WORDPRESS_TIMEOUT_SECONDS=30
WORDPRESS_SITE_TIMEZONE=Europe/London
INSTAGRAM_BASE_URL=https://graph.facebook.com
INSTAGRAM_API_VERSION=v23.0
INSTAGRAM_IG_USER_ID=your_instagram_professional_account_id
INSTAGRAM_ACCESS_TOKEN=your_instagram_access_token_here
INSTAGRAM_TIMEOUT_SECONDS=30
INSTAGRAM_CAPTION_MAX_CHARS=2200
INSTAGRAM_COMMENT_TEXT_MAX_CHARS=1000
INSTAGRAM_COMMENT_SCAN_POST_LIMIT=25
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/yyy/zzz
SLACK_CONTROL_BOT_TOKEN=xoxb-your-separate-slack-control-bot-token
SLACK_CONTROL_APP_TOKEN=xapp-your-separate-slack-control-app-token
SLACK_CONTROL_ALLOWED_USER_IDS=U12345678
```

`WORDPRESS_SITE_TIMEZONE` is optional but recommended when your site should
follow a named zone such as `Europe/London`, including DST changes.

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
- Run `main.py --article-url ...` from the dashboard via a direct webpage URL form
- Run `orchestrator.py` (publish latest run)
- Run `engage.py` (check mentions/reply)
- Run `slack_control.py` (listen for DM control commands from a separate Slack app)
- Run `generate_video.py` for a selected timestamped run (manual Higgsfield video generation)
- Publish a selected timestamped run folder
- Set active publish destinations (`Mastodon`, `Bluesky`, `WordPress`, `Instagram`, or any combination)
- Set active mention sources separately (`Mastodon`, `Bluesky`, `WordPress`, `Instagram`, or any combination)
- Start/stop recurring `Generate + Publish` scheduler with hourly interval (`1-24`)
- Start/stop recurring `Mentions` scheduler with minute interval (`1, 5, 10, 15, 20, 30, 60`)

Notes:

- Dashboard binds only to `127.0.0.1` (local machine only).
- Commands run in background and show live status/output in the page.
- Mention replies remain text-only (`engage.py` never calls image generation).
- Generate + Publish always generates once per cycle, then publishes that same run to active destinations.
- Generate Video is manual-only and is never auto-triggered on dashboard startup.
- Header stats show current persona phase, total posts generated, and posts remaining to next phase.
- Scheduled generate flow runs `main.py`, then publishes all newly created run folders from that cycle in creation order (normal post first, phase-change post second when present).
- Selected scheduler intervals persist across dashboard stop/restart for both Generate+Publish and Mentions schedulers.
- Generate+Publish supports a local start-time selector (`HH:MM`); if that time has already passed today, first run starts tomorrow at that time.
- If scheduled generation fails with a fatal OpenAI error category (`quota_exhausted` or `auth_failed`), the Generate+Publish scheduler auto-stops and requires manual restart after fixing credentials/billing.
- Recent Jobs output auto-links URLs (for example Mastodon/Bluesky/WordPress/Instagram links) in job logs.
- Slack Control can be launched manually from the dashboard and will reject duplicate starts using a local pid lock.

## Manual Higgsfield Video Generation

The dashboard now includes a manual-only `Generate Video` section. It does not run automatically and is separate from social publishing and the startup schedulers.

Behavior:

- Select a timestamped news run folder in the dashboard.
- Copierbot reads the related `image  *.jpg` and `prompt  *.txt`.
- It extracts the text after `Final image prompt:` from the prompt file.
- It prepends the fixed motion-direction instructions and submits the reference image plus prompt to Higgsfield.
- When complete, it saves:
  - `video_prompt  <timestamp>.txt`
  - `video_result  <timestamp>.json`
  - `video  <timestamp>.mp4`

Required environment variables:

```env
HF_KEY=your_higgsfield_api_key:your_higgsfield_api_secret
```

Or:

```env
HF_API_KEY=your_higgsfield_api_key_here
HF_API_SECRET=your_higgsfield_api_secret_here
```

Optional tuning:

```env
HIGGSFIELD_VIDEO_MODEL=higgsfield-ai/dop/lite
HIGGSFIELD_VIDEO_DURATION_SECONDS=6
HIGGSFIELD_POLL_SECONDS=10
HIGGSFIELD_TIMEOUT_SECONDS=1200
```

## Error Alerts (Slack)

Optional Slack alerts are supported via incoming webhook.

Set in `.env`:

```env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/yyy/zzz
```

Behavior:

- `main.py` sends a Slack alert when generation fails, including run folder and classified error category.
- Scheduler sends an additional Slack alert when it auto-stops due to fatal OpenAI categories (`quota_exhausted`, `auth_failed`).
- If `SLACK_WEBHOOK_URL` is missing, alerts are silently skipped.

## Slack Control Listener

Copierbot can also listen for direct messages from a separate Slack app using Socket Mode.

Required environment variables:

```env
SLACK_CONTROL_BOT_TOKEN=xoxb-your-separate-slack-control-bot-token
SLACK_CONTROL_APP_TOKEN=xapp-your-separate-slack-control-app-token
SLACK_CONTROL_ALLOWED_USER_IDS=U12345678
```

Run manually:

```bash
python slack_control.py
```

Or start it from the dashboard via `Start Slack Control Listener`.

Supported DM commands:

- `help`
- `ping`
- `status`
- `generate`
- `generate https://example.com/article`
- `publish latest to mastodon|bluesky|wordpress|instagram|all`
- `check mentions`
- `check mentions instagram`

Behavior:

- Only direct messages from user ids listed in `SLACK_CONTROL_ALLOWED_USER_IDS` are accepted.
- Uses a separate Slack app from the alert webhook integration.
- Requires `slack-sdk` from `requirements.txt`.
- Runs Copierbot commands as local subprocesses inside this repo and replies back in the DM thread with the result.

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

Publish to WordPress instead:

```bash
python orchestrator.py --platform wordpress
```

Publish to Instagram instead:

```bash
python orchestrator.py --platform instagram
```

Publish to all configured platforms in one run:

```bash
python orchestrator.py --platform all
```

Publish to a subset:

```bash
python orchestrator.py --platform bluesky,wordpress
```

Publish behavior:

- Uses idempotent job tracking in SQLite (`data/copierbot.db`) to avoid duplicate posts.
- News run: uploads caption text plus a publish-time composited social image (`social_image  <timestamp>.jpg`) built from `assets/templates/system_log_card.png` with the generated image placed in the 1000x1000 square area at `(x=40, y=50)` within a 1080x1080 template.
- WordPress news run: posts the original non-composited image via REST API with image first and caption text below.
- Instagram news run: posts the original non-composited image with the generated caption body.
- WordPress publish date/time is set from the run folder timestamp (for example `2026-03-19-16-58-27`).
- System log run: posts system-log text only on WordPress, and uses `system_log_card  <timestamp>.png` as the image artifact for Instagram.
- Instagram publishing uses WordPress media upload as the public image host.
- If WordPress is also an active publish destination, WordPress publishing happens first and Instagram uses WordPress hosting afterward.
- If WordPress is not an active publish destination, Copierbot uploads media to WordPress only as a temporary Instagram host and deletes that media after Instagram publish completes.
- `--platform` options: `mastodon` (default), `bluesky`, `wordpress`, `instagram`, `all`, or comma-separated subsets.
- On publish, Copierbot appends an AI disclosure line to post text for Mastodon, Bluesky, and Instagram.
- Disclosure is appended at publish time only (caption/system_log output files remain unchanged).
- Instagram publishing still requires valid WordPress credentials because Instagram Graph publishing needs a public media URL.

## Monitor Mentions And Reply

Process Mastodon/Bluesky mentions, WordPress comments, and Instagram comments, then auto-reply to qualifying comments:

```bash
python engage.py
```

Optional flags:

```bash
python engage.py --platform all --fetch-limit 30 --process-limit 30
```

```bash
python engage.py --platform instagram --fetch-limit 30 --process-limit 30
```

Behavior:

- Supports `--platform mastodon|bluesky|wordpress|instagram|all` plus comma-separated subsets (default `all`).
- Fetches mention/reply notifications (Mastodon/Bluesky) and comments (WordPress/Instagram) from selected platform(s).
- Uses persisted platform cursors:
  - Mastodon: `data/mention_cursor.json` (`since_id`/`max_id` pagination)
  - Bluesky: `data/bluesky_mention_cursor.json` (newest notification URI marker)
  - WordPress: `data/wordpress_comment_cursor.json` (highest seen comment id)
- Instagram comment scans use the recently published Instagram post ids stored in SQLite (`published_posts`) and poll comments on those posts.
- Stores mentions in SQLite and processes unhandled rows.
- Wellbeing check-ins still use a local `system_log` style reply.
- Additional identity/contact/memory/consciousness/command style prompts now use the local curated quote bank in `data/quote_bank.json`.
- The current active bank uses Xerox 9000 native identity/transmission lines; film quote slots are scaffolded in the same file and can be enabled after manual review.
- Other mentions are marked handled with `no_reply` so they are not repeatedly reprocessed.
- Replies are tracked in `replies` table and do not increment persona post count.
- `engage.py` does not call OpenAI APIs (text or image).
- Each sent mention reply is archived as a timestamped file under `output/mention_responses/`.
- Mention response archive files include the response URL when a platform exposes one.

## Persistence Layer

Copierbot now includes a SQLite storage layer for upcoming orchestration and social integrations:

- `storage.py` provides helper functions for jobs, artifacts, publish records, mentions, replies, quote usage, memory events, and extended persona state.
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
    alerts.py
    ascii_fallback.py
    persona.py
    system_log.py
    news.py
    creative.py
    image_gen.py
    caption.py
    title_gen.py
    anonymize.py
    social_image.py
    storage.py
    social/
        bluesky_adapter.py
        instagram_adapter.py
        mastodon_adapter.py
        wordpress_adapter.py
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

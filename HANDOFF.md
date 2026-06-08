# Copierbot Handoff

This file is the canonical handoff for continuing development on another machine/agent.

## 1) Current System Overview

Copierbot generates satirical posts with two post types:

- `news` (target: 80%)
- `system_log` (target: 20%)

Core pipeline:

1. `main.py` creates a timestamped run folder in `output/`.
2. For news posts:
   - fetch/filter headlines (`news.py`)
   - build context from source (`article_context.py`)
   - generate title (`title_gen.py`)
   - generate concept + prompt (`creative.py`)
   - generate image (`image_gen.py`) with safety retry + local ASCII fallback
   - generate caption (`caption.py`)
   - build social composite image (`social_image.py`)
3. For system logs:
   - generate log (`system_log.py`) and card image (`system_log_card.py`)
4. Persona counter increments and phase-change logs may be emitted (`phase_event.py`).

Publishing and engagement:

- `orchestrator.py` publishes generated run folders to Mastodon/Bluesky/WordPress/Instagram.
- `engage.py` checks mentions/comments and replies only with local text system-log style (no image generation).
- `dashboard.py` is local web control + scheduler host.

## 2) Persona Model (Current)

`persona.py` now has two layers:

- Major phases (first arc, every 20 posts):
  - `observer` -> `skeptic` -> `philosopher` -> `self_aware`
- Seasonal phases (from post 61 onward, every 40 posts, loops forever):
  - `glitch_oracle`
  - `archivist`
  - `unionizer`
  - `mythmaker`
  - `distributed_self`

Season cycle drift:

- Cycles use 3-step drift pattern:
  - baseline
  - elevated surreal intensity
  - elevated surreal intensity + slightly more abstraction

State file:

- `data/persona_state.json`

Important: both major and seasonal transitions can emit short phase-change system log run folders.

## 3) Scheduler Behavior (Dashboard)

`dashboard.py`:

- Contains two schedulers:
  - Generate + Publish
  - Mentions
- Also contains a manual-only `Generate Video` action for selected run folders.
- Generate scheduler supports:
  - hourly interval `1..24`
  - local start-time anchor `HH:MM`
  - if selected time already passed today, first run is tomorrow at that time
- Scheduler preferences persist in:
  - `data/dashboard_scheduler_state.json`

Startup behavior:

- On dashboard process start, both schedulers are auto-started from persisted settings.
- The manual `Generate Video` action is not auto-started and only runs when clicked in the dashboard.
- The dashboard itself does not need manual scheduler start after reboot if process starts successfully.

## 4) Alerting + Failure Handling

`alerts.py`:

- classifies OpenAI-like error text into categories:
  - `quota_exhausted`
  - `auth_failed`
  - `rate_limited`
  - `safety_rejected`
  - `network_error`
  - `unknown`
- sends Slack webhook alerts via `SLACK_WEBHOOK_URL`

`main.py` failure behavior:

- writes `error  <timestamp>.txt` into failing run folder
- sends Slack alert with category, run folder, and error summary

`dashboard.py` scheduled generate behavior:

- if scheduled `main.py` fails with fatal category (`quota_exhausted` or `auth_failed`), scheduler auto-stops and sends Slack alert.

## 5) Social Behavior

- Mastodon + Bluesky + Instagram posts append disclosure line at publish time.
- News posts publish composited social image for Mastodon/Bluesky.
- WordPress gets original non-composited image in content.
- Instagram gets the original non-composited image for news posts and the rendered system-log card for system-log posts.
- Instagram uses WordPress media upload as its public image host.
- If WordPress is not an active destination, temporary WordPress-hosted Instagram media is deleted after Instagram publish completes.
- Mention replies are text-only and do not increment persona post count.
- Manual Higgsfield video generation does not alter publish behavior or scheduler behavior.

## 5a) Manual Higgsfield Video Generation

- Triggered only from the dashboard `Generate Video` section.
- Uses a selected timestamped run folder under `output/`.
- Reads the run's `image  *.jpg` and `prompt  *.txt`.
- Extracts the text after `Final image prompt:` and prepends a fixed motion/style instruction block.
- Uploads the saved still image to Higgsfield, submits a video request, polls status, then saves:
  - `video_prompt  <timestamp>.txt`
  - `video_result  <timestamp>.json`
  - `video  <timestamp>.mp4`
- Uses Higgsfield credentials from `.env` via `HF_KEY` or `HF_API_KEY` + `HF_API_SECRET`.
- Default model is configured via `HIGGSFIELD_VIDEO_MODEL` and currently defaults to `higgsfield-ai/dop/lite`.

## 6) Current Operational Setup (Headless Mac)

Expected boot/login flow currently used:

- Auto-login enabled on macOS user.
- LaunchAgent/login item starts `run-copierbot-dashboard.sh`.
- Dashboard boots and auto-starts both schedulers.

Launcher script path in use on the current machine:

- `<user-bin>/run-copierbot-dashboard.sh`

Launcher contents should be equivalent to:

```zsh
#!/bin/zsh
cd <repo-root>
exec <repo-root>/.venv/bin/python dashboard.py
```

## 7) Secrets and Git Hygiene

- `.env` is ignored and no longer tracked.
- `.gitignore` should include at least:
  - `.env`
  - `.venv/`
  - `venv/`
  - `__pycache__/`
  - `*.py[cod]`
  - `.DS_Store`
  - `logs/`
  - `*.log`

If `.env` was previously pushed, it has been removed from tracking in current history state, but prior history still contains it.

## 8) Key Files

Core:

- `main.py`
- `dashboard.py`
- `orchestrator.py`
- `engage.py`
- `persona.py`
- `phase_event.py`
- `system_log.py`
- `alerts.py`

Config/state:

- `.env.example`
- `data/persona_state.json`
- `data/dashboard_scheduler_state.json`
- `data/copierbot.db`
- cursor files in `data/`

## 9) Quick Verification Commands

Run from repo root:

```bash
.venv/bin/python --version
.venv/bin/python -m py_compile main.py dashboard.py orchestrator.py engage.py persona.py
```

Dashboard up:

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
pgrep -af "dashboard.py"
```

Manual smoke checks:

```bash
.venv/bin/python main.py
.venv/bin/python orchestrator.py --platform bluesky
.venv/bin/python engage.py --platform all
```

## 10) Known Next Improvements

Recommended backlog:

1. Add explicit dashboard UI indicator that schedulers are auto-started-on-launch.
2. Add an operations health endpoint (`/health`) for external monitoring.
3. Add a periodic self-check job that validates API creds and posts one consolidated alert.
4. Consider splitting long-running workers from dashboard UI process (daemonized worker model).
5. Optionally move from auto-login + LaunchAgent to LaunchDaemon-only workers for stricter headless reliability.

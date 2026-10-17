# Hermes Agent — Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on [Railway](https://railway.app) with a web-based admin dashboard for configuration, gateway management, and user pairing.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-ai?referralCode=QXdhdr&utm_medium=integration&utm_source=template&utm_campaign=generic)

> Hermes Agent is an autonomous AI agent by [Nous Research](https://nousresearch.com/) that lives on your server, connects to your messaging channels (Telegram, Discord, Slack, etc.), and gets more capable the longer it runs.

<!-- TODO: Add dashboard screenshot -->
<!-- ![Dashboard](docs/dashboard.png) -->

## Features

- **Admin Dashboard** — dark-themed UI to configure providers, channels, tools, and manage the gateway
- **One-Page Setup** — provider dropdown, checkbox-based channel/tool toggles — no config files to edit
- **Gateway Management** — start, stop, restart the Hermes gateway from the browser
- **Live Status** — stat cards for gateway state, uptime, model, and pending pairing requests
- **Live Logs** — streaming gateway log viewer
- **User Pairing** — approve or deny users who message your bot, revoke access anytime
- **Basic Auth** — password-protected admin panel
- **Reset Config** — one-click reset to start fresh

## Getting Started

The easiest way to get started:

### 1. Get an LLM Provider Key (free)

1. Register for free at [OpenRouter](https://openrouter.ai/)
2. Create an API key from your [OpenRouter dashboard](https://openrouter.ai/keys)
3. Pick a free model from the [model list sorted by price](https://openrouter.ai/models?order=pricing-low-to-high) (e.g. `google/gemma-3-1b-it:free`, `meta-llama/llama-3.1-8b-instruct:free`)

### 2. Set Up a Telegram Bot (fastest channel)

Hermes Agent interacts entirely through messaging channels — there is no chat UI like ChatGPT. Telegram is the quickest to set up:

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, follow the prompts, and copy the **Bot Token**
3. Send a message to your new bot — it will appear as a pairing request in the admin dashboard
4. To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot)

### 3. Deploy to Railway

1. Click the **Deploy on Railway** button above
2. Set the `ADMIN_PASSWORD` environment variable (or a random one will be generated and printed to deploy logs)
3. Attach a **volume** mounted at `/data` (persists config across redeploys)
4. Open your app URL — log in with username `admin` and your password

### 4. Configure in the Admin Dashboard

1. **LLM Provider** — select OpenRouter from the dropdown, paste your API key, enter the model name
2. **Messaging Channel** — check Telegram, paste the Bot Token from BotFather
3. Click **Save & Start** — the gateway will start and your bot goes live

### 5. Start Chatting

Message your Telegram bot. If you're a new user, a pairing request will appear in the admin dashboard under **Users** — click **Approve**, and you're in.

<!-- TODO: Add Telegram chat screenshot -->
<!-- ![Telegram Example](docs/telegram-example.png) -->

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Web server port (set automatically by Railway) |
| `ADMIN_USERNAME` | `admin` | Basic auth username |
| `ADMIN_PASSWORD` | *(auto-generated)* | Basic auth password — if unset, a random password is printed to logs |
| `CAPITAL_DASHBOARD_DIR` | `${HERMES_HOME}/state/capital-dashboard/current` | Directory containing the receipt-gated `dashboard.html` served at authenticated `/capital` and `/capital/` routes |
| `HERMES_REF` | *(pinned in Dockerfile)* | Hermes Agent version to install (any upstream git tag/branch). Set this to override the Dockerfile default without editing code — see [Updating Hermes](#updating-hermes). |

All other configuration (LLM provider, model, channels, tools) is managed through the admin dashboard.

## Supported Providers

OpenRouter, DeepSeek, DashScope, GLM / Z.AI, Kimi, MiniMax, HuggingFace

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Tavily (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

```
Railway Container
├── Python Admin Server (Starlette + Uvicorn)
│   ├── /            — Native Hermes dashboard proxy (cookie auth)
│   ├── /capital[/]  — Receipt-gated persistent capital dashboard (cookie auth)
│   ├── /health      — Lightweight Railway liveness check (no auth)
│   ├── /health/ready — Secret-free gateway/dashboard/storage/process readiness
│   └── /setup/api/* — Config, status, logs, gateway, pairing
└── hermes gateway   — Managed as async subprocess
```

The server writes a private deployment-scoped readiness receipt to
`$HERMES_HOME/state/runtime-readiness.json`. It includes the full Railway
deployment/replica identifiers and is mode `0600`; `/health/ready` exposes only
status, freshness and the deployed commit prefix. The receipt is set to
`starting` before child startup, refreshed every five seconds, and set to
`stopping` during graceful shutdown. Verifiers must require the expected
deployment ID and a fresh `checked_at`. Keep Railway's own health check on
`/health` so an intentionally unconfigured first boot can still reach `/setup`.

The admin server runs on `$PORT` and manages the Hermes gateway as a child process. Config is stored in `/data/.hermes/.env` and `/data/.hermes/config.yaml`. Gateway stdout/stderr is captured into a ring buffer and streamed to the Logs panel.

## Verified self-redeploy

A running agent can ask Railway's existing GitHub integration to replace its
container without holding a Railway API token. The controller creates a
metadata-only commit through GitHub's Git Data API; it never force-updates the
branch and refuses if the live deployment SHA is not still the branch head.
Dry-run is the default:

```bash
python /app/scripts/request_redeploy.py --reason "operator maintenance"
python /app/scripts/request_redeploy.py --reason "operator maintenance" --execute
```

The GitHub ref update completes before the old container is stopped. The command
does not call that success: it waits for a GitHub deployment record for the new
SHA and fails explicitly if Railway Watch Paths or disabled autodeploy prevent
one from appearing. Railway then owns build/replacement and publishes external
deployment status. After the replacement boots, verify all three distinct receipts:

1. GitHub/Railway deployment status is `success` for the requested commit.
2. `/health/ready` reports that same commit prefix and `status: ready`.
3. The fresh private volume receipt has the new deployment ID, a lock/runtime-
   verified gateway with a connected platform and healthy session store, a
   loopback HTTP-verified dashboard, readable state DB, cron/session directories,
   adequate PID headroom and zero zombies.

The controller intentionally creates repository history. Use it only for an
explicit restart/redeploy decision, not as a liveness loop or automatic cron.

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v hermes-data:/data hermes-agent
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

### SQLite runtime acceptance

The image builds SQLite 3.53.4 from the official checksum-pinned amalgamation
archive and makes that library the system loader's SQLite provider. This is
intentional: installing a newer `sqlite3` command alone does not change the
library used by Python's stdlib `sqlite3` module.

The Docker build fails unless `/usr/local/bin/python` loads the pinned library
from `/opt/sqlite/lib`, reports the pinned release/source ID, includes FTS5,
and passes in-memory FTS5 `flush` and `integrity-check` operations. Recheck a
built image without reading any persistent database:

```bash
docker run --rm --entrypoint /usr/local/bin/python hermes-agent \
  /usr/local/libexec/hermes/verify-sqlite-runtime.py \
  --expected-version 3.53.4 \
  --expected-source-id "2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc" \
  --expected-library-prefix /opt/sqlite/lib
```

For storage maintenance, this image-level check is only the first gate. In an
isolated candidate container, run the external Astra runtime probe and full
synthetic storage test suite before any offline production-volume procedure.
Do not treat `/health` or the presence of a new SQLite CLI as acceptance.

## Updating Hermes

This template pins a specific Hermes Agent release in the `Dockerfile` (`ARG HERMES_REF`, currently `v2026.9.14`). To upgrade:

- **Recommended:** set a `HERMES_REF` service variable in Railway to any upstream [release tag](https://github.com/NousResearch/hermes-agent/releases) (e.g. `v2026.6.5`), then redeploy. It's passed in as a Docker build arg and overrides the Dockerfile default — no code change needed.
- **Or** bump `ARG HERMES_REF` in the `Dockerfile` and redeploy.

The "Update" button inside the Hermes dashboard is a **no-op on Railway** (it detects a container install and refuses) — the image is immutable, so a runtime self-update wouldn't survive a redeploy. Bump `HERMES_REF` and redeploy instead. When jumping releases, re-check that the Dockerfile's install extras still match upstream's `pyproject.toml`.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template

### September 2026 runtime baseline

The image now pins upstream `v2026.9.14` (Hermes 0.21.3). This release scopes cron approval context to each job and provides bounded gateway history-cleanup waits. Build-time import checks cover the gateway, scheduler, and cron context isolation key. Persistent configuration, credentials, conversations, workspace, and schedules remain on `/data`. Back up the SQLite databases with the SQLite backup API before upgrading an existing volume; use a private config/schedule backup as well. A rollback must account for database migrations, not just select the earlier image.

## Private bookmark worker

Owner-enabled `X_BOOKMARK_REVIEW_ENABLED=1` starts `bookmark_worker.py` hourly.
Set `X_BOOKMARK_REVIEW_JOB_ID` to the active hourly Hermes job named
`hourly-x-bookmark-research`. The fixed Bird 0.8.0 command reads at most 100
bookmarks using the service's `X_BIRD_AUTH_TOKEN` and `X_BIRD_CT0`; it has no posting
command. After the worker is forked, startup unsets those variables before launching the
server/gateway/agent process tree. Hermes's
cron credential filtering remains unchanged. No official paid X API is used.

The signed wiki gate reads only `/data/.hermes/state/x-bookmark-review/snapshot.json`.
No new/unfinished work means no model call. Publication receipts and confirmed
Telegram delivery govern progress; errors keep the batch pending. The persistent
worker deadline and singleton lock survive restarts. The worker collects hourly;
Hermes separately schedules the hourly review after collection. It never manually
dispatches jobs. The worker checks completed delivery receipts without a model. Disable with
`X_BOOKMARK_REVIEW_ENABLED=0` and redeploy. Operational flow and recovery are in the
wiki's `docs/x-bookmark-review.md`. This worker is independent of JEV.

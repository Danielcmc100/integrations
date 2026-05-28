# PSTG Integrations

Bidirectional sync service between Plane, GitHub, and Discord.

- Plane cards ↔ GitHub issues (create, edit, state, labels, assignees, comments)
- PR merge via branch name closes linked Plane card
- Discord notifications for PRs ready for review (green CI, not draft)
- Hourly reminders for unreviewed PRs > 24h

## Architecture

```
FastAPI (port 8000)
├── POST /webhooks/github   — GitHub App webhook receiver
├── POST /webhooks/plane    — Plane webhook receiver
├── GET  /metrics           — Prometheus metrics
├── GET  /healthz           — Health check
└── /admin/*                — Mapping config CRUD

ARQ Worker (Redis-backed)
├── process_github_event    — issues, pull_request, check_suite, review
├── process_plane_event     — card.created, card.updated
└── send_review_reminders   — hourly cron

PostgreSQL
├── card_issue_link         — Plane card ↔ GitHub issue mapping
├── webhook_event_log       — dedup + audit trail
├── repo_module_map         — GitHub repo ↔ Plane module
├── label_map               — GitHub label ↔ Plane label
├── user_map                — GitHub login ↔ Plane user ↔ Discord user
├── pr_notification_state   — Discord notification dedup per PR
└── dead_letter             — permanent failures after 5 retries
```

## Setup

### 1. Environment variables

Copy `.env.example` to `.env` and fill in values:

```bash
cp .env.example .env
```

See [Environment Variables](#environment-variables) section below.

### 2. GitHub App

Create a GitHub App with permissions:
- **Issues**: Read & Write
- **Pull requests**: Read & Write
- **Checks**: Read
- **Metadata**: Read

Subscribe to events: `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `check_suite`.

After creating, install the app on target repos and note the **Installation ID**.

Set webhook URL to `https://<your-host>/webhooks/github`.

### 3. Plane Webhook

In Plane workspace settings, add webhook pointing to `https://<your-host>/webhooks/plane`.  
Set the shared secret — same value as `PLANE_WEBHOOK_SECRET`.

### 4. Discord Bot

Create a Discord application at [discord.com/developers/applications](https://discord.com/developers/applications):

1. **New Application** → name it → go to **Bot** tab
2. Click **Reset Token** → copy value → set as `DISCORD_BOT_TOKEN`
3. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent**
4. Under **OAuth2 → URL Generator**, select scopes:
   - `bot`
   - `applications.commands`
5. Bot permissions required:
   - Send Messages
   - Embed Links
   - Create Public Threads
   - Send Messages in Threads
   - Manage Threads
   - Read Message History
6. Use generated URL to invite bot to your server

**Get channel IDs:** Enable Developer Mode in Discord (Settings → Advanced), right-click channel → **Copy Channel ID**.

- `DISCORD_REVIEW_CHANNEL_ID` — channel where PR review notifications post
- `DISCORD_OPS_CHANNEL_ID` — channel for dead-letter failure alerts (optional)

### 5. Run

```bash
docker compose up -d
```

Run migrations:

```bash
docker compose exec app alembic upgrade head
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | yes | PostgreSQL async URL (`postgresql+asyncpg://...`) |
| `REDIS_URL` | yes | Redis URL (`redis://...`) |
| `PLANE_API_TOKEN` | yes | Plane API token |
| `PLANE_WORKSPACE` | yes | Plane workspace slug |
| `PLANE_WEBHOOK_SECRET` | yes | Shared secret for Plane webhook validation |
| `PLANE_BASE_URL` | no | Plane API base URL (default: `https://api.plane.so/api/v1`) |
| `PLANE_APP_URL` | no | Plane app URL for card links (default: `https://app.plane.so`) |
| `GITHUB_APP_ID` | yes | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY` | yes | GitHub App private key (PEM, single line with `\n`) |
| `GITHUB_APP_INSTALLATION_ID` | yes | GitHub App installation ID on target org/repo |
| `GITHUB_WEBHOOK_SECRET` | yes | Secret for `X-Hub-Signature-256` validation |
| `GITHUB_BOT_LOGIN` | no | GitHub App bot username — skips echo on comment sync |
| `DISCORD_BOT_TOKEN` | yes | Discord bot token |
| `DISCORD_REVIEW_CHANNEL_ID` | yes | Channel ID for PR review notifications |
| `DISCORD_OPS_CHANNEL_ID` | no | Channel ID for dead-letter alerts |
| `ADMIN_TOKEN` | yes | Bearer token for `/admin/*` endpoints |

## Admin API

All endpoints require `Authorization: Bearer <ADMIN_TOKEN>`.

### Repo ↔ Module mapping

```
GET    /admin/repo-modules
POST   /admin/repo-modules        {"plane_module_id": "...", "plane_project_id": "...", "gh_repo": "owner/repo"}
PUT    /admin/repo-modules/{id}
DELETE /admin/repo-modules/{id}
```

### Label mapping

```
GET    /admin/labels
POST   /admin/labels              {"plane_project_id": "...", "plane_label_id": "...", "gh_repo": "...", "gh_label": "..."}
PUT    /admin/labels/{id}
DELETE /admin/labels/{id}
```

### User mapping

```
GET    /admin/users
POST   /admin/users               {"plane_user_id": "...", "gh_login": "...", "discord_user_id": "..."}
PUT    /admin/users/{id}
DELETE /admin/users/{id}
```

## Backfill CLI

Link existing Plane cards with existing GitHub issues (run once after configuring mappings):

```bash
# dry run — report unmatched only
python -m integration.backfill --project <plane-project-id> --repo owner/repo

# create missing counterparts
python -m integration.backfill --project <plane-project-id> --repo owner/repo --create-missing
```

Matching uses title first, then footer cross-references. Idempotent — safe to re-run.

## Sync behavior

| Trigger | Action |
|---|---|
| Plane card created (non-Backlog) | Create GitHub issue |
| GitHub issue opened | Create Plane card in Refinamento state |
| Title/description/labels/assignees edited | Sync to other side (5s loop prevention) |
| Plane card → Done or Cancelled | Close GitHub issue |
| Plane card → Em andamento | Add `in-progress` label to GitHub issue |
| GitHub issue closed (not by PR merge) | Move Plane card to Done |
| GitHub issue comment | Post on Plane card as `[GitHub @login]: ...` |
| PR merged (branch `<num>-*`) | Close linked Plane card, add comment |
| PR ready + CI green | Post Discord embed, create thread |
| PR review submitted | Post in Discord thread |
| PR closed/merged | Post final line in thread, archive thread |
| PR unreviewed > 24h | Hourly reminder in Discord thread |

## Webhook payload quirks

### Plane → GitHub

**Assignees format:** Plane sends `assignees` in `card.created` and `card.updated` webhooks as a list of user objects, not UUID strings:
```json
"assignees": [{"id": "uuid", "display_name": "user", "email": "...", ...}]
```
The handler extracts `.id` from each object. If the field is a plain UUID string (API-created cards), `str(uid)` is used as fallback.

**Labels field name:** Plane may send labels as `label_ids` (list of UUID strings) or `labels` (list of UUID strings or objects depending on context). The handler checks both fields.

**Module at creation time:** When a card is created via the Plane UI with a module selected, the `card.created` webhook includes `module` and/or `module_ids`. Cards created via API without a module require a fallback search across all mapped modules. If the module is assigned after creation, the `card.created` event has already fired — the subsequent `card.updated` may be skipped by loop prevention (5s window).

**Labels vs assignees in card.created payload:** Plane does not always include labels and assignees in the `card.created` payload. They reliably appear in the first non-loop-prevented `card.updated` event.

### GitHub → Plane

**Username casing:** GitHub sends usernames with the canonical casing from the user's profile (e.g. `Danielcmc100`). The `user_map` table stores logins lowercase. All `gh_login` lookups compare case-insensitively.

**Loop prevention:** A 5-second window (`LOOP_WINDOW_SECONDS = 5`) prevents echo loops. Events from the same source within 5s of the last sync on that source are skipped. This means the immediate `card.updated` fired after `card.created` writes the GitHub link to Plane is always skipped — labels and assignees from that update are synced by the next non-skipped event.

### label_map integrity

GitHub label names in `label_map` must exactly match labels that exist in the target repo. Invalid entries cause `create_issue`/`update_issue` calls to fail silently or partially. Verify with:
```bash
gh label list --repo owner/repo
```

## Development

```bash
uv sync --all-groups
cp .env.example .env  # fill in values
alembic upgrade head
uvicorn main:app --reload          # API
arq integration.worker.WorkerSettings  # worker
```

Run checks:

```bash
ruff check .
basedpyright
pytest
```

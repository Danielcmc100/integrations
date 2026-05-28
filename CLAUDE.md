# PSTG Integrations

Bidirectional sync between Plane (kanban), GitHub (issues/PRs), and Discord (notifications).

## What it does

- Plane card created → GitHub issue created (with labels + assignees)
- GitHub issue opened → Plane card created
- Title/description/labels/assignees edits sync both ways
- Plane card → Done/Cancelled → GitHub issue closed
- GitHub PR merged (branch `<number>-*`) → linked Plane card closed
- PR ready + CI green → Discord embed + thread
- Hourly reminders for unreviewed PRs > 24h

> **Rule:** Every new sync behaviour added to the codebase must also be documented in `README.md`.

## Stack

- **FastAPI** (`main.py`) — webhook receiver on port 8000 (mapped to 200 in prod)
- **ARQ worker** (`integration/worker.py`) — Redis-backed async job queue
- **PostgreSQL** — state, mappings, dedup log
- **Redis** — job queue

## Key tables

| Table | Purpose |
|---|---|
| `repo_module_map` | Plane module → GitHub repo mapping |
| `card_issue_link` | Plane card ↔ GitHub issue bidirectional link |
| `label_map` | Plane label → GitHub label |
| `user_map` | Plane user → GitHub login → Discord user |
| `webhook_event_log` | Dedup + audit trail (status stays `pending` — not updated by worker) |
| `dead_letter` | Events that failed 5 retries |

## Debugging

### 1. Check worker logs (Coolify)

**Important:** The Coolify application UUID `nowxvnparmwjgbr8lhnzbrt4` is a docker-compose app with two separate containers:
- `app-nowxvnparmwjgbr8lhnzbrt4-*` — FastAPI/uvicorn (API)
- `worker-nowxvnparmwjgbr8lhnzbrt4-005503632857` — ARQ worker

The Coolify MCP `application_logs` tool returns **API container logs only** (uvicorn access logs + webhook received/enqueued). Worker logs (`process_plane_event`, `card.created`, etc.) are only in the worker container.

To get worker logs via Coolify panel: `http://192.168.1.68:8000` → integrations → worker service logs.

Key log events:
- `process_plane_event.started` — webhook dequeued
- `card.created: module not in payload, searching mapped modules` — fallback running
- `card.created skipped: no module` — card not in any mapped module
- `card.created skipped: no repo mapping` — module exists but not in `repo_module_map`
- `card.created synced to github` — success
- `card.updated: loop prevention skip` — event within 5s of last sync, skipped (normal)
- `card.updated: no link found` — `card.updated` fired before `card.created` created the link
- `issues.assigned: unknown GH user skipped login=<X>` — `gh_login` in `user_map` doesn't match (check casing)
- `webhook.rejected` + `reason=invalid_signature` — wrong `PLANE_WEBHOOK_SECRET`

### 2. Check metrics endpoint

```bash
curl http://localhost:200/metrics | grep -E 'webhook|sync_action'
```

`webhooks_received_total{source="plane"}` = 0 → webhooks not arriving or all rejected before counter.

### 3. Query the database

SSH tunnel to DB (port 150 exposed):

```bash
# Open tunnel
ssh -fNL 15432:localhost:150 -p 2222 pstg-local-server-01@192.168.1.24

# Query with asyncpg
python3 -c "
import asyncpg, asyncio
async def q():
    conn = await asyncpg.connect('postgresql://integrations:integrations@localhost:15432/integrations')
    # Recent webhooks
    rows = await conn.fetch('SELECT source, event_type, received_at FROM webhook_event_log ORDER BY received_at DESC LIMIT 20')
    for r in rows: print(dict(r))
    # Successful syncs
    links = await conn.fetch('SELECT * FROM card_issue_link ORDER BY last_synced_at DESC LIMIT 10')
    for r in links: print(dict(r))
    # Dead letters
    dl = await conn.fetch('SELECT source, event_type, last_error, created_at FROM dead_letter ORDER BY created_at DESC LIMIT 10')
    for r in dl: print(dict(r))
    await conn.close()
asyncio.run(q())
"
```

### 4. Test webhook endpoint

```bash
# Should return 401 (endpoint reachable, signature rejected)
curl -s -o /dev/null -w '%{http_code}' -X POST https://integration.pstg.solutions/webhooks/plane \
  -H 'Content-Type: application/json' -d '{}'
```

### 5. Common skip reasons (silent — no dead letter)

| Log message | Cause | Fix |
|---|---|---|
| `card.created skipped: backlog state` | Card in backlog | Move card out of backlog |
| `card.created skipped: no module` | Card not in any Plane module | Add card to a module |
| `card.created skipped: no repo mapping` | Module not in `repo_module_map` | Add mapping via Admin API |
| `card.updated: unknown Plane label skipped` | Label not in `label_map` | Add via `POST /admin/labels` |
| `card.updated: unknown Plane user skipped` | User not in `user_map` | Add via `POST /admin/users` |
| `issues.assigned: unknown GH user skipped` | `gh_login` in `user_map` doesn't match GitHub login (case-insensitive comparison is applied, so check for typos) | Fix `gh_login` in `user_map` |
| `card.updated: loop prevention skip` | Normal — fires on updates triggered by our own sync within 5s | Not an error; labels/assignees sync on next non-skipped update |

### 8. Plane webhook payload format notes

**Assignees:** Plane sends `assignees` as a list of user objects in UI-created card events:
```json
[{"id": "uuid", "display_name": "user", "email": "..."}]
```
The handler extracts `.id`. Plain UUID strings also supported (API-created cards).

**Labels:** Plane may send `label_ids` or `labels` — handler checks both. Values may be UUID strings or label objects; `.id` is extracted from objects.

**label_map integrity:** GitHub label names in `label_map` must exist in the target repo. Invalid entries cause silent failures. Use `gh label list --repo owner/repo` to verify.

**GitHub login casing:** GitHub sends usernames with canonical casing (e.g. `Danielcmc100`). All `gh_login` lookups in `user_map` compare case-insensitively. Store logins lowercase in `user_map`.

### 6. Admin API (add/update mappings)

All endpoints: `Authorization: Bearer <ADMIN_TOKEN>`

```bash
# Add module → repo mapping
curl -X POST https://integration.pstg.solutions/admin/repo-modules \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"plane_module_id": "...", "plane_project_id": "...", "gh_repo": "owner/repo"}'

# Add label mapping
curl -X POST https://integration.pstg.solutions/admin/labels \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"plane_project_id": "...", "plane_label_id": "...", "gh_repo": "owner/repo", "gh_label": "github-label-name"}'

# Add user mapping
curl -X POST https://integration.pstg.solutions/admin/users \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"plane_user_id": "...", "gh_login": "...", "discord_user_id": "..."}'
```

### 7. Plane API (inspect labels, modules, cards)

```bash
# List project labels
curl -H "X-API-Key: $PLANE_API_TOKEN" \
  "https://kanban.pstg.solutions/api/v1/workspaces/pstg-tech/projects/<project_id>/labels/"

# List modules
curl -H "X-API-Key: $PLANE_API_TOKEN" \
  "https://kanban.pstg.solutions/api/v1/workspaces/pstg-tech/projects/<project_id>/modules/"

# Issues in a module
curl -H "X-API-Key: $PLANE_API_TOKEN" \
  "https://kanban.pstg.solutions/api/v1/workspaces/pstg-tech/projects/<project_id>/modules/<module_id>/module-issues/"
```

## SSH access

```bash
ssh -p 2222 pstg-local-server-01@192.168.1.24
# Key passphrase: see team vault
```

## Local dev

```bash
uv sync --all-groups
cp .env.example .env
alembic upgrade head
uvicorn main:app --reload          # API on :8000
arq integration.worker.WorkerSettings  # worker
```

```bash
ruff check .
basedpyright
pytest
```

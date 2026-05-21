# PRD: Plane ↔ GitHub ↔ Discord Integration Service

## 1. Introduction/Overview

Standalone integration service (separate repo, self-hosted) connecting three systems used by PSTG team:

- **Plane** — project/task management (cards in project `SINGU`)
- **GitHub** — code hosting, issues, pull requests, CI
- **Discord** — team communication

Today these systems are disconnected: cards in Plane have no GitHub issue counterpart, PR merges don't auto-close cards, and PR review requests rely on people remembering to ping reviewers. Engineers waste time on manual state-syncing and PRs sit unreviewed.

This service automates bidirectional sync between Plane cards and GitHub issues, auto-closes cards when their corresponding PR merges (branch-name based), and posts rich Discord notifications when PRs become ready-for-review with green CI.

## 2. Goals

- Bidirectional sync Plane card ↔ GitHub issue (state, title, description, labels, assignees, comments)
- Auto-move Plane card to `Done` when PR with matching branch name merges
- Rich Discord notification in review channel when PR exits draft and CI is green
- Discord thread per PR for review discussion + reviewer reminders
- Zero manual state-syncing for happy path
- Idempotent operations — replays/duplicate webhooks don't corrupt state

## 3. User Stories

### US-001: Bootstrap integration service skeleton
**Description:** As an integrator, I need a deployable service skeleton so the rest of the work has a home.

**Acceptance Criteria:**
- [ ] New repo created (e.g. `pstg-integrations`)
- [ ] FastAPI app with `/healthz` endpoint returning 200
- [ ] Dockerfile + docker-compose for local dev (app + postgres)
- [ ] `.env.example` listing required secrets (Plane API token, GitHub App credentials, Discord bot token, webhook secrets)
- [ ] Pre-commit + Ruff + pyright configured
- [ ] CI workflow runs lint + tests on PR

### US-002: Persistence layer for sync mapping
**Description:** As service, I need persistent mapping between Plane card IDs and GitHub issue numbers so events from either side resolve to same entity.

**Acceptance Criteria:**
- [ ] Table `card_issue_link` (plane_card_id PK, plane_project_id, gh_repo, gh_issue_number, gh_issue_node_id, last_synced_at, sync_source_last)
- [ ] Table `webhook_event_log` (id, source, event_type, payload_hash, received_at, processed_at, status)
- [ ] Alembic migrations
- [ ] Unique constraints prevent duplicate links
- [ ] Typecheck/lint passes

### US-003: GitHub webhook receiver
**Description:** As service, I need to receive GitHub webhooks securely so PR/issue/CI events trigger sync.

**Acceptance Criteria:**
- [ ] `POST /webhooks/github` validates `X-Hub-Signature-256` HMAC
- [ ] Persists raw event to `webhook_event_log` before processing
- [ ] Returns 200 within 5s (process async via task queue)
- [ ] Rejects with 401 on invalid signature
- [ ] Handles delivery retries idempotently (dedupe by `X-GitHub-Delivery`)
- [ ] Typecheck/lint passes

### US-004: Plane webhook receiver
**Description:** As service, I need to receive Plane webhooks securely so card events trigger sync.

**Acceptance Criteria:**
- [ ] `POST /webhooks/plane` validates shared-secret header
- [ ] Persists raw event to `webhook_event_log`
- [ ] Returns 200 within 5s, processes async
- [ ] Idempotent on duplicate deliveries
- [ ] Typecheck/lint passes

### US-005: Plane card creation → GitHub issue creation
**Description:** As engineer, when card created in Plane (state `Refinamento`, `A fazer`, `Em andamento`), corresponding GitHub issue auto-created in mapped repo.

**Acceptance Criteria:**
- [ ] Card create event triggers GH issue create via REST/GraphQL
- [ ] Issue title = card name; body = card description + footer `Plane: <card-url>`
- [ ] Plane card description updated with footer `GitHub: <issue-url>`
- [ ] Mapping written to `card_issue_link`
- [ ] Repo mapping configurable per Plane module (e.g. `frontend` → `pstgorg/frontend`)
- [ ] Cards in `Backlog` skipped (no issue created)
- [ ] Typecheck/lint passes

### US-006: GitHub issue creation → Plane card creation
**Description:** As engineer, when issue created directly in GitHub, corresponding Plane card auto-created in `Refinamento`.

**Acceptance Criteria:**
- [ ] GH issue `opened` event triggers Plane card create
- [ ] Card created in state `Refinamento`, priority `medium`, label inferred from GH label or default `Feature`
- [ ] Card placed in active cycle if one exists
- [ ] Module inferred from repo (configurable map)
- [ ] Card name in Portuguese rule does NOT apply (mirror GH title as-is)
- [ ] Mapping written to `card_issue_link`
- [ ] Typecheck/lint passes

### US-007: Bidirectional state/field sync
**Description:** As engineer, edits to title/description/labels/assignees/state on one side propagate to other within 30s.

**Acceptance Criteria:**
- [ ] Title change syncs both directions
- [ ] Description change syncs both directions (preserve footer link)
- [ ] Label add/remove syncs (map Plane labels ↔ GH labels via config table)
- [ ] Assignee changes sync (map Plane user ↔ GH login via config table)
- [ ] State transitions sync:
  - Plane `Done`/`Cancelled` → close GH issue
  - GH issue closed (not by PR merge) → Plane `Done`
  - Plane `Em andamento` → GH issue stays open + label `in-progress`
- [ ] Loop prevention: track `sync_source_last`; skip propagation when last change came from sync itself
- [ ] Conflicts (both sides edited within 10s window) logged and resolved last-write-wins by `updated_at`
- [ ] Typecheck/lint passes

### US-008: Comment sync (one-way GH → Plane)
**Description:** As reviewer, comments on GitHub issue appear on Plane card so non-GH stakeholders see context.

**Acceptance Criteria:**
- [ ] GH issue comment created → Plane card comment created
- [ ] Comment body prefixed with `[GitHub @username]:` for attribution
- [ ] Bot's own comments skipped (no echo)
- [ ] Plane → GH direction explicitly NOT implemented in this story
- [ ] Typecheck/lint passes

### US-009: PR merge → close linked Plane card via branch name
**Description:** As engineer, when PR merges and its branch name matches `<SINGU-id>-<slug>` or `<gh-issue-number>-<slug>`, linked Plane card moves to `Done`.

**Acceptance Criteria:**
- [ ] PR `closed` event with `merged=true` parsed
- [ ] Branch name regex: `^(?P<num>\d+)-` extracts leading numeric id
- [ ] Numeric id resolved against `card_issue_link.gh_issue_number` (preferred) then Plane card sequence
- [ ] Linked Plane card transitioned to `Done`
- [ ] Comment added to Plane card: `Fechado via PR <pr-url> (merge <sha>)`
- [ ] PR closed without merge → no card transition
- [ ] Branch name not matching pattern → log warning, no-op
- [ ] Multiple linked cards (PR closes >1 issue via `Closes #N` body) all transitioned
- [ ] Typecheck/lint passes

### US-010: PR ready-for-review + CI green → Discord notification
**Description:** As reviewer, when PR exits draft and all required CI checks pass, rich Discord message posted in review channel asking for review.

**Acceptance Criteria:**
- [ ] Trigger fires once per PR when both conditions true: `draft=false` AND all required check_suites `conclusion=success`
- [ ] If PR opened non-draft with CI already green at open time → fires
- [ ] If PR opened as draft → fires when `ready_for_review` event arrives AND CI green
- [ ] If CI completes after `ready_for_review` → fires on CI success
- [ ] Discord embed contains: PR title, author, repo, branch, additions/deletions, linked Plane card link (if mapped), required reviewers, "Review" button-link
- [ ] Posts to configured channel ID (env var `DISCORD_REVIEW_CHANNEL_ID`)
- [ ] Dedup: only one notification per PR per ready-state transition (track in DB)
- [ ] Subsequent push that fails CI → does NOT re-notify on next green (single notification per ready cycle)
- [ ] Re-notify if PR re-opens after close
- [ ] Typecheck/lint passes

### US-011: Discord thread per PR for review discussion
**Description:** As reviewer, each PR notification creates Discord thread so discussion stays scoped.

**Acceptance Criteria:**
- [ ] Discord message creates thread named `PR #<num> – <title-truncated>`
- [ ] Thread ID stored in DB linked to PR
- [ ] PR review submitted (approve/request changes) → bot posts summary in thread
- [ ] PR merged/closed → bot posts final status in thread + archives thread
- [ ] Typecheck/lint passes

### US-012: Reviewer reminder
**Description:** As team lead, if PR sits unreviewed >24h after notification, bot posts reminder mentioning assigned reviewers.

**Acceptance Criteria:**
- [ ] Scheduled job runs hourly
- [ ] Selects PRs where notification posted >24h ago AND no review submitted AND PR still open AND CI still green
- [ ] Posts reminder in original Discord thread mentioning reviewer Discord user IDs (mapped from GH login via config)
- [ ] Max 1 reminder per PR per 24h window
- [ ] No reminder after merge/close
- [ ] Typecheck/lint passes

### US-013: Admin endpoints for mapping config
**Description:** As admin, I need to manage repo↔module, label↔label, and user↔user mappings without redeploys.

**Acceptance Criteria:**
- [ ] CRUD REST endpoints for `repo_module_map`, `label_map`, `user_map`
- [ ] Auth via static admin token (env var)
- [ ] Config cached in-memory, invalidated on write
- [ ] Typecheck/lint passes

### US-014: Backfill command
**Description:** As admin, I need to backfill existing Plane cards ↔ GitHub issues so service starts with consistent state.

**Acceptance Criteria:**
- [ ] CLI command `python -m integration.backfill --project SINGU --repo pstgorg/singularity`
- [ ] For each existing card, looks for matching GH issue by title or footer link; creates mapping
- [ ] Reports unmatched cards/issues; no creation by default (use `--create-missing` flag)
- [ ] Idempotent (re-runnable)
- [ ] Typecheck/lint passes

### US-015: Observability
**Description:** As operator, I need logs/metrics to debug sync failures.

**Acceptance Criteria:**
- [ ] Structured JSON logs (event id, source, action, duration, outcome)
- [ ] Prometheus `/metrics` endpoint exposing: webhook receive count by source, sync action count by type/outcome, sync duration histogram, queue depth
- [ ] Failed sync attempts retried with exponential backoff (max 5 retries)
- [ ] Dead-letter queue table for permanent failures
- [ ] Alert (Discord ping to ops channel) on dead-letter insert
- [ ] Typecheck/lint passes

## 4. Functional Requirements

- FR-1: Service exposes HTTP endpoints `/webhooks/github`, `/webhooks/plane`, `/healthz`, `/metrics`, `/admin/*`.
- FR-2: All webhooks validated via HMAC signature or shared secret before processing.
- FR-3: Event processing is async (background task queue: ARQ, Dramatiq, or Celery — pick one) and idempotent.
- FR-4: Persistent mapping `card_issue_link` is single source of truth for card↔issue identity.
- FR-5: Plane card create in non-Backlog state creates GitHub issue.
- FR-6: GitHub issue open creates Plane card in `Refinamento`.
- FR-7: Title, description, labels, assignees, state sync bidirectionally with loop prevention.
- FR-8: Comments sync one-way GitHub → Plane.
- FR-9: PR merge with branch matching `^\d+-` closes mapped Plane card to `Done`.
- FR-10: PR ready-for-review + all required CI green emits one Discord notification per ready cycle.
- FR-11: Discord notification creates thread; review events posted into thread.
- FR-12: Reviewer reminder posted in thread if PR unreviewed >24h post-notification.
- FR-13: Mapping config (repo↔module, labels, users) managed via REST endpoints, not env vars.
- FR-14: Backfill CLI reconciles pre-existing state.
- FR-15: Failed syncs retry with exponential backoff; permanent failures land in dead-letter table.

## 5. Non-Goals

- No Plane → GitHub comment sync (one-way only, this version)
- No sync of attachments/files between systems
- No sync of Plane sub-issues hierarchy → GitHub task lists
- No Discord → Plane/GitHub command interface (no `/approve` from Discord etc.)
- No GitHub Projects integration (Plane is the project board)
- No multi-workspace Plane support (single workspace `pstg` only)
- No SSO / per-user OAuth (service uses bot tokens / GitHub App installation token)
- No UI dashboard (admin via REST + DB only)
- No real-time sync (<5s) — 30s budget acceptable
- No conflict resolution UI — last-write-wins, logged

## 6. Design Considerations

- **GitHub App over PAT** — install once on org, fine-grained perms, scoped tokens, webhook signing built-in.
- **Discord Bot** (not webhook) — needed for threads, reactions, slash commands later.
- **Plane API** — token-based, REST. Webhooks via Plane workspace settings.
- **Embeds rich** — author avatar, color by PR state (green=ready, yellow=changes-requested, blue=draft), inline fields for stats.

## 7. Technical Considerations

- **Stack:** Python 3.12, FastAPI, SQLAlchemy + Alembic, Postgres, ARQ (Redis-backed task queue), `httpx` for outbound, `discord.py` for bot, structlog.
- **Deployment:** self-hosted (Docker Compose) on existing PSTG server. Coolify-compatible.
- **Secrets:** env vars.
- **Required CI checks:** "required" determined per repo via GitHub branch protection API; cached 10min.
- **Branch name extraction regex:** `^(?P<num>\d+)-` — captures leading number. Document convention in repo CONTRIBUTING.
- **Loop prevention:** before applying inbound change, compare incoming `updated_at` vs `card_issue_link.last_synced_at`; skip if within 5s window from this service's own write.
- **Rate limits:** GitHub 5000/hr per installation; Plane unknown — implement client-side throttling.
- **Time-to-deliver:** webhook → effect ≤30s p95.

## 8. Success Metrics

- ≥95% of Plane cards in active cycle have linked GitHub issue
- ≥90% of merged PRs auto-close their Plane card with no manual intervention
- Median time from PR ready-for-review (CI green) → first reviewer activity drops by ≥30%
- <1% sync events end in dead-letter queue
- Zero duplicate Discord notifications per PR per ready cycle (measured weekly)

## 9. Open Questions

- Which task queue: ARQ vs Dramatiq vs Celery? (Recommend ARQ — async-native, simple.)
- Repo↔module mapping: is it 1:1 or can one module map to multiple repos?
- Discord user mapping source of truth: manual config table, or Plane custom property?
- Should `Cancelled` Plane cards close GH issue with reason comment, or just close silently?
- PRs that close issues across repos (`Closes pstgorg/other#123`) — in scope?
- What about PRs from forks (external contributors)? Webhook signature differs; CI may be gated.
- Branch name convention enforcement — add `commit-msg` hook in repos, or only document?
- Plane `Refinamento` cards: create GH issue with `draft` label, or skip until promoted? (Current PRD: create immediately.)

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import structlog
import structlog.contextvars
from sqlalchemy.ext.asyncio import AsyncSession

from integration.clients.github import GitHubClient
from integration.clients.plane import PlaneClient
from integration.config import settings
from integration.config_service import ConfigService
from integration.handlers._sync import (
    detect_conflict,
    event_wins_conflict,
    fetch_link_by_plane,
    parse_dt,
    should_skip_loop,
    strip_footer,
)
from integration.metrics import sync_actions_total, sync_duration_seconds
from integration.models import CardIssueLink, SyncSource
from integration.retry import DeadLetteredError, run_with_retry

log = structlog.get_logger()

BACKLOG_GROUP = "backlog"
EM_ANDAMENTO_STATE_NAME = "Em andamento"
IN_PROGRESS_LABEL = "in-progress"
DONE_CANCEL_GROUPS = frozenset({"completed", "cancelled"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def handle_card_created(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    github_client: GitHubClient,
    config_service: ConfigService,
    now_fn: Callable[[], datetime] = _utcnow,
    plane_workspace: str | None = None,
    plane_app_url: str | None = None,
) -> None:
    ws = plane_workspace if plane_workspace is not None else settings.plane_workspace
    app_url = plane_app_url if plane_app_url is not None else settings.plane_app_url

    data_raw: Any = payload.get("data")
    data: dict[str, Any] = (
        cast("dict[str, Any]", data_raw) if isinstance(data_raw, dict) else payload
    )

    card_id: str = str(data["id"])
    project_id: str = str(data["project"])
    card_name: str = str(data.get("name") or "")
    card_description: str = str(
        data.get("description_html") or data.get("description") or ""
    )

    state_detail_raw: Any = data.get("state_detail")
    state_detail: dict[str, Any] = (
        cast("dict[str, Any]", state_detail_raw)
        if isinstance(state_detail_raw, dict)
        else {}
    )
    state_group: str = str(state_detail.get("group") or "")
    if state_group == BACKLOG_GROUP:
        log.info("card.created skipped: backlog state", card_id=card_id)
        return

    raw_module: Any = data.get("module")
    raw_module_ids_raw: Any = data.get("module_ids")
    raw_module_ids: list[Any] = (
        cast("list[Any]", raw_module_ids_raw)
        if isinstance(raw_module_ids_raw, list)
        else []
    )

    module_id: str | None = None
    if isinstance(raw_module, str) and raw_module:
        module_id = raw_module
    elif raw_module_ids:
        module_id = str(raw_module_ids[0])

    if module_id is None:
        log.debug("card.created: module not in payload, fetching card from API", card_id=card_id)
        fetched = await plane_client.get_card(project_id, card_id)
        fetched_module: Any = fetched.get("module")
        fetched_module_ids_raw: Any = fetched.get("module_ids")
        fetched_module_ids: list[Any] = (
            cast("list[Any]", fetched_module_ids_raw)
            if isinstance(fetched_module_ids_raw, list)
            else []
        )
        if isinstance(fetched_module, str) and fetched_module:
            module_id = fetched_module
        elif fetched_module_ids:
            module_id = str(fetched_module_ids[0])

    if module_id is None:
        log.warning("card.created skipped: no module", card_id=card_id)
        return

    repo_map = await config_service.get_repo_module(module_id)
    if repo_map is None:
        log.warning(
            "card.created skipped: no repo mapping",
            card_id=card_id,
            module_id=module_id,
        )
        return

    gh_repo = repo_map.gh_repo
    owner, repo = gh_repo.split("/", 1)
    log.info("card.created: creating GitHub issue", card_id=card_id, gh_repo=gh_repo, module_id=module_id)

    plane_card_url = (
        f"{app_url.rstrip('/')}/{ws}/projects/{project_id}/issues/{card_id}/"
    )
    issue_body = f"{card_description}\n\n---\nPlane: {plane_card_url}"
    gh_issue = await github_client.create_issue(
        owner, repo, {"title": card_name, "body": issue_body}
    )
    gh_issue_number: int = int(gh_issue["number"])
    gh_issue_node_id: str = str(gh_issue["node_id"])
    gh_issue_url: str = str(gh_issue["html_url"])
    log.debug("card.created: github issue created", card_id=card_id, gh_issue_number=gh_issue_number, gh_issue_url=gh_issue_url)

    new_description = f"{card_description}\n\n---\nGitHub: {gh_issue_url}"
    await plane_client.update_card(
        project_id, card_id, {"description_html": new_description}
    )

    link = CardIssueLink(
        plane_card_id=card_id,
        plane_project_id=project_id,
        gh_repo=gh_repo,
        gh_issue_number=gh_issue_number,
        gh_issue_node_id=gh_issue_node_id,
        last_synced_at=now_fn(),
        sync_source_last=SyncSource.plane,
    )
    session.add(link)
    await session.commit()
    log.info(
        "card.created synced to github",
        card_id=card_id,
        gh_issue_number=gh_issue_number,
    )


async def handle_card_updated(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    github_client: GitHubClient,
    config_service: ConfigService,
    now_fn: Callable[[], datetime] = _utcnow,
    plane_workspace: str | None = None,
    plane_app_url: str | None = None,
) -> None:
    ws = plane_workspace if plane_workspace is not None else settings.plane_workspace
    app_url = plane_app_url if plane_app_url is not None else settings.plane_app_url

    data_raw: Any = payload.get("data")
    data: dict[str, Any] = (
        cast("dict[str, Any]", data_raw) if isinstance(data_raw, dict) else payload
    )

    card_id: str = str(data.get("id") or "")
    project_id: str = str(data.get("project") or "")
    if not card_id or not project_id:
        log.warning("card.updated: missing id or project")
        return

    link = await fetch_link_by_plane(session, card_id)
    if link is None:
        log.warning("card.updated: no link found", card_id=card_id)
        return

    event_updated_at = parse_dt(str(data.get("updated_at") or "")) or now_fn()
    if should_skip_loop(link, event_updated_at, SyncSource.plane):
        log.info("card.updated: loop prevention skip", card_id=card_id)
        return

    gh_repo = link.gh_repo
    issue_number = link.gh_issue_number
    owner, repo = gh_repo.split("/", 1)

    plane_card_url = (
        f"{app_url.rstrip('/')}/{ws}/projects/{project_id}/issues/{card_id}/"
    )

    # --- State transition sync (US-010) ---
    state_detail_raw: Any = data.get("state_detail")
    if isinstance(state_detail_raw, dict):
        sd = cast("dict[str, Any]", state_detail_raw)
        state_group = str(sd.get("group") or "")
        state_name_val = str(sd.get("name") or "")

        skip_state = False
        if detect_conflict(link, event_updated_at, SyncSource.plane):
            log.warning(
                "card.updated: state conflict detected",
                card_id=card_id,
                last_synced_at=link.last_synced_at,
                event_updated_at=event_updated_at,
            )
            if not event_wins_conflict(link, event_updated_at):
                skip_state = True

        if not skip_state:
            if state_group in DONE_CANCEL_GROUPS:
                await github_client.close_issue(owner, repo, issue_number)
                link.last_synced_at = now_fn()
                link.sync_source_last = SyncSource.plane
                await session.commit()
                log.info(
                    "card.updated: state Done/Cancelled -> GH issue closed",
                    card_id=card_id,
                    state_group=state_group,
                )
                return
            elif state_name_val == EM_ANDAMENTO_STATE_NAME:
                await github_client.add_labels(owner, repo, issue_number, [IN_PROGRESS_LABEL])
                link.last_synced_at = now_fn()
                link.sync_source_last = SyncSource.plane
                await session.commit()
                log.info(
                    "card.updated: Em andamento -> in-progress label added",
                    card_id=card_id,
                )

    update_payload: dict[str, Any] = {}

    if "name" in data:
        update_payload["title"] = str(data["name"])

    if "description_html" in data:
        plane_desc = str(data.get("description_html") or "")
        clean_desc = strip_footer(plane_desc)
        update_payload["body"] = f"{clean_desc}\n\n---\nPlane: {plane_card_url}"

    label_ids_raw: Any = data.get("label_ids")
    if isinstance(label_ids_raw, list):
        gh_labels: list[str] = []
        for lid in cast("list[Any]", label_ids_raw):
            lm = await config_service.get_label_map(project_id, str(lid))
            if lm is None:
                log.info("card.updated: unknown Plane label skipped", label_id=lid)
            else:
                gh_labels.append(lm.gh_label)
        update_payload["labels"] = gh_labels

    assignees_raw: Any = data.get("assignees")
    if isinstance(assignees_raw, list):
        gh_assignees: list[str] = []
        for uid in cast("list[Any]", assignees_raw):
            um = await config_service.get_user_map(str(uid))
            if um is None:
                log.info("card.updated: unknown Plane user skipped", user_id=uid)
            else:
                gh_assignees.append(um.gh_login)
        update_payload["assignees"] = gh_assignees

    if not update_payload:
        log.debug("card.updated: no syncable fields, skipping", card_id=card_id)
        return

    log.info(
        "card.updated: syncing to github",
        card_id=card_id,
        issue_number=issue_number,
        fields=list(update_payload.keys()),
    )
    await github_client.update_issue(owner, repo, issue_number, update_payload)
    link.last_synced_at = now_fn()
    link.sync_source_last = SyncSource.plane
    await session.commit()
    log.info("card.updated synced to github", card_id=card_id, issue_number=issue_number)


async def process_plane_event(
    ctx: dict[str, Any], log_id: str, payload_json: str
) -> None:
    payload: dict[str, Any] = json.loads(payload_json)
    event_type: str = str(payload.get("event") or "")
    action: str = str(payload.get("action") or "")

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        log_id=log_id,
        event_type=event_type,
        action=action,
        source="plane",
    )

    # Plane sends event="issue" + action="created"/"updated" (observed) or "create"/"update" (docs)
    # Legacy/test format: event="card.created"/"card.updated"
    is_created = event_type == "card.created" or (event_type == "issue" and action in ("created", "create"))
    is_updated = event_type == "card.updated" or (event_type == "issue" and action in ("updated", "update"))
    metric_event_type = f"{event_type}.{action}" if action else event_type

    log.info("process_plane_event.started", metric_event_type=metric_event_type)

    async def _dispatch() -> None:
        async with ctx["session_factory"]() as session:
            if is_created:
                await handle_card_created(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                    github_client=ctx["github_client"],
                    config_service=ctx["config_service"],
                )
            elif is_updated:
                await handle_card_updated(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                    github_client=ctx["github_client"],
                    config_service=ctx["config_service"],
                )
            else:
                log.warning(
                    "process_plane_event: unhandled event",
                    event_type=event_type,
                    action=action,
                    log_id=log_id,
                )

    start = time.perf_counter()
    outcome = "success"
    try:
        await run_with_retry(
            _dispatch,
            ctx=ctx,
            source="plane",
            event_type=metric_event_type,
            payload_json=payload_json,
        )
    except DeadLetteredError:
        outcome = "dead_lettered"
    except Exception:
        outcome = "error"
        raise
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        log.info(
            "process_plane_event.finished",
            outcome=outcome,
            duration_ms=duration_ms,
            metric_event_type=metric_event_type,
        )
        sync_actions_total.labels(type=metric_event_type, outcome=outcome).inc()
        sync_duration_seconds.labels(type=metric_event_type).observe(
            time.perf_counter() - start
        )

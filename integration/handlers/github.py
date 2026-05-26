from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from integration.clients.github import GitHubClient
from integration.clients.plane import PlaneClient
from integration.config import settings
from integration.config_service import ConfigService
from integration.handlers._sync import (
    extract_gh_coords,
    fetch_link_by_gh,
    parse_dt,
    should_skip_loop,
    strip_footer,
)
from integration.models import CardIssueLink, SyncSource

log = structlog.get_logger()

REFINAMENTO_STATE_NAME = "Refinamento"
DEFAULT_LABEL_NAME = "Feature"
DEFAULT_PRIORITY = "medium"


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def handle_issue_opened(
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

    issue_raw: Any = payload.get("issue")
    issue: dict[str, Any] = (
        cast("dict[str, Any]", issue_raw) if isinstance(issue_raw, dict) else {}
    )
    repo_raw: Any = payload.get("repository")
    repo_data: dict[str, Any] = (
        cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    )

    gh_repo: str = str(repo_data.get("full_name") or "")
    if not gh_repo:
        log.warning("issues.opened skipped: missing repository.full_name")
        return

    repo_map = await config_service.get_repo_module_by_repo(gh_repo)
    if repo_map is None:
        log.warning("issues.opened skipped: no repo mapping", gh_repo=gh_repo)
        return

    plane_project_id = repo_map.plane_project_id

    issue_number: int = int(issue.get("number") or 0)
    issue_title: str = str(issue.get("title") or "")
    issue_body: str = str(issue.get("body") or "")
    issue_node_id: str = str(issue.get("node_id") or "")
    gh_labels_raw: Any = issue.get("labels")
    gh_labels: list[str] = []
    if isinstance(gh_labels_raw, list):
        for lbl in cast("list[Any]", gh_labels_raw):
            if isinstance(lbl, dict):
                lbl_dict = cast("dict[str, Any]", lbl)
                name = lbl_dict.get("name")
                if isinstance(name, str):
                    gh_labels.append(name)

    # Resolve Plane state "Refinamento"
    states = await plane_client.list_states(plane_project_id)
    refinamento_state_id: str | None = None
    for s in states:
        if str(s.get("name") or "") == REFINAMENTO_STATE_NAME:
            refinamento_state_id = str(s["id"])
            break

    # Resolve Plane label via label_map or default "Feature"
    plane_label_id: str | None = None
    for gh_label in gh_labels:
        lm = await config_service.get_label_map_by_gh(gh_repo, gh_label)
        if lm is not None:
            plane_label_id = lm.plane_label_id
            break

    if plane_label_id is None:
        labels = await plane_client.list_labels(plane_project_id)
        for lbl in labels:
            if str(lbl.get("name") or "") == DEFAULT_LABEL_NAME:
                plane_label_id = str(lbl["id"])
                break

    card_payload: dict[str, Any] = {
        "name": issue_title,
        "priority": DEFAULT_PRIORITY,
    }
    if refinamento_state_id is not None:
        card_payload["state"] = refinamento_state_id
    if plane_label_id is not None:
        card_payload["label_ids"] = [plane_label_id]

    card = await plane_client.create_card(plane_project_id, card_payload)
    card_id: str = str(card["id"])

    # Place in active cycle if one exists
    cycles = await plane_client.list_cycles(plane_project_id)
    for cycle in cycles:
        if str(cycle.get("status") or "") == "CURRENT":
            await plane_client.add_issue_to_cycle(plane_project_id, str(cycle["id"]), card_id)
            break

    plane_card_url = (
        f"{app_url.rstrip('/')}/{ws}/projects/{plane_project_id}/issues/{card_id}/"
    )

    owner, repo = gh_repo.split("/", 1)
    new_body = f"{issue_body}\n\n---\nPlane: {plane_card_url}"
    await github_client.update_issue(owner, repo, issue_number, {"body": new_body})

    link = CardIssueLink(
        plane_card_id=card_id,
        plane_project_id=plane_project_id,
        gh_repo=gh_repo,
        gh_issue_number=issue_number,
        gh_issue_node_id=issue_node_id,
        last_synced_at=now_fn(),
        sync_source_last=SyncSource.github,
    )
    session.add(link)
    await session.commit()
    log.info(
        "issues.opened synced to plane",
        gh_repo=gh_repo,
        gh_issue_number=issue_number,
        card_id=card_id,
    )


async def handle_issue_edited(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    now_fn: Callable[[], datetime] = _utcnow,
) -> None:
    issue, gh_repo, issue_number = extract_gh_coords(payload)
    if not gh_repo or not issue_number:
        return

    changes_raw: Any = payload.get("changes")
    changes: dict[str, Any] = (
        cast("dict[str, Any]", changes_raw) if isinstance(changes_raw, dict) else {}
    )
    title_changed = "title" in changes
    body_changed = "body" in changes
    if not title_changed and not body_changed:
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.warning("issues.edited: no link found", gh_repo=gh_repo, issue_number=issue_number)
        return

    event_updated_at = parse_dt(str(issue.get("updated_at") or "")) or now_fn()
    if should_skip_loop(link, event_updated_at, SyncSource.github):
        log.info("issues.edited: loop prevention skip", issue_number=issue_number)
        return

    update_payload: dict[str, Any] = {}
    if title_changed:
        update_payload["name"] = str(issue.get("title") or "")
    if body_changed:
        gh_body = str(issue.get("body") or "")
        clean_body = strip_footer(gh_body)
        gh_issue_url = f"https://github.com/{gh_repo}/issues/{issue_number}"
        update_payload["description_html"] = f"{clean_body}\n\n---\nGitHub: {gh_issue_url}"

    await plane_client.update_card(link.plane_project_id, link.plane_card_id, update_payload)
    link.last_synced_at = now_fn()
    link.sync_source_last = SyncSource.github
    await session.commit()
    log.info("issues.edited synced to plane", gh_repo=gh_repo, issue_number=issue_number)


async def handle_issue_labels_changed(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    config_service: ConfigService,
    now_fn: Callable[[], datetime] = _utcnow,
) -> None:
    issue, gh_repo, issue_number = extract_gh_coords(payload)
    if not gh_repo or not issue_number:
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.warning("issues.labeled: no link found", gh_repo=gh_repo, issue_number=issue_number)
        return

    event_updated_at = parse_dt(str(issue.get("updated_at") or "")) or now_fn()
    if should_skip_loop(link, event_updated_at, SyncSource.github):
        log.info("issues.labeled: loop prevention skip", issue_number=issue_number)
        return

    gh_labels_raw: Any = issue.get("labels")
    plane_label_ids: list[str] = []
    if isinstance(gh_labels_raw, list):
        for lbl in cast("list[Any]", gh_labels_raw):
            if isinstance(lbl, dict):
                lbl_dict = cast("dict[str, Any]", lbl)
                name = str(lbl_dict.get("name") or "")
                if not name:
                    continue
                lm = await config_service.get_label_map_by_gh(gh_repo, name)
                if lm is None:
                    log.info("issues.labeled: unknown GH label skipped", label=name)
                else:
                    plane_label_ids.append(lm.plane_label_id)

    await plane_client.update_card(
        link.plane_project_id,
        link.plane_card_id,
        {"label_ids": plane_label_ids},
    )
    link.last_synced_at = now_fn()
    link.sync_source_last = SyncSource.github
    await session.commit()
    log.info(
        "issues.labeled synced to plane",
        gh_repo=gh_repo,
        issue_number=issue_number,
        label_count=len(plane_label_ids),
    )


async def handle_issue_assignees_changed(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    config_service: ConfigService,
    now_fn: Callable[[], datetime] = _utcnow,
) -> None:
    issue, gh_repo, issue_number = extract_gh_coords(payload)
    if not gh_repo or not issue_number:
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.warning("issues.assigned: no link found", gh_repo=gh_repo, issue_number=issue_number)
        return

    event_updated_at = parse_dt(str(issue.get("updated_at") or "")) or now_fn()
    if should_skip_loop(link, event_updated_at, SyncSource.github):
        log.info("issues.assigned: loop prevention skip", issue_number=issue_number)
        return

    assignees_raw: Any = issue.get("assignees")
    plane_assignees: list[str] = []
    if isinstance(assignees_raw, list):
        for a in cast("list[Any]", assignees_raw):
            if isinstance(a, dict):
                a_dict = cast("dict[str, Any]", a)
                login = str(a_dict.get("login") or "")
                if not login:
                    continue
                um = await config_service.get_user_map_by_gh(login)
                if um is None:
                    log.info("issues.assigned: unknown GH user skipped", login=login)
                else:
                    plane_assignees.append(um.plane_user_id)

    await plane_client.update_card(
        link.plane_project_id,
        link.plane_card_id,
        {"assignees": plane_assignees},
    )
    link.last_synced_at = now_fn()
    link.sync_source_last = SyncSource.github
    await session.commit()
    log.info(
        "issues.assigned synced to plane",
        gh_repo=gh_repo,
        issue_number=issue_number,
        assignee_count=len(plane_assignees),
    )


async def process_github_event(
    ctx: dict[str, Any], log_id: str, payload_json: str
) -> None:
    payload: dict[str, Any] = json.loads(payload_json)
    action: str = str(payload.get("action") or "")
    event_type: str = str(ctx.get("event_type") or "")

    if "issue" in payload and action == "opened":
        async with ctx["session_factory"]() as session:
            await handle_issue_opened(
                payload,
                session=session,
                plane_client=ctx["plane_client"],
                github_client=ctx["github_client"],
                config_service=ctx["config_service"],
            )
    elif "issue" in payload and action == "edited":
        async with ctx["session_factory"]() as session:
            await handle_issue_edited(
                payload,
                session=session,
                plane_client=ctx["plane_client"],
            )
    elif "issue" in payload and action in ("labeled", "unlabeled"):
        async with ctx["session_factory"]() as session:
            await handle_issue_labels_changed(
                payload,
                session=session,
                plane_client=ctx["plane_client"],
                config_service=ctx["config_service"],
            )
    elif "issue" in payload and action in ("assigned", "unassigned"):
        async with ctx["session_factory"]() as session:
            await handle_issue_assignees_changed(
                payload,
                session=session,
                plane_client=ctx["plane_client"],
                config_service=ctx["config_service"],
            )
    else:
        log.debug(
            "process_github_event: unhandled event",
            action=action,
            event_type=event_type,
            log_id=log_id,
        )

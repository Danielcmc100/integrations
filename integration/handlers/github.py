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


async def process_github_event(
    ctx: dict[str, Any], log_id: str, payload_json: str
) -> None:
    payload: dict[str, Any] = json.loads(payload_json)
    action: str = str(payload.get("action") or "")
    event_type: str = str(ctx.get("event_type") or "")

    # Determine event kind from payload structure
    if "issue" in payload and action == "opened":
        async with ctx["session_factory"]() as session:
            await handle_issue_opened(
                payload,
                session=session,
                plane_client=ctx["plane_client"],
                github_client=ctx["github_client"],
                config_service=ctx["config_service"],
            )
    else:
        log.debug(
            "process_github_event: unhandled event",
            action=action,
            event_type=event_type,
            log_id=log_id,
        )

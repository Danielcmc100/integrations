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

BACKLOG_GROUP = "backlog"


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


async def process_plane_event(
    ctx: dict[str, Any], log_id: str, payload_json: str
) -> None:
    payload: dict[str, Any] = json.loads(payload_json)
    event_type: str = str(payload.get("event") or "")

    async with ctx["session_factory"]() as session:
        if event_type == "card.created":
            await handle_card_created(
                payload,
                session=session,
                plane_client=ctx["plane_client"],
                github_client=ctx["github_client"],
                config_service=ctx["config_service"],
            )
        else:
            log.debug(
                "process_plane_event: unhandled event",
                event_type=event_type,
                log_id=log_id,
            )

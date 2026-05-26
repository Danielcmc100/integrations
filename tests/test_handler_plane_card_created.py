from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.handlers.plane import handle_card_created
from integration.models import CardIssueLink, RepoModuleMap, SyncSource


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


FIXED_TIME = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
PLANE_WORKSPACE = "test-workspace"
PLANE_APP_URL = "https://app.plane.so"


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commit_count: int = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1


def _make_repo_map(
    module_id: str = "mod-1",
    project_id: str = "proj-1",
    gh_repo: str = "owner/repo",
) -> RepoModuleMap:
    rm = RepoModuleMap()
    rm.plane_module_id = module_id
    rm.plane_project_id = project_id
    rm.gh_repo = gh_repo
    return rm


def _make_clients(
    repo_map: RepoModuleMap | None,
) -> tuple[FakeSession, MagicMock, AsyncMock, AsyncMock]:
    session = FakeSession()

    plane_client = MagicMock()
    plane_client.update_card = AsyncMock()

    github_client = MagicMock()
    github_client.create_issue = AsyncMock(
        return_value={
            "number": 42,
            "node_id": "node-abc",
            "html_url": "https://github.com/owner/repo/issues/42",
        }
    )

    config_service = MagicMock()
    config_service.get_repo_module = AsyncMock(return_value=repo_map)

    return session, plane_client, github_client, config_service


def _make_payload(
    *,
    card_id: str = "card-1",
    project_id: str = "proj-1",
    name: str = "Test Card",
    description_html: str = "<p>desc</p>",
    state_group: str = "started",
    module_id: str = "mod-1",
) -> dict[str, Any]:
    return {
        "event": "card.created",
        "data": {
            "id": card_id,
            "project": project_id,
            "name": name,
            "description_html": description_html,
            "state_detail": {"group": state_group, "name": "In Progress"},
            "module": module_id,
            "module_ids": [module_id],
        },
    }


def _call(
    payload: dict[str, Any],
    session: FakeSession,
    plane_client: Any,
    github_client: Any,
    config_service: Any,
) -> None:
    _run(
        handle_card_created(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            github_client=github_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
            plane_workspace=PLANE_WORKSPACE,
            plane_app_url=PLANE_APP_URL,
        )
    )


def test_card_created_success() -> None:
    payload = _make_payload()
    repo_map = _make_repo_map()
    session, plane_client, github_client, config_service = _make_clients(repo_map)

    _call(payload, session, plane_client, github_client, config_service)

    github_client.create_issue.assert_called_once()
    call_args = github_client.create_issue.call_args[0]
    assert call_args[0] == "owner"
    assert call_args[1] == "repo"
    issue_payload: dict[str, Any] = call_args[2]
    assert issue_payload["title"] == "Test Card"
    assert (
        "Plane: https://app.plane.so/test-workspace/projects/proj-1/issues/card-1/"
        in issue_payload["body"]
    )
    assert "<p>desc</p>" in issue_payload["body"]

    plane_client.update_card.assert_called_once()
    update_args = plane_client.update_card.call_args[0]
    assert update_args[0] == "proj-1"
    assert update_args[1] == "card-1"
    new_desc: str = update_args[2]["description_html"]
    assert "GitHub: https://github.com/owner/repo/issues/42" in new_desc
    assert "<p>desc</p>" in new_desc

    assert len(session.added) == 1
    link = session.added[0]
    assert isinstance(link, CardIssueLink)
    assert link.plane_card_id == "card-1"
    assert link.plane_project_id == "proj-1"
    assert link.gh_repo == "owner/repo"
    assert link.gh_issue_number == 42
    assert link.gh_issue_node_id == "node-abc"
    assert link.last_synced_at == FIXED_TIME
    assert link.sync_source_last == SyncSource.plane

    assert session.commit_count == 1


def test_card_created_backlog_skipped() -> None:
    payload = _make_payload(state_group="backlog")
    session, plane_client, github_client, config_service = _make_clients(None)

    _call(payload, session, plane_client, github_client, config_service)

    github_client.create_issue.assert_not_called()
    plane_client.update_card.assert_not_called()
    assert session.added == []
    assert session.commit_count == 0


def test_card_created_no_module_skipped() -> None:
    payload: dict[str, Any] = {
        "event": "card.created",
        "data": {
            "id": "card-1",
            "project": "proj-1",
            "name": "Test",
            "description_html": "",
            "state_detail": {"group": "started"},
        },
    }
    session, plane_client, github_client, config_service = _make_clients(None)

    _call(payload, session, plane_client, github_client, config_service)

    github_client.create_issue.assert_not_called()
    assert session.added == []


def test_card_created_no_repo_map_skipped() -> None:
    payload = _make_payload()
    session, plane_client, github_client, config_service = _make_clients(repo_map=None)

    _call(payload, session, plane_client, github_client, config_service)

    github_client.create_issue.assert_not_called()
    assert session.added == []
    assert session.commit_count == 0


def test_card_created_module_ids_fallback() -> None:
    payload: dict[str, Any] = {
        "event": "card.created",
        "data": {
            "id": "card-2",
            "project": "proj-1",
            "name": "Card via module_ids",
            "description_html": "<p>x</p>",
            "state_detail": {"group": "unstarted"},
            "module_ids": ["mod-1"],
        },
    }
    repo_map = _make_repo_map()
    session, plane_client, github_client, config_service = _make_clients(repo_map)

    _call(payload, session, plane_client, github_client, config_service)

    github_client.create_issue.assert_called_once()
    config_service.get_repo_module.assert_called_once_with("mod-1")

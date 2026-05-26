from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.handlers.github import handle_issue_opened
from integration.models import CardIssueLink, LabelMap, RepoModuleMap, SyncSource


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


def _make_label_map(
    plane_project_id: str = "proj-1",
    plane_label_id: str = "label-plane-1",
    gh_repo: str = "owner/repo",
    gh_label: str = "bug",
) -> LabelMap:
    lm = LabelMap()
    lm.plane_project_id = plane_project_id
    lm.plane_label_id = plane_label_id
    lm.gh_repo = gh_repo
    lm.gh_label = gh_label
    return lm


def _make_clients(
    repo_map: RepoModuleMap | None,
    label_map: LabelMap | None = None,
    states: list[dict[str, Any]] | None = None,
    plane_labels: list[dict[str, Any]] | None = None,
    cycles: list[dict[str, Any]] | None = None,
) -> tuple[FakeSession, MagicMock, AsyncMock, AsyncMock]:
    if states is None:
        states = [{"id": "state-ref", "name": "Refinamento"}]
    if plane_labels is None:
        plane_labels = [{"id": "label-feature", "name": "Feature"}]
    if cycles is None:
        cycles = []

    session = FakeSession()

    plane_client = MagicMock()
    plane_client.list_states = AsyncMock(return_value=states)
    plane_client.list_labels = AsyncMock(return_value=plane_labels)
    plane_client.list_cycles = AsyncMock(return_value=cycles)
    plane_client.add_issue_to_cycle = AsyncMock(return_value={"id": "cycle-issue-1"})
    plane_client.create_card = AsyncMock(
        return_value={"id": "card-new-1"}
    )

    github_client = MagicMock()
    github_client.update_issue = AsyncMock(return_value={"number": 7})

    config_service = MagicMock()
    config_service.get_repo_module_by_repo = AsyncMock(return_value=repo_map)
    config_service.get_label_map_by_gh = AsyncMock(return_value=label_map)

    return session, plane_client, github_client, config_service


def _make_payload(
    *,
    action: str = "opened",
    issue_number: int = 7,
    issue_title: str = "Test Issue",
    issue_body: str = "Issue body",
    issue_node_id: str = "node-gh-1",
    gh_repo: str = "owner/repo",
    labels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": issue_number,
            "title": issue_title,
            "body": issue_body,
            "node_id": issue_node_id,
            "labels": labels or [],
        },
        "repository": {
            "full_name": gh_repo,
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
        handle_issue_opened(
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


def test_issue_opened_success_no_cycle() -> None:
    payload = _make_payload()
    repo_map = _make_repo_map()
    session, plane_client, github_client, config_service = _make_clients(repo_map)

    _call(payload, session, plane_client, github_client, config_service)

    plane_client.create_card.assert_called_once()
    create_args = plane_client.create_card.call_args[0]
    assert create_args[0] == "proj-1"
    card_data: dict[str, Any] = create_args[1]
    assert card_data["name"] == "Test Issue"
    assert card_data["priority"] == "medium"
    assert card_data["state"] == "state-ref"
    # default Feature label used
    assert card_data["label_ids"] == ["label-feature"]

    github_client.update_issue.assert_called_once()
    update_args = github_client.update_issue.call_args[0]
    assert update_args[0] == "owner"
    assert update_args[1] == "repo"
    assert update_args[2] == 7
    new_body: str = update_args[3]["body"]
    assert (
        "Plane: https://app.plane.so/test-workspace/projects/proj-1/issues/card-new-1/"
        in new_body
    )
    assert "Issue body" in new_body

    assert len(session.added) == 1
    link = session.added[0]
    assert isinstance(link, CardIssueLink)
    assert link.plane_card_id == "card-new-1"
    assert link.plane_project_id == "proj-1"
    assert link.gh_repo == "owner/repo"
    assert link.gh_issue_number == 7
    assert link.gh_issue_node_id == "node-gh-1"
    assert link.last_synced_at == FIXED_TIME
    assert link.sync_source_last == SyncSource.github
    assert session.commit_count == 1

    plane_client.add_issue_to_cycle.assert_not_called()


def test_issue_opened_placed_in_active_cycle() -> None:
    payload = _make_payload()
    repo_map = _make_repo_map()
    cycles = [
        {"id": "cycle-1", "status": "COMPLETED"},
        {"id": "cycle-2", "status": "CURRENT"},
    ]
    session, plane_client, github_client, config_service = _make_clients(
        repo_map, cycles=cycles
    )

    _call(payload, session, plane_client, github_client, config_service)

    plane_client.add_issue_to_cycle.assert_called_once_with("proj-1", "cycle-2", "card-new-1")


def test_issue_opened_label_inferred_from_label_map() -> None:
    payload = _make_payload(labels=[{"name": "bug"}])
    repo_map = _make_repo_map()
    label_map = _make_label_map(gh_label="bug", plane_label_id="label-bug-plane")
    session, plane_client, github_client, config_service = _make_clients(
        repo_map, label_map=label_map
    )

    _call(payload, session, plane_client, github_client, config_service)

    create_args = plane_client.create_card.call_args[0]
    card_data: dict[str, Any] = create_args[1]
    assert card_data["label_ids"] == ["label-bug-plane"]
    plane_client.list_labels.assert_not_called()


def test_issue_opened_no_repo_mapping_skipped() -> None:
    payload = _make_payload()
    session, plane_client, github_client, config_service = _make_clients(repo_map=None)

    _call(payload, session, plane_client, github_client, config_service)

    plane_client.create_card.assert_not_called()
    github_client.update_issue.assert_not_called()
    assert session.added == []
    assert session.commit_count == 0


def test_issue_opened_missing_repo_full_name_skipped() -> None:
    payload: dict[str, Any] = {
        "action": "opened",
        "issue": {"number": 1, "title": "x", "body": "", "node_id": "n"},
        "repository": {},
    }
    session, plane_client, github_client, config_service = _make_clients(repo_map=None)

    _call(payload, session, plane_client, github_client, config_service)

    plane_client.create_card.assert_not_called()
    assert session.added == []


def test_issue_opened_no_refinamento_state_creates_without_state() -> None:
    payload = _make_payload()
    repo_map = _make_repo_map()
    # No Refinamento state in list
    states: list[dict[str, Any]] = [{"id": "state-todo", "name": "Todo"}]
    session, plane_client, github_client, config_service = _make_clients(
        repo_map, states=states
    )

    _call(payload, session, plane_client, github_client, config_service)

    create_args = plane_client.create_card.call_args[0]
    card_data: dict[str, Any] = create_args[1]
    assert "state" not in card_data


def test_issue_opened_no_feature_label_creates_without_label() -> None:
    payload = _make_payload()
    repo_map = _make_repo_map()
    # No Feature label in Plane
    plane_labels: list[dict[str, Any]] = [{"id": "lbl-bug", "name": "Bug"}]
    session, plane_client, github_client, config_service = _make_clients(
        repo_map, plane_labels=plane_labels
    )

    _call(payload, session, plane_client, github_client, config_service)

    create_args = plane_client.create_card.call_args[0]
    card_data: dict[str, Any] = create_args[1]
    assert "label_ids" not in card_data

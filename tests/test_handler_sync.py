from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.handlers.github import (
    handle_issue_assignees_changed,
    handle_issue_edited,
    handle_issue_labels_changed,
)
from integration.handlers.plane import handle_card_updated
from integration.models import CardIssueLink, LabelMap, SyncSource, UserMap


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


FIXED_TIME = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
PLANE_WORKSPACE = "test-ws"
PLANE_APP_URL = "https://app.plane.so"
GH_REPO = "owner/repo"
ISSUE_NUMBER = 42
CARD_ID = "card-abc"
PROJECT_ID = "proj-1"

# 2 seconds after FIXED_TIME — outside 5s loop window for "different source" tests
EVENT_TIME = datetime(2026, 5, 25, 14, 0, 0, tzinfo=UTC)
# 2 seconds after FIXED_TIME — inside 5s loop window
WITHIN_5S = datetime(2026, 5, 25, 12, 0, 2, tzinfo=UTC)


class FakeResult:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


class FakeSession:
    def __init__(self, link: CardIssueLink | None = None) -> None:
        self.added: list[Any] = []
        self.commit_count: int = 0
        self._link = link

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1

    async def execute(self, stmt: Any) -> FakeResult:
        return FakeResult(self._link)


def _make_link(
    *,
    last_synced_at: datetime = FIXED_TIME,
    sync_source_last: SyncSource = SyncSource.plane,
) -> CardIssueLink:
    link = CardIssueLink()
    link.plane_card_id = CARD_ID
    link.plane_project_id = PROJECT_ID
    link.gh_repo = GH_REPO
    link.gh_issue_number = ISSUE_NUMBER
    link.gh_issue_node_id = "node-42"
    link.last_synced_at = last_synced_at
    link.sync_source_last = sync_source_last
    return link


def _make_label_map(
    gh_label: str = "bug",
    plane_label_id: str = "pl-lbl-1",
) -> LabelMap:
    lm = LabelMap()
    lm.plane_project_id = PROJECT_ID
    lm.plane_label_id = plane_label_id
    lm.gh_repo = GH_REPO
    lm.gh_label = gh_label
    return lm


def _make_user_map(gh_login: str = "alice", plane_user_id: str = "pu-1") -> UserMap:
    um = UserMap()
    um.plane_user_id = plane_user_id
    um.gh_login = gh_login
    um.discord_user_id = None
    return um


# ---------------------------------------------------------------------------
# handle_issue_edited
# ---------------------------------------------------------------------------


def test_issue_edited_title_syncs_to_plane() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})

    payload: dict[str, Any] = {
        "action": "edited",
        "issue": {
            "number": ISSUE_NUMBER,
            "title": "New Title",
            "body": "body text",
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "changes": {"title": {"from": "Old Title"}},
    }
    _run(
        handle_issue_edited(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_called_once()
    call_args = plane_client.update_card.call_args[0]
    assert call_args[0] == PROJECT_ID
    assert call_args[1] == CARD_ID
    update: dict[str, Any] = call_args[2]
    assert update.get("name") == "New Title"
    assert "description_html" not in update
    assert session.commit_count == 1
    assert link.sync_source_last == SyncSource.github
    assert link.last_synced_at == FIXED_TIME


def test_issue_edited_body_strips_plane_footer_adds_gh_footer() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})

    plane_footer = "https://app.plane.so/ws/projects/proj-1/issues/card-abc/"
    gh_body = f"My content\n\n---\nPlane: {plane_footer}"
    payload: dict[str, Any] = {
        "action": "edited",
        "issue": {
            "number": ISSUE_NUMBER,
            "title": "Title",
            "body": gh_body,
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "changes": {"body": {"from": "Old body"}},
    }
    _run(
        handle_issue_edited(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_called_once()
    update: dict[str, Any] = plane_client.update_card.call_args[0][2]
    desc: str = update["description_html"]
    assert f"GitHub: https://github.com/{GH_REPO}/issues/{ISSUE_NUMBER}" in desc
    assert "Plane:" not in desc
    assert "My content" in desc


def test_issue_edited_no_link_skips() -> None:
    session = FakeSession(link=None)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock()

    payload: dict[str, Any] = {
        "action": "edited",
        "issue": {"number": ISSUE_NUMBER, "title": "T", "body": "", "updated_at": ""},
        "repository": {"full_name": GH_REPO},
        "changes": {"title": {"from": "Old"}},
    }
    _run(
        handle_issue_edited(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )
    plane_client.update_card.assert_not_called()
    assert session.commit_count == 0


def test_issue_edited_loop_prevention_skips() -> None:
    link = _make_link(last_synced_at=FIXED_TIME, sync_source_last=SyncSource.github)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock()

    payload: dict[str, Any] = {
        "action": "edited",
        "issue": {
            "number": ISSUE_NUMBER,
            "title": "New Title",
            "body": "body",
            "updated_at": WITHIN_5S.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "changes": {"title": {"from": "Old"}},
    }
    _run(
        handle_issue_edited(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )
    plane_client.update_card.assert_not_called()
    assert session.commit_count == 0


# ---------------------------------------------------------------------------
# handle_issue_labels_changed
# ---------------------------------------------------------------------------


def test_issue_labeled_known_label_syncs_to_plane() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    lm = _make_label_map(gh_label="bug", plane_label_id="pl-lbl-bug")
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_label_map_by_gh = AsyncMock(return_value=lm)

    payload: dict[str, Any] = {
        "action": "labeled",
        "issue": {
            "number": ISSUE_NUMBER,
            "labels": [{"name": "bug"}],
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "label": {"name": "bug"},
    }
    _run(
        handle_issue_labels_changed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_called_once()
    update: dict[str, Any] = plane_client.update_card.call_args[0][2]
    assert update["label_ids"] == ["pl-lbl-bug"]
    assert session.commit_count == 1
    assert link.sync_source_last == SyncSource.github


def test_issue_labeled_unknown_label_results_in_empty_list() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_label_map_by_gh = AsyncMock(return_value=None)

    payload: dict[str, Any] = {
        "action": "labeled",
        "issue": {
            "number": ISSUE_NUMBER,
            "labels": [{"name": "unknown-label"}],
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "label": {"name": "unknown-label"},
    }
    _run(
        handle_issue_labels_changed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )
    update: dict[str, Any] = plane_client.update_card.call_args[0][2]
    assert update["label_ids"] == []


def test_issue_unlabeled_removes_label() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_label_map_by_gh = AsyncMock(return_value=None)

    # After unlabeling, issue.labels is empty
    payload: dict[str, Any] = {
        "action": "unlabeled",
        "issue": {
            "number": ISSUE_NUMBER,
            "labels": [],
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "label": {"name": "bug"},
    }
    _run(
        handle_issue_labels_changed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )
    update: dict[str, Any] = plane_client.update_card.call_args[0][2]
    assert update["label_ids"] == []
    assert session.commit_count == 1


# ---------------------------------------------------------------------------
# handle_issue_assignees_changed
# ---------------------------------------------------------------------------


def test_issue_assigned_known_user_syncs_to_plane() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    um = _make_user_map(gh_login="alice", plane_user_id="pu-alice")
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_user_map_by_gh = AsyncMock(return_value=um)

    payload: dict[str, Any] = {
        "action": "assigned",
        "issue": {
            "number": ISSUE_NUMBER,
            "assignees": [{"login": "alice"}],
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "assignee": {"login": "alice"},
    }
    _run(
        handle_issue_assignees_changed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_called_once()
    update: dict[str, Any] = plane_client.update_card.call_args[0][2]
    assert update["assignees"] == ["pu-alice"]
    assert session.commit_count == 1
    assert link.sync_source_last == SyncSource.github


def test_issue_assigned_unknown_user_results_in_empty_list() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_user_map_by_gh = AsyncMock(return_value=None)

    payload: dict[str, Any] = {
        "action": "assigned",
        "issue": {
            "number": ISSUE_NUMBER,
            "assignees": [{"login": "ghost"}],
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "assignee": {"login": "ghost"},
    }
    _run(
        handle_issue_assignees_changed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )
    update: dict[str, Any] = plane_client.update_card.call_args[0][2]
    assert update["assignees"] == []


def test_issue_unassigned_clears_assignee() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_user_map_by_gh = AsyncMock(return_value=None)

    payload: dict[str, Any] = {
        "action": "unassigned",
        "issue": {
            "number": ISSUE_NUMBER,
            "assignees": [],
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
        "assignee": {"login": "alice"},
    }
    _run(
        handle_issue_assignees_changed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )
    update: dict[str, Any] = plane_client.update_card.call_args[0][2]
    assert update["assignees"] == []
    assert session.commit_count == 1


# ---------------------------------------------------------------------------
# handle_card_updated
# ---------------------------------------------------------------------------


def test_card_updated_title_and_body_syncs_to_github() -> None:
    link = _make_link(sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.update_issue = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_label_map = AsyncMock(return_value=None)
    config_service.get_user_map = AsyncMock(return_value=None)

    gh_footer = f"https://github.com/{GH_REPO}/issues/{ISSUE_NUMBER}"
    plane_desc = f"New desc\n\n---\nGitHub: {gh_footer}"
    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "name": "Updated Title",
            "description_html": plane_desc,
            "updated_at": EVENT_TIME.isoformat(),
        },
    }
    _run(
        handle_card_updated(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=MagicMock(),
            github_client=github_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
            plane_workspace=PLANE_WORKSPACE,
            plane_app_url=PLANE_APP_URL,
        )
    )

    github_client.update_issue.assert_called_once()
    call_args = github_client.update_issue.call_args[0]
    assert call_args[0] == "owner"
    assert call_args[1] == "repo"
    assert call_args[2] == ISSUE_NUMBER
    update: dict[str, Any] = call_args[3]
    assert update["title"] == "Updated Title"
    body: str = update["body"]
    assert "Plane:" in body
    assert "GitHub:" not in body
    assert "New desc" in body
    assert session.commit_count == 1
    assert link.sync_source_last == SyncSource.plane


def test_card_updated_labels_and_assignees_syncs_to_github() -> None:
    link = _make_link(sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.update_issue = AsyncMock(return_value={})
    lm = _make_label_map(gh_label="bug", plane_label_id="pl-lbl-bug")
    um = _make_user_map(gh_login="alice", plane_user_id="pu-alice")
    config_service = MagicMock()
    config_service.get_label_map = AsyncMock(return_value=lm)
    config_service.get_user_map = AsyncMock(return_value=um)

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "label_ids": ["pl-lbl-bug"],
            "assignees": ["pu-alice"],
            "updated_at": EVENT_TIME.isoformat(),
        },
    }
    _run(
        handle_card_updated(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=MagicMock(),
            github_client=github_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
            plane_workspace=PLANE_WORKSPACE,
            plane_app_url=PLANE_APP_URL,
        )
    )

    update: dict[str, Any] = github_client.update_issue.call_args[0][3]
    assert update["labels"] == ["bug"]
    assert update["assignees"] == ["alice"]


def test_card_updated_unknown_label_skipped() -> None:
    link = _make_link(sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.update_issue = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_label_map = AsyncMock(return_value=None)
    config_service.get_user_map = AsyncMock(return_value=None)

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "label_ids": ["unknown-pl-lbl"],
            "updated_at": EVENT_TIME.isoformat(),
        },
    }
    _run(
        handle_card_updated(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=MagicMock(),
            github_client=github_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
            plane_workspace=PLANE_WORKSPACE,
            plane_app_url=PLANE_APP_URL,
        )
    )
    update: dict[str, Any] = github_client.update_issue.call_args[0][3]
    assert update["labels"] == []


def test_card_updated_loop_prevention_skips() -> None:
    link = _make_link(last_synced_at=FIXED_TIME, sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.update_issue = AsyncMock()
    config_service = MagicMock()

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "name": "New name",
            "updated_at": WITHIN_5S.isoformat(),
        },
    }
    _run(
        handle_card_updated(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=MagicMock(),
            github_client=github_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
            plane_workspace=PLANE_WORKSPACE,
            plane_app_url=PLANE_APP_URL,
        )
    )
    github_client.update_issue.assert_not_called()
    assert session.commit_count == 0


def test_card_updated_no_link_skips() -> None:
    session = FakeSession(link=None)
    github_client = MagicMock()
    github_client.update_issue = AsyncMock()
    config_service = MagicMock()

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "name": "Title",
            "updated_at": EVENT_TIME.isoformat(),
        },
    }
    _run(
        handle_card_updated(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=MagicMock(),
            github_client=github_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
            plane_workspace=PLANE_WORKSPACE,
            plane_app_url=PLANE_APP_URL,
        )
    )
    github_client.update_issue.assert_not_called()
    assert session.commit_count == 0


def test_card_updated_no_syncable_fields_skips_commit() -> None:
    link = _make_link(sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.update_issue = AsyncMock()
    config_service = MagicMock()

    # Payload with no syncable fields (no name, description_html, label_ids, assignees)
    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "updated_at": EVENT_TIME.isoformat(),
        },
    }
    _run(
        handle_card_updated(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=MagicMock(),
            github_client=github_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
            plane_workspace=PLANE_WORKSPACE,
            plane_app_url=PLANE_APP_URL,
        )
    )
    github_client.update_issue.assert_not_called()
    assert session.commit_count == 0

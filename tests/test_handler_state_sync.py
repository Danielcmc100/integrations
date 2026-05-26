from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.handlers.github import handle_issue_closed
from integration.handlers.plane import handle_card_updated
from integration.models import CardIssueLink, SyncSource


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


FIXED_TIME = datetime(2026, 5, 26, 10, 0, 0, tzinfo=UTC)
GH_REPO = "owner/repo"
ISSUE_NUMBER = 7
CARD_ID = "card-state"
PROJECT_ID = "proj-state"
PLANE_WORKSPACE = "ws"
PLANE_APP_URL = "https://app.plane.so"

EVENT_TIME = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
WITHIN_5S = FIXED_TIME + timedelta(seconds=2)
WITHIN_10S = FIXED_TIME + timedelta(seconds=6)
AFTER_10S = FIXED_TIME + timedelta(seconds=15)


class FakeResult:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


class FakeSession:
    def __init__(self, link: CardIssueLink | None = None) -> None:
        self.commit_count: int = 0
        self._link = link

    def add(self, obj: Any) -> None:
        pass

    async def commit(self) -> None:
        self.commit_count += 1

    async def execute(self, stmt: Any) -> FakeResult:
        return FakeResult(self._link)


def _make_link(
    *,
    last_synced_at: datetime = FIXED_TIME,
    sync_source_last: SyncSource = SyncSource.github,
) -> CardIssueLink:
    link = CardIssueLink()
    link.plane_card_id = CARD_ID
    link.plane_project_id = PROJECT_ID
    link.gh_repo = GH_REPO
    link.gh_issue_number = ISSUE_NUMBER
    link.gh_issue_node_id = "node-7"
    link.last_synced_at = last_synced_at
    link.sync_source_last = sync_source_last
    return link


# ---------------------------------------------------------------------------
# Plane Done/Cancelled -> GH issue closed
# ---------------------------------------------------------------------------


def test_plane_done_closes_gh_issue() -> None:
    link = _make_link(sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.close_issue = AsyncMock(return_value={})
    github_client.update_issue = AsyncMock(return_value={})
    config_service = MagicMock()

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "updated_at": EVENT_TIME.isoformat(),
            "state_detail": {"group": "completed", "name": "Done"},
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

    github_client.close_issue.assert_called_once_with("owner", "repo", ISSUE_NUMBER)
    github_client.update_issue.assert_not_called()
    assert session.commit_count == 1
    assert link.sync_source_last == SyncSource.plane


def test_plane_cancelled_closes_gh_issue() -> None:
    link = _make_link(sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.close_issue = AsyncMock(return_value={})
    config_service = MagicMock()

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "updated_at": EVENT_TIME.isoformat(),
            "state_detail": {"group": "cancelled", "name": "Cancelled"},
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

    github_client.close_issue.assert_called_once()
    assert link.sync_source_last == SyncSource.plane


def test_plane_em_andamento_adds_in_progress_label() -> None:
    link = _make_link(sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.add_labels = AsyncMock(return_value=[])
    github_client.update_issue = AsyncMock(return_value={})
    config_service = MagicMock()
    config_service.get_label_map = AsyncMock(return_value=None)
    config_service.get_user_map = AsyncMock(return_value=None)

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "updated_at": EVENT_TIME.isoformat(),
            "state_detail": {"group": "started", "name": "Em andamento"},
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

    github_client.add_labels.assert_called_once_with("owner", "repo", ISSUE_NUMBER, ["in-progress"])
    github_client.close_issue = getattr(github_client, "close_issue", MagicMock())
    assert link.sync_source_last == SyncSource.plane


def test_plane_state_loop_prevention_skips() -> None:
    link = _make_link(last_synced_at=FIXED_TIME, sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.close_issue = AsyncMock(return_value={})
    config_service = MagicMock()

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "updated_at": WITHIN_5S.isoformat(),
            "state_detail": {"group": "completed", "name": "Done"},
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

    github_client.close_issue.assert_not_called()
    assert session.commit_count == 0


def test_plane_state_conflict_plane_wins() -> None:
    # sync_source_last=github means last sync came from github side
    # Plane event arrives 6s after (within 10s conflict window but plane is newer)
    link = _make_link(last_synced_at=FIXED_TIME, sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.close_issue = AsyncMock(return_value={})
    config_service = MagicMock()

    event_time = FIXED_TIME + timedelta(seconds=6)
    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "updated_at": event_time.isoformat(),
            "state_detail": {"group": "completed", "name": "Done"},
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

    github_client.close_issue.assert_called_once()


def test_plane_state_conflict_github_wins_skips() -> None:
    # sync_source_last=github, last_synced_at is NEWER than event → github wins
    event_time = FIXED_TIME - timedelta(seconds=3)
    link = _make_link(last_synced_at=FIXED_TIME, sync_source_last=SyncSource.github)
    session = FakeSession(link)
    github_client = MagicMock()
    github_client.close_issue = AsyncMock(return_value={})
    config_service = MagicMock()

    payload: dict[str, Any] = {
        "event": "card.updated",
        "data": {
            "id": CARD_ID,
            "project": PROJECT_ID,
            "updated_at": event_time.isoformat(),
            "state_detail": {"group": "completed", "name": "Done"},
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

    github_client.close_issue.assert_not_called()
    assert session.commit_count == 0


# ---------------------------------------------------------------------------
# GH issue closed -> Plane card moved to Done
# ---------------------------------------------------------------------------


def _plane_states() -> list[dict[str, Any]]:
    return [
        {"id": "state-todo", "name": "Todo", "group": "unstarted"},
        {"id": "state-done", "name": "Done", "group": "completed"},
    ]


def test_gh_issue_closed_moves_plane_card_to_done() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.list_states = AsyncMock(return_value=_plane_states())
    plane_client.update_card = AsyncMock(return_value={})

    payload: dict[str, Any] = {
        "action": "closed",
        "issue": {
            "number": ISSUE_NUMBER,
            "state": "closed",
            "state_reason": "completed",
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
    }
    _run(
        handle_issue_closed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_called_once_with(PROJECT_ID, CARD_ID, {"state": "state-done"})
    assert session.commit_count == 1
    assert link.sync_source_last == SyncSource.github


def test_gh_issue_closed_by_pr_skips() -> None:
    link = _make_link(sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.list_states = AsyncMock(return_value=_plane_states())
    plane_client.update_card = AsyncMock(return_value={})

    payload: dict[str, Any] = {
        "action": "closed",
        "issue": {
            "number": ISSUE_NUMBER,
            "state": "closed",
            "state_reason": "completed_by_pull_request",
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
    }
    _run(
        handle_issue_closed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_not_called()
    assert session.commit_count == 0


def test_gh_issue_closed_no_link_skips() -> None:
    session = FakeSession(link=None)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})

    payload: dict[str, Any] = {
        "action": "closed",
        "issue": {
            "number": ISSUE_NUMBER,
            "state": "closed",
            "state_reason": "completed",
            "updated_at": EVENT_TIME.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
    }
    _run(
        handle_issue_closed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_not_called()


def test_gh_issue_closed_loop_prevention_skips() -> None:
    link = _make_link(last_synced_at=FIXED_TIME, sync_source_last=SyncSource.github)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.update_card = AsyncMock(return_value={})

    payload: dict[str, Any] = {
        "action": "closed",
        "issue": {
            "number": ISSUE_NUMBER,
            "state": "closed",
            "state_reason": "completed",
            "updated_at": WITHIN_5S.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
    }
    _run(
        handle_issue_closed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_not_called()
    assert session.commit_count == 0


def test_gh_issue_closed_conflict_gh_wins() -> None:
    # sync_source_last=plane, gh event arrives 6s later (within 10s) and is newer
    link = _make_link(last_synced_at=FIXED_TIME, sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.list_states = AsyncMock(return_value=_plane_states())
    plane_client.update_card = AsyncMock(return_value={})

    event_time = FIXED_TIME + timedelta(seconds=6)
    payload: dict[str, Any] = {
        "action": "closed",
        "issue": {
            "number": ISSUE_NUMBER,
            "state": "closed",
            "state_reason": "completed",
            "updated_at": event_time.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
    }
    _run(
        handle_issue_closed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_called_once()


def test_gh_issue_closed_conflict_plane_wins_skips() -> None:
    # sync_source_last=plane, gh event is OLDER → plane wins, skip
    event_time = FIXED_TIME - timedelta(seconds=3)
    link = _make_link(last_synced_at=FIXED_TIME, sync_source_last=SyncSource.plane)
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.list_states = AsyncMock(return_value=_plane_states())
    plane_client.update_card = AsyncMock(return_value={})

    payload: dict[str, Any] = {
        "action": "closed",
        "issue": {
            "number": ISSUE_NUMBER,
            "state": "closed",
            "state_reason": "completed",
            "updated_at": event_time.isoformat(),
        },
        "repository": {"full_name": GH_REPO},
    }
    _run(
        handle_issue_closed(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_not_called()
    assert session.commit_count == 0

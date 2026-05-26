from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.handlers.github import handle_issue_comment_created
from integration.models import CardIssueLink, SyncSource


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


FIXED_TIME = datetime(2026, 5, 26, 10, 0, 0, tzinfo=UTC)
GH_REPO = "owner/repo"
ISSUE_NUMBER = 42
CARD_ID = "card-comment"
PROJECT_ID = "proj-comment"


class FakeResult:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


class FakeSession:
    def __init__(self, link: CardIssueLink | None = None) -> None:
        self._link = link

    async def execute(self, stmt: Any) -> FakeResult:
        return FakeResult(self._link)


def _make_link() -> CardIssueLink:
    link = CardIssueLink()
    link.plane_card_id = CARD_ID
    link.plane_project_id = PROJECT_ID
    link.gh_repo = GH_REPO
    link.gh_issue_number = ISSUE_NUMBER
    link.gh_issue_node_id = "node-42"
    link.last_synced_at = FIXED_TIME
    link.sync_source_last = SyncSource.github
    return link


def _payload(login: str = "alice", body: str = "Looks good!") -> dict[str, Any]:
    return {
        "action": "created",
        "comment": {"body": body, "user": {"login": login}},
        "issue": {"number": ISSUE_NUMBER},
        "repository": {"full_name": GH_REPO},
    }


def test_comment_synced_to_plane() -> None:
    link = _make_link()
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.add_comment = AsyncMock(return_value={})

    _run(
        handle_issue_comment_created(
            _payload(login="alice", body="Looks good!"),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
        )
    )

    plane_client.add_comment.assert_called_once_with(
        PROJECT_ID, CARD_ID, "[GitHub @alice]: Looks good!"
    )


def test_bot_comment_skipped() -> None:
    link = _make_link()
    session = FakeSession(link)
    plane_client = MagicMock()
    plane_client.add_comment = AsyncMock(return_value={})

    _run(
        handle_issue_comment_created(
            _payload(login="my-app[bot]"),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            github_bot_login="my-app[bot]",
        )
    )

    plane_client.add_comment.assert_not_called()


def test_no_link_skips() -> None:
    session = FakeSession(link=None)
    plane_client = MagicMock()
    plane_client.add_comment = AsyncMock(return_value={})

    _run(
        handle_issue_comment_created(
            _payload(),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
        )
    )

    plane_client.add_comment.assert_not_called()


def test_missing_repo_skips() -> None:
    session = FakeSession(_make_link())
    plane_client = MagicMock()
    plane_client.add_comment = AsyncMock(return_value={})

    payload: dict[str, Any] = {
        "action": "created",
        "comment": {"body": "hi", "user": {"login": "bob"}},
        "issue": {"number": ISSUE_NUMBER},
        "repository": {},
    }
    _run(
        handle_issue_comment_created(
            payload,
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
        )
    )

    plane_client.add_comment.assert_not_called()

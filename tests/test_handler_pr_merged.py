from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.handlers.github import handle_pr_merged
from integration.models import CardIssueLink, SyncSource


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


FIXED_TIME = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
GH_REPO = "owner/repo"
ISSUE_NUMBER = 42
CARD_ID = "card-pr-001"
PROJECT_ID = "proj-pr-001"
PR_URL = "https://github.com/owner/repo/pull/7"
MERGE_SHA = "abc1234"


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


class FakeSessionSequence:
    """Returns different links per execute call."""

    def __init__(self, results: list[CardIssueLink | None]) -> None:
        self._results = results
        self._idx = 0
        self.commit_count: int = 0

    def add(self, obj: Any) -> None:
        pass

    async def commit(self) -> None:
        self.commit_count += 1

    async def execute(self, stmt: Any) -> FakeResult:
        result = self._results[self._idx] if self._idx < len(self._results) else None
        self._idx += 1
        return FakeResult(result)


def _make_link() -> CardIssueLink:
    link = CardIssueLink()
    link.plane_card_id = CARD_ID
    link.plane_project_id = PROJECT_ID
    link.gh_repo = GH_REPO
    link.gh_issue_number = ISSUE_NUMBER
    link.gh_issue_node_id = "node-42"
    link.last_synced_at = FIXED_TIME
    link.sync_source_last = SyncSource.plane
    return link


def _plane_states() -> list[dict[str, Any]]:
    return [
        {"id": "state-todo", "name": "Todo", "group": "unstarted"},
        {"id": "state-done", "name": "Done", "group": "completed"},
    ]


def _make_plane_client(*, has_link: bool = True) -> MagicMock:
    client = MagicMock()
    client.list_states = AsyncMock(return_value=_plane_states())
    client.update_card = AsyncMock(return_value={})
    client.add_comment = AsyncMock(return_value={})
    client.get_card_by_sequence = AsyncMock(
        return_value={"id": CARD_ID} if has_link else None
    )
    return client


def _make_config_service(plane_project_id: str | None = PROJECT_ID) -> MagicMock:
    cs = MagicMock()
    if plane_project_id is not None:
        repo_map = MagicMock()
        repo_map.plane_project_id = plane_project_id
        cs.get_repo_module_by_repo = AsyncMock(return_value=repo_map)
    else:
        cs.get_repo_module_by_repo = AsyncMock(return_value=None)
    return cs


def _merged_payload(
    *,
    branch: str = f"{ISSUE_NUMBER}-my-feature",
    body: str = "",
    merged: bool = True,
) -> dict[str, Any]:
    return {
        "action": "closed",
        "pull_request": {
            "merged": merged,
            "html_url": PR_URL,
            "merge_commit_sha": MERGE_SHA,
            "body": body,
            "head": {"ref": branch},
        },
        "repository": {"full_name": GH_REPO},
    }


# ---------------------------------------------------------------------------
# Happy path: merged PR closes linked card
# ---------------------------------------------------------------------------


def test_pr_merged_transitions_card_to_done() -> None:
    link = _make_link()
    session = FakeSession(link)
    plane_client = _make_plane_client()
    config_service = _make_config_service()

    _run(
        handle_pr_merged(
            _merged_payload(),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_called_once_with(PROJECT_ID, CARD_ID, {"state": "state-done"})
    expected_comment = f"Fechado via PR {PR_URL} (merge {MERGE_SHA})"
    plane_client.add_comment.assert_called_once_with(PROJECT_ID, CARD_ID, expected_comment)
    assert session.commit_count == 1
    assert link.sync_source_last == SyncSource.github


# ---------------------------------------------------------------------------
# Not merged -> no-op
# ---------------------------------------------------------------------------


def test_pr_closed_not_merged_noop() -> None:
    session = FakeSession(_make_link())
    plane_client = _make_plane_client()
    config_service = _make_config_service()

    _run(
        handle_pr_merged(
            _merged_payload(merged=False),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_not_called()
    plane_client.add_comment.assert_not_called()
    assert session.commit_count == 0


# ---------------------------------------------------------------------------
# Branch doesn't match regex -> WARN, no-op
# ---------------------------------------------------------------------------


def test_pr_merged_branch_no_match_noop() -> None:
    session = FakeSession(_make_link())
    plane_client = _make_plane_client()
    config_service = _make_config_service()

    _run(
        handle_pr_merged(
            _merged_payload(branch="feature/no-number"),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_not_called()
    assert session.commit_count == 0


# ---------------------------------------------------------------------------
# Branch matches but no link found
# ---------------------------------------------------------------------------


def test_pr_merged_no_link_noop() -> None:
    session = FakeSession(None)
    plane_client = _make_plane_client(has_link=False)
    config_service = _make_config_service()

    _run(
        handle_pr_merged(
            _merged_payload(),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.update_card.assert_not_called()
    assert session.commit_count == 0


# ---------------------------------------------------------------------------
# Fallback: resolve by Plane sequence_id
# ---------------------------------------------------------------------------


def test_pr_merged_sequence_fallback() -> None:
    link = _make_link()
    # First execute (fetch_link_by_gh) returns None; second (fetch_link_by_plane) returns link
    session = FakeSessionSequence([None, link])
    plane_client = _make_plane_client(has_link=True)
    config_service = _make_config_service()

    _run(
        handle_pr_merged(
            _merged_payload(),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )

    plane_client.get_card_by_sequence.assert_called_once_with(PROJECT_ID, ISSUE_NUMBER)
    plane_client.update_card.assert_called_once_with(PROJECT_ID, CARD_ID, {"state": "state-done"})
    assert session.commit_count == 1


# ---------------------------------------------------------------------------
# Body "Closes #N" parsed and additional cards transitioned
# ---------------------------------------------------------------------------


def test_pr_merged_body_closes_transitions_cards() -> None:
    link1 = _make_link()
    link2 = CardIssueLink()
    link2.plane_card_id = "card-pr-099"
    link2.plane_project_id = PROJECT_ID
    link2.gh_repo = GH_REPO
    link2.gh_issue_number = 99
    link2.gh_issue_node_id = "node-99"
    link2.last_synced_at = FIXED_TIME
    link2.sync_source_last = SyncSource.plane

    call_count = 0

    class FakeSessionMulti:
        def __init__(self) -> None:
            self.commit_count = 0

        def add(self, obj: Any) -> None:
            pass

        async def commit(self) -> None:
            self.commit_count += 1

        async def execute(self, stmt: Any) -> FakeResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeResult(link1)
            return FakeResult(link2)

    session = FakeSessionMulti()
    plane_client = _make_plane_client()
    config_service = _make_config_service()

    body = "This PR implements the feature.\n\nCloses #99"
    _run(
        handle_pr_merged(
            _merged_payload(body=body),
            session=session,  # type: ignore[arg-type]
            plane_client=plane_client,
            config_service=config_service,
            now_fn=lambda: FIXED_TIME,
        )
    )

    assert plane_client.update_card.call_count == 2
    assert plane_client.add_comment.call_count == 2
    assert session.commit_count == 2

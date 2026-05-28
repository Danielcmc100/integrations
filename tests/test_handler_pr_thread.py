from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.handlers.github import (
    handle_check_suite_completed,
    handle_pr_closed_discord,
    handle_pr_review_submitted,
)
from integration.models import PrNotificationState


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


FIXED_TIME = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
GH_REPO = "owner/repo"
PR_NUMBER = 42
PR_NODE_ID = "PR_kwDOXYZ123"
DISCORD_CHANNEL_ID = "111222333"
DISCORD_MSG_ID = "999888777"
DISCORD_THREAD_ID = "444555666"


class FakeResult:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


class FakeSession:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.commit_count: int = 0
        self._results: list[Any] = results or []
        self._call_count: int = 0
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1

    async def execute(self, stmt: Any) -> FakeResult:
        idx = self._call_count
        self._call_count += 1
        obj = self._results[idx] if idx < len(self._results) else None
        return FakeResult(obj)


def _make_pr_state(
    *,
    ready_notified_at: datetime | None = FIXED_TIME,
    discord_message_id: str | None = DISCORD_MSG_ID,
    discord_thread_id: str | None = DISCORD_THREAD_ID,
    last_ready_cycle_id: str = "cycle-001",
) -> PrNotificationState:
    state = PrNotificationState()
    state.pr_node_id = PR_NODE_ID
    state.gh_repo = GH_REPO
    state.pr_number = PR_NUMBER
    state.last_ready_cycle_id = last_ready_cycle_id
    state.ready_notified_at = ready_notified_at
    state.discord_message_id = discord_message_id
    state.discord_thread_id = discord_thread_id
    return state


def _make_pr(
    *,
    draft: bool = False,
    title: str = "Add feature",
    merged: bool = False,
    html_url: str = "https://github.com/owner/repo/pull/42",
) -> dict[str, Any]:
    return {
        "node_id": PR_NODE_ID,
        "number": PR_NUMBER,
        "title": title,
        "html_url": html_url,
        "draft": draft,
        "merged": merged,
        "additions": 10,
        "deletions": 5,
        "user": {"login": "dev"},
        "head": {
            "ref": f"{PR_NUMBER}-feature",
            "sha": "abc123",
            "repo": {"full_name": GH_REPO},
        },
        "requested_reviewers": [],
    }


def _make_plane_client() -> MagicMock:
    client = MagicMock()
    client.list_states = AsyncMock(return_value=[])
    client.update_card = AsyncMock()
    return client


def _make_discord_bot() -> MagicMock:
    bot = MagicMock()
    bot.post_review_message = AsyncMock(return_value=DISCORD_MSG_ID)
    bot.create_thread = AsyncMock(return_value=DISCORD_THREAD_ID)
    bot.post_thread_message = AsyncMock()
    bot.archive_thread = AsyncMock()
    return bot


def _make_github_client(pr: dict[str, Any] | None = None) -> MagicMock:
    client = MagicMock()
    client.get_branch_protection = AsyncMock(return_value={})
    client.list_check_runs = AsyncMock(return_value=[])
    if pr is not None:
        client.get_pr = AsyncMock(return_value=pr)
    return client


def _check_suite_payload(gh_repo: str = GH_REPO, pr_number: int = PR_NUMBER) -> dict[str, Any]:
    return {
        "action": "completed",
        "check_suite": {"pull_requests": [{"number": pr_number}]},
        "repository": {"full_name": gh_repo},
    }


# ---------------------------------------------------------------------------
# Thread creation — triggered by check_suite.completed, not PR open
# ---------------------------------------------------------------------------


def test_thread_created_after_notification() -> None:
    pr = _make_pr(draft=False)
    payload = _check_suite_payload()
    # results: [no state, no link (check_and_notify), no link (stage trigger)]
    session = FakeSession(results=[None, None, None])
    discord_bot = _make_discord_bot()
    github_client = _make_github_client(pr=pr)

    _run(
        handle_check_suite_completed(
            payload,
            session=session,  # type: ignore[arg-type]
            github_client=github_client,
            discord_bot=discord_bot,
            discord_channel_id=DISCORD_CHANNEL_ID,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
            now_fn=lambda: FIXED_TIME,
        )
    )

    discord_bot.create_thread.assert_called_once_with(
        DISCORD_MSG_ID, DISCORD_CHANNEL_ID, f"PR #{PR_NUMBER} - Add feature"
    )
    state = session.added[0]
    assert isinstance(state, PrNotificationState)
    assert state.discord_thread_id == DISCORD_THREAD_ID


def test_thread_name_truncates_title_at_80_chars() -> None:
    long_title = "A" * 100
    pr = _make_pr(draft=False, title=long_title)
    payload = _check_suite_payload()
    session = FakeSession(results=[None, None, None])
    discord_bot = _make_discord_bot()
    github_client = _make_github_client(pr=pr)

    _run(
        handle_check_suite_completed(
            payload,
            session=session,  # type: ignore[arg-type]
            github_client=github_client,
            discord_bot=discord_bot,
            discord_channel_id=DISCORD_CHANNEL_ID,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
            now_fn=lambda: FIXED_TIME,
        )
    )

    call_args = discord_bot.create_thread.call_args
    thread_name: str = call_args.args[2]
    assert thread_name == f"PR #{PR_NUMBER} - {'A' * 80}"


def test_no_thread_when_not_ready() -> None:
    pr = _make_pr(draft=True)
    payload = _check_suite_payload()
    session = FakeSession(results=[None])
    discord_bot = _make_discord_bot()
    github_client = _make_github_client(pr=pr)

    _run(
        handle_check_suite_completed(
            payload,
            session=session,  # type: ignore[arg-type]
            github_client=github_client,
            discord_bot=discord_bot,
            discord_channel_id=DISCORD_CHANNEL_ID,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
        )
    )

    discord_bot.create_thread.assert_not_called()


# ---------------------------------------------------------------------------
# handle_pr_review_submitted
# ---------------------------------------------------------------------------


def _review_payload(
    *,
    reviewer: str = "alice",
    state: str = "APPROVED",
    body: str = "Looks good",
) -> dict[str, Any]:
    return {
        "action": "submitted",
        "review": {
            "state": state,
            "body": body,
            "user": {"login": reviewer},
        },
        "pull_request": {"node_id": PR_NODE_ID, "number": PR_NUMBER},
        "repository": {"full_name": GH_REPO},
    }


def test_review_submitted_posts_to_thread() -> None:
    state = _make_pr_state(discord_thread_id=DISCORD_THREAD_ID)
    payload = _review_payload(reviewer="alice", state="APPROVED", body="Looks good")
    session = FakeSession(results=[state])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_review_submitted(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
        )
    )

    discord_bot.post_thread_message.assert_called_once()
    content: str = discord_bot.post_thread_message.call_args.args[1]
    assert "APPROVED" in content
    assert "@alice" in content
    assert "Looks good" in content


def test_review_submitted_thread_id_correct() -> None:
    state = _make_pr_state(discord_thread_id=DISCORD_THREAD_ID)
    payload = _review_payload()
    session = FakeSession(results=[state])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_review_submitted(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
        )
    )

    call_thread_id: str = discord_bot.post_thread_message.call_args.args[0]
    assert call_thread_id == DISCORD_THREAD_ID


def test_review_submitted_body_truncated_to_200() -> None:
    long_body = "B" * 300
    state = _make_pr_state(discord_thread_id=DISCORD_THREAD_ID)
    payload = _review_payload(body=long_body)
    session = FakeSession(results=[state])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_review_submitted(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
        )
    )

    content: str = discord_bot.post_thread_message.call_args.args[1]
    assert "B" * 200 in content
    assert "B" * 201 not in content


def test_review_submitted_no_thread_noop() -> None:
    state = _make_pr_state(discord_thread_id=None)
    payload = _review_payload()
    session = FakeSession(results=[state])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_review_submitted(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
        )
    )

    discord_bot.post_thread_message.assert_not_called()


def test_review_submitted_no_state_noop() -> None:
    payload = _review_payload()
    session = FakeSession(results=[None])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_review_submitted(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
        )
    )

    discord_bot.post_thread_message.assert_not_called()


def test_review_submitted_missing_node_id_noop() -> None:
    payload: dict[str, Any] = {
        "action": "submitted",
        "review": {"state": "APPROVED", "body": "", "user": {"login": "alice"}},
        "pull_request": {"node_id": "", "number": PR_NUMBER},
    }
    session = FakeSession()
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_review_submitted(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
        )
    )

    discord_bot.post_thread_message.assert_not_called()


# ---------------------------------------------------------------------------
# handle_pr_closed_discord
# ---------------------------------------------------------------------------


def test_pr_closed_merged_archives_thread() -> None:
    state = _make_pr_state(discord_thread_id=DISCORD_THREAD_ID)
    pr = _make_pr(merged=True)
    payload = {"action": "closed", "pull_request": pr, "repository": {"full_name": GH_REPO}}
    session = FakeSession(results=[state])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_closed_discord(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
        )
    )

    discord_bot.post_thread_message.assert_called_once()
    content: str = discord_bot.post_thread_message.call_args.args[1]
    assert "merged" in content
    assert str(PR_NUMBER) in content
    discord_bot.archive_thread.assert_called_once_with(DISCORD_THREAD_ID)


def test_pr_closed_not_merged_archives_thread() -> None:
    state = _make_pr_state(discord_thread_id=DISCORD_THREAD_ID)
    pr = _make_pr(merged=False)
    payload = {"action": "closed", "pull_request": pr, "repository": {"full_name": GH_REPO}}
    session = FakeSession(results=[state])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_closed_discord(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
        )
    )

    content: str = discord_bot.post_thread_message.call_args.args[1]
    assert "closed" in content
    discord_bot.archive_thread.assert_called_once_with(DISCORD_THREAD_ID)


def test_pr_closed_no_thread_noop() -> None:
    state = _make_pr_state(discord_thread_id=None)
    pr = _make_pr(merged=True)
    payload = {"action": "closed", "pull_request": pr, "repository": {"full_name": GH_REPO}}
    session = FakeSession(results=[state])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_closed_discord(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
        )
    )

    discord_bot.post_thread_message.assert_not_called()
    discord_bot.archive_thread.assert_not_called()


def test_pr_closed_no_state_noop() -> None:
    pr = _make_pr(merged=True)
    payload = {"action": "closed", "pull_request": pr, "repository": {"full_name": GH_REPO}}
    session = FakeSession(results=[None])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_closed_discord(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
        )
    )

    discord_bot.post_thread_message.assert_not_called()
    discord_bot.archive_thread.assert_not_called()


def test_pr_closed_includes_html_url() -> None:
    state = _make_pr_state(discord_thread_id=DISCORD_THREAD_ID)
    pr = _make_pr(merged=True, html_url="https://github.com/owner/repo/pull/42")
    payload = {"action": "closed", "pull_request": pr, "repository": {"full_name": GH_REPO}}
    session = FakeSession(results=[state])
    discord_bot = _make_discord_bot()

    _run(
        handle_pr_closed_discord(
            payload,
            session=session,  # type: ignore[arg-type]
            discord_bot=discord_bot,
        )
    )

    content: str = discord_bot.post_thread_message.call_args.args[1]
    assert "https://github.com/owner/repo/pull/42" in content

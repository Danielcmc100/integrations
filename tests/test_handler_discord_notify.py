from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord

from integration.handlers.github import handle_check_suite_completed, handle_pr_notification
from integration.models import CardIssueLink, PrNotificationState, SyncSource


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


FIXED_TIME = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
GH_REPO = "owner/repo"
PR_NUMBER = 10
PR_NODE_ID = "PR_kwDOABCDEF"
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
    ready_notified_at: datetime | None = None,
    discord_message_id: str | None = None,
    last_ready_cycle_id: str = "cycle-001",
) -> PrNotificationState:
    state = PrNotificationState()
    state.pr_node_id = PR_NODE_ID
    state.gh_repo = GH_REPO
    state.pr_number = PR_NUMBER
    state.last_ready_cycle_id = last_ready_cycle_id
    state.ready_notified_at = ready_notified_at
    state.discord_message_id = discord_message_id
    state.discord_thread_id = None
    return state


def _make_pr(
    *,
    draft: bool = False,
    title: str = "My feature",
    html_url: str = "https://github.com/owner/repo/pull/10",
    sha: str = "abc123",
    additions: int = 10,
    deletions: int = 5,
    requested_reviewers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "node_id": PR_NODE_ID,
        "number": PR_NUMBER,
        "title": title,
        "html_url": html_url,
        "draft": draft,
        "additions": additions,
        "deletions": deletions,
        "user": {"login": "dev-author"},
        "head": {
            "ref": f"{PR_NUMBER}-feature",
            "sha": sha,
            "repo": {"full_name": GH_REPO},
        },
        "requested_reviewers": requested_reviewers or [],
    }


def _make_payload(pr: dict[str, Any], action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": pr,
        "repository": {"full_name": GH_REPO},
    }


def _make_discord_bot(
    message_id: str = DISCORD_MSG_ID,
    thread_id: str = DISCORD_THREAD_ID,
) -> MagicMock:
    bot = MagicMock()
    bot.post_review_message = AsyncMock(return_value=message_id)
    bot.create_thread = AsyncMock(return_value=thread_id)
    bot.post_thread_message = AsyncMock()
    bot.archive_thread = AsyncMock()
    return bot


def _make_plane_client() -> MagicMock:
    client = MagicMock()
    client.list_states = AsyncMock(return_value=[])
    client.update_card = AsyncMock()
    return client


def _make_github_client(
    *,
    required_contexts: list[str] | None = None,
    check_runs: list[dict[str, Any]] | None = None,
    pr: dict[str, Any] | None = None,
) -> MagicMock:
    client = MagicMock()
    protection: dict[str, Any] = {}
    if required_contexts is not None:
        protection = {"required_status_checks": {"contexts": required_contexts}}
    client.get_branch_protection = AsyncMock(return_value=protection)
    client.list_check_runs = AsyncMock(return_value=check_runs or [])
    if pr is not None:
        client.get_pr = AsyncMock(return_value=pr)
    return client


def _make_link() -> CardIssueLink:
    link = CardIssueLink()
    link.plane_card_id = "card-001"
    link.plane_project_id = "proj-001"
    link.gh_repo = GH_REPO
    link.gh_issue_number = PR_NUMBER
    link.gh_issue_node_id = "node-10"
    link.last_synced_at = FIXED_TIME
    link.sync_source_last = SyncSource.github
    return link


# ---------------------------------------------------------------------------
# handle_pr_notification — state management only, no Discord notification
# ---------------------------------------------------------------------------


def test_pr_opened_creates_state() -> None:
    pr = _make_pr(draft=False)
    payload = _make_payload(pr, "opened")
    session = FakeSession(results=[None])

    _run(handle_pr_notification(payload, session=session))  # type: ignore[arg-type]

    assert len(session.added) == 1
    state = session.added[0]
    assert isinstance(state, PrNotificationState)
    assert state.pr_node_id == PR_NODE_ID
    assert state.ready_notified_at is None
    assert state.discord_message_id is None
    assert session.commit_count == 1


def test_pr_opened_draft_creates_state() -> None:
    pr = _make_pr(draft=True)
    payload = _make_payload(pr, "opened")
    session = FakeSession(results=[None])

    _run(handle_pr_notification(payload, session=session))  # type: ignore[arg-type]

    assert len(session.added) == 1
    assert session.commit_count == 1


def test_pr_reopened_new_cycle_resets_state() -> None:
    existing_state = _make_pr_state(
        ready_notified_at=FIXED_TIME, discord_message_id="old-msg"
    )
    pr = _make_pr(draft=False)
    payload = _make_payload(pr, "reopened")
    session = FakeSession(results=[existing_state])

    _run(
        handle_pr_notification(
            payload,
            session=session,  # type: ignore[arg-type]
            new_cycle=True,
        )
    )

    assert len(session.added) == 0  # existing state mutated, not a new row
    assert existing_state.ready_notified_at is None
    assert existing_state.discord_message_id is None
    assert existing_state.last_ready_cycle_id != "cycle-001"
    assert session.commit_count == 1


def test_pr_missing_fields_returns_early() -> None:
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"node_id": "", "number": 0},
        "repository": {"full_name": GH_REPO},
    }
    session = FakeSession()

    _run(handle_pr_notification(payload, session=session))  # type: ignore[arg-type]

    assert len(session.added) == 0
    assert session.commit_count == 0


# ---------------------------------------------------------------------------
# Discord embed — via handle_check_suite_completed
# ---------------------------------------------------------------------------


def test_pr_embed_has_correct_fields() -> None:
    pr = _make_pr(
        draft=False,
        title="Add awesome feature",
        additions=120,
        deletions=30,
        requested_reviewers=[{"login": "reviewer1"}, {"login": "reviewer2"}],
    )
    payload = _check_suite_payload()
    # results: [no existing state, no link (in _check_and_notify), no link (stage trigger)]
    session = FakeSession(results=[None, None, None])
    github_client = _make_github_client(required_contexts=[], pr=pr)
    discord_bot = _make_discord_bot()

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

    call_args = discord_bot.post_review_message.call_args
    embed: discord.Embed = call_args.args[1]
    view: discord.ui.View = call_args.kwargs["view"]

    assert embed.title is not None
    assert f"PR #{PR_NUMBER}" in embed.title
    assert "Add awesome feature" in embed.title
    field_names = {f.name for f in embed.fields}
    assert "Repo" in field_names
    assert "Branch" in field_names
    assert "Changes" in field_names
    assert "Reviewers" in field_names
    assert any(isinstance(item, discord.ui.Button) for item in view.children)


def test_pr_embed_includes_plane_card_url_when_link_found() -> None:
    pr = _make_pr(draft=False)
    link = _make_link()
    payload = _check_suite_payload()
    # results: [no existing state, CardIssueLink found (in _check_and_notify), link (stage trigger)]
    session = FakeSession(results=[None, link, link])
    github_client = _make_github_client(required_contexts=[], pr=pr)
    discord_bot = _make_discord_bot()

    _run(
        handle_check_suite_completed(
            payload,
            session=session,  # type: ignore[arg-type]
            github_client=github_client,
            discord_bot=discord_bot,
            discord_channel_id=DISCORD_CHANNEL_ID,
            plane_client=_make_plane_client(),  # type: ignore[arg-type]
            now_fn=lambda: FIXED_TIME,
            plane_app_url="https://app.plane.so",
            plane_workspace="test-ws",
        )
    )

    call_args = discord_bot.post_review_message.call_args
    embed: discord.Embed = call_args.args[1]
    field_names = {f.name for f in embed.fields}
    assert "Plane Card" in field_names


def test_pr_notification_uses_correct_channel_id() -> None:
    pr = _make_pr(draft=False)
    payload = _check_suite_payload()
    session = FakeSession(results=[None, None, None])
    github_client = _make_github_client(required_contexts=[], pr=pr)
    discord_bot = _make_discord_bot()

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

    call_args = discord_bot.post_review_message.call_args
    assert call_args.args[0] == DISCORD_CHANNEL_ID


# ---------------------------------------------------------------------------
# handle_check_suite_completed
# ---------------------------------------------------------------------------


def _check_suite_payload(
    pr_numbers: list[int] | None = None,
    gh_repo: str = GH_REPO,
) -> dict[str, Any]:
    prs = [{"number": n} for n in (pr_numbers or [PR_NUMBER])]
    return {
        "action": "completed",
        "check_suite": {"pull_requests": prs},
        "repository": {"full_name": gh_repo},
    }


def test_check_suite_completed_notifies_ready_pr() -> None:
    pr = _make_pr(draft=False)
    payload = _check_suite_payload()
    # results: [no existing state, no link]
    session = FakeSession(results=[None, None])
    github_client = _make_github_client(required_contexts=[], pr=pr)
    discord_bot = _make_discord_bot()

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

    discord_bot.post_review_message.assert_called_once()
    github_client.get_pr.assert_called_once_with("owner", "repo", PR_NUMBER)


def test_check_suite_completed_already_notified_no_repost() -> None:
    pr = _make_pr(draft=False)
    existing_state = _make_pr_state(ready_notified_at=FIXED_TIME)
    payload = _check_suite_payload()
    # results: [existing state]
    session = FakeSession(results=[existing_state])
    github_client = _make_github_client(required_contexts=[], pr=pr)
    discord_bot = _make_discord_bot()

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

    discord_bot.post_review_message.assert_not_called()


def test_check_suite_completed_draft_pr_no_notify() -> None:
    pr = _make_pr(draft=True)
    payload = _check_suite_payload()
    session = FakeSession(results=[None])
    github_client = _make_github_client(required_contexts=[], pr=pr)
    discord_bot = _make_discord_bot()

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

    discord_bot.post_review_message.assert_not_called()


def test_check_suite_completed_missing_repo_noop() -> None:
    payload = {
        "action": "completed",
        "check_suite": {"pull_requests": [{"number": PR_NUMBER}]},
        "repository": {},
    }
    session = FakeSession()
    github_client = _make_github_client(required_contexts=[])
    discord_bot = _make_discord_bot()

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

    discord_bot.post_review_message.assert_not_called()


def test_check_suite_completed_get_pr_fails_skips() -> None:
    payload = _check_suite_payload()
    session = FakeSession()
    github_client = MagicMock()
    github_client.get_pr = AsyncMock(side_effect=RuntimeError("API error"))
    discord_bot = _make_discord_bot()

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

    discord_bot.post_review_message.assert_not_called()

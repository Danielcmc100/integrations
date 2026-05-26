from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.models import PrNotificationState, UserMap
from integration.reminders import send_review_reminders


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


FIXED_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
GH_REPO = "owner/repo"
PR_NUMBER = 42
PR_NODE_ID = "PR_kwDOXYZ123"
DISCORD_THREAD_ID = "444555666"

# ready_notified_at old enough to trigger reminder
OLD_NOTIFIED_AT = FIXED_NOW - timedelta(hours=25)
# last_reminder_at old enough (>24h)
OLD_REMINDED_AT = FIXED_NOW - timedelta(hours=25)
# last_reminder_at too recent (<24h)
RECENT_REMINDED_AT = FIXED_NOW - timedelta(hours=1)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)


class FakeResult:
    def __init__(self, obj: Any = None, rows: list[Any] | None = None) -> None:
        self._obj = obj
        self._rows = rows if rows is not None else []

    def scalar_one_or_none(self) -> Any:
        return self._obj

    def scalars(self) -> FakeScalars:
        return FakeScalars(self._rows)


class FakeSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.commit_count: int = 0
        self._rows: list[Any] = rows or []

    async def commit(self) -> None:
        self.commit_count += 1

    async def execute(self, stmt: Any) -> FakeResult:
        return FakeResult(rows=self._rows)


@contextlib.asynccontextmanager
async def _session_ctx(session: FakeSession) -> AsyncIterator[FakeSession]:
    yield session


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def __call__(self) -> Any:
        return _session_ctx(self._session)


def _make_pr_state(
    *,
    ready_notified_at: datetime | None = OLD_NOTIFIED_AT,
    last_reminder_at: datetime | None = None,
    discord_thread_id: str | None = DISCORD_THREAD_ID,
) -> PrNotificationState:
    state = PrNotificationState()
    state.pr_node_id = PR_NODE_ID
    state.gh_repo = GH_REPO
    state.pr_number = PR_NUMBER
    state.last_ready_cycle_id = "cycle-001"
    state.ready_notified_at = ready_notified_at
    state.last_reminder_at = last_reminder_at
    state.discord_thread_id = discord_thread_id
    state.discord_message_id = "999"
    return state


def _make_open_pr(
    *,
    state: str = "open",
    draft: bool = False,
    reviewers: list[str] | None = None,
) -> dict[str, Any]:
    requested: list[dict[str, Any]] = [{"login": r} for r in (reviewers or [])]
    return {
        "node_id": PR_NODE_ID,
        "number": PR_NUMBER,
        "title": "Add feature",
        "html_url": "https://github.com/owner/repo/pull/42",
        "state": state,
        "draft": draft,
        "merged": state == "closed" and False,
        "additions": 10,
        "deletions": 5,
        "user": {"login": "dev"},
        "requested_reviewers": requested,
        "head": {
            "ref": "42-feature",
            "sha": "abc123",
            "repo": {"full_name": GH_REPO},
        },
    }


def _make_github_client(
    *,
    pr: dict[str, Any] | None = None,
    reviews: list[dict[str, Any]] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.get_pr = AsyncMock(return_value=pr or _make_open_pr())
    client.list_reviews = AsyncMock(return_value=reviews or [])
    client.get_branch_protection = AsyncMock(return_value={})
    client.list_check_runs = AsyncMock(return_value=[])
    return client


def _make_config_service(discord_user_id: str | None = None) -> MagicMock:
    svc = MagicMock()
    if discord_user_id is not None:
        um = UserMap()
        um.plane_user_id = "plane-user-1"
        um.gh_login = "alice"
        um.discord_user_id = discord_user_id
        svc.get_user_map_by_gh = AsyncMock(return_value=um)
    else:
        svc.get_user_map_by_gh = AsyncMock(return_value=None)
    return svc


def _make_discord_bot() -> MagicMock:
    bot = MagicMock()
    bot.post_thread_message = AsyncMock()
    return bot


def _make_ctx(
    session: FakeSession,
    github_client: MagicMock,
    config_service: MagicMock,
    discord_bot: MagicMock | None,
) -> dict[str, Any]:
    return {
        "session_factory": FakeSessionFactory(session),
        "github_client": github_client,
        "config_service": config_service,
        "discord_bot": discord_bot,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reminder_sent_for_eligible_pr() -> None:
    state = _make_pr_state(ready_notified_at=OLD_NOTIFIED_AT, last_reminder_at=None)
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    ctx = _make_ctx(session, _make_github_client(), _make_config_service(), discord_bot)

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    discord_bot.post_thread_message.assert_called_once()
    args = discord_bot.post_thread_message.call_args.args
    assert args[0] == DISCORD_THREAD_ID
    assert "42" in args[1]
    assert session.commit_count == 1
    assert state.last_reminder_at == FIXED_NOW


def test_no_discord_bot_skips_all() -> None:
    state = _make_pr_state()
    session = FakeSession(rows=[state])
    ctx = _make_ctx(session, _make_github_client(), _make_config_service(), None)

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    assert session.commit_count == 0


def test_pr_closed_skips_reminder() -> None:
    state = _make_pr_state(ready_notified_at=OLD_NOTIFIED_AT, last_reminder_at=None)
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    closed_pr = _make_open_pr(state="closed")
    ctx = _make_ctx(
        session, _make_github_client(pr=closed_pr), _make_config_service(), discord_bot
    )

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    discord_bot.post_thread_message.assert_not_called()
    assert session.commit_count == 0


def test_pr_with_reviews_skips_reminder() -> None:
    state = _make_pr_state(ready_notified_at=OLD_NOTIFIED_AT, last_reminder_at=None)
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    existing_review: dict[str, Any] = {"id": 1, "state": "APPROVED", "user": {"login": "alice"}}
    ctx = _make_ctx(
        session,
        _make_github_client(reviews=[existing_review]),
        _make_config_service(),
        discord_bot,
    )

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    discord_bot.post_thread_message.assert_not_called()
    assert session.commit_count == 0


def test_pr_no_longer_ready_skips_reminder() -> None:
    state = _make_pr_state(ready_notified_at=OLD_NOTIFIED_AT, last_reminder_at=None)
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    draft_pr = _make_open_pr(draft=True)
    ctx = _make_ctx(
        session, _make_github_client(pr=draft_pr), _make_config_service(), discord_bot
    )

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    discord_bot.post_thread_message.assert_not_called()
    assert session.commit_count == 0


def test_old_reminder_sends_new_reminder() -> None:
    state = _make_pr_state(
        ready_notified_at=OLD_NOTIFIED_AT, last_reminder_at=OLD_REMINDED_AT
    )
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    ctx = _make_ctx(session, _make_github_client(), _make_config_service(), discord_bot)

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    discord_bot.post_thread_message.assert_called_once()
    assert state.last_reminder_at == FIXED_NOW


def test_reviewer_discord_mention_used_when_available() -> None:
    state = _make_pr_state(ready_notified_at=OLD_NOTIFIED_AT)
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    pr = _make_open_pr(reviewers=["alice"])
    ctx = _make_ctx(
        session,
        _make_github_client(pr=pr),
        _make_config_service(discord_user_id="discord-111"),
        discord_bot,
    )

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    message: str = discord_bot.post_thread_message.call_args.args[1]
    assert "<@discord-111>" in message


def test_reviewer_gh_login_fallback_when_no_discord_id() -> None:
    state = _make_pr_state(ready_notified_at=OLD_NOTIFIED_AT)
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    pr = _make_open_pr(reviewers=["bob"])
    ctx = _make_ctx(
        session,
        _make_github_client(pr=pr),
        _make_config_service(discord_user_id=None),
        discord_bot,
    )

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    message: str = discord_bot.post_thread_message.call_args.args[1]
    assert "@bob" in message


def test_no_rows_no_action() -> None:
    session = FakeSession(rows=[])
    discord_bot = _make_discord_bot()
    ctx = _make_ctx(session, _make_github_client(), _make_config_service(), discord_bot)

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    discord_bot.post_thread_message.assert_not_called()
    assert session.commit_count == 0


def test_pr_url_included_in_message() -> None:
    state = _make_pr_state(ready_notified_at=OLD_NOTIFIED_AT)
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    pr = _make_open_pr()
    ctx = _make_ctx(session, _make_github_client(pr=pr), _make_config_service(), discord_bot)

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    message: str = discord_bot.post_thread_message.call_args.args[1]
    assert "https://github.com/owner/repo/pull/42" in message


def test_github_client_failure_does_not_crash() -> None:
    state = _make_pr_state(ready_notified_at=OLD_NOTIFIED_AT)
    session = FakeSession(rows=[state])
    discord_bot = _make_discord_bot()
    client = _make_github_client()
    client.get_pr = AsyncMock(side_effect=RuntimeError("network error"))
    ctx = _make_ctx(session, client, _make_config_service(), discord_bot)

    _run(send_review_reminders(ctx, now_fn=lambda: FIXED_NOW))

    discord_bot.post_thread_message.assert_not_called()
    assert session.commit_count == 0

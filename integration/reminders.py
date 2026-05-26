"""Hourly reviewer-reminder cron job."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from integration.clients.github import GitHubClient
from integration.config_service import ConfigService
from integration.discord_bot import DiscordBotProtocol
from integration.models import PrNotificationState
from integration.pr_ready import compute_ready

log = structlog.get_logger()

_REMINDER_HOURS = 24


async def send_review_reminders(
    ctx: dict[str, Any],
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    discord_bot_raw: Any = ctx.get("discord_bot")
    if discord_bot_raw is None:
        log.info("send_review_reminders: discord_bot not configured, skipping")
        return

    discord_bot = cast(DiscordBotProtocol, discord_bot_raw)
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    github_client: GitHubClient = ctx["github_client"]
    config_service: ConfigService = ctx["config_service"]

    now = now_fn()
    threshold = now - timedelta(hours=_REMINDER_HOURS)

    async with session_factory() as session:
        result = await session.execute(
            select(PrNotificationState).where(
                PrNotificationState.ready_notified_at.is_not(None),
                PrNotificationState.ready_notified_at < threshold,
                PrNotificationState.discord_thread_id.is_not(None),
                or_(
                    PrNotificationState.last_reminder_at.is_(None),
                    PrNotificationState.last_reminder_at < threshold,
                ),
            )
        )
        states = list(result.scalars())

        for state in states:
            sent = await _check_and_remind(
                state,
                github_client=github_client,
                config_service=config_service,
                discord_bot=discord_bot,
            )
            if sent:
                state.last_reminder_at = now
                await session.commit()
                log.info(
                    "send_review_reminders: reminder sent",
                    gh_repo=state.gh_repo,
                    pr_number=state.pr_number,
                )


async def _check_and_remind(
    state: PrNotificationState,
    *,
    github_client: GitHubClient,
    config_service: ConfigService,
    discord_bot: DiscordBotProtocol,
) -> bool:
    gh_repo = state.gh_repo
    pr_number = state.pr_number
    thread_id = state.discord_thread_id

    if not gh_repo or not pr_number or not thread_id:
        return False
    if "/" not in gh_repo:
        return False

    owner, repo_name = gh_repo.split("/", 1)

    try:
        pr = await github_client.get_pr(owner, repo_name, pr_number)
    except Exception:
        log.warning(
            "send_review_reminders: failed to fetch PR",
            gh_repo=gh_repo,
            pr_number=pr_number,
        )
        return False

    pr_state = str(pr.get("state") or "")
    if pr_state == "closed":
        log.info(
            "send_review_reminders: PR closed, skip",
            gh_repo=gh_repo,
            pr_number=pr_number,
        )
        return False

    try:
        reviews = await github_client.list_reviews(owner, repo_name, pr_number)
    except Exception:
        log.warning(
            "send_review_reminders: failed to fetch reviews",
            gh_repo=gh_repo,
            pr_number=pr_number,
        )
        return False

    if reviews:
        log.info(
            "send_review_reminders: PR has reviews, skip",
            gh_repo=gh_repo,
            pr_number=pr_number,
        )
        return False

    is_ready = await compute_ready(pr, github_client)
    if not is_ready:
        log.info(
            "send_review_reminders: PR no longer ready, skip",
            gh_repo=gh_repo,
            pr_number=pr_number,
        )
        return False

    reviewers_raw: Any = pr.get("requested_reviewers") or []
    mention_parts: list[str] = []
    if isinstance(reviewers_raw, list):
        for r in cast("list[Any]", reviewers_raw):
            if isinstance(r, dict):
                r_dict = cast("dict[str, Any]", r)
                login = str(r_dict.get("login") or "")
                if login:
                    um = await config_service.get_user_map_by_gh(login)
                    if um is not None and um.discord_user_id:
                        mention_parts.append(f"<@{um.discord_user_id}>")
                    else:
                        mention_parts.append(f"@{login}")

    pr_title = str(pr.get("title") or "")
    pr_url = str(pr.get("html_url") or "")

    message = f'Reminder: PR #{pr_number} "{pr_title}" awaiting review for over 24h.'
    if mention_parts:
        message = " ".join(mention_parts) + " " + message
    if pr_url:
        message += f" {pr_url}"

    await discord_bot.post_thread_message(thread_id, message)
    return True

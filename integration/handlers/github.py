from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import discord
import discord.ui
import structlog
import structlog.contextvars
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from integration.clients.github import GitHubClient
from integration.clients.plane import PlaneClient
from integration.config import settings
from integration.config_service import ConfigService
from integration.discord_bot import DiscordBotProtocol
from integration.handlers._stage import apply_stage_trigger
from integration.handlers._sync import (
    detect_conflict,
    event_wins_conflict,
    extract_gh_coords,
    fetch_link_by_gh,
    fetch_link_by_plane,
    parse_dt,
    should_skip_loop,
    strip_footer,
)
from integration.metrics import sync_actions_total, sync_duration_seconds
from integration.models import CardIssueLink, PrNotificationState, StageTrigger, SyncSource
from integration.pr_ready import compute_ready
from integration.retry import DeadLetteredError, run_with_retry

log = structlog.get_logger()

REFINAMENTO_STATE_NAME = "Refinamento"
DEFAULT_LABEL_NAME = "Feature"
DEFAULT_PRIORITY = "medium"
DONE_STATE_GROUP = "completed"
CLOSED_BY_PR_REASON = "completed_by_pull_request"

_BRANCH_NUM_RE = re.compile(r"^(?P<num>\d+)-")
_CLOSES_RE = re.compile(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def handle_issue_opened(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    github_client: GitHubClient,
    config_service: ConfigService,
    now_fn: Callable[[], datetime] = _utcnow,
    plane_workspace: str | None = None,
    plane_app_url: str | None = None,
) -> None:
    ws = plane_workspace if plane_workspace is not None else settings.plane_workspace
    app_url = plane_app_url if plane_app_url is not None else settings.plane_app_url

    issue_raw: Any = payload.get("issue")
    issue: dict[str, Any] = (
        cast("dict[str, Any]", issue_raw) if isinstance(issue_raw, dict) else {}
    )
    repo_raw: Any = payload.get("repository")
    repo_data: dict[str, Any] = (
        cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    )

    gh_repo: str = str(repo_data.get("full_name") or "")
    if not gh_repo:
        log.warning("issues.opened skipped: missing repository.full_name")
        return

    repo_map = await config_service.get_repo_module_by_repo(gh_repo)
    if repo_map is None:
        log.warning("issues.opened skipped: no repo mapping", gh_repo=gh_repo)
        return

    plane_project_id = repo_map.plane_project_id

    issue_number: int = int(issue.get("number") or 0)
    issue_title: str = str(issue.get("title") or "")
    issue_body: str = str(issue.get("body") or "")
    issue_node_id: str = str(issue.get("node_id") or "")
    gh_labels_raw: Any = issue.get("labels")
    gh_labels: list[str] = []
    if isinstance(gh_labels_raw, list):
        for lbl in cast("list[Any]", gh_labels_raw):
            if isinstance(lbl, dict):
                lbl_dict = cast("dict[str, Any]", lbl)
                name = lbl_dict.get("name")
                if isinstance(name, str):
                    gh_labels.append(name)

    # Resolve Plane state "Refinamento"
    states = await plane_client.list_states(plane_project_id)
    refinamento_state_id: str | None = None
    for s in states:
        if str(s.get("name") or "") == REFINAMENTO_STATE_NAME:
            refinamento_state_id = str(s["id"])
            break
    log.debug(
        "issues.opened: state resolved",
        state_name=REFINAMENTO_STATE_NAME,
        state_id=refinamento_state_id,
    )

    # Resolve Plane label via label_map or default "Feature"
    plane_label_id: str | None = None
    for gh_label in gh_labels:
        lm = await config_service.get_label_map_by_gh(gh_repo, gh_label)
        if lm is not None:
            plane_label_id = lm.plane_label_id
            log.debug("issues.opened: label resolved via mapping", gh_label=gh_label, plane_label_id=plane_label_id)
            break

    if plane_label_id is None:
        labels = await plane_client.list_labels(plane_project_id)
        for lbl in labels:
            if str(lbl.get("name") or "") == DEFAULT_LABEL_NAME:
                plane_label_id = str(lbl["id"])
                log.debug("issues.opened: label resolved via default", default_label=DEFAULT_LABEL_NAME)
                break

    if plane_label_id is None:
        log.warning("issues.opened: no label resolved", gh_repo=gh_repo, gh_labels=gh_labels)

    card_payload: dict[str, Any] = {
        "name": issue_title,
        "priority": DEFAULT_PRIORITY,
    }
    if refinamento_state_id is not None:
        card_payload["state"] = refinamento_state_id
    if plane_label_id is not None:
        card_payload["label_ids"] = [plane_label_id]

    log.info("issues.opened: creating Plane card", gh_repo=gh_repo, issue_number=issue_number, project_id=plane_project_id)
    card = await plane_client.create_card(plane_project_id, card_payload)
    card_id: str = str(card["id"])
    log.debug("issues.opened: plane card created", card_id=card_id)

    # Place in active cycle if one exists
    cycles = await plane_client.list_cycles(plane_project_id)
    placed_in_cycle = False
    for cycle in cycles:
        if str(cycle.get("status") or "") == "CURRENT":
            await plane_client.add_issue_to_cycle(plane_project_id, str(cycle["id"]), card_id)
            log.info("issues.opened: card added to active cycle", card_id=card_id, cycle_id=cycle["id"])
            placed_in_cycle = True
            break
    if not placed_in_cycle:
        log.debug("issues.opened: no active cycle found, card not added to cycle", card_id=card_id)

    plane_card_url = (
        f"{app_url.rstrip('/')}/{ws}/projects/{plane_project_id}/issues/{card_id}/"
    )

    owner, repo = gh_repo.split("/", 1)
    new_body = f"{issue_body}\n\n---\nPlane: {plane_card_url}"
    await github_client.update_issue(owner, repo, issue_number, {"body": new_body})

    link = CardIssueLink(
        plane_card_id=card_id,
        plane_project_id=plane_project_id,
        gh_repo=gh_repo,
        gh_issue_number=issue_number,
        gh_issue_node_id=issue_node_id,
        last_synced_at=now_fn(),
        sync_source_last=SyncSource.github,
    )
    session.add(link)
    await session.commit()
    log.info(
        "issues.opened synced to plane",
        gh_repo=gh_repo,
        gh_issue_number=issue_number,
        card_id=card_id,
    )


async def handle_issue_edited(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    now_fn: Callable[[], datetime] = _utcnow,
) -> None:
    issue, gh_repo, issue_number = extract_gh_coords(payload)
    if not gh_repo or not issue_number:
        return

    changes_raw: Any = payload.get("changes")
    changes: dict[str, Any] = (
        cast("dict[str, Any]", changes_raw) if isinstance(changes_raw, dict) else {}
    )
    title_changed = "title" in changes
    body_changed = "body" in changes
    if not title_changed and not body_changed:
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.warning("issues.edited: no link found", gh_repo=gh_repo, issue_number=issue_number)
        return

    event_updated_at = parse_dt(str(issue.get("updated_at") or "")) or now_fn()
    if should_skip_loop(link, event_updated_at, SyncSource.github):
        log.info("issues.edited: loop prevention skip", issue_number=issue_number)
        return

    update_payload: dict[str, Any] = {}
    if title_changed:
        update_payload["name"] = str(issue.get("title") or "")
    if body_changed:
        gh_body = str(issue.get("body") or "")
        clean_body = strip_footer(gh_body)
        gh_issue_url = f"https://github.com/{gh_repo}/issues/{issue_number}"
        update_payload["description_html"] = f"{clean_body}\n\n---\nGitHub: {gh_issue_url}"

    await plane_client.update_card(link.plane_project_id, link.plane_card_id, update_payload)
    link.last_synced_at = now_fn()
    link.sync_source_last = SyncSource.github
    await session.commit()
    log.info("issues.edited synced to plane", gh_repo=gh_repo, issue_number=issue_number)


async def handle_issue_labels_changed(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    config_service: ConfigService,
    now_fn: Callable[[], datetime] = _utcnow,
) -> None:
    issue, gh_repo, issue_number = extract_gh_coords(payload)
    if not gh_repo or not issue_number:
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.warning("issues.labeled: no link found", gh_repo=gh_repo, issue_number=issue_number)
        return

    event_updated_at = parse_dt(str(issue.get("updated_at") or "")) or now_fn()
    if should_skip_loop(link, event_updated_at, SyncSource.github):
        log.info("issues.labeled: loop prevention skip", issue_number=issue_number)
        return

    gh_labels_raw: Any = issue.get("labels")
    plane_label_ids: list[str] = []
    if isinstance(gh_labels_raw, list):
        for lbl in cast("list[Any]", gh_labels_raw):
            if isinstance(lbl, dict):
                lbl_dict = cast("dict[str, Any]", lbl)
                name = str(lbl_dict.get("name") or "")
                if not name:
                    continue
                lm = await config_service.get_label_map_by_gh(gh_repo, name)
                if lm is None:
                    log.info("issues.labeled: unknown GH label skipped", label=name)
                else:
                    plane_label_ids.append(lm.plane_label_id)

    await plane_client.update_card(
        link.plane_project_id,
        link.plane_card_id,
        {"label_ids": plane_label_ids},
    )
    link.last_synced_at = now_fn()
    link.sync_source_last = SyncSource.github
    await session.commit()
    log.info(
        "issues.labeled synced to plane",
        gh_repo=gh_repo,
        issue_number=issue_number,
        label_count=len(plane_label_ids),
    )


async def handle_issue_assignees_changed(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    config_service: ConfigService,
    now_fn: Callable[[], datetime] = _utcnow,
) -> None:
    issue, gh_repo, issue_number = extract_gh_coords(payload)
    if not gh_repo or not issue_number:
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.warning("issues.assigned: no link found", gh_repo=gh_repo, issue_number=issue_number)
        return

    event_updated_at = parse_dt(str(issue.get("updated_at") or "")) or now_fn()
    if should_skip_loop(link, event_updated_at, SyncSource.github):
        log.info("issues.assigned: loop prevention skip", issue_number=issue_number)
        return

    assignees_raw: Any = issue.get("assignees")
    plane_assignees: list[str] = []
    if isinstance(assignees_raw, list):
        for a in cast("list[Any]", assignees_raw):
            if isinstance(a, dict):
                a_dict = cast("dict[str, Any]", a)
                login = str(a_dict.get("login") or "")
                if not login:
                    continue
                um = await config_service.get_user_map_by_gh(login)
                if um is None:
                    log.info("issues.assigned: unknown GH user skipped", login=login)
                else:
                    plane_assignees.append(um.plane_user_id)

    await plane_client.update_card(
        link.plane_project_id,
        link.plane_card_id,
        {"assignees": plane_assignees},
    )
    link.last_synced_at = now_fn()
    link.sync_source_last = SyncSource.github
    await session.commit()
    log.info(
        "issues.assigned synced to plane",
        gh_repo=gh_repo,
        issue_number=issue_number,
        assignee_count=len(plane_assignees),
    )


async def handle_issue_closed(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    now_fn: Callable[[], datetime] = _utcnow,
) -> None:
    issue, gh_repo, issue_number = extract_gh_coords(payload)
    if not gh_repo or not issue_number:
        return

    state_reason = str(issue.get("state_reason") or "")
    if state_reason == CLOSED_BY_PR_REASON:
        log.info("issues.closed: skipped, closed by PR", issue_number=issue_number)
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.warning("issues.closed: no link found", gh_repo=gh_repo, issue_number=issue_number)
        return

    event_updated_at = parse_dt(str(issue.get("updated_at") or "")) or now_fn()
    if should_skip_loop(link, event_updated_at, SyncSource.github):
        log.info("issues.closed: loop prevention skip", issue_number=issue_number)
        return

    if detect_conflict(link, event_updated_at, SyncSource.github):
        log.warning(
            "issues.closed: state conflict detected",
            gh_repo=gh_repo,
            issue_number=issue_number,
            last_synced_at=link.last_synced_at,
            event_updated_at=event_updated_at,
        )
        if not event_wins_conflict(link, event_updated_at):
            log.info(
                "issues.closed: conflict resolved, plane side newer, skip",
                issue_number=issue_number,
            )
            return

    states = await plane_client.list_states(link.plane_project_id)
    done_state_id: str | None = None
    for s in states:
        if str(s.get("group") or "") == DONE_STATE_GROUP:
            done_state_id = str(s["id"])
            break

    if done_state_id is None:
        log.warning(
            "issues.closed: no completed state in Plane",
            project_id=link.plane_project_id,
        )
        return

    await plane_client.update_card(
        link.plane_project_id,
        link.plane_card_id,
        {"state": done_state_id},
    )
    link.last_synced_at = now_fn()
    link.sync_source_last = SyncSource.github
    await session.commit()
    log.info(
        "issues.closed -> plane card moved to Done",
        gh_repo=gh_repo,
        issue_number=issue_number,
    )


async def handle_issue_comment_created(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    github_bot_login: str | None = None,
) -> None:
    comment_raw: Any = payload.get("comment")
    comment: dict[str, Any] = (
        cast("dict[str, Any]", comment_raw) if isinstance(comment_raw, dict) else {}
    )

    user_raw: Any = comment.get("user")
    user: dict[str, Any] = (
        cast("dict[str, Any]", user_raw) if isinstance(user_raw, dict) else {}
    )
    login: str = str(user.get("login") or "")

    bot_login = github_bot_login if github_bot_login is not None else settings.github_bot_login
    if bot_login and login == bot_login:
        log.info("issue_comment.created: skip bot comment", login=login)
        return

    issue_raw: Any = payload.get("issue")
    issue: dict[str, Any] = (
        cast("dict[str, Any]", issue_raw) if isinstance(issue_raw, dict) else {}
    )
    repo_raw: Any = payload.get("repository")
    repo: dict[str, Any] = (
        cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    )
    gh_repo: str = str(repo.get("full_name") or "")
    issue_number: int = int(issue.get("number") or 0)

    if not gh_repo or not issue_number:
        log.warning("issue_comment.created: missing repo or issue number")
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.warning(
            "issue_comment.created: no link found",
            gh_repo=gh_repo,
            issue_number=issue_number,
        )
        return

    comment_body: str = str(comment.get("body") or "")
    prefix = f"[GitHub @{login}]: " if login else "[GitHub]: "
    await plane_client.add_comment(
        link.plane_project_id, link.plane_card_id, f"{prefix}{comment_body}"
    )
    log.info(
        "issue_comment.created synced to plane",
        gh_repo=gh_repo,
        issue_number=issue_number,
        login=login,
    )


async def handle_pr_merged(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
    config_service: ConfigService,
    now_fn: Callable[[], datetime] = _utcnow,
) -> None:
    pr_raw: Any = payload.get("pull_request")
    pr: dict[str, Any] = cast("dict[str, Any]", pr_raw) if isinstance(pr_raw, dict) else {}

    merged: bool = bool(pr.get("merged"))
    if not merged:
        log.info("pull_request.closed: not merged, skip")
        return

    repo_raw: Any = payload.get("repository")
    repo_data: dict[str, Any] = (
        cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    )
    gh_repo: str = str(repo_data.get("full_name") or "")
    if not gh_repo:
        log.warning("pull_request.closed: missing repository.full_name")
        return

    head_raw: Any = pr.get("head")
    head: dict[str, Any] = cast("dict[str, Any]", head_raw) if isinstance(head_raw, dict) else {}
    branch: str = str(head.get("ref") or "")
    pr_url: str = str(pr.get("html_url") or "")
    merge_sha: str = str(pr.get("merge_commit_sha") or "")
    pr_body: str = str(pr.get("body") or "")

    # Plane project_id for sequence fallback
    repo_map = await config_service.get_repo_module_by_repo(gh_repo)
    plane_project_id: str | None = repo_map.plane_project_id if repo_map is not None else None

    # Collect issue numbers: branch prefix + body Closes #N
    issue_numbers: set[int] = set()
    branch_match = _BRANCH_NUM_RE.match(branch)
    if branch_match:
        issue_numbers.add(int(branch_match.group("num")))
    else:
        log.warning("pull_request.closed: branch does not match pattern", branch=branch)

    for closes_match in _CLOSES_RE.finditer(pr_body):
        issue_numbers.add(int(closes_match.group(1)))

    if not issue_numbers:
        return

    comment_text = f"Fechado via PR {pr_url} (merge {merge_sha})"

    for issue_num in issue_numbers:
        link = await fetch_link_by_gh(session, gh_repo, issue_num)

        if link is None and plane_project_id is not None:
            card = await plane_client.get_card_by_sequence(plane_project_id, issue_num)
            if card is not None:
                card_id = str(card.get("id") or "")
                if card_id:
                    link = await fetch_link_by_plane(session, card_id)

        if link is None:
            log.warning(
                "pull_request.closed: no link for issue number",
                gh_repo=gh_repo,
                issue_num=issue_num,
            )
            continue

        states = await plane_client.list_states(link.plane_project_id)
        done_state_id: str | None = None
        for s in states:
            if str(s.get("group") or "") == DONE_STATE_GROUP:
                done_state_id = str(s["id"])
                break

        if done_state_id is None:
            log.warning(
                "pull_request.closed: no completed state in Plane",
                project_id=link.plane_project_id,
            )
            continue

        await plane_client.update_card(
            link.plane_project_id, link.plane_card_id, {"state": done_state_id}
        )
        await plane_client.add_comment(
            link.plane_project_id, link.plane_card_id, comment_text
        )
        link.last_synced_at = now_fn()
        link.sync_source_last = SyncSource.github
        await session.commit()
        log.info(
            "pull_request.closed: plane card transitioned to Done",
            card_id=link.plane_card_id,
            issue_num=issue_num,
        )


def _build_pr_embed(
    pr: dict[str, Any],
    *,
    plane_card_url: str | None = None,
) -> tuple[discord.Embed, discord.ui.View]:
    pr_number = int(pr.get("number") or 0)
    title = str(pr.get("title") or "")
    html_url = str(pr.get("html_url") or "")

    user_raw: Any = pr.get("user") or {}
    user = cast("dict[str, Any]", user_raw)
    author_login = str(user.get("login") or "")

    head_raw: Any = pr.get("head") or {}
    head = cast("dict[str, Any]", head_raw)
    branch = str(head.get("ref") or "")
    repo_info_raw: Any = head.get("repo") or {}
    repo_info = cast("dict[str, Any]", repo_info_raw)
    gh_repo = str(repo_info.get("full_name") or "")

    additions = int(pr.get("additions") or 0)
    deletions = int(pr.get("deletions") or 0)

    reviewers_raw: Any = pr.get("requested_reviewers") or []
    reviewer_names: list[str] = []
    if isinstance(reviewers_raw, list):
        for r in cast("list[Any]", reviewers_raw):
            if isinstance(r, dict):
                r_dict = cast("dict[str, Any]", r)
                login = str(r_dict.get("login") or "")
                if login:
                    reviewer_names.append(login)

    embed = discord.Embed(
        title=f"PR #{pr_number}: {title}",
        color=0x00C853,
    )
    if html_url:
        embed.url = html_url
    if author_login:
        embed.set_author(name=author_login)
    embed.add_field(name="Repo", value=gh_repo or "unknown", inline=True)
    embed.add_field(name="Branch", value=branch or "unknown", inline=True)
    embed.add_field(name="Changes", value=f"+{additions} / -{deletions}", inline=True)
    if plane_card_url:
        embed.add_field(name="Plane Card", value=plane_card_url, inline=False)
    if reviewer_names:
        embed.add_field(name="Reviewers", value=", ".join(reviewer_names), inline=False)

    view = discord.ui.View()
    if html_url:
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Review PR",
                url=html_url,
            )
        )

    return embed, view


async def _upsert_pr_state(
    session: AsyncSession,
    pr_node_id: str,
    gh_repo: str,
    pr_number: int,
    *,
    new_cycle: bool = False,
) -> PrNotificationState:
    result = await session.execute(
        select(PrNotificationState).where(PrNotificationState.pr_node_id == pr_node_id)
    )
    state = result.scalar_one_or_none()
    if state is None:
        state = PrNotificationState(
            pr_node_id=pr_node_id,
            gh_repo=gh_repo,
            pr_number=pr_number,
            last_ready_cycle_id=str(uuid.uuid4()),
            ready_notified_at=None,
            discord_message_id=None,
            discord_thread_id=None,
        )
        session.add(state)
    elif new_cycle:
        state.last_ready_cycle_id = str(uuid.uuid4())
        state.ready_notified_at = None
        state.discord_message_id = None
    return state


async def _check_and_notify(
    pr: dict[str, Any],
    state: PrNotificationState,
    *,
    session: AsyncSession,
    github_client: GitHubClient,
    discord_bot: DiscordBotProtocol | None,
    discord_channel_id: str,
    now_fn: Callable[[], datetime] = _utcnow,
    plane_app_url: str | None = None,
    plane_workspace: str | None = None,
) -> bool:
    if state.ready_notified_at is not None:
        log.info("pr_ready: already notified this cycle", pr_number=state.pr_number)
        return True

    is_ready = await compute_ready(pr, github_client)
    if not is_ready:
        log.debug("pr_ready: not ready", pr_number=state.pr_number)
        return False

    if discord_bot is not None:
        plane_card_url: str | None = None
        link = await fetch_link_by_gh(session, state.gh_repo, state.pr_number)
        if link is not None:
            ws = plane_workspace if plane_workspace is not None else settings.plane_workspace
            app_url = plane_app_url if plane_app_url is not None else settings.plane_app_url
            plane_card_url = (
                f"{app_url.rstrip('/')}/{ws}/projects/"
                f"{link.plane_project_id}/issues/{link.plane_card_id}/"
            )

        embed, view = _build_pr_embed(pr, plane_card_url=plane_card_url)
        message_id = await discord_bot.post_review_message(
            discord_channel_id, embed, view=view
        )

        title = str(pr.get("title") or "")
        thread_name = f"PR #{state.pr_number} - {title[:80]}"
        thread_id = await discord_bot.create_thread(message_id, discord_channel_id, thread_name)

        state.ready_notified_at = now_fn()
        state.discord_message_id = message_id
        state.discord_thread_id = thread_id
        await session.commit()
        log.info(
            "pr_ready: discord notification sent",
            pr_number=state.pr_number,
            message_id=message_id,
            thread_id=thread_id,
        )

    return True


async def handle_pr_notification(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    github_client: GitHubClient,
    discord_bot: DiscordBotProtocol,
    discord_channel_id: str,
    new_cycle: bool = False,
    now_fn: Callable[[], datetime] = _utcnow,
    plane_app_url: str | None = None,
    plane_workspace: str | None = None,
) -> None:
    pr_raw: Any = payload.get("pull_request")
    if not isinstance(pr_raw, dict):
        log.warning("pull_request event: missing pull_request key")
        return
    pr = cast("dict[str, Any]", pr_raw)

    repo_raw: Any = payload.get("repository")
    repo = cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    gh_repo = str(repo.get("full_name") or "")

    pr_node_id = str(pr.get("node_id") or "")
    pr_number = int(pr.get("number") or 0)

    if not pr_node_id or not pr_number or not gh_repo:
        log.warning(
            "pull_request event: missing required fields",
            pr_node_id=pr_node_id,
            pr_number=pr_number,
            gh_repo=gh_repo,
        )
        return

    state = await _upsert_pr_state(
        session, pr_node_id, gh_repo, pr_number, new_cycle=new_cycle
    )
    await session.commit()

    await _check_and_notify(
        pr,
        state,
        session=session,
        github_client=github_client,
        discord_bot=discord_bot,
        discord_channel_id=discord_channel_id,
        now_fn=now_fn,
        plane_app_url=plane_app_url,
        plane_workspace=plane_workspace,
    )


async def handle_check_suite_completed(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    github_client: GitHubClient,
    discord_bot: DiscordBotProtocol | None,
    discord_channel_id: str,
    plane_client: PlaneClient,
    now_fn: Callable[[], datetime] = _utcnow,
    plane_app_url: str | None = None,
    plane_workspace: str | None = None,
) -> None:
    suite_raw: Any = payload.get("check_suite")
    if not isinstance(suite_raw, dict):
        return
    suite = cast("dict[str, Any]", suite_raw)

    repo_raw: Any = payload.get("repository")
    repo = cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    gh_repo = str(repo.get("full_name") or "")
    if not gh_repo or "/" not in gh_repo:
        return

    owner, repo_name = gh_repo.split("/", 1)

    prs_raw: Any = suite.get("pull_requests") or []
    if not isinstance(prs_raw, list):
        return

    for pr_ref_raw in cast("list[Any]", prs_raw):
        if not isinstance(pr_ref_raw, dict):
            continue
        pr_ref = cast("dict[str, Any]", pr_ref_raw)
        pr_number = int(pr_ref.get("number") or 0)
        if not pr_number:
            continue

        try:
            pr = await github_client.get_pr(owner, repo_name, pr_number)
        except Exception:
            log.warning(
                "check_suite.completed: failed to fetch PR",
                gh_repo=gh_repo,
                pr_number=pr_number,
            )
            continue

        pr_node_id = str(pr.get("node_id") or "")
        if not pr_node_id:
            continue

        state = await _upsert_pr_state(session, pr_node_id, gh_repo, pr_number)
        await session.commit()

        is_ready = await _check_and_notify(
            pr,
            state,
            session=session,
            github_client=github_client,
            discord_bot=discord_bot,
            discord_channel_id=discord_channel_id,
            now_fn=now_fn,
            plane_app_url=plane_app_url,
            plane_workspace=plane_workspace,
        )

        if is_ready:
            link = await fetch_link_by_gh(session, gh_repo, pr_number)
            if link is not None:
                await apply_stage_trigger(
                    link.plane_project_id,
                    link.plane_card_id,
                    StageTrigger.ci_passed,
                    session=session,
                    plane_client=plane_client,
                )


async def _fetch_pr_state(
    session: AsyncSession, pr_node_id: str
) -> PrNotificationState | None:
    result = await session.execute(
        select(PrNotificationState).where(PrNotificationState.pr_node_id == pr_node_id)
    )
    return result.scalar_one_or_none()


async def handle_pr_review_submitted(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    discord_bot: DiscordBotProtocol | None,
    plane_client: PlaneClient,
) -> None:
    review_raw: Any = payload.get("review")
    review: dict[str, Any] = (
        cast("dict[str, Any]", review_raw) if isinstance(review_raw, dict) else {}
    )
    pr_raw: Any = payload.get("pull_request")
    pr: dict[str, Any] = cast("dict[str, Any]", pr_raw) if isinstance(pr_raw, dict) else {}

    pr_node_id = str(pr.get("node_id") or "")
    if not pr_node_id:
        log.warning("pr_review_submitted: missing pr node_id")
        return

    review_state_val = str(review.get("state") or "").upper()

    if discord_bot is not None:
        state = await _fetch_pr_state(session, pr_node_id)
        if state is not None and state.discord_thread_id is not None:
            user_raw: Any = review.get("user") or {}
            user: dict[str, Any] = (
                cast("dict[str, Any]", user_raw) if isinstance(user_raw, dict) else {}
            )
            reviewer_login = str(user.get("login") or "")
            body = str(review.get("body") or "")[:200]
            content = f"{review_state_val} by @{reviewer_login}"
            if body:
                content += f": {body}"
            await discord_bot.post_thread_message(state.discord_thread_id, content)
            log.info("pr_review_submitted: posted to thread", pr_number=state.pr_number)

    trigger: StageTrigger | None = None
    if review_state_val == "CHANGES_REQUESTED":
        trigger = StageTrigger.changes_requested
    elif review_state_val == "APPROVED":
        trigger = StageTrigger.pr_approved

    if trigger is not None:
        repo_raw: Any = payload.get("repository")
        repo: dict[str, Any] = (
            cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
        )
        gh_repo = str(repo.get("full_name") or "")
        head_raw: Any = pr.get("head")
        head: dict[str, Any] = (
            cast("dict[str, Any]", head_raw) if isinstance(head_raw, dict) else {}
        )
        branch = str(head.get("ref") or "")
        issue_number: int | None = None
        m = _BRANCH_NUM_RE.match(branch)
        if m:
            issue_number = int(m.group("num"))
        if issue_number and gh_repo:
            link = await fetch_link_by_gh(session, gh_repo, issue_number)
            if link is not None:
                await apply_stage_trigger(
                    link.plane_project_id,
                    link.plane_card_id,
                    trigger,
                    session=session,
                    plane_client=plane_client,
                )


async def handle_pr_closed_discord(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    discord_bot: DiscordBotProtocol,
) -> None:
    pr_raw: Any = payload.get("pull_request")
    pr: dict[str, Any] = cast("dict[str, Any]", pr_raw) if isinstance(pr_raw, dict) else {}

    pr_node_id = str(pr.get("node_id") or "")
    if not pr_node_id:
        log.warning("pr_closed_discord: missing pr node_id")
        return

    state = await _fetch_pr_state(session, pr_node_id)
    if state is None or state.discord_thread_id is None:
        log.info("pr_closed_discord: no discord thread", pr_node_id=pr_node_id)
        return

    merged = bool(pr.get("merged"))
    pr_number = int(pr.get("number") or 0)
    status = "merged" if merged else "closed"
    html_url = str(pr.get("html_url") or "")

    content = f"PR #{pr_number} {status}."
    if html_url:
        content += f" {html_url}"

    thread_id = state.discord_thread_id
    await discord_bot.post_thread_message(thread_id, content)
    await discord_bot.archive_thread(thread_id)
    log.info("pr_closed_discord: thread archived", pr_number=pr_number, thread_id=thread_id)


def _gh_event_label(payload: dict[str, Any]) -> str:
    action = str(payload.get("action") or "")
    if "pull_request" in payload:
        return f"pull_request.{action}"
    if "check_suite" in payload:
        return f"check_suite.{action}"
    if "review" in payload:
        return f"review.{action}"
    if "comment" in payload:
        return f"issue_comment.{action}"
    if "issue" in payload:
        return f"issue.{action}"
    ref_type = str(payload.get("ref_type") or "")
    if ref_type:
        return f"create.{ref_type}"
    return action or "unknown"


async def handle_issue_deleted(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
) -> None:
    import httpx

    _, gh_repo, issue_number = extract_gh_coords(payload)
    if not gh_repo or not issue_number:
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.info(
            "issues.deleted: no link found, nothing to do",
            gh_repo=gh_repo,
            issue_number=issue_number,
        )
        return

    try:
        await plane_client.delete_card(link.plane_project_id, link.plane_card_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            log.info(
                "issues.deleted: plane card already gone",
                gh_repo=gh_repo,
                issue_number=issue_number,
                plane_card_id=link.plane_card_id,
            )
        else:
            raise

    await session.delete(link)
    await session.commit()
    log.info(
        "issues.deleted -> plane card deleted",
        gh_repo=gh_repo,
        issue_number=issue_number,
        plane_card_id=link.plane_card_id,
    )


async def handle_branch_created(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
) -> None:
    ref_type = str(payload.get("ref_type") or "")
    if ref_type != "branch":
        return

    repo_raw: Any = payload.get("repository")
    repo: dict[str, Any] = cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    gh_repo = str(repo.get("full_name") or "")
    if not gh_repo:
        return

    branch = str(payload.get("ref") or "")
    m = _BRANCH_NUM_RE.match(branch)
    if not m:
        log.debug("branch_created: branch does not match pattern", branch=branch)
        return

    issue_number = int(m.group("num"))
    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.info(
            "branch_created: no link for issue",
            gh_repo=gh_repo,
            issue_number=issue_number,
        )
        return

    await apply_stage_trigger(
        link.plane_project_id,
        link.plane_card_id,
        StageTrigger.branch_created,
        session=session,
        plane_client=plane_client,
    )


async def handle_pr_plane_stage(
    payload: dict[str, Any],
    trigger: StageTrigger,
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
) -> None:
    pr_raw: Any = payload.get("pull_request")
    pr: dict[str, Any] = cast("dict[str, Any]", pr_raw) if isinstance(pr_raw, dict) else {}
    repo_raw: Any = payload.get("repository")
    repo: dict[str, Any] = cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    gh_repo = str(repo.get("full_name") or "")
    if not gh_repo:
        return

    head_raw: Any = pr.get("head")
    head: dict[str, Any] = cast("dict[str, Any]", head_raw) if isinstance(head_raw, dict) else {}
    branch = str(head.get("ref") or "")

    issue_number: int | None = None
    m = _BRANCH_NUM_RE.match(branch)
    if m:
        issue_number = int(m.group("num"))
    else:
        pr_body = str(pr.get("body") or "")
        closes_matches = list(_CLOSES_RE.finditer(pr_body))
        if closes_matches:
            issue_number = int(closes_matches[0].group(1))

    if not issue_number:
        log.debug("pr_plane_stage: no issue number resolved", trigger=trigger, branch=branch)
        return

    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.info(
            "pr_plane_stage: no link",
            gh_repo=gh_repo,
            issue_number=issue_number,
            trigger=trigger,
        )
        return

    await apply_stage_trigger(
        link.plane_project_id,
        link.plane_card_id,
        trigger,
        session=session,
        plane_client=plane_client,
    )


async def handle_pr_closed_unmerged(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
) -> None:
    pr_raw: Any = payload.get("pull_request")
    pr: dict[str, Any] = cast("dict[str, Any]", pr_raw) if isinstance(pr_raw, dict) else {}

    if bool(pr.get("merged")):
        return  # handled by handle_pr_merged

    repo_raw: Any = payload.get("repository")
    repo: dict[str, Any] = cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    gh_repo = str(repo.get("full_name") or "")
    if not gh_repo:
        return

    head_raw: Any = pr.get("head")
    head: dict[str, Any] = cast("dict[str, Any]", head_raw) if isinstance(head_raw, dict) else {}
    branch = str(head.get("ref") or "")

    m = _BRANCH_NUM_RE.match(branch)
    if not m:
        log.debug("pr_closed_unmerged: branch does not match pattern", branch=branch)
        return

    issue_number = int(m.group("num"))
    link = await fetch_link_by_gh(session, gh_repo, issue_number)
    if link is None:
        log.info(
            "pr_closed_unmerged: no link",
            gh_repo=gh_repo,
            issue_number=issue_number,
        )
        return

    await apply_stage_trigger(
        link.plane_project_id,
        link.plane_card_id,
        StageTrigger.pr_closed,
        session=session,
        plane_client=plane_client,
    )


async def process_github_event(
    ctx: dict[str, Any], log_id: str, payload_json: str
) -> None:
    payload: dict[str, Any] = json.loads(payload_json)
    action: str = str(payload.get("action") or "")
    event_type: str = _gh_event_label(payload)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        log_id=log_id,
        event_type=event_type,
        action=action,
        source="github",
    )
    log.info("process_github_event.started")

    async def _dispatch() -> None:
        if "ref_type" in payload and "pull_request" not in payload and "issue" not in payload:
            async with ctx["session_factory"]() as session:
                await handle_branch_created(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                )
        elif "issue" in payload and action == "opened":
            async with ctx["session_factory"]() as session:
                await handle_issue_opened(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                    github_client=ctx["github_client"],
                    config_service=ctx["config_service"],
                )
        elif "issue" in payload and action == "edited":
            async with ctx["session_factory"]() as session:
                await handle_issue_edited(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                )
        elif "issue" in payload and action in ("labeled", "unlabeled"):
            async with ctx["session_factory"]() as session:
                await handle_issue_labels_changed(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                    config_service=ctx["config_service"],
                )
        elif "issue" in payload and action in ("assigned", "unassigned"):
            async with ctx["session_factory"]() as session:
                await handle_issue_assignees_changed(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                    config_service=ctx["config_service"],
                )
        elif "issue" in payload and action == "closed":
            async with ctx["session_factory"]() as session:
                await handle_issue_closed(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                )
        elif "issue" in payload and action == "deleted":
            async with ctx["session_factory"]() as session:
                await handle_issue_deleted(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                )
        elif "comment" in payload and action == "created":
            async with ctx["session_factory"]() as session:
                await handle_issue_comment_created(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                )
        elif "pull_request" in payload and action in ("opened", "ready_for_review"):
            _db: Any = ctx.get("discord_bot")
            if _db is not None:
                async with ctx["session_factory"]() as session:
                    await handle_pr_notification(
                        payload,
                        session=session,
                        github_client=ctx["github_client"],
                        discord_bot=_db,
                        discord_channel_id=settings.discord_review_channel_id,
                    )
            async with ctx["session_factory"]() as session:
                await handle_pr_plane_stage(
                    payload,
                    StageTrigger.pr_opened,
                    session=session,
                    plane_client=ctx["plane_client"],
                )
        elif "pull_request" in payload and action == "reopened":
            _db = ctx.get("discord_bot")
            if _db is not None:
                async with ctx["session_factory"]() as session:
                    await handle_pr_notification(
                        payload,
                        session=session,
                        github_client=ctx["github_client"],
                        discord_bot=_db,
                        discord_channel_id=settings.discord_review_channel_id,
                        new_cycle=True,
                    )
            async with ctx["session_factory"]() as session:
                await handle_pr_plane_stage(
                    payload,
                    StageTrigger.pr_opened,
                    session=session,
                    plane_client=ctx["plane_client"],
                )
        elif "check_suite" in payload and action == "completed":
            async with ctx["session_factory"]() as session:
                await handle_check_suite_completed(
                    payload,
                    session=session,
                    github_client=ctx["github_client"],
                    discord_bot=ctx.get("discord_bot"),
                    discord_channel_id=settings.discord_review_channel_id,
                    plane_client=ctx["plane_client"],
                )
        elif "review" in payload and action == "submitted":
            async with ctx["session_factory"]() as session:
                await handle_pr_review_submitted(
                    payload,
                    session=session,
                    discord_bot=ctx.get("discord_bot"),
                    plane_client=ctx["plane_client"],
                )
        elif "pull_request" in payload and action == "closed":
            async with ctx["session_factory"]() as session:
                await handle_pr_merged(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                    config_service=ctx["config_service"],
                )
            async with ctx["session_factory"]() as session:
                await handle_pr_closed_unmerged(
                    payload,
                    session=session,
                    plane_client=ctx["plane_client"],
                )
            _db = ctx.get("discord_bot")
            if _db is not None:
                async with ctx["session_factory"]() as session:
                    await handle_pr_closed_discord(
                        payload,
                        session=session,
                        discord_bot=_db,
                    )
        else:
            log.warning(
                "process_github_event: unhandled event",
                action=action,
                event_type=event_type,
                log_id=log_id,
            )

    start = time.perf_counter()
    outcome = "success"
    try:
        await run_with_retry(
            _dispatch,
            ctx=ctx,
            source="github",
            event_type=event_type,
            payload_json=payload_json,
        )
    except DeadLetteredError:
        outcome = "dead_lettered"
    except Exception:
        outcome = "error"
        raise
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        log.info(
            "process_github_event.finished",
            outcome=outcome,
            duration_ms=duration_ms,
        )
        sync_actions_total.labels(type=event_type, outcome=outcome).inc()
        sync_duration_seconds.labels(type=event_type).observe(
            time.perf_counter() - start
        )

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from integration.models import CardIssueLink, SyncSource

LOOP_WINDOW_SECONDS = 5
CONFLICT_WINDOW_SECONDS = 10

_FOOTER_RE = re.compile(r"\n\n---\n(?:Plane|GitHub): [^\n]*")


def strip_footer(text: str) -> str:
    return _FOOTER_RE.sub("", text)


def should_skip_loop(
    link: CardIssueLink,
    event_updated_at: datetime,
    source: SyncSource,
) -> bool:
    if link.sync_source_last != source:
        return False
    delta = abs((event_updated_at - link.last_synced_at).total_seconds())
    return delta <= LOOP_WINDOW_SECONDS


def detect_conflict(
    link: CardIssueLink,
    event_updated_at: datetime,
    source: SyncSource,
) -> bool:
    if link.sync_source_last == source:
        return False
    delta = abs((event_updated_at - link.last_synced_at).total_seconds())
    return delta <= CONFLICT_WINDOW_SECONDS


def event_wins_conflict(link: CardIssueLink, event_updated_at: datetime) -> bool:
    return event_updated_at > link.last_synced_at


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def fetch_link_by_gh(
    session: AsyncSession, gh_repo: str, issue_number: int
) -> CardIssueLink | None:
    stmt = select(CardIssueLink).where(
        CardIssueLink.gh_repo == gh_repo,
        CardIssueLink.gh_issue_number == issue_number,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def fetch_link_by_plane(
    session: AsyncSession, plane_card_id: str
) -> CardIssueLink | None:
    stmt = select(CardIssueLink).where(CardIssueLink.plane_card_id == plane_card_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def extract_gh_coords(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str, int]:
    from typing import cast

    issue_raw: Any = payload.get("issue")
    issue: dict[str, Any] = (
        cast("dict[str, Any]", issue_raw) if isinstance(issue_raw, dict) else {}
    )
    repo_raw: Any = payload.get("repository")
    repo: dict[str, Any] = (
        cast("dict[str, Any]", repo_raw) if isinstance(repo_raw, dict) else {}
    )
    gh_repo = str(repo.get("full_name") or "")
    issue_number = int(issue.get("number") or 0)
    return issue, gh_repo, issue_number

"""Backfill existing Plane cards <-> GitHub issues.

Usage:
    python -m integration.backfill --project <id> --repo <owner/repo> [--create-missing]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from integration.clients.github import GitHubClient
from integration.clients.plane import PlaneClient
from integration.config import settings
from integration.db import SessionLocal
from integration.handlers._sync import fetch_link_by_plane
from integration.models import CardIssueLink, SyncSource

log = structlog.get_logger()


def _card_url(app_url: str, workspace: str, project_id: str, card_id: str) -> str:
    return f"{app_url.rstrip('/')}/{workspace}/projects/{project_id}/issues/{card_id}/"


def _footer_linked(
    card: dict[str, Any],
    issue: dict[str, Any],
    card_url: str,
    gh_url: str,
) -> bool:
    desc = str(card.get("description_html") or card.get("description") or "")
    body = str(issue.get("body") or "")
    return gh_url in desc or card_url in body


async def backfill(
    *,
    project_id: str,
    gh_repo: str,
    create_missing: bool,
    session: AsyncSession,
    plane_client: PlaneClient,
    github_client: GitHubClient,
    app_url: str,
    workspace: str,
) -> None:
    owner, repo = gh_repo.split("/", 1)

    print(f"Fetching Plane cards for project {project_id!r}...")
    cards = await plane_client.list_cards(project_id)
    print(f"  {len(cards)} cards found")

    print(f"Fetching GitHub issues for {gh_repo!r}...")
    issues = await github_client.list_issues(owner, repo)
    print(f"  {len(issues)} open issues found")

    issues_by_number: dict[int, dict[str, Any]] = {int(i["number"]): i for i in issues}
    issues_by_title: dict[str, list[dict[str, Any]]] = {}
    for iss in issues:
        t = str(iss.get("title") or "")
        issues_by_title.setdefault(t, []).append(iss)

    matched_gh_numbers: set[int] = set()
    unmatched_cards: list[dict[str, Any]] = []
    unmatched_gh_numbers: set[int] = set(issues_by_number)

    print("\nMatching cards to issues...")
    for card in cards:
        card_id = str(card.get("id") or "")
        card_name = str(card.get("name") or "")

        existing = await fetch_link_by_plane(session, card_id)
        if existing is not None:
            matched_gh_numbers.add(existing.gh_issue_number)
            unmatched_gh_numbers.discard(existing.gh_issue_number)
            print(
                f"  [existing] card {card_id!r} already linked to GH#{existing.gh_issue_number}"
            )
            continue

        url = _card_url(app_url, workspace, project_id, card_id)
        matched_issue: dict[str, Any] | None = None

        for candidate in issues_by_title.get(card_name, []):
            num = int(candidate["number"])
            if num not in matched_gh_numbers:
                matched_issue = candidate
                break

        if matched_issue is None:
            for iss in issues:
                num = int(iss["number"])
                if num in matched_gh_numbers:
                    continue
                gh_url = str(iss.get("html_url") or "")
                if _footer_linked(card, iss, url, gh_url):
                    matched_issue = iss
                    break

        if matched_issue is not None:
            num = int(matched_issue["number"])
            node_id = str(matched_issue.get("node_id") or "")
            link = CardIssueLink(
                plane_card_id=card_id,
                plane_project_id=project_id,
                gh_repo=gh_repo,
                gh_issue_number=num,
                gh_issue_node_id=node_id,
                last_synced_at=datetime.now(UTC),
                sync_source_last=SyncSource.plane,
            )
            session.add(link)
            await session.commit()
            matched_gh_numbers.add(num)
            unmatched_gh_numbers.discard(num)
            print(f"  [linked] card {card_id!r} ({card_name!r}) -> GH#{num}")
        else:
            unmatched_cards.append(card)
            print(f"  [unmatched-card] {card_id!r} ({card_name!r})")

    for num in sorted(unmatched_gh_numbers):
        iss = issues_by_number[num]
        title = str(iss.get("title") or "")
        print(f"  [unmatched-issue] GH#{num} ({title!r})")

    if not create_missing:
        return

    if not unmatched_cards and not unmatched_gh_numbers:
        print("\nNo missing counterparts to create.")
        return

    print("\nCreating missing counterparts...")

    for card in unmatched_cards:
        card_id = str(card.get("id") or "")
        card_name = str(card.get("name") or "")
        card_desc = str(card.get("description_html") or card.get("description") or "")
        url = _card_url(app_url, workspace, project_id, card_id)
        issue_body = f"{card_desc}\n\n---\nPlane: {url}"
        gh_issue = await github_client.create_issue(
            owner, repo, {"title": card_name, "body": issue_body}
        )
        num = int(gh_issue["number"])
        node_id = str(gh_issue.get("node_id") or "")
        gh_url = str(gh_issue.get("html_url") or "")
        new_desc = f"{card_desc}\n\n---\nGitHub: {gh_url}"
        await plane_client.update_card(project_id, card_id, {"description_html": new_desc})
        link = CardIssueLink(
            plane_card_id=card_id,
            plane_project_id=project_id,
            gh_repo=gh_repo,
            gh_issue_number=num,
            gh_issue_node_id=node_id,
            last_synced_at=datetime.now(UTC),
            sync_source_last=SyncSource.plane,
        )
        session.add(link)
        await session.commit()
        print(f"  [created-gh-issue] card {card_id!r} ({card_name!r}) -> GH#{num}")

    if unmatched_gh_numbers:
        states = await plane_client.list_states(project_id)
        refinamento_id: str | None = None
        for s in states:
            if str(s.get("name") or "") == "Refinamento":
                refinamento_id = str(s.get("id") or "")
                break

        for num in sorted(unmatched_gh_numbers):
            iss = issues_by_number[num]
            issue_title = str(iss.get("title") or "")
            issue_body = str(iss.get("body") or "")
            issue_node_id = str(iss.get("node_id") or "")

            card_payload: dict[str, Any] = {"name": issue_title, "priority": "medium"}
            if refinamento_id:
                card_payload["state"] = refinamento_id

            plane_card = await plane_client.create_card(project_id, card_payload)
            plane_card_id = str(plane_card.get("id") or "")
            card_url = _card_url(app_url, workspace, project_id, plane_card_id)
            new_body = f"{issue_body}\n\n---\nPlane: {card_url}"
            await github_client.update_issue(owner, repo, num, {"body": new_body})
            link = CardIssueLink(
                plane_card_id=plane_card_id,
                plane_project_id=project_id,
                gh_repo=gh_repo,
                gh_issue_number=num,
                gh_issue_node_id=issue_node_id,
                last_synced_at=datetime.now(UTC),
                sync_source_last=SyncSource.github,
            )
            session.add(link)
            await session.commit()
            print(
                f"  [created-plane-card] GH#{num} ({issue_title!r}) -> card {plane_card_id!r}"
            )


async def _run(*, project_id: str, gh_repo: str, create_missing: bool) -> None:
    plane_client = PlaneClient(
        base_url=settings.plane_base_url,
        api_token=settings.plane_api_token,
        workspace=settings.plane_workspace,
    )
    github_client = GitHubClient(
        app_id=settings.github_app_id,
        private_key_pem=settings.github_app_private_key,
        installation_id=settings.github_app_installation_id,
    )
    try:
        async with SessionLocal() as session:
            await backfill(
                project_id=project_id,
                gh_repo=gh_repo,
                create_missing=create_missing,
                session=session,
                plane_client=plane_client,
                github_client=github_client,
                app_url=settings.plane_app_url,
                workspace=settings.plane_workspace,
            )
    finally:
        await plane_client.aclose()
        await github_client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m integration.backfill",
        description="Backfill Plane cards <-> GitHub issues",
    )
    parser.add_argument("--project", required=True, help="Plane project ID")
    parser.add_argument("--repo", required=True, help="GitHub repo (owner/repo)")
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Create missing counterparts",
    )
    args = parser.parse_args()

    if "/" not in args.repo:
        print(
            f"Error: --repo must be owner/repo format, got {args.repo!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    asyncio.run(
        _run(
            project_id=args.project,
            gh_repo=args.repo,
            create_missing=args.create_missing,
        )
    )


if __name__ == "__main__":
    main()

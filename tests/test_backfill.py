from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.backfill import backfill
from integration.models import CardIssueLink, SyncSource


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


APP_URL = "https://app.plane.so"
WORKSPACE = "test-ws"
PROJECT_ID = "proj-1"
GH_REPO = "owner/repo"
FIXED_TIME = datetime(2026, 5, 26, 10, 0, 0, tzinfo=UTC)


class FakeResult:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


class FakeSession:
    def __init__(self, existing_links: dict[str, CardIssueLink] | None = None) -> None:
        self.links: dict[str, CardIssueLink] = existing_links or {}
        self.added: list[Any] = []
        self.commit_count: int = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1

    async def execute(self, stmt: Any) -> FakeResult:
        # fetch_link_by_plane: look up the card_id from the WHERE clause
        # We match by inspecting the compiled parameters if possible,
        # but it's easier to match the last card queried via a side-channel.
        # Instead, we return None by default and let tests inject via _current_card_id.
        card_id = getattr(self, "_current_card_id", None)
        if card_id is not None:
            return FakeResult(self.links.get(card_id))
        return FakeResult(None)


def _make_plane_client(
    cards: list[dict[str, Any]],
    states: list[dict[str, Any]] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.list_cards = AsyncMock(return_value=cards)
    client.list_states = AsyncMock(return_value=states or [])
    client.update_card = AsyncMock(return_value={})
    client.create_card = AsyncMock(
        return_value={"id": "new-plane-card", "name": "new card"}
    )
    return client


def _make_github_client(
    issues: list[dict[str, Any]],
) -> MagicMock:
    client = MagicMock()
    client.list_issues = AsyncMock(return_value=issues)
    client.create_issue = AsyncMock(
        return_value={
            "number": 99,
            "node_id": "node-99",
            "html_url": "https://github.com/owner/repo/issues/99",
        }
    )
    client.update_issue = AsyncMock(return_value={})
    return client


def _make_card(
    card_id: str = "card-1",
    name: str = "My Feature",
    description: str = "",
) -> dict[str, Any]:
    return {"id": card_id, "name": name, "description_html": description}


def _make_issue(
    number: int = 1,
    title: str = "My Feature",
    body: str = "",
    node_id: str = "node-1",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "node_id": node_id,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
    }


async def _run_backfill(
    *,
    cards: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    create_missing: bool = False,
    existing_links: dict[str, CardIssueLink] | None = None,
    states: list[dict[str, Any]] | None = None,
) -> tuple[FakeSession, MagicMock, MagicMock]:
    session = FakeSession(existing_links)
    plane_client = _make_plane_client(cards, states)
    github_client = _make_github_client(issues)

    # Patch fetch_link_by_plane to use our FakeSession._current_card_id trick
    # by monkey-patching the module-level function used in _backfill
    async def smart_execute(stmt: Any) -> FakeResult:
        # Extract card_id from the WHERE clause comparison value
        try:

            compiled = stmt.compile()
            params: dict[str, Any] = dict(compiled.params)
            for val in params.values():
                if isinstance(val, str) and val in session.links:
                    return FakeResult(session.links[val])
            # If no match in links, return None
            return FakeResult(None)
        except Exception:
            return FakeResult(None)

    session.execute = smart_execute  # type: ignore[method-assign]

    await backfill(
        project_id=PROJECT_ID,
        gh_repo=GH_REPO,
        create_missing=create_missing,
        session=session,  # type: ignore[arg-type]
        plane_client=plane_client,
        github_client=github_client,
        app_url=APP_URL,
        workspace=WORKSPACE,
    )
    return session, plane_client, github_client


def test_match_by_title() -> None:
    card = _make_card(card_id="c1", name="Fix bug")
    issue = _make_issue(number=5, title="Fix bug")

    session, _, _ = _run(_run_backfill(cards=[card], issues=[issue]))

    assert len(session.added) == 1
    link: CardIssueLink = session.added[0]
    assert link.plane_card_id == "c1"
    assert link.gh_issue_number == 5
    assert link.gh_repo == GH_REPO
    assert link.plane_project_id == PROJECT_ID
    assert link.sync_source_last == SyncSource.plane
    assert session.commit_count == 1


def test_match_by_footer_plane_url_in_issue_body() -> None:
    card_url = f"{APP_URL}/{WORKSPACE}/projects/{PROJECT_ID}/issues/c2/"
    card = _make_card(card_id="c2", name="Task A")
    issue = _make_issue(number=7, title="Task B", body=f"some text\n\n---\nPlane: {card_url}")

    session, _, _ = _run(_run_backfill(cards=[card], issues=[issue]))

    assert len(session.added) == 1
    assert session.added[0].gh_issue_number == 7


def test_match_by_footer_gh_url_in_card_desc() -> None:
    gh_url = "https://github.com/owner/repo/issues/8"
    card = _make_card(
        card_id="c3",
        name="Task C",
        description=f"desc\n\n---\nGitHub: {gh_url}",
    )
    issue = _make_issue(number=8, title="Task D")

    session, _, _ = _run(_run_backfill(cards=[card], issues=[issue]))

    assert len(session.added) == 1
    assert session.added[0].gh_issue_number == 8


def test_unmatched_card_no_link_created_without_flag() -> None:
    card = _make_card(card_id="c4", name="Orphan Card")
    issue = _make_issue(number=10, title="Different Title")

    session, plane_client, github_client = _run(
        _run_backfill(cards=[card], issues=[issue], create_missing=False)
    )

    # No link written, no issue created
    assert len(session.added) == 0
    github_client.create_issue.assert_not_called()
    plane_client.create_card.assert_not_called()


def test_unmatched_issue_no_link_created_without_flag() -> None:
    card = _make_card(card_id="c5", name="My Card")
    issue = _make_issue(number=11, title="My Card")
    extra_issue = _make_issue(number=12, title="Extra Unmatched Issue")

    session, plane_client, _github_client = _run(
        _run_backfill(
            cards=[card],
            issues=[issue, extra_issue],
            create_missing=False,
        )
    )

    # Only the matched card-issue link is written
    assert len(session.added) == 1
    assert session.added[0].gh_issue_number == 11
    plane_client.create_card.assert_not_called()


def test_idempotent_already_linked() -> None:
    card_id = "c6"
    existing_link = CardIssueLink()
    existing_link.plane_card_id = card_id
    existing_link.plane_project_id = PROJECT_ID
    existing_link.gh_repo = GH_REPO
    existing_link.gh_issue_number = 20
    existing_link.gh_issue_node_id = "node-20"
    existing_link.last_synced_at = FIXED_TIME
    existing_link.sync_source_last = SyncSource.plane

    card = _make_card(card_id=card_id, name="Already Linked")
    issue = _make_issue(number=20, title="Already Linked")

    session, _, _ = _run(
        _run_backfill(
            cards=[card],
            issues=[issue],
            existing_links={card_id: existing_link},
        )
    )

    # No new link added — idempotent
    assert len(session.added) == 0


def test_create_missing_plane_card_creates_gh_issue() -> None:
    card = _make_card(card_id="c7", name="Orphan Plane Card", description="some desc")

    session, plane_client, github_client = _run(
        _run_backfill(cards=[card], issues=[], create_missing=True)
    )

    github_client.create_issue.assert_called_once()
    call_kwargs = github_client.create_issue.call_args
    payload = call_kwargs[0][2]  # positional arg: (owner, repo, payload)
    assert payload["title"] == "Orphan Plane Card"
    assert "Plane:" in payload["body"]

    plane_client.update_card.assert_called_once()
    assert "GitHub:" in plane_client.update_card.call_args[0][2]["description_html"]

    assert len(session.added) == 1
    link: CardIssueLink = session.added[0]
    assert link.plane_card_id == "c7"
    assert link.gh_issue_number == 99
    assert link.sync_source_last == SyncSource.plane


def test_create_missing_gh_issue_creates_plane_card() -> None:
    issue = _make_issue(number=30, title="Orphan GH Issue", body="issue body")
    states = [{"id": "state-ref-1", "name": "Refinamento"}]

    session, plane_client, github_client = _run(
        _run_backfill(cards=[], issues=[issue], create_missing=True, states=states)
    )

    plane_client.create_card.assert_called_once()
    card_payload = plane_client.create_card.call_args[0][1]
    assert card_payload["name"] == "Orphan GH Issue"
    assert card_payload["priority"] == "medium"
    assert card_payload["state"] == "state-ref-1"

    github_client.update_issue.assert_called_once()
    update_payload = github_client.update_issue.call_args[0][3]
    assert "Plane:" in update_payload["body"]

    assert len(session.added) == 1
    link: CardIssueLink = session.added[0]
    assert link.gh_issue_number == 30
    assert link.sync_source_last == SyncSource.github


def test_create_missing_gh_issue_no_refinamento_state() -> None:
    issue = _make_issue(number=31, title="Issue No State")

    _session, plane_client, _ = _run(
        _run_backfill(cards=[], issues=[issue], create_missing=True, states=[])
    )

    card_payload = plane_client.create_card.call_args[0][1]
    assert "state" not in card_payload


def test_no_create_when_nothing_unmatched() -> None:
    card = _make_card(card_id="c8", name="Matched")
    issue = _make_issue(number=40, title="Matched")

    session, plane_client, github_client = _run(
        _run_backfill(cards=[card], issues=[issue], create_missing=True)
    )

    # Matched by title -> link written; no create calls
    github_client.create_issue.assert_not_called()
    plane_client.create_card.assert_not_called()
    assert len(session.added) == 1

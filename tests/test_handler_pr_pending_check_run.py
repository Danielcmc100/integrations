from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.handlers.github import handle_pr_pending_check_run


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


GH_REPO = "owner/repo"
HEAD_SHA = "abc123def456"
CHECK_NAME = "CI"


def _make_payload(
    *,
    repo: str = GH_REPO,
    sha: str = HEAD_SHA,
) -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "head": {"sha": sha},
        },
        "repository": {"full_name": repo},
    }


def _make_github_client() -> MagicMock:
    client = MagicMock()
    client.create_check_run = AsyncMock(return_value={"id": 1})
    return client


def test_creates_queued_check_run() -> None:
    payload = _make_payload()
    client = _make_github_client()

    _run(handle_pr_pending_check_run(payload, github_client=client, check_name=CHECK_NAME))

    client.create_check_run.assert_called_once_with("owner", "repo", CHECK_NAME, HEAD_SHA)


def test_missing_pull_request_key() -> None:
    payload: dict[str, Any] = {"action": "opened", "repository": {"full_name": GH_REPO}}
    client = _make_github_client()

    _run(handle_pr_pending_check_run(payload, github_client=client, check_name=CHECK_NAME))

    client.create_check_run.assert_not_called()


def test_missing_head_sha() -> None:
    payload = _make_payload(sha="")
    client = _make_github_client()

    _run(handle_pr_pending_check_run(payload, github_client=client, check_name=CHECK_NAME))

    client.create_check_run.assert_not_called()


def test_missing_repository() -> None:
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"head": {"sha": HEAD_SHA}},
    }
    client = _make_github_client()

    _run(handle_pr_pending_check_run(payload, github_client=client, check_name=CHECK_NAME))

    client.create_check_run.assert_not_called()


def test_custom_check_name() -> None:
    payload = _make_payload()
    client = _make_github_client()

    _run(handle_pr_pending_check_run(payload, github_client=client, check_name="Jenkins"))

    client.create_check_run.assert_called_once_with("owner", "repo", "Jenkins", HEAD_SHA)

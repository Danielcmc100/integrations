from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import integration.pr_ready as pr_ready_module
from integration.pr_ready import compute_ready


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def _make_pr(
    *,
    draft: bool = False,
    sha: str = "abc123",
    full_name: str = "owner/repo",
    branch: str = "main",
) -> dict[str, Any]:
    return {
        "draft": draft,
        "head": {
            "sha": sha,
            "ref": branch,
            "repo": {"full_name": full_name},
        },
    }


def _make_github_client(
    *,
    required_contexts: list[str] | None = None,
    check_runs: list[dict[str, Any]] | None = None,
    protection_raises: bool = False,
) -> MagicMock:
    client = MagicMock()
    if protection_raises:
        client.get_branch_protection = AsyncMock(side_effect=Exception("not found"))
    else:
        protection_payload: dict[str, Any] = {}
        if required_contexts is not None:
            protection_payload = {
                "required_status_checks": {"contexts": required_contexts}
            }
        client.get_branch_protection = AsyncMock(return_value=protection_payload)
    client.list_check_runs = AsyncMock(return_value=check_runs or [])
    return client


def _fixed_time(t: float = 1000.0) -> Any:
    def _fn() -> float:
        return t

    return _fn


@pytest.fixture(autouse=True)
def clear_cache() -> Any:
    pr_ready_module._protection_cache.clear()
    yield
    pr_ready_module._protection_cache.clear()


# ---------------------------------------------------------------------------
# Draft PR
# ---------------------------------------------------------------------------


def test_draft_pr_not_ready() -> None:
    client = _make_github_client(required_contexts=[], check_runs=[])
    result = _run(compute_ready(_make_pr(draft=True), client))
    assert result is False
    client.get_branch_protection.assert_not_called()


# ---------------------------------------------------------------------------
# No required checks -> ready (not draft)
# ---------------------------------------------------------------------------


def test_no_required_checks_ready() -> None:
    client = _make_github_client(required_contexts=[], check_runs=[])
    result = _run(compute_ready(_make_pr(), client))
    assert result is True


# ---------------------------------------------------------------------------
# All required checks pass
# ---------------------------------------------------------------------------


def test_all_required_checks_pass() -> None:
    check_runs = [
        {"name": "ci/test", "conclusion": "success"},
        {"name": "ci/lint", "conclusion": "success"},
    ]
    client = _make_github_client(
        required_contexts=["ci/test", "ci/lint"], check_runs=check_runs
    )
    result = _run(compute_ready(_make_pr(), client))
    assert result is True


# ---------------------------------------------------------------------------
# One required check failing
# ---------------------------------------------------------------------------


def test_one_check_failing_not_ready() -> None:
    check_runs = [
        {"name": "ci/test", "conclusion": "success"},
        {"name": "ci/lint", "conclusion": "failure"},
    ]
    client = _make_github_client(
        required_contexts=["ci/test", "ci/lint"], check_runs=check_runs
    )
    result = _run(compute_ready(_make_pr(), client))
    assert result is False


# ---------------------------------------------------------------------------
# Required check still in progress (conclusion=None)
# ---------------------------------------------------------------------------


def test_check_in_progress_not_ready() -> None:
    check_runs = [
        {"name": "ci/test", "conclusion": None},
    ]
    client = _make_github_client(
        required_contexts=["ci/test"], check_runs=check_runs
    )
    result = _run(compute_ready(_make_pr(), client))
    assert result is False


# ---------------------------------------------------------------------------
# Required check missing from check_runs list
# ---------------------------------------------------------------------------


def test_missing_check_not_ready() -> None:
    client = _make_github_client(
        required_contexts=["ci/test"], check_runs=[]
    )
    result = _run(compute_ready(_make_pr(), client))
    assert result is False


# ---------------------------------------------------------------------------
# Branch protection raises -> treat as no required checks -> ready
# ---------------------------------------------------------------------------


def test_branch_protection_error_treated_as_no_requirements() -> None:
    client = _make_github_client(protection_raises=True, check_runs=[])
    result = _run(compute_ready(_make_pr(), client))
    assert result is True


# ---------------------------------------------------------------------------
# Cache: second call within TTL does not re-fetch
# ---------------------------------------------------------------------------


def test_branch_protection_cached() -> None:
    check_runs = [{"name": "ci/test", "conclusion": "success"}]
    client = _make_github_client(
        required_contexts=["ci/test"], check_runs=check_runs
    )
    t = _fixed_time(1000.0)

    _run(compute_ready(_make_pr(), client, time_fn=t))
    _run(compute_ready(_make_pr(), client, time_fn=t))

    assert client.get_branch_protection.call_count == 1


# ---------------------------------------------------------------------------
# Cache: expired after TTL -> re-fetch
# ---------------------------------------------------------------------------


def test_branch_protection_cache_expired() -> None:
    check_runs = [{"name": "ci/test", "conclusion": "success"}]
    client = _make_github_client(
        required_contexts=["ci/test"], check_runs=check_runs
    )

    _run(compute_ready(_make_pr(), client, time_fn=_fixed_time(1000.0)))
    _run(
        compute_ready(
            _make_pr(),
            client,
            time_fn=_fixed_time(1000.0 + pr_ready_module._CACHE_TTL + 1),
        )
    )

    assert client.get_branch_protection.call_count == 2


# ---------------------------------------------------------------------------
# Missing head fields -> not ready
# ---------------------------------------------------------------------------


def test_missing_sha_not_ready() -> None:
    pr = {"draft": False, "head": {"sha": "", "ref": "main", "repo": {"full_name": "o/r"}}}
    client = _make_github_client(required_contexts=[])
    result = _run(compute_ready(pr, client))
    assert result is False

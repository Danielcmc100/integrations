"""PR readiness computation with branch-protection cache."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

from integration.clients.github import GitHubClient

JsonDict = dict[str, Any]
TimeFn = Callable[[], float]

_CACHE_TTL = 600.0  # 10 minutes

_protection_cache: dict[str, tuple[float, list[str]]] = {}


async def _get_required_checks(
    github_client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    *,
    time_fn: TimeFn = time.time,
) -> list[str]:
    key = f"{owner}/{repo}/{branch}"
    now = time_fn()
    if key in _protection_cache:
        cached_at, contexts = _protection_cache[key]
        if now - cached_at < _CACHE_TTL:
            return contexts

    try:
        protection = await github_client.get_branch_protection(owner, repo, branch)
    except Exception:
        _protection_cache[key] = (now, [])
        return []

    rsc_raw: Any = protection.get("required_status_checks") or {}
    rsc = cast("dict[str, Any]", rsc_raw)
    raw_contexts_any: Any = rsc.get("contexts") or []
    raw_contexts = cast("list[Any]", raw_contexts_any)
    contexts = [c for c in raw_contexts if isinstance(c, str)]
    _protection_cache[key] = (now, contexts)
    return contexts


async def compute_ready(
    pr: JsonDict,
    github_client: GitHubClient,
    *,
    time_fn: TimeFn = time.time,
) -> bool:
    if pr.get("draft", True):
        return False

    head_raw: Any = pr.get("head") or {}
    head = cast("dict[str, Any]", head_raw)
    sha_raw: Any = head.get("sha") or ""
    sha = str(sha_raw)
    repo_info_raw: Any = head.get("repo") or {}
    repo_info = cast("dict[str, Any]", repo_info_raw)
    full_name_raw: Any = repo_info.get("full_name") or ""
    full_name = str(full_name_raw)
    branch_raw: Any = head.get("ref") or ""
    branch = str(branch_raw)

    if not sha or not full_name or not branch:
        return False

    parts = full_name.split("/", 1)
    if len(parts) != 2:
        return False
    owner, repo = parts

    required = await _get_required_checks(
        github_client, owner, repo, branch, time_fn=time_fn
    )

    if not required:
        return True

    check_runs = await github_client.list_check_runs(owner, repo, sha)

    conclusions: dict[str, str | None] = {}
    for run in check_runs:
        name_raw: Any = run.get("name")
        conclusion_raw: Any = run.get("conclusion")
        started_at_raw: Any = run.get("started_at")
        if not isinstance(name_raw, str) or not name_raw:
            continue
        # Skip placeholder check runs created before CI actually starts.
        # The Jenkins github-checks plugin sets started_at only when the build
        # truly begins; an initial "ok" run has started_at=null.
        if not isinstance(started_at_raw, str) or not started_at_raw:
            continue
        conclusions[name_raw] = conclusion_raw if isinstance(conclusion_raw, str) else None

    return all(conclusions.get(req) == "success" for req in required)

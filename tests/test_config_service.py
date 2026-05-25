from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from integration.config_service import ConfigService
from integration.models import LabelMap, RepoModuleMap, UserMap


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def _make_session_factory(
    repo_modules: list[RepoModuleMap],
    label_maps: list[LabelMap],
    user_maps: list[UserMap],
) -> Any:
    def _execute_side_effect(stmt: Any) -> Any:
        table_name: str = stmt.get_final_froms()[0].name  # type: ignore[union-attr]
        if table_name == "repo_module_map":
            result = MagicMock()
            result.scalars.return_value = repo_modules
            return result
        if table_name == "label_map":
            result = MagicMock()
            result.scalars.return_value = label_maps
            return result
        result = MagicMock()
        result.scalars.return_value = user_maps
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute_side_effect)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = session
    return factory


def _rm(module_id: str, project_id: str, repo: str) -> RepoModuleMap:
    rm = RepoModuleMap()
    rm.plane_module_id = module_id
    rm.plane_project_id = project_id
    rm.gh_repo = repo
    return rm


def _lm(pid: str, label_id: str, repo: str, gh_label: str) -> LabelMap:
    lm = LabelMap()
    lm.plane_project_id = pid
    lm.plane_label_id = label_id
    lm.gh_repo = repo
    lm.gh_label = gh_label
    return lm


def _um(plane_user_id: str, gh_login: str, discord_user_id: str | None = None) -> UserMap:
    um = UserMap()
    um.plane_user_id = plane_user_id
    um.gh_login = gh_login
    um.discord_user_id = discord_user_id
    return um


def test_get_repo_module_found() -> None:
    rm = _rm("mod-1", "proj-1", "owner/repo")
    svc = ConfigService(_make_session_factory([rm], [], []))
    result = _run(svc.get_repo_module("mod-1"))
    assert result is rm


def test_get_repo_module_not_found() -> None:
    svc = ConfigService(_make_session_factory([], [], []))
    result = _run(svc.get_repo_module("nonexistent"))
    assert result is None


def test_get_label_map_found() -> None:
    lm = _lm("proj-1", "lbl-1", "owner/repo", "bug")
    svc = ConfigService(_make_session_factory([], [lm], []))
    result = _run(svc.get_label_map("proj-1", "lbl-1"))
    assert result is lm


def test_get_label_map_not_found() -> None:
    lm = _lm("proj-1", "lbl-1", "owner/repo", "bug")
    svc = ConfigService(_make_session_factory([], [lm], []))
    result = _run(svc.get_label_map("proj-1", "lbl-99"))
    assert result is None


def test_get_user_map_found() -> None:
    um = _um("user-1", "gh-user", "discord-123")
    svc = ConfigService(_make_session_factory([], [], [um]))
    result = _run(svc.get_user_map("user-1"))
    assert result is not None
    assert result is um
    assert result.discord_user_id == "discord-123"


def test_get_user_map_discord_nullable() -> None:
    um = _um("user-1", "gh-user")
    svc = ConfigService(_make_session_factory([], [], [um]))
    result = _run(svc.get_user_map("user-1"))
    assert result is not None
    assert result.discord_user_id is None


def test_invalidate_clears_cache() -> None:
    rm = _rm("mod-1", "proj-1", "owner/repo")
    factory = _make_session_factory([rm], [], [])
    svc = ConfigService(factory)

    _run(svc.get_repo_module("mod-1"))
    call_count_after_first_load = factory.call_count

    _run(svc.get_repo_module("mod-1"))
    assert factory.call_count == call_count_after_first_load, "cache not used on second call"

    svc.invalidate()
    _run(svc.get_repo_module("mod-1"))
    assert factory.call_count > call_count_after_first_load, "cache not reloaded after invalidate"


def test_cache_loaded_only_once_without_invalidate() -> None:
    factory = _make_session_factory([], [], [])
    svc = ConfigService(factory)

    for _ in range(5):
        _run(svc.get_repo_module("x"))

    assert factory.call_count == 1

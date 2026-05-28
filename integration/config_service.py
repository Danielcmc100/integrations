from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from integration.models import LabelMap, RepoModuleMap, UserMap


@dataclass
class _Cache:
    repo_modules: list[RepoModuleMap] = field(default_factory=list)
    label_maps: list[LabelMap] = field(default_factory=list)
    user_maps: list[UserMap] = field(default_factory=list)
    loaded: bool = False


class ConfigService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._cache: _Cache = _Cache()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def _load(self) -> None:
        async with self._session_factory() as session:
            repo_modules = list((await session.execute(select(RepoModuleMap))).scalars())
            label_maps = list((await session.execute(select(LabelMap))).scalars())
            user_maps = list((await session.execute(select(UserMap))).scalars())
        self._cache = _Cache(
            repo_modules=repo_modules,
            label_maps=label_maps,
            user_maps=user_maps,
            loaded=True,
        )

    async def _ensure_loaded(self) -> None:
        if not self._cache.loaded:
            async with self._lock:
                if not self._cache.loaded:
                    await self._load()

    def invalidate(self) -> None:
        self._cache = _Cache()

    async def get_repo_module(self, plane_module_id: str) -> RepoModuleMap | None:
        await self._ensure_loaded()
        for rm in self._cache.repo_modules:
            if rm.plane_module_id == plane_module_id:
                return rm
        return None

    async def get_label_map(
        self, plane_project_id: str, plane_label_id: str
    ) -> LabelMap | None:
        await self._ensure_loaded()
        for lm in self._cache.label_maps:
            if lm.plane_project_id == plane_project_id and lm.plane_label_id == plane_label_id:
                return lm
        return None

    async def get_user_map(self, plane_user_id: str) -> UserMap | None:
        await self._ensure_loaded()
        for um in self._cache.user_maps:
            if um.plane_user_id == plane_user_id:
                return um
        return None

    async def get_all_repo_modules(self) -> list[RepoModuleMap]:
        await self._ensure_loaded()
        return list(self._cache.repo_modules)

    async def get_repo_module_by_repo(self, gh_repo: str) -> RepoModuleMap | None:
        await self._ensure_loaded()
        for rm in self._cache.repo_modules:
            if rm.gh_repo == gh_repo:
                return rm
        return None

    async def get_label_map_by_gh(self, gh_repo: str, gh_label: str) -> LabelMap | None:
        await self._ensure_loaded()
        for lm in self._cache.label_maps:
            if lm.gh_repo == gh_repo and lm.gh_label == gh_label:
                return lm
        return None

    async def get_user_map_by_gh(self, gh_login: str) -> UserMap | None:
        await self._ensure_loaded()
        for um in self._cache.user_maps:
            if um.gh_login == gh_login:
                return um
        return None

from __future__ import annotations

import typing
from typing import Any

from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from integration.clients.github import GitHubClient
from integration.clients.plane import PlaneClient
from integration.config import settings
from integration.config_service import ConfigService
from integration.handlers.plane import process_plane_event


async def startup(ctx: dict[str, Any]) -> None:
    engine = create_async_engine(settings.database_url)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    ctx["session_factory"] = session_factory
    ctx["plane_client"] = PlaneClient(
        base_url=settings.plane_base_url,
        api_token=settings.plane_api_token,
        workspace=settings.plane_workspace,
    )
    ctx["github_client"] = GitHubClient(
        app_id=settings.github_app_id,
        private_key_pem=settings.github_app_private_key,
        base_url=settings.github_api_base_url,
        installation_id=settings.github_app_installation_id,
    )
    ctx["config_service"] = ConfigService(session_factory)


async def shutdown(ctx: dict[str, Any]) -> None:
    plane: PlaneClient = ctx["plane_client"]
    github: GitHubClient = ctx["github_client"]
    await plane.aclose()
    await github.aclose()


class WorkerSettings:
    functions: typing.ClassVar[list[object]] = [process_plane_event]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)

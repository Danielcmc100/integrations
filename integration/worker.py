from __future__ import annotations

import typing
from typing import Any, cast

import structlog
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from integration.clients.github import GitHubClient
from integration.clients.plane import PlaneClient
from integration.config import settings
from integration.config_service import ConfigService
from integration.discord_bot import DiscordBot
from integration.handlers.github import process_github_event
from integration.handlers.plane import process_plane_event
from integration.reminders import send_review_reminders

log = structlog.get_logger()


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

    if settings.discord_bot_token:
        discord_bot = DiscordBot(settings.discord_bot_token)
        await discord_bot.start()
        ctx["discord_bot"] = discord_bot
    else:
        ctx["discord_bot"] = None
        log.warning("discord_bot_token not set; Discord notifications disabled")


async def shutdown(ctx: dict[str, Any]) -> None:
    plane: PlaneClient = ctx["plane_client"]
    github: GitHubClient = ctx["github_client"]
    await plane.aclose()
    await github.aclose()
    discord_bot_raw: Any = ctx.get("discord_bot")
    if discord_bot_raw is not None:
        await cast(DiscordBot, discord_bot_raw).stop()


class WorkerSettings:
    functions: typing.ClassVar[list[object]] = [process_plane_event, process_github_event]
    cron_jobs: typing.ClassVar[list[object]] = [cron(send_review_reminders, minute=0)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)

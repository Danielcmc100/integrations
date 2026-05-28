from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from integration.clients.github import GitHubClient
from integration.clients.plane import PlaneClient
from integration.config import settings
from integration.config_service import ConfigService
from integration.db import SessionLocal
from integration.queue import ArqEnqueuer, Enqueuer


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


_enqueuer: Enqueuer | None = None


def get_enqueuer() -> Enqueuer:
    global _enqueuer
    if _enqueuer is None:
        _enqueuer = ArqEnqueuer(settings.redis_url)
    return _enqueuer


_config_service: ConfigService | None = None


def get_config_service() -> ConfigService:
    global _config_service
    if _config_service is None:
        _config_service = ConfigService(SessionLocal)
    return _config_service


SessionDep = Annotated[AsyncSession, Depends(get_session)]
EnqueuerDep = Annotated[Enqueuer, Depends(get_enqueuer)]
ConfigServiceDep = Annotated[ConfigService, Depends(get_config_service)]


_plane_client: PlaneClient | None = None
_github_client: GitHubClient | None = None


def get_plane_client() -> PlaneClient:
    global _plane_client
    if _plane_client is None:
        _plane_client = PlaneClient(
            base_url=settings.plane_base_url,
            api_token=settings.plane_api_token,
            workspace=settings.plane_workspace,
        )
    return _plane_client


def get_github_client() -> GitHubClient:
    global _github_client
    if _github_client is None:
        _github_client = GitHubClient(
            app_id=settings.github_app_id,
            private_key_pem=settings.github_app_private_key,
            base_url=settings.github_api_base_url,
            installation_id=settings.github_app_installation_id,
        )
    return _github_client


PlaneClientDep = Annotated[PlaneClient, Depends(get_plane_client)]
GitHubClientDep = Annotated[GitHubClient, Depends(get_github_client)]

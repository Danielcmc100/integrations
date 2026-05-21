from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from integration.config import settings
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


SessionDep = Annotated[AsyncSession, Depends(get_session)]
EnqueuerDep = Annotated[Enqueuer, Depends(get_enqueuer)]

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from integration.config import settings
from integration.deps import ConfigServiceDep, SessionDep
from integration.models import LabelMap, RepoModuleMap, UserMap

log = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin"])


def _check_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=401, detail="unauthorized")
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    token = authorization.removeprefix("Bearer ")
    if token != settings.admin_token:
        raise HTTPException(status_code=401, detail="unauthorized")


AdminAuth = Annotated[None, Depends(_check_admin)]


class RepoModuleIn(BaseModel):
    plane_module_id: str
    plane_project_id: str
    gh_repo: str


class RepoModuleUpdate(BaseModel):
    plane_project_id: str
    gh_repo: str


class RepoModuleOut(BaseModel):
    plane_module_id: str
    plane_project_id: str
    gh_repo: str

    model_config = {"from_attributes": True}


class LabelMapIn(BaseModel):
    plane_project_id: str
    plane_label_id: str
    gh_repo: str
    gh_label: str


class LabelMapUpdate(BaseModel):
    plane_project_id: str
    plane_label_id: str
    gh_repo: str
    gh_label: str


class LabelMapOut(BaseModel):
    id: int
    plane_project_id: str
    plane_label_id: str
    gh_repo: str
    gh_label: str

    model_config = {"from_attributes": True}


class UserMapIn(BaseModel):
    plane_user_id: str
    gh_login: str
    discord_user_id: str | None = None


class UserMapUpdate(BaseModel):
    plane_user_id: str
    gh_login: str
    discord_user_id: str | None = None


class UserMapOut(BaseModel):
    id: int
    plane_user_id: str
    gh_login: str
    discord_user_id: str | None

    model_config = {"from_attributes": True}


# --- /admin/repo-modules ---


@router.get("/repo-modules", response_model=list[RepoModuleOut])
async def list_repo_modules(_auth: AdminAuth, session: SessionDep) -> list[RepoModuleMap]:
    result = await session.execute(select(RepoModuleMap))
    return list(result.scalars())


@router.post("/repo-modules", response_model=RepoModuleOut, status_code=201)
async def create_repo_module(
    body: RepoModuleIn,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> RepoModuleMap:
    row = RepoModuleMap(
        plane_module_id=body.plane_module_id,
        plane_project_id=body.plane_project_id,
        gh_repo=body.gh_repo,
    )
    session.add(row)
    await session.commit()
    config_service.invalidate()
    return row


@router.put("/repo-modules/{plane_module_id}", response_model=RepoModuleOut)
async def update_repo_module(
    plane_module_id: str,
    body: RepoModuleUpdate,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> RepoModuleMap:
    row = await session.get(RepoModuleMap, plane_module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    row.plane_project_id = body.plane_project_id
    row.gh_repo = body.gh_repo
    await session.commit()
    config_service.invalidate()
    return row


@router.delete("/repo-modules/{plane_module_id}", status_code=204)
async def delete_repo_module(
    plane_module_id: str,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> None:
    row = await session.get(RepoModuleMap, plane_module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    await session.delete(row)
    await session.commit()
    config_service.invalidate()


# --- /admin/labels ---


@router.get("/labels", response_model=list[LabelMapOut])
async def list_labels(_auth: AdminAuth, session: SessionDep) -> list[LabelMap]:
    result = await session.execute(select(LabelMap))
    return list(result.scalars())


@router.post("/labels", response_model=LabelMapOut, status_code=201)
async def create_label(
    body: LabelMapIn,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> LabelMap:
    row = LabelMap(
        plane_project_id=body.plane_project_id,
        plane_label_id=body.plane_label_id,
        gh_repo=body.gh_repo,
        gh_label=body.gh_label,
    )
    session.add(row)
    await session.commit()
    config_service.invalidate()
    return row


@router.put("/labels/{label_id}", response_model=LabelMapOut)
async def update_label(
    label_id: int,
    body: LabelMapUpdate,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> LabelMap:
    row = await session.get(LabelMap, label_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    row.plane_project_id = body.plane_project_id
    row.plane_label_id = body.plane_label_id
    row.gh_repo = body.gh_repo
    row.gh_label = body.gh_label
    await session.commit()
    config_service.invalidate()
    return row


@router.delete("/labels/{label_id}", status_code=204)
async def delete_label(
    label_id: int,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> None:
    row = await session.get(LabelMap, label_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    await session.delete(row)
    await session.commit()
    config_service.invalidate()


# --- /admin/users ---


@router.get("/users", response_model=list[UserMapOut])
async def list_users(_auth: AdminAuth, session: SessionDep) -> list[UserMap]:
    result = await session.execute(select(UserMap))
    return list(result.scalars())


@router.post("/users", response_model=UserMapOut, status_code=201)
async def create_user(
    body: UserMapIn,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> UserMap:
    row = UserMap(
        plane_user_id=body.plane_user_id,
        gh_login=body.gh_login,
        discord_user_id=body.discord_user_id,
    )
    session.add(row)
    await session.commit()
    config_service.invalidate()
    return row


@router.put("/users/{user_id}", response_model=UserMapOut)
async def update_user(
    user_id: int,
    body: UserMapUpdate,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> UserMap:
    row = await session.get(UserMap, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    row.plane_user_id = body.plane_user_id
    row.gh_login = body.gh_login
    row.discord_user_id = body.discord_user_id
    await session.commit()
    config_service.invalidate()
    return row


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    _auth: AdminAuth,
    session: SessionDep,
    config_service: ConfigServiceDep,
) -> None:
    row = await session.get(UserMap, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    await session.delete(row)
    await session.commit()
    config_service.invalidate()

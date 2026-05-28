from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from integration.clients.plane import PlaneClient
from integration.models import StageMap, StageTrigger

log = structlog.get_logger()


async def apply_stage_trigger(
    plane_project_id: str,
    card_id: str,
    trigger: StageTrigger,
    *,
    session: AsyncSession,
    plane_client: PlaneClient,
) -> bool:
    result = await session.execute(
        select(StageMap).where(
            StageMap.plane_project_id == plane_project_id,
            StageMap.trigger == trigger,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        log.debug(
            "apply_stage_trigger: no mapping",
            plane_project_id=plane_project_id,
            trigger=trigger,
        )
        return False

    states = await plane_client.list_states(plane_project_id)
    state_id = next(
        (str(s["id"]) for s in states if str(s.get("name") or "") == row.plane_state_name),
        None,
    )
    if state_id is None:
        log.warning(
            "apply_stage_trigger: state name not found in Plane",
            plane_project_id=plane_project_id,
            trigger=trigger,
            plane_state_name=row.plane_state_name,
        )
        return False

    await plane_client.update_card(plane_project_id, card_id, {"state": state_id})
    log.info(
        "apply_stage_trigger: card state updated",
        plane_project_id=plane_project_id,
        card_id=card_id,
        trigger=trigger,
        plane_state_name=row.plane_state_name,
    )
    return True

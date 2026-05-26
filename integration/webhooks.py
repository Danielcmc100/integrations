import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from integration.config import settings
from integration.deps import EnqueuerDep, SessionDep
from integration.models import WebhookEventLog, WebhookSource, WebhookStatus

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

PLANE_DEDUPE_WINDOW = timedelta(minutes=5)


def _verify_github_signature(secret: str, signature_header: str | None, body: bytes) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _verify_plane_secret(expected_secret: str, provided_secret: str | None) -> bool:
    if not expected_secret or not provided_secret:
        return False
    return hmac.compare_digest(expected_secret, provided_secret)


@router.post("/github")
async def github_webhook(
    request: Request,
    session: SessionDep,
    enqueuer: EnqueuerDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str, Header()] = "unknown",
    x_github_delivery: Annotated[str, Header()] = "",
) -> dict[str, str]:
    body = await request.body()
    if not _verify_github_signature(settings.github_webhook_secret, x_hub_signature_256, body):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
    if not x_github_delivery:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="missing delivery id"
        )
    existing = await session.scalar(
        select(WebhookEventLog).where(
            WebhookEventLog.source == WebhookSource.github,
            WebhookEventLog.payload_hash == x_github_delivery,
        )
    )
    if existing is not None:
        return {"status": "duplicate"}
    log_id = uuid.uuid4()
    log = WebhookEventLog(
        id=log_id,
        source=WebhookSource.github,
        event_type=x_github_event,
        payload_hash=x_github_delivery,
        received_at=datetime.now(UTC),
        processed_at=None,
        status=WebhookStatus.pending,
    )
    session.add(log)
    await session.commit()
    await enqueuer.enqueue("process_github_event", str(log_id), body.decode())
    return {"status": "accepted"}


@router.post("/plane")
async def plane_webhook(
    request: Request,
    session: SessionDep,
    enqueuer: EnqueuerDep,
    x_plane_signature: Annotated[str | None, Header()] = None,
    x_plane_event: Annotated[str, Header()] = "unknown",
) -> dict[str, str]:
    body = await request.body()
    if not _verify_plane_secret(settings.plane_webhook_secret, x_plane_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
    payload_hash = hashlib.sha256(body).hexdigest()
    now = datetime.now(UTC)
    window_start = now - PLANE_DEDUPE_WINDOW
    existing = await session.scalar(
        select(WebhookEventLog).where(
            WebhookEventLog.source == WebhookSource.plane,
            WebhookEventLog.payload_hash == payload_hash,
            WebhookEventLog.received_at >= window_start,
        )
    )
    if existing is not None:
        return {"status": "duplicate"}
    log_id = uuid.uuid4()
    log = WebhookEventLog(
        id=log_id,
        source=WebhookSource.plane,
        event_type=x_plane_event,
        payload_hash=payload_hash,
        received_at=now,
        processed_at=None,
        status=WebhookStatus.pending,
    )
    session.add(log)
    await session.commit()
    await enqueuer.enqueue("process_plane_event", str(log_id), body.decode())
    return {"status": "accepted"}

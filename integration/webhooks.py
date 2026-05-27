import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from integration.config import settings
from integration.deps import EnqueuerDep, SessionDep
from integration.metrics import webhooks_received_total
from integration.models import WebhookEventLog, WebhookSource, WebhookStatus

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = structlog.get_logger()

PLANE_DEDUPE_WINDOW = timedelta(minutes=5)


def _verify_github_signature(secret: str, signature_header: str | None, body: bytes) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _verify_plane_signature(secret: str, signature_header: str | None, body: bytes) -> bool:
    if not secret or not signature_header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


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
    log.info(
        "webhook.received",
        source="github",
        event_type=x_github_event,
        delivery=x_github_delivery,
        body_bytes=len(body),
    )
    if not _verify_github_signature(settings.github_webhook_secret, x_hub_signature_256, body):
        log.warning("webhook.rejected", source="github", reason="invalid_signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
    webhooks_received_total.labels(source="github").inc()
    if not x_github_delivery:
        log.warning("webhook.rejected", source="github", reason="missing_delivery_id")
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
        log.info("webhook.duplicate", source="github", delivery=x_github_delivery)
        return {"status": "duplicate"}
    log_id = uuid.uuid4()
    event_log = WebhookEventLog(
        id=log_id,
        source=WebhookSource.github,
        event_type=x_github_event,
        payload_hash=x_github_delivery,
        received_at=datetime.now(UTC),
        processed_at=None,
        status=WebhookStatus.pending,
    )
    session.add(event_log)
    await session.commit()
    await enqueuer.enqueue("process_github_event", str(log_id), body.decode())
    log.info(
        "webhook.enqueued",
        source="github",
        log_id=str(log_id),
        event_type=x_github_event,
    )
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
    log.info(
        "webhook.received",
        source="plane",
        event_type=x_plane_event,
        body_bytes=len(body),
    )
    if not _verify_plane_signature(settings.plane_webhook_secret, x_plane_signature, body):
        log.warning("webhook.rejected", source="plane", reason="invalid_signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
    webhooks_received_total.labels(source="plane").inc()
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
        log.info("webhook.duplicate", source="plane", payload_hash=payload_hash[:16])
        return {"status": "duplicate"}
    log_id = uuid.uuid4()
    event_log = WebhookEventLog(
        id=log_id,
        source=WebhookSource.plane,
        event_type=x_plane_event,
        payload_hash=payload_hash,
        received_at=now,
        processed_at=None,
        status=WebhookStatus.pending,
    )
    session.add(event_log)
    await session.commit()
    await enqueuer.enqueue("process_plane_event", str(log_id), body.decode())
    log.info(
        "webhook.enqueued",
        source="plane",
        log_id=str(log_id),
        event_type=x_plane_event,
    )
    return {"status": "accepted"}

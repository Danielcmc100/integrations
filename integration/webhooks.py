import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from integration.config import settings
from integration.deps import EnqueuerDep, SessionDep
from integration.models import WebhookEventLog, WebhookSource, WebhookStatus

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_github_signature(secret: str, signature_header: str | None, body: bytes) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
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
    await enqueuer.enqueue("process_github_event", str(log_id))
    return {"status": "accepted"}

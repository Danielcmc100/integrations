import hashlib
import hmac
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from integration.config import settings
from integration.deps import get_enqueuer, get_session
from integration.models import WebhookEventLog, WebhookSource
from main import app

SECRET = "testsecret-plane"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits: int = 0
        self.existing: WebhookEventLog | None = None
        self.scalar_calls: list[Any] = []

    async def scalar(self, stmt: Any) -> WebhookEventLog | None:
        self.scalar_calls.append(stmt)
        return self.existing

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class FakeEnqueuer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((function, args, kwargs))


@pytest.fixture(autouse=True)
def _set_secret() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    previous = settings.plane_webhook_secret
    settings.plane_webhook_secret = SECRET
    yield
    settings.plane_webhook_secret = previous


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def fake_enqueuer() -> FakeEnqueuer:
    return FakeEnqueuer()


@pytest.fixture
def client(fake_session: FakeSession, fake_enqueuer: FakeEnqueuer) -> Iterator[TestClient]:
    async def _session_override() -> AsyncIterator[FakeSession]:
        yield fake_session

    def _enqueuer_override() -> FakeEnqueuer:
        return fake_enqueuer

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_enqueuer] = _enqueuer_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_valid_secret_persists_and_enqueues(
    client: TestClient, fake_session: FakeSession, fake_enqueuer: FakeEnqueuer
) -> None:
    body = b'{"event":"card.created","data":{"id":"abc"}}'
    headers = {
        "X-Plane-Signature": _sign(body),
        "X-Plane-Event": "card.created",
        "Content-Type": "application/json",
    }
    resp = client.post("/webhooks/plane", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert len(fake_session.added) == 1
    log = fake_session.added[0]
    assert isinstance(log, WebhookEventLog)
    assert log.source == WebhookSource.plane
    assert log.event_type == "card.created"
    assert log.payload_hash == hashlib.sha256(body).hexdigest()
    assert log.status.value == "pending"
    assert fake_session.commits == 1
    assert len(fake_enqueuer.calls) == 1
    fn, args, _ = fake_enqueuer.calls[0]
    assert fn == "process_plane_event"
    assert args[0] == str(log.id)


def test_invalid_secret_returns_401(
    client: TestClient, fake_session: FakeSession, fake_enqueuer: FakeEnqueuer
) -> None:
    body = b"{}"
    headers = {
        "X-Plane-Signature": "wrong-secret",
        "X-Plane-Event": "card.created",
        "Content-Type": "application/json",
    }
    resp = client.post("/webhooks/plane", content=body, headers=headers)
    assert resp.status_code == 401
    assert fake_session.added == []
    assert fake_session.commits == 0
    assert fake_enqueuer.calls == []


def test_missing_secret_returns_401(
    client: TestClient, fake_session: FakeSession, fake_enqueuer: FakeEnqueuer
) -> None:
    body = b"{}"
    headers = {
        "X-Plane-Event": "card.created",
        "Content-Type": "application/json",
    }
    resp = client.post("/webhooks/plane", content=body, headers=headers)
    assert resp.status_code == 401
    assert fake_enqueuer.calls == []


def test_duplicate_payload_within_window_returns_200_without_enqueue(
    client: TestClient, fake_session: FakeSession, fake_enqueuer: FakeEnqueuer
) -> None:
    fake_session.existing = WebhookEventLog(
        received_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    body = b'{"event":"card.updated"}'
    headers = {
        "X-Plane-Signature": _sign(body),
        "X-Plane-Event": "card.updated",
        "Content-Type": "application/json",
    }
    resp = client.post("/webhooks/plane", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "duplicate"}
    assert fake_session.added == []
    assert fake_session.commits == 0
    assert fake_enqueuer.calls == []

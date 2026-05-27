from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from prometheus_client import Counter, Gauge, Histogram

from integration.logging_config import configure_logging
from integration.metrics import (
    REGISTRY,
    arq_queue_depth,
    sync_actions_total,
    sync_duration_seconds,
    webhooks_received_total,
)
from main import app


def test_metrics_objects_are_correct_types() -> None:
    assert isinstance(webhooks_received_total, Counter)
    assert isinstance(sync_actions_total, Counter)
    assert isinstance(sync_duration_seconds, Histogram)
    assert isinstance(arq_queue_depth, Gauge)


def test_metrics_endpoint_returns_200() -> None:
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_metrics_endpoint_contains_metric_names() -> None:
    from prometheus_client import generate_latest

    output = generate_latest(REGISTRY).decode()
    assert "webhooks_received_total" in output
    assert "sync_actions_total" in output
    assert "sync_duration_seconds" in output
    assert "arq_queue_depth" in output


def test_configure_logging_does_not_raise() -> None:
    import logging

    configure_logging(level=logging.DEBUG)
    configure_logging()


def test_webhook_github_increments_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    from integration import webhooks
    from integration.config import settings

    monkeypatch.setattr(settings, "github_webhook_secret", "secret")

    incremented: list[str] = []

    class FakeCounter:
        def labels(self, *, source: str) -> FakeCounter:
            incremented.append(source)
            return self

        def inc(self) -> None:
            pass

    monkeypatch.setattr(webhooks, "webhooks_received_total", FakeCounter())

    import hashlib
    import hmac

    body = b'{"action": "opened"}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    fake_session: Any = MagicMock()
    fake_session.scalar = MagicMock(return_value=None)
    fake_session.__aenter__ = MagicMock(return_value=fake_session)
    fake_session.__aexit__ = MagicMock(return_value=None)
    fake_session.add = MagicMock()

    async def fake_scalar(_stmt: Any) -> None:
        return None

    async def fake_commit() -> None:
        pass

    fake_session.scalar = fake_scalar
    fake_session.commit = fake_commit

    fake_enqueuer: Any = MagicMock()

    async def fake_enqueue(*args: Any, **kwargs: Any) -> None:
        pass

    fake_enqueuer.enqueue = fake_enqueue

    from integration.deps import get_enqueuer, get_session
    from main import app

    app.dependency_overrides[get_session] = lambda: fake_session
    app.dependency_overrides[get_enqueuer] = lambda: fake_enqueuer
    try:
        client = TestClient(app)
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-001",
            },
        )
        assert resp.status_code == 200
        assert "github" in incremented
    finally:
        app.dependency_overrides.clear()


def test_sync_actions_counter_labels_correct() -> None:
    sync_actions_total.labels(type="card.created", outcome="success").inc()
    sync_actions_total.labels(type="issue.opened", outcome="error").inc()
    output = b""
    from prometheus_client import generate_latest

    output = generate_latest(REGISTRY)
    assert b'type="card.created"' in output or b"card.created" in output


def test_sync_duration_histogram_observe() -> None:
    sync_duration_seconds.labels(type="card.updated").observe(0.123)
    from prometheus_client import generate_latest

    output = generate_latest(REGISTRY)
    assert b"sync_duration_seconds" in output


def test_arq_queue_depth_set() -> None:
    arq_queue_depth.set(5)
    from prometheus_client import generate_latest

    output = generate_latest(REGISTRY)
    assert b"arq_queue_depth" in output

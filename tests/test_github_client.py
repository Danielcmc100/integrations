import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from integration.clients.github import GitHubClient


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def rsa_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode()


def _make_client(
    rsa_key_pem: str,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    installation_id: int | None = 99,
    sleeps: list[float] | None = None,
    now: float = 1_700_000_000.0,
) -> GitHubClient:
    http = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )

    async def _sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return GitHubClient(
        app_id="12345",
        private_key_pem=rsa_key_pem,
        base_url="https://api.github.test",
        installation_id=installation_id,
        client=http,
        sleep=_sleep,
        time_fn=lambda: now,
    )


def test_installation_token_fetched_and_cached(rsa_key_pem: str) -> None:
    token_calls: list[int] = []
    api_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            token_calls.append(1)
            auth = request.headers["Authorization"]
            assert auth.startswith("Bearer ")
            return httpx.Response(
                201,
                json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"},
            )
        api_calls.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"number": 1, "title": "x"})

    client = _make_client(rsa_key_pem, handler)
    try:
        issue1 = _run(client.get_issue("o", "r", 1))
        issue2 = _run(client.get_issue("o", "r", 1))
    finally:
        _run(client.aclose())

    assert issue1 == {"number": 1, "title": "x"}
    assert issue2 == issue1
    assert len(token_calls) == 1
    assert api_calls == ["token ghs_test", "token ghs_test"]


def test_jwt_signed_with_rs256_and_app_id(rsa_key_pem: str) -> None:
    captured_jwt: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            captured_jwt.append(request.headers["Authorization"].removeprefix("Bearer "))
            return httpx.Response(
                201, json={"token": "tok", "expires_at": "2099-01-01T00:00:00Z"}
            )
        return httpx.Response(200, json={"number": 1})

    client = _make_client(rsa_key_pem, handler)
    try:
        _run(client.get_issue("o", "r", 1))
    finally:
        _run(client.aclose())

    pub = (
        serialization.load_pem_private_key(rsa_key_pem.encode(), password=None)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    decoded: Any = jwt.decode(
        captured_jwt[0], pub, algorithms=["RS256"], options={"verify_exp": False}
    )
    assert decoded["iss"] == "12345"
    assert "iat" in decoded and "exp" in decoded


def test_create_issue_posts_payload(rsa_key_pem: str) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"}
            )
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"number": 42})

    client = _make_client(rsa_key_pem, handler)
    try:
        result = _run(client.create_issue("o", "r", {"title": "T", "body": "B"}))
    finally:
        _run(client.aclose())

    assert result == {"number": 42}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/repos/o/r/issues")
    assert captured["body"] == {"title": "T", "body": "B"}


def test_close_issue_sets_state(rsa_key_pem: str) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"}
            )
        captured["body"] = json.loads(request.content.decode())
        captured["method"] = request.method
        return httpx.Response(200, json={"number": 5, "state": "closed"})

    client = _make_client(rsa_key_pem, handler)
    try:
        result = _run(client.close_issue("o", "r", 5, state_reason="completed"))
    finally:
        _run(client.aclose())

    assert result["state"] == "closed"
    assert captured["method"] == "PATCH"
    assert captured["body"] == {"state": "closed", "state_reason": "completed"}


def test_list_check_runs_returns_runs(rsa_key_pem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"}
            )
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "check_runs": [
                    {"name": "ci", "conclusion": "success"},
                    {"name": "lint", "conclusion": "failure"},
                ],
            },
        )

    client = _make_client(rsa_key_pem, handler)
    try:
        runs = _run(client.list_check_runs("o", "r", "abc123"))
    finally:
        _run(client.aclose())

    assert [r["name"] for r in runs] == ["ci", "lint"]


def test_get_branch_protection(rsa_key_pem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"}
            )
        assert request.url.path == "/repos/o/r/branches/main/protection"
        return httpx.Response(
            200,
            json={"required_status_checks": {"contexts": ["ci", "lint"]}},
        )

    client = _make_client(rsa_key_pem, handler)
    try:
        bp = _run(client.get_branch_protection("o", "r", "main"))
    finally:
        _run(client.aclose())

    assert bp["required_status_checks"]["contexts"] == ["ci", "lint"]


def test_429_triggers_sleep_and_retry(rsa_key_pem: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"}
            )
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "4"}, json={})
        return httpx.Response(200, json={"number": 1})

    sleeps: list[float] = []
    client = _make_client(rsa_key_pem, handler, sleeps=sleeps)
    try:
        _run(client.get_issue("o", "r", 1))
    finally:
        _run(client.aclose())

    assert sleeps == [4.0]
    assert len(calls) == 2


def test_secondary_rate_limit_403_with_zero_remaining(rsa_key_pem: str) -> None:
    state: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"}
            )
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1700000010",
                },
                json={"message": "rate limited"},
            )
        return httpx.Response(200, json={"number": 1})

    sleeps: list[float] = []
    client = _make_client(rsa_key_pem, handler, sleeps=sleeps, now=1_700_000_000.0)
    try:
        _run(client.get_issue("o", "r", 1))
    finally:
        _run(client.aclose())

    assert sleeps == [10.0]


def test_installation_discovery_when_id_not_provided(rsa_key_pem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 77}])
        if request.url.path == "/app/installations/77/access_tokens":
            return httpx.Response(
                201, json={"token": "tok77", "expires_at": "2099-01-01T00:00:00Z"}
            )
        return httpx.Response(200, json={"number": 1})

    client = _make_client(rsa_key_pem, handler, installation_id=None)
    try:
        _run(client.get_issue("o", "r", 1))
    finally:
        _run(client.aclose())

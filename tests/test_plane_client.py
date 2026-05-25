import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from integration.clients.plane import PlaneClient


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleeps: list[float] | None = None,
) -> PlaneClient:
    http = httpx.AsyncClient(
        base_url="https://plane.test/api/v1",
        headers={"X-API-Key": "token-xyz", "Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )

    async def _sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return PlaneClient(
        base_url="https://plane.test/api/v1",
        api_token="token-xyz",
        workspace="ws-1",
        client=http,
        sleep=_sleep,
    )


def test_get_card_uses_workspace_and_token() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("X-API-Key")
        return httpx.Response(200, json={"id": "card-1", "name": "Hi"})

    client = _make_client(handler)
    try:
        card = _run(client.get_card("proj-1", "card-1"))
    finally:
        _run(client.aclose())

    assert card == {"id": "card-1", "name": "Hi"}
    assert (
        captured["url"]
        == "https://plane.test/api/v1/workspaces/ws-1/projects/proj-1/issues/card-1/"
    )
    assert captured["auth"] == "token-xyz"


def test_create_card_posts_json() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": "new-card"})

    client = _make_client(handler)
    try:
        result = _run(client.create_card("proj-1", {"name": "T", "description_html": "d"}))
    finally:
        _run(client.aclose())

    assert result == {"id": "new-card"}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/issues/")
    assert captured["body"] == {"name": "T", "description_html": "d"}


def test_update_card_patches() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "card-1", "state": "Done"})

    client = _make_client(handler)
    try:
        result = _run(client.update_card("proj-1", "card-1", {"state": "Done"}))
    finally:
        _run(client.aclose())

    assert result["state"] == "Done"
    assert captured["method"] == "PATCH"


def test_add_comment_posts_html() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": "c-1"})

    client = _make_client(handler)
    try:
        result = _run(client.add_comment("proj-1", "card-1", "<p>hi</p>"))
    finally:
        _run(client.aclose())

    assert result == {"id": "c-1"}
    assert captured["body"] == {"comment_html": "<p>hi</p>"}


def test_list_states_handles_paginated_results() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "s1", "name": "Backlog"},
                    {"id": "s2", "name": "Done"},
                ]
            },
        )

    client = _make_client(handler)
    try:
        states = _run(client.list_states("proj-1"))
    finally:
        _run(client.aclose())

    assert [s["name"] for s in states] == ["Backlog", "Done"]


def test_list_modules_handles_array_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "m1", "name": "API"}])

    client = _make_client(handler)
    try:
        modules = _run(client.list_modules("proj-1"))
    finally:
        _run(client.aclose())

    assert modules == [{"id": "m1", "name": "API"}]


def test_429_triggers_sleep_and_retry() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json={"id": "card-1"})

    sleeps: list[float] = []
    client = _make_client(handler, sleeps=sleeps)
    try:
        result = _run(client.get_card("proj-1", "card-1"))
    finally:
        _run(client.aclose())

    assert result == {"id": "card-1"}
    assert sleeps == [2.0]
    assert len(calls) == 2


def test_zero_remaining_header_sleeps_after_success() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "3"},
            json={"id": "card-1"},
        )

    sleeps: list[float] = []
    client = _make_client(handler, sleeps=sleeps)
    try:
        _run(client.get_card("proj-1", "card-1"))
    finally:
        _run(client.aclose())

    assert sleeps == [3.0]

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from integration.models import DeadLetter
from integration.retry import DeadLetteredError, insert_dead_letter, run_with_retry


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake session infrastructure
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.committed = False

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True


@contextlib.asynccontextmanager
async def _session_ctx(session: FakeSession) -> AsyncGenerator[FakeSession, None]:
    yield session


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def __call__(self) -> Any:
        return _session_ctx(self._session)


def _make_ctx(
    session: FakeSession | None = None,
    discord_bot: Any = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    if session is not None:
        ctx["session_factory"] = FakeSessionFactory(session)
    if discord_bot is not None:
        ctx["discord_bot"] = discord_bot
    return ctx


def _transient_transport_error() -> httpx.TransportError:
    return httpx.ConnectError("connection refused")


def _transient_5xx() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://example.com")
    resp = httpx.Response(503, request=req)
    return httpx.HTTPStatusError("503", request=req, response=resp)


# ---------------------------------------------------------------------------
# run_with_retry tests
# ---------------------------------------------------------------------------


def test_success_first_try_no_sleep() -> None:
    calls: list[int] = []
    slept: list[float] = []

    async def fn() -> None:
        calls.append(1)

    async def sleep_fn(s: float) -> None:
        slept.append(s)

    _run(
        run_with_retry(
            fn,
            ctx={},
            source="plane",
            event_type="card.created",
            payload_json="{}",
            sleep_fn=sleep_fn,
        )
    )

    assert len(calls) == 1
    assert slept == []


def test_non_transient_error_raises_immediately() -> None:
    calls: list[int] = []
    slept: list[float] = []

    async def fn() -> None:
        calls.append(1)
        raise ValueError("permanent")

    async def sleep_fn(s: float) -> None:
        slept.append(s)

    with pytest.raises(ValueError, match="permanent"):
        _run(
            run_with_retry(
                fn,
                ctx={},
                source="plane",
                event_type="card.created",
                payload_json="{}",
                sleep_fn=sleep_fn,
            )
        )

    assert len(calls) == 1
    assert slept == []


def test_transient_error_retries_and_succeeds() -> None:
    calls: list[int] = []
    slept: list[float] = []

    async def fn() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise _transient_transport_error()

    async def sleep_fn(s: float) -> None:
        slept.append(s)

    _run(
        run_with_retry(
            fn,
            ctx={},
            source="github",
            event_type="issues.opened",
            payload_json="{}",
            sleep_fn=sleep_fn,
        )
    )

    assert len(calls) == 3
    assert slept == [1.0, 2.0]


def test_5xx_error_retries() -> None:
    calls: list[int] = []
    slept: list[float] = []

    async def fn() -> None:
        calls.append(1)
        if len(calls) < 2:
            raise _transient_5xx()

    async def sleep_fn(s: float) -> None:
        slept.append(s)

    _run(
        run_with_retry(
            fn,
            ctx={},
            source="github",
            event_type="issues.opened",
            payload_json="{}",
            sleep_fn=sleep_fn,
        )
    )

    assert len(calls) == 2
    assert slept == [1.0]


def test_max_retries_exhausted_raises_dead_lettered(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    inserted: list[tuple[str, str, str]] = []

    async def fn() -> None:
        raise _transient_transport_error()

    async def sleep_fn(s: float) -> None:
        slept.append(s)

    async def fake_insert(
        ctx: dict[str, Any],
        source: str,
        event_type: str,
        payload_json: str,
        last_error: str,
    ) -> None:
        inserted.append((source, event_type, last_error))

    import integration.retry as retry_mod

    monkeypatch.setattr(retry_mod, "insert_dead_letter", fake_insert)

    with pytest.raises(DeadLetteredError):
        _run(
            run_with_retry(
                fn,
                ctx={},
                source="plane",
                event_type="card.updated",
                payload_json='{"id": "abc"}',
                sleep_fn=sleep_fn,
            )
        )

    # 5 retries -> 5 sleeps (1,2,4,8,16) + 1 final failed attempt
    assert len(slept) == 5
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert len(inserted) == 1
    assert inserted[0][0] == "plane"
    assert inserted[0][1] == "card.updated"


def test_dead_lettered_error_propagates_without_re_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted: list[int] = []

    async def fn() -> None:
        raise DeadLetteredError("already dead")

    import integration.retry as retry_mod

    async def fake_insert(*args: Any, **kwargs: Any) -> None:
        inserted.append(1)

    monkeypatch.setattr(retry_mod, "insert_dead_letter", fake_insert)

    with pytest.raises(DeadLetteredError):
        _run(
            run_with_retry(
                fn,
                ctx={},
                source="plane",
                event_type="card.created",
                payload_json="{}",
            )
        )

    assert inserted == []


# ---------------------------------------------------------------------------
# insert_dead_letter tests
# ---------------------------------------------------------------------------


def test_insert_dead_letter_writes_row() -> None:
    session = FakeSession()
    ctx = _make_ctx(session=session)

    _run(
        insert_dead_letter(
            ctx,
            source="plane",
            event_type="card.created",
            payload_json='{"id": "x"}',
            last_error="ConnectError",
        )
    )

    assert session.committed
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, DeadLetter)
    assert row.source == "plane"
    assert row.event_type == "card.created"
    assert row.payload == '{"id": "x"}'
    assert row.last_error == "ConnectError"
    assert isinstance(row.created_at, datetime)
    assert row.created_at.tzinfo is UTC


def test_insert_dead_letter_no_session_factory_no_crash() -> None:
    ctx: dict[str, Any] = {}
    _run(
        insert_dead_letter(
            ctx,
            source="github",
            event_type="issues.opened",
            payload_json="{}",
            last_error="timeout",
        )
    )


def test_insert_dead_letter_posts_discord_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integration import config

    monkeypatch.setattr(config.settings, "discord_ops_channel_id", "ops-channel-123")

    discord_bot = MagicMock()
    discord_bot.post_message = AsyncMock()

    session = FakeSession()
    ctx = _make_ctx(session=session, discord_bot=discord_bot)

    _run(
        insert_dead_letter(
            ctx,
            source="plane",
            event_type="card.created",
            payload_json="{}",
            last_error="timeout error",
        )
    )

    discord_bot.post_message.assert_called_once()
    call_args = discord_bot.post_message.call_args
    assert call_args.args[0] == "ops-channel-123"
    assert "plane/card.created" in call_args.args[1]
    assert "timeout error" in call_args.args[1]


def test_insert_dead_letter_no_discord_bot_no_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integration import config

    monkeypatch.setattr(config.settings, "discord_ops_channel_id", "ops-channel-123")

    session = FakeSession()
    ctx = _make_ctx(session=session)

    _run(
        insert_dead_letter(
            ctx,
            source="plane",
            event_type="card.created",
            payload_json="{}",
            last_error="err",
        )
    )

    assert session.committed


def test_insert_dead_letter_no_ops_channel_skips_discord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integration import config

    monkeypatch.setattr(config.settings, "discord_ops_channel_id", "")

    discord_bot = MagicMock()
    discord_bot.post_message = AsyncMock()

    session = FakeSession()
    ctx = _make_ctx(session=session, discord_bot=discord_bot)

    _run(
        insert_dead_letter(
            ctx,
            source="github",
            event_type="issues.opened",
            payload_json="{}",
            last_error="err",
        )
    )

    discord_bot.post_message.assert_not_called()


def test_insert_dead_letter_truncates_long_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integration import config

    monkeypatch.setattr(config.settings, "discord_ops_channel_id", "ops-123")

    discord_bot = MagicMock()
    discord_bot.post_message = AsyncMock()

    long_error = "x" * 300
    ctx = _make_ctx(discord_bot=discord_bot)

    _run(
        insert_dead_letter(
            ctx,
            source="plane",
            event_type="card.updated",
            payload_json="{}",
            last_error=long_error,
        )
    )

    message: str = discord_bot.post_message.call_args.args[1]
    assert "..." in message
    assert len(message) < 300 + 100


def test_insert_dead_letter_discord_failure_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integration import config

    monkeypatch.setattr(config.settings, "discord_ops_channel_id", "ops-123")

    discord_bot = MagicMock()
    discord_bot.post_message = AsyncMock(side_effect=RuntimeError("network down"))

    session = FakeSession()
    ctx = _make_ctx(session=session, discord_bot=discord_bot)

    _run(
        insert_dead_letter(
            ctx,
            source="plane",
            event_type="card.created",
            payload_json="{}",
            last_error="err",
        )
    )

    assert session.committed

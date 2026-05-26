from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

MAX_RETRIES = 5
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)

SleepFn = Callable[[float], Awaitable[None]]


class DeadLetteredError(Exception):
    pass


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


async def _insert_dead_letter(
    ctx: dict[str, Any],
    source: str,
    event_type: str,
    payload_json: str,
    last_error: str,
) -> None:
    from integration.models import DeadLetter

    session_factory = ctx.get("session_factory")
    if session_factory is not None:
        record = DeadLetter(
            id=uuid.uuid4(),
            source=source,
            event_type=event_type,
            payload=payload_json,
            last_error=last_error,
            created_at=datetime.now(UTC),
        )
        async with session_factory() as session:
            session.add(record)
            await session.commit()

    log.error(
        "dead_letter created",
        source=source,
        event_type=event_type,
        last_error=last_error,
    )

    discord_bot_raw: Any = ctx.get("discord_bot")
    if discord_bot_raw is None:
        return

    from integration.config import settings

    ops_channel = settings.discord_ops_channel_id
    if not ops_channel:
        return

    summary = (last_error[:200] + "...") if len(last_error) > 200 else last_error
    message = f"**Dead letter** `{source}/{event_type}`\n```\n{summary}\n```"
    try:
        await discord_bot_raw.post_message(ops_channel, message)
    except Exception as notify_err:
        log.warning("dead_letter: discord notify failed", error=str(notify_err))


async def run_with_retry(
    fn: Callable[[], Awaitable[None]],
    *,
    ctx: dict[str, Any],
    source: str,
    event_type: str,
    payload_json: str,
    sleep_fn: SleepFn = asyncio.sleep,
) -> None:
    last_error = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            await fn()
            return
        except DeadLetteredError:
            raise
        except BaseException as exc:
            if not _is_transient(exc):
                raise
            last_error = repr(exc)
            log.warning(
                "transient error, retrying",
                source=source,
                event_type=event_type,
                attempt=attempt,
                error=last_error,
            )
            if attempt < MAX_RETRIES:
                await sleep_fn(BACKOFF_SECONDS[attempt])
            else:
                await _insert_dead_letter(ctx, source, event_type, payload_json, last_error)
                raise DeadLetteredError(
                    f"dead-lettered after {MAX_RETRIES} retries: {last_error}"
                ) from exc

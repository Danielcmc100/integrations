"""Discord bot service for posting PR review notifications."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol

import discord
import discord.ui
import structlog

log = structlog.get_logger()


class DiscordBotProtocol(Protocol):
    async def post_review_message(
        self,
        channel_id: str,
        embed: discord.Embed,
        *,
        view: discord.ui.View | None = None,
    ) -> str: ...

    async def create_thread(self, message_id: str, channel_id: str, name: str) -> str: ...

    async def post_thread_message(self, thread_id: str, content: str) -> None: ...

    async def archive_thread(self, thread_id: str) -> None: ...


class DiscordBot:
    def __init__(self, token: str) -> None:
        self._token = token
        self._client = discord.Client(intents=discord.Intents.default())
        self._ready_event: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        @self._client.event
        async def on_ready() -> None:  # pyright: ignore[reportUnusedFunction]
            log.info("discord_bot: ready", user=str(self._client.user))
            self._ready_event.set()

    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(
            self._client.start(self._token)
        )
        await asyncio.wait_for(self._ready_event.wait(), timeout=30.0)

    async def stop(self) -> None:
        await self._client.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _get_text_channel(self, channel_id: str) -> discord.TextChannel:
        ch = self._client.get_channel(int(channel_id))
        if not isinstance(ch, discord.TextChannel):
            raw: Any = await self._client.fetch_channel(int(channel_id))
            if not isinstance(raw, discord.TextChannel):
                raise RuntimeError(f"channel {channel_id!r} is not a TextChannel")
            ch = raw
        return ch

    async def post_review_message(
        self,
        channel_id: str,
        embed: discord.Embed,
        *,
        view: discord.ui.View | None = None,
    ) -> str:
        await self._ready_event.wait()
        ch = await self._get_text_channel(channel_id)
        if view is not None:
            msg = await ch.send(embed=embed, view=view)
        else:
            msg = await ch.send(embed=embed)
        return str(msg.id)

    async def create_thread(self, message_id: str, channel_id: str, name: str) -> str:
        await self._ready_event.wait()
        ch = await self._get_text_channel(channel_id)
        msg = await ch.fetch_message(int(message_id))
        thread = await msg.create_thread(name=name)
        return str(thread.id)

    async def post_thread_message(self, thread_id: str, content: str) -> None:
        await self._ready_event.wait()
        raw: Any = await self._client.fetch_channel(int(thread_id))
        if not isinstance(raw, discord.Thread):
            raise RuntimeError(f"channel {thread_id!r} is not a Thread")
        await raw.send(content)

    async def archive_thread(self, thread_id: str) -> None:
        await self._ready_event.wait()
        raw: Any = await self._client.fetch_channel(int(thread_id))
        if not isinstance(raw, discord.Thread):
            raise RuntimeError(f"channel {thread_id!r} is not a Thread")
        await raw.edit(archived=True)

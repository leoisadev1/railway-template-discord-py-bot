"""Isolated CLI gateway mock; never authenticates with Discord."""

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
import discord


async def start(self, token):
    if os.getenv("TEST_PROVIDER_MODE") == "invalid":
        raise discord.LoginFailure("401 synthetic provider rejection")
    if os.getenv("TEST_PROVIDER_MODE") == "intents":
        raise discord.PrivilegedIntentsRequired(None)
    if os.getenv("TEST_PROVIDER_MODE") == "identify-4004":
        raise discord.ConnectionClosed(SimpleNamespace(close_code=4004), shard_id=None)
    if os.getenv("TEST_PROVIDER_MODE") == "sync-401":
        raise discord.HTTPException(SimpleNamespace(status=401, reason="Unauthorized"), "401: Unauthorized")
    if os.getenv("TEST_PROVIDER_MODE") == "sync-403":
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "403: Missing Access")
    if os.getenv("TEST_PROVIDER_MODE") == "outage":
        raise OSError("synthetic gateway outage")
    self.commands_synced = True
    self._ready.set()
    await self.on_ready()
    print("MOCK_READY", flush=True)
    await asyncio.Event().wait()


bot.Bot.start = start
raise SystemExit(bot.main())

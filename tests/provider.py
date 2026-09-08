"""Isolated CLI gateway mock; never authenticates with Discord."""

import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
import discord


async def start(self, token):
    if os.getenv("TEST_PROVIDER_MODE") == "invalid":
        raise discord.LoginFailure("401 synthetic provider rejection")
    self.commands_synced = True
    self._ready.set()
    await self.on_ready()
    print("MOCK_READY", flush=True)
    await asyncio.Event().wait()


bot.Bot.start = start
raise SystemExit(bot.main())

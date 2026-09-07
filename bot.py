"""discord.py worker: slash commands plus a Railway-only /health endpoint."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import discord
from discord import app_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

HEALTH_BODY = b'{"status":"ok"}\n'


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    # Privileged intents. Turn them on in the Discord Developer Portal first,
    # then set the matching env var. Slash commands work with defaults.
    intents.message_content = env_flag("MESSAGE_CONTENT_INTENT")
    intents.members = env_flag("MEMBERS_INTENT")
    intents.presences = env_flag("PRESENCES_INTENT")
    return intents


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/healthz"}:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(HEALTH_BODY)))
            self.end_headers()
            self.wfile.write(HEALTH_BODY)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def start_health_server() -> ThreadingHTTPServer:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True, name="healthcheck")
    thread.start()
    log.info("healthcheck listening on 0.0.0.0:%s/health", port)
    return server


def wait_forever() -> None:
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return


class Bot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=build_intents())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %s slash command(s) to guild %s", len(synced), guild_id)
            return
        synced = await self.tree.sync()
        log.info("synced %s global slash command(s)", len(synced))

    async def on_ready(self) -> None:
        user = self.user
        log.info("logged in as %s (%s)", user, getattr(user, "id", "?"))


bot = Bot()


@bot.tree.command(name="ping", description="Check that the bot is alive")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! `{latency_ms}ms`")


@bot.tree.command(name="info", description="Show bot and library versions")
async def info(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"discord.py `{discord.__version__}` · `{bot.user}`",
        ephemeral=True,
    )


async def run_discord(token: str) -> None:
    try:
        async with bot:
            await bot.start(token)
    except discord.LoginFailure:
        log.error(
            "DISCORD_TOKEN was rejected by Discord. "
            "The /health endpoint stays up so Railway does not crash-loop. "
            "Create a bot token at https://discord.com/developers/applications "
            "and set DISCORD_TOKEN, then redeploy."
        )
        await asyncio.Event().wait()
    except discord.PrivilegedIntentsRequired as exc:
        log.error(
            "A privileged intent is enabled here but not in the Discord portal: %s",
            exc,
        )
        await asyncio.Event().wait()


def main() -> None:
    start_health_server()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        log.error(
            "DISCORD_TOKEN is required. Set it in the Railway service variables "
            "from the Discord Developer Portal bot token."
        )
        wait_forever()
        return
    log.info("discord.py %s starting", discord.__version__)
    asyncio.run(run_discord(token))


if __name__ == "__main__":
    main()

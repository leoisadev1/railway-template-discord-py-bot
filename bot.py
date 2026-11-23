"""discord.py gateway worker with readiness and liveness endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Callable

import discord
from discord import app_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    # Privileged intents also require opt-in in the Discord Developer Portal.
    intents.message_content = env_flag("MESSAGE_CONTENT_INTENT")
    intents.members = env_flag("MEMBERS_INTENT")
    intents.presences = env_flag("PRESENCES_INTENT")
    return intents


def start_health_server(
    get_status: Callable[[], dict], *, host: str | None = None, port: int | None = None
) -> tuple[ThreadingHTTPServer, Thread]:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path not in {"/health", "/healthz", "/ready", "/live"}:
                self.send_response(404)
                self.end_headers()
                return
            status = get_status()
            ok = status["live"] if path == "/live" else status["ready"]
            body = json.dumps({**status, "ok": ok, "status": "ok" if ok else "unavailable"}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    host = host if host is not None else os.getenv("HOST", "0.0.0.0")
    port = port if port is not None else int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), HealthHandler)
    thread = Thread(
        target=lambda: server.serve_forever(poll_interval=0.1),
        daemon=True,
        name="healthcheck",
    )
    thread.start()
    log.info("healthcheck listening on port %s", server.server_port)
    return server, thread


def stop_health_server(server: ThreadingHTTPServer, thread: Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join()


class Bot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=build_intents())
        self.commands_synced = False
        self.gateway_connected = False
        self.stopping = False
        self.tree = app_commands.CommandTree(self)

        @self.tree.command(name="ping", description="Check that the bot is alive")
        async def ping(interaction: discord.Interaction) -> None:
            latency = self.latency
            latency_ms = str(round(latency * 1000)) if math.isfinite(latency) else "unknown"
            await interaction.response.send_message(f"Pong! `{latency_ms}ms`")

        @self.tree.command(name="info", description="Show bot and library versions")
        async def info(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                f"discord.py `{discord.__version__}` · `{self.user}`",
                ephemeral=True,
            )

        @self.tree.error
        async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            log.error("Slash command failed (%s)", type(error).__name__)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("There was an error while executing this command.", ephemeral=True)
                else:
                    await interaction.response.send_message("There was an error while executing this command.", ephemeral=True)
            except discord.HTTPException:
                log.error("Could not send the command error response")

    def health_status(self) -> dict:
        connected = self.gateway_connected and self.is_ready() and not self.is_closed()
        return {
            "ready": connected and self.commands_synced and not self.stopping,
            "live": not self.stopping,
            "discord": "stopping" if self.stopping else "connected" if connected else "disconnected",
            "commandsSynced": self.commands_synced,
        }

    async def setup_hook(self) -> None:
        self.commands_synced = False
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            if not guild_id.isdecimal() or not 0 < int(guild_id) < 2**64:
                raise ValueError("DISCORD_GUILD_ID must be a positive Discord snowflake")
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %s slash command(s) to guild %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("synced %s global slash command(s)", len(synced))
        self.commands_synced = True

    async def on_ready(self) -> None:
        self.gateway_connected = True
        log.info("logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def on_disconnect(self) -> None:
        self.gateway_connected = False

    async def on_resumed(self) -> None:
        self.gateway_connected = True


def configuration_problem(exc: BaseException) -> str | None:
    """The NEEDS_CONFIGURATION reason for a startup failure a restart cannot fix, else None."""
    if isinstance(exc, discord.LoginFailure):  # HTTP 401 on login
        return "TokenInvalid"
    if isinstance(exc, discord.PrivilegedIntentsRequired):  # gateway close 4014
        return "PrivilegedIntentsRequired"
    if isinstance(exc, discord.ConnectionClosed) and exc.code == 4004:  # token refused at IDENTIFY
        return "TokenInvalid"
    if isinstance(exc, discord.HTTPException) and exc.status == 401:  # revoked before the app lookup or sync
        return "TokenInvalid"
    return None


async def run_discord(
    token: str, *, client: Bot | None = None, host: str | None = None,
    port: int | None = None, startup_timeout: float = 25.0, shutdown_timeout: float = 5.0,
) -> None:
    if not token.strip():
        raise ValueError("DISCORD_TOKEN is required")
    client = client if client is not None else Bot()
    server = thread = None
    tasks: list[asyncio.Task] = []
    failure: BaseException | None = None
    try:
        await client.__aenter__()
        server, thread = start_health_server(client.health_status, host=host, port=port)
        worker = asyncio.create_task(client.start(token))
        ready = asyncio.create_task(client.wait_until_ready())
        tasks.extend([worker, ready])
        done, _ = await asyncio.wait(tasks, timeout=startup_timeout, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
            raise RuntimeError("Discord gateway stopped before becoming ready")
        if ready not in done:
            raise TimeoutError("Discord startup timed out before gateway readiness")
        await ready
        if not client.commands_synced:
            raise RuntimeError("Discord commands were not synchronized")
        await worker
        raise RuntimeError("Discord gateway stopped unexpectedly")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        client.stopping = True

        async def close_gateway() -> None:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()

        try:
            # Close once: Client.__aexit__ would retry an already timed-out close.
            await asyncio.wait_for(close_gateway(), timeout=shutdown_timeout)
        except TimeoutError:
            # A stuck close must not hide a configuration verdict that main() turns into idle.
            if failure is None or configuration_problem(failure) is None:
                raise
            log.error("Discord gateway shutdown timed out")
        finally:
            if server is not None:
                await asyncio.to_thread(stop_health_server, server, thread)


async def serve(token: str) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await run_discord(token)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


TOKEN_HELP = (
    "In the Discord Developer Portal open your application, go to Bot, press Reset Token and set the new "
    "token as DISCORD_TOKEN on this Railway service (not the application ID, public key or client secret). "
    "Railway redeploys the service when the variable changes."
)
# Configuration a restart cannot fix. The bot idles with /live up instead of crash-looping, so the
# deployment stays reachable and the log says what to change.
NEEDS_CONFIGURATION = {
    "TokenMissing": "DISCORD_TOKEN is not set. " + TOKEN_HELP,
    "TokenInvalid": "Discord rejected DISCORD_TOKEN. " + TOKEN_HELP,
    "PrivilegedIntentsRequired": (
        "Discord refused a privileged intent enabled here (MESSAGE_CONTENT_INTENT, MEMBERS_INTENT or "
        "PRESENCES_INTENT). Turn it on under Bot > Privileged Gateway Intents in the Discord Developer "
        "Portal, or set the variable to false on this Railway service."
    ),
}


async def idle(reason: str, *, host: str | None = None, port: int | None = None) -> None:
    """Serve /live (200) and /health (503, needs_configuration) until SIGTERM or SIGINT."""
    status = {"ready": False, "live": True, "discord": "needs_configuration", "reason": reason, "commandsSynced": False}
    server, thread = start_health_server(lambda: status, host=host, port=port)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.error("The bot is idle and not connected to Discord. %s", NEEDS_CONFIGURATION[reason])
    try:
        await stop.wait()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        await asyncio.to_thread(stop_health_server, server, thread)


def main() -> int:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    reason = None
    if not token:
        reason = "TokenMissing"
    else:
        try:
            asyncio.run(serve(token))
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
        except Exception as exc:
            reason = configuration_problem(exc)
            if reason is None:
                log.error("Discord startup/runtime failed (%s); check configuration, command permissions and connectivity", type(exc).__name__)
                return 1
        else:
            return 0
    try:
        asyncio.run(idle(reason))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:
        log.error("Could not start the idle health listener (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import asyncio
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import app_commands

import bot

HOST = os.getenv("TEST_HOST", "127.0.0.1")
ROOT = Path(__file__).resolve().parents[1]


def request(port, path="/health", method="GET"):
    conn = http.client.HTTPConnection(HOST, port, timeout=3)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw.startswith(b"{") else raw
    finally:
        conn.close()


def interaction(done=False):
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), is_done=Mock(return_value=done)),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = bot.Bot()
        await self.client.__aenter__()
        self.addAsyncCleanup(self.client.close)

    async def test_ping_info_and_unknown_latency(self):
        self.assertEqual(sorted(command.name for command in self.client.tree.get_commands()), ["info", "ping"])
        ctx = interaction()
        self.client.ws = SimpleNamespace(latency=0.042)
        await self.client.tree.get_command("ping").callback(ctx)
        ctx.response.send_message.assert_awaited_once_with("Pong! `42ms`")
        self.client.ws = None
        ctx = interaction()
        await self.client.tree.get_command("ping").callback(ctx)
        ctx.response.send_message.assert_awaited_once_with("Pong! `unknownms`")
        ctx = interaction()
        await self.client.tree.get_command("info").callback(ctx)
        args, kwargs = ctx.response.send_message.await_args
        self.assertIn(discord.__version__, args[0])
        self.assertTrue(kwargs["ephemeral"])

    async def test_command_errors_are_reported_before_and_after_ack(self):
        for done in (False, True):
            ctx = interaction(done)
            await self.client.tree.on_error(ctx, app_commands.AppCommandError("synthetic"))
            response = ctx.followup.send if done else ctx.response.send_message
            self.assertTrue(response.await_args.kwargs["ephemeral"])
        ctx = interaction()
        ctx.response.send_message.side_effect = discord.HTTPException(SimpleNamespace(status=404, reason="expired"), "expired")
        await self.client.tree.on_error(ctx, app_commands.AppCommandError("synthetic"))

    async def test_global_and_guild_sync_and_invalid_guild(self):
        with patch.object(self.client.tree, "sync", AsyncMock(return_value=[1, 2])) as sync:
            with patch.dict(os.environ, {}, clear=True):
                await self.client.setup_hook()
            sync.assert_awaited_once_with()
            self.assertTrue(self.client.commands_synced)
        with patch.object(self.client.tree, "sync", AsyncMock(return_value=[1, 2])) as sync:
            with patch.dict(os.environ, {"DISCORD_GUILD_ID": "123"}, clear=True):
                await self.client.setup_hook()
            self.assertEqual(sync.await_args.kwargs["guild"].id, 123)
            self.assertEqual(len(self.client.tree.get_commands(guild=discord.Object(id=123))), 2)
        for value in ("oops", "0", "-1", str(2**64)):
            with patch.dict(os.environ, {"DISCORD_GUILD_ID": value}, clear=True):
                with self.assertRaisesRegex(ValueError, "DISCORD_GUILD_ID"):
                    await self.client.setup_hook()

    async def test_sync_failure_never_marks_synced(self):
        self.client.commands_synced = True
        with patch.dict(os.environ, {}, clear=True), patch.object(self.client.tree, "sync", AsyncMock(side_effect=RuntimeError("403 synthetic"))):
            with self.assertRaises(RuntimeError):
                await self.client.setup_hook()
        self.assertFalse(self.client.health_status()["ready"])
        self.assertFalse(self.client.commands_synced)

    async def test_health_liveness_sync_disconnect_resume_and_shutdown(self):
        server, thread = bot.start_health_server(self.client.health_status, host=HOST, port=0)
        self.addCleanup(bot.stop_health_server, server, thread)
        port = server.server_port
        print(f"Temporary health listener: {HOST}:{port}")
        self.assertEqual(request(port)[0], 503)
        self.assertEqual(request(port, "/live")[0], 200)
        self.client._ready.set()
        await self.client.on_ready()
        self.assertEqual(request(port)[0], 503)
        self.client.commands_synced = True
        for path in ("/health", "/healthz", "/ready?probe=1"):
            status, body = request(port, path)
            self.assertEqual(status, 200)
            self.assertTrue(body["ready"])
        await self.client.on_disconnect()
        self.assertTrue(self.client.is_ready(), "discord.py retains its cache-ready flag on disconnect")
        self.assertEqual(request(port)[0], 503)
        self.assertEqual(request(port, "/live")[0], 200)
        await self.client.on_resumed()
        self.assertEqual(request(port)[0], 200)
        for path in ("/", "/webhook", "/interactions"):
            self.assertEqual(request(port, path)[0], 404)
        self.assertNotEqual(request(port, "/interactions", "POST")[0], 200)
        self.client.stopping = True
        self.assertEqual(request(port)[0], 503)
        self.assertEqual(request(port, "/live")[0], 503)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_opens_no_listener(self):
        with patch.object(bot, "start_health_server") as start:
            with self.assertRaisesRegex(ValueError, "DISCORD_TOKEN"):
                await bot.run_discord("   ")
            start.assert_not_called()

    async def test_invalid_credentials_intents_sync_and_timeout_close_all_resources(self):
        for failure in (discord.LoginFailure("401 synthetic"), discord.PrivilegedIntentsRequired(None), RuntimeError("sync failed"), None):
            client = bot.Bot()
            if failure:
                client.start = AsyncMock(side_effect=failure)
            else:
                async def hang(_token):
                    await asyncio.Event().wait()
                client.start = hang
            resources = []
            original = bot.start_health_server

            def start(*args, **kwargs):
                result = original(*args, **kwargs)
                resources.append(result)
                return result

            with patch.object(bot, "start_health_server", start):
                with self.assertRaises(type(failure) if failure else TimeoutError):
                    await bot.run_discord("synthetic-token", client=client, host=HOST, port=0, startup_timeout=0.03)
            server, thread = resources[0]
            self.assertFalse(thread.is_alive())
            self.assertEqual(server.fileno(), -1)
            self.assertTrue(client.is_closed())
            self.assertFalse(client.health_status()["ready"])

    async def test_successful_start_and_cancellation_close_gateway_and_http(self):
        client = bot.Bot()
        started = asyncio.Event()

        async def gateway(_token):
            client.commands_synced = True
            client._ready.set()
            await client.on_ready()
            started.set()
            await asyncio.Event().wait()

        client.start = gateway
        worker = asyncio.create_task(bot.run_discord("synthetic-token", client=client, host=HOST, port=0))
        await asyncio.wait_for(started.wait(), timeout=3)
        self.assertTrue(client.health_status()["ready"])
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertTrue(client.is_closed())
        self.assertTrue(client.stopping)

    async def test_bind_failure_closes_client(self):
        owner, thread = bot.start_health_server(lambda: {"ready": False, "live": True}, host=HOST, port=0)
        self.addCleanup(bot.stop_health_server, owner, thread)
        client = bot.Bot()
        client.start = AsyncMock()
        with self.assertRaises(OSError):
            await bot.run_discord("synthetic", client=client, host=HOST, port=owner.server_port)
        self.assertTrue(client.is_closed())
        client.start.assert_not_awaited()

    async def test_cache_ready_without_command_sync_is_failure(self):
        client = bot.Bot()

        async def gateway(_token):
            client._ready.set()
            await asyncio.Event().wait()

        client.start = gateway
        with self.assertRaisesRegex(RuntimeError, "not synchronized"):
            await bot.run_discord("synthetic", client=client, host=HOST, port=0)
        self.assertTrue(client.is_closed())

    async def test_shutdown_timeout_still_closes_http_without_retrying_close(self):
        client = bot.Bot()
        client.start = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        real_close = client.close
        client.close = AsyncMock()
        resources = []
        original = bot.start_health_server

        def start(*args, **kwargs):
            result = original(*args, **kwargs)
            resources.append(result)
            return result

        async def hang():
            await asyncio.Event().wait()

        client.close.side_effect = hang
        try:
            with patch.object(bot, "start_health_server", start), self.assertRaises(TimeoutError):
                await bot.run_discord("synthetic", client=client, host=HOST, port=0, shutdown_timeout=0.03)
            client.close.assert_awaited_once()
            server, thread = resources[0]
            self.assertFalse(thread.is_alive())
            self.assertEqual(server.fileno(), -1)
        finally:
            await real_close()

    async def test_unexpected_gateway_return_is_failure(self):
        client = bot.Bot()
        client.start = AsyncMock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            await bot.run_discord("synthetic-token", client=client, host=HOST, port=0)


class ConfigurationTests(unittest.TestCase):
    def test_intents_are_off_unless_opted_in(self):
        with patch.dict(os.environ, {}, clear=True):
            intents = bot.build_intents()
            self.assertFalse(intents.message_content or intents.members or intents.presences)
        with patch.dict(os.environ, {"MESSAGE_CONTENT_INTENT": "yes", "MEMBERS_INTENT": "true", "PRESENCES_INTENT": "1"}, clear=True):
            intents = bot.build_intents()
            self.assertTrue(intents.message_content and intents.members and intents.presences)

    def test_cli_missing_and_mocked_invalid_token_exit_one(self):
        env = {"PATH": os.environ["PATH"], "HOST": HOST, "PORT": "0"}
        missing = subprocess.run([sys.executable, "bot.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("DISCORD_TOKEN is required", missing.stderr)
        rejected = subprocess.run([sys.executable, "tests/provider.py"], cwd=ROOT,
                                  env={**env, "DISCORD_TOKEN": "synthetic", "TEST_PROVIDER_MODE": "invalid"},
                                  capture_output=True, text=True, timeout=5)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("DISCORD_TOKEN was rejected", rejected.stderr)

    def test_cli_sigterm_and_sigint_close_cleanly(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            child = subprocess.Popen([sys.executable, "tests/provider.py"], cwd=ROOT,
                                     env={"PATH": os.environ["PATH"], "HOST": HOST, "PORT": "0", "DISCORD_TOKEN": "synthetic"},
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                import select
                readable, _, _ = select.select([child.stdout], [], [], 5)
                self.assertTrue(readable, "mock gateway did not start")
                self.assertEqual(child.stdout.readline().strip(), "MOCK_READY")
                child.send_signal(sig)
                _, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()


if __name__ == "__main__":
    unittest.main()

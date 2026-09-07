# Discord.py Bot

A one-service **discord.py 2.7** worker for Railway. Slash commands, a pinned Python 3.12 image, a `uv` lockfile, and a `/health` endpoint used only for Railway healthchecks.

This listing replaces rotting marketplace clones frozen on discord.py 2.1.0 (Jan 2023) with no lockfile and no healthcheck.

## One-click deploy

1. Click **Deploy on Railway**.
2. Paste your bot token into **`DISCORD_TOKEN`**. Railway will not generate this for you.
3. Deploy. The worker binds `$PORT` for `/health` and opens a Discord gateway session.

Create the token at [Discord Developer Portal](https://discord.com/developers/applications) → your application → **Bot** → **Reset Token**. Invite the bot with `applications.commands` and `bot` scopes.

## Variables

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | **yes** | none | Bot token from the Developer Portal. User-provided. Never baked in. |
| `PORT` | no | `8080` | Injected by Railway. Healthcheck listen port. |
| `DISCORD_GUILD_ID` | no | unset | If set, slash commands sync instantly to that guild. Omit for global sync (can take up to an hour). |
| `MESSAGE_CONTENT_INTENT` | no | `false` | Privileged. Set `true` only after enabling **Message Content Intent** in the portal. |
| `MEMBERS_INTENT` | no | `false` | Privileged. Set `true` only after enabling **Server Members Intent**. |
| `PRESENCES_INTENT` | no | `false` | Privileged. Set `true` only after enabling **Presence Intent**. |

No generated secrets. No dummy token.

## Privileged intents

Slash commands (`/ping`, `/info`) work with **default intents**. Privileged intents stay off unless you opt in with the flags above **and** toggle them on the Bot page in the Developer Portal. If the portal and the env flags disagree, discord.py raises `PrivilegedIntentsRequired` and the process keeps `/health` up so you can fix the config without a crash loop.

## Ports

| Port | Purpose |
| --- | --- |
| `$PORT` (HTTP) | Railway healthcheck only: `GET /health` → `{"status":"ok"}`. Not a website. No public homepage. |

You do not need a public domain for the bot to talk to Discord. Railway uses `/health` internally.

## Volumes

None. This is a stateless gateway worker. Add a volume later if you persist your own data.

## Login / invite

1. Developer Portal → OAuth2 → URL Generator.
2. Scopes: `bot` + `applications.commands`.
3. Grant the permissions your commands need (Send Messages is enough for `/ping`).
4. Open the generated URL, pick a server, authorize.
5. In Discord, type `/ping`.

## Included commands

- `/ping` — round-trip latency
- `/info` — discord.py version and bot user

Edit `bot.py` and redeploy to add commands.

## Why this is healthier than the old marketplace clones

| | This template | Typical rotting clone |
| --- | --- | --- |
| Python | **3.12.14-slim-bookworm** digest-pinned | Unpinned / 3.10 era |
| discord.py | **2.7.1** in `uv.lock` | `discord.py==2.1.0` (Jan 2023) |
| Commands | Slash (`app_commands`) | Prefix `!` only |
| Healthcheck | `GET /health` on `$PORT` | None (health 0) |
| Token | Required, empty default | Sometimes a dummy |
| Shape | One worker service | Dummy Flask homepage |

## Local run

```bash
uv sync --frozen
export DISCORD_TOKEN=your-token
export PORT=8080
uv run python bot.py
```

`curl -fsS http://127.0.0.1:8080/health` should return `{"status":"ok"}`.

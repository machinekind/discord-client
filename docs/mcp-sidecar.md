# MCP server sidecar (`discord-client-mcp`)

`discord-client-mcp` serves the read-only `discord-client` commands as an MCP
server over Streamable HTTP, at `/mcp`. It is for agents that have no shell
and so cannot run `discord-client` themselves, such as an agent running in a
container with no shell tool.

## Why a sidecar, not stdio

With stdio, the agent's MCP client starts the server as a child process, so the
Discord token would sit in the agent's own container. That agent reads
untrusted Discord messages, and taking its shell away is meant to contain it.
Running the server in a separate container means the agent holds only a URL
and a bearer token, never the Discord token. It also keeps Python out of the
agent's image.

## Tools

| Tool | CLI it runs | Notes |
|---|---|---|
| `whoami` | `discord-client whoami` | Bot identity and token tier |
| `list_channels` | `discord-client channels` | |
| `pull_channel(channel, since)` | `discord-client pull` | `since`: `3d` or `YYYY-MM-DD` |
| `search_messages(query, limit=25)` | `discord-client search` | Discord guild search, max 25 hits |
| `get_thread(thread_id)` | `discord-client thread` | Numeric id only |
| `day_transcript(date)` | `discord-client day` | Whole guild for one day; slow and large |

Dates, day windows and the message times in transcripts use the server's
configured timezone: `DISCORD_TIMEZONE`, UTC by default.

Each call runs the CLI in a fresh subprocess (`python -m discord_client.cli`),
so behaviour matches the CLI exactly, and a `[FAIL]` exit comes back to the
agent as a tool error carrying the CLI's message. When the CLI skips a channel
or thread it cannot read, it still exits 0 and prints a `[warn]` line on
stderr; the server appends those lines (up to 30) to the result, so the agent
can tell missing access from a quiet channel.

Every tool is annotated read-only. **Nothing posts.** The library has two
helpers that post (`DiscordClient.post_message`, `DiscordClient.start_thread`),
but the CLI has no posting command and no tool calls either. Give the sidecar
a read-only bot's `DISCORD_READER_TOKEN` only, and posting stays impossible
even if a posting tool were ever added by mistake.

## Auth

Two layers, kept apart:

1. **Sidecar to Discord:** `DISCORD_READER_TOKEN`, only in the sidecar's
   environment.
2. **Agent to sidecar:** a static bearer token, `DISCORD_CLIENT_MCP_TOKEN`,
   checked on every HTTP request with a constant-time compare. The server
   refuses to start without one of at least 32 characters. Requests whose
   `Host` header is not on the allowlist get `421`, and requests that carry an
   `Origin` header (that is, from a browser) get `403`.

The bearer matters even on a private network: a prompt injection in a Discord
message could get the agent's own web tools to request the sidecar's URL.
Keep the token in the MCP client's config, not in anything the model sees, and
those requests get `401`.

## Environment

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_CLIENT_MCP_TOKEN` | yes | Bearer secret, e.g. `openssl rand -hex 32` |
| `DISCORD_READER_TOKEN` | yes | Read-only bot token. `DISCORD_BOT_TOKEN` also works, but the server logs a warning: it never needs write access |
| `DISCORD_GUILD_ID` | if the bot is in more than one guild | Guild to read |
| `DISCORD_TIMEZONE` | no | IANA zone for dates and day windows; default `UTC` |
| `DISCORD_IGNORE_CHANNELS` | no | Comma-separated channel names that whole-guild reads skip |
| `DISCORD_CLIENT_CONFIG` | no | Path to a `discord-client.toml` mounted into the container, as an alternative to the three settings above; environment variables win over the file |
| `DISCORD_CLIENT_MCP_ALLOWED_HOSTS` | no | Comma-separated `Host` values. Replaces the default list, `localhost:*,127.0.0.1:*,[::1]:*,discord-client-mcp:*` |
| `DISCORD_CLIENT_MCP_HOST`, `DISCORD_CLIENT_MCP_PORT` | no | Bind address, default `127.0.0.1:8000`. The image's command passes `--host 0.0.0.0 --port 8000`, which override these |

## Build the image

From the repo root:

```sh
docker build -t discord-client-mcp .
```

`.dockerignore` is an allowlist: only `pyproject.toml`, `README.md`, `LICENSE`
and `discord_client/` enter the build context. A `.env`, a
`discord-client.toml` or pulled transcripts in the checkout never reach the
Docker daemon, let alone the image. The image holds no tokens or settings and
runs as an unprivileged user (uid 10001). Everything it needs comes from the
environment at run time.

## Docker Compose

```yaml
services:
  agent:
    # ...your existing agent service...
    environment:
      DISCORD_CLIENT_MCP_TOKEN: ${DISCORD_CLIENT_MCP_TOKEN}
    networks: [agents]

  discord-client-mcp:
    build: ./discord-client  # path to a checkout of this repo
    # DISCORD_CLIENT_MCP_TOKEN, DISCORD_READER_TOKEN, and DISCORD_GUILD_ID /
    # DISCORD_TIMEZONE / DISCORD_IGNORE_CHANNELS as needed
    env_file: ./discord-client-mcp.env
    networks: [agents]
    restart: unless-stopped
    # No `ports:` on purpose: only containers on `agents` can reach it.

networks:
  agents: {}
```

The service name `discord-client-mcp` is on the default Host allowlist. Under
a different name, set `DISCORD_CLIENT_MCP_ALLOWED_HOSTS=<name>:8000`.

## Connecting a client

Any MCP client that speaks Streamable HTTP can connect. It needs:

- **URL:** `http://discord-client-mcp:8000/mcp` (the compose service name), or
  `http://127.0.0.1:8000/mcp` for a local run.
- **Transport:** Streamable HTTP. Set it explicitly if your client defaults to
  the older SSE transport.
- **Header:** `Authorization: Bearer <DISCORD_CLIENT_MCP_TOKEN>`.
- **Request timeout:** at least 10 minutes if the agent will call
  `day_transcript`; the server allows `day` up to 600 seconds. The other tools
  are cut off at 120 seconds and usually finish in a few.

Config keys differ between clients. A common shape is:

```json
{
  "mcpServers": {
    "discord": {
      "type": "http",
      "url": "http://discord-client-mcp:8000/mcp",
      "headers": { "Authorization": "Bearer ${DISCORD_CLIENT_MCP_TOKEN}" }
    }
  }
}
```

Where the client supports it, reference the token from the environment as
above rather than writing it into the file.

To check the server by hand:

```sh
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $DISCORD_CLIENT_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## Running it locally

```sh
uv sync  # the dev group includes the mcp extra
export DISCORD_CLIENT_MCP_TOKEN=$(openssl rand -hex 32)
uv run discord-client-mcp  # http://127.0.0.1:8000/mcp
```

Outside the dev environment, install the extra with `pip install '.[mcp]'`
from a checkout, or straight from the repository (the package is not on PyPI):

```sh
pip install 'discord-client[mcp] @ git+https://github.com/machinekind/discord-client'
```

As with the CLI, a `.env` in the working directory is loaded at startup, so
the Discord token can come from there.

The server is stateless (no MCP sessions) and answers with plain JSON, not
SSE: a restart never leaves a client holding a dead session.

## Security notes

- **Read-only by construction.** No tool posts, and with only
  `DISCORD_READER_TOKEN` set the process holds no credential that could.
- **Plain HTTP.** The server does not terminate TLS. Keep it on a private
  container network and do not publish its port. If it has to cross hosts, put
  a TLS-terminating proxy in front of it.
- **Static bearer token.** There is no rotation or expiry; to rotate, change
  `DISCORD_CLIENT_MCP_TOKEN` on both sides and restart. The token is compared
  in constant time, but its length is not hidden.
- **DNS-rebinding protection.** The `Host` allowlist and the `Origin` check
  stop a web page from reaching the server through a browser on the same
  network.
- **No shell, validated arguments.** The CLI is started with an argument list,
  never through a shell. Dates, `since` values and thread ids are checked
  against fixed patterns before the CLI runs, values go in as `--opt=value`,
  and the search query follows `--`, so no input can be read as an option.
- **Results are untrusted text.** Transcripts are whatever people wrote in
  Discord, prompt injections included. The server passes them through
  unchanged; the agent has to treat them as data.

## Troubleshooting

- **`whoami` works, every other tool fails with `403 Missing Access`
  (code 50001):** the bot is not a member of the guild. Invite it again.
- **A result ends with `CLI warnings ... bot has no access (403)`:** the bot is
  in the guild but cannot see those channels. Grant it View Channel and Read
  Message History there. An empty transcript that comes with these warnings
  means missing access, not silence.
- **`this bot is in N guilds`:** set `DISCORD_GUILD_ID`.
- **`421 Invalid Host header`:** the agent reaches the sidecar under a name not
  on the allowlist. Add it to `DISCORD_CLIENT_MCP_ALLOWED_HOSTS`.
- **`401`:** the agent's `DISCORD_CLIENT_MCP_TOKEN` differs from the sidecar's.

## What has and has not been verified

- **Test suite:** covers the tool wiring, argument validation, warning
  pass-through, the subprocess runner, bearer auth, the Host and Origin checks,
  and startup. Tool tests fake the CLI subprocess; none of them talks to
  Discord.
- **Image:** built and smoke-tested on linux/arm64: it runs as uid 10001,
  answers `401` without the bearer and `421` for an unlisted `Host`, serves
  `initialize` and `tools/list`, and returns the CLI's `[FAIL]` as a tool
  error when no Discord token is set. Not built or tested on linux/amd64.
- **HTTP layer:** exercised without a Discord token, so no tool call over
  HTTP has reached Discord.
- **Tools against Discord:** the tool functions were called in process,
  bypassing HTTP, against a real server with a read-only bot.
- **MCP clients:** no specific client configuration has been tested. The JSON
  above is a common shape, not a tested config for a named client.

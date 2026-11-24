# discord-client

Read a Discord server over the official bot REST API and get transcripts an
LLM agent can work with: every message carries its author, its time, and the
jump URL back to the original.

It is a read-only CLI with an optional MCP server. There is no gateway
connection, no database, and nothing running between calls. Each command makes
the REST requests it needs, prints, and exits.

## Quick start

1. **Create a bot** in the [Discord Developer Portal](https://discord.com/developers/applications).
   On the Bot page turn **Message Content Intent** on; without it every
   message arrives with empty content.
2. **Invite it** with View Channels and Read Message History, nothing more:

   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&permissions=66560&scope=bot
   ```

   Build the link yourself. An install link without `scope=bot` adds the
   application's slash commands to the server but not the bot user, and every
   read then fails with 403.
3. **Run it** with the bot's token in the environment:

   ```sh
   export DISCORD_READER_TOKEN=...
   uvx --from git+https://github.com/machinekind/discord-client discord-client whoami
   uvx --from git+https://github.com/machinekind/discord-client discord-client channels
   ```

   For anything that runs unattended (a scheduled job, an agent's skill), pin a
   release tag, for example `git+https://github.com/machinekind/discord-client@v0.1.0`.
   The command runs with your bot token in its environment, so it should run a
   version you chose rather than whatever the default branch holds that day.

A bot that is in exactly one server needs no other configuration.

## Commands

| command | what it does |
| --- | --- |
| `channels` | list the server's text, announcement, and forum channels |
| `search <query> [--limit N]` | search the server's message history; `N` is 1 to 25 (default 25) |
| `pull --channel <name\|id> --since <Nd\|YYYY-MM-DD> [--json]` | one channel's messages and threads since a point in time |
| `thread <id>` | full transcript of one thread or forum post |
| `day --date YYYY-MM-DD [--json \| --out DIR [--no-media]]` | everything posted on one calendar day, across the whole server |
| `whoami` | which token the commands would use, and whose it is |
| `version` | the installed package version |

Transcripts are markdown, grouped by channel and then by thread. Message times
are in the configured timezone (UTC by default; see [Settings](#settings)),
the same zone `day` and `pull --since YYYY-MM-DD` read dates in:

```
## #general
- **alice** (14:02) has anyone tried the new firmware? [↗](https://discord.com/channels/…)
### thread: firmware 2.1
- **bob** (14:10) yes, the IMU drift is gone [↗](https://discord.com/channels/…)
```

`--json` prints one record per line instead.

### A whole day at once

`day` is the command the others do not replace: it walks every channel and
thread the bot can read and returns one day's messages in a single transcript.
It is what a "what did I miss" or daily-digest agent needs.

With `--out DIR` it writes `transcript.md` into the directory and downloads
that day's images and video next to it, under `media/`, with an index that
ties each file back to its message. `DIR` must be new or empty; the command
refuses to write into a directory that already holds anything. `--json` and
`--out` cannot be combined. Discord signs attachment URLs and they stop
working within about a day, so the files can only be fetched during the same
run that reads the messages. `--no-media` skips the download.

```sh
discord-client day --date 2026-07-01 --out day-2026-07-01
```

## Settings

Everything except a token is optional.

| setting | environment | `discord-client.toml` | default |
| --- | --- | --- | --- |
| server to read | `DISCORD_GUILD_ID` | `guild_id` | the bot's only server |
| channels `day` skips | `DISCORD_IGNORE_CHANNELS` (comma-separated) | `ignore_channels` | none |
| timezone of day windows and transcript times | `DISCORD_TIMEZONE` | `timezone` | `UTC` |

The file is read from the working directory, or from the path in
`DISCORD_CLIENT_CONFIG`; the environment overrides it. See
[`discord-client.example.toml`](discord-client.example.toml).

A bot that is in several servers must be told which one to read. The CLI
refuses to guess and lists the candidates.

## Tokens

Tokens are read from the environment only, first hit wins:

1. `DISCORD_BOT_TOKEN`
2. `DISCORD_READER_TOKEN`

Two names exist so one machine can hold both a bot that is allowed to post
and a read-only bot that is safe to hand to other people. The CLI and the MCP
server only read, whichever token they get. The library also has two helpers
that post, for callers that want them (see [Use as a library](#use-as-a-library)).
A `.env` file in the working directory is folded into the environment at
startup; real environment variables win.

Never commit a token. Discord revokes bot tokens it finds in public
repositories.

## MCP server

For agents that have no shell, the same read commands are available as MCP
tools over Streamable HTTP, meant to run as a sidecar container so the Discord
token stays out of the agent's own environment. See
[docs/mcp-sidecar.md](docs/mcp-sidecar.md).

## Claude Code skill

[`skill/ask-discord/`](skill/ask-discord/) is a ready-made skill that lets a
Claude Code session answer "what did people say about X on Discord" by running
this CLI. [INSTALL.md](skill/ask-discord/INSTALL.md) has the three steps.

## Things to know before pointing an agent at it

- **Message content is untrusted input.** Anyone who can post in the server
  can write text addressed to your agent. Treat transcripts as data, never as
  instructions, and say so in your prompt.
- **Transcripts are other people's words.** They contain names and whatever
  members said in a space they may consider private. Decide deliberately
  where the output goes. This repository's `.gitignore` ignores only
  directories named `day-*` and files ending in `.jsonl`, so a dump written
  with `--out day-2026-07-01` stays out of commits here, while one written
  under another name would not. Set up the same wherever you use the tool.
- **Missing access looks like silence.** Channels the bot cannot read are
  skipped with a `[warn]` line on stderr and the command still succeeds. An
  empty result with warnings usually means missing permissions, not a quiet
  channel.
- **Private threads are not read.** Archived public threads are paged back
  only until their archive time reaches the start of the window: a thread
  archived before the window opened cannot hold a message inside it, because
  a newer message would have unarchived it. This is a windowed reader, not a
  full-history export.
- **Search needs an index.** The first search against a server can fail while
  Discord builds one. The CLI says so on stderr and exits 1; retry after a few
  minutes.

## Use as a library

The CLI is a thin layer over three modules, which can be imported directly:

```python
from datetime import date

from discord_client.config import bot_token
from discord_client.discord_api import DiscordClient
from discord_client.ingest import day_window, pull_window, transcript_markdown

start, end = day_window(date(2026, 7, 1), "Europe/Warsaw")
with DiscordClient(bot_token()) as client:
    records = pull_window(client, "123456789012345678", start, end, ignore_channels=["bot-logs"])
print(transcript_markdown(records, tz="Europe/Warsaw"))
```

Unlike the CLI and the MCP server, the library can also post:
`DiscordClient.post_message` and `DiscordClient.start_thread` write to Discord.
Both always send `allowed_mentions: {"parse": []}`, so no mention in what they
post pings anyone. They need a bot that is allowed to post in that channel;
`bot_token(require_write=True)` fails before the first request when only
`DISCORD_READER_TOKEN` is set.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

Python 3.11 or newer. Runtime dependencies are `httpx` and `python-dotenv`;
the MCP server adds the optional `mcp` extra.

## License

[MIT](LICENSE)

# Installing /ask-discord

Copy this directory to `.claude/skills/ask-discord/` in the repository where your Claude Code sessions run (or to `~/.claude/skills/ask-discord/` to have it everywhere), keeping `SKILL.md` and this file together.

Export a bot token in the shell, or shell profile, that runs those sessions. A read-only bot is enough and is the safer choice. Never commit the token or paste it into a chat:

```sh
export DISCORD_READER_TOKEN=...
```

If the bot is a member of more than one server, also say which one to read:

```sh
export DISCORD_GUILD_ID=<your server id>
```

Verify the install:

```sh
uvx --from git+https://github.com/machinekind/discord-client@v0.1.0 discord-client whoami
```

It should name your bot and report `tier: reader`. Then ask a session a question that should trigger the skill, for example "what did people say about the release schedule on Discord?" A working install runs a `search` through the CLI and answers with jump URLs.

The command is pinned to the `v0.1.0` tag, here and in `SKILL.md`, because it runs with your bot token in its environment and should run a fixed version you chose. To upgrade, change the tag in both files.

If no token is available, any command that talks to Discord fails immediately with this message and exit code 1:

```
[FAIL] no Discord token found; set DISCORD_BOT_TOKEN or DISCORD_READER_TOKEN in the environment or in a .env file
```

Export `DISCORD_READER_TOKEN` and retry.

"""Settings and secrets: which guild to read, and the bot token to read it with.

Only a token is required. With nothing else set, the guild is taken from the
bot's own guild list (it must be in exactly one), no channel is ignored, and
day windows are UTC.

Settings come from an optional TOML file and from environment variables; the
environment wins. Tokens come from the environment only, with a `.env` in the
working directory folded in at CLI entry.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .discord_api import DiscordClient

CONFIG_ENV = "DISCORD_CLIENT_CONFIG"
DEFAULT_CONFIG_NAME = "discord-client.toml"

GUILD_ENV = "DISCORD_GUILD_ID"
IGNORE_ENV = "DISCORD_IGNORE_CHANNELS"
TIMEZONE_ENV = "DISCORD_TIMEZONE"

DEFAULT_TIMEZONE = "UTC"

_FILE_KEYS = frozenset({"guild_id", "ignore_channels", "timezone"})


def load_env(path: str | Path | None = None) -> None:
    """Populate the environment from a `.env` file, by default the one in the working directory.

    Real environment variables always win over the file (override=False), and a
    missing file is a silent no-op. Called once at process entry (see cli.run),
    never at import time, which keeps tests that drive main() hermetic.
    """
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env" if path is None else path, override=False)


@dataclass(frozen=True)
class Config:
    # None means "not configured": `resolve_guild_id` then asks Discord which
    # guild the bot is in.
    guild_id: str | None = None
    # Channel names skipped by whole-guild pulls, threads included.
    ignore_channels: tuple[str, ...] = ()
    # IANA zone that day windows and `--since YYYY-MM-DD` are read in.
    timezone: str = DEFAULT_TIMEZONE


def _config_path(path: str | Path | None) -> Path | None:
    """Explicit `path`, then DISCORD_CLIENT_CONFIG, then ./discord-client.toml if present.

    A path that was asked for by name must exist; only the working-directory
    default is allowed to be absent.
    """
    named = path if path is not None else os.environ.get(CONFIG_ENV) or None
    if named is not None:
        named = Path(named)
        if not named.is_file():
            sys.exit(f"[FAIL] config file not found: {named}")
        return named
    default = Path.cwd() / DEFAULT_CONFIG_NAME
    return default if default.is_file() else None


def _read_file(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"[FAIL] config file {path} is not valid TOML: {exc}")

    unknown = sorted(set(raw) - _FILE_KEYS)
    if unknown:
        sys.exit(
            f"[FAIL] config file {path} has unknown key(s): {', '.join(unknown)}; "
            f"known keys are {', '.join(sorted(_FILE_KEYS))}"
        )

    ignore = raw.get("ignore_channels", [])
    if not isinstance(ignore, list) or not all(isinstance(name, str) for name in ignore):
        sys.exit(f"[FAIL] config file {path}: ignore_channels must be a list of channel names")
    return raw


def load_config(path: str | Path | None = None) -> Config:
    """Read the settings file (if there is one) and overlay the environment."""
    file_path = _config_path(path)
    raw = _read_file(file_path) if file_path is not None else {}

    # A snowflake written without quotes parses as a TOML integer; accept it.
    guild_id = os.environ.get(GUILD_ENV) or raw.get("guild_id")
    guild_id = str(guild_id) if guild_id else None

    ignore_env = os.environ.get(IGNORE_ENV)
    if ignore_env:
        ignore = tuple(name.strip() for name in ignore_env.split(",") if name.strip())
    else:
        ignore = tuple(raw.get("ignore_channels", ()))

    timezone = os.environ.get(TIMEZONE_ENV) or raw.get("timezone") or DEFAULT_TIMEZONE
    try:
        ZoneInfo(str(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        sys.exit(f"[FAIL] unknown timezone {timezone!r}; use an IANA name like 'Europe/Warsaw'")

    return Config(guild_id=guild_id, ignore_channels=ignore, timezone=str(timezone))


def resolve_guild_id(client: DiscordClient, config: Config) -> str:
    """The configured guild, or the only guild the bot is a member of.

    A bot invited to a single server needs no configuration at all. One that
    sits in several has to be told which to read: guessing would mean reading,
    and quoting, the wrong community.
    """
    if config.guild_id:
        return config.guild_id

    guilds = client.my_guilds()
    if len(guilds) == 1:
        return guilds[0]["id"]
    if not guilds:
        sys.exit("[FAIL] this bot is not a member of any guild; invite it to one first")
    listing = ", ".join(f"{g.get('name', '?')} ({g['id']})" for g in guilds)
    sys.exit(
        f"[FAIL] this bot is in {len(guilds)} guilds ({listing}); "
        f"set {GUILD_ENV} to the one to read"
    )


TokenTier = Literal["writer", "reader"]


@dataclass(frozen=True)
class ResolvedToken:
    token: str
    tier: TokenTier
    source: str  # "env:<VAR>", printed verbatim by whoami


def resolve_token(require_write: bool = False) -> ResolvedToken:
    """The first token found, writer before reader; the environment is the only source.

    Two variables are read so that one machine can hold both a bot that may
    post (DISCORD_BOT_TOKEN) and a read-only one that is safe to hand out
    (DISCORD_READER_TOKEN). The CLI and the MCP server only read. The library
    has two helpers that post, `DiscordClient.post_message` and
    `DiscordClient.start_thread`; `require_write` is for callers that use them
    and want to fail before the first request when only the reader is set.

    `load_env` has already folded a `.env` into the environment at CLI entry,
    so "environment" covers its values too. The token is fetched lazily by
    callers that actually talk to Discord, never at import time, so `--help`
    and argument errors never require one.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if token:
        return ResolvedToken(token, "writer", "env:DISCORD_BOT_TOKEN")

    if require_write:
        sys.exit(
            "[FAIL] this command writes to Discord and needs the writer token; "
            "set DISCORD_BOT_TOKEN in the environment or in a .env file"
        )

    token = os.environ.get("DISCORD_READER_TOKEN")
    if token:
        return ResolvedToken(token, "reader", "env:DISCORD_READER_TOKEN")

    sys.exit(
        "[FAIL] no Discord token found; set DISCORD_BOT_TOKEN or DISCORD_READER_TOKEN "
        "in the environment or in a .env file"
    )


def bot_token(require_write: bool = False) -> str:
    return resolve_token(require_write=require_write).token

"""Read-only CLI subcommands: `channels`, `pull`, `search`, `thread`.

Every handler builds its own `DiscordClient` and calls `config.bot_token()`
lazily, so `discord-client --help` and argument-parsing errors never require
a token to be present. Nothing here can write to Discord.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config as config_module
from .discord_api import SEARCH_MAX_LIMIT, DiscordAPIError, DiscordClient, SearchNotIndexed
from .ingest import display_name, normalize_message, pull_channel, transcript_markdown

_CHANNEL_TYPE_NAMES = {
    DiscordClient.TEXT: "text",
    DiscordClient.ANNOUNCEMENT: "announcement",
    DiscordClient.FORUM: "forum",
}

# Discord's three thread types: announcement, public, private. Captured at
# import time, because tests swap `DiscordClient` for a fake with no type
# constants of its own.
_THREAD_TYPES = (
    DiscordClient.ANNOUNCEMENT_THREAD,
    DiscordClient.PUBLIC_THREAD,
    DiscordClient.PRIVATE_THREAD,
)

_SINCE_DAYS_RE = re.compile(r"^(\d+)d$")


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Wire the four read-only subcommands into the shared parser."""
    _register_channels(subparsers)
    _register_pull(subparsers)
    _register_search(subparsers)
    _register_thread(subparsers)


# -- channels -----------------------------------------------------------------


def _register_channels(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "channels", help="list the guild's text/announcement/forum channels"
    )
    parser.set_defaults(func=_cmd_channels)


def _cmd_channels(args: argparse.Namespace) -> int:
    cfg = config_module.load_config()
    with DiscordClient(config_module.bot_token()) as client:
        channels = client.guild_channels(config_module.resolve_guild_id(client, cfg))

    rows = [
        (ch["name"], ch["id"], _CHANNEL_TYPE_NAMES[ch["type"]])
        for ch in channels
        if ch.get("type") in _CHANNEL_TYPE_NAMES
    ]
    if not rows:
        print("No text/announcement/forum channels found.")
        return 0

    name_width = max(len(row[0]) for row in rows)
    id_width = max(len(row[1]) for row in rows)
    for name, channel_id, kind in rows:
        print(f"{name:<{name_width}}  {channel_id:<{id_width}}  {kind}")
    return 0


# -- pull -----------------------------------------------------------------------


def _register_pull(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "pull", help="pull one channel's messages (and its threads) since a date"
    )
    parser.add_argument("--channel", required=True, help="channel name (without #) or id")
    parser.add_argument(
        "--since", required=True, help="how far back to pull: 'Nd' (last N days) or 'YYYY-MM-DD'"
    )
    parser.add_argument(
        "--json", action="store_true", help="print JSONL records instead of a markdown transcript"
    )
    parser.set_defaults(func=_cmd_pull)


def _parse_since(value: str, tz: str) -> datetime:
    """'Nd' means N days back from now; 'YYYY-MM-DD' means that date's local midnight."""
    zone = ZoneInfo(tz)
    days_match = _SINCE_DAYS_RE.match(value)
    if days_match:
        return datetime.now(zone) - timedelta(days=int(days_match.group(1)))
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        sys.exit(f"[FAIL] --since must be 'Nd' or 'YYYY-MM-DD', got {value!r}")
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=zone)


def _find_channel(channels: list[dict], name_or_id: str) -> dict:
    bare_name = name_or_id[1:] if name_or_id.startswith("#") else name_or_id
    for ch in channels:
        if ch["id"] == name_or_id or ch.get("name") == bare_name:
            return ch
    sys.exit(f"[FAIL] no channel named or with id {name_or_id!r}")


def _cmd_pull(args: argparse.Namespace) -> int:
    cfg = config_module.load_config()
    start = _parse_since(args.since, cfg.timezone)
    with DiscordClient(config_module.bot_token()) as client:
        guild_id = config_module.resolve_guild_id(client, cfg)
        channel = _find_channel(client.guild_channels(guild_id), args.channel)
        end = datetime.now(start.tzinfo)
        records = pull_channel(client, guild_id, channel, start, end)

    if args.json:
        for record in records:
            print(json.dumps(record))
    else:
        print(transcript_markdown(records, tz=cfg.timezone), end="")
    return 0


# -- search -----------------------------------------------------------------


def _register_search(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser("search", help="search the guild's messages")
    parser.add_argument("query", help="search query")
    parser.add_argument(
        "--limit",
        type=int,
        default=SEARCH_MAX_LIMIT,
        help=f"max results, 1 to {SEARCH_MAX_LIMIT} (default {SEARCH_MAX_LIMIT})",
    )
    parser.set_defaults(func=_cmd_search)


def _search_hits(result: dict) -> list[dict]:
    """The matched message from each result group (Discord returns match + context per group)."""
    return [group[0] for group in result.get("messages", []) if group]


def _cmd_search(args: argparse.Namespace) -> int:
    # Checked before the token, so a bad value fails without touching Discord,
    # which would otherwise answer 400.
    if not 1 <= args.limit <= SEARCH_MAX_LIMIT:
        sys.exit(f"[FAIL] --limit must be between 1 and {SEARCH_MAX_LIMIT}, got {args.limit}")
    cfg = config_module.load_config()
    with DiscordClient(config_module.bot_token()) as client:
        guild_id = config_module.resolve_guild_id(client, cfg)
        try:
            result = client.search(guild_id, args.query, limit=args.limit)
        except SearchNotIndexed:
            sys.exit(
                "[FAIL] this guild hasn't finished indexing for search yet. Discord indexes a "
                "guild's messages the first time anyone searches it; wait a few minutes and retry."
            )

    hits = _search_hits(result)
    if not hits:
        print(f"No results (total_results={result.get('total_results', 0)}).")
        return 0

    print(f"{result.get('total_results', len(hits))} result(s):")
    for msg in hits:
        author = display_name(msg.get("author") or {})
        snippet = (msg.get("content") or "").replace("\n", " ")[:100]
        jump_url = f"https://discord.com/channels/{guild_id}/{msg['channel_id']}/{msg['id']}"
        print(f"- {author} | channel {msg['channel_id']} | {snippet} | {jump_url}")
    return 0


# -- thread -----------------------------------------------------------------


def _register_thread(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser("thread", help="print the full transcript of one thread")
    parser.add_argument("id", help="thread (channel) id")
    parser.set_defaults(func=_cmd_thread)


def _lookup(client: DiscordClient, channel_id: str, what: str) -> dict:
    """GET one channel or thread, turning "not there" and "not ours" into a clean exit."""
    try:
        return client.channel(channel_id)
    except DiscordAPIError as exc:
        if exc.status_code == 404:
            sys.exit(f"[FAIL] no {what} with id {channel_id}")
        if exc.status_code == 403:
            sys.exit(f"[FAIL] the bot has no access to {what} {channel_id} (403)")
        raise


def _parent_name(client: DiscordClient, parent_id: str | None) -> str:
    """The parent channel's name, or "unknown" when the bot cannot see the parent.

    A thread can be readable while its parent is not; that must not cost the
    reader the transcript, so only the heading degrades.
    """
    if not parent_id:
        return "unknown"
    try:
        return client.channel(parent_id).get("name") or "unknown"
    except DiscordAPIError as exc:
        if exc.status_code not in (403, 404):
            raise
        return "unknown"


def _cmd_thread(args: argparse.Namespace) -> int:
    # Read for the timezone transcript times are shown in. The guild needs no
    # setting here: the thread object names its own.
    cfg = config_module.load_config()
    with DiscordClient(config_module.bot_token()) as client:
        # Looked up directly by id. Enumerating the guild's threads to find it
        # would touch every channel, and one the bot cannot read would fail
        # the whole command.
        target = _lookup(client, args.id, "thread or channel")
        if target.get("type") in _THREAD_TYPES:
            channel_name = _parent_name(client, target.get("parent_id"))
            thread_name = target.get("name")
        else:
            channel_name, thread_name = target.get("name") or "unknown", None
        records = [
            normalize_message(
                msg,
                channel_id=args.id,
                channel_name=channel_name,
                thread_name=thread_name,
                # The thread object names its own guild, so no setting is needed.
                guild_id=target.get("guild_id") or "@me",
            )
            for msg in client.messages(args.id)
        ]
    print(transcript_markdown(records, tz=cfg.timezone), end="")
    return 0

"""Windowed pull of a guild's messages into the shared record shape, plus a markdown transcript."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .discord_api import DiscordAPIError, DiscordClient

DISCORD_EPOCH_MS = 1420070400000

_MESSAGE_CHANNEL_TYPES = (DiscordClient.TEXT, DiscordClient.ANNOUNCEMENT)
_THREAD_PARENT_TYPES = (DiscordClient.TEXT, DiscordClient.ANNOUNCEMENT, DiscordClient.FORUM)
# The thread types read from the active-thread list: the same two kinds the
# archived-thread endpoint returns (announcement threads under announcement
# channels, public threads everywhere else). Private threads are not read.
_PUBLIC_THREAD_TYPES = (DiscordClient.ANNOUNCEMENT_THREAD, DiscordClient.PUBLIC_THREAD)


def day_window(day: date, tz: str) -> tuple[datetime, datetime]:
    """The [00:00, 24:00) window of `day` in the `tz` timezone, as aware datetimes."""
    zone = ZoneInfo(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    return start, start + timedelta(days=1)


def _snowflake(dt: datetime) -> str:
    unix_ms = int(dt.timestamp() * 1000)
    return str((unix_ms - DISCORD_EPOCH_MS) << 22)


def window_snowflakes(start: datetime, end: datetime) -> tuple[str, str]:
    """Convert a [start, end) datetime window to (after, before) snowflake strings.

    Discord's `after` is exclusive, so `after` is the start snowflake minus
    one; a message stamped exactly at the window's first millisecond is
    included.
    """
    return str(int(_snowflake(start)) - 1), _snowflake(end)


def display_name(author: dict) -> str:
    """The record's `author` field: global_name if set, else username."""
    return author.get("global_name") or author.get("username", "")


def normalize_message(
    msg: dict,
    *,
    channel_id: str,
    channel_name: str,
    thread_name: str | None,
    guild_id: str,
) -> dict:
    """Turn one raw Discord message into the shared message record.

    `channel_id` is the id of the message's immediate container: a text or
    announcement channel, or a thread. Discord jump URLs use that same id as
    their channel component, even when the container is a thread, so it
    doubles as the id `jump_url` is built from.
    """
    author = msg.get("author") or {}
    raw_attachments = [a for a in msg.get("attachments", []) if a.get("url")]
    attachment_urls = [a["url"] for a in raw_attachments]
    # `attachment_urls` is the flat list for callers that only need the URLs.
    # `attachments` adds the type and filename that media downloading and the
    # transcript need to tell a photo from a shell script.
    attachments = [
        {
            "url": a["url"],
            "filename": a.get("filename") or "attachment",
            "content_type": a.get("content_type") or "",
            "size": int(a.get("size") or 0),
        }
        for a in raw_attachments
    ]
    return {
        "id": msg["id"],
        "channel_id": channel_id,
        "channel_name": channel_name,
        "thread_name": thread_name,
        "author": display_name(author),
        "author_is_bot": bool(author.get("bot", False)),
        "timestamp": msg.get("timestamp", ""),
        "content": msg.get("content", ""),
        "attachment_urls": attachment_urls,
        "attachments": attachments,
        "jump_url": f"https://discord.com/channels/{guild_id}/{channel_id}/{msg['id']}",
    }


def _skip_no_access(exc: DiscordAPIError, label: str) -> None:
    """Re-raise anything but a 403.

    The guild channel list includes channels whose permission overwrites deny
    the bot access: /guilds/{id}/channels returns them, and their /messages
    then answers 403 with code 50001. So a 403 on one channel or thread means
    the bot may not read it, and the pull skips it rather than failing.
    """
    if exc.status_code != 403:
        raise exc
    print(f"[warn] skipping {label}: bot has no access (403)", file=sys.stderr)


def _plain_channel_messages(
    client: DiscordClient, guild_id: str, channel: dict, after: str, before: str
) -> list[dict]:
    """Messages posted directly in `channel` (nothing for forum channels, which hold only threads)."""
    if channel.get("type") not in _MESSAGE_CHANNEL_TYPES:
        return []
    try:
        msgs = list(client.messages(channel["id"], after=after, before=before))
    except DiscordAPIError as exc:
        _skip_no_access(exc, f"#{channel['name']}")
        return []
    return [
        normalize_message(
            msg,
            channel_id=channel["id"],
            channel_name=channel["name"],
            thread_name=None,
            guild_id=guild_id,
        )
        for msg in msgs
    ]


def _is_stale_archived(thread: dict, after_int: int) -> bool:
    """True when an archived thread's last message predates the window (nothing to pull)."""
    last_message_id = thread.get("last_message_id")
    return last_message_id is not None and int(last_message_id) < after_int


def _thread_messages(
    client: DiscordClient,
    guild_id: str,
    channel: dict,
    active_threads: list[dict],
    start: datetime,
    after: str,
    before: str,
) -> list[dict]:
    """Messages from every thread of `channel` (active or archived) that can fall in the window.

    Archived threads are paged back only to `start`: one archived before the
    window opened cannot hold a message inside it (see
    `DiscordClient.archived_public_threads`).
    """
    after_int, before_int = int(after), int(before)

    active_here = [
        t
        for t in active_threads
        if t.get("parent_id") == channel["id"] and t.get("type") in _PUBLIC_THREAD_TYPES
    ]
    try:
        archived_here = [
            t
            for t in client.archived_public_threads(channel["id"], since=start)
            if not _is_stale_archived(t, after_int)
        ]
    except DiscordAPIError as exc:
        _skip_no_access(exc, f"archived threads of #{channel['name']}")
        archived_here = []

    records: list[dict] = []
    seen_ids: set[str] = set()
    for thread in (*active_here, *archived_here):
        thread_id = thread["id"]
        if thread_id in seen_ids or int(thread_id) >= before_int:
            # a thread created at/after the window's end cannot hold an
            # in-window message, since every message in it postdates it
            continue
        seen_ids.add(thread_id)
        try:
            thread_msgs = list(client.messages(thread_id, after=after, before=before))
        except DiscordAPIError as exc:
            _skip_no_access(exc, f"thread {thread.get('name', thread_id)!r}")
            continue
        for msg in thread_msgs:
            records.append(
                normalize_message(
                    msg,
                    channel_id=thread_id,
                    channel_name=channel["name"],
                    thread_name=thread.get("name"),
                    guild_id=guild_id,
                )
            )
    return records


def pull_channel(
    client: DiscordClient, guild_id: str, channel: dict, start: datetime, end: datetime
) -> list[dict]:
    """One channel's messages, plus every thread of it, for [start, end)."""
    after, before = window_snowflakes(start, end)
    active_threads = client.active_threads(guild_id)

    records = _plain_channel_messages(client, guild_id, channel, after, before)
    records += _thread_messages(client, guild_id, channel, active_threads, start, after, before)

    records.sort(key=lambda r: int(r["id"]))
    return records


def pull_window(
    client: DiscordClient,
    guild_id: str,
    start: datetime,
    end: datetime,
    ignore_channels: Iterable[str] = (),
) -> list[dict]:
    """The whole guild's messages for [start, end): text/announcement channels and forum threads.

    Channels named in `ignore_channels` are skipped entirely, including any
    threads parented to them. Channels the bot cannot read are skipped with a
    `[warn]` on stderr rather than failing the pull.
    """
    all_channels = client.guild_channels(guild_id)
    ignore = set(ignore_channels)
    in_scope = [
        ch
        for ch in all_channels
        if ch.get("type") in _THREAD_PARENT_TYPES and ch.get("name") not in ignore
    ]

    after, before = window_snowflakes(start, end)
    active_threads = client.active_threads(guild_id)

    records: list[dict] = []
    for channel in in_scope:
        records += _plain_channel_messages(client, guild_id, channel, after, before)
        records += _thread_messages(
            client, guild_id, channel, active_threads, start, after, before
        )

    records.sort(key=lambda r: int(r["id"]))
    return records


def _format_time(timestamp: str, zone: ZoneInfo | None) -> str:
    """HH:MM in `zone`, or in the timestamp's own offset (UTC from Discord) when None."""
    moment = datetime.fromisoformat(timestamp)
    if zone is not None:
        moment = moment.astimezone(zone)
    return moment.strftime("%H:%M")


def _attachment_lines(record: dict, media_paths: dict[str, str]) -> list[str]:
    """One indented line per attachment, naming the local copy when one was fetched.

    Without this a photo-only message renders as an author and a timestamp with
    nothing between them, and whoever reads the transcript cannot tell that
    anything was posted at all.
    """
    lines = []
    for attachment in record.get("attachments") or []:
        name = attachment.get("filename") or "attachment"
        kind = attachment.get("content_type") or "unknown type"
        local = media_paths.get(attachment.get("url", ""))
        where = f" -> {local}" if local else " (not downloaded)"
        lines.append(f"  [attachment: {name}, {kind}{where}]")
    return lines


def _message_line(
    record: dict, media_paths: dict[str, str] | None = None, zone: ZoneInfo | None = None
) -> str:
    parts = [f"- **{record['author']}**"]
    if record["author_is_bot"]:
        parts.append("[bot]")
    parts.append(f"({_format_time(record['timestamp'], zone)})")
    content_lines = record["content"].splitlines()
    first, rest = (content_lines[0], content_lines[1:]) if content_lines else ("", [])
    if first:
        parts.append(first)
    parts.append(f"[↗]({record['jump_url']})")
    line = " ".join(parts)
    trailing = [f"  {rest_line}" for rest_line in rest]
    trailing += _attachment_lines(record, media_paths or {})
    if trailing:
        line += "\n" + "\n".join(trailing)
    return line


def transcript_markdown(
    records: list[dict],
    media_paths: dict[str, str] | None = None,
    tz: str | None = None,
) -> str:
    """Group `records` by channel, then thread, chronological within each group.

    Plain channel messages come before that channel's thread subsections.
    Channel and thread order follows first appearance in `records`, which is
    chronological order when `records` comes from `pull_window`/`pull_channel`.
    Threads are told apart by id (a thread message's `channel_id`), not by
    name, so two threads that share a name get a heading each.

    `media_paths` maps an attachment URL to the local path it was downloaded to
    (see `media.download_day`); attachments missing from it are marked
    "(not downloaded)".

    `tz` is the IANA zone message times are shown in. Pass the zone the
    window was built in, so the times match it. None shows each timestamp in
    its own offset, which for Discord's timestamps is UTC.
    """
    if not records:
        return "No messages in this window.\n"

    zone = ZoneInfo(tz) if tz is not None else None
    channels: dict[str, dict] = {}
    order: list[str] = []
    for record in records:
        name = record["channel_name"]
        if name not in channels:
            channels[name] = {"plain": [], "threads": {}}
            order.append(name)
        bucket = channels[name]
        thread_name = record["thread_name"]
        if thread_name is None:
            bucket["plain"].append(record)
        else:
            thread = bucket["threads"].setdefault(
                record["channel_id"], {"name": thread_name, "records": []}
            )
            thread["records"].append(record)

    sections = []
    for name in order:
        bucket = channels[name]
        lines = [f"## #{name}"]
        lines.extend(_message_line(r, media_paths, zone) for r in bucket["plain"])
        for thread in bucket["threads"].values():
            lines.append(f"### thread: {thread['name']}")
            lines.extend(_message_line(r, media_paths, zone) for r in thread["records"])
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import respx

from discord_client.discord_api import API_BASE_URL, DiscordClient
from discord_client.ingest import (
    day_window,
    display_name,
    normalize_message,
    pull_channel,
    pull_window,
    transcript_markdown,
    window_snowflakes,
)

GUILD_ID = "g1"
WINDOW_START = datetime(2026, 7, 11, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 12, tzinfo=timezone.utc)
AFTER, BEFORE = window_snowflakes(WINDOW_START, WINDOW_END)
AFTER_INT, BEFORE_INT = int(AFTER), int(BEFORE)

CH_GENERAL = "c1"
CH_BOT_LOGS = "c2"  # in ignore_channels
CH_PAPERS = "c3"  # forum

ACTIVE_THREAD = str(AFTER_INT + 500)  # in-window, parented to c1 -> pulled
TOO_NEW_THREAD = str(BEFORE_INT + 100)  # created after window end -> skipped
IGNORED_PARENT_THREAD = str(AFTER_INT + 700)  # parented to the ignored channel -> skipped
PRIVATE_THREAD_ID = str(AFTER_INT + 800)  # private, not public -> skipped

ARCHIVED_FRESH_THREAD = str(AFTER_INT + 600)  # last_message_id inside window -> pulled
ARCHIVED_STALE_THREAD = str(AFTER_INT + 10)  # last_message_id before window start -> skipped


IGNORE_CHANNELS = ("bot-logs",)


def discord_message(
    msg_id,
    *,
    author="alice",
    global_name=None,
    bot=False,
    content="hi",
    timestamp="2026-07-11T09:00:00+00:00",
    attachments=None,
):
    return {
        "id": msg_id,
        "author": {"username": author, "global_name": global_name, "bot": bot},
        "timestamp": timestamp,
        "content": content,
        "attachments": attachments or [],
    }


def _register_fixture_guild() -> dict[str, respx.Route]:
    """A guild with 2 text channels (one ignored), 1 forum, and a handful of threads.

    Registers only the routes a correct `pull_window` should ever hit; any
    other request (e.g. messages for a skipped thread, or bot-logs at all)
    fails the test via respx's unmocked-request error.
    """
    routes: dict[str, respx.Route] = {}

    routes["channels"] = respx.get(f"{API_BASE_URL}/guilds/{GUILD_ID}/channels").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": CH_GENERAL, "name": "general", "type": DiscordClient.TEXT},
                {"id": CH_BOT_LOGS, "name": "bot-logs", "type": DiscordClient.TEXT},
                {"id": CH_PAPERS, "name": "papers", "type": DiscordClient.FORUM},
            ],
        )
    )
    routes["active_threads"] = respx.get(f"{API_BASE_URL}/guilds/{GUILD_ID}/threads/active").mock(
        return_value=httpx.Response(
            200,
            json={
                "threads": [
                    {
                        "id": ACTIVE_THREAD,
                        "name": "live-chat",
                        "parent_id": CH_GENERAL,
                        "type": DiscordClient.PUBLIC_THREAD,
                    },
                    {
                        "id": TOO_NEW_THREAD,
                        "name": "too-new",
                        "parent_id": CH_GENERAL,
                        "type": DiscordClient.PUBLIC_THREAD,
                    },
                    {
                        "id": IGNORED_PARENT_THREAD,
                        "name": "ignored-parent",
                        "parent_id": CH_BOT_LOGS,
                        "type": DiscordClient.PUBLIC_THREAD,
                    },
                    {
                        "id": PRIVATE_THREAD_ID,
                        "name": "private-chat",
                        "parent_id": CH_GENERAL,
                        "type": DiscordClient.PRIVATE_THREAD,
                    },
                ],
                "members": [],
            },
        )
    )
    routes["archived_general"] = respx.get(
        f"{API_BASE_URL}/channels/{CH_GENERAL}/threads/archived/public"
    ).mock(return_value=httpx.Response(200, json={"threads": [], "members": [], "has_more": False}))
    routes["archived_papers"] = respx.get(
        f"{API_BASE_URL}/channels/{CH_PAPERS}/threads/archived/public"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "threads": [
                    {
                        "id": ARCHIVED_FRESH_THREAD,
                        "name": "paper-alpha",
                        "parent_id": CH_PAPERS,
                        "last_message_id": str(AFTER_INT + 650),
                    },
                    {
                        "id": ARCHIVED_STALE_THREAD,
                        "name": "paper-stale",
                        "parent_id": CH_PAPERS,
                        "last_message_id": str(AFTER_INT - 100),
                    },
                ],
                "members": [],
                "has_more": False,
            },
        )
    )

    routes["general_messages"] = respx.get(f"{API_BASE_URL}/channels/{CH_GENERAL}/messages").mock(
        return_value=httpx.Response(
            200,
            json=[
                discord_message(
                    str(AFTER_INT + 200),
                    author="bot-account",
                    bot=True,
                    content="daily digest",
                    timestamp="2026-07-11T09:05:00+00:00",
                ),
                discord_message(
                    str(AFTER_INT + 100),
                    author="alice",
                    global_name="Alice A.",
                    content="morning\nsecond line",
                    attachments=[{"url": "https://cdn.example.com/f.png"}],
                    timestamp="2026-07-11T09:00:00+00:00",
                ),
            ],  # newest first, like the real API
        )
    )
    routes["active_thread_messages"] = respx.get(f"{API_BASE_URL}/channels/{ACTIVE_THREAD}/messages").mock(
        return_value=httpx.Response(200, json=[discord_message(str(AFTER_INT + 550))])
    )
    routes["archived_fresh_messages"] = respx.get(
        f"{API_BASE_URL}/channels/{ARCHIVED_FRESH_THREAD}/messages"
    ).mock(return_value=httpx.Response(200, json=[discord_message(str(AFTER_INT + 640))]))

    return routes


def _client() -> DiscordClient:
    return DiscordClient("test-token")


# -- snowflake math -----------------------------------------------------------


def test_window_snowflakes_matches_known_timestamp():
    # 2026-07-11T00:00:00+02:00 == 2026-07-10T22:00:00Z; verified independently
    # against the formula snowflake = (unix_ms - 1420070400000) << 22. `after`
    # is one below the start snowflake because Discord's `after` is exclusive.
    start = datetime.fromisoformat("2026-07-11T00:00:00+02:00")
    end = datetime.fromisoformat("2026-07-12T00:00:00+02:00")

    after, before = window_snowflakes(start, end)

    assert after == "1525260327321599999"
    assert before == "1525622715187200000"


def test_day_window_is_local_midnight_to_midnight():
    start, end = day_window(date(2026, 7, 11), "Europe/Warsaw")

    assert start.isoformat() == "2026-07-11T00:00:00+02:00"
    assert end.isoformat() == "2026-07-12T00:00:00+02:00"


# -- normalize_message / display_name -----------------------------------------


def test_display_name_prefers_global_name():
    assert display_name({"username": "alice", "global_name": "Alice A."}) == "Alice A."


def test_display_name_falls_back_to_username():
    assert display_name({"username": "alice", "global_name": None}) == "alice"


def test_normalize_message_has_every_contract_key():
    msg = discord_message("42", global_name="Alice A.", attachments=[{"url": "https://x/y.png"}])
    record = normalize_message(
        msg, channel_id="c1", channel_name="general", thread_name=None, guild_id=GUILD_ID
    )

    assert set(record) == {
        "id",
        "channel_id",
        "channel_name",
        "thread_name",
        "author",
        "author_is_bot",
        "timestamp",
        "content",
        "attachment_urls",
        "attachments",
        "jump_url",
    }
    assert record["id"] == "42"
    assert record["author"] == "Alice A."
    assert record["author_is_bot"] is False
    assert record["attachment_urls"] == ["https://x/y.png"]
    assert record["jump_url"] == f"https://discord.com/channels/{GUILD_ID}/c1/42"


def test_normalize_message_keeps_attachment_type_and_name_beside_the_urls():
    msg = discord_message(
        "42",
        attachments=[
            {"url": "https://x/y.png", "filename": "y.png", "content_type": "image/png", "size": 7},
            {"url": "https://x/run.sh"},  # Discord omits content_type for some uploads
            {"filename": "no-url.png"},  # no URL: dropped from both lists
        ],
    )
    record = normalize_message(
        msg, channel_id="c1", channel_name="general", thread_name=None, guild_id=GUILD_ID
    )

    assert record["attachment_urls"] == ["https://x/y.png", "https://x/run.sh"]
    assert record["attachments"] == [
        {"url": "https://x/y.png", "filename": "y.png", "content_type": "image/png", "size": 7},
        {"url": "https://x/run.sh", "filename": "attachment", "content_type": "", "size": 0},
    ]


def test_normalize_message_thread_jump_url_uses_thread_id_as_channel():
    msg = discord_message("99")
    record = normalize_message(
        msg, channel_id="t1", channel_name="papers", thread_name="paper-alpha", guild_id=GUILD_ID
    )

    assert record["channel_id"] == "t1"
    assert record["thread_name"] == "paper-alpha"
    assert record["jump_url"] == f"https://discord.com/channels/{GUILD_ID}/t1/99"


# -- pull_window: enumeration and window filtering -----------------------------


@respx.mock
def test_pull_window_enumerates_channels_and_threads_correctly():
    routes = _register_fixture_guild()

    with _client() as client:
        records = pull_window(client, GUILD_ID, WINDOW_START, WINDOW_END, IGNORE_CHANNELS)

    ids = {r["id"] for r in records}
    assert ids == {
        str(AFTER_INT + 200),
        str(AFTER_INT + 100),
        str(AFTER_INT + 550),
        str(AFTER_INT + 640),
    }
    # bot-logs is ignored: neither its own messages nor any endpoint scoped to
    # it (archived threads) should ever be requested.
    assert routes["archived_general"].called
    assert routes["archived_papers"].called
    assert f"{API_BASE_URL}/channels/{CH_BOT_LOGS}" not in "".join(
        str(c.request.url) for c in respx.calls
    )


@respx.mock
def test_pull_window_skips_channels_the_bot_cannot_read(capsys):
    # The guild channel list includes channels whose overwrites deny the bot
    # access; their /messages returns 403 code 50001 and must be skipped, not
    # kill the pull.
    routes = _register_fixture_guild()
    routes["general_messages"].mock(
        return_value=httpx.Response(403, json={"message": "Missing Access", "code": 50001})
    )

    with _client() as client:
        records = pull_window(client, GUILD_ID, WINDOW_START, WINDOW_END, IGNORE_CHANNELS)

    ids = {r["id"] for r in records}
    assert ids == {str(AFTER_INT + 550), str(AFTER_INT + 640)}
    assert "skipping #general" in capsys.readouterr().err


@respx.mock
def test_pull_window_sends_window_bounds_to_messages_endpoint():
    _register_fixture_guild()

    with _client() as client:
        pull_window(client, GUILD_ID, WINDOW_START, WINDOW_END, IGNORE_CHANNELS)

    general_call = respx.get(f"{API_BASE_URL}/channels/{CH_GENERAL}/messages").calls.last
    assert general_call.request.url.params["after"] == AFTER
    assert general_call.request.url.params["limit"] == "100"


@respx.mock
def test_pull_window_record_shape_and_thread_fields():
    _register_fixture_guild()

    with _client() as client:
        records = pull_window(client, GUILD_ID, WINDOW_START, WINDOW_END, IGNORE_CHANNELS)

    by_id = {r["id"]: r for r in records}

    plain = by_id[str(AFTER_INT + 100)]
    assert plain["channel_id"] == CH_GENERAL
    assert plain["channel_name"] == "general"
    assert plain["thread_name"] is None
    assert plain["author"] == "Alice A."
    assert plain["author_is_bot"] is False
    assert plain["attachment_urls"] == ["https://cdn.example.com/f.png"]
    assert plain["jump_url"] == f"https://discord.com/channels/{GUILD_ID}/{CH_GENERAL}/{plain['id']}"

    bot_msg = by_id[str(AFTER_INT + 200)]
    assert bot_msg["author_is_bot"] is True

    thread_msg = by_id[str(AFTER_INT + 550)]
    assert thread_msg["channel_id"] == ACTIVE_THREAD
    assert thread_msg["channel_name"] == "general"
    assert thread_msg["thread_name"] == "live-chat"
    assert thread_msg["jump_url"] == f"https://discord.com/channels/{GUILD_ID}/{ACTIVE_THREAD}/{thread_msg['id']}"

    forum_thread_msg = by_id[str(AFTER_INT + 640)]
    assert forum_thread_msg["channel_id"] == ARCHIVED_FRESH_THREAD
    assert forum_thread_msg["channel_name"] == "papers"
    assert forum_thread_msg["thread_name"] == "paper-alpha"


@respx.mock
def test_pull_window_sorts_chronologically_by_id():
    _register_fixture_guild()

    with _client() as client:
        records = pull_window(client, GUILD_ID, WINDOW_START, WINDOW_END, IGNORE_CHANNELS)

    ids = [int(r["id"]) for r in records]
    assert ids == sorted(ids)


@respx.mock
def test_pull_channel_reads_active_announcement_threads():
    channel = {"id": "c9", "name": "announcements", "type": DiscordClient.ANNOUNCEMENT}
    announcement_thread = str(AFTER_INT + 300)
    respx.get(f"{API_BASE_URL}/guilds/{GUILD_ID}/threads/active").mock(
        return_value=httpx.Response(
            200,
            json={
                "threads": [
                    {
                        "id": announcement_thread,
                        "name": "launch-questions",
                        "parent_id": "c9",
                        "type": DiscordClient.ANNOUNCEMENT_THREAD,
                    }
                ],
                "members": [],
            },
        )
    )
    respx.get(f"{API_BASE_URL}/channels/c9/messages").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{API_BASE_URL}/channels/c9/threads/archived/public").mock(
        return_value=httpx.Response(200, json={"threads": [], "has_more": False})
    )
    respx.get(f"{API_BASE_URL}/channels/{announcement_thread}/messages").mock(
        return_value=httpx.Response(200, json=[discord_message(str(AFTER_INT + 350))])
    )

    with _client() as client:
        records = pull_channel(client, GUILD_ID, channel, WINDOW_START, WINDOW_END)

    assert [(r["id"], r["thread_name"]) for r in records] == [
        (str(AFTER_INT + 350), "launch-questions")
    ]


def _archived_thread(thread_id: str, name: str, archived_at: str, last_message_id: str) -> dict:
    return {
        "id": thread_id,
        "name": name,
        "parent_id": CH_GENERAL,
        "last_message_id": last_message_id,
        "thread_metadata": {"archive_timestamp": archived_at},
    }


@respx.mock
def test_archived_threads_on_a_later_page_are_still_read():
    # A thread archived after the window ends fills the first page; the one
    # holding the window's messages is on the second.
    routes = _register_fixture_guild()
    late = _archived_thread(
        str(BEFORE_INT + 10), "later-topic", "2026-07-13T08:00:00+00:00", str(BEFORE_INT + 20)
    )
    in_window = _archived_thread(
        str(AFTER_INT + 400), "older-topic", "2026-07-11T20:00:00+00:00", str(AFTER_INT + 450)
    )

    def pages(request: httpx.Request) -> httpx.Response:
        if "before" not in request.url.params:
            return httpx.Response(200, json={"threads": [late], "has_more": True})
        assert request.url.params["before"] == "2026-07-13T08:00:00+00:00"
        return httpx.Response(200, json={"threads": [in_window], "has_more": False})

    routes["archived_general"].mock(side_effect=pages)
    respx.get(f"{API_BASE_URL}/channels/{in_window['id']}/messages").mock(
        return_value=httpx.Response(200, json=[discord_message(str(AFTER_INT + 450))])
    )
    channel = {"id": CH_GENERAL, "name": "general", "type": DiscordClient.TEXT}

    with _client() as client:
        records = pull_channel(client, GUILD_ID, channel, WINDOW_START, WINDOW_END)

    assert str(AFTER_INT + 450) in {r["id"] for r in records}
    assert routes["archived_general"].call_count == 2


@respx.mock
def test_archived_threads_are_paged_back_only_to_the_window_start():
    routes = _register_fixture_guild()
    stale = _archived_thread(
        str(AFTER_INT - 5000), "old-topic", "2026-07-10T08:00:00+00:00", str(AFTER_INT - 4000)
    )

    def one_page(request: httpx.Request) -> httpx.Response:
        assert "before" not in request.url.params, "paged past the window start"
        return httpx.Response(200, json={"threads": [stale], "has_more": True})

    routes["archived_general"].mock(side_effect=one_page)
    channel = {"id": CH_GENERAL, "name": "general", "type": DiscordClient.TEXT}

    with _client() as client:
        pull_channel(client, GUILD_ID, channel, WINDOW_START, WINDOW_END)

    assert routes["archived_general"].call_count == 1


@respx.mock
def test_pull_channel_scopes_to_one_channel_and_its_threads():
    _register_fixture_guild()
    channel = {"id": CH_GENERAL, "name": "general", "type": DiscordClient.TEXT}

    with _client() as client:
        records = pull_channel(client, GUILD_ID, channel, WINDOW_START, WINDOW_END)

    ids = {r["id"] for r in records}
    assert ids == {str(AFTER_INT + 200), str(AFTER_INT + 100), str(AFTER_INT + 550)}
    assert all(r["channel_name"] == "general" for r in records)


# -- transcript_markdown -------------------------------------------------------


def test_transcript_markdown_empty():
    assert transcript_markdown([]) == "No messages in this window.\n"


def test_transcript_markdown_groups_plain_before_threads_chronologically():
    records = [
        normalize_message(
            discord_message("1", timestamp="2026-07-11T09:00:00+00:00"),
            channel_id="c1",
            channel_name="general",
            thread_name=None,
            guild_id=GUILD_ID,
        ),
        normalize_message(
            discord_message("2", timestamp="2026-07-11T09:05:00+00:00"),
            channel_id="t1",
            channel_name="general",
            thread_name="live-chat",
            guild_id=GUILD_ID,
        ),
        normalize_message(
            discord_message("3", timestamp="2026-07-11T09:10:00+00:00"),
            channel_id="t2",
            channel_name="papers",
            thread_name="paper-alpha",
            guild_id=GUILD_ID,
        ),
    ]

    md = transcript_markdown(records)

    assert md.index("## #general") < md.index("### thread: live-chat")
    assert md.index("### thread: live-chat") < md.index("## #papers")
    assert md.index("## #papers") < md.index("### thread: paper-alpha")


def test_transcript_markdown_bot_marker_and_time_and_jump_url():
    record = normalize_message(
        discord_message("1", author="digest-bot", bot=True, content="hi", timestamp="2026-07-11T14:30:00+00:00"),
        channel_id="c1",
        channel_name="general",
        thread_name=None,
        guild_id=GUILD_ID,
    )

    md = transcript_markdown([record])

    assert "**digest-bot** [bot] (14:30) hi" in md
    assert f"[↗](https://discord.com/channels/{GUILD_ID}/c1/1)" in md


def test_transcript_markdown_shows_times_in_the_given_timezone():
    record = normalize_message(
        discord_message("1", content="hi", timestamp="2026-07-11T22:30:00.123000+00:00"),
        channel_id="c1",
        channel_name="general",
        thread_name=None,
        guild_id=GUILD_ID,
    )

    assert "**alice** (00:30) hi" in transcript_markdown([record], tz="Europe/Warsaw")
    # without a zone, the timestamp's own offset: UTC for Discord's timestamps
    assert "**alice** (22:30) hi" in transcript_markdown([record])


def test_transcript_markdown_keeps_same_named_threads_apart():
    def in_thread(msg_id: str, thread_id: str) -> dict:
        return normalize_message(
            discord_message(msg_id, content=f"message {msg_id}"),
            channel_id=thread_id,
            channel_name="help",
            thread_name="question",
            guild_id=GUILD_ID,
        )

    md = transcript_markdown([in_thread("1", "t1"), in_thread("2", "t2"), in_thread("3", "t1")])

    assert md.count("### thread: question") == 2
    first, second = md.split("### thread: question")[1:]
    assert "message 1" in first and "message 3" in first and "message 2" not in first
    assert "message 2" in second


def test_transcript_markdown_indents_multiline_content():
    record = normalize_message(
        discord_message("1", content="line one\nline two", timestamp="2026-07-11T09:00:00+00:00"),
        channel_id="c1",
        channel_name="general",
        thread_name=None,
        guild_id=GUILD_ID,
    )

    md = transcript_markdown([record])

    assert "line one" in md
    assert "\n  line two" in md


def _record_with_attachments() -> dict:
    return normalize_message(
        discord_message(
            "1",
            content="look\nboth of these",
            attachments=[
                {"url": "https://x/a.png", "filename": "a.png", "content_type": "image/png"},
                {"url": "https://x/b.step", "filename": "b.step"},
            ],
        ),
        channel_id="c1",
        channel_name="general",
        thread_name=None,
        guild_id=GUILD_ID,
    )


def test_transcript_markdown_lists_every_attachment_after_the_text():
    md = transcript_markdown([_record_with_attachments()])

    assert md.index("\n  both of these") < md.index("[attachment: a.png")
    assert "\n  [attachment: a.png, image/png (not downloaded)]" in md
    assert "\n  [attachment: b.step, unknown type (not downloaded)]" in md


def test_transcript_markdown_names_the_local_copy_of_a_downloaded_attachment():
    md = transcript_markdown(
        [_record_with_attachments()], media_paths={"https://x/a.png": "media/1-a.png"}
    )

    assert "[attachment: a.png, image/png -> media/1-a.png]" in md
    assert "[attachment: b.step, unknown type (not downloaded)]" in md

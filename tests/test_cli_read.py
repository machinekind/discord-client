from __future__ import annotations

import json

import httpx
import pytest

from discord_client import cli_read
from discord_client import config as config_module
from discord_client.cli import main
from discord_client.config import Config
from discord_client.discord_api import DiscordAPIError, DiscordClient, SearchNotIndexed

GUILD_ID = "g1"


def make_config() -> Config:
    return Config(guild_id=GUILD_ID, timezone="Europe/Warsaw")


def _api_error(status: int, url: str) -> DiscordAPIError:
    return DiscordAPIError(
        httpx.Response(status, request=httpx.Request("GET", url), text=f"{status}")
    )


class FakeClient:
    """Stands in for DiscordClient: no network, canned responses."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def guild_channels(self, guild_id):
        return [
            {"id": "c1", "name": "general", "type": DiscordClient.TEXT},
            {"id": "c2", "name": "papers", "type": DiscordClient.FORUM},
        ]

    def my_guilds(self):
        return [{"id": GUILD_ID, "name": "Robotics"}]

    def channel(self, channel_id):
        known = {
            "500": {
                "id": "500",
                "name": "live-chat",
                "parent_id": "c1",
                "guild_id": GUILD_ID,
                "type": DiscordClient.PUBLIC_THREAD,
            },
            "600": {
                "id": "600",
                "name": "release-notes",
                "parent_id": "c1",
                "guild_id": GUILD_ID,
                "type": DiscordClient.ANNOUNCEMENT_THREAD,
            },
            "c1": {"id": "c1", "name": "general", "guild_id": GUILD_ID, "type": DiscordClient.TEXT},
        }
        if channel_id not in known:
            raise _api_error(404, f"https://discord.com/api/v10/channels/{channel_id}")
        return known[channel_id]

    def active_threads(self, guild_id):
        return [
            {
                "id": "500",
                "name": "live-chat",
                "parent_id": "c1",
                "type": DiscordClient.PUBLIC_THREAD,
            }
        ]

    def archived_public_threads(self, channel_id, since=None):
        return []

    def messages(self, channel_id, after=None, before=None):
        yield {
            "id": "1",
            "author": {"username": "bob", "global_name": None, "bot": False},
            "timestamp": "2026-07-11T10:00:00+00:00",
            "content": "hey",
            "attachments": [],
        }

    def search(self, guild_id, query, limit=25):
        return {
            "total_results": 1,
            "messages": [
                [
                    {
                        "id": "5",
                        "channel_id": "c1",
                        "author": {"username": "bob", "global_name": None},
                        "content": "hello world",
                    }
                ]
            ],
        }


class NotIndexedClient(FakeClient):
    def search(self, guild_id, query, limit=25):
        raise SearchNotIndexed(guild_id, "still indexing")


def _explode(*args, **kwargs):
    raise AssertionError("Discord must not be contacted")


@pytest.fixture
def patch_client(monkeypatch):
    """Patch the DiscordClient class and config loading; no network or env needed."""

    def _apply(client_cls=FakeClient):
        monkeypatch.setattr(cli_read, "DiscordClient", client_cls)
        monkeypatch.setattr(config_module, "load_config", make_config)
        monkeypatch.setattr(config_module, "bot_token", lambda: "test-token")

    return _apply


# -- channels -------------------------------------------------------------------


def test_channels_lists_text_announcement_forum(patch_client, capsys):
    patch_client()

    exit_code = main(["channels"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "general" in out and "c1" in out and "text" in out
    assert "papers" in out and "c2" in out and "forum" in out


# -- pull -------------------------------------------------------------------


def test_pull_prints_markdown_transcript_by_default(patch_client, capsys):
    patch_client()

    exit_code = main(["pull", "--channel", "general", "--since", "3d"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## #general" in out
    assert "bob" in out


def test_pull_shows_times_in_the_configured_timezone(patch_client, capsys):
    patch_client()

    main(["pull", "--channel", "general", "--since", "3d"])

    # 10:00 UTC is 12:00 in Europe/Warsaw (summer time), the zone --since was read in.
    assert "**bob** (12:00) hey" in capsys.readouterr().out


def test_pull_json_prints_jsonl_records(patch_client, capsys):
    patch_client()

    exit_code = main(["pull", "--channel", "general", "--since", "3d", "--json"])

    out = capsys.readouterr().out
    assert exit_code == 0
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert records
    assert records[0]["channel_name"] == "general"
    assert records[0]["author"] == "bob"
    assert set(records[0]) == {
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


def test_pull_accepts_channel_by_id(patch_client, capsys):
    patch_client()

    exit_code = main(["pull", "--channel", "c1", "--since", "2026-07-01"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## #general" in out


def test_pull_unknown_channel_exits(patch_client):
    patch_client()

    with pytest.raises(SystemExit):
        main(["pull", "--channel", "does-not-exist", "--since", "3d"])


def test_pull_bad_since_exits(patch_client):
    patch_client()

    with pytest.raises(SystemExit):
        main(["pull", "--channel", "general", "--since", "not-a-date"])


# -- search -----------------------------------------------------------------


def test_search_happy_path(patch_client, capsys):
    patch_client()

    exit_code = main(["search", "hello"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "bob" in out
    assert "channel c1" in out
    assert "hello world" in out


def test_search_not_indexed_fails_on_stderr_not_stdout(patch_client, capsys):
    patch_client(NotIndexedClient)

    # sys.exit with a message: Python prints it to stderr and exits 1.
    with pytest.raises(SystemExit) as excinfo:
        main(["search", "hello"])

    assert str(excinfo.value).startswith("[FAIL]")
    assert "index" in str(excinfo.value).lower()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("limit", ["0", "26", "-1"])
def test_search_rejects_a_limit_outside_discords_range(patch_client, monkeypatch, limit):
    patch_client()
    monkeypatch.setattr(cli_read, "DiscordClient", _explode)

    with pytest.raises(SystemExit) as excinfo:
        main(["search", f"--limit={limit}", "hello"])

    assert str(excinfo.value) == f"[FAIL] --limit must be between 1 and 25, got {limit}"


@pytest.mark.parametrize("limit", ["1", "25"])
def test_search_accepts_the_ends_of_discords_range(patch_client, limit):
    patch_client()

    assert main(["search", f"--limit={limit}", "hello"]) == 0


# -- thread -----------------------------------------------------------------


def test_thread_prints_transcript_with_resolved_names(patch_client, capsys):
    patch_client()

    exit_code = main(["thread", "500"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## #general" in out
    assert "### thread: live-chat" in out
    assert "bob" in out


def test_thread_shows_times_in_the_configured_timezone(patch_client, capsys):
    patch_client()

    main(["thread", "500"])

    assert "**bob** (12:00) hey" in capsys.readouterr().out


def test_thread_recognises_an_announcement_thread(patch_client, capsys):
    patch_client()

    exit_code = main(["thread", "600"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## #general" in out
    assert "### thread: release-notes" in out


def test_thread_builds_jump_urls_from_the_threads_own_guild(patch_client, capsys):
    patch_client()

    main(["thread", "500"])

    assert f"https://discord.com/channels/{GUILD_ID}/500/1" in capsys.readouterr().out


def test_thread_accepts_a_plain_channel_id(patch_client, capsys):
    patch_client()

    exit_code = main(["thread", "c1"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## #general" in out
    assert "### thread:" not in out


def test_thread_unknown_id_exits_with_a_clear_message(patch_client):
    patch_client()

    with pytest.raises(SystemExit) as excinfo:
        main(["thread", "does-not-exist"])

    assert "no thread or channel with id does-not-exist" in str(excinfo.value)


def test_thread_survives_a_parent_the_bot_cannot_read(patch_client, capsys):
    # A thread can be readable while its parent channel is not. Only the
    # heading degrades; the transcript is still printed.
    class HiddenParentClient(FakeClient):
        def channel(self, channel_id):
            if channel_id == "c1":
                raise _api_error(403, "https://discord.com/api/v10/channels/c1")
            return super().channel(channel_id)

    patch_client(HiddenParentClient)

    exit_code = main(["thread", "500"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## #unknown" in out
    assert "### thread: live-chat" in out


def test_thread_without_access_exits_with_a_clear_message(patch_client):
    class NoAccessClient(FakeClient):
        def channel(self, channel_id):
            raise _api_error(403, f"https://discord.com/api/v10/channels/{channel_id}")

    patch_client(NoAccessClient)

    with pytest.raises(SystemExit) as excinfo:
        main(["thread", "500"])

    assert "no access" in str(excinfo.value)


# -- guild resolution ---------------------------------------------------------


def test_guild_is_taken_from_the_bots_only_guild_when_unconfigured(monkeypatch, capsys):
    seen: list[str] = []

    class RecordingClient(FakeClient):
        def guild_channels(self, guild_id):
            seen.append(guild_id)
            return super().guild_channels(guild_id)

    monkeypatch.setattr(cli_read, "DiscordClient", RecordingClient)
    monkeypatch.setattr(config_module, "bot_token", lambda: "test-token")

    exit_code = main(["channels"])

    assert exit_code == 0
    assert seen == [GUILD_ID]

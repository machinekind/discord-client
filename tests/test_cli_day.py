from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from discord_client import cli_day
from discord_client import config as config_module
from discord_client.cli import main
from discord_client.config import Config
from discord_client.discord_api import DiscordAPIError, DiscordClient
from discord_client.ingest import day_window, window_snowflakes

GUILD_ID = "g1"
TIMEZONE = "Europe/Warsaw"

# Signed CDN URLs. 0xf4865700 is 2100-01-01 UTC, valid whenever the suite runs;
# 0x5fee6600 is 2021-01-01 UTC, long lapsed.
PHOTO_URL = "https://cdn.discordapp.com/attachments/1/101/photo.png?ex=f4865700&is=1&hm=a"
OLD_URL = "https://cdn.discordapp.com/attachments/1/102/old.png?ex=5fee6600&is=1&hm=b"
SCRIPT_URL = "https://cdn.discordapp.com/attachments/1/102/setup.sh?ex=f4865700&is=1&hm=c"

CHANNELS = [
    {"id": "c1", "name": "general", "type": DiscordClient.TEXT},
    {"id": "c2", "name": "bot-logs", "type": DiscordClient.TEXT},  # ignored by config
    {"id": "c3", "name": "papers", "type": DiscordClient.FORUM},
]
FORUM_THREAD = {
    "id": "500",
    "name": "paper-alpha",
    "parent_id": "c3",
    "type": DiscordClient.PUBLIC_THREAD,
}


def _msg(msg_id: str, author: str, content: str, attachments: list[dict] | None = None) -> dict:
    return {
        "id": msg_id,
        "author": {"username": author, "global_name": None, "bot": False},
        "timestamp": "2026-07-11T10:00:00+00:00",
        "content": content,
        "attachments": attachments or [],
    }


DAY_MESSAGES = {
    "c1": [
        _msg(
            "101",
            "alice",
            "café prototype, first light",
            [{"url": PHOTO_URL, "filename": "photo.png", "content_type": "image/png", "size": 4}],
        ),
        _msg(
            "102",
            "bob",
            "older shot and the script",
            [
                {"url": OLD_URL, "filename": "old.png", "content_type": "image/png", "size": 9},
                {"url": SCRIPT_URL, "filename": "setup.sh", "content_type": "text/x-sh"},
            ],
        ),
    ],
    "c2": [_msg("103", "log-bot", "this channel is ignored")],
    "500": [_msg("104", "bob", "reply in the forum thread")],
}


def make_config(**overrides) -> Config:
    settings = {"guild_id": GUILD_ID, "ignore_channels": ("bot-logs",), "timezone": TIMEZONE}
    return Config(**(settings | overrides))


def _api_error(status: int, url: str) -> DiscordAPIError:
    return DiscordAPIError(
        httpx.Response(status, request=httpx.Request("GET", url), text=f"{status}")
    )


def make_client(messages: dict[str, list[dict]], forbidden: frozenset[str] = frozenset()):
    class FakeClient:
        """Stands in for DiscordClient: no network, canned channels and messages."""

        calls: list[tuple[str, str | None, str | None]] = []
        guilds_read: list[str] = []

        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def my_guilds(self):
            return [{"id": GUILD_ID, "name": "Test Guild"}]

        def guild_channels(self, guild_id):
            self.guilds_read.append(guild_id)
            return CHANNELS

        def active_threads(self, guild_id):
            return [FORUM_THREAD]

        def archived_public_threads(self, channel_id, since=None):
            return []

        def messages(self, channel_id, after=None, before=None):
            self.calls.append((channel_id, after, before))
            if channel_id in forbidden:
                raise _api_error(403, f"https://discord.com/api/v10/channels/{channel_id}/messages")
            yield from messages.get(channel_id, [])

    return FakeClient


@pytest.fixture
def patch_env(monkeypatch):
    """Patch the client class, config, and token; returns the fake class for inspection."""

    def _apply(messages=DAY_MESSAGES, *, forbidden=frozenset(), config=None):
        client_cls = make_client(messages, frozenset(forbidden))
        cfg = config or make_config()
        monkeypatch.setattr(cli_day, "DiscordClient", client_cls)
        monkeypatch.setattr(config_module, "load_config", lambda: cfg)
        monkeypatch.setattr(config_module, "bot_token", lambda: "test-token")
        return client_cls

    return _apply


def _explode(*args, **kwargs):
    raise AssertionError("Discord must not be contacted")


# -- stdout form --------------------------------------------------------------


@respx.mock  # no routes: any CDN request would fail the test
def test_stdout_prints_the_markdown_transcript_and_downloads_nothing(patch_env, tmp_path, capsys):
    patch_env()

    exit_code = main(["day", "--date=2026-07-11"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "## #general" in captured.out
    assert "### thread: paper-alpha" in captured.out
    assert "café prototype" in captured.out
    assert "[attachment: photo.png, image/png (not downloaded)]" in captured.out
    # 10:00 UTC shown in the configured zone, the one the day window is cut in
    assert "**alice** (12:00) café prototype" in captured.out
    assert captured.err == ""
    assert list(tmp_path.iterdir()) == []  # the working directory: nothing written


@respx.mock
def test_stdout_json_prints_one_record_per_line(patch_env, capsys):
    patch_env()

    exit_code = main(["day", "--date", "2026-07-11", "--json"])

    out = capsys.readouterr().out
    assert exit_code == 0
    records = [json.loads(line) for line in out.splitlines()]
    assert [r["id"] for r in records] == ["101", "102", "104"]
    assert records[0]["attachments"] == [
        {"url": PHOTO_URL, "filename": "photo.png", "content_type": "image/png", "size": 4}
    ]


def test_stdout_carries_only_the_transcript_when_a_channel_is_unreadable(patch_env, capsys):
    patch_env(forbidden={"c1"})

    exit_code = main(["day", "--date=2026-07-11"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.startswith("## #papers\n")
    assert "[warn]" not in captured.out
    assert "[warn] skipping #general" in captured.err


def test_a_day_with_no_messages_prints_the_empty_transcript_and_succeeds(patch_env, capsys):
    patch_env(messages={})

    exit_code = main(["day", "--date", "2026-07-11"])

    assert exit_code == 0
    assert capsys.readouterr().out == "No messages in this window.\n"


# -- what gets read -----------------------------------------------------------


def test_window_is_the_whole_day_in_the_configured_timezone(patch_env, capsys):
    client_cls = patch_env()

    main(["day", "--date", "2026-07-11"])

    expected = window_snowflakes(*day_window(date(2026, 7, 11), TIMEZONE))
    assert client_cls.calls
    assert {(after, before) for _, after, before in client_cls.calls} == {expected}


def test_ignored_channels_are_never_read(patch_env, capsys):
    client_cls = patch_env()

    main(["day", "--date", "2026-07-11"])

    assert "c2" not in {channel for channel, _, _ in client_cls.calls}
    assert "ignored" not in capsys.readouterr().out


def test_guild_is_resolved_when_not_configured(patch_env, capsys):
    client_cls = patch_env(config=make_config(guild_id=None))

    assert main(["day", "--date", "2026-07-11"]) == 0
    assert client_cls.guilds_read == [GUILD_ID]


# -- --out form ---------------------------------------------------------------


@respx.mock
def test_out_writes_transcript_media_and_manifest(patch_env, tmp_path, capsys):
    photo = respx.get(PHOTO_URL).mock(return_value=httpx.Response(200, content=b"\x89PNG"))
    patch_env()
    out_dir = tmp_path / "day-2026-07-11"

    exit_code = main(["day", "--date", "2026-07-11", "--out", str(out_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == (
        f"wrote {out_dir}: 3 message(s) across 2 channel(s); "
        "media: 1 downloaded, 1 expired, 0 failed\n"
    )
    assert "[warn] 1 image/video attachment(s) could not be downloaded" in captured.err
    assert photo.call_count == 1  # the expired image and the script are never requested

    assert (out_dir / "media" / "101-photo.png").read_bytes() == b"\x89PNG"
    transcript = (out_dir / "transcript.md").read_text(encoding="utf-8")
    assert "café prototype" in transcript
    assert "**alice** (12:00) café prototype" in transcript
    assert "[attachment: photo.png, image/png -> media/101-photo.png]" in transcript
    assert "[attachment: old.png, image/png (not downloaded)]" in transcript
    assert "[attachment: setup.sh, text/x-sh (not downloaded)]" in transcript
    manifest = (out_dir / "media" / "index.md").read_text(encoding="utf-8")
    assert "`media/101-photo.png`" in manifest
    assert "Expired before download" in manifest


SHOT_1 = "https://cdn.discordapp.com/attachments/1/201/image.png?ex=f4865700&is=1&hm=d"
SHOT_2 = "https://cdn.discordapp.com/attachments/1/202/image.png?ex=f4865700&is=1&hm=e"


@respx.mock
def test_out_points_each_same_named_attachment_at_its_own_file(patch_env, tmp_path, capsys):
    # Pasted screenshots all arrive as image.png, and each must keep its own file.
    respx.get(SHOT_1).mock(return_value=httpx.Response(200, content=b"first"))
    respx.get(SHOT_2).mock(return_value=httpx.Response(200, content=b"second"))
    shot = {"filename": "image.png", "content_type": "image/png", "size": 5}
    shots = [shot | {"url": SHOT_1}, shot | {"url": SHOT_2}]
    patch_env(messages={"c1": [_msg("105", "alice", "two screenshots", shots)]})
    out_dir = tmp_path / "dump"

    assert main(["day", "--date", "2026-07-11", "--out", str(out_dir)]) == 0

    assert (out_dir / "media" / "105-image.png").read_bytes() == b"first"
    assert (out_dir / "media" / "105-image-2.png").read_bytes() == b"second"
    transcript = (out_dir / "transcript.md").read_text(encoding="utf-8")
    first = transcript.index("[attachment: image.png, image/png -> media/105-image.png]")
    second = transcript.index("[attachment: image.png, image/png -> media/105-image-2.png]")
    assert first < second  # in the message's own attachment order
    manifest = (out_dir / "media" / "index.md").read_text(encoding="utf-8")
    assert "`media/105-image.png`" in manifest and "`media/105-image-2.png`" in manifest


@respx.mock  # no routes: --no-media must not request anything from the CDN
def test_out_with_no_media_writes_the_skipped_manifest(patch_env, tmp_path, capsys):
    patch_env()
    out_dir = tmp_path / "dump"

    exit_code = main(["day", "--date", "2026-07-11", "--out", str(out_dir), "--no-media"])

    assert exit_code == 0
    assert capsys.readouterr().out.endswith("; media: skipped (--no-media)\n")
    assert sorted(p.name for p in (out_dir / "media").iterdir()) == ["index.md"]
    manifest = (out_dir / "media" / "index.md").read_text(encoding="utf-8")
    assert "--no-media" in manifest
    assert "Do not describe any image or video" in manifest
    transcript = (out_dir / "transcript.md").read_text(encoding="utf-8")
    assert "[attachment: photo.png, image/png (not downloaded)]" in transcript


def test_out_on_a_day_with_no_messages_writes_the_empty_transcript(patch_env, tmp_path, capsys):
    patch_env(messages={})
    out_dir = tmp_path / "dump"

    exit_code = main(["day", "--date", "2026-07-11", "--out", str(out_dir)])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        f"wrote {out_dir}: 0 message(s) across 0 channel(s); "
        "media: 0 downloaded, 0 expired, 0 failed\n"
    )
    transcript = (out_dir / "transcript.md").read_text(encoding="utf-8")
    assert transcript == "No messages in this window.\n"
    assert (out_dir / "media" / "index.md").is_file()


def test_out_creates_missing_parent_directories(patch_env, tmp_path, capsys):
    patch_env(messages={})
    out_dir = tmp_path / "dumps" / "2026" / "07-11"

    assert main(["day", "--date", "2026-07-11", "--out", str(out_dir), "--no-media"]) == 0
    assert (out_dir / "transcript.md").is_file()


def test_out_accepts_an_existing_empty_directory(patch_env, tmp_path, capsys):
    patch_env(messages={})
    out_dir = tmp_path / "dump"
    out_dir.mkdir()

    assert main(["day", "--date", "2026-07-11", "--out", str(out_dir), "--no-media"]) == 0
    assert (out_dir / "transcript.md").is_file()


def test_out_refuses_a_non_empty_directory_before_touching_discord(
    patch_env, monkeypatch, tmp_path
):
    patch_env()
    monkeypatch.setattr(cli_day, "DiscordClient", _explode)
    out_dir = tmp_path / "dump"
    out_dir.mkdir()
    (out_dir / "transcript.md").write_text("an earlier day", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(["day", "--date", "2026-07-11", "--out", str(out_dir)])

    assert str(excinfo.value).startswith("[FAIL]")
    assert "not empty" in str(excinfo.value)
    assert (out_dir / "transcript.md").read_text(encoding="utf-8") == "an earlier day"


def test_out_refuses_a_path_that_is_a_file(patch_env, monkeypatch, tmp_path):
    patch_env()
    monkeypatch.setattr(cli_day, "DiscordClient", _explode)
    target = tmp_path / "dump"
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(["day", "--date", "2026-07-11", "--out", str(target)])

    assert "[FAIL]" in str(excinfo.value) and "not a directory" in str(excinfo.value)


# -- argument errors ----------------------------------------------------------


def test_json_and_out_are_mutually_exclusive(patch_env, tmp_path, capsys):
    patch_env()

    with pytest.raises(SystemExit) as excinfo:
        main(["day", "--date", "2026-07-11", "--json", "--out", str(tmp_path / "dump")])

    assert excinfo.value.code == 2
    assert "not allowed with" in capsys.readouterr().err
    assert not (tmp_path / "dump").exists()


def test_no_media_without_out_is_refused(patch_env, monkeypatch):
    patch_env()
    monkeypatch.setattr(cli_day, "DiscordClient", _explode)

    with pytest.raises(SystemExit) as excinfo:
        main(["day", "--date", "2026-07-11", "--no-media"])

    assert str(excinfo.value).startswith("[FAIL] --no-media only applies with --out")


def test_bad_date_exits_with_a_fail_message(patch_env, monkeypatch):
    patch_env()
    monkeypatch.setattr(cli_day, "DiscordClient", _explode)

    with pytest.raises(SystemExit) as excinfo:
        main(["day", "--date", "11/07/2026"])

    assert str(excinfo.value) == "[FAIL] --date must be YYYY-MM-DD, got '11/07/2026'"

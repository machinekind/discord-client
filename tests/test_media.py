from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from discord_client import media
from discord_client.media import (
    MediaReport,
    download_day,
    expiry,
    guess_type,
    is_expired,
    is_wanted,
    items_from_records,
    manifest_markdown,
    safe_name,
    unique_name,
    write_skipped_manifest,
)

CDN = "https://cdn.discordapp.com/attachments/1/2/photo.png"
# 0x6ab26dc0 is 2026-09-22 12:00 UTC: after NOW, so the signature is still valid.
SIGNED = f"{CDN}?ex=6ab26dc0&is=68cf4e80&hm=deadbeef"
NOW = datetime(2026, 9, 21, tzinfo=UTC)


def _record(attachments: list[dict] | None = None, **kw) -> dict:
    base = {
        "id": "555",
        "channel_id": "c1",
        "channel_name": "general",
        "thread_name": None,
        "author": "alice",
        "author_is_bot": False,
        "timestamp": "2026-09-20T14:22:00+00:00",
        "content": "it works — finally",
        "attachment_urls": [a["url"] for a in attachments or [] if a.get("url")],
        "attachments": attachments or [],
        "jump_url": "https://discord.com/channels/g1/c1/555",
    }
    base.update(kw)
    return base


def _attachment(**kw) -> dict:
    return {
        "url": SIGNED,
        "filename": "photo.png",
        "content_type": "image/png",
        "size": 1234,
    } | kw


# -- signed URL expiry --------------------------------------------------------


def test_expiry_decodes_the_hex_timestamp():
    assert expiry(SIGNED) == datetime.fromtimestamp(0x6AB26DC0, UTC)


def test_url_without_a_signature_has_no_known_expiry():
    assert expiry(CDN) is None
    assert not is_expired(CDN, NOW)


def test_malformed_expiry_is_treated_as_unknown_not_as_expired():
    assert expiry(f"{CDN}?ex=nothex") is None
    assert not is_expired(f"{CDN}?ex=nothex", NOW)


def test_is_expired_compares_against_the_given_moment():
    past = datetime(2030, 1, 1, tzinfo=UTC)
    assert is_expired(SIGNED, past)
    assert not is_expired(SIGNED, NOW)


# -- filename safety ----------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../.ssh/authorized_keys",
        "/etc/passwd",
        "..\\..\\windows\\system32\\evil.dll",
        "....//....//escape.png",
    ],
)
def test_safe_name_cannot_escape_the_media_directory(hostile, tmp_path):
    """Attachment filenames are chosen by whoever uploaded them, so they are hostile input."""
    name = safe_name("999", hostile)
    resolved = (tmp_path / name).resolve()

    assert "/" not in name and "\\" not in name
    assert resolved.parent == tmp_path.resolve()


def test_safe_name_disarms_a_leading_dot():
    assert not safe_name("1", ".bashrc").startswith(".")


def test_safe_name_keeps_two_peoples_files_apart():
    assert safe_name("1", "IMG_0001.png") != safe_name("2", "IMG_0001.png")


def test_safe_name_survives_an_empty_or_stripped_filename():
    assert safe_name("7", "...") == "7-attachment"


def test_long_names_differing_past_the_cut_do_not_collide():
    """Truncation alone made the second download silently overwrite the first."""
    a = "x" * 90 + "_version_A.png"
    b = "x" * 90 + "_version_B.png"
    assert safe_name("555", a) != safe_name("555", b)


def test_truncation_keeps_the_extension():
    name = safe_name("555", "y" * 120 + ".mp4")
    assert name.endswith(".mp4")
    assert len(name) < 90


def test_unique_name_numbers_a_repeat_before_the_extension(tmp_path):
    taken: set[str] = set()
    assert unique_name("1-image.png", taken, tmp_path) == "1-image.png"
    assert unique_name("1-image.png", taken, tmp_path) == "1-image-2.png"
    assert unique_name("1-image.png", taken, tmp_path) == "1-image-3.png"


def test_unique_name_treats_names_differing_only_in_case_as_the_same(tmp_path):
    # On the default macOS and Windows filesystems they are the same file.
    taken: set[str] = set()
    unique_name("1-image.png", taken, tmp_path)
    assert unique_name("1-IMAGE.png", taken, tmp_path) == "1-IMAGE-2.png"


def test_unique_name_numbers_a_name_without_an_extension(tmp_path):
    taken: set[str] = set()
    unique_name("7-attachment", taken, tmp_path)
    assert unique_name("7-attachment", taken, tmp_path) == "7-attachment-2"


def test_unique_name_never_picks_a_file_already_on_disk(tmp_path):
    (tmp_path / "1-image.png").write_bytes(b"an earlier run")
    assert unique_name("1-image.png", set(), tmp_path) == "1-image-2.png"


# -- what is worth downloading ------------------------------------------------


def test_only_pictures_and_video_are_wanted():
    assert is_wanted("image/png")
    assert is_wanted("video/mp4")
    assert not is_wanted("text/markdown")
    assert not is_wanted("")


def test_type_falls_back_to_the_extension_when_discord_omits_it():
    assert guess_type("clip.MOV", "") == "video/quicktime"
    assert guess_type("bracket.step", "") == ""
    assert guess_type("x.png", "image/jpeg") == "image/jpeg"


def test_items_carry_the_message_context_that_explains_them():
    (item,) = items_from_records([_record([_attachment()])])
    assert item.author == "alice"
    assert item.jump_url.endswith("/555")
    assert item.content == "it works — finally"


def test_attachment_without_a_url_is_ignored():
    assert items_from_records([_record([{"filename": "x.png"}])]) == []


# -- downloading --------------------------------------------------------------


@respx.mock
def test_downloads_pictures_and_writes_them_where_the_manifest_says(tmp_path):
    respx.get(SIGNED).mock(return_value=httpx.Response(200, content=b"\x89PNG..."))

    report = download_day([_record([_attachment()])], tmp_path, now=NOW)

    (item, path) = report.downloaded[0]
    assert (tmp_path / path).read_bytes() == b"\x89PNG..."
    assert path == "media/555-photo.png"
    assert item.filename == "photo.png"
    assert path in (tmp_path / "media" / "index.md").read_text(encoding="utf-8")


SHOT_1 = "https://cdn.discordapp.com/attachments/1/11/image.png?ex=6ab26dc0&is=1&hm=a"
SHOT_2 = "https://cdn.discordapp.com/attachments/1/12/image.png?ex=6ab26dc0&is=1&hm=b"


def _two_screenshots() -> dict:
    """One message with two attachments that share a name, as pasted screenshots do."""
    return _record(
        [
            _attachment(url=SHOT_1, filename="image.png"),
            _attachment(url=SHOT_2, filename="image.png"),
        ]
    )


@respx.mock
def test_same_named_attachments_of_one_message_each_get_their_own_file(tmp_path):
    respx.get(SHOT_1).mock(return_value=httpx.Response(200, content=b"first"))
    respx.get(SHOT_2).mock(return_value=httpx.Response(200, content=b"second"))

    report = download_day([_two_screenshots()], tmp_path, now=NOW)

    paths = {item.url: path for item, path in report.downloaded}
    assert paths == {SHOT_1: "media/555-image.png", SHOT_2: "media/555-image-2.png"}
    assert (tmp_path / paths[SHOT_1]).read_bytes() == b"first"
    assert (tmp_path / paths[SHOT_2]).read_bytes() == b"second"
    manifest = (tmp_path / "media" / "index.md").read_text(encoding="utf-8")
    assert "`media/555-image.png`" in manifest and "`media/555-image-2.png`" in manifest


@respx.mock
def test_a_failed_download_never_removes_another_attachments_file(tmp_path, monkeypatch):
    # The second screenshot fails part-way, after its own file was opened.
    monkeypatch.setattr(media, "MAX_BYTES", 1024)
    respx.get(SHOT_1).mock(return_value=httpx.Response(200, content=b"first"))
    respx.get(SHOT_2).mock(return_value=httpx.Response(200, content=b"x" * 1025))

    report = download_day([_two_screenshots()], tmp_path, now=NOW)

    assert [path for _, path in report.downloaded] == ["media/555-image.png"]
    assert [item.url for item, _ in report.failed] == [SHOT_2]
    assert (tmp_path / "media" / "555-image.png").read_bytes() == b"first"
    assert not (tmp_path / "media" / "555-image-2.png").exists()


@respx.mock
def test_a_file_already_in_the_media_directory_is_never_overwritten(tmp_path):
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "555-photo.png").write_bytes(b"an earlier run")
    respx.get(SIGNED).mock(return_value=httpx.Response(200, content=b"today"))

    report = download_day([_record([_attachment()])], tmp_path, now=NOW)

    assert [path for _, path in report.downloaded] == ["media/555-photo-2.png"]
    assert (tmp_path / "media" / "555-photo.png").read_bytes() == b"an earlier run"
    assert (tmp_path / "media" / "555-photo-2.png").read_bytes() == b"today"


@respx.mock
def test_expired_attachment_is_reported_not_fetched(tmp_path):
    route = respx.get(SIGNED).mock(return_value=httpx.Response(200, content=b"x"))

    report = download_day(
        [_record([_attachment()])], tmp_path, now=datetime(2030, 1, 1, tzinfo=UTC)
    )

    assert not route.called
    assert len(report.expired) == 1
    assert "Expired before download" in (tmp_path / "media" / "index.md").read_text(
        encoding="utf-8"
    )


@respx.mock
def test_non_media_attachment_is_skipped_without_a_request(tmp_path):
    route = respx.get(SIGNED).mock(return_value=httpx.Response(200, content=b"x"))

    report = download_day(
        [_record([_attachment(filename="watchdog.sh", content_type="text/x-shellscript")])],
        tmp_path,
        now=NOW,
    )

    assert not route.called
    assert report.skipped_type == 1


@respx.mock
def test_one_failed_download_does_not_cost_the_run_the_rest(tmp_path):
    good = f"{CDN}?ex=6ab26dc0&is=1&hm=a"
    bad = "https://cdn.discordapp.com/attachments/1/3/bad.png?ex=6ab26dc0&is=1&hm=b"
    respx.get(good).mock(return_value=httpx.Response(200, content=b"ok"))
    respx.get(bad).mock(return_value=httpx.Response(403))

    records = [
        _record([_attachment(url=good)]),
        _record(id="556", attachments=[_attachment(url=bad, filename="bad.png")]),
    ]
    report = download_day(records, tmp_path, now=NOW)

    assert len(report.downloaded) == 1
    assert len(report.failed) == 1
    assert "Failed to download" in (tmp_path / "media" / "index.md").read_text(encoding="utf-8")


@respx.mock
def test_oversized_attachment_is_aborted_and_leaves_no_partial_file(tmp_path, monkeypatch):
    # The cap is lowered rather than a real 100 MB body mocked: the mechanism is the
    # same, and the suite stays fast and light on memory.
    monkeypatch.setattr(media, "MAX_BYTES", 1024)
    respx.get(SIGNED).mock(return_value=httpx.Response(200, content=b"x" * 1025))

    report = download_day([_record([_attachment()])], tmp_path, now=NOW)

    assert report.downloaded == []
    assert len(report.failed) == 1
    assert "ValueError" in report.failed[0][1]
    assert not (tmp_path / "media" / "555-photo.png").exists()


class _JumpingClock:
    """A monotonic clock that leaps a long way on every reading."""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def monotonic(self) -> float:
        reading = self._now
        self._now += self._step
        return reading


def test_a_download_that_never_finishes_is_abandoned(tmp_path, monkeypatch):
    # A fake clock rather than MAX_SECONDS_PER_FILE = 0: with a zero budget, a fast
    # machine reads the same monotonic value twice and the deadline never trips.
    monkeypatch.setattr(media, "time", _JumpingClock(step=media.MAX_SECONDS_PER_FILE + 1))
    with respx.mock:
        respx.get(SIGNED).mock(return_value=httpx.Response(200, content=b"x" * 2048))
        report = download_day([_record([_attachment()])], tmp_path, now=NOW)

    assert report.downloaded == []
    assert "TimeoutError" in report.failed[0][1]
    assert not (tmp_path / "media" / "555-photo.png").exists()


@respx.mock
def test_no_bot_token_is_sent_to_the_cdn(tmp_path):
    """The URL signature is the whole authorisation; the bot token has no business here."""
    route = respx.get(SIGNED).mock(return_value=httpx.Response(200, content=b"ok"))

    download_day([_record([_attachment()])], tmp_path, now=NOW)

    assert "authorization" not in {k.lower() for k in route.calls[0].request.headers}


def test_downloaded_path_is_relative_so_the_dump_can_move(tmp_path):
    with respx.mock:
        respx.get(SIGNED).mock(return_value=httpx.Response(200, content=b"ok"))
        report = download_day([_record([_attachment()])], tmp_path, now=NOW)
    (_, path) = report.downloaded[0]
    assert not Path(path).is_absolute()


# -- manifest -----------------------------------------------------------------


def test_manifest_ties_each_file_back_to_its_source_message():
    (item,) = items_from_records([_record([_attachment()])])
    report = MediaReport(
        downloaded=[(item, "media/555-photo.png")], expired=[], failed=[], skipped_type=0
    )
    text = manifest_markdown(report)

    assert "media/555-photo.png" in text
    assert "alice" in text
    assert item.jump_url in text
    assert "it works — finally" in text


def test_manifest_says_so_when_there_is_nothing():
    text = manifest_markdown(MediaReport([], [], [], 0))
    assert "No image or video attachments" in text


def test_empty_day_still_writes_a_manifest(tmp_path):
    download_day([], tmp_path, now=NOW)
    assert (tmp_path / "media" / "index.md").is_file()


def test_skipped_manifest_says_nothing_was_seen(tmp_path):
    write_skipped_manifest(tmp_path)

    text = (tmp_path / "media" / "index.md").read_text(encoding="utf-8")
    assert "--no-media" in text
    assert "Do not describe any image or video" in text


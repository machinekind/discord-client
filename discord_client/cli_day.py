"""The `day` subcommand: the whole guild's conversation for one calendar day.

It has two forms. Without `--out`, the transcript (or JSONL records with `--json`)
goes to stdout and nothing touches the disk. Other tools shell out to this form, so
stdout carries the transcript and nothing else; every warning goes to stderr.

With `--out DIR`, the day lands on disk: `DIR/transcript.md`, the day's images and
video under `DIR/media/` with a manifest, and a one-line summary on stdout. Media is
fetched only here, and in the same run as the pull, because Discord's attachment
URLs are signed and expire within a day (see `media`). A transcript printed to
stdout marks every attachment "(not downloaded)".

A day with no messages is an ordinary result, not an error: it prints, or writes,
the usual "No messages in this window." transcript and exits 0.

Builds its own `DiscordClient` and calls `config.bot_token()` lazily, same as
`cli_read`, so `--help` and argument errors never need a token.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import config as config_module
from . import media as media_module
from .discord_api import DiscordClient
from .ingest import day_window, pull_window, transcript_markdown

TRANSCRIPT_NAME = "transcript.md"


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "day", help="dump every channel's messages for one calendar day"
    )
    parser.add_argument(
        "--date", required=True, help="the day to dump, YYYY-MM-DD, in the configured timezone"
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json", action="store_true", help="print JSONL records instead of a markdown transcript"
    )
    output.add_argument(
        "--out",
        metavar="DIR",
        help="write transcript.md and the day's images and video into DIR "
        "(created if absent, refused if not empty) instead of printing",
    )
    parser.add_argument(
        "--no-media",
        action="store_true",
        help="with --out: skip downloading images and video (their URLs expire within a day)",
    )
    parser.set_defaults(func=_cmd_day)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        sys.exit(f"[FAIL] --date must be YYYY-MM-DD, got {value!r}")


def _prepare_out_dir(out_dir: Path) -> None:
    """Create `out_dir`, refusing one that already holds anything.

    A dump is never written into an earlier one. A transcript beside another run's
    media, or a manifest describing files from a different day, would be read as
    one consistent day by whoever opens the directory next.
    """
    if out_dir.exists():
        if not out_dir.is_dir():
            sys.exit(f"[FAIL] --out {out_dir} exists and is not a directory")
        if any(out_dir.iterdir()):
            sys.exit(
                f"[FAIL] --out {out_dir} exists and is not empty; "
                "pick a new directory or empty this one"
            )
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(f"[FAIL] cannot create --out {out_dir}: {exc}")


def _print_records(records: list[dict], as_json: bool, tz: str) -> None:
    if as_json:
        for record in records:
            print(json.dumps(record))
    else:
        print(transcript_markdown(records, tz=tz), end="")


def _summary(out_dir: Path, records: list[dict], report: media_module.MediaReport | None) -> str:
    channel_count = len({r["channel_name"] for r in records})
    line = f"wrote {out_dir}: {len(records)} message(s) across {channel_count} channel(s); media: "
    if report is None:
        return line + "skipped (--no-media)"
    return line + (
        f"{len(report.downloaded)} downloaded, {len(report.expired)} expired, "
        f"{len(report.failed)} failed"
    )


def _write_dump(records: list[dict], out_dir: Path, download_media: bool, tz: str) -> None:
    # Media before the transcript: the transcript names each downloaded file's
    # local path, and the signed URLs in `records` will not outlive the day.
    report = None
    media_paths: dict[str, str] = {}
    if download_media:
        report = media_module.download_day(records, out_dir)
        media_paths = {item.url: path for item, path in report.downloaded}
    else:
        media_module.write_skipped_manifest(out_dir)

    (out_dir / TRANSCRIPT_NAME).write_text(
        transcript_markdown(records, media_paths, tz=tz), encoding="utf-8"
    )

    print(_summary(out_dir, records, report))
    lost = len(report.expired) + len(report.failed) if report else 0
    if lost:
        # Not fatal, but it is the difference between a reader who can see a
        # photo and one who has to guess at it.
        manifest = out_dir / media_module.DIR_NAME / media_module.MANIFEST_NAME
        print(
            f"[warn] {lost} image/video attachment(s) could not be downloaded; see {manifest}",
            file=sys.stderr,
        )


def _cmd_day(args: argparse.Namespace) -> int:
    if args.no_media and args.out is None:
        sys.exit("[FAIL] --no-media only applies with --out; without --out nothing is downloaded")
    day = _parse_date(args.date)
    cfg = config_module.load_config()
    token = config_module.bot_token()
    out_dir = Path(args.out) if args.out is not None else None
    if out_dir is not None:
        # After everything that can refuse to start, so a refusal leaves no empty
        # directory behind; before the pull, so a bad path fails in a second.
        _prepare_out_dir(out_dir)

    start, end = day_window(day, cfg.timezone)
    with DiscordClient(token) as client:
        guild_id = config_module.resolve_guild_id(client, cfg)
        records = pull_window(client, guild_id, start, end, cfg.ignore_channels)

    # Transcript times are shown in the same zone as the day window.
    if out_dir is None:
        _print_records(records, as_json=args.json, tz=cfg.timezone)
    else:
        _write_dump(records, out_dir, download_media=not args.no_media, tz=cfg.timezone)
    return 0

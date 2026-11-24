"""Pull a day's image and video attachments down before Discord's links die.

Since December 2023 every Discord CDN attachment URL is signed with `ex` (expiry),
`is` (issued) and `hm` (signature) query parameters, a measure against using the CDN
to distribute malware. The practical consequence is that an attachment URL recorded
today is unusable tomorrow. A jump URL to the message survives; the file behind it
does not.

So the bytes have to be fetched in the same run that pulled the records: a URL saved
into a JSONL file or a transcript will not resolve by the time anyone follows it.
That is what this module does. The files land in a `media/` directory next to the
transcript, together with a manifest that ties each file back to its message.

Four things are deliberate:

- **Only pictures and video.** People also attach scripts, CAD files and documents.
  Those stay listed by name and type in the transcript, but their bytes are rarely
  what a reader of a day's conversation needs, so fetching them would spend
  bandwidth and disk on files nobody opens.
- **No bot token leaves for the CDN.** The signature in the URL is the whole
  authorisation, so downloads use a bare client rather than `DiscordClient`. Sending
  the bot token to a host that does not need it is a gratuitous exposure.
- **Filenames are treated as hostile.** An attachment filename is chosen by whoever
  uploaded it, and this module writes it to disk. Everything but a conservative
  charset is replaced, only the basename survives, and the message id is prefixed,
  so an uploader cannot steer a write out of the media directory or collide with
  someone else's file.
- **Every file gets its own name.** Pasted screenshots all arrive as `image.png`, so
  one message can carry several attachments with the same name. A numbered suffix
  keeps each one on its own file, files are opened exclusively so nothing is ever
  overwritten, and a failed download removes only the file it created itself.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

import httpx

DIR_NAME = "media"
MANIFEST_NAME = "index.md"
WANTED_PREFIXES = ("image/", "video/")
# A fully boosted guild accepts uploads up to 100 MB (Nitro subscribers can send
# more), so anything past this is a surprise worth skipping loudly rather than
# streaming to disk.
MAX_BYTES = 100 * 1024 * 1024
# httpx's timeout is per socket operation, so a transfer that keeps trickling never trips
# it. This is the wall-clock ceiling for one file, which is what actually stops a slow
# 90 MB clip from holding the whole run open.
MAX_SECONDS_PER_FILE = 180.0
TIMEOUT_SECONDS = 30.0
# Filenames are truncated to keep paths sane; past this length a digest of the original
# is appended so two long names cannot collapse onto each other.
STEM_LIMIT = 60

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_EXTENSION_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


@dataclass(frozen=True)
class MediaItem:
    """One attachment worth downloading, with the message context that explains it."""

    url: str
    filename: str
    content_type: str
    size: int
    message_id: str
    author: str
    timestamp: str
    channel_name: str
    thread_name: str | None
    jump_url: str
    content: str


@dataclass
class MediaReport:
    """What one `download_day` call managed to fetch, and what it could not."""

    downloaded: list[tuple[MediaItem, str]]  # (item, path relative to `out_dir`)
    expired: list[MediaItem]
    failed: list[tuple[MediaItem, str]]
    skipped_type: int


def expiry(url: str) -> datetime | None:
    """The moment this signed URL stops working, or None when it carries no signature.

    `ex` is a Unix timestamp in lowercase hex. A URL without it either predates the
    signing scheme or is not a CDN attachment link; either way there is nothing to
    check, so callers treat None as "not known to be expired".
    """
    raw = parse_qs(urlparse(url).query).get("ex", [None])[0]
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw, 16), UTC)
    except (ValueError, OverflowError, OSError):
        return None


def is_expired(url: str, now: datetime | None = None) -> bool:
    when = expiry(url)
    return when is not None and when <= (now or datetime.now(UTC))


def guess_type(filename: str, declared: str) -> str:
    """Discord's `content_type`, falling back to the extension when it is absent."""
    if declared:
        return declared
    return _EXTENSION_TYPES.get(PurePosixPath(filename).suffix.lower(), "")


def is_wanted(content_type: str) -> bool:
    return content_type.startswith(WANTED_PREFIXES)


def safe_name(message_id: str, filename: str) -> str:
    """A filename safe to join onto the media directory, however hostile the input.

    Only the basename survives, so `../../.ssh/authorized_keys` becomes
    `authorized_keys`; everything outside `[A-Za-z0-9._-]` is replaced, which also
    disarms a leading dot; and the message id prefix keeps two people's `IMG_0001.png`
    apart. Two attachments of one message can still share a name; `download_day`
    numbers those apart (see `unique_name`).

    Two long names that differ only past the truncation point would otherwise produce the
    same file, and the second download would overwrite the first with no sign that
    anything was lost, so a digest of the original name goes on the end. The extension
    is preserved through all of this, because whatever opens the file later (an image
    viewer, an agent's image tool, a browser choosing between `<img>` and `<video>`)
    decides how to by looking at it.
    """
    name = PurePosixPath(filename).name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = _UNSAFE.sub("_", stem).lstrip(".")
    ext = _UNSAFE.sub("_", ext)[:16]
    if len(stem) > STEM_LIMIT:
        digest = hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:8]
        stem = f"{stem[: STEM_LIMIT - 9]}-{digest}"
    base = f"{stem or 'attachment'}{'.' + ext if ext else ''}"
    return f"{message_id}-{base}"


def unique_name(name: str, taken: set[str], directory: Path) -> str:
    """`name`, or `name` with `-2`, `-3`, ... before its extension, unused so far.

    `taken` holds the names already handed out in this run and is updated. The
    comparison ignores case, because the default filesystems on macOS and Windows
    do, and `IMAGE.png` would land on `image.png`'s file there. A file already in
    `directory` counts as taken too, so a run never writes over an earlier one.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    candidate, number = name, 2
    while candidate.casefold() in taken or (directory / candidate).exists():
        candidate = f"{stem}-{number}{dot}{ext}"
        number += 1
    taken.add(candidate.casefold())
    return candidate


def items_from_records(records: list[dict]) -> list[MediaItem]:
    """Every attachment in a day's records, in message order, typed but not yet filtered."""
    items: list[MediaItem] = []
    for record in records:
        for attachment in record.get("attachments") or []:
            url = attachment.get("url")
            if not url:
                continue
            filename = attachment.get("filename") or "attachment"
            content_type = guess_type(filename, attachment.get("content_type") or "")
            items.append(
                MediaItem(
                    url=url,
                    filename=filename,
                    content_type=content_type,
                    size=int(attachment.get("size") or 0),
                    message_id=record["id"],
                    author=record["author"],
                    timestamp=record["timestamp"],
                    channel_name=record["channel_name"],
                    thread_name=record["thread_name"],
                    jump_url=record["jump_url"],
                    content=record["content"],
                )
            )
    return items


def download_day(
    records: list[dict],
    out_dir: Path,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> MediaReport:
    """Fetch the day's pictures and video into `out_dir/media/` and write the manifest.

    Never raises for a single bad attachment: one 403 or one oversized file must not
    cost the run the rest of the day's media, so failures are collected into the
    report and recorded in the manifest, where whoever reads the dump will see them.
    """
    items = items_from_records(records)
    report = MediaReport(downloaded=[], expired=[], failed=[], skipped_type=0)

    wanted = []
    for item in items:
        if not is_wanted(item.content_type):
            report.skipped_type += 1
        elif is_expired(item.url, now):
            report.expired.append(item)
        else:
            wanted.append(item)

    if not wanted:
        _write_manifest(out_dir, report)
        return report

    media_dir = out_dir / DIR_NAME
    media_dir.mkdir(parents=True, exist_ok=True)

    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    taken: set[str] = set()
    try:
        for item in wanted:
            name = unique_name(safe_name(item.message_id, item.filename), taken, media_dir)
            try:
                _fetch(client, item.url, media_dir / name)
            except Exception as exc:  # every failure mode is reportable
                report.failed.append((item, f"{type(exc).__name__}: {exc}"))
            else:
                report.downloaded.append((item, f"{DIR_NAME}/{name}"))
    finally:
        if owned:
            client.close()

    _write_manifest(out_dir, report)
    return report


def _fetch(client: httpx.Client, url: str, dest: Path) -> None:
    """Stream one attachment to disk, aborting past MAX_BYTES or MAX_SECONDS_PER_FILE.

    Streaming rather than reading into memory keeps a 90 MB clip from becoming a 90 MB
    string, and the running total means a server lying in Content-Length cannot make
    us write more than the cap. The deadline covers the case the byte cap does not: a
    transfer that stays under the limit but never finishes.

    `dest` is opened exclusively, so a name clash fails this download instead of
    overwriting another file, and a failure part-way removes `dest` only once this
    call has created it: cleanup can never reach a file that belongs to someone else.
    """
    written = 0
    deadline = time.monotonic() + MAX_SECONDS_PER_FILE
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with dest.open("xb") as handle:
            try:
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"download still running after {MAX_SECONDS_PER_FILE:.0f}s"
                        )
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise ValueError(f"attachment exceeds {MAX_BYTES} bytes")
                    handle.write(chunk)
            except BaseException:
                handle.close()  # Windows cannot remove a file that is still open
                dest.unlink(missing_ok=True)
                raise


def _write_manifest(out_dir: Path, report: MediaReport) -> None:
    (out_dir / DIR_NAME).mkdir(parents=True, exist_ok=True)
    (out_dir / DIR_NAME / MANIFEST_NAME).write_text(manifest_markdown(report), encoding="utf-8")


def write_skipped_manifest(out_dir: Path) -> None:
    """Record that media was deliberately skipped.

    Whoever reads the dump, a person or an agent, looks in `media/index.md` to learn
    what was posted besides text. Leaving the file out under `--no-media` would leave
    a gap, and an agent that finds a gap tends to invent an explanation for it; one
    that finds this note does not.
    """
    (out_dir / DIR_NAME).mkdir(parents=True, exist_ok=True)
    (out_dir / DIR_NAME / MANIFEST_NAME).write_text(
        "# Media from this day\n\n"
        "Nothing was downloaded: this dump was written with `--no-media`.\n\n"
        "Discord's attachment URLs are signed and expire within a day, so any image or "
        "video from this day has to be opened on Discord by hand, through the jump URLs "
        "in `transcript.md`.\n\n"
        "**Do not describe any image or video from this day.** Nobody reading this dump "
        "has seen them.\n",
        encoding="utf-8",
    )


def manifest_markdown(report: MediaReport) -> str:
    """The index to read before opening any of these files.

    Its job is to connect a file on disk back to the moment that produced it: who
    posted it, in which channel, what they said around it, and the jump URL needed
    to check the context on Discord or to ask the poster before reusing the file.
    """
    lines = [
        "# Media from this day",
        "",
        "Downloaded when this dump was written, because Discord's attachment URLs expire",
        "within a day. Open these files directly to see what they show; never describe one",
        "you have not opened. Paths are relative to the directory that holds `media/`.",
        "",
    ]

    if not report.downloaded:
        lines.append("No image or video attachments were downloaded for this day.")
    for item, path in report.downloaded:
        where = f"#{item.channel_name}"
        if item.thread_name:
            where += f" › {item.thread_name}"
        lines.append(f"## `{path}`")
        lines.append("")
        lines.append(f"- **Posted by:** {item.author} in {where} at {item.timestamp}")
        lines.append(f"- **Type:** {item.content_type}, {item.size} bytes")
        lines.append(f"- **Source message:** {item.jump_url}")
        said = " ".join(item.content.split())
        lines.append(f"- **Said alongside it:** {said or '(no text)'}")
        lines.append("")

    if report.expired:
        lines.append("## Expired before download")
        lines.append("")
        lines.append(
            "These attachments' signed URLs had already lapsed when this dump was written. "
            "The files may still be visible on Discord through the jump URL, but they are "
            "not on disk here."
        )
        lines.append("")
        for item in report.expired:
            lines.append(f"- {item.filename} — {item.author} — {item.jump_url}")
        lines.append("")

    if report.failed:
        lines.append("## Failed to download")
        lines.append("")
        for item, reason in report.failed:
            lines.append(f"- {item.filename} — {reason} — {item.jump_url}")
        lines.append("")

    if report.skipped_type:
        lines.append(
            f"{report.skipped_type} attachment(s) that are neither image nor video were not "
            "downloaded; the transcript lists them under their messages."
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"

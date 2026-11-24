"""`discord-client` CLI entrypoint: an argparse skeleton each command module registers into."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import httpx

from . import cli_day, cli_read, cli_whoami
from .config import load_env
from .discord_api import DiscordAPIError

PACKAGE_NAME = "discord-client"
FALLBACK_VERSION = "0.0.0+unknown"

# How much of a failed response's body the `[FAIL]` line keeps. Discord's error
# bodies are short JSON; a proxy's HTML error page is not, and only its end
# tends to say anything.
ERROR_BODY_TAIL_CHARS = 300

# Each entry wires one subcommand's parser and handler. New commands append
# here instead of touching build_parser, so the registration order stays the
# single place that defines `discord-client --help`'s command list.
SubcommandRegistrar = Callable[["argparse._SubParsersAction[argparse.ArgumentParser]"], None]


def _package_version() -> str:
    try:
        return _pkg_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION


def _cmd_version(args: argparse.Namespace) -> int:
    print(_package_version())
    return 0


def _register_version(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser("version", help="print the installed package version")
    parser.set_defaults(func=_cmd_version)


SUBCOMMANDS: list[SubcommandRegistrar] = [
    _register_version,
    cli_read.register,
    cli_day.register,
    cli_whoami.register,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discord-client",
        description="Read a Discord guild over the bot REST API: channels, search, transcripts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for register in SUBCOMMANDS:
        register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def _api_failure_message(exc: DiscordAPIError) -> str:
    """One `[FAIL]` line for a Discord error no command handled itself.

    Names the request and the status, and keeps the tail of the body on the
    same line: Discord's own error JSON says what went wrong far better than
    the status code does.
    """
    request = exc.response.request
    body = " ".join(exc.response.text.split()) or "(empty body)"
    if len(body) > ERROR_BODY_TAIL_CHARS:
        body = "..." + body[-ERROR_BODY_TAIL_CHARS:]
    return (
        f"[FAIL] Discord API error: {request.method} {request.url} -> "
        f"{exc.status_code}: {body}"
    )


def run() -> int:
    """Console-script entry point: load .env, then dispatch.

    Kept separate from main() so the test suite can drive the parser
    hermetically, without a .env being read into the process environment.
    main() lets a DiscordAPIError propagate, so tests see it; here, at the
    edge, it becomes a `[FAIL]` line on stderr and exit code 1 instead of a
    traceback. So does a request that never got an answer (no network, DNS
    failure, timeout).
    """
    load_env()
    # Transcripts are full of non-ASCII text, and on Windows a piped stdout
    # defaults to a legacy code page that cannot encode it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        return main()
    except DiscordAPIError as exc:
        print(_api_failure_message(exc), file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"[FAIL] could not reach Discord: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())

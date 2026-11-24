"""`discord-client-mcp`: the read-only CLI commands as an MCP server over Streamable HTTP.

Built for agents with no shell, such as an agent running in a locked-down
container, and meant to run as a sidecar container next to them: the Discord
token lives only in this process's environment, and the agent holds just the
URL plus a bearer token. See docs/mcp-sidecar.md for the deployment.

Every tool shells out to the `discord-client` CLI (`python -m
discord_client.cli`) rather than calling its internals. The CLI reports
failures through `sys.exit("[FAIL] ...")` and prints its results to stdout, so
a subprocess per call keeps a failing command from taking the server down,
keeps concurrent calls from interleaving their output, and leaves the CLI the
single source of behaviour.

Nothing here posts to Discord. The library has two helpers that do
(`DiscordClient.post_message` and `DiscordClient.start_thread`), but the CLI
never calls them and no tool wraps them. Run the sidecar on a read-only bot's
DISCORD_READER_TOKEN and posting is impossible by construction, not only absent
from the tool list.
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import re
import subprocess
import sys

from .config import load_env
from .discord_api import SEARCH_MAX_LIMIT

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp_types import ToolAnnotations
    from starlette.datastructures import Headers
    from starlette.responses import PlainTextResponse
    from starlette.types import ASGIApp, Receive, Scope, Send
except ImportError as exc:  # pragma: no cover - exercised only on a bare install
    # Not on PyPI: the extra is installed from the git repository.
    raise SystemExit(
        "[FAIL] the MCP server needs the 'mcp' extra: "
        "pip install 'discord-client[mcp] @ git+https://github.com/machinekind/discord-client'"
    ) from exc

log = logging.getLogger("discord-client")

TOKEN_ENV = "DISCORD_CLIENT_MCP_TOKEN"
ALLOWED_HOSTS_ENV = "DISCORD_CLIENT_MCP_ALLOWED_HOSTS"
HOST_ENV = "DISCORD_CLIENT_MCP_HOST"
PORT_ENV = "DISCORD_CLIENT_MCP_PORT"
MIN_TOKEN_LENGTH = 32

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Host headers accepted by default: local runs, plus the service name the
# compose example in docs/mcp-sidecar.md gives the sidecar.
DEFAULT_ALLOWED_HOSTS = ("localhost:*", "127.0.0.1:*", "[::1]:*", "discord-client-mcp:*")

# `day` walks every channel and thread in the guild for a whole day; give it
# room. The other read commands finish in seconds.
DAY_TIMEOUT_S = 600
READ_TIMEOUT_S = 120

# How much CLI output a failed call hands back to the agent.
ERROR_TAIL_CHARS = 2000

# `[warn]` lines passed through on success, so a bot that lost access to
# ninety channels doesn't bury the result.
MAX_WARNINGS = 30

_SNOWFLAKE_RE = re.compile(r"^\d{15,25}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SINCE_RE = re.compile(r"^(\d{1,3}d|\d{4}-\d{2}-\d{2})$")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
# Every tool returns plain text. structured_output=False stops the SDK from
# repeating that text as structuredContent, which would double a 70 KB transcript.

INSTRUCTIONS = """\
Read-only access to one Discord guild, through a bot. Dates, day windows and
the message times in transcripts use the server's configured timezone
(DISCORD_TIMEZONE, UTC by default).

Start with list_channels to learn channel names, search_messages to find a
topic, then pull_channel or get_thread for the full conversation. Use
day_transcript for a whole calendar day across every channel: it is slow (up
to a few minutes) and large.

Nothing here posts to Discord.
"""


class CLIError(Exception):
    """A `discord-client` CLI run timed out or exited non-zero."""


def run_cli(
    args: list[str], *, timeout: float = READ_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """Run `discord-client <args>` in a fresh interpreter and return the finished process.

    The child inherits this process's environment, so it resolves the same
    token and settings. Arguments go in as a list, never through a shell.
    Output is decoded as UTF-8 whatever the locale, since transcripts are
    full of non-ASCII names and text.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "discord_client.cli", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CLIError(f"discord-client {args[0]} timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        output = (proc.stderr.strip() or proc.stdout.strip() or "(no output)")[-ERROR_TAIL_CHARS:]
        raise CLIError(f"discord-client {args[0]} failed (exit {proc.returncode}):\n{output}")
    return proc


def with_warnings(text: str, stderr: str) -> str:
    """Append the CLI's `[warn]` lines to a successful result.

    The CLI skips channels and threads it cannot read with a `[warn]` on
    stderr and still exits 0. Dropping those lines would let an agent read
    "No messages in this window" as a quiet channel when the bot has actually
    lost access.
    """
    warns = [line for line in stderr.splitlines() if line.startswith("[warn]")]
    if not warns:
        return text
    shown = warns[:MAX_WARNINGS]
    if len(warns) > MAX_WARNINGS:
        shown.append(f"[warn] ... and {len(warns) - MAX_WARNINGS} more")
    header = "CLI warnings (these sources were skipped, so the result above is incomplete):"
    return f"{text.rstrip()}\n\n{header}\n" + "\n".join(shown) + "\n"


def _cli_stdout(args: list[str], *, timeout: float = READ_TIMEOUT_S) -> str:
    try:
        proc = run_cli(args, timeout=timeout)
    except CLIError as exc:
        raise ToolError(str(exc)) from exc
    return with_warnings(proc.stdout, proc.stderr)


server = MCPServer(name="discord-client", instructions=INSTRUCTIONS)


@server.tool(annotations=READ_ONLY, structured_output=False)
def whoami() -> str:
    """Show which Discord bot identity and token tier (reader or writer) the server runs on."""
    return _cli_stdout(["whoami"])


@server.tool(annotations=READ_ONLY, structured_output=False)
def list_channels() -> str:
    """List the guild's text, announcement and forum channels: name, id and type, one per line."""
    return _cli_stdout(["channels"])


@server.tool(annotations=READ_ONLY, structured_output=False)
def pull_channel(channel: str, since: str) -> str:
    """Return one channel's messages (and its threads) as a markdown transcript.

    Message times are in the server's configured timezone (DISCORD_TIMEZONE,
    UTC by default).

    Args:
        channel: Channel name without '#' (as list_channels prints it) or channel id.
        since: How far back to read: 'Nd' for the last N days (e.g. '3d') or a date
            'YYYY-MM-DD', meaning from that day's midnight until now. Dates use the
            server's configured timezone (DISCORD_TIMEZONE, UTC by default).
    """
    if not _SINCE_RE.match(since):
        raise ToolError(f"since must be 'Nd' or 'YYYY-MM-DD', got {since!r}")
    # `--opt=value` keeps a value starting with '-' from being read as an option.
    return _cli_stdout(["pull", f"--channel={channel}", f"--since={since}"])


@server.tool(annotations=READ_ONLY, structured_output=False)
def search_messages(query: str, limit: int = SEARCH_MAX_LIMIT) -> str:
    """Search the whole guild's messages with Discord search.

    Returns one line per hit: author, channel id, a snippet and a jump URL.
    To read around a hit, pass its channel id to pull_channel when
    list_channels shows that id; otherwise the hit is inside a thread or forum
    post, and the id goes to get_thread.

    Args:
        query: Search text, as typed in Discord's search box.
        limit: Maximum hits, 1 to 25.
    """
    if not 1 <= limit <= SEARCH_MAX_LIMIT:
        raise ToolError(f"limit must be between 1 and {SEARCH_MAX_LIMIT}, got {limit}")
    if not query.strip():
        raise ToolError("query must not be empty")
    # `--` ends option parsing, so a query like '-foo' stays the query.
    return _cli_stdout(["search", f"--limit={limit}", "--", query])


@server.tool(annotations=READ_ONLY, structured_output=False)
def get_thread(thread_id: str) -> str:
    """Return the full transcript of one thread or forum post.

    Message times are in the server's configured timezone (DISCORD_TIMEZONE,
    UTC by default).

    Args:
        thread_id: The thread's numeric id (the channel id in its jump URL).
    """
    if not _SNOWFLAKE_RE.match(thread_id):
        raise ToolError(f"thread_id must be a numeric Discord id, got {thread_id!r}")
    return _cli_stdout(["thread", thread_id])


@server.tool(annotations=READ_ONLY, structured_output=False)
def day_transcript(date: str) -> str:
    """Return the whole guild's markdown transcript for one calendar day.

    Covers every channel and thread the bot can read, from midnight to
    midnight in the server's configured timezone (DISCORD_TIMEZONE, UTC by
    default); message times are in that timezone too. Slow (up to a few
    minutes) and large (tens of KB on a busy day). A day with no messages is
    not an error. Channels the bot could not read are listed as warnings at
    the end; an empty or thin day that comes with warnings may just be
    missing access.

    Args:
        date: The day, YYYY-MM-DD.
    """
    if not _DATE_RE.match(date):
        raise ToolError(f"date must be YYYY-MM-DD, got {date!r}")
    return _cli_stdout(["day", f"--date={date}"], timeout=DAY_TIMEOUT_S)


class BearerTokenMiddleware:
    """Reject every HTTP request that lacks `Authorization: Bearer <token>`.

    Lifespan and other non-HTTP scopes pass straight through, so the wrapped
    app's startup (the session manager) still runs.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scheme, _, presented = Headers(scope=scope).get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.strip().encode("utf-8"), self._token
        ):
            response = PlainTextResponse(
                "unauthorized\n",
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="discord-client-mcp"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(token: str, allowed_hosts: list[str]) -> ASGIApp:
    """The Streamable HTTP app at /mcp, behind the bearer check and a Host allowlist.

    Stateless with plain JSON responses: no session survives a restart, so
    none can go stale, and the tools never push notifications.
    """
    inner = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[],  # server-to-server clients send no Origin
        ),
    )
    return BearerTokenMiddleware(inner, token)


def _allowed_hosts() -> list[str]:
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return hosts or list(DEFAULT_ALLOWED_HOSTS)


def run(argv: list[str] | None = None) -> int:
    """Console-script entry point for `discord-client-mcp`."""
    # Before the parser, so a .env can set the bind address as well as the tokens.
    load_env()
    parser = argparse.ArgumentParser(
        prog="discord-client-mcp",
        description="Serve the read-only discord-client commands as an MCP server "
        "(Streamable HTTP at /mcp, bearer-token auth).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(HOST_ENV) or DEFAULT_HOST,
        help=f"address to bind (default {DEFAULT_HOST}, or ${HOST_ENV}; "
        "use 0.0.0.0 inside a container)",
    )
    # A string default goes through `type` too, so a bad env value is a clean
    # argparse error rather than a traceback.
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get(PORT_ENV) or str(DEFAULT_PORT),
        help=f"port to bind (default {DEFAULT_PORT}, or ${PORT_ENV})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    token = os.environ.get(TOKEN_ENV, "").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        sys.exit(
            f"[FAIL] set {TOKEN_ENV} to a random secret of at least {MIN_TOKEN_LENGTH} "
            "characters (e.g. `openssl rand -hex 32`); clients send it as "
            "'Authorization: Bearer <token>'"
        )
    if os.environ.get("DISCORD_BOT_TOKEN"):
        log.warning(
            "DISCORD_BOT_TOKEN (writer tier) is set; this server only reads, so "
            "DISCORD_READER_TOKEN alone is enough"
        )

    allowed_hosts = _allowed_hosts()
    log.info("allowed Host headers: %s", ", ".join(allowed_hosts))

    import uvicorn

    uvicorn.run(build_app(token, allowed_hosts), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(run())

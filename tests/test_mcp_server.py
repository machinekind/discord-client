"""discord-client-mcp: tool wiring (CLI arguments, validation, warnings), the
subprocess runner, the HTTP layer (bearer auth, Host allowlist), and startup.

Tool tests swap `run_cli` for a fake, so nothing touches Discord. The runner
tests spawn the real CLI only for commands that fail or finish before any
token or network is needed.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from discord_client import mcp_server

TOKEN = "t" * 40
THREAD_ID = "123456789012345678"
EXPECTED_TOOLS = {
    "whoami",
    "list_channels",
    "pull_channel",
    "search_messages",
    "get_thread",
    "day_transcript",
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
NO_ACCESS_WARNING = "[warn] skipping #general: bot has no access (403)"


@pytest.fixture(autouse=True)
def _no_mcp_settings(monkeypatch):
    """The shared conftest clears the CLI's variables; this clears the server's."""
    for name in (
        mcp_server.TOKEN_ENV,
        mcp_server.ALLOWED_HOSTS_ENV,
        mcp_server.HOST_ENV,
        mcp_server.PORT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def call(name: str, arguments: dict | None = None) -> str:
    result = asyncio.run(mcp_server.server.call_tool(name, arguments or {}))
    assert not result.is_error
    return "".join(block.text for block in result.content)


@pytest.fixture
def fake_cli(monkeypatch):
    """Replace run_cli; record each call's args and keyword options."""
    calls: list[tuple[list[str], dict]] = []

    def fake(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(["discord-client", *args], 0, f"ran {args[0]}\n", "")

    monkeypatch.setattr(mcp_server, "run_cli", fake)
    return calls


# -- tool surface ---------------------------------------------------------------


def test_tools_are_the_read_commands_and_nothing_posts():
    tools = asyncio.run(mcp_server.server.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert all(t.annotations.read_only_hint for t in tools)
    assert not any(t.annotations.destructive_hint for t in tools)
    assert not any("post" in t.name for t in tools)


def test_tool_results_are_not_duplicated_as_structured_content(fake_cli):
    result = asyncio.run(mcp_server.server.call_tool("whoami", {}))
    assert result.structured_content is None


def test_dates_are_documented_in_the_configured_timezone():
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert "DISCORD_TIMEZONE" in mcp_server.INSTRUCTIONS
    assert "DISCORD_TIMEZONE" in tools["day_transcript"].description
    assert "DISCORD_TIMEZONE" in tools["pull_channel"].description


# -- argument handling ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "argv"),
    [
        ("whoami", {}, ["whoami"]),
        ("list_channels", {}, ["channels"]),
        ("pull_channel", {"channel": "general", "since": "3d"}, None),
        ("search_messages", {"query": "release"}, None),
        ("get_thread", {"thread_id": THREAD_ID}, ["thread", THREAD_ID]),
    ],
)
def test_read_tools_use_the_short_timeout(fake_cli, tool, arguments, argv):
    call(tool, arguments)
    args, kwargs = fake_cli[0]
    if argv is not None:
        assert args == argv
    assert kwargs["timeout"] == mcp_server.READ_TIMEOUT_S


def test_pull_channel_passes_values_inside_equals_options(fake_cli):
    assert call("pull_channel", {"channel": "-x", "since": "3d"}) == "ran pull\n"
    assert fake_cli[0][0] == ["pull", "--channel=-x", "--since=3d"]


def test_pull_channel_accepts_a_date(fake_cli):
    call("pull_channel", {"channel": "general", "since": "2026-09-07"})
    assert fake_cli[0][0] == ["pull", "--channel=general", "--since=2026-09-07"]


@pytest.mark.parametrize("since", ["yesterday", "3", "2026-9-7", "--help"])
def test_pull_channel_rejects_a_malformed_since(fake_cli, since):
    with pytest.raises(ToolError, match="since must be"):
        call("pull_channel", {"channel": "general", "since": since})
    assert fake_cli == []


def test_search_puts_the_query_after_double_dash(fake_cli):
    call("search_messages", {"query": "--limit 99", "limit": 5})
    assert fake_cli[0][0] == ["search", "--limit=5", "--", "--limit 99"]


def test_search_defaults_to_discords_cap(fake_cli):
    call("search_messages", {"query": "release"})
    assert fake_cli[0][0] == ["search", "--limit=25", "--", "release"]


@pytest.mark.parametrize("limit", [0, 26])
def test_search_rejects_a_limit_outside_discords_range(fake_cli, limit):
    with pytest.raises(ToolError, match="limit must be between 1 and 25"):
        call("search_messages", {"query": "release", "limit": limit})
    assert fake_cli == []


def test_search_rejects_an_empty_query(fake_cli):
    with pytest.raises(ToolError, match="query must not be empty"):
        call("search_messages", {"query": "   "})
    assert fake_cli == []


def test_get_thread_requires_a_numeric_id(fake_cli):
    with pytest.raises(ToolError, match="numeric Discord id"):
        call("get_thread", {"thread_id": "--help"})
    call("get_thread", {"thread_id": THREAD_ID})
    assert fake_cli[0][0] == ["thread", THREAD_ID]


def test_read_tool_passes_cli_warnings_through(monkeypatch):
    # A 403-skipped channel exits 0 with an empty transcript; without the
    # warning the agent would take the channel for quiet.
    def skipped(args, **kwargs):
        stderr = f"{NO_ACCESS_WARNING}\nunrelated noise\n"
        return subprocess.CompletedProcess(args, 0, "No messages in this window.\n", stderr)

    monkeypatch.setattr(mcp_server, "run_cli", skipped)
    text = call("pull_channel", {"channel": "general", "since": "7d"})
    assert text.startswith("No messages in this window.\n\nCLI warnings")
    assert text.rstrip().endswith(NO_ACCESS_WARNING)
    assert "unrelated noise" not in text


def test_results_without_warnings_are_unchanged():
    assert mcp_server.with_warnings("ok\n", "") == "ok\n"
    assert mcp_server.with_warnings("ok\n", "DeprecationWarning: x\n") == "ok\n"


def test_warnings_are_capped():
    stderr = "".join(f"[warn] skipping #c{i}: bot has no access (403)\n" for i in range(45))
    lines = mcp_server.with_warnings("x", stderr).splitlines()
    assert lines[-1] == "[warn] ... and 15 more"
    assert sum(line.startswith("[warn] skipping") for line in lines) == mcp_server.MAX_WARNINGS


def test_cli_failure_reaches_the_agent_as_a_tool_error(monkeypatch):
    def failing(args, **kwargs):
        raise mcp_server.CLIError("discord-client whoami failed (exit 1):\n[FAIL] no Discord token")

    monkeypatch.setattr(mcp_server, "run_cli", failing)
    with pytest.raises(ToolError, match=r"\[FAIL\] no Discord token"):
        call("whoami")


# -- day_transcript -------------------------------------------------------------


@pytest.fixture
def fake_day(monkeypatch):
    """A run_cli that answers the way `discord-client day` does: transcript on
    stdout, exit 0, unreadable channels as `[warn]` lines on stderr."""
    seen: dict = {"stdout": "## #general\n\n- 09:00 alice: hello\n", "stderr": ""}

    def fake(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return subprocess.CompletedProcess(args, 0, seen["stdout"], seen["stderr"])

    monkeypatch.setattr(mcp_server, "run_cli", fake)
    return seen


def test_day_transcript_runs_day_for_the_date_with_the_long_timeout(fake_day):
    text = call("day_transcript", {"date": "2026-09-07"})
    assert text == fake_day["stdout"]
    assert fake_day["args"] == ["day", "--date=2026-09-07"]
    assert fake_day["kwargs"]["timeout"] == mcp_server.DAY_TIMEOUT_S


def test_day_transcript_returns_an_empty_day_as_it_is(fake_day):
    fake_day["stdout"] = "No messages in this window.\n"
    assert call("day_transcript", {"date": "2026-09-07"}) == "No messages in this window.\n"


def test_day_transcript_ends_with_the_skipped_channels(fake_day):
    fake_day["stderr"] = NO_ACCESS_WARNING + "\n"
    text = call("day_transcript", {"date": "2026-09-07"})
    assert text.startswith(fake_day["stdout"].rstrip())
    assert "result above is incomplete" in text
    assert text.rstrip().endswith(NO_ACCESS_WARNING)


@pytest.mark.parametrize("date", ["7 Sept", "2026-9-7", "--help", "2026-09-07T00:00", ""])
def test_day_transcript_rejects_a_malformed_date(fake_day, date):
    with pytest.raises(ToolError, match="date must be YYYY-MM-DD"):
        call("day_transcript", {"date": date})
    assert "args" not in fake_day


def test_day_transcript_failure_reaches_the_agent_as_a_tool_error(monkeypatch):
    def failing(args, **kwargs):
        raise mcp_server.CLIError("discord-client day timed out after 600s")

    monkeypatch.setattr(mcp_server, "run_cli", failing)
    with pytest.raises(ToolError, match="day timed out after 600s"):
        call("day_transcript", {"date": "2026-09-07"})


# -- run_cli --------------------------------------------------------------------


def test_run_cli_runs_the_real_cli():
    proc = mcp_server.run_cli(["version"])
    assert proc.returncode == 0
    assert proc.stdout.strip()


def test_run_cli_raises_with_the_fail_message():
    # `pull` validates --since before it needs a token or the network.
    with pytest.raises(mcp_server.CLIError, match=r"exit 1\):\n\[FAIL\] --since must be"):
        mcp_server.run_cli(["pull", "--channel=general", "--since=nope"])


def test_run_cli_runs_this_interpreter_without_a_shell_and_decodes_utf8(monkeypatch):
    seen: dict = {}

    def recording(cmd, **kwargs):
        seen.update(cmd=cmd, kwargs=kwargs)
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(mcp_server.subprocess, "run", recording)
    mcp_server.run_cli(["day", "--date=2026-09-07"], timeout=600)
    assert seen["cmd"] == [sys.executable, "-m", "discord_client.cli", "day", "--date=2026-09-07"]
    assert not seen["kwargs"].get("shell")
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert seen["kwargs"]["timeout"] == 600


def test_run_cli_keeps_the_tail_of_a_long_failure(monkeypatch):
    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "x" * 5000 + "[FAIL] the end\n")

    monkeypatch.setattr(mcp_server.subprocess, "run", failing)
    with pytest.raises(mcp_server.CLIError) as excinfo:
        mcp_server.run_cli(["channels"])
    message = str(excinfo.value)
    assert message.endswith("[FAIL] the end")
    assert len(message.split("\n", 1)[1]) == mcp_server.ERROR_TAIL_CHARS


def test_run_cli_reports_a_silent_failure(monkeypatch):
    monkeypatch.setattr(
        mcp_server.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, "", "")
    )
    with pytest.raises(mcp_server.CLIError, match=r"exit 3\):\n\(no output\)"):
        mcp_server.run_cli(["channels"])


def test_run_cli_reports_a_timeout(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="discord-client", timeout=1)

    monkeypatch.setattr(mcp_server.subprocess, "run", slow)
    with pytest.raises(mcp_server.CLIError, match="day timed out after 600s"):
        mcp_server.run_cli(["day"], timeout=600)


# -- HTTP: bearer auth and Host allowlist -------------------------------------------


@pytest.fixture
def http(fake_cli):
    with TestClient(mcp_server.build_app(TOKEN, ["testserver"])) as client:
        yield client


def _authorized() -> dict:
    return {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x", "Bearer"],
)
def test_http_rejects_a_request_without_the_right_bearer_token(http, authorization):
    headers = dict(MCP_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    response = http.post("/mcp", json=INITIALIZE, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_http_serves_mcp_with_the_right_token(http):
    init = http.post("/mcp", json=INITIALIZE, headers=_authorized())
    assert init.status_code == 200
    assert init.json()["result"]["capabilities"]["tools"] is not None

    call_whoami = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "whoami", "arguments": {}},
    }
    result = http.post("/mcp", json=call_whoami, headers=_authorized()).json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "ran whoami\n"


def test_http_lists_the_read_only_tools(http):
    list_tools = {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}
    tools = http.post("/mcp", json=list_tools, headers=_authorized()).json()["result"]["tools"]
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)


def test_http_reports_bad_arguments_as_a_tool_error(http):
    bad_thread = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "get_thread", "arguments": {"thread_id": "abc"}},
    }
    result = http.post("/mcp", json=bad_thread, headers=_authorized()).json()["result"]
    assert result["isError"] is True
    assert "numeric Discord id" in result["content"][0]["text"]


def test_http_rejects_a_host_outside_the_allowlist(fake_cli):
    app = mcp_server.build_app(TOKEN, ["discord-client-mcp:*"])
    with TestClient(app) as client:  # sends Host: testserver
        response = client.post("/mcp", json=INITIALIZE, headers=_authorized())
    assert response.status_code == 421


def test_http_rejects_a_browser_origin(http):
    # Server-to-server clients send no Origin; a browser always does on a
    # cross-site POST, which is what a DNS-rebinding page would make.
    headers = {**_authorized(), "Origin": "http://attacker.example"}
    response = http.post("/mcp", json=INITIALIZE, headers=headers)
    assert response.status_code == 403


def test_allowed_hosts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(mcp_server.ALLOWED_HOSTS_ENV, " mcp.internal:8000 , other:* ,")
    assert mcp_server._allowed_hosts() == ["mcp.internal:8000", "other:*"]
    monkeypatch.delenv(mcp_server.ALLOWED_HOSTS_ENV)
    assert mcp_server._allowed_hosts() == list(mcp_server.DEFAULT_ALLOWED_HOSTS)


# -- run(): startup ---------------------------------------------------------------


@pytest.fixture
def fake_uvicorn(monkeypatch):
    import uvicorn

    started: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.append(kwargs))
    return started


@pytest.mark.parametrize("token", [None, "", "short", "x" * (mcp_server.MIN_TOKEN_LENGTH - 1)])
def test_run_refuses_to_start_without_a_long_enough_token(monkeypatch, fake_uvicorn, token):
    if token is not None:
        monkeypatch.setenv(mcp_server.TOKEN_ENV, token)
    with pytest.raises(SystemExit, match=mcp_server.TOKEN_ENV):
        mcp_server.run([])
    assert fake_uvicorn == []


def test_run_binds_loopback_by_default(monkeypatch, fake_uvicorn):
    monkeypatch.setenv(mcp_server.TOKEN_ENV, TOKEN)
    assert mcp_server.run([]) == 0
    assert fake_uvicorn == [{"host": "127.0.0.1", "port": 8000}]


def test_run_takes_the_bind_address_from_the_environment(monkeypatch, fake_uvicorn):
    monkeypatch.setenv(mcp_server.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(mcp_server.HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(mcp_server.PORT_ENV, "9001")
    mcp_server.run([])
    assert fake_uvicorn == [{"host": "0.0.0.0", "port": 9001}]


def test_run_flags_override_the_environment(monkeypatch, fake_uvicorn):
    monkeypatch.setenv(mcp_server.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(mcp_server.PORT_ENV, "9001")
    mcp_server.run(["--host", "::1", "--port", "9002"])
    assert fake_uvicorn == [{"host": "::1", "port": 9002}]


def test_run_rejects_a_non_numeric_port(monkeypatch, fake_uvicorn):
    monkeypatch.setenv(mcp_server.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(mcp_server.PORT_ENV, "eighty")
    with pytest.raises(SystemExit) as excinfo:
        mcp_server.run([])
    assert excinfo.value.code == 2
    assert fake_uvicorn == []

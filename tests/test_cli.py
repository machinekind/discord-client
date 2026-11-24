from __future__ import annotations

import httpx
import pytest

from discord_client import cli
from discord_client.cli import main
from discord_client.discord_api import DiscordAPIError

URL = "https://discord.com/api/v10/guilds/g1/channels"


def _api_error(status: int, body: str) -> DiscordAPIError:
    return DiscordAPIError(httpx.Response(status, request=httpx.Request("GET", URL), text=body))


def test_version_returns_zero_and_prints_version(capsys):
    exit_code = main(["version"])

    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    assert out


def _raising_main(exc: Exception):
    def fake_main(argv=None):
        raise exc

    return fake_main


def test_run_turns_a_discord_api_error_into_a_fail_line(monkeypatch, capsys):
    body = '{"message": "Missing Access", "code": 50001}'
    monkeypatch.setattr(cli, "main", _raising_main(_api_error(403, body)))

    exit_code = cli.run()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == f"[FAIL] Discord API error: GET {URL} -> 403: {body}\n"


def test_run_keeps_only_the_tail_of_a_long_body_on_one_line(monkeypatch, capsys):
    body = "<html>\n" + "x" * 5000 + "\nthe end</html>"
    monkeypatch.setattr(cli, "main", _raising_main(_api_error(502, body)))

    assert cli.run() == 1

    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith(f"[FAIL] Discord API error: GET {URL} -> 502: ...")
    assert err.rstrip().endswith("x the end</html>")
    assert len(err) < cli.ERROR_BODY_TAIL_CHARS + 200


def test_run_names_an_empty_body(monkeypatch, capsys):
    monkeypatch.setattr(cli, "main", _raising_main(_api_error(500, "")))

    assert cli.run() == 1
    assert capsys.readouterr().err.rstrip().endswith("-> 500: (empty body)")


def test_run_turns_a_network_failure_into_a_fail_line(monkeypatch, capsys):
    def no_network(argv=None):
        raise httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(cli, "main", no_network)

    assert cli.run() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "[FAIL] could not reach Discord: ConnectError: name resolution failed"
    )


def test_main_still_raises_so_tests_see_the_error(monkeypatch):
    def failing(args):
        raise _api_error(500, "boom")

    monkeypatch.setattr(cli, "_cmd_version", failing)

    with pytest.raises(DiscordAPIError):
        main(["version"])

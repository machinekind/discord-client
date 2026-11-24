"""Hermeticity: every test starts with no tokens and no settings, whatever the
developer's shell exports or the working directory holds. Tests that need a
token or a setting set it on top of this fixture.

The working directory moves to a temp dir because `load_config` picks up a
`discord-client.toml` from it and `load_env` a `.env`.
"""

from __future__ import annotations

import pytest

_AMBIENT = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_READER_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_IGNORE_CHANNELS",
    "DISCORD_TIMEZONE",
    "DISCORD_CLIENT_CONFIG",
)


@pytest.fixture(autouse=True)
def _no_ambient_state(monkeypatch, tmp_path):
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

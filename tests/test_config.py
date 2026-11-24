from __future__ import annotations

import pytest

from discord_client import config as config_module
from discord_client.config import Config, bot_token, load_config, resolve_guild_id, resolve_token

VALID_TOML = """
guild_id = "111"
ignore_channels = ["bot-logs"]
timezone = "Europe/Warsaw"
"""


class FakeGuildClient:
    def __init__(self, guilds: list[dict]) -> None:
        self._guilds = guilds
        self.calls = 0

    def my_guilds(self) -> list[dict]:
        self.calls += 1
        return self._guilds


# -- settings ---------------------------------------------------------------------


def test_defaults_when_nothing_is_configured():
    config = load_config()

    assert config == Config(guild_id=None, ignore_channels=(), timezone="UTC")


def test_load_config_reads_the_file_named_by_env(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.toml"
    config_path.write_text(VALID_TOML)
    monkeypatch.setenv("DISCORD_CLIENT_CONFIG", str(config_path))

    config = load_config()

    assert config.guild_id == "111"
    assert config.ignore_channels == ("bot-logs",)
    assert config.timezone == "Europe/Warsaw"


def test_load_config_picks_up_the_working_directory_file(tmp_path):
    # conftest already moved the working directory to tmp_path
    (tmp_path / "discord-client.toml").write_text(VALID_TOML)

    assert load_config().guild_id == "111"


def test_load_config_explicit_path_overrides_env(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.toml"
    config_path.write_text(VALID_TOML)
    monkeypatch.setenv("DISCORD_CLIENT_CONFIG", str(tmp_path / "does-not-exist.toml"))

    assert load_config(config_path).guild_id == "111"


def test_load_config_named_file_missing_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_CLIENT_CONFIG", str(tmp_path / "missing.toml"))

    with pytest.raises(SystemExit) as excinfo:
        load_config()

    assert "missing.toml" in str(excinfo.value)


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    (tmp_path / "discord-client.toml").write_text(VALID_TOML)
    monkeypatch.setenv("DISCORD_GUILD_ID", "999")
    monkeypatch.setenv("DISCORD_IGNORE_CHANNELS", "spam, logs ,")
    monkeypatch.setenv("DISCORD_TIMEZONE", "America/New_York")

    config = load_config()

    assert config.guild_id == "999"
    assert config.ignore_channels == ("spam", "logs")
    assert config.timezone == "America/New_York"


def test_unquoted_guild_id_is_accepted(tmp_path):
    (tmp_path / "discord-client.toml").write_text("guild_id = 123456789012345678\n")

    assert load_config().guild_id == "123456789012345678"


def test_unknown_key_exits_naming_it(tmp_path):
    # A misspelt key must not be silently ignored: the bot would then read
    # whichever guild it happens to be in, not the one the file meant.
    (tmp_path / "discord-client.toml").write_text('guild-id = "111"\n')

    with pytest.raises(SystemExit) as excinfo:
        load_config()

    assert "guild-id" in str(excinfo.value)


def test_ignore_channels_must_be_a_list_of_names(tmp_path):
    (tmp_path / "discord-client.toml").write_text('ignore_channels = "bot-logs"\n')

    with pytest.raises(SystemExit) as excinfo:
        load_config()

    assert "ignore_channels" in str(excinfo.value)


def test_invalid_toml_exits(tmp_path):
    (tmp_path / "discord-client.toml").write_text("guild_id = \n")

    with pytest.raises(SystemExit) as excinfo:
        load_config()

    assert "not valid TOML" in str(excinfo.value)


def test_unknown_timezone_exits(monkeypatch):
    monkeypatch.setenv("DISCORD_TIMEZONE", "Mars/Olympus_Mons")

    with pytest.raises(SystemExit) as excinfo:
        load_config()

    assert "Mars/Olympus_Mons" in str(excinfo.value)


# -- guild resolution -------------------------------------------------------------


def test_configured_guild_wins_without_asking_discord():
    client = FakeGuildClient([{"id": "other", "name": "Other"}])

    assert resolve_guild_id(client, Config(guild_id="111")) == "111"
    assert client.calls == 0


def test_single_guild_is_detected():
    client = FakeGuildClient([{"id": "g1", "name": "Robotics"}])

    assert resolve_guild_id(client, Config()) == "g1"


def test_no_guild_exits():
    with pytest.raises(SystemExit) as excinfo:
        resolve_guild_id(FakeGuildClient([]), Config())

    assert "not a member of any guild" in str(excinfo.value)


def test_several_guilds_exit_listing_them():
    client = FakeGuildClient([{"id": "g1", "name": "Robotics"}, {"id": "g2", "name": "Chess"}])

    with pytest.raises(SystemExit) as excinfo:
        resolve_guild_id(client, Config())

    message = str(excinfo.value)
    assert "Robotics (g1)" in message and "Chess (g2)" in message
    assert "DISCORD_GUILD_ID" in message


# -- tokens -----------------------------------------------------------------------


def test_bot_token_missing_exits():
    with pytest.raises(SystemExit) as excinfo:
        bot_token()

    assert "DISCORD_BOT_TOKEN" in str(excinfo.value)


def test_bot_token_present(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-token")

    assert bot_token() == "secret-token"


def test_resolve_env_writer_wins_over_env_reader(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-writer")
    monkeypatch.setenv("DISCORD_READER_TOKEN", "env-reader")

    resolved = resolve_token()

    assert resolved.token == "env-writer"
    assert resolved.tier == "writer"
    assert resolved.source == "env:DISCORD_BOT_TOKEN"


def test_resolve_env_reader_only(monkeypatch):
    monkeypatch.setenv("DISCORD_READER_TOKEN", "env-reader")

    resolved = resolve_token()

    assert resolved.token == "env-reader"
    assert resolved.tier == "reader"
    assert resolved.source == "env:DISCORD_READER_TOKEN"


def test_resolve_nothing_anywhere_exits_with_remediation():
    with pytest.raises(SystemExit) as excinfo:
        resolve_token()

    message = str(excinfo.value)
    assert "DISCORD_BOT_TOKEN" in message
    assert "DISCORD_READER_TOKEN" in message


def test_require_write_satisfied_by_env_writer(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-writer")

    resolved = resolve_token(require_write=True)

    assert resolved.token == "env-writer"
    assert resolved.tier == "writer"


def test_require_write_rejects_reader_sources(monkeypatch):
    monkeypatch.setenv("DISCORD_READER_TOKEN", "env-reader")

    with pytest.raises(SystemExit) as excinfo:
        resolve_token(require_write=True)

    message = str(excinfo.value)
    assert "writes to Discord" in message
    assert "DISCORD_BOT_TOKEN" in message


def test_empty_env_var_falls_through_to_next_source(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    monkeypatch.setenv("DISCORD_READER_TOKEN", "env-reader")

    resolved = resolve_token()

    assert resolved.token == "env-reader"
    assert resolved.tier == "reader"


def test_bot_token_delegates_to_resolve(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-writer")

    assert bot_token() == "env-writer"
    assert bot_token(require_write=True) == "env-writer"


# -- .env -------------------------------------------------------------------------


def test_load_env_reads_the_working_directory_dotenv(tmp_path):
    (tmp_path / ".env").write_text('DISCORD_READER_TOKEN="dotenv-token"\n')

    config_module.load_env()

    assert resolve_token().token == "dotenv-token"


def test_load_env_never_overrides_real_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_READER_TOKEN", "from-env")
    env_file = tmp_path / ".env"
    env_file.write_text('DISCORD_READER_TOKEN="dotenv-file-token"\n')

    config_module.load_env(env_file)

    assert resolve_token().token == "from-env"

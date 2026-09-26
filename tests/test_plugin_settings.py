"""Native settings: /config (userConfig) values reach the harness identity.

Claude Code exports each plugin option to hooks as CLAUDE_PLUGIN_OPTION_<KEY>
and .mcp.json forwards the same names to the MCP server. These tests drive the
binding with those names and assert what the real harness resolves.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from musubi_harness.plugin_runtime import PluginRuntime, RuntimeConfigError

from scripts.musubi_claude_runtime import STATE_NAME, apply_plugin_settings

OPTION = "CLAUDE_PLUGIN_OPTION_"


def settings(**values: str) -> dict[str, str]:
    return {OPTION + k.upper(): v for k, v in values.items()}


def resolve(env: dict[str, str], data_root: Path):
    """Apply the mapping, then ask the real harness what identity it sees."""
    with patch.dict(os.environ, env, clear=True):
        source = apply_plugin_settings()
        os.environ["PLUGIN_DATA"] = str(data_root)
        return source, PluginRuntime(STATE_NAME).runtime_config()


def test_full_settings_become_the_harness_identity(tmp_path: Path) -> None:
    source, config = resolve(settings(actor="alice", seat="laptop", zone="work", delivery_mode="verified"), tmp_path)
    assert source == "settings"
    assert (config.actor, config.presence, config.zone, config.delivery_mode) == (
        "alice",
        "alice/laptop",
        "work",
        "verified",
    )


def test_delivery_mode_defaults_to_shadow_when_unset(tmp_path: Path) -> None:
    _, config = resolve(settings(actor="alice", seat="laptop", zone="home"), tmp_path)
    assert config.delivery_mode == "shadow"


def test_no_actor_keeps_the_legacy_config_json(tmp_path: Path) -> None:
    # zone and delivery_mode always carry their picker defaults; alone they
    # must not switch an existing config.json install over to settings.
    (tmp_path / "config.json").write_text('{"actor": "bob", "presence": "bob/desk", "zone": "home"}', encoding="utf-8")
    source, config = resolve(settings(actor="", seat="", zone="home", delivery_mode="shadow"), tmp_path)
    assert source == "legacy"
    assert (config.actor, config.presence) == ("bob", "bob/desk")


def test_actor_without_seat_is_refused_not_guessed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigError, match="partial_identity_config_refused"):
        resolve(settings(actor="alice", seat="", zone="home"), tmp_path)


def test_settings_outrank_a_legacy_config_json(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text('{"actor": "bob", "presence": "bob/desk", "zone": "home"}', encoding="utf-8")
    _, config = resolve(settings(actor="alice", seat="laptop", zone="work"), tmp_path)
    assert (config.actor, config.presence, config.zone) == ("alice", "alice/laptop", "work")


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ({"actor": "Alice", "seat": "laptop", "zone": "home"}, "identity_config_invalid"),
        ({"actor": "alice", "seat": "lap top", "zone": "home"}, "identity_config_invalid"),
        ({"actor": "alice", "seat": "laptop", "zone": "home", "delivery_mode": "live"}, "delivery_mode_invalid"),
    ],
    ids=["uppercase actor", "space in seat", "unknown delivery mode"],
)
def test_invalid_settings_are_refused_by_the_harness(tmp_path: Path, values: dict[str, str], error: str) -> None:
    with pytest.raises(RuntimeConfigError, match=error):
        resolve(settings(**values), tmp_path)


def test_manifest_and_mcp_forward_the_same_option_names() -> None:
    import json

    root = Path(__file__).resolve().parent.parent
    options = set(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["userConfig"])
    env = json.loads((root / ".mcp.json").read_text())["mcpServers"]["musubi-claude"]["env"]
    assert options == {"actor", "seat", "zone", "delivery_mode", "musubi_url", "musubi_token"}
    assert env == {OPTION + key.upper(): f"${{user_config.{key}}}" for key in options}


def test_only_the_token_is_sensitive() -> None:
    import json

    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
    assert [key for key, spec in config.items() if spec.get("sensitive")] == ["musubi_token"]


TRANSPORT = {"MUSUBI_API_URL", "MUSUBI_TOKEN"}


def transport_env(env: dict[str, str]) -> dict[str, str]:
    with patch.dict(os.environ, env, clear=True):
        apply_plugin_settings()
        return {k: v for k, v in os.environ.items() if k in TRANSPORT}


def test_url_and_token_settings_become_the_harness_transport_env() -> None:
    env = settings(musubi_url="https://musubi.example.com", musubi_token="a.b.c")
    assert transport_env(env) == {"MUSUBI_API_URL": "https://musubi.example.com", "MUSUBI_TOKEN": "a.b.c"}


def test_transport_settings_apply_without_a_settings_identity() -> None:
    # A legacy config.json identity can still use a URL and token from settings.
    env = settings(actor="", musubi_url="https://musubi.example.com", musubi_token="a.b.c")
    assert transport_env(env)["MUSUBI_API_URL"] == "https://musubi.example.com"


def test_empty_transport_settings_never_clear_an_existing_setup() -> None:
    existing = {"MUSUBI_API_URL": "https://house.example", "MUSUBI_TOKEN": "x.y.z"}
    assert transport_env({**existing, **settings(musubi_url="", musubi_token="  ")}) == existing


def test_transport_settings_outrank_an_inherited_environment() -> None:
    env = {"MUSUBI_API_URL": "https://old.example", **settings(musubi_url="https://new.example")}
    assert transport_env(env)["MUSUBI_API_URL"] == "https://new.example"

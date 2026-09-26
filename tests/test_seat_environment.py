"""A seat whose launcher sets MUSUBI_ACTOR owns identity, transport and binaries.

Plugin options are per OS user: on a host where several seats share one user,
whatever /config holds would otherwise make every seat run as its author, with
their token (Aoi's 0.5.0 canary). And a legacy config.json pinning the old
fleet-tools binaries would otherwise keep the seat on the old client.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from musubi_harness.plugin_runtime import PluginRuntime

from scripts.musubi_claude_runtime import STATE_NAME, apply_plugin_settings

OPTION = "CLAUDE_PLUGIN_OPTION_"
OTHER_SEAT = {
    OPTION + "ACTOR": "aoi",
    OPTION + "SEAT": "voice",
    OPTION + "ZONE": "work",
    OPTION + "DELIVERY_MODE": "shadow",
    OPTION + "MUSUBI_URL": "https://other.example",
    OPTION + "MUSUBI_TOKEN": "other.seats.token",
    OPTION + "PROMPT_RECALL": "on",
}
SEAT = {"MUSUBI_ACTOR": "shiori", "MUSUBI_PRESENCE": "shiori/command-chair", "MUSUBI_ZONE": "home"}
TRANSPORT = {"MUSUBI_API_URL": "https://musubi.example", "MUSUBI_TOKEN": "seat.own.token"}


@pytest.fixture
def venv(tmp_path: Path) -> Path:
    """A plugin venv bin with the new harness and its bundled client."""
    bin_dir = tmp_path / "plugin-venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python", "musubi-harness", "musubi-memory-data"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    return bin_dir


def resolve(env: dict[str, str], venv: Path, data: Path) -> tuple[str, dict[str, str], str, str]:
    """Apply settings as the plugin does, then ask the real harness what it resolves."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(
        json.dumps({"harness_bin": "/fleet-tools/bin/musubi-harness", "memory_data_bin": "/fleet-tools/bin/memory-data"})
    )
    with patch.dict(os.environ, env, clear=True), patch("sys.executable", str(venv / "python")):
        source = apply_plugin_settings()
        os.environ["PLUGIN_DATA"] = str(data)
        runtime = PluginRuntime(STATE_NAME)
        config = runtime.runtime_config()
        return source, dict(os.environ), runtime.harness_bin(config), runtime.memory_data_bin(config)


def test_the_seat_env_wins_over_another_seats_plugin_options(venv: Path, tmp_path: Path) -> None:
    source, env, harness, memory_data = resolve({**OTHER_SEAT, **SEAT, **TRANSPORT}, venv, tmp_path / "data")
    assert source == "environment"
    assert (env["MUSUBI_ACTOR"], env["MUSUBI_PRESENCE"]) == ("shiori", "shiori/command-chair")
    assert env["MUSUBI_TOKEN"] == "seat.own.token" and env["MUSUBI_API_URL"] == "https://musubi.example"
    assert "other.seats.token" not in json.dumps(env)  # never mapped, never kept
    assert env[OPTION + "PROMPT_RECALL"] == "on"  # preferences are not identity
    # Off fleet-tools: the plugin's own harness and bundled client, not the config.json pins.
    assert (harness, memory_data) == (str(venv / "musubi-harness"), str(venv / "musubi-memory-data"))


def test_without_a_seat_token_the_configured_client_is_kept(venv: Path, tmp_path: Path) -> None:
    # The bundled client cannot work without a URL and token; fall back, visibly (health reports it).
    _, _, harness, memory_data = resolve({**OTHER_SEAT, **SEAT}, venv, tmp_path / "data")
    assert harness == str(venv / "musubi-harness")
    assert memory_data == "/fleet-tools/bin/memory-data"


def test_an_explicit_launcher_pin_is_respected(venv: Path, tmp_path: Path) -> None:
    pinned = {**SEAT, **TRANSPORT, "MUSUBI_HARNESS_BIN": "/launcher/musubi-harness", "MUSUBI_MEMORY_DATA_BIN": "/launcher/md"}
    _, _, harness, memory_data = resolve(pinned, venv, tmp_path / "data")
    assert (harness, memory_data) == ("/launcher/musubi-harness", "/launcher/md")


def test_a_single_user_install_without_seat_env_is_unchanged(venv: Path, tmp_path: Path) -> None:
    single = {**OTHER_SEAT, OPTION + "ACTOR": "alice", OPTION + "SEAT": "laptop"}
    source, env, _, _ = resolve(single, venv, tmp_path / "data")
    assert source == "settings"
    assert (env["MUSUBI_ACTOR"], env["MUSUBI_PRESENCE"], env["MUSUBI_TOKEN"]) == ("alice", "alice/laptop", "other.seats.token")

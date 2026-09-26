"""The plugin state root follows Claude Code's CLAUDE_PLUGIN_DATA without splitting a live outbox."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from musubi_harness.plugin_runtime import PluginRuntime

from scripts.musubi_claude_runtime import STATE_NAME, resolve_state_root


def legacy(home: Path) -> Path:
    return home / ".local" / "state" / STATE_NAME


def test_without_claude_plugin_data_the_harness_default_stands(tmp_path: Path) -> None:
    assert resolve_state_root({}, tmp_path) == (None, "legacy_default")


def test_fresh_install_uses_claude_plugin_data(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    assert resolve_state_root({"CLAUDE_PLUGIN_DATA": str(data)}, tmp_path) == (data, "claude_plugin_data")


def outbox(root: Path, actor: str = "alice", zone: str = "home") -> None:
    (root / actor / zone).mkdir(parents=True, exist_ok=True)
    (root / actor / zone / "shadow.db").write_bytes(b"")


def test_live_legacy_state_is_kept_not_split(tmp_path: Path) -> None:
    outbox(legacy(tmp_path))
    data = tmp_path / "plugin-data"
    assert resolve_state_root({"CLAUDE_PLUGIN_DATA": str(data)}, tmp_path) == (legacy(tmp_path), "legacy_kept")


def test_once_the_plugin_data_dir_has_state_it_wins(tmp_path: Path) -> None:
    outbox(legacy(tmp_path))
    data = tmp_path / "plugin-data"
    outbox(data)
    assert resolve_state_root({"CLAUDE_PLUGIN_DATA": str(data)}, tmp_path) == (data, "claude_plugin_data")


def test_an_empty_legacy_dir_does_not_hold_the_plugin_back(tmp_path: Path) -> None:
    legacy(tmp_path).mkdir(parents=True)
    data = tmp_path / "plugin-data"
    assert resolve_state_root({"CLAUDE_PLUGIN_DATA": str(data)}, tmp_path)[1] == "claude_plugin_data"


def test_the_harness_uses_the_chosen_root_and_plugin_data_still_wins(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    root, _ = resolve_state_root({"CLAUDE_PLUGIN_DATA": str(data)}, tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        assert PluginRuntime(STATE_NAME, default_data_root=root).data_root() == data
    with patch.dict(os.environ, {"PLUGIN_DATA": str(tmp_path / "explicit")}, clear=True):
        assert PluginRuntime(STATE_NAME, default_data_root=root).data_root() == tmp_path / "explicit"


def test_setup_artifacts_do_not_move_a_live_legacy_outbox(tmp_path: Path) -> None:
    # Aoi's repro on #6: setup's venv/ and the not-set-up degraded.jsonl made the
    # plugin data dir look like state and orphaned the legacy outbox.
    outbox(legacy(tmp_path), actor="aoi")
    data = tmp_path / "plugin-data"
    (data / "venv" / "bin").mkdir(parents=True)
    (data / "degraded.jsonl").write_text('{"reason": "harness_unavailable:stop"}\n')
    assert resolve_state_root({"CLAUDE_PLUGIN_DATA": str(data)}, tmp_path) == (legacy(tmp_path), "legacy_kept")


def test_a_legacy_config_json_alone_counts_as_state(tmp_path: Path) -> None:
    legacy(tmp_path).mkdir(parents=True)
    (legacy(tmp_path) / "config.json").write_text("{}")
    data = tmp_path / "plugin-data"
    assert resolve_state_root({"CLAUDE_PLUGIN_DATA": str(data)}, tmp_path)[1] == "legacy_kept"

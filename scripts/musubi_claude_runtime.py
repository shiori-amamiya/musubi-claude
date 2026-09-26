"""Claude deployment binding for the shared Musubi plugin runtime.

This is the only host-specific runtime code the Claude adapter owns: it
names this plugin's state root and re-exports the harness's runtime
helpers. All identity, namespace, delivery-mode, and canonical-tool
POLICY lives in ``musubi_harness.plugin_runtime`` so the Claude and Codex
seats can never silently evolve different memory boundaries.

Compared with the in-fleet-tools version, the filesystem walk that
located ``../lib/musubi_harness/`` is gone — the harness is a real
PyPI dependency now (``musubi-harness>=1.0.0``), installed via pip,
imported the normal way. Same escape hatches: ``MUSUBI_HARNESS_BIN``
env override, ``harness_bin`` config override, ``shutil.which`` PATH
lookup. Same state root: ``$PLUGIN_DATA`` (set by Claude Code) or
``~/.local/state/musubi-claude``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from musubi_harness.plugin_runtime import (
    PluginRuntime,
    RuntimeConfig,
    RuntimeConfigError,
)

STATE_NAME = "musubi-claude"

# Claude Code passes the plugin's /config settings (userConfig) to hooks as
# CLAUDE_PLUGIN_OPTION_<KEY>, and .mcp.json forwards the same names into the
# MCP server via ${user_config.<key>}. Users never set these by hand.
_OPTION = "CLAUDE_PLUGIN_OPTION_"


def apply_plugin_settings(environ: MutableMapping[str, str] | None = None) -> str:
    """Map this plugin's settings onto the harness identity, in-process only.

    Settings are active when ``actor`` is set; ``presence`` is built as
    ``actor/seat`` so it always satisfies the harness's actor/presence rule.
    With no ``actor``, the harness keeps reading its legacy ``config.json``
    (existing installs are unaffected). A half-filled identity (actor
    without seat) is passed through as-is so the harness refuses it with
    ``partial_identity_config_refused`` instead of guessing.

    Returns "settings" or "legacy", for status and tests.
    """
    env = os.environ if environ is None else environ
    actor = env.get(_OPTION + "ACTOR", "").strip()
    seat = env.get(_OPTION + "SEAT", "").strip()
    zone = env.get(_OPTION + "ZONE", "").strip()
    mode = env.get(_OPTION + "DELIVERY_MODE", "").strip()
    if not actor:
        return "legacy"
    env["MUSUBI_ACTOR"] = actor
    env["MUSUBI_PRESENCE"] = f"{actor}/{seat}" if seat else ""
    env["MUSUBI_ZONE"] = zone
    if mode:
        env["MUSUBI_DELIVERY_MODE"] = mode
    return "settings"


settings_source = apply_plugin_settings()


def _has_state(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def resolve_state_root(environ: Mapping[str, str] | None = None, home: Path | None = None) -> tuple[Path | None, str]:
    """Pick the state root to hand the harness, and say why.

    Claude Code gives each plugin ``CLAUDE_PLUGIN_DATA`` (kept across updates,
    removed on uninstall). The harness itself reads ``PLUGIN_DATA`` and
    otherwise falls back to ``~/.local/state/musubi-claude``, which is where
    every earlier install kept its outbox. Never split a live outbox: if the
    legacy root holds state and the plugin data dir does not, keep the legacy
    root and report it, so a move is a deliberate step, not a side effect.

    Returns (default_data_root for PluginRuntime, source label), where the
    label is "claude_plugin_data", "legacy_kept" or "legacy_default".
    """
    env = os.environ if environ is None else environ
    legacy = (Path.home() if home is None else home) / ".local" / "state" / STATE_NAME
    plugin_data = env.get("CLAUDE_PLUGIN_DATA", "").strip()
    if not plugin_data:
        return None, "legacy_default"
    target = Path(plugin_data).expanduser()
    if _has_state(legacy) and not _has_state(target):
        return legacy, "legacy_kept"
    return target, "claude_plugin_data"


_default_root, state_root_source = resolve_state_root()
# The harness still honours an explicit PLUGIN_DATA first; otherwise it uses
# the root chosen above.
_runtime = PluginRuntime(STATE_NAME, default_data_root=_default_root)

# The shared thin bindings (mcp-facade, continuity) bind to this exact
# instance so adapter behaviour cannot diverge from the harness contract.
runtime = _runtime
data_root = _runtime.data_root
plugin_config = _runtime.plugin_config
runtime_config = _runtime.runtime_config
harness_bin = _runtime.harness_bin
memory_data_bin = _runtime.memory_data_bin
tool_environment = _runtime.tool_environment
require_owned_namespace = _runtime.require_owned_namespace

# Re-export the harness's exception type so adapter scripts can catch
# identity/config failures uniformly.
__all__ = [
    "PluginRuntime",
    "RuntimeConfig",
    "RuntimeConfigError",
    "STATE_NAME",
    "apply_plugin_settings",
    "settings_source",
    "resolve_state_root",
    "state_root_source",
    "runtime",
    "data_root",
    "plugin_config",
    "runtime_config",
    "harness_bin",
    "memory_data_bin",
    "tool_environment",
    "require_owned_namespace",
]

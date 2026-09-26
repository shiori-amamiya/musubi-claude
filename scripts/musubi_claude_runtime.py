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
import sys
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


def apply_transport_settings(env: MutableMapping[str, str]) -> str:
    """Map the Musubi URL and token settings onto the harness's child env.

    musubi-harness (>=1.1.0) passes ``MUSUBI_API_URL`` and ``MUSUBI_TOKEN`` to
    whichever memory-data it runs: its bundled HTTP client, or an operator
    memory-data, which also honours them. Each is set only when its setting is
    non-empty, so an unset field never clears an existing setup. The token is
    a ``sensitive`` option: Claude Code keeps it in the credential store and
    exports it only to this plugin's hooks and MCP server. Nothing here prints
    or records it.

    Returns "settings" when either value came from settings, else "none".
    """
    source = "none"
    for option, target in (("MUSUBI_URL", "MUSUBI_API_URL"), ("MUSUBI_TOKEN", "MUSUBI_TOKEN")):
        value = env.get(_OPTION + option, "").strip()
        if value:
            env[target] = value
            source = "settings"
    # Claude Code exports the sensitive option under its own name too. Keep
    # MUSUBI_TOKEN as the only holder, so stripping it (local_tool_environment)
    # really leaves a local subprocess without the credential. (Aoi's review.)
    env.pop(_OPTION + "MUSUBI_TOKEN", None)
    return source


# Plugin options are per OS user (one pluginConfigs block, one keychain token), so on
# a host where several seats share a user they cannot express a per-seat identity
# (Aoi's 0.5.0 canary: a local-scope install still wrote her identity globally).
_SEAT_OWNED_OPTIONS = ("ACTOR", "SEAT", "ZONE", "DELIVERY_MODE", "MUSUBI_URL", "MUSUBI_TOKEN")


def apply_seat_environment(env: MutableMapping[str, str]) -> bool:
    """A seat whose launcher sets MUSUBI_ACTOR owns identity AND transport.

    For that process the per-user identity and transport options are dropped
    (whatever /config holds cannot make this seat run as someone else, or with
    their token), and the plugin's own harness is used rather than a legacy
    config.json pin: its venv's ``musubi-harness`` unless the launcher set
    MUSUBI_HARNESS_BIN, and its bundled ``musubi-memory-data`` whenever the seat
    supplies MUSUBI_API_URL and MUSUBI_TOKEN. Without those two the harness falls
    back to whatever memory-data is configured; health reports which one runs.

    Returns True when the seat owns its identity.
    """
    if not env.get("MUSUBI_ACTOR", "").strip():
        return False
    for key in _SEAT_OWNED_OPTIONS:
        env.pop(_OPTION + key, None)
    venv_bin = Path(sys.executable).parent
    harness = venv_bin / "musubi-harness"
    if not env.get("MUSUBI_HARNESS_BIN", "").strip() and harness.is_file():
        env["MUSUBI_HARNESS_BIN"] = str(harness)
    bundled = venv_bin / "musubi-memory-data"
    if (
        env.get("MUSUBI_API_URL", "").strip()
        and env.get("MUSUBI_TOKEN", "").strip()
        and not env.get("MUSUBI_MEMORY_DATA_BIN", "").strip()
        and bundled.is_file()
    ):
        env["MUSUBI_MEMORY_DATA_BIN"] = str(bundled)
    return True


def apply_plugin_settings(environ: MutableMapping[str, str] | None = None) -> str:
    """Map this plugin's settings onto the harness identity, in-process only.

    Settings are active when ``actor`` is set; ``presence`` is built as
    ``actor/seat`` so it always satisfies the harness's actor/presence rule.
    With no ``actor``, the harness keeps reading its legacy ``config.json``
    (existing installs are unaffected). A half-filled identity (actor
    without seat) is passed through as-is so the harness refuses it with
    ``partial_identity_config_refused`` instead of guessing.

    A seat launcher that sets MUSUBI_ACTOR takes precedence over all of this
    (see apply_seat_environment).

    Returns "environment", "settings" or "legacy", for status and tests.
    """
    env = os.environ if environ is None else environ
    if apply_seat_environment(env):
        return "environment"
    apply_transport_settings(env)
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
    """True when ``path`` holds harness state: its ``config.json`` or an outbox.

    The harness keeps one outbox per identity at ``<actor>/<zone>/shadow.db``.
    Anything else (the plugin's own ``venv/`` from setup, a ``degraded.jsonl``
    written while not set up) is not state and must not move the root.
    """
    try:
        return (path / "config.json").is_file() or any(path.glob("*/*/shadow.db"))
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
# For subprocesses that only touch the local outbox (enqueue, stage, remember):
# tool_environment minus MUSUBI_API_URL / MUSUBI_TOKEN. musubi-harness >= 1.1.0.
local_tool_environment = _runtime.local_tool_environment
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
    "local_tool_environment",
    "require_owned_namespace",
]

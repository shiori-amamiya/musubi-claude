"""Smoke tests for the Claude plugin's runtime binding.

These tests verify the Claude-specific runtime binding — the one and
only piece of host-specific code the adapter owns — wires up to the
shared `musubi_harness` runtime correctly. They do not exercise
behavior (the harness has its own tests); they verify the binding
shape so a future harness-API change surfaces here.
"""

from __future__ import annotations

import importlib

import pytest


def test_runtime_binding_imports() -> None:
    """The runtime binding module imports cleanly under its installed name.

    Note: `scripts/` is the package directory because Hatch treats it
    that way for the wheel build. The module is callable as
    `scripts.musubi_claude_runtime` from inside the plugin's venv.
    """
    mod = importlib.import_module("scripts.musubi_claude_runtime")
    assert mod.STATE_NAME == "musubi-claude"


def test_state_name_segment_compliant() -> None:
    """The state name is a valid lowercase segment.

    PluginRuntime.__init__ validates `state_name` against the segment
    regex (^[a-z0-9][a-z0-9._-]*$). Anything else raises ValueError.
    """
    mod = importlib.import_module("scripts.musubi_claude_runtime")
    import re

    assert re.fullmatch(r"^[a-z0-9][a-z0-9._-]*$", mod.STATE_NAME), f"STATE_NAME {mod.STATE_NAME!r} is not a valid segment"


def test_runtime_instance_exposes_harness_contract() -> None:
    """Every helper the harness facade expects is re-exported."""
    mod = importlib.import_module("scripts.musubi_claude_runtime")
    expected = {
        "runtime",
        "data_root",
        "plugin_config",
        "runtime_config",
        "harness_bin",
        "memory_data_bin",
        "tool_environment",
        "require_owned_namespace",
    }
    missing = expected - set(dir(mod))
    assert not missing, f"runtime binding missing exports: {missing}"


@pytest.mark.parametrize(
    "harness_name",
    ["PluginRuntime", "RuntimeConfig", "RuntimeConfigError"],
)
def test_harness_types_re_exported(harness_name: str) -> None:
    """The harness's exception and dataclass types are importable from the binding."""
    mod = importlib.import_module("scripts.musubi_claude_runtime")
    assert hasattr(mod, harness_name), f"binding missing {harness_name}"


def test_no_filesystem_walk_for_harness_lib() -> None:
    """The binding must not import from `parents[3]/lib` (legacy fleet-tools layout).

    The harness is now a real PyPI dependency; the binding imports it
    directly. Any future PR that re-introduces a filesystem walk for
    `../lib` should fail this test loudly.
    """
    mod = importlib.import_module("scripts.musubi_claude_runtime")
    source_path = mod.__file__
    assert source_path is not None
    # Static check on the source: no references to parents[3]/lib or
    # sys.path.insert. This is cheap and catches the regression before
    # the import-time walk could fire.
    import pathlib

    text = pathlib.Path(source_path).read_text(encoding="utf-8")
    assert "parents[3]" not in text, (
        "binding contains 'parents[3]' — this is the legacy fleet-tools filesystem walk for ../lib; the harness is a PyPI dep now"
    )
    assert "sys.path.insert" not in text, (
        "binding contains 'sys.path.insert' — the harness is a PyPI dep now and should be imported the normal way"
    )

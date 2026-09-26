"""scripts/musubi-claude-run: pick a Python that has musubi-harness, or refuse visibly.

The launcher never installs anything. These tests run it with a PATH whose
python3 cannot import musubi_harness (the system interpreter), so they prove the
not-set-up behaviour on any machine and in CI.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "scripts" / "musubi-claude-run"
BARE_PATH = "/usr/bin:/bin"


def run(component: str, data: Path, stdin: str = "{}") -> subprocess.CompletedProcess[str]:
    env = {"PATH": BARE_PATH, "HOME": str(data.parent), "CLAUDE_PLUGIN_ROOT": str(ROOT), "CLAUDE_PLUGIN_DATA": str(data)}
    return subprocess.run([str(RUN), component], input=stdin, capture_output=True, text=True, env=env, timeout=30)


@pytest.fixture(autouse=True)
def _bare_python_lacks_harness() -> None:
    probe = subprocess.run(["python3", "-c", "import musubi_harness"], env={"PATH": BARE_PATH}, capture_output=True, check=False)
    if probe.returncode == 0:
        pytest.skip("the system python3 on this machine already has musubi_harness")


def degraded(data: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in (data / "degraded.jsonl").read_text().splitlines()]


def test_stop_without_setup_records_and_lets_the_session_continue(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    result = run("stop", data)
    assert result.returncode == 0 and result.stdout == ""
    assert [r["reason"] for r in degraded(data)] == ["harness_unavailable:stop"]


def test_session_start_without_setup_tells_the_user_how_to_fix_it(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    result = run("session-start", data)
    assert result.returncode == 0
    assert "/musubi-claude:setup" in json.loads(result.stdout)["systemMessage"]
    assert degraded(data)[0]["reason"] == "harness_unavailable:session-start"


def test_mcp_without_setup_fails_visibly(tmp_path: Path) -> None:
    result = run("mcp", tmp_path / "plugin-data")
    assert result.returncode == 1 and "musubi-claude:setup" in result.stderr


REQUIRED = (ROOT / "scripts" / "harness-requirement").read_text().strip().removeprefix("musubi-harness==")


def fake_venv(data: Path, harness_version: str | None) -> None:
    """A plugin venv whose python echoes its script, with harness metadata at a version."""
    fake = data / "venv" / "bin" / "python"
    fake.parent.mkdir(parents=True)
    fake.write_text('#!/bin/sh\necho "ran $1"\n')
    fake.chmod(0o755)
    if harness_version is not None:
        (data / "venv" / "lib" / "python3.12" / "site-packages" / f"musubi_harness-{harness_version}.dist-info").mkdir(parents=True)


def test_the_plugin_venv_python_is_used_when_present(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    fake_venv(data, REQUIRED)
    result = run("stop", data)
    assert result.returncode == 0
    assert result.stdout.strip() == f"ran {ROOT / 'scripts' / 'musubi-claude-stop'}"
    assert not (data / "degraded.jsonl").exists()


def test_a_newer_harness_than_required_is_used(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    fake_venv(data, "99.0.0")
    assert run("stop", data).stdout.startswith("ran ")


def test_a_venv_without_the_harness_counts_as_not_set_up(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    fake_venv(data, None)
    result = run("stop", data)
    assert result.stdout == "" and degraded(data)[0]["reason"] == "harness_unavailable:stop"


# Aoi, 2026-09-26: after a plugin update raised the pin, a venv left by an
# earlier setup still had musubi-harness 1.0.1, and every entry point died on
# import with an AttributeError traceback and no hint.
@pytest.mark.parametrize("old", ["1.0.1", "1.0.99", "1.1.0rc1"])
def test_an_outdated_harness_is_refused_visibly_on_every_entry_point(tmp_path: Path, old: str) -> None:
    data = tmp_path / "plugin-data"
    fake_venv(data, old)

    stop = run("stop", data)
    assert stop.returncode == 0 and stop.stdout == ""  # never ran the old harness

    start = run("session-start", data)
    message = json.loads(start.stdout)["systemMessage"]
    assert f"musubi-harness {old} is older than the required {REQUIRED}" in message
    assert "/musubi-claude:setup" in message

    mcp = run("mcp", data)
    assert mcp.returncode == 1 and f"{old} is older than the required {REQUIRED}" in mcp.stderr
    assert "Traceback" not in mcp.stderr

    health = json.loads(run("health", data).stdout)
    assert (health["setup"], health["error"], health["installed"], health["required"]) == (False, "harness_outdated", old, REQUIRED)

    assert [r["reason"] for r in degraded(data)] == ["harness_outdated:stop", "harness_outdated:session-start", "harness_outdated:mcp"]


def test_an_unknown_component_is_refused(tmp_path: Path) -> None:
    result = run("../../etc/passwd", tmp_path)
    assert result.returncode == 2


def test_hooks_and_mcp_start_through_the_launcher() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    commands = [h["command"] for event in hooks.values() for group in event for h in group["hooks"]]
    assert all("scripts/musubi-claude-run" in c for c in commands)
    mcp = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["musubi-claude"]
    assert mcp["command"].endswith("scripts/musubi-claude-run") and mcp["args"] == ["mcp"]
    assert os.access(RUN, os.X_OK) and os.access(ROOT / "scripts" / "musubi-claude-setup", os.X_OK)

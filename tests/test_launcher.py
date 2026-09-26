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


def test_the_plugin_venv_python_is_used_when_present(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    fake = data / "venv" / "bin" / "python"
    fake.parent.mkdir(parents=True)
    fake.write_text('#!/bin/sh\necho "ran $1"\n')
    fake.chmod(0o755)
    result = run("stop", data)
    assert result.returncode == 0
    assert result.stdout.strip() == f"ran {ROOT / 'scripts' / 'musubi-claude-stop'}"
    assert not (data / "degraded.jsonl").exists()


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

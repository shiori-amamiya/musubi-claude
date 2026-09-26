"""The Stop hook asks for a batch drain, and falls back once for an older harness.

The drain runs whatever harness binary is configured, which may predate
``--max`` (musubi-harness < 1.2.0). Such a binary exits 2 with argparse's
"unrecognized arguments"; the hook then runs the one-row drain it always ran.
Any other failure is not retried.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_credential_boundary import OPTION, SETTINGS, hook_payload, load_stop

ARGPARSE_REJECT = "usage: musubi-harness ...\nmusubi-harness: error: unrecognized arguments: --max 5 --budget-seconds 10\n"


def run_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drain: Any) -> tuple[Any, list[list[str]]]:
    stop = load_stop(
        monkeypatch,
        tmp_path,
        **SETTINGS,
        **{OPTION + "DELIVERY_MODE": "verified"},
        MUSUBI_HARNESS_BIN="/opt/fake/musubi-harness",
        MUSUBI_MEMORY_DATA_BIN="/opt/fake/memory-data",
    )
    drains: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if argv[3] != "drain":
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        drains.append(argv)
        return drain(argv)

    monkeypatch.setattr(stop.subprocess, "run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_payload(tmp_path)))
    assert stop.main() == 0
    return stop, drains


def degraded(tmp_path: Path) -> list[str]:
    path = tmp_path / "plugin-data" / "degraded.jsonl"
    return [json.loads(line)["reason"] for line in path.read_text().splitlines()] if path.exists() else []


def test_a_new_harness_gets_one_batched_drain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ok = json.dumps({"ok": True, "result": {"state": "idle"}, "results": [{"state": "idle"}]})
    stop, drains = run_stop(monkeypatch, tmp_path, lambda argv: subprocess.CompletedProcess(argv, 0, ok, ""))
    assert len(drains) == 1 and drains[0][-4:] == stop.BATCH_DRAIN
    assert degraded(tmp_path) == []


def test_an_older_harness_is_retried_once_without_the_batch_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def old_binary(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "--max" in argv:
            return subprocess.CompletedProcess(argv, 2, "", ARGPARSE_REJECT)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": {"state": "idle"}}), "")

    _, drains = run_stop(monkeypatch, tmp_path, old_binary)
    assert len(drains) == 2 and "--max" in drains[0] and "--max" not in drains[1]
    assert drains[1] == drains[0][:-4]  # the exact one-row drain it always ran
    assert degraded(tmp_path) == []


def test_any_other_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The harness also exits 2 for delivery errors (stdout {"ok": false}); those are real failures.
    failed = json.dumps({"ok": False, "error": "delivery requires an initialized capture outbox"})
    _, drains = run_stop(monkeypatch, tmp_path, lambda argv: subprocess.CompletedProcess(argv, 2, failed, ""))
    assert len(drains) == 1
    assert degraded(tmp_path) == ["verified_delivery_failed:exit=2"]


def test_the_drain_timeout_covers_a_full_row_and_fits_the_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json as _json

    stop = load_stop(
        monkeypatch,
        tmp_path,
        **SETTINGS,
        **{OPTION + "DELIVERY_MODE": "verified"},
        MUSUBI_HARNESS_BIN="/opt/fake/musubi-harness",
        MUSUBI_MEMORY_DATA_BIN="/opt/fake/memory-data",
    )
    commands = stop.delivery_commands({"actor": "aoi", "zone": "home", "event_id": "claude-code:s:p"}, stop.runtime_config())
    (stage, stage_timeout, _), (drain, drain_timeout, _) = commands
    budget = float(drain[drain.index("--budget-seconds") + 1])
    per_call = float(drain[drain.index("--timeout") + 1])
    assert drain_timeout > budget + 4 * per_call
    hook = _json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]
    assert stage_timeout + drain_timeout < hook["timeout"]

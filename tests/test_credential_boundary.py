"""The Musubi token reaches only the process that talks to Musubi.

Claude Code exports the sensitive ``musubi_token`` setting to hooks as
``CLAUDE_PLUGIN_OPTION_MUSUBI_TOKEN``; the runtime maps it to ``MUSUBI_TOKEN``
and drops the option copy, so there is one holder. The Stop hook then gives
``enqueue`` and ``stage`` (local outbox writes) no credentials and only the
``drain`` the full environment. Malformed remote settings cannot block a local
capture, because nothing validates them before the drain's memory-data child.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_stop_envelope import _load_stop_module, _prompt_record, _write_transcript

OPTION = "CLAUDE_PLUGIN_OPTION_"
CREDENTIAL_KEYS = ("MUSUBI_API_URL", "MUSUBI_TOKEN", OPTION + "MUSUBI_TOKEN")


def load_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Any:
    for key in list(dict(__import__("os").environ)):
        if key.startswith(("MUSUBI_", OPTION)):
            monkeypatch.delenv(key)
    # Isolate HOME: with the real one, the state-root rule keeps a legacy
    # ~/.local/state/musubi-claude that has state, and a real harness run would
    # write into that live outbox. (It did, once, on the first run of this test.)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # The runtime maps settings at import, so it must be imported under this env.
    sys.modules.pop("musubi_claude_runtime", None)
    return _load_stop_module()


def hook_payload(tmp_path: Path) -> str:
    transcript = _write_transcript(tmp_path, [_prompt_record()])
    return json.dumps({"transcript_path": str(transcript), "session_id": "s-abc", "prompt_id": "p-123", "last_assistant_message": "world"})


SETTINGS = {
    OPTION + "ACTOR": "aoi",
    OPTION + "SEAT": "command-chair",
    OPTION + "ZONE": "home",
    OPTION + "MUSUBI_URL": "https://musubi.example",
    OPTION + "MUSUBI_TOKEN": "a.b.c",
}


def test_only_the_drain_receives_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = load_stop(
        monkeypatch,
        tmp_path,
        **SETTINGS,
        **{OPTION + "DELIVERY_MODE": "verified"},
        MUSUBI_HARNESS_BIN="/opt/fake/musubi-harness",
        MUSUBI_MEMORY_DATA_BIN="/opt/fake/memory-data",
    )
    seen: dict[str, dict[str, str]] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen[argv[3]] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(stop.subprocess, "run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_payload(tmp_path)))
    assert stop.main() == 0
    assert set(seen) == {"enqueue", "stage", "drain"}
    for local in ("enqueue", "stage"):
        assert seen[local], f"{local} must get an explicit env, not inherit the hook's"
        assert not set(CREDENTIAL_KEYS) & set(seen[local]), local
        assert seen[local]["FLEET_IDENTITY"] == "aoi"
    drain = seen["drain"]
    assert (drain["MUSUBI_API_URL"], drain["MUSUBI_TOKEN"]) == ("https://musubi.example", "a.b.c")
    assert OPTION + "MUSUBI_TOKEN" not in drain  # one holder only
    assert not (tmp_path / "plugin-data" / "degraded.jsonl").exists()


@pytest.mark.skipif(shutil.which("musubi-harness") is None, reason="needs the musubi-harness CLI")
def test_malformed_remote_settings_never_block_a_local_capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Yua's condition: bad remote settings must not refuse a shadow-mode capture.
    bad = {**SETTINGS, OPTION + "MUSUBI_URL": "::::not a url", OPTION + "MUSUBI_TOKEN": "bad\ntoken with space"}
    stop = load_stop(monkeypatch, tmp_path, **bad, **{OPTION + "DELIVERY_MODE": "shadow"})
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_payload(tmp_path)))
    assert stop.main() == 0
    root = tmp_path / "plugin-data"
    assert not (root / "degraded.jsonl").exists()
    with sqlite3.connect(root / "aoi" / "home" / "shadow.db") as conn:
        assert conn.execute("SELECT disposition FROM capture_events").fetchall() == [("shadow",)]

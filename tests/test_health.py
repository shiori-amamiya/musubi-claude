"""/musubi-claude:health reports local state honestly and never leaks content."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "scripts" / "musubi-claude-health"
SECRET = "the-secret-prompt-text-must-never-appear"


def seed_outbox(root: Path, *, captured: int, pending: int, verified: int, actor: str = "alice") -> None:
    db = root / actor / "home" / "shadow.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE capture_events (envelope_json TEXT, disposition TEXT, reason TEXT, enqueued_at TEXT)")
        conn.execute("CREATE TABLE delivery_events (event_id TEXT, state TEXT, object_id TEXT, attempt_count INT, content TEXT)")
        for _ in range(captured):
            conn.execute("INSERT INTO capture_events VALUES (?, 'shadow', NULL, 'now')", (json.dumps({"prompt": SECRET}),))
        for _ in range(pending):
            conn.execute("INSERT INTO delivery_events VALUES (NULL, 'pending', NULL, 0, ?)", (SECRET,))
        for _ in range(verified):
            conn.execute("INSERT INTO delivery_events VALUES (NULL, 'verified', 'ep-x', 1, ?)", (SECRET,))


def run_health(home: Path, plugin_data: Path | None, *args: str) -> tuple[dict, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(ROOT / "scripts"),
        "CLAUDE_PLUGIN_OPTION_ACTOR": "alice",
        "CLAUDE_PLUGIN_OPTION_SEAT": "laptop",
        "CLAUDE_PLUGIN_OPTION_ZONE": "home",
        "CLAUDE_PLUGIN_OPTION_DELIVERY_MODE": "verified",
    }
    if plugin_data is not None:
        env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
    done = subprocess.run([sys.executable, str(HEALTH), *args], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), done.stdout


def test_reports_settings_identity_and_queue_counts(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    seed_outbox(data, captured=3, pending=2, verified=5)
    report, _ = run_health(tmp_path, data)
    assert report["settings_source"] == "settings"
    assert report["state_root_source"] == "claude_plugin_data"
    assert report["identity"] == {
        "ok": True,
        "actor": "alice",
        "presence": "alice/laptop",
        "zone": "home",
        "delivery_mode": "verified",
    }
    assert report["outbox"]["capture"] == {"shadow": 3}
    assert report["outbox"]["delivery"] == {"pending": 2, "verified": 5}


def test_never_prints_captured_or_delivered_content(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    seed_outbox(data, captured=2, pending=1, verified=1)
    _, raw = run_health(tmp_path, data)
    assert SECRET not in raw


def test_undelivered_rows_in_the_root_not_in_use_are_called_out(tmp_path: Path) -> None:
    legacy = tmp_path / ".local" / "state" / "musubi-claude"
    seed_outbox(legacy, captured=0, pending=4, verified=1)
    data = tmp_path / "plugin-data"
    seed_outbox(data, captured=1, pending=0, verified=1)
    report, _ = run_health(tmp_path, data)
    assert report["state_root_source"] == "claude_plugin_data"
    assert report["other_root"]["undelivered"] == 4


def test_degraded_reasons_are_summarised_without_detail(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    data.mkdir()
    rows = [
        {"at": "2026-09-26T10:00:00Z", "reason": "prompt_id_ambiguous"},
        {"at": "2026-09-26T11:00:00Z", "reason": "prompt_id_ambiguous"},
        {"at": "2026-09-26T12:00:00Z", "reason": "harness_unavailable:stop"},
    ]
    (data / "degraded.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    report, _ = run_health(tmp_path, data)
    degraded = report["degraded"]["state_root"]
    assert degraded["top_reasons"] == {"prompt_id_ambiguous": 2, "harness_unavailable": 1}
    assert degraded["latest_at"] == "2026-09-26T12:00:00Z"
    assert degraded["lines_total"] == 4


def test_missing_identity_is_reported_not_raised(tmp_path: Path) -> None:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": str(ROOT / "scripts")}
    done = subprocess.run([sys.executable, str(HEALTH)], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0
    report = json.loads(done.stdout)
    assert report["identity"]["ok"] is False
    assert "outbox" not in report


def test_health_reads_without_creating_anything(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    run_health(tmp_path, data)
    assert not data.exists()


def test_launcher_reports_not_set_up_as_data(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_PLUGIN_DATA": str(tmp_path / "pd")}
    done = subprocess.run(
        ["sh", str(ROOT / "bin" / "musubi-claude-health")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert done.returncode == 0
    report = json.loads(done.stdout)
    assert (report["setup"], report["error"], report["fix"]) == (False, "harness_unavailable", "run /musubi-claude:setup")
    # A read-only question is not a failed capture: nothing is recorded.
    assert not (tmp_path / "pd" / "degraded.jsonl").exists()


def test_bin_wrapper_passes_plugin_data_through(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run(
        ["sh", str(ROOT / "bin" / "musubi-claude-health"), "--plugin-data", str(tmp_path / "explicit")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert done.returncode == 0
    # Not set up in the explicit dir either, but it must not have looked at the
    # legacy default instead: the launcher only reports, it does not write.
    assert json.loads(done.stdout)["setup"] is False
    assert not (tmp_path / ".local").exists()


def test_free_text_arguments_never_abort_the_skill(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    report, _ = run_health(tmp_path, data, "probe", "please", "--verbose")
    assert report["ignored_args"] == ["please", "--verbose"]
    assert "provider" in report or report["identity"]["ok"] is True


def test_empty_plugin_data_is_reported_not_guessed(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run(
        ["sh", str(ROOT / "bin" / "musubi-claude-health"), "--plugin-data", ""],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["error"] == "plugin_data_unknown"


def test_setup_refuses_without_a_plugin_data_dir(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for args in (["--plugin-data", ""], []):
        done = subprocess.run(
            ["sh", str(ROOT / "bin" / "musubi-claude-setup"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert done.returncode == 2, args
        assert "refusing" in done.stderr
    # Nothing was installed anywhere, in particular not in the legacy root.
    assert not (tmp_path / ".local").exists()


def test_one_event_is_reported_by_exact_id_without_content(tmp_path: Path) -> None:
    data = tmp_path / "plugin-data"
    seed_outbox(data, captured=0, pending=0, verified=0)
    db = data / "alice" / "home" / "shadow.db"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO delivery_events VALUES ('evt-1', 'verified', 'ep-9', 2, ?)", (SECRET,))
        conn.execute("INSERT INTO delivery_events VALUES ('evt-2', 'pending', NULL, 0, ?)", (SECRET,))
    report, raw = run_health(tmp_path, data, "--event", "evt-1")
    assert report["event"] == {"event_id": "evt-1", "found": True, "state": "verified", "object_id": "ep-9", "attempts": 2}
    assert SECRET not in raw
    report, _ = run_health(tmp_path, data, "--event", "evt-404")
    assert report["event"] == {"event_id": "evt-404", "found": False}

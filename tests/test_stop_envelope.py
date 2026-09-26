"""Tests for the Stop-hook envelope projection.

These tests exercise `scripts.musubi-claude-stop` directly, which is the
piece of Claude-specific logic the harness doesn't know about. The harness
validates the envelope once it arrives; this adapter is responsible for
*producing* one from a Claude Stop event + transcript.

The tests here are intentionally focused on the things that have caused
production defects:
- Event id is bound to the event's own identity (`claude-code:<session>:<prompt>`),
  so retries dedupe.
- The Stop event's `last_assistant_message` is authoritative — never
  reconstructed from the transcript.
- `prompt_id_ambiguous` is refused, not silently collapsed.
- `isMeta` records fall back only when no non-meta sibling exists.
- The hook degrades visibly and exits 0 on every failure.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Locate the stop-hook script in the plugin's scripts/ directory. It's a
# standalone .py file with a shebang, not importable as a normal module.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STOP_SCRIPT = PLUGIN_ROOT / "scripts" / "musubi-claude-stop"


def _load_stop_module() -> Any:
    """Import the stop script as `scripts.musubi_claude_stop`.

    The script has a shebang (`#!/usr/bin/env python3`) at the top.
    `importlib.util.spec_from_file_location` returns None for files
    starting with a shebang, so we strip the first line before loading.

    The script imports its sibling `musubi_claude_runtime` from the
    same directory; in production that's resolved because Claude Code
    invokes the script with `scripts/` on `sys.path`. We replicate that
    here by inserting the directory into `sys.path` before exec.
    """
    text = STOP_SCRIPT.read_text(encoding="utf-8")
    if text.startswith("#!"):
        text = text.split("\n", 1)[1]
    scripts_dir = str(STOP_SCRIPT.parent)
    sys.path.insert(0, scripts_dir)
    try:
        spec = importlib.util.spec_from_loader(
            "scripts.musubi_claude_stop",
            loader=None,
            origin=str(STOP_SCRIPT),
        )
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        exec(compile(text, str(STOP_SCRIPT), "exec"), module.__dict__)
    finally:
        if sys.path and sys.path[0] == scripts_dir:
            sys.path.pop(0)
    return module


@pytest.fixture
def stop_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Load the stop script with a fake identity and a per-test plugin-data dir."""
    monkeypatch.setenv("MUSUBI_ACTOR", "aoi")
    monkeypatch.setenv("MUSUBI_PRESENCE", "aoi/command-chair")
    monkeypatch.setenv("MUSUBI_ZONE", "home")
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    return _load_stop_module()


def _write_transcript(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    """Write records as a JSONL transcript and return the path."""
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def _prompt_record(
    *,
    prompt_id: str = "p-123",
    session_id: str = "s-abc",
    text: str = "hello",
    is_meta: bool = False,
) -> dict[str, Any]:
    return {
        "type": "user",
        "isMeta": is_meta,
        "promptId": prompt_id,
        "sessionId": session_id,
        "message": {"role": "user", "content": text},
    }


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    """A single-record transcript that pairs with the event defaults below."""
    return _write_transcript(tmp_path, [_prompt_record()])


@pytest.fixture
def event() -> dict[str, Any]:
    """The Stop-event payload that pairs with the `transcript` fixture."""
    return {
        "transcript_path": "",  # filled in by the test
        "session_id": "s-abc",
        "prompt_id": "p-123",
        "last_assistant_message": "world",
    }


# ---------------------------------------------------------------------------
# Event-id shape and aliasing
# ---------------------------------------------------------------------------


def test_event_id_shape(stop_module: Any, transcript: Path, event: dict[str, Any]) -> None:
    """The event id is `claude-code:<session>:<prompt>` — stable across retries."""
    event["transcript_path"] = str(transcript)
    envelope = stop_module.build_envelope(event)
    assert envelope["event_id"] == "claude-code:s-abc:p-123"


def test_event_id_uses_camelcase_aliases(stop_module: Any, transcript: Path) -> None:
    """Aliases (promptId, lastAssistantMessage) are accepted alongside snake_case."""
    envelope = stop_module.build_envelope(
        {
            "transcript_path": str(transcript),
            "session_id": "s-abc",
            "promptId": "p-123",
            "lastAssistantMessage": "world",
        }
    )
    assert envelope["event_id"] == "claude-code:s-abc:p-123"


def test_alias_conflict_refused(stop_module: Any) -> None:
    """If snake_case and camelCase disagree on the prompt id, refuse closed.

    Taking the first alias that happened to be present would let a
    payload carrying both `prompt_id` and `promptId` with DIFFERENT
    values decide event identity by key order — the object would be
    stored under one id while the turn it describes belongs to another.
    """
    with pytest.raises(stop_module.AdapterError, match="prompt_id_conflict"):
        stop_module.build_envelope(
            {
                "transcript_path": "/nonexistent",
                "session_id": "s-abc",
                "prompt_id": "p-123",
                "promptId": "p-different",
                "last_assistant_message": "world",
            }
        )


def test_missing_prompt_id_refused(stop_module: Any) -> None:
    """Without a prompt_id, no envelope is constructed."""
    with pytest.raises(stop_module.AdapterError, match="hook_prompt_id_missing"):
        stop_module.build_envelope(
            {
                "transcript_path": "/nonexistent",
                "session_id": "s-abc",
                "last_assistant_message": "world",
            }
        )


def test_missing_session_id_refused(stop_module: Any) -> None:
    """Without a session_id, no envelope is constructed."""
    with pytest.raises(stop_module.AdapterError, match="hook_session_missing"):
        stop_module.build_envelope(
            {
                "transcript_path": "/nonexistent",
                "prompt_id": "p-123",
                "last_assistant_message": "world",
            }
        )


def test_missing_answer_refused(stop_module: Any) -> None:
    """Without a last_assistant_message, no envelope is constructed."""
    with pytest.raises(stop_module.AdapterError, match="hook_answer_missing"):
        stop_module.build_envelope(
            {
                "transcript_path": "/nonexistent",
                "session_id": "s-abc",
                "prompt_id": "p-123",
            }
        )


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


def test_envelope_projects_assistant_text_verbatim(stop_module: Any, transcript: Path, event: dict[str, Any]) -> None:
    """The Stop event's assistant_text is stored verbatim — no normalising."""
    raw = "  Hello, **world**!\n```\ncode block\n```\n"
    event["transcript_path"] = str(transcript)
    event["last_assistant_message"] = raw
    envelope = stop_module.build_envelope(event)
    assert envelope["assistant_text"] == raw


def test_envelope_carries_event_identity(stop_module: Any, transcript: Path, event: dict[str, Any]) -> None:
    """The envelope's identity keys come from the runtime, not the event."""
    event["transcript_path"] = str(transcript)
    envelope = stop_module.build_envelope(event)
    assert envelope["actor"] == "aoi"
    assert envelope["presence"] == "aoi/command-chair"
    assert envelope["zone"] == "home"
    assert envelope["source"] == "claude-code"
    assert envelope["plane"] == "episodic"


# ---------------------------------------------------------------------------
# Transcript lookup
# ---------------------------------------------------------------------------


def test_ismeta_only_prompt_falls_back(stop_module: Any, tmp_path: Path) -> None:
    """When the only record under a promptId is `isMeta: true`, accept it.

    This recovers native cross-session messages, which Claude Code
    delivers as a single `isMeta=true` user record with no non-meta
    sibling. (See the long comment in `find_prompt_turn`.)
    """
    path = _write_transcript(
        tmp_path,
        [
            _prompt_record(prompt_id="p-bridge", text="Cross-session message", is_meta=True),
        ],
    )
    turn = stop_module.find_prompt_turn(path, "p-bridge")
    assert turn["user_text"] == "Cross-session message"
    assert turn["session_id"] == "s-abc"


def test_prompt_id_ambiguous_refused(stop_module: Any, tmp_path: Path) -> None:
    """Two records under the same promptId with different content refuse closed."""
    path = _write_transcript(
        tmp_path,
        [
            _prompt_record(text="first"),
            _prompt_record(text="second"),
        ],
    )
    with pytest.raises(stop_module.AdapterError, match="prompt_id_ambiguous"):
        stop_module.find_prompt_turn(path, "p-123")


def test_tool_result_user_record_excluded(stop_module: Any, tmp_path: Path) -> None:
    """A type=user record carrying a tool_result is not a prompt carrier.

    Such a record leaves the prompt lookup with no carrier, so the
    failure surfaces as `prompt_not_found_in_transcript`. The carrier
    test is its own: if a real prompt had a sibling tool_result under
    the same promptId, the carrier would still resolve because the
    record with the actual user text would also be present.
    """
    path = tmp_path / "transcript.jsonl"
    record = {
        "type": "user",
        "isMeta": False,
        "promptId": "p-tr",
        "sessionId": "s-abc",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": "tool output"}],
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(stop_module.AdapterError, match="prompt_not_found_in_transcript"):
        stop_module.find_prompt_turn(path, "p-tr")


def test_corrupt_line_skipped_not_bound(stop_module: Any, tmp_path: Path) -> None:
    """A corrupt line cannot mis-bind a prompt to the wrong turn.

    Selection is by prompt_id, not position — a span we cannot read
    simply cannot match.
    """
    path = tmp_path / "transcript.jsonl"
    text = "this is not valid json\n" + json.dumps(_prompt_record()) + "\n"
    path.write_text(text, encoding="utf-8")

    turn = stop_module.find_prompt_turn(path, "p-123")
    assert turn["user_text"] == "hello"


def test_transcript_session_mismatch_refused(stop_module: Any, tmp_path: Path) -> None:
    """A prompt record with a sessionId different from the event's session_id is refused."""
    path = _write_transcript(
        tmp_path,
        [
            _prompt_record(session_id="s-DIFFERENT"),
        ],
    )
    with pytest.raises(stop_module.AdapterError, match="hook_session_transcript_mismatch"):
        stop_module.build_envelope(
            {
                "transcript_path": str(path),
                "session_id": "s-event",
                "prompt_id": "p-123",
                "last_assistant_message": "world",
            }
        )


# ---------------------------------------------------------------------------
# Failure-path invariants
# ---------------------------------------------------------------------------


def test_degraded_record_has_no_prose(stop_module: Any) -> None:
    """The degraded sink holds only structured failure codes — never prose.

    Verified via the scope helper, which is the only thing that can
    leak conversation content through the diagnostic sink. Its output
    must contain exactly: at, reason, seat (and optionally session_id).
    """
    scope = stop_module._degraded_scope({"session_id": "s-abc"})
    assert set(scope.keys()).issubset({"seat", "session_id"})
    assert scope.get("session_id") == "s-abc"


def test_degraded_scope_handles_non_dict(stop_module: Any) -> None:
    """The helper must not raise even when the hook payload is malformed.

    Seat comes from the runtime config (env or file), not from the
    hook payload, so it's correctly populated even when the payload
    itself is unusable. Session id, however, comes from the hook and
    must be absent in this case.
    """
    scope = stop_module._degraded_scope("not a dict")
    assert scope.get("seat") == "aoi"
    assert "session_id" not in scope


def test_degraded_scope_handles_missing_session(stop_module: Any) -> None:
    """A hook payload with no session_id leaves the session_id key absent."""
    scope = stop_module._degraded_scope({})
    assert "session_id" not in scope

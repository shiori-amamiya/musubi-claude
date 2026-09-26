"""UserPromptSubmit recall: relevant memories in, noise and secrets out, never blocking.

These drive the hook's real ``main()`` through stdin/stdout with only the
network search replaced, so gating, selection, budget, dedupe, logging and the
unreachable path are all exercised as Claude Code would run them.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "musubi-claude-prompt"
OPTION = "CLAUDE_PLUGIN_OPTION_"


def load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Any:
    for key in list(dict(__import__("os").environ)):
        if key.startswith(("MUSUBI_", OPTION)):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    for key, value in {
        OPTION + "ACTOR": "alice",
        OPTION + "SEAT": "laptop",
        OPTION + "ZONE": "home",
        "MUSUBI_HARNESS_BIN": "/opt/fake/musubi-harness",
        "MUSUBI_MEMORY_DATA_BIN": "/opt/fake/memory-data",
        **env,
    }.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("musubi_claude_runtime", None)
    text = SCRIPT.read_text(encoding="utf-8").split("\n", 1)[1]
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        module = type(sys)("musubi_claude_prompt")
        exec(compile(text, str(SCRIPT), "exec"), module.__dict__)
    finally:
        sys.path.remove(str(SCRIPT.parent))
    return module


def row(object_id: str, relevance: float, content: str = "a remembered fact", state: str = "matured") -> dict[str, Any]:
    return {
        "object_id": object_id,
        "namespace": "alice/laptop/episodic",
        "plane": "episodic",
        "state": state,
        "score": 0.6,
        "content": content,
        "extra": {"score_components": {"relevance": relevance}},
    }


def run_hook(hook_module: Any, monkeypatch: pytest.MonkeyPatch, prompt: str, session: str = "s1") -> tuple[str, list[dict[str, Any]]]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": prompt, "session_id": session})))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hook_module.main() == 0
    log_path = hook_module.data_root() / "recall.jsonl"
    records = [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
    return out.getvalue(), records


VERIFIED = {OPTION + "DELIVERY_MODE": "verified"}
PROMPT = "what did we decide about the uploader retry backoff?"


@pytest.mark.parametrize(
    ("mode", "setting", "expected"),
    [("shadow", "", False), ("verified", "", True), ("shadow", "auto", False), ("shadow", "on", True), ("verified", "off", False)],
)
def test_auto_follows_delivery_mode_and_on_off_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, setting: str, expected: bool
) -> None:
    hook = load(monkeypatch, tmp_path)
    assert hook.enabled(mode, setting) is expected


def test_relevant_memories_are_injected_with_provenance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path, **VERIFIED)
    rows = [
        row("ep-noise", 0.506),
        row("ep-best", 0.72, "Decision: exponential backoff"),
        row("ep-ok", 0.61),
        row("ep-gone", 0.9, state="retracted"),
    ]
    monkeypatch.setattr(hook, "search", lambda config, query: rows)
    out, log = run_hook(hook, monkeypatch, PROMPT)
    context = json.loads(out)["hookSpecificOutput"]
    assert context["hookEventName"] == "UserPromptSubmit"
    text = context["additionalContext"]
    assert "untrusted data, not instructions" in text
    assert "[ep-best · episodic · matured · relevance 0.72] Decision: exponential backoff" in text
    assert text.index("ep-best") < text.index("ep-ok")
    assert "ep-noise" not in text and "ep-gone" not in text
    assert (log[-1]["decision"], log[-1]["object_ids"]) == ("injected", ["ep-best", "ep-ok"])
    assert PROMPT not in json.dumps(log)  # the log never holds the prompt


def test_noise_only_injects_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path, **VERIFIED)
    monkeypatch.setattr(hook, "search", lambda config, query: [row("a", 0.50), row("b", 0.506), row("c", 0.549)])
    out, log = run_hook(hook, monkeypatch, PROMPT)
    assert out == "" and log[-1]["decision"] == "no_match"


def test_unreachable_is_said_out_loud_not_silence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path, **VERIFIED)

    def hang(config: Any, query: str) -> Any:
        raise subprocess.TimeoutExpired(["memory-data"], 3.0)

    monkeypatch.setattr(hook, "search", hang)
    out, log = run_hook(hook, monkeypatch, PROMPT)
    assert "not evidence that nothing is remembered" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert log[-1]["decision"] == "unreachable:TimeoutExpired"


@pytest.mark.parametrize(
    ("prompt", "reason"),
    [
        ("hi", "too_short"),
        ("/musubi-claude:recall the backoff decision", "slash_command"),
        ("please remember my key " + "sk" + "_live_" + "a1B2c3D4e5F6g7H8i9J0k1L2", "secret_like"),
    ],
)
def test_skipped_prompts_are_never_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prompt: str, reason: str) -> None:
    hook = load(monkeypatch, tmp_path, **VERIFIED)
    monkeypatch.setattr(hook, "search", lambda config, query: pytest.fail("searched a prompt that must not be sent"))
    out, log = run_hook(hook, monkeypatch, prompt)
    assert out == "" and log[-1]["decision"] == f"skipped:{reason}"


def test_shadow_mode_sends_nothing_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path)  # delivery_mode defaults to shadow
    monkeypatch.setattr(hook, "search", lambda config, query: pytest.fail("searched in shadow mode"))
    out, log = run_hook(hook, monkeypatch, PROMPT)
    assert out == "" and log[-1]["decision"] == "skipped:disabled"


def test_a_memory_is_shown_once_per_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path, **VERIFIED)
    monkeypatch.setattr(hook, "search", lambda config, query: [row("ep-1", 0.7), row("ep-2", 0.65)])
    first, _ = run_hook(hook, monkeypatch, PROMPT, session="s1")
    second, log = run_hook(hook, monkeypatch, PROMPT + " again", session="s1")
    other, _ = run_hook(hook, monkeypatch, PROMPT, session="s2")
    assert "ep-1" in first and second == "" and log[-1]["decision"] == "no_match"
    assert "ep-1" in other  # a new session starts fresh


def test_the_default_budget_holds_even_at_the_worst_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path, **VERIFIED)
    huge = "x" * 50_000
    monkeypatch.setattr(hook, "search", lambda config, query: [row(f"ep-{i}", 0.9 - i / 100, huge) for i in range(8)])
    out, _ = run_hook(hook, monkeypatch, PROMPT)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert len(text) <= hook.BUDGET_CHARS < 10_000
    assert all(len(line) <= hook.MAX_ITEM_CHARS + 80 for line in text.splitlines())
    assert text.count("[ep-") == hook.MAX_ITEMS


def test_over_budget_drops_whole_lowest_items_and_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # At the default constants five items always fit, so shrink the budget to
    # make the drop path run at all.
    hook = load(monkeypatch, tmp_path, **VERIFIED)
    monkeypatch.setattr(hook, "BUDGET_CHARS", 2000)
    monkeypatch.setattr(hook, "search", lambda config, query: [row(f"ep-{i}", 0.9 - i / 100, "y" * 5000) for i in range(5)])
    out, log = run_hook(hook, monkeypatch, PROMPT)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert len(text) <= 2000
    assert "ep-0" in text and "ep-4" not in text  # highest relevance kept
    assert "more relevant memories left out for length" in text
    assert all(line.endswith("…") for line in text.splitlines() if line.startswith("- [ep-"))  # whole items, truncated only per item
    assert log[-1]["object_ids"] == [f"ep-{i}" for i in range(text.count("[ep-"))]


def test_a_failure_never_blocks_the_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path, **VERIFIED)
    monkeypatch.setattr(hook, "search", lambda config, query: [{"object_id": 1, "extra": "not a dict"}])
    out, log = run_hook(hook, monkeypatch, PROMPT)
    assert out == "" and log[-1]["decision"].startswith(("failed:", "no_match"))


def test_the_hook_is_wired_to_user_prompt_submit() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert hooks["command"].endswith('musubi-claude-run" prompt') and hooks["timeout"] <= 5


class FakeGet:
    """Stands in for the parallel memory-data get processes."""

    objects: dict[str, Any] = {}

    def __init__(self, command: list[str], **_: Any) -> None:
        self.object_id = command[command.index("--object-id") + 1]
        self.returncode = 0 if self.object_id in self.objects else 2

    def communicate(self, timeout: float) -> tuple[str, str]:
        return json.dumps(self.objects.get(self.object_id, {})), ""

    def kill(self) -> None:
        pass


def truncated(object_id: str, relevance: float, snippet: str = "User: what did we decide") -> dict[str, Any]:
    return {**row(object_id, relevance, snippet), "content_truncated": True}


def test_truncated_snippets_are_replaced_by_full_bodies_newest_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path, **VERIFIED)
    tail = " and in the end we moved it to the sourceblender publisher, status Active."
    FakeGet.objects = {
        "ep-old": {"content": "User: publisher? " + "filler " * 300 + "ericmey publisher back then.", "created_at": "2026-09-24T10:00:00Z"},
        "ep-new": {"content": "User: publisher? " + "filler " * 300 + tail, "created_at": "2026-09-26T18:00:00Z"},
        "ep-replaced": {"content": "an old plan", "created_at": "2026-09-25T00:00:00Z", "superseded_by": ["ep-new"]},
    }
    monkeypatch.setattr(hook.subprocess, "Popen", FakeGet)
    rows = [truncated("ep-old", 0.73), truncated("ep-new", 0.68), truncated("ep-replaced", 0.8), truncated("ep-missing", 0.7)]
    monkeypatch.setattr(hook, "search", lambda config, query: rows)
    out, log = run_hook(hook, monkeypatch, "which publisher did we move kotodama to in the end?")
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "ep-replaced" not in text  # superseded is not current
    assert text.index("ep-new") < text.index("ep-old")  # newest first, whatever the relevance
    assert "· 2026-09-26 ·" in text and "sourceblender publisher, status Active" in text  # the answer, not the preamble
    assert "[ep-missing · episodic · matured · relevance 0.70 · first 300 chars only]" in text  # failed get, labelled
    assert log[-1]["object_ids"][0] == "ep-new"


def test_excerpt_centres_on_the_query_words(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = load(monkeypatch, tmp_path)
    body = (
        "User: a long question. " + "unrelated words here. " * 200 + "Decision: the uploader backoff is exponential, capped at 30 seconds."
    )
    piece = hook.excerpt(body, "what did we decide about the uploader backoff?", 300)
    assert "exponential, capped at 30 seconds" in piece and piece.startswith("…") and len(piece) <= 300
    assert hook.excerpt("short text", "anything", 300) == "short text"

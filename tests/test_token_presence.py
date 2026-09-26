"""SessionStart names a Musubi token that belongs to another seat or cannot write this one.

Aoi's 0.5.0 canary ran with another seat's token (sub aoi/voice, scope
"aoi/voice:r aoi/voice/*:rw **:r"): reads worked and every delivery 403'd at
the drain, silently. The claims decode locally, so SessionStart can say so on
the first turn. The token itself is never printed.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "musubi-claude-session-start"
BLOCK = "## Musubi continuity\n(stub)"


def jwt(claims: dict[str, Any]) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str | None) -> Any:
    for key in list(dict(__import__("os").environ)):
        if key.startswith(("MUSUBI_", "CLAUDE_PLUGIN_OPTION_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("MUSUBI_ACTOR", "aoi")
    monkeypatch.setenv("MUSUBI_PRESENCE", "aoi/command-chair")
    monkeypatch.setenv("MUSUBI_ZONE", "home")
    if token is not None:
        monkeypatch.setenv("MUSUBI_TOKEN", token)
    sys.modules.pop("musubi_claude_runtime", None)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        module = type(sys)("musubi_claude_session_start")
        exec(compile(SCRIPT.read_text().split("\n", 1)[1], str(SCRIPT), "exec"), module.__dict__)
    finally:
        sys.path.remove(str(SCRIPT.parent))
    module.continuity_block = lambda: BLOCK
    return module


def run_main(module: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert module.main() == 0
    return out.getvalue()


RIGHT = jwt({"sub": "aoi/command-chair", "scope": "aoi/command-chair/*:rw"})
VOICE = jwt({"sub": "aoi/voice", "scope": "aoi/voice:r aoi/voice/*:rw **:r"})  # the canary's actual token shape


def test_the_right_token_changes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = load(monkeypatch, tmp_path, RIGHT)
    assert module.token_warning() is None
    assert run_main(module, monkeypatch) == BLOCK + "\n"  # byte-identical to before


def test_another_seats_token_is_named_on_the_first_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = load(monkeypatch, tmp_path, VOICE)
    output = json.loads(run_main(module, monkeypatch))
    message = output["systemMessage"]
    assert "for aoi/voice, but this seat is aoi/command-chair" in message
    assert "cannot write aoi/command-chair/episodic" in message
    assert output["hookSpecificOutput"] == {"hookEventName": "SessionStart", "additionalContext": BLOCK}
    assert VOICE not in json.dumps(output) and "sig" not in message  # never the token


@pytest.mark.parametrize(
    ("scope", "writable"),
    [
        ("aoi/command-chair/*:rw", True),
        ("**:rw", True),
        (["aoi/command-chair/*:rw"], True),
        ("aoi/command-chair/**:w", True),
        ("**:r", False),
        ("aoi/command-chair:rw", False),  # the presence itself, not its episodic plane
        ("aoi/*:rw", False),  # one segment does not reach aoi/command-chair/episodic
        ("aoi/**:rw", True),
    ],
)
def test_write_scope_matching(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: Any, writable: bool) -> None:
    module = load(monkeypatch, tmp_path, None)
    assert module._grants_write(scope, "aoi/command-chair/episodic") is writable


@pytest.mark.parametrize("token", [None, "not-a-jwt", "a.b.c"], ids=["absent", "opaque", "undecodable"])
def test_no_token_or_an_unreadable_one_is_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str | None) -> None:
    module = load(monkeypatch, tmp_path, token)
    assert module.token_warning() is None
    assert run_main(module, monkeypatch) == BLOCK + "\n"

"""Our write-scope check agrees with Musubi's own matcher, pair for pair.

The server's functions (musubi/auth/scopes.py) are loaded from a Musubi source
checkout, so this runs only when pointed at one:

    MUSUBI_SOURCE_DIR=~/Projects/musubi pytest tests/test_scope_parity.py

A disagreement in the permissive direction is the failure SessionStart exists
to prevent: a token called fine that then 403s at delivery.
"""

from __future__ import annotations

import itertools
import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.test_token_presence import load

SOURCE = Path(os.environ.get("MUSUBI_SOURCE_DIR", "")).expanduser() / "src" / "musubi" / "auth" / "scopes.py"
pytestmark = pytest.mark.skipif(not SOURCE.is_file(), reason="set MUSUBI_SOURCE_DIR to a Musubi checkout to run scope parity")


def server_allows() -> Any:
    text = SOURCE.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    for name in ("_parse_namespace_scope", "_namespace_matches", "_access_allows", "_namespace_scope_allows"):
        match = re.search(rf"^def {name}\(.*?(?=^def |\Z)", text, re.S | re.M)
        assert match, f"{name} not found in {SOURCE}"
        exec(match.group(0).replace("AccessLevel", "str"), namespace)
    return namespace["_namespace_scope_allows"]


def test_grants_write_matches_the_server_on_every_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    allows = server_allows()
    mine = load(monkeypatch, tmp_path, None)._grants_write
    parts = ["aoi", "command-chair", "episodic", "*", "**", "voice", "x"]
    patterns = {"/".join(p) for n in (1, 2, 3, 4) for p in itertools.product(parts, repeat=n)}
    namespaces = ["aoi/command-chair/episodic", "aoi/voice/episodic", "a/b", "aoi/command-chair/curated"]
    mismatches = [
        (f"{pattern}:{access}", ns)
        for pattern in patterns
        for access in ("r", "w", "rw", "rwx", "")
        for ns in namespaces
        if allows(f"{pattern}:{access}", ns, "w") != mine(f"{pattern}:{access}", ns)
    ]
    assert mismatches == []

from pathlib import Path

import pytest

from tests.integration.architecture.static_checks import SRC_ROOT


@pytest.mark.parametrize(
    "module",
    ("claude_json.py", "claude_json_parser.py", "json_number.py"),
)
def test_claude_json_modules_remain_reviewable(module: str) -> None:
    path: Path = SRC_ROOT / "crewplane/adapters/invokers/cli_invoker" / module
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 500

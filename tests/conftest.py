import pytest


@pytest.fixture(autouse=True)
def fixed_terminal_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")

from __future__ import annotations

from collections.abc import Iterator
from types import ModuleType, SimpleNamespace

import pytest

from scripts.release import retry
from tests.unit.packaging.release_tool_support import loaded_release_script, no_op


@pytest.fixture(autouse=True)
def no_release_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "time", SimpleNamespace(sleep=no_op))


@pytest.fixture
def release_script() -> Iterator[ModuleType]:
    with loaded_release_script() as module:
        yield module

from __future__ import annotations

from collections.abc import Iterator
from types import ModuleType

import pytest

from tests.unit.packaging.release_tool_support import loaded_release_script


@pytest.fixture
def release_script() -> Iterator[ModuleType]:
    with loaded_release_script() as module:
        yield module

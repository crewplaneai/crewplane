from __future__ import annotations

from typing import cast

import pytest

from tests.integration.observability.render.case_fixtures import (
    STATUS_CASES,
    TOPOLOGY_CASES,
)
from tests.integration.observability.render.case_types import CaseData


def _case_id(case_data: CaseData) -> str:
    return str(case_data["case_id"])


@pytest.fixture(params=TOPOLOGY_CASES, ids=_case_id)
def dag_render_topology_case(request: pytest.FixtureRequest) -> CaseData:
    return cast(CaseData, request.param)


@pytest.fixture(params=STATUS_CASES, ids=_case_id)
def dag_render_status_case(request: pytest.FixtureRequest) -> CaseData:
    return cast(CaseData, request.param)

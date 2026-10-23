from unittest.mock import patch

import pytest

from crewplane.cli.workspace_cleanup import evidence_claims
from tests.helpers.platforms import requires_workspace_support
from tests.unit.cli.workspace_cleanup_evidence_support import (
    RUN_KEY,
    snapshot_cache_path,
    snapshot_claim_payload,
    write_cleanup_claim,
)

pytestmark = requires_workspace_support


class ClaimGeneration(int):
    pass


@pytest.mark.parametrize(
    ("generation", "expected"),
    [
        (None, None),
        (True, None),
        (False, None),
        (-1, -1),
        (0, 0),
        (1, 1),
        (2**128, 2**128),
        (1.5, None),
        ("7", None),
        (ClaimGeneration(7), 7),
    ],
)
def test_claim_loading_normalizes_generation_without_integer_coercion(
    tmp_path, generation, expected
):
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    payload = snapshot_claim_payload(workspace_path)
    payload["workspace"]["reuse_generation"] = generation
    state_path = write_cleanup_claim(stage_root, RUN_KEY, payload)
    loaded_plan = evidence_claims.load_authoritative_plan(stage_root / RUN_KEY, RUN_KEY)
    assert loaded_plan.plan is not None

    with patch.object(evidence_claims.json, "loads", return_value=payload):
        loaded = evidence_claims.load_workspace_claim(
            state_path, RUN_KEY, "build", loaded_plan.plan, cache_root
        )

    assert loaded.invalid is False
    assert loaded.claim is not None
    assert loaded.claim.generation == expected
    if expected is not None:
        assert loaded.claim.generation is generation

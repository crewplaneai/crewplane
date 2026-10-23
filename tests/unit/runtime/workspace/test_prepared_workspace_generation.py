import json
from unittest.mock import patch

import pytest

from crewplane.runtime.workspace import prepared_workspace
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from tests.helpers.workspace_service import workspace_invocation_context


class ReuseGeneration(int):
    pass


@pytest.mark.parametrize("generation", [-1, 0, 1, 2**128, ReuseGeneration(7)])
def test_reuse_generation_preserves_all_integer_values(tmp_path, generation):
    prepared = PreparedWorkspace(tmp_path, workspace_invocation_context())
    with patch.object(
        prepared_workspace,
        "read_workspace_state",
        return_value={"workspace": {"reuse_generation": generation}},
    ):
        assert prepared._reuse_generation(tmp_path / "state.json") is generation


@pytest.mark.parametrize("generation", [None, True, False, 1.5, "7"])
def test_reuse_generation_rejects_invalid_persisted_values(tmp_path, generation):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"workspace": {"reuse_generation": generation}}))
    prepared = PreparedWorkspace(tmp_path, workspace_invocation_context())

    with pytest.raises(RuntimeError) as caught:
        prepared._reuse_generation(state_path)

    assert str(caught.value) == "Reusable workspace lacks a valid reuse generation."

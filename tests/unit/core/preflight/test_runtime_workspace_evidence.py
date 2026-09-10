from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from crewplane.core.preflight.runtime_config.workspace import (
    invoker_workspace_descriptor,
    requires_controlled_child_environment,
)
from crewplane.runtime.workspace.invocation import controlled_child_environment_required


class Snapshot(BaseModel):
    invoker: dict[str, object]


@pytest.mark.parametrize(
    ("implementation", "launch_mode", "controlled"),
    [("cli", "runtime_command_runner", True), ("mock", "mock_no_child_process", False)],
)
def test_workspace_evidence_preserves_model_and_mapping_projections(
    implementation,
    launch_mode,
    controlled,
) -> None:
    invoker = {
        "implementation": implementation,
        "capabilities": {
            "workspace": {
                "supported": True,
                "honors_cwd": True,
                "launch_mode": launch_mode,
                "controlled_child_environment": controlled,
            }
        },
    }
    expected = {
        "implementation": implementation,
        "honors_cwd": True,
        "launch_mode": launch_mode,
        "controlled_child_environment": controlled,
    }
    assert invoker_workspace_descriptor({"invoker": invoker}) == expected
    assert invoker_workspace_descriptor(Snapshot(invoker=invoker)) == expected
    assert (
        invoker_workspace_descriptor(MappingProxyType({"invoker": invoker})) == expected
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        1,
        {},
        {"invoker": None},
        {"invoker": {"capabilities": []}},
        {"invoker": {"capabilities": {"workspace": []}}},
        SimpleNamespace(model_dump=Mock(return_value=[])),
    ],
)
def test_missing_or_malformed_workspace_capabilities_have_no_descriptor(
    snapshot,
) -> None:
    assert invoker_workspace_descriptor(snapshot) is None


def test_malformed_workspace_fields_remain_explicit_nulls() -> None:
    assert invoker_workspace_descriptor(
        {
            "invoker": {
                "implementation": 1,
                "capabilities": {
                    "workspace": {
                        "honors_cwd": 1,
                        "launch_mode": False,
                        "controlled_child_environment": "true",
                    }
                },
            }
        }
    ) == {
        "implementation": None,
        "honors_cwd": None,
        "launch_mode": None,
        "controlled_child_environment": None,
    }


@pytest.mark.parametrize("controlled", [None, False, True, 0, 1, "true"])
@pytest.mark.parametrize(
    "launch_mode", [None, "runtime_command_runner", "mock_no_child_process"]
)
def test_environment_requirement_needs_literal_true(launch_mode, controlled) -> None:
    workspace = {"launch_mode": launch_mode, "controlled_child_environment": controlled}
    expected = launch_mode == "runtime_command_runner" and controlled is True
    assert requires_controlled_child_environment(workspace) is expected
    plan = SimpleNamespace(
        runtime_config_snapshot={
            "invoker": {
                "capabilities": {"workspace": workspace},
            }
        }
    )
    assert controlled_child_environment_required(plan) is expected


@pytest.mark.parametrize("level", ["invoker", "capabilities", "workspace"])
@pytest.mark.parametrize("malformed", [None, [], MappingProxyType({})])
def test_runtime_environment_requirement_preserves_dictionary_guards(
    level, malformed
) -> None:
    snapshot = {
        "invoker": {
            "capabilities": {
                "workspace": {
                    "launch_mode": "runtime_command_runner",
                    "controlled_child_environment": True,
                }
            }
        }
    }
    parent = snapshot
    for key in ("invoker", "capabilities", "workspace"):
        if key == level:
            parent[key] = (
                MappingProxyType(parent[key])
                if isinstance(malformed, MappingProxyType)
                else malformed
            )
            break
        parent = parent[key]
    assert (
        controlled_child_environment_required(
            SimpleNamespace(runtime_config_snapshot=snapshot)
        )
        is False
    )

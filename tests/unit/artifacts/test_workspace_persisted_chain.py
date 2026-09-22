from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.artifacts.workspace.persisted_chain import (
    workspace_result_descriptor_from_payload,
)


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize(
    ("values", "valid_size"),
    [
        ({}, False),
        ({"value": None}, False),
        ({"value": True}, False),
        ({"value": False}, False),
        ({"value": "1"}, False),
        ({"value": 1.0}, False),
        ({"value": -1}, False),
        ({"value": 0}, True),
        ({"value": 1}, True),
    ],
)
def test_persisted_source_chain_size_boundaries(
    tmp_path: Path, required: bool, values: dict[str, object], valid_size: bool
) -> None:
    (tmp_path / "result.bundle").touch()
    source = {"kind": "project", "commit": "a" * 40, "tree": "b" * 40}
    bundle = {"path": "result.bundle", "sha256": "e" * 64, "size_bytes": 1}
    payload = {
        "node_id": "build",
        "source": source,
        "result": {"result_commit": "c" * 40, "result_tree": "d" * 40},
        "refs": {"result": "refs/crewplane/result"},
        "bundle": bundle,
    }
    record, key = (bundle, "size_bytes") if required else (source, "bundle_size_bytes")
    if "value" in values:
        record[key] = values["value"]
    else:
        record.pop(key, None)
    value = values.get("value")
    if not valid_size and (required or value is not None):
        with pytest.raises(
            RuntimeError, match=r"^Workspace source descriptor lacks a required size\.$"
        ):
            workspace_result_descriptor_from_payload(tmp_path, payload)
        return

    descriptor = workspace_result_descriptor_from_payload(tmp_path, payload)
    decoded = descriptor if required else descriptor.upstream_sources[0]
    assert decoded.bundle_size_bytes == value

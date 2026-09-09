from __future__ import annotations

import pytest

from crewplane.core.workspace.naming import (
    result_ref_names,
    safe_file_component,
    safe_ref_component,
    temporary_import_ref_prefix,
)


@pytest.mark.parametrize(
    ("value", "expected_ref", "expected_file"),
    [
        ("", "e3b0c44298fc1c14", "e3b0c44298fc1c14"),
        ("...---", "8d4e47cef93b335c", "8d4e47cef93b335c"),
        ("feature.lock", "3d6455080e321ebf", "feature.lock"),
        ("a..b", "a.b", "a..b"),
        ("Café / report", "Caf-report", "Caf-report"),
        ("x" * 96, "x" * 96, "x" * 96),
        ("x" * 140, "x" * 83 + "-f5e19a4ccf0b", "x" * 106 + "--f5e19a4ccf0b"),
        ("x" * 140 + "y", "x" * 83 + "-3993a9bc4c6c", "x" * 106 + "--3993a9bc4c6c"),
    ],
)
def test_workspace_names_preserve_persisted_encoding(
    value: str, expected_ref: str, expected_file: str
) -> None:
    assert safe_ref_component(value) == expected_ref
    assert safe_file_component(value) == expected_file


def test_workspace_ref_layouts_preserve_invocation_scope() -> None:
    assert result_ref_names("run/name", "node..a", "feature.lock") == (
        "refs/crewplane/runs/run-name/node.a/3d6455080e321ebf/candidate",
        "refs/crewplane/runs/run-name/node.a/3d6455080e321ebf/result",
    )
    assert temporary_import_ref_prefix("run/name", "node..a", "task.1") == (
        "refs/crewplane/runs/run-name/imports/node.a/task.1/"
    )
    assert result_ref_names("run", "node", "task.1") != result_ref_names(
        "run", "node", "task.2"
    )
    assert temporary_import_ref_prefix(
        "run", "node", "task.1"
    ) != temporary_import_ref_prefix("run", "node-other", "task.1")

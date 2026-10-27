import hashlib
from pathlib import Path

import pytest

from crewplane.architecture.contracts.artifacts import (
    ArtifactContract,
    build_stage_directory_name,
)
from crewplane.architecture.safe_files import is_safe_relative_path
from crewplane.core.execution_state import ArtifactDescriptor
from crewplane.core.review_checkpoint_state import validate_checkpoint_path


@pytest.mark.parametrize(
    "locator",
    [
        "",
        ".",
        "..",
        "a//b",
        "a/./b",
        "a/../b",
        "/a",
        "C:a",
        "C:/a",
        "\\a",
        "a\\b",
        "file:stream",
        "NUL",
        "con.txt",
        "CON .txt",
        "CONIN$",
        "CONOUT$.log",
        "COM1.md",
        "LPT².json",
        "tail.",
        "tail ",
        "a/ b ",
        "a?b",
        "a|b",
        "a\x00b",
        "a\x1fb",
    ],
)
def test_raw_locator_validation_agrees_across_contracts(locator) -> None:
    assert not is_safe_relative_path(locator)
    with pytest.raises(ValueError):
        ArtifactContract(output_path=locator)
    with pytest.raises(ValueError):
        validate_checkpoint_path(locator)
    with pytest.raises(ValueError):
        ArtifactDescriptor(
            kind="output",
            relative_path=locator,
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        )


@pytest.mark.parametrize(
    "locator",
    [
        "nested/café.md",
        "white space/hello.txt",
        "CONSOLE.md",
        "COM10/file",
        "日本語/🙂.md",
    ],
)
def test_safe_locators_preserve_forward_slashes_and_unicode(locator) -> None:
    assert is_safe_relative_path(locator)
    assert ArtifactContract(output_path=locator).output_path == locator


@pytest.mark.parametrize(
    "name",
    [
        "con",
        "NUL.md",
        "aux.txt",
        "com9.txt",
        "lpt1",
        "trailing.",
        "trailing ",
        "a" * 200 + ".",
    ],
)
def test_generated_names_are_safe_bounded_and_deterministic(name) -> None:
    result = build_stage_directory_name(name)
    assert is_safe_relative_path(result)
    assert len(result) <= 180
    assert result == build_stage_directory_name(name)
    if name.endswith("."):
        assert result != build_stage_directory_name(name.rstrip(". "))


def test_generated_directory_suffix_removal_is_disambiguated():
    from crewplane.artifacts.naming import build_generated_file_result_dir_name

    assert build_generated_file_result_dir_name("valid") == "valid"
    assert build_generated_file_result_dir_name("valid.") != "valid"
    assert is_safe_relative_path(build_generated_file_result_dir_name("valid."))


def test_reserved_log_filename_keeps_valid_task_ids_unchanged():
    from crewplane.architecture.contracts.artifacts import build_log_filename

    assert build_log_filename("aux") == "safe-aux.log"
    assert build_log_filename("aux_executor_0") == "aux-executor-0.log"


@pytest.mark.parametrize("round_num", [0, 1, 10])
@pytest.mark.parametrize("role", ["executor", "reviewer"])
def test_invocation_sidecars_at_component_budget_are_portable(round_num, role):
    from crewplane.architecture.contracts.artifacts import (
        build_log_filename,
        build_review_audit_directory_name,
        build_reviewer_state_filename,
        build_task_round_filename,
        candidate_identity_path,
        review_metadata_path,
        review_raw_output_path,
    )
    from tests.helpers.invocation_names import boundary_alias

    alias = boundary_alias("review", 9, role, index=10)
    task_id = f"{alias}_{role}_10"
    task = Path(build_task_round_filename(task_id, round_num))
    sidecar = (
        candidate_identity_path(task)
        if role == "executor"
        else review_metadata_path(task)
    )
    names = [
        task.name,
        sidecar.name,
        build_log_filename(task_id, 10, round_num),
        build_review_audit_directory_name(10),
    ]
    if role == "reviewer":
        names.extend(
            [
                review_raw_output_path(task).name,
                build_reviewer_state_filename(task_id, round_num),
            ]
        )
    assert all(is_safe_relative_path(name) and len(name) <= 180 for name in names)
    if round_num == 10:
        assert len(sidecar.name) == 180

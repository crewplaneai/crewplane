import hashlib

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
    from crewplane.artifacts.naming import build_log_filename

    assert build_log_filename("aux") == "safe-aux.log"
    assert build_log_filename("aux_executor_0") == "aux-executor-0.log"

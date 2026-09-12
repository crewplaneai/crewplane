import hashlib
import json

import pytest

from crewplane.artifacts.naming import (
    MAX_GENERATED_PATH_COMPONENT_CHARS,
    build_findings_filename,
    build_lock_name,
    build_log_filename,
    build_node_state_filename,
    build_provider_process_state_filename,
    build_result_filename,
    build_run_key_name,
    build_stage_directory_name,
    build_workspace_export_filename,
    safe_stage_name,
    validate_run_key_name,
)
from crewplane.core.workflow.keywords import ProviderRole


def test_generated_names_budget_final_component_length() -> None:
    long_workflow = "Workflow " + ("Alpha Beta " * 80)
    long_node = "node." + ("segment-" * 90)
    long_task = "task " + ("executor " * 90)
    workflow_identity = "/repo/.crewplane/workflows/" + ("deep/" * 40) + "task.md"
    workflow_signature = "a" * 64

    generated = [
        build_lock_name(long_workflow, workflow_identity, workflow_signature),
        build_run_key_name(long_workflow, "20260609-120000-123456"),
        build_stage_directory_name(long_node),
        build_result_filename(long_node),
        build_findings_filename(long_node),
        build_node_state_filename(long_node),
        build_log_filename(long_task, audit_round_num=12, round_num=34),
    ]

    assert all(
        len(component) <= MAX_GENERATED_PATH_COMPONENT_CHARS for component in generated
    )
    assert workflow_identity not in generated[0]
    assert generated[0].endswith(f"--{workflow_signature}.lock")
    assert generated[5].endswith(".json")
    assert generated[6].endswith("-audit12-round34.log")


def test_empty_or_punctuation_names_still_generate_safe_components() -> None:
    assert build_stage_directory_name("!!!") == "-"
    assert build_result_filename("!!!") == "--result.md"
    assert build_findings_filename("!!!") == "--findings.md"


def test_stage_names_preserve_valid_node_id_punctuation() -> None:
    assert build_stage_directory_name("..-") == "..-"
    assert build_result_filename("..-") == "..--result.md"
    assert build_stage_directory_name("-a") == "-a"
    assert build_stage_directory_name("a") == "a"
    assert build_result_filename("-a") == "-a-result.md"
    assert build_result_filename("-a") != build_result_filename("a")


def test_validate_run_key_name_rejects_unsafe_components() -> None:
    assert validate_run_key_name("workflow--abc123-20260609-120000")

    unsafe_run_keys = [
        "",
        ".",
        "..",
        "../../outside",
        "workflow/other",
        "workflow\\other",
        "Workflow--ABC",
        "x" * (MAX_GENERATED_PATH_COMPONENT_CHARS + 1),
    ]

    for run_key_name in unsafe_run_keys:
        try:
            validate_run_key_name(run_key_name)
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe run key {run_key_name!r}")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", "task"),
        (".", "task"),
        ("..", "task"),
        ("  ", "task"),
        (" A B ", "a-b"),
        ("Ünicode", "-nicode"),
        ("!!!", "-"),
        ("..-", "..-"),
        ("-a", "-a"),
        ("a", "a"),
    ],
)
def test_stage_normalization_preserves_persisted_names(name, expected) -> None:
    assert safe_stage_name(name) == expected
    assert build_stage_directory_name(name) == expected
    assert build_result_filename(name) == f"{expected}-result.md"
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    prefix = expected.rstrip("-._") or "artifact"
    assert build_node_state_filename(name) == f"{prefix}--{digest}.json"


@pytest.mark.parametrize("length", [169, 170, 171, 179, 180, 181])
def test_stage_names_preserve_exact_length_boundaries(length) -> None:
    name = "a" * length
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    expected_stage = name if length <= 180 else f"{'a' * 166}--{digest}"
    expected_result = (
        f"{name}-result.md"
        if length + 10 <= 180
        else f"{'a' * 156}--{digest}-result.md"
    )
    assert build_stage_directory_name(name) == expected_stage
    assert build_result_filename(name) == expected_result


@pytest.mark.parametrize(
    ("name", "stage_prefix", "artifact_prefix"),
    [
        ("Build.A", "build.a", "build-a"),
        ("Ünicode", "-nicode", "nicode"),
        ("..-", "artifact", "task"),
        ("a" * 200, "a" * 161, "a" * 161),
    ],
)
def test_state_run_lock_and_export_names_preserve_exact_output(
    name, stage_prefix, artifact_prefix
):
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    identity_digest = hashlib.sha256(b"identity").hexdigest()[:12]
    assert build_node_state_filename(name) == f"{stage_prefix}--{digest}.json"
    assert build_workspace_export_filename(name) == f"{artifact_prefix}--{digest}.json"
    run_prefix = artifact_prefix if len(name) < 180 else "a" * 162
    assert build_run_key_name(name, "run") == f"{run_prefix}--{digest}-run"
    assert (
        build_lock_name(name, "identity", "s" * 64)
        == f"{artifact_prefix[:95]}--{identity_digest}--{'s' * 64}.lock"
    )
    identity = json.dumps(
        [name, "task", "mock", "executor", 2, 3, 4], separators=(",", ":")
    )
    process_digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    process_prefix = {
        "Build.A": "build.a-task",
        "Ünicode": "-nicode-task",
        "..-": "..--task",
    }.get(name, "a" * 161)
    assert (
        build_provider_process_state_filename(
            name, "task", "mock", ProviderRole.EXECUTOR, 2, 3, 4
        )
        == f"{process_prefix}--{process_digest}.json"
    )


@pytest.mark.parametrize(
    "length", [152, 153, 154, 159, 160, 161, 167, 168, 169, 170, 180, 181]
)
def test_findings_and_log_names_preserve_exact_boundaries(length):
    name = "a" * length
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    expected_findings = (
        f"{name}-findings.md" if length <= 168 else f"{'a' * 154}--{digest}-findings.md"
    )
    expected_log = (
        f"{name}-audit12-round34.log"
        if length <= 160
        else f"{'a' * 146}--{digest}-audit12-round34.log"
    )
    assert build_findings_filename(name) == expected_findings
    assert build_log_filename(name, 12, 34) == expected_log


@pytest.mark.parametrize("total_length", [179, 180, 181])
@pytest.mark.parametrize(
    ("audit", "round_num"), [(None, None), (0, None), (None, 0), (12, 34)]
)
def test_complete_log_filename_threshold(total_length, audit, round_num):
    suffix = "" if audit is None else f"-audit{audit}"
    suffix += ("" if round_num is None else f"-round{round_num}") + ".log"
    name = "a" * (total_length - len(suffix))
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    expected = (
        name + suffix
        if total_length <= 180
        else ("a" * (180 - 14 - len(suffix)) + f"--{digest}{suffix}")
    )
    assert build_log_filename(name, audit, round_num) == expected


@pytest.mark.parametrize(
    ("name", "stage", "log"),
    [
        ("Build.A", "build.a", "build-a"),
        ("Ünicode", "-nicode", "nicode"),
        ("..-", "..-", "task"),
        ("  ", "task", "task"),
        ("!!!", "-", "task"),
    ],
)
def test_result_findings_and_log_keep_distinct_normalization(name, stage, log):
    assert build_result_filename(name) == f"{stage}-result.md"
    assert build_findings_filename(name) == f"{stage}-findings.md"
    assert build_log_filename(name) == f"{log}.log"

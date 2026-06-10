from orchestrator_cli.artifacts.naming import (
    MAX_GENERATED_PATH_COMPONENT_CHARS,
    build_findings_filename,
    build_lock_name,
    build_log_filename,
    build_node_state_filename,
    build_result_filename,
    build_run_key_name,
    build_stage_directory_name,
    validate_run_key_name,
)


def test_generated_names_budget_final_component_length() -> None:
    long_workflow = "Workflow " + ("Alpha Beta " * 80)
    long_node = "node." + ("segment-" * 90)
    long_task = "task " + ("executor " * 90)
    workflow_identity = "/repo/.orchestrator/workflows/" + ("deep/" * 40) + "task.md"
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

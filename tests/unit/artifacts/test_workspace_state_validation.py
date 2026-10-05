from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.artifacts.workspace.state import materialization_results, validation
from crewplane.core.preflight.models import PreflightExecutionPlan
from tests.helpers.resume import make_plan, make_workspace_source_snapshot
from tests.helpers.resume_validation import (
    provider_workspace_state_payload,
    snapshot_workspace_state_payload,
    source_record,
)
from tests.helpers.workspace_records import workspace_selection_record


def _checkpoint_case(
    tmp_path: Path,
    kind: str = "snapshot",
) -> tuple[RunHistoryRecord, PreflightExecutionPlan, dict[str, object]]:
    source = source_record(tmp_path)
    plan = make_plan()
    node = plan.nodes[0].model_copy(
        update={"workspace_policy": workspace_selection_record(kind=kind)}
    )
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
            "workspace_source": make_workspace_source_snapshot(),
            "runtime_config_snapshot": {
                "invoker": {
                    "implementation": "cli",
                    "capabilities": {
                        "workspace": {
                            "honors_cwd": True,
                            "launch_mode": "runtime_command_runner",
                            "controlled_child_environment": True,
                        }
                    },
                }
            },
        }
    )
    if kind == "snapshot":
        payload = snapshot_workspace_state_payload(source, plan, "alpha")
    else:
        workspace_source = plan.workspace_source
        assert workspace_source is not None
        payload = provider_workspace_state_payload(
            source, plan, workspace_source.run_base_commit, workspace_source.source_tree
        )
    workspace = payload["workspace"]
    assert isinstance(workspace, dict)
    workspace.update(retention="not_applicable", retained_reason="checkpoint")
    payload["execution"] = {}
    payload["child_process_environment"] = {"required": True, "applied": True}
    return source, plan, payload


def _validate(
    case: tuple[RunHistoryRecord, PreflightExecutionPlan, dict[str, object]],
    source_matches: bool = True,
) -> bool:
    source, plan, payload = case
    return validation.checkpoint_invocation_is_valid(
        source,
        plan,
        plan.nodes[0],
        payload,
        source_matches,
        source.manifest.run_id,
        source.manifest.run_key_name,
    )


@pytest.mark.parametrize("status", ["succeeded", "failed"])
@pytest.mark.parametrize("applied", [True, False, None, 0, 1, "true"])
def test_checkpoint_requires_strict_environment_application(
    tmp_path: Path, status: str, applied: object
) -> None:
    case = _checkpoint_case(tmp_path)
    payload = case[2]
    payload["status"] = status
    payload["child_process_environment"] = {"required": True, "applied": applied}
    if status == "failed":
        payload.pop("result")
    before = deepcopy(payload)

    assert _validate(case) is (
        isinstance(applied, bool) if status == "failed" else applied is True
    )
    assert payload == before


@pytest.mark.parametrize(
    "mismatch",
    [
        "version",
        "run_id",
        "run_key_name",
        "workflow_name",
        "workflow_signature",
        "node_id",
        "logical_worktree_name",
        "invoker",
        "git",
        "source",
        "status",
    ],
)
def test_checkpoint_rejects_context_before_rendered_file_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    case = _checkpoint_case(tmp_path)
    if mismatch not in {"source", "status"}:
        case[2][mismatch] = "wrong"
    elif mismatch == "status":
        case[2]["status"] = "running"
    rendered = Mock(side_effect=AssertionError("rendered evidence checked too early"))
    monkeypatch.setattr(validation, "provider_rendered_workspace_files_match", rendered)

    assert _validate(case, source_matches=mismatch != "source") is False
    rendered.assert_not_called()


@pytest.mark.parametrize(
    "section, field",
    [("workspace", "path"), ("workspace", "effective_cwd")]
    + [
        ("execution", field)
        for field in (
            "workspace_path",
            "effective_cwd",
            "cache_root",
            "checkout_root",
            "worktree_git_dir",
        )
    ],
)
def test_checkpoint_checks_rendered_files_before_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str, field: str
) -> None:
    case = _checkpoint_case(tmp_path)
    placement = case[2][section]
    assert isinstance(placement, dict)
    placement[field] = "/live/path"
    rendered = Mock(return_value=True)
    monkeypatch.setattr(validation, "provider_rendered_workspace_files_match", rendered)

    assert _validate(case) is False
    rendered.assert_called_once_with(case[1], case[1].nodes[0], case[2], case[0])


@pytest.mark.parametrize("kind", ["snapshot", "disposable"])
@pytest.mark.parametrize(
    "excluded",
    [
        None,
        "candidate_commit",
        "result_commit",
        "candidate_tree",
        "result_tree",
        "bundle",
    ],
)
def test_checkpoint_non_lineage_results_exclude_lineage_evidence(
    tmp_path: Path, kind: str, excluded: str | None
) -> None:
    case = _checkpoint_case(tmp_path, "snapshot" if kind == "snapshot" else "worktree")
    payload = case[2]
    if kind == "disposable":
        payload["role"] = "reviewer"
        workspace = payload["workspace"]
        assert isinstance(workspace, dict)
        workspace["lineage_producer"] = False
        payload.pop("refs")
        payload.pop("bundle")
        payload["result"] = {
            "lineage_produced": False,
            "changed_path_count": 1,
            "final_head": "a" * 40,
        }
    result = payload["result"]
    assert isinstance(result, dict)
    if excluded == "bundle":
        payload["bundle"] = None
    elif excluded is not None:
        result[excluded] = None

    assert _validate(case) is (excluded is None)


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_checkpoint_limited_snapshot_keeps_existing_result_rules(
    tmp_path: Path, status: str
) -> None:
    case = _checkpoint_case(tmp_path)
    case[2]["status"] = status
    case[2]["result"] = {
        "lineage_produced": False,
        "drift_scan_complete": False,
        "drift_scan_limit_reason": "max_entries",
        "candidate_commit": None,
    }

    assert _validate(case) is True


@pytest.mark.parametrize(
    "error_type", [None, OSError, RuntimeError, subprocess.SubprocessError, ValueError]
)
def test_checkpoint_bundle_validation_preserves_errors_and_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception] | None
) -> None:
    case = _checkpoint_case(tmp_path, "worktree")
    source, plan, payload = case
    bundle = payload["bundle"]
    assert isinstance(bundle, dict)
    bundle_bytes = b"bundle"
    bundle_path = source.run_dir / str(bundle["path"])
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_bytes(bundle_bytes)
    error = error_type("chain failure") if error_type is not None else None
    verify = Mock(side_effect=error)
    monkeypatch.setattr(
        materialization_results, "verify_persisted_workspace_result_chain", verify
    )
    before = deepcopy(payload)

    if error_type is ValueError:
        with pytest.raises(ValueError) as caught:
            _validate(case)
        assert caught.value is error
    else:
        assert _validate(case) is (error_type is None)
    verify.assert_called_once_with(plan.workspace_source, source.run_dir, payload)
    assert payload == before
    assert bundle_path.read_bytes() == bundle_bytes
    assert hashlib.sha256(bundle_bytes).hexdigest() == bundle["sha256"]


@pytest.mark.parametrize("status", ["succeeded", "failed"])
@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "identity",
        "policy",
        "invoker",
        "environment",
        "git",
        "source",
        "rendered",
        "placement",
    ],
)
def test_node_workspace_context_preserves_validation_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, mismatch: str | None
) -> None:
    source, plan, _payload = _checkpoint_case(tmp_path)
    node = plan.nodes[0].model_copy(update={"mode": "parallel"})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    payload = snapshot_workspace_state_payload(source, plan, "alpha")
    payload["status"] = status
    payload["child_process_environment"] = {
        "required": True,
        "applied": status == "succeeded",
    }
    if status == "failed":
        payload.pop("result")
    match mismatch:
        case "identity":
            payload["run_id"] = "wrong"
        case "policy":
            payload["logical_worktree_name"] = "wrong"
        case "invoker":
            payload["invoker"] = {}
        case "environment":
            payload["child_process_environment"] = {"required": True, "applied": 1}
        case "git":
            payload["git"]["repo_id"] = "wrong"
        case "source":
            payload["source"]["commit"] = "c" * 40
            payload["invocation_source"]["source_commit"] = "c" * 40
        case "rendered":
            payload["rendered_workspace_files"] = ["unexpected"]
        case "placement":
            payload["workspace"]["path"] = "/live/path"
    state_path = source.run_dir / "a" / "workspace-state-alpha.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    before = state_path.read_bytes()
    checks = Mock()
    checks.attach_mock(
        Mock(wraps=validation.workspace_invocation_source_matches), "source"
    )
    checks.attach_mock(
        Mock(wraps=validation.provider_rendered_workspace_files_match), "rendered"
    )
    monkeypatch.setattr(
        validation, "workspace_invocation_source_matches", checks.source
    )
    monkeypatch.setattr(
        validation, "provider_rendered_workspace_files_match", checks.rendered
    )

    assert validation.workspace_node_state_is_valid(source, plan, node) is (
        mismatch is None
    )
    expected_calls = []
    if mismatch in {None, "source", "rendered", "placement"}:
        expected_calls.append(call.source(source, plan, node, payload))
    if mismatch in {None, "rendered", "placement"}:
        expected_calls.append(call.rendered(plan, node, payload, source))
    assert checks.mock_calls == expected_calls
    assert state_path.read_bytes() == before

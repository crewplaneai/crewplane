from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.cli.run.workspace import (
    filesystem_policy as workspace_filesystem_policy,
)
from crewplane.cli.run.workspace import git_source as git_source_probe
from crewplane.cli.run.workspace import source_policy as policy
from crewplane.cli.run.workspace.cache_policy import paths_overlap
from crewplane.cli.run.workspace.git_source import GitSourceContext
from crewplane.cli.run.workspace.preflight_diagnostics import (
    workspace_preflight_diagnostics,
)
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.workflow.models import (
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workspace.git_policy import workspace_git_base_environment
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_source_policy import (
    apply_patched_git_policy,
    git_source_context,
    workspace_input_only_workflow,
    workspace_source_config,
    workspace_source_workflow,
)


@pytest.fixture
def patched_git_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    apply_patched_git_policy(monkeypatch, tmp_path)


def test_disabled_workspace_source_policy_does_not_probe_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_discover_git_context(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("disabled mode must not probe Git")

    config = Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["echo"])},
    )
    monkeypatch.setattr(
        policy,
        "discover_git_context",
        fail_discover_git_context,
    )

    result = policy.collect_workspace_source_policy(
        config=config,
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=True,
    )

    assert result.errors == ()
    assert result.warnings == ()
    assert result.source_snapshot is None


def test_workspace_source_policy_fails_native_windows_before_git_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_discover_git_context(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("native Windows must fail first")

    monkeypatch.setattr(policy, "is_native_windows", lambda: True)
    monkeypatch.setattr(
        policy,
        "discover_git_context",
        fail_discover_git_context,
    )

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert len(result.errors) == 1
    assert "native Windows" in result.errors[0]


def test_workspace_source_policy_reports_git_discovery_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def timeout_run(command: list[str], *args: object, **kwargs: object) -> None:
        del args
        assert command[0] == "git"
        assert kwargs["timeout"] == git_source_probe.GIT_SOURCE_PROBE_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(git_source_probe.subprocess, "run", timeout_run)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert len(result.errors) == 1
    assert "requires a Git repository with a valid HEAD commit" in result.errors[0]
    assert "Git command timed out after 30.0 second(s)" in result.errors[0]


def test_workspace_source_policy_skips_git_when_workspace_policy_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_discover_git_context(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("workspace policy errors must fail before Git discovery")

    workflow = workspace_input_only_workflow()
    monkeypatch.setattr(policy, "discover_git_context", fail_discover_git_context)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workflow,
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.errors == ()
    assert result.source_snapshot is None


def test_workspace_source_policy_skips_git_when_workflow_graph_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_discover_git_context(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("invalid workflow graph must fail before Git discovery")

    workflow = workspace_source_workflow()
    workflow = workflow.model_copy(
        update={
            "nodes": [
                workflow.nodes[0].model_copy(update={"needs": ["missing"]}),
            ]
        }
    )
    monkeypatch.setattr(policy, "discover_git_context", fail_discover_git_context)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workflow,
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.errors == ()
    assert result.source_snapshot is None


@pytest.mark.usefixtures("patched_git_policy")
@pytest.mark.usefixtures("patched_git_policy")
def test_workspace_source_policy_rejects_real_execution_without_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_discover_git_context(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("unsupported invoker must fail before Git discovery")

    monkeypatch.setattr(policy, "discover_git_context", fail_discover_git_context)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=True,
        invoker_capabilities=None,
    )

    assert result.source_snapshot is None
    assert result.errors == (
        "Workspace invoker compatibility failed: selected invoker does not "
        "declare the workspace launch contract.",
    )


def test_workspace_source_policy_rejects_relative_cli_path_for_managed_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_discover_git_context(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("relative workspace executable must fail before Git discovery")

    monkeypatch.setattr(policy, "discover_git_context", fail_discover_git_context)
    agent = AgentConfig(cli_cmd=["echo"]).model_copy(
        update={"cli_cmd": ["./bin/provider"]}
    )
    config = Config(
        version=SCHEMA_VERSION,
        agents={"alpha": agent},
        settings=Settings(workspace={"enabled": True}),
    )

    result = policy.collect_workspace_source_policy(
        config=config,
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=True,
        invoker_capabilities={
            "workspace": {
                "supported": True,
                "launch_mode": "runtime_command_runner",
                "honors_cwd": True,
                "controlled_child_environment": True,
            }
        },
    )

    assert result.source_snapshot is None
    assert result.errors == (
        "Workspace invoker compatibility failed: agent 'alpha' uses relative "
        "path executable './bin/provider'. Workspace runtime_command_runner "
        "invocations require an absolute executable path or a PATH-resolved "
        "command name.",
    )


@pytest.mark.usefixtures("patched_git_policy")
@pytest.mark.usefixtures("patched_git_policy")
def test_workspace_source_policy_records_snapshot_when_checks_pass(
    tmp_path: Path,
) -> None:
    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=True,
        invoker_capabilities={
            "workspace": {
                "supported": True,
                "launch_mode": "runtime_command_runner",
                "honors_cwd": True,
                "controlled_child_environment": True,
            }
        },
    )

    assert result.errors == ()
    assert result.source_snapshot is not None
    assert result.source_snapshot.worktree_contract.mode == "blob_exact"
    assert result.source_snapshot.run_base_commit == "a" * 40
    assert result.source_snapshot.source_tree == "b" * 40
    assert result.source_snapshot.project_root_relative_path == "."


def test_workspace_source_policy_estimates_full_repo_only_for_worktrees() -> None:
    config = workspace_source_config()
    snapshot_workflow = WorkflowPlan(
        name="workspace snapshot",
        worktrees={"scratch": {"kind": "snapshot"}},
        nodes=workspace_source_workflow().nodes,
    )

    assert policy.workflow_requires_full_repository_checkout(
        workspace_source_workflow(),
        config,
    )
    assert not policy.workflow_requires_full_repository_checkout(
        snapshot_workflow,
        config,
    )


def test_workspace_cache_overlap_uses_casefolded_unicode_normalization(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "CAFÉ" / "cache"
    blocked_root = tmp_path / "cafe\u0301"

    assert paths_overlap(cache_root, blocked_root)


def test_executable_bit_probe_creates_empty_probe_file(tmp_path: Path) -> None:
    supported = workspace_filesystem_policy.executable_bit_supported(tmp_path)

    probe = tmp_path / "exec-probe"
    assert probe.exists()
    assert probe.read_bytes() == b""
    assert supported == bool(stat.S_IMODE(probe.stat().st_mode) & stat.S_IXUSR)


@pytest.mark.usefixtures("patched_git_policy")
def test_invalid_cache_root_skips_filesystem_capability_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_probe(*args: object, **kwargs: object) -> dict[str, bool]:
        del args, kwargs
        pytest.fail("invalid cache root must fail before filesystem probing")

    monkeypatch.setattr(policy, "probe_filesystem_capabilities", fail_probe)
    config = workspace_source_config().model_copy(
        update={
            "settings": Settings(
                workspace={
                    "enabled": True,
                    "cache_root": (tmp_path / "cache").as_posix(),
                }
            )
        }
    )

    result = policy.collect_workspace_source_policy(
        config=config,
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any("cache root must not overlap" in error for error in result.errors)


@pytest.mark.usefixtures("patched_git_policy")
def test_dry_run_workspace_source_policy_skips_filesystem_capability_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_probe(*args: object, **kwargs: object) -> dict[str, bool]:
        del args, kwargs
        pytest.fail("dry-run source policy must not probe writable filesystem state")

    monkeypatch.setattr(policy, "probe_filesystem_capabilities", fail_probe)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.errors == ()
    assert result.source_snapshot is not None
    assert result.source_snapshot.filesystem_capabilities == {}


@pytest.mark.usefixtures("patched_git_policy")
@pytest.mark.usefixtures("patched_git_policy")
def test_workspace_source_policy_skips_git_for_input_only_workflow(
    tmp_path: Path,
) -> None:
    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_input_only_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.errors == ()
    assert result.source_snapshot is None


@pytest.mark.usefixtures("patched_git_policy")
@pytest.mark.usefixtures("patched_git_policy")
def test_workspace_source_policy_skips_git_for_external_input_only_workflow(
    tmp_path: Path,
) -> None:
    workflow = WorkflowPlan(
        name="workspace external input",
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:/tmp/external.md}}",
            )
        ],
    )

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workflow,
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.errors == ()
    assert result.source_snapshot is None


def test_workspace_source_policy_stops_after_unsupported_git_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_discover_git_context(
        project_root: Path,
        builder: policy.WorkspacePolicyBuilder,
    ) -> GitSourceContext:
        del project_root, builder
        return replace(git_source_context(tmp_path), git_version="2.34.0")

    def fail_later_policy(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("old Git must fail before later source policy probes")

    monkeypatch.setattr(policy, "discover_git_context", fake_discover_git_context)
    monkeypatch.setattr(policy, "validate_cache_root", fail_later_policy)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert len(result.errors) == 1
    assert "Git 2.34.1 or newer is required" in result.errors[0]


def test_workspace_source_policy_stops_after_failed_git_capability_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_discover_git_context(
        project_root: Path,
        builder: policy.WorkspacePolicyBuilder,
    ) -> GitSourceContext:
        del project_root, builder
        return git_source_context(tmp_path)

    def fake_validate_git_capabilities(
        git_context: GitSourceContext,
        builder: policy.WorkspacePolicyBuilder,
    ) -> None:
        del git_context
        builder.errors.append(
            "Workspace Git contract failed: required Git command-surface probes failed."
        )

    def fail_later_policy(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("failed capability probes must stop later source policy checks")

    monkeypatch.setattr(policy, "discover_git_context", fake_discover_git_context)
    monkeypatch.setattr(
        policy,
        "validate_git_capabilities",
        fake_validate_git_capabilities,
    )
    monkeypatch.setattr(policy, "validate_cache_root", fail_later_policy)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert result.errors == (
        "Workspace Git contract failed: required Git command-surface probes failed.",
    )


def test_git_capability_probe_reports_failed_named_command_probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected_probes = (
        (
            "object-format discovery",
            ("rev-parse", "--show-object-format=storage"),
        ),
        (
            "porcelain v2 status",
            ("status", "--porcelain=v2", "-z", "--untracked-files=no"),
        ),
        (
            "index flag inspection",
            ("ls-files", "-v", "-z"),
        ),
        (
            "full-tree listing",
            ("ls-tree", "-r", "-z", "--full-tree", "--full-name", "HEAD"),
        ),
        (
            "object existence checks",
            ("cat-file", "-e", "HEAD^{tree}"),
        ),
        (
            "effective attribute inspection",
            (
                "--literal-pathspecs",
                "check-attr",
                "--cached",
                "-z",
                "--all",
                "--",
                "__crewplane_missing_probe_path__",
            ),
        ),
    )
    observed_commands: list[tuple[str, ...]] = []

    def unsupported_probe(project_root: Path, *args: str) -> bool:
        assert project_root == tmp_path
        observed_commands.append(tuple(args))
        return False

    def supported_literal_pathspec(project_root: Path) -> bool:
        assert project_root == tmp_path
        return True

    def supported_worktree_locking(project_root: Path) -> bool:
        assert project_root == tmp_path
        return True

    monkeypatch.setattr(git_source_probe, "git_probe_supported", unsupported_probe)
    monkeypatch.setattr(
        git_source_probe,
        "git_literal_pathspec_probe_supported",
        supported_literal_pathspec,
    )
    monkeypatch.setattr(
        git_source_probe,
        "worktree_locking_supported",
        supported_worktree_locking,
    )
    builder = policy.WorkspacePolicyBuilder()

    git_source_probe.validate_git_capabilities(git_source_context(tmp_path), builder)

    assert observed_commands == [args for _, args in expected_probes]
    assert builder.errors == [
        (
            "Workspace Git contract failed: required Git command-surface probes "
            "failed for blob_exact: object-format discovery, porcelain v2 status, "
            "index flag inspection, full-tree listing, object existence checks, "
            "effective attribute inspection."
        )
    ]


def test_git_capability_probe_rejects_missing_worktree_lock_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def supported_probe(*args: object) -> bool:
        del args
        return True

    def supported_literal_pathspec(project_root: Path) -> bool:
        del project_root
        return True

    def unsupported_worktree_locking(project_root: Path) -> bool:
        del project_root
        return False

    monkeypatch.setattr(
        git_source_probe,
        "git_probe_supported",
        supported_probe,
    )
    monkeypatch.setattr(
        git_source_probe,
        "git_literal_pathspec_probe_supported",
        supported_literal_pathspec,
    )
    monkeypatch.setattr(
        git_source_probe,
        "worktree_locking_supported",
        unsupported_worktree_locking,
    )
    builder = policy.WorkspacePolicyBuilder()

    git_source_probe.validate_git_capabilities(git_source_context(tmp_path), builder)

    assert builder.errors == [
        (
            "Workspace Git contract failed: required Git command-surface probes "
            "failed for blob_exact: locked detached worktree provisioning."
        )
    ]


def test_literal_pathspec_probe_treats_timeout_as_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def timeout_run(command: list[str], *args: object, **kwargs: object) -> None:
        del args
        assert "--literal-pathspecs" in command
        assert kwargs["timeout"] == git_source_probe.GIT_SOURCE_PROBE_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(git_source_probe.subprocess, "run", timeout_run)

    assert not git_source_probe.git_literal_pathspec_probe_supported(tmp_path)


def test_worktree_locking_probe_treats_timeout_as_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def timeout_run(command: list[str], *args: object, **kwargs: object) -> None:
        del args
        assert command[-3:] == ["worktree", "add", "-h"]
        assert kwargs["check"] is False
        assert kwargs["timeout"] == git_source_probe.GIT_SOURCE_PROBE_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(git_source_probe.subprocess, "run", timeout_run)

    assert not git_source_probe.worktree_locking_supported(tmp_path)


def test_git_env_applies_sanitized_template_without_mutating_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREWPLANE_TEST_KEEP", "kept")
    monkeypatch.setenv("GIT_DIR", "/tmp/wrong-repo")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", "/tmp/template")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    env = git_source_probe.git_env()
    expected_overlay = workspace_git_base_environment(read_only=True)

    assert env["CREWPLANE_TEST_KEEP"] == "kept"
    assert "GIT_DIR" not in env
    assert "GIT_TEMPLATE_DIR" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert {key: env[key] for key in expected_overlay} == expected_overlay
    assert os.environ["GIT_DIR"] == "/tmp/wrong-repo"
    assert os.environ["GIT_OPTIONAL_LOCKS"] == "1"
    assert os.environ["GIT_TEMPLATE_DIR"] == "/tmp/template"


def test_workspace_source_policy_wraps_late_git_probe_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    apply_patched_git_policy(monkeypatch, tmp_path)

    def fail_clean_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise subprocess.CalledProcessError(
            128,
            ("git", "status"),
            stderr=b"fatal: bad index file",
        )

    monkeypatch.setattr(policy, "validate_clean_start", fail_clean_start)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert result.errors == (
        "Workspace source policy failed: Git source inspection failed "
        "(fatal: bad index file).",
    )


def test_workspace_source_policy_wraps_late_git_probe_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    apply_patched_git_policy(monkeypatch, tmp_path)

    def timeout_clean_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise subprocess.TimeoutExpired(("git", "status"), 0.01)

    monkeypatch.setattr(policy, "validate_clean_start", timeout_clean_start)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert result.errors == (
        "Workspace source policy failed: Git source inspection failed "
        "(Git command timed out after 0.01 second(s)).",
    )


def test_workspace_preflight_diagnostics_use_workspace_codes() -> None:
    result = policy.WorkspacePolicyCheck(
        errors=(
            "Workspace invoker compatibility failed: selected invoker does not "
            "declare the workspace launch contract.",
            "Workspace Git contract failed: Git 2.34.1 or newer is required.",
            "Workspace source policy failed: project root must be inside a Git repository.",
        ),
        warnings=(
            "Workspace source policy warning: tracked_only excluded 1 untracked files.",
        ),
    )

    diagnostics = workspace_preflight_diagnostics(result)

    assert [(item.code, item.phase, item.severity) for item in diagnostics] == [
        ("WORKSPACE-INVOKER", "invoker_workspace_compatibility", "error"),
        ("WORKSPACE-GIT-CONTRACT", "worktree_contract", "error"),
        ("WORKSPACE-SOURCE", "source_policy", "error"),
        ("WORKSPACE-SOURCE", "source_policy", "warning"),
    ]

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from orchestrator_cli.bootstrap.container import build_runtime_components
from orchestrator_cli.core.config import Config, load_config
from orchestrator_cli.core.versions import WORKFLOW_SCHEMA_VERSION
from orchestrator_cli.core.workflow_loader import load_tasks_with_sources
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.core.workflow_validation import validate_workflow_plan
from orchestrator_cli.observability.runtime import ObservabilityHub
from orchestrator_cli.observability.types import RunContext, RunResult
from orchestrator_cli.runtime.execution.workflow import execute_workflow
from tests.helpers.observability import topology_from_workflow
from tests.integration.compiled_plan_helpers import compile_plan_for_components
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime

CONFIG_TEMPLATE_PATH = Path(__file__).with_name("fixtures") / "config.yml"
FAKE_TMUX_BIND_ARG_THRESHOLD = 800


def provider(name: str, role: str = "executor") -> ProviderSpec:
    return ProviderSpec(provider=name, role=role)


def write_workflow(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def build_compact_linear_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="compact.linear",
        nodes=[
            WorkflowNode(
                id="design.discovery",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Discover")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="design.iteration",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Iterate")],
                needs=["design.discovery"],
                providers=[provider("codex", role="executor")],
            ),
        ],
    )


def build_compact_namespaced_workflow(tmp_path: Path) -> WorkflowPlan:
    module_path = tmp_path / "module.task.md"
    workflow_path = tmp_path / "root.task.md"

    write_workflow(
        module_path,
        [
            "---",
            f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
            "name: Auth Module",
            "nodes:",
            "  - id: plan",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## plan",
            "",
            "Build auth module.",
        ],
    )
    write_workflow(
        workflow_path,
        [
            "---",
            f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
            "name: Root",
            "imports:",
            "  - path: module.task.md",
            "    as: auth",
            "nodes:",
            "  - id: summary.final",
            "    mode: sequential",
            "    needs: [auth.plan]",
            "    providers: [alpha]",
            "---",
            "",
            "## summary.final",
            "",
            "Summarize {{auth.plan.output}}.",
        ],
    )
    return validate_workflow_plan(
        load_tasks_with_sources(workflow_path, project_root=tmp_path).workflow
    )


def ordered_node_ids(snapshot) -> list[str]:  # type: ignore[no-untyped-def]
    node_ids: list[str] = []
    seen: set[str] = set()
    for wave in snapshot.layout.waves:
        for node_id in wave:
            if node_id in seen:
                continue
            seen.add(node_id)
            node_ids.append(node_id)
    extras = sorted(
        (node_id for node_id in snapshot.state.nodes if node_id not in seen),
        key=snapshot.state.node_order.__getitem__,
    )
    node_ids.extend(extras)
    return node_ids


def render_compact_dashboard(run_result) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    runtime = SimulatedTmuxRuntime()
    runtime.start(
        RunContext(
            workflow_topology=topology_from_workflow(run_result.workflow),
            run_id="compact-runtime",
            refresh_per_second=0,
        )
    )
    try:
        node_index = ordered_node_ids(run_result.selected_snapshot).index(
            run_result.selected_node_id
        )
        runtime.runtime_files.selection_index.write_text(
            str(node_index), encoding="utf-8"
        )
        runtime.on_snapshot(None, run_result.selected_snapshot)
        runtime.refresh_once()
        left_text = runtime.runtime_files.left_content.read_text(encoding="utf-8")
        right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")
        return left_text, right_text
    finally:
        runtime.stop(RunResult(failed=False))


def test_auto_closing_stop_clears_runtime_file_state() -> None:
    runtime = SimulatedTmuxRuntime(auto_close_session=True)
    runtime.start(
        RunContext(
            workflow_topology=topology_from_workflow(
                build_compact_linear_workflow(Path("."))
            ),
            run_id="compact-runtime-stop",
            refresh_per_second=0,
        )
    )

    runtime.stop(RunResult(failed=False))

    assert runtime.lifecycle.session is None
    assert runtime.lifecycle.last_session is not None
    assert not runtime.lifecycle.last_session.runtime_lease.root.exists()


def test_non_auto_closing_stop_preserves_runtime_file_state() -> None:
    runtime = SimulatedTmuxRuntime(auto_close_session=False)
    runtime.start(
        RunContext(
            workflow_topology=topology_from_workflow(
                build_compact_linear_workflow(Path("."))
            ),
            run_id="compact-runtime-keep",
            refresh_per_second=0,
        )
    )
    left_content_path = runtime.runtime_files.left_content

    try:
        runtime.stop(RunResult(failed=False))

        preserved_path = runtime.runtime_files.left_content
        assert preserved_path == left_content_path
        assert preserved_path.exists()
    finally:
        runtime.cleanup_preserved_runtime()


def test_pane_width_uses_reported_tmux_width() -> None:
    runtime = SimulatedTmuxRuntime(auto_close_session=False)
    runtime.left_pane_width = 10
    runtime.start(
        RunContext(
            workflow_topology=topology_from_workflow(
                build_compact_linear_workflow(Path("."))
            ),
            run_id="compact-runtime-width",
            refresh_per_second=0,
        )
    )

    try:
        assert (
            runtime.client.pane_dimension(
                runtime.session.targets.left_pane_id,
                "#{pane_width}",
                80,
            )[0]
            == 10
        )
    finally:
        runtime.cleanup_preserved_runtime()


def test_failed_start_cleans_up_partial_runtime_state() -> None:
    runtime = SimulatedTmuxRuntime(auto_close_session=False)
    runtime.lifecycle.attach_failure = RuntimeError("attach failed")

    with pytest.raises(RuntimeError, match="attach failed"):
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(
                    build_compact_linear_workflow(Path("."))
                ),
                run_id="compact-runtime-start-fail",
                refresh_per_second=0,
            )
        )

    assert runtime.lifecycle.session is None
    assert runtime.lifecycle.last_session is not None
    assert not runtime.lifecycle.last_session.runtime_lease.root.exists()
    assert any(args[:2] == ["kill-session", "-t"] for args, _, _ in runtime.calls)
    assert any(
        args[:2] == ["kill-session", "-t"] and socket_name is not None
        for (args, _, _), socket_name in zip(
            runtime.calls, runtime.call_sockets, strict=False
        )
    )


def test_failed_start_cleanup_resets_state_even_if_tmux_rollback_fails() -> None:
    warnings: list[str] = []
    runtime = SimulatedTmuxRuntime(
        auto_close_session=False,
        warning_sink=warnings.append,
    )
    runtime.lifecycle.attach_failure = RuntimeError("attach failed")
    runtime.client.fail_kill_session = True

    with pytest.raises(RuntimeError, match="attach failed"):
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(
                    build_compact_linear_workflow(Path("."))
                ),
                run_id="compact-runtime-rollback-fail",
                refresh_per_second=0,
            )
        )

    assert runtime.lifecycle.session is None
    assert runtime.lifecycle.last_session is not None
    assert not runtime.lifecycle.last_session.runtime_lease.root.exists()
    assert warnings == ["tmux compact rollback failed: rollback failed"]


def load_integration_config(project_root: Path, tmux_executable: Path) -> Config:
    with CONFIG_TEMPLATE_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise AssertionError(
            f"Config fixture must deserialize to a mapping: {CONFIG_TEMPLATE_PATH}"
        )
    if data["settings"]["integrations"]["ui"]["implementation"] != "tmux":  # type: ignore[index]
        raise AssertionError("Observability integration config must enable tmux UI.")

    ui_options = data["settings"]["integrations"]["ui"]["options"]  # type: ignore[index]
    if not isinstance(ui_options, dict):
        raise AssertionError("Config fixture tmux ui options must be a mapping.")
    ui_options["tmux_executable"] = str(tmux_executable)

    config_path = project_root / ".orchestrator" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_config(config_path)


def write_fake_tmux_executable(path: Path) -> None:
    script = """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

raw_args = sys.argv[1:]
args = raw_args[2:] if len(raw_args) >= 2 and raw_args[0] == "-L" else raw_args
log_path = Path(os.environ["FAKE_TMUX_LOG"])
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")

threshold = int(os.environ["FAKE_TMUX_BIND_ARG_THRESHOLD"])
if args and args[0] == "bind-key":
    if any(len(arg) > threshold for arg in args):
        print("command too long", file=sys.stderr)
        sys.exit(1)

if args and args[0] == "new-session" and "-P" in args:
    print("%10")
    sys.exit(0)
if args and args[0] == "split-window" and "-P" in args:
    print("%20")
    sys.exit(0)
if args and args[0] == "has-session":
    sys.exit(0)
if args and args[0] == "display-message" and args[-1] in {"#{pane_width}", "#{pane_height}"}:
    target = args[args.index("-t") + 1]
    if args[-1] == "#{pane_width}":
        print("100" if target == "%10" else "180")
    else:
        print("18" if target == "%10" else "30")
    sys.exit(0)
if args and args[0] == "attach":
    sys.exit(0)

sys.exit(0)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def read_fake_tmux_commands(log_path: Path) -> list[list[str]]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


COMPACT_RUNTIME_CASES = [
    pytest.param(
        {
            "case_id": "compact-linear-dashboard",
            "build_workflow": build_compact_linear_workflow,
            "snapshot_event_type": "workflow_finished",
            "selected_node_id": "design.iteration",
            "expected_left_fragments": (
                "DAG Summary",
                "▸● design.iteration",
                "design.discovery",
                "│",
            ),
            "expected_right_fragments": (
                "Node Output: design.iteration",
                "codex/executor/codex_executor_0 (round1) [succeeded]",
                '"provider": "codex"',
            ),
        },
        id="compact-linear-dashboard",
    ),
    pytest.param(
        {
            "case_id": "compact-namespaced-dashboard",
            "build_workflow": build_compact_namespaced_workflow,
            "snapshot_event_type": "workflow_finished",
            "selected_node_id": "auth.plan",
            "expected_left_fragments": (
                "DAG Summary",
                "▸● auth.plan",
                "summary.final",
            ),
            "expected_right_fragments": (
                "Node Output: auth.plan",
                "alpha/executor/alpha_executor_0 (round1) [succeeded]",
                '"output_mode": "echo"',
            ),
        },
        id="compact-namespaced-dashboard",
    ),
]


@pytest.mark.parametrize("case_data", COMPACT_RUNTIME_CASES)
def test_compact_runtime_renders_real_execution_snapshots(
    tmp_path: Path,
    run_visualization_case,
    case_data: dict[str, object],
) -> None:
    run_result = run_visualization_case(tmp_path, case_data)
    left_text, right_text = render_compact_dashboard(run_result)

    for fragment in case_data["expected_left_fragments"]:
        assert fragment in left_text
    for fragment in case_data["expected_right_fragments"]:
        assert fragment in right_text


def test_compact_runtime_dashboard_help_mentions_log_inspect_mode(
    tmp_path: Path,
    run_visualization_case,
) -> None:
    case_data = {
        "case_id": "compact-linear-help-text",
        "build_workflow": build_compact_linear_workflow,
        "snapshot_event_type": "workflow_finished",
        "selected_node_id": "design.iteration",
        "expected_left_fragments": (),
        "expected_right_fragments": (),
    }

    run_result = run_visualization_case(tmp_path, case_data)
    left_text, _ = render_compact_dashboard(run_result)

    assert "[Enter] inspect log" in left_text


def test_compact_runtime_inspect_mode_preserves_right_pane_and_updates_title(
    tmp_path: Path,
    run_visualization_case,
) -> None:
    case_data = {
        "case_id": "compact-linear-inspect-mode",
        "build_workflow": build_compact_linear_workflow,
        "snapshot_event_type": "workflow_finished",
        "selected_node_id": "design.iteration",
        "expected_left_fragments": (),
        "expected_right_fragments": (),
    }

    run_result = run_visualization_case(tmp_path, case_data)
    runtime = SimulatedTmuxRuntime()
    runtime.start(
        RunContext(
            workflow_topology=topology_from_workflow(run_result.workflow),
            run_id="compact-runtime-inspect",
            refresh_per_second=0,
        )
    )
    try:
        node_index = ordered_node_ids(run_result.selected_snapshot).index(
            run_result.selected_node_id
        )
        runtime.runtime_files.selection_index.write_text(
            str(node_index), encoding="utf-8"
        )
        runtime.on_snapshot(None, run_result.selected_snapshot)
        runtime.refresh_once()

        runtime.calls.clear()
        runtime.runtime_files.mode.write_text("inspect", encoding="utf-8")
        runtime.runtime_files.inspect_node_id.write_text(
            run_result.selected_node_id,
            encoding="utf-8",
        )
        runtime.runtime_files.right_content.write_text(
            "inspection-active", encoding="utf-8"
        )
        runtime.refresh_once()

        left_text = runtime.runtime_files.left_content.read_text(encoding="utf-8")
        right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")
        pane_title_writes = [
            args
            for args, _, _ in runtime.calls
            if len(args) >= 6
            and args[:3] == ["set-option", "-p", "-t"]
            and args[4] == "@orchestrator_title"
        ]

        assert "[Log Inspect] [Esc] return  [q] quit run" in left_text
        assert right_text == "inspection-active"
        assert any(
            args[5] == f"Node Log: {run_result.selected_node_id}"
            for args in pane_title_writes
        )
    finally:
        runtime.stop(RunResult(failed=False))


def test_compact_runtime_live_tmux_startup_uses_short_script_backed_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = build_compact_linear_workflow(tmp_path)
    fake_tmux_path = tmp_path / "fake-tmux"
    fake_tmux_log_path = tmp_path / "fake-tmux-log.jsonl"
    write_fake_tmux_executable(fake_tmux_path)

    config = load_integration_config(tmp_path, fake_tmux_path)
    warnings: list[str] = []
    components = build_runtime_components(
        config=config,
        workflow_topology=topology_from_workflow(workflow),
        orchestrator_dir=tmp_path / ".orchestrator",
        project_root=tmp_path,
        console=Console(
            file=io.StringIO(),
            force_terminal=True,
            color_system=None,
            width=120,
        ),
        no_live=False,
        warning_sink=warnings.append,
        which_fn=lambda executable: (
            executable if executable == str(fake_tmux_path) else None
        ),
    )

    assert len(components.observers) == 1

    monkeypatch.setenv("FAKE_TMUX_LOG", fake_tmux_log_path.as_posix())
    monkeypatch.setenv(
        "FAKE_TMUX_BIND_ARG_THRESHOLD",
        str(FAKE_TMUX_BIND_ARG_THRESHOLD),
    )
    try:
        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id=components.artifact_store.run_id,
            observers=components.observers,
            refresh_per_second=0,
            warning_sink=warnings.append,
        ) as hub:
            assert hub.active_observer_count == 1
            plan, secret_context = compile_plan_for_components(
                config=config,
                workflow=workflow,
                components=components,
                project_root=tmp_path,
            )
            asyncio.run(
                execute_workflow(
                    plan=plan,
                    output=components.artifact_store,
                    invoker=components.base_invoker,
                    secret_context=secret_context,
                    event_sink=hub.emit,
                    run_id=components.artifact_store.run_id,
                    suppress_progress_output=True,
                )
            )
    finally:
        monkeypatch.delenv("FAKE_TMUX_LOG", raising=False)
        monkeypatch.delenv("FAKE_TMUX_BIND_ARG_THRESHOLD", raising=False)

    assert not warnings

    recorded_commands = read_fake_tmux_commands(fake_tmux_log_path)
    bind_key_commands = [
        command for command in recorded_commands if command and command[0] == "bind-key"
    ]
    assert bind_key_commands
    assert all(
        max(len(arg) for arg in command) <= FAKE_TMUX_BIND_ARG_THRESHOLD
        for command in bind_key_commands
    )
    bind_key_args = [arg for command in bind_key_commands for arg in command]
    assert any("inspect-enter.sh" in arg for arg in bind_key_args)
    assert any("inspect-exit.sh" in arg for arg in bind_key_args)
    assert not any(
        'respawn-pane -k -t "$right_pane" tail -n +1 -F "$log_path"' in arg
        for arg in bind_key_args
    )

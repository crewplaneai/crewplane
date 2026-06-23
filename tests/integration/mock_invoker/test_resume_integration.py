from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

import crewplane.cli.app as cli
from crewplane.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import ConsoleFactory


def _write_config(path: Path, fixture_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": SCHEMA_VERSION,
                "agents": {
                    "alpha": {
                        "cli_cmd": [sys.executable],
                        "default_model": "model-a",
                    }
                },
                "settings": {
                    "integrations": {
                        "invoker": {
                            "implementation": "mock",
                            "options": {
                                "observation_delay_seconds": 0,
                                "output_mode": "file",
                                "output_dir": str(fixture_dir),
                                "strict_file_mode": True,
                            },
                        },
                        "ui": {"implementation": "none", "options": {}},
                        "artifacts": {
                            "implementation": "filesystem",
                            "options": {
                                "allowed_template_paths": [],
                                "log_cli_output": True,
                            },
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_workflow(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Mock Resume",
                "nodes:",
                "  - id: a",
                "    mode: sequential",
                "    providers: [alpha]",
                "  - id: b",
                "    mode: sequential",
                "    needs: [a]",
                "    providers: [alpha]",
                "---",
                "",
                "## a",
                "",
                "Generate A.",
                "",
                "## b",
                "",
                "Use {{a.output}}.",
            ]
        ),
        encoding="utf-8",
    )


def _write_review_loop_workflow(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Mock Review Resume",
                "nodes:",
                "  - id: review.iterate",
                "    mode: sequential",
                "    depth: 1",
                "    audit_rounds: 2",
                "    providers:",
                "      - provider: alpha",
                "        role: executor",
                "      - provider: alpha",
                "        role: reviewer",
                "  - id: after",
                "    mode: sequential",
                "    needs: [review.iterate]",
                "    providers: [alpha]",
                "---",
                "",
                "## review.iterate",
                "",
                "Review this implementation.",
                "",
                "## after",
                "",
                "Use {{review.iterate.output}}.",
            ]
        ),
        encoding="utf-8",
    )


def _write_fixture(fixture_dir: Path, node_id: str, content: str) -> None:
    fixture_path = fixture_dir / node_id / "alpha_executor_0_round1.md"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(content, encoding="utf-8")


def _write_review_loop_fixtures(fixture_dir: Path) -> None:
    review_dir = fixture_dir / "review.iterate" / "review-audit-round-1"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "alpha_executor_0_round1.md").write_text(
        "Reviewed implementation candidate.\n",
        encoding="utf-8",
    )
    (review_dir / "reviewer-round-1.md").write_text(
        "\n".join(
            [
                "Review accepted.",
                "",
                "## Major Issues",
                "None",
                "",
                "## Minor Issues",
                "None",
                "",
                "## Nitpicks",
                "None",
                "",
                "---",
                "VERDICT: NO_FINDINGS",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _run_dirs(root: Path) -> list[Path]:
    stages_root = root / ".crewplane" / "execution-stages"
    return sorted(path for path in stages_root.iterdir() if path.is_dir())


def _manifest(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "manifests" / "run.json").read_text(encoding="utf-8"))


def _event_records(run_dir: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (run_dir / "logs" / "events.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_cli_rerun_resumes_node_boundary_with_builtin_mock_invoker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".crewplane" / "config.yml"
    workflow_path = tmp_path / ".crewplane" / "workflows" / "resume.task.md"
    fixture_dir = tmp_path / "fixtures"
    console_stream = io.StringIO()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "Console",
        ConsoleFactory(
            file=console_stream,
            force_terminal=False,
            color_system=None,
            width=120,
        ),
    )
    _write_config(config_path, fixture_dir)
    _write_workflow(workflow_path)
    _write_fixture(fixture_dir, "a", "A result\n")

    with pytest.raises(RuntimeError, match="could not resolve fixture"):
        cli.run(
            tasks_file=workflow_path,
            config_file=config_path,
            dry_run=False,
            force=False,
            no_live=True,
        )
    first_run = _run_dirs(tmp_path)[0]
    assert _manifest(first_run)["status"] == "failed"

    _write_fixture(fixture_dir, "b", "B result\n")
    cli.run(
        tasks_file=workflow_path,
        config_file=config_path,
        dry_run=False,
        force=False,
        no_live=True,
    )

    first_run, second_run = _run_dirs(tmp_path)
    first_manifest = _manifest(first_run)
    second_manifest = _manifest(second_run)
    first_results_dir = tmp_path / ".crewplane" / "execution-results" / first_run.name
    second_results_dir = tmp_path / ".crewplane" / "execution-results" / second_run.name

    assert second_run.name != first_run.name
    assert second_manifest["status"] == "succeeded"
    assert second_manifest["resumed_nodes"] == ["a"]
    assert any(
        record.get("event_type") == "runtime_log"
        and record.get("operation") == "node_resumed"
        and record.get("node_id") == "a"
        for record in _event_records(second_run)
    )
    assert (
        json.loads((second_run / "a" / "resume-source.json").read_text("utf-8"))[
            "source_run_id"
        ]
        == first_manifest["run_id"]
    )
    assert (second_results_dir / "a-result.md").read_text("utf-8") == (
        first_results_dir / "a-result.md"
    ).read_text("utf-8")
    assert "A result" in (second_results_dir / "a-result.md").read_text("utf-8")
    assert "B result" in (second_results_dir / "b-result.md").read_text("utf-8")
    assert not (second_run / "a" / "alpha_executor_0_round1.md").exists()
    assert not (second_run / "a" / "logs").exists()
    assert (second_run / "b" / "alpha_executor_0_round1.md").exists()
    assert (
        second_run / "b" / "logs" / "alpha" / "alpha-executor-0-round1.log"
    ).exists()


def test_cli_rerun_resumes_completed_review_loop_node_boundary_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".crewplane" / "config.yml"
    workflow_path = tmp_path / ".crewplane" / "workflows" / "review-resume.task.md"
    fixture_dir = tmp_path / "fixtures"
    console_stream = io.StringIO()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "Console",
        ConsoleFactory(
            file=console_stream,
            force_terminal=False,
            color_system=None,
            width=120,
        ),
    )
    _write_config(config_path, fixture_dir)
    _write_review_loop_workflow(workflow_path)
    _write_review_loop_fixtures(fixture_dir)

    with pytest.raises(RuntimeError, match="could not resolve fixture"):
        cli.run(
            tasks_file=workflow_path,
            config_file=config_path,
            dry_run=False,
            force=False,
            no_live=True,
        )
    first_run = _run_dirs(tmp_path)[0]
    first_results_dir = tmp_path / ".crewplane" / "execution-results" / first_run.name
    assert _manifest(first_run)["status"] == "failed"
    assert (
        first_run / "review.iterate" / "review-state" / "review-loop-status.json"
    ).exists()
    assert (first_run / "review.iterate" / "review-audit-round-1").exists()
    assert "Reviewed implementation candidate" in (
        first_results_dir / "review.iterate-result.md"
    ).read_text("utf-8")

    _write_fixture(fixture_dir, "after", "After result\n")
    cli.run(
        tasks_file=workflow_path,
        config_file=config_path,
        dry_run=False,
        force=False,
        no_live=True,
    )

    first_run, second_run = _run_dirs(tmp_path)
    second_results_dir = tmp_path / ".crewplane" / "execution-results" / second_run.name
    second_manifest = _manifest(second_run)
    resumed_review_stage = second_run / "review.iterate"

    assert first_run.name != second_run.name
    assert second_manifest["status"] == "succeeded"
    assert second_manifest["resumed_nodes"] == ["review.iterate"]
    assert (
        json.loads((resumed_review_stage / "resume-source.json").read_text("utf-8"))[
            "source_run_id"
        ]
        == _manifest(first_run)["run_id"]
    )
    assert (second_results_dir / "review.iterate-result.md").read_text("utf-8") == (
        first_results_dir / "review.iterate-result.md"
    ).read_text("utf-8")
    assert "After result" in (second_results_dir / "after-result.md").read_text("utf-8")
    assert any(
        record.get("event_type") == "runtime_log"
        and record.get("operation") == "node_resumed"
        and record.get("node_id") == "review.iterate"
        for record in _event_records(second_run)
    )
    assert not (resumed_review_stage / "review-state").exists()
    assert not (resumed_review_stage / "review-audit-round-1").exists()
    assert not (resumed_review_stage / "logs").exists()
    assert not (resumed_review_stage / "alpha_executor_0_round1.md").exists()
    assert (second_run / "after" / "alpha_executor_0_round1.md").exists()

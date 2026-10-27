import json

import pytest
import yaml
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.core.review_contract import ParsedReviewResult, render_review_contract
from crewplane.version import SCHEMA_VERSION
from tests.helpers.mock_resume import write_mock_config


def review(verdict, nitpicks="None"):
    return render_review_contract(ParsedReviewResult(verdict, "None", "None", nitpicks))


@pytest.mark.parametrize(
    ("raw", "original", "normalized", "approved"),
    [
        ("I would not say approved.", None, None, False),
        ("Approved if tests pass.", None, None, False),
        ("LGTM?", None, None, False),
        ('"approved"', None, None, False),
        ("```\nLGTM\n```", None, None, False),
        ("LGTM overall. One optional nitpick: rename the helper.", None, None, False),
        (
            review("CHANGES_REQUESTED", "- Rename it."),
            "CHANGES_REQUESTED",
            "CHANGES_REQUESTED",
            False,
        ),
        (review("CHANGES_REQUESTED"), "CHANGES_REQUESTED", "CHANGES_REQUESTED", False),
        ("LGTM!", None, "NO_FINDINGS", True),
        (review("NITS_ONLY", "- Rename it."), "NITS_ONLY", "NITS_ONLY", True),
    ],
)
def test_cli_gate_requires_explicit_approval(
    tmp_path, monkeypatch, raw, original, normalized, approved
):
    _run_gate(tmp_path, monkeypatch, raw, original, normalized, approved, "fatal")


def test_configured_continuation_does_not_claim_consensus(tmp_path, monkeypatch):
    _run_gate(
        tmp_path, monkeypatch, "Approved if tests pass.", None, None, False, "continue"
    )


def _run_gate(root, monkeypatch, raw, original, normalized, approved, policy):
    monkeypatch.chdir(root)
    config = root / ".crewplane/config.yml"
    fixtures = root / "fixtures"
    write_mock_config(config, fixtures)
    data = yaml.safe_load(config.read_text())
    data["settings"]["sequential_consensus_on_exhaustion"] = policy
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    workflow = root / "workflow.task.md"
    workflow.write_text(
        f'''---
schema_version: "{SCHEMA_VERSION}"
name: Approval gate
nodes:
  - id: review
    mode: sequential
    depth: 1
    providers:
      - provider: alpha
        role: executor
      - provider: alpha
        role: reviewer
  - id: after
    mode: sequential
    needs: [review]
    providers: [alpha]
---
## review
Review a candidate.
## after
Use {{{{review.output}}}}.
''',
        encoding="utf-8",
        newline="\n",
    )
    (fixtures / "review").mkdir(parents=True)
    (fixtures / "review/reviewer.md").write_bytes(raw.encode())
    for round_num in (1, 2):
        (fixtures / f"review/alpha_executor_0_round{round_num}.md").write_text(
            f"Candidate {round_num}.\n", encoding="utf-8"
        )
    (fixtures / "after.md").write_text("Dependent ran.\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["run", "--tasks", str(workflow), "--no-live"])
    succeeds = approved or policy == "continue"
    assert result.exit_code == (0 if succeeds else 1), result.output
    (run_dir,) = (root / ".crewplane/execution-stages").iterdir()
    metadata = list((run_dir / "review").rglob("*.review.json"))
    assert metadata
    for path in metadata:
        payload = json.loads(path.read_bytes())
        assert payload["original_verdict"] == original
        assert payload["normalized_verdict"] == normalized
        assert payload["approved"] is approved
        assert path.with_suffix("").with_suffix(".raw.txt").read_bytes() == raw.encode()
    status = json.loads(
        (run_dir / "review/review-state/review-loop-status.json").read_bytes()
    )
    assert status["consensus_reached"] is approved
    if not approved:
        assert status["stop_reason"] == "consensus_exhausted"
        assert status["continued_after_consensus_exhaustion"] is (policy == "continue")
    manifest = json.loads((run_dir / "manifests/run.json").read_bytes())
    assert manifest["status"] == ("succeeded" if succeeds else "failed")
    assert (run_dir / "after/alpha_executor_0_round1.md").exists() is succeeds
    assert (
        root / ".crewplane/execution-results" / run_dir.name / "after-result.md"
    ).exists() is succeeds

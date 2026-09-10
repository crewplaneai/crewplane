import json
import re
import subprocess

import yaml

from tests.unit.packaging.release_surfaces_support import (
    REPOSITORY_URL,
    ROOT,
    load_pyproject,
    read_text,
    repo_path,
)

GRANDFATHERED_LARGE_FILE_LIMITS = {
    ".github/crewplane-splash.png": 1_093_755,
    "docs/images/concepts/control-plane.png": 1_664_884,
    "docs/images/concepts/different-design.png": 1_466_376,
    "docs/images/concepts/why-crewplane.png": 1_511_410,
}


def test_large_file_hook_enforces_limit_with_narrow_grandfathering() -> None:
    config = yaml.safe_load(read_text(".pre-commit-config.yaml"))
    hooks = [
        hook
        for repository in config["repos"]
        for hook in repository["hooks"]
        if hook["id"] == "check-added-large-files"
    ]
    assert len(hooks) == 1
    hook = hooks[0]
    assert hook["args"] == ["--maxkb=1024", "--enforce-all"]

    exclusion = re.compile(hook["exclude"])
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    tracked_paths = {path for path in tracked if path}
    oversized = {
        path
        for path in tracked_paths
        if (ROOT / path).is_file() and (ROOT / path).stat().st_size > 1024**2
    }
    excluded = {path for path in tracked_paths if exclusion.search(path)}

    grandfathered = set(GRANDFATHERED_LARGE_FILE_LIMITS)
    assert oversized == grandfathered
    assert excluded == grandfathered
    for path, size_limit in GRANDFATHERED_LARGE_FILE_LIMITS.items():
        assert (ROOT / path).stat().st_size <= size_limit
    ci_workflow = read_text(".github", "workflows", "ci.yml")
    assert re.search(
        r"uvx pre-commit==[0-9]+\.[0-9]+\.[0-9]+ run --all-files",
        ci_workflow,
    )


def test_pre_commit_hooks_are_immutable_and_dependabot_managed() -> None:
    pre_commit_text = read_text(".pre-commit-config.yaml")
    pre_commit = yaml.safe_load(pre_commit_text)
    hook_repository = next(
        repository
        for repository in pre_commit["repos"]
        if repository["repo"] == "https://github.com/pre-commit/pre-commit-hooks"
    )

    hook_revision = hook_repository["rev"]
    assert isinstance(hook_revision, str)
    assert re.fullmatch(r"[0-9a-f]{40}", hook_revision)
    assert re.search(
        rf"^\s+rev:\s+{re.escape(hook_revision)}\s+"
        r"# frozen: v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\s*$",
        pre_commit_text,
        re.MULTILINE,
    )

    dependabot = yaml.safe_load(read_text(".github", "dependabot.yml"))
    pre_commit_updates = [
        update
        for update in dependabot["updates"]
        if update["package-ecosystem"] == "pre-commit"
    ]
    assert len(pre_commit_updates) == 1
    assert pre_commit_updates[0]["directory"] == "/"
    assert {"dependencies", "status: needs-triage", "area: ci"} <= set(
        pre_commit_updates[0]["labels"]
    )


def test_ruff_lint_rejects_debugger_calls() -> None:
    ruff_lint = load_pyproject()["tool"]["ruff"]["lint"]

    assert "T10" in ruff_lint["select"]


def test_label_automation_uses_declared_labels() -> None:
    labels = json.loads(read_text(".github", "labels.json"))
    label_names = [label["name"] for label in labels]
    assert len(label_names) == len(set(label_names))
    declared_labels = set(label_names)

    template_labels: set[str] = set()
    template_paths = [
        *repo_path(".github", "ISSUE_TEMPLATE").glob("*.yml"),
        *repo_path(".github", "DISCUSSION_TEMPLATE").glob("*.yml"),
    ]
    for template_path in template_paths:
        template = yaml.safe_load(template_path.read_text(encoding="utf-8"))
        template_labels.update(template.get("labels", []))

    path_labels = yaml.safe_load(read_text(".github", "labeler.yml"))
    referenced_labels = {
        "status: needs-triage",
        "dependencies",
        *template_labels,
        *path_labels,
    }
    assert referenced_labels <= declared_labels
    packaging_globs = path_labels["area: packaging"][0]["changed-files"][0][
        "any-glob-to-any-file"
    ]
    assert "packaging/**" in packaging_globs

    triage = read_text(".github", "workflows", "issue-triage.yml")
    assert 'item.user?.login === "dependabot[bot]"' not in triage
    assert 'item.user?.type === "Bot"' not in triage
    assert "github.event_name != 'pull_request_target'" in triage
    assert "github.event.pull_request.user.login != 'dependabot[bot]'" in triage

    pr_labeler = read_text(".github", "workflows", "pr-labeler.yml")
    assert "github.event.pull_request.user.login != 'dependabot[bot]'" in pr_labeler

    dependabot = yaml.safe_load(read_text(".github", "dependabot.yml"))
    for update in dependabot["updates"]:
        assert {"dependencies", "status: needs-triage"} <= set(update["labels"])

    sync = read_text(".github", "workflows", "sync-labels.yml")
    assert 'branches: ["master"]' in sync
    assert "inputs.prune || 'false'" in sync


def test_manual_label_sync_fails_outside_master() -> None:
    workflow = yaml.safe_load(read_text(".github", "workflows", "sync-labels.yml"))
    steps = workflow["jobs"]["sync-labels"]["steps"]
    guard = steps[0]

    assert guard["name"] == "Reject non-master runs"
    assert guard["if"] == "github.ref != 'refs/heads/master'"
    assert "exit 1" in guard["run"]
    assert steps[1]["uses"].startswith("actions/checkout@")


def test_questions_and_usage_help_are_routed_to_discussions() -> None:
    assert not repo_path(".github", "ISSUE_TEMPLATE", "question.yml").exists()

    issue_config = yaml.safe_load(read_text(".github", "ISSUE_TEMPLATE", "config.yml"))
    discussions_links = [
        link
        for link in issue_config["contact_links"]
        if link["url"] == f"{REPOSITORY_URL}/discussions"
    ]
    assert len(discussions_links) == 1
    assert "questions" in discussions_links[0]["about"].lower()
    assert "usage help" in discussions_links[0]["about"].lower()

    labels = json.loads(read_text(".github", "labels.json"))
    assert "type: question" not in {label["name"] for label in labels}


def test_bug_report_requires_support_environment_details() -> None:
    bug_report = yaml.safe_load(
        read_text(".github", "ISSUE_TEMPLATE", "bug_report.yml")
    )
    fields = {field["id"]: field for field in bug_report["body"] if "id" in field}
    required_fields = {
        "os": "Operating system",
        "shell": "Shell",
        "python": "Python version",
        "install_method": "Installation method",
        "provider_invoker": "Provider CLI or invoker",
        "live_mode": "Live mode",
    }

    for field_id, label in required_fields.items():
        assert fields[field_id]["attributes"]["label"] == label
        assert fields[field_id]["validations"]["required"] is True

    privacy_guidance = fields["logs"]["attributes"]["description"].lower()
    for protected_detail in ("secrets", "tokens", "customer data", "provider payloads"):
        assert protected_detail in privacy_guidance
    safety_checks = fields["safety"]["attributes"]["options"]
    assert all(option["required"] is True for option in safety_checks)


def test_repository_gitattributes_preserve_blob_exact_workspace_compatibility() -> None:
    attributes = read_text(".gitattributes")
    policy_lines = {
        line.strip()
        for line in attributes.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "* -text" in policy_lines
    assert "text=" not in attributes
    assert " eol=" not in attributes
    assert " crlf=" not in attributes
    assert "working-tree-encoding" not in attributes


def test_dependency_review_covers_python_and_npm_manifests() -> None:
    workflow = yaml.load(
        read_text(".github", "workflows", "dependency-review.yml"),
        Loader=yaml.BaseLoader,
    )
    paths = set(workflow["on"]["pull_request"]["paths"])

    assert {"pyproject.toml", "uv.lock", "packaging/npm/package*.json"} <= paths

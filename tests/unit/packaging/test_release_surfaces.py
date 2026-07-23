import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PACKAGE_NAME = "crewplane"
AUTHORED_VERSION = str(PYPROJECT["project"]["version"])
NORMALIZED_VERSION = str(Version(AUTHORED_VERSION))
CLI_COMMAND = "crewplane"
IMPORT_PACKAGE = "crewplane"
REPOSITORY_URL = "https://github.com/crewplaneai/crewplane"
GRANDFATHERED_LARGE_FILE_LIMITS = {
    ".github/crewplane-splash.png": 1_093_755,
    "docs/images/concepts/control-plane.png": 1_664_884,
    "docs/images/concepts/different-design.png": 1_466_376,
    "docs/images/concepts/why-crewplane.png": 1_511_410,
}


def parse_requirement_map(requirements: list[str]) -> dict[str, Requirement]:
    return {
        Requirement(requirement).name: Requirement(requirement)
        for requirement in requirements
    }


def has_lower_bound(requirement: Requirement) -> bool:
    return any(spec.operator in {">", ">="} for spec in requirement.specifier)


def repo_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def read_text(*parts: str) -> str:
    return repo_path(*parts).read_text(encoding="utf-8")


def load_pyproject() -> dict[str, object]:
    return PYPROJECT


def load_uv_lock() -> dict[str, object]:
    return tomllib.loads(read_text("uv.lock"))


def load_npm_package() -> dict[str, object]:
    return json.loads(read_text("packaging", "npm", "package.json"))


def make_target_body(target: str) -> str:
    makefile = read_text("Makefile")
    match = re.search(
        rf"^{re.escape(target)}:.*?(?=^[A-Za-z0-9_.-]+:|\Z)",
        makefile,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def test_python_distribution_metadata_reserves_crewplane_name() -> None:
    pyproject = load_pyproject()
    project = pyproject["project"]
    assert project["name"] == PACKAGE_NAME
    assert project["version"] == AUTHORED_VERSION
    assert str(Version(project["version"])) == NORMALIZED_VERSION
    assert project["license"] == "Apache-2.0"

    scripts = project["scripts"]
    assert scripts == {CLI_COMMAND: "crewplane.cli.app:app"}

    urls = project["urls"]
    assert urls["Repository"] == REPOSITORY_URL
    assert urls["Issues"] == f"{REPOSITORY_URL}/issues"
    assert urls["Documentation"] == f"{REPOSITORY_URL}/blob/master/docs/index.md"

    build_system = pyproject["build-system"]
    assert build_system["requires"] == ["hatchling==1.30.1"]
    assert build_system["build-backend"] == "hatchling.build"

    wheel_config = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel_config["packages"] == [f"src/{IMPORT_PACKAGE}"]

    sdist_config = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sdist_config["only-include"] == [
        "LICENSE",
        "README.md",
        "pyproject.toml",
        f"src/{IMPORT_PACKAGE}",
    ]
    assert "include" not in sdist_config

    dev_dependencies = parse_requirement_map(project["optional-dependencies"]["dev"])
    assert "build" in dev_dependencies
    assert "twine" in dev_dependencies
    assert "packaging" in dev_dependencies
    assert all(
        has_lower_bound(dep)
        for dep in dev_dependencies.values()
        if dep.name in {"build", "twine", "packaging"}
    )

    dependencies = parse_requirement_map(project["dependencies"])
    assert "typer" in dependencies
    assert "click" in dependencies
    assert "shellingham" in dependencies
    assert "rich" in dependencies
    assert all(
        has_lower_bound(dep)
        for dep in dependencies.values()
        if dep.name in {"typer", "click", "shellingham", "rich"}
    )
    assert not dependencies["typer"].extras


def test_uv_lock_tracks_editable_crewplane_package() -> None:
    lock = load_uv_lock()
    editable_packages = [
        package
        for package in lock["package"]
        if package.get("source") == {"editable": "."}
    ]
    assert len(editable_packages) == 1

    package = editable_packages[0]
    assert package["name"] == PACKAGE_NAME
    assert package["version"] == NORMALIZED_VERSION

    dev_dependencies = {
        dependency["name"] for dependency in package["optional-dependencies"]["dev"]
    }
    assert {"build", "packaging", "pytest", "ruff", "twine"} <= dev_dependencies


def test_makefile_delegates_release_targets_to_release_tool() -> None:
    makefile = read_text("Makefile")
    assert "RUN_RELEASE = $(RUN_PYTHON) scripts/release.py" in makefile
    assert "packaging/release_checks.py" not in makefile
    expected_delegations = {
        "release-prepare": "prepare",
        "release-check": "check",
        "release-confirm": "confirm",
        "release-pypi": "publish-pypi --execute",
        "release-npm": "publish-npm --execute",
    }
    for target, command in expected_delegations.items():
        assert f"$(RUN_RELEASE) {command}" in make_target_body(target)
    release = make_target_body("release")
    assert "$(MAKE) release-pypi" in release
    assert "$(MAKE) release-npm" in release
    assert "$(RUN_RELEASE) finalize --execute" in release


def test_legacy_release_check_helper_was_replaced() -> None:
    assert not repo_path("packaging", "release_checks.py").exists()


def test_release_script_exposes_stateful_commands() -> None:
    result = subprocess.run(
        [sys.executable, str(repo_path("scripts", "release.py")), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    for command in (
        "prepare",
        "release-artifacts",
        "check",
        "verify-complete",
        "github-release-plan",
        "publish-pypi",
        "publish-npm",
        "finalize",
    ):
        assert command in result.stdout
    assert "recover-release-artifacts" not in result.stdout
    assert "verify-backfill" not in result.stdout


def test_ci_package_job_smoke_tests_wheel_before_inspection_and_upload() -> None:
    workflow = yaml.load(
        read_text(".github", "workflows", "ci.yml"), Loader=yaml.BaseLoader
    )
    package_steps = workflow["jobs"]["package"]["steps"]
    step_positions = {
        step.get("name"): index for index, step in enumerate(package_steps)
    }
    smoke_index = step_positions["Build and smoke-test package"]
    inspect_index = step_positions["Inspect dist"]
    upload_index = step_positions["Upload dist artifact"]

    assert package_steps[smoke_index]["run"] == "make install-smoke-pip"
    assert smoke_index < inspect_index < upload_index
    assert all(step.get("run") != "uv build" for step in package_steps)


def test_production_release_workflow_reuses_release_tool_without_pypi_publish() -> None:
    workflow = read_text(".github", "workflows", "release.yml")
    workflow_config = yaml.load(workflow, Loader=yaml.BaseLoader)
    release_script = read_text("scripts", "publish_github_release.sh")

    dispatch = workflow_config["on"]["workflow_dispatch"]
    assert "push" not in workflow_config["on"]
    assert dispatch["inputs"]["tag"]["required"] == "true"
    assert dispatch["inputs"]["tag"]["type"] == "string"
    assert dispatch["inputs"]["tag"]["description"] == (
        "Tag just created by make release from the current master commit"
    )
    verify_steps = workflow_config["jobs"]["verify"]["steps"]
    master_guard = verify_steps[0]
    assert master_guard["name"] == "Reject non-master dispatch"
    assert master_guard["if"] == "github.ref != 'refs/heads/master'"
    assert "exit 1" in master_guard["run"]
    release_source_guard = verify_steps[2]
    assert release_source_guard["name"] == "Resolve and verify current release source"
    assert (
        "release_commit=\"$(git rev-parse --verify 'HEAD^{commit}')\""
        in release_source_guard["run"]
    )
    assert (
        'if [ "$release_commit" != "$GITHUB_SHA" ]; then'
        in (release_source_guard["run"])
    )
    assert (
        "Release tag must point to the dispatched master commit."
        in (release_source_guard["run"])
    )
    assert workflow.count("TAG_NAME: ${{ inputs.tag }}") == 2
    assert workflow.count("fetch-depth: 0") == 2
    assert workflow.count("git fetch --quiet --no-tags origin refs/heads/master") == 1
    assert workflow.count("git merge-base --is-ancestor") == 1
    assert "ref: refs/tags/${{ inputs.tag }}" in workflow
    assert (
        "release_commit: ${{ steps.release-source.outputs.release_commit }}" in workflow
    )
    assert "ref: ${{ needs.verify.outputs.release_commit }}" in workflow
    assert 'echo "release_commit=$release_commit" >> "$GITHUB_OUTPUT"' in workflow
    assert "group: github-release-publication" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "queue: max" in workflow
    assert workflow.count("uses: actions/checkout@") == 2
    assert "github.event.inputs" not in workflow
    assert "github.ref_name" not in workflow
    assert "path: tooling" not in workflow
    assert "path: source" not in workflow
    assert "working-directory:" not in workflow
    assert "python scripts/release.py release-artifacts" in workflow
    assert workflow.count("python scripts/release.py github-release-plan") == 1
    assert "python scripts/release.py github-release-plan" in release_script
    assert "github-release-metadata" not in workflow
    assert workflow.count("needs.verify.outputs.release_commit") == 1
    assert "steps.release-plan.outputs" not in workflow
    assert "name: release-bundle" in workflow
    assert "dist/*" in workflow
    assert ".release/npm/*.tgz" in workflow
    assert ".release/release-manifest.json" in workflow
    assert "include-hidden-files: true" in workflow
    assert "overwrite: true" in workflow
    assert "scripts/publish_github_release.sh dist" in workflow
    assert "release_flags=(--prerelease --latest=false)" in release_script
    assert "release_flags=(--prerelease=false --latest)" in release_script
    assert "release_flags=(--prerelease=false --latest=false)" in release_script
    assert '"${release_flags[@]}"' in release_script
    assert '--expected-tag "$TAG_NAME"' in workflow
    assert "recover-release-artifacts" not in workflow
    assert "verify-backfill" not in workflow
    assert "IS_BACKFILL" not in workflow
    assert 'gh release create "$tag_name" "${release_artifacts[@]}"' in release_script
    assert "release(tagName: $tag)" in release_script
    assert "nodes { name size digest }" in release_script
    assert "totalCount" in release_script
    assert 'gh release upload "$tag_name" "${release_artifacts[@]}"' in (release_script)
    assert "--clobber" in release_script
    assert 'release_artifacts=("$dist_dir"/*)' in release_script
    assert 'comm -13 "$expected_names_file" "$release_names_file"' in (release_script)
    assert 'cmp -s "$expected_assets_file" "$release_assets_file"' in release_script
    assert "Refusing to publish a draft with unexpected assets" in release_script
    assert "assets do not match the verified dist artifacts" in release_script
    assert "prerelease state does not match" in release_script
    assert "Latest state does not match" in release_script
    assert "Verified existing published GitHub Release" in release_script
    assert "query was truncated or internally inconsistent" in release_script
    assert "refusing to mutate it" in release_script
    assert 'gh release edit "$tag_name"' in release_script
    assert '--tag "$tag_name"' in release_script
    assert "--draft=false" in release_script
    assert "--verify-tag" in release_script
    assert "GH_REPO: ${{ github.repository }}" in workflow
    assert '--repo "$repository"' in release_script
    assert workflow.count("contents: write") == 1
    assert "uv build" not in workflow
    assert "urllib.request" not in workflow
    assert "pypa/gh-action-pypi-publish" not in workflow
    assert "id-token: write" not in workflow


def test_release_drafter_was_removed() -> None:
    assert not repo_path(".github", "release-drafter.yml").exists()
    assert not repo_path(".github", "workflows", "release-drafter.yml").exists()


def test_github_workflow_actions_are_pinned_to_commits() -> None:
    workflows = sorted(repo_path(".github", "workflows").glob("*.yml"))
    for workflow in workflows:
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "uses:" not in line:
                continue
            action = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action), (
                f"{workflow.relative_to(ROOT)}:{line_number} is not commit-pinned"
            )


def test_github_workflow_uv_installs_are_version_pinned() -> None:
    workflows = sorted(repo_path(".github", "workflows").glob("*.yml"))
    for workflow in workflows:
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if "astral-sh/setup-uv@" not in line:
                continue
            step = "\n".join(lines[line_number - 1 : line_number + 6])
            assert 'version: "0.10.9"' in step, (
                f"{workflow.relative_to(ROOT)}:{line_number} does not pin uv"
            )


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
    assert "pre-commit==4.6.0 run --all-files" in read_text(
        ".github", "workflows", "ci.yml"
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
    assert "**Questions and ideas:** use GitHub Discussions." in read_text("SUPPORT.md")


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


def test_release_docs_describe_single_production_publish_path() -> None:
    development = read_text("DEVELOPMENT.md")
    contributing = read_text("CONTRIBUTING.md")
    normalized_development = " ".join(development.split())
    normalized_contributing = " ".join(contributing.split())

    assert "Production publishing is local-only." in development
    assert "Maintainers run `make release`" in normalized_development
    assert "does not publish production PyPI or npm packages" in normalized_development
    assert "does not need PyPI or npm credentials" in normalized_development
    assert "manually dispatched GitHub Release automation" in normalized_development
    assert "required `tag` input" in normalized_development
    assert "rejects dispatches outside `refs/heads/master`" in normalized_development
    assert "manual `make release` flow is the production source of truth" in (
        normalized_development
    )
    assert "second phase of the same release operation" in normalized_development
    assert (
        "requires it to match the master commit that dispatched the workflow exactly"
        in normalized_development
    )
    assert (
        "Historical-tag backfills and new dispatches after `master` advances are unsupported"
        in normalized_development
    )
    assert "exact Hatchling build-system pin" in normalized_development
    assert "offline runtime wheelhouse" in normalized_development
    assert "GitHub Release itself contains only `dist/*`" in (normalized_development)
    assert "New releases are built as drafts, verified, published" in (
        normalized_development
    )
    assert "exact name, size, and GitHub's immutable upload-time SHA-256 digest" in (
        normalized_development
    )
    assert "highest published stable version on PyPI" in normalized_development
    assert "does not update the Homebrew tap" in development
    assert "TestPyPI Trusted Publishing workflow" in normalized_development
    assert "dispatch it from any selected ref" in normalized_development
    assert "not restricted to `master`" in normalized_development
    assert "delayed GitHub Release" not in normalized_development
    assert "stale attempts for older releases" not in normalized_development
    assert "PyPI Trusted Publishing through GitHub OIDC" not in development
    assert "scripts/release.py release-artifacts" in contributing
    assert "scripts/release.py github-release-plan" in contributing
    assert "recover-release-artifacts" not in contributing
    assert "verify-backfill" not in contributing
    assert "exact Hatchling build-system pin" in normalized_contributing
    assert "offline runtime wheelhouse" in normalized_contributing
    assert "exact asset names, sizes, and GitHub SHA-256 digests" in (
        normalized_contributing
    )
    assert "never mutates an already-published mismatch" in (normalized_contributing)
    assert "highest published stable version on PyPI" in normalized_contributing
    assert "checks out the requested tag" in normalized_contributing
    assert (
        "requires it to match the master commit that dispatched the workflow exactly"
        in normalized_contributing
    )
    assert (
        "Historical-tag backfills and new dispatches after `master` advances are unsupported"
        in normalized_contributing
    )
    assert "checks out the exact verified commit" in normalized_contributing
    assert "current `master` tip" not in normalized_contributing
    assert "reloads and re-verifies draft assets immediately before publication" in (
        normalized_contributing
    )
    assert "does not publish production packages" in normalized_contributing
    assert "dispatch it from any selected ref" in normalized_contributing
    assert "not restricted to `master`" in normalized_contributing
    assert "delayed GitHub Release" not in normalized_contributing
    assert "stale attempts for older releases" not in normalized_contributing


def test_repository_automation_matches_supported_platform_and_publish_policy() -> None:
    development = read_text("DEVELOPMENT.md")
    contributing = read_text("CONTRIBUTING.md")
    nightly_text = read_text(".github", "workflows", "nightly.yml")
    nightly = yaml.safe_load(nightly_text)
    testpypi = read_text(".github", "workflows", "testpypi.yml")

    for document in (development, contributing):
        normalized_document = " ".join(document.split())
        assert "Linux, macOS, and WSL" in normalized_document
        assert "Native Windows is not supported" in normalized_document
        assert (
            "supports Python 3.13+ on Linux, macOS, Windows" not in normalized_document
        )

    assert nightly["jobs"]["cross-platform"]["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "macos-latest",
    ]
    assert "windows-latest" not in nightly_text
    assert "skip-existing" not in testpypi


def test_private_reporting_surfaces_use_github_security_advisories() -> None:
    issue_config = yaml.safe_load(read_text(".github", "ISSUE_TEMPLATE", "config.yml"))
    advisory_url = f"{REPOSITORY_URL}/security/advisories/new"
    security_links = [
        link for link in issue_config["contact_links"] if link["url"] == advisory_url
    ]

    assert len(security_links) == 1
    assert "privately" in security_links[0]["about"].lower()
    assert "GitHub Security Advisories" in read_text("SECURITY.md")
    assert "GitHub Security Advisories" in read_text("SUPPORT.md")
    assert advisory_url in read_text("CODE_OF_CONDUCT.md")


def test_repository_hosting_policy_uses_current_workflow_surfaces() -> None:
    assert not repo_path(".github", "branch-protection.json").exists()
    assert not repo_path(".github", "settings.yml").exists()

    testpypi = yaml.safe_load(read_text(".github", "workflows", "testpypi.yml"))
    publisher = testpypi["jobs"]["publish-testpypi"]
    assert publisher["environment"]["name"] == "testpypi"
    assert publisher["permissions"] == {"id-token": "write", "contents": "read"}


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


def test_install_script_uses_uv_and_supports_local_artifact_smoke() -> None:
    installer = read_text("install.sh")
    assert 'PACKAGE_NAME="crewplane"' in installer
    assert "CLI_NAME" not in installer
    assert (
        f'CREWPLANE_VERSION="${{CREWPLANE_VERSION:-{AUTHORED_VERSION}}}"' in installer
    )
    assert "CREWPLANE_INSTALL_FIND_LINKS" in installer
    assert "CREWPLANE_INSTALL_NO_INDEX" in installer
    assert "CREWPLANE_INSTALL_HOME" in installer
    assert "CREWPLANE_INSTALL_PYTHON" in installer
    assert "tool install --force" in installer
    assert "--find-links" in installer
    assert "--no-index" in installer
    assert "tool dir --bin" in installer
    assert "export PATH=" in installer
    assert "${PACKAGE_NAME} --help" in installer
    assert "uv tool uninstall ${PACKAGE_NAME}" in installer
    assert "native Windows is not supported" in installer
    assert "First run:" in installer
    assert "${PACKAGE_NAME} run" in installer
    assert "${PACKAGE_NAME} run --no-live" not in installer
    assert "provider CLIs are not required" in installer
    assert "Real provider setup:" in installer
    assert "does not install provider CLIs" in installer
    assert (
        "does not install provider CLIs, manage provider credentials, or sandbox provider CLI execution"
        in installer
    )


def test_npm_wrapper_metadata_and_scripts_pin_python_package() -> None:
    package = load_npm_package()
    assert package["name"] == PACKAGE_NAME
    assert package["version"] == AUTHORED_VERSION
    assert package["repository"]["url"] == f"git+{REPOSITORY_URL}.git"
    assert package["bin"] == {CLI_COMMAND: "bin/crewplane.js"}
    assert package["scripts"]["postinstall"] == "node scripts/postinstall.js"
    assert package["crewplane"]["pythonPackage"] == PACKAGE_NAME
    assert package["crewplane"]["pythonPackageVersion"] == AUTHORED_VERSION
    assert package["crewplane"]["pythonConsoleCommand"] == CLI_COMMAND

    postinstall = read_text("packaging", "npm", "scripts", "postinstall.js")
    assert "CREWPLANE_VERSION" in postinstall
    assert "CREWPLANE_INSTALL_FIND_LINKS" in postinstall
    assert "CREWPLANE_INSTALL_NO_INDEX" in postinstall
    assert "CREWPLANE_INSTALL_PYTHON" in postinstall
    assert 'const DEFAULT_PYTHON = "3.13";' in postinstall
    assert "process.env.CREWPLANE_INSTALL_PYTHON || DEFAULT_PYTHON" in postinstall
    assert "ensureSupportedPlatform();" in postinstall
    assert "uv" in postinstall
    assert "venv" in postinstall
    assert "Provider CLIs and credentials are not managed" in postinstall

    shim = read_text("packaging", "npm", "bin", "crewplane.js")
    assert ".venv" in shim
    assert CLI_COMMAND in shim
    assert "native Windows is not supported" in shim
    assert "lifecycle scripts may have been disabled" in shim
    assert "process.argv.slice(2)" in shim


def test_npm_install_docs_explain_global_bin_path() -> None:
    npm_readme = read_text("packaging", "npm", "README.md")
    installation_doc = read_text("docs", "getting-started", "installation.md")
    for content in (npm_readme, installation_doc):
        assert "npm config get prefix" in content
        assert "PATH" in content
        assert "command -v crewplane" in content
        assert "node" in content
        assert "crewplane@alpha" not in content


def test_public_first_run_docs_are_mock_first_and_provider_free() -> None:
    readme = read_text("README.md")
    quickstart = read_text("docs", "getting-started", "quickstart.md")
    docs_index = read_text("docs", "index.md")

    for content in (readme, quickstart):
        assert "crewplane init" in content
        assert "crewplane validate" in content
        assert "crewplane run" in content
        assert "crewplane run --no-live" not in content
        assert "crewplane onboarding" in content
        assert "provider CLI" in content
        assert (
            "does not require" in content
            or "needs no" in content
            or "no provider CLIs" in content
        )
        assert "API key" in content
        assert content.index("crewplane run") < content.index("crewplane onboarding")
        assert content.index("crewplane run") < content.index("provider setup")

    assert "not model output" in quickstart
    assert "crewplane onboarding" in docs_index
    assert "guides/inspecting-artifacts.md" in docs_index
    assert "guides/troubleshooting.md" in docs_index
    assert "guides/reproducible-support-bundle.md" in docs_index
    assert "reference/configuration.md" in docs_index
    assert "safety/" not in docs_index
    assert "../AGENTS.md" not in docs_index
    assert "../DEVELOPMENT.md" not in docs_index
    assert "architecture/" not in docs_index
    assert "maintainers/" not in docs_index
    assert "experimental-worktree-implementation" not in docs_index


def test_launch_support_docs_cover_skip_force_resume_and_bundles() -> None:
    running = read_text("docs", "guides", "running-workflows.md")
    troubleshooting = read_text("docs", "guides", "troubleshooting.md")
    support_bundle = read_text("docs", "guides", "reproducible-support-bundle.md")
    artifacts = read_text("docs", "reference", "artifacts.md")

    for content in (running, troubleshooting, artifacts):
        assert "workflow_signature" in content
        assert "--force" in content
        assert "resume" in content.lower()

    for expected in (
        "logs/summary.md",
        "events.ndjson",
        ".crewplane/config.yml",
        "versions",
        "Redact",
    ):
        assert expected in support_bundle


def test_npm_postinstall_defaults_to_python_313_without_override(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported npm smoke surface")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm postinstall regression")

    fake_uv = tmp_path / "uv"
    fake_uv_log = tmp_path / "uv.log"
    fake_uv.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "{",
                "  printf 'CALL'",
                '  for arg in "$@"; do printf "\\t%s" "$arg"; done',
                "  printf '\\n'",
                '} >> "$CREWPLANE_FAKE_UV_LOG"',
            ]
        ),
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    for name in (
        "CREWPLANE_VERSION",
        "CREWPLANE_INSTALL_FIND_LINKS",
        "CREWPLANE_INSTALL_NO_INDEX",
        "CREWPLANE_INSTALL_PYTHON",
    ):
        env.pop(name, None)
    env["CREWPLANE_UV_BIN"] = str(fake_uv)
    env["CREWPLANE_FAKE_UV_LOG"] = str(fake_uv_log)

    subprocess.run(
        [node, str(repo_path("packaging", "npm", "scripts", "postinstall.js"))],
        cwd=ROOT,
        env=env,
        check=True,
    )

    calls = [
        line.split("\t")
        for line in fake_uv_log.read_text(encoding="utf-8").splitlines()
    ]
    assert calls[0][:4] == ["CALL", "venv", "--python", "3.13"]
    assert calls[0][4] == str(repo_path("packaging", "npm", ".venv"))
    assert calls[1][:4] == [
        "CALL",
        "pip",
        "install",
        "--python",
    ]
    assert calls[1][-1] == f"{PACKAGE_NAME}=={AUTHORED_VERSION}"


def test_npm_postinstall_rejects_native_windows_before_uv_lookup(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm postinstall regression")

    fake_uv = tmp_path / "uv"
    fake_uv_log = tmp_path / "uv.log"
    fake_uv.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "printf 'uv called\\n' >> \"$CREWPLANE_FAKE_UV_LOG\"",
            ]
        ),
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["CREWPLANE_UV_BIN"] = str(fake_uv)
    env["CREWPLANE_FAKE_UV_LOG"] = str(fake_uv_log)

    result = subprocess.run(
        [
            node,
            "-e",
            (
                "Object.defineProperty(process, 'platform', { value: 'win32' });"
                "require('./packaging/npm/scripts/postinstall.js');"
            ),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "native Windows is not supported" in result.stderr
    assert not fake_uv_log.exists()


def test_npm_bin_rejects_native_windows_before_venv_lookup() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm bin regression")

    result = subprocess.run(
        [
            node,
            "-e",
            (
                "Object.defineProperty(process, 'platform', { value: 'win32' });"
                "require('./packaging/npm/bin/crewplane.js');"
            ),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "native Windows is not supported" in result.stderr


def test_homebrew_formula_uses_normalized_python_artifact_and_virtualenv() -> None:
    formula = read_text("packaging", "homebrew", "Formula", "crewplane.rb")
    assert "class Crewplane < Formula" in formula
    assert "include Language::Python::Virtualenv" in formula
    assert f'version "{AUTHORED_VERSION}"' in formula
    assert f"crewplane-{NORMALIZED_VERSION}.tar.gz" in formula
    assert 'license "Apache-2.0"' in formula
    assert 'depends_on "python@3.13"' in formula
    assert 'depends_on "maturin" => :build' in formula
    assert 'depends_on "rust" => :build' in formula
    assert 'branch: "master"' in formula
    assert 'def python3\n    "python3.13"\n  end' in formula
    assert "virtualenv_create(libexec, python3)" in formula
    assert "venv.pip_install build_resources.map" in formula
    assert "venv.pip_install resources.reject" in formula
    assert "venv.pip_install_and_link buildpath, build_isolation: false" in formula
    assert 'shell_output("#{bin}/crewplane --help")' in formula
    assert "file://" not in formula
    assert "/home/" not in formula

    hashes = re.findall(r'sha256 "([a-f0-9]{64})"', formula)
    assert len(hashes) >= 10
    assert all(hash_value != "0" * 64 for hash_value in hashes)
    for resource in (
        "hatchling",
        "packaging",
        "pathspec",
        "pluggy",
        "trove-classifiers",
        "annotated-doc",
        "annotated-types",
        "click",
        "markdown-it-py",
        "mdurl",
        "pydantic",
        "pydantic-core",
        "pygments",
        "pyyaml",
        "rich",
        "shellingham",
        "typer",
        "typing-extensions",
        "typing-inspection",
    ):
        assert f'resource "{resource}" do' in formula
    assert formula.index('depends_on "maturin" => :build') < formula.index(
        'resource "pydantic-core" do'
    )
    for wheel in (
        "hatchling-1.30.1-py3-none-any.whl",
        "packaging-26.2-py3-none-any.whl",
        "pathspec-1.1.1-py3-none-any.whl",
        "pluggy-1.6.0-py3-none-any.whl",
        "trove_classifiers-2026.6.1.19-py3-none-any.whl",
    ):
        assert wheel in formula


def test_gitignore_contains_release_build_manifest_patterns() -> None:
    gitignore = read_text(".gitignore")
    for pattern in (
        ".release/",
        ".release-manifests/",
        "release-manifest.json",
        "release-manifest.*.json",
        "build-manifest.json",
        "build-manifest.*.json",
        "*.build-manifest.json",
        "dist/",
        "*.egg-info/",
        "!packaging/npm/scripts/",
        "!packaging/npm/scripts/**",
    ):
        assert pattern in gitignore


def test_package_surfaces_use_crewplane_command() -> None:
    pyproject = load_pyproject()
    project = pyproject["project"]
    assert project["name"] == PACKAGE_NAME
    assert project["scripts"] == {"crewplane": "crewplane.cli.app:app"}

    npm_package = load_npm_package()
    assert npm_package["bin"] == {"crewplane": "bin/crewplane.js"}

    install_script = read_text("install.sh")
    assert "CLI_NAME" not in install_script
    assert "${PACKAGE_NAME} --help" in install_script
    makefile = read_text("Makefile")
    assert "PROJECT_NAME_CMD =" in makefile
    assert "PACKAGE_NAME := $(shell $(PROJECT_NAME_CMD))" in makefile
    assert "crewplane" in read_text("packaging", "homebrew", "Formula", "crewplane.rb")

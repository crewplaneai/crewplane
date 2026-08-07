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
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PACKAGE_NAME = str(PYPROJECT["project"]["name"])
AUTHORED_VERSION = str(PYPROJECT["project"]["version"])
NORMALIZED_VERSION = str(Version(AUTHORED_VERSION))
CLI_COMMAND = "crewplane"
IMPORT_PACKAGE = "crewplane"
REPOSITORY_URL = "https://github.com/crewplaneai/crewplane"
UV_BOOTSTRAP_METADATA = json.loads(
    (ROOT / "packaging/uv-bootstrap.json").read_text(encoding="utf-8")
)
PINNED_UV_VERSION = str(UV_BOOTSTRAP_METADATA["version"])
UV_CHECKSUMS = {
    str(target): str(checksum)
    for target, checksum in UV_BOOTSTRAP_METADATA["checksums"].items()
}
GRANDFATHERED_LARGE_FILE_LIMITS = {
    ".github/crewplane-splash.png": 1_093_755,
    "docs/images/concepts/control-plane.png": 1_664_884,
    "docs/images/concepts/different-design.png": 1_466_376,
    "docs/images/concepts/why-crewplane.png": 1_511_410,
}
UV_BOOTSTRAP_CASES = (
    (
        "Darwin",
        "arm64",
        "darwin",
        "arm64",
        "aarch64-apple-darwin",
        UV_CHECKSUMS["aarch64-apple-darwin"],
        "curl",
        "gnu",
    ),
    (
        "Darwin",
        "x86_64",
        "darwin",
        "x64",
        "x86_64-apple-darwin",
        UV_CHECKSUMS["x86_64-apple-darwin"],
        "wget",
        "gnu",
    ),
    (
        "Linux",
        "aarch64",
        "linux",
        "arm64",
        "aarch64-unknown-linux-gnu",
        UV_CHECKSUMS["aarch64-unknown-linux-gnu"],
        "curl",
        "gnu",
    ),
    (
        "Linux",
        "x86_64",
        "linux",
        "x64",
        "x86_64-unknown-linux-gnu",
        UV_CHECKSUMS["x86_64-unknown-linux-gnu"],
        "wget",
        "gnu",
    ),
    (
        "Linux",
        "aarch64",
        "linux",
        "arm64",
        "aarch64-unknown-linux-musl",
        UV_CHECKSUMS["aarch64-unknown-linux-musl"],
        "curl",
        "musl",
    ),
    (
        "Linux",
        "x86_64",
        "linux",
        "x64",
        "x86_64-unknown-linux-musl",
        UV_CHECKSUMS["x86_64-unknown-linux-musl"],
        "wget",
        "musl",
    ),
)


def parse_requirement_map(requirements: list[str]) -> dict[str, Requirement]:
    return {
        Requirement(requirement).name: Requirement(requirement)
        for requirement in requirements
    }


def has_lower_bound(requirement: Requirement) -> bool:
    return any(spec.operator in {">", ">="} for spec in requirement.specifier)


def extract_python_floor_from_pyproject() -> str:
    requires_python = str(PYPROJECT["project"]["requires-python"])
    specifier_set = SpecifierSet(requires_python)
    lower_bounds = [
        Version(spec.version) for spec in specifier_set if spec.operator in {">", ">="}
    ]
    assert lower_bounds
    return str(min(lower_bounds))


def parse_formula_resources(formula: str) -> dict[str, tuple[str, str]]:
    resource_pattern = re.compile(
        r'resource "(?P<name>[^"]+)" do\s+url "(?P<url>[^"]+)"\s+'
        r'sha256 "(?P<sha>[a-f0-9]{64})"\s+end',
        re.MULTILINE,
    )
    return {
        match.group("name"): (match.group("url"), match.group("sha"))
        for match in resource_pattern.finditer(formula)
    }


def repo_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def read_text(*parts: str) -> str:
    return repo_path(*parts).read_text(encoding="utf-8")


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def link_test_command(directory: Path, command: str) -> None:
    executable = shutil.which(command)
    assert executable is not None, f"{command} is required for installer tests"
    (directory / command).symlink_to(executable)


def prepare_uv_bootstrap_environment(
    tmp_path: Path,
    downloader: str,
    kernel: str,
    machine: str,
    target: str,
    actual_sha256: str,
    libc: str,
) -> tuple[dict[str, str], Path, Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for command in ("awk", "basename", "chmod", "cp", "mkdir", "mktemp", "rm"):
        link_test_command(fake_bin, command)

    write_executable(
        fake_bin / "uname",
        """#!/bin/sh
case "${1:-}" in
    -s)
        if [ ! -e "$CREWPLANE_FAKE_PLATFORM_GATE_STATE" ]; then
            : > "$CREWPLANE_FAKE_PLATFORM_GATE_STATE"
            printf 'Darwin\n'
        else
            printf '%s\n' "$CREWPLANE_FAKE_KERNEL"
        fi
        ;;
    -m) printf '%s\n' "$CREWPLANE_FAKE_MACHINE" ;;
    *) exit 1 ;;
esac
""",
    )
    write_executable(
        fake_bin / "ldd",
        """#!/bin/sh
if [ "$CREWPLANE_FAKE_LIBC" = "musl" ]; then
    printf 'musl libc\n' >&2
else
    printf 'ldd (GNU libc) 2.39\n'
fi
""",
    )
    write_executable(
        fake_bin / "grep",
        """#!/bin/sh
case "$*" in
    *musl*) [ "$CREWPLANE_FAKE_LIBC" = "musl" ] ;;
    *) exit 1 ;;
esac
""",
    )
    write_executable(
        fake_bin / downloader,
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    exit 0
fi
output=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o|--output|-O|-qO|--output-document)
            output="$2"
            shift 2
            ;;
        --output-document=*)
            output="${1#*=}"
            shift
            ;;
        http*)
            url="$1"
            shift
            ;;
        *)
            shift
            ;;
    esac
done
[ -n "$output" ] || exit 1
printf 'archive fixture' > "$output"
printf '%s\n' "$url" >> "$CREWPLANE_FAKE_DOWNLOAD_LOG"
""",
    )
    write_executable(
        fake_bin / "sha256sum",
        """#!/bin/sh
printf '%s  %s\n' "$CREWPLANE_FAKE_ACTUAL_SHA256" "$1"
""",
    )
    write_executable(
        fake_bin / "tar",
        """#!/bin/sh
destination=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -C)
            destination="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done
[ -n "$destination" ] || exit 1
archive_dir="$destination/uv-$CREWPLANE_FAKE_UV_TARGET"
/bin/mkdir -p "$archive_dir"
/bin/cp "$CREWPLANE_FAKE_UV" "$archive_dir/uv"
/bin/cp "$CREWPLANE_FAKE_UV" "$archive_dir/uvx"
/bin/chmod 0755 "$archive_dir/uv" "$archive_dir/uvx"
""",
    )

    fake_uv = tmp_path / "fake-uv"
    write_executable(
        fake_uv,
        """#!/bin/sh
{
    printf 'CALL'
    for arg in "$@"; do printf '\t%s' "$arg"; done
    printf '\n'
} >> "$CREWPLANE_FAKE_UV_LOG"
if [ "${1:-}" = "tool" ] && [ "${2:-}" = "dir" ]; then
    printf '%s\n' "$CREWPLANE_FAKE_TOOL_BIN"
fi
""",
    )

    tool_bin = tmp_path / "tool-bin"
    tool_bin.mkdir()
    fake_cli = tool_bin / PACKAGE_NAME
    write_executable(fake_cli, "#!/bin/sh\nexit 0\n")
    (fake_bin / PACKAGE_NAME).symlink_to(fake_cli)

    install_home = tmp_path / "home"
    install_home.mkdir()
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    download_log = tmp_path / "download.log"
    uv_log = tmp_path / "uv.log"

    env = os.environ.copy()
    for name in ("CREWPLANE_UV_BIN", "UV_INSTALL_DIR", "UV_UNMANAGED_INSTALL"):
        env.pop(name, None)
    env.update(
        {
            "CREWPLANE_FAKE_ACTUAL_SHA256": actual_sha256,
            "CREWPLANE_FAKE_DOWNLOAD_LOG": str(download_log),
            "CREWPLANE_FAKE_KERNEL": kernel,
            "CREWPLANE_FAKE_LIBC": libc,
            "CREWPLANE_FAKE_MACHINE": machine,
            "CREWPLANE_FAKE_PLATFORM_GATE_STATE": str(tmp_path / "platform-gate.state"),
            "CREWPLANE_FAKE_TOOL_BIN": str(tool_bin),
            "CREWPLANE_FAKE_UV": str(fake_uv),
            "CREWPLANE_FAKE_UV_LOG": str(uv_log),
            "CREWPLANE_FAKE_UV_TARGET": target,
            "CREWPLANE_INSTALL_HOME": str(install_home),
            "HOME": str(install_home),
            "PATH": str(fake_bin),
            "TMPDIR": str(temp_dir),
        }
    )
    return env, install_home, temp_dir, download_log


def npm_bootstrap_command(
    node_platform: str,
    node_arch: str,
    mock_crypto: bool,
    node_libc: str = "gnu",
) -> str:
    statements = [
        f"Object.defineProperty(process, 'platform', {{ value: '{node_platform}' }});",
        f"Object.defineProperty(process, 'arch', {{ value: '{node_arch}' }});",
    ]
    if node_platform == "linux":
        report_header = (
            "{ glibcVersionRuntime: '2.39' }" if node_libc == "gnu" else "{}"
        )
        statements.append(
            f"process.report.getReport = () => ({{ header: {report_header} }});"
        )
    if mock_crypto:
        statements.extend(
            [
                "const Module = require('node:module');",
                "const originalLoad = Module._load;",
                "Module._load = function(request, parent, isMain) {",
                "  if (request === 'node:crypto') {",
                "    return { createHash() { return {",
                "      update() { return this; },",
                "      digest() { return process.env.CREWPLANE_FAKE_ACTUAL_SHA256; },",
                "    }; } };",
                "  }",
                "  return originalLoad.call(this, request, parent, isMain);",
                "};",
            ]
        )
    statements.append("require('./packaging/npm/scripts/postinstall.js');")
    return "".join(statements)


def workflow_step_run(job: dict[str, object], name: str) -> str:
    steps = {step.get("name"): step for step in job["steps"] if isinstance(step, dict)}
    return str(steps[name].get("run", ""))


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
    build_system_requirements = parse_requirement_map(build_system["requires"])
    hatchling_requirement = build_system_requirements["hatchling"]
    assert any(spec.operator == "==" for spec in hatchling_requirement.specifier)
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


def test_pytest_reliability_contract_is_explicit() -> None:
    pyproject = load_pyproject()
    pytest_config = pyproject["tool"]["pytest"]["ini_options"]
    assert pytest_config == {
        "minversion": "9.1",
        "testpaths": ["tests"],
        "addopts": [
            "-ra",
            "--import-mode=importlib",
            "--disable-plugin-autoload",
        ],
        "strict_config": True,
        "strict_markers": True,
        "strict_parametrization_ids": True,
        "collect_imported_tests": False,
        "empty_parameter_set_mark": "fail_at_collect",
        "tmp_path_retention_policy": "failed",
        "filterwarnings": ["error"],
    }

    optional_dependencies = pyproject["project"]["optional-dependencies"]
    dev_dependencies = parse_requirement_map(optional_dependencies["dev"])
    stress_dependencies = parse_requirement_map(optional_dependencies["stress"])
    assert str(dev_dependencies["pytest"].specifier) == ">=9.1"
    assert set(stress_dependencies) == {"pytest-randomly"}

    makefile = read_text("Makefile")
    assert makefile.startswith("COVERAGE_FLOOR := 90\n")
    test_target = make_target_body("test")
    assert "-p pytest_cov" in test_target
    assert '-m "not scale"' not in test_target
    assert "--cov=crewplane --cov-branch" in test_target
    assert "--cov-fail-under=$(COVERAGE_FLOOR)" in test_target


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


def test_makefile_package_name_lookup_supports_gnu_make_3_81() -> None:
    makefile = read_text("Makefile")
    assert ".SHELLSTATUS" not in makefile
    assert (
        "PACKAGE_NAME := $(shell $(PROJECT_NAME_CMD) || "
        "printf '%s\\n' __PACKAGE_NAME_LOOKUP_FAILED__)"
    ) in makefile
    assert "ifeq ($(PACKAGE_NAME),__PACKAGE_NAME_LOOKUP_FAILED__)" in makefile


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


def test_ci_git_floor_lane_is_verified_required_mode_and_skip_free() -> None:
    workflow = yaml.load(
        read_text(".github", "workflows", "ci.yml"), Loader=yaml.BaseLoader
    )
    jobs = workflow["jobs"]
    git_floor = jobs["git-floor"]
    steps = {step.get("name"): step for step in git_floor["steps"]}

    assert git_floor["name"] == "git floor (2.34.1)"
    assert git_floor["runs-on"] == "ubuntu-latest"
    package = jobs["package"]
    assert "git-floor" in package["needs"]
    assert package["if"] == "${{ always() }}"
    prerequisite_guard = workflow_step_run(package, "Verify required prerequisite jobs")
    for fragment in (
        "test '${{ needs.git-floor.result }}' = 'success'",
        "test '${{ needs.lint.result }}' = 'success'",
        "test '${{ needs.test.result }}' = 'success'",
    ):
        assert fragment in prerequisite_guard

    build_step = steps["Build verified Git 2.34.1"]
    assert build_step["env"]["GIT_VERSION"] == "2.34.1"
    assert (
        build_step["env"]["GIT_ARCHIVE_SHA256"]
        == "3a0755dd1cfab71a24dd96df3498c29cd0acd13b04f3d08bf933e81286db802c"
    )
    for fragment in (
        "curl --fail --location --proto '=https' --tlsv1.2",
        "sha256sum --check -",
        'printf \'%s\\n\' "$prefix/bin" >> "$GITHUB_PATH"',
        'printf \'GIT_FLOOR_BIN=%s\\n\' "$prefix/bin" >> "$GITHUB_ENV"',
    ):
        assert fragment in build_step["run"]

    report_step = steps["Report floor environment"]
    assert "command -v git" in report_step["run"]
    assert 'test "$(git --version)" = "git version 2.34.1"' in report_step["run"]
    assert steps["Install dependencies"]["run"] == (
        "uv sync --locked --python 3.13 --extra dev"
    )

    floor_step = steps["Run exact Git floor tests"]
    assert floor_step["env"]["CREWPLANE_REQUIRED_GIT"] == "1"
    assert '--junitxml="$GIT_REPORT"' in floor_step["run"]
    assert "tests/unit/cli/test_workspace_source_policy_git.py" in floor_step["run"]
    assert (
        "tests/integration/cli/test_workspace_workflow_runner.py" in floor_step["run"]
    )

    zero_skip_step = steps["Enforce zero skips"]
    assert 'root.iter("testsuite")' in zero_skip_step["run"]
    assert "Git floor lane skipped {skipped} tests" in zero_skip_step["run"]


def test_ci_test_jobs_select_supported_python_versions_explicitly() -> None:
    workflow = yaml.load(
        read_text(".github", "workflows", "ci.yml"), Loader=yaml.BaseLoader
    )
    test_job = workflow["jobs"]["test"]

    assert test_job["strategy"]["matrix"]["python-version"] == ["3.13", "3.14"]
    commands = "\n".join(
        step.get("run", "") for step in test_job["steps"] if isinstance(step, dict)
    )
    assert "uv sync --locked --python ${{ matrix.python-version }} --extra dev" in (
        commands
    )
    assert (
        "uv run --locked --python ${{ matrix.python-version }} --extra dev make test"
        in commands
    )
    assert (
        "uv run --locked --python ${{ matrix.python-version }} --extra dev "
        "crewplane --help"
    ) in commands


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


def test_repository_automation_matches_supported_platform_and_publish_policy() -> None:
    nightly_text = read_text(".github", "workflows", "nightly.yml")
    nightly = yaml.safe_load(nightly_text)
    testpypi = read_text(".github", "workflows", "testpypi.yml")

    assert nightly["jobs"]["cross-platform"]["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "macos-latest",
    ]
    assert nightly["jobs"]["cross-platform"]["strategy"]["matrix"][
        "python-version"
    ] == ["3.13", "3.14"]
    cross_platform_commands = "\n".join(
        step.get("run", "")
        for step in nightly["jobs"]["cross-platform"]["steps"]
        if isinstance(step, dict)
    )
    assert "uv sync --locked --python ${{ matrix.python-version }} --extra dev" in (
        cross_platform_commands
    )
    assert (
        "uv run --locked --python ${{ matrix.python-version }} --extra dev "
        "python -m pytest -q"
    ) in cross_platform_commands
    assert '-m "not scale"' not in cross_platform_commands
    shuffled = nightly["jobs"]["shuffled-suite"]
    assert shuffled["runs-on"] == "ubuntu-latest"
    assert shuffled["env"]["CREWPLANE_RANDOM_SEED"] == "${{ github.run_id }}"
    shuffled_diagnostics = workflow_step_run(
        shuffled, "Print reliability canary diagnostics"
    )
    for fragment in (
        "OS:",
        "Architecture:",
        "python --version",
        "python -m pytest --version",
        "uv --version",
        "git --version",
        "locale",
        "Timezone:",
        "pytest-randomly seed:",
        "Selection: full test suite",
        "Iteration count: 1",
        "Skip summary: reported by pytest -ra",
    ):
        assert fragment in shuffled_diagnostics

    shuffled_commands = "\n".join(
        step.get("run", "") for step in shuffled["steps"] if isinstance(step, dict)
    )
    assert "uv sync --locked --python 3.13 --extra dev --extra stress" in (
        shuffled_commands
    )
    assert "python -m pytest -p randomly" in shuffled_commands
    assert '--randomly-seed="$seed"' in shuffled_commands
    assert '-m "not scale"' not in shuffled_commands
    assert "Reproduce locally:" in shuffled_commands

    focused = nightly["jobs"]["focused-race-loop"]
    assert focused["runs-on"] == "ubuntu-latest"
    assert focused["env"]["FOCUSED_RACE_ITERATIONS"] == "5"
    assert focused["env"]["FOCUSED_RACE_SELECTION"].endswith(
        "::ExecutorSequentialStageBasicsTests"
        "::test_multi_provider_reviewers_run_in_parallel_within_local_round"
    )
    focused_commands = "\n".join(
        step.get("run", "") for step in focused["steps"] if isinstance(step, dict)
    )
    focused_diagnostics = workflow_step_run(focused, "Print focused race diagnostics")
    for fragment in (
        "OS:",
        "Architecture:",
        "python --version",
        "python -m pytest --version",
        "uv --version",
        "git --version",
        "locale",
        "Timezone:",
        "Selection:",
        "Iteration count:",
        "Skip summary: reported by pytest -ra for each iteration",
        "Reproduce locally:",
        "FOCUSED_RACE_SELECTION=%q FOCUSED_RACE_ITERATIONS=%q",
    ):
        assert fragment in focused_diagnostics

    assert 'while [ "$iteration" -le "$FOCUSED_RACE_ITERATIONS" ]' in (focused_commands)
    assert 'python -m pytest -q -ra "$FOCUSED_RACE_SELECTION"' in focused_commands
    assert "windows-latest" not in nightly_text
    assert "skip-existing" not in testpypi


def test_nightly_uv_update_job_is_repository_scoped_and_uses_a_pull_request() -> None:
    nightly = yaml.safe_load(read_text(".github", "workflows", "nightly.yml"))
    update_job = nightly["jobs"]["ci-tooling-update"]

    assert update_job["permissions"] == {
        "actions": "write",
        "contents": "write",
        "pull-requests": "write",
    }
    assert "crewplaneai/crewplane" in update_job["if"]
    commands = "\n".join(
        step.get("run", "") for step in update_job["steps"] if isinstance(step, dict)
    )
    for fragment in (
        "scripts/update_uv_bootstrap.py update latest",
        "--app dependabot",
        "select(.isCrossRepository == false)",
        "gh pr create",
        "gh workflow run ci.yml",
    ):
        assert fragment in commands


def test_private_reporting_surfaces_use_github_security_advisories() -> None:
    issue_config = yaml.safe_load(read_text(".github", "ISSUE_TEMPLATE", "config.yml"))
    advisory_url = f"{REPOSITORY_URL}/security/advisories/new"
    security_links = [
        link for link in issue_config["contact_links"] if link["url"] == advisory_url
    ]

    assert len(security_links) == 1
    assert "privately" in security_links[0]["about"].lower()


def test_testpypi_workflow_uses_trusted_publishing() -> None:
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
    assert 'CREWPLANE_VERSION="${CREWPLANE_VERSION:-}"' in installer
    assert 'package_spec="$PACKAGE_NAME"' in installer
    assert 'package_spec="${PACKAGE_NAME}==${CREWPLANE_VERSION}"' in installer
    assert f"CREWPLANE_VERSION:-{AUTHORED_VERSION}" not in installer
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


@pytest.mark.parametrize(
    (
        "kernel",
        "machine",
        "node_platform",
        "node_arch",
        "target",
        "sha256",
        "downloader",
        "libc",
    ),
    UV_BOOTSTRAP_CASES,
)
def test_install_script_bootstraps_verified_uv_archive(
    tmp_path: Path,
    kernel: str,
    machine: str,
    node_platform: str,
    node_arch: str,
    target: str,
    sha256: str,
    downloader: str,
    libc: str,
) -> None:
    del node_platform, node_arch
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported installer surface")
    shell = shutil.which("sh")
    assert shell is not None
    env, install_home, temp_dir, download_log = prepare_uv_bootstrap_environment(
        tmp_path,
        downloader,
        kernel,
        machine,
        target,
        sha256,
        libc,
    )

    result = subprocess.run(
        [shell, str(repo_path("install.sh"))],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    expected_url = f"https://github.com/astral-sh/uv/releases/download/{PINNED_UV_VERSION}/uv-{target}.tar.gz"
    assert download_log.read_text(encoding="utf-8").strip() == expected_url
    assert (install_home / ".local" / "bin" / "uv").stat().st_mode & 0o111
    assert (install_home / ".local" / "bin" / "uvx").stat().st_mode & 0o111
    assert not any(temp_dir.iterdir())


def test_install_script_rejects_uv_archive_checksum_mismatch(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported installer surface")
    shell = shutil.which("sh")
    assert shell is not None
    target = "x86_64-unknown-linux-gnu"
    env, install_home, temp_dir, _ = prepare_uv_bootstrap_environment(
        tmp_path,
        "curl",
        "Linux",
        "x86_64",
        target,
        "0" * 64,
        "gnu",
    )

    result = subprocess.run(
        [shell, str(repo_path("install.sh"))],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "checksum" in result.stderr.lower()
    assert not (install_home / ".local" / "bin" / "uv").exists()
    assert not any(temp_dir.iterdir())


@pytest.mark.parametrize(
    ("version_override", "expected_package_spec"),
    [
        (None, PACKAGE_NAME),
        ("9.8.7", f"{PACKAGE_NAME}==9.8.7"),
    ],
)
def test_install_script_only_pins_an_explicit_version_override(
    tmp_path: Path,
    version_override: str | None,
    expected_package_spec: str,
) -> None:
    fake_uv = tmp_path / "uv"
    fake_uv_log = tmp_path / "uv.log"
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    fake_cli = tool_bin / PACKAGE_NAME
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cli.chmod(0o755)
    fake_uv.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "{",
                "  printf 'CALL'",
                '  for arg in "$@"; do printf "\\t%s" "$arg"; done',
                "  printf '\\n'",
                '} >> "$CREWPLANE_FAKE_UV_LOG"',
                'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then',
                '  printf "%s\\n" "$CREWPLANE_FAKE_TOOL_BIN"',
                "fi",
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
    env["CREWPLANE_FAKE_TOOL_BIN"] = str(tool_bin)
    env["CREWPLANE_INSTALL_HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{tool_bin}{os.pathsep}{env.get('PATH', '')}"
    if version_override is not None:
        env["CREWPLANE_VERSION"] = version_override

    subprocess.run(
        ["sh", str(repo_path("install.sh"))],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    calls = [
        line.split("\t")
        for line in fake_uv_log.read_text(encoding="utf-8").splitlines()
    ]
    assert calls[0] == ["CALL", "tool", "install", "--force", expected_package_spec]


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
    default_python = extract_python_floor_from_pyproject()
    assert f'const DEFAULT_PYTHON = "{default_python}";' in postinstall
    assert "process.env.CREWPLANE_INSTALL_PYTHON || DEFAULT_PYTHON" in postinstall
    assert "ensureSupportedPlatform();" in postinstall
    assert "uv" in postinstall
    assert "venv" in postinstall

    shim = read_text("packaging", "npm", "bin", "crewplane.js")
    assert ".venv" in shim
    assert CLI_COMMAND in shim
    assert "native Windows is not supported" in shim
    assert "lifecycle scripts may have been disabled" in shim
    assert "process.argv.slice(2)" in shim


@pytest.mark.parametrize(
    (
        "kernel",
        "machine",
        "node_platform",
        "node_arch",
        "target",
        "sha256",
        "downloader",
        "node_libc",
    ),
    UV_BOOTSTRAP_CASES,
)
def test_npm_postinstall_bootstraps_verified_uv_archive(
    tmp_path: Path,
    kernel: str,
    machine: str,
    node_platform: str,
    node_arch: str,
    target: str,
    sha256: str,
    downloader: str,
    node_libc: str,
) -> None:
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported npm surface")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm postinstall regression")
    env, install_home, temp_dir, download_log = prepare_uv_bootstrap_environment(
        tmp_path,
        downloader,
        kernel,
        machine,
        target,
        sha256,
        node_libc,
    )
    env.pop("CREWPLANE_INSTALL_HOME")

    result = subprocess.run(
        [
            node,
            "-e",
            npm_bootstrap_command(node_platform, node_arch, True, node_libc),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    expected_url = f"https://github.com/astral-sh/uv/releases/download/{PINNED_UV_VERSION}/uv-{target}.tar.gz"
    assert download_log.read_text(encoding="utf-8").strip() == expected_url
    assert (install_home / ".local" / "bin" / "uv").stat().st_mode & 0o111
    assert (install_home / ".local" / "bin" / "uvx").stat().st_mode & 0o111
    assert not any(temp_dir.iterdir())


def test_npm_postinstall_rejects_uv_archive_checksum_mismatch(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported npm surface")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm postinstall regression")
    target = "x86_64-unknown-linux-gnu"
    env, install_home, temp_dir, _ = prepare_uv_bootstrap_environment(
        tmp_path,
        "curl",
        "Linux",
        "x86_64",
        target,
        "0" * 64,
        "gnu",
    )
    env.pop("CREWPLANE_INSTALL_HOME")

    result = subprocess.run(
        [node, "-e", npm_bootstrap_command("linux", "x64", False)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "checksum" in result.stderr.lower()
    assert not (install_home / ".local" / "bin" / "uv").exists()
    assert not any(temp_dir.iterdir())


def test_npm_postinstall_defaults_to_min_supported_python_without_override(
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
    default_python = extract_python_floor_from_pyproject()
    assert calls[0][:4] == ["CALL", "venv", "--python", default_python]
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
    default_python = extract_python_floor_from_pyproject()
    assert "class Crewplane < Formula" in formula
    assert "include Language::Python::Virtualenv" in formula
    assert f'version "{AUTHORED_VERSION}"' in formula
    assert f"crewplane-{NORMALIZED_VERSION}.tar.gz" in formula
    assert 'license "Apache-2.0"' in formula
    assert f'depends_on "python@{default_python}"' in formula
    assert 'depends_on "maturin" => :build' in formula
    assert 'depends_on "rust" => :build' in formula
    assert 'branch: "master"' in formula
    assert f'def python3\n    "python{default_python}"\n  end' in formula
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
    formula_resources = parse_formula_resources(formula)
    assert formula_resources
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
        assert resource in formula_resources, f"missing resource {resource}"
        resource_url, sha = formula_resources[resource]
        assert resource_url and sha
        assert sha == sha.lower()
        normalized_name = resource.replace("-", "_")
        assert re.search(
            rf"{re.escape(normalized_name)}-[^/]+\.(?:whl|tar\.gz)$",
            resource_url,
        )
    assert formula.index('depends_on "maturin" => :build') < formula.index(
        'resource "pydantic-core" do'
    )


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

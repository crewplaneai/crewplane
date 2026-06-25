import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PACKAGE_NAME = "crewplane"
AUTHORED_VERSION = str(PYPROJECT["project"]["version"])
NORMALIZED_VERSION = str(Version(AUTHORED_VERSION))
CLI_COMMAND = "crewplane"
IMPORT_PACKAGE = "crewplane"
REPOSITORY_URL = "https://github.com/crewplaneai/crewplane"


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

    dev_dependencies = set(project["optional-dependencies"]["dev"])
    assert "build>=1.3" in dev_dependencies
    assert "twine>=6.0" in dev_dependencies
    assert "packaging>=24.0" in dev_dependencies

    dependencies = set(project["dependencies"])
    assert "typer>=0.12.0" in dependencies
    assert "click>=8.0.0" in dependencies
    assert "shellingham>=1.3.0" in dependencies
    assert "rich>=13.0" in dependencies
    assert all("typer[all]" not in dependency for dependency in dependencies)


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
    for command in ("prepare", "check", "publish-pypi", "publish-npm", "finalize"):
        assert command in result.stdout


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
    assert "${PACKAGE_NAME} run --no-live" in installer
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
        assert "crewplane run --no-live" in content
        assert "provider CLI" in content
        assert "does not require" in content or "needs no" in content
        assert "API key" in content
        assert "not model output" in content
        assert content.index("crewplane run --no-live") < content.index(
            "provider setup"
        )

    assert "getting-started/setup-checklist.md" in docs_index
    assert "guides/inspecting-artifacts.md" in docs_index
    assert "reference/configuration.md" in docs_index
    assert "safety/security-and-trust.md" in docs_index
    assert "safety/troubleshooting.md" in docs_index
    assert "../AGENTS.md" not in docs_index
    assert "../DEVELOPMENT.md" not in docs_index
    assert "architecture/" not in docs_index
    assert "maintainers/" not in docs_index
    assert "experimental-worktree-implementation" not in docs_index


def test_launch_support_docs_cover_skip_force_resume_and_bundles() -> None:
    running = read_text("docs", "guides", "running-workflows.md")
    troubleshooting = read_text("docs", "safety", "troubleshooting.md")
    support_bundle = read_text("docs", "safety", "reproducible-support-bundle.md")
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

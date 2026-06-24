import importlib.util
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
PACKAGE_NAME = "crewplane"
AUTHORED_VERSION = "0.1.0-alpha.2"
NORMALIZED_VERSION = "0.1.0a2"
CLI_COMMAND = "crewplane"
IMPORT_PACKAGE = "crewplane"
REPOSITORY_URL = "https://github.com/crewplaneai/crewplane"


def repo_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def read_text(*parts: str) -> str:
    return repo_path(*parts).read_text(encoding="utf-8")


def load_pyproject() -> dict[str, object]:
    return tomllib.loads(read_text("pyproject.toml"))


def load_uv_lock() -> dict[str, object]:
    return tomllib.loads(read_text("uv.lock"))


def load_npm_package() -> dict[str, object]:
    return json.loads(read_text("packaging", "npm", "package.json"))


def load_release_checks_module() -> object:
    module_path = repo_path("packaging", "release_checks.py")
    spec = importlib.util.spec_from_file_location("release_checks", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
    assert urls["Documentation"] == f"{REPOSITORY_URL}/blob/main/docs/index.md"

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


def test_makefile_exposes_local_release_validation_targets() -> None:
    makefile = read_text("Makefile")
    assert "VERSION ?= 0.1.0-alpha.1" not in makefile
    assert "PROJECT_NAME_CMD =" in makefile
    assert 'project["name"]' in makefile
    assert 'project.get("scripts", {})' in makefile
    assert "expected one [project.scripts] key matching project.name" in makefile
    assert "PACKAGE_NAME := $(shell $(PROJECT_NAME_CMD))" in makefile
    assert "PACKAGE_NAME := crewplane" not in makefile
    assert "CLI_NAME" not in makefile
    assert "PROJECT_VERSION_CMD =" in makefile
    assert "PROJECT_VERSION = $(shell $(PROJECT_VERSION_CMD))" in makefile
    assert "RELEASE_CHECKS = $(RUN_PYTHON) packaging/release_checks.py" in makefile
    assert "CONFIRM_VERSION_MISMATCH" not in makefile
    assert "PYPI_REPOSITORY ?= pypi" in makefile
    assert "TWINE_UPLOAD_ARGS ?=" in makefile
    assert "NPM_TAG ?= alpha" in makefile
    assert "NPM_PUBLISH_ARGS ?=" in makefile
    assert "UNINSTALL_CMD = uv pip uninstall $(PACKAGE_NAME)" in makefile
    assert "help:" in makefile
    assert "package-build:" in makefile
    assert "package-check:" in makefile
    assert "formula_sha" in makefile
    assert "sdist_sha" in makefile
    assert "packaging/homebrew/Formula/$(PACKAGE_NAME).rb" in makefile
    assert "uv export --frozen --no-dev --no-emit-project" in makefile
    assert "changelog-check:" in makefile
    assert "release-version-check:" in makefile
    assert "release-remote-version-check:" in makefile
    assert "release-confirm:" in makefile
    assert "install-smoke-pip:" in makefile
    assert "install-smoke-uv:" in makefile
    assert "install-smoke-pipx:" in makefile
    assert "install-script-smoke:" in makefile
    assert "npm-pack:" in makefile
    assert "npm-smoke:" in makefile
    assert "brew-smoke:" in makefile
    assert "install-check:" in makefile
    assert (
        "release-check: release-version-check release-remote-version-check" in makefile
    )
    assert "release-prereqs:" in makefile
    assert "release-pypi:" in makefile
    assert "release-npm:" in makefile
    assert ".NOTPARALLEL: release-check release" in makefile
    assert (
        "release: release-confirm release-check changelog-check release-prereqs"
        in makefile
    )
    assert "twine upload --repository" in makefile
    assert "npm publish" in makefile
    assert "npm whoami" in makefile
    assert "--no-index" in makefile
    assert "$(PACKAGE_NAME)==$(PROJECT_VERSION)" in makefile
    assert 'CREWPLANE_VERSION="$(PROJECT_VERSION)"' in makefile
    assert 'implementation: "mock"' in makefile
    for target in (
        "install-smoke-pip",
        "install-smoke-uv",
        "install-smoke-pipx",
        "npm-smoke",
    ):
        smoke_target = make_target_body(target)
        assert "'  mock:'" in smoke_target
        assert (
            "'    cli_cmd: [\"__crewplane_mock_invoker_never_executes__\"]'"
            in smoke_target
        )
        assert "'    provider_kind: \"generic\"'" in smoke_target
        assert "'    prompt_transport: \"stdin\"'" in smoke_target
        assert "'    default_model: \"mock\"'" in smoke_target
    assert 'brew list --formula "$(PACKAGE_NAME)"' in makefile
    assert (
        "Skipping brew-smoke: Homebrew formula $(PACKAGE_NAME) is already installed."
        in makefile
    )
    brew_smoke = make_target_body("brew-smoke")
    assert r"1,/url \"https:.*$(PACKAGE_NAME).*tar.gz\"/s" in brew_smoke
    assert r"1,/sha256 \".*\"/s" in brew_smoke
    assert "0,/" not in brew_smoke

    npm_pack = make_target_body("npm-pack")
    assert "$(PACKAGE_NAME)-*.tgz" in npm_pack

    npm_smoke = make_target_body("npm-smoke")
    assert 'mkdir -p "$$tmp/home" "$$tmp/npm-cache" "$$tmp/xdg-cache"' in npm_smoke
    assert 'HOME="$$tmp/home"' in npm_smoke
    assert 'NPM_CONFIG_CACHE="$$tmp/npm-cache"' in npm_smoke
    assert 'XDG_CACHE_HOME="$$tmp/xdg-cache"' in npm_smoke
    assert "export HOME NPM_CONFIG_CACHE XDG_CACHE_HOME" in npm_smoke
    assert npm_smoke.index("export HOME") < npm_smoke.index("npm install -g")
    assert 'PATH="$$tmp/prefix/bin:$$PATH"' in npm_smoke
    assert "export PATH" in npm_smoke
    assert 'command -v "$(PACKAGE_NAME)" >/dev/null' in npm_smoke
    assert "$(PACKAGE_NAME) --help >/dev/null" in npm_smoke
    assert '( cd "$$project" && $(PACKAGE_NAME) init >/dev/null )' in npm_smoke
    assert '( cd "$$project" && $(PACKAGE_NAME) validate >/dev/null )' in npm_smoke
    assert "$$exe" not in npm_smoke

    help_target = make_target_body("help")
    assert "Release variables:" in help_target
    assert (
        "release            Confirm version, run release checks, publish PyPI, then npm"
        in help_target
    )
    assert (
        "release-check      Check local metadata, remote availability, tests, and packages"
        in help_target
    )
    assert "Release version is read from pyproject.toml" in help_target
    assert "PYPI_REPOSITORY    Twine repository name" in help_target
    assert "NPM_TAG            npm publish dist-tag" in help_target
    assert "Homebrew tap publishing is separate" in help_target

    release_version_check = make_target_body("release-version-check")
    assert 'local --version "$(PROJECT_VERSION)"' in release_version_check

    release_remote_version_check = make_target_body("release-remote-version-check")
    assert 'remote --package-name "$(PACKAGE_NAME)" --version "$(PROJECT_VERSION)"' in (
        release_remote_version_check
    )

    release_confirm = make_target_body("release-confirm")
    assert (
        "Release $(PACKAGE_NAME) $(PROJECT_VERSION) to PyPI repository"
        in release_confirm
    )
    assert "[y/N]" in release_confirm
    assert "[Yy])" in release_confirm

    assert "node -p 'require(\"./packaging/npm/package.json\").version'" in npm_pack
    assert "differs from pyproject.toml version" in npm_pack
    assert "exit 1" in npm_pack

    release = make_target_body("release")
    assert "$(MAKE) release-pypi" in release
    assert "$(MAKE) release-npm" in release
    assert release.index("release-confirm") < release.index("release-check")
    assert release.index("release-pypi") < release.index("release-npm")


def test_release_check_helper_validates_local_packaging_versions(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    npm_root = package_root / "packaging" / "npm"
    formula_root = package_root / "packaging" / "homebrew" / "Formula"
    npm_root.mkdir(parents=True)
    formula_root.mkdir(parents=True)

    (npm_root / "package.json").write_text(
        json.dumps(
            {
                "version": AUTHORED_VERSION,
                "crewplane": {"pythonPackageVersion": AUTHORED_VERSION},
            }
        ),
        encoding="utf-8",
    )
    (formula_root / "crewplane.rb").write_text(
        f'class Crewplane < Formula\n  version "{AUTHORED_VERSION}"\nend\n',
        encoding="utf-8",
    )

    script = repo_path("packaging", "release_checks.py")
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "local",
            "--root",
            str(package_root),
            "--version",
            AUTHORED_VERSION,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Packaging versions match pyproject.toml version" in result.stdout

    (npm_root / "package.json").write_text(
        json.dumps(
            {
                "version": "0.1.0-alpha.3",
                "crewplane": {"pythonPackageVersion": AUTHORED_VERSION},
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "local",
            "--root",
            str(package_root),
            "--version",
            AUTHORED_VERSION,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Packaging versions differ from pyproject.toml version" in result.stdout
    assert "packaging/npm/package.json version: 0.1.0-alpha.3" in result.stdout


def test_release_check_helper_detects_remote_duplicate_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_checks = load_release_checks_module()

    def duplicate_fetch(url: str) -> dict[str, object]:
        if "pypi.org" in url:
            return {"releases": {AUTHORED_VERSION: []}}
        return {"versions": {}}

    monkeypatch.setattr(release_checks, "fetch_registry_json", duplicate_fetch)
    assert release_checks.check_remote(PACKAGE_NAME, AUTHORED_VERSION) == 1

    def available_fetch(url: str) -> dict[str, object]:
        if "pypi.org" in url:
            return {"releases": {}}
        return {"versions": {}}

    monkeypatch.setattr(release_checks, "fetch_registry_json", available_fetch)
    assert release_checks.check_remote(PACKAGE_NAME, AUTHORED_VERSION) == 0


def test_install_script_uses_uv_and_supports_local_artifact_smoke() -> None:
    installer = read_text("install.sh")
    assert 'PACKAGE_NAME="crewplane"' in installer
    assert "CLI_NAME" not in installer
    assert 'CREWPLANE_VERSION="${CREWPLANE_VERSION:-0.1.0-alpha.2}"' in installer
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

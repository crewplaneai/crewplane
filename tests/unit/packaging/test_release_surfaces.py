import json
import re
import subprocess
import sys
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version

from tests.unit.packaging.release_surfaces_support import (
    AUTHORED_VERSION,
    PACKAGE_NAME,
    REPOSITORY_URL,
    extract_python_floor_from_pyproject,
    load_pyproject,
    read_text,
    repo_path,
)

NORMALIZED_VERSION = str(Version(AUTHORED_VERSION))


CLI_COMMAND = "crewplane"


IMPORT_PACKAGE = "crewplane"


def parse_requirement_map(requirements: list[str]) -> dict[str, Requirement]:
    return {
        Requirement(requirement).name: Requirement(requirement)
        for requirement in requirements
    }


def has_lower_bound(requirement: Requirement) -> bool:
    return any(spec.operator in {">", ">="} for spec in requirement.specifier)


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
    assert "STATEMENT_COVERAGE_FLOOR ?= 96" in makefile.splitlines()
    assert "BRANCH_COVERAGE_FLOOR ?= 90" in makefile.splitlines()
    test_target = make_target_body("test")
    assert "-p pytest_cov" in test_target
    assert '-m "not scale"' not in test_target
    assert "--cov=crewplane --cov-branch" in test_target
    assert "--cov-report=json:.coverage.json" in test_target
    assert "--cov-fail-under=0" in test_target
    assert "$(MAKE) coverage-check" in test_target
    coverage_target = make_target_body("coverage-check")
    assert "scripts/check_coverage.py .coverage.json" in coverage_target
    assert "--statements $(STATEMENT_COVERAGE_FLOOR)" in coverage_target
    assert "--branches $(BRANCH_COVERAGE_FLOOR)" in coverage_target


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
        "homebrew-formula",
        "publish-homebrew-pr",
        "publish-pypi",
        "publish-npm",
        "finalize",
    ):
        assert command in result.stdout


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
    assert 'depends_on "libyaml"' in formula
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
        assert resource_url
        assert sha
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

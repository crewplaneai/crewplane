from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from scripts.release import state

ROOT = Path(__file__).resolve().parents[3]


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        command,
        cwd: Path,
        env=None,
        timeout=None,
        capture_output: bool = True,
        check: bool = True,
    ) -> state.CommandResult:
        del env, timeout, capture_output, check
        self.commands.append(tuple(command))
        if tuple(command) == ("uv", "lock"):
            version = project_version(cwd)
            write_uv_lock(cwd, state.ReleaseVersion.from_project(version).python)
        return state.CommandResult(tuple(command), 0, "", "")


def constant(value):
    def wrapper(*args, **kwargs):
        del args, kwargs
        return value

    return wrapper


def no_op(*args, **kwargs) -> None:
    del args, kwargs


def project_version(root: Path) -> str:
    content = (root / "pyproject.toml").read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise AssertionError("missing version")


def write_minimal_repo(root: Path, version: str = "1.2.3-alpha.4") -> None:
    (root / "packaging" / "npm").mkdir(parents=True)
    (root / "packaging" / "homebrew" / "Formula").mkdir(parents=True)
    (root / "docs" / "getting-started").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "crewplane"',
                f'version = "{version}"',
                'dependencies = ["typer>=0.12.0"]',
                "",
                "[project.scripts]",
                'crewplane = "crewplane.cli.app:app"',
                "",
                "[build-system]",
                'requires = ["hatchling"]',
                'build-backend = "hatchling.build"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_uv_lock(root, "0.0.0")
    (root / "install.sh").write_text(
        'CREWPLANE_VERSION="${CREWPLANE_VERSION:-0.0.0}"\n',
        encoding="utf-8",
    )
    (root / "packaging" / "npm" / "package.json").write_text(
        json.dumps(
            {
                "name": "crewplane",
                "version": "0.0.0",
                "crewplane": {
                    "pythonPackage": "crewplane",
                    "pythonPackageVersion": "0.0.0",
                    "pythonConsoleCommand": "crewplane",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "npm install -g crewplane@alpha\n", encoding="utf-8"
    )
    (root / "docs" / "getting-started" / "installation.md").write_text(
        "curl -fsSL https://raw.githubusercontent.com/crewplaneai/crewplane/main/install.sh | sh\n"
        "npm install -g crewplane@alpha\n",
        encoding="utf-8",
    )
    (root / "packaging" / "npm" / "README.md").write_text(
        "This alpha npm package exposes Crewplane.\n"
        "npm install -g crewplane@alpha\n"
        "npx crewplane@alpha --help\n"
        "npm install -g ./crewplane-0.0.0.tgz\n",
        encoding="utf-8",
    )
    (root / "packaging" / "homebrew" / "Formula" / "crewplane.rb").write_text(
        "\n".join(
            [
                "class Crewplane < Formula",
                '  url "https://files.pythonhosted.org/packages/source/c/crewplane/crewplane-0.0.0.tar.gz"',
                '  version "0.0.0"',
                f'  sha256 "{"0" * 64}"',
                '  head "https://github.com/crewplaneai/crewplane.git", branch: "main"',
                '  resource "hatchling" do',
                '    url "https://example.com/hatchling-0.0.0.tar.gz"',
                f'    sha256 "{"f" * 64}"',
                "  end",
                '  resource "packaging" do',
                '    url "https://example.com/packaging-0.0.0.tar.gz"',
                f'    sha256 "{"c" * 64}"',
                "  end",
                '  resource "typer" do',
                '    url "https://example.com/typer-0.0.0.tar.gz"',
                f'    sha256 "{"e" * 64}"',
                "  end",
                '  resource "click" do',
                '    url "https://example.com/click-0.0.0.tar.gz"',
                f'    sha256 "{"a" * 64}"',
                "  end",
                "end",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_uv_lock(root: Path, version: str) -> None:
    hatchling_url = "https://example.com/hatchling-0.0.0-py3-none-any.whl"
    typer_url = "https://example.com/typer-0.0.0-py3-none-any.whl"
    (root / "uv.lock").write_text(
        "\n".join(
            [
                "[[package]]",
                'name = "crewplane"',
                f'version = "{version}"',
                'source = { editable = "." }',
                "dependencies = [",
                '    { name = "typer" },',
                '    { name = "tzdata", marker = "sys_platform == \'win32\'" },',
                "]",
                "",
                "[[package]]",
                'name = "hatchling"',
                'version = "0.0.0"',
                "dependencies = [",
                '    { name = "packaging" },',
                "]",
                'sdist = { url = "https://example.com/hatchling-0.0.0.tar.gz", hash = "sha256:'
                + "f" * 64
                + '" }',
                'wheels = [ { url = "'
                + hatchling_url
                + '", hash = "sha256:'
                + "b" * 64
                + '" } ]',
                "",
                "[[package]]",
                'name = "packaging"',
                'version = "0.0.0"',
                'sdist = { url = "https://example.com/packaging-0.0.0.tar.gz", hash = "sha256:'
                + "c" * 64
                + '" }',
                "",
                "[[package]]",
                'name = "typer"',
                'version = "0.0.0"',
                "dependencies = [",
                '    { name = "click" },',
                "]",
                'sdist = { url = "https://example.com/typer-0.0.0.tar.gz", hash = "sha256:'
                + "e" * 64
                + '" }',
                'wheels = [ { url = "'
                + typer_url
                + '", hash = "sha256:'
                + "d" * 64
                + '" } ]',
                "",
                "[[package]]",
                'name = "click"',
                'version = "0.0.0"',
                'sdist = { url = "https://example.com/click-0.0.0.tar.gz", hash = "sha256:'
                + "a" * 64
                + '" }',
                "",
            ]
        ),
        encoding="utf-8",
    )


def append_uv_lock_package(
    root: Path, package: str, wheel_url: str, wheel_sha: str
) -> None:
    lock_path = root / "uv.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8")
        + "\n"
        + "\n".join(
            [
                "[[package]]",
                f'name = "{package}"',
                'version = "0.0.0"',
                f'wheels = [ {{ url = "{wheel_url}", hash = "sha256:{wheel_sha}" }} ]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def release_state_fixture(
    root: Path,
) -> tuple[
    state.ReleaseContext, state.ReleaseManifest, state.FormulaState, state.GitState
]:
    write_minimal_repo(root, "1.0.0-alpha.1")
    context = state.read_release_context(root)
    artifacts = {
        "pypi_sdist": state.ArtifactIdentity(
            "pypi_sdist",
            "dist/crewplane-1.0.0a1.tar.gz",
            context.sdist_filename,
            10,
            "a" * 64,
        ),
        "pypi_wheel": state.ArtifactIdentity(
            "pypi_wheel",
            "dist/crewplane-1.0.0a1-py3-none-any.whl",
            context.wheel_filename,
            20,
            "b" * 64,
        ),
        "npm_tarball": state.ArtifactIdentity(
            "npm_tarball",
            ".release/npm/crewplane-1.0.0-alpha.1.tgz",
            context.npm_filename,
            30,
            "c" * 64,
            sha1="d" * 40,
            integrity="sha512-good",
        ),
    }
    manifest = state.ReleaseManifest(
        package_name=context.package_name,
        project_version=context.version.project,
        python_version=context.version.python,
        npm_version=context.version.npm,
        git_tag=context.version.tag,
        artifacts=artifacts,
    )
    formula = state.FormulaState(
        path=root / "packaging/homebrew/Formula/crewplane.rb",
        url=context.sdist_url,
        version=context.version.project,
        sha256=manifest.artifact("pypi_sdist").sha256,
        head_branch="master",
        resources=frozenset({"click", "hatchling", "packaging", "typer"}),
        resource_specs={
            "click": ("https://example.com/click-0.0.0.tar.gz", "a" * 64),
            "hatchling": (
                "https://example.com/hatchling-0.0.0-py3-none-any.whl",
                "b" * 64,
            ),
            "packaging": ("https://example.com/packaging-0.0.0.tar.gz", "c" * 64),
            "typer": ("https://example.com/typer-0.0.0.tar.gz", "e" * 64),
        },
    )
    git = state.GitState(
        branch="master",
        default_branch="master",
        head_commit="abc",
        upstream_ahead=0,
        upstream_behind=0,
        dirty=False,
        tag_commit="abc",
        remote_tag_commit="abc",
    )
    return context, manifest, formula, git


def matching_pypi(
    context: state.ReleaseContext, manifest: state.ReleaseManifest
) -> state.PypiRelease:
    sdist = manifest.artifact("pypi_sdist")
    wheel = manifest.artifact("pypi_wheel")
    return state.PypiRelease(
        True,
        context.version.python,
        {
            sdist.filename: state.PypiFile(sdist.filename, sdist.size, sdist.sha256),
            wheel.filename: state.PypiFile(wheel.filename, wheel.size, wheel.sha256),
        },
    )


def matching_npm(
    context: state.ReleaseContext,
    manifest: state.ReleaseManifest,
    latest: str,
    shasum: str | None = None,
) -> state.NpmRelease:
    npm = manifest.artifact("npm_tarball")
    return state.NpmRelease(
        True,
        context.version.npm,
        latest,
        context.package_name,
        context.version.npm,
        context.package_name,
        context.version.project,
        shasum or npm.sha1,
        npm.integrity,
    )


def write_manifest(root: Path, manifest: state.ReleaseManifest) -> None:
    path = root / state.MANIFEST_PATH
    path.parent.mkdir(parents=True)
    payload = {
        "package": {
            "name": manifest.package_name,
            "project_version": manifest.project_version,
            "python_version": manifest.python_version,
            "npm_version": manifest.npm_version,
            "git_tag": manifest.git_tag,
        },
        "artifacts": {
            key: {
                "path": artifact.path,
                "filename": artifact.filename,
                "size": artifact.size,
                "sha256": artifact.sha256,
                "sha1": artifact.sha1,
                "integrity": artifact.integrity,
            }
            for key, artifact in manifest.artifacts.items()
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def load_release_script():
    scripts_path = str(ROOT / "scripts")
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    module_path = ROOT / "scripts" / "release.py"
    spec = importlib.util.spec_from_file_location(
        "release_script_under_test", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

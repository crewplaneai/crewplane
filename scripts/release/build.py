from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .artifact_layout import release_artifact_layout
from .plugins import release_plugins
from .state import (
    COMMAND_TIMEOUT_SECONDS,
    MANIFEST_PATH,
    ArtifactIdentity,
    CommandRunner,
    ReleaseContext,
    ReleaseError,
    ReleaseManifest,
    artifact_identity,
    command_exists,
    fail_if_generated_metadata_stale,
    query_registry_state,
    read_clean_source_commit,
    read_release_context,
    serialize_manifest,
    sync_generated_metadata,
    sync_homebrew_formula_metadata,
    verify_local_manifest_artifacts,
    write_json,
)


def prepare_release(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    pypi, npm = query_registry_state(context)
    existing = []
    if pypi.exists:
        existing.append("PyPI")
    if npm.exists:
        existing.append("npm")
    if existing:
        registries = ", ".join(existing)
        raise ReleaseError(
            f"{context.package_name} {context.version.project} already exists on {registries}; "
            "release-prepare only prepares unpublished versions."
        )
    sync_generated_metadata(context, runner)
    clean_release_outputs(context)
    artifacts = build_release_artifacts(context, runner)
    build_wheelhouse(context, runner)
    sync_homebrew_formula_metadata(context, artifacts["pypi_sdist"].sha256)
    manifest = write_release_manifest(context, artifacts)
    fail_if_generated_metadata_stale(context, manifest)
    print("Release artifacts prepared.")
    print_homebrew_instructions(context)


def release_artifacts(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    source_commit = read_clean_source_commit(root, runner)
    fail_if_generated_metadata_stale(context, None)
    clean_release_outputs(context)
    artifacts = build_release_artifacts(context, runner)
    if read_clean_source_commit(root, runner) != source_commit:
        raise ReleaseError("release source commit changed while building artifacts")
    manifest = write_release_manifest(context, artifacts, source_commit)
    fail_if_generated_metadata_stale(context, manifest)
    print("Release artifacts rebuilt.")


def clean_release_outputs(context: ReleaseContext) -> None:
    for relative in ("dist", "build", ".release", ".release-manifests"):
        shutil.rmtree(context.root / relative, ignore_errors=True)
    for path in context.root.glob("*.egg-info"):
        if path.is_dir():
            shutil.rmtree(path)


def build_release_artifacts(
    context: ReleaseContext, runner: CommandRunner
) -> dict[str, ArtifactIdentity]:
    build_python_artifacts(context, runner)
    run_twine_check(context, runner)
    npm_artifact = build_npm_artifact(context, runner)
    ensure_file(context.root / "dist" / context.sdist_filename)
    ensure_file(context.root / "dist" / context.wheel_filename)
    paths = release_artifact_layout(context)
    return {
        key: artifact_identity(
            npm_artifact if key == "npm_tarball" else context.root / path,
            context.root,
            key,
        )
        for key, path in paths.items()
    }


def build_python_artifacts(context: ReleaseContext, runner: CommandRunner) -> None:
    print(
        f"Building {context.package_name} {context.version.project} from pyproject.toml"
    )
    shutil.rmtree(context.root / "dist", ignore_errors=True)
    shutil.rmtree(context.root / "build", ignore_errors=True)
    for path in context.root.glob("*.egg-info"):
        if path.is_dir():
            shutil.rmtree(path)
    runner.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", "dist"],
        cwd=context.root,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )

    for plugin in release_plugins(context):
        runner.run(
            [
                sys.executable,
                "-m",
                "build",
                "--sdist",
                "--wheel",
                "--outdir",
                str(context.root / "dist"),
                str(plugin.source),
            ],
            cwd=context.root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )


def run_twine_check(context: ReleaseContext, runner: CommandRunner) -> None:
    packages = [context, *(plugin.context for plugin in release_plugins(context))]
    artifacts = [
        context.root / "dist" / filename
        for package in packages
        for filename in (package.sdist_filename, package.wheel_filename)
    ]
    for artifact in artifacts:
        ensure_file(artifact)
    runner.run(
        [sys.executable, "-m", "twine", "check", *map(str, artifacts)],
        cwd=context.root,
    )


def build_npm_artifact(context: ReleaseContext, runner: CommandRunner) -> Path:
    if not command_exists("npm"):
        raise ReleaseError("npm is required to build the release npm package")
    package_dir = context.root / ".release" / "npm"
    package_dir.mkdir(parents=True, exist_ok=True)
    for path in package_dir.glob(f"{context.package_name}-*.tgz"):
        path.unlink()
    runner.run(
        ["npm", "pack", "./packaging/npm", "--pack-destination", str(package_dir)],
        cwd=context.root,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    package = package_dir / context.npm_filename
    ensure_file(package)
    return package


def build_wheelhouse(context: ReleaseContext, runner: CommandRunner) -> None:
    wheelhouse = context.root / ".release" / "wheelhouse"
    shutil.rmtree(wheelhouse, ignore_errors=True)
    wheelhouse.mkdir(parents=True, exist_ok=True)
    wheel = context.root / "dist" / context.wheel_filename
    ensure_file(wheel)
    plugins = release_plugins(context)
    plugin_wheels = [
        str(context.root / "dist" / plugin.context.wheel_filename) for plugin in plugins
    ]
    for plugin_wheel in plugin_wheels:
        ensure_file(Path(plugin_wheel))
    if command_exists("uv"):
        requirements = context.root / ".release" / "runtime-requirements.txt"
        runner.run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                *[
                    arg
                    for plugin in plugins
                    for arg in ("--no-emit-package", plugin.context.package_name)
                ],
                "--no-hashes",
                "--format",
                "requirements.txt",
                "--output-file",
                str(requirements),
            ],
            cwd=context.root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        runner.run(
            pip_download_command()
            + [
                "download",
                "--dest",
                str(wheelhouse),
                "-r",
                str(requirements),
                str(wheel),
                *plugin_wheels,
            ],
            cwd=context.root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return
    runner.run(
        pip_download_command()
        + ["download", "--dest", str(wheelhouse), str(wheel), *plugin_wheels],
        cwd=context.root,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def pip_download_command() -> list[str]:
    if command_exists("uv"):
        return ["uv", "run", "--extra", "dev", "--with", "pip", "python", "-m", "pip"]
    return [sys.executable, "-m", "pip"]


def write_release_manifest(
    context: ReleaseContext,
    artifacts: dict[str, ArtifactIdentity],
    source_commit: str = "",
) -> ReleaseManifest:
    manifest = ReleaseManifest(
        package_name=context.package_name,
        project_version=context.version.project,
        python_version=context.version.python,
        npm_version=context.version.npm,
        git_tag=context.version.tag,
        artifacts=artifacts,
        source_commit=source_commit,
    )
    payload = serialize_manifest(manifest)
    manifest_path = context.root / MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(manifest_path, payload)
    archived = (
        context.root
        / ".release-manifests"
        / f"release-manifest.{context.version.project}.json"
    )
    archived.parent.mkdir(parents=True, exist_ok=True)
    write_json(archived, payload)
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if loaded != payload:
        raise ReleaseError("release manifest read-back validation failed")
    return manifest


def bind_release_manifest(
    context: ReleaseContext,
    manifest: ReleaseManifest,
    source_commit: str,
    runner: CommandRunner,
) -> None:
    if read_clean_source_commit(context.root, runner) != source_commit:
        raise ReleaseError("release source commit changed during validation")
    issues = verify_local_manifest_artifacts(
        context, manifest, tuple(manifest.artifacts)
    )
    if issues:
        raise ReleaseError(
            "validated release artifacts changed:\n  " + "\n  ".join(issues)
        )
    write_release_manifest(context, manifest.artifacts, source_commit)


def package_build(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    build_python_artifacts(context, runner)


def package_check(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    manifest = None
    fail_if_generated_metadata_stale(context, manifest)
    build_python_artifacts(context, runner)
    run_twine_check(context, runner)
    sdist = context.root / "dist" / context.sdist_filename
    formula_sha = first_formula_sha(context)
    sdist_sha = artifact_identity(sdist, context.root, "pypi_sdist").sha256
    if formula_sha != sdist_sha:
        raise ReleaseError(
            "Homebrew formula source SHA does not match the built sdist; "
            "run make release-prepare first."
        )


def package_wheelhouse(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    build_python_artifacts(context, runner)
    build_wheelhouse(context, runner)


def npm_pack(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    fail_if_generated_metadata_stale(context, None)
    build_npm_artifact(context, runner)


def first_formula_sha(context: ReleaseContext) -> str:
    formula = (
        context.root
        / "packaging"
        / "homebrew"
        / "Formula"
        / f"{context.package_name}.rb"
    )
    for line in formula.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith('sha256 "'):
            return stripped.split('"', 2)[1]
    raise ReleaseError(f"missing Homebrew sha256 in {formula}")


def ensure_file(path: Path) -> None:
    if not path.is_file():
        raise ReleaseError(f"expected release artifact is missing: {path}")


def print_homebrew_instructions(context: ReleaseContext) -> None:
    formula = Path("packaging") / "homebrew" / "Formula" / f"{context.package_name}.rb"
    print("Homebrew formula ready:")
    print(f"  {formula}")
    print("")
    print("After publishing the Git tag, run the GitHub release workflow.")
    print("Eligible releases open a Homebrew tap pull request automatically.")
    print("After its checks pass, publish it with the tap's brew pr-pull workflow.")

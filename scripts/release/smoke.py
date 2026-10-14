from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from . import build, retry
from .state import (
    COMMAND_TIMEOUT_SECONDS,
    CommandRunner,
    ReleaseContext,
    ReleaseError,
    artifact_identity,
    command_exists,
    read_release_context,
)


def install_check(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    build.package_check(root, runner)
    build.build_wheelhouse(context, runner)
    _exercise_python_install_smokes(context, runner)
    _install_script_smoke(context, runner)
    if command_exists("npm"):
        build.npm_pack(root, runner)
        _npm_smoke(context, runner)
    else:
        print("Skipping npm-smoke: npm not found.")
    if command_exists("brew"):
        _brew_smoke(context, runner)
    else:
        print("Skipping brew-smoke: brew not found.")


def install_smoke(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    build.package_wheelhouse(root, runner)
    _exercise_python_install_smokes(context, runner)


def _exercise_python_install_smokes(
    context: ReleaseContext, runner: CommandRunner
) -> None:
    _install_smoke_pip(context, runner)
    if command_exists("uv"):
        _install_smoke_uv(context, runner)
    else:
        print("Skipping install-smoke-uv: uv not found.")
    if command_exists("pipx"):
        _install_smoke_pipx(context, runner)
    else:
        print("Skipping install-smoke-pipx: pipx not found.")


def install_smoke_pip(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    build.package_wheelhouse(root, runner)
    _install_smoke_pip(context, runner)


def _install_smoke_pip(context: ReleaseContext, runner: CommandRunner) -> None:
    root = context.root
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        venv = tmp / "venv"
        if command_exists("uv"):
            runner.run(
                ["uv", "venv", "--seed", "--python", sys.executable, str(venv)],
                cwd=root,
            )
        else:
            runner.run([sys.executable, "-m", "venv", str(venv)], cwd=root)
        python = venv / "bin" / "python"
        runner.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse(context)),
                f"{context.package_name}=={context.version.project}",
            ],
            cwd=root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        exe = venv / "bin" / context.package_name
        exercise_installed_cli(context, runner, exe, tmp)


def install_smoke_uv(root: Path, runner: CommandRunner) -> None:
    if not command_exists("uv"):
        print("Skipping install-smoke-uv: uv not found.")
        return
    context = read_release_context(root)
    build.package_wheelhouse(root, runner)
    _install_smoke_uv(context, runner)


def _install_smoke_uv(context: ReleaseContext, runner: CommandRunner) -> None:
    root = context.root
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        home = tmp / "home"
        home.mkdir()
        env = {"HOME": str(home)}
        runner.run(
            [
                "uv",
                "tool",
                "install",
                "--force",
                "--python",
                sys.executable,
                "--find-links",
                str(wheelhouse(context)),
                "--no-index",
                f"{context.package_name}=={context.version.project}",
            ],
            cwd=root,
            env=env,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        tool_bin = runner.run(
            ["uv", "tool", "dir", "--bin"], cwd=root, env=env
        ).stdout.strip()
        exercise_installed_cli(
            context, runner, Path(tool_bin) / context.package_name, tmp
        )


def install_smoke_pipx(root: Path, runner: CommandRunner) -> None:
    if not command_exists("pipx"):
        print("Skipping install-smoke-pipx: pipx not found.")
        return
    context = read_release_context(root)
    build.package_wheelhouse(root, runner)
    _install_smoke_pipx(context, runner)


def _install_smoke_pipx(context: ReleaseContext, runner: CommandRunner) -> None:
    root = context.root
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        env = {"PIPX_HOME": str(tmp / "pipx-home"), "PIPX_BIN_DIR": str(tmp / "bin")}
        runner.run(
            [
                "pipx",
                "install",
                "--force",
                "--python",
                sys.executable,
                f"--pip-args=--no-index --find-links {wheelhouse(context)}",
                f"{context.package_name}=={context.version.project}",
            ],
            cwd=root,
            env=env,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        exercise_installed_cli(context, runner, tmp / "bin" / context.package_name, tmp)


def install_script_smoke(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    build.package_wheelhouse(root, runner)
    _install_script_smoke(context, runner)


def _install_script_smoke(context: ReleaseContext, runner: CommandRunner) -> None:
    root = context.root
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        env = {
            "CREWPLANE_VERSION": context.version.project,
            "CREWPLANE_INSTALL_FIND_LINKS": str(wheelhouse(context)),
            "CREWPLANE_INSTALL_NO_INDEX": "1",
            "CREWPLANE_INSTALL_PYTHON": sys.executable,
            "CREWPLANE_INSTALL_HOME": str(tmp / "home"),
            "HOME": str(tmp / "home"),
        }
        runner.run(
            ["sh", "install.sh"], cwd=root, env=env, timeout=COMMAND_TIMEOUT_SECONDS
        )


def npm_smoke(root: Path, runner: CommandRunner) -> None:
    if not command_exists("npm"):
        print("Skipping npm-smoke: npm not found.")
        return
    context = read_release_context(root)
    build.package_wheelhouse(root, runner)
    build.npm_pack(root, runner)
    _npm_smoke(context, runner)


def _npm_smoke(context: ReleaseContext, runner: CommandRunner) -> None:
    root = context.root
    package = context.root / ".release" / "npm" / context.npm_filename
    if not package.is_file():
        raise ReleaseError(f"npm package artifact is missing: {package}")
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        home = tmp / "home"
        cache = tmp / "npm-cache"
        xdg_cache = tmp / "xdg-cache"
        for path in (home, cache, xdg_cache):
            path.mkdir()
        env = {
            "HOME": str(home),
            "NPM_CONFIG_CACHE": str(cache),
            "XDG_CACHE_HOME": str(xdg_cache),
            "CREWPLANE_VERSION": context.version.project,
            "CREWPLANE_INSTALL_FIND_LINKS": str(wheelhouse(context)),
            "CREWPLANE_INSTALL_NO_INDEX": "1",
            "CREWPLANE_INSTALL_PYTHON": sys.executable,
        }
        prefix = tmp / "prefix"
        runner.run(
            [
                "npm",
                "install",
                "-g",
                str(package),
                "--prefix",
                str(prefix),
                "--foreground-scripts",
            ],
            cwd=root,
            env=env,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        path = f"{prefix / 'bin'}:{os.environ.get('PATH', '')}"
        exercise_installed_cli(
            context,
            runner,
            prefix / "bin" / context.package_name,
            tmp,
            {"PATH": path, **env},
        )


def brew_smoke(root: Path, runner: CommandRunner) -> None:
    if not command_exists("brew"):
        print("Skipping brew-smoke: brew not found.")
        return
    context = read_release_context(root)
    build.package_build(root, runner)
    _brew_smoke(context, runner)


def _brew_smoke(context: ReleaseContext, runner: CommandRunner) -> None:
    root = context.root
    installed = runner.run(
        ["brew", "list", "--formula", context.package_name],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if installed.returncode == 0:
        print(
            f"Skipping brew-smoke: Homebrew formula {context.package_name} is already installed."
        )
        return
    sdist = root / "dist" / context.sdist_filename
    build.ensure_file(sdist)
    sha = artifact_identity(sdist, root, "pypi_sdist").sha256
    formula = root / "packaging" / "homebrew" / "Formula" / f"{context.package_name}.rb"
    text = formula.read_text(encoding="utf-8")
    text = replace_first(text, r'url "[^"]+"', f'url "file://{sdist}"')
    text = replace_first(text, r'sha256 "[a-f0-9]{64}"', f'sha256 "{sha}"')
    tap_name = f"crewplane/release-smoke-{uuid4().hex}"
    runner.run(
        ["brew", "tap-new", "--no-git", tap_name],
        cwd=root,
        env={"HOMEBREW_DEVELOPER": "1"},
    )
    try:
        repository = runner.run(["brew", "--repository", tap_name], cwd=root)
        local_formula = (
            Path(repository.stdout.strip()) / "Formula" / f"{context.package_name}.rb"
        )
        local_formula.write_text(text, encoding="utf-8")
        _check_brew_installation(context, runner, f"{tap_name}/{context.package_name}")
    finally:
        runner.run(["brew", "untap", tap_name], cwd=root)


def _check_brew_installation(
    context: ReleaseContext, runner: CommandRunner, formula_name: str
) -> None:
    try:
        runner.run(
            ["brew", "install", "--build-from-source", formula_name],
            cwd=context.root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        runner.run(
            ["brew", "test", formula_name],
            cwd=context.root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    finally:
        runner.run(
            ["brew", "uninstall", formula_name],
            cwd=context.root,
            capture_output=True,
            check=False,
        )


def post_publish_pypi_check(
    context: ReleaseContext,
    runner: CommandRunner,
    attempts: int = retry.VERIFICATION_ATTEMPTS,
) -> None:
    run_bounded_post_publish_check(
        attempts,
        "PyPI",
        lambda: remote_pip_install_check(context, runner),
    )


def post_publish_npm_check(
    context: ReleaseContext,
    runner: CommandRunner,
    attempts: int = retry.VERIFICATION_ATTEMPTS,
) -> None:
    run_bounded_post_publish_check(
        attempts,
        "npm",
        lambda: remote_npm_install_check(context, runner),
    )


def run_bounded_post_publish_check(
    attempts: int,
    label: str,
    check: Callable[[], None],
) -> None:
    def collect_issues() -> list[str]:
        try:
            check()
        except ReleaseError as error:
            return [str(error)]
        return []

    issues = retry.wait_for_verification(
        f"{label} post-publish install", collect_issues, attempts
    )
    if issues:
        raise ReleaseError(
            f"{label} post-publish install check did not pass after {attempts} attempts. "
            f"Manual recovery may be needed: {'; '.join(issues)}"
        )


def remote_pip_install_check(context: ReleaseContext, runner: CommandRunner) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        runner.run([sys.executable, "-m", "venv", str(tmp / "venv")], cwd=context.root)
        python = tmp / "venv" / "bin" / "python"
        runner.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                f"{context.package_name}=={context.version.project}",
            ],
            cwd=context.root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        runner.run(
            [str(tmp / "venv" / "bin" / context.package_name), "--help"],
            cwd=context.root,
        )


def remote_npm_install_check(context: ReleaseContext, runner: CommandRunner) -> None:
    if not command_exists("npm"):
        raise ReleaseError("npm is required for npm post-publish checks")
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        prefix = tmp / "prefix"
        runner.run(
            [
                "npm",
                "install",
                "-g",
                f"{context.package_name}@{context.version.npm}",
                "--prefix",
                str(prefix),
            ],
            cwd=context.root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        runner.run(
            [str(prefix / "bin" / context.package_name), "--help"], cwd=context.root
        )


def exercise_installed_cli(
    context: ReleaseContext,
    runner: CommandRunner,
    executable: Path,
    tmp: Path,
    env: dict[str, str] | None = None,
) -> None:
    resolved = executable.resolve()
    python = (
        resolved.parent.parent / ".venv/bin/python"
        if resolved.suffix == ".js"
        else resolved.parent / "python"
    )
    diagnostics = context.root / "test-results" / f"package-release-{uuid4().hex}"
    runner.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[2]
                / "tests/helpers/installed_package_assertions.py"
            ),
            "--python",
            str(python),
            "--cli",
            str(executable),
            "--expected-version",
            context.version.project,
            "--checkout",
            str(context.root),
            "--diagnostics",
            str(diagnostics),
        ],
        cwd=tmp,
        env=env,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def wheelhouse(context: ReleaseContext) -> Path:
    return context.root / ".release" / "wheelhouse"


def replace_first(text: str, pattern: str, replacement: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise ReleaseError(f"expected one replacement for {pattern}")
    return updated

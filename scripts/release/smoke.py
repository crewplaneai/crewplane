from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

from . import build
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
    build.package_check(root, runner)
    install_smoke(root, runner)
    install_script_smoke(root, runner)
    build.npm_pack(root, runner)
    npm_smoke(root, runner)
    brew_smoke(root, runner)


def install_smoke(root: Path, runner: CommandRunner) -> None:
    install_smoke_pip(root, runner)
    install_smoke_uv(root, runner)
    install_smoke_pipx(root, runner)


def install_smoke_pip(root: Path, runner: CommandRunner) -> None:
    context = read_release_context(root)
    build.package_wheelhouse(root, runner)
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        smoke_python = current_python(root, runner)
        venv = tmp / "venv"
        if command_exists("uv"):
            runner.run(
                ["uv", "venv", "--seed", "--python", smoke_python, str(venv)], cwd=root
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
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        home = tmp / "home"
        home.mkdir()
        smoke_python = current_python(root, runner)
        env = {"HOME": str(home)}
        runner.run(
            [
                "uv",
                "tool",
                "install",
                "--force",
                "--python",
                smoke_python,
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
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        smoke_python = current_python(root, runner)
        env = {"PIPX_HOME": str(tmp / "pipx-home"), "PIPX_BIN_DIR": str(tmp / "bin")}
        runner.run(
            [
                "pipx",
                "install",
                "--force",
                "--python",
                smoke_python,
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
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        env = {
            "CREWPLANE_VERSION": context.version.project,
            "CREWPLANE_INSTALL_FIND_LINKS": str(wheelhouse(context)),
            "CREWPLANE_INSTALL_NO_INDEX": "1",
            "CREWPLANE_INSTALL_PYTHON": current_python(root, runner),
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
            "CREWPLANE_INSTALL_PYTHON": current_python(root, runner),
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
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        sdist = context.root / "dist" / context.sdist_filename
        build.ensure_file(sdist)
        sha = artifact_identity(sdist, context.root, "pypi_sdist").sha256
        local_formula = tmp / f"{context.package_name}.rb"
        formula = (
            context.root
            / "packaging"
            / "homebrew"
            / "Formula"
            / f"{context.package_name}.rb"
        )
        text = formula.read_text(encoding="utf-8")
        text = replace_first(text, r'url "[^"]+"', f'url "file://{sdist}"')
        text = replace_first(text, r'sha256 "[a-f0-9]{64}"', f'sha256 "{sha}"')
        local_formula.write_text(text, encoding="utf-8")
        try:
            runner.run(
                ["brew", "install", "--build-from-source", str(local_formula)],
                cwd=root,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            runner.run(
                ["brew", "test", context.package_name],
                cwd=root,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        finally:
            runner.run(
                ["brew", "uninstall", context.package_name],
                cwd=root,
                capture_output=True,
                check=False,
            )


def post_publish_pypi_check(
    context: ReleaseContext, runner: CommandRunner, attempts: int = 6
) -> None:
    run_bounded_post_publish_check(
        attempts,
        "PyPI",
        lambda: remote_pip_install_check(context, runner),
    )


def post_publish_npm_check(
    context: ReleaseContext, runner: CommandRunner, attempts: int = 6
) -> None:
    run_bounded_post_publish_check(
        attempts,
        "npm",
        lambda: remote_npm_install_check(context, runner),
    )


def run_bounded_post_publish_check(
    attempts: int,
    label: str,
    check,
) -> None:
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            check()
            return
        except ReleaseError as error:
            last_error = str(error)
            if attempt == attempts:
                break
            print(
                f"{label} post-publish check failed; retrying ({attempt}/{attempts})."
            )
            time.sleep(2 ** (attempt - 1))
    raise ReleaseError(
        f"{label} post-publish install check did not pass after {attempts} attempts. "
        f"Manual recovery may be needed: {last_error}"
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
    runner.run([str(executable), "--help"], cwd=context.root, env=env)
    project = tmp / "project"
    project.mkdir(exist_ok=True)
    runner.run([str(executable), "init"], cwd=project, env=env)
    write_mock_config(project / ".crewplane" / "config.yml")
    runner.run([str(executable), "validate"], cwd=project, env=env)


def write_mock_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "agents:",
                "  mock:",
                '    cli_cmd: ["__crewplane_mock_invoker_never_executes__"]',
                '    provider_kind: "generic"',
                '    prompt_transport: "stdin"',
                '    default_model: "mock"',
                "settings:",
                "  integrations:",
                "    invoker:",
                '      implementation: "mock"',
                "      options:",
                "        delay_seconds: 0",
                "        observation_delay_seconds: 0",
                '        output_mode: "lorem"',
                "    ui:",
                '      implementation: "none"',
                "      options: {}",
                "    artifacts:",
                '      implementation: "filesystem"',
                "      options:",
                "        log_cli_output: true",
                "        allowed_template_paths: []",
                "",
            ]
        ),
        encoding="utf-8",
    )


def current_python(root: Path, runner: CommandRunner) -> str:
    return runner.run(
        [sys.executable, "-c", "import sys; print(sys.executable)"],
        cwd=root,
    ).stdout.strip()


def wheelhouse(context: ReleaseContext) -> Path:
    return context.root / ".release" / "wheelhouse"


def replace_first(text: str, pattern: str, replacement: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise ReleaseError(f"expected one replacement for {pattern}")
    return updated

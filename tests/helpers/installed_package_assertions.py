"""Installed-package assertions shared by release and wheel acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

if __package__:
    from .execution_environment import build_execution_environment
else:
    from execution_environment import build_execution_environment

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/packaging"
# Read policy data without adding checkout imports to installed-package probes.
with (Path(__file__).resolve().parents[2] / "pyproject.toml").open("rb") as source:
    COMMAND_TIMEOUT_SECONDS = tomllib.load(source)["tool"]["crewplane"]["validation"][
        "timeouts"
    ]["installed_command_seconds"]


def run(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    output = result.stdout.decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command}\n{output}")
    return output


def verify_run(project: Path, stage: Path) -> None:
    manifest = json.loads((stage / "manifests/run.json").read_bytes())
    assert manifest["status"] == "succeeded", manifest
    results = project / ".crewplane/execution-results" / stage.name
    assert list(results.glob("*.md"))
    assert list(stage.rglob("summary.md"))
    descriptors = []
    for path in (stage / "manifests/nodes").glob("*.json"):
        state = json.loads(path.read_bytes())
        descriptors.extend(state.get("artifacts", []))
    assert descriptors, "Expected persisted output descriptors"
    for descriptor in descriptors:
        data = (results / descriptor["relative_path"]).read_bytes()
        assert len(data) == descriptor["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == descriptor["sha256"]


def smoke(
    python: Path,
    cli: Path,
    project: Path,
    expected_version: str,
    checkout: Path,
    diagnostics: Path,
    tool_path: str | None = None,
) -> None:
    env = package_environment(diagnostics / "environment", tool_path)
    assert not project.resolve().is_relative_to(checkout.resolve()), (
        "Acceptance must run outside the checkout"
    )
    project.mkdir()
    installed = inspect_installation(python, project, env)
    diagnostics.mkdir(parents=True, exist_ok=True)
    (diagnostics / "installed-environment.json").write_text(
        json.dumps(installed, indent=2) + "\n", encoding="utf-8"
    )
    assert installed["version"] == expected_version
    origin = Path(installed["origin"]).resolve()
    assert not origin.is_relative_to(checkout.resolve()), origin
    assert origin.is_relative_to(Path(installed["prefix"]).resolve()), origin
    assert installed["config_resource"] and installed["default_workflow_resource"]
    assert installed["library_resources"] > 0
    assert installed["ollama_version"]
    plugin_origin = Path(installed["ollama_origin"]).resolve()
    assert not plugin_origin.is_relative_to(checkout.resolve()), plugin_origin
    assert plugin_origin.is_relative_to(Path(installed["prefix"]).resolve()), (
        plugin_origin
    )
    run([str(cli), "--help"], project, env)
    assert (
        run([str(cli), "--version"], project, env).strip()
        == f"crewplane {expected_version}"
    )
    run([str(cli), "init"], project, env)
    schema = installed["schema_version"]
    workflows = project / ".crewplane/workflows"
    (workflows / "modules").mkdir(parents=True, exist_ok=True)
    config = {
        "version": schema,
        "agents": {
            "alpha": {
                "cli_cmd": [str(python)],
                "provider_kind": "generic",
                "prompt_transport": "stdin",
            }
        },
        "settings": {
            "workspace": {"enabled": False},
            "integrations": {
                "invoker": {
                    "implementation": "mock",
                    "options": {"output_mode": "echo", "observation_delay_seconds": 0},
                },
                "ui": {"implementation": "none", "options": {}},
                "artifacts": {
                    "implementation": "filesystem",
                    "options": {"log_cli_output": True},
                },
            },
        },
    }
    config_path = project / ".crewplane/config.yml"
    config_path.write_bytes(json.dumps(config).encode())
    (project / "context.md").write_bytes("Unicode café\r\nCtrl-Z\x1a\n".encode())
    (workflows / "modules/child.task.md").write_bytes(
        (
            f'---\nschema_version: "{schema}"\nname: Child\nnodes:\n  - id: build\n    mode: sequential\n    providers: [alpha]\n---\n## build\nRead {{{{file:context.md}}}}\n'
        ).encode()
    )
    workflow = workflows / "smoke.task.md"
    workflow.write_bytes(
        (
            f'---\nschema_version: "{schema}"\nname: Installed smoke\nimports:\n  - path: modules/child.task.md\n    as: child\nnodes:\n  - id: fanout\n    mode: parallel\n    needs: [child.build]\n    providers: [alpha, alpha]\n---\n## fanout\nUse {{{{child.build.output}}}}\n'
        ).encode()
    )
    base = [str(cli), "run", "--tasks", str(workflow), "--no-live"]
    run([str(cli), "validate", str(workflow)], project, env)
    run([*base, "--dry-run"], project, env)
    assert not (project / ".crewplane/execution-stages").exists()
    run(base, project, env)
    stages = project / ".crewplane/execution-stages"
    first = next(stages.iterdir())
    verify_run(project, first)
    assert "Identical context detected" in run(base, project, env)
    assert len(list(stages.iterdir())) == 1
    run([*base, "--force"], project, env)
    assert len(list(stages.iterdir())) == 2
    config["agents"]["alpha"]["cli_cmd"] = [
        str(python),
        "-c",
        "import sys; "
        "sys.stdout.buffer.write(b'provider bytes\\r\\n\\x1a' + sys.stdin.buffer.read())",
    ]
    config["settings"]["integrations"]["invoker"] = {
        "implementation": "cli",
        "options": {},
    }
    config_path.write_bytes(json.dumps(config).encode())
    run(base, project, env)
    new_stages = list(stages.iterdir())
    assert len(new_stages) == 3
    for stage in new_stages:
        verify_run(project, stage)
    receipts = list(stages.rglob("provider-processes/*.json"))
    assert receipts and all(
        json.loads(path.read_bytes())["status"] == "exited" for path in receipts
    )
    assert all(
        not process_running(json.loads(path.read_bytes())["pid"]) for path in receipts
    )
    print(
        f"Installed package acceptance passed: {cli}; mock, dedupe, force, generic subprocess, hashes and receipts"
    )


def package_environment(
    root: Path, tool_path: str | None = None, scenario: Mapping[str, str] | None = None
) -> dict[str, str]:
    return build_execution_environment(
        root,
        os.environ.get("PATH", os.defpath) if tool_path is None else tool_path,
        scenario,
    )


def inspect_installation(
    python: Path, project: Path, env: dict[str, str]
) -> dict[str, object]:
    probe = project / "inspect_installation.py"
    shutil.copyfile(FIXTURES / probe.name, probe)
    return json.loads(run([str(python), str(probe)], project, env))


def process_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise ctypes.WinError()
        try:
            result = api.WaitForSingleObject(handle, 0)
            if result == 0xFFFFFFFF:
                raise ctypes.WinError()
            return result == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="crewplane-installed-") as temporary:
        smoke(
            args.python,
            args.cli,
            Path(temporary) / "project",
            args.expected_version,
            args.checkout,
            args.diagnostics,
        )


if __name__ == "__main__":
    main()

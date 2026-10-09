"""Run installed-wheel acceptance outside the checkout through pip or uv."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def run(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    output = result.stdout.decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command}\n{output}")
    return output


def install(wheel: Path, root: Path, installer: str) -> tuple[Path, Path]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if installer == "pip":
        environment = root / "pip-environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        run([str(python), "-m", "pip", "install", str(wheel)], root, env)
    else:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required for the uv installed-wheel smoke")
        env.update(
            UV_TOOL_DIR=str(root / "tools"), UV_TOOL_BIN_DIR=str(root / "tool-bin")
        )
        run([uv, "tool", "install", "--python", sys.executable, str(wheel)], root, env)
        binary = root / "tools/crewplane" / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
    return python, binary / ("crewplane.exe" if os.name == "nt" else "crewplane")


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


def smoke(python: Path, cli: Path, project: Path) -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    directories = [str(python.parent)]
    if os.name == "nt":
        directories.append(str(Path(env["SYSTEMROOT"]) / "System32"))
    env["PATH"] = os.pathsep.join(directories)
    assert shutil.which("node", path=env["PATH"]) is None
    assert shutil.which("npm", path=env["PATH"]) is None
    project.mkdir()
    run([str(cli), "--help"], project, env)
    schema = run(
        [
            str(python),
            "-c",
            "from crewplane.version import SCHEMA_VERSION; print(SCHEMA_VERSION)",
        ],
        project,
        env,
    ).strip()
    workflows = project / ".crewplane/workflows"
    (workflows / "modules").mkdir(parents=True)
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
    provider = project / "provider.py"
    provider.write_bytes(
        b"import sys\nsys.stdout.buffer.write(b'provider bytes\\r\\n\\x1a' + sys.stdin.buffer.read())\n"
    )
    config["agents"]["alpha"]["cli_cmd"] = [str(python), str(provider)]
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
    print(
        f"Installed wheel smoke passed: {cli}; no Node/npm; mock, dedupe, force, generic subprocess, hashes and receipts"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", choices=("pip", "uv"), required=True)
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel or next(Path("dist").glob("crewplane-*.whl"))
    with tempfile.TemporaryDirectory(prefix="crewplane-wheel-smoke-") as temporary:
        root = Path(temporary)
        python, cli = install(wheel.resolve(), root, args.installer)
        smoke(python, cli, root / "project")


if __name__ == "__main__":
    main()

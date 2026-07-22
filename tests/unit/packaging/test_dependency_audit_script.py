from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "dependency_audit.py"


def load_dependency_audit_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dependency_audit_script_under_test", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_audits_locked_and_build_dependencies_as_separate_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency_audit = load_dependency_audit_script()
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling==1.30.1", "packaging>=24"]\n'
        'build-backend = "hatchling.build"\n',
        encoding="utf-8",
    )
    locked_requirements = "click==8.3.1 --hash=sha256:abc123\n"
    commands: list[list[str]] = []
    timeouts: list[int] = []
    audit_inputs: list[tuple[Path, str]] = []

    def fake_run(
        command: list[str],
        cwd: Path,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        timeouts.append(timeout)
        assert cwd == tmp_path
        assert check is True
        if command[0] == "uv":
            output_path = Path(command[command.index("--output-file") + 1])
            output_path.write_text(locked_requirements, encoding="utf-8")
        else:
            requirements_path = Path(command[command.index("-r") + 1])
            audit_inputs.append(
                (
                    requirements_path,
                    requirements_path.read_text(encoding="utf-8"),
                )
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dependency_audit.subprocess, "run", fake_run)

    dependency_audit.audit_dependencies(tmp_path)

    assert [command[0] for command in commands] == ["uv", "uvx", "uvx"]
    assert timeouts == [120, 300, 300]
    assert commands[0][1:-1] == [
        "export",
        "--locked",
        "--extra",
        "dev",
        "--no-emit-project",
        "--format",
        "requirements-txt",
        "--output-file",
    ]
    assert all(
        command[1:5]
        == [
            "--python",
            "3.13",
            "pip-audit==2.10.1",
            "-r",
        ]
        and command[-1] == "--strict"
        for command in commands[1:]
    )
    assert audit_inputs[0][1] == locked_requirements
    assert audit_inputs[1][1] == "hatchling==1.30.1\npackaging>=24\n"
    assert audit_inputs[0][0] != audit_inputs[1][0]
    assert all(not path.exists() for path, _contents in audit_inputs)


@pytest.mark.parametrize(
    "pyproject_text",
    [
        '[project]\nname = "crewplane"\n',
        '[build-system]\nrequires = "hatchling==1.30.1"\n',
        "[build-system]\nrequires = []\n",
        '[build-system]\nrequires = [""]\n',
    ],
)
def test_rejects_missing_or_invalid_build_requirements(
    tmp_path: Path,
    pyproject_text: str,
) -> None:
    dependency_audit = load_dependency_audit_script()
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_text, encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"pyproject\.toml \[build-system\]\.requires must be a non-empty list",
    ):
        dependency_audit.read_build_requirements(pyproject_path)


def test_workflow_and_makefile_use_the_dependency_audit_script() -> None:
    workflow = (ROOT / ".github/workflows/vulnerability-scan.yml").read_text(
        encoding="utf-8"
    )
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert '      - "scripts/dependency_audit.py"' in workflow
    assert '      - "Makefile"' in workflow
    assert "run: make dependency-audit" in workflow
    assert "uv export --locked" not in workflow
    assert "\ndependency-audit:\n\t$(PYTHON) scripts/dependency_audit.py\n" in makefile


def test_main_reports_subprocess_timeout_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dependency_audit = load_dependency_audit_script()

    def time_out(root: Path) -> None:
        assert root == Path.cwd()
        raise subprocess.TimeoutExpired(["uvx", "pip-audit"], 300)

    monkeypatch.setattr(dependency_audit, "audit_dependencies", time_out)

    assert dependency_audit.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "timed out after 300 seconds" in captured.err
    assert "Traceback" not in captured.err

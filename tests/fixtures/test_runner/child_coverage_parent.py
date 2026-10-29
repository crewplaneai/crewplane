import json
import os
import subprocess
from pathlib import Path

import crewplane.version  # noqa: F401 -- seed coverage in the parent interpreter
from tests.helpers.execution_environment import build_execution_environment


def test_child() -> None:
    child_python = json.loads(Path("child-python.json").read_text(encoding="utf-8"))
    coverage = (
        {"COVERAGE_PROCESS_CONFIG": os.environ["COVERAGE_PROCESS_CONFIG"]}
        if "COVERAGE_PROCESS_CONFIG" in os.environ
        else {}
    )
    environment = build_execution_environment(
        Path("child-environment"), os.environ["PATH"], coverage
    )
    child = subprocess.run(
        [child_python, str(Path("child_coverage.py").resolve())],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    Path("child.json").write_text(child.stdout, encoding="utf-8")

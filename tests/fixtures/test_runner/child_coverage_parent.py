import json
import os
import subprocess
from pathlib import Path

import crewplane.version  # noqa: F401 -- seed coverage in the parent interpreter


def test_child() -> None:
    child_python = json.loads(Path("child-python.json").read_text(encoding="utf-8"))
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "COVERAGE_PROCESS_CONFIG",
        )
        if key in os.environ
    }
    child = subprocess.run(
        [child_python, str(Path("child_coverage.py").resolve())],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    Path("child.json").write_text(child.stdout, encoding="utf-8")

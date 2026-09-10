import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_make_test_reports_coverage_in_fresh_processes(tmp_path: Path) -> None:
    shutil.copy2(ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "scripts").mkdir()
    shutil.copy2(
        ROOT / "scripts" / "check_coverage.py",
        tmp_path / "scripts" / "check_coverage.py",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "crewplane"\n'
        '[project.scripts]\ncrewplane = "crewplane:main"\n'
        '[tool.pytest.ini_options]\naddopts = ["--disable-plugin-autoload"]\n',
        encoding="utf-8",
    )
    (tmp_path / "crewplane.py").write_text(
        "def choose(flag):\n    if flag:\n        return 1\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "test_example.py").write_text(
        "import coverage\n"
        "from crewplane import choose\n"
        "\n"
        "def forbid_reporting_in_test_process(*args, **kwargs):\n"
        "    raise RuntimeError('coverage reporting reused the test process')\n"
        "\n"
        "def test_choose():\n"
        "    assert choose(True) == 1\n"
        "    assert choose(False) == 2\n"
        "    coverage.Coverage.report = forbid_reporting_in_test_process\n"
        "    coverage.Coverage.json_report = forbid_reporting_in_test_process\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "make",
            "test",
            "HAVE_UV=0",
            f"PYTHON={shlex.quote(sys.executable)}",
        ],
        cwd=tmp_path,
        env={**os.environ, "COVERAGE_FILE": str(tmp_path / ".coverage")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "Statements: 100.00%" in result.stdout
    assert "Branches: 100.00%" in result.stdout
    report = json.loads((tmp_path / ".coverage.json").read_text(encoding="utf-8"))
    assert report["meta"]["branch_coverage"] is True
    assert report["totals"]["missing_lines"] == 0
    assert report["totals"]["missing_branches"] == 0
    assert report["totals"]["num_branches"] > 0

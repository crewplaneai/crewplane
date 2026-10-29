"""Pass test-authored scenario values through the launcher's explicit API."""

import json
import sys
from pathlib import Path

import run_tests

if __name__ == "__main__":
    scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    raise SystemExit(run_tests.main(sys.argv[2:], scenario=scenario))

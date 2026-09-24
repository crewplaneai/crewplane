"""Provider that leaves a descendant for Crewplane to drain."""

import subprocess
import sys
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
path = Path(sys.argv[1])
temporary = path.with_suffix(".tmp")
temporary.write_text(str(child.pid), encoding="utf-8")
temporary.replace(path)

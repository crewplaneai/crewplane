"""A real provider process that corrupts a compiled bundle during its invocation."""

import sys
from pathlib import Path

prompt = sys.stdin.read()
with Path("invocations.txt").open("a", encoding="utf-8") as handle:
    handle.write(prompt.strip() + "\n")
if prompt.strip() == "Mutate bundle." and not Path("clean-run").exists():
    for path in Path(".crewplane/execution-stages").glob(
        "*/preflight/static-files/*.txt"
    ):
        path.write_bytes(b"x" * path.stat().st_size)
print("Provider completed.")

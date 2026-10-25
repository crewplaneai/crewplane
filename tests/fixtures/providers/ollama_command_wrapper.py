"""Run only the plugin command boundary under the real runtime supervisor."""

import asyncio
import json
import sys
from pathlib import Path

from crewplane_ollama.command_executor import execute_command


def main() -> None:
    arguments = json.loads(sys.stdin.read())
    try:
        result = asyncio.run(execute_command(arguments))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    Path(sys.argv[1]).write_text("completed")
    print(json.dumps(result))


if __name__ == "__main__":
    main()

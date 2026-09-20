"""Synthetic OpenCode protocol inputs shared by adapter tests.

Based on anomalyco/opencode v1.18.31, commit
014614d35b397775e5d397a490fc72368c894ec2: packages/opencode/src/cli/cmd/run.ts
and packages/opencode/src/session/{processor,session}.ts.
"""

import json
import sys
from pathlib import Path

FIXTURES = (
    Path(__file__).parents[1]
    / "unit/adapters/invokers/cli_invoker/fixtures/provider_usage"
)


def fixture_text(name: str = "simple") -> str:
    return (FIXTURES / f"opencode_{name}.jsonl").read_text(encoding="utf-8")


def event(kind: str, part_id: str, message: str = "message-1", **fields) -> dict:
    return {
        "type": kind,
        "timestamp": 1_800_000_000_000,
        "sessionID": "session-1",
        "part": {
            "id": part_id,
            "sessionID": "session-1",
            "messageID": message,
            **fields,
        },
    }


def text_event(text: str = "Answer λ", part_id: str = "text-1", **fields) -> dict:
    return event("text", part_id, text=text, time={"end": 2}, **fields)


def finish_event(reason: str = "stop", part_id: str = "finish-1", **fields) -> dict:
    return event("step_finish", part_id, reason=reason, **fields)


def stream(*events: dict) -> str:
    return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events)


def native_tokens(**overrides) -> dict:
    return {
        "input": 10,
        "output": 6,
        "reasoning": 3,
        "cache": {"read": 4, "write": 2},
        **overrides,
    }


def write_fake_executable(directory: Path) -> Path:
    executable = directory / "opencode-double"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1] == 'run'\n"
        "root = Path(os.environ.get('PWD', os.getcwd())).resolve()\n"
        "directory = root\n"
        "if '--dir' in sys.argv:\n"
        "    directory = (root / sys.argv[sys.argv.index('--dir') + 1]).resolve()\n"
        "calls = Path('calls.jsonl')\n"
        "attempt = len(calls.read_text().splitlines()) if calls.exists() else 0\n"
        "scenario = json.loads(Path('scenarios.json').read_text())[attempt]\n"
        "record = {'argv': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode('utf-8'),\n"
        "          'cwd': str(Path.cwd()), 'env': os.environ.get('OPENCODE_TEST_ENV'),\n"
        "          'pwd': os.environ.get('PWD'), 'session_directory': str(directory)}\n"
        "with calls.open('a') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
        "print(scenario.get('stderr', ''), file=sys.stderr)\n"
        "print(scenario.get('stdout', ''), end='')\n"
        "sys.exit(scenario.get('exit', 0))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable

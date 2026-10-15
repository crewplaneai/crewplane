"""Deterministic text CLI with readiness receipts and owned descendants."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def hold(root: Path) -> None:
    child = subprocess.Popen([sys.executable, __file__, "descendant", str(root)])
    try:
        deadline = time.monotonic() + 5
        while not (root / f"descendant-{child.pid}").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("descendant did not become ready")
            time.sleep(0.01)
        (root / f"ready-{os.getpid()}").write_text(str(child.pid))
        while not (root / "release").exists():
            time.sleep(0.01)
    finally:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


def main() -> int:
    if sys.argv[1] == "descendant":
        root = Path(sys.argv[2])
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (root / f"descendant-{os.getpid()}").write_text(str(os.getpid()))
        while True:
            time.sleep(1)
    root = Path(os.environ["CREWPLANE_FAKE_ROOT"])
    root.mkdir(exist_ok=True)
    phase = sys.argv[1]
    prompt = sys.stdin.buffer.read() if phase == "run" else b""
    receipt = {
        "argv": sys.argv,
        "stdin": prompt.decode("utf-8"),
        "cwd": os.getcwd(),
        "host": os.environ.get("OLLAMA_HOST"),
    }
    (root / f"{phase}-{os.getpid()}.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    scenario = os.environ.get("CREWPLANE_FAKE_SCENARIO", "success")
    if scenario == f"hold-{phase}":
        hold(root)
    if phase == "show":
        if scenario == "show-fail" or sys.argv[2] == "missing":
            print("model or server unavailable", file=sys.stderr)
            return 1
        print("local model")
        return 0
    if scenario == "downstream-fail" and prompt.startswith(b"Summarize"):
        print("downstream failed", file=sys.stderr)
        return 1
    if scenario == "fixture-answer" and prompt != b"Reply with OK only.":
        print(Path(os.environ["CREWPLANE_FAKE_ANSWER"]).read_text(), end="")
        return 0
    if scenario == "empty":
        print("   ")
    elif scenario == "stderr":
        print("diagnostic only", file=sys.stderr)
    elif scenario == "partial":
        print("partial answer")
        print("inference failed", file=sys.stderr)
        return 1
    elif scenario in {"quota-fail", "retry"}:
        if scenario == "quota-fail" or len(list(root.glob("run-*.json"))) == 1:
            print("quota exceeded", file=sys.stderr)
            return 7
        print("retry succeeded")
    elif scenario == "large":
        print("本" * 200_000)
    elif scenario == "quota-success":
        print("A story about quota exceeded")
    else:
        print("answer: " + prompt.decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

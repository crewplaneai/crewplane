#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

expected = [
    "run",
    "--locked",
    "--extra",
    "dev",
    "python",
    "scripts/release.py",
    "github-release-plan",
    "--expected-tag",
    os.environ["TAG_NAME"],
]
if sys.argv[1:] != expected:
    print(f"unexpected uv invocation: {sys.argv[1:]}", file=sys.stderr)
    raise SystemExit(2)

state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
if state.get("plan_failure"):
    print("simulated release plan failure", file=sys.stderr)
    raise SystemExit(1)

plan_count = int(state.get("plan_count", 0))
plans = state.get("plans")
if plans:
    plan = plans[min(plan_count, len(plans) - 1)]
    prerelease = str(plan["prerelease"]).lower()
    latest = str(plan["latest"]).lower()
    notes_start_tag = str(plan.get("notes_start_tag", ""))
else:
    prerelease = os.environ["FAKE_PLAN_PRERELEASE"]
    latest = os.environ["FAKE_PLAN_LATEST"]
    notes_start_tag = os.environ["FAKE_PLAN_NOTES_START_TAG"]
state["plan_count"] = plan_count + 1
if state.get("corrupt_during_second_plan") and plan_count == 1 and state["assets"]:
    state["assets"][0]["digest"] = "sha256:" + "0" * 64
state_path.write_text(json.dumps(state), encoding="utf-8")
print(f"prerelease={prerelease}")
print(f"latest={latest}")
print(f"notes_start_tag={notes_start_tag}")

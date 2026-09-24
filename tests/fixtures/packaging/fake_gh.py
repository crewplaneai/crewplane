#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state_path = Path(os.environ["FAKE_GH_STATE"])
log_path = Path(os.environ["GH_CALL_LOG"])
with log_path.open("a", encoding="utf-8") as stream:
    stream.write("\t".join(args) + "\n")
state = json.loads(state_path.read_text(encoding="utf-8"))
release_automation_marker = os.environ["RELEASE_AUTOMATION_MARKER"]


def save() -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")


def boolean_flag(name: str, default: bool) -> bool:
    if name in args:
        return True
    if f"{name}=false" in args:
        return False
    return default


def option_value(name: str, default: str = "") -> str:
    if name not in args:
        return default
    index = args.index(name)
    if index + 1 >= len(args):
        return default
    return args[index + 1]


def artifact_paths() -> list[Path]:
    paths: list[Path] = []
    for value in args[3:]:
        if value.startswith("--"):
            break
        paths.append(Path(value))
    return paths


def uploaded_asset(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "name": path.name,
        "size": len(payload),
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "state": "uploaded",
    }


def corrupt_asset() -> None:
    if state["assets"]:
        state["assets"][0]["digest"] = "sha256:" + "0" * 64


if args[:2] == ["api", "graphql"]:
    state["query_count"] = int(state.get("query_count", 0)) + 1
    save()
    if state.get("query_failure"):
        print("simulated GraphQL failure", file=sys.stderr)
        raise SystemExit(1)
    if not state["exists"]:
        print("release\tabsent")
        raise SystemExit(0)
    assets = list(state["assets"])
    returned_limit = state.get("returned_limit")
    if returned_limit is not None:
        assets = assets[: int(returned_limit)]
    total_count = int(state.get("total_count", len(state["assets"])))
    print(
        "\t".join(
            (
                "release",
                "present",
                "R_test",
                str(state["title"]),
                str(str(state["body"]).startswith(release_automation_marker)).lower(),
                str(state["draft"]).lower(),
                str(state["prerelease"]).lower(),
                str(state["latest"]).lower(),
                str(total_count),
                str(len(assets)),
            )
        )
    )
    for asset in assets:
        print(
            "\t".join(
                (
                    "asset",
                    str(asset["name"]),
                    str(asset["size"]),
                    str(asset["digest"]),
                )
            )
        )
    raise SystemExit(0)

if args[:2] == ["release", "create"]:
    notes = option_value("--notes")
    state.update(
        {
            "exists": True,
            "title": option_value("--title", args[2]),
            "body": f"{notes}\n\nGenerated release notes",
            "draft": True,
            "prerelease": boolean_flag("--prerelease", False),
            "latest": boolean_flag("--latest", False),
            "assets": [uploaded_asset(path) for path in artifact_paths()],
        }
    )
    if state.get("corrupt_after_create"):
        corrupt_asset()
    save()
    raise SystemExit(0)

if args[:2] == ["release", "upload"]:
    by_name = {asset["name"]: asset for asset in state["assets"]}
    for path in artifact_paths():
        asset = uploaded_asset(path)
        by_name[asset["name"]] = asset
    state["assets"] = list(by_name.values())
    if state.get("corrupt_after_upload"):
        corrupt_asset()
    save()
    raise SystemExit(0)

if args[:2] == ["release", "edit"]:
    state["title"] = option_value("--title", str(state["title"]))
    state["body"] = option_value("--notes", str(state["body"]))
    state["draft"] = boolean_flag("--draft", state["draft"])
    state["prerelease"] = boolean_flag("--prerelease", state["prerelease"])
    state["latest"] = boolean_flag("--latest", state["latest"])
    if state.get("corrupt_after_edit") == "asset":
        corrupt_asset()
    elif state.get("corrupt_after_edit") == "latest":
        state["latest"] = not state["latest"]
    elif state.get("corrupt_after_edit") == "title":
        state["title"] = "Wrong title"
    elif state.get("corrupt_after_edit") == "body":
        state["body"] = "Unmarked release notes"
    save()
    raise SystemExit(0)

print(f"unexpected gh invocation: {args}", file=sys.stderr)
raise SystemExit(2)

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GITHUB_RELEASE_SCRIPT = ROOT / "scripts" / "publish_github_release.sh"
EXPECTED_ASSETS = (
    "crewplane-1.2.3-py3-none-any.whl",
    "crewplane-1.2.3.tar.gz",
)
RELEASE_AUTOMATION_MARKER = "<!-- crewplane-release:v1 -->"
GENERATED_RELEASE_NOTES = f"{RELEASE_AUTOMATION_MARKER}\n\nGenerated release notes"
MUTATING_COMMANDS = ("release\tupload", "release\tedit", "release\tcreate")


def asset_record(name: str, content: str | None = None) -> dict[str, object]:
    payload = (content if content is not None else name).encode()
    return {
        "name": name,
        "size": len(payload),
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "state": "uploaded",
    }


def exact_assets() -> list[dict[str, object]]:
    return [asset_record(name) for name in EXPECTED_ASSETS]


def release_state(
    *,
    exists: bool = True,
    draft: bool = False,
    prerelease: bool = False,
    latest: bool = True,
    assets: list[dict[str, object]] | None = None,
    **faults: object,
) -> dict[str, object]:
    return {
        "exists": exists,
        "title": "v1.2.3",
        "body": GENERATED_RELEASE_NOTES,
        "draft": draft,
        "prerelease": prerelease,
        "latest": latest,
        "assets": exact_assets() if assets is None else assets,
        **faults,
    }


def write_fake_gh(path: Path) -> None:
    path.write_text(
        r"""#!/usr/bin/env python3
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
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_fake_uv(path: Path) -> None:
    path.write_text(
        r"""#!/usr/bin/env python3
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
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def run_release(
    tmp_path: Path,
    initial_state: dict[str, object],
    *,
    expected_prerelease: str = "false",
    expected_latest: str = "true",
    expected_notes_start_tag: str = "",
    repository: str = "crewplaneai/crewplane",
) -> tuple[
    subprocess.CompletedProcess[str],
    tuple[str, ...],
    dict[str, object],
]:
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in EXPECTED_ASSETS:
        (dist / name).write_text(name, encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_gh(fake_bin / "gh")
    write_fake_uv(fake_bin / "uv")
    call_log = tmp_path / "gh-calls.log"
    call_log.touch()
    state_path = tmp_path / "gh-state.json"
    state_path.write_text(json.dumps(initial_state), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "FAKE_PLAN_LATEST": expected_latest,
            "FAKE_PLAN_NOTES_START_TAG": expected_notes_start_tag,
            "FAKE_PLAN_PRERELEASE": expected_prerelease,
            "FAKE_GH_STATE": str(state_path),
            "GH_CALL_LOG": str(call_log),
            "GH_REPO": repository,
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "RELEASE_AUTOMATION_MARKER": RELEASE_AUTOMATION_MARKER,
            "TAG_NAME": "v1.2.3",
        }
    )

    result = subprocess.run(
        [str(GITHUB_RELEASE_SCRIPT), str(dist)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    return result, calls, final_state


def assert_no_release_mutations(calls: tuple[str, ...]) -> None:
    assert not any(call.startswith(MUTATING_COMMANDS) for call in calls)


@pytest.mark.parametrize(
    ("prerelease", "latest"),
    (("false", "true"), ("false", "false"), ("true", "false")),
)
def test_matching_published_release_is_verified_without_mutation(
    tmp_path: Path,
    prerelease: str,
    latest: str,
) -> None:
    result, calls, _state = run_release(
        tmp_path,
        release_state(prerelease=prerelease == "true", latest=latest == "true"),
        expected_prerelease=prerelease,
        expected_latest=latest,
    )

    assert result.returncode == 0, result.stderr
    assert (
        result.stdout
        == "Verified existing published GitHub Release v1.2.3; leaving it unchanged.\n"
    )
    assert calls[0].startswith("api\tgraphql")
    assert_no_release_mutations(calls)


@pytest.mark.parametrize(
    ("state", "expected_error"),
    (
        (
            release_state(assets=exact_assets()[:-1]),
            "assets do not match",
        ),
        (
            release_state(assets=[*exact_assets(), asset_record("unexpected.txt")]),
            "assets do not match",
        ),
        (
            release_state(
                assets=[
                    {**exact_assets()[0], "size": 999},
                    exact_assets()[1],
                ]
            ),
            "assets do not match",
        ),
        (
            release_state(
                assets=[
                    {**exact_assets()[0], "digest": "sha256:" + "0" * 64},
                    exact_assets()[1],
                ]
            ),
            "assets do not match",
        ),
        (
            release_state(
                assets=[
                    {**exact_assets()[0], "digest": ""},
                    exact_assets()[1],
                ]
            ),
            "incomplete size or digest metadata",
        ),
        (release_state(prerelease=True), "prerelease state does not match"),
        (release_state(latest=False), "Latest state does not match"),
        (release_state(title="Wrong title"), "title does not match"),
        (
            release_state(body="Unmarked release notes"),
            "notes are missing the release automation marker",
        ),
    ),
)
def test_published_release_mismatch_fails_without_mutation(
    tmp_path: Path,
    state: dict[str, object],
    expected_error: str,
) -> None:
    result, calls, _state = run_release(tmp_path, state)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert_no_release_mutations(calls)


@pytest.mark.parametrize(
    ("prerelease", "latest", "expected_error"),
    (
        ("invalid", "false", "malformed output"),
        ("false", "invalid", "malformed output"),
        ("true", "true", "marked a prerelease as GitHub Latest"),
    ),
)
def test_invalid_release_plan_fails_before_querying_github(
    tmp_path: Path,
    prerelease: str,
    latest: str,
    expected_error: str,
) -> None:
    result, calls, _state = run_release(
        tmp_path,
        release_state(),
        expected_prerelease=prerelease,
        expected_latest=latest,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert calls == ()


@pytest.mark.parametrize(
    "notes_start_tag",
    ("--not-a-tag", "v1.1.0\nunexpected=true"),
)
def test_invalid_release_notes_start_tag_fails_before_querying_github(
    tmp_path: Path,
    notes_start_tag: str,
) -> None:
    result, calls, _state = run_release(
        tmp_path,
        release_state(),
        expected_notes_start_tag=notes_start_tag,
    )

    assert result.returncode != 0
    assert "malformed output" in result.stderr
    assert calls == ()


def test_github_query_failure_never_creates_a_release(tmp_path: Path) -> None:
    result, calls, _state = run_release(
        tmp_path,
        release_state(exists=False, query_failure=True),
    )

    assert result.returncode != 0
    assert "refusing to mutate" in result.stderr
    assert_no_release_mutations(calls)


@pytest.mark.parametrize(
    ("prerelease", "latest", "expected_edit_flags"),
    (
        ("false", "true", ("--prerelease=false", "--latest")),
        ("false", "false", ("--prerelease=false", "--latest=false")),
        ("true", "false", ("--prerelease", "--latest=false")),
    ),
)
def test_absent_release_is_created_as_verified_draft_then_published(
    tmp_path: Path,
    prerelease: str,
    latest: str,
    expected_edit_flags: tuple[str, str],
) -> None:
    result, calls, final_state = run_release(
        tmp_path,
        release_state(exists=False, assets=[]),
        expected_prerelease=prerelease,
        expected_latest=latest,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Published GitHub Release v1.2.3.\n"
    create = next(call for call in calls if call.startswith("release\tcreate"))
    edit = next(call for call in calls if call.startswith("release\tedit"))
    assert "\t--draft\t" in create
    assert "\t--notes-start-tag\t" not in create
    assert f"\t--notes\t{RELEASE_AUTOMATION_MARKER}\t" in create
    assert "\t--title\tv1.2.3\t" in create
    assert all(f"\t{flag}" in edit for flag in expected_edit_flags)
    assert final_state["draft"] is False
    assert final_state["prerelease"] is (prerelease == "true")
    assert final_state["latest"] is (latest == "true")
    assert final_state["title"] == "v1.2.3"
    assert final_state["body"] == GENERATED_RELEASE_NOTES
    assert final_state["assets"] == exact_assets()


def test_delayed_release_uses_verified_notes_predecessor(tmp_path: Path) -> None:
    result, calls, final_state = run_release(
        tmp_path,
        release_state(exists=False, assets=[]),
        expected_notes_start_tag="v1.1.0",
    )

    assert result.returncode == 0, result.stderr
    create = next(call for call in calls if call.startswith("release\tcreate"))
    assert "\t--notes-start-tag\tv1.1.0\t" in create
    assert final_state["draft"] is False


@pytest.mark.parametrize(
    "initial_assets",
    (
        [],
        exact_assets()[:-1],
        [asset_record(EXPECTED_ASSETS[0], "wrong bytes")],
        [{**exact_assets()[0], "digest": ""}],
    ),
)
def test_existing_draft_assets_are_clobbered_and_verified_before_publish(
    tmp_path: Path,
    initial_assets: list[dict[str, object]],
) -> None:
    result, calls, final_state = run_release(
        tmp_path,
        release_state(draft=True, latest=False, assets=initial_assets),
    )

    assert result.returncode == 0, result.stderr
    upload_index = next(
        index for index, call in enumerate(calls) if call.startswith("release\tupload")
    )
    edit_index = next(
        index for index, call in enumerate(calls) if call.startswith("release\tedit")
    )
    assert upload_index < edit_index
    assert final_state["assets"] == exact_assets()
    assert final_state["draft"] is False


@pytest.mark.parametrize(
    ("metadata", "expected_error"),
    (
        ({"title": "Wrong title"}, "title does not match the release tag"),
        (
            {"body": "Manual release notes"},
            "notes are missing the release automation marker",
        ),
    ),
)
def test_unverified_draft_metadata_fails_before_mutation(
    tmp_path: Path,
    metadata: dict[str, str],
    expected_error: str,
) -> None:
    result, calls, final_state = run_release(
        tmp_path,
        release_state(draft=True, latest=False, **metadata),
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert_no_release_mutations(calls)
    assert final_state["draft"] is True


def test_draft_with_unexpected_asset_fails_without_mutation(tmp_path: Path) -> None:
    result, calls, _state = run_release(
        tmp_path,
        release_state(
            draft=True,
            latest=False,
            assets=[*exact_assets(), asset_record("unexpected.txt")],
        ),
    )

    assert result.returncode != 0
    assert "unexpected assets" in result.stderr
    assert_no_release_mutations(calls)


def test_corrupt_upload_prevents_draft_publication(tmp_path: Path) -> None:
    result, calls, final_state = run_release(
        tmp_path,
        release_state(
            draft=True,
            latest=False,
            assets=[],
            corrupt_after_upload=True,
        ),
    )

    assert result.returncode != 0
    assert "Draft assets do not match" in result.stderr
    assert any(call.startswith("release\tupload") for call in calls)
    assert not any(call.startswith("release\tedit") for call in calls)
    assert final_state["draft"] is True


@pytest.mark.parametrize("corruption", ("asset", "latest", "title", "body"))
def test_post_publish_mismatch_is_detected(
    tmp_path: Path,
    corruption: str,
) -> None:
    result, calls, final_state = run_release(
        tmp_path,
        release_state(
            exists=False,
            assets=[],
            corrupt_after_edit=corruption,
        ),
    )

    assert result.returncode != 0
    assert any(call.startswith("release\tedit") for call in calls)
    assert final_state["draft"] is False
    assert "Published release" in result.stderr


def test_truncated_asset_query_fails_without_mutation(tmp_path: Path) -> None:
    result, calls, _state = run_release(
        tmp_path,
        release_state(total_count=3, returned_limit=1),
    )

    assert result.returncode != 0
    assert "truncated or internally inconsistent" in result.stderr
    assert_no_release_mutations(calls)


def test_latest_plan_is_refreshed_immediately_before_publication(
    tmp_path: Path,
) -> None:
    result, calls, final_state = run_release(
        tmp_path,
        release_state(
            exists=False,
            assets=[],
            plans=[
                {"prerelease": False, "latest": True},
                {"prerelease": False, "latest": False},
            ],
        ),
    )

    assert result.returncode == 0, result.stderr
    edit = next(call for call in calls if call.startswith("release\tedit"))
    assert "\t--latest=false" in edit
    assert final_state["latest"] is False
    assert final_state["plan_count"] == 2


def test_draft_mutation_during_second_plan_prevents_publication(
    tmp_path: Path,
) -> None:
    result, calls, final_state = run_release(
        tmp_path,
        release_state(
            exists=False,
            assets=[],
            corrupt_during_second_plan=True,
        ),
    )

    assert result.returncode != 0
    assert "Final draft assets do not match" in result.stderr
    assert not any(call.startswith("release\tedit") for call in calls)
    assert final_state["draft"] is True


def test_same_owner_and_repository_name_is_valid(tmp_path: Path) -> None:
    result, _calls, _state = run_release(
        tmp_path,
        release_state(),
        repository="crewplane/crewplane",
    )

    assert result.returncode == 0, result.stderr

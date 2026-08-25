from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.release import homebrew, state
from tests.helpers import isolated_git as _isolated_git_support
from tests.unit.packaging.release_tool_support import (
    constant,
    matching_pypi,
    release_state_fixture,
    write_manifest,
)

isolated_git = _isolated_git_support.isolated_git


class LocalTapRunner(state.CommandRunner):
    def __init__(self) -> None:
        self.pull_request: dict[str, object] | None = None
        self.gh_calls: list[tuple[str, ...]] = []

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: int | None = state.COMMAND_TIMEOUT_SECONDS,
        capture_output: bool = True,
        check: bool = True,
    ) -> state.CommandResult:
        command_tuple = tuple(command)
        if command_tuple == ("git", "remote", "get-url", "origin"):
            return state.CommandResult(
                command_tuple,
                0,
                "https://github.com/crewplaneai/homebrew-crewplane.git\n",
                "",
            )
        if command_tuple[:2] == ("gh", "api"):
            self.gh_calls.append(command_tuple)
            records = [] if self.pull_request is None else [self.pull_request]
            return state.CommandResult(command_tuple, 0, json.dumps(records), "")
        if command_tuple[:3] == ("gh", "pr", "create"):
            self.gh_calls.append(command_tuple)
            branch = command_value(command_tuple, "--head")
            title = command_value(command_tuple, "--title")
            body = command_value(command_tuple, "--body")
            head_sha = super().run(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip()
            self.pull_request = {
                "number": 7,
                "state": "open",
                "merged_at": None,
                "html_url": (
                    "https://github.com/crewplaneai/homebrew-crewplane/pull/7"
                ),
                "body": body,
                "head": {
                    "sha": head_sha,
                    "ref": branch,
                    "repo": {"full_name": homebrew.TAP_REPOSITORY},
                },
                "base": {"ref": "main"},
                "title": title,
            }
            return state.CommandResult(
                command_tuple, 0, str(self.pull_request["html_url"]), ""
            )
        if command_tuple[:3] == ("gh", "pr", "reopen"):
            self.gh_calls.append(command_tuple)
            assert self.pull_request is not None
            self.pull_request["state"] = "open"
            return state.CommandResult(command_tuple, 0, "", "")
        result = super().run(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            capture_output=capture_output,
            check=check,
        )
        if command_tuple[:2] == ("git", "push") and self.pull_request is not None:
            head = self.pull_request["head"]
            assert isinstance(head, dict)
            head["sha"] = (
                super().run(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip()
            )
        return result


@dataclass(frozen=True)
class PublicationSetup:
    context: state.ReleaseContext
    options: homebrew.HomebrewPrOptions
    runner: LocalTapRunner
    tap: Path
    origin: Path
    seed: Path


def command_value(command: tuple[str, ...], option: str) -> str:
    return command[command.index(option) + 1]


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def initialize_source_repository(root: Path, tag: str) -> str:
    git(root, "init", "--initial-branch=master")
    git(root, "config", "user.name", "Release Test")
    git(root, "config", "user.email", "release@example.com")
    git(root, "add", ".")
    git(root, "commit", "-m", "release source")
    git(root, "tag", tag)
    return git(root, "rev-parse", "HEAD")


def initialize_tap_checkout(root: Path, formula: str) -> tuple[Path, Path, Path]:
    origin = root / "tap-origin.git"
    seed = root / "tap-seed"
    tap = root / "homebrew-tap"
    git(root, "init", "--bare", "--initial-branch=main", str(origin))
    seed.mkdir()
    git(seed, "init", "--initial-branch=main")
    git(seed, "config", "user.name", "Tap Test")
    git(seed, "config", "user.email", "tap@example.com")
    (seed / "Formula").mkdir()
    (seed / "Formula" / "crewplane.rb").write_text(formula, encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "initial formula")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "origin", "main")
    git(root, "clone", str(origin), str(tap))
    git(tap, "config", "user.name", "crewplane-release[bot]")
    git(tap, "config", "user.email", "release-bot@example.com")
    return tap, origin, seed


def prepared_release(
    root: Path,
) -> tuple[state.ReleaseContext, state.ReleaseManifest, state.PypiRelease]:
    context, manifest, _formula, _git = release_state_fixture(root, "1.2.3")
    state.sync_homebrew_formula_metadata(
        context, manifest.artifact("pypi_sdist").sha256
    )
    write_manifest(root, manifest)
    release = matching_pypi(context, manifest)
    return context, manifest, release


def publication_setup(root: Path, monkeypatch: pytest.MonkeyPatch) -> PublicationSetup:
    context, _manifest, release = prepared_release(root)
    source_commit = initialize_source_repository(root, context.version.tag)
    monkeypatch.setattr(homebrew, "query_pypi_release", constant(release))
    homebrew.prepare_formula(root, context.version.tag)
    tap, origin, seed = initialize_tap_checkout(
        root,
        """class Crewplane < Formula
  url "https://files.pythonhosted.org/packages/aa/bb/cccccccccccccccccccc/crewplane-1.0.0.tar.gz"
  sha256 "1111111111111111111111111111111111111111111111111111111111111111"
end
""",
    )
    runner = LocalTapRunner()
    monkeypatch.setenv("GH_TOKEN", "installation-token")
    options = homebrew.HomebrewPrOptions(
        expected_tag=context.version.tag,
        source_commit=source_commit,
        tap_root=tap.relative_to(root),
        execute=True,
    )
    return PublicationSetup(context, options, runner, tap, origin, seed)


def test_prepare_homebrew_formula_uses_verified_canonical_pypi_sdist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, release = prepared_release(tmp_path)
    source_formula = tmp_path / "packaging" / "homebrew" / "Formula" / "crewplane.rb"
    source_text = source_formula.read_text(encoding="utf-8")
    monkeypatch.setattr(homebrew, "query_pypi_release", constant(release))

    output = homebrew.prepare_formula(tmp_path, context.version.tag)

    rendered = output.read_text(encoding="utf-8")
    sdist = release.files[context.sdist_filename]
    assert output == tmp_path / homebrew.TAP_FORMULA_ARTIFACT
    assert f'  url "{sdist.url}"' in rendered
    assert f'  sha256 "{manifest.artifact("pypi_sdist").sha256}"' in rendered
    assert '\n  version "' not in rendered
    assert '  depends_on "libyaml"' in rendered
    assert rendered.count('  resource "') == source_text.count('  resource "')
    assert source_formula.read_text(encoding="utf-8") == source_text


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    (
        (
            lambda release, context: release.files.__setitem__(
                context.sdist_filename,
                state.PypiFile(
                    filename=context.sdist_filename,
                    size=10,
                    sha256="a" * 64,
                    url=f"https://example.com/{context.sdist_filename}",
                    package_type="sdist",
                    yanked=False,
                ),
            ),
            "canonical files.pythonhosted.org URL",
        ),
        (
            lambda release, context: release.files.__setitem__(
                context.sdist_filename,
                state.PypiFile(
                    filename=context.sdist_filename,
                    size=10,
                    sha256="0" * 64,
                    url=(
                        "https://files.pythonhosted.org/packages/aa/bb/"
                        f"{'c' * 60}/{context.sdist_filename}"
                    ),
                    package_type="sdist",
                    yanked=False,
                ),
            ),
            "PyPI hash mismatch",
        ),
        (
            lambda release, context: release.files.__setitem__(
                context.sdist_filename,
                state.PypiFile(
                    filename=context.sdist_filename,
                    size=10,
                    sha256="a" * 64,
                    url=(
                        "https://files.pythonhosted.org/packages/aa/bb/"
                        f"{'c' * 60}/{context.sdist_filename}"
                    ),
                    package_type="sdist",
                    yanked=True,
                ),
            ),
            "yanked",
        ),
    ),
)
def test_prepare_homebrew_formula_rejects_untrusted_sdist_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    expected_error: str,
) -> None:
    context, _manifest, release = prepared_release(tmp_path)
    mutate(release, context)
    monkeypatch.setattr(homebrew, "query_pypi_release", constant(release))

    with pytest.raises(state.ReleaseError, match=expected_error):
        homebrew.prepare_formula(tmp_path, context.version.tag)

    assert not (tmp_path / homebrew.TAP_FORMULA_ARTIFACT).exists()


@pytest.mark.parametrize(
    ("version", "latest_stable", "expected"),
    (
        ("1.2.3-alpha.1", "1.2.2", homebrew.HomebrewEligibility.PRERELEASE),
        ("1.2.3", "1.2.4", homebrew.HomebrewEligibility.SUPERSEDED),
        ("1.2.3", "1.2.3", homebrew.HomebrewEligibility.ELIGIBLE),
    ),
)
def test_homebrew_eligibility_tracks_latest_stable_release(
    tmp_path: Path,
    version: str,
    latest_stable: str,
    expected: homebrew.HomebrewEligibility,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path, version)
    release = matching_pypi(context, manifest, latest_stable=latest_stable)

    assert homebrew.release_eligibility(context, release) == expected


def test_pypi_lookup_retains_homebrew_sdist_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path, "1.2.3")
    artifact = manifest.artifact("pypi_sdist")
    canonical_url = (
        f"https://files.pythonhosted.org/packages/aa/bb/{'c' * 60}/{artifact.filename}"
    )
    payload = {
        "releases": {
            context.version.python: [
                {
                    "filename": artifact.filename,
                    "size": artifact.size,
                    "digests": {"sha256": artifact.sha256},
                    "url": canonical_url,
                    "packagetype": "sdist",
                    "yanked": False,
                }
            ]
        }
    }
    monkeypatch.setattr(state.state_types, "fetch_registry_json", constant(payload))

    release = state.query_pypi_release(context)

    assert release.files[artifact.filename] == state.PypiFile(
        filename=artifact.filename,
        size=artifact.size,
        sha256=artifact.sha256,
        url=canonical_url,
        package_type="sdist",
        yanked=False,
    )


def test_homebrew_formula_comparison_allows_only_bottle_metadata() -> None:
    expected = """class Crewplane < Formula
  url "https://files.pythonhosted.org/packages/aa/crewplane-1.2.3.tar.gz"
  head "https://github.com/crewplaneai/crewplane.git", branch: "master"

  depends_on "libyaml"
end
"""
    bottled = """class Crewplane < Formula
  url "https://files.pythonhosted.org/packages/aa/crewplane-1.2.3.tar.gz"
  head "https://github.com/crewplaneai/crewplane.git", branch: "master"

  bottle do
    sha256 cellar: :any_skip_relocation, arm64_sequoia: "abc"
  end

  depends_on "libyaml"
end
"""

    assert homebrew.formula_without_bottle_block(bottled) == expected
    assert homebrew.formula_without_bottle_block(expected) == expected


def test_pull_request_snapshot_rejects_ambiguous_or_unowned_prs() -> None:
    owned = {
        "number": 7,
        "state": "open",
        "merged_at": None,
        "html_url": "https://github.com/crewplaneai/homebrew-crewplane/pull/7",
        "body": homebrew.AUTOMATION_MARKER + "\nbody",
        "head": {
            "sha": "a" * 40,
            "ref": "automation/crewplane-1.2.3",
            "repo": {"full_name": homebrew.TAP_REPOSITORY},
        },
        "base": {"ref": "main"},
        "title": "crewplane 1.2.3",
    }

    snapshot = homebrew.parse_pull_request_snapshot(json.dumps([owned]))

    assert snapshot is not None
    assert snapshot.number == 7
    with pytest.raises(state.ReleaseError, match="multiple pull requests"):
        homebrew.parse_pull_request_snapshot(json.dumps([owned, owned]))
    owned["head"]["repo"]["full_name"] = "attacker/homebrew-crewplane"
    with pytest.raises(state.ReleaseError, match="invalid data"):
        homebrew.parse_pull_request_snapshot(json.dumps([owned]))
    owned["head"]["repo"]["full_name"] = homebrew.TAP_REPOSITORY
    owned["body"] = "edited by hand"
    with pytest.raises(state.ReleaseError, match="automation marker"):
        homebrew.parse_pull_request_snapshot(json.dumps([owned]))


@pytest.mark.usefixtures("isolated_git")
def test_publish_homebrew_pr_creates_one_branch_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = publication_setup(tmp_path, monkeypatch)

    first_result = homebrew.publish_formula_pull_request(
        tmp_path, setup.runner, setup.options
    )
    second_result = homebrew.publish_formula_pull_request(
        tmp_path, setup.runner, setup.options
    )

    assert first_result == second_result == 0
    branch = f"automation/crewplane-{setup.context.version.project}"
    branch_formula = git(
        setup.origin, "show", f"refs/heads/{branch}:{homebrew.TAP_FORMULA_PATH}"
    )
    expected_formula = (tmp_path / homebrew.TAP_FORMULA_ARTIFACT).read_text(
        encoding="utf-8"
    )
    assert branch_formula + "\n" == expected_formula
    assert git(setup.origin, "rev-parse", "refs/heads/main") != git(
        setup.origin, "rev-parse", f"refs/heads/{branch}"
    )
    assert setup.runner.pull_request is not None
    assert setup.runner.pull_request["state"] == "open"
    assert str(setup.runner.pull_request["body"]).startswith(homebrew.AUTOMATION_MARKER)
    create_calls = [
        call for call in setup.runner.gh_calls if call[:3] == ("gh", "pr", "create")
    ]
    assert len(create_calls) == 1
    query_calls = [call for call in setup.runner.gh_calls if call[:2] == ("gh", "api")]
    assert query_calls
    assert all(
        f"head={homebrew.TAP_OWNER}:{branch}" in call and "per_page=2" in call
        for call in query_calls
    )
    assert os.environ["GH_TOKEN"] == "installation-token"


@pytest.mark.usefixtures("isolated_git")
def test_publish_homebrew_pr_recovers_missing_and_closed_pull_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = publication_setup(tmp_path, monkeypatch)
    homebrew.publish_formula_pull_request(tmp_path, setup.runner, setup.options)

    setup.runner.pull_request = None
    homebrew.publish_formula_pull_request(tmp_path, setup.runner, setup.options)
    assert setup.runner.pull_request is not None
    setup.runner.pull_request["state"] = "closed"
    homebrew.publish_formula_pull_request(tmp_path, setup.runner, setup.options)

    create_calls = [
        call for call in setup.runner.gh_calls if call[:3] == ("gh", "pr", "create")
    ]
    reopen_calls = [
        call for call in setup.runner.gh_calls if call[:3] == ("gh", "pr", "reopen")
    ]
    assert len(create_calls) == 2
    assert len(reopen_calls) == 1
    assert setup.runner.pull_request["state"] == "open"


@pytest.mark.usefixtures("isolated_git")
def test_publish_homebrew_pr_rebases_stale_automation_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = publication_setup(tmp_path, monkeypatch)
    homebrew.publish_formula_pull_request(tmp_path, setup.runner, setup.options)
    branch = f"automation/crewplane-{setup.context.version.project}"
    original_branch_sha = git(setup.origin, "rev-parse", f"refs/heads/{branch}")
    (setup.seed / "README.md").write_text("tap documentation\n", encoding="utf-8")
    git(setup.seed, "add", "README.md")
    git(setup.seed, "commit", "-m", "document tap")
    git(setup.seed, "push", "origin", "main")

    result = homebrew.publish_formula_pull_request(
        tmp_path, setup.runner, setup.options
    )

    current_main = git(setup.origin, "rev-parse", "refs/heads/main")
    updated_branch = git(setup.origin, "rev-parse", f"refs/heads/{branch}")
    branch_parent = git(setup.origin, "show", "-s", "--format=%P", updated_branch)
    assert result == 0
    assert updated_branch != original_branch_sha
    assert branch_parent == current_main
    assert setup.runner.pull_request is not None
    head = setup.runner.pull_request["head"]
    assert isinstance(head, dict)
    assert head["sha"] == updated_branch


@pytest.mark.usefixtures("isolated_git")
def test_publish_homebrew_pr_rejects_modified_automation_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = publication_setup(tmp_path, monkeypatch)
    homebrew.publish_formula_pull_request(tmp_path, setup.runner, setup.options)
    branch = f"automation/crewplane-{setup.context.version.project}"
    formula_path = setup.tap / homebrew.TAP_FORMULA_PATH
    formula_path.write_text(
        formula_path.read_text(encoding="utf-8") + "# manual change\n",
        encoding="utf-8",
    )
    git(setup.tap, "add", str(homebrew.TAP_FORMULA_PATH))
    git(setup.tap, "commit", "-m", "manual change")
    git(setup.tap, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    modified_sha = git(setup.tap, "rev-parse", "HEAD")
    assert setup.runner.pull_request is not None
    head = setup.runner.pull_request["head"]
    assert isinstance(head, dict)
    head["sha"] = modified_sha

    with pytest.raises(state.ReleaseError, match="not owned by release automation"):
        homebrew.publish_formula_pull_request(tmp_path, setup.runner, setup.options)

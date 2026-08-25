from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from packaging.version import InvalidVersion, Version

from .state import (
    CommandRunner,
    PypiFile,
    PypiRelease,
    ReleaseContext,
    ReleaseError,
    ReleaseManifest,
    manifest_context_issues,
    query_pypi_release,
    read_formula_state,
    read_manifest,
    read_release_context,
    verify_formula_state_for_release,
    verify_pypi_artifacts,
)

TAP_OWNER = "crewplaneai"
TAP_REPOSITORY = f"{TAP_OWNER}/homebrew-crewplane"
TAP_BASE_BRANCH = "main"
TAP_FORMULA_PATH = Path("Formula/crewplane.rb")
TAP_FORMULA_ARTIFACT = Path(".release/homebrew/Formula/crewplane.rb")
AUTOMATION_MARKER = "<!-- crewplane-homebrew-release:v1 -->"
SOURCE_REPOSITORY = "crewplaneai/crewplane"


class HomebrewEligibility(StrEnum):
    ELIGIBLE = "eligible"
    PRERELEASE = "prerelease"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class HomebrewRelease:
    context: ReleaseContext
    manifest: ReleaseManifest
    pypi: PypiRelease
    sdist: PypiFile
    formula: str


@dataclass(frozen=True)
class HomebrewPrOptions:
    expected_tag: str
    source_commit: str
    tap_root: Path
    execute: bool


@dataclass(frozen=True)
class PullRequestSnapshot:
    number: int
    state: str
    merged_at: str | None
    url: str
    body: str
    head_sha: str
    head_branch: str
    base_branch: str
    title: str


def prepare_formula(root: Path, expected_tag: str) -> Path:
    release = verified_homebrew_release(root, expected_tag)
    require_eligible_release(release)
    output = root / TAP_FORMULA_ARTIFACT
    ensure_safe_formula_output(root, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(release.formula, encoding="utf-8")
    print(f"Prepared Homebrew tap formula: {TAP_FORMULA_ARTIFACT}")
    return output


def publish_formula_pull_request(
    root: Path, runner: CommandRunner, options: HomebrewPrOptions
) -> int:
    if not options.execute:
        print("Dry run only. Re-run with --execute in the GitHub release workflow.")
        return 1
    validate_source_identity(root, runner, options)
    release = verified_homebrew_release(root, options.expected_tag)
    eligibility = release_eligibility(release.context, release.pypi)
    if eligibility != HomebrewEligibility.ELIGIBLE:
        print_homebrew_skip(release.context, eligibility)
        return 0
    verify_prepared_formula(root, release.formula)
    require_github_authentication()
    tap_root = resolve_tap_root(root, options.tap_root)
    verify_tap_checkout(tap_root, runner)
    return update_pull_request(tap_root, runner, release, options.source_commit)


def verified_homebrew_release(root: Path, expected_tag: str) -> HomebrewRelease:
    context = read_release_context(root)
    if expected_tag != context.version.tag:
        raise ReleaseError(
            f"expected tag {expected_tag!r} does not match {context.version.tag!r}"
        )
    manifest = read_manifest(root)
    issues = manifest_context_issues(context, manifest)
    formula_state = read_formula_state(context)
    issues.extend(verify_formula_state_for_release(context, formula_state, manifest))
    pypi = query_pypi_release(context)
    issues.extend(verify_pypi_artifacts(context, pypi, manifest))
    if issues:
        raise ReleaseError(
            "Homebrew formula verification failed:\n  " + "\n  ".join(issues)
        )
    sdist = pypi.files[context.sdist_filename]
    validate_pypi_sdist(context, sdist)
    source = formula_state.path.read_text(encoding="utf-8")
    formula = render_formula(source, sdist.url, sdist.sha256)
    return HomebrewRelease(context, manifest, pypi, sdist, formula)


def release_eligibility(
    context: ReleaseContext, pypi: PypiRelease
) -> HomebrewEligibility:
    if context.version.is_prerelease:
        return HomebrewEligibility.PRERELEASE
    try:
        latest = Version(pypi.latest_stable)
    except InvalidVersion as error:
        raise ReleaseError("PyPI has no verifiable stable Homebrew release") from error
    target = Version(context.version.python)
    if latest < target:
        raise ReleaseError(
            "PyPI reports a stable release older than the target version"
        )
    if latest > target:
        return HomebrewEligibility.SUPERSEDED
    return HomebrewEligibility.ELIGIBLE


def require_eligible_release(release: HomebrewRelease) -> None:
    eligibility = release_eligibility(release.context, release.pypi)
    if eligibility != HomebrewEligibility.ELIGIBLE:
        raise ReleaseError(
            f"Homebrew formula generation skipped for {eligibility.value} release "
            f"{release.context.version.project}"
        )


def validate_pypi_sdist(context: ReleaseContext, sdist: PypiFile) -> None:
    if sdist.package_type != "sdist":
        raise ReleaseError("PyPI release metadata does not identify the sdist")
    if sdist.yanked:
        raise ReleaseError("PyPI sdist is yanked")
    parsed = urllib.parse.urlsplit(sdist.url)
    decoded_path = urllib.parse.unquote(parsed.path)
    canonical_path = re.fullmatch(
        rf"/packages/[0-9a-f]{{2}}/[0-9a-f]{{2}}/[0-9a-f]{{20,}}/"
        rf"{re.escape(context.sdist_filename)}",
        decoded_path,
    )
    if (
        parsed.scheme != "https"
        or parsed.netloc != "files.pythonhosted.org"
        or parsed.query
        or parsed.fragment
        or canonical_path is None
    ):
        raise ReleaseError(
            "PyPI sdist does not use a canonical files.pythonhosted.org URL"
        )


def render_formula(source: str, sdist_url: str, sha256: str) -> str:
    rendered = replace_one_formula_line(
        source, r'^  url "[^"]+"$', f'  url "{sdist_url}"', "url"
    )
    rendered = replace_one_formula_line(
        rendered, r'^  sha256 "[a-f0-9]{64}"$', f'  sha256 "{sha256}"', "sha256"
    )
    rendered = remove_one_formula_line(rendered, r'^  version "[^"]+"\n', "version")
    if rendered.count('  depends_on "libyaml"') != 1:
        raise ReleaseError(
            'Homebrew formula must contain one depends_on "libyaml" line'
        )
    if re.search(r"^  bottle do$", rendered, re.MULTILINE):
        raise ReleaseError("source Homebrew formula must not contain bottle metadata")
    return rendered


def replace_one_formula_line(
    text: str, pattern: str, replacement: str, label: str
) -> str:
    matches = re.findall(pattern, text, re.MULTILINE)
    if len(matches) != 1:
        raise ReleaseError(
            f"Homebrew formula must contain exactly one top-level {label}"
        )
    return re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)


def remove_one_formula_line(text: str, pattern: str, label: str) -> str:
    matches = re.findall(pattern, text, re.MULTILINE)
    if len(matches) != 1:
        raise ReleaseError(
            f"Homebrew formula must contain exactly one top-level {label}"
        )
    return re.sub(pattern, "", text, count=1, flags=re.MULTILINE)


def formula_without_bottle_block(formula: str) -> str:
    pattern = re.compile(
        r"^  bottle do\n(?:(?:    [^\n]*)?\n)*?^  end\n(?:\n)?",
        re.MULTILINE,
    )
    matches = tuple(pattern.finditer(formula))
    if len(matches) > 1:
        raise ReleaseError("Homebrew formula contains multiple bottle blocks")
    return pattern.sub("", formula, count=1)


def formula_version(formula: str, package_name: str) -> Version:
    match = re.search(r'^  url "([^"]+)"$', formula, re.MULTILINE)
    if match is None:
        raise ReleaseError("tap formula is missing its top-level URL")
    filename = Path(
        urllib.parse.unquote(urllib.parse.urlsplit(match.group(1)).path)
    ).name
    prefix = f"{package_name}-"
    suffix = ".tar.gz"
    if not filename.startswith(prefix) or not filename.endswith(suffix):
        raise ReleaseError(
            "tap formula URL does not contain the expected sdist filename"
        )
    try:
        return Version(filename[len(prefix) : -len(suffix)])
    except InvalidVersion as error:
        raise ReleaseError("tap formula URL contains an invalid version") from error


def ensure_safe_formula_output(root: Path, output: Path) -> None:
    resolved_root = root.resolve()
    if not output.resolve(strict=False).is_relative_to(resolved_root):
        raise ReleaseError("Homebrew formula output escapes the source repository")
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise ReleaseError(f"Homebrew formula output is not a regular file: {output}")


def verify_prepared_formula(root: Path, expected: str) -> None:
    path = root / TAP_FORMULA_ARTIFACT
    ensure_safe_formula_output(root, path)
    if not path.is_file():
        raise ReleaseError(
            f"prepared Homebrew formula is missing: {TAP_FORMULA_ARTIFACT}"
        )
    if path.read_text(encoding="utf-8") != expected:
        raise ReleaseError(
            "prepared Homebrew formula does not match fresh PyPI metadata"
        )


def validate_source_identity(
    root: Path, runner: CommandRunner, options: HomebrewPrOptions
) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", options.source_commit) is None:
        raise ReleaseError("source commit must be a full lowercase Git SHA")
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    tag_commit = runner.run(
        ["git", "rev-parse", f"refs/tags/{options.expected_tag}^{{commit}}"], cwd=root
    ).stdout.strip()
    if head != options.source_commit or tag_commit != options.source_commit:
        raise ReleaseError(
            "source checkout, release tag, and verified commit do not match"
        )


def require_github_authentication() -> None:
    if not os.environ.get("GH_TOKEN"):
        raise ReleaseError("GH_TOKEN must contain a Homebrew tap GitHub App token")


def resolve_tap_root(root: Path, requested: Path) -> Path:
    if requested.is_absolute():
        raise ReleaseError("Homebrew tap checkout path must be relative")
    resolved_root = root.resolve()
    tap_root = (root / requested).resolve()
    if not tap_root.is_relative_to(resolved_root) or tap_root == resolved_root:
        raise ReleaseError("Homebrew tap checkout must be inside the source workspace")
    if not tap_root.is_dir():
        raise ReleaseError(f"Homebrew tap checkout is missing: {requested}")
    return tap_root


def verify_tap_checkout(tap_root: Path, runner: CommandRunner) -> None:
    origin = runner.run(["git", "remote", "get-url", "origin"], cwd=tap_root).stdout
    normalized = origin.strip().removesuffix(".git")
    accepted = {
        f"https://github.com/{TAP_REPOSITORY}",
        f"git@github.com:{TAP_REPOSITORY}",
    }
    if normalized not in accepted:
        raise ReleaseError(f"Homebrew tap origin is not {TAP_REPOSITORY}")
    status = runner.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=tap_root
    ).stdout
    if status:
        raise ReleaseError("Homebrew tap checkout must be clean before publication")


def update_pull_request(
    tap_root: Path,
    runner: CommandRunner,
    release: HomebrewRelease,
    source_commit: str,
) -> int:
    base_sha = fetch_tap_main(tap_root, runner)
    current_formula = git_output(
        runner, tap_root, ["git", "show", f"{base_sha}:{TAP_FORMULA_PATH}"]
    )
    target_version = Version(release.context.version.python)
    current_version = formula_version(current_formula, release.context.package_name)
    if current_version > target_version:
        print_homebrew_skip(release.context, HomebrewEligibility.SUPERSEDED)
        return 0
    if current_version == target_version:
        if formula_without_bottle_block(current_formula) == release.formula:
            print(
                f"Homebrew tap already contains crewplane {release.context.version.project}."
            )
            return 0
        raise ReleaseError("tap formula has conflicting changes for the target version")

    branch = f"automation/crewplane-{release.context.version.project}"
    title = f"crewplane {release.context.version.project}"
    body = pull_request_body(release, source_commit)
    pull_request = query_pull_request(tap_root, runner, branch)
    remote_sha = remote_branch_sha(tap_root, runner, branch)
    validate_existing_automation(
        tap_root, runner, pull_request, remote_sha, branch, title, body, release.formula
    )

    if remote_sha:
        parent_sha = git_output(
            runner, tap_root, ["git", "show", "-s", "--format=%P", remote_sha]
        ).strip()
        if parent_sha == base_sha and pull_request is not None:
            if pull_request.state == "CLOSED":
                reopen_pull_request(tap_root, runner, pull_request.number)
            verify_published_pull_request(
                tap_root, runner, branch, title, body, remote_sha
            )
            print(f"Verified existing Homebrew pull request: {pull_request.url}")
            return 0

    new_sha = commit_formula_update(
        tap_root, runner, branch, base_sha, remote_sha, title, release.formula
    )
    if fetch_tap_main(tap_root, runner) != base_sha:
        raise ReleaseError("Homebrew tap main changed during publication; rerun safely")
    if pull_request is None:
        create_pull_request(tap_root, runner, branch, title, body)
    elif pull_request.state == "CLOSED":
        reopen_pull_request(tap_root, runner, pull_request.number)
    snapshot = verify_published_pull_request(
        tap_root, runner, branch, title, body, new_sha
    )
    print(f"Published Homebrew pull request: {snapshot.url}")
    return 0


def fetch_tap_main(tap_root: Path, runner: CommandRunner) -> str:
    runner.run(
        [
            "git",
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            f"refs/heads/{TAP_BASE_BRANCH}",
        ],
        cwd=tap_root,
    )
    sha = git_output(runner, tap_root, ["git", "rev-parse", "FETCH_HEAD"]).strip()
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ReleaseError("could not resolve Homebrew tap main")
    return sha


def remote_branch_sha(tap_root: Path, runner: CommandRunner, branch: str) -> str:
    reference = f"refs/heads/{branch}"
    output = git_output(
        runner, tap_root, ["git", "ls-remote", "--heads", "origin", reference]
    ).strip()
    if not output:
        return ""
    fields = output.split("\t")
    if (
        len(fields) != 2
        or fields[1] != reference
        or re.fullmatch(r"[0-9a-f]{40}", fields[0]) is None
    ):
        raise ReleaseError("Homebrew automation branch query returned malformed output")
    runner.run(
        ["git", "fetch", "--quiet", "--no-tags", "origin", reference], cwd=tap_root
    )
    fetched_sha = git_output(
        runner, tap_root, ["git", "rev-parse", "FETCH_HEAD"]
    ).strip()
    if fetched_sha != fields[0]:
        raise ReleaseError("Homebrew automation branch changed during inspection")
    return fetched_sha


def query_pull_request(
    tap_root: Path, runner: CommandRunner, branch: str
) -> PullRequestSnapshot | None:
    result = runner.run(
        [
            "gh",
            "api",
            f"repos/{TAP_REPOSITORY}/pulls",
            "--method",
            "GET",
            "--field",
            "state=all",
            "--field",
            f"head={TAP_OWNER}:{branch}",
            "--field",
            f"base={TAP_BASE_BRANCH}",
            "--field",
            "per_page=2",
        ],
        cwd=tap_root,
    )
    return parse_pull_request_snapshot(result.stdout)


def parse_pull_request_snapshot(payload: str) -> PullRequestSnapshot | None:
    try:
        records = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ReleaseError(
            "Homebrew pull request query returned invalid JSON"
        ) from error
    if not isinstance(records, list):
        raise ReleaseError("Homebrew pull request query returned malformed data")
    if not records:
        return None
    if len(records) != 1:
        raise ReleaseError("Homebrew automation branch has multiple pull requests")
    record = records[0]
    if not isinstance(record, dict):
        raise ReleaseError("Homebrew pull request query returned malformed data")
    head = record.get("head")
    base = record.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise ReleaseError("Homebrew pull request query returned malformed data")
    head_repository = head.get("repo")
    if not isinstance(head_repository, dict):
        raise ReleaseError("Homebrew pull request query returned incomplete data")
    body = record.get("body")
    if not isinstance(body, str) or not body.startswith(AUTOMATION_MARKER):
        raise ReleaseError("existing Homebrew pull request lacks the automation marker")
    values = {
        "state": record.get("state"),
        "url": record.get("html_url"),
        "head_sha": head.get("sha"),
        "head_branch": head.get("ref"),
        "head_repository": head_repository.get("full_name"),
        "base_branch": base.get("ref"),
        "title": record.get("title"),
    }
    if any(not isinstance(value, str) for value in values.values()):
        raise ReleaseError("Homebrew pull request query returned incomplete data")
    number = record.get("number")
    merged_at = record.get("merged_at")
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number <= 0
        or values["state"] not in {"open", "closed"}
        or values["head_repository"] != TAP_REPOSITORY
        or (merged_at is not None and not isinstance(merged_at, str))
    ):
        raise ReleaseError("Homebrew pull request query returned invalid data")
    return PullRequestSnapshot(
        number=number,
        state=values["state"].upper(),
        merged_at=merged_at,
        url=values["url"],
        body=body,
        head_sha=values["head_sha"],
        head_branch=values["head_branch"],
        base_branch=values["base_branch"],
        title=values["title"],
    )


def validate_existing_automation(
    tap_root: Path,
    runner: CommandRunner,
    pull_request: PullRequestSnapshot | None,
    remote_sha: str,
    branch: str,
    title: str,
    body: str,
    expected_formula: str,
) -> None:
    if pull_request is not None:
        if pull_request.merged_at is not None:
            raise ReleaseError(
                "Homebrew pull request is merged but tap main is inconsistent"
            )
        if (
            pull_request.head_branch != branch
            or pull_request.base_branch != TAP_BASE_BRANCH
            or pull_request.title != title
            or pull_request.body != body
        ):
            raise ReleaseError("existing Homebrew pull request metadata was changed")
        if remote_sha and pull_request.head_sha != remote_sha:
            raise ReleaseError("Homebrew pull request head does not match its branch")
        if not remote_sha and pull_request.state == "OPEN":
            raise ReleaseError("open Homebrew pull request is missing its branch")
    if not remote_sha:
        return
    subject = git_output(
        runner, tap_root, ["git", "show", "-s", "--format=%s", remote_sha]
    ).strip()
    if subject != title:
        raise ReleaseError(
            "existing Homebrew branch is not owned by release automation"
        )
    parents = git_output(
        runner, tap_root, ["git", "show", "-s", "--format=%P", remote_sha]
    ).split()
    if len(parents) != 1:
        raise ReleaseError("existing Homebrew automation branch is not a single commit")
    changed = git_output(
        runner, tap_root, ["git", "diff", "--name-only", parents[0], remote_sha]
    ).splitlines()
    if changed != [str(TAP_FORMULA_PATH)]:
        raise ReleaseError(
            "existing Homebrew automation branch changes unexpected files"
        )
    branch_formula = git_output(
        runner, tap_root, ["git", "show", f"{remote_sha}:{TAP_FORMULA_PATH}"]
    )
    if branch_formula != expected_formula:
        raise ReleaseError(
            "existing Homebrew automation branch has conflicting formula content"
        )


def commit_formula_update(
    tap_root: Path,
    runner: CommandRunner,
    branch: str,
    base_sha: str,
    remote_sha: str,
    title: str,
    formula: str,
) -> str:
    runner.run(["git", "switch", "--force-create", branch, base_sha], cwd=tap_root)
    formula_path = tap_root / TAP_FORMULA_PATH
    if formula_path.is_symlink() or not formula_path.is_file():
        raise ReleaseError("tap formula target is not a regular file")
    formula_path.write_text(formula, encoding="utf-8")
    status = git_output(
        runner,
        tap_root,
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
    ).splitlines()
    if status != [f" M {TAP_FORMULA_PATH}"]:
        raise ReleaseError("Homebrew update would change files outside the formula")
    runner.run(["git", "add", "--", str(TAP_FORMULA_PATH)], cwd=tap_root)
    staged = git_output(
        runner, tap_root, ["git", "diff", "--cached", "--name-only"]
    ).splitlines()
    if staged != [str(TAP_FORMULA_PATH)]:
        raise ReleaseError("Homebrew commit contains unexpected files")
    runner.run(["git", "commit", "-m", title], cwd=tap_root)
    new_sha = git_output(runner, tap_root, ["git", "rev-parse", "HEAD"]).strip()
    lease = f"--force-with-lease=refs/heads/{branch}:{remote_sha}"
    runner.run(
        ["git", "push", lease, "origin", f"HEAD:refs/heads/{branch}"], cwd=tap_root
    )
    return new_sha


def create_pull_request(
    tap_root: Path, runner: CommandRunner, branch: str, title: str, body: str
) -> None:
    runner.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            TAP_REPOSITORY,
            "--base",
            TAP_BASE_BRANCH,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=tap_root,
    )


def reopen_pull_request(tap_root: Path, runner: CommandRunner, number: int) -> None:
    runner.run(
        ["gh", "pr", "reopen", str(number), "--repo", TAP_REPOSITORY], cwd=tap_root
    )


def verify_published_pull_request(
    tap_root: Path,
    runner: CommandRunner,
    branch: str,
    title: str,
    body: str,
    head_sha: str,
) -> PullRequestSnapshot:
    snapshot = query_pull_request(tap_root, runner, branch)
    if snapshot is None:
        raise ReleaseError("Homebrew pull request is missing after publication")
    if (
        snapshot.state != "OPEN"
        or snapshot.merged_at is not None
        or snapshot.title != title
        or snapshot.body != body
        or snapshot.head_sha != head_sha
        or snapshot.head_branch != branch
        or snapshot.base_branch != TAP_BASE_BRANCH
    ):
        raise ReleaseError("Homebrew pull request does not match the published update")
    return snapshot


def pull_request_body(release: HomebrewRelease, source_commit: str) -> str:
    tag = release.context.version.tag
    return (
        f"{AUTOMATION_MARKER}\n"
        f"Automated Homebrew update for Crewplane `{release.context.version.project}`.\n\n"
        f"- Source release: https://github.com/{SOURCE_REPOSITORY}/releases/tag/{tag}\n"
        f"- Source commit: `{source_commit}`\n"
        f"- PyPI sdist: {release.sdist.url}\n"
        f"- SHA-256: `{release.sdist.sha256}`\n\n"
        "After the Homebrew checks pass, publish this PR with the tap's "
        "`brew pr-pull` workflow.\n"
    )


def print_homebrew_skip(
    context: ReleaseContext, eligibility: HomebrewEligibility
) -> None:
    print(
        f"Skipping Homebrew pull request for {context.version.project}: "
        f"release is {eligibility.value}."
    )


def git_output(runner: CommandRunner, cwd: Path, command: list[str]) -> str:
    return runner.run(command, cwd=cwd).stdout

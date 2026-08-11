from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from scripts import update_uv_bootstrap as updater

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE_TARGETS = tuple(target.archive_target for target in updater.UV_TARGETS)


def make_release(version: str) -> updater.UvRelease:
    checksums = {
        target: f"{index:064x}" for index, target in enumerate(ARCHIVE_TARGETS, start=1)
    }
    return updater.UvRelease(version, checksums)


def copy_update_surfaces(destination: Path) -> Path:
    for relative_path in (
        Path("install.sh"),
        Path("packaging/npm/scripts/postinstall.js"),
        Path("packaging/uv-bootstrap.json"),
        Path("packaging/uv-bootstrap-version.txt"),
    ):
        source = ROOT / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copytree(ROOT / ".github/workflows", destination / ".github/workflows")
    return destination


def test_repository_uv_bootstrap_metadata_is_synchronized() -> None:
    release = updater.validate_repository(ROOT)

    assert updater.VERSION_PATTERN.fullmatch(release.version)
    assert set(release.checksums) == set(ARCHIVE_TARGETS)


def test_fetch_latest_release_collects_every_platform_checksum() -> None:
    requested_urls: list[str] = []
    release = make_release("9.8.7")

    def fetch_text(url: str) -> str:
        requested_urls.append(url)
        if url == updater.LATEST_RELEASE_URL:
            return '{"tag_name":"9.8.7","draft":false,"prerelease":false}'
        target = next(target for target in ARCHIVE_TARGETS if target in url)
        filename = f"uv-{target}.tar.gz"
        return f"{release.checksums[target]}  {filename}\n"

    fetched = updater.fetch_latest_release(fetch_text)

    assert fetched == release
    assert len(requested_urls) == len(ARCHIVE_TARGETS) + 1


@pytest.mark.parametrize(
    "checksum_text",
    [
        "not-a-checksum",
        f"{'0' * 64}  wrong-archive.tar.gz\n",
        f"{'A' * 64}  uv-x86_64-unknown-linux-gnu.tar.gz\n",
    ],
)
def test_fetch_release_rejects_untrusted_checksum_shape(checksum_text: str) -> None:
    def fetch_text(url: str) -> str:
        del url
        return checksum_text

    with pytest.raises(updater.UvBootstrapError, match="checksum"):
        updater.fetch_release("9.8.7", fetch_text)


def test_fetch_text_sends_repository_token_only_to_github_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, limit: int) -> bytes:
            assert limit == 1_048_577
            return b"response"

    def urlopen(request: urllib.request.Request, timeout: int) -> Response:
        assert timeout == 30
        requests.append(request)
        return Response()

    monkeypatch.setenv("GH_TOKEN", "repository-token")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    updater.fetch_text(updater.LATEST_RELEASE_URL)
    updater.fetch_text(
        f"{updater.RELEASE_BASE_URL}/9.8.7/uv-x86_64-unknown-linux-gnu.tar.gz.sha256"
    )

    api_headers = dict(requests[0].header_items())
    asset_headers = dict(requests[1].header_items())
    assert api_headers["Authorization"] == "Bearer repository-token"
    assert "Authorization" not in asset_headers


@pytest.mark.parametrize(
    ("kernel", "machine", "libc_output", "target"),
    [
        ("Darwin", "arm64", "musl libc", "aarch64-apple-darwin"),
        ("Darwin", "x86_64", "musl libc", "x86_64-apple-darwin"),
        ("Linux", "aarch64", "ldd (GNU libc) 2.39", "aarch64-unknown-linux-gnu"),
        ("Linux", "aarch64", "musl libc", "aarch64-unknown-linux-musl"),
        ("Linux", "x86_64", "ldd (GNU libc) 2.39", "x86_64-unknown-linux-gnu"),
        ("Linux", "x86_64", "musl libc", "x86_64-unknown-linux-musl"),
    ],
)
def test_render_shell_metadata_selects_platform_archive(
    kernel: str,
    machine: str,
    libc_output: str,
    target: str,
) -> None:
    release = make_release("9.8.7")
    script = (
        """fail() { printf '%s\n' "$*" >&2; exit 1; }
uname() {
    case "$1" in
        -s) printf '%s\n' "$UV_TEST_KERNEL" ;;
        -m) printf '%s\n' "$UV_TEST_MACHINE" ;;
    esac
}
ldd() {
    printf '%s\n' "$UV_TEST_LIBC_OUTPUT"
    case "$UV_TEST_LIBC_OUTPUT" in
        *musl*) return 1 ;;
    esac
}
"""
        + updater.render_shell_metadata(release)
        + "\nuv_archive_details\n"
    )
    shell = shutil.which("sh")
    assert shell is not None

    result = subprocess.run(
        [shell, "-c", script],
        env={
            **os.environ,
            "UV_TEST_KERNEL": kernel,
            "UV_TEST_MACHINE": machine,
            "UV_TEST_LIBC_OUTPUT": libc_output,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{target}|{release.checksums[target]}\n"


def test_validate_workflow_version_file_inputs_accepts_shared_pin() -> None:
    workflow = """steps:
  - uses: astral-sh/setup-uv@commit
    env:
      version: "application-version"
    with:
      enable-cache: true
      prune-cache: true
      cache-dependency-glob: uv.lock
      cache-suffix: one
      ignore-empty-workdir: true
      github-token: token
      version-file: "packaging/uv-bootstrap-version.txt"
"""

    setup_uv_steps = updater.validate_workflow_version_file_inputs(
        workflow,
        Path(".github/workflows/test.yml"),
    )

    assert setup_uv_steps == 1


@pytest.mark.parametrize(
    "workflow",
    [
        """steps:
  - uses: astral-sh/setup-uv@commit
    env:
      version: "application-version"
    with:
      enable-cache: true
""",
        """steps:
  - uses: astral-sh/setup-uv@commit
    with:
      version-file: "packaging/other-version.txt"
  - run: echo test
    env:
      version: "application-version"
""",
        """steps:
  - uses: astral-sh/setup-uv@commit
    with:
      version: "0.0.0"
""",
    ],
)
def test_validate_workflow_version_file_inputs_rejects_other_pins(
    workflow: str,
) -> None:
    with pytest.raises(updater.UvBootstrapError, match="setup-uv"):
        updater.validate_workflow_version_file_inputs(
            workflow,
            Path(".github/workflows/test.yml"),
        )


def test_synchronize_repository_updates_pins_without_rewriting_workflows(
    tmp_path: Path,
) -> None:
    repository = copy_update_surfaces(tmp_path)
    release = make_release("9.8.7")
    ci_workflow_path = repository / ".github/workflows/ci.yml"
    original_ci_workflow = ci_workflow_path.read_text(encoding="utf-8")

    changed_paths = updater.synchronize_repository(repository, release)

    assert changed_paths
    assert not any(path.parts[:2] == (".github", "workflows") for path in changed_paths)
    assert updater.WORKFLOW_VERSION_PATH in changed_paths
    assert updater.validate_repository(repository) == release
    assert ci_workflow_path.read_text(encoding="utf-8") == original_ci_workflow
    assert (repository / updater.WORKFLOW_VERSION_PATH).read_text(
        encoding="utf-8"
    ) == "uv==9.8.7\n"


def test_validate_repository_rejects_inline_workflow_version(tmp_path: Path) -> None:
    repository = copy_update_surfaces(tmp_path)
    workflow_path = repository / ".github/workflows/ci.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    workflow_path.write_text(
        workflow.replace(
            'version-file: "packaging/uv-bootstrap-version.txt"',
            'version: "0.0.0"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(updater.UvBootstrapError, match="ci.yml"):
        updater.validate_repository(repository)


def test_validate_repository_rejects_workflow_version_file_drift(
    tmp_path: Path,
) -> None:
    repository = copy_update_surfaces(tmp_path)
    version_path = repository / updater.WORKFLOW_VERSION_PATH
    version_path.write_text("uv==0.0.0\n", encoding="utf-8")

    with pytest.raises(updater.UvBootstrapError, match="uv-bootstrap-version.txt"):
        updater.validate_repository(repository)

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crewplane.core.preflight.secrets import FingerprintKeyProvider, SecretContext


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("short", "exactly 32 bytes"),
        ("permissions", "owner-only"),
        ("directory", "regular file"),
        ("symlink", "must not be a symlink"),
    ],
)
def test_existing_invalid_fingerprint_key_returns_diagnostic_without_replacement(
    tmp_path: Path, kind: str, message: str
) -> None:
    provider = FingerprintKeyProvider(tmp_path / ".crewplane")
    key_path = provider.key_path
    key_path.parent.mkdir(parents=True)
    if kind == "directory":
        key_path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "external-key"
        target.write_bytes(b"s" * 32)
        target.chmod(0o600)
        key_path.symlink_to(target)
    else:
        key_path.write_bytes(b"s" * (31 if kind == "short" else 32))
        key_path.chmod(0o644 if kind == "permissions" else 0o600)
    original_metadata = key_path.lstat()

    result = provider.load_key("persist_if_needed")

    assert result.key == b""
    assert result.persisted is True
    assert any(message in diagnostic.message for diagnostic in result.diagnostics)
    assert {diagnostic.code for diagnostic in result.diagnostics} == {"FINGERPRINT-KEY"}
    assert key_path.lstat() == original_metadata
    assert list(key_path.parent.glob("*.tmp")) == []


def test_fingerprint_key_inspection_failure_preserves_diagnostic_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FingerprintKeyProvider(tmp_path)
    provider.key_path.parent.mkdir()
    provider.key_path.write_bytes(b"s" * 32)
    original_lstat = Path.lstat

    def inspect(path: Path) -> os.stat_result:
        if path == provider.key_path:
            raise PermissionError("inspection denied")
        return original_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", inspect)
        result = provider.load_key("read_only")

    assert result.key == b""
    assert len(result.diagnostics) == 1
    assert (
        "Unable to inspect fingerprint key: inspection denied"
        in result.diagnostics[0].message
    )


def test_failed_key_publication_removes_temporary_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FingerprintKeyProvider(tmp_path)

    def denied_link(source: object, destination: object) -> None:
        assert Path(str(source)).parent == provider.key_path.parent
        assert destination == provider.key_path
        raise PermissionError("publication denied")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", denied_link)
        with pytest.raises(PermissionError, match="publication denied"):
            provider.load_key("persist_if_needed")

    assert list(provider.key_path.parent.iterdir()) == []


def test_secret_context_distinguishes_missing_and_nontext_values() -> None:
    context = SecretContext()
    assert context.has_values() is False
    with pytest.raises(KeyError, match="not available"):
        context.get("missing")
    context.put_config_value("options", {"enabled": True})
    assert context.has_values() is True
    with pytest.raises(TypeError, match="does not contain text"):
        context.get("options")
    assert context.get_config_value("options") == {"enabled": True}

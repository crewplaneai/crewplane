from __future__ import annotations

import hmac
import os
import secrets
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal

from crewplane.architecture.contracts import JsonObject

from .diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from .serialization import canonical_json_bytes

FingerprintKeyPolicy = Literal["persist_if_needed", "read_only", "ephemeral"]
FINGERPRINT_KEY_SIZE = 32

# This version belongs to the HMAC fingerprint payload shape, not the public
# config/workflow/preflight schema. Bump it only when fields feeding
# fingerprint_payload() change incompatibly, so persisted fingerprints are
# never compared across formats.
FINGERPRINT_PAYLOAD_VERSION = "1"


@dataclass
class SecretContext:
    """Same-process secret handles used by runtime fragment assembly."""

    _values: dict[str, str] = field(default_factory=dict)

    def put(self, handle: str, value: str) -> None:
        self._values[handle] = value

    def get(self, handle: str) -> str:
        try:
            return self._values[handle]
        except KeyError as exc:
            raise KeyError(f"Secret handle '{handle}' is not available.") from exc

    def has_values(self) -> bool:
        return bool(self._values)


@dataclass
class FingerprintKeyCache:
    """Same-run cache for ephemeral fingerprint keys."""

    _keys: dict[Path, bytes] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def ephemeral_key_for(self, key_path: Path) -> bytes:
        cache_key = key_path.resolve(strict=False)
        with self._lock:
            key = self._keys.get(cache_key)
            if key is None:
                key = secrets.token_bytes(FINGERPRINT_KEY_SIZE)
                self._keys[cache_key] = key
            return key


@dataclass(frozen=True)
class FingerprintKeyResult:
    key: bytes
    persisted: bool
    diagnostics: tuple[PreflightDiagnostic, ...] = ()


class FingerprintKeyProvider:
    """Load or publish the project-local HMAC fingerprint key."""

    def __init__(
        self,
        state_dir: Path,
        cache: FingerprintKeyCache | None = None,
    ) -> None:
        self.key_path = state_dir / "preflight" / "fingerprint.key"
        self.cache = cache if cache is not None else FingerprintKeyCache()

    def load_key(self, policy: FingerprintKeyPolicy) -> FingerprintKeyResult:
        if self.key_path.exists() or self.key_path.is_symlink():
            return self._read_existing_key()
        if policy == "read_only" or policy == "ephemeral":
            return FingerprintKeyResult(
                key=self._ephemeral_key(),
                persisted=False,
            )
        return self._publish_new_key()

    def _ephemeral_key(self) -> bytes:
        return self.cache.ephemeral_key_for(self.key_path)

    def _read_existing_key(self) -> FingerprintKeyResult:
        diagnostics = self._validate_key_file()
        if diagnostics:
            return FingerprintKeyResult(
                key=b"",
                persisted=True,
                diagnostics=tuple(diagnostics),
            )
        return FingerprintKeyResult(key=self.key_path.read_bytes(), persisted=True)

    def _validate_key_file(self) -> list[PreflightDiagnostic]:
        diagnostics: list[PreflightDiagnostic] = []
        path_label = self.key_path.as_posix()
        try:
            metadata = self.key_path.lstat()
        except OSError as exc:
            return [
                PreflightDiagnostic(
                    code=PreflightDiagnosticCode.FINGERPRINT_KEY,
                    phase=PreflightDiagnosticPhase.ENV_POLICY,
                    path=path_label,
                    message=f"Unable to inspect fingerprint key: {exc}",
                )
            ]
        if stat.S_ISLNK(metadata.st_mode):
            diagnostics.append(
                PreflightDiagnostic(
                    code=PreflightDiagnosticCode.FINGERPRINT_KEY,
                    phase=PreflightDiagnosticPhase.ENV_POLICY,
                    path=path_label,
                    message="Fingerprint key must not be a symlink.",
                )
            )
        if not stat.S_ISREG(metadata.st_mode):
            diagnostics.append(
                PreflightDiagnostic(
                    code=PreflightDiagnosticCode.FINGERPRINT_KEY,
                    phase=PreflightDiagnosticPhase.ENV_POLICY,
                    path=path_label,
                    message="Fingerprint key must be a regular file.",
                )
            )
        if metadata.st_size != FINGERPRINT_KEY_SIZE:
            diagnostics.append(
                PreflightDiagnostic(
                    code=PreflightDiagnosticCode.FINGERPRINT_KEY,
                    phase=PreflightDiagnosticPhase.ENV_POLICY,
                    path=path_label,
                    message="Fingerprint key must contain exactly 32 bytes.",
                )
            )
        if os.name == "posix" and metadata.st_mode & 0o077:
            diagnostics.append(
                PreflightDiagnostic(
                    code=PreflightDiagnosticCode.FINGERPRINT_KEY,
                    phase=PreflightDiagnosticPhase.ENV_POLICY,
                    path=path_label,
                    message="Fingerprint key permissions must be owner-only.",
                )
            )
        return diagnostics

    def _publish_new_key(self) -> FingerprintKeyResult:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync_parent()
        key_bytes = secrets.token_bytes(FINGERPRINT_KEY_SIZE)
        file_descriptor, temp_path_label = tempfile.mkstemp(
            dir=self.key_path.parent,
            prefix=f".{self.key_path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_path_label)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(key_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_parent()
            try:
                os.link(temp_path, self.key_path)
            except FileExistsError:
                return self._read_existing_key()
            finally:
                with suppress(OSError):
                    temp_path.unlink()
                self._fsync_parent()
        except Exception:
            with suppress(OSError):
                temp_path.unlink()
            raise
        return self._read_existing_key()

    def _fsync_parent(self) -> None:
        if os.name != "posix":
            return
        try:
            descriptor = os.open(self.key_path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def fingerprint_payload(key: bytes, payload: JsonObject) -> str:
    return hmac.new(key, canonical_json_bytes(payload), "sha256").hexdigest()

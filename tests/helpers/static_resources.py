from hashlib import sha256

from crewplane.core.preflight.compiled_records import StaticResource


def static_resource(payload: bytes, content_ref: str | None = None) -> StaticResource:
    digest = sha256(payload).hexdigest()
    return StaticResource(
        resource_id=digest,
        kind="file",
        raw_path="fixture.txt",
        source_root="/source",
        resolved_path="/source/fixture.txt",
        content_ref=content_ref or f"static-files/{digest}.txt",
        size_bytes=len(payload),
        sha256=digest,
    )

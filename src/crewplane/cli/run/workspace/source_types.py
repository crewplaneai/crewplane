from __future__ import annotations

from dataclasses import dataclass, field

from crewplane.core.preflight.models import WorkspaceSourceSnapshot


@dataclass(frozen=True)
class WorkspacePolicyCheck:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_snapshot: WorkspaceSourceSnapshot | None = None


@dataclass
class WorkspacePolicyBuilder:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def result(
        self,
        source_snapshot: WorkspaceSourceSnapshot | None = None,
    ) -> WorkspacePolicyCheck:
        return WorkspacePolicyCheck(
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
            source_snapshot=source_snapshot if not self.errors else None,
        )

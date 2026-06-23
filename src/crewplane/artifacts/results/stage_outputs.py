from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..generated_files.catalog import GeneratedFileLink


@dataclass
class StageOutputAggregation:
    result_sections: list[str] = field(default_factory=list)
    included_outputs: list[Path] = field(default_factory=list)
    skipped_empty_outputs: list[Path] = field(default_factory=list)
    findings_sections: list[tuple[str, str]] = field(default_factory=list)
    generated_file_reference_content: list[str] = field(default_factory=list)
    generated_file_links: list[GeneratedFileLink] = field(default_factory=list)

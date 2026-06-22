from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..generated_files.catalog import (
    GeneratedFileLink,
    GeneratedFileReferenceDetector,
    build_generated_file_links_section,
    build_generated_files_section,
)


def write_stage_result_file(
    result_file: Path,
    stage_name: str,
    result_sections: list[str],
    generated_file_reference_content: list[str],
    generated_file_links: list[GeneratedFileLink],
    workspace_root: Path | None,
    generated_file_detector: GeneratedFileReferenceDetector | None,
) -> None:
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(
        build_stage_result_document(
            result_file,
            stage_name,
            result_sections,
            generated_file_reference_content,
            generated_file_links,
            workspace_root,
            generated_file_detector,
        ),
        encoding="utf-8",
    )


def build_stage_result_document(
    result_file: Path,
    stage_name: str,
    result_sections: list[str],
    generated_file_reference_content: list[str],
    generated_file_links: list[GeneratedFileLink],
    workspace_root: Path | None,
    generated_file_detector: GeneratedFileReferenceDetector | None,
) -> str:
    sections = [
        f"# {stage_name} Results\n\n",
        f"Generated: {datetime.now().isoformat()}\n\n",
        *result_sections,
    ]
    generated_files_section = generated_files_section_for_result(
        result_file,
        generated_file_reference_content,
        generated_file_links,
        workspace_root,
        generated_file_detector,
    )
    if generated_files_section is not None:
        sections.append(generated_files_section)
    return "".join(sections)


def generated_files_section_for_result(
    result_file: Path,
    included_contents: list[str],
    generated_file_links: list[GeneratedFileLink],
    workspace_root: Path | None,
    generated_file_detector: GeneratedFileReferenceDetector | None,
) -> str | None:
    if generated_file_links:
        return build_generated_file_links_section(result_file, generated_file_links)
    if workspace_root is None or generated_file_detector is None:
        return None
    generated_files = generated_file_detector.detect("\n".join(included_contents))
    return build_generated_files_section(
        result_file,
        workspace_root,
        generated_files,
    )

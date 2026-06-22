from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..safe_files import contained_regular_file

GENERATED_FILE_ACTION_PATTERN = re.compile(
    r"\b(?P<action>created|wrote|written|saved|generated|renamed|moved|updated)\b",
    re.IGNORECASE,
)
GENERATED_FILES_HEADING_PATTERN = re.compile(
    r"^\s{0,3}#{1,6}\s+Generated Files\s*$",
    re.IGNORECASE,
)
MARKDOWN_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+\S")
MODAL_GUIDANCE_PREFIX_FRAGMENT = (
    r"\b(?:should|must|need(?:s|ed)?|could|would|will)\b"
    r"(?:\W+\w+){0,4}\W+(?:be\W+)?"
)
NEGATED_CLAIM_PREFIX_FRAGMENT = (
    r"\b(?:no|not|never|nothing|neither|without)\b(?:\W+\w+){0,6}\W+"
)
AUXILIARY_NOT_PREFIX_FRAGMENT = (
    r"\b(?:was|were|is|are|did|does|do|has|have|had)\W+not\b"
    r"(?:\W+\w+){0,5}\W+"
)
CONTRACTION_NOT_PREFIX_FRAGMENT = (
    r"\b(?:wasn|weren|isn|aren|didn|doesn|don|hasn|haven|hadn)['’]t\b"
    r"(?:\W+\w+){0,5}\W+"
)
# Review outputs often mention generated-file verbs in recommendations or negated
# findings; only affirmative provider claims should create Generated Files links.
NON_CLAIM_ACTION_PREFIX_PATTERN = re.compile(
    r"(?:"
    rf"{MODAL_GUIDANCE_PREFIX_FRAGMENT}"
    rf"|{NEGATED_CLAIM_PREFIX_FRAGMENT}"
    rf"|{AUXILIARY_NOT_PREFIX_FRAGMENT}"
    rf"|{CONTRACTION_NOT_PREFIX_FRAGMENT}"
    r")$",
    re.IGNORECASE,
)
MARKDOWN_LINK_TARGET_PATTERN = re.compile(r"\[[^\]\n]+\]\((?P<target>[^)\n]+)\)")
CODE_SPAN_PATTERN = re.compile(r"`(?P<path>[^`\n]+)`")
URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
BARE_PATH_PATTERN = re.compile(
    r"(?<![\w@])(?P<path>(?:\.{1,2}/|[A-Za-z0-9_.-]+/)"
    r"[A-Za-z0-9_./@%+-]+\.[A-Za-z0-9]{1,16})(?::\d+(?:-\d+)?)?"
)
LINE_NUMBER_SUFFIX_PATTERN = re.compile(r":\d+(?:-\d+)?$")
RESERVED_WORKSPACE_PATH_ROOTS = frozenset(
    {".orchestrator", "execution-stages", "execution-results"}
)
GENERATED_FILE_SOURCE_METADATA_NAME = ".orchestrator-generated-file-source.json"
GENERATED_FILE_SNAPSHOT_METADATA_NAME = ".orchestrator-generated-file-snapshot.json"


@dataclass(frozen=True)
class GeneratedFileLink:
    label: str
    target_path: Path


class GeneratedFileReferenceDetector:
    """Detect workspace files a provider output says it generated."""

    def __init__(self, workspace_root: Path, source_root: Path | None = None) -> None:
        self._workspace_root = workspace_root.resolve()
        self._source_root = source_root.resolve(strict=False) if source_root else None

    def detect(self, content: str) -> tuple[Path, ...]:
        generated_files: list[Path] = []
        seen_generated_files: set[Path] = set()
        in_explicit_section = False
        for line in content.splitlines():
            if GENERATED_FILES_HEADING_PATTERN.fullmatch(line):
                in_explicit_section = True
                continue
            if in_explicit_section:
                if MARKDOWN_HEADING_PATTERN.match(line):
                    in_explicit_section = False
                else:
                    self._add_line_references(
                        line, generated_files, seen_generated_files
                    )
                    continue

            if not line_claims_generated_file(line):
                continue
            self._add_claimed_line_references(
                line, generated_files, seen_generated_files
            )
        return tuple(generated_files)

    def _add_line_references(
        self,
        line: str,
        generated_files: list[Path],
        seen_generated_files: set[Path],
    ) -> None:
        for candidate in self._extract_candidates(line):
            resolved_path = self._resolve_candidate(candidate)
            if resolved_path is None or resolved_path in seen_generated_files:
                continue
            seen_generated_files.add(resolved_path)
            generated_files.append(resolved_path)

    def _add_claimed_line_references(
        self,
        line: str,
        generated_files: list[Path],
        seen_generated_files: set[Path],
    ) -> None:
        for candidate, start_index in self._extract_candidate_locations(line):
            if not candidate_claims_generated_file(line, start_index):
                continue
            resolved_path = self._resolve_candidate(candidate)
            if resolved_path is None or resolved_path in seen_generated_files:
                continue
            seen_generated_files.add(resolved_path)
            generated_files.append(resolved_path)

    def _extract_candidates(self, line: str) -> tuple[str, ...]:
        return tuple(
            candidate
            for candidate, _start_index in self._extract_candidate_locations(line)
        )

    def _extract_candidate_locations(self, line: str) -> tuple[tuple[str, int], ...]:
        candidate_locations: list[tuple[str, int]] = []
        candidate_locations.extend(
            (match.group("target"), match.start("target"))
            for match in MARKDOWN_LINK_TARGET_PATTERN.finditer(line)
        )
        candidate_locations.extend(
            (match.group("path"), match.start("path"))
            for match in CODE_SPAN_PATTERN.finditer(line)
        )
        scrubbed_line = URL_PATTERN.sub(
            lambda match: " " * (match.end() - match.start()), line
        )
        candidate_locations.extend(
            (match.group("path"), match.start("path"))
            for match in BARE_PATH_PATTERN.finditer(scrubbed_line)
        )
        return tuple(candidate_locations)

    def _resolve_candidate(self, raw_candidate: str) -> Path | None:
        candidate = self._clean_candidate(raw_candidate)
        if candidate is None:
            return None

        candidates = [candidate]
        if any(char.isspace() for char in candidate):
            candidates.append(candidate.split()[0])
        for candidate_value in candidates:
            resolved_path = self._resolve_clean_candidate(candidate_value)
            if resolved_path is not None:
                return resolved_path
        return None

    def _resolve_clean_candidate(self, candidate: str) -> Path | None:
        candidate_path = Path(candidate)
        if not candidate_path.suffix:
            return None
        try:
            workspace_root = self._workspace_root.resolve(strict=True)
            relative_path = self._candidate_relative_path(
                candidate_path,
                workspace_root,
            )
        except OSError:
            return None
        if relative_path is None:
            return None
        if is_reserved_workspace_path(relative_path):
            return None
        resolved_path = contained_regular_file(workspace_root, relative_path.as_posix())
        if resolved_path is None:
            return None
        if is_reserved_workspace_path(resolved_path.relative_to(workspace_root)):
            return None
        return resolved_path

    def _candidate_relative_path(
        self,
        candidate_path: Path,
        workspace_root: Path,
    ) -> Path | None:
        if not candidate_path.is_absolute():
            return candidate_path
        for root in self._absolute_candidate_roots(workspace_root):
            try:
                return candidate_path.resolve(strict=False).relative_to(root)
            except ValueError:
                continue
        return None

    def _absolute_candidate_roots(self, workspace_root: Path) -> tuple[Path, ...]:
        if self._source_root is None or self._source_root == workspace_root:
            return (workspace_root,)
        return (workspace_root, self._source_root)

    def _clean_candidate(self, raw_candidate: str) -> str | None:
        candidate = raw_candidate.strip()
        if not candidate or candidate.startswith(("#", "~")) or "://" in candidate:
            return None
        if candidate.startswith("<"):
            closing_index = candidate.find(">")
            if closing_index == -1:
                return None
            candidate = candidate[1:closing_index]
        candidate = candidate.strip().strip("'\"")
        candidate = LINE_NUMBER_SUFFIX_PATTERN.sub("", candidate)
        if not candidate or candidate.startswith(("#", "~")) or "://" in candidate:
            return None
        return candidate


def is_reserved_workspace_path(relative_path: Path) -> bool:
    if relative_path.parts and relative_path.parts[0] in {
        GENERATED_FILE_SOURCE_METADATA_NAME,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    }:
        return True
    return bool(
        relative_path.parts and relative_path.parts[0] in RESERVED_WORKSPACE_PATH_ROOTS
    )


def line_claims_generated_file(line: str) -> bool:
    return any(
        not action_match_is_non_claim(line, match.start())
        for match in GENERATED_FILE_ACTION_PATTERN.finditer(line)
    )


def action_match_is_non_claim(line: str, action_start: int) -> bool:
    prefix = line[max(0, action_start - 120) : action_start]
    return bool(NON_CLAIM_ACTION_PREFIX_PATTERN.search(prefix))


def candidate_claims_generated_file(line: str, candidate_start: int) -> bool:
    action_matches = tuple(GENERATED_FILE_ACTION_PATTERN.finditer(line))
    if not action_matches:
        return False
    nearest_action = min(
        action_matches,
        key=lambda match: distance_to_candidate(
            match.start(), match.end(), candidate_start
        ),
    )
    return not action_match_is_non_claim(line, nearest_action.start())


def distance_to_candidate(
    action_start: int,
    action_end: int,
    candidate_start: int,
) -> int:
    if action_start <= candidate_start <= action_end:
        return 0
    if action_end < candidate_start:
        return candidate_start - action_end
    return action_start - candidate_start

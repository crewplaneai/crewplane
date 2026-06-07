from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.observability.events import RunDashboardState

MAX_STREAM_LINES_PER_NODE = 20


@dataclass
class _LogCursor:
    offset: int = 0
    partial_line: str = ""
    header_complete: bool = False


class NodeLogStreamTracker:
    """Track and tail per-node invocation logs for live dashboard rendering."""

    def __init__(self, lines_per_node: int) -> None:
        clamped_lines = max(0, min(lines_per_node, MAX_STREAM_LINES_PER_NODE))
        self.lines_per_node = clamped_lines
        self._node_lines: dict[str, deque[str]] = {}
        self._cursors: dict[str, _LogCursor] = {}

    def refresh(self, state: RunDashboardState) -> None:
        if self.lines_per_node <= 0:
            return

        node_to_log_files: dict[str, set[str]] = {}
        for node_id, node_state in state.nodes.items():
            log_paths = {
                invocation.log_file
                for invocation in node_state.invocations.values()
                if invocation.status != "pending" and invocation.log_file
            }
            if log_paths:
                node_to_log_files[node_id] = set(log_paths)

        for node_id, log_paths in node_to_log_files.items():
            for log_path in sorted(log_paths):
                self._consume_log_file(node_id, Path(log_path))

    def get_node_lines(self) -> dict[str, list[str]]:
        return {
            node_id: list(lines) for node_id, lines in self._node_lines.items() if lines
        }

    def _consume_log_file(self, node_id: str, log_path: Path) -> None:
        if not log_path.exists():
            return

        cursor = self._cursors.setdefault(str(log_path), _LogCursor())
        file_size = log_path.stat().st_size
        if cursor.offset > file_size:
            cursor.offset = 0
            cursor.partial_line = ""
            cursor.header_complete = False

        if cursor.offset == file_size:
            return

        with log_path.open("rb") as handle:
            handle.seek(cursor.offset)
            raw_content = handle.read()
        cursor.offset += len(raw_content)

        text_chunk = raw_content.decode("utf-8", errors="replace")
        if (
            not cursor.header_complete
            and cursor.offset == len(raw_content)
            and "\n---\n" not in f"\n{text_chunk}\n"
        ):
            cursor.header_complete = True
        combined = f"{cursor.partial_line}{text_chunk}"
        lines = combined.splitlines(keepends=True)
        cursor.partial_line = ""

        for index, line in enumerate(lines):
            is_complete = line.endswith("\n") or line.endswith("\r")
            if not is_complete and index == len(lines) - 1:
                cursor.partial_line = line
                continue
            self._consume_line(node_id, line.rstrip("\r\n"), cursor)

    def _consume_line(self, node_id: str, line: str, cursor: _LogCursor) -> None:
        if not cursor.header_complete:
            if line.strip() == "---":
                cursor.header_complete = True
            return
        if not line:
            return

        node_lines = self._node_lines.setdefault(
            node_id,
            deque(maxlen=self.lines_per_node),
        )
        node_lines.append(line)

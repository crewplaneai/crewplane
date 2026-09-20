from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from crewplane.architecture.contracts import (
    CommandResult,
    OutputExtractionResult,
    ProviderTokenUsage,
    UsageDecodeResult,
)

from ..usage_decoders import CounterReader, UsageAccumulator, complete_sum

_ANSWER_EVENTS = frozenset({"step_start", "text", "tool_use", "step_finish"})
type _Identity = tuple[str, str, str]
type _Counters = tuple[int | None, ...]


def extract_opencode_output(
    result: CommandResult,
    structured_output_file: Path | None,  # noqa: ARG001 - OutputExtractor contract.
) -> OutputExtractionResult:
    """Select only completed text from the terminal stopped message."""
    state = _AnswerState()
    try:
        for line in result.iter_stdout_lines():
            event = _read_event(line)
            if event is not None:
                state.consume(event)
    except ValueError:
        return OutputExtractionResult("", "malformed")
    return state.result()


@dataclass(frozen=True)
class _AnswerPart:
    kind: str
    message_id: str
    text: str = ""
    end: int | float | None = None
    reason: str = ""


@dataclass
class _AnswerState:
    session_id: str | None = None
    last_message: str | None = None
    parts: dict[str, _AnswerPart] = field(default_factory=dict)
    finish_reasons: dict[str, str] = field(default_factory=dict)

    def consume(self, event: Mapping[str, object]) -> None:
        kind = event.get("type")
        if kind == "error":
            raise ValueError("OpenCode session error.")
        if not isinstance(kind, str) or kind not in _ANSWER_EVENTS:
            return
        identity, part = _identified_part(event)
        session_id, message_id, part_id = identity
        if self.session_id is not None and self.session_id != session_id:
            raise ValueError("Inconsistent OpenCode session identity.")
        self.session_id = session_id
        snapshot = _answer_part(kind, message_id, part)
        if part_id in self.parts:
            if self.parts[part_id] != snapshot:
                raise ValueError("Contradictory OpenCode part snapshot.")
            return
        self.parts[part_id] = snapshot
        self.last_message = message_id
        if kind == "step_finish":
            self.finish_reasons[message_id] = snapshot.reason
        else:
            self.finish_reasons.pop(message_id, None)

    def result(self) -> OutputExtractionResult:
        if (
            self.last_message is None
            or self.finish_reasons.get(self.last_message) != "stop"
        ):
            return OutputExtractionResult("", "missing")
        texts = [
            part.text
            for part in self.parts.values()
            if part.message_id == self.last_message and part.text.strip()
        ]
        if not texts:
            return OutputExtractionResult("", "missing")
        text = "\n".join(texts)
        if not text.endswith("\n"):
            text += "\n"
        return OutputExtractionResult(text, "success", output_char_count=len(text))


def _answer_part(kind: str, message_id: str, part: Mapping[str, object]) -> _AnswerPart:
    if kind == "text":
        text = part.get("text")
        if not isinstance(text, str):
            raise ValueError("Malformed OpenCode text.")
        return _AnswerPart(kind, message_id, text=text, end=_completed_time(part))
    if kind == "step_finish":
        reason = part.get("reason")
        if not isinstance(reason, str):
            raise ValueError("Malformed OpenCode finish reason.")
        return _AnswerPart(kind, message_id, reason=reason)
    return _AnswerPart(kind, message_id)


def _completed_time(part: Mapping[str, object]) -> int | float:
    time = part.get("time")
    end = time.get("end") if isinstance(time, dict) else None
    if (
        isinstance(end, bool)
        or not isinstance(end, (int, float))
        or end <= 0
        or (isinstance(end, float) and not math.isfinite(end))
    ):
        raise ValueError("OpenCode text requires a completed time.end.")
    return end


def _read_event(line: str) -> Mapping[str, object] | None:
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("Malformed OpenCode JSON output.") from exc
    if not isinstance(event, dict):
        raise ValueError("OpenCode events must be JSON objects.")
    return event


def _identified_part(
    event: Mapping[str, object],
) -> tuple[_Identity, Mapping[str, object]]:
    part = event.get("part")
    if not isinstance(part, dict):
        raise ValueError("Malformed OpenCode event part.")
    session_id = _identity_field(event, "sessionID")
    if session_id != _identity_field(part, "sessionID"):
        raise ValueError("Inconsistent OpenCode envelope/part session identity.")
    return (
        (session_id, _identity_field(part, "messageID"), _identity_field(part, "id")),
        part,
    )


def _identity_field(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"OpenCode {key} must be a nonblank string.")
    return value


def decode_opencode_usage(result: CommandResult) -> UsageDecodeResult:
    """Aggregate distinct native steps independently of answer extraction."""
    accumulator = UsageAccumulator()
    reports: dict[_Identity, _Counters] = {}
    for line in result.iter_stdout_lines():
        try:
            event = _read_event(line)
            if event is None or event.get("type") != "step_finish":
                continue
            report = _usage_report(event)
            if report is None:
                continue
            identity, counters = report
            if identity in reports:
                if reports[identity] != counters:
                    raise ValueError("Contradictory OpenCode step usage report.")
                continue
            reports[identity] = counters
            accumulator.record_usage(_normalized_usage(counters))
        except ValueError as exc:
            accumulator.record_error(str(exc))
    return accumulator.result()


def _usage_report(
    event: Mapping[str, object],
) -> tuple[_Identity, _Counters] | None:
    part = event.get("part")
    if not isinstance(part, dict) or "tokens" not in part:
        return None
    identity, part = _identified_part(event)
    tokens = part["tokens"]
    if not isinstance(tokens, dict):
        raise ValueError("Malformed OpenCode token payload.")
    cache = tokens.get("cache")
    if cache is None:
        cache = {}
    if not isinstance(cache, dict):
        raise ValueError("Malformed OpenCode cache payload.")
    counters = CounterReader("OpenCode", tokens)
    cached = CounterReader("OpenCode", cache)
    return identity, (
        counters.optional("input"),
        counters.optional("output"),
        counters.optional("reasoning"),
        cached.optional("read"),
        cached.optional("write"),
        counters.optional("total"),
    )


def _normalized_usage(counters: _Counters) -> ProviderTokenUsage:
    input_tokens, output, reasoning, cache_read, cache_write, total = counters
    return ProviderTokenUsage(
        input=complete_sum(input_tokens, cache_read, cache_write),
        cached_input=cache_read,
        cache_write=cache_write,
        output=complete_sum(output, reasoning),
        reasoning=reasoning,
        total=total if total is not None else complete_sum(*counters[:5]),
    )

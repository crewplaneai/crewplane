"""Exact token scopes and snapshot-backed call coverage from one event pass."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from crewplane.architecture.contracts import EventType
from crewplane.architecture.contracts.invocation import (
    TOKEN_BUCKETS,
    ProviderTokenUsage,
)
from crewplane.core.value_checks import is_nonnegative_int
from crewplane.observability.events import (
    ExecutionEvent,
    InvocationEventPayload,
    NodeRuntimeState,
    is_invocation_event_type,
)
from crewplane.observability.types import DashboardSnapshot

from .models import (
    NodeTokenAggregate,
    ProviderTokenAggregate,
    ProviderTokenAggregates,
    TokenAggregates,
)

_TERMINAL_EVENTS = {EventType.INVOCATION_FINISHED, EventType.INVOCATION_FAILED}
type CallIdentity = tuple[str, int | None, int | None]


@dataclass
class ExactTokenCounter:
    """Accumulate already-normalized reports without filling unknown buckets."""

    report_count: int = 0
    tokens: ProviderTokenUsage = field(default_factory=ProviderTokenUsage)

    def record(self, payload: InvocationEventPayload) -> None:
        count = payload.provider_usage_report_count
        if not is_nonnegative_int(count) or count == 0:
            return
        values = payload.provider_tokens or {}
        tokens = ProviderTokenUsage(
            **{
                bucket: value if is_nonnegative_int(value) else None
                for bucket, value in values.items()
                if bucket in TOKEN_BUCKETS
            }
        )
        self.tokens = self.tokens.add_exact(tokens) if self.report_count else tokens
        self.report_count += count

    def freeze(self, provider: str | None) -> ProviderTokenAggregate:
        return ProviderTokenAggregate(
            provider, self.report_count, **self.tokens.as_dict()
        )


@dataclass
class NodeTokenCounter:
    """Keep scope counters; identity tracking is bounded by the existing snapshot."""

    node_id: str | None
    snapshot_node: NodeRuntimeState | None = None
    tokens: ExactTokenCounter = field(default_factory=ExactTokenCounter)
    terminal_calls: int = 0
    known_report_calls: int = 0
    known_attempt_calls: int = 0
    attempt_count: int = 0
    positive_report_calls: int = 0
    zero_report_calls: int = 0
    missing_report_count_calls: int = 0
    missing_attempt_count_calls: int = 0
    usage_full_calls: int = 0
    usage_partial_calls: int = 0
    usage_malformed_calls: int = 0
    parse_error_calls: int = 0
    identities_agree: bool = True
    # Only snapshot identities are retained, never a second durable invocation log.
    terminal_reports: dict[CallIdentity, bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.snapshot_node is not None:
            self.terminal_reports = {
                (call.task_id, call.audit_round_num, call.round_num): None
                for call in self.snapshot_node.invocations.values()
            }
            self.identities_agree = len(self.terminal_reports) == len(
                self.snapshot_node.invocations
            ) and all(key[0].strip() for key in self.terminal_reports)

    def observe(self, event: ExecutionEvent) -> None:
        context = event.context
        identity = (context.task_id or "", context.audit_round_num, context.round_num)
        if not identity[0].strip() or identity not in self.terminal_reports:
            self.identities_agree = False
        if event.event_type not in _TERMINAL_EVENTS:
            return
        payload = event.payload
        if not isinstance(payload, InvocationEventPayload):
            raise TypeError("Terminal invocation event requires invocation payload.")
        self.record(payload)
        if identity in self.terminal_reports:
            if self.terminal_reports[identity] is not None:
                self.identities_agree = False
            self.terminal_reports[identity] = (
                is_nonnegative_int(payload.provider_usage_report_count)
                and payload.provider_usage_report_count > 0
            )

    def record(self, payload: InvocationEventPayload) -> None:
        self.terminal_calls += 1
        self.tokens.record(payload)
        count = payload.provider_usage_report_count
        if is_nonnegative_int(count):
            self.known_report_calls += 1
            self.positive_report_calls += int(count > 0)
            self.zero_report_calls += int(count == 0)
        else:
            self.missing_report_count_calls += 1
        if is_nonnegative_int(payload.attempt_count):
            self.known_attempt_calls += 1
            self.attempt_count += payload.attempt_count
        else:
            self.missing_attempt_count_calls += 1
        self.usage_full_calls += int(payload.provider_usage_status == "full")
        self.usage_partial_calls += int(payload.provider_usage_status == "partial")
        self.usage_malformed_calls += int(payload.provider_usage_status == "malformed")
        self.parse_error_calls += int(bool(payload.usage_parse_error))

    def freeze(self) -> NodeTokenAggregate:
        reconciled = self.snapshot_node is not None and self.identities_agree
        return NodeTokenAggregate(
            node_id=self.node_id,
            input=self.tokens.tokens.input,
            cached_input=self.tokens.tokens.cached_input,
            cache_write=self.tokens.tokens.cache_write,
            output=self.tokens.tokens.output,
            reasoning=self.tokens.tokens.reasoning,
            total=self.tokens.tokens.total,
            report_count=self.tokens.report_count if self.known_report_calls else None,
            attempt_count=self.attempt_count if self.known_attempt_calls else None,
            missing_report_count_calls=self.missing_report_count_calls,
            missing_attempt_count_calls=self.missing_attempt_count_calls,
            positive_report_calls=self.positive_report_calls,
            zero_report_calls=self.zero_report_calls,
            terminal_calls=self.terminal_calls,
            observed_calls=len(self.terminal_reports) if reconciled else None,
            calls_with_reports=(
                sum(value is True for value in self.terminal_reports.values())
                if reconciled
                else None
            ),
            calls_without_terminal=(
                sum(value is None for value in self.terminal_reports.values())
                if reconciled
                else None
            ),
            usage_full_calls=self.usage_full_calls,
            usage_partial_calls=self.usage_partial_calls,
            usage_malformed_calls=self.usage_malformed_calls,
            parse_error_calls=self.parse_error_calls,
            status=self.snapshot_node.status if self.snapshot_node else None,
            exact=True,
        )


def aggregate_tokens(
    events: Iterable[ExecutionEvent], snapshot: DashboardSnapshot | None = None
) -> TokenAggregates:
    """Project eligible terminal payloads once into node and provider scopes."""
    nodes = {
        node_id: NodeTokenCounter(node_id, node)
        for node_id, node in (snapshot.state.nodes.items() if snapshot else ())
    }
    unattributed: NodeTokenCounter | None = None
    overall: ExactTokenCounter | None = None
    providers: dict[str, ExactTokenCounter] = {}
    for event in events:
        if not is_invocation_event_type(event.event_type):
            continue
        node_id = event.context.node_id
        if node_id is not None and node_id.strip():
            nodes.setdefault(node_id, NodeTokenCounter(node_id)).observe(event)
        else:
            if unattributed is None:
                unattributed = NodeTokenCounter(None)
            unattributed.observe(event)
        if event.event_type not in _TERMINAL_EVENTS:
            continue
        payload = event.payload
        if not isinstance(payload, InvocationEventPayload):
            raise TypeError("Terminal invocation event requires invocation payload.")
        if not is_nonnegative_int(payload.provider_usage_report_count):
            continue
        if overall is None:
            overall = ExactTokenCounter()
        overall.record(payload)
        providers.setdefault(
            event.context.provider or "unknown", ExactTokenCounter()
        ).record(payload)
    return TokenAggregates(
        provider=ProviderTokenAggregates(
            overall=overall.freeze(None) if overall is not None else None,
            providers=tuple(providers[key].freeze(key) for key in sorted(providers)),
        ),
        nodes=tuple(nodes[key].freeze() for key in sorted(nodes)),
        unattributed=unattributed.freeze() if unattributed is not None else None,
        exact=True,
    )

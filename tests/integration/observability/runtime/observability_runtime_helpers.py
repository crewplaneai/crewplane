from threading import Event

from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)


def provider(name: str) -> ProviderSpec:
    return ProviderSpec(provider=name)


def single_node_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="runtime.sample",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="a")],
                providers=[provider("alpha")],
            ),
        ],
    )


def two_node_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="runtime.compact",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="a")],
                providers=[provider("alpha")],
            ),
            WorkflowNode(
                id="node.b",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="b")],
                providers=[provider("beta")],
            ),
        ],
    )


def provider_label_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="runtime.provider.labels",
        nodes=[
            WorkflowNode(
                id="review.context",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="a")],
                providers=[provider("alpha"), provider("beta")],
            ),
            WorkflowNode(
                id="review.summary",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="b")],
                needs=["review.context"],
                providers=[ProviderSpec(provider="gamma", role="executor")],
            ),
        ],
    )


class RecordingObserver:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.event_types: list[str | None] = []
        self.workflow_statuses: list[str] = []

    def start(self, context) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        self.started = True

    def on_snapshot(self, event, snapshot) -> None:  # type: ignore[no-untyped-def]
        self.event_types.append(event.event_type if event is not None else None)
        self.workflow_statuses.append(snapshot.state.workflow_status)

    def stop(self, result) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        self.stopped = True


class FailingObserver(RecordingObserver):
    def on_snapshot(self, event, snapshot) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        raise RuntimeError("observer failure")


class CountingFailingObserver(RecordingObserver):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    def on_snapshot(self, event, snapshot) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        self.call_count += 1
        raise RuntimeError("observer failure")


class StartFailObserver(RecordingObserver):
    def start(self, context) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        raise RuntimeError("start failure")


class BlockingStartObserver(RecordingObserver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()

    def start(self, context) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        self.entered.set()
        Event().wait()


class DelayedStartObserver(RecordingObserver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self.cleaned_up = Event()

    def start(self, context) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        self.entered.set()
        self.release.wait()

    def stop(self, result) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        self.cleaned_up.set()


class NoCleanupDelayedStartObserver(DelayedStartObserver):
    cleanup_after_start_timeout = False


class RequiredFailingObserver(RecordingObserver):
    required = True

    def on_snapshot(self, event, snapshot) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        raise RuntimeError("required observer failure")


class RequiredStopFailObserver(RecordingObserver):
    required = True

    def stop(self, result) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        raise RuntimeError("required stop failure")


class RequiredStopThreadFailureObserver(RecordingObserver):
    required = True


class RequiredStopTimeoutObserver(RecordingObserver):
    required = True

    def stop(self, result) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback or protocol signature.
        Event().wait()


class RequiredStartThreadFailureObserver(RecordingObserver):
    required = True


class BlockingObserver(RecordingObserver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def on_snapshot(self, event, snapshot) -> None:  # type: ignore[no-untyped-def]
        self.entered.set()
        self.release.wait()
        super().on_snapshot(event, snapshot)


def status_option_write_count(calls: list[tuple[list[str], bool, bool]]) -> int:
    return sum(
        1
        for args, _, _ in calls
        if len(args) >= 4
        and args[0] == "set-option"
        and args[3] in {"status-left", "status-right"}
    )


def pane_option_write_count(
    calls: list[tuple[list[str], bool, bool]],
    option: str,
) -> int:
    return len(pane_option_writes(calls, option))


def pane_option_writes(
    calls: list[tuple[list[str], bool, bool]],
    option: str,
) -> list[list[str]]:
    return [
        args
        for args, _, _ in calls
        if len(args) >= 6
        and args[:3] == ["set-option", "-p", "-t"]
        and args[4] == option
    ]


def binding_map(
    calls: list[tuple[list[str], bool, bool]],
) -> dict[tuple[str, str], str]:
    return {
        (args[2], args[3]): " ".join(args[4:])
        for args, _, _ in calls
        if len(args) >= 4 and args[0] == "bind-key" and args[1] == "-T"
    }

"""Stable values for the fixed document-processing workflow."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Mapping, Protocol


class WorkflowStage(StrEnum):
    PREFLIGHT = 'preflight'
    EXTRACT = 'extract'
    UNDERSTAND = 'understand'
    ANSWER = 'answer'
    VALIDATE = 'validate'
    WRITE = 'write'
    VERIFY = 'verify'


class WorkflowOperation(StrEnum):
    RUN = 'run'
    APPLY = 'apply'


class NodeStatus(StrEnum):
    PENDING = 'pending'
    RUNNING = 'running'
    SUCCESS = 'success'
    SKIPPED = 'skipped'
    FAILED = 'failed'
    INTERRUPTED = 'interrupted'


@dataclass(frozen=True)
class WorkflowRequest:
    identity: str
    operation: WorkflowOperation = WorkflowOperation.RUN
    answer_mode: str = 'grounded'
    payload: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)


@dataclass
class WorkflowContext:
    request: WorkflowRequest
    cancelled: Callable[[], bool] = lambda: False
    artifacts: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeResult:
    stage: WorkflowStage
    status: NodeStatus = NodeStatus.SUCCESS
    artifacts: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)
    counts: Mapping[str, int] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True)
class WorkflowProgress:
    node_id: str
    status: NodeStatus
    occurred_at: str
    counts: Mapping[str, int] = field(default_factory=dict)
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {
            'node_id': self.node_id,
            'status': self.status.value,
            'occurred_at': self.occurred_at,
            'counts': dict(self.counts),
            'error_code': self.error_code,
        }


@dataclass(frozen=True)
class WorkflowResult:
    identity: str
    status: NodeStatus
    nodes: tuple[NodeResult, ...]
    artifacts: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)
    failure: BaseException | None = field(default=None, repr=False, compare=False)


class WorkflowNode(Protocol):
    stage: WorkflowStage

    def execute(self, context: WorkflowContext) -> NodeResult: ...

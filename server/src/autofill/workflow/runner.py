"""Fail-stop runner for the fixed seven-stage workflow graph."""

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Callable, Iterable

from ..domain.errors import normalize_error_code
from .contracts import (
    NodeResult,
    NodeStatus,
    WorkflowContext,
    WorkflowOperation,
    WorkflowProgress,
    WorkflowResult,
    WorkflowStage,
)


STAGES = tuple(WorkflowStage)
APPLY_SKIPPED = frozenset((WorkflowStage.UNDERSTAND, WorkflowStage.ANSWER))
COUNT_FIELDS = frozenset(('completed', 'total', 'attempt', 'batch', 'batches'))
SAFE_ERROR_CODES = frozenset((
    'TASK_CANCELLED', 'CANCELLED', 'PROCESSING_ERROR', 'LLM_TIMEOUT', 'LLM_AUTH',
    'LLM_RATE_LIMIT', 'LLM_BAD_JSON', 'LLM_CONNECTION', 'LLM_HTTP', 'LLM_ABORTED',
    'LLM_CONTENT_FILTER', 'LLM_INCOMPLETE_RESPONSE', 'SOURCE_CHANGED',
    'BUNDLE_CONTENT_CHANGED', 'VERIFICATION_FAILED', 'UNSUPPORTED_FORMAT',
    'UNSUPPORTED_QUESTIONNAIRE', 'UNSUPPORTED_DOCUMENT_STRUCTURE', 'DOCUMENT_CORRUPT',
    'DOCUMENT_ACTIVE_CONTENT', 'FORMAT_EXTENSION_MISMATCH', 'PASSWORD_REQUIRED',
    'DOCUMENT_PROTECTED', 'UPLOAD_SIZE', 'PAGE_LIMIT', 'QUESTION_LIMIT', 'NO_FILL_TARGETS',
    'NO_READABLE_TEXT', 'PROCESS_TIMEOUT', 'CONVERSION_FAILED', 'PROCESSOR_UNAVAILABLE',
    'ANSWER_SCHEMA', 'REVIEW_REQUIRED', 'WRITE_PLAN_REQUIRED', 'OUTPUT_EXISTS',
    'TEST_FILENAME_REQUIRED', 'TEST_LABEL_TARGET_REQUIRED', 'TEST_LABEL_NOT_READY',
    'UNSUPPORTED_TEMPLATE', 'UNSUPPORTED_TARGET', 'TARGET_CHANGED', 'TARGET_BOUNDS',
))
def _safe_error_code(error) -> str:
    code = normalize_error_code(error)
    return code if code in SAFE_ERROR_CODES else 'PROCESSING_ERROR'


def _safe_counts(values) -> dict[str, int]:
    if not isinstance(values, Mapping):
        return {}
    return {key: value for key, value in values.items()
            if key in COUNT_FIELDS and type(value) is int and 0 <= value <= 1_000_000}


class WorkflowRunner:
    """Execute exactly seven injected nodes and stop after failure/interruption."""

    def __init__(self, nodes: Iterable, *, progress: Callable[[WorkflowProgress], None] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.nodes = tuple(nodes)
        if tuple(getattr(node, 'stage', None) for node in self.nodes) != STAGES:
            raise ValueError('WORKFLOW_GRAPH')
        if any(not isinstance(node.stage, WorkflowStage) for node in self.nodes):
            raise ValueError('WORKFLOW_GRAPH')
        self.progress = progress
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _emit(self, stage, status, *, counts=None, error_code=None):
        if self.progress is None:
            return
        timestamp = self.clock().astimezone(timezone.utc).isoformat()
        self.progress(WorkflowProgress(
            stage.value, status, timestamp, _safe_counts(counts or {}),
            error_code if error_code in SAFE_ERROR_CODES else None,
        ))

    def _skip(self, stage, results):
        result = NodeResult(stage, NodeStatus.SKIPPED)
        results.append(result)
        self._emit(stage, result.status)

    def run(self, context: WorkflowContext) -> WorkflowResult:
        results = []
        overall = NodeStatus.SUCCESS
        stopped = False
        failure = None
        for node in self.nodes:
            stage = node.stage
            if stopped:
                self._skip(stage, results)
                continue
            if context.request.operation == WorkflowOperation.APPLY and stage in APPLY_SKIPPED:
                self._skip(stage, results)
                continue
            if context.cancelled():
                result = NodeResult(stage, NodeStatus.INTERRUPTED, error_code='TASK_CANCELLED')
                results.append(result)
                self._emit(stage, result.status, error_code=result.error_code)
                overall = NodeStatus.INTERRUPTED
                failure = ValueError('TASK_CANCELLED')
                stopped = True
                continue

            self._emit(stage, NodeStatus.RUNNING)
            try:
                result = node.execute(context)
                if (not isinstance(result, NodeResult) or result.stage != stage
                        or not isinstance(result.status, NodeStatus)):
                    raise ValueError('WORKFLOW_NODE_RESULT')
                if result.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                    raise ValueError('WORKFLOW_NODE_RESULT')
                if result.status == NodeStatus.FAILED:
                    code = _safe_error_code(result.error_code or 'PROCESSING_ERROR')
                    result = NodeResult(stage, result.status, counts=_safe_counts(result.counts), error_code=code)
                    overall = NodeStatus.FAILED
                    failure = ValueError(code)
                    stopped = True
                elif result.status == NodeStatus.INTERRUPTED:
                    code = _safe_error_code(result.error_code or 'TASK_CANCELLED')
                    result = NodeResult(stage, result.status, counts=_safe_counts(result.counts), error_code=code)
                    overall = NodeStatus.INTERRUPTED
                    failure = ValueError(code)
                    stopped = True
                elif result.status == NodeStatus.SKIPPED:
                    result = NodeResult(stage, result.status, counts=_safe_counts(result.counts), error_code=None)
                else:
                    safe_artifacts = dict(result.artifacts)
                    if any(key in context.artifacts for key in safe_artifacts):
                        raise ValueError('WORKFLOW_ARTIFACT_COLLISION')
                    context.artifacts.update(safe_artifacts)
                    result = NodeResult(stage, NodeStatus.SUCCESS, safe_artifacts,
                                        _safe_counts(result.counts), None)
                results.append(result)
                self._emit(stage, result.status, counts=result.counts, error_code=result.error_code)
            except Exception as error:
                code = _safe_error_code(error)
                status = NodeStatus.INTERRUPTED if code in ('TASK_CANCELLED', 'CANCELLED') else NodeStatus.FAILED
                result = NodeResult(stage, status, error_code=code)
                results.append(result)
                self._emit(stage, status, error_code=code)
                overall = status
                failure = error
                stopped = True
        return WorkflowResult(context.request.identity, overall, tuple(results), dict(context.artifacts), failure)

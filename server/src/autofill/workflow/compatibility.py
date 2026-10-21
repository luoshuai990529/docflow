"""Legacy five-stage projection for consumers of the pre-runner job view."""

from .contracts import NodeStatus, WorkflowProgress, WorkflowStage
from .runner import COUNT_FIELDS, SAFE_ERROR_CODES


LEGACY_STAGE_BY_NODE = {
    WorkflowStage.PREFLIGHT.value: 'checking',
    WorkflowStage.EXTRACT.value: 'reading',
    WorkflowStage.UNDERSTAND.value: 'reading',
    WorkflowStage.ANSWER.value: 'matching',
    WorkflowStage.VALIDATE.value: 'matching',
    WorkflowStage.WRITE.value: 'writing',
    WorkflowStage.VERIFY.value: 'verifying',
}
_WORKFLOW_STATUS = frozenset(status.value for status in NodeStatus)


def legacy_stage_for(node_id):
    try:
        return LEGACY_STAGE_BY_NODE[str(node_id)]
    except KeyError:
        raise ValueError('WORKFLOW_STAGE') from None


def workflow_node_read_model(events):
    """Project safe events into the fixed order; old records remain untouched."""
    latest = {}
    for event in events if isinstance(events, list) else ():
        if not isinstance(event, dict):
            continue
        node_id, status = event.get('node_id'), event.get('status')
        if node_id not in LEGACY_STAGE_BY_NODE or status not in _WORKFLOW_STATUS:
            continue
        raw_counts = event.get('counts')
        counts = {key: value for key, value in raw_counts.items()
                  if isinstance(raw_counts, dict) and key in COUNT_FIELDS
                  and type(value) is int and 0 <= value <= 1_000_000} if isinstance(raw_counts, dict) else {}
        occurred_at = event.get('occurred_at')
        if not isinstance(occurred_at, str) or len(occurred_at) > 40:
            occurred_at = None
        error_code = event.get('error_code')
        if error_code not in SAFE_ERROR_CODES:
            error_code = None
        latest[node_id] = {
            'node_id': node_id,
            'status': status,
            'occurred_at': occurred_at,
            'counts': counts,
            'error_code': error_code,
        }
    return [latest.get(stage.value, {'node_id': stage.value, 'status': NodeStatus.PENDING.value})
            for stage in WorkflowStage]


class LegacyProgressSink:
    """Write runner events without changing the existing five-stage read view."""

    def __init__(self, store, identity):
        self.store = store
        self.identity = identity

    def __call__(self, event: WorkflowProgress):
        if not isinstance(event, WorkflowProgress):
            raise ValueError('WORKFLOW_EVENT')
        stage = legacy_stage_for(event.node_id)
        if event.status == NodeStatus.RUNNING:
            details = dict(event.counts)
            details['phase'] = event.node_id
            self.store.progress(self.identity, stage, **details)
        self.store.record_workflow_progress(self.identity, event)

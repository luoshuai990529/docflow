"""The workflow graph is fixed, fail-stop, cancellable, and event-safe."""

import json
import tempfile
import unittest
from pathlib import Path

from autofill.workflow.contracts import (
    NodeResult,
    NodeStatus,
    WorkflowContext,
    WorkflowOperation,
    WorkflowProgress,
    WorkflowRequest,
    WorkflowStage,
)
from autofill.workflow.compatibility import LegacyProgressSink, legacy_stage_for
from autofill.workflow.runner import WorkflowRunner


class FakeNode:
    def __init__(self, stage, *, calls, error=None, status=NodeStatus.SUCCESS, on_execute=None):
        self.stage = stage
        self.calls = calls
        self.error = error
        self.status = status
        self.on_execute = on_execute

    def execute(self, context):
        self.calls.append(self.stage)
        if self.on_execute:
            self.on_execute(context)
        if self.error:
            raise self.error
        return NodeResult(self.stage, self.status, artifacts={self.stage.value: self.stage.value},
                          counts={'completed': 1, 'total': 1})


class WorkflowRunnerContractTests(unittest.TestCase):
    def context(self, operation=WorkflowOperation.RUN, cancelled=lambda: False):
        return WorkflowContext(WorkflowRequest('run-1', operation=operation), cancelled=cancelled)

    def nodes(self, calls, **overrides):
        return [FakeNode(stage, calls=calls, **overrides.get(stage, {})) for stage in WorkflowStage]

    def test_runner_executes_the_fixed_order_and_emits_safe_typed_progress(self):
        calls = []
        events = []
        result = WorkflowRunner(self.nodes(calls), progress=events.append).run(self.context())
        self.assertEqual(calls, list(WorkflowStage))
        self.assertEqual([node.stage for node in result.nodes], list(WorkflowStage))
        self.assertEqual(result.status, NodeStatus.SUCCESS)
        self.assertIsInstance(events[0], WorkflowProgress)
        self.assertEqual(set(events[0].to_dict()), {'node_id', 'status', 'occurred_at', 'counts', 'error_code'})
        self.assertEqual(events[0].node_id, 'preflight')

    def test_failure_stops_later_nodes_and_reduces_error_to_safe_code(self):
        calls = []
        events = []
        configs = {WorkflowStage.EXTRACT: {'error': ValueError('LLM_TIMEOUT: private request body')}}
        result = WorkflowRunner(self.nodes(calls, **configs), progress=events.append).run(self.context())
        statuses = {node.stage: node.status for node in result.nodes}
        self.assertEqual(calls, [WorkflowStage.PREFLIGHT, WorkflowStage.EXTRACT])
        self.assertEqual(result.status, NodeStatus.FAILED)
        self.assertEqual(statuses[WorkflowStage.EXTRACT], NodeStatus.FAILED)
        self.assertTrue(all(statuses[stage] == NodeStatus.SKIPPED for stage in list(WorkflowStage)[2:]))
        event_dump = json.dumps([event.to_dict() for event in events])
        self.assertIn('LLM_TIMEOUT', event_dump)
        self.assertNotIn('private request body', event_dump)

    def test_cancellation_interrupts_next_node_and_skips_the_rest(self):
        calls = []
        events = []
        cancelled = {'value': False}
        configs = {WorkflowStage.PREFLIGHT: {'on_execute': lambda _context: cancelled.update(value=True)}}
        result = WorkflowRunner(self.nodes(calls, **configs), progress=events.append).run(
            self.context(cancelled=lambda: cancelled['value']))
        statuses = {node.stage: node.status for node in result.nodes}
        self.assertEqual(calls, [WorkflowStage.PREFLIGHT])
        self.assertEqual(result.status, NodeStatus.INTERRUPTED)
        self.assertEqual(statuses[WorkflowStage.EXTRACT], NodeStatus.INTERRUPTED)
        self.assertTrue(all(statuses[stage] == NodeStatus.SKIPPED for stage in list(WorkflowStage)[2:]))

    def test_explicitly_skipped_node_does_not_stop_later_nodes(self):
        calls = []
        configs = {WorkflowStage.UNDERSTAND: {'status': NodeStatus.SKIPPED}}
        result = WorkflowRunner(self.nodes(calls, **configs)).run(self.context())
        self.assertEqual(calls, list(WorkflowStage))
        self.assertEqual(result.nodes[2].status, NodeStatus.SKIPPED)
        self.assertEqual(result.status, NodeStatus.SUCCESS)

    def test_apply_skips_understand_and_answer_without_invoking_their_nodes(self):
        calls = []
        configs = {
            WorkflowStage.UNDERSTAND: {'error': AssertionError('must not run model-backed understand')},
            WorkflowStage.ANSWER: {'error': AssertionError('must not call model')},
        }
        result = WorkflowRunner(self.nodes(calls, **configs)).run(self.context(WorkflowOperation.APPLY))
        self.assertEqual(calls, [WorkflowStage.PREFLIGHT, WorkflowStage.EXTRACT,
                                 WorkflowStage.VALIDATE, WorkflowStage.WRITE, WorkflowStage.VERIFY])
        self.assertEqual(result.nodes[2].status, NodeStatus.SKIPPED)
        self.assertEqual(result.nodes[3].status, NodeStatus.SKIPPED)
        self.assertEqual(result.status, NodeStatus.SUCCESS)

    def test_runner_rejects_a_noncanonical_or_incomplete_node_graph(self):
        calls = []
        with self.assertRaisesRegex(ValueError, 'WORKFLOW_GRAPH'):
            WorkflowRunner(self.nodes(calls)[1:])
        with self.assertRaisesRegex(ValueError, 'WORKFLOW_GRAPH'):
            WorkflowRunner(list(reversed(self.nodes(calls))))

    def test_new_nodes_project_into_old_five_stage_view_without_migrating_node_ids(self):
        expected = {
            'preflight': 'checking', 'extract': 'reading', 'understand': 'reading',
            'answer': 'matching', 'validate': 'matching', 'write': 'writing', 'verify': 'verifying',
        }
        for node_id, old_stage in expected.items():
            self.assertEqual(legacy_stage_for(node_id), old_stage)

        from autofill.adapters.persistence.sqlite_store import SQLiteWorkflowStore, STAGES
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteWorkflowStore(Path(temporary) / 'state', Path(temporary) / 'key')
            try:
                identity = 'a' * 32
                store.create_run(identity, 'request-local-0001', {'questionnaire_name': 'synthetic.pdf'})
                sink = LegacyProgressSink(store, identity)
                sink(WorkflowProgress('preflight', NodeStatus.RUNNING, '2026-10-20T00:00:00+00:00'))
                sink(WorkflowProgress('preflight', NodeStatus.SUCCESS, '2026-10-20T00:00:01+00:00', {'completed': 1}))
                job = store.run(identity)
                self.assertEqual([item['stage'] for item in job['nodes']], list(STAGES))
                self.assertEqual(job['workflow_nodes'][0]['node_id'], 'preflight')
                self.assertEqual(job['workflow_nodes'][0]['status'], 'success')
                self.assertEqual(job['workflow_nodes'][0]['counts'], {'completed': 1})
                self.assertNotIn('workflow_events', job)
            finally:
                store.close()

    def test_recovery_terminates_current_workflow_node_and_skips_the_rest(self):
        from autofill.adapters.persistence.sqlite_store import SQLiteWorkflowStore

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state'
            key = Path(temporary) / 'key'
            identity = 'b' * 32
            store = SQLiteWorkflowStore(state, key)
            store.create_run(identity, 'request-recovery-local-0001', {'questionnaire_name': 'synthetic.pdf'})
            store.record_workflow_progress(identity, WorkflowProgress(
                'preflight', NodeStatus.RUNNING, '2026-10-20T00:00:00+00:00'))
            store.record_workflow_progress(identity, WorkflowProgress(
                'preflight', NodeStatus.SUCCESS, '2026-10-20T00:00:01+00:00'))
            store.record_workflow_progress(identity, WorkflowProgress(
                'extract', NodeStatus.RUNNING, '2026-10-20T00:00:02+00:00'))
            store.close()

            recovered = SQLiteWorkflowStore(state, key)
            try:
                job = recovered.run(identity)
                self.assertEqual(job['status'], 'interrupted')
                statuses = {node['node_id']: node['status'] for node in job['workflow_nodes']}
                self.assertEqual(statuses['preflight'], 'success')
                self.assertEqual(statuses['extract'], 'interrupted')
                self.assertTrue(all(status == 'skipped' for node, status in statuses.items()
                                    if node not in ('preflight', 'extract')))
            finally:
                recovered.close()

    def test_active_workflow_node_exposes_safe_legacy_phase_and_counts(self):
        from autofill.adapters.persistence.sqlite_store import SQLiteWorkflowStore

        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteWorkflowStore(Path(temporary) / 'state', Path(temporary) / 'key')
            try:
                identity = 'c' * 32
                store.create_run(identity, 'request-progress-local-0001', {'questionnaire_name': 'synthetic.pdf'})
                LegacyProgressSink(store, identity)(WorkflowProgress(
                    'answer', NodeStatus.RUNNING, '2026-10-20T00:00:00+00:00'))
                store.progress(identity, 'matching', phase='deciding', completed=2, total=4,
                               attempt=2, batch=1, batches=2)
                job = store.run(identity)
                answer = next(node for node in job['workflow_nodes'] if node['node_id'] == 'answer')
                self.assertEqual(answer['status'], 'running')
                self.assertEqual(answer['phase'], 'deciding')
                self.assertEqual(answer['counts'], {
                    'completed': 2, 'total': 4, 'attempt': 2, 'batch': 1, 'batches': 2,
                })
                sink = LegacyProgressSink(store, identity)
                sink(WorkflowProgress('answer', NodeStatus.SUCCESS, '2026-10-20T00:00:01+00:00'))
                sink(WorkflowProgress('write', NodeStatus.RUNNING, '2026-10-20T00:00:02+00:00'))
                store.progress(identity, 'matching', phase='answer_compaction', completed=1, total=2,
                               attempt=1)
                job = store.run(identity)
                write = next(node for node in job['workflow_nodes'] if node['node_id'] == 'write')
                self.assertEqual(write['status'], 'running')
                self.assertEqual(write['phase'], 'answer_compaction')
                self.assertEqual(write['counts'], {'completed': 1, 'total': 2, 'attempt': 1})
                store.progress(identity, 'writing', phase='writing')
                job = store.run(identity)
                write = next(node for node in job['workflow_nodes'] if node['node_id'] == 'write')
                self.assertEqual(write['phase'], 'writing')
                self.assertEqual(write['counts'], {})
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()

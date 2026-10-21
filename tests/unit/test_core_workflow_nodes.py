"""Synthetic fixed-template run/apply paths execute through the seven-node runner."""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from autofill.config import preset_config
from autofill.core import apply_pair, prepare_pair, run_pair
from autofill.general import apply_general, prepare_general
from autofill.models import Decision, document_from_dict
from tests.helpers import pdf, template
from autofill.workflow.contracts import WorkflowStage


class FakeModelClient:
    def __init__(self):
        self.calls = 0

    def generate_json(self, _prompt, _schema):
        self.calls += 1
        return SimpleNamespace(request_id='local-fake-request', data={'decisions': [{
            'question_id': 'company', 'status': 'ready', 'value': 'SAMPLE CO',
            'option_ids': [], 'span_ids': ['s1'], 'quote': 'Company: SAMPLE CO',
            'reason': 'Synthetic source evidence.', 'evidence': [],
        }]})


class CoreWorkflowNodeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.questionnaire = pdf(self.root / 'questionnaire.pdf')
        self.source = pdf(self.root / 'source.pdf', text='Company: SAMPLE CO')
        self.template_path = self.root / 'template.json'
        self.template_path.write_text(json.dumps(template(self.questionnaire)), encoding='utf-8')

    def tearDown(self):
        self.temporary.cleanup()

    def test_fixed_template_run_invokes_stages_in_order_and_keeps_originals_read_only(self):
        questionnaire_bytes = self.questionnaire.read_bytes()
        source_bytes = self.source.read_bytes()
        events = []
        client = FakeModelClient()
        result = run_pair(self.questionnaire, self.source, self.template_path,
                          preset_config('deepseek'), self.root / 'result', client=client,
                          workflow_progress=events.append)
        self.assertIsNotNone(result.output_path)
        self.assertEqual(client.calls, 1)
        completed = [event.node_id for event in events if event.status.value == 'running']
        self.assertEqual(completed, [stage.value for stage in WorkflowStage])
        self.assertEqual(self.questionnaire.read_bytes(), questionnaire_bytes)
        self.assertEqual(self.source.read_bytes(), source_bytes)

    def test_fixed_apply_skips_understand_and_answer_and_never_receives_a_model(self):
        questionnaire_bytes = self.questionnaire.read_bytes()
        source_bytes = self.source.read_bytes()
        bundle = prepare_pair(self.questionnaire, self.source, self.template_path, self.root / 'bundle')
        metadata = json.loads((bundle / 'metadata.json').read_text(encoding='utf-8'))
        answers = self.root / 'answers.json'
        answers.write_text(json.dumps({
            'origin': 'reviewed',
            'questionnaire_sha256': metadata['questionnaire_sha256'],
            'source_sha256': metadata['source_sha256'],
            'decisions': [asdict(Decision('company', 'ready', 'SAMPLE CO', [], ['s1'],
                                          'Company: SAMPLE CO', 'Reviewed synthetic evidence.'))],
        }), encoding='utf-8')
        events = []
        result = apply_pair(bundle, answers, self.root / 'apply-result', 'real', workflow_progress=events.append)
        self.assertIsNotNone(result.output_path)
        self.assertEqual([event.node_id for event in events if event.status.value == 'running'],
                         ['preflight', 'extract', 'validate', 'write', 'verify'])
        self.assertEqual({event.node_id: event.status.value for event in events if event.status.value != 'running'}
                         .get('answer'), 'skipped')
        self.assertEqual(self.questionnaire.read_bytes(), questionnaire_bytes)
        self.assertEqual(self.source.read_bytes(), source_bytes)

    def test_general_apply_skips_model_nodes_and_runs_through_shared_workflow(self):
        from autofill.domain.models import Decision as DomainDecision

        questionnaire = pdf(self.root / 'general-questionnaire.pdf', text='Company name', form=True)
        source = self.root / 'general-source.txt'
        source.write_text('Company: SAMPLE CO', encoding='utf-8')
        original = questionnaire.read_bytes()
        bundle = self.root / 'general-prepare'
        prepare_general(questionnaire, [source], bundle)
        q = document_from_dict(json.loads((bundle / 'questionnaire.json').read_text(encoding='utf-8')))
        s = document_from_dict(json.loads((bundle / 'source.json').read_text(encoding='utf-8')))
        target_question = next(question for question in q.questions
                               if question.kind == 'text' and question.target_ids)
        evidence_span = next(span for span in s.spans if 'SAMPLE CO' in span.text)
        decisions = [
            DomainDecision(question.id, 'ready', 'SAMPLE CO', [], [evidence_span.id],
                           'SAMPLE CO', 'Reviewed synthetic evidence.')
            if question.id == target_question.id else
            DomainDecision(question.id, 'not_found', None, [], [], None, 'No synthetic evidence.')
            for question in q.questions
        ]
        meta = json.loads((bundle / 'metadata.json').read_text(encoding='utf-8'))
        answers = self.root / 'general-answers.json'
        answers.write_text(json.dumps({
            'origin': 'reviewed',
            'questionnaire_sha256': q.sha256,
            'source_sha256': s.sha256,
            'decisions': [asdict(decision) for decision in decisions],
        }), encoding='utf-8')

        events = []
        result = apply_general(bundle, answers, self.root / 'general-apply-result', 'real',
                               workflow_progress=events.append)
        self.assertIsNotNone(result.output_path)
        self.assertEqual([event.node_id for event in events if event.status.value == 'running'],
                         ['preflight', 'extract', 'validate', 'write', 'verify'])
        self.assertEqual({event.node_id: event.status.value for event in events
                          if event.status.value != 'running'}.get('understand'), 'skipped')
        self.assertEqual({event.node_id: event.status.value for event in events
                          if event.status.value != 'running'}.get('answer'), 'skipped')
        self.assertEqual(questionnaire.read_bytes(), original)
        self.assertEqual(meta['answer_mode'], 'grounded')


if __name__ == '__main__':
    unittest.main()

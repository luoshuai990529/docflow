"""Document writes consume immutable, domain-approved operations."""

import unittest

from autofill.domain.models import Decision, DocumentData, Question, Target
from autofill.domain.write_plan import WritePlan, build_write_plan


class WritePlanContractTests(unittest.TestCase):
    def document(self):
        question = Question('q1', 'Choose one', 'single_choice', ['YES', 'NO'], ['yes', 'no'])
        yes = Target('yes', {'type': 'pdf_field', 'full_name': 'yes'}, {'exports': {'YES': 'Y'}})
        no = Target('no', {'type': 'pdf_field', 'full_name': 'no'}, {'exports': {'NO': 'N'}})
        marker = Target('marker', {'type': 'pdf_rect', 'rect': [1, 1, 10, 10]}, {'test_label': True})
        question.target_ids = ['yes', 'no', 'marker']
        return DocumentData('pdf', 'a' * 64, [], [question], [yes, no, marker], {})

    def test_builder_emits_only_ready_target_operations_without_mutating_inputs(self):
        document = self.document()
        decisions = [Decision('q1', 'ready', None, ['YES'], [], None, 'printed choice'),
                     Decision('q2', 'needs_review', 'ignored', [], [], None, 'pending')]
        plan = build_write_plan(document, decisions, 'real')
        self.assertIsInstance(plan, WritePlan)
        self.assertEqual(plan.source_sha256, 'a' * 64)
        self.assertEqual([(op.target_id, op.value) for op in plan.operations],
                         [('yes', 'Y'), ('no', 'YES'), ('marker', 'YES')])
        self.assertEqual([decision.status for decision in decisions], ['ready', 'needs_review'])

    def test_fictional_test_marker_is_program_owned(self):
        document = self.document()
        plan = build_write_plan(document, [Decision('q1', 'ready', None, ['YES'], [], None, 'test')], 'fictional_test')
        self.assertEqual(plan.mode, 'fictional_test')
        self.assertEqual([op.value for op in plan.operations], ['Y', 'YES', 'FICTIONAL DATA — LAYOUT TEST ONLY'])

    def test_plan_builder_rejects_unknown_mode_and_unready_test_marker(self):
        document = self.document()
        with self.assertRaisesRegex(ValueError, 'MODE'):
            build_write_plan(document, [], 'unknown')
        with self.assertRaisesRegex(ValueError, 'TEST_LABEL_NOT_READY'):
            build_write_plan(document, [Decision('q1', 'needs_review', None, [], [], None, 'pending')], 'fictional_test')

    def test_plan_freezes_target_metadata_and_document_adapter_requires_plan(self):
        plan = build_write_plan(self.document(), [Decision('q1', 'ready', None, ['YES'], [], None, 'test')], 'real')
        with self.assertRaises(TypeError):
            plan.operations[0].locator['full_name'] = 'changed'
        from autofill.adapters.documents.write import write_copy as document_write_copy
        with self.assertRaisesRegex(ValueError, 'WRITE_PLAN_REQUIRED'):
            document_write_copy(None, None, self.document())

    def test_ready_decision_rejects_unknown_question_and_duplicate_question_decisions(self):
        document = self.document()
        with self.assertRaisesRegex(ValueError, 'UNKNOWN_QUESTION'):
            build_write_plan(document, [Decision('missing', 'ready', 'x', [], [], None, 'bad')], 'real')
        duplicate = [Decision('q1', 'ready', 'x', [], [], None, 'one'),
                     Decision('q1', 'ready', 'y', [], [], None, 'two')]
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_DECISION'):
            build_write_plan(document, duplicate, 'real')


if __name__ == '__main__':
    unittest.main()

"""Synthetic offline contracts for the Office apply path."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autofill.domain.models import Decision, DocumentData, Question, Target, Verification, WriteResult
from autofill.general import apply_general, execute_general


class OfficeApplyContractTests(unittest.TestCase):
    def test_office_apply_creates_a_processor_for_readback_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / 'questionnaire.docx'
            original.write_bytes(b'synthetic Office original')
            questionnaire = DocumentData('docx', 'bundle-hash', [],
                [Question('q1', 'Company name', 'text', [], ['cell-a1'])],
                [Target('cell-a1', {'type': 'xlsx_cell'}, {})])
            source = DocumentData('collection', 'empty-source', [], [], [])
            processor = object()

            def write_synthetic_copy(_original, output, _plan):
                output.write_bytes(b'synthetic filled Office copy')
                return [WriteResult('q1', 'cell-a1', 'written', 'Acme', 'Acme', '')]

            def verify_synthetic_copy(_original, _output, _questionnaire, results, _work,
                                      actual_processor, **_kwargs):
                self.assertIs(actual_processor, processor)
                return results, Verification(True, 'passed', False, {})

            with patch('autofill.adapters.processor.processor.Processor', return_value=processor) as factory, \
                    patch('autofill.general.write_copy', side_effect=write_synthetic_copy), \
                    patch('autofill.general.verify_office', side_effect=verify_synthetic_copy):
                result = execute_general(original, questionnaire, source,
                    [Decision('q1', 'ready', 'Acme', [], [], None, 'Synthetic answer.')],
                    root / 'result')

            factory.assert_called_once_with()
            self.assertEqual(result.output_path.read_bytes(), b'synthetic filled Office copy')

    def test_apply_keeps_answer_binding_hashes_when_old_office_conversion_changes_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / 'legacy.doc'
            original.write_bytes(b'synthetic legacy Office source')
            bundle = root / 'bundle'
            bundle.mkdir()
            bundle_document = b'normalized bundle Office bytes'
            (bundle / 'questionnaire.docx').write_bytes(bundle_document)
            bundle_questionnaire = DocumentData('docx', hashlib.sha256(bundle_document).hexdigest(), [],
                [Question('q1', 'Company name', 'text', [], ['cell-a1'])],
                [Target('cell-a1', {'type': 'xlsx_cell'}, {})])
            bundle_source = DocumentData('collection', 'bundle-source-hash', [], [], [])
            (bundle / 'questionnaire.json').write_text(json.dumps(asdict(bundle_questionnaire)))
            (bundle / 'source.json').write_text(json.dumps(asdict(bundle_source)))
            (bundle / 'metadata.json').write_text(json.dumps({
                'questionnaire_path': str(original),
                'questionnaire_original_sha256': hashlib.sha256(original.read_bytes()).hexdigest(),
                'source_paths': [],
                'source_hashes': [],
                'answer_mode': 'autonomous',
                'conversion': {'format': 'docx'},
            }))
            answers = root / 'answers.json'
            answers.write_text(json.dumps({
                'origin': 'reviewed',
                'questionnaire_sha256': bundle_questionnaire.sha256,
                'source_sha256': bundle_source.sha256,
                'decisions': [asdict(Decision('q1', 'ready', 'Example company', [], [], None,
                                               'Synthetic reviewed answer.'))],
            }))

            converted_bytes = b'reconverted Office bytes with a new volatile identifier'
            fresh_questionnaire = DocumentData('docx', hashlib.sha256(converted_bytes).hexdigest(), [],
                bundle_questionnaire.questions, bundle_questionnaire.targets)
            fresh_source = DocumentData('collection', 'reextracted-source-hash', [], [], [])

            def reprepare(_path, _sources, destination, *, answer_mode):
                self.assertEqual(answer_mode, 'autonomous')
                destination.mkdir(parents=True)
                (destination / 'questionnaire.docx').write_bytes(converted_bytes)
                return fresh_questionnaire, fresh_source, {'format': 'docx'}

            result = object()
            output = root / 'result'
            with patch('autofill.general.prepare_general', side_effect=reprepare), \
                    patch('autofill.general.execute_general', return_value=result) as execute, \
                    patch('autofill.general._verify_run_result', side_effect=lambda value, _dir: value):
                returned = apply_general(bundle, answers, output, 'real')

            self.assertIs(returned, result)
            self.assertIs(execute.call_args.args[1], fresh_questionnaire)
            self.assertIs(execute.call_args.args[2], fresh_source)


if __name__ == '__main__':
    unittest.main()

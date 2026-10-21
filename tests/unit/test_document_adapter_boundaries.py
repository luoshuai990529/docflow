"""Synthetic-input contracts for document and sandbox adapters."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from zipfile import ZipFile

from autofill.adapters.documents.catalog import convert_doc, resolve_questionnaire
from autofill.adapters.documents.documents import read_document
from autofill.adapters.documents.preflight import Limits, inspect_file
from autofill.adapters.documents.preflight import LocalDocumentPreflight
from autofill.adapters.documents.questionnaire_structure import LocalQuestionnaireStructure
from autofill.application.ports import DocumentPreflight, QuestionnaireStructure
from autofill.adapters.processor.processor import Processor
from autofill.adapters.processor.processor import execute_task, reap_orphans


class DocumentAdapterBoundaryTests(unittest.TestCase):
    def test_application_ports_are_implemented_by_document_and_processor_adapters(self):
        self.assertIsInstance(LocalDocumentPreflight(), DocumentPreflight)
        self.assertIsInstance(LocalQuestionnaireStructure(), QuestionnaireStructure)

    def test_bad_magic_and_active_xml_fail_closed_during_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / 'questionnaire.docx'
            malformed.write_bytes(b'not a zip archive')
            with self.assertRaisesRegex(ValueError, 'FORMAT_EXTENSION_MISMATCH'):
                inspect_file(malformed, 'questionnaire', Limits())

            active = root / 'active.docx'
            with ZipFile(active, 'w') as archive:
                archive.writestr('word/document.xml', b'<!DOCTYPE root [<!ENTITY x "unsafe">]><root>&x;</root>')
            with self.assertRaisesRegex(ValueError, 'ACTIVE_CONTENT'):
                inspect_file(active, 'questionnaire', Limits())

    def test_unreviewed_questionnaire_is_rejected_before_model_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unknown = root / 'unknown.pdf'
            import pymupdf
            document = pymupdf.open()
            document.new_page().insert_text((30, 30), 'Synthetic unknown questionnaire')
            document.save(unknown)
            document.close()
            with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_QUESTIONNAIRE'):
                resolve_questionnaire(unknown, root / 'work')

    def test_legacy_office_template_conversion_uses_the_processor_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'reviewed.doc'
            source.write_bytes(b'synthetic legacy office bytes')
            class FakeProcessor:
                def __init__(self):
                    self.calls = []

                def convert(self, path, format, destination):
                    self.calls.append((Path(path), format, Path(destination)))
                    return Path(destination)

            fake = FakeProcessor()
            converted = convert_doc(source, root / 'converted', fake)
            self.assertEqual(converted, root / 'converted' / 'reviewed.pdf')
            self.assertEqual(fake.calls, [(source, 'pdf', converted)])
            self.assertEqual(source.read_bytes(), b'synthetic legacy office bytes')

    def test_processor_cancel_cleans_its_task_directory_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / 'queue'
            queue.mkdir()
            (queue / 'heartbeat').touch()
            source = root / 'input.docx'
            source.write_bytes(b'synthetic source')

            def acknowledge_cancel():
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    jobs = [path for path in queue.iterdir() if path.is_dir()]
                    if jobs and (jobs[0] / 'cancel').exists():
                        (jobs[0] / 'done.json').write_text(json.dumps({'error': 'CANCELLED'}))
                        return
                    time.sleep(0.01)

            helper = threading.Thread(target=acknowledge_cancel, daemon=True)
            helper.start()
            processor = Processor(queue, cancelled=lambda: True)
            with self.assertRaisesRegex(ValueError, 'CANCELLED'):
                processor.run(source, 'convert', root / 'output.pdf', 'pdf')
            helper.join(timeout=1)
            self.assertEqual(source.read_bytes(), b'synthetic source')
            self.assertFalse((root / 'output.pdf').exists())
            self.assertEqual([path.name for path in queue.iterdir()], ['heartbeat'])

    def test_processor_request_protocol_rejects_executable_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'PROCESS_OPERATION'):
                execute_task(temporary, {'operation': 'shell', 'command': 'echo unsafe'})


if __name__ == '__main__':
    unittest.main()

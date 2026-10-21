"""Small public workflow contracts; synthetic inputs and no external model calls."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from autofill.models import Decision, Question, Verification, WriteResult
from autofill.adapters.documents.report import build_report, prepare_report
from autofill.service import WorkflowService


class WorkflowStateTests(unittest.TestCase):
    def test_office_capacity_distinguishes_reflow_fixed_rows_and_native_limits(self):
        from zipfile import ZipFile
        from autofill.adapters.documents.documents import W, node_paths
        from autofill.models import DocumentData, Target
        from autofill.adapters.documents.office import office_answer_budgets
        from autofill.adapters.documents.preflight import xml
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capacity.docx'
            body = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
              <w:p><w:r><w:t>Answer: ___</w:t></w:r></w:p>
              <w:tbl><w:tr><w:trPr><w:trHeight w:val="480" w:hRule="exact"/></w:trPr>
                <w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/></w:tcPr><w:p/></w:tc>
              </w:tr></w:tbl>
              <w:p><w:r><w:fldChar><w:ffData><w:textInput><w:maxLength w:val="8"/></w:textInput></w:ffData></w:fldChar></w:r></w:p>
              </w:body></w:document>'''
            styles = '''<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:rPr><w:sz w:val="24"/></w:rPr></w:style></w:styles>'''
            root = xml(body.encode()); paths = node_paths(root)
            flow = root.find('.//' + W + 'p'); fixed = root.find('.//' + W + 'tc'); field = root.find('.//' + W + 'ffData')
            targets = [Target('flow', dict(type='docx_runs', path=paths[id(flow)]), {}),
                       Target('fixed', dict(type='docx_path', path=paths[id(fixed)]), {}),
                       Target('limit', dict(type='docx_legacy', path=paths[id(field)], checkbox=False, text_paths=[]), {})]
            document = DocumentData('docx', '', [], [], targets)
            with ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', body)
                archive.writestr('word/styles.xml', styles)
            budget = office_answer_budgets(path, document)
            self.assertEqual(budget['flow']['space_type'], 'flow')
            self.assertIsNone(budget['flow']['suggested_max_chars'])
            self.assertEqual(budget['fixed']['space_type'], 'fixed')
            self.assertEqual(budget['fixed']['font_size'], 12)
            self.assertEqual(budget['fixed']['max_lines'], 1)
            self.assertGreater(budget['fixed']['suggested_max_chars'], 0)
            self.assertEqual(budget['limit']['native_max_length'], 8)
            self.assertEqual(budget['limit']['space_type'], 'explicit_limit')
            from autofill.adapters.documents.office import write_office
            native_results = write_office(path, Path(tmp) / 'native-limit.docx', document,
                [('q1', targets[2], 'too long for this native field')])
            self.assertEqual(native_results[0].status, 'layout_blocked')
            self.assertEqual(native_results[0].reason, 'NATIVE_MAX_LENGTH')
            # Absent native font information is not silently treated as 10pt.
            with ZipFile(path, 'w') as archive: archive.writestr('word/document.xml', body)
            self.assertIsNone(office_answer_budgets(path, document)['fixed']['suggested_max_chars'])
            # The outer workflow can request the clipped candidates for one
            # batch rewrite instead of an internal remove-and-render cycle.
            import pymupdf as M
            from autofill.adapters.documents.office import verify_office, write_office
            class ClippingRenderer:
                def convert(self, source, fmt, destination):
                    with M.open() as pdf:
                        pdf.new_page().insert_text((30, 40), 'Question')
                        pdf.save(destination)
                    return Path(destination)
            output = Path(tmp) / 'filled.docx'; renderer = ClippingRenderer()
            results = write_office(path, output, document, [('q1', targets[1], 'Acme')])
            results, verification = verify_office(path, output, document, results,
                Path(tmp) / 'verify', renderer, defer_fixed_overflow=True)
            self.assertEqual(results[0].reason, 'FIXED_ROW_HEIGHT_OVERFLOW')
            self.assertEqual(results[0].status, 'layout_blocked')
            self.assertEqual(verification.layout_check, 'needs_review')
            with ZipFile(output) as archive:
                self.assertIn(b'Acme', archive.read('word/document.xml'))

    def test_excel_capacity_sums_merged_columns_and_never_shrinks_shared_rows(self):
        from zipfile import ZipFile
        from autofill.adapters.documents.documents import S
        from autofill.models import DocumentData, Target
        from autofill.adapters.documents.office import office_answer_budgets, write_office, xlsx_text_layout
        from autofill.adapters.documents.preflight import xml
        sheet = '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <cols><col min="1" max="1" width="8"/><col min="2" max="2" width="24"/><col min="3" max="3" width="8"/></cols>
          <sheetData><row r="1"><c r="A1"/><c r="C1"/></row></sheetData>
          <mergeCells><mergeCell ref="A1:B1"/></mergeCells></worksheet>'''
        styles = '''<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <fonts count="1"><font><sz val="12"/></font></fonts><cellXfs count="1"><xf fontId="0"/></cellXfs></styleSheet>'''
        root = xml(sheet.encode()); style_root = xml(styles.encode())
        layout = xlsx_text_layout(root, 'A1', 'W' * 120, style_root)
        # Width is 8 + 24, not twice the first column's width.
        self.assertEqual(layout['width'], ((8 * 7 + 5) + (24 * 7 + 5)) * .75)
        self.assertGreater(layout['height'], 15)
        self.assertFalse(layout['overflow'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capacity.xlsx'; output = Path(tmp) / 'filled.xlsx'
            with ZipFile(path, 'w') as archive:
                archive.writestr('xl/worksheets/sheet1.xml', sheet)
                archive.writestr('xl/styles.xml', styles)
            targets = [Target(address, dict(type='xlsx_cell', part='xl/worksheets/sheet1.xml', cell=address), {})
                       for address in ('A1', 'C1')]
            document = DocumentData('xlsx', '', [], [], targets)
            budgets = office_answer_budgets(path, document)
            self.assertEqual(budgets['A1']['space_type'], 'flow')
            self.assertGreater(budgets['A1']['suggested_max_chars'], budgets['C1']['suggested_max_chars'])
            initial_results = write_office(path, output, document, [('q1', targets[0], 'W' * 120), ('q2', targets[1], 'ok')])
            self.assertTrue(all(r.reason == 'XLSX_ROW_HEIGHT_CANDIDATE' for r in initial_results))
            with ZipFile(output) as archive:
                row = xml(archive.read('xl/worksheets/sheet1.xml')).find(S + 'sheetData/' + S + 'row')
            self.assertEqual(float(row.get('ht')), layout['height'])
            merged = xml(sheet.replace('A1:B1', 'A1:B2').encode())
            self.assertEqual(xlsx_text_layout(merged, 'A1', 'ok', style_root)['reason'], 'MERGED_ROW_REVIEW')
            # Large-font narrow letters can fit even when the wrapping estimate
            # exceeds Excel's row limit. Real rendering, not the hint, decides.
            with ZipFile(path, 'w') as archive:
                archive.writestr('xl/worksheets/sheet1.xml', sheet)
                archive.writestr('xl/styles.xml', styles.replace('val="12"', 'val="20"'))
            document.questions = [Question('q3', 'Answer', 'text', [], ['C1'])]
            from autofill.adapters.documents.answer_layout import check_answer_fit
            self.assertEqual(check_answer_fit(path, document,
                Decision('q3', 'ready', 'i' * 100, [], [], None, '')), [])
            narrow = Path(tmp) / 'narrow.xlsx'
            results = write_office(path, narrow, document, [('q3', targets[1], 'i' * 100)])
            self.assertEqual(results[0].status, 'pending_readback')
            self.assertEqual(results[0].reason, 'XLSX_ROW_HEIGHT_CANDIDATE')
            with ZipFile(narrow) as archive:
                row = xml(archive.read('xl/worksheets/sheet1.xml')).find(S + 'sheetData/' + S + 'row')
                self.assertEqual(float(row.get('ht')), 409)

    def test_decision_profiles_are_optional_encrypted_and_do_not_replace_the_main_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkflowService(Path(tmp))
            try:
                for provider in ('typesafe',):
                    with self.subTest(provider=provider):
                        profile = service.store.save_profile(dict(name='Fake decision', provider=provider,
                            api_key='FAKE-DECISION-SECRET'))
                        self.assertEqual(profile['protocol'], 'typesafe_systemone')
                        self.assertFalse(service.configurations()['configured'])
                        self.assertNotIn('FAKE-DECISION-SECRET', json.dumps(service.configurations()))
                        config, key, snapshot = service._configuration({'config_id': profile['id']},
                            protocol='typesafe_systemone')
                        self.assertEqual(config.protocol, 'typesafe_systemone')
                        self.assertEqual(key, 'FAKE-DECISION-SECRET')
                        self.assertNotIn(key, json.dumps(snapshot))
                        with self.assertRaisesRegex(ValueError, 'CONFIG_MODEL_ROLE'):
                            service._configuration({'config_id': profile['id']})
                        with self.assertRaisesRegex(ValueError, 'CONFIG_KEY_REENTRY'):
                            service.store.save_profile(dict(profile, base_url='http://127.0.0.1:9998/v1'), profile['id'])
                profile = service.store.save_profile(dict(name='Legacy fake', provider='deepseek',
                    api_key='FAKE-PRIMARY-SECRET'))
                # Existing profile metadata predates the protocol field.
                with service.store.lock, service.store.db:
                    row = service.store.db.execute('SELECT metadata FROM profiles WHERE id=?', (profile['id'],)).fetchone()
                    metadata = json.loads(row[0]); metadata.pop('protocol')
                    service.store.db.execute('UPDATE profiles SET metadata=? WHERE id=?', (json.dumps(metadata), profile['id']))
                self.assertTrue(service.configurations()['configured'])
                self.assertEqual(service._configuration({'config_id': profile['id']})[0].protocol, 'openai_chat')
                self.assertNotIn(b'FAKE-DECISION-SECRET', (service.store.directory / 'workflow.sqlite3').read_bytes())
            finally:
                service.close()

    def test_office_baseline_is_reused_within_task_without_skipping_verification(self):
        from zipfile import ZipFile
        import pymupdf as M
        from autofill.general import prepare_general, execute_general
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); original = root / 'q.docx'; source = root / 's.txt'
            with ZipFile(original, 'w') as archive:
                archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
                archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Company</w:t></w:r></w:p></w:tc><w:tc><w:p/></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>')
            source.write_text('Company: Acme'); before = original.read_bytes(); calls = []
            class Renderer:
                def convert(self, path, fmt, destination):
                    calls.append(Path(path).name)
                    with M.open() as pdf:
                        page = pdf.new_page(); page.insert_text((30, 40), 'Company')
                        if Path(path).name == 'filled.docx': page.insert_text((30, 70), 'Acme')
                        pdf.save(destination)
                    return Path(destination)
            processor = Renderer(); bundle = root / 'prepare'
            q, s, conversion = prepare_general(original, [source], bundle, processor)
            self.assertTrue((bundle / 'baseline.pdf').exists())
            result = execute_general(bundle / 'questionnaire.docx', q, s,
                [Decision(q.questions[0].id, 'ready', 'Acme', [], [], None, '')], root / 'result', processor,
                baseline=bundle / 'baseline.pdf')
            self.assertIsNotNone(result.output_path)
            self.assertEqual(calls, ['q.docx', 'filled.docx'])
            self.assertEqual(json.loads(result.report_json.read_text())['verification']['layout_check'], 'passed')
            self.assertEqual(original.read_bytes(), before)

    def test_one_summary_counts_partial_readbacks_without_claiming_complete(self):
        questions = [Question('q1', '名称与地址？', 'text', [], ['a', 'b']),
                     Question('q2', '联系人？', 'text', [], ['c'])]
        decisions = [Decision('q1', 'ready', '示例', [], [], None, ''),
                     Decision('q2', 'not_found', None, [], [], None, '资料中没有联系人。')]
        results = [WriteResult('q1', 'a', 'written', '示例', '示例', ''),
                   WriteResult('q1', 'b', 'layout_blocked', '地址', None, 'TEXT_OVERFLOW')]
        report = build_report(questions, decisions, results, Verification(True, 'passed', False, {}), {}, 'hash', 'real')
        report['published_file'] = 'filled.pdf'
        prepared = prepare_report(report)
        summary = prepared['result_summary']
        self.assertEqual(summary['counts'], dict(total=2, complete=0, partial=1, blank=1))
        self.assertEqual([row['number'] for row in summary['pending']], [1, 2])
        self.assertIn('内容超出', summary['pending'][0]['reason'])
        self.assertEqual(summary, prepare_report(prepared)['result_summary'])
        self.assertEqual(prepared['reading_summary']['统计']['其中部分填写'], 1)

    def test_completed_partial_task_keeps_completed_nodes_and_shared_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkflowService(Path(tmp))
            try:
                store = service.store
                store.create_run('a' * 32, 'request-1234567890', {})
                store.progress('a' * 32, 'matching', phase='reviewing', completed=0, total=1)
                store.progress('a' * 32, 'verifying')
                store.progress('a' * 32, 'matching', phase='answer_compaction', completed=0, total=1, attempt=1)
                matching=next(n for n in store.run('a'*32)['nodes'] if n['stage']=='matching')
                self.assertIsNone(matching['finished_at'])
                store.progress('a' * 32, 'verifying')
                store.finish('a' * 32, dict(status='done', outcome='partial'))
                job = store.run('a' * 32)
                self.assertEqual({n['stage']: n['status'] for n in job['nodes']}['matching'], 'success')
            finally:
                service.close()

    def test_history_summary_is_derived_without_rewriting_old_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkflowService(Path(tmp))
            try:
                path = Path(tmp) / 'old-report.json'
                report = build_report([Question('q1', '联系人？', 'text', [], ['a'])],
                    [Decision('q1', 'not_found', None, [], [], None, '')], [],
                    Verification(True, 'not_run', True, {}), {}, None, 'real')
                path.write_text(json.dumps(report)); before = path.read_bytes()
                service.store.create_run('c' * 32, 'request-1234567892', {})
                service.store.finish('c' * 32, dict(status='done', outcome='partial', files={'report.json': str(path)}))
                self.assertEqual(service.public_job('c' * 32)['summary']['counts']['blank'], 1)
                self.assertEqual(service.jobs()[0]['summary'], service.public_job('c' * 32)['summary'])
                self.assertEqual(path.read_bytes(), before)
            finally:
                service.close()

    def test_cancel_is_acknowledged_and_diagnostic_survives_scratch_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkflowService(Path(tmp))
            identity = 'b' * 32
            directory = Path(tmp) / 'persistent' / identity
            work = directory / 'task-work-v2'
            paths = [work / '0' / 'q.txt', work / '1' / 's.txt']
            for path in paths:
                path.parent.mkdir(parents=True)
                path.write_text('synthetic only')
            service.store.create_run(identity, 'request-1234567891', {'version': 2})
            control = {'cancel': threading.Event()}
            service.tasks.controls[identity] = control
            class Config:
                model = 'fake'
                provider = 'fake'
            try:
                service.store.progress(identity, 'matching', phase='reviewing', completed=1, total=2)
                service.tasks.cancel(identity)
                self.assertTrue(service.store.run(identity)['cancelling'])
                service.tasks.work(identity, paths, Config(), 'FAKE-ONLY', control)
                job = service.store.run(identity)
                self.assertEqual(job['status'], 'cancelled')
                diagnostic = json.loads((directory / 'failure.json').read_text())
                self.assertEqual(diagnostic['stage'], 'matching')
                self.assertEqual(diagnostic['progress']['phase'], 'reviewing')
                self.assertEqual(len(diagnostic['input_sha256']), 2)
                self.assertNotIn('FAKE-ONLY', json.dumps(diagnostic))
                self.assertNotIn('synthetic only', json.dumps(diagnostic))
                self.assertFalse(work.exists())
            finally:
                service.close()

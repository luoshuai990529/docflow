"""Source-free preparation/apply and truthful, readback-based result reporting."""
import json
import tempfile
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import asdict
from pathlib import Path

import pymupdf as M

from autofill.general import prepare_general, apply_general, run_general
from autofill.config import LLMConfig
from autofill.adapters.models.openai_chat import OpenAIChatClient
from autofill.models import Decision, Question, Verification, WriteResult
from autofill.adapters.documents.report import build_report, prepare_report, render_html, render_markdown


class AutonomousWorkflowTests(unittest.TestCase):
    def test_actual_layout_repairs_only_overflow_and_stops_after_two_rounds(self):
        for repaired in (True, False):
            with self.subTest(repaired=repaired), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); original = root / 'questionnaire.pdf'; calls = []
                with M.open() as pdf:
                    page = pdf.new_page(width=400, height=250)
                    for i, name in enumerate(('Long', 'Stable')):
                        page.insert_text((20, 35+i*85), name)
                        widget = M.Widget(); widget.field_name = name
                        widget.field_type = M.PDF_WIDGET_TYPE_TEXT
                        widget.rect = M.Rect(20, 45+i*85, 115, 75+i*85)
                        widget.text_fontsize = 10; page.add_widget(widget)
                    pdf.save(original)
                before = original.read_bytes()
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args): pass
                    def do_POST(self):
                        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        body = json.loads(request['messages'][-1]['content'].split('\n', 1)[1]); calls.append(body)
                        if body['phase'] == 'question_inventory':
                            spans = {s['id']:s for s in body['spans']}
                            data = dict(reviewed_span_ids=list(spans), excluded_target_ids=[], items=[dict(
                                span_ids=slot['span_ids'], quote=spans[slot['span_ids'][0]]['text'],
                                kind='text', options=[], target_id=slot['target_id']) for slot in body['slots']])
                        elif body['phase'] == 'autonomous_context':
                            data = dict(scenario='One example company.', assumptions='Fictional local test.')
                        else:
                            compact = body['phase'] == 'answer_compaction'
                            data = {'answers':[dict(question_id=q['id'],
                                value=('Acme' if compact and repaired else 'W'*160) if 'Long' in q['text'] else 'OK',
                                option_ids=[], assumptions='Fictional local test.', reason='Model example.') for q in body['questions']]}
                        response = json.dumps({'choices':[{'message':{'content':json.dumps(data)},'finish_reason':'stop'}]}).encode()
                        self.send_response(200); self.send_header('Content-Length', str(len(response)))
                        self.end_headers(); self.wfile.write(response)
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    config = LLMConfig('local', f'http://127.0.0.1:{server.server_port}/v1', 'fake', 'FAKE_LOCAL_KEY')
                    client = OpenAIChatClient(config, api_key='fake-local-only')
                    result = run_general(original, [], config, root/'result', client, answer_mode='autonomous')
                finally:
                    server.shutdown(); server.server_close(); thread.join()
                self.assertIsNotNone(result.output_path)
                report = json.loads(result.report_json.read_text())
                long = next(row for row in report['rows'] if 'Long' in row['question'])
                stable = next(row for row in report['rows'] if 'Stable' in row['question'])
                self.assertEqual(stable['status'], 'written'); self.assertEqual(stable['answer'], 'OK')
                self.assertEqual(long['status'], 'written' if repaired else 'layout_blocked')
                self.assertEqual(report['verification']['layout_check'], 'passed')
                generation = next(c for c in calls if c['phase'] == 'autonomous_answers')
                self.assertIn('suggested_max_chars', generation['answer_constraints'][long['question_id']])
                compactions = [c for c in calls if c['phase'] == 'answer_compaction']
                self.assertEqual(len(compactions), 1 if repaired else 2)
                self.assertTrue(all([q['id'] for q in c['questions']] == [long['question_id']] for c in compactions))
                layout = long['autonomous_answer']['layout']
                self.assertEqual(layout['original_chars'], 160)
                self.assertEqual(layout['final_chars'], 4 if repaired else 160)
                self.assertEqual(layout['compression_attempts'], len(compactions))
                self.assertEqual(layout['fit_status'], 'fits' if repaired else 'overflow')
                self.assertEqual(layout['attempts'][-1]['fit_status'], layout['fit_status'])
                if not repaired: self.assertIn('精简', long['review_note'])
                self.assertEqual(original.read_bytes(), before)

    def test_explicit_source_free_bundle_can_be_applied_without_changing_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); original = root / 'questionnaire.pdf'
            with M.open() as pdf:
                page = pdf.new_page(width=600, height=300)
                page.insert_text((30, 50), 'Company name')
                widget = M.Widget(); widget.field_name = 'Company name'
                widget.field_type = M.PDF_WIDGET_TYPE_TEXT; widget.rect = M.Rect(30, 70, 350, 110)
                page.add_widget(widget); pdf.save(original)
            before = original.read_bytes()
            q, sources, _ = prepare_general(original, [], root / 'bundle', answer_mode='autonomous')
            self.assertEqual(sources.spans, [])
            self.assertEqual(json.loads((root / 'bundle/metadata.json').read_text())['answer_mode'], 'autonomous')
            decisions = [Decision(question.id, 'ready', 'Example company', [], [], None, '模型拟定') for question in q.questions]
            answers = root / 'answers.json'
            answers.write_text(json.dumps(dict(origin='reviewed', questionnaire_sha256=q.sha256,
                source_sha256=sources.sha256, decisions=[asdict(d) for d in decisions])))
            result = apply_general(root / 'bundle', answers, root / 'result', 'real')
            self.assertIsNotNone(result.output_path)
            report = json.loads(result.report_json.read_text())
            self.assertEqual(report['answer_mode'], 'autonomous')
            self.assertEqual(report['result_summary']['counts']['complete'], 1)
            self.assertEqual(report['rows'][0]['answer'], 'Example company')
            self.assertEqual(original.read_bytes(), before)
            with self.assertRaises(ValueError):
                prepare_general(original, [], root / 'grounded')

    def test_autonomous_report_explains_assumptions_and_fallback_without_fake_citations(self):
        report = build_report([Question('q1', '企业名称', 'text', [], ['t1'])],
            [Decision('q1', 'ready', '示例企业', [], [], None, '模型拟定')],
            [WriteResult('q1', 't1', 'written', '示例企业', '示例企业', '')],
            Verification(True, 'passed', False, {}), {}, 'hash', 'real')
        report.update(answer_mode='autonomous', published_file='filled.docx')
        report['rows'][0]['autonomous_answer'] = dict(origin='llm', assumptions='企业名称为假设', fallback_code='JEV_TIMEOUT')
        prepared = prepare_report(report)
        self.assertEqual(prepared['result_summary']['mode_label'], '模型自主作答')
        self.assertEqual(prepared['rows'][0]['autonomous_answer']['fallback_code'], 'JEV_TIMEOUT')
        self.assertNotIn('核对原文', prepared['reading_summary']['下一步'])
        for content in (render_html(prepared), render_markdown(prepared)):
            self.assertIn('企业名称为假设', content)
            self.assertIn('模型自主作答', content)
            self.assertIn('回退', content)
            self.assertNotIn('此报告未记录原文引文', content)
        partial = build_report([Question('q1', '名称（重复位置）', 'text', [], ['t1','t2'])],
            [Decision('q1','ready','示例企业',[],[],None,'')],
            [WriteResult('q1','t1','written','示例企业','示例企业',''),
             WriteResult('q1','t2','layout_blocked','示例企业',None,'TEXT_OVERFLOW')],
            Verification(True,'passed',False,{}), {}, 'hash', 'real')
        partial.update(answer_mode='autonomous',published_file='filled.pdf')
        partial['rows'][0]['autonomous_answer']={'origin':'llm','layout':{'fit_status':'overflow','compression_attempts':2}}
        note=prepare_report(partial)['rows'][0]['review_note']
        self.assertIn('保留成功填写的位置',note);self.assertNotIn('已留空',note)

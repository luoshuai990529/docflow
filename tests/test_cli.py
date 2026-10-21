import tempfile,unittest,json,subprocess,sys,os
from pathlib import Path
from tests.helpers import pdf,docx,template,sha
class CLITests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.r=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def cli(self,*args):return subprocess.run([sys.executable,'-m','autofill',*map(str,args)],capture_output=True,text=True,env={**os.environ,'PYTHONPATH':'server/src'})
 def prepare(self,p,t):
  source=pdf(self.r/'source.pdf');tp=self.r/'t.json';tp.write_text(json.dumps(t));b=self.r/'bundle';r=self.cli('prepare','--questionnaire',p,'--source',source,'--template',tp,'--out',b);self.assertEqual(r.returncode,0,r.stderr);return b,source
 def test_cli_real_agent_json_pdf(self):
  p=pdf(self.r/'q.pdf');before=sha(p);b,source=self.prepare(p,template(p));a=b/'answers.json';a.write_text(json.dumps({'origin':'agent','questionnaire_sha256':sha(p),'source_sha256':sha(source),'decisions':[{'question_id':'company','status':'ready','value':'SAMPLE CO','option_ids':[],'span_ids':['s1'],'quote':'Company: SAMPLE CO','reason':'explicit'}]}));out=self.r/'out';r=self.cli('apply','--bundle',b,'--answers',a,'--mode','real','--out',out);self.assertEqual(r.returncode,0,r.stderr);self.assertTrue((out/'filled.pdf').exists());self.assertEqual(sha(p),before);self.assertEqual(json.loads((out/'report.json').read_text())['counts'],{'written':1})
 def test_cli_mock_docx_test_label(self):
  p=docx(self.r/'q.docx');target={'id':'company','locator':{'type':'sdt','tag':'note'},'constraints':{'test_label':True}};b,_=self.prepare(p,template(p,[target]));a=self.r/'TEST-SAMPLE-answers.json';r=self.cli('mock-answers','--bundle',b,'--mode','fictional_test','--out',a);self.assertEqual(r.returncode,0,r.stderr);out=self.r/'TEST-SAMPLE-out';r=self.cli('apply','--bundle',b,'--answers',a,'--mode','fictional_test','--out',out);self.assertEqual(r.returncode,2,r.stderr);self.assertTrue((out/'TEST-SAMPLE-filled.docx').exists());self.assertEqual(json.loads((out/'report.json').read_text())['verification']['layout_check'],'not_run')
 def test_cli_unknown_template_and_doc_error(self):
  p=pdf(self.r/'q.pdf');source=pdf(self.r/'source.pdf');b=self.r/'bundle';r=self.cli('prepare','--questionnaire',p,'--source',source,'--out',b);self.assertEqual(r.returncode,0);a=self.r/'a.json';a.write_text('{}');r=self.cli('apply','--bundle',b,'--answers',a,'--mode','real','--out',self.r/'out');self.assertEqual(r.returncode,3);self.assertIn('UNSUPPORTED_TEMPLATE',r.stderr)
  p=self.r/'q.doc';p.write_bytes(b'old');r=self.cli('prepare','--questionnaire',p,'--source',source,'--out',self.r/'another');self.assertEqual(r.returncode,3)
 def test_cli_prepare_export(self):
  p=pdf(self.r/'q.pdf');b,_=self.prepare(p,template(p));self.assertTrue((b/'source.json').exists());self.assertFalse((b/'filled.pdf').exists());r=self.cli('apply','--bundle',b,'--answers',self.r/'missing.json','--mode','real','--out',self.r/'out');self.assertEqual(r.returncode,3);self.assertIn('MATCH_INPUT_REQUIRED',r.stderr)
 def test_source_changed_and_real_mock_rejected(self):
  p=pdf(self.r/'q.pdf');b,source=self.prepare(p,template(p));a=self.r/'TEST-SAMPLE.json';self.cli('mock-answers','--bundle',b,'--mode','fictional_test','--out',a);r=self.cli('apply','--bundle',b,'--answers',a,'--mode','real','--out',self.r/'out');self.assertIn('MOCK_IN_REAL',r.stderr);self.assertEqual(r.returncode,1)
  source.write_bytes(b'changed');r=self.cli('apply','--bundle',b,'--answers',a,'--mode','fictional_test','--out',self.r/'TEST-SAMPLE-out');self.assertIn('SOURCE_CHANGED',r.stderr)

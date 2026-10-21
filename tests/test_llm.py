import unittest,json,tempfile,threading,subprocess,sys,os,socket
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from http.server import HTTPServer,BaseHTTPRequestHandler
from io import BytesIO
from autofill.config import preset_config,LLMConfig,init_config
from autofill.adapters.models.openai_chat import OpenAIChatClient
from autofill.application.answering.match import LLMMatcher,validate_decisions
from autofill.adapters.documents.extract import extract_source,extract_questionnaire
from tests.helpers import pdf,docx,template,sha

def decision(): return {'question_id':'company','status':'ready','value':'SAMPLE CO','option_ids':[],'span_ids':['s1'],'quote':'Company: SAMPLE CO','reason':'explicit'}
def envelope(content,finish='stop'): return json.dumps({'id':'test-request','choices':[{'finish_reason':finish,'message':{'content':content}}]}).encode()
class Tests(unittest.TestCase):
 def test_presets_overrides_and_no_secrets(self):
  self.assertEqual(preset_config('deepseek').model,'deepseek-v4-pro')
  self.assertEqual(preset_config('typesafe').base_url,'https://api.typesafe.ai/v1')
  self.assertEqual(preset_config('typesafe').protocol,'typesafe_systemone')
  self.assertEqual(preset_config('deepseek',model='custom').model,'custom')
  with tempfile.TemporaryDirectory() as t:
   p=init_config('deepseek',Path(t)/'config.json');self.assertNotIn('api_key',json.loads(p.read_text()));self.assertEqual((p.parent/'.env.example').read_text(),'DEEPSEEK_API_KEY=\n')
   with self.assertRaisesRegex(ValueError,'OUTPUT_EXISTS'):init_config('deepseek',p)
 def test_transport_modes_and_bad_json(self):
  for mode in ('prompt_only','json_object','json_schema'):
   captured=[]
   def transport(r,timeout): captured.append(json.loads(r.data));return BytesIO(envelope('{"decisions":[]}'))
   with patch.dict(os.environ,{'TEST_LLM_KEY':'dummy'}):
    c=LLMConfig('test','http://localhost:1','test','TEST_LLM_KEY',output_mode=mode)
    self.assertEqual(OpenAIChatClient(c,transport).generate_json('JSON',{}).data,{'decisions':[]})
   self.assertEqual('response_format' in captured[0],mode!='prompt_only')
  for raw in (b'bad',envelope('prefix ```json\n{}\n```'),envelope('{}','length')):
   with patch.dict(os.environ,{'TEST_LLM_KEY':'dummy'}),self.assertRaises(ValueError):OpenAIChatClient(c,lambda *a,**k:BytesIO(raw)).generate_json('JSON',{})
 def test_json_wrappers_and_safe_format_diagnostics(self):
  from autofill.adapters.models.openai_chat import LLMFailure
  c=LLMConfig('test','http://localhost:1','test','UNUSED')
  for content in ['{"decisions":[]}','```json\n{"decisions":[]}\n```','```\n{"decisions":[]}\n```']:
   with self.subTest(content=content):
    client=OpenAIChatClient(c,lambda *a,**k:BytesIO(envelope(content)),api_key='FAKE-ONLY')
    self.assertEqual(client.generate_json('JSON',{}).data,{'decisions':[]})
  for content in ['',None,'{"decisions":','PRIVATE_RAW_CONTENT','[]','prefix {"decisions":[]}','{} {}']:
   with self.subTest(content=content):
    client=OpenAIChatClient(c,lambda *a,**k:BytesIO(envelope(content)),api_key='FAKE-ONLY')
    with self.assertRaises(LLMFailure) as caught:client.generate_json('JSON',{})
    self.assertEqual(caught.exception.diagnostics['stage'],'answer')
    self.assertNotIn('PRIVATE',json.dumps(caught.exception.diagnostics))
  client=OpenAIChatClient(c,lambda *a,**k:BytesIO(b'PRIVATE_GATEWAY_ERROR'),api_key='FAKE-ONLY')
  with self.assertRaises(LLMFailure) as caught:client.generate_json('JSON',{})
  self.assertEqual(caught.exception.diagnostics['stage'],'response')
  refused=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':None,'refusal':'PRIVATE_REFUSAL_TEXT'}}]}).encode()
  with self.assertRaises(LLMFailure) as caught:OpenAIChatClient(c,lambda *a,**k:BytesIO(refused),api_key='FAKE-ONLY').generate_json('JSON',{})
  self.assertEqual(str(caught.exception),'LLM_CONTENT_FILTER');self.assertEqual(caught.exception.diagnostics['reason'],'refusal')
  self.assertNotIn('PRIVATE',json.dumps(caught.exception.diagnostics))
 def test_response_contract_and_unsupported_mode_diagnostics(self):
  from autofill.adapters.models.openai_chat import LLMFailure
  schema={'type':'object','additionalProperties':False,'required':['answer'],'properties':{'answer':{'type':'string'}}}
  captured=[]
  def transport(request,timeout):captured.append(json.loads(request.data));return BytesIO(envelope('{"answer":"yes"}'))
  config=LLMConfig('test','http://localhost:1','test','UNUSED',output_mode='json_schema')
  client=OpenAIChatClient(config,transport,api_key='FAKE-ONLY')
  self.assertEqual(client.generate_json('JSON',schema).data,{'answer':'yes'})
  self.assertEqual(captured[0]['response_format'],{'type':'json_schema','json_schema':{'name':'answers','strict':True,'schema':schema}})
  def unsupported(request,timeout):
   raise HTTPError('redacted',400,'bad',{},BytesIO(b'{"error":{"param":"response_format","message":"json_schema unsupported SECRET"}}'))
  with self.assertRaises(LLMFailure) as caught:OpenAIChatClient(config,unsupported,api_key='FAKE-ONLY').generate_json('JSON',schema)
  self.assertEqual(str(caught.exception),'LLM_OUTPUT_MODE_UNSUPPORTED')
  self.assertEqual(caught.exception.diagnostics['output_mode'],'json_schema')
  self.assertNotIn('SECRET',json.dumps(caught.exception.diagnostics))
  # Even a schema provider may violate its contract. Leave individual answers
  # available to the matcher, which validates and repairs only failing items.
  result=OpenAIChatClient(config,lambda *a,**k:BytesIO(envelope('{"answer":3}')),api_key='FAKE-ONLY').generate_json('JSON',schema)
  self.assertEqual(result.data,{'answer':3});self.assertEqual(result.metadata['output_mode'],'json_schema')
  self.assertEqual(len(result.metadata['response_sha256']),64);self.assertEqual(len(result.metadata['schema_sha256']),64)
  self.assertNotIn('answer',json.dumps(result.metadata))
 def test_errors(self):
  c=LLMConfig('test','http://localhost:1','test','TEST_LLM_KEY')
  with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(ValueError,'LLM_MISSING_KEY'):OpenAIChatClient(c).generate_json('JSON',{})
  def timeout(*a,**k):raise socket.timeout()
  def auth(*a,**k):raise HTTPError('redacted',401,'bad',{},None)
  for fn,code in ((timeout,'LLM_TIMEOUT'),(auth,'LLM_AUTH')):
   with patch.dict(os.environ,{'TEST_LLM_KEY':'dummy'}),self.assertRaisesRegex(ValueError,code):OpenAIChatClient(c,fn).generate_json('JSON',{})
 def test_evidence_and_schema(self):
  with tempfile.TemporaryDirectory() as t:
   p=pdf(Path(t)/'q.pdf');q=extract_questionnaire(p,template(p));s=extract_source(p)
   class Client:
    def generate_json(self,prompt,schema):
     from autofill.adapters.models.openai_chat import LLMResponse
     return LLMResponse({'decisions':[decision()]})
   ds=LLMMatcher(Client()).match(q.questions,s.spans);self.assertEqual(validate_decisions(ds,q.questions,s.spans,'real')[0].status,'ready')
   ds[0].quote='invented'
   with self.assertRaisesRegex(ValueError,'QUOTE_MISMATCH'):validate_decisions(ds,q.questions,s.spans,'real')
 def test_secret_header_and_unsupported_text(self):
  c=LLMConfig('test','http://localhost:1','test','TEST_LLM_KEY')
  with patch.dict(os.environ,{'TEST_LLM_KEY':'FAKE_SECRET\n'}),self.assertRaisesRegex(ValueError,'^LLM_INVALID_KEY_FORMAT$'):OpenAIChatClient(c).generate_json('JSON',{})
  class Client:
   def generate_json(self,prompt,schema):
    from autofill.adapters.models.openai_chat import LLMResponse
    d=decision();d['value']='INVENTED INC';return LLMResponse({'decisions':[d]})
  self.assertEqual(LLMMatcher(Client()).match([],[])[0].status,'needs_review')
 def test_standalone_cli_http_pdf_docx(self):
  requests=[]
  class Handler(BaseHTTPRequestHandler):
   def do_POST(self):
    requests.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
    self.send_response(200);self.end_headers();self.wfile.write(envelope(json.dumps({'decisions':[decision()]})))
   def log_message(self,*a):pass
  server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   with tempfile.TemporaryDirectory() as t:
    root=Path(t);source=pdf(root/'source.pdf')
    for kind in ('pdf','docx'):
     p=pdf(root/'q.pdf') if kind=='pdf' else docx(root/'q.docx');before=sha(p)
     targets=None if kind=='pdf' else [{'id':'company','locator':{'type':'sdt','tag':'note'},'constraints':{}}]
     tp=root/(kind+'.json');tp.write_text(json.dumps(template(p,targets)))
     config=root/'config.json';config.write_text(json.dumps({'provider':'deepseek','base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'fake','credential_ref':'TEST_LLM_KEY'}))
     out=root/('out-'+kind)
     r=subprocess.run([sys.executable,'-m','autofill','run','--questionnaire',str(p),'--source',str(source),'--template',str(tp),'--config',str(config),'--out',str(out)],capture_output=True,text=True,env={**os.environ,'TEST_LLM_KEY':'dummy','PYTHONPATH':'server/src'})
     self.assertEqual(r.returncode,0 if kind=='pdf' else 2,r.stderr);self.assertTrue((out/('filled.'+kind)).exists());self.assertEqual(sha(p),before)
     report=json.loads((out/'report.json').read_text());self.assertEqual(report['origin'],'llm');self.assertEqual(report['engine']['model'],'fake')
    self.assertEqual(len(requests),2);self.assertEqual(requests[0][0],'/v1/chat/completions')
    self.assertTrue(all(body['response_format']=={'type':'json_object'} for _,body in requests))
  finally:server.shutdown();server.server_close();thread.join()

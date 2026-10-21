import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autofill.config import LLMConfig, preset_config
from autofill.models import Decision, Question
from autofill.adapters.models.openai_chat import LLMResponse
from autofill.domain.decisions import validate_decisions


class FakeModel:
 def __init__(self, invalid=False): self.calls=[];self.invalid=invalid
 def generate_json(self,prompt,schema):
  body=json.loads(prompt.split('\n',1)[1]);self.calls.append(body)
  if body['phase']=='autonomous_context':
   return LLMResponse({'scenario':'统一采用示例物业企业，50 名员工。','assumptions':'企业数据为模型拟定。'})
  answers=[]
  for q in body['questions']:
   answers.append({'question_id':q['id'],'value':'示例物业有 50 名员工。' if q['kind']=='text' else None,
    'option_ids':[] if q['kind']=='text' else [q['options'][0]],
    'assumptions':'采用示例企业。','reason':'模型自主作答。'})
  if self.invalid and len(self.calls)==2:answers[-1]['option_ids']=['unknown']
  return LLMResponse({'answers':answers},metadata={'output_mode':'json_object'})


class FakeJev:
 def __init__(self, invalid=False):self.calls=[];self.invalid=invalid
 def evaluate(self,state,questions):
  self.calls.append((state,questions))
  if self.invalid:return LLMResponse({'answers':{}})
  answers={}
  for identity,q in questions.items():
   if q['type']=='score':
    score=3 if q['instructions']['option']=='B' else 2 if q['instructions']['option']=='C' else 1
    answers[identity]={'type':'score','score':score}
   else:
    options=list(q['criteria']);choice='2' if '2' in options else options[-1]
    answers[identity]={'type':'choice','choice':choice,'probabilities':{o:0.8 if o==choice else 0.1 for o in options}}
  return LLMResponse({'answers':answers})


class AutonomousAnswerTests(unittest.TestCase):
 def questions(self):
  return [Question('q1','企业人数','text',[],['t1']),Question('q2','单选','single_choice',['A','B'],['t2']),
   Question('q3','选择两项服务','multiple_choice',['A','B','C'],['t3'])]
 def test_autonomous_has_no_citations_but_preserves_choice_and_grounded_contracts(self):
  q=self.questions()[0];answer=Decision('q1','ready','示例企业 50 人。',[],[],None,'模型拟定。')
  self.assertEqual(validate_decisions([answer],[q],[],'real',answer_mode='autonomous'),[answer])
  with self.assertRaisesRegex(ValueError,'EVIDENCE_ID'):validate_decisions([answer],[q],[],'real')
  blank=Decision('q1','ready',' \n\t ',[],[],None,'模型拟定。')
  with self.assertRaisesRegex(ValueError,'TEXT_VALUE'):validate_decisions([blank],[q],[],'real',answer_mode='autonomous')
  choice=Decision('q2','ready',None,['foreign'],[],None,'模型拟定。')
  with self.assertRaisesRegex(ValueError,'CHOICE_VALUE'):
   validate_decisions([choice],[self.questions()[1]],[],'real',answer_mode='autonomous')
 def test_jev_ranks_multiselect_and_shares_scenario_with_text(self):
  from autofill.application.answering.autonomous import generate_autonomous
  llm=FakeModel();jev=FakeJev();coverage={}
  result=generate_autonomous(self.questions(),llm,jev,coverage=coverage)
  self.assertEqual([d.status for d in result],['ready']*3)
  self.assertEqual(result[1].option_ids,['B']);self.assertEqual(result[2].option_ids,['B','C'])
  self.assertTrue(all('50' in json.dumps(state,ensure_ascii=False) for state,_ in jev.calls))
  self.assertEqual(coverage['autonomous_answers']['q1']['origin'],'llm')
  self.assertEqual(coverage['autonomous_answers']['q3']['origin'],'jev')
  self.assertEqual(coverage['autonomous_answers']['q3']['selected_count'],2)
  self.assertEqual(result[2].span_ids,[])
  self.assertEqual(len(jev.calls),1)
  class PartialJev(FakeJev):
   def evaluate(self,state,questions):
    response=super().evaluate(state,questions)
    if len(self.calls)==1:response.data['answers'].pop('q0_selection')
    return response
  partial=PartialJev();coverage={}
  answers=generate_autonomous(self.questions(),FakeModel(),partial,coverage=coverage)
  self.assertTrue(all(d.status=='ready' for d in answers));self.assertEqual(len(partial.calls),2)
  self.assertEqual(set(partial.calls[1][1]),{'q0_selection'})
  self.assertEqual(len(coverage['autonomous_attempts']['q3']),1)
 def test_only_failed_rows_are_corrected_and_jev_failure_falls_back(self):
  from autofill.application.answering.autonomous import generate_autonomous
  llm=FakeModel(invalid=True);coverage={}
  answers=generate_autonomous(self.questions(),llm,coverage=coverage)
  self.assertEqual([q['id'] for q in llm.calls[-1]['questions']],['q3'])
  self.assertTrue(all(d.status=='ready' for d in answers))
  llm=FakeModel();jev=FakeJev(invalid=True);coverage={}
  answers=generate_autonomous(self.questions(),llm,jev,coverage=coverage)
  self.assertTrue(all(d.status=='ready' for d in answers))
  self.assertEqual(coverage['autonomous_answers']['q2']['origin'],'llm')
  self.assertEqual(coverage['autonomous_answers']['q2']['fallback_code'],'JEV_BAD_RESPONSE')
  self.assertEqual(len(jev.calls),4) # One initial batch plus three corrections.
  self.assertTrue(coverage['validation_events'])
  class UnavailableJev:
   def evaluate(self,*args):raise ValueError('JEV_TIMEOUT: fake-private-error-body')
  coverage={};phases=[]
  answers=generate_autonomous(self.questions(),FakeModel(),UnavailableJev(),coverage=coverage,progress=lambda node,**values:phases.append(values['phase']))
  self.assertTrue(all(d.status=='ready' for d in answers));self.assertIn('fallback',phases)
  self.assertEqual(coverage['autonomous_answers']['q2']['fallback_code'],'JEV_TIMEOUT')
  self.assertNotIn('fake-private-error-body',json.dumps(coverage))
  for malformed in ([],{},10**1000):
   with self.subTest(choice_type=type(malformed).__name__):
    class MalformedChoiceJev(FakeJev):
     def evaluate(self,state,questions):
      response=super().evaluate(state,questions)
      for answer in response.data['answers'].values():
       if isinstance(malformed,int):
        if answer.get('type')=='score':answer['score']=malformed
        elif answer.get('type')=='choice' and '0' in answer['probabilities']:answer['probabilities']['0']=malformed
       elif answer.get('type')=='choice':answer['choice']=malformed
      return response
    coverage={};jev=MalformedChoiceJev()
    answers=generate_autonomous(self.questions(),FakeModel(),jev,coverage=coverage)
    self.assertTrue(all(d.status=='ready' for d in answers));self.assertEqual(len(jev.calls),4)
    for identity in ('q2','q3'):
     self.assertEqual(coverage['autonomous_answers'][identity]['fallback_code'],'JEV_BAD_RESPONSE')
 def test_jev_official_uses_systemone_wire_protocol(self):
  from autofill.adapters.models.jev_client import JevClient
  captured=[]
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    captured.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
    body=json.dumps({'model':'jev-test','answers':{'test':{'type':'choice','choice':'B','probabilities':{'A':0.1,'B':0.9}}}}).encode()
    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   for provider,suffix,expected in [('typesafe','/v1','https://api.typesafe.ai/v1')]:
    config=preset_config(provider)
    self.assertEqual(config.base_url,expected);self.assertEqual(config.protocol,'typesafe_systemone')
    config.base_url=f'http://127.0.0.1:{server.server_port}'+suffix
    result=JevClient(config,api_key='fake-local-key').evaluate({'question':'示例'}, {'test':{'type':'choice','instructions':'选择','criteria':{'A':None,'B':None}}})
    self.assertEqual(result.data['answers']['test']['choice'],'B')
    self.assertEqual(captured[-1][0],suffix+'/systemone')
    self.assertEqual(set(captured[-1][1]),{'state','model','questions'})
    self.assertNotIn('fake-local-key',json.dumps(result.metadata))
    config.max_input_chars=10
    with self.assertRaisesRegex(ValueError,'JEV_INPUT_TOO_LARGE'):
     JevClient(config,api_key='fake-local-key').evaluate('context',{})
  finally:server.shutdown();server.server_close();thread.join()

 def test_layout_hints_reach_generation_without_rejecting_estimates(self):
  from autofill.application.answering.autonomous import generate_autonomous
  q=self.questions()[0];llm=FakeModel()
  constraints={'q1':{'suggested_max_chars':3,'max_lines':1,'single_line':True,'native_max_length':20}}
  answers=generate_autonomous([q],llm,answer_constraints=constraints)
  self.assertEqual(answers[0].value,'示例物业有 50 名员工。')
  body=llm.calls[1]
  self.assertEqual(body['answer_constraints'],constraints)
  self.assertEqual(body['schema']['properties']['answers']['items']['anyOf'][0]['properties']['value']['maxLength'],20)
  constraints['q1']['native_max_length']=4
  class NativeLimitModel(FakeModel):
   def generate_json(self,prompt,schema):
    response=super().generate_json(prompt,schema)
    if self.calls[-1]['phase']=='autonomous_answers_correction':response.data['answers'][0]['value']='50人'
    return response
  llm=NativeLimitModel();answers=generate_autonomous([q],llm,answer_constraints=constraints)
  self.assertEqual(answers[0].value,'50人')
  self.assertEqual(llm.calls[-1]['validation_errors'],[{'question_id':'q1','code':'ANSWER_TOO_LONG'}])

 def test_compaction_only_replaces_failed_text_and_keeps_scenario_and_choices(self):
  import autofill.application.answering.autonomous as autonomous
  self.assertTrue(hasattr(autonomous,'compact_answers'))
  questions=self.questions()+[Question('q4','服务安排','text',[],['t4']),Question('q5','其他说明','text',[],['t5'])]
  decisions=[Decision('q1','ready','有 50 名员工，每月培训两次。',[],[],None,'拟定'),
   Decision('q2','ready',None,['B'],[],None,'Jev'),Decision('q3','ready',None,['B','C'],[],None,'Jev'),
   Decision('q4','ready','全年正常提供所有服务。',[],[],None,'拟定'),
   Decision('q5','ready','无需其他说明。',[],[],None,'拟定')]
  class CompactModel(FakeModel):
   def generate_json(self,prompt,schema):
    body=json.loads(prompt.split('\n',1)[1]);self.calls.append(body)
    return LLMResponse({'answers':[{'question_id':identity,'value':value,'option_ids':[],
     'assumptions':'维持原企业背景。','reason':'仅压缩表达。'}
     for identity,value in [('q1','50人；每月培训2次'),('q4','全年服务')]]})
  llm=CompactModel();coverage={'autonomous_context':{'scenario':'50 人企业','assumptions':'示例企业。'},
   'autonomous_answers':{'q1':{'origin':'llm','assumptions':'示例企业。'}}}
  constraints={'q1':{'suggested_max_chars':14,'max_lines':1,'single_line':True}}
  failures=[{'question_id':'q1','target_id':'t1','code':'WRITE_OVERFLOW'},
   {'question_id':'q4','target_id':'t4','code':'WRITE_OVERFLOW'},
   {'question_id':'q2','target_id':'t2','code':'WRITE_OVERFLOW'}]
  result=autonomous.compact_answers(questions,decisions,llm,constraints,failures,coverage=coverage)
  self.assertEqual(result[0].value,'50人；每月培训2次')
  self.assertEqual(result[1:3],decisions[1:3]);self.assertEqual(result[4],decisions[4])
  self.assertEqual(result[3].value,'全年服务');self.assertEqual(len(llm.calls),1)
  body=llm.calls[0]
  self.assertEqual(body['phase'],'answer_compaction');self.assertEqual(body['shared_scenario']['scenario'],'50 人企业')
  self.assertEqual([q['id'] for q in body['questions']],['q1','q4'])
  self.assertEqual(body['previous_answers'][0]['value'],decisions[0].value)
  self.assertEqual(body['answer_constraints'],constraints)
  layout=coverage['autonomous_answers']['q1']['layout']
  self.assertEqual(layout['original_chars'],len(decisions[0].value));self.assertEqual(layout['compression_attempts'],1)
  self.assertEqual(layout['final_chars'],len(result[0].value))

 def test_compaction_failure_preserves_candidate_and_cancellation_propagates(self):
  import autofill.application.answering.autonomous as autonomous
  self.assertTrue(hasattr(autonomous,'compact_answers'))
  questions=[self.questions()[0]];decisions=[Decision('q1','ready','原答案仍由排版检查判断。',[],[],None,'拟定')]
  class FailingModel:
   def generate_json(self,*args):raise ValueError('LLM_TIMEOUT: private-provider-response')
  coverage={};failures={'q1':[{'target_id':'t1','code':'WRITE_OVERFLOW'}]}
  result=autonomous.compact_answers(questions,decisions,FailingModel(),{},failures,coverage=coverage)
  self.assertEqual(result,decisions);self.assertNotIn('private-provider-response',json.dumps(coverage))
  self.assertEqual(coverage['autonomous_answers']['q1']['layout']['attempts'][0]['code'],'LLM_TIMEOUT')
  class MalformedModel:
   def generate_json(self,*args):return LLMResponse({'answers':[{'question_id':'q1','value':'rejected-private-value'}]})
  coverage={}
  result=autonomous.compact_answers(questions,decisions,MalformedModel(),{},failures,coverage=coverage)
  self.assertEqual(result,decisions);self.assertNotIn('rejected-private-value',json.dumps(coverage))
  self.assertEqual(coverage['autonomous_answers']['q1']['layout']['attempts'][0]['code'],'ANSWER_SCHEMA')
  with self.assertRaisesRegex(ValueError,'TASK_CANCELLED'):
   autonomous.compact_answers(questions,decisions,FailingModel(),{},failures,cancelled=lambda:True)

"""Bounded local corrections repair contracts without bypassing evidence checks."""
import json, unittest
from dataclasses import asdict
from autofill.application.answering.match import match_bounded
from autofill.models import Decision, Question, SourceSpan
from autofill.adapters.models.openai_chat import LLMResponse, LLMFailure

class CorrectionTests(unittest.TestCase):
 def test_supported_summary_and_halving_reaches_ready_after_evidence_review(self):
  question=Question('q10','满1年普通企业和专精特新企业提前退租违约金？','text',[],['t10'])
  source=SourceSpan('s1',{},'租赁周期满1年申请提前退租：收取1个月标准租金作为违约金。经工信局认定的专精特新企业，违约金统一减半收取。','hash')
  value='满1年退租：普通企业违约金为1个月标准租金；经工信局认定的专精特新企业为0.5个月标准租金。'
  calls=[]
  class Model:
   def generate_json(self,prompt,schema):
    data=json.loads(prompt.split('\n',1)[1]);calls.append(data['phase'])
    if data['phase']=='answer_support_review':
     return LLMResponse({'reviews':[{'question_id':'q10','verdict':'supported','transformations':['paraphrase','calculation'],'reason':'完整保留期限和认定条件，减半计算正确。','calculations':[{'operation':'multiply','operands':[{'value':'1','span_id':'s1','quote':'1个月标准租金','interpretation':'literal'},{'value':'0.5','span_id':'s1','quote':'经工信局认定的专精特新企业，违约金统一减半收取。','interpretation':'half'}],'result':'0.5','unit':'个月'}]}]})
    return LLMResponse({'decisions':[{'question_id':'q10','status':'ready','value':value,'option_ids':[],'evidence':[{'span_id':'s1','quote':source.text}],'reason':'据原文整理'}]})
  coverage={};decisions,_=match_bounded(Model(),[question],[source],coverage=coverage)
  self.assertEqual(decisions[0].status,'ready');self.assertEqual(decisions[0].value,value)
  self.assertEqual(calls,['answer_matching','answer_support_review'])
  self.assertEqual(coverage['incomplete_questions'],[])
  self.assertEqual(coverage['support_reviews']['q10']['verdict'],'supported')

 def test_only_invalid_questions_are_corrected_once_with_separate_evidence(self):
  questions=[Question('q4','Commission','multiple_choice',['A','B'],['t4']),Question('q5','Certificate required?','boolean',['√','×'],['t5']),Question('q10','Both response times','text',[],['t10'])]
  spans=[SourceSpan('s1',{},'Commission A and B. Certificate required.','hash'),SourceSpan('s2',{},'Outage: 30 minutes','hash'),SourceSpan('s3',{},'Leak: 1 hour','hash')]
  calls=[]
  class Model:
   def generate_json(self,prompt,schema):
    data=json.loads(prompt.split('\n',1)[1]);calls.append(data)
    if data.get('phase')=='answer_support_review':
     return LLMResponse({'reviews':[dict(question_id=item['question']['id'],verdict='supported',transformations=[],reason='所有选项和两项时限均有对应原文。',calculations=[]) for item in data['questions']]})
    if data.get('phase')=='answer_correction':
     return LLMResponse({'decisions':[dict(question_id='q5',status='ready',value=None,option_ids=['√'],evidence=[dict(span_id='s1',quote='Certificate required.')],reason='Exact source'),dict(question_id='q10',status='ready',value='Outage: 30 minutes\nLeak: 1 hour',option_ids=[],evidence=[dict(span_id='s2',quote='Outage: 30 minutes'),dict(span_id='s3',quote='Leak: 1 hour')],reason='Both sources')]})
    return LLMResponse({'decisions':[asdict(Decision('q4','ready',None,['A','B'],['s1'],'Commission A and B.','')),asdict(Decision('q5','ready',None,['false'],['s1'],'Certificate required.','')),asdict(Decision('q10','ready','Outage: 30 minutes\nLeak: 1 hour',[],['s2','s3'],'Outage: 30 minutes\nLeak: 1 hour',''))]})
  coverage={};ds,_=match_bounded(Model(),questions,spans,coverage=coverage)
  self.assertEqual([d.status for d in ds],['ready']*3)
  self.assertEqual([call['phase'] for call in calls],['answer_matching','answer_correction','answer_support_review'])
  self.assertEqual([q['id'] for q in calls[1]['questions']],['q5','q10'])
  self.assertEqual(ds[2].span_ids,['s2','s3']);self.assertEqual(ds[2].quote,'Outage: 30 minutes')
  self.assertEqual(ds[1].option_ids,['√'])
  self.assertEqual(coverage['incomplete_questions'],[])
  events=[e for e in coverage['validation_events'] if not e.get('review_stage')];self.assertEqual({e['question_id'] for e in events},{'q5','q10'})
  self.assertTrue(all(e['outcome']=='resolved' for e in events))
  self.assertNotIn('Certificate required.',json.dumps(events))

 def test_three_corrections_are_bounded_and_transport_failures_stop_early(self):
  questions=[Question('q1','Company','text',[],['t1']),Question('q2','Choice','boolean',['√','×'],['t2'])]
  spans=[SourceSpan('s1',{},'Acme','hash')]
  for retry in ('quote','transport'):
   calls=[]
   class Model:
    def generate_json(self,prompt,schema):
     data=json.loads(prompt.split('\n',1)[1]);calls.append(data)
     if data.get('phase')=='answer_support_review':
      return LLMResponse({'reviews':[dict(question_id=item['question']['id'],verdict='supported',transformations=[],reason='公司名称为原文 Acme。',calculations=[]) for item in data['questions']]})
     if data.get('phase')=='answer_correction' and retry=='transport':raise ValueError('LLM_TIMEOUT')
     return LLMResponse({'decisions':[asdict(Decision(q['id'],'ready','Acme' if q['id']=='q1' else None,[] if q['id']=='q1' else ['×'],['s1'],'Acme' if q['id']=='q1' else 'PRIVATE_BAD_QUOTE','')) for q in data['questions']]})
   coverage={};ds,_=match_bounded(Model(),questions,spans,coverage=coverage)
   self.assertEqual([d.status for d in ds],['ready','needs_review'])
   correction_count=3 if retry=='quote' else 1
   self.assertEqual([call['phase'] for call in calls],['answer_matching']+['answer_correction']*correction_count+['answer_support_review']);self.assertIn('引用',ds[1].reason)
   self.assertEqual(coverage['incomplete_questions'],['q2'])
   events=coverage['validation_events'];self.assertEqual(events[0]['code'],'QUOTE_MISMATCH')
   self.assertEqual(events[0]['outcome'],'unresolved');self.assertNotIn('PRIVATE',json.dumps(events))
   self.assertEqual(len(events[0]['attempts']),correction_count+1)
   self.assertTrue(all([q['id'] for q in call['questions']]==['q2'] for call in calls if call['phase']=='answer_correction'))

 def test_changed_contract_failures_are_retried_only_for_unresolved_rows(self):
  questions=[Question('q5','Certificate required?','boolean',['√','×'],['t5']),Question('q6','Missing fact','text',[],['t6']),Question('q7','Ambiguous fact','text',[],['t7'])]
  spans=[SourceSpan('s1',{},'Certificate required.','hash')];calls=[];corrections=0
  class Model:
   def generate_json(self,prompt,schema):
    nonlocal corrections
    data=json.loads(prompt.split('\n',1)[1]);calls.append(data)
    if data['phase']=='answer_support_review':
     return LLMResponse({'reviews':[dict(question_id='q5',verdict='supported',transformations=[],reason='原文支持选项',calculations=[])]})
    answer=dict(question_id='q5',status='ready',value=None,option_ids=['×'],evidence=[dict(span_id='s1',quote='PRIVATE_WRONG_QUOTE')],reason='')
    if data['phase']=='answer_correction':
     corrections+=1
     if corrections==1:answer['reason']=None;answer['evidence'][0]['quote']=spans[0].text
     elif corrections==3:answer['evidence'][0]['quote']=spans[0].text
     return LLMResponse({'decisions':[answer]},metadata={'output_mode':'json_object','request_id':f'correction-{corrections}'})
    return LLMResponse({'decisions':[answer,dict(question_id='q6',status='not_found',value=None,option_ids=[],evidence=[],reason='无依据'),dict(question_id='q7',status='needs_review',value=None,option_ids=[],evidence=[],reason='有歧义')]})
  coverage={};ds,_=match_bounded(Model(),questions,spans,coverage=coverage)
  self.assertEqual([d.status for d in ds],['ready','not_found','needs_review'])
  self.assertEqual(corrections,3);self.assertEqual(coverage['incomplete_questions'],[])
  for call in calls:
   if call['phase']=='answer_correction':self.assertEqual([q['id'] for q in call['questions']],['q5'])
  event=next(e for e in coverage['validation_events'] if not e.get('review_stage'))
  self.assertEqual(event['outcome'],'resolved');self.assertEqual(event['attempt_count'],4)
  self.assertEqual([attempt['code'] for attempt in event['attempts']],['QUOTE_MISMATCH','DECISION_SCHEMA','QUOTE_MISMATCH','OK'])
  self.assertEqual(event['attempts'][1]['path'],'reason');self.assertEqual(event['attempts'][1]['check'],'expected_string')
  self.assertEqual(event['attempts'][1]['details'],{'actual_type':'null'})
  self.assertEqual(event['attempts'][1]['request_id'],'correction-1')
  self.assertNotIn('PRIVATE',json.dumps(event));self.assertNotIn('Certificate required.',json.dumps(event))
  # A cancellation while either HTTP request is in flight must stop before
  # accepting its answer or starting another correction/support request.
  from autofill.application.answering.match import _answer_batch
  for phase in ('answer_matching','answer_correction'):
   with self.subTest(cancelled_after=phase):
    calls.clear();corrections=0;state={'cancelled':False}
    class CancelModel(Model):
     def generate_json(self,prompt,schema):
      response=super().generate_json(prompt,schema)
      if calls[-1]['phase']==phase:
       response.data['decisions'][0]['reason']=''
       response.data['decisions'][0]['evidence']=[dict(span_id='s1',quote=spans[0].text)]
       state['cancelled']=True
      return response
    with self.assertRaisesRegex(ValueError,'TASK_CANCELLED'):
     _answer_batch(CancelModel(),questions,spans,lambda:state['cancelled'])
    self.assertNotIn('answer_support_review',[call['phase'] for call in calls])
    self.assertEqual(len(calls),1 if phase=='answer_matching' else 2)

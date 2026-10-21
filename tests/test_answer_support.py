"""Semantic support is a run-local permission bound to actual source evidence."""
import copy,json,unittest
from dataclasses import replace
from autofill.models import Question,Decision,SourceSpan
from autofill.adapters.models.openai_chat import LLMResponse
from autofill.application.answering.answer_support import SupportLedger,review_answers


class FakeReviewer:
 def __init__(self,reviews):self.reviews=reviews;self.calls=[]
 def generate_json(self,prompt,schema):
  self.calls.append(json.loads(prompt.split('\n',1)[1]))
  if isinstance(self.reviews,Exception):raise self.reviews
  return LLMResponse({'reviews':self.reviews},metadata={'output_mode':'json_object'})


class AnswerSupportTests(unittest.TestCase):
 def setUp(self):
  self.q=Question('q10','说明专精特新企业满一年提前退租违约金，以及断电和漏水响应时限。','text',[],['t10'])
  self.s=SourceSpan('s1',{'source_name':'制度.docx','paragraph':1},'普通企业满1年退租，违约金为1个月标准租金。专精特新减半。断电30分钟，漏水1小时。','source-hash')
  self.d=Decision('q10','ready','专精特新企业违约金减半；断电应在30分钟内响应，漏水应在1小时内响应。',[],['s1'],'专精特新减半。','由资料整理')
 def verdict(self,**updates):
  return dict(question_id='q10',verdict='supported',transformations=['paraphrase'],reason='逐项对应原文，保留企业类型和满一年条件。',calculations=[])|updates
 def calculation(self):
  return dict(operation='multiply',operands=[dict(value='1',span_id='s1',quote='违约金为1个月标准租金',interpretation='literal'),dict(value='0.5',span_id='s1',quote='专精特新减半',interpretation='half')],result='0.5',unit='个月')
 def run_review(self,decision=None,review=None,spans=None):
  ledger=SupportLedger();client=FakeReviewer([review or self.verdict()]);decision=decision or self.d
  result=review_answers(client,[(self.q,decision)],spans or [self.s],ledger,lambda:False)
  return result['q10'],ledger,client
 def test_semantics_are_reviewed_without_numeric_or_keyword_guessing(self):
  from autofill.domain.decisions import requires_review
  value='专精特新企业满一年退租违约金为0.5个月；物业故障示例1：断电30分钟；示例2：漏水1小时。'
  d=replace(self.d,value=value)
  review={'question_id':'q10','verdict':'supported','reason':'减半换算及两项故障均有依据。'}
  result,ledger,client=self.run_review(d,review)
  self.assertTrue(result['accepted']);self.assertTrue(ledger.accepts(self.q,d,[self.s]))
  self.assertEqual(client.calls[0]['schema']['properties']['reviews']['items']['required'],['question_id','verdict','reason'])
  self.assertIn('source_markdown',client.calls[0])
  q=replace(self.q,text='承诺书由哪个部门保管？')
  self.assertFalse(requires_review(q,d,[self.s],check_text=False))
  calc=self.calculation();calc['operands'][1]['interpretation']='percent'
  result,ledger,_=self.run_review(d,self.verdict(transformations=[],calculations=[calc]))
  self.assertTrue(result['accepted']);self.assertEqual(ledger.records['q10']['calculations'][0]['operands'][1]['interpretation'],'half')
 def test_supported_paraphrase_and_translation_keep_original_evidence(self):
  for transformation,value in [('paraphrase',self.d.value),('translation','The early termination penalty is halved. Respond to power failure within 30 minutes and water leakage within 1 hour.')]:
   with self.subTest(transformation=transformation):
    d=replace(self.d,value=value);result,ledger,client=self.run_review(d,self.verdict(transformations=[transformation]))
    self.assertTrue(result['accepted']);self.assertTrue(ledger.accepts(self.q,d,[self.s]));self.assertEqual(len(client.calls),1)
    self.assertEqual(client.calls[0]['phase'],'answer_support_review')
    self.assertEqual(ledger.records['q10']['value'],value)
    self.assertEqual(ledger.records['q10']['source_ids'],['s1'])
    self.assertEqual(ledger.records['q10']['transformations'],[transformation])
 def test_supported_text_does_not_require_auxiliary_transformation_tags(self):
  q=Question('q9','完整说明审批流程、退款期限及账户要求。','text',[],['t9'])
  source=SourceSpan('s9',{},'租户申请→运营核验→财务核算→主管审批。审批完成后，保证金90个工作日内无息原路退还租户对公账户。','hash')
  values=['审批：\n1. 租户申请\n2. 运营核验\n3. 财务核算\n4. 主管审批\n退款：审批完成后，保证金90个工作日内无息原路退还租户对公账户。',
   '主管完成审批前，依次由租户申请、运营核验和财务核算。全部审批完成后，保证金于90个工作日内无息原路退回租户对公账户。',
   'Tenant applies; operations verifies; finance checks; supervisor approves. After approval, return the deposit without interest within 90 working days through the original route to the tenant corporate account.']
  for value in values:
   with self.subTest(value=value):
    d=Decision('q9','ready',value,[],['s9'],source.text,'有原文依据')
    ledger=SupportLedger();client=FakeReviewer([self.verdict(question_id='q9',transformations=[])])
    result=review_answers(client,[(q,d)],[source],ledger)
    self.assertTrue(result['q9']['accepted']);self.assertTrue(ledger.accepts(q,d,[source]))
    self.assertEqual(len(client.calls),1);self.assertEqual(ledger.records['q9']['transformations'],[])
  # Provenance is deterministic; wrong answer meaning is the reviewer's decision.
  d=Decision('q9','ready',values[0],[],['s9'],source.text,'有原文依据')
  for invalid in (replace(d,quote='原文中不存在的引文'),replace(d,span_ids=['unknown'])):
   with self.subTest(invalid=invalid):
    ledger=SupportLedger();client=FakeReviewer([self.verdict(question_id='q9',transformations=[])])
    result=review_answers(client,[(q,invalid)],[source],ledger)
    self.assertFalse(result['q9']['accepted']);self.assertFalse(ledger.accepts(q,invalid,[source]))
  for review in (self.verdict(transformations=[],calculations=[self.calculation()]),self.verdict(transformations=['calculation'],calculations=[])):
   result,ledger,_=self.run_review(review=review);self.assertTrue(result['accepted'])
 def test_half_and_percent_calculation_are_sourced_and_recomputed(self):
  d=replace(self.d,value='专精特新企业满一年提前退租，违约金为0.5个月标准租金；断电30分钟内响应，漏水1小时内响应。')
  for interpretation,quote,text in [('half','专精特新减半',self.s.text),('percent','专精特新按50%收取',self.s.text.replace('专精特新减半','专精特新按50%收取'))]:
   calc=self.calculation();calc['operands'][1].update(interpretation=interpretation,quote=quote)
   s=replace(self.s,text=text);candidate=replace(d,quote=quote)
   result,ledger,_=self.run_review(candidate,self.verdict(transformations=['paraphrase','calculation'],calculations=[calc]),[s])
   self.assertTrue(result['accepted']);self.assertTrue(ledger.accepts(self.q,candidate,[s]))
   self.assertEqual(ledger.records['q10']['calculations'][0]['result'],'0.5')
  # Formatting belongs to semantics; only explicit arithmetic is recomputed.
  numbered=replace(d,value='1. 违约金为.5个月标准租金。\n2）断电30分钟，漏水1小时。')
  result,_,_=self.run_review(numbered,self.verdict(transformations=['calculation'],calculations=[self.calculation()]))
  self.assertTrue(result['accepted'])
  result,_,_=self.run_review(replace(d,value='专精特新企业违约金半个月标准租金；断电30分钟，漏水1小时。'),self.verdict(transformations=['calculation'],calculations=[self.calculation()]))
  self.assertTrue(result['accepted'])
  for marker in ('1、','2）','第1项：'):
   result,_,_=self.run_review(replace(self.d,value=marker+self.d.value));self.assertTrue(result['accepted'])
  text='普通企业满一年退租，违约金为一个月标准租金。专精特新减半。断电三十分钟，漏水一小时。'
  s=replace(self.s,text=text)
  calc=self.calculation();calc['operands'][0]['quote']='违约金为一个月标准租金'
  result,_,_=self.run_review(d,self.verdict(transformations=['calculation'],calculations=[calc]),[s]);self.assertTrue(result['accepted'])
  s=replace(self.s,text=self.s.text+' Available stock: 1,000kg.')
  result,_,_=self.run_review(replace(self.d,value=self.d.value+' Available inventory: 1000kg.'),spans=[s]);self.assertTrue(result['accepted'])
  # Equivalent labels do not replace exact source and arithmetic checks.
  for quote,text in [('专精特新减半',self.s.text),('专精特新按50%收取',self.s.text.replace('专精特新减半','专精特新按50%收取')),
                     ('专精特新按五十％收取',self.s.text.replace('专精特新减半','专精特新按五十％收取'))]:
   with self.subTest(quote=quote):
    calc=self.calculation();calc['unit']='月';calc['operands'][1].update(interpretation='literal',quote=quote)
    s=replace(self.s,text=text);candidate=replace(d,quote=quote)
    result,ledger,_=self.run_review(candidate,self.verdict(transformations=['calculation'],calculations=[calc]),[s])
    self.assertTrue(result['accepted']);self.assertTrue(ledger.accepts(self.q,candidate,[s]))
    self.assertIn(ledger.records['q10']['calculations'][0]['operands'][1]['interpretation'],('half','percent'))
  s=replace(self.s,text=self.s.text.replace('专精特新减半','专精特新按50%收取'))
  calc=self.calculation();calc['operands'][1].update(value='50',quote='专精特新按50%收取',interpretation='literal');calc['result']='50'
  result,ledger,_=self.run_review(replace(d,value='违约金50个月。',quote='专精特新按50%收取'),self.verdict(transformations=['calculation'],calculations=[calc]),[s])
  self.assertFalse(result['accepted']);self.assertFalse(ledger.accepts(self.q,replace(d,value='违约金50个月。'),[s]))
  # A minimal semantic review may validate a derivation without legacy calculation records.
  s=replace(self.s,text=self.s.text+'未满半年适用其他条款。')
  candidate=replace(d,evidence=[{'span_id':'s1','quote':quote} for quote in ('违约金为1个月标准租金','专精特新减半','断电30分钟，漏水1小时')])
  result,ledger,_=self.run_review(candidate,self.verdict(transformations=[]),[s])
  self.assertTrue(result['accepted']);self.assertEqual(ledger.records['q10']['attempts'][0]['code'],'SUPPORTED')
  result,_,_=self.run_review(candidate,self.verdict(transformations=['calculation'],calculations=[self.calculation()]),[s]);self.assertTrue(result['accepted'])
  for amount,answer,quote,value in [('关联退款账户6222021234567890123；违约金为1个月标准租金','违约金0.5个月。','违约金为1个月标准租金','1'),
                                   ('违约金为半个月标准租金','违约金0.25个月。','违约金为半个月标准租金','0.5')]:
   s=replace(self.s,text=amount+'。专精特新减半。');calc=self.calculation()
   calc['operands'][0].update(value=value,quote=amount);calc['result']='0.5' if value=='1' else '0.25'
   result,_,_=self.run_review(replace(d,value=answer,quote=quote),self.verdict(transformations=['calculation'],calculations=[calc]),[s]);self.assertTrue(result['accepted'])
 def test_false_numbers_quotes_units_and_calculations_cannot_grant_permission(self):
  candidate=replace(self.d,value='违约金为0.5个月标准租金；断电30分钟，漏水1小时。')
  valid=self.calculation()
  cases=[(replace(self.d,value='违约金为99个月标准租金。'),self.verdict(verdict='unsupported')),
    (replace(self.d,value='Available inventory: 300kg.'),self.verdict(verdict='unsupported')),
    (replace(self.d,value='违约金1e999999个月。'),self.verdict(verdict='unsupported')),
    (replace(self.d,value='违约金9999999999999999999999个月。'),self.verdict(verdict='unsupported')),
    (replace(candidate,quote='资料中没有这句话'),self.verdict(transformations=['calculation'],calculations=[valid]))]
  for field,value in [('result','0.6'),('operation','divide'),('unit','元')]:
   calc=copy.deepcopy(valid);calc[field]=value;cases.append((candidate,self.verdict(transformations=['calculation'],calculations=[calc])))
  for field,value in [('quote','违约金为100个月'),('span_id','missing'),('value','NaN'),('value','1e999999'),('value','10')]:
   calc=copy.deepcopy(valid);calc['operands'][0][field]=value;cases.append((candidate,self.verdict(transformations=['calculation'],calculations=[calc])))
  for d,review in cases:
   with self.subTest(answer=d.value,review=review):
    result,ledger,_=self.run_review(d,review)
    self.assertFalse(result['accepted']);self.assertFalse(ledger.accepts(self.q,d,[self.s]))
 def test_permission_binding_bounded_failures_and_cancellation(self):
  result,ledger,client=self.run_review();self.assertTrue(result['accepted'])
  self.assertTrue(ledger.accepts(self.q,replace(self.d,reason='仅新增报告说明'),[self.s]))
  for q,d,spans in [(replace(self.q,text='另一问题'),self.d,[self.s]),(self.q,replace(self.d,value='新的答案'),[self.s]),(self.q,self.d,[replace(self.s,text=self.s.text+'来源发生改变')]),(self.q,self.d,[replace(self.s,locator={'paragraph':2})]),(self.q,self.d,[replace(self.s,source_sha256='changed')])]:
   self.assertFalse(ledger.accepts(q,d,spans))
  fresh=SupportLedger();fresh.records.update(ledger.records);self.assertFalse(fresh.accepts(self.q,self.d,[self.s]))
  review_answers(client,[(self.q,replace(self.d,value='新的答案'))],[self.s],ledger,lambda:False);self.assertEqual(len(client.calls),1)
  for response in [[],[self.verdict(),self.verdict()],ValueError('LLM_TIMEOUT')]:
   client=FakeReviewer(response);fresh=SupportLedger();result=review_answers(client,[(self.q,self.d)],[self.s],fresh,lambda:False)
   self.assertFalse(result['q10']['accepted']);self.assertEqual(len(client.calls),1 if isinstance(response,Exception) else 4)
  for client,cancelled in [(FakeReviewer(ValueError('TASK_CANCELLED')),lambda:False),(FakeReviewer([self.verdict()]),lambda:True)]:
   with self.assertRaisesRegex(ValueError,'TASK_CANCELLED'):review_answers(client,[(self.q,self.d)],[self.s],SupportLedger(),cancelled)
 def test_json_object_review_retries_three_times_and_records_field_paths(self):
  valid=self.verdict();bad=self.verdict(reason=None)
  class SequenceReviewer(FakeReviewer):
   def generate_json(inner,prompt,schema):
    inner.reviews=[bad if len(inner.calls)<3 else valid]
    return super().generate_json(prompt,schema)
  client=SequenceReviewer([]);ledger=SupportLedger()
  result=review_answers(client,[(self.q,self.d)],[self.s],ledger)
  self.assertTrue(result['q10']['accepted']);self.assertEqual(len(client.calls),4)
  attempts=ledger.records['q10']['attempts']
  self.assertEqual(len(attempts),4)
  self.assertEqual(attempts[0]['path'],'reason')
  self.assertEqual(attempts[-1]['code'],'SUPPORTED')
  self.assertEqual(client.calls[1]['correction_errors'][0]['path'],'reason')
  client=FakeReviewer([bad]);ledger=SupportLedger()
  result=review_answers(client,[(self.q,self.d)],[self.s],ledger)
  self.assertFalse(result['q10']['accepted']);self.assertEqual(len(client.calls),4)
  self.assertEqual(len(ledger.records['q10']['attempts']),4)
  # A valid sibling must not be re-reviewed; a semantic rejection is final.
  other=replace(self.q,id='q9');other_d=replace(self.d,question_id='q9')
  class MixedReviewer(FakeReviewer):
   def generate_json(inner,prompt,schema):
    inner.reviews=([self.verdict(question_id='q9'),bad] if not inner.calls else [valid])
    return super().generate_json(prompt,schema)
  client=MixedReviewer([]);ledger=SupportLedger()
  result=review_answers(client,[(other,other_d),(self.q,self.d)],[self.s],ledger)
  self.assertTrue(all(r['accepted'] for r in result.values()))
  self.assertEqual([item['question']['id'] for item in client.calls[1]['questions']],['q10'])
  self.assertEqual(len(ledger.records['q9']['attempts']),1)
  for verdict in ('unsupported','uncertain'):
   _,_,client=self.run_review(review=self.verdict(verdict=verdict));self.assertEqual(len(client.calls),1)
  class StrictReviewer(FakeReviewer):
   def generate_json(inner,prompt,schema):
    response=super().generate_json(prompt,schema);response.metadata['output_mode']='json_schema';return response
  client=StrictReviewer([bad]);review_answers(client,[(self.q,self.d)],[self.s],SupportLedger())
  self.assertEqual(len(client.calls),1)
  client=FakeReviewer([bad])
  with self.assertRaisesRegex(ValueError,'TASK_CANCELLED'):
   review_answers(client,[(self.q,self.d)],[self.s],SupportLedger(),lambda:len(client.calls)>=1)
 def test_operand_diagnostic_is_specific_and_survives_report(self):
  from autofill.adapters.documents.report import _answer_review
  calc=self.calculation();calc['operands'][1].update(interpretation='literal',value='0.4')
  d=replace(self.d,value='违约金0.5个月，断电30分钟，漏水1小时。')
  result,ledger,client=self.run_review(d,self.verdict(transformations=['calculation'],calculations=[calc]))
  self.assertFalse(result['accepted']);self.assertEqual(len(client.calls),4)
  attempts=_answer_review(ledger.records['q10'])['attempts']
  self.assertEqual(attempts[0]['path'],'calculations[0].operands[1]')
  self.assertEqual(attempts[0]['check'],'operand_not_supported_by_quote')
  self.assertEqual(attempts[0]['details']['operand_value'],'0.4')
  self.assertEqual(attempts[0]['details']['interpretation'],'literal')
  self.assertNotIn('专精特新',json.dumps(attempts,ensure_ascii=False))
  # Retries need actual field-level feedback, including arithmetic failures.
  for field,value,path,check in [('result',0.5,'calculations[0].result','expected_decimal_string'),
      ('result','0.6','calculations[0].result','arithmetic_result_mismatch'),
      ('operands',[],'calculations[0].operands','expected_two_operands')]:
   calc=self.calculation();calc[field]=value
   result,ledger,client=self.run_review(d,self.verdict(transformations=['calculation'],calculations=[calc]))
   self.assertFalse(result['accepted']);attempt=ledger.records['q10']['attempts'][0]
   self.assertEqual(attempt['path'],path);self.assertEqual(attempt['check'],check)
   self.assertEqual(client.calls[1]['correction_errors'][0]['path'],path)
   self.assertEqual(_answer_review(ledger.records['q10'])['attempts'][0]['details'],attempt['details'])
  result,ledger,client=self.run_review(d,self.verdict(transformations=[]))
  self.assertTrue(result['accepted']);self.assertEqual(ledger.records['q10']['attempts'][0]['path'],'verdict')

 def test_literal_and_choice_answers_are_reviewed_and_revised_once(self):
  q=Question('q9','请说明审批负责人及退款期限。','text',[],['t9'])
  source=SourceSpan('s9',{},'经理负责审批。90天退款。','hash')
  candidate=Decision('q9','ready','经理负责审批。',[],['s9'],'经理负责审批。','原文')
  calls=[]
  class Reviewer:
   def generate_json(inner,prompt,schema):
    body=json.loads(prompt.split('\n',1)[1]);calls.append(body['phase'])
    if body['phase']=='answer_revision':
     return LLMResponse({'decisions':[dict(question_id='q9',status='ready',value='经理负责审批。\n90天退款。',option_ids=[],evidence=[dict(span_id='s9',quote='经理负责审批。'),dict(span_id='s9',quote='90天退款。')],reason='补全退款子项')]})
    value=body['questions'][0]['answer']['value']
    return LLMResponse({'reviews':[dict(question_id='q9',verdict='supported' if '90天' in value else 'uncertain',transformations=[],reason='覆盖全部问题' if '90天' in value else '缺少退款期限',calculations=[])]})
  ledger=SupportLedger();result=review_answers(Reviewer(),[(q,candidate)],[source],ledger,allow_revision=True)
  revised=result['q9']['decision']
  self.assertTrue(result['q9']['accepted']);self.assertEqual(revised.value,'经理负责审批。\n90天退款。')
  self.assertEqual(calls,['answer_support_review','answer_revision','answer_support_review'])
  self.assertTrue(ledger.accepts(q,revised,[source]));self.assertFalse(ledger.accepts(q,candidate,[source]))
  q=Question('q10','是否立即退款？','boolean',['是','否'],['t10'])
  d=Decision('q10','ready',None,['否'],['s9'],'90天退款。','原文')
  result,_,_=self.run_review() # Original paraphrase seam remains supported.
  ledger=SupportLedger();result=review_answers(FakeReviewer([self.verdict(question_id='q10',transformations=[])]),[(q,d)],[source],ledger)
  self.assertTrue(result['q10']['accepted']);self.assertTrue(ledger.accepts(q,d,[source]))
  q=Question('q10','专精特新企业违约金是否减半？','boolean',['是','否'],['t10'])
  d=Decision('q10','ready',None,['是'],['s1'],'专精特新减半。','原文')
  ledger=SupportLedger()
  result=review_answers(FakeReviewer([self.verdict(transformations=['calculation'],calculations=[self.calculation()])]),[(q,d)],[self.s],ledger)
  self.assertTrue(result['q10']['accepted'])

import unittest
from autofill.models import Question, SourceSpan
from autofill.domain.decisions import batch_schema, decode_model_batch


class AnswerContractTests(unittest.TestCase):
 def setUp(self):
  self.questions=[Question('q4','选择规则','multiple_choice',['A','B'],[]),Question('q10','费用和响应','text',[],[])]
  self.spans=[SourceSpan('s1',{},'违约金减半。','one'),SourceSpan('s2',{},'30 分钟\n响应。','two')]
 def item(self,**updates):
  value=dict(question_id='q10',status='ready',value='违约金减半。\n30 分钟 响应。',option_ids=[],evidence=[{'span_id':'s1','quote':'违约金减半。'},{'span_id':'s2','quote':'30 分钟 响应。'}],reason='原文')
  value.update(updates);return value
 def decode(self,*items):return decode_model_batch({'decisions':list(items)},self.questions,self.spans)
 def test_schema_binds_question_options_and_evidence(self):
  schema=batch_schema(self.questions,self.spans)
  branches=schema['properties']['decisions']['items']['anyOf']
  self.assertFalse(schema['additionalProperties'])
  for branch,question in zip(branches,self.questions):
   self.assertEqual(set(branch['required']),set(branch['properties']))
   self.assertFalse(branch['additionalProperties'])
   self.assertEqual(branch['properties']['question_id']['enum'],[question.id])
   self.assertEqual(branch['properties']['evidence'],{'$ref':'#/$defs/evidence'})
   citation=schema['$defs']['evidence']['items']
   self.assertEqual(citation['properties']['span_id']['enum'],['s1','s2'])
   self.assertFalse(citation['additionalProperties'])
  self.assertEqual(branches[0]['properties']['option_ids']['items']['enum'],['A','B'])
  self.assertEqual(branches[0]['properties']['value']['type'],'null')
  self.assertEqual(branches[1]['properties']['option_ids']['maxItems'],0)
  # A normal eight-question batch must not multiply the same source-ID enum
  # until the provider rejects the whole schema at its 1,000-value limit.
  large=batch_schema([Question(f'q{i}','Choose','multiple_choice',[f'option{n}' for n in range(100)],[]) for i in range(8)],
    [SourceSpan(f's{i}',{},'x','hash') for i in range(101)])
  def enum_count(value):
   if isinstance(value,dict):return len(value.get('enum',[]))+sum(enum_count(v) for k,v in value.items() if k!='enum')
   if isinstance(value,list):return sum(enum_count(v) for v in value)
   return 0
  self.assertEqual(enum_count(large),949)
 def test_independent_quotes_support_compound_answer(self):
  decisions,issues=self.decode(self.item())
  self.assertEqual(decisions[1].status,'ready')
  self.assertEqual(decisions[1].span_ids,['s1','s2'])
  self.assertEqual(decisions[1].quote,'违约金减半。')
  self.assertEqual(decisions[1].evidence,[{'span_id':'s1','quote':'违约金减半。'},{'span_id':'s2','quote':'30 分钟\n响应。'}])
  self.assertNotIn('q10',issues)
  for evidence,code in [([{'span_id':'s1','quote':'30 分钟 响应。'}],'QUOTE_MISMATCH'),([{'span_id':'foreign','quote':'违约金减半。'}],'EVIDENCE_ID'),([{'span_id':'s2','quote':'300 分钟响应。'}],'QUOTE_MISMATCH')]:
   with self.subTest(code=code,evidence=evidence):
    decisions,issues=self.decode(self.item(evidence=evidence))
    self.assertEqual(decisions[1].status,'needs_review');self.assertEqual(issues['q10'],code)
 def test_choice_quote_error_is_not_an_option_error(self):
  decisions,issues=self.decode(self.item(question_id='q4',value=None,option_ids=['A'],evidence=[{'span_id':'s1','quote':'invented'}]))
  self.assertEqual(issues['q4'],'QUOTE_MISMATCH')
  self.assertIn('引用',decisions[0].reason);self.assertNotIn('选项',decisions[0].reason)
  self.assertNotIn('QUOTE_MISMATCH',decisions[0].reason)
 def test_schema_and_business_review_are_distinct_and_legacy_still_works(self):
  for updates in [dict(reason=None),dict(evidence='s1'),dict(option_ids='A'),dict(status=[])]:
   with self.subTest(updates=updates):
    decisions,issues=self.decode(self.item(status='needs_review',value=None,**updates) if 'status' not in updates else self.item(value=None,**updates))
    self.assertIn('q10',issues);self.assertEqual(decisions[1].status,'needs_review')
  decisions,issues=self.decode(self.item(status='needs_review',value=None,evidence=[],reason='适用范围不明确'))
  self.assertEqual(decisions[1].reason,'适用范围不明确');self.assertNotIn('q10',issues)
  legacy=self.item();legacy.pop('evidence');legacy.update(span_ids=['s1','s2'],quote='违约金减半。')
  decisions,issues=self.decode(legacy)
  self.assertEqual(decisions[1].status,'ready');self.assertNotIn('q10',issues)
  decisions,issues=self.decode(dict(legacy,status='not_found',value=None,evidence=None))
  self.assertEqual(issues['q10'],'DECISION_SCHEMA')
  decisions,issues=self.decode(dict(legacy,status='not_found',value=None,evidence=[{'span_id':'s1'}]))
  self.assertEqual(issues['q10'],'DECISION_SCHEMA')
  decisions,issues=self.decode(self.item(),self.item())
  self.assertEqual(issues['q10'],'UNKNOWN_OR_DUPLICATE_QUESTION')
  self.assertEqual(issues['q4'],'MODEL_OMITTED')
  # Rejected values never reach diagnostics; field paths still identify the
  # exact contract check, including nested evidence failures.
  cases=[(self.item(reason=None),'reason','expected_string'),
   (self.item(evidence=False),'evidence','expected_array'),
   (self.item(evidence=[{'span_id':'s1','quote':9}]),'evidence[0].quote','expected_string'),
   (self.item(evidence=[{'span_id':'s1','quote':'PRIVATE_FAKE_QUOTE'}]),'evidence[0].quote','literal_source_quote'),
   (self.item(PRIVATE_FIELD_NAME='PRIVATE_VALUE'),'decisions[]','required_fields')]
  for item,path,check in cases:
   with self.subTest(path=path,check=check):
    diagnostics={};_,issues=decode_model_batch({'decisions':[item]},self.questions,self.spans,diagnostics=diagnostics)
    self.assertIn('q10',issues);self.assertEqual(diagnostics['q10']['path'],path);self.assertEqual(diagnostics['q10']['check'],check)
    self.assertNotIn('PRIVATE',str(diagnostics))

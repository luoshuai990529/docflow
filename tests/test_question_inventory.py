import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from autofill.models import DocumentData,SourceSpan,Question,Target
from autofill.adapters.documents.questionnaire_structure import slot_anchors

class InventoryTests(unittest.TestCase):
 def doc(self):
  return DocumentData('xlsx','hash',[
   SourceSpan('s1',{'sheet':'Sheet','cell':'A1'},'Do you inspect equipment? Yes / No','hash'),
   SourceSpan('s2',{'sheet':'Sheet','cell':'A2'},'Describe your fire procedure.','hash')],
   [Question('q1','Do you inspect equipment? Yes / No','text',[],['t1'])],
   [Target('t1',{'type':'xlsx_cell','sheet':'Sheet','part':'xl/worksheets/sheet1.xml','cell':'B1'}, {})])
 def test_unlabelled_type_and_unlocated_question_remain_in_inventory(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  class Model:
   def generate_json(self,prompt,schema):
    return SimpleNamespace(data={'reviewed_span_ids':['s1','s2'],'items':[
     {'span_ids':['s1'],'quote':'Do you inspect equipment? Yes / No','kind':'boolean','options':['Yes','No'],'target_id':'t1'},
     {'span_ids':['s2'],'quote':'Describe your fire procedure.','kind':'text','options':[],'target_id':None}]})
  q=self.doc();interpret_questionnaire(q,Path('unused.xlsx'),Model())
  self.assertEqual([(v.kind,v.target_ids) for v in q.questions],[('boolean',['t1']),('text',[])])
  self.assertEqual(q.metadata['question_inventory']['unlocated_questions'],1)
  self.assertEqual(q.metadata['question_inventory']['status'],'checked')
 def test_bad_or_omitted_inventory_cannot_erase_native_slots_or_claim_full_coverage(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  for payload in ({'decisions':[]},{'reviewed_span_ids':['s1','s2'],'items':[]},{'reviewed_span_ids':['s1'],'items':[]},
    {'reviewed_span_ids':['s1','s2'],'items':[{'span_ids':['s2'],'quote':'Describe your fire procedure.','kind':'text','options':[],'target_id':'t1'}]}):
   with self.subTest(payload=payload):
    class Model:
     def generate_json(self,*args):return SimpleNamespace(data=payload)
    q=self.doc();interpret_questionnaire(q,Path('unused.xlsx'),Model())
    self.assertEqual(q.questions[0].target_ids,['t1'])
    self.assertEqual(q.metadata['question_inventory']['status'],'incomplete')
 def test_no_writable_slots_is_a_reported_question_not_a_silent_omission(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  q=self.doc();q.questions=[];q.targets=[]
  class Model:
   def generate_json(self,*args):return SimpleNamespace(data={'reviewed_span_ids':['s1','s2'],'items':[
    {'span_ids':['s1'],'quote':'Do you inspect equipment? Yes / No','kind':'boolean','options':['Yes','No'],'target_id':None},
    {'span_ids':['s2'],'quote':'Describe your fire procedure.','kind':'text','options':[],'target_id':None}]})
  interpret_questionnaire(q,Path('unused.xlsx'),Model())
  self.assertEqual(len(q.questions),2)
  self.assertTrue(all(not v.target_ids for v in q.questions))
 def test_located_field_does_not_hide_a_second_question_in_the_same_span(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  q=self.doc();q.spans=q.spans[:1];q.spans[0].text='公司名称：______；是否通过认证？'
  q.questions[0].text='公司名称'
  class Model:
   def generate_json(self,*args):return SimpleNamespace(data={'reviewed_span_ids':['s1'],'items':[
    {'span_ids':['s1'],'quote':'公司名称','kind':'text','options':[],'target_id':'t1'}]})
  interpret_questionnaire(q,Path('unused.xlsx'),Model())
  self.assertEqual(len(q.questions),2)
  self.assertEqual(q.questions[0].target_ids,['t1'])
  self.assertEqual(q.questions[1].target_ids,[])
  self.assertIn('是否通过认证？',q.questions[1].text)
  self.assertEqual(q.metadata['question_inventory']['unlocated_questions'],1)
 def test_nearby_questions_cannot_reclassify_an_unrelated_native_slot(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  for kind,locators in (
   ('xlsx',[{'sheet':'Sheet','cell':'A1'},{'sheet':'Sheet','cell':'C1'}]),
   ('pdf',[{'page_index':0,'bbox':[20,20,100,30]},{'page_index':0,'bbox':[20,40,100,50]}])):
   with self.subTest(kind=kind):
    q=DocumentData(kind,'hash',[SourceSpan('s1',locators[0],'公司名称','hash'),SourceSpan('s2',locators[1],'是否通过认证？是/否','hash')],
     [Question('q1','公司名称','text',[],['t1']),Question('q2','是否通过认证？是/否','text',[],['t2'])],
     [Target('t1',{'type':'xlsx_cell','sheet':'Sheet','cell':'B1'} if kind=='xlsx' else {'type':'pdf_rect','page_index':0,'rect':[110,18,200,32]},{}),
      Target('t2',{'type':'xlsx_cell','sheet':'Sheet','cell':'D1'} if kind=='xlsx' else {'type':'pdf_rect','page_index':0,'rect':[110,38,200,52]}, {})])
    self.assertEqual(slot_anchors(q,None),{'t1':{'s1'},'t2':{'s2'}})
    class Model:
     def generate_json(self,*args):return SimpleNamespace(data={'reviewed_span_ids':['s1','s2'],'items':[
      {'span_ids':['s2'],'quote':'是否通过认证？是/否','kind':'boolean','options':['是','否'],'target_id':'t1'},
      {'span_ids':['s1'],'quote':'公司名称','kind':'text','options':[],'target_id':'t2'}]})
    interpret_questionnaire(q,Path('unused'),Model())
    self.assertEqual(q.questions[0].kind,'text')
    self.assertEqual(q.metadata['question_inventory']['status'],'incomplete')
    self.assertTrue(all(t.constraints.get('position_review') for t in q.targets))
 def test_identical_question_text_at_different_locations_is_not_deduplicated(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  for native_slot in (True,False):
   with self.subTest(native_slot=native_slot):
    q=self.doc();q.spans[0].text=q.spans[1].text='Company name'
    q.questions[0].text='Company name'
    if not native_slot:q.questions=[];q.targets=[]
    first={'span_ids':['s1'],'quote':'Company name','kind':'text','options':[],'target_id':'t1' if native_slot else None}
    second={'span_ids':['s2'],'quote':'Company name','kind':'text','options':[],'target_id':None}
    class Model:
     def generate_json(self,*args):return SimpleNamespace(data={'reviewed_span_ids':['s1','s2'],'items':[first,second,second]})
    interpret_questionnaire(q,Path('unused.xlsx'),Model())
    self.assertEqual(len(q.questions),2)
    self.assertEqual(q.metadata['question_inventory']['unlocated_questions'],1 if native_slot else 2)
 def test_inventory_uses_full_original_question_and_explicit_nonquestion_exclusion(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  q=self.doc();q.spans[0].text='In a fire, describe the response and notification procedure.'
  q.questions[0].text='response'
  q.spans[0].locator['context']={'heading':'Fire safety'}
  q.spans[1].text='Example supplier name: ______'
  q.questions.append(Question('q2','Example supplier name:','text',[],['t2']))
  q.targets.append(Target('t2',{'type':'xlsx_cell','sheet':'Sheet','cell':'B2'},{}))
  class Model:
   def generate_json(inner,prompt,schema):
    body=json.loads(prompt.split('\n',1)[1])
    self.assertIn('Fire safety',body['reading_context'])
    return SimpleNamespace(data={'reviewed_span_ids':['s1','s2'],'excluded_target_ids':['t2'],'items':[
     {'span_ids':['s1'],'quote':q.spans[0].text,'kind':'text','options':[],'target_id':'t1'}]})
  interpret_questionnaire(q,Path('unused.xlsx'),Model())
  self.assertEqual([(v.id,v.text,v.target_ids) for v in q.questions],[('q1',q.spans[0].text,['t1'])])
  self.assertEqual([t.id for t in q.targets],['t1'])
  self.assertEqual(q.metadata['question_inventory']['status'],'checked')
  self.assertEqual(q.metadata['question_inventory']['excluded_targets'],['t2'])
  # A shorter quote cannot erase native conditions or subquestions.
  doc=self.doc();doc.spans[0].text=doc.questions[0].text='请说明营业收入，分别列出境内与境外收入。'
  class ShortQuote:
   def generate_json(inner,*args):return SimpleNamespace(data={'reviewed_span_ids':['s1','s2'],'items':[
    {'span_ids':['s1'],'quote':'请说明营业收入','kind':'text','options':[],'target_id':'t1'}]})
  interpret_questionnaire(doc,Path('unused.xlsx'),ShortQuote())
  self.assertIn('境内与境外',doc.questions[0].text)
  # Removing an excluded native slot cannot cause unlocated IDs to collide.
  doc=self.doc();doc.spans.extend([SourceSpan('s3',{'sheet':'Sheet','cell':'A3'},'请列出产品','hash'),SourceSpan('s4',{'sheet':'Sheet','cell':'A4'},'请说明地址','hash')])
  class Excluded:
   def generate_json(inner,*args):return SimpleNamespace(data={'reviewed_span_ids':['s1','s2','s3','s4'],'excluded_target_ids':['t1'],'items':[
    {'span_ids':['s3'],'quote':'请列出产品','kind':'text','options':[],'target_id':None}]})
  interpret_questionnaire(doc,Path('unused.xlsx'),Excluded())
  self.assertEqual(len({v.id for v in doc.questions}),len(doc.questions))
  for excluded,reviewed,items in [(['t2'],['s1'],[]),(['foreign'],['s1','s2'],[]),(['t2','t2'],['s1','s2'],[]),
    (['t1'],['s1','s2'],[{'span_ids':['s1'],'quote':q.spans[0].text,'kind':'text','options':[],'target_id':'t1'}])]:
   with self.subTest(excluded=excluded,reviewed=reviewed):
    doc=self.doc()
    class Invalid:
     def generate_json(inner,*args):return SimpleNamespace(data={'reviewed_span_ids':reviewed,'excluded_target_ids':excluded,'items':items})
    interpret_questionnaire(doc,Path('unused.xlsx'),Invalid())
    self.assertEqual(doc.questions[0].target_ids,['t1'])
    self.assertEqual(doc.metadata['question_inventory']['status'],'incomplete')
 def test_unreviewed_refs_do_not_rewrite_question_or_override_native_choice_controls(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  for reviewed in ([],['s1','s2']):
   q=self.doc();q.questions[0].kind='single_choice';q.questions[0].options=['approved','rejected']
   class Model:
    def generate_json(inner,*args):return SimpleNamespace(data={'reviewed_span_ids':reviewed,'items':[
     {'span_ids':['s1'],'quote':q.spans[0].text,'kind':'boolean','options':['Yes','No'],'target_id':'t1'}]})
   interpret_questionnaire(q,Path('unused.xlsx'),Model())
   self.assertEqual(q.questions[0].kind,'single_choice')
   self.assertEqual(q.questions[0].options,['approved','rejected'])
   if not reviewed:self.assertEqual(q.metadata['question_inventory']['status'],'incomplete')
 def test_slot_is_only_supplied_once_when_its_anchors_cross_batches(self):
  from tests.helpers import interpret_with_local_structure as interpret_questionnaire
  q=self.doc();q.spans=[SourceSpan(f's{i}',{'sheet':'Sheet','cell':'A1'},'Company name' if i==0 else 'heading '+str(i),'hash') for i in range(33)]
  q.questions[0].text='Company name'
  calls=[]
  class Model:
   def generate_json(inner,prompt,schema):
    body=json.loads(prompt.split('\n',1)[1]);calls.extend(s['target_id'] for s in body['slots'])
    return SimpleNamespace(data={'reviewed_span_ids':[s['id'] for s in body['spans']],'items':[
     {'span_ids':[body['spans'][0]['id']],'quote':body['spans'][0]['text'],'kind':'text','options':[],'target_id':s['target_id']} for s in body['slots']]})
  interpret_questionnaire(q,Path('unused.xlsx'),Model())
  self.assertEqual(calls,['t1'])
  self.assertEqual(q.metadata['question_inventory']['status'],'checked')

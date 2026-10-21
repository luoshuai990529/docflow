import unittest,tempfile
from pathlib import Path
from tests.helpers import pdf
class CatalogTests(unittest.TestCase):
 def test_unknown_rejected(self):
  from autofill.adapters.documents.catalog import resolve_questionnaire
  with tempfile.TemporaryDirectory() as t:
   p=pdf(Path(t)/'unknown.pdf')
   with self.assertRaisesRegex(ValueError,'UNSUPPORTED_QUESTIONNAIRE'):resolve_questionnaire(p,Path(t)/'work')
 def test_test_marker_preflight_before_model(self):
  from autofill.core import run_pair
  from autofill.config import preset_config
  from autofill.domain.write_plan import TEST_LABEL
  from tests.helpers import template
  import json
  class Never:
   def generate_json(self,*a):raise AssertionError('must reject before model call')
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);q=pdf(r/'q.pdf')
   from tests.helpers import docx
   from zipfile import ZipFile
   s=docx(r/'s.docx')
   with ZipFile(s) as z:parts={name:z.read(name) for name in z.namelist()}
   parts['word/document.xml']=parts['word/document.xml'].replace(b'Supplier data',TEST_LABEL.encode())
   with ZipFile(s,'w') as z:
    for name,data in parts.items():z.writestr(name,data)
   tp=r/'t.json';tp.write_text(json.dumps(template(q)))
   with self.assertRaisesRegex(ValueError,'TEST_LABEL_TARGET_REQUIRED'):run_pair(q,s,tp,preset_config('deepseek'),r/'out',client=Never())
 def test_render_signature_includes_blank_graphics(self):
  import pymupdf as M
  from autofill.adapters.documents.catalog import render_signature
  with tempfile.TemporaryDirectory() as t:
   paths=[]
   for i in range(2):
    p=Path(t)/f'{i}.pdf';d=M.open();page=d.new_page();page.insert_text((30,30),'same text');page.draw_rect(M.Rect(30+i*100,60,40+i*100,70));d.save(p);d.close();paths.append(p)
   self.assertNotEqual(render_signature(paths[0]),render_signature(paths[1]))
 def test_production_certificate_wording_neutral(self):
  import json
  p=Path(__file__).resolve().parents[1]/'server/src/autofill/templates/supplier-pdf.json'
  data=json.loads(p.read_text())
  self.assertTrue(all('Test data NO' not in q['text'] for q in data['questions']))
 def test_matcher_batches_large_questionnaire(self):
  from autofill.application.answering.match import LLMMatcher
  from autofill.models import Question
  from autofill.adapters.models.openai_chat import LLMResponse
  import json
  sizes=[]
  class Client:
   def generate_json(self,prompt,schema):
    data=json.loads(prompt[prompt.index('{'):]);sizes.append(len(data['questions']));return LLMResponse({'decisions':[]})
  LLMMatcher(Client()).match([Question(str(i),'q','text',[],[]) for i in range(202)],[])
  self.assertGreater(len(sizes),1);self.assertLessEqual(max(sizes),24);self.assertEqual(sum(sizes),202)
 def test_reviewed_prechecked_box_preserved_only_if_same(self):
  import pymupdf as M
  from autofill.adapters.documents.extract import extract_questionnaire
  from autofill.models import Decision
  from tests.helpers import write_candidate as write_copy
  from tests.helpers import template
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);p=pdf(r/'q.pdf');d=M.open(p);page=d[0];page.draw_rect(M.Rect(30,80,40,90));page.draw_line((32,82),(38,88));page.draw_line((32,88),(38,82));d.save(r/'marked.pdf');d.close();p=r/'marked.pdf'
   target={'id':'company','locator':{'type':'pdf_rect','page_index':0,'rect':[30,80,40,90],'coordinate_space':'unrotated_pymupdf','option_id':'YES'},'constraints':{'content_type':'checkbox','initial_checked':True}}
   question={'id':'company','text':'Marked box','kind':'single_choice','options':['YES','NO'],'target_ids':['company']}
   q=extract_questionnaire(p,template(p,[target],[question]));ds=[Decision('company','ready',None,['YES'],['s1'],'Company: SAMPLE CO','explicit')]
   write_copy(p,r/'same.pdf',q,ds,'real')
   with M.open(p) as a,M.open(r/'same.pdf') as b:self.assertEqual(a[0].get_pixmap().samples,b[0].get_pixmap().samples)
   ds[0].option_ids=['NO']
   with self.assertRaisesRegex(ValueError,'UNSUPPORTED_PREEXISTING_CHECKBOX'):write_copy(p,r/'conflict.pdf',q,ds,'real')
 def test_overlay_readback_excludes_existing_labels_in_rect(self):
  import pymupdf as M
  from autofill.adapters.documents.extract import extract_questionnaire
  from autofill.models import Decision
  from tests.helpers import write_candidate as write_copy
  from autofill.adapters.documents.verify import verify_copy
  from tests.helpers import template
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);d=M.open();p=d.new_page(width=400,height=500);p.insert_text((25,98),'Label:        kg',fontsize=10);d.save(r/'q.pdf');d.close()
   target={'id':'company','locator':{'type':'pdf_rect','page_index':0,'rect':[50,82,200,102],'coordinate_space':'unrotated_pymupdf'},'constraints':{'font_size':8,'min_font_size':8}}
   q=extract_questionnaire(r/'q.pdf',template(r/'q.pdf',[target]));ds=[Decision('company','ready','500',[],['s1'],'Label:        kg','explicit')]
   results=write_copy(r/'q.pdf',r/'out.pdf',q,ds,'real');results,v=verify_copy(r/'q.pdf',r/'out.pdf',q,results,r/'pages')
   self.assertTrue(v.content_ok);self.assertEqual(results[0].readback_value,'500');self.assertEqual(v.layout_check,'passed')
 def test_overlay_readback_rejects_removed_original_label(self):
  import pymupdf as M
  from autofill.adapters.documents.verify import overlay_text_delta
  a=M.open();p=a.new_page(width=400,height=500);p.insert_text((30,95),'Original label',fontsize=9)
  b=M.open();q=b.new_page(width=400,height=500);q.insert_text((50,95),'500',fontsize=9)
  try:self.assertIsNone(overlay_text_delta(p,q,M.Rect(25,80,180,110)))
  finally:a.close();b.close()

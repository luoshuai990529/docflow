"""Capacity hints stay advisory; actual writer geometry decides PDF fit."""
import tempfile
import unittest
from pathlib import Path

import pymupdf as M
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject, TextStringObject

from autofill.adapters.documents.answer_layout import annotate_layout, answer_hints, check_answer_fit
from autofill.models import Decision, DocumentData, Question, Target
from autofill.adapters.documents.extract import sha
from tests.helpers import write_candidate as write_copy


class AnswerLayoutTests(unittest.TestCase):
 def setUp(self):
  self.temp = tempfile.TemporaryDirectory()
  self.root = Path(self.temp.name)
 def tearDown(self):
  self.temp.cleanup()
 def pdf(self, targets, questions=None, widget=False):
  path = self.root / 'questionnaire.pdf'
  with M.open() as doc:
   page = doc.new_page(width=300, height=300)
   if widget:
    field = M.Widget(); field.field_name = 'answer'; field.field_type = M.PDF_WIDGET_TYPE_TEXT
    field.rect = M.Rect(20, 20, 200, 70); field.text_fontsize = 10
    page.add_widget(field)
   doc.save(path)
  questions = questions or [Question('q', 'Answer', 'text', [], [target.id for target in targets])]
  return path, DocumentData('pdf', sha(path), [], questions, targets)
 def answer(self, value, option_ids=None):
  return Decision('q', 'ready', value, option_ids or [], [], None, '')
 def test_same_length_different_glyphs_and_writer_use_same_fit(self):
  target = Target('t', {'type':'pdf_rect', 'page_index':0, 'rect':[20,20,100,50]}, {'font_size':10})
  path, document = self.pdf([target]); annotate_layout(path, document)
  self.assertGreater(target.constraints['answer_budget']['suggested_max_chars'], 0)
  for index, value in enumerate(('i' * 24, 'W' * 24)):
   failures = check_answer_fit(path, document, self.answer(value))
   results = write_copy(path, self.root / f'output{index}.pdf', document, [self.answer(value)], 'real')
   self.assertEqual(bool(failures), results[0].status == 'layout_blocked')
   self.assertEqual(bool(failures), index == 1)
  self.assertEqual(check_answer_fit(path, document, self.answer('W' * 24))[0]['code'], 'TEXT_OVERFLOW')
 def test_form_native_length_single_line_and_editable_appearance(self):
  target = Target('t', {'type':'pdf_field', 'full_name':'answer'}, {'font_size':10})
  path, document = self.pdf([target], widget=True)
  reader = PdfReader(path); writer = PdfWriter(); writer.clone_document_from_reader(reader)
  field = writer.get_fields()['answer']; field[NameObject('/MaxLen')] = NumberObject(5)
  # get_fields returns field dictionaries; update the original widget too.
  for page in writer.pages:
   for reference in page.get('/Annots', []):
    reference.get_object()[NameObject('/MaxLen')] = NumberObject(5)
  with path.open('wb') as stream: writer.write(stream)
  document.sha256 = sha(path); annotate_layout(path, document)
  budget = target.constraints['answer_budget']
  self.assertTrue(budget['single_line']); self.assertEqual(budget['native_max_length'], 5)
  self.assertEqual(check_answer_fit(path, document, self.answer('abcdef'))[0]['code'], 'NATIVE_MAX_LENGTH')
  self.assertEqual(check_answer_fit(path, document, self.answer('a\nb'))[0]['code'], 'FIELD_SINGLE_LINE')
  output = self.root / 'filled.pdf'
  results = write_copy(path, output, document, [self.answer('测试')], 'real')
  self.assertEqual(results[0].status, 'pending_readback')
  from autofill.adapters.documents.verify import verify_copy
  _, verification = verify_copy(path, output, document, results, self.root / 'preview')
  self.assertTrue(verification.content_ok); self.assertEqual(verification.layout_check, 'passed')
 def test_repeated_targets_use_tightest_budget_and_options_text_measures_actual_value(self):
  large = Target('large', {'type':'pdf_rect','page_index':0,'rect':[20,20,240,80]}, {'font_size':10})
  small = Target('small', {'type':'pdf_rect','page_index':0,'rect':[20,100,80,120]}, {'font_size':10,'options_text':True})
  box = Target('box', {'type':'pdf_rect','page_index':0,'rect':[20,130,28,138],'option_id':'A'}, {'content_type':'checkbox'})
  path, document = self.pdf([large, small, box]); annotate_layout(path, document)
  hint = answer_hints(document)['q']
  self.assertEqual(hint['suggested_max_chars'], small.constraints['answer_budget']['suggested_max_chars'])
  self.assertEqual([row['target_id'] for row in hint['targets']], ['large','small'])
  # options_text writes option identifiers joined by the writer, not Decision.value.
  document.questions[0].target_ids = ['small','box']
  failures = check_answer_fit(path, document, self.answer(None, ['W' * 50]))
  self.assertEqual([row['target_id'] for row in failures], ['small'])
 def test_all_native_widgets_share_trial_layout_including_rotation_and_original_small_font(self):
  path = self.root / 'repeated.pdf'
  with M.open() as doc:
   page=doc.new_page(width=300,height=300)
   for rect in ([20,20,240,70],[20,100,80,150]):
    widget=M.Widget();widget.field_name='answer';widget.field_type=M.PDF_WIDGET_TYPE_TEXT
    widget.rect=M.Rect(rect);widget.text_fontsize=8;page.add_widget(widget)
   doc.save(path)
  writer=PdfWriter();writer.clone_document_from_reader(PdfReader(path))
  second=writer.pages[0]['/Annots'][1].get_object()
  second[NameObject('/MK')]=DictionaryObject({NameObject('/R'):NumberObject(90)})
  second[NameObject('/DA')]=TextStringObject('/Helv 8 Tf 0 g')
  with path.open('wb') as stream:writer.write(stream)
  target=Target('t',{'type':'pdf_field','full_name':'answer'},{'font_size':10,'min_font_size':9})
  document=DocumentData('pdf',sha(path),[],[Question('q','Answer','text',[],['t'])],[target])
  annotate_layout(path,document)
  self.assertEqual(len(target.constraints['pdf_text_layouts']),2)
  self.assertEqual(target.constraints['pdf_text_layouts'][1]['font_size'],8)
  self.assertTrue(check_answer_fit(path,document,self.answer('W'*30)))
  self.assertFalse(check_answer_fit(path,document,self.answer('abcd')))
  output=self.root/'repeated-filled.pdf'
  results=write_copy(path,output,document,[self.answer('abcd')],'real')
  from autofill.adapters.documents.verify import verify_copy
  _,verification=verify_copy(path,output,document,results,self.root/'repeated-preview')
  self.assertTrue(verification.content_ok);self.assertEqual(verification.layout_check,'passed')


if __name__ == '__main__': unittest.main()

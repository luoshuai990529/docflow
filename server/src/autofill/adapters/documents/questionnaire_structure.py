"""Native questionnaire slot relationships, isolated from model interpretation."""
import re
from zipfile import ZipFile

from ...domain.models import DocumentData
from .documents import W, resolve_path, column_number, column_name
from .preflight import Limits, xml
from .source_context import enrich_source


def compact(value):
 return ''.join(value.split())


def slot_anchors(document,path):
 """Link native targets to nearby original spans, never model-generated locations."""
 anchors={t.id:set() for t in document.targets};paragraphs=[];root=None
 if document.kind=='docx':
  with ZipFile(path) as z:root=xml(z.read('word/document.xml'))
  paragraphs=list(root.iter(W+'p'));indices={id(p):i for i,p in enumerate(paragraphs)}
 for target in document.targets:
  loc=target.locator;result=anchors[target.id]
  question=next((q for q in document.questions if target.id in q.target_ids),None)
  if document.kind=='docx':
   nodes=[resolve_path(root,loc['path'][:i]) for i in range(len(loc['path'])+1)] if 'path' in loc else []
   ps=[n for n in nodes if n.tag==W+'p']
   if not ps and nodes:ps=list(nodes[-1].iter(W+'p'))
   positions={indices[id(p)] for p in ps}
   # Use the previous label only for an empty slot, never an unrelated
   # preceding question beside an already populated paragraph.
   represented={s.locator.get('paragraph') for s in document.spans if s.locator.get('part')=='word/document.xml'}
   if not positions & represented:positions.update(i-1 for i in list(positions) if i>0)
   result.update(s.id for s in document.spans if s.locator.get('part')=='word/document.xml' and s.locator.get('paragraph') in positions)
  elif document.kind=='xlsx':
   row=re.search(r'\d+',loc.get('cell',''))
   if row and column_number(loc['cell'])>1:
    # Native XLSX discovery pairs an answer cell with its immediate left label,
    # not with every question sharing that row.
    label=column_name(column_number(loc['cell'])-1)+row[0]
    result.update(s.id for s in document.spans if s.locator.get('sheet')==loc.get('sheet') and s.locator.get('cell')==label)
  elif loc.get('type')=='pdf_rect':
   rect=loc['rect']
   result.update(s.id for s in document.spans if question and compact(s.text)==compact(question.text)
                 and s.locator.get('page_index')==loc.get('page_index') and s.locator.get('bbox')
                 and -2<=rect[0]-s.locator['bbox'][2]<=105 and abs(s.locator['bbox'][3]-rect[3])<26)
  if question and not result and document.kind!='xlsx' and loc.get('type')!='pdf_rect':
   result.update(s.id for s in document.spans if compact(question.text) in compact(s.text))
 return anchors

class LocalQuestionnaireStructure:
 @property
 def max_questions(self):
  return Limits().max_questions

 def enrich_source(self, document, path):
  return enrich_source(document, path)

 def slot_anchors(self, document, path):
  return slot_anchors(document, path)

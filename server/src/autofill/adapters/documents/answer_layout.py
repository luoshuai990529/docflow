"""Program-owned answer-space hints and the PDF writer's actual text layout.

Character budgets are conservative hints, never geometry validation. PDF text is
trial-laid out with the same font, rectangle, padding and line spacing as writing.
Office flow regions stay expandable; their existing rendered verification wins.
"""
from functools import lru_cache
from pathlib import Path
import math
import re

import pymupdf as M
from pypdf import PdfReader


@lru_cache(maxsize=2)
def _font(unicode_text):
 return M.Font('china-s' if unicode_text else 'helv')


def _inherited(node, key):
 while node is not None:
  if key in node:return node[key]
  parent=node.get('/Parent');node=parent.get_object() if parent else None
 return None


def _widget_name(node):
 parts=[]
 while node is not None:
  if '/T' in node:parts.insert(0,str(node['/T']))
  parent=node.get('/Parent');node=parent.get_object() if parent else None
 return '.'.join(parts)


def _form_profiles(path, targets):
 """Keep every same-name widget: repeated displays must all fit one answer."""
 reader=PdfReader(path);by_name={t.locator['full_name']:t for t in targets if t.locator.get('type')=='pdf_field'}
 form=reader.trailer['/Root'].get('/AcroForm',{});form=form.get_object() if hasattr(form,'get_object') else form
 profiles={name:[] for name in by_name}
 for page_index,page in enumerate(reader.pages):
  for reference in page.get('/Annots',[]):
   widget=reference.get_object();name=_widget_name(widget)
   if widget.get('/Subtype')!='/Widget' or name not in by_name:continue
   field_type=str(_inherited(widget,'/FT') or '')
   by_name[name].constraints['pdf_field_type']=field_type
   if field_type!='/Tx':continue
   constraints=by_name[name].constraints;bounds=widget['/Rect']
   width=float(bounds[2]-bounds[0]);height=float(bounds[3]-bounds[1])
   da=str(_inherited(widget,'/DA') or form.get('/DA',''));sizes=re.findall(r'([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s+Tf\b',da)
   native_size=float(sizes[-1]) if sizes else 0
   size=native_size if native_size>0 else float(constraints.get('font_size',10))
   flags=int(_inherited(widget,'/Ff') or 0);maximum=int(_inherited(widget,'/MaxLen') or 0)
   appearance=widget.get('/MK',{});appearance=appearance.get_object() if hasattr(appearance,'get_object') else appearance
   rotation=int(appearance.get('/R',0))%360
   # Preserve an explicitly sized native field. The existing default minimum
   # must not reject its original 8pt size, or force it up to a larger font.
   minimum=float(constraints.get('min_font_size',9))
   if native_size>0:minimum=min(minimum,native_size)
   profiles[name].append(dict(rect=[2,2,width-2,height-2],font_size=size,
    min_font_size=minimum,line_height=None,
    single_line=not bool(flags & 4096),native_max_length=maximum if maximum>0 else None,
    rotation=rotation,alignment=int(_inherited(widget,'/Q') or 0),
    appearance_width=width,appearance_height=height,page_index=page_index))
 return profiles


def pdf_text_profiles(target):
 """JSON-safe profiles generated during preparation, never supplied by a model."""
 return target.constraints.get('pdf_text_layouts',[])


def place_pdf_text(page, profile, value):
 """Return (uncommitted shape, failure code); caller commits only a passing fit.

Used by scratch measurement, overlay writing and editable form appearances.
"""
 size=float(profile['font_size']);minimum=float(profile.get('min_font_size',size))
 if not math.isfinite(size) or size<minimum or size<=0:raise ValueError('FONT_SIZE')
 rect=M.Rect(profile['rect']);rotation=int(profile.get('rotation',0))
 if rotation not in (0,90,180,270):return None,'UNSUPPORTED_FIELD_ROTATION'
 if not all(math.isfinite(v) for v in rect) or rect.width<=0 or rect.height<=0:return None,'AREA_TOO_SMALL'
 maximum=profile.get('native_max_length')
 if maximum and len(value)>maximum:return None,'NATIVE_MAX_LENGTH'
 if profile.get('single_line') and ('\n' in value or '\r' in value):return None,'FIELD_SINGLE_LINE'
 unicode_text=any(ord(c)>255 for c in value);font=_font(unicode_text)
 if any(font.has_glyph(ord(c))==0 for c in value if not c.isspace()):return None,'FONT_GLYPH_MISSING'
 direction_width=rect.height if rotation in (90,270) else rect.width
 if profile.get('single_line') and font.text_length(value,fontsize=size)>direction_width:return None,'TEXT_OVERFLOW'
 name='FillCJK' if unicode_text else 'FillLatin';page.insert_font(fontname=name,fontbuffer=font.buffer)
 kwargs=dict(fontname=name,fontsize=size,rotate=rotation,align=int(profile.get('alignment',0)),color=(0,0,0))
 if profile.get('line_height') is not None:kwargs['lineheight']=float(profile['line_height'])
 shape=page.new_shape();space=shape.insert_textbox(rect,value,**kwargs)
 if space<0:return None,'TEXT_OVERFLOW'
 return shape,None


def check_pdf_text(profile, value):
 rect=M.Rect(profile['rect'])
 with M.open() as scratch:
  page=scratch.new_page(width=max(1,rect.x1+2),height=max(1,rect.y1+2))
  _,code=place_pdf_text(page,profile,value)
 return code


def _capacity(profile, character, lines=False):
 rect=M.Rect(profile['rect']);size=float(profile['font_size'])
 # Bound scratch input from geometry rather than a global questionnaire limit.
 upper=max(1,int(max(rect.width,0)*max(rect.height,0)/max(size*size,1)*4+4))
 if lines:upper=max(1,int(max(rect.width,rect.height)/max(size,1)*2+2))
 if profile.get('native_max_length'):upper=min(upper,profile['native_max_length'])
 if lines and profile.get('single_line'):upper=1
 low=0
 while low<upper:
  middle=(low+upper+1)//2;value=('\n'.join([character]*middle)) if lines else character*middle
  if check_pdf_text(profile,value) is None:low=middle
  else:upper=middle-1
 return low


_LAYOUT_KEYS=('font_size','min_font_size','line_height','single_line','native_max_length','rotation','alignment')


@lru_cache(maxsize=512)
def _profile_capacity(key):
 """Repeated table cells share measurements only when every layout value agrees."""
 width,height,*values=key;profile=dict(zip(_LAYOUT_KEYS,values));profile['rect']=[0,0,width,height]
 return min(_capacity(profile,'W'),_capacity(profile,'字')),min(_capacity(profile,'A',True),_capacity(profile,'字',True))


def _pdf_budget(profiles):
 measured=[]
 for profile in profiles:
  rect=M.Rect(profile['rect']);key=(rect.width,rect.height,*(profile.get(name) for name in _LAYOUT_KEYS))
  measured.append(_profile_capacity(key))
 capacities=[item[0] for item in measured];lines=[item[1] for item in measured]
 limits=[p['native_max_length'] for p in profiles if p.get('native_max_length')]
 capacity=min(capacities)
 budget=dict(space_type='fixed',suggested_max_chars=max(1,int(capacity*.9)) if capacity else 0,
  max_lines=min(lines),single_line=any(p.get('single_line') for p in profiles),basis='pdf_actual_font_trial',
  font_size=max(p['font_size'] for p in profiles))
 if limits:budget['native_max_length']=min(limits);budget['suggested_max_chars']=min(budget['suggested_max_chars'],min(limits))
 return budget


def annotate_layout(path, document):
 """Attach preparation-time hints without changing any customer document."""
 path=Path(path)
 if document.kind in ('docx','xlsx'):
  from .office import office_answer_budgets
  budgets=office_answer_budgets(path,document)
  for target in document.targets:
   if target.constraints.get('content_type')!='checkbox':target.constraints['answer_budget']=budgets.get(target.id,{'space_type':'unknown'})
  return
 if document.kind!='pdf':return
 fields=_form_profiles(path,document.targets)
 for target in document.targets:
  if target.constraints.get('content_type')=='checkbox':continue
  profiles=[]
  if target.locator.get('type')=='pdf_rect':
   size=float(target.constraints.get('font_size',10))
   profiles=[dict(rect=list(target.locator['rect']),font_size=size,min_font_size=float(target.constraints.get('min_font_size',size)),
    line_height=float(target.constraints.get('line_height',1.05)),single_line=False,native_max_length=None,rotation=0,alignment=0)]
  elif target.locator.get('type')=='pdf_field':profiles=fields.get(target.locator['full_name'],[])
  if profiles:
   target.constraints['pdf_text_layouts']=profiles;target.constraints['answer_budget']=_pdf_budget(profiles)
  else:
   target.constraints['answer_budget']={'space_type':'unknown'}
   if target.constraints.get('pdf_field_type') in ('/Btn','/Ch'):target.constraints['answer_budget']['text_capacity']=False


def answer_hints(document):
 """One repeated answer must fit its tightest text target, never summed areas."""
 targets={target.id:target for target in document.targets};hints={}
 for question in document.questions:
  budgets=[dict(target_id=tid,**targets[tid].constraints['answer_budget']) for tid in question.target_ids
   if tid in targets and targets[tid].constraints.get('content_type')!='checkbox' and targets[tid].constraints.get('answer_budget')
   and targets[tid].constraints['answer_budget'].get('text_capacity',True)]
  if not budgets:continue
  hint={'targets':budgets,'space_type':'fixed' if any(b.get('space_type')=='fixed' for b in budgets) else budgets[0]['space_type']}
  for key in ('suggested_max_chars','max_lines','native_max_length'):
   values=[b[key] for b in budgets if isinstance(b.get(key),int)]
   if values:hint[key]=min(values)
  hint['single_line']=any(b.get('single_line') for b in budgets);hints[question.id]=hint
 return hints


def check_answer_fit(path, document, decision):
 """Return only measurable failures; Office's rendered verifier remains final."""
 if decision.status!='ready':return []
 question=next((q for q in document.questions if q.id==decision.question_id),None)
 if question is None:return []
 targets={t.id:t for t in document.targets}
 if any('answer_budget' not in targets[tid].constraints for tid in question.target_ids if tid in targets and targets[tid].constraints.get('content_type')!='checkbox'):
  annotate_layout(path,document)
 from ...domain.write_plan import target_value
 failures=[]
 for tid in question.target_ids:
  target=targets.get(tid)
  if target is None or target.constraints.get('content_type')=='checkbox':continue
  value=target_value(target,decision);budget=target.constraints.get('answer_budget',{});code=None
  if not budget.get('text_capacity',True):continue
  maximum=budget.get('native_max_length')
  if maximum and len(value)>maximum:code='NATIVE_MAX_LENGTH'
  elif document.kind=='pdf':
   for profile in pdf_text_profiles(target):
    code=check_pdf_text(profile,value)
    if code:break
   if code=='TEXT_OVERFLOW' and any(check_pdf_text(p,'i') is not None and check_pdf_text(p,'字') is not None for p in pdf_text_profiles(target)):
    code='AREA_TOO_SMALL'
  # Excel rows can grow; approximate glyph widths and height limits are hints.
  # The writer attempts capped row growth and LibreOffice confirms visibility.
  if code:failures.append(dict(target_id=tid,**budget,code=code))
 return failures

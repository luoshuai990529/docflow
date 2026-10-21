"""Separate logical readback and basic layout checks, without an Office GUI."""
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as E
import pymupdf as M
from pypdf import PdfReader
from ...domain.models import DocumentData,WriteResult,Verification
from .extract import NS,sha,doc_root,resolve_docx,text
from .write import static_box_checked

def norm(s):return ''.join((s or '').split())
def inherited(annot,key):
 obj=annot
 while obj:
  if key in obj:return obj[key]
  parent=obj.get('/Parent');obj=parent.get_object() if parent else None
 return None
def widget_name(obj):
 names=[]
 while obj:
  if '/T' in obj:names.insert(0,str(obj['/T']))
  parent=obj.get('/Parent');obj=parent.get_object() if parent else None
 return '.'.join(names)
def verify_fields(path):
 reader=PdfReader(path);fields=reader.get_fields() or {};widgets={}
 for page in reader.pages:
  for ref in page.get('/Annots',[]):
   obj=ref.get_object()
   if obj.get('/Subtype')=='/Widget':widgets.setdefault(widget_name(obj),[]).append(obj)
 return fields,widgets


def appearance_fonts_embedded(widgets):
 dereference=lambda value:value.get_object() if hasattr(value,'get_object') else value
 for group in widgets.values():
  for widget in group:
   ap=dereference(widget.get('/AP',{}));normal=ap.get('/N')
   if normal is None:continue
   normal=normal.get_object()
   if '/Resources' not in normal:continue
   resources=dereference(normal['/Resources']);fonts=dereference(resources.get('/Font',{}))
   for name,ref in fonts.items():
    if not str(name).startswith('/Fill'):continue
    font=ref.get_object()
    if '/DescendantFonts' in font:font=font['/DescendantFonts'][0].get_object()
    descriptor=font.get('/FontDescriptor')
    if descriptor is None:return False
    descriptor=descriptor.get_object()
    if not any(descriptor.get(key) is not None and descriptor[key].get_object().get_data()
               for key in ('/FontFile','/FontFile2','/FontFile3')):return False
 return True

def pixel_diff(a,b,rects,scale=1.5):
 pa=a.get_pixmap(matrix=M.Matrix(scale,scale),alpha=False);pb=b.get_pixmap(matrix=M.Matrix(scale,scale),alpha=False)
 if (pa.width,pa.height)!=(pb.width,pb.height):return -1,-1
 masks=[r*a.rotation_matrix for r in rects];changed=outside=0;aa=pa.samples;bb=pb.samples;stride=pa.width*3
 for y in range(pa.height):
  first=y*stride
  if aa[first:first+stride]==bb[first:first+stride]:continue
  for x in range(pa.width):
   k=first+x*3
   if aa[k:k+3]!=bb[k:k+3]:
    changed+=1
    if not any(r.x0-1<=x/scale<=r.x1+1 and r.y0-1<=y/scale<=r.y1+1 for r in masks):outside+=1
 return changed,outside

def overlay_text_delta(original_page,filled_page,rect):
 """Read added spans independently while retaining original labels and units."""
 from collections import Counter
 def spans(page):
  return [(span['text'],tuple(round(v,3) for v in span['bbox']),span['font'],round(span['size'],3))
          for block in page.get_text('dict',clip=rect)['blocks']
          for line in block.get('lines',[]) for span in line['spans']]
 baseline=Counter(spans(original_page));added=[]
 for span in spans(filled_page):
  if baseline[span]:baseline[span]-=1
  else:added.append(span[0])
 if any(count and span[0].strip() for span,count in baseline.items()):return None
 return ''.join(added)

def verify_copy(original:Path,output:Path,questionnaire:DocumentData,results:list[WriteResult],artifacts_dir:Path)->tuple[list[WriteResult],Verification]:
 if sha(original)!=questionnaire.sha256:raise ValueError('SOURCE_CHANGED')
 targets={t.id:t for t in questionnaire.targets};details={};structural=True
 if questionnaire.kind=='docx':
  source=doc_root(original);root=doc_root(output)
  with ZipFile(original) as a,ZipFile(output) as b:
   structural=set(a.namelist())==set(b.namelist()) and all(a.read(n)==b.read(n) for n in a.namelist() if n!='word/document.xml')
  for tag in ['tblPr','tblGrid','trPr','tcPr','pPr','rPr','sectPr']:
   structural &= [E.tostring(n) for n in source.findall('.//w:'+tag,NS)]==[E.tostring(n) for n in root.findall('.//w:'+tag,NS)]
  for r in results:
   if r.status!='pending_readback':continue
   t=targets[r.target_id];node=resolve_docx(root,t.locator)
   if t.constraints.get('content_type')=='checkbox':
    mark=node.find('w:sdtPr/w14:checkbox/w14:checked',NS);value='checked' if mark is not None and mark.get('{'+NS['w14']+'}val')=='1' else 'unchecked'
   else:value=text(node)
   r.readback_value=value;r.status='written' if norm(value)==norm(r.expected_value) else 'readback_failed'
  details={'structure_preserved':bool(structural),'renderer':'none','note':'OOXML readback only. Pagination, clipping and visual layout not verified.'}
  ok=structural and not any(r.status=='readback_failed' for r in results)
  return results,Verification(ok,'not_run',True,details)
 fields,widgets=verify_fields(output)
 with M.open(original) as a,M.open(output) as b:
  structural=len(a)==len(b) and all(x.rect==y.rect and x.cropbox==y.cropbox and x.rotation==y.rotation for x,y in zip(a,b))
  for r in results:
   if r.status!='pending_readback':continue
   t=targets[r.target_id];loc=t.locator;appearance=True
   if loc['type']=='pdf_field':
    field=fields.get(loc['full_name']);value=str(field.get('/V','')) if field else None
    group=widgets.get(loc['full_name'],[])
    appearance=bool(group) and all(str(inherited(w,'/V'))==value and bool(w.get('/AP',{}).get('/N')) for w in group)
    if field and field.get('/FT')=='/Tx':
     visible=[page.get_textbox(w.rect) for page in b for w in page.widgets() or [] if w.field_name==loc['full_name']]
     appearance &= bool(visible) and all(norm(v)==norm(value) for v in visible)
   else:
    page=b[loc['page_index']];rect=M.Rect(loc['rect'])
    if t.constraints.get('content_type')=='checkbox':
     value='checked' if static_box_checked(page,rect) else 'unchecked'
    else:value=overlay_text_delta(a[loc['page_index']],page,rect)
   r.readback_value=value;r.status='written' if norm(value)==norm(r.expected_value) and appearance else 'readback_failed'
   if r.status=='readback_failed':r.reason='VALUE_OR_APPEARANCE_MISMATCH'
  artifacts_dir=Path(artifacts_dir);artifacts_dir.mkdir(parents=True,exist_ok=True);pages=[]
  for i,(p,q) in enumerate(zip(a,b)):
   rects=[M.Rect(targets[r.target_id].locator['rect']) for r in results if r.status=='written' and targets[r.target_id].locator['type']=='pdf_rect' and targets[r.target_id].locator['page_index']==i]
   approved_names={targets[r.target_id].locator['full_name'] for r in results if r.status=='written' and targets[r.target_id].locator['type']=='pdf_field'}
   rects.extend(M.Rect(w.rect) for w in q.widgets() or [] if w.field_name in approved_names)
   diff,outside=pixel_diff(p,q,rects);q.get_pixmap(matrix=M.Matrix(1.5,1.5)).save(artifacts_dir/f'page-{i+1}.png')
   pages.append({'page':i+1,'changed_pixels':diff,'outside_fill_pixels':outside})
  font_ok=appearance_fonts_embedded(widgets)
  for p in b:
   for f in p.get_fonts():
    if f[4].startswith('Fill') and not b.extract_font(f[0])[3]:font_ok=False
  details={'pages':pages,'dimensions_preserved':bool(structural),'inserted_fonts_embedded':font_ok,'dpi':108,'mask_tolerance_pt':1,'renderer':M.__version__}
  ok=structural and font_ok and not any(r.status=='readback_failed' for r in results)
  layout='passed' if structural and font_ok and all(p['outside_fill_pixels']==0 for p in pages) else 'needs_review'
  return results,Verification(ok,layout,layout!='passed',details)

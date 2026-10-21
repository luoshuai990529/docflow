"""Direct PDF/OOXML extraction; no conversion or template guessing."""
from pathlib import Path
from zipfile import ZipFile,is_zipfile
import hashlib,json,xml.etree.ElementTree as E
import pymupdf as M
from pypdf import PdfReader
from ...domain.models import DocumentData,SourceSpan,Question,Target
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main','w14':'http://schemas.microsoft.com/office/word/2010/wordml'}
def sha(path:Path)->str:return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def text(node)->str:return ''.join(t.text or '' for t in node.findall('.//w:t',NS))
def load_template(path:Path)->dict:return json.loads(Path(path).read_text(encoding='utf-8'))
def kind(path:Path)->str:
 path=Path(path);magic=path.read_bytes()[:8]
 if magic.startswith(b'%PDF-'):
  if path.suffix.lower()!='.pdf':raise ValueError('FORMAT_EXTENSION_MISMATCH')
  return 'pdf'
 if is_zipfile(path):
  with ZipFile(path) as z:
   if 'word/document.xml' in z.namelist():
    if path.suffix.lower()!='.docx':raise ValueError('FORMAT_EXTENSION_MISMATCH')
    return 'docx'
 raise ValueError('UNSUPPORTED_FORMAT: only electronic PDF and controlled DOCX')
def doc_root(path):
 with ZipFile(path) as z:return E.fromstring(z.read('word/document.xml'))
def resolve_docx(root,locator):
 typ=locator.get('type')
 if typ=='sdt':
  found=[s for s in root.findall('.//w:sdt',NS) if (s.find('w:sdtPr/w:tag',NS) is not None and s.find('w:sdtPr/w:tag',NS).get('{'+NS['w']+'}val')==locator.get('tag'))]
 elif typ=='table_cell':
  found=[]
  for table in root.findall('.//w:tbl',NS):
   caption=table.find('w:tblPr/w:tblCaption',NS)
   if caption is None or caption.get('{'+NS['w']+'}val')!=locator.get('table_caption'):continue
   rows=table.findall('w:tr',NS)
   if not rows:continue
   cols=[i for i,c in enumerate(rows[0].findall('w:tc',NS)) if text(c).strip()==locator.get('column_label')]
   for row in rows[1:]:
    cells=row.findall('w:tc',NS)
    if cells and text(cells[0]).strip()==locator.get('row_label'):
     found.extend(cells[i] for i in cols if i<len(cells))
 else:raise ValueError('UNSUPPORTED_CONTROL')
 if len(found)!=1:raise ValueError('AMBIGUOUS_TARGET: expected exactly one anchor')
 node=found[0]
 if any(node.findall('.//w:'+tag,NS) for tag in ['fldSimple','fldChar','instrText','sdt']):raise ValueError('UNSUPPORTED_CONTROL: dynamic field or nested control')
 return node
def extract_source(path:Path)->DocumentData:
 path=Path(path);fmt=kind(path);digest=sha(path);spans=[]
 if fmt=='pdf':
  with M.open(path) as d:
   if d.needs_pass:raise ValueError('PASSWORD_REQUIRED')
   for p in d:
    for block in p.get_text('dict')['blocks']:
     if 'lines' not in block:continue
     for line in block['lines']:
      value=''.join(s['text'] for s in line['spans'])
      if value.strip():spans.append(SourceSpan(f's{len(spans)+1}',{'page_index':p.number,'bbox':list(line['bbox'])},value,digest))
 else:
  with ZipFile(path) as z:
   parts=[n for n in z.namelist() if n=='word/document.xml' or (n.startswith('word/') and n.endswith('.xml') and any(k in n for k in ['header','footer','footnotes','endnotes']))]
   for part in parts:
    root=E.fromstring(z.read(part));parents={child:parent for parent in root.iter() for child in parent}
    tables=root.findall('.//w:tbl',NS)
    for i,p in enumerate(root.findall('.//w:p',NS)):
     value=text(p)
     if not value.strip():continue
     loc={'part_uri':part,'paragraph_index':i};cur=p
     while cur in parents:
      cur=parents[cur]
      if cur.tag=='{'+NS['w']+'}tc':loc['cell_text']=text(cur)
      if cur.tag=='{'+NS['w']+'}tbl':loc['table_index']=tables.index(cur);break
     spans.append(SourceSpan(f's{len(spans)+1}',loc,value,digest))
 if not spans:raise ValueError('NO_READABLE_TEXT: scan/OCR not supported')
 return DocumentData(fmt,digest,spans,[],[])
def extract_questionnaire(path:Path,template:dict|None)->DocumentData:
 x=extract_source(path)
 if template is None:return x
 if template.get('schema_version')!=1:raise ValueError('TEMPLATE_SCHEMA')
 if template.get('questionnaire_sha256')!=x.sha256:raise ValueError('TEMPLATE_HASH_MISMATCH')
 x.questions=[Question(**q) for q in template['questions']];x.targets=[Target(**t) for t in template['targets']]
 for objects in [x.questions,x.targets]:
  if len({o.id for o in objects})!=len(objects):raise ValueError('DUPLICATE_ID')
 tids={t.id for t in x.targets};owners=[]
 for q in x.questions:
  if q.kind not in ['text','single_choice','multi_choice','boolean']:raise ValueError('UNSUPPORTED_QUESTION_TYPE')
  if len(set(q.options))!=len(q.options):raise ValueError('DUPLICATE_OPTION')
  if not set(q.target_ids)<=tids:raise ValueError('UNKNOWN_TARGET')
  owners.extend(q.target_ids)
 if len(owners)!=len(set(owners)):raise ValueError('AMBIGUOUS_TARGET_OWNER')
 if any(t.constraints.get('test_label') and t.id not in owners for t in x.targets):raise ValueError('TEST_LABEL_UNOWNED')
 if x.kind=='docx':
  root=doc_root(path)
  for t in x.targets:resolve_docx(root,t.locator)
 else:
  reader=PdfReader(path);form=reader.trailer['/Root'].get('/AcroForm')
  if form and form.get_object().get('/XFA'):raise ValueError('UNSUPPORTED_XFA')
  fields=reader.get_fields() or {}
  unique_form_names(reader)
  with M.open(path) as d:
   for t in x.targets:
    loc=t.locator
    if loc.get('type')=='pdf_rect':
     if loc.get('coordinate_space')!='unrotated_pymupdf':raise ValueError('COORDINATE_SPACE')
     index=loc['page_index']
     if not isinstance(index,int) or not 0<=index<len(d):raise ValueError('TARGET_BOUNDS')
     rect=M.Rect(loc['rect']);bounds=d[index].rect*d[index].derotation_matrix
     if rect.is_empty or rect.is_infinite or not bounds.contains(rect):raise ValueError('TARGET_BOUNDS')
    elif loc.get('type')=='pdf_field':
     name=loc.get('full_name')
     if name not in fields:raise ValueError('UNKNOWN_FIELD')
     if fields[name].get('/FT')=='/Sig':raise ValueError('UNSUPPORTED_SIGNATURE_FIELD')
    else:raise ValueError('UNSUPPORTED_TARGET')
  if len({t.locator['type'] for t in x.targets})>1:raise ValueError('UNSUPPORTED_MIXED_PDF_WRITERS')
 return x


def unique_form_names(reader):
 """Reject independent objects with the same qualified name, not shared widgets."""
 owners={}
 form=reader.trailer['/Root'].get('/AcroForm')
 if not form:return
 def walk(ref,prefix='',ft=None):
  obj=ref.get_object();name=str(obj.get('/T',''));full=(prefix+'.'+name if prefix and name else name or prefix);typ=obj.get('/FT',ft)
  if name and typ:
   identity=(getattr(ref,'idnum',None),getattr(ref,'generation',None))
   owners.setdefault(full,set()).add(identity)
  for kid in obj.get('/Kids',[]):walk(kid,full,typ)
 for ref in form.get_object().get('/Fields',[]):walk(ref)
 if any(len(ids)>1 for ids in owners.values()):raise ValueError('AMBIGUOUS_FIELD: independent fields share a name')
 for page in reader.pages:
  for ref in page.get('/Annots',[]):
   obj=ref.get_object()
   if obj.get('/Subtype')!='/Widget':continue
   naming=ref;cur=obj;parts=[]
   while cur:
    if '/T' in cur:
     parts.insert(0,str(cur['/T']))
     if not parts[:-1]:naming=getattr(cur,'indirect_reference',naming)
    parent=cur.get('/Parent');cur=parent.get_object() if parent else None
   full='.'.join(parts);identity=(getattr(naming,'idnum',None),getattr(naming,'generation',None))
   if full not in owners or identity not in owners[full]:raise ValueError('AMBIGUOUS_FIELD: orphan or unrelated widget')

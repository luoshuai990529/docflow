"""Fixed writers for hash-bound targets; never rebuild a questionnaire."""
from pathlib import Path
from zipfile import ZipFile
from tempfile import NamedTemporaryFile
import copy,os,xml.etree.ElementTree as E
from io import BytesIO
import pymupdf as M
from pypdf import PdfReader,PdfWriter
from ...domain.models import WriteResult
from ...domain.write_plan import WritePlan
from .extract import sha,NS,doc_root,resolve_docx
from .answer_layout import pdf_text_profiles,place_pdf_text,check_pdf_text
def set_docx_value(node,value,checkbox=False):
 if checkbox:
  mark=node.find('w:sdtPr/w14:checkbox/w14:checked',NS)
  if mark is None:raise ValueError('UNSUPPORTED_CONTROL: missing SDT checkbox')
  mark.set('{'+NS['w14']+'}val','1' if value=='checked' else '0')
  value='☒' if value=='checked' else '☐'
 texts=node.findall('.//w:t',NS)
 if not texts:raise ValueError('UNSUPPORTED_CONTROL: no existing text run')
 for t in texts[1:]:t.text=''
 from .office import node_path,set_word_text
 set_word_text(node,node_path(node,texts[0]),value)


def cjk_field_appearances(writer,fields):
 """Keep editable text fields and their appearances on the shared trial layout.

The historical name is retained; Latin fields now use the same path too, so
preflight and delivered appearances cannot silently use different fonts/layouts.
"""
 from pypdf.generic import DictionaryObject,NameObject,TextStringObject,DecodedStreamObject,ArrayObject,FloatObject
 from .verify import widget_name
 form=writer._root_object['/AcroForm'].get_object()
 resources=form.setdefault(NameObject('/DR'),DictionaryObject()).get_object()
 fonts=resources.setdefault(NameObject('/Font'),DictionaryObject()).get_object()
 indices={}
 for page in writer.pages:
  for ref in page.get('/Annots',[]):
   widget=ref.get_object();name=widget_name(widget)
   if widget.get('/Subtype')!='/Widget' or name not in fields:continue
   value,profiles=fields[name];index=indices.get(name,0);indices[name]=index+1
   profile=profiles[index];size=profile['font_size'];width=profile['appearance_width'];height=profile['appearance_height']
   with M.open() as scratch:
    surface=scratch.new_page(width=width,height=height);shape,code=place_pdf_text(surface,profile,value)
    if code:raise ValueError('FORM_APPEARANCE_'+code)
    shape.commit();data=scratch.tobytes()
   appearance_pdf=PdfReader(BytesIO(data));appearance_page=appearance_pdf.pages[0]
   appearance=widget.setdefault(NameObject('/AP'),DictionaryObject()).get_object();background=appearance.get('/N')
   ap=DecodedStreamObject();contents=appearance_page.get_contents();ap.set_data(contents.get_data() if contents is not None else b'')
   ap.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Form'),
              NameObject('/BBox'):ArrayObject([FloatObject(0),FloatObject(0),FloatObject(width),FloatObject(height)]),
              NameObject('/Resources'):appearance_page['/Resources'].clone(writer)})
   inserted_fonts=ap['/Resources']['/Font'];font_name='/FillCJK' if any(ord(c)>255 for c in value) else '/FillLatin'
   fonts[NameObject(font_name)]=inserted_fonts[font_name]
   if background is not None and hasattr(background.get_object(),'get_data'):
    # Keep the blank field's original border/background as a nested Form.
    ap['/Resources'][NameObject('/XObject')]=DictionaryObject({NameObject('/OriginalField'):background})
    ap.set_data(b'q /OriginalField Do Q\n'+ap.get_data())
   appearance[NameObject('/N')]=writer._add_object(ap)
   parent=widget
   while '/Parent' in parent and '/T' not in parent:parent=parent['/Parent'].get_object()
   parent[NameObject('/V')]=TextStringObject(value)
   parent[NameObject('/DA')]=TextStringObject(f'{font_name} {size:g} Tf 0 g')
   widget[NameObject('/V')]=TextStringObject(value)
   widget[NameObject('/DA')]=parent['/DA']
 writer.set_need_appearances_writer(False)
def write_copy(source_path:Path,output_path:Path,plan:WritePlan)->list[WriteResult]:
 if not isinstance(plan,WritePlan):raise ValueError('WRITE_PLAN_REQUIRED')
 source_path=Path(source_path);output_path=Path(output_path)
 if output_path.resolve()==source_path.resolve() or output_path.exists():raise ValueError('OUTPUT_EXISTS_OR_SOURCE')
 if sha(source_path)!=plan.source_sha256:raise ValueError('SOURCE_CHANGED')
 mode=plan.mode;kind=plan.questionnaire_kind
 if mode not in ('real','fictional_test'):raise ValueError('MODE')
 if mode=='fictional_test' and not output_path.name.startswith('TEST-SAMPLE'):raise ValueError('TEST_FILENAME_REQUIRED')
 label_id=plan.test_label_target_id
 if mode=='fictional_test' and not label_id:raise ValueError('TEST_LABEL_TARGET_REQUIRED')
 if not plan.target_count:raise ValueError('UNSUPPORTED_TEMPLATE')
 operations=[(operation.question_id,operation,operation.value) for operation in plan.operations]
 results=[]
 if mode=='fictional_test' and not any(operation.target_id==label_id for operation in plan.operations):raise ValueError('TEST_LABEL_NOT_READY')
 output_path.parent.mkdir(parents=True,exist_ok=True)
 with NamedTemporaryFile(dir=output_path.parent,suffix=output_path.suffix,delete=False) as f:tmp=Path(f.name)
 try:
  if kind=='xlsx' or (kind=='docx' and plan.questionnaire_metadata.get('discovery')=='structural-v2'):
   from .office import write_office
   results=write_office(source_path,tmp,kind,operations)
  elif kind=='docx':
   root=doc_root(source_path)
   for qid,t,value in operations:
    set_docx_value(resolve_docx(root,t.locator),value,t.constraints.get('content_type')=='checkbox')
    results.append(WriteResult(qid,t.id,'pending_readback',value,None,''))
   # Preserve namespace declarations used by mc:Ignorable as well as element prefixes.
   with ZipFile(source_path) as z:
    original=z.read('word/document.xml')
    import io
    declarations=list(E.iterparse(io.BytesIO(original),events=['start-ns']))
    for _,(prefix,uri) in declarations:
     try:E.register_namespace(prefix,uri)
     except ValueError:pass
    rewritten=E.tostring(root,encoding='utf-8',xml_declaration=True)
    import re
    opening=rewritten.find(b'>',rewritten.find(b'<w:document')) if b'<w:document' in rewritten else rewritten.find(b'>',rewritten.find(b'<ns0:document'))
    # ElementTree omits unused namespace declarations referenced in attribute values.
    for _,(prefix,uri) in declarations:
     token=('xmlns:'+prefix+'=').encode() if prefix else b'xmlns='
     if token not in rewritten[:opening]:
      addition=(' xmlns'+(':'+prefix if prefix else '')+'="'+uri+'"').encode();rewritten=rewritten[:opening]+addition+rewritten[opening:];opening+=len(addition)
    with ZipFile(tmp,'w') as dest:
     for info in z.infolist():dest.writestr(copy.copy(info),rewritten if info.filename=='word/document.xml' else z.read(info.filename))
  else:
   field_operations=[op for op in operations if op[1].locator['type']=='pdf_field']
   overlay_operations=[op for op in operations if op[1].locator['type']=='pdf_rect']
   if len(field_operations)+len(overlay_operations)!=len(operations):raise ValueError('UNSUPPORTED_TARGET')
   if field_operations:
    reader=PdfReader(source_path);writer=PdfWriter();writer.clone_document_from_reader(reader)
    values={};text_values={};fields=reader.get_fields() or {}
    for qid,t,value in field_operations:
     name=t.locator['full_name'];field=fields[name]
     if field.get('/FT')=='/Tx':
      profiles=pdf_text_profiles(t)
      if not profiles:raise ValueError('UNSUPPORTED_FIELD_WIDGET')
      code=next((code for profile in profiles if (code:=check_pdf_text(profile,value))),None)
      if code:
       results.append(WriteResult(qid,t.id,'layout_blocked',value,None,code));continue
      text_values[name]=(value,profiles)
     else:values[name]=value
     results.append(WriteResult(qid,t.id,'pending_readback',value,None,''))
    if values:writer.update_page_form_field_values(None,values,auto_regenerate=False)
    if text_values:cjk_field_appearances(writer,text_values)
    with tmp.open('wb') as f:writer.write(f)
   if overlay_operations or not field_operations:
    # Apply overlays to the form-filled copy, keeping its editable fields and
    # appearances. Opening bytes avoids saving over an opened intermediate file.
    document=M.open(stream=tmp.read_bytes(),filetype='pdf') if field_operations else M.open(source_path)
    with document as doc:
     for qid,t,value in overlay_operations:
      p=doc[t.locator['page_index']];rect=M.Rect(t.locator['rect'])
      if t.constraints.get('content_type')=='checkbox':
       if static_box_checked(p,rect):
        if t.constraints.get('initial_checked') is True and value=='checked':
         results.append(WriteResult(qid,t.id,'pending_readback',value,None,'Preserved reviewed existing check mark'));continue
        raise ValueError('UNSUPPORTED_PREEXISTING_CHECKBOX')
       if value=='checked':
        inset=min(rect.width,rect.height)*.2;a=rect.tl+M.Point(inset,inset);b=rect.br-M.Point(inset,inset)
        p.draw_line(a,b,color=(0,0,0),width=1);p.draw_line(M.Point(a.x,b.y),M.Point(b.x,a.y),color=(0,0,0),width=1)
       results.append(WriteResult(qid,t.id,'pending_readback',value,None,''));continue
      shape,code=place_pdf_text(p,pdf_text_profiles(t)[0],value)
      if code:results.append(WriteResult(qid,t.id,'layout_blocked',value,None,code));continue
      shape.commit(overlay=True);results.append(WriteResult(qid,t.id,'pending_readback',value,None,''))
     doc.save(tmp,garbage=0,deflate=True)
  if mode=='fictional_test' and not any(r.target_id==label_id and r.status=='pending_readback' for r in results):raise ValueError('TEST_LABEL_WRITE_FAILED')
  if sha(source_path)!=plan.source_sha256:raise ValueError('SOURCE_CHANGED')
  # Exclusive final placement, including concurrent attempts to the same path.
  os.link(tmp,output_path)
 finally:tmp.unlink(missing_ok=True)
 return results


def static_box_checked(page,rect):
 """Controlled blank-box subset: reject dark marks inside the approved box."""
 from collections import Counter
 inset=min(rect.width,rect.height)*.25
 inner=M.Rect(rect.x0+inset,rect.y0+inset,rect.x1-inset,rect.y1-inset)
 pix=page.get_pixmap(matrix=M.Matrix(2,2),clip=inner*page.rotation_matrix,alpha=False)
 pixels=list(zip(pix.samples[0::3],pix.samples[1::3],pix.samples[2::3]))
 if not pixels:return True
 background=Counter(pixels).most_common(1)[0][0]
 return sum(background)/3<170 or any(any(c<bg-40 for c,bg in zip(px,background)) for px in pixels)

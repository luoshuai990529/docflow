"""Surgical OOXML writes and independent readback/render gates."""
import copy
import math
import re
from collections import Counter
import xml.etree.ElementTree as E
from pathlib import Path
from zipfile import ZipFile
import pymupdf as M
from .documents import W, S, resolve_path, cell_text
from .preflight import xml
from ...domain.models import WriteResult, Verification


def serialize(root, original):
 import io
 declarations=list(E.iterparse(io.BytesIO(original),events=['start-ns']))
 for _,(prefix,uri) in declarations:
  try:E.register_namespace(prefix,uri)
  except ValueError:pass
 data=E.tostring(root,encoding='utf-8',xml_declaration=True)
 start=data.find(b'<',data.find(b'?>')+2);end=data.find(b'>',start)
 for _,(prefix,uri) in declarations:
  token=('xmlns'+(':'+prefix if prefix else '')+'=').encode()
  if token not in data[start:end]:
   extra=(' xmlns'+(':'+prefix if prefix else '')+'="'+uri+'"').encode();data=data[:end]+extra+data[end:];end+=len(extra)
 return data


def docx_run_plan(root, operations):
 """Compose disjoint original-text ranges once, including shared Word runs."""
 plan={}
 for target,value in operations:
  for index,edit in enumerate(target.locator['edits']):
   path=tuple(edit['path']);original=edit['original'];node=resolve_path(root,list(path))
   if (node.text or '')!=original:raise ValueError('TARGET_CHANGED')
   start=edit.get('start',len(edit['prefix']));end=edit.get('end',len(original)-len(edit['suffix']))
   if type(start) is not int or type(end) is not int or not 0<=start<=end<=len(original):raise ValueError('TARGET_BOUNDS')
   if original[:start]!=edit['prefix'] or original[end:]!=edit['suffix']:raise ValueError('TARGET_CHANGED')
   item=plan.setdefault(path,{'original':original,'ranges':[]})
   for left,right,_ in item['ranges']:
    if max(left,start)<min(right,end) or (start==end and left<=start<=right) or (left==right and start<=left<=end):
     raise ValueError('OVERLAPPING_TARGETS')
   item['ranges'].append((start,end,value if index==0 else ''))
 for item in plan.values():
  value=item['original']
  for start,end,replacement in sorted(item['ranges'],reverse=True):value=value[:start]+replacement+value[end:]
  item['expected']=value
 return plan


def apply_docx_runs(root, operations):
 # Work from right to left: inserting w:br/w:t siblings must not invalidate
 # another original text path in the same Word run.
 for path,item in sorted(docx_run_plan(root,operations).items(),reverse=True):
  set_word_text(root,list(path),item['expected'])


def set_word_text(root,path,value):
 """Preserve the run's formatting and encode answer line breaks as OOXML."""
 node=resolve_path(root,path);parent=resolve_path(root,path[:-1])
 set_word_text_at(parent,node,value)


def set_word_text_at(parent,node,value):
 pieces=value.replace('\r\n','\n').replace('\r','\n').split('\n')
 node.text=pieces[0];node.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
 index=list(parent).index(node)+1
 for piece in pieces[1:]:
  parent.insert(index,E.Element(W+'br'));index+=1
  following=E.Element(W+'t',dict(node.attrib));following.text=piece
  parent.insert(index,following);index+=1


def word_text(node):
 return ''.join('\n' if n.tag==W+'br' else '\t' if n.tag==W+'tab' else n.text or ''
                for n in node.iter() if n.tag in (W+'t',W+'br',W+'tab'))


def fill_docx(root, target, value):
 loc=target.locator;node=resolve_path(root,loc['path'])
 if loc['type']=='docx_legacy':
  if loc.get('checkbox'):
   checkbox=node.find(W+'checkBox');check=checkbox.find(W+'checked')
   if check is None:check=E.SubElement(checkbox,W+'checked')
   check.set(W+'val','1' if value=='checked' else '0')
   value='☒' if value=='checked' else '☐'
  for i,path in sorted(enumerate(loc['text_paths']),key=lambda pair:pair[1],reverse=True):set_word_text(root,path,value if i==0 else '')
 elif loc['type']=='docx_checkbox':
  from .write import set_docx_value
  set_docx_value(node,value,True)
 elif loc['type']=='docx_runs':
  apply_docx_runs(root,[(target,value)])
 elif loc['type']=='docx_text':
  if (node.text or '')!=loc['original']:raise ValueError('TARGET_CHANGED')
  set_word_text(root,loc['path'],loc['prefix']+value+loc['suffix'])
 else:
  existing=''.join(n.text or '' for n in node.iter(W+'t')).strip()
  if existing and existing!=target.constraints.get('placeholder','').strip():raise ValueError('TARGET_NOT_EMPTY')
  for n in node.iter(W+'t'):n.text=''
  paragraph=node if node.tag==W+'p' else node.find('.//'+W+'p')
  if paragraph is None:paragraph=E.SubElement(node,W+'p')
  existing_style=paragraph.find('.//'+W+'rPr')
  run=E.SubElement(paragraph,W+'r')
  if existing_style is not None:run.append(copy.deepcopy(existing_style))
  set_word_text_at(run,E.SubElement(run,W+'t'),value)


def node_path(root,node):
 from .documents import node_paths
 return node_paths(root)[id(node)]


def _number(value, default=None):
 try:
  value=float(value)
  return value if math.isfinite(value) else default
 except (ValueError,TypeError):return default


def _word_font_size(root, styles, path):
 """Resolve explicit run, paragraph/style and document-default point sizes."""
 nodes=[resolve_path(root,path[:index]) for index in range(len(path)+1)]
 run=next((n for n in reversed(nodes) if n.tag==W+'r'),None)
 paragraph=next((n for n in reversed(nodes) if n.tag==W+'p'),None)
 if paragraph is None:paragraph=nodes[-1].find('.//'+W+'p')
 props=[]
 if run is not None:props.append(run.find(W+'rPr'))
 elif paragraph is not None:props.append(paragraph.find('.//'+W+'rPr'))
 if paragraph is not None:props.append(paragraph.find(W+'pPr/'+W+'rPr'))
 style_map={n.get(W+'styleId'):n for n in styles.findall(W+'style')} if styles is not None else {}
 def inherit(identity):
  seen=set()
  while identity and identity not in seen and identity in style_map:
   seen.add(identity);style=style_map[identity];props.append(style.find(W+'rPr'))
   base=style.find(W+'basedOn');identity=base.get(W+'val') if base is not None else None
 for prop in list(props[:1]):
  child=prop.find(W+'rStyle') if prop is not None else None
  if child is not None:inherit(child.get(W+'val'))
 pstyle=paragraph.find(W+'pPr/'+W+'pStyle') if paragraph is not None else None
 if pstyle is not None:inherit(pstyle.get(W+'val'))
 else:
  default=next((n for n in style_map.values() if n.get(W+'type')=='paragraph' and n.get(W+'default') in ('1','true')),None)
  if default is not None:inherit(default.get(W+'styleId'))
 if styles is not None:props.append(styles.find(W+'docDefaults/'+W+'rPrDefault/'+W+'rPr'))
 for prop in props:
  if prop is None:continue
  size=prop.find(W+'sz')
  if size is None:size=prop.find(W+'szCs')
  value=_number(size.get(W+'val')) if size is not None else None
  if value is not None and value>0:return value/2
 return None


def _docx_budget(root,styles,target):
 path=target.locator['path'];nodes=[resolve_path(root,path[:i]) for i in range(len(path)+1)]
 budget=dict(space_type='flow',suggested_max_chars=None,max_lines=None,native_max_length=None,
             basis='word_natural_reflow')
 field=nodes[-1].find(W+'textInput/'+W+'maxLength') if target.locator.get('type')=='docx_legacy' else None
 length=_number(field.get(W+'val')) if field is not None else None
 if length is not None and length>0:
  budget.update(space_type='explicit_limit',native_max_length=int(length),suggested_max_chars=int(length),basis='word_textinput_max_length')
 cell=next((n for n in reversed(nodes) if n.tag==W+'tc'),None)
 row=next((n for n in reversed(nodes) if n.tag==W+'tr'),None)
 table=next((n for n in reversed(nodes) if n.tag==W+'tbl'),None)
 height=row.find(W+'trPr/'+W+'trHeight') if row is not None else None
 if height is None or height.get(W+'hRule')!='exact':return budget
 budget.update(space_type='explicit_limit' if budget['native_max_length'] else 'fixed',basis='word_exact_row_hint_render_required')
 height=_number(height.get(W+'val'))
 width_node=cell.find(W+'tcPr/'+W+'tcW') if cell is not None else None
 width=_number(width_node.get(W+'w')) if width_node is not None and width_node.get(W+'type','dxa')=='dxa' else None
 if width is None and table is not None and cell is not None:
  grid=[_number(n.get(W+'w')) for n in table.findall(W+'tblGrid/'+W+'gridCol')]
  offset=0
  for sibling in row.findall(W+'tc'):
   span=sibling.find(W+'tcPr/'+W+'gridSpan');count=int(span.get(W+'val','1')) if span is not None else 1
   if sibling is cell:
    widths=grid[offset:offset+count]
    if len(widths)==count and all(v is not None for v in widths):width=sum(widths)
    break
   offset+=count
 font_path=(target.locator.get('edits') or [{}])[0].get('path',path)
 fontsize=_word_font_size(root,styles,font_path)
 if width is None or height is None or fontsize is None:return budget
 def margin(side,default):
  node=cell.find(W+'tcPr/'+W+'tcMar/'+W+side) if cell is not None else None
  if node is None and table is not None:node=table.find(W+'tblPr/'+W+'tblCellMar/'+W+side)
  return _number(node.get(W+'w'),default) if node is not None and node.get(W+'type','dxa')=='dxa' else default
 available_width=max(0,(width-margin('left',108)-margin('right',108))/20)
 available_height=max(0,(height-margin('top',0)-margin('bottom',0))/20)
 # Hint in full-width characters: actual Word fonts and wrapping are checked by
 # LibreOffice, never rejected by this conservative approximation.
 lines=max(1,int(available_height/(fontsize*1.3)))
 chars=max(1,int(available_width/fontsize)*lines)
 if budget['native_max_length']:chars=min(chars,budget['native_max_length'])
 budget.update(suggested_max_chars=chars,max_lines=lines,font_size=fontsize,
               width=round(available_width,2),height=round(available_height,2),single_line=lines==1)
 return budget


def xlsx_text_layout(root,address,value,styles=None):
 """Shared Excel wrapping heuristic; rows grow to fit, up to Excel's limit.

 Column widths are summed individually for merged ranges. Font metrics remain
 an estimate and final output still undergoes real rendered verification.
 """
 from .documents import column_number
 col=column_number(address);rowno=re.search(r'\d+',address)[0]
 row=root.find(S+'sheetData/'+S+'row[@r="'+rowno+'"]')
 cell=row.find(S+'c[@r="'+address+'"]') if row is not None else None
 font_size=11.;font_known=False
 if styles is not None:
  xfs=styles.find(S+'cellXfs');fonts=styles.find(S+'fonts')
  try:
   xf=xfs[int(cell.get('s','0')) if cell is not None else 0]
   font=fonts[int(xf.get('fontId','0'))];size=font.find(S+'sz');size=_number(size.get('val')) if size is not None else None
   if size is not None and size>0:font_size=size;font_known=True
  except (IndexError,TypeError,ValueError):pass
 end=col
 for merge in root.findall(S+'mergeCells/'+S+'mergeCell'):
  parts=merge.get('ref','').split(':')
  if len(parts)==2 and parts[0]==address:
   if re.search(r'\d+',parts[1])[0]!=rowno:
    return dict(width=0,height=0,overflow=True,reason='MERGED_ROW_REVIEW',font_size=font_size,font_known=font_known)
   end=column_number(parts[1]);break
 sheet_format=root.find(S+'sheetFormatPr')
 default_width=_number(sheet_format.get('defaultColWidth'),8.43) if sheet_format is not None else 8.43
 width=0
 for index in range(col,end+1):
  column_width=default_width
  for column in root.findall(S+'cols/'+S+'col'):
   if int(column.get('min','1'))<=index<=int(column.get('max','1')):
    column_width=0 if column.get('hidden') in ('1','true') else _number(column.get('width'),default_width)
  width+=(column_width*7+5)*.75 if column_width>0 else 0
 available=max(width-5,1)
 # Approximate individual line widths instead of counting newline twice.
 lines=sum(max(1,math.ceil(sum((1.0 if ord(c)>255 else .55)*font_size for c in line)/available))
           for line in value.replace('\r\n','\n').replace('\r','\n').split('\n'))
 required=lines*font_size*1.35
 default_height=_number(sheet_format.get('defaultRowHeight'),15) if sheet_format is not None else 15
 current=_number(row.get('ht'),default_height) if row is not None else default_height
 height=max(current,required)
 return dict(width=round(width,2),height=height,overflow=height>409 or width<=5,
             reason='TEXT_OVERFLOW' if height>409 or width<=5 else '',font_size=font_size,font_known=font_known)


def office_answer_budgets(path,document):
 """Expose native Office limits and conservative, nonbinding layout hints."""
 budgets={}
 with ZipFile(path) as archive:
  if document.kind=='docx':
   root=xml(archive.read('word/document.xml'))
   styles=xml(archive.read('word/styles.xml')) if 'word/styles.xml' in archive.namelist() else None
   for target in document.targets:budgets[target.id]=_docx_budget(root,styles,target)
  elif document.kind=='xlsx':
   styles=xml(archive.read('xl/styles.xml')) if 'xl/styles.xml' in archive.namelist() else None
   roots={}
   for target in document.targets:
    loc=target.locator
    root=roots.setdefault(loc['part'],xml(archive.read(loc['part'])))
    layout=xlsx_text_layout(root,loc['cell'],'',styles)
    unsupported=layout['reason']=='MERGED_ROW_REVIEW'
    font=layout['font_size'];lines=max(1,int(409/(font*1.35)))
    budgets[target.id]=dict(space_type='unknown' if unsupported else 'flow',
     suggested_max_chars=None if unsupported else max(1,int(max(layout['width']-5,1)/font)*lines),
     max_lines=None if unsupported else lines,native_max_length=None,
     basis='xlsx_merged_rows_unsupported' if unsupported else 'xlsx_growing_row_hint_render_required',
     width=layout['width'],font_size=font)
 return budgets


def write_office(source, destination, kind, operations):
 kind=kind.kind if hasattr(kind,'kind') else kind
 results=[]
 with ZipFile(source) as z:
  updates={};roots={};run_operations=[]
  for qid,t,value in operations:
   part='word/document.xml' if kind=='docx' else t.locator['part']
   root=roots.setdefault(part,xml(z.read(part)))
   if kind=='docx':
    # Try the original row without changing its height. Actual rendered readback
    # below removes only answers that do not fit; short values remain fillable.
    path=t.locator['path'];ancestors=[resolve_path(root,path[:i]) for i in range(len(path)+1)]
    # Enforce the native field constraint at the writer boundary too: reviewed
    # CLI apply answers do not pass through autonomous generation's decoder.
    if t.locator['type']=='docx_legacy':
     limit=ancestors[-1].find(W+'textInput/'+W+'maxLength')
     maximum=_number(limit.get(W+'val')) if limit is not None else None
     if maximum is not None and maximum>0 and len(value)>maximum:
      results.append(WriteResult(qid,t.id,'layout_blocked',value,None,'NATIVE_MAX_LENGTH'));continue
    fixed=any(n.find('.//'+W+'trHeight[@'+W+'hRule="exact"]') is not None for n in ancestors if n.tag==W+'tr')
    if t.locator['type']=='docx_runs':run_operations.append((t,value))
    else:fill_docx(root,t,value)
   else:
    address=t.locator['cell'];rowno=re.search(r'\d+',address)[0]
    row=root.find(S+'sheetData/'+S+'row[@r="'+rowno+'"]')
    if row is None:raise ValueError('TARGET_BOUNDS')
    cell=row.find(S+'c[@r="'+address+'"]')
    if cell is not None and (cell.find(S+'f') is not None or cell_text(cell,[])):raise ValueError('TARGET_NOT_EMPTY')
    # A wrapping estimate controls row growth, never whether text may be filled.
    # Above Excel's row limit, retain the original font and let real rendering
    # decide whether narrow glyphs fit or the answer needs compaction.
    styles=roots.setdefault('xl/styles.xml',xml(z.read('xl/styles.xml')))
    layout=xlsx_text_layout(root,address,value,styles)
    if layout['reason']=='MERGED_ROW_REVIEW':raise ValueError('MERGED_ROW_REVIEW')
    if layout['width']<=5:
     results.append(WriteResult(qid,t.id,'layout_blocked',value,None,'AREA_TOO_SMALL'));continue
    if cell is None:cell=E.SubElement(row,S+'c',{'r':address})
    xfs=styles.find(S+'cellXfs');xf=copy.deepcopy(xfs[int(cell.get('s','0'))]);align=xf.find(S+'alignment')
    if align is None:align=E.SubElement(xf,S+'alignment')
    align.set('wrapText','1');align.set('vertical','top');xf.set('applyAlignment','1')
    cell.set('s',str(len(xfs)));xfs.append(xf);xfs.set('count',str(len(xfs)))
    row.set('ht',str(min(409,layout['height'])));row.set('customHeight','1');cell.set('t','inlineStr')
    for child in list(cell):cell.remove(child)
    E.SubElement(E.SubElement(cell,S+'is'),S+'t').text=value
   # Both overestimates and underestimates must reach the renderer: narrow
   # glyphs can fit at the cap while wide glyphs can clip below the estimate.
   reason=('FIXED_ROW_HEIGHT_CANDIDATE' if fixed else '') if kind=='docx' else 'XLSX_ROW_HEIGHT_CANDIDATE'
   results.append(WriteResult(qid,t.id,'pending_readback',value,None,reason))
  if run_operations:apply_docx_runs(roots['word/document.xml'],run_operations)
  updates={part:serialize(root,z.read(part)) for part,root in roots.items()}
  with ZipFile(destination,'w') as dest:
   for info in z.infolist():dest.writestr(copy.copy(info),updates.get(info.filename,z.read(info.filename)))
 return results


def canonical(n):
 return (n.tag,sorted(n.attrib.items()),n.text or '',[canonical(c) for c in n])


def verify_docx_structure(original,output,q,results):
 """Allow exactly the planned text/break/control changes, including shared runs.

 Paths are resolved against the original before new w:br siblings are inserted.
 Comparing the permitted tree also checks that styles, fields and all other
 original children have stayed intact.
 """
 targets={t.id:t for t in q.targets}
 with ZipFile(original) as a,ZipFile(output) as b:
  if set(a.namelist())!=set(b.namelist()):return results,False
  source=xml(a.read('word/document.xml'));expected=copy.deepcopy(source);actual=xml(b.read('word/document.xml'));runs=[]
  pending=[r for r in results if r.status=='pending_readback']
  for result in pending:
   target=targets[result.target_id]
   if target.locator['type']=='docx_runs':runs.append((target,result.expected_value))
   else:fill_docx(expected,target,result.expected_value)
  if runs:apply_docx_runs(expected,runs)
  for result in pending:
   loc=targets[result.target_id].locator;path=loc['path']
   if loc['type']=='docx_text':path=path[:-1]
   elif loc['type']=='docx_legacy':
    paths=[loc['path']]+loc['text_paths'];path=[]
    for items in zip(*paths):
     if len(set(items))!=1:break
     path.append(items[0])
   try:matches=canonical(resolve_path(actual,path))==canonical(resolve_path(expected,path))
   except ValueError:matches=False
   result.readback_value=result.expected_value if matches else None
   result.status='written' if matches else 'readback_failed'
  preserved=canonical(expected)==canonical(actual) and all(a.read(n)==b.read(n) for n in a.namelist() if n!='word/document.xml')
  return results,bool(preserved and all(r.status!='readback_failed' for r in results))


def verify_structure(original, output, q, results):
 if q.kind=='docx':return verify_docx_structure(original,output,q,results)
 targets={t.id:t for t in q.targets};changed={}
 with ZipFile(original) as a,ZipFile(output) as b:
  if set(a.namelist())!=set(b.namelist()):return results,False
  for r in results:
   if r.status!='pending_readback':continue
   t=targets[r.target_id];loc=t.locator;part=loc['part']
   if part not in changed:changed[part]=(xml(a.read(part)),xml(b.read(part)))
   before,after=changed[part]
   addr=loc['cell'];rn=re.search(r'\d+',addr)[0];xp=S+'sheetData/'+S+'row[@r="'+rn+'"]'
   oldrow=before.find(xp);newrow=after.find(xp);old=oldrow.find(S+'c[@r="'+addr+'"]');new=newrow.find(S+'c[@r="'+addr+'"]')
   value=cell_text(new,[]);expected=r.expected_value;r.readback_value=value
   if new is not None:
    index=list(newrow).index(new);newrow.remove(new)
    if old is not None:newrow.insert(index,copy.deepcopy(old))
   for attr in ('ht','customHeight'):
    newrow.attrib.pop(attr,None)
    if attr in oldrow.attrib:newrow.set(attr,oldrow.get(attr))
   r.status='written' if value==expected else 'readback_failed'
  ok=all(canonical(x)==canonical(y) for x,y in changed.values())
  allowed=set(changed)
  if q.kind=='xlsx' and changed:
   old=xml(a.read('xl/styles.xml'));new=xml(b.read('xl/styles.xml'));oldx=old.find(S+'cellXfs');newx=new.find(S+'cellXfs')
   if len(newx)<len(oldx):ok=False
   for child in list(newx)[len(oldx):]:newx.remove(child)
   newx.set('count',str(len(oldx)));ok &= canonical(old)==canonical(new);allowed.add('xl/styles.xml')
  ok &= all(a.read(n)==b.read(n) for n in a.namelist() if n not in allowed)
 return results,bool(ok and all(r.status!='readback_failed' for r in results))


def overlapping_text(page):
 return bool(text_overlap_pairs(page))


def text_overlap_pairs(page):
 words=[w for w in page.get_text('words') if w[4].strip()]
 words.sort(key=lambda w:w[1]);pairs=Counter()
 for i,a in enumerate(words):
  ar=M.Rect(a[:4])
  for b in words[i+1:]:
   if b[1]>=a[3]:break
   br=M.Rect(b[:4]);overlap=(ar&br).get_area()
   if min(ar.get_area(),br.get_area()) and overlap>0.25*min(ar.get_area(),br.get_area()):pairs[tuple(sorted((a[4],b[4])))]+=1
 return pairs


def image_text_pairs(page):
 """Detect text obscuring embedded objects; full-page backgrounds are excluded."""
 pairs=Counter();words=page.get_text('words')
 for image in page.get_image_info(hashes=True):
  rect=M.Rect(image['bbox'])
  if rect.get_area()>.5*page.rect.get_area():continue
  identity=image.get('digest',b'').hex()
  for word in words:
   area=M.Rect(word[:4]);intersection=(area&rect).get_area()
   if area.get_area() and intersection>.25*area.get_area():pairs[(identity,word[4])]+=1
 return pairs


def rendered_counts(value,pages):
 normalized=''.join(value.split());content=''.join(''.join(page.split()) for page in pages)
 return content.count(normalized) if normalized else 0


def docx_target_context(root,target):
 loc=target.locator;path=loc['path'];node=resolve_path(root,path)
 if loc['type']=='docx_runs':return word_text(node)
 if loc['type']!='docx_path':return None
 for length in range(len(path)+1):
  cell=resolve_path(root,path[:length])
  if cell.tag!=W+'tc':continue
  row=resolve_path(root,path[:length-1]);cells=list(row);index=cells.index(cell)
  previous=[word_text(item) for item in cells[:index] if item.tag==W+'tc' and word_text(item).strip()]
  if previous:return previous[-1]+word_text(cell)
 return None


def rendered_text_present(value, pages):
 """Allow an exact text run to continue on the immediately following page.

 Only original whitespace boundaries may split; both literal fragments must
 occur near the page boundary. No fuzzy matching or arbitrary token skipping.
 """
 normalize=lambda text:''.join(text.split())
 normalized=normalize(value);page_text=[normalize(page) for page in pages]
 if normalized in ''.join(page_text):return True
 for boundary in re.finditer(r'\s+',value):
  left,right=normalize(value[:boundary.start()]),normalize(value[boundary.end():])
  if min(len(left),len(right))<4:continue
  if any(left in a[-300:] and right in b[:300] for a,b in zip(page_text,page_text[1:])):return True
 return False


def verify_office(original, output, q, results, work, processor, _allow_fixed_retry=True, *, baseline=None,defer_fixed_overflow=False):
 results,structure=verify_structure(original,output,q,results)
 work=Path(work);work.mkdir(parents=True,exist_ok=True)
 # The caller may reuse the immutable original rendered earlier in this task.
 baseline=baseline if baseline is not None else processor.convert(original,'pdf',work/'before.pdf')
 filled=processor.convert(output,'pdf',work/'after.pdf')
 def text_of(path):
  with M.open(path) as doc:
   return [p.get_text() for p in doc],len(doc)
 before_pages,pages=text_of(baseline);after_pages,outpages=text_of(filled)
 before=''.join(''.join(v.split()) for v in before_pages);after=''.join(''.join(v.split()) for v in after_pages)
 normalize=lambda value:''.join(value.split())
 text_results=[r for r in results if r.status=='written' and r.expected_value not in ('checked','unchecked')]
 expected_counts=Counter(normalize(r.expected_value) for r in text_results)
 missing=[r.target_id for r in text_results if not rendered_text_present(r.expected_value,after_pages)
          or rendered_counts(r.expected_value,after_pages)<rendered_counts(r.expected_value,before_pages)+expected_counts[normalize(r.expected_value)]]
 # An answer in a shared Word paragraph must be visible together with its
 # original prefix/suffix, rather than only somewhere else in the document.
 if q.kind=='docx':
  with ZipFile(output) as z:render_tree=xml(z.read('word/document.xml'))
  targets={target.id:target for target in q.targets}
  for result in text_results:
   loc=targets[result.target_id].locator
   context=docx_target_context(render_tree,targets[result.target_id])
   if context and not rendered_text_present(context,after_pages):missing.append(result.target_id)
 missing=list(dict.fromkeys(missing))
 # Fixed Word rows and Excel's capped row-growth candidates are tested by the
 # real renderer. Remove only clipped values, then verify the remaining copy.
 candidate_codes={'FIXED_ROW_HEIGHT_CANDIDATE':'FIXED_ROW_HEIGHT_OVERFLOW','XLSX_ROW_HEIGHT_CANDIDATE':'TEXT_OVERFLOW'}
 blocked=[r for r in results if r.target_id in missing and r.reason in candidate_codes]
 if blocked and structure and defer_fixed_overflow:
  for result in blocked:result.status='layout_blocked';result.reason=candidate_codes[result.reason];result.readback_value=None
 elif blocked and structure and _allow_fixed_retry:
  targets={t.id:t for t in q.targets};excluded={r.target_id for r in blocked}
  remaining=[(r.question_id,targets[r.target_id],r.expected_value) for r in results if r.status=='written' and r.target_id not in excluded]
  omitted=[r for r in results if r.status!='written' and r.target_id not in excluded]
  retry=work/('without-overflow.'+q.kind);retry_results=write_office(original,retry,q,remaining);retry.replace(output)
  for result in blocked:result.status='layout_blocked';result.reason=candidate_codes[result.reason];result.readback_value=None
  return verify_office(original,output,q,retry_results+omitted+blocked,work/'recheck',processor,False,baseline=baseline)
 # Every original text run must remain visible after natural repagination.
 original_text=[];retained_fragments=[]
 with ZipFile(original) as z:
  if q.kind=='docx':
   protected=xml(z.read('word/document.xml'));targets={t.id:t for t in q.targets};run_operations=[]
   for result in results:
    if result.status!='written':continue
    loc=targets[result.target_id].locator
    if loc['type']=='docx_runs':
     run_operations.append((targets[result.target_id],''))
    elif loc['type']=='docx_text':
     resolve_path(protected,loc['path']).text=loc['prefix'];retained_fragments.append(loc['suffix'])
    elif loc['type']=='docx_path':
     for node in resolve_path(protected,loc['path']).iter(W+'t'):node.text=''
    elif loc['type']=='docx_legacy':
     for path in loc['text_paths']:resolve_path(protected,path).text=''
   # Preserve each untouched substring separately; joining across an answer slot
   # would require the original label and suffix to remain adjacent after filling.
   for path,item in docx_run_plan(protected,run_operations).items():
    offset=0
    for start,end,_ in sorted(item['ranges']):
     retained_fragments.append(item['original'][offset:start]);offset=end
    retained_fragments.append(item['original'][offset:]);resolve_path(protected,list(path)).text=''
   original_text=[n.text for n in protected.iter(W+'t') if n.text and not re.fullmatch(r'[_\s☐☒]+',n.text)]
  else:
   from .documents import sheet_parts
   strings=[''.join(n.itertext()) for n in xml(z.read('xl/sharedStrings.xml'))] if 'xl/sharedStrings.xml' in z.namelist() else []
   original_text=[cell_text(c,strings) for _,part in sheet_parts(z) for c in xml(z.read(part)).iter(S+'c') if c.find(S+'f') is None]
 original_text.extend(retained_fragments)
 protected_values={normalize(v):v for v in original_text if normalize(v) and rendered_text_present(v,before_pages)}
 protected_counts=Counter(normalize(v) for v in original_text if normalize(v) in protected_values)
 preserved=all(rendered_text_present(protected_values[v],after_pages) and
               max(rendered_counts(v,after_pages),1 if rendered_text_present(protected_values[v],after_pages) else 0)>=
               min(count,max(rendered_counts(v,before_pages),1)) for v,count in protected_counts.items())
 # Original hidden text / cells outside a print area stay protected by OOXML
 # comparison, but are not required to become visible in the rendered copy.
 coverage=all(rendered_text_present(v,before_pages) for v in original_text if normalize(v))
 with M.open(baseline) as initial,M.open(filled) as rendered:
  old_overlap=sum((text_overlap_pairs(page) for page in initial),Counter());new_overlap=sum((text_overlap_pairs(page) for page in rendered),Counter())
  old_objects=sum((image_text_pairs(page) for page in initial),Counter());new_objects=sum((image_text_pairs(page) for page in rendered),Counter())
  overlap=bool(new_overlap-old_overlap);object_overlap=bool(new_objects-old_objects)
 checkbox_failures=verify_checkboxes(output,filled,q,results,work,processor)
 for result in results:
  if result.target_id in checkbox_failures:result.status='layout_blocked';result.reason='复选框实际显示未通过校验，未交付填写文件。'
  elif result.status=='written' and result.reason in candidate_codes:result.reason=''
 passed=structure and not missing and preserved and not overlap and not object_overlap and not checkbox_failures
 return results,Verification(structure,'passed' if passed else 'needs_review',not passed,
  {'structure_preserved':structure,'renderer':'LibreOffice isolated','baseline_pages':pages,'output_pages':outpages,
   'checkbox_visual_failures':checkbox_failures,'visible_answers_missing':missing,'original_visible_text_preserved':preserved,'baseline_text_coverage':coverage,'text_overlap':overlap,
   'baseline_text_overlap_pairs':sum(old_overlap.values()),'new_text_overlap_pairs':sum((new_overlap-old_overlap).values()),'new_image_text_overlap':object_overlap,
   'checks':['OOXML non-target preservation','rendered answer occurrence and target context','visible original text preservation','baseline overlap comparison'],
   'limits':'Embedded image overlap checked; vector/background objects and cross-application pagination still require visual review.'})


def checkbox_visual_matches(actual, inverse, expected):
 """A single toggled checkbox must change ink in one small, stable page region."""
 from PIL import Image, ImageChops
 changed=[];delta=0
 with M.open(actual) as a,M.open(inverse) as b:
  if len(a)!=len(b):return False
  for pa,pb in zip(a,b):
   if pa.rect!=pb.rect:return False
   ap=pa.get_pixmap(matrix=M.Matrix(2,2),colorspace=M.csGRAY,alpha=False)
   bp=pb.get_pixmap(matrix=M.Matrix(2,2),colorspace=M.csGRAY,alpha=False)
   ai=Image.frombytes('L',(ap.width,ap.height),ap.samples);bi=Image.frombytes('L',(bp.width,bp.height),bp.samples)
   box=ImageChops.difference(ai,bi).point(lambda x:255 if x>20 else 0).getbbox()
   if box:
    if box[2]-box[0]>48 or box[3]-box[1]>48:return False
    changed.append(box);delta+=sum(bi.crop(box).get_flattened_data())-sum(ai.crop(box).get_flattened_data())
 return len(changed)==1 and (delta>255 if expected=='checked' else delta < -255)


def occupied_ranges(values):
 start=last=None;result=[]
 for index,value in enumerate(values):
  if not value:continue
  if last is not None and index-last>3:result.append((start,last+1));start=None
  if start is None:start=index
  last=index
 if start is not None:result.append((start,last+1))
 return result


def checkbox_change_regions(actual,inverse):
 """Find individual changed marks after one batched inverse rendering."""
 from PIL import Image,ImageChops
 regions=[]
 with M.open(actual) as a,M.open(inverse) as b:
  if len(a)!=len(b):return None
  for index,(pa,pb) in enumerate(zip(a,b)):
   if pa.rect!=pb.rect:return None
   images=[]
   for page in (pa,pb):
    pix=page.get_pixmap(matrix=M.Matrix(2,2),colorspace=M.csGRAY,alpha=False)
    images.append(Image.frombytes('L',(pix.width,pix.height),pix.samples))
   first,second=images;diff=ImageChops.difference(first,second).point(lambda value:255 if value>20 else 0)
   for top,bottom in occupied_ranges(diff.getprojection()[1]):
    strip=diff.crop((0,top,diff.width,bottom))
    for left,right in occupied_ranges(strip.getprojection()[0]):
     bounds=(left,top,right,bottom)
     if right-left>48 or bottom-top>48:return None
     delta=sum(second.crop(bounds).get_flattened_data())-sum(first.crop(bounds).get_flattened_data())
     regions.append({'page':index,'rect':[coordinate/2 for coordinate in bounds],'delta':delta})
 return regions


def checkbox_anchors(root,target,question):
 path=target.locator['path'];paragraph=None
 for length in range(len(path)+1):
  node=resolve_path(root,path[:length])
  if node.tag==W+'p':paragraph=node
 if paragraph is None:paragraph=resolve_path(root,path).find('.//'+W+'p')
 values=[word_text(paragraph) if paragraph is not None else '',question.text]
 return list(dict.fromkeys(re.sub('[☐☒]','',value).strip() for value in values if re.sub('[☐☒]','',value).strip()))


def batch_checkbox_matches(output,rendered,q,results,work,processor):
 targets={t.id:t for t in q.targets};questions={item.id:item for item in q.questions}
 probe=Path(work)/'checkbox-batch.docx';pdf=Path(work)/'checkbox-batch.pdf'
 try:
  operations=[(r.question_id,targets[r.target_id],'unchecked' if r.expected_value=='checked' else 'checked') for r in results]
  write_office(output,probe,q,operations);processor.convert(probe,'pdf',pdf)
  regions=checkbox_change_regions(rendered,pdf)
  if regions is None:return {}
  with ZipFile(output) as z:root=xml(z.read('word/document.xml'))
  candidates={}
  with M.open(rendered) as doc:
   for result in results:
    anchors=checkbox_anchors(root,targets[result.target_id],questions[result.question_id]);found=set()
    for page_index,page in enumerate(doc):
     for anchor in anchors:
      for bounds in page.search_for(anchor):
       expanded=M.Rect(bounds.x0-35,bounds.y0-8,bounds.x1+35,bounds.y1+8)
       nearby=[]
       for index,region in enumerate(regions):
        area=M.Rect(region['rect'])
        if region['page']!=page_index or not expanded.intersects(area):continue
        distance=4*abs((area.y0+area.y1-bounds.y0-bounds.y1)/2)+max(bounds.x0-area.x1,area.x0-bounds.x1,0)
        nearby.append((distance,index))
       nearby.sort()
       if nearby and (len(nearby)==1 or nearby[1][0]-nearby[0][0]>2):found.add(nearby[0][1])
    if len(found)==1:candidates[result.target_id]=next(iter(found))
  counts=Counter(candidates.values());matched={}
  for result in results:
   index=candidates.get(result.target_id)
   if index is None or counts[index]!=1:continue
   delta=regions[index]['delta'];matched[result.target_id]=delta>255 if result.expected_value=='checked' else delta < -255
  return matched
 finally:probe.unlink(missing_ok=True);pdf.unlink(missing_ok=True)


def verify_checkboxes(output, rendered, q, results, work, processor):
 failures=[];targets={t.id:t for t in q.targets}
 checks=[r for r in results if r.status=='written' and r.expected_value in ('checked','unchecked')]
 matched=batch_checkbox_matches(output,rendered,q,checks,work,processor) if len(checks)>1 else {}
 for index,result in enumerate(checks):
  if result.target_id in matched:
   if not matched[result.target_id]:failures.append(result.target_id)
   continue
  probe=Path(work)/f'checkbox-{index}.docx';pdf=Path(work)/f'checkbox-{index}.pdf'
  target=targets[result.target_id];inverse='unchecked' if result.expected_value=='checked' else 'checked'
  try:
   write_office(output,probe,q,[(result.question_id,target,inverse)])
   processor.convert(probe,'pdf',pdf)
   if not checkbox_visual_matches(rendered,pdf,result.expected_value):failures.append(result.target_id)
  finally:probe.unlink(missing_ok=True);pdf.unlink(missing_ok=True)
 return failures

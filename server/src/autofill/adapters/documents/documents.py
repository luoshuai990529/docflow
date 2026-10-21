"""Bounded native readers, local OCR, and structural target discovery."""
from collections import defaultdict
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import pymupdf as M
from ...domain.models import DocumentData, SourceSpan, Question, Target
from .preflight import Limits, inspect_file, inspect_sources, decode_text, digest, xml
from ..processor.processor import Processor
from .conversion import normalize_office, pdf_text
from .extract import NS, text, unique_form_names

S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
W = '{' + NS['w'] + '}'


def prose_choice(label):
    """Use explicit question types and printed options; never infer an answer."""
    if re.search(r'【判断题】|\[判断题\]', label):
        yes = re.search(r'正确\s*打\s*([√✓✔])', label)
        no = re.search(r'错误\s*打\s*([×✗✘])', label)
        return 'boolean', [yes[1], no[1]] if yes and no else ['正确', '错误']
    kind = 'multiple_choice' if re.search(r'【多选题】|\[多选题\]', label) else 'single_choice' if re.search(r'【单选题】|\[单选题\]', label) else None
    if kind:
        options = re.findall(r'(?:^|\s)([A-Z])[.．、)）]\s*\S', label)
        if len(options) >= 2 and options == list('ABCDEFGHIJKLMNOPQRSTUVWXYZ'[:len(options)]):
            return kind, options
    return 'text', []


def node_paths(root):
    result = {}
    def walk(node, path):
        result[id(node)] = path
        for i, child in enumerate(node):
            walk(child, path + [i])
    walk(root, [])
    return result


def resolve_path(root, path):
    node = root
    if not isinstance(path, (list, tuple)) or len(path) > 100:
        raise ValueError('TARGET_BOUNDS')
    for i in path:
        if not isinstance(i, int) or not 0 <= i < len(node):
            raise ValueError('TARGET_BOUNDS')
        node = node[i]
    return node


def sheet_parts(z):
    workbook = xml(z.read('xl/workbook.xml'))
    rels = {n.get('Id'):n.get('Target') for n in xml(z.read('xl/_rels/workbook.xml.rels'))}
    result = []
    for sheet in workbook.findall(S+'sheets/'+S+'sheet'):
        target = rels[sheet.get(R+'id')]
        part = str(PurePosixPath(target.lstrip('/'))) if target.startswith('/') else str(PurePosixPath('xl') / target)
        if '..' in PurePosixPath(part).parts or part not in z.namelist():
            raise ValueError('DOCUMENT_CORRUPT')
        result.append((sheet.get('name'), part))
    return result


def cell_text(cell, strings):
    if cell is None:
        return ''
    value = cell.findtext(S+'v', '')
    if cell.get('t') == 's':
        return strings[int(value)] if value else ''
    if cell.get('t') == 'inlineStr':
        return ''.join(n.text or '' for n in cell.findall('.//'+S+'t'))
    return value


def column_number(address):
    n=0
    for letter in re.match(r'[A-Z]+',address)[0]:
        n=n*26+ord(letter)-64
    return n


def column_name(n):
    result=''
    while n:
        n,r=divmod(n-1,26);result=chr(65+r)+result
    return result


class Reader:
    def __init__(self, path, role, work, processor, limits):
        self.path=Path(path);self.role=role;self.work=Path(work);self.work.mkdir(parents=True,exist_ok=True)
        self.processor=processor or Processor();self.limits=limits
        self.info=inspect_file(path,role,limits)
        self.doc=DocumentData(self.info['format'],self.info['sha256'],[],[],[],
                              {'coverage':[], 'discovery':'structural-v2', 'original_name':self.path.name})
        self.characters=0

    def span(self, value, locator):
        if not value or not value.strip():return
        self.characters+=len(value)
        if self.characters>self.limits.max_text_chars:raise ValueError('TEXT_LIMIT')
        start=0
        while start<len(value):
            end=min(start+2000,len(value))
            if end<len(value):
                boundaries=list(re.finditer(r'[。！？；\n]|[.!?;](?=\s|$)',value[start:end]))
                choices=[start+match.end() for match in boundaries if match.end()>=1000]
                if choices:end=choices[-1]
            loc=dict(locator,source_name=self.path.name)
            if len(value)>2000:loc['offset']=start
            self.doc.spans.append(SourceSpan('s'+str(len(self.doc.spans)+1),loc,value[start:end],self.doc.sha256))
            if end==len(value):break
            # Raw overlapping excerpts keep qualifiers visible at batch boundaries.
            overlap=value[max(start,end-200):end]
            boundaries=list(re.finditer(r'[。！？；\n]|[.!?;](?=\s|$)',overlap))
            next_start=end-150
            if boundaries:next_start=end-len(overlap)+boundaries[0].end()
            start=max(start+1,min(next_start,end-1))

    def question(self, label, locator, constraints=None, kind='text', options=None):
        label=label.strip()
        if not label or len(label)>2000:return
        constraints=dict(constraints or {})
        if kind=='text' and locator['type'] in ('docx_path','docx_text','docx_runs','xlsx_cell','pdf_rect'):
            kind,options=prose_choice(label)
            if options:constraints['options_text']=True
        if len(self.doc.questions)>=self.limits.max_questions:raise ValueError('QUESTION_LIMIT')
        identity='q'+str(len(self.doc.questions)+1);target='t'+str(len(self.doc.targets)+1)
        self.doc.questions.append(Question(identity,label,kind,options or [],[target]))
        self.doc.targets.append(Target(target,locator,dict({'font_size':10,'min_font_size':9},**(constraints or {}))))

    def image_words(self, image, index):
        words=self.processor.ocr(image,self.work/f'ocr-{index}.json')
        if not words:raise ValueError('OCR_UNREADABLE')
        return words

    def pdf(self):
        from pypdf import PdfReader
        reader=PdfReader(self.path);unique_form_names(reader)
        fields=reader.get_fields() or {}
        with M.open(self.path) as doc:
            for page, coverage in zip(doc,self.info['pages']):
                lines=[]
                for block in page.get_text('dict')['blocks']:
                    for line in block.get('lines',[]):
                        value=''.join(s['text'] for s in line['spans'])
                        if value.strip():lines.append({'text':value,'bbox':list(line['bbox']),'confidence':100})
                if coverage['status']=='ocr_required':
                    image=self.work/f'page-{page.number}.png';scale=200/72
                    page.get_pixmap(matrix=M.Matrix(scale,scale),alpha=False).save(image)
                    words=self.image_words(image,page.number);groups=defaultdict(list)
                    for word in words:groups[tuple(word['line'])].append(word)
                    for group in groups.values():
                        bounds=M.Rect(group[0]['bbox'])
                        for word in group[1:]:bounds|=M.Rect(word['bbox'])
                        # OCR pixels are in rendered rotation space; return to unrotated PDF coordinates.
                        rect=(bounds/scale)*page.derotation_matrix
                        value=' '.join(w['text'] for w in group)
                        if any((rect & M.Rect(l['bbox'])).get_area()>.5*rect.get_area() for l in lines):continue
                        lines.append({'text':value,'bbox':list(rect),'confidence':min(w['confidence'] for w in group)})
                    image.unlink(missing_ok=True)
                    coverage=dict(coverage,status='ocr',low_confidence=sum(l['confidence']<65 for l in lines))
                for line in lines:self.span(line['text'],{'page_index':page.number,'bbox':line['bbox'],'confidence':line['confidence']})
                self.doc.metadata['coverage'].append(coverage)
                if self.role!='questionnaire':continue
                widgets=list(page.widgets() or [])
                if fields:
                    existing={t.locator.get('full_name') for t in self.doc.targets}
                    for widget in widgets:
                        name=widget.field_name
                        if name in existing or widget.field_value not in (None,'','Off'):continue
                        field=fields.get(name,{})
                        if field.get('/FT')=='/Sig':continue
                        options=[]
                        if field.get('/FT')=='/Btn':
                            options=[v for v in (widget.button_states() or {}).get('normal',[]) if v!='Off']
                        if field.get('/FT')=='/Ch':
                            options=[str(v[0] if isinstance(v,list) else v) for v in field.get('/Opt',[])]
                        constraints={}
                        if field.get('/FT')=='/Btn':
                            if len(options)==1:
                                constraints={'exports':{'Yes':'/'+options[0].lstrip('/'),'No':'/Off'}};options=['Yes','No']
                            else:constraints={'exports':{v:'/'+v.lstrip('/') for v in options}}
                        self.question(widget.field_label or name,{'type':'pdf_field','full_name':name},constraints,
                                      kind='single_choice' if options else 'text',options=options)
                        existing.add(name)
                # Form fields and printed answer lines can coexist, including on
                # different pages. Existing widgets reserve their whole area.
                self.pdf_targets(page,lines,[M.Rect(widget.rect) for widget in widgets])
        return self.doc

    def pdf_targets(self,page,lines,reserved=()):
        drawings=page.get_drawings();rules=[];rectangles=[]
        for drawing in drawings:
            for item in drawing['items']:
                if item[0]=='l' and abs(item[1].y-item[2].y)<1 and abs(item[1].x-item[2].x)>35:
                    rules.append(M.Rect(min(item[1].x,item[2].x),item[1].y-15,max(item[1].x,item[2].x),item[1].y+1))
                if item[0]=='re' and item[1].width>35 and item[1].height>12:rectangles.append(M.Rect(item[1]))
        occupied=[M.Rect(rect) for rect in reserved]
        for line in sorted(lines,key=lambda l:(l['bbox'][1],l['bbox'][0])):
            value=line['text'].strip();bounds=M.Rect(line['bbox'])
            if len(value)<2 or re.fullmatch(r'[\d\W]+',value):continue
            candidates=[r for r in rules+rectangles if r.x0>=bounds.x1-2 and r.x0-bounds.x1<100 and abs(r.y1-bounds.y1)<25]
            if candidates:
                rect=min(candidates,key=lambda r:abs(r.x0-bounds.x1)+abs(r.y1-bounds.y1))
                confirm=False
            elif value.endswith((':','：','?','？')) and bounds.x1<page.rect.width-100:
                rect=M.Rect(bounds.x1+5,bounds.y0,page.rect.width-25,bounds.y0+max(18,bounds.height+3));confirm=True
            else:continue
            rect=M.Rect(rect.x0+2,rect.y0,rect.x1-2,rect.y1)
            if not (page.rect*page.derotation_matrix).contains(rect):continue
            if any((rect & M.Rect(other['bbox'])).get_area()>2 for other in lines if other is not line):continue
            if any((rect & previous).get_area()>2 for previous in occupied):continue
            occupied.append(rect)
            self.question(value,{'type':'pdf_rect','page_index':page.number,'rect':list(rect),'coordinate_space':'unrotated_pymupdf'},
                          {'position_review':confirm or line['confidence']<65})

    def docx(self):
        with ZipFile(self.path) as z:
            main=xml(z.read('word/document.xml'));paths=node_paths(main)
            if any(main.findall('.//'+W+n) for n in ('ins','del','altChunk','object')):raise ValueError('UNSUPPORTED_DOCUMENT_STRUCTURE')
            for part in z.namelist():
                if part=='word/document.xml' or re.fullmatch(r'word/(header\d+|footer\d+|footnotes|endnotes)\.xml',part):
                    root=main if part=='word/document.xml' else xml(z.read(part))
                    for index,p in enumerate(root.findall('.//'+W+'p')):self.span(text(p),{'part':part,'paragraph':index})
                    self.doc.metadata['coverage'].append({'part':part,'status':'text'})
                elif part.startswith('word/media/'):
                    suffix=Path(part).suffix.lower()
                    if suffix not in ('.png','.jpg','.jpeg'):raise ValueError('UNSUPPORTED_EMBEDDED_IMAGE')
                    image=self.work/Path(part).name;image.write_bytes(z.read(part))
                    words=self.image_words(image,'docx-'+Path(part).stem)
                    self.span(' '.join(w['text'] for w in words),{'part':part,'ocr':True,'confidence':min(w['confidence'] for w in words)})
                    self.doc.metadata['coverage'].append({'part':part,'status':'ocr'})
            if self.role!='questionnaire':return self.doc
            owned=set()
            from .forms import legacy_fields
            for label,loc,kind,options,review in legacy_fields(main):
                constraints={'position_review':review}
                if loc['checkbox']:constraints['exports']={'Yes':'checked','No':'unchecked'}
                self.question(label,loc,constraints,kind,options)
                owned.update(id(resolve_path(main,path)) for path in loc['text_paths'])
            if self.info.get('forms_only'):return self.doc
            for table in main.findall('.//'+W+'tbl'):
                for row in table.findall(W+'tr'):
                    cells=row.findall(W+'tc')
                    for i,cell in enumerate(cells):
                        if i==0 or text(cell).strip() or cell.find('.//'+W+'sdt') is not None or cell.find('.//'+W+'fldChar') is not None:continue
                        label=' / '.join(text(c).strip() for c in cells[:i] if text(c).strip())
                        if not label:continue
                        self.question(label,{'type':'docx_path','path':paths[id(cell)]})
                        owned.update(id(n) for n in cell.iter())
            for sdt in main.findall('.//'+W+'sdt'):
                if sdt.find(W+'sdtPr/'+W+'lock') is not None:continue
                content=sdt.find(W+'sdtContent')
                if content is None:continue
                labelnode=sdt.find(W+'sdtPr/'+W+'alias')
                if labelnode is None:labelnode=sdt.find(W+'sdtPr/'+W+'tag')
                if labelnode is None:continue
                label=labelnode.get(W+'val','')
                checkbox=sdt.find(W+'sdtPr/{'+NS['w14']+'}checkbox')
                if checkbox is not None:
                    checked=checkbox.find('{'+NS['w14']+'}checked')
                    if checked is not None and checked.get('{'+NS['w14']+'}val')=='1':continue
                    self.question(label,{'type':'docx_checkbox','path':paths[id(sdt)]},{'exports':{'Yes':'checked','No':'unchecked'}},'boolean',['Yes','No'])
                    owned.update(id(n) for n in sdt.iter());continue
                placeholder=sdt.find(W+'sdtPr/'+W+'showingPlcHdr') is not None
                if text(content).strip() and not placeholder:continue
                self.question(label,{'type':'docx_path','path':paths[id(content)]},{'placeholder':text(content)})
                owned.update(id(n) for n in content.iter())
            parents={id(child):parent for parent in main.iter() for child in parent}
            for paragraph in main.iter(W+'p'):
                nodes=list(paragraph.iter(W+'t'))
                if not nodes or any(id(n) in owned for n in nodes):continue
                # Do not edit fields, drawings, or paragraphs containing nested paragraphs.
                if any(paragraph.find('.//'+W+n) is not None for n in ('fldChar','fldSimple','sdt','drawing','pict','p')):continue
                value=''.join(n.text or '' for n in nodes)
                blanks=list(re.finditer(r'_{3,}|＿{3,}',value))
                marker=re.search(r'(?:你的作答|您的作答|作答|答案)\s*[：:]\s*$',value)
                if blanks:
                    slots=[blank.span() for blank in blanks]
                elif not blanks and marker and value[:marker.start()].strip():
                    # A following answer paragraph is already populated: preserve it.
                    siblings=list(parents[id(paragraph)]);index=siblings.index(paragraph)
                    following=siblings[index+1] if index+1<len(siblings) else None
                    if following is not None and following.tag==W+'p' and text(following).strip():
                        if not re.search(r'【(?:判断|多选|单选|简答)题】',text(following)):continue
                    slots=[(len(value),len(value))]
                else:continue
                for slot,(start,end) in enumerate(slots):
                    label=value[:start].strip()
                    if len(slots)>1:
                        previous=slots[slot-1][1] if slot else 0
                        nearby=value[previous:start].strip(' \t\n，,；;。') or value[end:slots[slot+1][0] if slot+1<len(slots) else len(value)].strip()
                        label=value+'\n填写位置：第 '+str(slot+1)+' 个空（'+nearby+'）'
                    if not label:continue
                    edits=[];offset=0
                    for index,node in enumerate(nodes):
                        original=node.text or '';stop=offset+len(original)
                        if (offset<end and stop>start) or (start==end and index==len(nodes)-1):
                            left=max(0,start-offset);right=max(0,min(len(original),end-offset))
                            edits.append({'path':paths[id(node)],'original':original,'prefix':original[:left],'suffix':original[right:],
                                          'start':left,'end':right})
                        offset=stop
                    if edits:self.question(label,{'type':'docx_runs','path':paths[id(paragraph)],'edits':edits},
                                           {'blank_index':slot+1,'text_start':start,'text_end':end})
        return self.doc

    def xlsx(self):
        with ZipFile(self.path) as z:
            parts=sheet_parts(z)
            if len(parts)>self.limits.max_sheets:raise ValueError('SHEET_LIMIT')
            strings=[]
            if 'xl/sharedStrings.xml' in z.namelist():
                strings=[''.join(t.text or '' for t in n.iter(S+'t')) for n in xml(z.read('xl/sharedStrings.xml'))]
            # Inventory all image/drawing evidence, including images outside cell ranges.
            for part in z.namelist():
                if part.startswith('xl/media/'):
                    if Path(part).suffix.lower() not in ('.png','.jpg','.jpeg'):raise ValueError('UNSUPPORTED_EMBEDDED_IMAGE')
                    image=self.work/Path(part).name;image.write_bytes(z.read(part))
                    inspect_file(image,'source',self.limits)
                    words=self.image_words(image,'xlsx-'+Path(part).stem)
                    self.span(' '.join(w['text'] for w in words),{'part':part,'ocr':True,'confidence':min(w['confidence'] for w in words)})
                    self.doc.metadata['coverage'].append({'part':part,'status':'ocr'})
                elif part.startswith('xl/drawings/') and part.endswith('.xml'):
                    drawing=xml(z.read(part))
                    for i,node in enumerate(drawing.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}p')):
                        self.span(''.join(n.text or '' for n in node.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}t')),{'part':part,'paragraph':i})
                    self.doc.metadata['coverage'].append({'part':part,'status':'drawing_text'})
                elif part.startswith('xl/drawings/') and part.endswith('.vml'):
                    raise ValueError('UNSUPPORTED_DOCUMENT_STRUCTURE')
            count=0
            for name,part in parts:
                root=xml(z.read(part));cells={c.get('r'):c for c in root.findall('.//'+S+'c')};count+=len(cells)
                if count>self.limits.max_cells:raise ValueError('CELL_LIMIT')
                rows=defaultdict(dict)
                for address,cell in cells.items():
                    value=cell_text(cell,strings);row=int(re.search(r'\d+',address)[0]);col=column_number(address)
                    rows[row][col]=(address,cell,value)
                    formula=cell.findtext(S+'f')
                    if formula is not None and not value and self.role=='source':raise ValueError('FORMULA_VALUE_UNAVAILABLE')
                    self.span(value,{'sheet':name,'cell':address,'formula':formula})
                self.doc.metadata['coverage'].append({'sheet':name,'status':'cells','cells':len(cells)})
                if self.role!='questionnaire':continue
                maxcol=max((max(r) for r in rows.values()),default=1)
                formatted_max=max((int(c.get('max','1')) for c in root.findall(S+'cols/'+S+'col')),default=1)
                maxcol=max(maxcol,min(formatted_max,maxcol+1))
                for row,values in rows.items():
                    for col,(address,cell,value) in sorted(values.items()):
                        if not value or cell.find(S+'f') is not None:continue
                        nextaddr=column_name(col+1)+str(row);nextcell=cells.get(nextaddr)
                        if col>=maxcol or (nextcell is not None and (cell_text(nextcell,strings) or nextcell.find(S+'f') is not None)):continue
                        # A merged answer range is addressed by its top-left cell only.
                        merged=[n.get('ref') for n in root.findall(S+'mergeCells/'+S+'mergeCell')]
                        def interior(ref):
                            first,last=ref.split(':');r1=int(re.search(r'\d+',first)[0]);r2=int(re.search(r'\d+',last)[0])
                            return first!=nextaddr and r1<=row<=r2 and column_number(first)<=col+1<=column_number(last)
                        if any(interior(ref) for ref in merged):continue
                        options=[]
                        for validation in root.findall(S+'dataValidations/'+S+'dataValidation'):
                            if nextaddr in validation.get('sqref','').split() and validation.get('type')=='list':
                                formula=validation.findtext(S+'formula1','')
                                if formula.startswith('"') and formula.endswith('"'):options=next(csv.reader([formula[1:-1]]))
                        self.question(value,{'type':'xlsx_cell','sheet':name,'part':part,'cell':nextaddr},kind='single_choice' if options else 'text',options=options)
        return self.doc

    def plain(self):
        value=decode_text(self.path.read_bytes())
        if self.doc.kind=='csv':
            try:dialect=csv.Sniffer().sniff(value[:8192],delimiters=',;\t|')
            except csv.Error:dialect=csv.excel
            rows=csv.reader(io.StringIO(value),dialect);headers=next(rows,[])
            for index,row in enumerate(rows,2):
                self.span('; '.join(f'{headers[i] if i<len(headers) else i+1}: {v}' for i,v in enumerate(row)),{'row':index})
        else:
            for index,line in enumerate(value.splitlines(),1):self.span(line,{'line':index})
        self.doc.metadata['coverage']=[{'part':'text','status':'read'}]
        return self.doc

    def image(self):
        words=self.image_words(self.path,'source')
        self.span(' '.join(w['text'] for w in words),{'ocr':True,'confidence':min(w['confidence'] for w in words)})
        self.doc.metadata['coverage']=[{'part':'image','status':'ocr'}]
        return self.doc


def read_document(path, role, work, processor=None, limits=Limits()):
    reader=Reader(path,role,work,processor,limits)
    method={'pdf':'pdf','docx':'docx','xlsx':'xlsx','csv':'plain','txt':'plain','md':'plain','markdown':'plain',
            'png':'image','jpg':'image','jpeg':'image'}.get(reader.doc.kind)
    if method is None:raise ValueError('CONVERSION_REQUIRED')
    result=getattr(reader,method)()
    if not result.spans:raise ValueError('NO_READABLE_TEXT')
    return result


def read_sources(paths, work, processor=None, limits=Limits(), progress=None):
    inspect_sources(paths,limits);spans=[];coverage=[];hashes=[]
    for index,path in enumerate(paths):
        if progress:progress('reading',phase='sources',completed=index,total=len(paths))
        original=Path(path);folder=Path(work)/str(index);folder.mkdir(parents=True,exist_ok=True)
        if original.suffix.lower() in ('.doc','.xls'):
            path=normalize_office(original,folder,processor or Processor(),limits,role='source')[0]
        doc=read_document(path,'source',folder,processor,limits);hashes.append(digest(original))
        from .source_context import enrich_source
        enrich_source(doc,path)
        for span in doc.spans:
            span.id=f'd{index+1}-{span.id}';span.locator['source_name']=original.name;spans.append(span)
        coverage.append({'file':original.name,'sha256':hashes[-1],'parts':doc.metadata['coverage']})
    if progress:progress('reading',phase='sources',completed=len(paths),total=len(paths))
    if sum(len(s.text) for s in spans)>limits.max_text_chars:raise ValueError('TEXT_LIMIT')
    return DocumentData('collection',hashlib.sha256(json.dumps(hashes).encode()).hexdigest(),spans,[],[],{'coverage':coverage})

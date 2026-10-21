"""Legacy Office normalization and preservation checks, independent of readers."""
from pathlib import Path
from collections import defaultdict
import hashlib
import pymupdf as M
from .preflight import inspect_file, Limits, xml

ODF_OFFICE='{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
ODF_TABLE='{urn:oasis:names:tc:opendocument:xmlns:table:1.0}'
ODF_TEXT='{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
ODF_FORM='{urn:oasis:names:tc:opendocument:xmlns:form:1.0}'


def audit_signature(file,limits=Limits()):
    """Compare logical content, not ODF's physical compression of empty cells."""
    if file.stat().st_size>limits.max_expanded_bytes:raise ValueError('EXPANDED_LIMIT')
    root=xml(file.read_bytes());body=root.find(ODF_OFFICE+'body')
    if body is None:raise ValueError('DOCUMENT_CORRUPT')
    parents={id(child):parent for parent in root.iter() for child in parent}
    def text_nodes(scope):
        return (node for node in scope.iter() if node.tag in (ODF_TEXT+'p',ODF_TEXT+'h'))
    def paragraph_content(node):
        # A page-number's cached display may differ after a fresh import even
        # when both PDFs show identical computed page numbers. Retain the field
        # type/attributes, not its stale display cache; images are hashed below.
        parts=[];pending=[]
        def flush():
            value=''.join(''.join(pending).split());pending.clear()
            if value:parts.append(('text',value))
        def visit(item):
            if item.tag==ODF_OFFICE+'binary-data':return
            if item.tag in (ODF_TEXT+'page-number',ODF_TEXT+'page-count'):
                flush();parts.append(('field',item.tag,tuple(sorted(item.attrib.items()))));return
            if item.text:pending.append(item.text)
            for child in item:
                visit(child)
                if child.tail:pending.append(child.tail)
        visit(node);flush();return tuple(parts)
    def outside_table(node):
        while id(node) in parents:
            node=parents[id(node)]
            if node.tag==ODF_TABLE+'table':return False
        return True
    # Headers/footers live in master styles outside office:body; they are also
    # readable evidence and must survive, together with heading text.
    paragraphs=tuple(value for node in text_nodes(root) if outside_table(node)
                     if (value:=paragraph_content(node)))
    formulas=[];images=[];controls=[];tables=[]
    for node in root.iter():
        formula=node.get(ODF_TABLE+'formula')
        if formula and node.tag!=ODF_TABLE+'table-cell':formulas.append(formula)
        if node.tag==ODF_OFFICE+'binary-data':images.append(hashlib.sha256(''.join((node.text or '').split()).encode()).hexdigest())
        if node.tag.startswith(ODF_FORM) and node.tag.rsplit('}',1)[-1] in ('checkbox','textarea','text','listbox','combobox'):
            controls.append((node.tag,tuple(sorted((key,value) for key,value in node.attrib.items() if key!=ODF_FORM+'id'))))
    def repeats(node,kind):
        count=int(node.get(ODF_TABLE+'number-'+kind+'-repeated','1'))
        if count<1:raise ValueError('DOCUMENT_CORRUPT')
        return count
    def rows(table):
        for child in table:
            if child.tag==ODF_TABLE+'table-row':yield child
            elif child.tag not in (ODF_TABLE+'table',ODF_TABLE+'table-cell',ODF_TABLE+'covered-table-cell'):yield from rows(child)
    spreadsheet=body.find(ODF_OFFICE+'spreadsheet') is not None
    for table in root.iter(ODF_TABLE+'table'):
        row_index=0;logical_rows=[]
        for row in rows(table):
            row_repeat=repeats(row,'rows');column=0;cells=[]
            for cell in row:
                if cell.tag not in (ODF_TABLE+'table-cell',ODF_TABLE+'covered-table-cell'):continue
                column_repeat=repeats(cell,'columns')
                text=tuple(value for node in text_nodes(cell) if (value:=paragraph_content(node)))
                attrs=tuple(sorted((key,value) for key,value in cell.attrib.items()
                    if key.startswith(ODF_OFFICE) or key in (ODF_TABLE+'formula',ODF_TABLE+'number-columns-spanned',ODF_TABLE+'number-rows-spanned')))
                if text or any(key!=ODF_OFFICE+'value-type' for key,_ in attrs):
                    value=(text,attrs)
                    if cells and cells[-1][1]==column and cells[-1][2]==value:cells[-1]=(cells[-1][0],column+column_repeat,value)
                    else:cells.append((column,column+column_repeat,value))
                column+=column_repeat
            if cells:
                value=tuple(cells)
                if logical_rows and logical_rows[-1][1]==row_index and logical_rows[-1][2]==value:logical_rows[-1]=(logical_rows[-1][0],row_index+row_repeat,value)
                else:logical_rows.append((row_index,row_index+row_repeat,value))
            row_index+=row_repeat
        # Generated Writer table names may change; spreadsheet names are addresses.
        tables.append((table.get(ODF_TABLE+'name') if spreadsheet else None,tuple(logical_rows)))
    return tuple(tables),paragraphs,tuple(formulas),tuple(images),tuple(controls)

def pdf_text(path):
    with M.open(path) as d:return ''.join(''.join(p.get_text(sort=True).split()) for p in d)


def normalize_office(path, work, processor, limits=Limits(),role='questionnaire'):
    path=Path(path);work=Path(work);work.mkdir(parents=True,exist_ok=True)
    info=inspect_file(path,'source',limits)
    if info['format'] not in ('doc','xls'):return path,{'kind':'original','converted':False}
    fmt='docx' if info['format']=='doc' else 'xlsx'
    normalized=processor.convert(path,fmt,work/('normalized.'+fmt))
    inspect_file(normalized,'source',limits)
    audit_format='fodt' if fmt=='docx' else 'fods'
    left_audit=processor.convert(path,audit_format,work/('original.'+audit_format))
    right_audit=processor.convert(normalized,audit_format,work/('normalized.'+audit_format))
    if audit_signature(left_audit,limits)!=audit_signature(right_audit,limits):raise ValueError('CONVERSION_STRUCTURE_LOSS')
    # Source documents supply evidence; natural reflow is irrelevant to their
    # correctness. The audit above also protects text outside PDF print areas.
    if role=='source':return normalized,{'kind':f'{info["format"]}_to_{fmt}','converted':True,
                       'source_format':info['format'],'target_format':fmt,'content_verified':True,'structure_verified':True,'layout_check':'not_required_for_source'}
    original_pdf=processor.convert(path,'pdf',work/'original.pdf')
    normalized_pdf=processor.convert(normalized,'pdf',work/'normalized.pdf')
    before,after=pdf_text(original_pdf),pdf_text(normalized_pdf)
    if before!=after:raise ValueError('CONVERSION_CONTENT_LOSS')
    with M.open(original_pdf) as a,M.open(normalized_pdf) as b:
        if len(a)!=len(b):raise ValueError('CONVERSION_LAYOUT_REVIEW')
        for left,right in zip(a,b):
            if left.rect!=right.rect:raise ValueError('CONVERSION_LAYOUT_REVIEW')
            def characters(page):
                result=defaultdict(list)
                for block in page.get_text('rawdict')['blocks']:
                    for line in block.get('lines',[]):
                        for span in line['spans']:
                            for char in span['chars']:
                                if not char['c'].isspace():result[char['c']].append(char['bbox'])
                return result
            la,ra=characters(left),characters(right)
            if set(la)!=set(ra):raise ValueError('CONVERSION_LAYOUT_REVIEW')
            for char,positions in la.items():
                available=list(ra[char])
                if len(positions)!=len(available):raise ValueError('CONVERSION_LAYOUT_REVIEW')
                for bounds in positions:
                    closest=min(range(len(available)),key=lambda i:sum(abs(a-b) for a,b in zip(bounds,available[i])))
                    other=available.pop(closest)
                    if max(abs(a-b) for a,b in zip(bounds,other))>5:raise ValueError('CONVERSION_LAYOUT_REVIEW')
            from .verify import pixel_diff
            masks=[M.Rect(line['bbox']) for page in (left,right) for block in page.get_text('dict')['blocks'] for line in block.get('lines',[])]
            _,outside=pixel_diff(left,right,masks,scale=1)
            if outside<0 or outside>left.rect.get_area()*.005:raise ValueError('CONVERSION_LAYOUT_REVIEW')
    return normalized,{'kind':f'{info["format"]}_to_{fmt}','converted':True,
                       'source_format':info['format'],'target_format':fmt,'content_verified':True,'structure_verified':True}

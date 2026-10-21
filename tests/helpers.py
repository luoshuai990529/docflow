from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib
import pymupdf as M
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def pdf(path, text='Company: SAMPLE CO', rotation=0, crop=False, form=False):
 d=M.open();p=d.new_page(width=400,height=500);p.insert_text((30,40),text)
 if crop:p.set_cropbox(M.Rect(10,10,390,490))
 if form:
  for name,rect,kind in [('company',M.Rect(30,80,300,105),M.PDF_WIDGET_TYPE_TEXT),('approved',M.Rect(30,120,45,135),M.PDF_WIDGET_TYPE_CHECKBOX)]:
   w=M.Widget();w.field_name=name;w.field_type=kind;w.rect=rect;p.add_widget(w)
 p.set_rotation(rotation);d.save(path);d.close();return Path(path)
def docx(path, duplicate=False):
 row='<w:tr><w:tc><w:p><w:r><w:t>Company</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t></w:t></w:r></w:p></w:tc></w:tr>'
 xml=f'''<w:document xmlns:w="{W}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:body><w:p><w:r><w:t>Supplier data</w:t></w:r></w:p><w:tbl><w:tblPr><w:tblCaption w:val="Basics"/></w:tblPr><w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="4000"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>Field</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>{row}{row if duplicate else ''}</w:tbl><w:sdt><w:sdtPr><w:tag w:val="note"/><w:id w:val="101"/></w:sdtPr><w:sdtContent><w:p><w:r><w:rPr><w:sz w:val="20"/></w:rPr><w:t></w:t></w:r></w:p></w:sdtContent></w:sdt><w:sdt><w:sdtPr><w:tag w:val="flag"/><w14:checkbox><w14:checked w14:val="0"/></w14:checkbox></w:sdtPr><w:sdtContent><w:p><w:r><w:t>☐</w:t></w:r></w:p></w:sdtContent></w:sdt><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'''
 with ZipFile(path,'w',ZIP_DEFLATED) as z:
  z.writestr('word/document.xml',xml);z.writestr('word/styles.xml','<styles/>');z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
 return Path(path)
def template(path, targets=None, questions=None):
 return {'schema_version':1,'questionnaire_sha256':sha(path),'questions':questions or [{'id':'company','text':'Company name','kind':'text','options':[],'target_ids':['company']}],'targets':targets or [{'id':'company','locator':{'type':'pdf_rect','page_index':0,'rect':[30,80,300,105],'coordinate_space':'unrotated_pymupdf'},'constraints':{'font_size':10,'min_font_size':9}}]}

def interpret_with_local_structure(document, path, client, cancelled=lambda: False, progress=None):
    from autofill.adapters.documents.questionnaire_structure import LocalQuestionnaireStructure
    from autofill.application.answering.questionnaire import interpret_questionnaire

    return interpret_questionnaire(
        document,
        path,
        client,
        cancelled,
        progress,
        structure=LocalQuestionnaireStructure(),
    )

def write_candidate(source_path, output_path, questionnaire, decisions, mode):
    from autofill.adapters.documents.answer_layout import annotate_layout
    from autofill.adapters.documents.write import write_copy
    from autofill.domain.write_plan import build_write_plan

    source_path = Path(source_path)
    if questionnaire.kind == 'pdf' and any(
        'answer_budget' not in target.constraints
        for target in questionnaire.targets
        if target.constraints.get('content_type') != 'checkbox'
    ):
        annotate_layout(source_path, questionnaire)
    plan = build_write_plan(questionnaire, decisions, mode)
    return write_copy(source_path, output_path, plan)

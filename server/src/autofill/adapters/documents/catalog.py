"""Internal exact-input registry; no arbitrary questionnaire inference."""
from pathlib import Path
import json,hashlib
import pymupdf as M
from ...application.ports import ProcessorPort
from .extract import sha
from ..processor.processor import Processor
PDF_HASH='cbc5890a1231cd79c70c99df3fe2a8885525d8a6156ac201f0d1ebb9bdd8f5b0'
DOC_HASH='80540f15fe88339272fdeb920bf2541604bc09712a3d1d0453cfbae21a635832'
TEMPLATES=Path(__file__).resolve().parents[2]/'templates'
def render_signature(path):
 digest=hashlib.sha256()
 with M.open(path) as doc:
  for page in doc:
   pix=page.get_pixmap(matrix=M.Matrix(1.5,1.5),alpha=False)
   digest.update(str((pix.width,pix.height,pix.n)).encode());digest.update(pix.samples)
 return digest.hexdigest()
def convert_doc(path,work,processor:ProcessorPort):
 work=Path(work);work.mkdir(parents=True,exist_ok=True)
 return processor.convert(path,'pdf',work/(Path(path).stem+'.pdf'))

def resolve_questionnaire(path,work,processor:ProcessorPort|None=None):
 path=Path(path);digest=sha(path);work=Path(work)
 if digest==PDF_HASH:
  if path.suffix.lower()!='.pdf':raise ValueError('FORMAT_EXTENSION_MISMATCH')
  return path,TEMPLATES/'supplier-pdf.json','original_pdf'
 if digest!=DOC_HASH:raise ValueError('UNSUPPORTED_QUESTIONNAIRE: no internal mapping for this exact file')
 if path.suffix.lower()!='.doc' or path.read_bytes()[:8]!=bytes.fromhex('d0cf11e0a1b11ae1'):raise ValueError('FORMAT_EXTENSION_MISMATCH')
 template=json.loads((TEMPLATES/'product-doc.json').read_text())
 converted=convert_doc(path,work/'converted',processor or Processor())
 if render_signature(converted)!=template['render_signature']:raise ValueError('DOC_LAYOUT_CHANGED: converted layout does not match reviewed mapping')
 template['questionnaire_sha256']=sha(converted);target=work/'internal-template.json';target.write_text(json.dumps(template,ensure_ascii=False))
 return converted,target,'legacy_doc_converted_to_3_page_pdf: output is PDF; Microsoft Word pagination not guaranteed; original DOC unchanged; reviewed render pixel signature passed'

"""Format capabilities and bounded inspection before any model request."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, is_zipfile
import hashlib
import xml.etree.ElementTree as ET
import pymupdf as M

QUESTION_FORMATS = {'pdf', 'doc', 'docx', 'xls', 'xlsx'}
SOURCE_FORMATS = QUESTION_FORMATS | {'csv', 'txt', 'md', 'markdown', 'png', 'jpg', 'jpeg'}


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 50 * 1024 * 1024
    max_source_bytes: int = 200 * 1024 * 1024
    max_sources: int = 20
    max_pages: int = 500
    max_source_pages: int = 2000
    max_questions: int = 2000
    max_expanded_bytes: int = 512 * 1024 * 1024
    max_xml_bytes: int = 32 * 1024 * 1024
    max_entries: int = 10000
    max_page_pixels: int = 40_000_000
    max_total_pixels: int = 2_000_000_000
    max_sheets: int = 100
    max_cells: int = 1_000_000
    max_text_chars: int = 5_000_000


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def xml(data):
    declaration_bytes = data.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in declaration_bytes or b'<!ENTITY' in declaration_bytes:
        raise ValueError('ACTIVE_CONTENT')
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError('DOCUMENT_CORRUPT') from e


def decode_text(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    if b'\0' in data:
        raise ValueError('FORMAT_EXTENSION_MISMATCH')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    raise ValueError('TEXT_ENCODING')


def inspect_file(path, role='source', limits=Limits()):
    path = Path(path)
    size = path.stat().st_size
    if not 0 < size <= limits.max_file_bytes:
        raise ValueError('UPLOAD_SIZE')
    suffix = path.suffix.lower().lstrip('.')
    if suffix not in (QUESTION_FORMATS if role == 'questionnaire' else SOURCE_FORMATS):
        raise ValueError('UNSUPPORTED_FORMAT')
    with path.open('rb') as stream:
        magic = stream.read(8)
    info = {'format': suffix, 'bytes': size, 'sha256': digest(path), 'pages': []}
    if magic.startswith(b'%PDF-'):
        if suffix != 'pdf':
            raise ValueError('FORMAT_EXTENSION_MISMATCH')
        with M.open(path) as doc:
            if doc.needs_pass:
                raise ValueError('PASSWORD_REQUIRED')
            if len(doc) > (limits.max_pages if role == 'questionnaire' else limits.max_source_pages):
                raise ValueError('PAGE_LIMIT')
            # Reject actions and existing signatures, not just a target signature field.
            for index in range(1, doc.xref_length()):
                obj = doc.xref_object(index)
                if any(token in obj for token in ('/JavaScript', '/OpenAction', '/ByteRange', '/EmbeddedFile', '/Launch', '/XFA')):
                    raise ValueError('ACTIVE_CONTENT')
            pixels = 0
            for page in doc:
                area = int(page.rect.width * page.rect.height * (200 / 72) ** 2)
                pixels += area
                if area > limits.max_page_pixels or pixels > limits.max_total_pixels:
                    raise ValueError('RENDER_LIMIT')
                words = page.get_text('words')
                images = page.get_image_info()
                # Any substantial raster area receives OCR even on a mixed text page.
                raster = any(M.Rect(i['bbox']).get_area() > page.rect.get_area() * .03 for i in images)
                drawings = bool(page.get_drawings())
                status = 'ocr_required' if raster or (not words and drawings) else 'text' if words else 'blank'
                info['pages'].append({'page': page.number, 'status': status,
                                      'width': page.rect.width, 'height': page.rect.height})
        return info
    if is_zipfile(path):
        with ZipFile(path) as z:
            entries = z.infolist()
            if len(entries) > limits.max_entries or sum(i.file_size for i in entries) > limits.max_expanded_bytes:
                raise ValueError('EXPANDED_LIMIT')
            names = z.namelist()
            if len(set(names)) != len(names):
                raise ValueError('DOCUMENT_CORRUPT')
            if 'word/document.xml' in names:
                actual = 'docx'
            elif 'xl/workbook.xml' in names:
                actual = 'xlsx'
            else:
                raise ValueError('FORMAT_EXTENSION_MISMATCH')
            if suffix != actual:
                raise ValueError('FORMAT_EXTENSION_MISMATCH')
            for item in entries:
                name = item.filename.lower()
                if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or item.flag_bits & 1:
                    raise ValueError('DOCUMENT_CORRUPT')
                if any(v in name for v in ('vbaproject', 'activex/', 'embeddings/', '_xmlsignatures/', 'externallinks/')):
                    raise ValueError('ACTIVE_CONTENT')
                if name.endswith(('.xml', '.rels')):
                    if item.file_size > limits.max_xml_bytes:
                        raise ValueError('EXPANDED_LIMIT')
                    root = xml(z.read(item))
                    for node in root.iter():
                        local = node.tag.rsplit('}', 1)[-1]
                        active = {'documentProtection': ('enforcement',), 'sheetProtection': ('sheet',),
                                  'workbookProtection': ('lockStructure', 'lockWindows')}.get(local, ())
                        if any(key.rsplit('}', 1)[-1] in active and value.lower() in ('1', 'true', 'on')
                               for key, value in node.attrib.items()):
                            if local=='documentProtection' and any(k.endswith('}edit') and v=='forms' for k,v in node.attrib.items()):
                                info['forms_only']=True
                            else:raise ValueError('DOCUMENT_PROTECTED')
                        if node.get('TargetMode') == 'External' and not node.get('Type', '').endswith('/hyperlink'):
                            raise ValueError('ACTIVE_CONTENT')
        return info
    if magic == bytes.fromhex('d0cf11e0a1b11ae1'):
        import olefile
        with olefile.OleFileIO(path) as ole:
            names = ['/'.join(n) for n in ole.listdir()]
            if any(any(v in n.lower() for v in ('vba', 'macros', 'objectpool', 'encryptedpackage')) for n in names):
                raise ValueError('ACTIVE_CONTENT')
            actual = 'doc' if 'WordDocument' in names else 'xls' if any(n in names for n in ('Workbook', 'Book')) else None
            if suffix != actual:
                raise ValueError('FORMAT_EXTENSION_MISMATCH')
        return info
    if suffix in ('png', 'jpg', 'jpeg'):
        expected = magic.startswith(b'\x89PNG') if suffix == 'png' else magic.startswith(b'\xff\xd8\xff')
        if not expected:
            raise ValueError('FORMAT_EXTENSION_MISMATCH')
        from PIL import Image
        with Image.open(path) as image:
            if image.width * image.height > limits.max_page_pixels:
                raise ValueError('RENDER_LIMIT')
            image.verify()
        return info
    if suffix in ('txt', 'md', 'markdown', 'csv'):
        content = decode_text(path.read_bytes())
        if len(content) > limits.max_text_chars:
            raise ValueError('TEXT_LIMIT')
        if not content.strip():
            raise ValueError('NO_READABLE_TEXT')
        return info
    raise ValueError('FORMAT_EXTENSION_MISMATCH')


def inspect_sources(paths, limits=Limits()):
    if not 1 <= len(paths) <= limits.max_sources:
        raise ValueError('SOURCE_COUNT')
    if sum(Path(p).stat().st_size for p in paths) > limits.max_source_bytes:
        raise ValueError('SOURCE_TOTAL')
    return [inspect_file(p, 'source', limits) for p in paths]


class LocalDocumentPreflight:
    """Application-facing facade over the bounded document inspection rules."""

    def __init__(self, limits=None):
        self.limits = limits or Limits()

    @property
    def max_upload_bytes(self):
        return self.limits.max_file_bytes

    def inspect_file(self, path, role='source'):
        return inspect_file(path, role, self.limits)

    def inspect_sources(self, paths):
        return inspect_sources(paths, self.limits)

    def digest(self, path):
        return digest(path)

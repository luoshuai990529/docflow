"""Semantic source context, separate from the native text used as evidence.

Markdown helps a model read tables; it is not a new source of facts or a write
locator. IDs and hashes stay native. Excel date/percentage display values are
decoded from stored values and formats, keeping the original value in metadata.
"""
from dataclasses import asdict, replace
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from pathlib import PurePosixPath
from zipfile import ZipFile
import re

from .preflight import xml

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def _short(value, limit=160):
    value = ' '.join(value.split())
    return value if len(value) <= limit else value[:limit] + '…'


def _text(node):
    return ''.join(n.text or '' for n in node.iter(W + 't'))


def _header_row(row):
    flag = row.find(W + 'trPr/' + W + 'tblHeader')
    return flag is not None and flag.get(W + 'val', '1').lower() not in ('0', 'false', 'off')


def _docx_context(doc, path):
    by_part = {}
    for span in doc.spans:
        loc = span.locator
        if 'paragraph' in loc:
            by_part.setdefault(loc.get('part'), {}).setdefault(loc['paragraph'], []).append(span)
    with ZipFile(path) as package:
        for part, spans in by_part.items():
            if part not in package.namelist():
                continue
            root = xml(package.read(part))
            parents = {id(child): parent for parent in root.iter() for child in parent}
            paragraphs = list(root.iter(W + 'p'))
            indices = {id(p): i for i, p in enumerate(paragraphs)}
            heading = ''
            for index, paragraph in enumerate(paragraphs):
                style = paragraph.find(W + 'pPr/' + W + 'pStyle')
                if style is not None and re.search(r'heading\s*\d|标题\s*\d', style.get(W + 'val', ''), re.I):
                    heading = _short(_text(paragraph))
                if heading:
                    for span in spans.get(index, []):
                        span.locator.setdefault('context', {})['heading'] = heading
            for table_index, table in enumerate(root.iter(W + 'tbl'), 1):
                rows = table.findall(W + 'tr')
                if not rows:
                    continue
                header_row = next((r for r in rows if _header_row(r)), rows[0])
                header_cells = [_short(_text(c)) for c in header_row.findall(W + 'tc')]
                explicit_header = _header_row(header_row)
                for row_index, row in enumerate(rows, 1):
                    cells = row.findall(W + 'tc')
                    # Store row text once, not once per cell in large tables.
                    row_context = [_short(_text(c)) for c in cells[:24]]
                    first = True
                    for cell_index, cell in enumerate(cells, 1):
                        for paragraph in cell.iter(W + 'p'):
                            ancestor = parents.get(id(paragraph))
                            while ancestor is not None and ancestor.tag != W + 'tc':
                                ancestor = parents.get(id(ancestor))
                            if ancestor is not cell:  # Nested tables have their own coordinates.
                                continue
                            for span in spans.get(indices[id(paragraph)], []):
                                context = span.locator.setdefault('context', {})
                                context.update(kind='table_cell', table=table_index, row=row_index, cell=cell_index)
                                if len(cells) == len(header_cells):
                                    context['column_label'] = header_cells[cell_index - 1]
                                    context['header_kind'] = 'explicit' if explicit_header else 'first_row_candidate'
                                if first:
                                    context['row_cells'] = row_context
                                    context['row_truncated'] = len(cells) > 24
                                    first = False


def _display_number(raw, number_format, epoch):
    """A deliberately small formatter; unsupported Excel syntax stays explicit."""
    from datetime import datetime
    from openpyxl.utils.datetime import from_excel

    fmt = number_format.lower()
    percent = re.fullmatch(r'0(?:\.(0*#*))?%', fmt)
    if percent:
        decimals = percent[1] or ''
        if len(decimals) > 10 or ('.' in fmt and not decimals):
            return None
        places = len(decimals)
        stored = Decimal(raw)
        if not stored.is_finite():
            return None
        value = (stored * 100).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        displayed = f'{value:.{places}f}'
        for _ in range(decimals.count('#')):
            if displayed.endswith('0'):
                displayed = displayed[:-1]
            else:
                break
        return displayed.rstrip('.') + '%', 'percentage'

    date_formats = {'mm-dd-yy', 'm/d/yy', 'mm/dd/yy', 'm/d/yyyy', 'mm/dd/yyyy',
                    'yyyy-mm-dd', 'yyyy/mm/dd', 'yyyy/m/d', 'dd/mm/yyyy', 'dd-mm-yyyy', 'd-mmm-yy'}
    # Strip only a known time suffix. Literal text, locale directives, conditions,
    # multi-section codes and ambiguous custom formats are not guessed.
    date_part, separator, time_part = fmt.partition(' ')
    has_date = date_part in date_formats
    clock = time_part if has_date and separator else fmt
    has_time = bool(re.fullmatch(r'hh?:mm(?::ss)?(?: am/pm)?|mm:ss', clock))
    if not has_date and not has_time and fmt not in ('d-mmm', 'mmm-yy', '[h]:mm:ss'):
        return None
    if has_date and separator and not has_time:
        return None
    serial = Decimal(raw)
    if not serial.is_finite():
        return None
    if fmt == '[h]:mm:ss':
        if abs(serial) > Decimal('1000000000'):
            return None
        seconds = int((serial * 86400).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        sign = '-' if seconds < 0 else ''
        hours, remainder = divmod(abs(seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{sign}{hours}:{minutes:02}:{seconds:02}', 'elapsed_time'
    if serial < 0 or serial > Decimal('2958466'):
        return None
    # Excel's fictitious 1900-02-29 has no truthful ISO date representation.
    if epoch.year == 1899 and int(serial) == 60:
        return None
    value = from_excel(float(serial), epoch=epoch)
    if has_date or fmt in ('d-mmm', 'mmm-yy'):
        if not isinstance(value, datetime) and epoch.year == 1904 and 0 <= serial < 1:
            value = datetime.combine(epoch.date(), value)
        if not isinstance(value, datetime):
            return None
        if fmt == 'd-mmm':
            months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
            return f'{value.day}-{months[value.month - 1]}', 'partial_date'
        if fmt == 'mmm-yy':
            return f'{value.year:04}-{value.month:02}', 'partial_date'
        date_text = value.date().isoformat()
        if not separator:
            return date_text, 'date'
    else:
        date_text = ''
    if clock == 'mm:ss':
        time_text = f'{value.minute:02}:{value.second:02}'
    else:
        # Canonical 24-hour clock preserves AM/PM meaning without locale guesses.
        time_text = f'{value.hour:02}:{value.minute:02}'
        if ':ss' in clock:
            time_text += f':{value.second:02}'
    return (date_text + ' ' + time_text).strip(), 'datetime' if has_date else 'time'


def _xlsx_values(doc, path):
    """Decode numeric source cells using only saved values; never evaluate formulas."""
    from openpyxl.styles.numbers import BUILTIN_FORMATS
    from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900

    spans = {(s.locator.get('sheet'), s.locator.get('cell')): s for s in doc.spans if s.locator.get('cell')}
    with ZipFile(path) as package:
        workbook = xml(package.read('xl/workbook.xml'))
        properties = workbook.find(S + 'workbookPr')
        epoch = CALENDAR_MAC_1904 if properties is not None and properties.get('date1904', '').lower() in ('1', 'true', 'on') else CALENDAR_WINDOWS_1900
        if 'xl/styles.xml' not in package.namelist():
            return
        styles = xml(package.read('xl/styles.xml'))
        formats = dict(BUILTIN_FORMATS)
        formats.update({int(n.get('numFmtId')): n.get('formatCode', '') for n in styles.findall(S + 'numFmts/' + S + 'numFmt')})
        cell_styles = [formats.get(int(n.get('numFmtId', '0')), '') for n in styles.findall(S + 'cellXfs/' + S + 'xf')]
        relationships = {n.get('Id'): n.get('Target') for n in xml(package.read('xl/_rels/workbook.xml.rels'))}
        for sheet in workbook.findall(S + 'sheets/' + S + 'sheet'):
            target = relationships[sheet.get(R + 'id')]
            part = str(PurePosixPath(target.lstrip('/'))) if target.startswith('/') else str(PurePosixPath('xl') / target)
            for cell in xml(package.read(part)).iter(S + 'c'):
                span = spans.get((sheet.get('name'), cell.get('r')))
                if span is None or cell.get('t', 'n') != 'n':
                    continue
                raw = cell.findtext(S + 'v')
                if not raw:
                    continue  # Missing formula caches are handled by the native reader.
                style = int(cell.get('s', '0'))
                number_format = cell_styles[style] if 0 <= style < len(cell_styles) else ''
                if number_format == 'General':
                    continue  # Ordinary numeric identifiers must never be guessed as dates.
                span.locator.update(raw_value=raw, number_format=number_format)
                if re.fullmatch(r'(?:0|#,##0)(?:\.0*#*)?', number_format):
                    # Decimal precision and thousands separators do not change
                    # the stored quantity. Keep exact precision, not rounding.
                    span.locator['format_status'] = 'raw_numeric'
                    continue
                try:
                    displayed = _display_number(raw, number_format, epoch)
                except (DecimalException, ValueError, OverflowError):
                    displayed = None
                if displayed:
                    span.text, conversion = displayed
                    span.locator.update(format_status='normalized', format_conversion=conversion)
                else:
                    span.locator['format_status'] = 'unsupported'


def _xlsx_context(doc):
    # Existing native locators already identify the sheet and cell. Reuse the
    # parsed spans rather than load the workbook a second time or recalculate it.
    sheets = {}
    for span in doc.spans:
        loc = span.locator
        match = re.fullmatch(r'([A-Z]+)(\d+)', loc.get('cell', ''))
        if match:
            sheets.setdefault(loc.get('sheet', ''), {}).setdefault(int(match[2]), []).append((match[1], span))
    for rows in sheets.values():
        first_row = rows[min(rows)]
        headers = {}
        for column, span in first_row:
            headers.setdefault(column, _short(span.text))
        for row_index, values in rows.items():
            # The source order is the workbook's native cell order; cell labels
            # retain explicit positions even for sparse or unsorted rows.
            row_cells = [(column, _short(span.text)) for column, span in values[:24]]
            for index, (column, span) in enumerate(values):
                context = span.locator.setdefault('context', {})
                context.update(kind='sheet_cell', row=row_index, column=column)
                if column in headers:
                    context.update(column_label=headers[column], header_kind='first_row_candidate')
                if index == 0:
                    context['row_cells'] = row_cells
                    context['row_truncated'] = len(values) > 24


def enrich_source(doc, path):
    """Add native structure and decode supported numeric source display values."""
    if doc.kind == 'docx':
        _docx_context(doc, path)
    elif doc.kind == 'xlsx':
        _xlsx_values(doc, path)
        _xlsx_context(doc)
    elif doc.kind in ('md', 'markdown'):
        # Explicit Markdown headings are source syntax, not a guessed topic.
        heading = ''
        for span in doc.spans:
            match = re.fullmatch(r' {0,3}#{1,6}\s+(.+?)\s*#*\s*', span.text)
            if match:
                heading = match[1]
            if heading:
                span.locator.setdefault('context', {})['heading'] = heading
    return doc

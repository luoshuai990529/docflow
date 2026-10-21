"""Safe model reading views built from native, hash-bound source spans."""
from dataclasses import asdict, replace
import re

def _inline(value):
    return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')


def _position(loc):
    values = []
    if 'page_index' in loc:
        values.append(f"页 {loc['page_index'] + 1}")
    if 'part' in loc:
        values.append(str(loc['part']))
    if 'paragraph' in loc:
        values.append(f"段落 {loc['paragraph'] + 1}")
    if 'sheet' in loc:
        values.append(f"工作表 {loc['sheet']}")
    if 'cell' in loc:
        values.append(f"单元格 {loc['cell']}")
    if 'line' in loc:
        values.append(f"行 {loc['line']}")
    if 'row' in loc:
        values.append(f"行 {loc['row']}")
    if loc.get('ocr'):
        values.append('OCR')
    if 'offset' in loc:
        values.append(f"字符偏移 {loc['offset']}")
    return ' · '.join(values) or '原文片段'


def source_markdown(spans):
    """Render a model/debug view; citations must still match raw span.text only."""
    lines = ['# 答案资料', '', '结构上下文仅辅助理解；引用必须使用片段 ID 和该片段的原文，不能引用合成表格或标题。', '']
    source = None
    heading = None
    for span in spans:
        loc = span.locator
        name = loc.get('source_name', '未命名资料')
        identity = (name, span.source_sha256)
        if identity != source:
            lines.extend([f'## 文件：{_inline(name)}', ''])
            source = identity
            heading = None
        context = loc.get('context', {})
        if context.get('heading') and context['heading'] != heading:
            lines.extend(['章节上下文：' + _inline(context['heading']), ''])
            heading = context['heading']
        # Coordinates already travel in source_spans. Avoid repeating verbose
        # native package paths for every paragraph in the reading view.
        lines.append(f'### 片段 {span.id}')
        if 'raw_value' in loc:
            lines.append(f"存储值：{_inline(loc['raw_value'])}；单元格格式：{_inline(loc['number_format'])}")
            if loc.get('format_status') == 'normalized':
                lines.append('以下原文已按该单元格的日期、时间或百分比格式规范化；未计算公式。')
            elif loc.get('format_status') == 'raw_numeric':
                lines.append('无单位数字显示格式；以下保留精确存储数值，未按显示精度舍入。')
            else:
                lines.append('该数字格式尚未转换，以下保留存储值；不可自行推断其显示含义。')
        if context.get('kind') == 'table_cell':
            lines.append(f"表 {context['table']} / 行 {context['row']} / 单元格 {context['cell']}")
        if context.get('column_label'):
            label = '表头' if context.get('header_kind') == 'explicit' else '首行候选（不保证是表头）'
            lines.append(label + '：' + _inline(context['column_label']))
        cells = context.get('row_cells', [])
        if cells:
            lines.extend(['', '同一行上下文（不可作为原文引用）：'])
            if context.get('kind') == 'sheet_cell':
                labels = [item[0] for item in cells]
                values = [item[1] for item in cells]
            else:
                labels = [f'单元格 {i + 1}' for i in range(len(cells))]
                values = cells
            lines.extend(['| ' + ' | '.join(map(_inline, labels)) + ' |',
                          '| ' + ' | '.join(['---'] * len(cells)) + ' |',
                          '| ' + ' | '.join(map(_inline, values)) + ' |'])
            if context.get('row_truncated'):
                lines.append('行上下文仅显示前 24 项；完整原文仍见各片段。')
        # Use a longer fence than any literal run in user content so the original
        # text, including Markdown-looking text, remains an unmodified block.
        longest = max((len(m[0]) for m in re.finditer(r'`+', span.text)), default=0)
        fence = '`' * max(3, longest + 1)
        lines.extend([fence + 'text', span.text, fence, ''])
    return '\n'.join(lines)


def model_source_payload(spans):
    """The one reading view used by model calls and prepare/apply diagnostics.

    Source identities, original text and coordinates remain authoritative.
    Row context is emitted once in Markdown, rather than repeating a synthetic
    table row in both JSON locators and Markdown. Neither view changes a quote.
    """
    raw = []
    for span in spans:
        value = asdict(span)
        context = value['locator'].get('context')
        if context is not None:
            context.pop('row_cells', None)
            context.pop('row_truncated', None)
        raw.append(value)
    return {'source_spans': raw, 'source_markdown': source_markdown(spans)}


def source_groups(spans):
    """Native sections and table rows are preferred reading boundaries.

    These are structural groups, not inferred facts or semantic retrieval.
    A caller may split a group that exceeds its actual serialized budget.
    """
    group = []
    previous = None
    for span in spans:
        loc = span.locator
        context = loc.get('context', {})
        source = (span.source_sha256, loc.get('source_name'), loc.get('part'),
                  loc.get('page_index'), loc.get('sheet'))
        if context.get('kind') in ('table_cell', 'sheet_cell'):
            key = source + (context.get('kind'), context.get('table'), context.get('row'))
        elif 'row' in loc:
            key = source + ('source_row', loc['row'])
        else:
            key = source + ('section', context.get('heading', ''))
        if group and key != previous:
            yield group
            group = []
        group.append(span)
        previous = key
    if group:
        yield group


def carry_row_context(group, fragment):
    """Repeat only bounded row context when a wide native row must be split."""
    if not fragment:
        return fragment
    first = fragment[0]
    context = first.locator.get('context', {})
    if context.get('kind') not in ('table_cell', 'sheet_cell') or context.get('row_cells'):
        return fragment
    original = next((s.locator.get('context', {}) for s in group
                     if s.locator.get('context', {}).get('row_cells')), None)
    if original is None:
        return fragment
    restored = dict(context, row_cells=original['row_cells'], row_truncated=original.get('row_truncated', False))
    return [replace(first, locator=dict(first.locator, context=restored)), *fragment[1:]]

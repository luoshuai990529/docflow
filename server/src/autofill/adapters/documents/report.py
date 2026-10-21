"""Readable, inert reports derived from verified output, with v1 JSON compatibility."""
from collections import Counter
from dataclasses import asdict
from html import escape
import json
import math
from pathlib import Path
import re

from ...models import dump_json


STATUS_LABELS = {
    'model_omitted': '模型未处理此题',
    'location_missing': '未能定位填写区域',
    'written': '已填写', 'not_found': '未找到答案', 'conflict': '来源内容有冲突',
    'needs_review': '答案待复核', 'not_written': '未写入',
    'write_failed': '写入未通过校验', 'layout_blocked': '受版式限制，未完整写入',
    'candidate_not_delivered': '候选内容未交付',
}
REASON_LABELS = {
    'FIXED_ROW_HEIGHT_OVERFLOW': '答案超出固定行高，已留空。',
    'TEXT_OVERFLOW': '内容超出填写区域，需人工缩短或调整版式。',
    'NATIVE_MAX_LENGTH': '答案超过表单原有字符限制，已留空。',
    'FIELD_SINGLE_LINE': '填写区域只允许一行，答案未能完整写入。',
    'AREA_TOO_SMALL': '填写区域过小，无法按原字号写入。',
    'UNSUPPORTED_FORM_FONT': '表单字体不支持答案中的字符，需人工填写。',
    'FONT_GLYPH_MISSING': '填写字体缺少所需字符，需人工检查。',
    'VALUE_OR_APPEARANCE_MISMATCH': '写入后的内容或外观与预期不一致，需人工检查。',
    'Verification failed; candidate retained for inspection': '文件未通过校验，候选内容未交付。',
    'No decision returned; left blank': '未获得可写入的答案，请补充答案资料。',
    'No decision': '未获得可写入的答案，请补充答案资料。',
    'Text value is not a literal part of the cited evidence; review required': '答案不在所引用的原文中，已留空，请人工确认。',
}
REVIEW_DEFAULTS = {
    'location_missing': '已识别题目，但未能可靠定位填写区域，本题未写入。',
    'model_omitted': '本题尚未得到模型处理结果，请重新处理或人工确认。',
    'not_found': '未找到有原文支持的答案，请补充答案资料。',
    'conflict': '来源内容有冲突，请确认采用哪个答案。',
    'needs_review': '答案尚不能可靠确认，请核对原文后补充。',
    'write_failed': '写入结果未通过校验，请查看原文并人工处理。',
    'layout_blocked': '答案未能完整写入，请检查填写区域与字体。',
    'not_written': '未确认写入，请查看技术记录中的原始说明。',
}
SEMANTIC_NOTE = '原文引文用于核对；涉及改写、翻译或计算的答案在逐题记录中说明核对方式。'
AUTONOMOUS_NOTE = '答案由模型自主生成，未使用答案资料；企业情况等可能为拟定假设，未经事实核实。'


def build_report(questions, decisions, results, verification, input_hashes, output_hash, mode, targets=None):
    decisions_by_id = {d.question_id: d for d in decisions}
    targets_by_id = {target.id: target for target in targets or []}
    rows = []
    for question in questions:
        decision = decisions_by_id.get(question.id)
        writes = [r for r in results if r.question_id == question.id]
        written = [r for r in writes if r.status == 'written']
        if any(r.status == 'readback_failed' for r in writes):
            status = 'write_failed'
        elif any(r.status == 'layout_blocked' for r in writes):
            status = 'layout_blocked'
        elif written:
            status = 'written'
        else:
            status = decision.status if decision and decision.status != 'ready' else 'not_written'
        if not question.target_ids:status='location_missing'
        target_options = {}
        for target_id in question.target_ids:
            target = targets_by_id.get(target_id)
            if target and target.constraints.get('content_type') == 'checkbox':
                option = target.locator.get('option_id') or target.constraints.get('checked_option')
                if option in question.options:
                    target_options[target_id] = option
        rows.append({
            'question_id': question.id, 'question': question.text, 'status': status,
            'question_kind': question.kind, 'option_labels': list(question.options),
            'target_options': target_options,
            'completion': 'partial' if written and len(written) < len(question.target_ids) else 'complete' if written else 'blank',
            'reason': '; '.join(r.reason for r in writes if r.reason) or (decision.reason if decision else 'No decision'),
            'evidence_span_ids': decision.span_ids if decision else [],
            'evidence_quote': decision.quote if decision else None,
            'results': [asdict(r) for r in writes],
        })
    counts = dict(Counter(row['status'] for row in rows))
    assert sum(counts.values()) == len(questions)
    return prepare_report({
        'schema_version': 1, 'mode': mode, 'question_total': len(questions), 'counts': counts,
        'input_hashes': input_hashes, 'output_sha256': output_hash,
        'verification': asdict(verification), 'rows': rows,
        'note': 'Semantic evidence was matched by The configured matcher/approved JSON. Citation checks do not independently prove semantic correctness.',
    })


def _pick(value, keys):
    return {key: value[key] for key in keys if key in value} if isinstance(value, dict) else {}


def _validation_details(raw):
    """Only generated numeric facts, field names and types; never rejected text."""
    if not isinstance(raw,dict):return {}
    result={}
    decimal=lambda value:isinstance(value,str) and len(value)<=64 and bool(re.fullmatch(r'[-+]?(?:\d+(?:\.\d+)?|\.\d+)',value))
    for key,value in raw.items():
        if key in ('operand_value','result_value','expected_result') and decimal(value):result[key]=value
        elif key in ('quoted_numbers','missing_numbers','operand_values') and isinstance(value,list):result[key]=[v for v in value[:32] if decimal(v)]
        elif key=='quote_sha256' and isinstance(value,str) and re.fullmatch(r'[a-f0-9]{64}',value):result[key]=value
        elif key=='interpretation' and value in ('literal','half','percent','invalid'):result[key]=value
        elif key=='actual_type' and value in ('null','boolean','string','array','object','number','unknown','str','int','float','dict','list','bool','NoneType'):result[key]=value
        elif key=='operation' and value in ('add','subtract','multiply','divide'):result[key]=value
        elif key=='operand_measures' and isinstance(value,list):result[key]=[v for v in value[:2] if isinstance(v,bool)]
        elif key in ('unexpected_field_count','option_count','actual_chars','max_chars') and type(value)==int and 0<=value<=1000000:result[key]=value
        elif key=='missing_fields' and isinstance(value,list):result[key]=[v for v in value[:16] if isinstance(v,str) and v in ('question_id','status','value','option_ids','span_ids','quote','evidence','reason','span_id','interpretation','operation','operands','result','unit')]
    return result


def _validation_attempt(raw):
    from ...application.diagnostics import diagnostic_metadata
    result=_pick(raw,('attempt','stage','phase','result_status'))
    for key,pattern in (('code',r'[A-Z][A-Z_]{1,79}'),('path',r'[\w.\[\]$]{1,160}'),('check',r'[a-z_]{1,100}')):
        value=raw.get(key)
        if isinstance(value,str) and re.fullmatch(pattern,value):result[key]=value
    result['details']=_validation_details(raw.get('details'))
    result['metadata']=diagnostic_metadata(raw.get('metadata',raw))
    return result


def _answer_review(raw):
    """Export review evidence, never the in-memory permission or rejected text."""
    from ...application.diagnostics import diagnostic_metadata
    if not isinstance(raw,dict):return {}
    result=_pick(raw,('verdict','accepted','code','transformations','reason','review_reason','source_ids','context_source_ids'))
    result['evidence']=[_pick(item,('span_id','quote')) for item in raw.get('evidence',[]) if isinstance(item,dict)]
    result['calculations']=[]
    for item in raw.get('calculations',[]):
        if not isinstance(item,dict):continue
        calculation=_pick(item,('operation','result','unit','verification'))
        calculation['operands']=[_pick(operand,('value','span_id','quote','interpretation')) for operand in item.get('operands',[]) if isinstance(operand,dict)]
        result['calculations'].append(calculation)
    result['metadata']=diagnostic_metadata(raw.get('metadata',{}))
    result['attempts']=[_validation_attempt(item) for item in raw.get('attempts',[])[:8] if isinstance(item,dict)]
    if isinstance(raw.get('revision'),dict):result['revision']=dict(_pick(raw['revision'],('code',)),**diagnostic_metadata(raw['revision']))
    if isinstance(raw.get('retrieval_review'),dict):result['retrieval_review']=_pick(raw['retrieval_review'],('code','review_reason','verdict'))
    if isinstance(raw.get('initial_review'),dict):result['initial_review']=_pick(raw['initial_review'],('code','reason','review_reason','verdict'))
    return result


def _transformation_note(row):
    review=row.get('answer_review',{})
    if not review.get('accepted'):return ''
    labels={'paraphrase':'归纳表达','translation':'翻译','calculation':'计算'}
    kinds='、'.join(labels[k] for k in review.get('transformations',[]) if k in labels)
    note=(kinds+'；' if kinds else '')+'原文引用经程序核对，含义及适用条件经模型核对。'
    calculations=[item.get('verification','') for item in review.get('calculations',[]) if item.get('verification')]
    if calculations:note+=' 程序复算：'+'；'.join(calculations)
    return note


def _report_fields(report):
    """Retain the v1 data contract; never expose arbitrary provider/config fields."""
    result = _pick(report, ('schema_version', 'mode', 'question_total', 'counts', 'output_sha256', 'note',
                            'published_file', 'origin', 'input_original_questionnaire_sha256'))
    result['answer_mode']='autonomous' if report.get('answer_mode')=='autonomous' else 'grounded'
    if result['answer_mode']=='autonomous':
        context=report.get('autonomous_context',{})
        result['autonomous_context']={name:value[:limit] for name,limit in (('scenario',4000),('assumptions',2000))
            if isinstance(context,dict) and isinstance(value:=context.get(name),str)}
    result['question_inventory']=_pick(report.get('question_inventory'),('status','readable_spans','reviewed_spans','unreviewed_spans','unlocated_questions','native_fields_confirmed','excluded_targets','unconfirmed_targets','note'))
    result['processing_warnings']=[value for value in report.get('processing_warnings',[]) if isinstance(value,str)]
    diagnostics=report.get('model_diagnostics',{})
    if isinstance(diagnostics,dict):
        from ...application.diagnostics import diagnostic_metadata
        events=[]
        for raw in diagnostics.get('validation_events',[])[:1000]:
            if not isinstance(raw,dict):continue
            event=_pick(raw,('question_id','batch','source_chunk','outcome','result_status','review_stage','phase','model_role'))
            role=raw.get('model_role',raw.get('role'))
            if role in ('llm','jev'):event['model_role']=role
            if type(raw.get('attempt'))==int and 1<=raw['attempt']<=4:event['attempt']=raw['attempt']
            for key in ('code','correction_code'):
                value=raw.get(key)
                if isinstance(value,str) and re.fullmatch(r'[A-Z][A-Z_]{1,79}',value):event[key]=value
            event.update(diagnostic_metadata(raw))
            if isinstance(raw.get('correction'),dict):event['correction']=diagnostic_metadata(raw['correction'])
            if type(raw.get('attempt_count'))==int and 1<=raw['attempt_count']<=4:event['attempt_count']=raw['attempt_count']
            if isinstance(raw.get('attempts'),list):event['attempts']=[_validation_attempt(item) for item in raw['attempts'][:4] if isinstance(item,dict)]
            events.append(event)
        result['model_diagnostics']={'validation_events':events,'validation_events_truncated':diagnostics.get('validation_events_truncated',0)}
    result['input_hashes'] = _pick(report.get('input_hashes'), ('questionnaire', 'source'))
    verification = _pick(report.get('verification'), ('content_ok', 'layout_check', 'needs_review'))
    details = _pick(report.get('verification', {}).get('details'), (
        'structure_preserved', 'dimensions_preserved', 'inserted_fonts_embedded',
        'renderer', 'note', 'dpi', 'mask_tolerance_pt', 'baseline_pages', 'output_pages',
        'checkbox_visual_failures', 'visible_answers_missing', 'original_visible_text_preserved', 'baseline_text_coverage', 'checks', 'limits', 'text_overlap', 'baseline_text_overlap_pairs', 'new_text_overlap_pairs', 'new_image_text_overlap',
    ))
    pages = report.get('verification', {}).get('details', {}).get('pages')
    if isinstance(pages, list):
        details['pages'] = [_pick(page, ('page', 'changed_pixels', 'outside_fill_pixels')) for page in pages]
    verification['details'] = details
    result['verification'] = verification
    if 'engine' in report:
        result['engine'] = _pick(report['engine'], ('provider', 'model', 'protocol', 'output_mode', 'request_id', 'request_ids'))
    if 'decision_engine' in report:
        result['decision_engine'] = _pick(report['decision_engine'], ('provider', 'model', 'protocol'))
    if 'questionnaire_conversion' in report:
        conversion = report['questionnaire_conversion']
        result['questionnaire_conversion'] = _pick(conversion, ('kind', 'converted', 'source_format', 'target_format', 'content_verified', 'structure_verified')) if isinstance(conversion, dict) else str(conversion)
    if isinstance(report.get('coverage'), dict):
        result['coverage'] = {}
        for key in ('questionnaire', 'sources'):
            data = report['coverage'].get(key, {})
            if not isinstance(data, dict): continue
            result['coverage'][key] = _pick(data, ('discovery', 'original_name'))
            result['coverage'][key]['coverage'] = [_pick(item, ('file', 'sha256', 'page', 'status', 'part', 'sheet', 'cells', 'low_confidence')) for item in data.get('coverage', []) if isinstance(item, dict)]
            if key == 'questionnaire' and isinstance(data.get('answer_coverage'), dict):
                coverage = data['answer_coverage']
                result['coverage'][key]['answer_coverage'] = {
                    'source_chunks': coverage.get('source_chunks', 0),
                    'questions': {identity: _pick(value, ('checked_chunks', 'total_chunks', 'incomplete_chunks', 'search_mode', 'exhaustive', 'complete'))
                                  for identity, value in coverage.get('questions', {}).items() if isinstance(value, dict)},
                    'fallback_questions': coverage.get('fallback_questions', []),
                }
    result['rows'] = []
    for raw in report.get('rows', []):
        row = _pick(raw, ('question_id', 'question', 'status', 'completion', 'reason', 'evidence_span_ids', 'evidence_quote', 'question_kind'))
        row['evidence_citations']=[_pick(item,('span_id','quote')) for item in (raw.get('evidence_citations') or []) if isinstance(item,dict) and isinstance(item.get('span_id'),str) and isinstance(item.get('quote'),str)]
        if isinstance(raw.get('answer_review'),dict):row['answer_review']=_answer_review(raw['answer_review'])
        if result['answer_mode']=='autonomous':
            generated=raw.get('autonomous_answer',{})
            if not isinstance(generated,dict):generated={}
            note={'origin':'jev' if generated.get('origin')=='jev' else 'llm',
                  'assumptions':str(generated.get('assumptions') or '')[:4000]}
            code=generated.get('fallback_code')
            if isinstance(code,str) and re.fullmatch(r'[A-Z][A-Z_]{1,79}',code):note['fallback_code']=code
            count=generated.get('selected_count')
            if type(count)==int and 0<=count<=2000:note['selected_count']=count
            scores=generated.get('decision_scores')
            if isinstance(scores,dict):
                note['decision_scores']={label:score for label,score in scores.items() if label in raw.get('option_labels',[]) and type(score) in (int,float) and math.isfinite(score)}
            layout=generated.get('layout')
            if isinstance(layout,dict):
                safe={key:layout[key] for key in ('suggested_max_chars','max_lines','native_max_length','original_chars','final_chars','compression_attempts')
                      if type(layout.get(key))==int and 0<=layout[key]<=10000000}
                if layout.get('space_type') in ('fixed','flow','explicit_limit','unknown'):safe['space_type']=layout['space_type']
                if layout.get('fit_status') in ('fits','overflow','unknown'):safe['fit_status']=layout['fit_status']
                if type(layout.get('single_line'))==bool:safe['single_line']=layout['single_line']
                safe['attempts']=[]
                for attempt in layout.get('attempts',[])[:2]:
                    if not isinstance(attempt,dict):continue
                    item={key:attempt[key] for key in ('round','original_chars','char_count') if type(attempt.get(key))==int and 0<=attempt[key]<=10000000}
                    if isinstance(attempt.get('code'),str) and re.fullmatch(r'[A-Z][A-Z_]{1,79}',attempt['code']):item['code']=attempt['code']
                    if attempt.get('fit_status') in ('fits','overflow','unknown'):item['fit_status']=attempt['fit_status']
                    item['fit_codes']=[code for code in attempt.get('fit_codes',[]) if isinstance(code,str) and re.fullmatch(r'[A-Z][A-Z_]{1,79}',code)][:10]
                    safe['attempts'].append(item)
                note['layout']=safe
            row['autonomous_answer']=note
        if isinstance(raw.get('option_labels'), list):
            row['option_labels'] = [label for label in raw['option_labels'] if isinstance(label, str)]
        if isinstance(raw.get('target_options'), dict):
            row['target_options'] = {target_id: label for target_id, label in raw['target_options'].items()
                                     if isinstance(target_id, str) and isinstance(label, str)
                                     and label in row.get('option_labels', [])}
        if isinstance(raw.get('source_locations'), list):
            row['source_locations'] = [_pick(loc, ('source_name', 'page_index', 'sheet', 'cell', 'row', 'line', 'part', 'paragraph', 'confidence', 'raw_value', 'number_format', 'format_status', 'format_conversion')) for loc in raw['source_locations']]
        row['results'] = [_pick(write, ('question_id', 'target_id', 'status', 'expected_value', 'readback_value', 'reason')) for write in raw.get('results', [])]
        result['rows'].append(row)
    return result


def _delivered(report):
    # Older reports can predate published_file, but always carry the final hash.
    return bool(report.get('output_sha256')) and ('published_file' not in report or bool(report['published_file']))


def _review_note(row, delivered):
    writes = row.get('results', [])
    if not delivered and any(w.get('status') in ('written', 'candidate_not_delivered') for w in writes):
        return '填写文件未交付，候选内容不能作为已完成答案使用。'
    if row.get('answer_review') and not row['answer_review'].get('accepted'):
        return row.get('reason') or '答案未通过资料含义核对，已留空。'
    layout=row.get('autonomous_answer',{}).get('layout',{})
    if layout.get('fit_status')=='overflow' and layout.get('compression_attempts',0)>0:
        return '已尝试精简 '+str(layout['compression_attempts'])+' 次，'+('部分位置仍无法完整显示，已保留成功填写的位置。' if any(w.get('status')=='written' for w in writes)
            else '仍无法在原填写区域完整显示，已留空；其他答案已保留。')
    reasons = [str(w.get('reason', '')) for w in writes if w.get('status') != 'written']
    reasons.append(str(row.get('reason', '')))
    notes = []
    for reason in reasons:
        if reason in REASON_LABELS:
            notes.append(REASON_LABELS[reason])
        elif reason.startswith('CHOICE_REVIEW:'):
            notes.append('选项未能可靠校验，已留空，请人工确认。')
        elif reason in (
            '这题的答案引用未能与资料原文核对，未采用该答案。',
            '这题的选项未能与问卷选项核对，未采用该答案。',
            '这题的答案内容未能与引用原文核对，未采用该答案。',
            '这题的答案格式无法可靠校验，已留空。',
            '模型重复返回了这道题，无法确定采用哪条答案，已留空。',
            '模型未完整返回这题的答案，已留空。',
            '模型服务未提供本批答案，相关题目已留空。',
            '答案或引用过长，未采用这题的答案。',
        ):
            notes.append(reason)
    if notes:
        return ' '.join(dict.fromkeys(notes))
    if row.get('completion') == 'partial' and delivered:
        return '本题仅部分位置写入成功，请检查其余填写位置。'
    if row.get('status') == 'written' and delivered:
        return ''
    if row.get('autonomous_answer'):
        return {'model_omitted':'模型未完整返回本题答案，已留空。',
                'not_found':'本次未生成可写入的答案。',
                'conflict':'模型生成的选项组合无法采用，已留空。',
                'needs_review':'本题答案格式或填写位置未通过检查，已留空。'}.get(row.get('status'),REVIEW_DEFAULTS.get(row.get('status'),'本题未能完成填写，请查看处理记录。'))
    return REVIEW_DEFAULTS.get(row.get('status'), '请人工核对本题；原始说明保留在技术记录中。')


def _answer_display(row, accepted):
    """Translate checkbox states using reviewed targets, never model candidates."""
    selected, unchecked, other = [], [], []
    mapping = row.get('target_options', {})
    for write in accepted:
        value = str(write['readback_value'])
        target_id = str(write.get('target_id') or '未记录位置')
        if value not in ('checked', 'unchecked') or row.get('question_kind') == 'text':
            other.append(value)
            continue
        label = mapping.get(target_id)
        if label is not None:
            (selected if value == 'checked' else unchecked).append(label)
        else:
            other.append(target_id + '：' + ('已勾选' if value == 'checked' else '未勾选'))
    if selected:
        other.insert(0, '已选：' + '、'.join(dict.fromkeys(selected)))
    elif unchecked:
        other.insert(0, '未勾选：' + '、'.join(dict.fromkeys(unchecked)))
    return '\n'.join(other) if other else None


def prepare_report(report):
    """Return a fresh readable v1 report, also for historical reports without quotes."""
    result = _report_fields(report)
    autonomous=result['answer_mode']=='autonomous'
    delivered = _delivered(result)
    for row in result['rows']:
        accepted = [w for w in row['results'] if delivered and w.get('status') == 'written' and w.get('readback_value') is not None]
        # The readback value is authoritative; a decision/expected value is not.
        row['answer'] = _answer_display(row, accepted)
        row['written_answers'] = [{'target_id': w.get('target_id'), 'value': w['readback_value']} for w in accepted]
        partial = bool(accepted) and (row.get('completion') == 'partial' or any(w.get('status') != 'written' for w in row['results']))
        row['status_label'] = '部分填写' if partial else STATUS_LABELS.get(row.get('status'), '待复核')
        if not delivered and any(w.get('status') in ('written', 'candidate_not_delivered') for w in row['results']):
            row['status_label'] = '候选内容未交付'
        row['review_note'] = _review_note(row, delivered)
        row['needs_action'] = row['status_label'] != '已填写' or not accepted
        row.setdefault('evidence_quote', None)

    rows = result['rows']
    written = sum(bool(r['written_answers']) for r in rows)
    partial = sum(r['status_label'] == '部分填写' for r in rows)
    review = [r for r in rows if r['needs_action']]
    verification = result['verification']
    no_attempts = not any(r['results'] for r in rows)
    failed = verification.get('content_ok') is False or verification.get('layout_check') == 'needs_review'
    layout = verification.get('layout_check')
    file_name = result.get('published_file')
    docx = str(file_name or '').lower().endswith('.docx') or verification.get('details', {}).get('renderer') == 'none'
    if not delivered:
        only_missing = bool(rows) and all(r['status']=='not_found' for r in rows)
        title = '文件未通过校验，未交付填写文件' if failed else ('未找到可填写的答案' if only_missing else '本次未写入答案') if no_attempts else '未交付填写文件'
        next_step = '请补充答案资料，再重新填写。' if no_attempts and only_missing and not failed else '请查看逐题原因，区分资料不足、答案校验与写入问题。'
    elif docx and layout == 'not_run':
        title = '内容已填写，排版待复核'
        next_step = '请打开 DOCX，检查分页、截断和表格排版。'
    elif review:
        title = '填写完成，部分题目未填完整'
        next_step = '请查看下方逐题原因。'
    elif verification.get('needs_review') or layout != 'passed':
        title = '已生成填写文件，仍需复核'
        next_step = '请打开文件检查未验证的内容与版式。'
    else:
        title = '填写完成'
        next_step = '请结合原文确认答案后使用填写文件。'
    if autonomous:
        next_step='答案由模型生成，拟定假设及处理情况见报告。' if delivered and not review else '请查看逐题说明，了解模型作答及未完成原因。'
    if no_attempts and not delivered:
        layout_note = '没有可写入的答案，未运行版式检查。'
    elif layout == 'not_run' and docx:
        layout_note = 'DOCX 的分页与排版尚未验证。'
    elif layout == 'passed':
        layout_note = '基础版式检查通过。'
    elif layout == 'needs_review':
        layout_note = '版式检查未通过，需人工复核。'
    else:
        layout_note = '版式检查尚未完成，请人工复核。'
    summary = {
        '结论': title,
        '交付状态': '已生成填写文件' if delivered else '未交付填写文件',
        '填写文件': file_name if delivered else None,
        '统计': {'题目总数': result.get('question_total', len(rows)), '有答案写入': written,
                 '其中部分填写': partial, '未写入': len(rows) - written, '待处理题目': len(review)},
        '版式检查': layout_note,
        '下一步': next_step,
        '说明': AUTONOMOUS_NOTE if autonomous else SEMANTIC_NOTE,
        '测试说明': '虚构测试数据，仅用于排版验证，不可用于业务提交。' if result.get('mode') == 'fictional_test' else None,
    }
    if result['processing_warnings']:
        if delivered:summary['结论']='已生成填写文件，部分内容未完成处理'
        summary['下一步']=' '.join(result['processing_warnings'])
    # One readback-based contract for report, result screen and history. A partly
    # written question is neither a complete answer nor a wholly blank question.
    pending = [{'question_id': row['question_id'], 'number': index,
                'question': row.get('question', '')[:240], 'status': row['status_label'],
                'reason': (row['review_note'] or '本题未填完整，请查看报告。')[:180]}
               for index, row in enumerate(rows, 1) if row['needs_action']]
    result['result_summary'] = {
        'title': summary['结论'], 'message': summary['下一步'], 'delivered': delivered,
        'counts': {'total': len(rows), 'complete': written - partial, 'partial': partial, 'blank': len(rows) - written},
        'pending': pending[:5], 'pending_total': len(pending),
        'layout_check': layout, 'layout_note': layout_note,
        'answer_mode':result['answer_mode'], 'mode_label':'模型自主作答' if autonomous else '依据资料填写',
    }
    return {'reading_summary': summary, **result}


def _md(value):
    """Render untrusted values as literal Markdown text, not links or HTML."""
    return re.sub(r'([\\`*_{}\[\]()#+\-.!|>])', r'\\\1', escape(str(value), quote=False)).replace('\r', '')


def _technical(report):
    return {key: value for key, value in report.items() if key != 'reading_summary'}


def _generation_notes(row):
    generated=row.get('autonomous_answer')
    if not generated:return []
    notes=['作答模型：'+('Jev' if generated['origin']=='jev' else 'LLM')+'；模型生成，未作资料核实。']
    if generated.get('assumptions'):notes.append('拟定假设：'+generated['assumptions'])
    if generated.get('fallback_code'):notes.append('决策模型未完成，本题已自动回退 LLM 作答；具体原因见技术记录。')
    if generated.get('decision_scores'):
        notes.append('选项评分：'+'；'.join(label+' = '+format(score,'.3g') for label,score in generated['decision_scores'].items()))
    if generated.get('selected_count') is not None:notes.append('模型决定选取 '+str(generated['selected_count'])+' 项。')
    layout=generated.get('layout',{})
    if layout:
        parts=[]
        if 'suggested_max_chars' in layout:parts.append('建议约 '+str(layout['suggested_max_chars'])+' 字符（按版面估算）')
        if 'original_chars' in layout and 'final_chars' in layout:parts.append(str(layout['original_chars'])+' → '+str(layout['final_chars'])+' 字符')
        if layout.get('compression_attempts'):parts.append('自动精简 '+str(layout['compression_attempts'])+' 次')
        parts.append({'fits':'实际填写检查通过','overflow':'仍超出填写区域','unknown':'填写容量未确认'}.get(layout.get('fit_status'),'填写容量未确认'))
        notes.append('填写容量：'+'；'.join(parts)+'。')
    return notes


def render_markdown(report):
    report = prepare_report(report)
    summary = report['reading_summary']
    lines = ['# 文件填写报告', '', '## 结论与交付状态', '', '**' + _md(summary['结论']) + '**', '',
             '交付状态：' + _md(summary['交付状态']) + '。']
    if summary['测试说明']:
        lines += ['', '**' + summary['测试说明'] + '**', '']
    if summary['填写文件']:
        lines.append('填写文件：' + _md(summary['填写文件']))
    if report['answer_mode']=='autonomous':
        lines += ['', '填写模式：模型自主作答', '', AUTONOMOUS_NOTE]
        for key,label in (('scenario','统一作答背景'),('assumptions','拟定假设')):
            if report['autonomous_context'].get(key):lines += ['',label+'：'+_md(report['autonomous_context'][key])]
    lines += ['', summary['版式检查'], '', summary['下一步'], '', '## 统计', '', '| 项目 | 数量 |', '| --- | ---: |']
    lines += [f'| {label} | {_md(count)} |' for label, count in summary['统计'].items()]
    lines += ['', '## 优先待复核', '']
    review = [(index, row) for index, row in enumerate(report['rows'], 1) if row['needs_action']]
    if summary['版式检查'] != '基础版式检查通过。' and _delivered(report):
        lines += ['- **文件排版**：' + summary['版式检查']]
    if review:
        for index, row in review:
            lines.append(f'- **第 {index} 题 · {_md(row["question"])}**：{_md(row["review_note"])}')
    elif _delivered(report):
        lines.append('- 没有待处理的题目。')
    else:
        lines.append('- 未交付填写文件，请查看逐题结果。')
    lines += ['', '## 逐题结果', '']
    for index, row in enumerate(report['rows'], 1):
        lines += [f'### {index}. {_md(row["question"])}', '', '状态：' + row['status_label'], '']
        if row.get('source_locations'):
            lines += ['来源：' + _md('；'.join(location_label(loc) for loc in row['source_locations'])), '']
        if row.get('reason'):
            lines += ['说明：' + _md(row['reason']), '']
        for note in _generation_notes(row):lines += [_md(note),'']
        if row['answer'] is not None:
            lines += ['**实际已写答案**', '', _md(row['answer']), '']
        if _transformation_note(row):
            lines += ['**答案整理与核对**', '', _md(_transformation_note(row)), '']
        if row['review_note']:
            lines += ['**未写入／待处理说明**', '', _md(row['review_note']), '']
        if report['answer_mode']=='autonomous':continue
        lines += ['**原文引文**', '']
        if row.get('evidence_citations'):
            for citation in row['evidence_citations']:lines += ['> '+_md(citation['quote']).replace('\n','\n> '),'']
        elif row.get('evidence_quote'):
            lines += ['> ' + _md(row['evidence_quote']).replace('\n', '\n> '), '']
        else:
            lines += ['此报告未记录原文引文，请回到答案资料核对。', '']
    lines += [summary['说明'], '', '## 技术记录', '', '<details>', '<summary>展开原始状态、逐位置读回与校验信息</summary>', '',
              '<pre>' + escape(json.dumps(_technical(report), ensure_ascii=False, indent=2)) + '</pre>', '', '</details>', '']
    return '\n'.join(lines)


STYLE = '''
:root{color-scheme:light;--ink:#20241f;--muted:#606860;--line:#dfe3dc;--paper:#fff;--surface:#f6f7f4}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--ink);font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:940px;margin:40px auto;padding:48px 56px;background:var(--paper)}header{border-bottom:1px solid var(--line);padding-bottom:26px}h1{font-size:28px;line-height:1.3;margin:10px 0 24px}h2{font-size:19px;margin:34px 0 16px}h3{font-size:17px;line-height:1.6;margin:0 0 14px;overflow-wrap:anywhere}.conclusion{font-size:22px;font-weight:600;margin:0 0 12px}p{margin:10px 0}.muted{color:var(--muted)}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:20px 28px;margin:0}.stats div{display:flex;align-items:baseline;justify-content:space-between;gap:12px;border-top:1px solid var(--line);padding-top:10px}.stats dt{font-size:13px;color:var(--muted)}.stats dd{font-size:18px;line-height:1.4;margin:0;font-variant-numeric:tabular-nums}.review{padding:16px 20px;background:var(--surface)}.review ul{margin:0;padding-left:20px}.review li{overflow-wrap:anywhere}.review li+li{margin-top:12px}.review a{color:inherit;text-decoration-thickness:1px;text-underline-offset:3px}.question{border-top:1px solid var(--line);padding:24px 0}.status{display:inline-block;font-size:12px;border:1px solid var(--line);border-radius:4px;padding:2px 8px;margin-bottom:14px}.label{font-size:13px;color:var(--muted);margin:14px 0 4px}.answer,.copy,blockquote{white-space:pre-wrap;overflow-wrap:anywhere}.answer{font-weight:500}blockquote{margin:0;padding:8px 0 8px 16px;border-left:1px solid var(--line)}.technical{border-top:1px solid var(--line);padding-top:2px}details{margin-top:12px}summary{cursor:pointer;font-size:14px;min-height:44px;align-content:center}summary:focus-visible,a:focus-visible{outline:2px solid var(--ink);outline-offset:4px}pre{font:12px/1.65 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;padding:16px;background:var(--surface)}footer{font-size:13px;color:var(--muted);margin-top:30px}.file{overflow-wrap:anywhere}
@media(max-width:640px){main{margin:0;padding:28px 20px}h1{font-size:24px}.conclusion{font-size:20px}.stats{grid-template-columns:repeat(2,1fr);gap:18px}h2{margin-top:28px}}
@media print{body{background:white}main{margin:0;padding:0;max-width:none}.question{break-inside:avoid}.technical{display:none}a{text-decoration:none}.review{background:white;border:1px solid var(--line)}}
'''


def render_html(report):
    report = prepare_report(report)
    summary = report['reading_summary']
    h = lambda value: escape(str(value), quote=True)
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<title>文件填写报告</title><style>' + STYLE + '</style></head><body><main>',
             '<header><h1>文件填写报告</h1>',
             '<p class="conclusion">' + h(summary['结论']) + '</p>', '<p>' + h(summary['交付状态']) + '</p>']
    if summary['测试说明']:
        parts.append('<p><strong>' + h(summary['测试说明']) + '</strong></p>')
    if summary['填写文件']:
        parts.append('<p class="file muted">' + h(summary['填写文件']) + '</p>')
    if report['answer_mode']=='autonomous':
        parts += ['<p>填写模式：模型自主作答</p><p class="muted">'+h(AUTONOMOUS_NOTE)+'</p>']
        for key,label in (('scenario','统一作答背景'),('assumptions','拟定假设')):
            if report['autonomous_context'].get(key):parts += ['<p class="copy muted">'+label+'：'+h(report['autonomous_context'][key])+'</p>']
    parts += ['<p>' + h(summary['版式检查']) + '</p>', '<p class="muted">' + h(summary['下一步']) + '</p></header>',
              '<section aria-labelledby="stats"><h2 id="stats">统计</h2><dl class="stats">']
    parts += ['<div><dt>' + h(label) + '</dt><dd>' + h(count) + '</dd></div>' for label, count in summary['统计'].items()]
    parts += ['</dl></section><section aria-labelledby="review"><h2 id="review">优先待复核</h2><div class="review"><ul>']
    review = [(index, row) for index, row in enumerate(report['rows'], 1) if row['needs_action']]
    if summary['版式检查'] != '基础版式检查通过。' and _delivered(report):
        parts += ['<li><strong>文件排版</strong>：' + h(summary['版式检查']) + '</li>']
    if review:
        for index, row in review:
            parts += [f'<li><a href="#question-{index}">第 {index} 题 · {h(row["question"])}</a><br>{h(row["review_note"])}</li>']
    elif _delivered(report):
        parts += ['<li>没有待处理的题目。</li>']
    else:
        parts += ['<li>未交付填写文件，请查看逐题结果。</li>']
    parts += ['</ul></div></section><section aria-labelledby="questions"><h2 id="questions">逐题结果</h2>']
    for index, row in enumerate(report['rows'], 1):
        parts += [f'<article class="question" id="question-{index}"><h3>{index}. {h(row["question"])}</h3>', '<div class="status">' + h(row['status_label']) + '</div>']
        if row.get('source_locations'):
            parts += ['<p class="muted">来源：' + h('；'.join(location_label(loc) for loc in row['source_locations'])) + '</p>']
        if row.get('reason'):
            parts += ['<p class="copy">说明：' + h(row['reason']) + '</p>']
        for note in _generation_notes(row):parts += ['<p class="copy muted">'+h(note)+'</p>']
        if row['answer'] is not None:
            parts += ['<p class="label">实际已写答案</p><div class="answer">' + h(row['answer']) + '</div>']
        if _transformation_note(row):
            parts += ['<p class="label">答案整理与核对</p><div class="copy">' + h(_transformation_note(row)) + '</div>']
        if row['review_note']:
            parts += ['<p class="label">未写入／待处理说明</p><div class="copy">' + h(row['review_note']) + '</div>']
        if report['answer_mode']!='autonomous':
            parts += ['<p class="label">原文引文</p>']
            parts += ['<blockquote>' + h(item['quote']) + '</blockquote>' for item in row['evidence_citations']] if row.get('evidence_citations') else ['<blockquote>' + h(row['evidence_quote']) + '</blockquote>'] if row.get('evidence_quote') else ['<p class="muted">此报告未记录原文引文，请回到答案资料核对。</p>']
        parts += ['</article>']
    parts += ['</section><p class="muted">' + h(summary['说明']) + '</p>',
              '<section class="technical"><h2>技术记录</h2><details><summary>展开原始状态、逐位置读回与校验信息</summary><pre>',
              h(json.dumps(_technical(report), ensure_ascii=False, indent=2)), '</pre></details></section>',
              '<footer>本报告中的已写答案来自交付文件的读回结果。</footer></main></body></html>']
    return ''.join(parts)


def save_report(report, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared = prepare_report(report)
    json_path, markdown_path = output_dir / 'report.json', output_dir / 'report.md'
    dump_json(json_path, prepared)
    markdown_path.write_text(render_markdown(prepared), encoding='utf-8')
    (output_dir / 'report.html').write_text(render_html(prepared), encoding='utf-8')
    return json_path, markdown_path


def location_label(loc):
    parts = [str(loc.get('source_name', ''))]
    if 'page_index' in loc: parts.append('第 ' + str(loc['page_index'] + 1) + ' 页')
    for key in ('sheet', 'cell', 'row', 'line'):
        if loc.get(key) is not None: parts.append(str(loc[key]))
    if loc.get('format_status')=='normalized':parts.append('已按单元格格式 '+str(loc.get('number_format',''))+' 转换（存储值 '+str(loc.get('raw_value',''))+'）')
    return ' · '.join(p for p in parts if p)

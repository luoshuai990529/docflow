"""General workflow: bounded evidence matching, automatic safe omissions, verified delivery."""
from dataclasses import asdict, replace
from pathlib import Path
import hashlib
import json
import secrets
import shutil
from .models import Decision, DocumentData, Verification, RunResult, dump_json, document_from_dict
from .application.answering.match import prepare_match, match_bounded, chunks, chunks_questions
from .domain.decisions import validate_decisions, validate_model_batch, requires_review, supported_conversion, review_decisions
from .adapters.documents.documents import read_document, read_sources
from .adapters.documents.conversion import normalize_office
from .adapters.documents.preflight import digest, Limits, LocalDocumentPreflight
from .adapters.documents.write import write_copy
from .adapters.documents.verify import verify_copy
from .adapters.documents.office import verify_office
from .adapters.documents.report import build_report, save_report
from .domain.publication import can_publish_general_candidate, general_workflow_outcome
from .domain.write_plan import build_write_plan
from .application.answering.answer_support import SupportLedger
from .adapters.documents.answer_layout import annotate_layout, answer_hints, check_answer_fit
from .adapters.documents.questionnaire_structure import LocalQuestionnaireStructure
from .workflow.contracts import NodeResult,NodeStatus,WorkflowContext,WorkflowOperation,WorkflowRequest,WorkflowStage
from .workflow.runner import WorkflowRunner
from .workflow.compatibility import legacy_stage_for


def _answer_layout_records(q, ds, *, results=None):
 """Report actual fit separately from estimated space and semantic decisions."""
 hints=answer_hints(q);answers=q.metadata.setdefault('answer_coverage',{}).setdefault('autonomous_answers',{})
 for d in ds:
  question=next((item for item in q.questions if item.id==d.question_id),None)
  if not question or question.kind!='text' or d.status!='ready':continue
  layout=answers.setdefault(d.question_id,{'origin':'llm'}).setdefault('layout',{})
  for key in ('space_type','suggested_max_chars','max_lines','native_max_length','single_line'):
   if key in hints.get(d.question_id,{}):layout[key]=hints[d.question_id][key]
  layout.setdefault('original_chars',len(d.value));layout['final_chars']=len(d.value)
  layout.setdefault('compression_attempts',0);layout.setdefault('fit_status','unknown')
  if results is not None:
   writes=[item for item in results if item.question_id==d.question_id]
   overflow=any(item.status=='layout_blocked' and item.reason in
    ('TEXT_OVERFLOW','FIXED_ROW_HEIGHT_OVERFLOW','NATIVE_MAX_LENGTH','FIELD_SINGLE_LINE','AREA_TOO_SMALL') for item in writes)
   layout['fit_status']='overflow' if overflow else 'fits' if writes and all(item.status=='written' for item in writes) else 'unknown'
   if layout.get('attempts'):
    layout['attempts'][-1]['fit_status']=layout['fit_status']
    layout['attempts'][-1]['fit_codes']=sorted({item.reason for item in writes if item.status=='layout_blocked' and item.reason in
     ('TEXT_OVERFLOW','FIXED_ROW_HEIGHT_OVERFLOW','NATIVE_MAX_LENGTH','FIELD_SINGLE_LINE','AREA_TOO_SMALL')})

def prepare_general(questionnaire, sources, bundle, processor=None, progress=None, *, answer_mode='grounded'):
 if answer_mode not in ('grounded','autonomous'):raise ValueError('ANSWER_MODE')
 if answer_mode=='autonomous' and sources:raise ValueError('AUTONOMOUS_SOURCES')
 if processor is None:
  from .adapters.processor.processor import Processor
  processor=Processor()
 bundle=Path(bundle);work=bundle.parent/(bundle.name+'-work');work.mkdir(parents=True,exist_ok=False)
 original=Path(questionnaire);normalized=original;conversion=None
 try:
  if progress:progress('reading',phase='questionnaire',completed=0,total=1)
  if original.suffix.lower() in ('.doc','.xls'):normalized,conversion=normalize_office(original,work/'conversion',processor)
  q=read_document(normalized,'questionnaire',work/'questionnaire',processor)
  from .adapters.documents.catalog import PDF_HASH, TEMPLATES
  if q.kind=='pdf' and q.sha256==PDF_HASH:
   from .adapters.documents.extract import extract_questionnaire, load_template
   trusted=extract_questionnaire(normalized,load_template(TEMPLATES/'supplier-pdf.json'))
   trusted.metadata=q.metadata; q=trusted
   q.metadata['question_inventory']={'status':'template_verified','unlocated_questions':0}
  q.metadata['original_sha256']=digest(original)
  if answer_mode=='autonomous':q.metadata['answer_mode']=answer_mode
  annotate_layout(normalized,q)
  s=(DocumentData('collection',hashlib.sha256(b'[]').hexdigest(),[],[],[],{'coverage':[]})
     if answer_mode=='autonomous' else read_sources(sources,work/'sources',processor,progress=progress))
  if q.kind in ('docx','xlsx'):
   if progress:progress('reading',phase='baseline',completed=0,total=1)
   baseline=processor.convert(normalized,'pdf',work/'baseline.pdf')
   import pymupdf as M
   with M.open(baseline) as pdf:
    if len(pdf)>Limits().max_pages:raise ValueError('PAGE_LIMIT')
  prepare_match(q,s,bundle)
  if q.kind in ('docx','xlsx'):shutil.copy2(baseline,bundle/'baseline.pdf')
  shutil.copy2(normalized,bundle/('questionnaire.'+q.kind))
  dump_json(bundle/'metadata.json',{'schema_version':2,'questionnaire_original_sha256':digest(original),
   'questionnaire_sha256':q.sha256,'source_sha256':s.sha256,'source_paths':[str(Path(p).resolve()) for p in sources],
   'source_hashes':[digest(p) for p in sources],'questionnaire_path':str(original.resolve()),'conversion':conversion,'answer_mode':answer_mode})
  return q,s,conversion
 finally:shutil.rmtree(work,ignore_errors=True)


def execute_general(original,q,s,ds,output_dir,processor=None,progress=None,conversion=None,baseline=None,*,answer_repair=None):
 output_dir=Path(output_dir);output_dir.mkdir(parents=True,exist_ok=False);results=[];output=None
 unlocated={question.id for question in q.questions if not question.target_ids}
 ds=[replace(d,status='needs_review',value=None,option_ids=[],reason='已识别题目，但未能可靠定位填写区域，已留空。') if d.question_id in unlocated else d for d in ds]
 try:
  if answer_repair:
   # The same writer geometry decides fit; an estimated character budget is never
   # a gate. Only failed text answers are sent back to the model, at most twice.
   while True:
    failures={d.question_id:issue for d in ds if (issue:=check_answer_fit(original,q,d))}
    if not failures:break
    repaired=answer_repair(ds,failures)
    if repaired is None:break
    ds=repaired
  if any(d.status=='ready' for d in ds):
   output=output_dir/('filled.'+q.kind)
   defer_fixed=bool(answer_repair)
   while True:
    if progress:progress('writing')
    if q.kind=='pdf' and any('answer_budget' not in target.constraints for target in q.targets if target.constraints.get('content_type')!='checkbox'):
     annotate_layout(original,q)
    plan=build_write_plan(q,ds,'real')
    results=write_copy(original,output,plan)
    if progress:progress('verifying')
    work=output_dir/'verification-work'
    try:
     if q.kind in ('docx','xlsx'):
      office_processor=processor
      if office_processor is None:
       from .adapters.processor.processor import Processor
       office_processor=Processor()
      results,v=verify_office(original,output,q,results,work,office_processor,baseline=baseline,defer_fixed_overflow=defer_fixed)
     else:results,v=verify_copy(original,output,q,results,work)
    finally:shutil.rmtree(work,ignore_errors=True)
    fixed={}
    for result in results:
     if q.kind in ('docx','xlsx') and result.status=='layout_blocked' and result.reason in ('FIXED_ROW_HEIGHT_OVERFLOW','TEXT_OVERFLOW'):
      budget=next((t.constraints.get('answer_budget',{}) for t in q.targets if t.id==result.target_id),{})
      fixed.setdefault(result.question_id,[]).append(dict(budget,target_id=result.target_id,code=result.reason))
    if not (fixed and defer_fixed):break
    repaired=answer_repair(ds,fixed)
    if repaired is None:defer_fixed=False  # Existing verified omission path is final.
    else:ds=repaired
    output.unlink()  # Always rebuild from the immutable original, never edit a failed copy.
   if not can_publish_general_candidate(v,results):
    output.unlink();output=None
    for r in results:
     if r.status=='written':r.status='candidate_not_delivered';r.reason='整份交付校验未通过。'
  else:v=Verification(True,'not_run',True,{'note':'没有确认可写入的答案。'})
  if q.metadata.get('answer_mode')=='autonomous':_answer_layout_records(q,ds,results=results)
  report=build_report(q.questions,ds,results,v,{'questionnaire':q.sha256,'source':s.sha256},digest(output) if output else None,'real',targets=q.targets)
  report['input_original_questionnaire_sha256']=q.metadata.get('original_sha256',q.sha256)
  report['answer_mode']=q.metadata.get('answer_mode','grounded')
  if report['answer_mode']=='autonomous':report['autonomous_context']=q.metadata.get('answer_coverage',{}).get('autonomous_context',{})
  if q.metadata.get('engine'):report['engine']=q.metadata['engine']
  if q.metadata.get('decision_engine'):report['decision_engine']=q.metadata['decision_engine']
  report['published_file']=output.name if output else None;report['origin']='llm'
  report['questionnaire_conversion']=conversion
  report['coverage']={'questionnaire':q.metadata,'sources':s.metadata}
  report['question_inventory']=q.metadata.get('question_inventory',{})
  report['model_diagnostics']={key:q.metadata.get('answer_coverage',{}).get(key,[] if key=='validation_events' else 0)
   for key in ('validation_events','validation_events_truncated')}
  incomplete=q.metadata.get('answer_coverage',{}).get('incomplete_questions',[])
  report['processing_warnings']=[]
  if report['question_inventory'].get('status')=='incomplete':report['processing_warnings'].append('部分内容未完成题目识别；以下统计仅代表已识别的题目。')
  if incomplete:report['processing_warnings'].append('部分题目未完成模型作答，请查看逐题说明。' if report['answer_mode']=='autonomous' else '部分答案资料未完整处理，已保留有依据的答案；请结合逐题说明使用。')
  for row in report['rows']:
   d=next((d for d in ds if d.question_id==row.get('question_id')),None)
   if d:row['evidence_citations']=d.evidence
   if d:row['source_locations']=[span.locator for span in s.spans if span.id in d.span_ids]
   support=q.metadata.get('answer_coverage',{}).get('support_reviews',{}).get(row['question_id'])
   if support:row['answer_review']=support
   generated=q.metadata.get('answer_coverage',{}).get('autonomous_answers',{}).get(row['question_id'])
   if report['answer_mode']=='autonomous':row['autonomous_answer']=generated or {'origin':'llm','assumptions':''}
  j,m=save_report(report,output_dir)
  outcome=general_workflow_outcome(v,output,report['processing_warnings'],report['rows'])
  return RunResult(output,j,m,outcome)
 except BaseException:
  if output is not None:output.unlink(missing_ok=True)
  raise


def _verify_run_result(result,output_dir):
 root=Path(output_dir).resolve()
 report_path=Path(result.report_json).resolve()
 if not report_path.is_relative_to(root) or not report_path.is_file():raise ValueError('REPORT_DELIVERY_MISMATCH')
 report=json.loads(report_path.read_text(encoding='utf-8'))
 if result.output_path is None:
  if report.get('published_file') is not None or report.get('output_sha256') is not None:
   raise ValueError('REPORT_DELIVERY_MISMATCH')
 else:
  output=Path(result.output_path).resolve()
  if (not output.is_relative_to(root) or not output.is_file()
      or report.get('published_file')!=output.name or digest(output)!=report.get('output_sha256')
      or not report.get('verification',{}).get('content_ok')):
   if output.is_relative_to(root):output.unlink(missing_ok=True)
   raise ValueError('VERIFICATION_FAILED')
 return result


class _GeneralFunctionNode:
 def __init__(self,stage,handler):self.stage=stage;self.handler=handler
 def execute(self,context):return self.handler(context)


def run_general(questionnaire,sources,config,output_dir,client,progress=None,review=None,cancelled=lambda:False,*,answer_mode='grounded',decision_client=None,processor=None,workflow_progress=None):
 questionnaire=Path(questionnaire);sources=[Path(path) for path in sources];output_dir=Path(output_dir)
 handlers={}

 def preflight(_context):
  if answer_mode not in ('grounded','autonomous'):raise ValueError('ANSWER_MODE')
  if answer_mode=='autonomous' and sources:raise ValueError('AUTONOMOUS_SOURCES')
  if output_dir.exists():raise ValueError('OUTPUT_EXISTS')
  checker=LocalDocumentPreflight();checker.inspect_file(questionnaire,'questionnaire')
  if answer_mode=='grounded':checker.inspect_sources(sources)
  selected=processor
  if selected is None:
   from .adapters.processor.processor import Processor
   selected=Processor(cancelled=cancelled)
  if cancelled():raise ValueError('TASK_CANCELLED')
  return NodeResult(WorkflowStage.PREFLIGHT,artifacts={'processor':selected,'input_questionnaire':questionnaire,
   'input_sources':sources,'output_dir':output_dir,'bundle':output_dir.parent/'prepare'})

 def extract(context):
  q,s,conversion=prepare_general(context.artifacts['input_questionnaire'],context.artifacts['input_sources'],
   context.artifacts['bundle'],context.artifacts['processor'],progress,answer_mode=answer_mode)
  q.metadata['engine']=config.metadata()
  if answer_mode=='autonomous' and decision_client is not None and getattr(decision_client,'config',None):
   q.metadata['decision_engine']=decision_client.config.metadata()
  return NodeResult(WorkflowStage.EXTRACT,artifacts={'questionnaire':q,'source':s,'conversion':conversion})

 def understand(context):
  q=context.artifacts['questionnaire'];bundle=context.artifacts['bundle']
  if cancelled():raise ValueError('TASK_CANCELLED')
  from .application.answering.questionnaire import interpret_questionnaire
  interpret_questionnaire(q,bundle/('questionnaire.'+q.kind),client,cancelled,progress,
                          structure=LocalQuestionnaireStructure())
  dump_json(bundle/'interpreted-questionnaire.json',q)
  return NodeResult(WorkflowStage.UNDERSTAND,artifacts={'located_questions':[question for question in q.questions if question.target_ids]})

 def answer(context):
  q=context.artifacts['questionnaire'];s=context.artifacts['source'];located=context.artifacts['located_questions']
  coverage={};support=SupportLedger();hints=None
  if answer_mode=='autonomous':
   from .application.answering.autonomous import generate_autonomous,compact_answers
   hints=answer_hints(q)
   decisions=generate_autonomous(located,client,decision_client,progress=progress,cancelled=cancelled,
    coverage=coverage,answer_constraints=hints);candidates={}
  else:
   from .application.answering.autonomous import compact_answers
   decisions,candidates=match_bounded(client,located,s.spans,progress,cancelled,coverage=coverage,text_support=support)
  q.metadata['answer_coverage']=coverage
  return NodeResult(WorkflowStage.ANSWER,artifacts={'answer_coverage':coverage,'text_support':support,
   'answer_hints':hints,'initial_decisions':decisions,'answer_candidates':candidates,
   'compact_answers':compact_answers})

 def validate(context):
  q=context.artifacts['questionnaire'];s=context.artifacts['source'];decisions=list(context.artifacts['initial_decisions'])
  coverage=context.artifacts['answer_coverage'];located=context.artifacts['located_questions']
  decisions.extend(Decision(question.id,'needs_review',None,[],[],None,'已识别题目，但未能可靠定位填写区域，已留空。')
   for question in q.questions if not question.target_ids)
  targets={target.id:target for target in q.targets}
  for index,decision in enumerate(decisions):
   question=next(item for item in q.questions if item.id==decision.question_id)
   if decision.status=='ready' and any(targets[target_id].constraints.get('position_review') for target_id in question.target_ids):
    decisions[index]=replace(decision,status='needs_review',value=None,option_ids=[],reason='填写位置无法可靠确定，已留空。')
  if review and answer_mode=='grounded' and any(decision.status!='ready' for decision in decisions):
   decisions=review_decisions(decisions,context.artifacts['answer_candidates'],
    review(q,s,decisions,context.artifacts['answer_candidates']))
  decisions=validate_decisions(decisions,q.questions,s.spans,'real',text_support=context.artifacts['text_support'],answer_mode=answer_mode)
  repair=None
  if answer_mode=='autonomous':
   _answer_layout_records(q,decisions)
   compact_answers=context.artifacts['compact_answers'];hints=context.artifacts['answer_hints']
   def repair(current,failures):
    for identity,issues in failures.items():
     layout=coverage.get('autonomous_answers',{}).get(identity,{}).get('layout',{})
     if layout.get('attempts'):layout['attempts'][-1].update(fit_status='overflow',fit_codes=sorted({item['code'] for item in issues}))
    eligible={question.id:failures[question.id] for question in located if question.kind=='text' and question.id in failures
     and coverage.get('autonomous_answers',{}).get(question.id,{}).get('layout',{}).get('compression_attempts',0)<2
     and any(issue.get('code') in ('TEXT_OVERFLOW','FIXED_ROW_HEIGHT_OVERFLOW','NATIVE_MAX_LENGTH','FIELD_SINGLE_LINE') for issue in failures[question.id])}
    if not eligible:return None
    round_number=max(coverage['autonomous_answers'][identity]['layout']['compression_attempts']+1 for identity in eligible)
    return compact_answers(located,current,client,hints,eligible,coverage=coverage,cancelled=cancelled,
     progress=progress,round_number=round_number)
  if cancelled():raise ValueError('TASK_CANCELLED')
  return NodeResult(WorkflowStage.VALIDATE,artifacts={'decisions':decisions,'answer_repair':repair})

 def write(context):
  if cancelled():raise ValueError('TASK_CANCELLED')
  q=context.artifacts['questionnaire'];s=context.artifacts['source'];bundle=context.artifacts['bundle']
  result=execute_general(bundle/('questionnaire.'+q.kind),q,s,context.artifacts['decisions'],
   context.artifacts['output_dir'],context.artifacts['processor'],progress,context.artifacts['conversion'],
   baseline=bundle/'baseline.pdf' if q.kind in ('docx','xlsx') else None,
   answer_repair=context.artifacts['answer_repair'])
  return NodeResult(WorkflowStage.WRITE,artifacts={'unverified_run_result':result})

 def verify(context):
  return NodeResult(WorkflowStage.VERIFY,artifacts={'run_result':_verify_run_result(
   context.artifacts['unverified_run_result'],context.artifacts['output_dir'])})

 handlers.update({WorkflowStage.PREFLIGHT:preflight,WorkflowStage.EXTRACT:extract,
  WorkflowStage.UNDERSTAND:understand,WorkflowStage.ANSWER:answer,WorkflowStage.VALIDATE:validate,
  WorkflowStage.WRITE:write,WorkflowStage.VERIFY:verify})
 def emit(event):
  if workflow_progress is not None:
   workflow_progress(event)
  elif progress is not None and event.status==NodeStatus.RUNNING:
   progress(legacy_stage_for(event.node_id),phase=event.node_id)
 nodes=[_GeneralFunctionNode(stage,handlers[stage]) for stage in WorkflowStage]
 request=WorkflowRequest('general-'+secrets.token_hex(12),WorkflowOperation.RUN,answer_mode)
 result=WorkflowRunner(nodes,progress=emit).run(WorkflowContext(request,cancelled=cancelled))
 if result.failure is not None:raise result.failure
 return result.artifacts['run_result']


def apply_general(bundle,answers,output,mode,workflow_progress=None):
 bundle=Path(bundle);answers=Path(answers);output=Path(output)
 handlers={};temporary_directories=[]

 def preflight(_context):
  if mode!='real':raise ValueError('GENERAL_REAL_ONLY')
  if output.exists():raise ValueError('OUTPUT_EXISTS')
  if not answers.is_file():raise ValueError('MATCH_INPUT_REQUIRED')
  meta=json.loads((bundle/'metadata.json').read_text(encoding='utf-8'))
  answer_mode=meta.get('answer_mode','grounded')
  if answer_mode not in ('grounded','autonomous'):raise ValueError('ANSWER_MODE')
  if digest(Path(meta['questionnaire_path']))!=meta['questionnaire_original_sha256']:
   raise ValueError('SOURCE_CHANGED')
  if [digest(Path(path)) for path in meta['source_paths']]!=meta['source_hashes']:
   raise ValueError('SOURCE_CHANGED')
  return NodeResult(WorkflowStage.PREFLIGHT,artifacts={'metadata':meta,'answer_mode':answer_mode})

 def extract(context):
  meta=context.artifacts['metadata'];answer_mode=context.artifacts['answer_mode']
  q=document_from_dict(json.loads((bundle/'questionnaire.json').read_text(encoding='utf-8')))
  s=document_from_dict(json.loads((bundle/'source.json').read_text(encoding='utf-8')))
  # Re-extract the bundle; locators and evidence from editable debug files are never trusted.
  import tempfile
  temporary=tempfile.TemporaryDirectory(dir=bundle.parent)
  temporary_directories.append(temporary)
  root=Path(temporary.name)
  try:
   freshq,freshs,_=prepare_general(Path(meta['questionnaire_path']),[Path(path) for path in meta['source_paths']],
    root/'prepare',answer_mode=answer_mode)
   def semantic(document):
    value=asdict(document);value.pop('sha256',None)
    for span in value['spans']:span.pop('source_sha256',None)
    for target in value['targets']:
     for key in ('answer_budget','pdf_text_layouts','pdf_field_type'):target['constraints'].pop(key,None)
    return value
   if semantic(q)!=semantic(freshq) or semantic(s)!=semantic(freshs):
    raise ValueError('BUNDLE_CONTENT_CHANGED')
   # Conversion containers contain volatile IDs/timestamps. Compare re-extracted contracts, then
   # write only the freshly converted copy. The original input hashes remain authoritative.
   fresh_original=root/'prepare'/('questionnaire.'+freshq.kind)
   if digest(bundle/('questionnaire.'+q.kind))!=q.sha256:
    raise ValueError('BUNDLE_CONTENT_CHANGED')
   verified_original=root/('verified.'+freshq.kind)
   shutil.copy2(fresh_original,verified_original)
   return NodeResult(WorkflowStage.EXTRACT,artifacts={'questionnaire':freshq,'source':freshs,
    'bundle_questionnaire_sha256':q.sha256,'bundle_source_sha256':s.sha256,
    'verified_original':verified_original,'conversion':meta.get('conversion')})
  except BaseException:
   temporary.cleanup()
   raise

 def validate(context):
  from .application.answering.match import AgentJsonMatcher
  payload=json.loads(answers.read_text(encoding='utf-8'))
  if payload['origin'] not in ('agent','reviewed'):raise ValueError('MOCK_IN_REAL')
  q=context.artifacts['questionnaire'];s=context.artifacts['source']
  if (payload['questionnaire_sha256']!=context.artifacts['bundle_questionnaire_sha256']
      or payload['source_sha256']!=context.artifacts['bundle_source_sha256']):
   raise ValueError('ANSWER_SOURCE_HASH_MISMATCH')
  matcher=AgentJsonMatcher(answers)
  decisions=validate_decisions(matcher.match(q.questions,s.spans),q.questions,s.spans,'real',
   answer_mode=context.artifacts['answer_mode'])
  for decision in decisions:
   if decision.status=='ready' and payload['origin']!='reviewed':
    question=next(item for item in q.questions if item.id==decision.question_id)
    if ((context.artifacts['answer_mode']=='grounded' and requires_review(question,decision,s.spans))
        or any(target.constraints.get('position_review') for target in q.targets
               if target.id in question.target_ids)):
     raise ValueError('REVIEW_REQUIRED')
  return NodeResult(WorkflowStage.VALIDATE,artifacts={'decisions':decisions,'origin':payload['origin']})

 def write(context):
  result=execute_general(context.artifacts['verified_original'],context.artifacts['questionnaire'],
   context.artifacts['source'],context.artifacts['decisions'],output,conversion=context.artifacts['conversion'])
  return NodeResult(WorkflowStage.WRITE,artifacts={'unverified_run_result':result})

 def verify(context):
  return NodeResult(WorkflowStage.VERIFY,artifacts={'run_result':_verify_run_result(
   context.artifacts['unverified_run_result'],output)})

 handlers.update({WorkflowStage.PREFLIGHT:preflight,WorkflowStage.EXTRACT:extract,
  WorkflowStage.VALIDATE:validate,WorkflowStage.WRITE:write,WorkflowStage.VERIFY:verify})
 nodes=[_GeneralFunctionNode(stage,handlers.get(stage,lambda _context,stage=stage:NodeResult(stage,NodeStatus.SKIPPED)))
  for stage in WorkflowStage]
 request=WorkflowRequest('apply-'+secrets.token_hex(12),WorkflowOperation.APPLY)
 def emit(event):
  if workflow_progress is not None:workflow_progress(event)
 result=None
 try:
  result=WorkflowRunner(nodes,progress=emit).run(WorkflowContext(request))
  if result.failure is not None:raise result.failure
  return result.artifacts['run_result']
 finally:
  for temporary in temporary_directories:temporary.cleanup()

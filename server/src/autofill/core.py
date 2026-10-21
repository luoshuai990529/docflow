"""Fixed document workflow with optional independent LLM matching."""
from pathlib import Path
from dataclasses import asdict
import json
import secrets
from .adapters.documents.extract import extract_source,extract_questionnaire,load_template,sha
from .adapters.documents.preflight import LocalDocumentPreflight
from .models import dump_json,document_from_dict,Verification,RunResult
from .application.answering.match import prepare_match,AgentJsonMatcher
from .domain.decisions import validate_decisions
from .adapters.documents.write import write_copy
from .adapters.documents.verify import verify_copy
from .adapters.documents.report import build_report,save_report
from .domain.publication import can_publish_candidate, workflow_outcome
from .domain.write_plan import TEST_LABEL, build_write_plan
from .workflow.contracts import NodeResult,NodeStatus,WorkflowContext,WorkflowOperation,WorkflowRequest,WorkflowStage
from .workflow.runner import WorkflowRunner
from .workflow.compatibility import legacy_stage_for


class _FunctionNode:
 def __init__(self,stage,handler):self.stage=stage;self.handler=handler
 def execute(self,context):return self.handler(context)


def _run_fixed(operation,initial,handlers,progress=None,workflow_progress=None,answer_mode='grounded'):
 """Run the fixed-template entrypoint through the shared seven-node contract."""
 def emit(event):
  if workflow_progress is not None:workflow_progress(event)
  elif progress is not None and event.status==NodeStatus.RUNNING:
   progress(legacy_stage_for(event.node_id),phase=event.node_id)
 nodes=[_FunctionNode(stage,handlers[stage]) for stage in WorkflowStage]
 context=WorkflowContext(WorkflowRequest('core-'+secrets.token_hex(12),operation,answer_mode),artifacts=initial)
 result=WorkflowRunner(nodes,progress=emit).run(context)
 if result.failure is not None:raise result.failure
 return result.artifacts


def _output_handlers(progress=None):
 def write(context):
  original=context.artifacts['original'];q=context.artifacts['questionnaire'];s=context.artifacts['source']
  ds=context.artifacts['decisions'];mode=context.artifacts['mode'];output_dir=Path(context.artifacts['output_dir'])
  if mode=='fictional_test' and not output_dir.name.startswith('TEST-SAMPLE'):raise ValueError('TEST_FILENAME_REQUIRED')
  output_dir.mkdir(parents=True,exist_ok=False)
  output=None;results=[]
  if any(decision.status=='ready' for decision in ds):
   output=output_dir/(('TEST-SAMPLE-' if mode=='fictional_test' else '')+'filled.'+q.kind)
   if progress:progress('writing')
   if q.kind=='pdf' and any('answer_budget' not in target.constraints for target in q.targets if target.constraints.get('content_type')!='checkbox'):
    from .adapters.documents.answer_layout import annotate_layout
    annotate_layout(original,q)
   plan=build_write_plan(q,ds,mode)
   results=write_copy(original,output,plan)
  return NodeResult(WorkflowStage.WRITE,artifacts={'candidate_path':output,'write_results':results})

 def verify(context):
  original=context.artifacts['original'];q=context.artifacts['questionnaire'];s=context.artifacts['source']
  ds=context.artifacts['decisions'];mode=context.artifacts['mode'];output_dir=Path(context.artifacts['output_dir'])
  output=context.artifacts['candidate_path'];results=context.artifacts['write_results']
  if output is not None:
   if progress:progress('verifying')
   results,v=verify_copy(original,output,q,results,output_dir/'page-images')
   if mode=='fictional_test':
    labels={target.id for target in q.targets if target.constraints.get('test_label')}
    if not any(result.target_id in labels and result.status=='written' for result in results):
     v.content_ok=False;v.needs_review=True
   if not can_publish_candidate(v,results):
    output.rename(output_dir/('candidate-unverified.'+q.kind));output=None
    for result in results:
     if result.status=='written':
      result.status='candidate_not_delivered';result.reason='Verification failed; candidate retained for inspection'
  else:
   v=Verification(True,'not_run',True,{'note':'No ready answers; report only'})
  report=build_report(q.questions,ds,results,v,{'questionnaire':q.sha256,'source':s.sha256},sha(output) if output else None,mode,targets=q.targets)
  report['published_file']=output.name if output else None
  report['origin']=context.artifacts['origin']
  engine=context.artifacts.get('engine')
  if engine is not None:report['engine']=engine
  report_json,report_markdown=save_report(report,output_dir)
  outcome=workflow_outcome(v,report['rows'])
  return NodeResult(WorkflowStage.VERIFY,artifacts={'run_result':RunResult(output,report_json,report_markdown,outcome)})

 return {WorkflowStage.WRITE:write,WorkflowStage.VERIFY:verify}

def prepare_pair(questionnaire:Path,source:Path,template_path:Path|None,output_dir:Path)->Path:
 questionnaire=Path(questionnaire).resolve();source=Path(source).resolve()
 template=load_template(template_path) if template_path else None
 q=extract_questionnaire(questionnaire,template);s=extract_source(source);bundle=prepare_match(q,s,output_dir)
 dump_json(bundle/'metadata.json',{'schema_version':1,'questionnaire_path':str(questionnaire),'source_path':str(source),'questionnaire_sha256':q.sha256,'source_sha256':s.sha256,'template':template})
 return bundle

def apply_pair(bundle_dir:Path,answers_path:Path,output_dir:Path,mode:str,workflow_progress=None)->RunResult:
 bundle_dir=Path(bundle_dir);output_dir=Path(output_dir);answers_path=Path(answers_path)
 # Route schema-v2 bundles through the general application use case.
 meta=json.loads((bundle_dir/'metadata.json').read_text(encoding='utf-8'))
 if meta.get('schema_version')==2:
  from .general import apply_general
  return apply_general(bundle_dir,answers_path,output_dir,mode,workflow_progress=workflow_progress)

 handlers={}
 def preflight(context):
  if not meta.get('template'):raise ValueError('UNSUPPORTED_TEMPLATE')
  original=Path(meta['questionnaire_path']);source=Path(meta['source_path'])
  if sha(original)!=meta['questionnaire_sha256'] or sha(source)!=meta['source_sha256']:
   raise ValueError('SOURCE_CHANGED')
  preflight=LocalDocumentPreflight();preflight.inspect_file(original,'questionnaire');preflight.inspect_file(source,'source')
  return NodeResult(WorkflowStage.PREFLIGHT,artifacts={'metadata':meta,'original':original,'source_path':source})

 def extract(context):
  meta=context.artifacts['metadata'];original=context.artifacts['original'];source=context.artifacts['source_path']
  q=extract_questionnaire(original,meta['template']);s=extract_source(source)
  if json.loads((bundle_dir/'questionnaire.json').read_text())!=asdict(q) or json.loads((bundle_dir/'source.json').read_text())!=asdict(s):
   raise ValueError('BUNDLE_CONTENT_CHANGED')
  return NodeResult(WorkflowStage.EXTRACT,artifacts={'questionnaire':q,'source':s})

 def validate(context):
  q=context.artifacts['questionnaire'];s=context.artifacts['source']
  if not answers_path.is_file():raise ValueError('MATCH_INPUT_REQUIRED')
  matcher=AgentJsonMatcher(answers_path)
  if mode=='real' and matcher.payload['origin']=='synthetic_test':raise ValueError('MOCK_IN_REAL')
  if matcher.payload['questionnaire_sha256']!=q.sha256 or matcher.payload['source_sha256']!=s.sha256:
   raise ValueError('ANSWER_SOURCE_HASH_MISMATCH')
  ds=validate_decisions(matcher.match(q.questions,s.spans),q.questions,s.spans,mode)
  return NodeResult(WorkflowStage.VALIDATE,artifacts={'decisions':ds,'mode':mode,'origin':matcher.payload['origin'],
   'output_dir':output_dir})

 handlers.update({WorkflowStage.PREFLIGHT:preflight,WorkflowStage.EXTRACT:extract,
  WorkflowStage.UNDERSTAND:lambda _context:NodeResult(WorkflowStage.UNDERSTAND,NodeStatus.SKIPPED),
  WorkflowStage.ANSWER:lambda _context:NodeResult(WorkflowStage.ANSWER,NodeStatus.SKIPPED),
  WorkflowStage.VALIDATE:validate})
 handlers.update(_output_handlers())
 artifacts=_run_fixed(WorkflowOperation.APPLY,{},handlers,workflow_progress=workflow_progress)
 return artifacts['run_result']


def run_pair(questionnaire,source,template_path,config,output_dir,client=None,progress=None,workflow_progress=None):
 from .application.answering.match import LLMMatcher
 original=Path(questionnaire).resolve();source=Path(source).resolve()
 output_dir=Path(output_dir)
 handlers={}
 def preflight(context):
  if client is None:raise ValueError('MODEL_CLIENT_REQUIRED')
  if output_dir.exists():raise ValueError('OUTPUT_EXISTS')
  checker=LocalDocumentPreflight();checker.inspect_file(original,'questionnaire');checker.inspect_file(source,'source')
  return NodeResult(WorkflowStage.PREFLIGHT,artifacts={'original':original,'source_path':source})

 def extract(context):
  q=extract_questionnaire(original,load_template(template_path));s=extract_source(source)
  return NodeResult(WorkflowStage.EXTRACT,artifacts={'questionnaire':q,'source':s})

 def understand(context):
  q=context.artifacts['questionnaire'];s=context.artifacts['source'];mode='real';candidate_dir=output_dir;owner=None
  test_span=next((span for span in s.spans if TEST_LABEL in span.text),None)
  if test_span is not None:
   labels=[target for target in q.targets if target.constraints.get('test_label')]
   if len(labels)!=1:raise ValueError('TEST_LABEL_TARGET_REQUIRED')
   owner=next(question for question in q.questions if labels[0].id in question.target_ids)
   mode='fictional_test';candidate_dir=output_dir.with_name('TEST-SAMPLE-'+output_dir.name)
  if candidate_dir.exists():raise ValueError('OUTPUT_EXISTS')
  return NodeResult(WorkflowStage.UNDERSTAND,artifacts={'mode':mode,'output_dir':candidate_dir,
   'test_span':test_span,'test_owner':owner})

 def answer(context):
  q=context.artifacts['questionnaire'];s=context.artifacts['source']
  matcher=LLMMatcher(client,progress);decisions=matcher.match(q.questions,s.spans)
  return NodeResult(WorkflowStage.ANSWER,artifacts={'raw_decisions':decisions,'matcher':matcher})

 def validate(context):
  q=context.artifacts['questionnaire'];s=context.artifacts['source'];mode=context.artifacts['mode']
  decisions=validate_decisions(context.artifacts['raw_decisions'],q.questions,s.spans,'real')
  test_span=context.artifacts['test_span'];owner=context.artifacts['test_owner']
  if test_span is not None:
   from .models import Decision
   decisions=[decision for decision in decisions if decision.question_id!=owner.id]+[
    Decision(owner.id,'ready',TEST_LABEL,[],[test_span.id],TEST_LABEL,'Program-enforced fictional test label')]
  if sha(original)!=q.sha256 or sha(source)!=s.sha256:raise ValueError('SOURCE_CHANGED')
  matcher=context.artifacts['matcher']
  engine=config.metadata()|{'request_id':matcher.request_id,'request_ids':matcher.request_ids}
  return NodeResult(WorkflowStage.VALIDATE,artifacts={'decisions':decisions,'origin':'llm','engine':engine})

 handlers.update({WorkflowStage.PREFLIGHT:preflight,WorkflowStage.EXTRACT:extract,
  WorkflowStage.UNDERSTAND:understand,WorkflowStage.ANSWER:answer,WorkflowStage.VALIDATE:validate})
 handlers.update(_output_handlers(progress))
 return _run_fixed(WorkflowOperation.RUN,{},handlers,progress,workflow_progress)['run_result']

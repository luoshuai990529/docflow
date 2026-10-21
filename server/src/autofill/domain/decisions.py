"""Pure answer contracts, evidence checks and explicit review decisions.

Strict validation is used by offline apply. Model batch validation withholds
ambiguous identities and unsupported evidence without guessing their targets.
"""
from dataclasses import fields, replace
from collections import Counter
import re
from .models import Decision, Evidence, Question, SourceSpan

DECISION_KEYS={f.name for f in fields(Decision) if f.name!='evidence'}
DECISION_FIELDS=DECISION_KEYS|{'evidence'}
STATUSES={'ready','not_found','conflict','needs_review','model_omitted'}
ANSWER_SCHEMA={'type':'object','additionalProperties':False,'required':['origin','questionnaire_sha256','source_sha256','decisions'],'properties':{'origin':{'enum':['agent','reviewed','synthetic_test']},'questionnaire_sha256':{'type':'string'},'source_sha256':{'type':'string'},'decisions':{'type':'array','items':{'type':'object','additionalProperties':False,'required':sorted(DECISION_KEYS),'properties':{'question_id':{'type':'string'},'status':{'type':'string','enum':sorted(STATUSES)},'value':{'type':['string','null']},'option_ids':{'type':'array','items':{'type':'string'}},'span_ids':{'type':'array','items':{'type':'string'}},'quote':{'type':['string','null']},'reason':{'type':'string'}}}}}}

LLM_SCHEMA={'type':'object','additionalProperties':False,'required':['decisions'],'properties':{'decisions':ANSWER_SCHEMA['properties']['decisions']}}

ANSWER_SCHEMA['properties']['decisions']['items']['properties']['evidence']={'type':'array','items':{'type':'object','additionalProperties':False,'required':['span_id','quote'],'properties':{'span_id':{'type':'string'},'quote':{'type':'string'}}}}

WIRE_KEYS={'question_id','status','value','option_ids','evidence','reason'}

__all__ = [
 'ANSWER_SCHEMA','LLM_SCHEMA','DECISION_KEYS','DECISION_FIELDS','STATUSES','WIRE_KEYS',
 'batch_schema','validation_failure','validation_detail','decode_model_batch',
 'validate_decisions','validate_llm_choice','supported_conversion','text_answer_supported',
 'requires_review','validate_model_batch','review_decisions','original_quote',
]


def _evidence_mapping(value):
 if isinstance(value,Evidence):return {'span_id':value.span_id,'quote':value.quote}
 if isinstance(value,dict):return value
 return None

def batch_schema(questions,spans):
 """Bind model output to this batch; the offline Decision format stays stable."""
 span_id={'type':'string','enum':[s.id for s in spans]} if spans else {'type':'string'}
 evidence={'type':'array','items':{'type':'object','additionalProperties':False,
  'required':['span_id','quote'],'properties':{'span_id':span_id,'quote':{'type':'string','minLength':1,'maxLength':400}}}}
 if not spans:evidence['maxItems']=0
 branches=[]
 for q in questions:
  options={'type':'array','items':{'type':'string','enum':q.options}} if q.options else {'type':'array','items':{'type':'string'},'maxItems':0}
  if q.kind=='text':options={'type':'array','items':{'type':'string'},'maxItems':0}
  elif q.options and q.kind in ('single_choice','boolean'):options['maxItems']=1
  properties={'question_id':{'type':'string','enum':[q.id]},'status':{'type':'string','enum':sorted(STATUSES)},
   'value':{'type':['string','null']} if q.kind=='text' else {'type':'null'},
   'option_ids':options,'evidence':{'$ref':'#/$defs/evidence'},'reason':{'type':'string'}}
  branches.append({'type':'object','additionalProperties':False,'required':sorted(WIRE_KEYS),'properties':properties})
 decisions={'type':'array','items':{'anyOf':branches}} if branches else {'type':'array','items':{'type':'object','properties':{},'required':[],'additionalProperties':False},'maxItems':0}
 # Count source IDs only once against structured-output enum limits, even
 # though each question has its own constrained answer and option shape.
 return {'type':'object','additionalProperties':False,'required':['decisions'],'properties':{'decisions':decisions},'$defs':{'evidence':evidence}}


def validation_failure(question_id,code,issues=None):
 """Machine diagnostics travel separately from the concise user explanation."""
 if issues is not None:issues[question_id]=code
 if code in ('EVIDENCE_ID','QUOTE_MISMATCH'):
  reason='这题的答案引用未能与资料原文核对，未采用该答案。'
 elif code.startswith('CHOICE_'):
  reason='这题的选项未能与问卷选项核对，未采用该答案。'
 elif code=='TEXT_EVIDENCE':reason='这题的答案内容未能与引用原文核对，未采用该答案。'
 elif code=='UNKNOWN_OR_DUPLICATE_QUESTION':reason='模型重复返回了这道题，无法确定采用哪条答案，已留空。'
 elif code=='MODEL_OMITTED':
  return Decision(question_id,'model_omitted',None,[],[],None,'模型未完整返回这题的答案，已留空。')
 elif code=='ANSWER_TOO_LONG':reason='答案或引用过长，未采用这题的答案。'
 else:reason='这题的答案格式无法可靠校验，已留空。'
 return Decision(question_id,'needs_review',None,[],[],None,reason)


def original_quote(quote,text):
 """Restore whitespace differences only; never fuzzy-match facts or source IDs."""
 if quote in text:return quote
 tokens=quote.split()
 if tokens:
  found=re.search(r'\s+'.join(re.escape(token) for token in tokens),text)
  if found and len(found[0])<=400:return found[0]
 return None


def validation_detail(code,item=None):
 """Field names, checks and counts only; never retain a rejected model value."""
 def kind(value):
  if value is None:return 'null'
  if isinstance(value,bool):return 'boolean'
  if isinstance(value,str):return 'string'
  if isinstance(value,list):return 'array'
  if isinstance(value,dict):return 'object'
  if isinstance(value,(int,float)):return 'number'
  return 'unknown'
 def detail(path,check,**values):return {'path':path,'check':check,'details':values}
 row=item if isinstance(item,dict) else ({f.name:getattr(item,f.name) for f in fields(Decision)} if isinstance(item,Decision) else {})
 if code=='DECISION_SCHEMA':
  if item is None:return detail('decisions','expected_array')
  legacy=isinstance(item,Decision) or ('span_ids' in row and 'quote' in row)
  expected=DECISION_KEYS if legacy else WIRE_KEYS;allowed=DECISION_FIELDS if legacy else WIRE_KEYS
  missing=sorted(expected-set(row))
  if missing or set(row)-allowed:
   return detail('decisions[]','required_fields',missing_fields=missing,unexpected_field_count=len(set(row)-allowed))
  for name in ('reason','option_ids','span_ids','evidence'):
   if name not in row:continue
   value=row[name];expected_type=str if name=='reason' else list
   if not isinstance(value,expected_type):return detail(name,'expected_string' if name=='reason' else 'expected_array',actual_type=kind(value))
   if name in ('option_ids','span_ids'):
    for index,value in enumerate(value):
     if not isinstance(value,str):return detail(f'{name}[{index}]','expected_string',actual_type=kind(value))
  for index,citation in enumerate(row.get('evidence',[])):
   original=citation
   citation=_evidence_mapping(citation)
   if citation is None:return detail(f'evidence[{index}]','expected_object',actual_type=kind(original))
   if set(citation)!={'span_id','quote'}:return detail(f'evidence[{index}]','required_fields',missing_fields=sorted({'span_id','quote'}-set(citation)),unexpected_field_count=len(set(citation)-{'span_id','quote'}))
   for name in ('span_id','quote'):
    if not isinstance(citation[name],str):return detail(f'evidence[{index}].{name}','expected_string',actual_type=kind(citation[name]))
  return detail('decisions[]','valid_decision_shape')
 names={'DECISION_STATUS':('status','allowed_status'),'VALUE_TYPE':('value','expected_string_or_null'),
  'QUOTE_TYPE':('quote','expected_string_or_null'),'NON_READY_VALUE':('value','non_ready_answer_empty'),
  'TEXT_VALUE':('value','nonempty_text_without_options'),'EVIDENCE_ID':('evidence','known_source_id'),
  'QUOTE_MISMATCH':('evidence','literal_source_quote'),'TEXT_EVIDENCE':('value','supported_source_text'),
  'UNKNOWN_OR_DUPLICATE_QUESTION':('question_id','unique_batch_question'),'MODEL_OMITTED':('question_id','required_question'),
  'ANSWER_TOO_LONG':('value','answer_and_quote_length')}
 if code.startswith('CHOICE_'):
  name='value' if code=='CHOICE_VALUE_NOT_NULL' else 'option_ids'
  checks={'CHOICE_OPTION_TYPE':'string_array','CHOICE_UNKNOWN_OR_AMBIGUOUS_OPTION':'unique_printed_option',
   'CHOICE_EMPTY':'nonempty_options','CHOICE_CARDINALITY':'one_option','CHOICE_VALUE':'valid_choice','CHOICE_VALUE_NOT_NULL':'expected_null'}
  return detail(name,checks.get(code,'valid_choice'),actual_type=kind(row.get(name)),option_count=len(row['option_ids']) if isinstance(row.get('option_ids'),list) else 0)
 if code=='ANSWER_TOO_LONG':
  for name,limit in (('value',8000),('quote',400)):
   if isinstance(row.get(name),str) and len(row[name])>limit:return detail(name,'max_length',actual_chars=len(row[name]),max_chars=limit)
 path,check=names.get(code,('response','valid_model_response'))
 if code=='EVIDENCE_ID' and not row.get('evidence'):path='span_ids'
 if code=='QUOTE_MISMATCH' and not row.get('evidence'):path='quote'
 return detail(path,check)


def decode_model_batch(payload,questions,spans,*,check_text=True,diagnostics=None):
 """Decode modern per-span evidence or the legacy flat Decision, never mix them.

 issues contains only program-detected failures eligible for bounded correction.
 A model's explicit uncertainty or conflict is not a formatting failure.
 """
 issues={};qmap={q.id:q for q in questions};smap={s.id:s for s in spans};raw=[]
 def failed(identity,code,item=None,path=None):
  if diagnostics is not None:
   diagnostics[identity]=validation_detail(code,item)
   if path is not None and code!='DECISION_SCHEMA':diagnostics[identity]['path']=path
  return validation_failure(identity,code,issues)
 # Ignore unused envelope fields; validate every actual decision independently.
 if not isinstance(payload,dict) or not isinstance(payload.get('decisions'),list):
  return [failed(q.id,'DECISION_SCHEMA') for q in questions],issues
 for item in payload['decisions']:
  if not isinstance(item,dict) or not isinstance(item.get('question_id'),str) or item['question_id'] not in qmap:continue
  identity=item['question_id']
  if set(item) in (DECISION_KEYS,DECISION_FIELDS):
   raw.append(Decision(**item));continue
  if set(item)!=WIRE_KEYS:
   raw.append(failed(identity,'DECISION_SCHEMA',item));continue
  citations=item['evidence'];ids=[];quotes=[];code=None;path=None
  if not isinstance(citations,list):code='DECISION_SCHEMA'
  else:
   for index,citation in enumerate(citations):
    path=f'evidence[{index}]'
    if (not isinstance(citation,dict) or set(citation)!={'span_id','quote'}
      or not isinstance(citation['span_id'],str) or not isinstance(citation['quote'],str)):
     code='DECISION_SCHEMA';break
    if citation['span_id'] not in smap:code='EVIDENCE_ID';path+='.span_id';break
    path+='.quote'
    quote=citation['quote']
    if not quote or len(quote)>400:code='QUOTE_MISMATCH' if not quote else 'ANSWER_TOO_LONG';break
    quote=original_quote(quote,smap[citation['span_id']].text)
    if quote is None:code='QUOTE_MISMATCH';break
    if citation['span_id'] not in ids:ids.append(citation['span_id'])
    quotes.append(quote)
  if code:
   raw.append(failed(identity,code,item,path));continue
  raw.append(Decision(identity,item['status'],item['value'],item['option_ids'],ids,quotes[0] if quotes else None,item['reason'],[{'span_id':citation['span_id'],'quote':quote} for citation,quote in zip(citations,quotes)]))
 checked=validate_model_batch(raw,questions,spans,issues=issues,check_text=check_text,diagnostics=diagnostics)
 return checked,issues

def validate_decisions(decisions:list[Decision],questions:list[Question],spans:list[SourceSpan],mode:str,*,check_text=True,text_support=None,answer_mode='grounded')->list[Decision]:
 if mode not in ['real','fictional_test']:raise ValueError('MODE')
 if answer_mode not in ('grounded','autonomous'):raise ValueError('ANSWER_MODE')
 qmap={q.id:q for q in questions};smap={s.id:s for s in spans};seen=set();result=[]
 for d in decisions:
  if not isinstance(d.question_id,str) or d.question_id not in qmap or d.question_id in seen:raise ValueError('UNKNOWN_OR_DUPLICATE_QUESTION')
  seen.add(d.question_id);q=qmap[d.question_id]
  if not isinstance(d.status,str) or d.status not in STATUSES:raise ValueError('DECISION_STATUS')
  if not isinstance(d.reason,str) or not isinstance(d.option_ids,list) or not isinstance(d.span_ids,list) or any(not isinstance(v,str) for v in d.option_ids+d.span_ids):raise ValueError('DECISION_SCHEMA')
  if d.value is not None and not isinstance(d.value,str):raise ValueError('VALUE_TYPE')
  if d.quote is not None and not isinstance(d.quote,str):raise ValueError('QUOTE_TYPE')
  if not isinstance(d.evidence,list):raise ValueError('DECISION_SCHEMA')
  for citation in d.evidence:
   citation=_evidence_mapping(citation)
   if (citation is None or set(citation)!={'span_id','quote'} or not isinstance(citation['span_id'],str) or not isinstance(citation['quote'],str)):raise ValueError('DECISION_SCHEMA')
  synthetic=d.reason.startswith('synthetic_test:')
  if mode=='real' and synthetic:raise ValueError('MOCK_IN_REAL')
  if answer_mode=='autonomous':
   if d.span_ids or d.quote is not None or d.evidence:raise ValueError('AUTONOMOUS_EVIDENCE')
   if d.status=='ready' and isinstance(d.value,str) and not d.value.strip():raise ValueError('TEXT_VALUE')
   if isinstance(d.value,str) and len(d.value)>8000:raise ValueError('ANSWER_TOO_LONG')
  if d.status!='ready':
   if d.value is not None or d.option_ids:raise ValueError('NON_READY_VALUE')
  else:
   if q.kind=='text':
    if not d.value or d.option_ids:raise ValueError('TEXT_VALUE')
   elif len(d.option_ids)!=len(set(d.option_ids)) or not set(d.option_ids)<=set(q.options) or not d.option_ids or (q.kind in ['single_choice','boolean'] and len(d.option_ids)!=1) or d.value is not None:raise ValueError('CHOICE_VALUE')
   if not synthetic and answer_mode=='grounded':
    if not d.span_ids or any(s not in smap for s in d.span_ids):raise ValueError('EVIDENCE_ID')
    if not isinstance(d.quote,str) or not d.quote or not any(d.quote in smap[s].text for s in d.span_ids):raise ValueError('QUOTE_MISMATCH')
    for citation in d.evidence:
     citation=_evidence_mapping(citation)
     if citation is None or set(citation)!={'span_id','quote'}:raise ValueError('DECISION_SCHEMA')
     if citation['span_id'] not in d.span_ids or citation['span_id'] not in smap:raise ValueError('EVIDENCE_ID')
     if not isinstance(citation['quote'],str) or not 0<len(citation['quote'])<=400 or citation['quote'] not in smap[citation['span_id']].text:raise ValueError('QUOTE_MISMATCH')
    if check_text and q.kind=='text' and not text_answer_supported(d,spans) and not (text_support and text_support.accepts(q,d,spans)):raise ValueError('TEXT_EVIDENCE')
  result.append(d)
 for q in questions:
  if q.id not in seen:result.append(Decision(q.id,'model_omitted',None,[],[],None,'Model did not return this question; not evidence of a missing answer'))
 return sorted(result,key=lambda d:list(qmap).index(d.question_id))

def validate_llm_choice(d,q,spans,issues=None,diagnostics=None):
 # Only representation-preserving normalization; never infer a selected answer.
 def review(code):
  if diagnostics is not None:diagnostics[q.id]=validation_detail(code,d)
  return validation_failure(q.id,code,issues)
 if not isinstance(d.status,str) or d.status not in STATUSES:return review('DECISION_STATUS')
 if not isinstance(d.option_ids,list) or any(not isinstance(x,str) for x in d.option_ids):return review('CHOICE_OPTION_TYPE')
 if d.status=='ready' and isinstance(d.value,str) and not d.option_ids:
  # Some providers place an exact option in value. Move only an unambiguous
  # printed option; explanations, aliases and conflicting fields stay withheld.
  candidates=[option for option in q.options if option==d.value]
  if not candidates:candidates=[option for option in q.options if option.strip().casefold()==d.value.strip().casefold()]
  if len(candidates)==1:d=replace(d,value=None,option_ids=candidates)
 if d.value is not None:return review('CHOICE_VALUE_NOT_NULL')
 if d.status=='ready':
  normalized=[]
  for value in d.option_ids:
   candidates=[option for option in q.options if option==value]
   if not candidates:candidates=[option for option in q.options if option.strip().casefold()==value.strip().casefold()]
   if len(candidates)!=1:return review('CHOICE_UNKNOWN_OR_AMBIGUOUS_OPTION')
   if candidates[0] not in normalized:normalized.append(candidates[0])
  if not normalized:return review('CHOICE_EMPTY')
  if q.kind in ('single_choice','boolean') and len(normalized)!=1:return review('CHOICE_CARDINALITY')
  if normalized!=d.option_ids:
   if not isinstance(d.reason,str):return review('DECISION_SCHEMA')
   d=Decision(d.question_id,d.status,None,normalized,d.span_ids,d.quote,
              'CHOICE_NORMALIZED: case/whitespace/duplicate representation only; '+d.reason)
 try:return validate_decisions([d],[q],spans,'real')[0]
 except ValueError as e:return review(str(e).split(':',1)[0])

def supported_conversion(quote, value):
 from decimal import Decimal, InvalidOperation
 units={'kg':('mass','1000'),'g':('mass','1'),'mg':('mass','.001'),'ug':('mass','.000001'),'µg':('mass','.000001'),
        'l':('volume','1'),'ml':('volume','.001'),'µl':('volume','.000001'),
        'm':('length','1'),'cm':('length','.01'),'mm':('length','.001'),'km':('length','1000')}
 pattern=r'([-+]?\d+(?:\.\d+)?)\s*(kg|mg|ug|µg|g|ml|µl|l|km|cm|mm|m)'
 a=re.fullmatch(pattern,quote.strip(),re.I);b=re.fullmatch(pattern,value.strip(),re.I)
 if not a or not b:return False
 au,bu=units[a[2].lower()],units[b[2].lower()]
 try:return au[0]==bu[0] and Decimal(a[1])*Decimal(au[1])==Decimal(b[1])*Decimal(bu[1])
 except InvalidOperation:return False


def text_answer_supported(decision, spans):
 """Every answer line must be an excerpt of a cited raw span, never MD framing."""
 if not isinstance(decision.value,str) or not decision.value.strip() or not isinstance(decision.span_ids,list):return False
 evidence=[s.text for s in spans if s.id in decision.span_ids]
 def literal(value,text):
  return value in text or re.sub(r'\s+',' ',value).strip() in re.sub(r'\s+',' ',text).strip()
 lines=[line.strip() for line in decision.value.splitlines() if line.strip()]
 return bool(lines) and (all(any(literal(line,text) for text in evidence) for line in lines)
  or (isinstance(decision.quote,str) and supported_conversion(decision.quote,decision.value)))


def requires_review(question, decision, spans,*,check_text=True,text_support=None):
 evidence=[s for s in spans if s.id in decision.span_ids]
 # Confidence and read support are measurable input properties. Whether a
 # question asks about a fact or a new undertaking is a model decision.
 return any(s.locator.get('confidence',100)<65 or s.locator.get('format_status')=='unsupported' for s in evidence) or (check_text and question.kind=='text' and not text_answer_supported(decision,spans) and not (text_support and text_support.accepts(question,decision,spans)))


def validate_model_batch(decisions, questions, spans,issues=None,*,check_text=True,diagnostics=None):
 """Only exact, unique batch IDs may supply answers; never rebind model output."""
 qmap={q.id:q for q in questions};smap={s.id:s for s in spans}
 # Foreign IDs are not evidence for this batch, including IDs from later batches.
 decisions=[d for d in decisions if isinstance(d.question_id,str) and d.question_id in qmap]
 counts=Counter(d.question_id for d in decisions)
 def failed(identity,code,decision=None):
  if diagnostics is not None:diagnostics[identity]=validation_detail(code,decision)
  return validation_failure(identity,code,issues)
 checked=[failed(q.id,'UNKNOWN_OR_DUPLICATE_QUESTION')
  for q in questions if counts[q.id]>1]
 for d in decisions:
  if counts[d.question_id]>1:continue
  if (isinstance(d.value,str) and len(d.value)>8000) or (isinstance(d.quote,str) and len(d.quote)>400):
   checked.append(failed(d.question_id,'ANSWER_TOO_LONG',d));continue
  if d.status=='ready' and isinstance(d.quote,str) and d.quote and isinstance(d.span_ids,list) and all(isinstance(i,str) and i in smap for i in d.span_ids):
   # Keep exact provenance: tolerate whitespace representation only, then restore
   # the actual source substring before the strict evidence validator runs.
   if not any(d.quote in smap[i].text for i in d.span_ids):
    for identity in d.span_ids:
     quote=original_quote(d.quote,smap[identity].text)
     if quote is not None:d=replace(d,quote=quote);break
  if qmap[d.question_id].kind!='text':
   checked.append(validate_llm_choice(d,qmap[d.question_id],spans,issues,diagnostics));continue
  try:checked.extend(validate_decisions([d],[qmap[d.question_id]],spans,'real',check_text=check_text))
  except ValueError as error:
   checked.append(failed(d.question_id,str(error).split(':',1)[0],d))
 for q in questions:
  if q.id not in counts:checked.append(failed(q.id,'MODEL_OMITTED'))
 return validate_decisions(checked,questions,spans,'real',check_text=check_text)

def review_decisions(decisions, candidates, choices):
 pending={d.question_id for d in decisions if d.status!='ready'}
 if set(choices)!=pending:raise ValueError('REVIEW_ITEMS')
 result=[]
 for d in decisions:
  if d.question_id not in pending:result.append(d);continue
  choice=choices[d.question_id]
  if choice is None:result.append(replace(d,reason=d.reason+' 用户确认留空。'));continue
  if type(choice) is not int or not 0<=choice<len(candidates.get(d.question_id,[])):raise ValueError('REVIEW_CHOICE')
  candidate=candidates[d.question_id][choice]
  result.append(replace(candidate,reason='用户确认依据与填写位置。'+candidate.reason))
 return result

"""Streaming uploads and ephemeral general-task lifecycle; no restart resume."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from ..general import run_general
from ..errors import safe_error

class GeneralTasks:
 def __init__(self,service):
  self.service=service;self.store=service.store;self.preflight=service.preflight;self.lock=threading.RLock();self.uploads={};self.controls={};self.stop=threading.Event()
  self.staging=service.root/'staged-v2';shutil.rmtree(self.staging,ignore_errors=True);self.staging.mkdir(mode=0o700)
  # Only the new task-owned scratch directory is eligible for startup cleanup.
  for directory in (service.root/'persistent').glob('*/task-work-v2'):shutil.rmtree(directory,ignore_errors=True)
  for job in self.store.runs():
   if job['status']=='interrupted':
    self.store.set_review(job['id'],None)

  self.janitor=threading.Thread(target=self.sweep,daemon=True);self.janitor.start()

 def sweep(self):
  while not self.stop.wait(30):
   with self.lock:self.expire()

 def expire(self):
  for token,item in list(self.uploads.items()):
   if time.monotonic()-item['time']>3600:self.discard(token)

 def discard(self,token):
  with self.lock:
   item=self.uploads.pop(token,None)
   if item:shutil.rmtree(item['path'].parent,ignore_errors=True)

 def upload(self,stream,length,name,role):
  if self.service._closing:raise ValueError('SERVICE_CLOSING')
  if not self.service.configurations()['configured']:raise ValueError('LLM_MISSING_KEY')
  if type(length) is not int or not 0<length<=self.preflight.max_upload_bytes:raise ValueError('UPLOAD_SIZE')
  if role not in ('source','questionnaire') or not isinstance(name,str) or not name or len(name)>240 or Path(name).name!=name or '\\' in name:raise ValueError('INVALID_UPLOAD')
  with self.lock:
   self.expire()
   if len(self.uploads)>=21 or sum(x['size'] for x in self.uploads.values())+length>250*1024*1024:raise ValueError('UPLOAD_COLLECTION_LIMIT')
   token=secrets.token_hex(16);folder=self.staging/token;folder.mkdir(mode=0o700);path=folder/name
   try:
    remaining=length
    with path.open('xb') as out:
     while remaining:
      chunk=stream.read(min(1024*1024,remaining))
      if not chunk:raise ValueError('UPLOAD_INCOMPLETE')
      out.write(chunk);remaining-=len(chunk)
    info=self.preflight.inspect_file(path,role)
    self.uploads[token]={'path':path,'size':length,'role':role,'time':time.monotonic(),'sha256':info['sha256']}
    return {'token':token,'name':name,'size':length}
   except Exception:shutil.rmtree(folder,ignore_errors=True);raise

 def submit(self,body):
  required={'request_id','config_id','questionnaire','sources'}
  if not isinstance(body,dict) or not required<=set(body) or set(body)-required-{'answer_mode','decision_config_id'}:raise ValueError('UPLOAD_FIELDS')
  answer_mode=body.get('answer_mode','grounded');decision_id=body.get('decision_config_id')
  if answer_mode not in ('grounded','autonomous'):raise ValueError('ANSWER_MODE')
  if not isinstance(body['config_id'],str) or not body['config_id'] or (decision_id is not None and (not isinstance(decision_id,str) or not decision_id)):raise ValueError('UPLOAD_FIELDS')
  if not isinstance(body['sources'],list) or (answer_mode=='grounded' and (not body['sources'] or decision_id is not None)) or (answer_mode=='autonomous' and body['sources']):raise ValueError('UPLOAD_FIELDS')
  request=body['request_id']
  if not isinstance(request,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request):raise ValueError('REQUEST_ID')
  fingerprint=hashlib.sha256(json.dumps(body|{'answer_mode':answer_mode,'decision_config_id':decision_id},sort_keys=True).encode()).hexdigest()
  # An in-flight browser draft from before modes existed retains its original ID.
  legacy_fingerprint=hashlib.sha256(json.dumps({name:body[name] for name in required},sort_keys=True).encode()).hexdigest() if answer_mode=='grounded' and decision_id is None else None
  with self.service._submit_lock,self.lock:
   if self.service._closing:raise ValueError('SERVICE_CLOSING')
   previous=self.store.by_request(request)
   if previous:
    if previous.get('submission_sha256') not in ({fingerprint,legacy_fingerprint} if legacy_fingerprint else {fingerprint}):raise ValueError('REQUEST_CONFLICT')
    return previous['id']
   if any(j['status']=='running' for j in self.store.runs()):raise ValueError('BUSY')
   self.expire();tokens=[body['questionnaire']]+body['sources']
   if not (2 if answer_mode=='grounded' else 1)<=len(tokens)<=21 or any(not isinstance(t,str) for t in tokens) or len(set(tokens))!=len(tokens):raise ValueError('UPLOAD_FIELDS')
   items=[self.uploads.get(t) for t in tokens]
   if any(x is None for x in items):raise ValueError('UPLOAD_EXPIRED')
   if items[0]['role']!='questionnaire' or any(x['role']!='source' for x in items[1:]):raise ValueError('UPLOAD_FIELDS')
   if answer_mode=='grounded':self.preflight.inspect_sources([x['path'] for x in items[1:]])
   config,key,snapshot=self.service._configuration({'config_id':body['config_id']})
   decision_config=decision_key=decision_snapshot=None
   if decision_id is not None:
    decision_config,decision_key,decision_snapshot=self.service._configuration({'config_id':decision_id},protocol='typesafe_systemone')
   identity=secrets.token_hex(16);directory=self.service.root/'persistent'/identity;work=directory/'task-work-v2';work.mkdir(parents=True,mode=0o700)
   paths=[]
   try:
    for index,(token,item) in enumerate(zip(tokens,items)):
     folder=work/str(index);folder.mkdir();path=folder/item['path'].name;shutil.move(item['path'],path);paths.append(path);self.discard(token)
    self.store.create_run(identity,request,{'version':2,'submission_sha256':fingerprint,'questionnaire_name':paths[0].name,'source_name':'、'.join(p.name for p in paths[1:]),'config':snapshot,'answer_mode':answer_mode,'decision_config':decision_snapshot})
    control={'cancel':threading.Event()};self.controls[identity]=control
    self.service.start_worker(self.work,identity,paths,config,key,control,answer_mode=answer_mode,decision_config=decision_config,decision_key=decision_key)
    return identity
   except Exception:
    shutil.rmtree(directory,ignore_errors=True)
    if self.store.run(identity):self.store.finish(identity,{'status':'error','error':safe_error(ValueError('TASK_START_FAILED'))},'failed')
    raise

 def cancel(self,identity):
  with self.lock:
   c=self.controls.get(identity)
   if not c:raise ValueError('TASK_NOT_RUNNING')
   c['cancel'].set()
   self.store.request_cancel(identity)

 def work(self,identity,paths,config,key,control,*,answer_mode='grounded',decision_config=None,decision_key=None):
  directory=paths[0].parent.parent.parent;work=directory/'task-work-v2';client=decision_client=None
  hashes={}
  def progress(stage,**details):
   if control['cancel'].is_set():raise ValueError('TASK_CANCELLED')
   self.store.progress(identity,stage,**details)
  try:
   hashes={str(index):self.preflight.digest(path) for index,path in enumerate(paths)}
   progress('checking');client=self.service._client_factory(config,key)
   if decision_config is not None:decision_client=self.service._decision_client_factory(decision_config,decision_key)
   processor=self.service._processor_factory(cancelled=control['cancel'].is_set)
   from ..workflow.compatibility import LegacyProgressSink
   result=run_general(paths[0],paths[1:],config,work/'result',client,progress,cancelled=control['cancel'].is_set,answer_mode=answer_mode,decision_client=decision_client,processor=processor,workflow_progress=LegacyProgressSink(self.store,identity))
   if control['cancel'].is_set():raise ValueError('TASK_CANCELLED')
   final=directory/'result';shutil.move(work/'result',final)
   report=json.loads((final/'report.json').read_text());files={p.name:str(p) for p in final.iterdir() if p.is_file()}
   shutil.rmtree(work,ignore_errors=True)
   self.service.complete_task(identity,report,result.outcome,files)
  except Exception as error:
   # Capture generated diagnostics before deleting task-owned working files.
   try:self.service.fail_task(identity,directory,error,config,hashes)
   finally:shutil.rmtree(work,ignore_errors=True)
  finally:
   if client is not None and hasattr(client,'api_key'):client.api_key=None
   if decision_client is not None and hasattr(decision_client,'api_key'):decision_client.api_key=None
   key=None
   decision_key=None
   with self.lock:self.controls.pop(identity,None)

 def close(self):
  self.stop.set()
  with self.lock:
   for c in self.controls.values():c['cancel'].set()
   for token in list(self.uploads):self.discard(token)

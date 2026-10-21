"""Local SQLite metadata and authenticated encryption; no secret-returning HTTP DTOs."""
import fcntl,json,os,secrets,sqlite3,threading
from datetime import datetime,timezone
from pathlib import Path
from cryptography.fernet import Fernet,InvalidToken
from ...config import PRESETS, allowed_preset_endpoint, preset_config, validate_api_key

STAGES=('checking','reading','matching','writing','verifying')
def now():return datetime.now(timezone.utc).isoformat()
def valid_key(key):
 return validate_api_key(key)

class SQLiteWorkflowStore:
 def __init__(self,directory,key_file):
  self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
  self.lock=threading.RLock();self._closed=False
  self.lease=(self.directory/'service.lock').open('a')
  try:fcntl.flock(self.lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:self.lease.close();raise ValueError('STORE_IN_USE') from None
  try:
   self.db=sqlite3.connect(self.directory/'workflow.sqlite3',check_same_thread=False)
   os.chmod(self.directory/'workflow.sqlite3',0o600);self.db.row_factory=sqlite3.Row
   self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
   CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, encrypted_key BLOB, updated_at TEXT NOT NULL);
   CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, request_id TEXT UNIQUE, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT, data TEXT NOT NULL);
   CREATE TABLE IF NOT EXISTS nodes(run_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, finished_at TEXT, details TEXT NOT NULL, PRIMARY KEY(run_id,stage));
   CREATE UNIQUE INDEX IF NOT EXISTS one_running ON runs((1)) WHERE status='running';
   ''')
   k=Path(key_file).resolve()
   if not k.exists():
    if self.db.execute('SELECT 1 FROM profiles WHERE encrypted_key IS NOT NULL LIMIT 1').fetchone():raise ValueError('MASTER_KEY_MISSING')
    k.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(k,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(Fernet.generate_key())
   if k.is_symlink() or k.stat().st_mode&0o077:raise ValueError('MASTER_KEY_PERMISSIONS')
   self.cipher=Fernet(k.read_bytes())
   self.recover()
   if not self.profiles():
    for provider,title in [('deepseek','DeepSeek 官方'),('typesafe','Jev 官方 · 选项决策')]:
     self.save_profile({'name':title,'provider':provider})
  except Exception:
   if hasattr(self,'db'):self.db.close()
   self.lease.close();raise
 def close(self):
  with self.lock:
   if not self._closed:self.db.close();self.lease.close();self._closed=True
 def profiles(self):
  with self.lock:
   result=[]
   for r in self.db.execute('SELECT * FROM profiles ORDER BY updated_at,id'):
    metadata=json.loads(r['metadata'])
    # Keep historical profiles encrypted on disk without offering unsupported providers.
    if not allowed_preset_endpoint(metadata.get('provider'),metadata.get('base_url')):continue
    metadata.pop('max_tokens',None)
    result.append({'output_mode':'json_object','protocol':'openai_chat'}|metadata|{'id':r['id'],'has_key':r['encrypted_key'] is not None,'key_mask':'••••••••' if r['encrypted_key'] is not None else '', 'updated_at':r['updated_at']})
   return result
 def save_profile(self,body,profile_id=None):
  with self.lock:
   old=self.db.execute('SELECT * FROM profiles WHERE id=?',(profile_id,)).fetchone() if profile_id else None
   if profile_id and old is None:raise ValueError('PROFILE_NOT_FOUND')
   name=body.get('name','')
   if not isinstance(name,str) or not 1<=len(name.strip())<=80:raise ValueError('CONFIG_NAME')
   if 'max_tokens' in body:raise ValueError('CONFIG_FIELDS')
   previous=json.loads(old['metadata']) if old else {}
   config=preset_config(body.get('provider'),model=body.get('model') or None,base_url=body.get('base_url') or None,max_tokens=previous.get('max_tokens',8192),output_mode=body.get('output_mode',previous.get('output_mode','json_object')),protocol=body.get('protocol'))
   if config.max_tokens>65536 or len(config.model)>200:raise ValueError('CONFIG_LIMIT')
   supplied=body.get('api_key') or '';clear=body.get('clear_key',False)
   if type(clear) is not bool or (clear and supplied):raise ValueError('CONFIG_KEY_ACTION')
   token=old['encrypted_key'] if old else None
   if old and token and json.loads(old['metadata'])['base_url']!=config.base_url and not supplied and not clear:raise ValueError('CONFIG_KEY_REENTRY')
   if clear:token=None
   elif supplied:token=self.cipher.encrypt(valid_key(supplied).encode())
   metadata={'name':name.strip(),'provider':config.provider,'model':config.model,'base_url':config.base_url,'max_tokens':config.max_tokens,'output_mode':config.output_mode,'protocol':config.protocol}
   identity=profile_id or secrets.token_hex(16)
   with self.db:self.db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)',(identity,json.dumps(metadata),token,now()))
   return next(p for p in self.profiles() if p['id']==identity)
 def delete_profile(self,profile_id):
  with self.lock,self.db:
   if self.db.execute('DELETE FROM profiles WHERE id=?',(profile_id,)).rowcount!=1:raise ValueError('PROFILE_NOT_FOUND')
 def resolve_profile(self,profile_id):
  with self.lock:
   r=self.db.execute('SELECT * FROM profiles WHERE id=?',(profile_id,)).fetchone()
   if r is None:raise ValueError('PROFILE_NOT_FOUND')
   d=json.loads(r['metadata'])
   if d.get('provider') not in PRESETS:raise ValueError('UNKNOWN_PRESET')
   if not allowed_preset_endpoint(d['provider'],d.get('base_url')):raise ValueError('CONFIG_OFFICIAL_ENDPOINT')
   if r['encrypted_key'] is None:raise ValueError('LLM_MISSING_KEY')
   try:key=self.cipher.decrypt(r['encrypted_key']).decode()
   except InvalidToken:raise ValueError('KEY_UNAVAILABLE') from None
   return preset_config(d['provider'],model=d['model'],base_url=d['base_url'],max_tokens=d.get('max_tokens',8192),output_mode=d.get('output_mode','json_object'),protocol=d.get('protocol','openai_chat')),key
 def resolve_profile_for_use(self,profile_id):
  """Resolve private credentials and public profile metadata atomically."""
  with self.lock:
   r=self.db.execute('SELECT * FROM profiles WHERE id=?',(profile_id,)).fetchone()
   if r is None:raise ValueError('PROFILE_NOT_FOUND')
   d=json.loads(r['metadata'])
   if d.get('provider') not in PRESETS:raise ValueError('UNKNOWN_PRESET')
   if not allowed_preset_endpoint(d['provider'],d.get('base_url')):raise ValueError('CONFIG_OFFICIAL_ENDPOINT')
   if r['encrypted_key'] is None:raise ValueError('LLM_MISSING_KEY')
   try:key=self.cipher.decrypt(r['encrypted_key']).decode()
   except InvalidToken:raise ValueError('KEY_UNAVAILABLE') from None
   config=preset_config(d['provider'],model=d['model'],base_url=d['base_url'],max_tokens=d.get('max_tokens',8192),output_mode=d.get('output_mode','json_object'),protocol=d.get('protocol','openai_chat'))
   public={'output_mode':'json_object','protocol':'openai_chat'}|{k:v for k,v in d.items() if k!='max_tokens'}|{'id':r['id']}
   return config,key,public
 def run(self,identity,internal=False):
  with self.lock:
   r=self.db.execute('SELECT * FROM runs WHERE id=?',(identity,)).fetchone()
   if r is None:return None
   data=json.loads(r['data'])|{'id':r['id'],'status':r['status'],'created_at':r['created_at'],'updated_at':r['updated_at'],'finished_at':r['finished_at']}
   workflow_events=data.pop('workflow_events',[])
   if workflow_events:
    from ...workflow.compatibility import workflow_node_read_model
    data['workflow_nodes']=workflow_node_read_model(workflow_events)
   ns={n['stage']:dict(n) for n in self.db.execute('SELECT * FROM nodes WHERE run_id=?',(identity,))}
   data['nodes']=[{'stage':s,'status':ns[s]['status'],'started_at':ns[s]['started_at'],'finished_at':ns[s]['finished_at'],**json.loads(ns[s]['details'])} for s in STAGES if s in ns]
   if data.get('workflow_nodes'):
    from ...workflow.compatibility import legacy_stage_for
    legacy={node['stage']:node for node in data['nodes']}
    for workflow_node in data['workflow_nodes']:
     if workflow_node['status']!='running':continue
     stage=legacy_stage_for(workflow_node['node_id'])
     details=legacy.get(stage,{})
     if workflow_node['node_id']=='write':
      repair=legacy.get('matching',{})
      if repair.get('status')=='running' and repair.get('phase')=='answer_compaction':details=repair
     phase=details.get('phase')
     if isinstance(phase,str) and phase.isascii() and phase.replace('_','').isalnum() and len(phase)<=64:
      workflow_node['phase']=phase
     counts=dict(workflow_node.get('counts') or {})
     for key in ('completed','total','attempt','batch','batches'):
      value=details.get(key)
      if type(value) is int and 0<=value<=1_000_000:counts[key]=value
     workflow_node['counts']=counts
   if data['status']=='done':
    for node in data['nodes']:
     if node['status']=='review':node['status']='success'
   if data.get('error'):
    from ...errors import present_error
    data['error']=present_error(data['error'])
   if not internal:data['files']=list(data.get('files',{}))
   return data
 def runs(self):
  with self.lock:return [self.run(r[0]) for r in self.db.execute('SELECT id FROM runs ORDER BY created_at DESC LIMIT 200')]
 def by_request(self,request_id):
  with self.lock:
   r=self.db.execute('SELECT id FROM runs WHERE request_id=?',(request_id,)).fetchone()
   return self.run(r[0]) if r else None
 def create_run(self,identity,request_id,metadata):
  with self.lock,self.db:
   previous=self.by_request(request_id)
   if previous:return previous
   if self.db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():raise ValueError('BUSY')
   stamp=now();self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',(identity,request_id,'running',stamp,stamp,None,json.dumps(metadata|{'files':{},'stage':'checking'})))
   for stage in STAGES:self.db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?)',(identity,stage,'pending',None,None,'{}'))
   return self.run(identity)
 def progress(self,identity,stage,**details):
  if stage not in STAGES:raise ValueError('STAGE')
  with self.lock,self.db:
   r=self.run(identity,True)
   if not r or r['status']!='running':return
   stamp=now();idx=STAGES.index(stage)
   for n in r['nodes']:
    if n['stage']==stage:continue
    if n['status']=='running':self.db.execute("UPDATE nodes SET status='success',finished_at=? WHERE run_id=? AND stage=?",(stamp,identity,n['stage']))
    elif n['status']=='pending' and STAGES.index(n['stage'])<idx:self.db.execute("UPDATE nodes SET status='skipped',finished_at=? WHERE run_id=? AND stage=?",(stamp,identity,n['stage']))
   self.db.execute("UPDATE nodes SET status='running',started_at=COALESCE(started_at,?),finished_at=NULL,details=? WHERE run_id=? AND stage=?",(stamp,json.dumps(details),identity,stage))
   event={'at':stamp,'stage':stage,**{k:v for k,v in details.items() if k in ('phase','completed','total','attempt','batch','batches')}}
   log=r.get('progress_log',[])
   self._data(identity,{'stage':stage,**details,'progress_log':(log+[event])[-100:]})
 def record_workflow_progress(self,identity,event):
  from ...workflow.contracts import NodeStatus,WorkflowProgress,WorkflowStage
  if not isinstance(event,WorkflowProgress) or event.node_id not in {stage.value for stage in WorkflowStage}:
   raise ValueError('WORKFLOW_EVENT')
  if event.status not in (NodeStatus.RUNNING,NodeStatus.SUCCESS,NodeStatus.SKIPPED,NodeStatus.FAILED,NodeStatus.INTERRUPTED):
   raise ValueError('WORKFLOW_EVENT')
  allowed={'completed','total','attempt','batch','batches'}
  counts={key:value for key,value in event.counts.items()
          if key in allowed and type(value) is int and 0<=value<=1_000_000}
  code=event.error_code if event.error_code in (
   'TASK_CANCELLED','CANCELLED','PROCESSING_ERROR','LLM_TIMEOUT','LLM_AUTH','LLM_RATE_LIMIT',
   'LLM_BAD_JSON','SOURCE_CHANGED','BUNDLE_CONTENT_CHANGED','VERIFICATION_FAILED',
   'UNSUPPORTED_FORMAT','UNSUPPORTED_DOCUMENT_STRUCTURE','DOCUMENT_CORRUPT','UPLOAD_SIZE',
   'PAGE_LIMIT','QUESTION_LIMIT','PROCESS_TIMEOUT','CONVERSION_FAILED','PROCESSOR_UNAVAILABLE',
   'ANSWER_SCHEMA','REVIEW_REQUIRED','WRITE_PLAN_REQUIRED','OUTPUT_EXISTS',
  ) else None
  safe=WorkflowProgress(event.node_id,event.status,event.occurred_at,counts,code).to_dict()
  with self.lock,self.db:
   run=self.run(identity,True)
   if not run or run['status']!='running':return
   raw=self.db.execute('SELECT data FROM runs WHERE id=?',(identity,)).fetchone()
   data=json.loads(raw[0]);events=data.get('workflow_events',[])
   events=(events+[safe])[-100:]
   self._data(identity,{'workflow_events':events})
 def set_review(self,identity,review):
  """Persist or clear a task's confirmation view in one store-owned transaction."""
  with self.lock,self.db:self._data(identity,{'review':review})
 def request_cancel(self,identity):
  with self.lock,self.db:
   job=self.run(identity)
   if job and job['status']=='running':self._data(identity,{'cancelling':True})
 def _data(self,identity,updates):
  r=self.db.execute('SELECT data FROM runs WHERE id=?',(identity,)).fetchone();d=json.loads(r[0]);d.update(updates)
  self.db.execute('UPDATE runs SET data=?,updated_at=? WHERE id=?',(json.dumps(d),now(),identity))
 def finish(self,identity,result,node_status='success'):
  with self.lock,self.db:
   stamp=now();self._data(identity,result)
   self.db.execute('UPDATE runs SET status=?,finished_at=? WHERE id=?',(result['status'],stamp,identity))
   if result.get('error'):
    for node in self.db.execute("SELECT stage,details FROM nodes WHERE run_id=? AND status='running'",(identity,)).fetchall():
     details=json.loads(node['details']);details['error']=result['error']
     self.db.execute('UPDATE nodes SET details=? WHERE run_id=? AND stage=?',(json.dumps(details),identity,node['stage']))
   self.db.execute("UPDATE nodes SET status=?,finished_at=? WHERE run_id=? AND status='running'",(node_status,stamp,identity))
   self.db.execute("UPDATE nodes SET status='skipped',finished_at=? WHERE run_id=? AND status='pending'",(stamp,identity))
 def recover(self):
  with self.lock:
   for row in list(self.db.execute("SELECT id FROM runs WHERE status='running'")):
    raw=self.db.execute('SELECT data FROM runs WHERE id=?',(row[0],)).fetchone()
    data=json.loads(raw[0]);events=data.get('workflow_events',[])
    from ...workflow.compatibility import workflow_node_read_model
    from ...workflow.contracts import NodeStatus,WorkflowProgress
    latest=workflow_node_read_model(events)
    stamp=now();terminal=[]
    for node in latest:
     status=NodeStatus.INTERRUPTED if node['status']==NodeStatus.RUNNING else (
      NodeStatus.SKIPPED if node['status']==NodeStatus.PENDING else None)
     if status is not None:
      terminal.append(WorkflowProgress(node['node_id'],status,stamp).to_dict())
    self.finish(row[0],{'status':'interrupted','workflow_events':(events+terminal)[-100:],
     'error':{'code':'SERVICE_RESTARTED','message':'服务重启时任务尚未完成。未自动重试，请重新选择文件后手动提交。'}},'interrupted')
 def import_legacy(self,root):
  """Import only whitelisted metadata from completed reports; never raw uploads/config."""
  root=Path(root)
  with self.lock,self.db:
   for p in list(root.glob('*/*/*/report.json'))+list(root.glob('*/*/failure.json')):
    identity=p.parent.parent.name if p.name=='report.json' else p.parent.name
    if len(identity)!=32 or any(c not in '0123456789abcdef' for c in identity):continue
    if self.run(identity):continue
    try:
     d=json.loads(p.read_text());stamp=datetime.fromtimestamp(p.stat().st_mtime,timezone.utc).isoformat()
     if p.name=='report.json':
      v=d['verification'];files={x.name:str(x.resolve()) for x in [p,p.with_name('report.md')] if x.is_file()}
      name=d.get('published_file');candidate=p.parent/name if isinstance(name,str) and Path(name).name==name else None
      if candidate and candidate.is_file():files[name]=str(candidate.resolve())
      data={'outcome':'partial' if v.get('needs_review') or any(k!='written' for k in d['counts']) else 'complete','counts':d['counts'],'layout_check':v['layout_check'],'files':files,'legacy':True,'questionnaire_name':'旧任务 · '+identity[:8]};status='done'
     else:
      err=d.get('error',{});code=err.get('code','PROCESSING_ERROR')
      data={'error':{'code':code,'message':'旧任务失败；可下载原诊断报告。'},'stage':d.get('stage'),'files':{'failure.json':str(p.resolve())},'legacy':True,'questionnaire_name':'旧任务 · '+identity[:8]};status='error'
     self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',(identity,None,status,stamp,stamp,stamp,json.dumps(data)))
    except (ValueError,KeyError,TypeError,OSError):continue


# Compatibility for the pre-adapter import path.
Store = SQLiteWorkflowStore

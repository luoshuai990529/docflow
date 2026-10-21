"""Secret-free provider configuration; credentials are resolved only at run time."""
from dataclasses import dataclass, asdict
from pathlib import Path
import json, re
from urllib.parse import urlsplit

PRESETS = {
 'deepseek': {'base_url':'https://api.deepseek.com','model':'deepseek-v4-pro','credential_ref':'DEEPSEEK_API_KEY'},
 'typesafe': {'base_url':'https://api.typesafe.ai/v1','model':'jev-latest','credential_ref':'TYPESAFE_API_KEY','protocol':'typesafe_systemone'},
}

def validate_api_key(key):
 if not isinstance(key,str) or not key or len(key)>512:raise ValueError('LLM_MISSING_KEY')
 if any(ord(c)<33 or ord(c)>126 for c in key):raise ValueError('LLM_INVALID_KEY_FORMAT')
 return key

@dataclass
class LLMConfig:
 provider: str
 base_url: str
 model: str
 credential_ref: str
 protocol: str = 'openai_chat'
 output_mode: str = 'json_object'
 timeout: float = 120
 max_tokens: int = 8192
 max_input_chars: int = 500000
 def __post_init__(self):
  u=urlsplit(self.base_url)
  if u.scheme not in ('https','http') or not u.hostname or u.username or u.password or u.query or u.fragment: raise ValueError('CONFIG_BASE_URL')
  if u.scheme=='http' and u.hostname not in ('localhost','127.0.0.1','::1'): raise ValueError('CONFIG_HTTPS_REQUIRED')
  if self.protocol not in ('openai_chat','typesafe_systemone'): raise ValueError('UNSUPPORTED_PROTOCOL')
  if self.output_mode not in ('prompt_only','json_object','json_schema'): raise ValueError('CONFIG_OUTPUT_MODE')
  if not all(isinstance(x,str) and x.strip() for x in (self.provider,self.model,self.credential_ref)): raise ValueError('CONFIG_STRING')
  if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',self.credential_ref): raise ValueError('CONFIG_CREDENTIAL_REF')
  if any(isinstance(x,bool) or not isinstance(x,(int,float)) or x<=0 for x in (self.timeout,self.max_tokens,self.max_input_chars)): raise ValueError('CONFIG_LIMIT')
  if not isinstance(self.max_tokens,int) or not isinstance(self.max_input_chars,int): raise ValueError('CONFIG_LIMIT')
 def metadata(self):
  return {'provider':self.provider,'model':self.model,'protocol':self.protocol,'output_mode':self.output_mode}

def load_config(path):
 config=LLMConfig(**json.loads(Path(path).read_text(encoding='utf-8')))
 if config.provider not in PRESETS:raise ValueError('UNKNOWN_PRESET')
 if not allowed_preset_endpoint(config.provider,config.base_url):raise ValueError('CONFIG_OFFICIAL_ENDPOINT')
 return config
def allowed_preset_endpoint(provider,base_url):
 if provider not in PRESETS or not isinstance(base_url,str):return False
 url=urlsplit(base_url)
 if url.scheme=='http' and url.hostname in ('localhost','127.0.0.1','::1'):
  return True
 official={PRESETS[provider]['base_url']}
 if provider=='deepseek':official.add('https://api.deepseek.com/v1')
 return base_url.rstrip('/') in official
def preset_config(provider,**overrides):
 if provider not in PRESETS: raise ValueError('UNKNOWN_PRESET')
 values=PRESETS[provider]|{k:v for k,v in overrides.items() if v is not None}
 if not allowed_preset_endpoint(provider,values['base_url']):raise ValueError('CONFIG_OFFICIAL_ENDPOINT')
 return LLMConfig(provider=provider,**values)
def init_config(provider,path,**overrides):
 c=preset_config(provider,**overrides);path=Path(path)
 example=path.parent/'.env.example'
 if path.exists() or example.exists(): raise ValueError('OUTPUT_EXISTS')
 path.parent.mkdir(parents=True,exist_ok=True)
 public=asdict(c);public.pop('max_tokens',None)
 with path.open('x',encoding='utf-8') as f: json.dump(public,f,indent=2)
 with example.open('x',encoding='utf-8') as f: f.write(c.credential_ref+'=\n')
 return path

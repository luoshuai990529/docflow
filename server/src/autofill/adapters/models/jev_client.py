"""Bounded TypeSafe System One transport for official Jev profiles.

The evaluator returns typed data only. Questionnaire-specific ranking and
fallback belong to the autonomous answer workflow, not this HTTP adapter.
"""
import hashlib
import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from ...application.ports import LLMFailure, LLMResponse
from .openai_chat import NoRedirect, request_id


def _validate_answers(payload):
 answers=payload.get('answers') if isinstance(payload,dict) else None
 if not isinstance(answers,dict):raise ValueError('JEV_BAD_RESPONSE')
 # Each questionnaire answer is decoded independently by the workflow. One
 # missing typed primitive must not discard valid answers from the same HTTP call.
 return payload


class JevClient:
 def __init__(self,config,api_key=None,transport=None):
  self.config=config;self.api_key=api_key;self.transport=transport or build_opener(NoRedirect()).open

 def evaluate(self,state,questions):
  c=self.config
  if c.protocol!='typesafe_systemone':raise ValueError('UNSUPPORTED_PROTOCOL')
  key=self.api_key if self.api_key is not None else os.environ.get(c.credential_ref)
  if not key:raise ValueError('JEV_MISSING_KEY')
  if not isinstance(key,str) or any(ord(ch)<33 or ord(ch)>126 for ch in key):raise ValueError('JEV_INVALID_KEY_FORMAT')
  serialized=json.dumps({'state':state,'model':c.model,'questions':questions},ensure_ascii=False,allow_nan=False)
  data=serialized.encode()
  if len(serialized)>c.max_input_chars or len(data)>32000000:raise ValueError('JEV_INPUT_TOO_LARGE')
  metadata={'protocol':'typesafe_systemone','schema_sha256':hashlib.sha256(json.dumps(questions,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
  request=Request(c.base_url.rstrip('/')+'/systemone',data=data,headers={'Content-Type':'application/json','Authorization':'Bearer '+key},method='POST')
  try:
   with self.transport(request,timeout=c.timeout) as response:
    raw=response.read(4000001)
    if len(raw)>4000000:raise LLMFailure('JEV_RESPONSE_TOO_LARGE',metadata)
    rid=request_id(response.headers.get('x-request-id'),key)
  except HTTPError as error:
   code='JEV_AUTH' if error.code in (401,403) else 'JEV_RATE_LIMIT' if error.code in (429,529) else 'JEV_HTTP'
   raise LLMFailure(code,metadata|{'http_status':error.code}) from None
  except (TimeoutError,socket.timeout):raise LLMFailure('JEV_TIMEOUT',metadata) from None
  except URLError as error:
   raise LLMFailure('JEV_TIMEOUT' if isinstance(error.reason,(TimeoutError,socket.timeout)) else 'JEV_CONNECTION',metadata) from None
  metadata['response_sha256']=hashlib.sha256(raw).hexdigest()
  if rid:metadata['request_id']=rid
  try:payload=_validate_answers(json.loads(raw))
  except (ValueError,TypeError,KeyError,RecursionError,UnicodeDecodeError):
   raise LLMFailure('JEV_BAD_RESPONSE',metadata|{'stage':'response','reason':'invalid_typed_answers'}) from None
  return LLMResponse(payload,rid,metadata)

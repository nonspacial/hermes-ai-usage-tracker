"""Passive Responses cache evidence before SDK model construction.

Does not set cache options, inject breakpoints, replay requests or perform network
calls. Only active, exactly correlated Hermes Responses requests are observed.
Request/response content is inspected in memory; only allowlisted cache metadata,
numeric usage and process-local keyed fingerprints are retained.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import secrets
import threading
import time
import weakref
from collections import Counter
from urllib.parse import urlparse

from . import recorder as r
from .accounting import safe_usage, num

_REQUESTS = weakref.WeakKeyDictionary()
_LOCK = threading.RLock()
_SECRET = secrets.token_bytes(32)
_MAX_BODY_BYTES = 16 * 1024 * 1024
_TERMINAL = {'response.completed', 'response.incomplete', 'response.failed'}
_WRITE_PATHS = (
    ('input_tokens_details', 'cache_write_tokens'),
    ('input_tokens_details', 'cache_creation_tokens'),
    ('cache_write_tokens',), ('cache_creation_tokens',),
    ('cache_creation_input_tokens',),
)
_DIAG_TYPES = {'cache_hit', 'cache_miss', 'comparison_response_not_found', 'unavailable'}
_DIAG_REASONS = {'model_changed', 'tools_changed', 'tool_choice_changed',
    'prompt_cache_key_changed', 'service_tier_changed', 'reasoning_changed',
    'reasoning_effort_changed', 'text_settings_changed', 'input_changed',
    'instructions_changed', 'cache_expired', 'prefix_changed', 'cache_not_found',
    'breakpoint_changed', 'cache_breakpoints_changed', 'settings_changed',
    'different_machine', 'insufficient_prefix_length', 'context_compacted'}


def _fingerprint(value):
    encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':')).encode()
    return hmac.new(_SECRET, encoded, hashlib.sha256).hexdigest()


def _enum(value, values):
    return value if isinstance(value,str) and value in values else 'other' if value is not None else None


def _context():
    item=r.AUX.get()
    if item:
        root,rec=item
        cur={**rec,'root':root}
    else:
        cur=r.CURRENT.get()
    if not cur or not cur.get('id') or not cur.get('root'):return None
    if cur.get('provider') not in ('openai-codex','openai'):return None
    if cur.get('api_mode') not in ('codex_responses','responses'):return None
    return dict(cur)


def request_metadata(body):
    """No body strings, message text, tool schemas, auth, or raw cache keys saved."""
    options=body.get('prompt_cache_options')
    opt=options if isinstance(options,dict) else {}
    items=body.get('input')
    items=items if isinstance(items,list) else []
    roles=Counter();breakpoints=0;last_role=None
    for item in items:
        if not isinstance(item,dict):continue
        role=_enum(item.get('role'),{'user','assistant','developer','system','tool'})
        if role:roles[role]+=1;last_role=role
        content=item.get('content')
        if isinstance(content,list):
            for part in content:
                if isinstance(part,dict) and 'prompt_cache_breakpoint' in part:breakpoints+=1
    out={
        'options_present':'prompt_cache_options' in body,
        'mode_sent':_enum(opt.get('mode'),{'explicit','implicit'}),
        'ttl_sent':_enum(opt.get('ttl'),{'30m'}),
        'retention_sent':_enum(body.get('prompt_cache_retention'),{'in_memory','24h'}),
        'comparison_requested':isinstance(opt.get('comparison_response_id'),str),
        'cache_key_present':'prompt_cache_key' in body,
        'explicit_breakpoint_count':breakpoints,
        'input_item_count':len(items), 'message_role_counts':dict(roles),
        'last_message_role':last_role,
        'fingerprint_scope':'same recording process only; HMAC key not saved',
    }
    if isinstance(body.get('prompt_cache_key'),str):out['cache_key_fingerprint']=_fingerprint(body['prompt_cache_key'])
    for key in ('instructions','tools'):
        if key in body:out[key+'_fingerprint']=_fingerprint(body[key])
    for key in ('store','stream'):
        if isinstance(body.get(key),bool):out[key]=body[key]
    if isinstance(body.get('service_tier'),str):out['service_tier_sent']=_enum(body['service_tier'],{'auto','default','standard','priority','fast','flex','batch'})
    reasoning=body.get('reasoning')
    if isinstance(reasoning,dict):
        out['reasoning_effort_sent']=_enum(reasoning.get('effort'),{'none','minimal','low','medium','high','xhigh','ultra'})
    return out


def _endpoint(request):
    try:
        u=urlparse(str(request.url))
        if not u.path.rstrip('/').endswith('/responses'):return None
        if u.hostname=='chatgpt.com' and u.path.startswith('/backend-api/codex/'):
            return 'chatgpt_codex_subscription'
        if u.hostname=='api.openai.com':return 'openai_public_api'
        return 'other_responses_endpoint'
    except Exception:return None


@r.safe
def observe_request(request):
    cur=_context()
    if not cur:return
    route=_endpoint(request)
    if route is None:return
    if getattr(request,'method','').upper()!='POST':return
    # Accessing .content never consumes an unread request stream.
    try:content=request.content
    except Exception:return
    if not isinstance(content,bytes):return
    if len(content)>_MAX_BODY_BYTES:
        metadata={'inspection_skipped':'body_size_limit'}
    else:
        body=json.loads(content)
        if not isinstance(body,dict):return
        metadata=request_metadata(body)
    entry={'context':cur,'request':metadata,'endpoint_kind':route,'prepared_at':time.time()}
    with _LOCK:_REQUESTS[request]=entry
    r.store(cur['root']).request({'id':cur['id'],'cache_request':metadata,
        'cache_endpoint_kind':route},'cache_request_observed')


def write_fields(raw):
    """Preserve absent/null/invalid/zero separately, without coercing strings."""
    raw=raw if isinstance(raw,dict) else {}
    result=[]
    for path in _WRITE_PATHS:
        node=raw;present=True
        for part in path:
            if not isinstance(node,dict) or part not in node:present=False;break
            node=node[part]
        if not present:continue
        value=num(node)
        state='null' if node is None else 'invalid' if value is None else 'explicit_zero' if value==0 else 'positive'
        result.append({'path':'.'.join(path),'state':state,'tokens':value})
    return result


def diagnostics_metadata(value):
    if not isinstance(value,dict):return None
    out={'type':_enum(value.get('type'),_DIAG_TYPES)}
    if 'reason' in value:out['reason']=_enum(value['reason'],_DIAG_REASONS)
    for key in ('comparison_reusable_tokens','cache_missed_tokens'):
        if num(value.get(key)) is not None:out[key]=num(value[key])
    return out


@r.safe
def observe_response(data,response):
    """Data is decoded HTTP JSON before OpenAI's construct_type/model_dump."""
    try:request=response.request
    except Exception:return
    with _LOCK:entry=_REQUESTS.get(request)
    if not entry or not isinstance(data,dict):return
    event=data
    if isinstance(data.get('data'),dict) and data.get('event'):
        event=data['data']
    et=event.get('type')
    if et in _TERMINAL:
        obj=event.get('response')
    elif event.get('object')=='response' and not entry['request'].get('stream'):
        obj=event;et='response.http_body'
    else:return
    if not isinstance(obj,dict):return
    cur=entry['context'];s=r.store(cur['root'])
    with s.db() as c:row=c.execute('SELECT data FROM requests WHERE id=?',(cur['id'],)).fetchone()
    if not row:return
    prior=json.loads(row[0]);raw=obj.get('usage');fields=write_fields(raw)
    diag=diagnostics_metadata(obj.get('prompt_cache_diagnostics'))
    states={f['state'] for f in fields}
    values={f['tokens'] for f in fields if f['tokens'] is not None}
    write_state='conflicting_fields' if len(values)>1 else 'positive' if states=={'positive'} else 'explicit_zero' if states=={'explicit_zero'} else 'field_absent' if not fields else 'invalid_or_null'
    evidence={
        'version':1, 'boundary':'decoded_http_json_before_sdk_models',
        'event_type':et, 'endpoint_kind':entry['endpoint_kind'],
        'response_id':r.text(obj.get('id')),
        'observed_at':time.time(),
        'usage':safe_usage(raw), 'write_fields':fields,
        'cache_write_state':write_state,
        'diagnostics_present':'prompt_cache_diagnostics' in obj,
        'diagnostics':diag,
        'request':entry['request'],
        'scope':'provider-reported usage; not physical cache inventory or all KV writes',
    }
    previous=prior.get('cache_evidence') or {}
    evidence['distinct_terminal_ids_seen']=list(dict.fromkeys([
        *previous.get('distinct_terminal_ids_seen',[]),evidence['response_id']]))[-16:]
    if len(evidence['distinct_terminal_ids_seen'])>1:evidence['multiple_terminal_responses']=True
    payload={'id':cur['id'],'cache_evidence':evidence,**r.response_metadata(obj)}
    # Prefer bytes-derived evidence without erasing a previously complete usage
    # report when a final event has no usage at all. No new logical request row.
    if evidence['usage'] or not (prior.get('usage') or {}).get('raw_usage'):
        from .accounting import normalize
        usage=normalize(raw,provider=cur['provider'],api_mode=cur['api_mode'])
        usage['usage_source']='wire_terminal_usage'
        if write_state=='conflicting_fields':
            usage['cache_write_tokens']=None;usage['input_tokens']=None
            usage['field_provenance']['cache_write_tokens']='conflicting_response_fields'
            usage['field_provenance']['input_tokens']='unverified_cache_decomposition'
            usage['warnings'].append('conflicting_cache_write_fields')
        payload['usage']=usage
        if prior.get('ended') is None:payload['status']='usage_received'
    s.request(payload,'cache_wire_evidence')


def request_wrapper(original):
    @functools.wraps(original)
    def wrapped(self,*args,**kwargs):
        request=original(self,*args,**kwargs)
        observe_request(request)
        return request
    return wrapped


def response_wrapper(original):
    @functools.wraps(original)
    def wrapped(self,*,data,cast_to,response,**kwargs):
        observe_response(data,response)
        return original(self,data=data,cast_to=cast_to,response=response,**kwargs)
    return wrapped


def install(module):
    from .adapters import patch
    patch(module.BaseClient,'_build_request',request_wrapper,('self','options'))
    patch(module.BaseClient,'_process_response_data',response_wrapper,('self','data','cast_to','response'))

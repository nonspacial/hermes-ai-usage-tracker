"""Native request observers. No prompt, output text, credentials or headers saved."""
from __future__ import annotations
import atexit, contextvars, functools, logging, os, threading, time, uuid
from datetime import datetime
from .storage import Store,home
from .ownership import identity, reconcile
from .accounting import mapping,normalize,num,safe_usage,usage_priority

log=logging.getLogger('hermes.ai_usage_ledger')
PROCESS=f'{os.getpid()}-{uuid.uuid4().hex[:12]}'
CURRENT=contextvars.ContextVar('usage_current_request',default=None)
AUX=contextvars.ContextVar('usage_aux_request',default=None)
COMPRESSION=contextvars.ContextVar('usage_current_compression',default=None)
_STORES={};_LOOKUP={};_TURN_ROOTS={};_LOCK=threading.RLock()
ADAPTERS={};FAILURES=0;LAST_FAILURE_AT=0
_HEARTBEATS={}
_REGISTERED_ROOTS=set()

def text(v,limit=240):return str(v or '')[:limit]
def timestamp(v):
    if isinstance(v,(int,float)) and v>0:return float(v)
    if isinstance(v,str):
        try:return datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
        except ValueError:pass
    return time.time()
def store(root=None):
    p=str(root or home())
    with _LOCK:
        if p not in _STORES:_STORES[p]=Store(p)
        return _STORES[p]
def safe(fn):
    @functools.wraps(fn)
    def wrapper(*a,**kw):
        global FAILURES,LAST_FAILURE_AT
        try:return fn(*a,**kw)
        except Exception as exc:
            FAILURES+=1;LAST_FAILURE_AT=time.time()
            # Exception messages/tracebacks can include sensitive objects. Class only.
            log.error('Usage recorder failure (%s); ledger may have gaps.',type(exc).__name__)
            return None
    return wrapper

@safe
def health(root=None):
    from pathlib import Path
    target=str(Path(root or home()).expanduser().resolve())
    live=target in _REGISTERED_ROOTS
    store(target).health(PROCESS,{'version':'2.0.0-test.14','pid':os.getpid(),'request_hooks_registered':live,
      'heartbeat_at':time.time() if live else None,'last_failure_at':LAST_FAILURE_AT,'adapters':dict(ADAPTERS),'recorder_failures':FAILURES,
      'coverage':'main hooks + guarded auxiliary/compression adapters; not a transport proxy',
      'limits':['SDK-internal retries and calls bypassing Hermes are not individually visible.',
                'No authoritative usage supplied => unknown, never synthesized.',
                'Provider adapters may omit raw cache categories.',
                'Each producer/profile must load this plugin. Remote hosts are not centrally merged.',
                'Codex app-server runtime is not covered by this direct-provider build.']})


def start_heartbeat(root=None):
    """One heartbeat writer and bounded open-request batch per producer/profile.

    Call only AFTER request hooks register successfully. The target is captured
    here rather than resolved in the daemon, so profile ContextVars cannot drift.
    """
    from pathlib import Path
    from .connection import HEARTBEAT_SECONDS
    target=str(Path(root or home()).expanduser().resolve())
    key=(os.getpid(),target)
    with _LOCK:
        _REGISTERED_ROOTS.add(target)
        existing=_HEARTBEATS.get(key)
        if existing and existing[1].is_alive():return existing[1]
        stop=threading.Event()
        def run():
            while not stop.wait(HEARTBEAT_SECONDS):
                if os.getpid()!=key[0]:break
                safe(reconcile)(store(target))
                health(target)
        thread=threading.Thread(target=run,name='usage-recorder-heartbeat',daemon=True)
        _HEARTBEATS[key]=(stop,thread)
        safe(reconcile)(store(target))
        health(target)
        thread.start()
        return thread


def stop_heartbeats():
    # No disk writes during shutdown. The lease expires if the process dies.
    with _LOCK:
        for stop,thread in _HEARTBEATS.values():stop.set()
        _HEARTBEATS.clear();_REGISTERED_ROOTS.clear()

atexit.register(stop_heartbeats)

def response_metadata(response):
    obj=mapping(response)
    out={}
    for field,target in (('model','response_model'),('service_tier','returned_service_tier'),('id','provider_response_id')):
        if isinstance(obj.get(field),str) and obj[field]:out[target]=text(obj[field])
    return out

def body_settings(kw):
    body=mapping(mapping(kw.get('request')).get('body'))
    # Same helper also accepts the kwargs passed to the LLM client.
    if not body:body=kw.get('_request_kwargs') or {}
    extra=mapping(body.get('extra_body'))
    reasoning=mapping(body.get('reasoning') or extra.get('reasoning'))
    return {'service_tier':text(body.get('service_tier') or extra.get('service_tier') or extra.get('speed') or 'unspecified',80),
            'reasoning_effort':text(reasoning.get('effort') or body.get('reasoning_effort'),80)}

def meta(kw):
    return {k:text(kw.get(k)) for k in ('session_id','turn_id','task_id','platform','model','provider','api_mode')}

@safe
def pre(**kw):
    from .adapters import install
    install()
    started=timestamp(kw.get('started_at'))
    aid=text(kw.get('api_request_id')) or uuid.uuid4().hex
    key=f'{PROCESS}:{aid}:{started:.6f}'
    rec={'id':key,**meta(kw),'started':started,'provider':text(kw.get('provider')) or 'unknown',
         'task':text(kw.get('task_id')) or 'main','source':'main_hook','status':'pending','process':PROCESS,'owner':identity(),
         'api_request_id':aid,'approx_input_tokens':num(kw.get('approx_input_tokens')),
         'retry_count':num(kw.get('retry_count')),**body_settings(kw)}
    root=str(home()); CURRENT.set({'id':key,'root':root,'session_id':rec['session_id'],'provider':rec['provider'],'api_mode':rec['api_mode'],'api_request_id':aid})
    with _LOCK:
        _LOOKUP[(root,rec['session_id'],aid)]=key
        if rec['turn_id']:
            _TURN_ROOTS[(root,rec['session_id'],rec['turn_id'])]=True
            if len(_TURN_ROOTS)>20000:
                for k in list(_TURN_ROOTS)[:10000]:_TURN_ROOTS.pop(k,None)
        if len(_LOOKUP)>20000:
            for k in list(_LOOKUP)[:10000]:_LOOKUP.pop(k,None)
    from .attribution import capture
    rec.update(capture(store(root),rec['session_id'],rec.get('platform','')))
    store(root).request(rec,'request_started');health()
    from .skills import pre as skills_pre
    skills_pre(store(root),rec,kw)

def request_context(kw):
    root=str(home());aid=text(kw.get('api_request_id'));sid=text(kw.get('session_id'))
    with _LOCK:
        key=_LOOKUP.get((root,sid,aid))
        matches=[(p,k) for (p,s,a),k in _LOOKUP.items() if s==sid and a==aid] if aid and sid and not key else []
    if not key and len(matches)==1:root,key=matches[0]
    if key:return {'id':key,'root':root,'session_id':sid,'provider':kw.get('provider',''),'api_mode':kw.get('api_mode',''),'api_request_id':aid}
    cur=CURRENT.get()
    # Only legacy callbacks without request IDs may use the same-session context.
    if not aid and cur and sid and cur['session_id']==sid:return cur
    return None

@safe
def agent_context(agent):
    """Resolve only an exact session/request pair, never 'the latest' request."""
    sid=text(getattr(agent,'session_id',''));aid=text(getattr(agent,'_current_api_request_id',''))
    cur=CURRENT.get()
    if aid and sid:
        root=str(home())
        with _LOCK:
            key=_LOOKUP.get((root,sid,aid))
            matches=[(p,k) for (p,s,a),k in _LOOKUP.items() if s==sid and a==aid] if not key else []
        if key:
            return {'id':key,'root':root,'session_id':sid,'api_request_id':aid,
                    'provider':text(getattr(agent,'provider','')),'api_mode':text(getattr(agent,'api_mode',''))}
        # A worker may have lost the profile ContextVar as well. A unique exact
        # pair identifies its registered root; ambiguity is never guessed.
        if len(matches)==1:
            root,key=matches[0]
            return {'id':key,'root':root,'session_id':sid,'api_request_id':aid,
                    'provider':text(getattr(agent,'provider','')),'api_mode':text(getattr(agent,'api_mode',''))}
        return None
    if cur and sid and cur['session_id']==sid:return cur
    return None

@safe
def capture_raw(response,agent=None,source='response_usage',context=None):
    cur=context if context is not None else agent_context(agent) if agent is not None else CURRENT.get()
    if not cur:return
    raw=mapping(response).get('usage') if isinstance(response,dict) else getattr(response,'usage',None)
    if not safe_usage(raw):return
    usage=normalize(raw,provider=cur.get('provider',''),api_mode=cur.get('api_mode',''))
    usage['usage_source']=source
    # A later normalized adapter result must not replace retained terminal data.
    import json
    with store(cur['root']).db() as c:
        row=c.execute('SELECT data FROM requests WHERE id=?',(cur['id'],)).fetchone()
    previous=json.loads(row[0]) if row else {}
    old=previous.get('usage') or {}
    if usage_priority(old.get('usage_source'))>usage_priority(source):return
    update={'id':cur['id'],'usage':usage,**response_metadata(response)}
    if previous.get('ended') is None:update['status']='usage_received'
    store(cur['root']).request(update,'usage_received_before_normalization')

@safe
def post(**kw):
    cur=request_context(kw)
    if not cur:
        # Keep orphan success rather than losing it. The missing pre-event is explicit.
        started=timestamp(kw.get('started_at'))
        cur={'id':f'{PROCESS}:orphan:{kw.get("api_request_id") or uuid.uuid4().hex}:{started}', 'root':str(home())}
    s=store(cur['root'])
    with s.db() as c:
        row=c.execute('SELECT data FROM requests WHERE id=?',(cur['id'],)).fetchone()
    import json
    old=json.loads(row[0]) if row else {}
    usage=old.get('usage')
    if not usage or (not usage.get('raw_usage') and usage.get('usage_source')!='wire_terminal_usage'):
        candidate=normalize(canonical=kw.get('usage'))
        if not usage or candidate.get('raw_usage'):usage=candidate
    rec={'id':cur['id'],**meta(kw),'started':old.get('started',timestamp(kw.get('started_at'))),
         'ended':timestamp(kw.get('ended_at')),'status':'completed','source':'main_hook',
         'response_model':text(kw.get('response_model')) or old.get('response_model',''),'usage':usage,
         'orphan_success':not bool(row),'process':PROCESS,'owner':old.get('owner') or identity()}
    s.request(rec,'request_completed');health()
    if CURRENT.get() and CURRENT.get()['id']==cur['id']:CURRENT.set(None)

@safe
def error(**kw):
    cur=request_context(kw)
    if cur:
        error=kw.get('error')
        error_class=mapping(error).get('type') if isinstance(error,dict) else type(error).__name__
        if not isinstance(error_class,str) or len(error_class)>100 or not error_class.isidentifier():error_class='UnknownError'
        store(cur['root']).request({'id':cur['id'],'ended':timestamp(kw.get('ended_at')),'status':'failed',
            'status_code':num(kw.get('status_code')),'error_class':error_class},'request_failed')
    else:ADAPTERS['orphan_error']='error hook without matching request'
    health()
    if cur and CURRENT.get() and CURRENT.get()['id']==cur['id']:CURRENT.set(None)

@safe
def session_end(**kw):
    # Hermes emits this at TURN completion; missing identity must not sweep a session.
    sid,turn=text(kw.get('session_id')),text(kw.get('turn_id'))
    if not sid or not turn:return
    root=str(home())
    with _LOCK:
        if (root,sid,turn) not in _TURN_ROOTS:
            roots=[p for p,s,t in _TURN_ROOTS if s==sid and t==turn]
            if len(roots)!=1:return
            root=roots[0]
    outcome={k:kw[k] for k in ('completed','failed','interrupted') if isinstance(kw.get(k),bool)}
    reason=kw.get('turn_exit_reason')
    allowed={'unknown','completed','interrupted','failed','interpreter_shutdown','partial_stream_recovery',
             'fallback_prior_turn_content','empty_response_exhausted','session_persistence_failed',
             'guardrail_halt','review_input_budget_exhausted','budget_exhausted',
             'redirect_restart_limit_exceeded','compaction_handoff_not_actionable',
             'rebuilt_restart_limit_exceeded','all_retries_exhausted_no_response',
             'context_compression_timeout','ollama_runtime_context_too_small'}
    if isinstance(reason,str):outcome['exit_reason']=reason if reason in allowed else 'other'
    s=store(root)
    owner=identity()
    with s.db() as c:
        rows=c.execute("SELECT id FROM requests WHERE session_id=? AND status IN ('pending','usage_received') AND ended IS NULL AND json_extract(data,'$.turn_id')=? AND json_extract(data,'$.process')=? AND json_extract(data,'$.owner.generation')=?",(sid,turn,PROCESS,owner['generation'])).fetchall()
    for r in rows:
        s.request({'id':r['id'],'ended':time.time(),'status':'ended_without_usage','turn_outcome':outcome},
                  'request_ended_without_usage',expected={'status':('pending','usage_received'),'turn_id':(turn,), 'process':(PROCESS,), 'owner':(owner,), 'ended':(None,)})
    from .skills import turn_end as skills_turn_end
    skills_turn_end(s,kw)
    reconcile(s)
    health(root)


@safe
def session_start(**kw):
    from .attribution import capture
    reconcile(store())
    capture(store(),text(kw.get('session_id')),text(kw.get('platform')))

@safe
def subagent_start(**kw):
    from .attribution import lifecycle
    lifecycle(store(),'start',kw);health()

@safe
def subagent_stop(**kw):
    from .attribution import lifecycle
    lifecycle(store(),'stop',kw);health()

"""Guarded in-memory observers of current-main internal call boundaries.

Never change provider request arguments, inference results, retries, compaction
decisions, or exceptions. The hook summary gains a numeric-only usage envelope. No on-disk Hermes core patches. Signature drift is reported.
"""
from __future__ import annotations
import contextvars, functools, importlib, inspect, threading, time, uuid
from . import recorder as r
from .accounting import mapping,normalize,safe_usage,num
_LOCK=threading.RLock();_LAST=0

def patch(obj,name,factory,required):
    key=f'{getattr(obj,"__name__",type(obj).__name__)}.{name}'
    old=getattr(obj,name,None)
    if getattr(old,'_ai_usage_observer',False):r.ADAPTERS[key]='installed';return
    if not callable(old):r.ADAPTERS[key]='unavailable';return
    try:params=inspect.signature(old).parameters
    except (TypeError,ValueError):r.ADAPTERS[key]='signature unavailable';return
    if not set(required)<=set(params):r.ADAPTERS[key]='signature mismatch; NOT wrapped';return
    new=factory(old);new._ai_usage_observer=True
    setattr(obj,name,new);r.ADAPTERS[key]='installed'

@r.safe
def install():
    global _LAST
    with _LOCK:
        if time.monotonic()-_LAST<5:return
        _LAST=time.monotonic()
        specs=[('run_agent',lambda m:patch(m.AIAgent,'_usage_summary_for_api_request_hook',raw_wrapper,('self','response'))),
          ('openai._base_client',install_wire_probe),('agent.auxiliary_client',install_aux),('agent.conversation_compression',install_comp),
          ('agent.context_compressor',install_micro),('agent.codex_runtime',install_codex_stream),('agent.anthropic_adapter',install_anthropic),
          ('agent.gemini_native_adapter',install_gemini),('agent.chat_completion_helpers',install_native_main),
          ('agent.relay_llm',install_anthropic_accumulator)]
        for name,fn in specs:
            try:fn(importlib.import_module(name))
            except Exception as exc:r.ADAPTERS[name]='unavailable: '+type(exc).__name__

def install_wire_probe(module):
    from .cache_probe import install as install_probe
    install_probe(module)

def raw_wrapper(original):
    @functools.wraps(original)
    def wrapped(self,response,*a,**kw):
        r.capture_raw(response,self)
        result=original(self,response,*a,**kw)
        # Carry the content-free usage object through the SAME request hook.
        # This does not depend on thread context surviving between callbacks.
        raw=safe_usage(mapping(response).get('usage'))
        if isinstance(result,dict) and raw:return {**result,'_ai_usage_raw_usage':raw}
        return result
    return wrapped

def aux_metadata(module,kwargs,options):
    context={}
    var=getattr(module,'_RELAY_AUX_CALL_CONTEXT',None)
    if var is not None:
        try:context=var.get() or {}
        except Exception:pass
    runtime={}
    var=getattr(module,'_RUNTIME_MAIN_CONTEXT',None)
    if var is not None:
        try:runtime=var.get() or {}
        except Exception:pass
    comp=r.COMPRESSION.get()
    cur=r.CURRENT.get() or {}
    return {'id':f'{r.PROCESS}:aux:{uuid.uuid4().hex}','started':time.time(),'status':'pending','source':'auxiliary_adapter',
      'process':r.PROCESS,'owner':r.identity(),'provider':r.text(options.get('provider') or context.get('provider') or 'unknown'),
      'api_mode':r.text(options.get('api_mode') or context.get('api_mode')),
      'model':r.text(kwargs.get('model') or context.get('model')),
      'session_id':r.text((comp or {}).get('session_id') or runtime.get('session_id') or cur.get('session_id')),
      'task':r.text(context.get('task') or ('compression' if comp else 'auxiliary')),
      'compression_id':comp.get('id') if comp else None,
      **r.body_settings({'_request_kwargs':kwargs})}

@r.safe
def aux_begin(module,kwargs,options):
    rec=aux_metadata(module,kwargs,options); root=str(r.home())
    from .attribution import capture
    rec.update(capture(r.store(root),rec['session_id']))
    r.store(root).request(rec,'request_started')
    return root,rec
@r.safe
def aux_end(item,response=None,exc=None):
    if not item:return
    root,rec=item
    raw=getattr(response,'usage',None) if not isinstance(response,dict) else response.get('usage')
    import json
    with r.store(root).db() as c:old=c.execute('SELECT data FROM requests WHERE id=?',(rec['id'],)).fetchone()
    existing=json.loads(old[0]).get('usage') if old else None
    usage=existing or normalize(raw,provider=rec['provider'],api_mode=rec['api_mode'])
    if not existing:usage['usage_source']='adapter_response_usage' if safe_usage(raw) else 'missing'
    r.store(root).request({'id':rec['id'],'ended':time.time(),'status':'failed' if exc else 'completed',
       'error_class':type(exc).__name__ if exc else None,'usage':usage,
       **r.response_metadata(response)},'request_failed' if exc else 'request_completed')
    r.health()

def auxiliary_wrapper(module,asynchronous=False):
    def factory(original):
        if asynchronous:
            @functools.wraps(original)
            async def wrapped(client,kwargs,**opts):
                item=aux_begin(module,kwargs,opts);token=r.AUX.set(item)
                try:
                    try:result=await original(client,kwargs,**opts)
                    except BaseException as exc:aux_end(item,exc=exc);raise
                    aux_end(item,result);return result
                finally:r.AUX.reset(token)
        else:
            @functools.wraps(original)
            def wrapped(client,kwargs,**opts):
                item=aux_begin(module,kwargs,opts);token=r.AUX.set(item)
                try:
                    try:result=original(client,kwargs,**opts)
                    except BaseException as exc:aux_end(item,exc=exc);raise
                    aux_end(item,result);return result
                finally:r.AUX.reset(token)
        return wrapped
    return factory

def install_aux(m):
    patch(m,'_relay_sync_completion',auxiliary_wrapper(m),('client','kwargs'))
    patch(m,'_relay_async_completion',auxiliary_wrapper(m,True),('client','kwargs'))
    if hasattr(m,'_CodexCompletionsAdapter'):
        patch(m._CodexCompletionsAdapter,'create',codex_direct_wrapper(m),('self','kwargs'))
    r.ADAPTERS['auxiliary_generic_direct_stream']='not intercepted: non-Codex direct streaming helper routes require a future adapter'

# Content-free native compression telemetry fields. Explicit allowlist avoids
# persisting focus-topic prompts, summaries, messages or arbitrary future fields.
COMP_FIELDS={'attempt_id','session_id','trigger_source','main_provider','main_model','main_context_limit',
 'current_estimated_tokens','effective_threshold','protected_head_tokens','protected_tail_tokens','middle_window_tokens',
 'prellm_skip_count','aux_prompt_tokens','aux_output_reservation','aux_provider','aux_model','effective_aux_context',
 'fit_margin','chunking','chunk_count','total_duration_ms','aux_call_duration_ms','fallback_used','commit_status','split_status','failure_class'}

def comp_base(agent,kwargs,kind='compression'):
    cc=getattr(agent,'context_compressor',agent)
    return {'id':f'{r.PROCESS}:comp:{uuid.uuid4().hex}','started':time.time(),'kind':kind,
        'session_id':r.text(getattr(agent,'session_id',getattr(cc,'_session_id',''))),
        'provider':r.text(getattr(agent,'provider',getattr(cc,'provider','unknown'))),
        'model':r.text(getattr(agent,'model',getattr(cc,'model',''))),'status':'pending',
        'trigger':'manual' if kwargs.get('force') else ('idle' if kind=='micro_compaction' else 'automatic'),
        'before_estimated_tokens':num(kwargs.get('approx_tokens')),
        'prior_reported_prompt_tokens':num(getattr(cc,'last_real_prompt_tokens',None)),
        'context_limit':num(getattr(cc,'_resolved_context_length',None)),
        'threshold_tokens':num(getattr(cc,'_threshold_tokens',None)),'process':r.PROCESS}

@r.safe
def comp_start(agent,kwargs,kind):
    rec=comp_base(agent,kwargs,kind);root=str(r.home())
    from .attribution import capture
    rec.update(capture(r.store(root),rec['session_id'],r.text(getattr(agent,'platform',''))))
    r.store(root).compression(rec,'compression_started')
    return root,rec
@r.safe
def comp_end(item,agent,exc=None):
    if not item:return
    root,rec=item;cc=getattr(agent,'context_compressor',agent)
    from .attribution import capture
    new_sid=r.text(getattr(agent,'session_id',getattr(cc,'_session_id','')))
    if new_sid and new_sid!=rec['session_id']:
        s=r.store(root)
        with s.db() as c:
            from .attribution import resolve
            ctx=resolve(c,rec['session_id'])
        capture(s,new_sid,explicit={k:v for k,v in ctx.items() if k in ('agent_kind','parent_session_id','subagent_id','parent_subagent_id','agent_role') and v})
    r.store(root).compression({'id':rec['id'],'ended':time.time(),
       'session_after':r.text(getattr(agent,'session_id',getattr(cc,'_session_id',''))),
       'after_estimated_tokens':num(getattr(cc,'last_compression_rough_tokens',None)) if getattr(agent,'_last_compression_attempt_in_place',None) in (True,False) and getattr(agent,'_last_compression_attempt_in_place',None) is not None else None,
       'wrapper_result':'raised' if exc else 'returned','error_class':type(exc).__name__ if exc else None},'compression_returned')
    r.health()

def compression_wrapper(original):
    @functools.wraps(original)
    def wrapped(agent,messages,system_message,**kwargs):
        item=comp_start(agent,kwargs,'compression');token=r.COMPRESSION.set(item[1] if item else None)
        from .skills import compression, compression_result
        compression(item,agent,messages,getattr(agent,'_cached_system_prompt',None),'compression_before')
        try:
            result=original(agent,messages,system_message,**kwargs)
        except BaseException as exc:comp_end(item,agent,exc);raise
        else:
            comp_end(item,agent)
            compression_result(item,agent,result)
            return result
        finally:r.COMPRESSION.reset(token)
    return wrapped

@r.safe
def native_comp_event(agent,kwargs):
    current=r.COMPRESSION.get()
    if not current:return
    telemetry=getattr(agent.context_compressor,'_last_compression_telemetry',{}) or {}
    payload={k:v for k,v in telemetry.items() if k in COMP_FIELDS and isinstance(v,(str,int,float,bool))}
    r.store().compression({'id':current['id'],'native':payload,
        'status':r.text(kwargs.get('commit_status')) or 'observed',
        'split_status':r.text(kwargs.get('split_status')),
        'native_attempt_id':r.text(payload.get('attempt_id') or getattr(agent,'_compression_attempt_id','')),
        'trigger':r.text(payload.get('trigger_source') or current.get('trigger')),
        'before_estimated_tokens':num(payload.get('current_estimated_tokens')),
        'threshold_tokens':num(payload.get('effective_threshold')),
        'context_limit':num(payload.get('main_context_limit')),
        'provider':r.text(payload.get('main_provider') or current.get('provider')),
        'error_class':r.text(kwargs.get('failure_class')) or None},'compression_native_telemetry')

def comp_emitter_wrapper(original):
    @functools.wraps(original)
    def wrapped(agent,**kwargs):
        native_comp_event(agent,kwargs)
        return original(agent,**kwargs)
    return wrapped

def install_comp(m):
    patch(m,'compress_context',compression_wrapper,('agent','messages','system_message'))
    patch(m,'_emit_compression_attempt_telemetry',comp_emitter_wrapper,('agent','commit_status','split_status'))

def micro_wrapper(original):
    @functools.wraps(original)
    def wrapped(self,messages,*args,**kwargs):
        # Allocate a correlation id but persist only when native micro telemetry fires.
        rec=comp_base(self,{},'micro_compaction');token=r.COMPRESSION.set(rec)
        try:return original(self,messages,*args,**kwargs)
        finally:r.COMPRESSION.reset(token)
    return wrapped
@r.safe
def native_micro(cc,kw):
    rec=r.COMPRESSION.get() or comp_base(cc,{},'micro_compaction')
    r.store().compression({**rec,'ended':time.time(),'status':r.text(kw.get('outcome')),
      'session_after':r.text(getattr(cc,'_session_id','')),'before_estimated_tokens':num(kw.get('tokens_before')),
      'after_estimated_tokens':num(kw.get('tokens_after')),'messages_before':num(kw.get('messages_before')),
      'messages_after':num(kw.get('messages_after')),'duration_ms':num(kw.get('duration_ms'))},'micro_compaction_native_telemetry')
    r.health()
def micro_emitter_wrapper(original):
    @functools.wraps(original)
    def wrapped(self,**kw):
        native_micro(self,kw);return original(self,**kw)
    return wrapped

def install_micro(m):
    patch(m.ContextCompressor,'_micro_compact',micro_wrapper,('self','messages'))
    patch(m.ContextCompressor,'_emit_micro_compaction_telemetry',micro_emitter_wrapper,('self','tokens_before','tokens_after','outcome'))


@r.safe
def capture_native_response(response,source='native_terminal_usage'):
    item=r.AUX.get()
    if not item:
        r.capture_raw(response,source=source);return
    root,rec=item
    raw=mapping(response).get('usage')
    if not safe_usage(raw):return
    usage=normalize(raw,provider=rec['provider'],api_mode=rec['api_mode'])
    usage['usage_source']=source
    # Terminal SSE evidence is stronger than an assembled/normalized response.
    import json
    with r.store(root).db() as c:
        row=c.execute('SELECT data FROM requests WHERE id=?',(rec['id'],)).fetchone()
    previous=json.loads(row[0]) if row else {}
    if r.usage_priority((previous.get('usage') or {}).get('usage_source'))>r.usage_priority(source):return
    update={'id':rec['id'],'usage':usage,**r.response_metadata(response)}
    if previous.get('ended') is None:update['status']='usage_received'
    r.store(root).request(update,'native_usage_received')

def codex_stream_wrapper(original):
    @functools.wraps(original)
    def wrapped(event_iter,**kwargs):
        callback=kwargs.get('on_event')
        def observe(event):
            ev=mapping(event)
            if ev.get('type') in ('response.completed','response.incomplete','response.failed'):
                capture_native_response(ev.get('response'))
            if callback is not None:return callback(event)
        result=original(event_iter,**{**kwargs,'on_event':observe})
        capture_native_response(result,'native_assembled_usage')
        return result
    return wrapped
def install_codex_stream(m):
    patch(m,'_consume_codex_event_stream',codex_stream_wrapper,('event_iter','on_event'))
    patch(m,'run_codex_stream',codex_run_wrapper,('agent','api_kwargs'))

def codex_direct_wrapper(module):
    def factory(original):
        @functools.wraps(original)
        def wrapped(self,**kwargs):
            if r.AUX.get():return original(self,**kwargs)
            # Handles the Codex MoA direct-stream special case that bypasses Relay.
            # Only hostname is inspected locally; no endpoint or key is exported.
            from urllib.parse import urlparse
            host=urlparse(str(getattr(getattr(self,'_client',None),'base_url',''))).hostname or ''
            provider='openai-codex' if host=='chatgpt.com' else 'copilot' if host.endswith('githubcopilot.com') else 'xai' if host.endswith('x.ai') else 'unknown'
            item=aux_begin(module,kwargs,{'provider':provider,'api_mode':'codex_responses'});token=r.AUX.set(item)
            try:
                try:result=original(self,**kwargs)
                except BaseException as exc:aux_end(item,exc=exc);raise
                aux_end(item,result);return result
            finally:r.AUX.reset(token)
        return wrapped
    return factory
def anthropic_wrapper(original):
    @functools.wraps(original)
    def wrapped(client,api_kwargs,**kwargs):
        result=original(client,api_kwargs,**kwargs)
        capture_native_response(result,'native_anthropic_usage')
        return result
    return wrapped
def install_anthropic(m):
    patch(m,'create_anthropic_message',anthropic_wrapper,('client','api_kwargs'))


# A worker may not inherit CURRENT (and may retain a stale one). Resolve the
# exact agent/request pair at the actual Hermes dispatch boundary instead.
_ANTHROPIC_SCOPE: contextvars.ContextVar[dict | None]=contextvars.ContextVar('usage_anthropic_stream',default=None)

def native_main_wrapper(original,anthropic=False,agent_argument=False):
    @functools.wraps(original)
    def wrapped(owner,*args,**kwargs):
        agent=owner if agent_argument else getattr(owner,'agent',None)
        resolved=r.agent_context(agent)
        cur=dict(resolved) if resolved else None
        token=r.CURRENT.set(cur)
        # Main requests must not accidentally attach to inherited auxiliary state.
        aux_token=r.AUX.set(None)
        scope_token=_ANTHROPIC_SCOPE.set({'context':cur,'usage':{},'response':{}} if anthropic else None)
        try:return original(owner,*args,**kwargs)
        finally:
            _ANTHROPIC_SCOPE.reset(scope_token)
            r.AUX.reset(aux_token)
            r.CURRENT.reset(token)
    return wrapped

def install_native_main(m):
    cls=getattr(m,'_StreamingCall',None)
    if cls is not None:
        patch(cls,'_call_anthropic',lambda fn:native_main_wrapper(fn,anthropic=True),('self','request_client'))
        patch(cls,'_call_chat_completions',native_main_wrapper,('self','stream_attempt_id'))
    patch(m,'_dispatch_nonstreaming_api_request',lambda fn:native_main_wrapper(fn,agent_argument=True),('agent','api_kwargs','make_client'))


@r.safe
def capture_gemini_usage(usage_meta):
    if safe_usage(usage_meta):
        capture_native_response({'usage':usage_meta},'native_gemini_usage')
        return
    # Even wholly absent metadata is evidence that the converter's zeroes were
    # not reported. Retain unknowns at native priority; do not claim usage arrived.
    item=r.AUX.get()
    cur=r.CURRENT.get()
    if item:
        root,rec=item
    elif cur:
        root,rec=cur['root'],cur
    else:return
    usage=normalize()
    usage.update(usage_source='native_gemini_usage',raw_usage={})
    r.store(root).request({'id':rec['id'],'usage':usage},'native_usage_absent')


def gemini_usage_wrapper(original):
    @functools.wraps(original)
    def wrapped(usage_meta,*args,**kwargs):
        # BEFORE Gemini's OpenAI conversion inserts zero cache/total fields and
        # drops thoughtsTokenCount. Never alter the conversion or its result.
        capture_gemini_usage(usage_meta)
        return original(usage_meta,*args,**kwargs)
    return wrapped

def install_gemini(m):
    patch(m,'_usage_from_metadata',gemini_usage_wrapper,('usage_meta',))


def native_mapping(value):
    # SDK defaults are not provider reports. In particular an output-only delta
    # must not overwrite input/cache evidence with model default zeroes.
    if hasattr(value,'model_dump'):
        return value.model_dump(exclude_none=True,exclude_unset=True)
    return mapping(value)

@r.safe
def observe_anthropic_usage(event):
    scope=_ANTHROPIC_SCOPE.get()
    if not scope or not scope['context']:return
    payload=native_mapping(event)
    kind=payload.get('type')
    if kind=='message_start':
        message=native_mapping(payload.get('message'))
        scope['usage']={}
        scope['response']={k:message[k] for k in ('id','model') if isinstance(message.get(k),str)}
        raw=safe_usage(native_mapping(message.get('usage')))
    elif kind=='message_delta':
        raw=safe_usage(native_mapping(payload.get('usage')))
    else:return
    if not raw:return
    # Anthropic reports cumulative snapshots, NOT additive deltas. Retain the
    # start's cache categories when a later event only reports output_tokens.
    scope['usage'].update(raw)
    # The existing native evidence priority protects these reported snapshots
    # from later lossy assembled usage. This does NOT mark a request completed;
    # an interrupted stream retains the last provider-reported snapshot only.
    r.capture_raw({**scope['response'],'usage':dict(scope['usage'])},
                  source='native_anthropic_stream_usage',context=scope['context'])

def anthropic_observe_wrapper(original):
    @functools.wraps(original)
    def wrapped(self,event,*args,**kwargs):
        observe_anthropic_usage(event)
        return original(self,event,*args,**kwargs)
    return wrapped

def install_anthropic_accumulator(m):
    cls=getattr(m,'AnthropicStreamAccumulator',None)
    if cls is not None:patch(cls,'observe',anthropic_observe_wrapper,('self','event'))


def codex_run_wrapper(original):
    @functools.wraps(original)
    def wrapped(agent,api_kwargs,*args,**kwargs):
        resolved=r.agent_context(agent)
        cur=dict(resolved) if resolved else None
        token=r.CURRENT.set(cur)
        try:
            result=original(agent,api_kwargs,*args,**kwargs)
            if cur:r.capture_raw(result,source='native_assembled_usage',context=cur)
            return result
        finally:r.CURRENT.reset(token)
    return wrapped

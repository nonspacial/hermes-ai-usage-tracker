"""Synthetic ledgers + real, AST-loaded Hermes seams; no provider clients/imports.

Set HERMES_CAPTURE_CORE to a read-only source tree in the OS-isolated runner.
Only the selected definitions are executed, never Hermes startup/module imports.
"""
import ast
import contextlib
import json
import os
from pathlib import Path
import threading
import time
from decimal import Decimal
from types import SimpleNamespace as NS, ModuleType
from typing import Any, Dict

import pytest
from _hermes_ai_usage_ledger_v2 import adapters as ad, recorder as r

CORE = Path(os.environ['HERMES_CAPTURE_CORE']) if os.environ.get('HERMES_CAPTURE_CORE') else None


def definitions(relative, names, namespace, class_name=None):
    if CORE is None:
        pytest.skip('Set HERMES_CAPTURE_CORE to run installed-core contract checks')
    tree = ast.parse((CORE / relative).read_text())
    body = tree.body
    if class_name:
        body = next(n for n in body if isinstance(n, ast.ClassDef) and n.name == class_name).body
    def name(n):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return n.name
        # Module constants the selected definitions read (e.g. a frozenset of keys).
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            return n.targets[0].id
    selected = [n for n in body if name(n) in names]
    assert {name(n) for n in selected} == set(names)
    unit = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), str(CORE / relative), 'exec'), namespace)
    return namespace


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(ad, 'install', lambda: None)
    monkeypatch.setattr(r, 'health', lambda *a, **kw: None)
    monkeypatch.setattr(r, '_STORES', {})
    monkeypatch.setattr(r, '_LOOKUP', {})
    monkeypatch.setattr(r, '_TURN_ROOTS', {})
    current = r.CURRENT.set(None)
    auxiliary = r.AUX.set(None)
    yield tmp_path
    r.CURRENT.reset(current)
    r.AUX.reset(auxiliary)


def start(aid='one', provider='anthropic', mode='anthropic_messages', **pre):
    r.pre(api_request_id=aid, session_id='fixture', turn_id='turn', provider=provider, api_mode=mode, model='fixture', started_at=time.time(), **pre)
    return NS(session_id='fixture', _current_api_request_id=aid, provider=provider, api_mode=mode, _interrupt_requested=False)


def rows(env):
    with r.store(env).db() as c:
        return {v['api_request_id']: v for v in (json.loads(row[0]) for row in c.execute('SELECT data FROM requests'))}


@pytest.fixture
def gemini():
    ns = definitions('agent/gemini_native_adapter.py', ['_usage_from_metadata'], dict(SimpleNamespace=NS, Dict=Dict, Any=Any))
    module = NS(_usage_from_metadata=ns['_usage_from_metadata'])
    ad.install_gemini(module)
    return module._usage_from_metadata


@pytest.mark.parametrize('raw', [
    {'promptTokenCount':100,'candidatesTokenCount':8,'thoughtsTokenCount':7,'totalTokenCount':115,'cachedContentTokenCount':40},
    {'promptTokenCount':100,'candidatesTokenCount':8,'thoughtsTokenCount':7,'totalTokenCount':130},
    {'totalTokenCount':0},
    {'thoughtsTokenCount':7},
    {},
])
def test_gemini_raw_precedes_lossy_conversion(env, gemini, raw):
    start(provider='google', mode='gemini')
    given = dict(raw, private_text='DO NOT SAVE')
    response = gemini(given)
    assert given == dict(raw, private_text='DO NOT SAVE')
    assert response.prompt_tokens == raw.get('promptTokenCount', 0)
    assert not hasattr(response, 'thoughtsTokenCount')
    r.capture_raw({'usage':response}, source='native_assembled_usage')
    record = rows(env)['one']
    usage = record['usage']
    assert usage['raw_usage'] == raw
    assert usage['usage_source'] == 'native_gemini_usage'
    if 'totalTokenCount' in raw:
        assert usage['total_tokens'] == raw['totalTokenCount']
    if 'cachedContentTokenCount' not in raw:
        assert usage['cache_read_tokens'] is None
    assert usage['reasoning_tokens'] == raw.get('thoughtsTokenCount')
    if 'candidatesTokenCount' in raw:
        assert usage['output_tokens'] == raw['candidatesTokenCount'] + raw.get('thoughtsTokenCount', 0)
    assert 'DO NOT SAVE' not in json.dumps(record)


def test_gemini_auxiliary_priority_and_identity(env, gemini):
    start(provider='google', mode='gemini')
    module = NS()
    def call(client, kwargs, **opts):
        return NS(usage=gemini({'promptTokenCount':20,'totalTokenCount':25,'thoughtsTokenCount':3,'candidatesTokenCount':2}))
    wrapped = ad.auxiliary_wrapper(module)(call)
    result = wrapped(None, {'model':'fixture'}, provider='google', api_mode='gemini')
    assert result.usage.total_tokens == 25
    with r.store(env).db() as c:
        records = [json.loads(row[0]) for row in c.execute('SELECT data FROM requests')]
    assert len(records) == 2
    aux = next(v for v in records if v['source'] == 'auxiliary_adapter')
    assert aux['usage']['usage_source'] == 'native_gemini_usage'
    assert aux['usage']['reasoning_tokens'] == 3
    assert aux['usage']['output_tokens'] == 5
    assert not next(v for v in records if v['source'] == 'main_hook').get('usage')


ADAPTER = ['_RESPONSES_ONLY_KWARGS', 'sanitize_anthropic_kwargs', '_UsageNormalizingStream', 'normalize_stream_usage',
           '_is_stream_unavailable_error', '_stream_final_message', 'create_anthropic_message']


def anthropic_adapter():
    """Real core anthropic_adapter helpers the main paths import; no SDK import.

    normalize_stream_usage leaves a stream without ``_raw_stream`` unchanged."""
    module = ModuleType('agent.anthropic_adapter')
    vars(module).update(Any=Any, EmptyStreamError=RuntimeError, logger=__import__('logging').getLogger('fixture.anthropic'))
    definitions('agent/anthropic_adapter.py', ADAPTER, vars(module))
    return module


@pytest.fixture
def claude(monkeypatch):
    ns = definitions('agent/relay_llm.py', ['AnthropicStreamAccumulator','_jsonable','_jsonable_dict','_namespace'],
        dict(Any=Any, SimpleNamespace=NS, contextlib=contextlib, json=json,
             _ANTHROPIC_APPEND_DELTAS={'text_delta':'text','thinking_delta':'thinking','signature_delta':'signature'}))
    relay = ModuleType('agent.relay_llm')
    relay.AnthropicStreamAccumulator = ns['AnthropicStreamAccumulator']
    relay._namespace = ns['_namespace']
    ad.install_anthropic_accumulator(relay)
    # Run the real main messages.stream branch with only transport/lifecycle fake.
    class Managed:
        output_modified = False
        def __init__(self, kwargs, opener, **options):
            self.raw = opener(kwargs)
            self.observe = options['on_chunk']
            options['on_stream_created'](self.raw)
        def __iter__(self):
            for event in self.raw:
                self.observe(event)
                yield event
    relay.stream = Managed
    agent_module = ModuleType('agent')
    agent_module.relay_llm = relay
    monkeypatch.setitem(__import__('sys').modules, 'agent', agent_module)
    monkeypatch.setitem(__import__('sys').modules, 'agent.relay_llm', relay)
    monkeypatch.setitem(__import__('sys').modules, 'agent.anthropic_adapter', anthropic_adapter())
    ns = definitions('agent/chat_completion_helpers.py', ['_call_anthropic','_check_anthropic_message','_mark_finish_seen'],
        dict(Any=Any, time=time, _relay_stream_identity=lambda *a:{}, _relay_stream_metadata=lambda *a:{},
             claim_stream_writer=lambda a:None, EmptyStreamError=RuntimeError), class_name='_StreamingCall')
    cls = type('_StreamingCall', (), {k: ns[k] for k in ('_call_anthropic','_check_anthropic_message','_mark_finish_seen')})
    ad.install_native_main(NS(_StreamingCall=cls))
    return cls, relay


def run_stream(claude, agent, events, failure=None, interrupt=False, during=None, model='fixture'):
    cls, relay = claude
    # Current core requires a terminal message_stop (else EmptyStreamError) and
    # reads SDK-style attribute events; a failed stream never reaches one.
    events = [e if not isinstance(e, dict) else relay._namespace(e) for e in (events if failure else [*events, STOP])]
    usage = NS(input_tokens=100, output_tokens=12, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    # SDK get_final_message() snapshot; its model comes from message_start.
    final = NS(id='msg-fixture',model=model,usage=usage, content=[NS(type='text')],stop_reason='end_turn')
    class Raw:
        response = None
        def __iter__(self):
            for event in events:
                if during: during()
                if interrupt: agent._interrupt_requested = True
                yield event
            if failure: raise failure
        def get_final_message(self): return final
    class Manager:
        exited = False
        def __enter__(self): return Raw()
        def __exit__(self, *args): self.exited = True
    manager = Manager()
    client = NS(messages=NS(stream=lambda **kw:manager))
    obj = cls()
    obj.agent = agent
    obj.api_kwargs = {'model':'fixture'}
    obj.last_chunk_time = {}
    obj._new_diag = lambda: {}
    obj._quiet = lambda fn: None
    obj._reabort_if_cancelled = lambda response: None
    obj._set_managed_stream = lambda stream: stream
    obj._close_managed_stream = lambda: None
    obj._count_chunk = lambda *args: None
    result = obj._call_anthropic(client)
    assert manager.exited
    return result


START = {'type':'message_start','message':{'id':'msg-fixture','model':'fixture','usage':{'input_tokens':100,'output_tokens':0,'cache_read_input_tokens':60,'cache_creation_input_tokens':20},'content':'PRIVATE'}}
DELTA = {'type':'message_delta','usage':{'output_tokens':12},'delta':{'stop_reason':'end_turn'}}
STOP = {'type':'message_stop'}


@pytest.mark.parametrize('termination', ['completed','interrupted','raised'])
def test_claude_actual_main_stream_retains_snapshots(env, claude, termination):
    agent = start()
    if termination == 'raised':
        failure = RuntimeError('synthetic interruption')
        with pytest.raises(RuntimeError) as raised:
            run_stream(claude, agent, [START, DELTA], failure=failure)
        assert raised.value is failure
        r.error(session_id='fixture', api_request_id='one', error=failure)
    else:
        result = run_stream(claude, agent, [START, DELTA, DELTA], interrupt=termination=='interrupted')
        if termination == 'completed':
            r.capture_raw(result, source='native_assembled_usage')
            r.post(session_id='fixture', api_request_id='one')
        else:
            assert result is None
            r.session_end(session_id='fixture', turn_id='turn', interrupted=True)
    data = rows(env)
    assert len(data) == 1
    record = data['one']
    usage = record['usage']
    assert usage['raw_usage'] == dict(START['message']['usage'], output_tokens=0 if termination=='interrupted' else 12)
    assert usage['cache_read_tokens'] == 60
    assert usage['cache_write_tokens'] == 20
    assert usage['request_count'] == 1
    assert usage['usage_source'] == 'native_anthropic_stream_usage'
    assert record['status'] == {'completed':'completed','interrupted':'ended_with_usage','raised':'failed'}[termination]
    assert 'PRIVATE' not in json.dumps(record)


def test_claude_worker_pins_exact_identity_not_current(env, claude):
    agent = start()
    start('two')
    before = dict(r.CURRENT.get())
    errors = []
    def worker():
        try:
            run_stream(claude, agent, [START,DELTA], during=lambda:setattr(agent,'_current_api_request_id','two'))
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start(); thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    assert r.CURRENT.get() == before
    data = rows(env)
    assert data['one']['usage']['output_tokens'] == 12
    assert not data['two'].get('usage')


def test_claude_missing_exact_pair_does_not_attach_stale_context(env, claude):
    agent = start()
    agent._current_api_request_id = 'unregistered'
    run_stream(claude, agent, [START,DELTA])
    assert not rows(env)['one'].get('usage')


def test_unscoped_accumulator_is_not_a_new_request(env, claude):
    start()
    claude[1].AnthropicStreamAccumulator().observe(START)
    assert not rows(env)['one'].get('usage')


def test_observer_failure_does_not_change_stream_or_exception(env, claude, monkeypatch):
    agent = start()
    def broken(*args, **kwargs): raise ValueError('observer-only')
    monkeypatch.setattr(ad, 'safe_usage', broken)
    result = run_stream(claude, agent, [START,DELTA])
    assert result.id == 'msg-fixture'
    assert not rows(env)['one'].get('usage')


def test_pydantic_style_default_cache_zero_is_not_reported(env, claude):
    agent = start()
    class Event:
        type = 'message_delta'
        def model_dump(self, **kwargs):
            if kwargs.get('exclude_unset'):
                return dict(DELTA)
            return {'type':'message_delta','usage':dict(DELTA['usage'],cache_read_input_tokens=0,cache_creation_input_tokens=0)}
    run_stream(claude, agent, [START,Event()])
    assert rows(env)['one']['usage']['cache_read_tokens'] == 60
    assert rows(env)['one']['usage']['cache_write_tokens'] == 20


def test_gemini_empty_metadata_stays_unknown_after_post(env, gemini):
    start(provider='google', mode='gemini')
    result = gemini({})
    r.capture_raw({'usage':result})
    r.post(session_id='fixture', api_request_id='one', usage={'input_tokens':0,'output_tokens':0,'total_tokens':0})
    usage = rows(env)['one']['usage']
    assert usage['raw_usage'] == {}
    assert all(usage[k] is None for k in ('input_tokens','output_tokens','cache_read_tokens','total_tokens'))


def test_gemini_worker_restores_context_and_propagates_failure(env, gemini):
    agent = start(provider='google', mode='gemini')
    start('two', provider='google', mode='gemini')
    prior = dict(r.CURRENT.get())
    failure = RuntimeError('provider failed')
    def dispatch(agent, api_kwargs, *, make_client):
        gemini({'promptTokenCount':10,'totalTokenCount':13})
        raise failure
    module = NS(_dispatch_nonstreaming_api_request=dispatch)
    ad.install_native_main(module)
    with pytest.raises(RuntimeError) as caught:
        module._dispatch_nonstreaming_api_request(agent, {}, make_client=None)
    assert caught.value is failure
    assert r.CURRENT.get() == prior
    assert rows(env)['one']['usage']['total_tokens'] == 13
    assert not rows(env)['two'].get('usage')


def test_claude_output_only_usage_does_not_invent_input(env, claude):
    agent = start()
    run_stream(claude, agent, [DELTA])
    usage = rows(env)['one']['usage']
    assert usage['raw_usage'] == {'output_tokens':12}
    assert usage['output_tokens'] == 12
    assert all(usage[k] is None for k in ('input_tokens','cache_read_tokens','cache_write_tokens','total_tokens'))


def test_claude_scope_reset_after_failure_and_new_request(env, claude):
    agent = start()
    with pytest.raises(RuntimeError):
        run_stream(claude, agent, [START], failure=RuntimeError('drop'))
    assert ad._ANTHROPIC_SCOPE.get() is None
    newer = start('two')
    run_stream(claude, newer, [DELTA])
    data = rows(env)
    assert data['one']['usage']['cache_read_tokens'] == 60
    assert data['two']['usage']['cache_read_tokens'] is None
    assert len(data) == 2


# --- Candidate metadata/cost through the actual main Claude capture paths ------------

# Shape of a native Anthropic usage block (TTL split + documented billing enums);
# the synthetic catalogue prices claude-opus-9-5 at 4/20/0.20/5/8 standard, 8/40/0.4/10/16 fast.
PRICED_START = {'type':'message_start','message':{'id':'msg-priced','model':'claude-opus-9-5','content':'PRIVATE',
    'usage':{'input_tokens':1000,'output_tokens':0,'cache_read_input_tokens':20000,'cache_creation_input_tokens':3000,
             'cache_creation':{'ephemeral_5m_input_tokens':1000,'ephemeral_1h_input_tokens':2000},'inference_geo':'global'}}}
PRICED_DELTA = {'type':'message_delta','usage':{'output_tokens':500,'speed':'fast','service_tier':'standard'},'delta':{'stop_reason':'end_turn'}}
ENDPOINTS = [('https://api.anthropic.com', 'first_party'), ('https://gw.example/anthropic', 'custom')]


@pytest.fixture
def priced(env):
    import test_anthropic_pricing as T  # synthetic catalogue fixtures, no network
    T.insert(r.store(str(env)), T.catalog(), time.time() - 10)
    return env


def post_hook(agent, response_model, usage=None):
    """Kwargs subset core's post_api_request hook sends (turn_response_intake), with
    Hermes' lossy canonical usage; retained native evidence must win over it."""
    r.post(task_id='', turn_id='turn', api_request_id='one', session_id='fixture', platform='', model='fixture',
           provider='anthropic', base_url=agent.base_url, api_mode='anthropic_messages', started_at=time.time() - 1,
           ended_at=time.time(), response_model=response_model,
           usage=usage or {'input_tokens': 100, 'output_tokens': 12, 'cache_read_tokens': 0, 'cache_write_tokens': 0})


def assert_priced(record, endpoint, total, speed):
    usage = record['usage']
    assert record['anthropic_endpoint'] == endpoint and record['response_model'] == 'claude-opus-9-5'
    assert record['returned_speed'] == speed and record['returned_inference_geo'] == 'global'
    assert record['returned_usage_service_tier'] == 'standard'
    assert (usage['input_tokens'], usage['output_tokens'], usage['cache_read_tokens'], usage['cache_write_tokens']) == (1000, 500, 20000, 3000)
    assert record['provider'] == 'anthropic'
    rate = record['cost']['rate']
    if endpoint == 'first_party':
        assert rate['service_tier'] == speed and rate['speed_inferred'] is False and rate['endpoint_assumption'] is False
        assert rate['retrospective'] is False
        assert Decimal(record['cost']['total_usd']) == Decimal(total) and record['cost']['complete']
    else:  # a custom gateway is never valued at first-party list prices
        assert rate is None and record['cost']['total_usd'] is None
    dumped = json.dumps(record)
    assert 'PRIVATE' not in dumped and 'gw.example' not in dumped and 'api.anthropic.com' not in dumped


@pytest.mark.parametrize('base_url,endpoint', ENDPOINTS)
def test_claude_actual_main_stream_prices_candidate_metadata(priced, claude, base_url, endpoint):
    agent = start(base_url=base_url)
    agent.base_url = base_url
    result = run_stream(claude, agent, [PRICED_START, PRICED_DELTA], model='claude-opus-9-5')
    assert rows(priced)['one']['usage']['usage_source'] == 'native_anthropic_stream_usage'
    # The lossy assembled message usage (100/12, no cache/enums) must not replace native evidence.
    r.capture_raw(result, source='native_assembled_usage')
    post_hook(agent, result.model)
    record = rows(priced)['one']
    assert record['status'] == 'completed' and record['usage']['usage_source'] == 'native_anthropic_stream_usage'
    assert_priced(record, endpoint, '0.078', 'fast')


@pytest.fixture
def claude_nonstream(monkeypatch):
    """Real _dispatch_nonstreaming_api_request -> _anthropic_messages_create ->
    create_anthropic_message (installed wrapper) with only the SDK client fake."""
    adapter = anthropic_adapter()
    ad.install_anthropic(adapter)
    agent_module = ModuleType('agent')
    agent_module.anthropic_adapter = adapter
    monkeypatch.setitem(__import__('sys').modules, 'agent', agent_module)
    monkeypatch.setitem(__import__('sys').modules, 'agent.anthropic_adapter', adapter)
    helpers = definitions('agent/chat_completion_helpers.py', ['_dispatch_nonstreaming_api_request'], dict(Any=Any))
    module = NS(_dispatch_nonstreaming_api_request=helpers['_dispatch_nonstreaming_api_request'])
    ad.install_native_main(module)
    lifecycle = definitions('agent/client_lifecycle.py', ['_anthropic_messages_create'], dict(Any=Any), class_name='ClientLifecycleMixin')
    return module._dispatch_nonstreaming_api_request, lifecycle['_anthropic_messages_create']


@pytest.mark.parametrize('streamed', [True, False])
@pytest.mark.parametrize('base_url,endpoint', ENDPOINTS)
def test_claude_actual_main_nonstream_prices_candidate_metadata(priced, claude_nonstream, base_url, endpoint, streamed):
    dispatch, create = claude_nonstream
    usage = dict(PRICED_START['message']['usage'], output_tokens=500, speed='standard', service_tier='standard')
    message = NS(id='msg-priced', model='claude-opus-9-5', content='PRIVATE', stop_reason='end_turn', usage=relay_namespace(usage))
    calls = []
    class Stream:
        response = None
        def __iter__(self):
            yield NS(type='message_delta', delta=NS(stop_reason='end_turn'))
            yield NS(type='message_stop')
        def get_final_message(self): return message
    class Manager:
        def __enter__(self): return Stream()
        def __exit__(self, *args): calls.append('exited')
    def stream(**kw): calls.append(('stream', sorted(kw))); return Manager()
    def create_(**kw): calls.append(('create', sorted(kw))); return message
    client = NS(messages=NS(stream=stream, create=create_))
    agent = start(base_url=base_url)
    agent.base_url = base_url
    agent.log_prefix = ''
    agent._disable_streaming = not streamed
    agent._capture_anthropic_response_headers = lambda response: None
    agent._anthropic_messages_create = create.__get__(agent)
    kwargs = {'model': 'claude-opus-9-5', 'messages': [], 'input': 'leaked'}  # Responses-only key is stripped
    result = dispatch(agent, kwargs, make_client=lambda reason, kind=None: client)
    assert result is message and 'input' not in kwargs
    assert calls == ([('stream', ['messages', 'model']), 'exited'] if streamed else [('create', ['messages', 'model'])])
    record = rows(priced)['one']
    assert record['usage']['usage_source'] == 'native_anthropic_usage'
    post_hook(agent, result.model)
    record = rows(priced)['one']
    assert record['status'] == 'completed' and record['usage']['usage_source'] == 'native_anthropic_usage'
    assert_priced(record, endpoint, '0.039', 'standard')


AUX_CLIENT = ['_relay_sync_completion', '_relay_async_completion', '_ChatShim', '_AsyncCompletionsAdapter',
              '_AsyncAuxiliaryClientBase', 'AnthropicAuxiliaryClient', 'AsyncAnthropicAuxiliaryClient']


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('base_url,endpoint', [*ENDPOINTS, ('https://api.anthropic.com./', 'first_party'),
                                                ('https://bedrock-runtime.us-east-1.amazonaws.com', 'custom')])
def test_claude_actual_auxiliary_relay_prices_by_client_endpoint(priced, monkeypatch, base_url, endpoint, asynchronous):
    """Real auxiliary relay funnel + Anthropic client classes (unmanaged route) with
    the installed create_anthropic_message wrapper; only the provider call is fake."""
    wire = ModuleType('agent.auxiliary_wire')
    wire.prepare_chat_messages = lambda client, kwargs: kwargs
    monkeypatch.setitem(__import__('sys').modules, 'agent', ModuleType('agent'))
    monkeypatch.setitem(__import__('sys').modules, 'agent.auxiliary_wire', wire)
    module = ModuleType('agent.auxiliary_client')
    vars(module).update(Any=Any, Callable=__import__('typing').Callable, _relay_auxiliary_metadata=lambda **kw: None,
                        _run_protected_sync_provider_call=lambda callback, kwargs: callback(kwargs),
                        _AnthropicCompletionsAdapter=lambda *a, **kw: None)
    definitions('agent/auxiliary_client.py', AUX_CLIENT, vars(module))
    ad.install_aux(module)
    usage = dict(PRICED_START['message']['usage'], output_tokens=500, speed='standard', service_tier='standard')
    create_message = ad.anthropic_wrapper(lambda client, api_kwargs, **kw: {'id': 'msg-aux', 'model': 'claude-opus-9-5',
                                                                               'usage': usage, 'content': 'PRIVATE'})
    adapted = NS(model='claude-opus-9-5', usage=NS(prompt_tokens=1000, completion_tokens=500))
    def callback(request):
        create_message(None, {'model': request['model']})
        return adapted
    async def async_callback(request):
        return callback(request)
    client = module.AnthropicAuxiliaryClient(object(), 'claude-opus-9-5', 'sk-ant-FAKE', base_url)
    kwargs = {'model': 'claude-opus-9-5', 'messages': ['PRIVATE']}
    if asynchronous:
        import asyncio
        result = asyncio.run(module._relay_async_completion(module.AsyncAnthropicAuxiliaryClient(client), kwargs,
                             provider='anthropic', api_mode='anthropic_messages', create=async_callback))
    else:
        result = module._relay_sync_completion(client, kwargs, provider='anthropic', api_mode='anthropic_messages', create=callback)
    assert result is adapted
    with r.store(str(priced)).db() as c:
        [record] = [json.loads(row[0]) for row in c.execute('SELECT data FROM requests')]
    assert record['source'] == 'auxiliary_adapter' and record['status'] == 'completed'
    assert record['usage']['usage_source'] == 'native_anthropic_usage'
    assert_priced(record, endpoint, '0.039', 'standard')
    assert 'sk-ant-FAKE' not in json.dumps(record) and 'bedrock-runtime' not in json.dumps(record)


def relay_namespace(value):
    return NS(**{k: relay_namespace(v) for k, v in value.items()}) if isinstance(value, dict) else value


def test_signature_drift_is_not_wrapped():
    def changed(metadata): return metadata
    module = NS(_usage_from_metadata=changed)
    ad.install_gemini(module)
    assert module._usage_from_metadata is changed


# --- Relay stream modes through the real core ManagedLlmStream -------------------------
# The whole core agent/relay_llm.py runs unchanged. Only agent.relay_runtime (turn/session
# resolution, leases) and Relay's native stream_execute are fakes; the real
# managed_callback_guard/_is_relay_wrapped_callback_error/_run_on_daemon_thread are loaded.
# Managed: Relay's collector is the real observe_chunk -> on_chunk(_jsonable(chunk)).
# Unmanaged: _start_unmanaged iterates the provider stream and never calls on_chunk.

def sdk_models():
    """Pydantic models shaped like the Anthropic SDK's: optional fields left unset are None."""
    pydantic = pytest.importorskip('pydantic')
    from typing import Literal, Optional
    class Model(pydantic.BaseModel):
        pass
    class CacheCreation(Model):
        ephemeral_5m_input_tokens: int
        ephemeral_1h_input_tokens: int
    class Usage(Model):
        input_tokens: int
        output_tokens: int
        cache_creation_input_tokens: Optional[int] = None
        cache_read_input_tokens: Optional[int] = None
        cache_creation: Optional[CacheCreation] = None
        server_tool_use: Optional[dict] = None
        service_tier: Optional[str] = None
        inference_geo: Optional[str] = None
        speed: Optional[str] = None
    class MessageDeltaUsage(Model):
        output_tokens: int
        input_tokens: Optional[int] = None
        cache_creation_input_tokens: Optional[int] = None
        cache_read_input_tokens: Optional[int] = None
        server_tool_use: Optional[dict] = None
        service_tier: Optional[str] = None
        inference_geo: Optional[str] = None
        speed: Optional[str] = None
    class TextBlock(Model):
        type: Literal['text'] = 'text'
        text: str
        citations: Optional[list] = None
    class Message(Model):
        id: str
        type: Literal['message'] = 'message'
        role: Literal['assistant'] = 'assistant'
        model: str
        content: list[TextBlock]
        stop_reason: Optional[str] = None
        stop_sequence: Optional[str] = None
        usage: Usage
    class Delta(Model):
        stop_reason: Optional[str] = None
        stop_sequence: Optional[str] = None
    class MessageStart(Model):
        type: Literal['message_start'] = 'message_start'
        message: Message
    class MessageDelta(Model):
        type: Literal['message_delta'] = 'message_delta'
        delta: Delta
        usage: MessageDeltaUsage
    class MessageStop(Model):
        type: Literal['message_stop'] = 'message_stop'
    creation = CacheCreation(ephemeral_5m_input_tokens=1000, ephemeral_1h_input_tokens=2000)
    start_usage = Usage(input_tokens=1000, output_tokens=0, cache_read_input_tokens=20000,
                        cache_creation_input_tokens=3000, cache_creation=creation, inference_geo='global')
    events = [MessageStart(message=Message(id='msg-priced', model='claude-opus-9-5', content=[], usage=start_usage)),
              MessageDelta(delta=Delta(stop_reason='end_turn'),
                           usage=MessageDeltaUsage(output_tokens=500, speed='fast', service_tier='standard')),
              MessageStop()]
    final = Message(id='msg-priced', model='claude-opus-9-5', content=[TextBlock(text='PRIVATE')], stop_reason='end_turn',
                    usage=Usage(**dict(start_usage.model_dump(), output_tokens=500, speed='fast', service_tier='standard')))
    # The encoded delta really carries the unset SDK fields as explicit None.
    assert _jsonable_like(events[1])['usage']['cache_read_input_tokens'] is None
    return events, final


def _jsonable_like(model):
    return model.model_dump(mode='json', warnings=False)


class FakeRelayRuntime:
    """Managed-execution host: Relay's native pipeline yields the provider callback's
    JSON chunks, runs the collector on each (post-intercept, identity here) and then
    the finalizer, as nemo_relay.llm.stream_execute documents."""
    def __init__(self):
        self.stream_calls, self.collected = [], []
        self.relay = NS(LLMRequest=lambda headers, body: NS(headers=headers, content=body),
                        llm=NS(stream_execute=self.stream_execute))
    def managed_execution_enabled(self): return True
    def acquire_operation_lease(self): return NS(release=lambda: None)
    async def run_in_session_async(self, session, callback, *args, **kwargs):
        result = callback(*args, **kwargs)
        return await result if __import__('inspect').isawaitable(result) else result
    def stream_execute(self, name, request, func, collector, finalizer, **kwargs):
        self.stream_calls.append(name)
        async def pipeline():
            async for chunk in func(request):
                self.collected.append(chunk)
                collector(chunk)
                yield chunk
            finalizer()
        async def opened(): return pipeline()
        return opened()


@pytest.fixture
def relay_mode(monkeypatch):
    import sys
    def build(managed):
        runtime = FakeRelayRuntime() if managed else None
        rt = ModuleType('agent.relay_runtime')
        vars(rt).update(Any=Any, Callable=__import__('typing').Callable, contextlib=contextlib, threading=threading,
                        _MANAGED_CALLBACK_DEPTH=__import__('contextvars').ContextVar('fixture_depth', default=0))
        definitions('agent/relay_runtime.py', ['managed_callback_guard', '_is_relay_wrapped_callback_error',
                                               '_run_on_daemon_thread'], vars(rt))
        def resolve(session_id):
            if rt._MANAGED_CALLBACK_DEPTH.get() > 0 or runtime is None:
                return None, None, None
            return runtime, NS(session_id=session_id), None
        vars(rt).update(resolve_execution_context=resolve, active_turn=lambda *a: None,
                        RelayTurnContext=object, RelayOperationLease=object, RelayRuntime=FakeRelayRuntime)
        adapter = anthropic_adapter()
        package = ModuleType('agent')
        package.relay_runtime, package.anthropic_adapter = rt, adapter
        for name, module in (('agent', package), ('agent.relay_runtime', rt), ('agent.anthropic_adapter', adapter)):
            monkeypatch.setitem(sys.modules, name, module)
        relay = ModuleType('agent.relay_llm')
        path = CORE / 'agent/relay_llm.py'
        exec(compile(path.read_text(), str(path), 'exec'), vars(relay))  # whole real module
        package.relay_llm = relay
        monkeypatch.setitem(sys.modules, 'agent.relay_llm', relay)
        ad.install_anthropic_accumulator(relay)
        ns = dict(Any=Any, time=time, logger=__import__('logging').getLogger('fixture.helpers'),
                  claim_stream_writer=lambda agent: None, stream_writer_is_current=lambda agent, token: True,
                  EmptyStreamError=RuntimeError)
        definitions('agent/chat_completion_helpers.py', ['_relay_stream_identity', '_relay_stream_metadata'], ns)
        methods = ['_call_anthropic', '_check_anthropic_message', '_mark_finish_seen', '_set_managed_stream',
                   '_close_managed_stream', '_writer_still_current']
        definitions('agent/chat_completion_helpers.py', methods, ns, class_name='_StreamingCall')
        cls = type('_StreamingCall', (), {k: ns[k] for k in methods})
        ad.install_native_main(NS(_StreamingCall=cls))
        return cls, relay, runtime
    monkeypatch.setattr(r, 'ADAPTERS', {})
    return build


def hook_agent(base_url, monkeypatch):
    """Agent carrying core's real _usage_summary_for_api_request_hook (normalize_usage
    included), wrapped by the plugin's existing raw_wrapper exactly as install() does."""
    pricing = ModuleType('agent.usage_pricing')  # registered: @dataclass resolves its module
    monkeypatch.setitem(__import__('sys').modules, pricing.__name__, pricing)
    vars(pricing).update(Any=Any, Optional=__import__('typing').Optional, dataclass=__import__('dataclasses').dataclass,
                         fields=__import__('dataclasses').fields, logger=__import__('logging').getLogger('fixture.usage'))
    usage_ns = definitions('agent/usage_pricing.py', ['CanonicalUsage', '_usage_field', '_first_nonzero',
        '_ANTHROPIC_USAGE_SHAPE', '_CODEX_USAGE_SHAPE', '_CHAT_USAGE_SHAPE', 'normalize_usage'], vars(pricing))
    hooks = definitions('agent/api_request_hooks.py', ['_usage_summary_for_api_request_hook'],
                        dict(Any=Any, Optional=__import__('typing').Optional, Dict=Dict,
                             normalize_usage=usage_ns['normalize_usage']), class_name='ApiRequestHooksMixin')
    Agent = type('AIAgent', (), {'_usage_summary_for_api_request_hook': hooks['_usage_summary_for_api_request_hook']})
    ad.patch(Agent, '_usage_summary_for_api_request_hook', ad.raw_wrapper, ('self', 'response'))
    assert r.ADAPTERS['AIAgent._usage_summary_for_api_request_hook'] == 'installed'
    agent = Agent()
    vars(agent).update(vars(start(base_url=base_url)), base_url=base_url, model='claude-opus-9-5', log_prefix='')
    return agent


def run_mode_stream(cls, agent, events, final):
    class Raw:
        response = None
        def __iter__(self): return iter(events)
        def get_final_message(self): return final
    class Manager:
        exited = False
        def __enter__(self): return Raw()
        def __exit__(self, *args): self.exited = True
    manager = Manager()
    obj = cls()
    obj.agent, obj.api_kwargs, obj.last_chunk_time, obj.managed_stream_holder = agent, {'model': 'claude-opus-9-5', 'messages': []}, {}, {}
    obj._new_diag, obj._quiet, obj._reabort_if_cancelled, obj._count_chunk = (lambda: {}), (lambda fn: None), (lambda response: None), (lambda *a: None)
    result = obj._call_anthropic(NS(messages=NS(stream=lambda **kw: manager)))
    assert manager.exited and obj.managed_stream_holder == {}
    return result


def finish_turn(agent, result):
    """turn_response_intake's post_api_request: the wrapped hook summary, then post."""
    summary = agent._usage_summary_for_api_request_hook(result)
    assert summary['_ai_usage_raw_usage']['cache_creation'] == {'ephemeral_5m_input_tokens': 1000, 'ephemeral_1h_input_tokens': 2000}
    post_hook(agent, getattr(result, 'model', None), usage=summary)


@pytest.mark.parametrize('base_url,endpoint', ENDPOINTS)
def test_relay_managed_stream_event_callback_prices_native_usage(priced, relay_mode, base_url, endpoint, monkeypatch):
    cls, relay, runtime = relay_mode(managed=True)
    observed = []
    wrapped = relay.AnthropicStreamAccumulator.observe  # the plugin-wrapped real observe
    monkeypatch.setattr(relay.AnthropicStreamAccumulator, 'observe', lambda self, event: (observed.append(event), wrapped(self, event))[1])
    events, final = sdk_models()
    agent = hook_agent(base_url, monkeypatch)
    result = run_mode_stream(cls, agent, events, final)
    # Relay drove the provider stream and its collector saw every _jsonable-encoded event,
    # including the SDK's unset None usage fields.
    assert runtime.stream_calls == ['anthropic.messages'] and [c['type'] for c in runtime.collected] == ['message_start', 'message_delta', 'message_stop']
    assert runtime.collected[1]['usage']['cache_read_input_tokens'] is None and runtime.collected[1]['usage']['input_tokens'] is None
    # on_chunk received core _jsonable output (plain dicts), not the SDK objects.
    assert observed == runtime.collected and all(type(e) is dict for e in observed)
    assert result is final  # provider chunks matched: output_modified stays False
    before = rows(priced)['one']
    assert before['usage']['usage_source'] == 'native_anthropic_stream_usage' and before['returned_speed'] == 'fast'
    finish_turn(agent, result)
    record = rows(priced)['one']
    assert record['status'] == 'completed' and record['usage']['usage_source'] == 'native_anthropic_stream_usage'
    assert record['usage']['raw_usage']['cache_creation'] == {'ephemeral_5m_input_tokens': 1000, 'ephemeral_1h_input_tokens': 2000}
    assert_priced(record, endpoint, '0.078', 'fast')


@pytest.mark.parametrize('base_url,endpoint', ENDPOINTS)
def test_relay_unmanaged_stream_prices_final_message_via_raw_wrapper(priced, relay_mode, base_url, endpoint, monkeypatch):
    cls, relay, _ = relay_mode(managed=False)
    observed = []
    wrapped = relay.AnthropicStreamAccumulator.observe
    monkeypatch.setattr(relay.AnthropicStreamAccumulator, 'observe', lambda self, event: (observed.append(event), wrapped(self, event))[1])
    events, final = sdk_models()
    agent = hook_agent(base_url, monkeypatch)
    result = run_mode_stream(cls, agent, events, final)
    assert observed == [] and result is final  # no Relay pipeline, no on_chunk callback
    assert not rows(priced)['one'].get('usage')
    finish_turn(agent, result)
    record = rows(priced)['one']
    assert record['status'] == 'completed' and record['usage']['usage_source'] == 'response_usage'
    assert record['usage']['raw_usage']['cache_creation'] == {'ephemeral_5m_input_tokens': 1000, 'ephemeral_1h_input_tokens': 2000}
    assert_priced(record, endpoint, '0.078', 'fast')

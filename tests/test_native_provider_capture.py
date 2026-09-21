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
    selected = [n for n in body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    assert {n.name for n in selected} == set(names)
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


def start(aid='one', provider='anthropic', mode='anthropic_messages'):
    r.pre(api_request_id=aid, session_id='fixture', turn_id='turn', provider=provider, api_mode=mode, model='fixture', started_at=time.time())
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


@pytest.fixture
def claude(monkeypatch):
    ns = definitions('agent/relay_llm.py', ['AnthropicStreamAccumulator','_jsonable','_jsonable_dict','_namespace'],
        dict(Any=Any, SimpleNamespace=NS, contextlib=contextlib, json=json,
             _ANTHROPIC_APPEND_DELTAS={'text_delta':'text','thinking_delta':'thinking','signature_delta':'signature'}))
    relay = ModuleType('agent.relay_llm')
    relay.AnthropicStreamAccumulator = ns['AnthropicStreamAccumulator']
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
    anthropic_module = ModuleType('agent.anthropic_adapter')
    anthropic_module.sanitize_anthropic_kwargs = lambda *a, **kw: None
    monkeypatch.setitem(__import__('sys').modules, 'agent', agent_module)
    monkeypatch.setitem(__import__('sys').modules, 'agent.relay_llm', relay)
    monkeypatch.setitem(__import__('sys').modules, 'agent.anthropic_adapter', anthropic_module)
    ns = definitions('agent/chat_completion_helpers.py', ['_call_anthropic','_check_anthropic_message'],
        dict(Any=Any, time=time, _relay_stream_identity=lambda *a:{}, _relay_stream_metadata=lambda *a:{},
             claim_stream_writer=lambda a:None, EmptyStreamError=RuntimeError), class_name='_StreamingCall')
    cls = type('_StreamingCall', (), {'_call_anthropic':ns['_call_anthropic'], '_check_anthropic_message':ns['_check_anthropic_message']})
    ad.install_native_main(NS(_StreamingCall=cls))
    return cls, relay


def run_stream(claude, agent, events, failure=None, interrupt=False, during=None):
    cls, _ = claude
    usage = NS(input_tokens=100, output_tokens=12, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    final = NS(id='msg-fixture',model='fixture',usage=usage, content=[NS(type='text')],stop_reason='end_turn')
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


def test_signature_drift_is_not_wrapped():
    def changed(metadata): return metadata
    module = NS(_usage_from_metadata=changed)
    ad.install_gemini(module)
    assert module._usage_from_metadata is changed
